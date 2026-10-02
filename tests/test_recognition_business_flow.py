import os
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.recognition import cache as recognition_cache
from app.utils.types import MatchMode, MediaType


class _SearchFixture:
    total_results = 1

    def __init__(self, candidate):
        self.candidate = candidate
        self.queries = []

    def tv_shows(self, params):
        self.queries.append(dict(params))
        return [dict(self.candidate)]


class RecognitionBusinessFlowTest(TestCase):
    def test_file_transfer_runs_real_parse_tmdb_match_assembly_and_recording(self):
        candidate = {
            "id": 481,
            "name": "Example Show",
            "original_name": "Example Show",
            "first_air_date": "2021-05-12",
            "media_type": "tv",
            "genres": [{"id": 18, "name": "Drama"}],
        }
        search = _SearchFixture(candidate)
        media = object.__new__(Media)
        media.tmdb = object()
        media.search = search
        media._tmdb_clients = []
        media._rmt_match_mode = MatchMode.NORMAL
        media._ai_inference = False
        media._ai_inference_url = None
        media._search_tmdbweb = False
        media.meta = Mock()
        # A prior failed parse may have left a negative legacy cache entry.
        # File transfer must still run the current recorded resolution flow.
        media.meta.get_meta_data_by_key.return_value = {"id": 0}

        database = Mock()
        database.insert_recognition_record.side_effect = lambda record: record["request_id"]
        runtime_config = {
            "recognition": {"execution": {"total_timeout_seconds": 10},
                            "decision": {"strategy": "legacy"}},
            "laboratory": {"ai_inference": False},
        }

        with tempfile.TemporaryDirectory() as directory:
            file_path = os.path.join(directory, "Example Show S01E01 2024 1080p.mkv")
            with open(file_path, "wb"):
                pass

            caller = {"__name__": "app.filetransfer"}
            exec("def transfer(media, path):\n    return media.get_media_info_on_files(path)", caller)
            with patch("app.media.recognition.records._runtime_config_snapshot",
                       return_value=runtime_config), \
                    patch("app.media.recognition.records._recognition_config_snapshot",
                          return_value=("business-flow-test", {"recognition": {}, "runtime": {}})), \
                    patch("app.media.recognition.records._write_spool", return_value=None), \
                    patch("app.helper.db_helper.DbHelper", return_value=database), \
                    patch.object(media, "_Media__insert_media_cache"), \
                    patch.object(media, "_Media__has_additional_recognizer", return_value=False), \
                    patch.object(recognition_cache, "get", return_value=recognition_cache.CACHE_MISS), \
                    patch.object(recognition_cache, "put", return_value=None):
                results = caller["transfer"](media, file_path)

        self.assertIn(file_path, results)
        parsed_media = results[file_path]
        self.assertEqual("Example Show", parsed_media.get_name())
        self.assertEqual(1, parsed_media.begin_season)
        self.assertEqual(1, parsed_media.begin_episode)
        self.assertEqual(481, parsed_media.tmdb_id)
        self.assertEqual(MediaType.TV, parsed_media.type)
        self.assertEqual([{"query": "Example Show"}], search.queries)

        self.assertEqual(1, database.insert_recognition_record.call_count)
        record = database.insert_recognition_record.call_args.args[0]
        self.assertEqual("Example Show S01E01 2024 1080p.mkv", record["original_name"])
        self.assertEqual("file_transfer", record["source"])
        self.assertEqual("app.filetransfer", record["context"]["caller_module"])
        self.assertEqual("success", record["overall_result"]["status"])
        self.assertEqual(481, record["overall_result"]["tmdb_result"]["id"])
        self.assertEqual("Example Show", record["overall_result"]["parsed_result"]["name"])
        self.assertEqual("success", record["provider_results"][0]["status"])
        self.assertEqual("local_rules", record["provider_results"][0]["provider_id"])
        self.assertEqual("success", record["tmdb_results"][0]["status"])
        self.assertEqual("Example Show", record["tmdb_results"][0]["result"]["name"])
        self.assertIn("preprocess", [action["action_type"] for action in record["actions"]])
        self.assertIn("provider_parse", [action["action_type"] for action in record["actions"]])
        self.assertIn("tmdb_query", [action["action_type"] for action in record["actions"]])
        self.assertIn("decision", [action["action_type"] for action in record["actions"]])
        self.assertEqual(record["request_id"], parsed_media.recognition_request_id)
