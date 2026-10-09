from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media import media as media_module
from app.media.media import Media
from app.media.recognition import cache


class RecognitionCacheTest(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_keys_are_stable_and_inputs_version_language_and_strict_are_isolated(self):
        common = {"title": "Show", "config_version": "v1", "language": "zh-CN",
                  "strict": False}
        self.assertEqual(cache.cache_key("tmdb", common),
                         cache.cache_key("tmdb", deepcopy(common)))
        for changed in (
                {**common, "title": "Other"},
                {**common, "config_version": "v2"},
                {**common, "language": "en-US"},
                {**common, "strict": True}):
            self.assertNotEqual(cache.cache_key("tmdb", common),
                                cache.cache_key("tmdb", changed))

    def test_layers_are_separate_values_are_copied_and_cache_can_be_disabled(self):
        settings = {"cache": {"enabled": True, "parse_ttl_seconds": 60}}
        with patch.object(cache, "recognition_config", return_value=settings):
            cache.put("parse", "same", {"nested": [1]})
            cache.put("tmdb", "same", {"nested": [2]})
            parsed = cache.get("parse", "same")
            parsed["nested"].append(3)
            self.assertEqual({"nested": [1]}, cache.get("parse", "same"))
            self.assertEqual({"nested": [2]}, cache.get("tmdb", "same"))
            cache.clear("parse")
            self.assertIs(cache.CACHE_MISS, cache.get("parse", "same"))
            self.assertEqual({"nested": [2]}, cache.get("tmdb", "same"))
            settings["cache"]["enabled"] = False
            self.assertIs(cache.CACHE_MISS, cache.get("parse", "same"))
            self.assertFalse(cache.put("parse", "new", {"ok": True}))

    def test_parse_cache_enforces_entry_and_byte_limits_with_lru_eviction(self):
        store = cache._ParseCache()
        settings = {"enabled": True, "parse": {
            "enabled": True, "ttl_seconds": 60, "max_entries": 2,
            "max_bytes": 100, "max_entry_bytes": 60, "singleflight": True,
        }}
        self.assertTrue(store.put("a", {"x": 1}, settings))
        self.assertTrue(store.put("b", {"x": 2}, settings))
        self.assertEqual({"x": 1}, store.get("a", settings))  # a 成为最近使用项
        self.assertTrue(store.put("c", {"x": 3}, settings))
        self.assertIs(cache.CACHE_MISS, store.get("b", settings))
        self.assertEqual({"entries": 2, "bytes": store.info(settings)["bytes"],
                          "generation": store.info(settings)["generation"],
                          "enabled": True, "ttl_seconds": 60, "max_entries": 2,
                          "max_bytes": 100, "max_entry_bytes": 60,
                          "singleflight": True}, store.info(settings))

    def test_parse_cache_evicts_until_total_byte_budget_is_met(self):
        store = cache._ParseCache()
        settings = {"parse": {"enabled": True, "ttl_seconds": 60,
                              "max_entries": 5, "max_bytes": 60,
                              "max_entry_bytes": 40}}
        first = {"text": "a" * 25}
        second = {"text": "b" * 25}
        self.assertTrue(store.put("first", first, settings))
        self.assertTrue(store.put("second", second, settings))
        self.assertLessEqual(store.info(settings)["bytes"], 60)
        self.assertEqual({"text": "b" * 25}, store.get("second", settings))
        self.assertIs(cache.CACHE_MISS, store.get("first", settings))

    def test_parse_cache_rejects_oversized_entry_and_respects_ttl(self):
        store = cache._ParseCache()
        settings = {"parse": {"enabled": True, "ttl_seconds": 1,
                              "max_entries": 2, "max_bytes": 100,
                              "max_entry_bytes": 10}}
        self.assertFalse(store.put("large", {"text": "too large"}, settings))
        self.assertIs(cache.CACHE_MISS, store.get("large", settings))
        now = [10]
        with patch("app.media.recognition.cache.time.time", side_effect=lambda: now[0]):
            self.assertTrue(store.put("short", {"x": 1}, settings))
            self.assertEqual({"x": 1}, store.get("short", settings))
            now[0] = 12
            self.assertIs(cache.CACHE_MISS, store.get("short", settings))

    def test_clear_and_configuration_change_invalidate_old_writer_generation(self):
        store = cache._ParseCache()
        settings = {"parse": {"enabled": True, "ttl_seconds": 60}}
        generation = store.generation(settings)
        store.clear(settings)
        self.assertGreater(store.generation(settings), generation)
        self.assertFalse(store.put("stale", {"x": 1}, settings, generation=generation))
        current_generation = store.generation(settings)
        updated = {"parse": {"enabled": True, "ttl_seconds": 120}}
        self.assertGreater(store.generation(updated), current_generation)
        self.assertFalse(store.put("old-config", {"x": 2}, updated,
                                   generation=current_generation))

    def test_parse_cache_ttl_migrates_from_legacy_setting(self):
        store = cache._ParseCache()
        self.assertEqual(37, store.info({"parse_ttl_seconds": 37})["ttl_seconds"])
        self.assertEqual(42, store.info({"parse_ttl_seconds": 37,
                                         "parse": {"ttl_seconds": 42}})["ttl_seconds"])

    def test_title_evidence_hit_records_action_and_skips_alias_query(self):
        media = object.__new__(Media)
        aliases = Mock(return_value=(None, ["The Example"]))
        recorder = SimpleNamespace(context={"recognition_config_version": "v1"}, action=Mock())
        config = {"recognition": {"decision": {"title_evidence": {
            "name_sources": ["primary", "alternative"],
            "normalization": ["nfkc", "casefold", "separators", "whitespace"],
        }}}}
        with patch.object(media, "_Media__search_tmdb_allnames", aliases), \
                patch.object(media_module, "recognition_config", return_value=config), \
                patch.object(media_module, "current_recorder", return_value=recorder), \
                patch.object(cache, "recognition_config", return_value=config):
            first = media._Media__tmdb_title_evidence(
                "The Example S01E01", {"id": 12, "media_type": "tv", "name": "Other"})
            second = media._Media__tmdb_title_evidence(
                "The Example S01E01", {"id": 12, "media_type": "tv", "name": "Other"})

        self.assertEqual(first, second)
        self.assertEqual("strong", second["level"])
        aliases.assert_called_once()
        recorder.action.assert_called_once()
        self.assertEqual("decision", recorder.action.call_args.kwargs["input"]["layer"])

    def test_recognition_request_does_not_reuse_a_cached_final_tmdb_winner(self):
        media = object.__new__(Media)
        media._rmt_match_mode = "normal"
        media._search_tmdbweb = False
        media.meta = SimpleNamespace(get_meta_data_by_key=Mock(
            side_effect=AssertionError("final winner cache must be bypassed")))
        meta_info = SimpleNamespace(
            type=media_module.MediaType.MOVIE, year="2024", begin_season=None,
            get_name=Mock(return_value="Example"), set_tmdb_info=Mock())
        recorder = SimpleNamespace(context={"recognition_config_version": "v1"}, action=Mock())
        config = {"recognition": {}, "laboratory": {"search_tmdbweb": False}}
        with patch.object(media, "_Media__make_cache_key", return_value="base"), \
                patch.object(media, "_Media__search_tmdb", return_value=None), \
                patch.object(media, "_Media__search_multi_tmdb", return_value=None), \
                patch.object(media, "_Media__search_by_title_aliases", return_value=None), \
                patch.object(media, "_Media__search_fallback", return_value=None), \
                patch.object(media_module, "recognition_config", side_effect=lambda section=None:
                             config.get(section, config)), \
                patch.object(media_module, "current_recorder", return_value=recorder), \
                patch.object(cache, "recognition_config", return_value=config):
            result = media._Media__search_meta_tmdb(meta_info, cache=True)

        self.assertIsNone(result)
        media.meta.get_meta_data_by_key.assert_not_called()
        meta_info.set_tmdb_info.assert_called_once_with(None)
