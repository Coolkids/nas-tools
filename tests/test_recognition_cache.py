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
            settings["cache"]["enabled"] = False
            self.assertIs(cache.CACHE_MISS, cache.get("parse", "same"))
            self.assertFalse(cache.put("parse", "new", {"ok": True}))

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
