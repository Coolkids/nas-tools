from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.metainfo import _infer_recognition_source
from app.media.recognition.records import RecognitionRecorder, recognition_scope


class RecognitionStagesTest(TestCase):
    def test_parse_only_record_is_successful_parse_without_tmdb_request(self):
        config = SimpleNamespace(
            get_config=lambda section=None: ({"recognition": {}} if section is None else {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-stage-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db):
            parsed = MetaInfo("Example Show S01E02 2024 1080p")

        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.get_name())
        saved = db.insert_recognition_record.call_args.args[0]
        self.assertEqual("parse_only", saved["stage"])
        self.assertEqual("success", saved["overall_result"]["status"])
        self.assertEqual("not_requested", saved["overall_result"]["tmdb_status"])
        self.assertEqual("unknown", saved["source"])
        self.assertEqual("tests.test_recognition_stages", saved["context"]["caller_module"])
        self.assertEqual([], saved["tmdb_results"])
        self.assertNotIn("tmdb_query", [action["action_type"] for action in saved["actions"]])

    def test_direct_parse_source_is_inferred_from_business_module(self):
        cases = {
            "app.indexer.client._base": "indexer",
            "app.rss": "rss",
            "app.rsschecker": "rss",
            "app.downloader.downloader": "downloader",
            "app.subscribe": "subscribe",
            "app.media.douban": "douban",
            "app.doubansync": "douban_sync",
            "app.filetransfer": "file_transfer",
            "web.backend.web_utils": "web",
            "app.media.media": "media_internal",
            "tests.example": "unknown",
        }

        for module_name, expected in cases.items():
            with self.subTest(module=module_name):
                self.assertEqual(expected, _infer_recognition_source(module_name))

    def test_media_resolve_records_business_caller_as_source(self):
        config = SimpleNamespace(
            get_config=lambda section=None: ({"recognition": {}} if section is None else {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-source-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]
        media = Media.__new__(Media)
        media.tmdb = object()
        media._get_media_info_impl = Mock(return_value=None)
        namespace = {"__name__": "app.rss"}
        exec("def resolve(media):\n    return media.get_media_info('Example 2024')", namespace)

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db):
            namespace["resolve"](media)

        saved = db.insert_recognition_record.call_args.args[0]
        self.assertEqual("rss", saved["source"])
        self.assertEqual("app.rss", saved["context"]["caller_module"])

    def test_all_public_resolve_entrypoints_keep_business_source(self):
        cases = {
            "app.indexer.client._base": "indexer",
            "app.rss": "rss",
            "app.rsschecker": "rss",
            "app.downloader.downloader": "downloader",
            "app.subscribe": "subscribe",
            "app.media.douban": "douban",
            "app.doubansync": "douban_sync",
            "app.filetransfer": "file_transfer",
            "web.backend.web_utils": "web",
            "app.media.media": "media_internal",
        }
        database = Mock()
        database.insert_recognition_record.side_effect = lambda record: record["request_id"]
        media = Media.__new__(Media)
        media.tmdb = object()
        media._get_media_info_impl = Mock(return_value=None)
        media._get_media_info_original_title_impl = Mock(return_value=None)
        media._get_media_info_on_files_impl = Mock(return_value={})
        media._search_media_info_force_impl = Mock(return_value=None)
        media._Media__meta_snapshot = Mock(return_value={"name": "Example"})
        media._Media__json_safe = lambda value: value
        meta = SimpleNamespace(
            org_string="Example.Release.Name", get_name=lambda: "Example",
            year="2024", type=None, begin_season=None, recognition_source="local_rules",
            recognition_request_id="parent-request",
        )

        with patch("app.media.recognition.records._runtime_config_snapshot",
                   return_value={"recognition": {}, "laboratory": {}}), \
                patch("app.media.recognition.records._recognition_config_snapshot",
                      return_value=("test-version", {"recognition": {}, "runtime": {}})), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            for module_name in cases:
                namespace = {"__name__": module_name}
                exec("""
def resolve_all(media, meta):
    media.get_media_info('Example 2024')
    media.get_media_info_original_title('Example 2024', name='Example', year=2024)
    media.get_media_info_on_files('/tmp/Example.mkv')
    media.search_media_info_force(meta)
    MetaInfo('Example Show S01E01 2024 1080p')
""", namespace)
                namespace["MetaInfo"] = MetaInfo
                namespace["resolve_all"](media, meta)

        records = [call.args[0] for call in database.insert_recognition_record.call_args_list]
        self.assertEqual(len(cases) * 5, len(records))
        for offset, (module_name, expected_source) in enumerate(cases.items()):
            with self.subTest(module=module_name):
                group = records[offset * 5:(offset + 1) * 5]
                self.assertEqual([expected_source] * 5, [record["source"] for record in group])
                self.assertEqual([module_name] * 4,
                                 [record["context"]["caller_module"] for record in group[:4]])
                self.assertEqual(module_name, group[4]["context"]["caller_module"])
                self.assertEqual("parse_only", group[4]["stage"])

    def test_tmdb_no_result_is_distinct_from_parse_only_not_requested(self):
        recorder = RecognitionRecorder("Example", stage="resolve")
        recorder.add_provider_result("local_rules", "success", normalized_result={"name": "Example"})
        tmdb_call = recorder.add_tmdb_result("local_rules", {"query": "Example"}, None)
        recorder.set_overall("failed", reason="no_tmdb_match", parsed_result={"name": "Example"},
                             selected_provider="local_rules")

        self.assertEqual("no_result", tmdb_call["status"])
        self.assertEqual("no_tmdb_match", recorder.overall_result["reason"])
        self.assertNotIn("tmdb_status", recorder.overall_result)
        self.assertEqual([tmdb_call], recorder.provider_results[0]["tmdb_results"])

    def test_exception_preserves_partial_parse_and_tmdb_trace(self):
        config = SimpleNamespace(
            get_config=lambda section=None: ({"recognition": {}} if section is None else {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-exception-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db):
            with self.assertRaisesRegex(RuntimeError, "later processing failed"):
                with recognition_scope("Example 2024", source="web", stage="resolve") as recorder:
                    recorder.add_provider_result(
                        "local_rules", "success", normalized_result={"name": "Example", "year": 2024})
                    recorder.add_tmdb_result(
                        "local_rules", {"query": "Example"}, {"id": 42, "title": "Example"})
                    recorder.set_overall(
                        "success", parsed_result={"name": "Example", "year": 2024},
                        selected_provider="local_rules", tmdb_result={"id": 42, "title": "Example"})
                    raise RuntimeError("later processing failed")

        saved = db.insert_recognition_record.call_args.args[0]
        self.assertEqual("failed", saved["overall_result"]["status"])
        self.assertEqual("later processing failed", saved["overall_result"]["reason"])
        self.assertEqual({"name": "Example", "year": 2024},
                         saved["overall_result"]["parsed_result"])
        self.assertEqual({"id": 42, "title": "Example"}, saved["overall_result"]["tmdb_result"])
        self.assertEqual("error", saved["overall_result"]["lifecycle"])
        self.assertEqual("success", saved["provider_results"][0]["status"])
        self.assertEqual("success", saved["tmdb_results"][0]["status"])
        self.assertIn("request_error", [action["action_type"] for action in saved["actions"]])
