import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from web.action import WebAction


class RecognitionApiContractTest(unittest.TestCase):
    def _name_test(self, reason=None, parsed=True, tmdb_result=None, error=None,
                   tmdb_allowed=True):
        from app.media.media import Media
        from app.media.recognition.records import current_recorder

        media = object.__new__(Media)
        media.tmdb = object()
        media_info = SimpleNamespace(tmdb_info=tmdb_result, recognition_source="original") if parsed else None
        helper = Mock()
        helper.insert_recognition_record.return_value = "saved"

        def resolve(**_kwargs):
            if error:
                raise error
            if reason:
                current_recorder().context["decision_reason"] = reason
            return media_info

        with patch("web.action.Media", return_value=media), \
                patch.object(media, "_get_media_info_impl", side_effect=resolve), \
                patch.object(media, "_Media__meta_snapshot", return_value={"name": "Example"}), \
                patch("app.media.media.profile_tmdb_allowed", return_value=tmdb_allowed), \
                patch.object(WebAction, "mediainfo_dict", return_value={
                    "name": "Example", "title": "Example" if tmdb_result else "",
                    "tmdbid": tmdb_result.get("id") if tmdb_result else None,
                }), \
                patch("app.media.recognition.records.replay_recognition_spool"), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=helper):
            response = object.__new__(WebAction)._WebAction__name_test({"name": "Example.S01E01"})

        # 测试接口和核心识别共用一个请求，不能产生两条识别记录。
        helper.insert_recognition_record.assert_called_once()
        record = helper.insert_recognition_record.call_args.args[0]
        self.assertEqual("web", record["source"])
        self.assertEqual("resolve", record["stage"])
        return response, record

    def test_name_test_returns_failure_reason_with_or_without_partial_parse(self):
        for reason in ("ambiguous_tmdb", "tmdb_no_results", "tmdb_network_error",
                       "insufficient_title_evidence", "recognition_deadline_exceeded"):
            for parsed in (True, False):
                with self.subTest(reason=reason, parsed=parsed):
                    response, record = self._name_test(reason=reason, parsed=parsed)
                    self.assertEqual(0, response["code"])
                    self.assertEqual("failed", response["data"]["recognition_status"])
                    self.assertEqual(reason, response["data"]["recognition_reason"])
                    self.assertEqual(reason, record["overall_result"]["reason"])
                    self.assertEqual(parsed, "title" in response["data"])

    def test_name_test_preserves_success_and_skipped_outcomes(self):
        success, _ = self._name_test(tmdb_result={"id": 42, "name": "Example"})
        self.assertEqual("success", success["data"]["recognition_status"])
        self.assertIsNone(success["data"]["recognition_reason"])
        self.assertEqual(42, success["data"]["tmdbid"])

        skipped, _ = self._name_test(tmdb_allowed=False)
        self.assertEqual("skipped", skipped["data"]["recognition_status"])
        self.assertEqual("tmdb_disallowed_by_profile", skipped["data"]["recognition_reason"])

    def test_name_test_reports_missing_name_and_parser_exception(self):
        failed, _ = self._name_test(parsed=False)
        self.assertEqual("no_name_parsed", failed["data"]["recognition_reason"])

        error, record = self._name_test(error=ValueError("解析响应格式无效"))
        self.assertEqual("failed", error["data"]["recognition_status"])
        self.assertEqual("解析响应格式无效", error["data"]["recognition_reason"])
        self.assertEqual("解析响应格式无效", record["overall_result"]["reason"])

    def test_list_validates_pagination_and_date_before_query(self):
        helper = Mock()
        helper.get_recognition_records.return_value = (0, [])
        with patch("web.action.DbHelper", return_value=helper):
            invalid_page = WebAction._WebAction__get_recognition_records({"page": "abc"})
            invalid_date = WebAction._WebAction__get_recognition_records({
                "created_from": "2026-02-30"})
            malformed_filters = WebAction._WebAction__get_recognition_records({
                "title": 42, "source": ["rss"], "created_to": 20261002})

        self.assertEqual({"code": 1, "msg": "分页参数无效"}, invalid_page)
        self.assertEqual({"code": 1, "msg": "日期筛选格式应为 YYYY-MM-DD"}, invalid_date)
        self.assertEqual(0, malformed_filters["code"])
        helper.get_recognition_records.assert_called_once_with(
            title="", source="", status="", provider_id="", action_type="", reason="",
            created_from="", created_to="", page=1, page_size=20)

    def test_provider_endpoint_keeps_new_provider_descriptors_dynamic(self):
        provider = SimpleNamespace(descriptor=SimpleNamespace(
            display_name="Third party", version="3.1", evidence_family="title",
            config_schema={"endpoint": {"type": "string"}}))
        registry_mock = Mock()
        registry_mock.discover.return_value = {"fixture_third": provider}
        registry_mock.diagnostics.return_value = []

        with patch("app.media.recognition.registry.registry", registry_mock):
            response = WebAction._WebAction__get_recognition_providers()

        self.assertEqual("fixture_third", response["providers"][0]["provider_id"])
        self.assertEqual("Third party", response["providers"][0]["display_name"])
        self.assertEqual({"endpoint": {"type": "string"}},
                         response["providers"][0]["config_schema"])

    def test_parse_cache_info_and_clear_actions_are_parse_only(self):
        parse_cache = {"entries": 7, "bytes": 1536, "generation": 2}
        cleared_cache = {"entries": 0, "bytes": 0, "generation": 3}
        with patch("app.media.recognition.cache.info",
                   side_effect=[parse_cache, parse_cache, cleared_cache]) as info, \
                patch("app.media.recognition.cache.clear") as clear:
            status = WebAction._WebAction__get_recognition_parse_cache_info()
            result = WebAction._WebAction__clear_recognition_parse_cache()

        self.assertEqual({"code": 0, "cache": parse_cache}, status)
        self.assertEqual({"code": 0, "cleared_entries": 7, "cache": cleared_cache}, result)
        clear.assert_called_once_with("parse")
        self.assertEqual([("parse",), ("parse",), ("parse",)],
                         [call.args for call in info.call_args_list])

    def test_parse_cache_clear_action_requires_login(self):
        from web.main import App

        with patch("app.media.recognition.cache.clear") as clear:
            response = App.test_client().post("/do", data={
                "cmd": "clear_recognition_parse_cache", "data": "{}"})

        self.assertEqual(-1, response.json["code"])
        self.assertEqual("用户未登录", response.json["msg"])
        clear.assert_not_called()

    def test_list_preserves_parse_only_tmdb_and_provider_statuses(self):
        summaries = [
            {"overall_result": {"status": "success", "tmdb_status": "not_requested"},
             "provider_results": [{"provider_id": "local_rules", "status": "success"}]},
            {"overall_result": {"status": "failed", "reason": "no_tmdb_match"},
             "provider_results": [{"provider_id": "local_rules", "status": "success"}]},
            {"overall_result": {"status": "failed", "reason": "provider_error"},
             "provider_results": [{"provider_id": "fixture_third", "status": "error"}]},
        ]
        rows = [SimpleNamespace(
            SUMMARY=json.dumps(summary), REQUEST_ID=f"r{index}", ORIGINAL_NAME="Example",
            SOURCE="test", STAGE="resolve", CREATED_AT="2026-10-02 10:00:00")
            for index, summary in enumerate(summaries, start=1)]
        helper = Mock()
        helper.get_recognition_records.return_value = (len(rows), rows)

        with patch("web.action.DbHelper", return_value=helper):
            response = WebAction._WebAction__get_recognition_records({})

        results = response["records"]
        self.assertEqual("not_requested", results[0]["overall_result"]["tmdb_status"])
        self.assertEqual("no_tmdb_match", results[1]["overall_result"]["reason"])
        self.assertEqual("error", results[2]["provider_results"][0]["status"])
        self.assertEqual("fixture_third", results[2]["provider_results"][0]["provider_id"])

    def test_legacy_ai_action_still_returns_compatibility_projection(self):
        old_record = SimpleNamespace(
            ID=17, TITLE="Example", STATUS="error", ADD_TIME="2026-10-02",
            ANITOPY_RESULT='{"name":"Example"}', AI_RESULT='{"name":"Other"}',
            ANITOPY_TMDB="null", AI_TMDB='{"id":42}')
        helper = Mock()
        helper.get_ai_recognition_records.return_value = (1, [old_record])

        with patch("web.action.DbHelper", return_value=helper):
            response = WebAction().action("get_ai_recognition_records", {"page": 1})

        self.assertEqual(0, response["code"])
        self.assertEqual(17, response["records"][0]["id"])
        self.assertEqual({"name": "Example"}, response["records"][0]["anitopy_result"])
        self.assertEqual({"id": 42}, response["records"][0]["ai_tmdb"])

    def test_export_routes_remain_authenticated_and_reject_bad_dates(self):
        from web.main import App

        client = App.test_client()
        with patch.object(App.login_manager, "unauthorized", return_value=("", 401)):
            for path in ("/recognition_export.jsonl", "/recognition_export.xlsx",
                         "/ai_recognition_export.xlsx"):
                response = client.get(path)
                self.assertEqual(401, response.status_code, path)

        # Route date validation runs after authentication; exercise the view
        # directly to keep this contract independent of test login setup.
        with App.test_request_context("/recognition_export.jsonl?created_from=2026-02-30"):
            from web.main import recognition_export_jsonl
            response = recognition_export_jsonl.__wrapped__()
        self.assertEqual(400, response.status_code)


if __name__ == "__main__":
    unittest.main()
