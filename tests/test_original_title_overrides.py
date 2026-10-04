from unittest import TestCase
from unittest.mock import Mock, patch

from app.media import media as media_module
from app.media.media import Media
from app.utils.types import MediaType


class FakeMetaInfo:
    def __init__(self, name, media_type=MediaType.TV):
        self.org_string = name
        self.cn_name = None
        self.en_name = name
        self.year = None
        self.type = media_type
        self.begin_season = None
        self.tmdb_info = None
        self.recognition_source = "local_rules"

    def get_name(self):
        return self.cn_name or self.en_name

    def set_tmdb_info(self, value):
        self.tmdb_info = value


class OriginalTitleOverridesTest(TestCase):
    def test_missing_tmdb_client_is_recorded_with_specific_failure_reason(self):
        media = object.__new__(Media)
        media.tmdb = None
        database = Mock()
        database.insert_recognition_record.side_effect = lambda record: record["request_id"]

        with patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            result = media.get_media_info_original_title(
                title="Requested show", name="Requested show")

        self.assertIsNone(result)
        record = database.insert_recognition_record.call_args.args[0]
        self.assertEqual("failed", record["overall_result"]["status"])
        self.assertEqual("tmdb_unavailable", record["overall_result"]["reason"])

    def test_ai_path_keeps_parse_input_and_applies_explicit_lookup_overrides(self):
        media = object.__new__(Media)
        media.tmdb = object()
        media._ai_inference = True
        media._ai_inference_url = "http://ai.local"
        media._rmt_match_mode = media_module.MatchMode.NORMAL
        media._search_tmdbweb = False
        parsed_local = FakeMetaInfo("Parser guessed title")
        parsed_ai = FakeMetaInfo("AI guessed title")
        tmdb_result = {"id": 88, "media_type": MediaType.TV, "name": "Requested show",
                       "genres": ["Drama"]}
        resolver_calls = []
        database = Mock()
        database.insert_recognition_record.side_effect = lambda record: record["request_id"]
        config = {
            "laboratory": {"ai_inference": True, "ai_inference_url": "http://ai.local"},
            "recognition": {"execution": {"total_timeout_seconds": 30},
                            "decision": {"strategy": "legacy"},
                            "providers": {"anitopy_ml": {"enabled": True}}},
        }

        def search_meta(meta_info, **kwargs):
            resolver_calls.append((meta_info, kwargs))
            return tmdb_result

        with patch("app.media.media.MetaInfo", return_value=parsed_local), \
                patch.object(media, "_Media__request_ai_parse", return_value={"extracted": {"title": "AI"}}) as ai_parse, \
                patch.object(media, "_Media__meta_from_ai_result", return_value=parsed_ai), \
                patch.object(media, "_Media__search_meta_tmdb", side_effect=search_meta), \
                patch.object(media, "_Media__meta_snapshot", side_effect=lambda item: {
                    "name": item.get_name(), "year": item.year}), \
                patch.object(media, "_Media__json_safe", side_effect=lambda value: value), \
                patch.object(media, "_Media__recognition_provider_enabled", return_value=True), \
                patch.object(media, "_Media__has_additional_recognizer", return_value=False), \
                patch.object(media_module.registry, "discover", return_value={}), \
                patch("app.media.media.recognition_config", side_effect=lambda section=None: config.get(section, config)), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            result = media.get_media_info_original_title(
                title="Requested show 2022 S02", name="Requested show", year=2022,
                season=2, mtype=MediaType.TV, strict=True, cache=False)

        self.assertEqual("Requested show 2022 S02", ai_parse.call_args.args[0])
        self.assertEqual(2, len(resolver_calls))
        for query_meta, options in resolver_calls:
            self.assertEqual("Requested show", query_meta.get_name())
            self.assertEqual("2022", query_meta.year)
            self.assertEqual(2, query_meta.begin_season)
            self.assertEqual(MediaType.TV, query_meta.type)
            self.assertTrue(options["strict"])
        self.assertIsNone(parsed_local.year)
        self.assertIsNone(parsed_local.begin_season)
        self.assertEqual(tmdb_result, result.tmdb_info)
        record = database.insert_recognition_record.call_args.args[0]
        self.assertEqual("Requested show 2022 S02", record["context"]["title"])
        self.assertEqual("Requested show", record["context"]["name"])
        self.assertEqual(2022, record["context"]["year"])
        self.assertEqual(2, record["context"]["season"])
        override_action = next(action for action in record["actions"]
                               if action["action_type"] == "resolution_overrides")
        self.assertEqual(2022, override_action["input"]["year"])

    def test_original_title_resolve_reuses_supplied_local_parse(self):
        media = object.__new__(Media)
        media.tmdb = object()
        parsed = FakeMetaInfo("Local parser title")
        resolved = FakeMetaInfo("Requested show")
        config = {
            "recognition": {"providers": {"anitopy_ml": {
                "enabled": True, "endpoint": "http://ai.local"}}},
            "laboratory": {"ai_inference": True, "ai_inference_url": "http://ai.local"},
        }
        database = Mock()
        database.insert_recognition_record.side_effect = lambda record: record["request_id"]
        with patch("app.media.media.recognition_config",
                   side_effect=lambda section=None: config.get(section, config)), \
                patch.object(media, "_Media__recognition_provider_enabled", return_value=True), \
                patch.object(media, "_Media__has_additional_recognizer", return_value=False), \
                patch.object(media, "_Media__get_media_info_with_providers",
                             return_value=resolved) as resolve, \
                patch.object(media, "_Media__meta_snapshot", return_value={"name": "Requested show"}), \
                patch.object(media, "_Media__json_safe", side_effect=lambda value: value), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            result = media.get_media_info_original_title(
                title="Requested show S01", name="Requested show", pre_parsed=parsed)

        self.assertIs(resolved, result)
        self.assertIs(parsed, resolve.call_args.kwargs["pre_parsed"])
