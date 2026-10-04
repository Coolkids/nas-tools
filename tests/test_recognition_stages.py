import json
import uuid
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.metainfo import _infer_recognition_source
from app.media.recognition.contracts import ParseResult
from app.media.recognition.records import current_recorder
from app.media.recognition.records import RecognitionRecorder, recognition_scope
from app.media.recognition.records import (
    annotate_business_short_circuit,
    record_deferred_parse_result,
)
from app.db.models import RECOGNITIONATTEMPT, RECOGNITIONREQUEST
from app.helper.db_helper import DbHelper


class RecognitionStagesTest(TestCase):
    def test_parse_only_profile_can_block_ai_network_requests(self):
        runtime = {
            "recognition": {
                "profiles": {"parse_only": {"network_allowed": False}},
                "providers": {"anitopy_ml": {
                    "enabled": True, "endpoint": "http://ai.local",
                }},
            },
            "laboratory": {"ai_inference": False},
        }
        config = SimpleNamespace(
            get_config=lambda section=None: runtime if section is None else runtime.get(section, {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-profile-network-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db), \
                patch("app.media.meta.metainfo.recognition_service.run_provider") as run_provider:
            parsed = MetaInfo("Example Show S01E02 2024 1080p")

        self.assertEqual("Example Show", parsed.get_name())
        run_provider.assert_not_called()
        saved = db.insert_recognition_record.call_args.args[0]
        ai_attempt = next(item for item in saved["provider_results"]
                          if item["provider_id"] == "anitopy_ml")
        self.assertEqual("skipped", ai_attempt["status"])
        self.assertEqual("network_disallowed_by_profile", ai_attempt["error"])

    def test_resolve_profile_can_disable_tmdb_and_network(self):
        runtime = {
            "recognition": {
                "profiles": {"resolve": {
                    "network_allowed": False, "tmdb_allowed": False,
                }},
                "providers": {"anitopy_ml": {
                    "enabled": True, "endpoint": "http://ai.local",
                }},
            },
            "laboratory": {"ai_inference": False, "ai_inference_url": ""},
        }
        config = SimpleNamespace(
            get_config=lambda section=None: runtime if section is None else runtime.get(section, {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-profile-resolve-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]
        media = Media.__new__(Media)
        media.tmdb = None

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db), \
                patch("app.media.meta.metainfo.recognition_service.run_provider") as run_provider:
            parsed = media.get_media_info("Example Show S01E02 2024 1080p")

        self.assertIsNotNone(parsed)
        self.assertEqual("Example Show", parsed.get_name())
        self.assertFalse(parsed.tmdb_info)
        run_provider.assert_not_called()
        saved = db.insert_recognition_record.call_args.args[0]
        self.assertEqual("skipped", saved["overall_result"]["status"])
        self.assertEqual("tmdb_disallowed_by_profile", saved["overall_result"]["reason"])
        self.assertEqual("not_requested", saved["overall_result"]["tmdb_status"])
        self.assertEqual([], saved["tmdb_results"])
        ai_attempt = next(item for item in saved["provider_results"]
                          if item["provider_id"] == "anitopy_ml")
        self.assertEqual("network_disallowed_by_profile", ai_attempt["error"])

    def test_parse_only_keeps_local_result_when_ai_is_disabled(self):
        runtime = {
            "recognition": {"providers": {"anitopy_ml": {
                "enabled": False, "endpoint": "http://ai.local",
            }}},
            "laboratory": {"ai_inference": False,
                           "ai_inference_url": "http://ai.local"},
        }
        config = SimpleNamespace(
            get_config=lambda section=None: runtime if section is None else runtime.get(section, {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-ai-disabled-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db), \
                patch("app.media.meta.metainfo.recognition_service.run_provider") as run_provider:
            parsed = MetaInfo("Example Show S01E02 2024 1080p")

        run_provider.assert_not_called()
        self.assertEqual("Example Show", parsed.get_name())
        saved = db.insert_recognition_record.call_args.args[0]
        ai_result = next(item for item in saved["provider_results"]
                         if item["provider_id"] == "anitopy_ml")
        self.assertEqual("skipped", ai_result["status"])
        self.assertEqual("anitopy_ml_provider_disabled", ai_result["error"])
        self.assertEqual("success", saved["overall_result"]["status"])
        self.assertEqual("not_requested", saved["overall_result"]["tmdb_status"])
        self.assertEqual([], saved["tmdb_results"])

    def test_parse_only_ai_timeout_preserves_local_result_and_never_queries_tmdb(self):
        runtime = {
            "recognition": {"providers": {"anitopy_ml": {
                "enabled": True, "endpoint": "http://ai.local",
            }}},
            # 新配置中的 provider 开关和地址独立生效，不要求旧实验室开关开启。
            "laboratory": {"ai_inference": False,
                           "ai_inference_url": ""},
        }
        config = SimpleNamespace(
            get_config=lambda section=None: runtime if section is None else runtime.get(section, {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-ai-timeout-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]

        def timeout(provider_id, request, **_kwargs):
            current_recorder().add_provider_result(
                provider_id, "timeout", input={"title": request.title},
                error="request_timeout")
            return ParseResult(provider_id, "timeout", error="request_timeout")

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db), \
                patch("app.media.meta.metainfo.recognition_service.run_provider",
                      side_effect=timeout) as run_provider:
            parsed = MetaInfo("Example Show S01E02 2024 1080p")

        run_provider.assert_called_once()
        self.assertEqual("Example Show", parsed.get_name())
        saved = db.insert_recognition_record.call_args.args[0]
        ai_result = next(item for item in saved["provider_results"]
                         if item["provider_id"] == "anitopy_ml")
        self.assertEqual("timeout", ai_result["status"])
        self.assertEqual("request_timeout", ai_result["error"])
        self.assertEqual("success", saved["overall_result"]["status"])
        self.assertEqual("not_requested", saved["overall_result"]["tmdb_status"])
        self.assertEqual([], saved["tmdb_results"])
        self.assertNotIn("tmdb_query", [action["action_type"] for action in saved["actions"]])

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

    def test_branch_short_circuit_is_appended_to_the_original_parse_record(self):
        config = SimpleNamespace(
            get_config=lambda section=None: ({"recognition": {}} if section is None else {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-short-circuit-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]
        db.append_recognition_business_action.return_value = True

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db):
            parsed = MetaInfo("Example Show S01E02 2024 1080p", include_ai=False,
                              ai_skip_reason="ai_deferred_to_indexer_filter_or_resolution")
            request_id = parsed.recognition_request_id
            self.assertTrue(annotate_business_short_circuit(
                parsed, "prefilter_rejected", "indexer_filter_rejected",
                {"filter_message": "wrong type"}))

        self.assertEqual(1, db.insert_recognition_record.call_count)
        db.append_recognition_business_action.assert_called_once_with(
            request_id, "prefilter_rejected", "indexer_filter_rejected",
            {"filter_message": "wrong type"}, "skipped")

    def test_deferred_local_parse_is_persisted_once_after_business_branch(self):
        config = SimpleNamespace(
            get_config=lambda section=None: ({"recognition": {}} if section is None else {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-deferred-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db):
            parsed = MetaInfo("Example Show S01E02 2024 1080p", record=False,
                              include_ai=False)
            self.assertEqual("", parsed.recognition_request_id or "")
            self.assertEqual(0, db.insert_recognition_record.call_count)
            record_deferred_parse_result(
                parsed, "Example Show S01E02 2024 1080p", "indexer",
                "prefilter_rejected", "indexer_filter_rejected",
                {"filter": "test"})

        self.assertEqual(1, db.insert_recognition_record.call_count)
        saved = db.insert_recognition_record.call_args.args[0]
        self.assertEqual("indexer", saved["source"])
        self.assertEqual("success", next(item["status"] for item in saved["provider_results"]
                                           if item["provider_id"] == "local_rules"))
        self.assertEqual("skipped", next(item["status"] for item in saved["provider_results"]
                                          if item["provider_id"] == "anitopy_ml"))
        self.assertEqual("indexer_filter_rejected",
                         saved["overall_result"]["business_result"]["reason"])

    def test_deferred_parse_continues_with_ai_without_reparsing_local_or_tmdb(self):
        runtime = {
            "recognition": {"providers": {
                "local_rules": {"enabled": True},
                "anitopy_ml": {"enabled": True, "endpoint": "http://ai.local"},
            }},
            "laboratory": {"ai_inference": True, "ai_inference_url": "http://ai.local"},
        }
        config = SimpleNamespace(
            get_config=lambda section=None: runtime if section is None else runtime.get(section, {}),
            get_config_path=lambda: "/tmp/nas-tools-recognition-deferred-ai-test",
        )
        db = Mock()
        db.insert_recognition_record.side_effect = lambda record: record["request_id"]

        def run_ai(provider_id, request, **_kwargs):
            result = ParseResult(provider_id, "success", parsed={
                "title": "Example Show", "media_type": "tv", "seasons": [1], "episodes": [2],
            }, raw_result={"result": {"title": "Example Show"}})
            recorder = current_recorder()
            recorder.add_provider_result(
                provider_id, "success", input={"title": request.title},
                raw_result=result.raw_result, normalized_result=result.parsed)
            return result

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db), \
                patch("app.media.meta.metainfo.recognition_service.run_provider",
                      side_effect=run_ai) as run_provider:
            title = "Example Show S01E02 2024 1080p"
            local = MetaInfo(title, include_ai=False, record=False)
            parsed = MetaInfo(title, pre_parsed=local)

        run_provider.assert_called_once()
        self.assertEqual("Example Show", parsed.get_name())
        saved = db.insert_recognition_record.call_args.args[0]
        self.assertEqual("parse_only", saved["stage"])
        self.assertEqual(["local_rules", "anitopy_ml"],
                         [item["provider_id"] for item in saved["provider_results"]])
        self.assertEqual("not_requested", saved["overall_result"]["tmdb_status"])
        self.assertNotIn("tmdb_query", [action["action_type"] for action in saved["actions"]])

    def test_late_business_reason_updates_request_summary_and_attempt_row(self):
        helper = DbHelper()
        request_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "original_name": "Example Show S01E02",
            "source": "indexer",
            "stage": "parse_only",
            "created_at": "2026-10-04 00:00:00",
            "actions": [],
            "provider_results": [{
                "attempt_id": attempt_id, "provider_id": "anitopy_ml",
                "status": "skipped", "input": {"title": "Example Show S01E02"},
                "error": "ai_deferred_to_indexer_filter_or_resolution",
            }],
            "overall_result": {"status": "success", "tmdb_status": "not_requested"},
            "tmdb_results": [],
        }

        helper.insert_recognition_record(payload)
        try:
            self.assertTrue(helper.append_recognition_business_action(
                request_id, "media_cache_hit", "indexer_media_cache_match",
                {"cache_entity_id": 42}, "reused"))
            request, attempts = helper.get_recognition_record(request_id)
            self.assertEqual("indexer_media_cache_match",
                             json.loads(request.OVERALL_RESULT)["business_result"]["reason"])
            cache_action = next(item for item in json.loads(request.ACTIONS)
                                if item["action_type"] == "media_cache_hit")
            self.assertEqual(attempt_id, cache_action["attempt_id"])
            self.assertEqual("indexer_media_cache_match",
                             json.loads(request.PROVIDER_RESULTS)[0]["error"])
            attempt = next(row for row in attempts if row.ATTEMPT_ID == attempt_id)
            self.assertEqual("indexer_media_cache_match", attempt.ERROR)
            self.assertEqual("indexer_media_cache_match",
                             json.loads(request.SUMMARY)["overall_result"][
                                 "business_result"]["reason"])
        finally:
            helper._db.query(RECOGNITIONATTEMPT).filter(
                RECOGNITIONATTEMPT.REQUEST_ID == request_id).delete()
            helper._db.query(RECOGNITIONREQUEST).filter(
                RECOGNITIONREQUEST.REQUEST_ID == request_id).delete()
            helper._db.commit()

    def test_direct_parse_source_is_inferred_from_business_module(self):
        cases = {
            "app.indexer.client._base": "indexer",
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
        namespace = {"__name__": "app.rsschecker"}
        exec("def resolve(media):\n    return media.get_media_info('Example 2024')", namespace)

        with patch("config.Config", return_value=config), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=db):
            namespace["resolve"](media)

        saved = db.insert_recognition_record.call_args.args[0]
        self.assertEqual("rss", saved["source"])
        self.assertEqual("app.rsschecker", saved["context"]["caller_module"])

    def test_all_public_resolve_entrypoints_keep_business_source(self):
        cases = {
            "app.indexer.client._base": "indexer",
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
