# -*- coding: utf-8 -*-
import unittest
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.media import media as media_module
from app.media.media import Media
from app.media.recognition import cache as recognition_cache
from app.media.recognition.records import recognition_scope
from app.utils.types import MediaType


class ForcedRecognitionTraceTest(unittest.TestCase):
    def test_forced_tmdb_retry_clears_only_tmdb_cache_and_keeps_ai_parse_cache(self):
        meta_info = SimpleNamespace(
            get_name=lambda: "Example Show", year="2024", type=MediaType.TV,
            begin_season=1,
        )
        media = object.__new__(Media)
        media.meta = Mock()
        media._rmt_match_mode = media_module.MatchMode.NORMAL
        ai_value = {"provider_id": "anitopy_ml", "parsed": {"title": "Example Show"}}
        runtime = {"cache": {"enabled": True, "parse": {
            "enabled": True, "ttl_seconds": 60, "max_entries": 8,
            "max_bytes": 4096, "max_entry_bytes": 1024,
        }}}
        tmdb_result = {"id": 77, "media_type": "tv", "name": "Example Show",
                       "genres": ["Drama"]}

        with patch("app.media.recognition.cache.recognition_config",
                   return_value=runtime), \
                patch.object(media, "_Media__make_cache_key", return_value="tmdb-key"), \
                patch.object(media, "_Media__search_tmdb", return_value=tmdb_result), \
                patch.object(media, "_Media__insert_media_cache"), \
                patch("app.media.recognition.records._write_spool", return_value=None):
            recognition_cache.clear("parse")
            recognition_cache.put("parse", "ai-key", ai_value)
            result = media._search_media_info_force_impl(meta_info)
            cached_ai = recognition_cache.get("parse", "ai-key")

        self.assertEqual(tmdb_result, result)
        media.meta.delete_meta_data.assert_called_once_with("tmdb-key")
        self.assertEqual(ai_value, cached_ai)

    def test_forced_retry_skips_legacy_sleep_when_deadline_cannot_fit_retry(self):
        media = object.__new__(Media)
        media.meta = Mock()
        media._rmt_match_mode = media_module.MatchMode.NORMAL
        meta_info = SimpleNamespace(
            get_name=lambda: "Example Show", year="2024", type=MediaType.TV,
            begin_season=1,
        )
        with patch.object(media, "_Media__make_cache_key", return_value="example-cache"), \
                patch.object(media, "_Media__search_tmdb", return_value=None) as search, \
                patch.object(media, "_Media__insert_media_cache"), \
                patch("app.media.media.time.sleep") as sleep, \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper"):
            with recognition_scope("Example.Show.S01E01", source="forced-timeout") as recorder:
                recorder.deadline_monotonic = time.monotonic() + 0.5
                result = media._search_media_info_force_impl(meta_info)

        self.assertIsNone(result)
        search.assert_called_once()
        sleep.assert_not_called()

    def test_batch_files_without_tmdb_still_persist_one_failure_per_file(self):
        media = object.__new__(Media)
        media.tmdb = None
        files = ["/media/Show/episode-1.mkv", "/media/Show/episode-2.mkv"]
        database = Mock()
        database.insert_recognition_record.side_effect = lambda payload: payload["request_id"]

        with patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            result = media.get_media_info_on_files(files)

        self.assertEqual({}, result)
        self.assertEqual(2, database.insert_recognition_record.call_count)
        first, second = [call.args[0] for call in database.insert_recognition_record.call_args_list]
        self.assertEqual("episode-1.mkv", first["original_name"])
        self.assertEqual("episode-2.mkv", second["original_name"])
        self.assertEqual("tmdb_unavailable", first["overall_result"]["reason"])
        self.assertEqual("tmdb_unavailable", second["overall_result"]["reason"])
        self.assertEqual(first["context"]["batch_id"], second["context"]["batch_id"])

    def test_file_exception_is_saved_as_specific_failure_action(self):
        media = object.__new__(Media)
        media.tmdb = object()
        media._ai_inference = False
        media._ai_inference_url = None
        database = Mock()
        database.insert_recognition_record.side_effect = lambda payload: payload["request_id"]
        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "Broken.Show.S01E01.mkv")
            with open(file_path, "wb"):
                pass
            with patch("app.media.recognition.records._write_spool", return_value=None), \
                    patch("app.media.media.recognition_config", return_value={"ai_inference": False}), \
                    patch("app.media.media.MetaInfo", side_effect=RuntimeError("parse exploded")), \
                    patch.object(media, "_Media__has_additional_recognizer", return_value=False), \
                    patch("app.helper.db_helper.DbHelper", return_value=database):
                result = media.get_media_info_on_files(file_path)

        self.assertEqual({}, result)
        payload = database.insert_recognition_record.call_args.args[0]
        self.assertEqual("failed", payload["overall_result"]["status"])
        self.assertEqual("file_resolution_error", payload["overall_result"]["reason"])
        error_action = next(action for action in payload["actions"]
                            if action["action_type"] == "file_resolution_error")
        self.assertEqual("parse exploded", error_action["output"]["error"])

    def test_file_resolution_request_id_is_carried_into_media_object(self):
        media = object.__new__(Media)
        media.tmdb = object()
        file_path = "/tmp/Show.mkv"
        meta_info = SimpleNamespace(tmdb_info={"id": 77})
        database = Mock()
        database.insert_recognition_record.side_effect = lambda payload: payload["request_id"]

        with patch.object(Media, "_get_media_info_on_files_impl", return_value={file_path: meta_info}), \
                patch.object(Media, "_Media__meta_snapshot", return_value={"name": "Show"}), \
                patch.object(Media, "_Media__json_safe", side_effect=lambda value: value), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            result = media.get_media_info_on_files(file_path)

        self.assertIs(result[file_path], meta_info)
        parent_id = database.insert_recognition_record.call_args.args[0]["request_id"]
        self.assertEqual(parent_id, meta_info.recognition_request_id)

    def test_forced_search_persists_action_and_tmdb_result(self):
        media = object.__new__(Media)
        meta_info = SimpleNamespace(
            org_string="Original.Release.Name",
            get_name=lambda: "Original Name",
            year="2020",
            type=MediaType.TV,
            begin_season=1,
            recognition_source="anitopy_ml",
            recognition_request_id="file-request-1",
        )
        tmdb_result = {"id": 77, "media_type": "tv", "name": "Original Name"}
        database = Mock()
        database.insert_recognition_record.side_effect = lambda payload: payload["request_id"]

        with patch.object(Media, "_search_media_info_force_impl", return_value=tmdb_result), \
                patch.object(Media, "_Media__meta_snapshot", return_value={"name": "Original Name"}), \
                patch.object(Media, "_Media__json_safe", side_effect=lambda value: value), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            result = media.search_media_info_force(meta_info)

        self.assertEqual(tmdb_result, result)
        payload = database.insert_recognition_record.call_args.args[0]
        self.assertEqual("Original.Release.Name", payload["original_name"])
        self.assertEqual("media.search_media_info_force", payload["source"])
        self.assertEqual("Original Name", payload["context"]["parsed_name"])
        self.assertEqual("file-request-1", payload["context"]["parent_request_id"])
        self.assertEqual("success", payload["overall_result"]["status"])
        self.assertEqual(tmdb_result, payload["overall_result"]["tmdb_result"])
        self.assertIn("forced_search", [item["action_type"] for item in payload["actions"]])


if __name__ == "__main__":
    unittest.main()
