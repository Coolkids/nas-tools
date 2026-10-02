# -*- coding: utf-8 -*-
import json
from unittest import TestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace

from requests.exceptions import Timeout

from app.helper.db_helper import DbHelper
from app.media.media import Media
from app.media.recognition.contracts import RecognitionRequest
from app.media.recognition.providers.anitopy_ml import AnitopyMlRecognizer
from app.media.recognition.records import record_tmdb_call
from app.media.tmdbv3api.tmdb import TMDb


class RecognitionTimeoutTest(TestCase):
    def test_ai_http_request_uses_remaining_timeout_budget(self):
        response = Mock(status_code=200)
        response.json.return_value = {"result": {"title": "Example"}}
        client = Mock()
        client.post_res.return_value = response

        with patch("app.media.recognition.providers.anitopy_ml.RequestUtils",
                   return_value=client) as request_utils, \
                patch("app.media.recognition.providers.anitopy_ml.Config",
                      return_value=SimpleNamespace(get_ua=lambda: "test-agent")):
            result = AnitopyMlRecognizer(endpoint="http://ai.local", timeout=0.25).parse(
                RecognitionRequest(title="Example.2024.1080p"))

        self.assertEqual("success", result.status)
        self.assertEqual({"title": "Example"}, result.parsed)
        self.assertEqual(0.25, request_utils.call_args.kwargs["timeout"])

    def test_media_resolution_records_ai_http_timeout_with_remaining_budget(self):
        title = "Example.Show.S01E01.1080p"

        def config(section=None):
            if section == "recognition":
                return {
                    "execution": {"total_timeout_seconds": 0.05},
                    "providers": {"anitopy_ml": {"enabled": True, "endpoint": "http://ai.local"}},
                    "decision": {"strategy": "legacy", "shadow": {"enabled": False}},
                }
            if section == "laboratory":
                return {"ai_inference": True, "ai_inference_url": "http://ai.local"}
            return {}

        media = Media.__new__(Media)
        media.tmdb = object()
        media._ai_inference = True
        media._ai_inference_url = "http://ai.local"
        tmdb_session = Mock()
        tmdb_session.request.side_effect = Timeout("TMDB timed out")

        class TmdbLookup:
            @record_tmdb_call
            def search(self, *args, **kwargs):
                try:
                    return TMDb(session=tmdb_session).session_request(
                        tmdb_session, "GET", "https://tmdb.local/search", None, None)
                except Timeout:
                    return None

        with patch("app.media.media.recognition_config", side_effect=config), \
                patch("app.media.media.prepare_media_title", return_value=(title, None, {})), \
                patch.object(media, "_Media__search_meta_tmdb", side_effect=TmdbLookup().search), \
                patch("app.media.recognition.providers.anitopy_ml.RequestUtils",
                      side_effect=Timeout("inference timed out")) as request_utils, \
                patch("app.media.recognition.providers.anitopy_ml.Config",
                      return_value=SimpleNamespace(get_ua=lambda: "test-agent")):
            result = media.get_media_info(title)

        self.assertIsNotNone(result)
        self.assertGreater(request_utils.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(request_utils.call_args.kwargs["timeout"], 0.05)
        tmdb_session.request.assert_called_once()
        self.assertGreater(tmdb_session.request.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(tmdb_session.request.call_args.kwargs["timeout"], 0.05)
        helper = DbHelper()
        _, records = helper.get_recognition_records(
            title=title, source="media.get_media_info", page=1, page_size=10)
        record = records[0]
        request, attempts = helper.get_recognition_record(record.REQUEST_ID)
        provider_results = json.loads(request.PROVIDER_RESULTS)
        actions = json.loads(request.ACTIONS)
        overall = json.loads(request.OVERALL_RESULT)
        tmdb_results = json.loads(request.TMDB_RESULTS)
        attempt_providers = [(attempt.PROVIDER_ID, attempt.STATUS) for attempt in attempts]
        DbHelper.release_session()
        ai_result = next(item for item in provider_results if item["provider_id"] == "anitopy_ml")
        self.assertEqual("timeout", ai_result["status"])
        self.assertEqual("inference timed out", ai_result["error"])
        self.assertTrue(any(action["action_type"] == "provider_parse"
                            and action["status"] == "timeout"
                            for action in actions))
        self.assertEqual("failed", overall["status"])
        self.assertEqual("no_tmdb_match", overall["reason"])
        self.assertIn(("anitopy_ml", "timeout"), attempt_providers)
        self.assertEqual("timeout", tmdb_results[0]["status"])
