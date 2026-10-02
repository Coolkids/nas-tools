import time
import unittest
from unittest.mock import Mock, patch

import requests

from app.media.recognition.records import recognition_scope, record_tmdb_call
from app.media.media import Media
from app.media.tmdbv3api.tmdb import TMDb
from app.utils.types import MediaType


class TmdbRecognitionDeadlineTest(unittest.TestCase):
    def test_tmdb_web_fallback_uses_remaining_budget(self):
        media = object.__new__(Media)
        request = Mock()
        request.get_res.return_value = None
        database = Mock()
        database.insert_recognition_record.side_effect = lambda payload: payload["request_id"]

        with patch("app.media.recognition.records._runtime_config_snapshot",
                   return_value={"recognition": {}, "laboratory": {}}), \
                patch("app.media.recognition.records._recognition_config_snapshot",
                      return_value=("test-version", {})), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database), \
                patch("app.media.media.RequestUtils", return_value=request) as request_utils:
            with recognition_scope("Deadline Website Search", source="timeout-test") as recorder:
                recorder.deadline_monotonic = time.monotonic() + 0.2
                self.assertIsNone(media._Media__search_tmdb_web(
                    "Deadline Website Search", MediaType.TV))

        self.assertGreater(request_utils.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(request_utils.call_args.kwargs["timeout"], 0.2)

    def test_recognition_context_deadline_reaches_tmdb_and_records_timeout(self):
        session = Mock()
        session.request.side_effect = requests.exceptions.Timeout("slow response")
        client = TMDb(session=session)

        class Search:
            @record_tmdb_call
            def lookup(self):
                return client.session_request(
                    session, "GET", "https://tmdb.local/test", None, None)

        database = Mock()
        database.insert_recognition_record.side_effect = lambda payload: payload["request_id"]
        with patch("app.media.recognition.records._runtime_config_snapshot",
                   return_value={"recognition": {}, "laboratory": {}}), \
                patch("app.media.recognition.records._recognition_config_snapshot",
                      return_value=("test-version", {})), \
                patch("app.media.recognition.records._write_spool", return_value=None), \
                patch("app.helper.db_helper.DbHelper", return_value=database):
            with recognition_scope("Example.Show.S01E01", source="timeout-test") as recorder:
                recorder.deadline_monotonic = time.monotonic() + 0.2
                with self.assertRaises(requests.exceptions.Timeout):
                    Search().lookup()

        session.request.assert_called_once()
        self.assertGreater(session.request.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(session.request.call_args.kwargs["timeout"], 0.2)
        saved = database.insert_recognition_record.call_args.args[0]
        self.assertEqual("timeout", saved["tmdb_results"][0]["status"])

    def test_uncached_tmdb_request_uses_budget_and_does_not_retry(self):
        session = Mock()
        session.request.side_effect = requests.exceptions.Timeout("slow response")
        client = TMDb(session=session)

        with patch("app.media.tmdbv3api.tmdb._recognition_remaining_seconds",
                   return_value=0.15):
            with self.assertRaises(requests.exceptions.Timeout):
                client.session_request(session, "GET", "https://tmdb.local/test", None, None)

        session.request.assert_called_once()
        self.assertGreater(session.request.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(session.request.call_args.kwargs["timeout"], 0.15)

    def test_cached_tmdb_request_uses_budget_and_does_not_retry(self):
        session = Mock()
        session.request.side_effect = requests.exceptions.Timeout("slow response")
        key = ("deadline-test", time.time())

        with patch.object(TMDb, "_cache_session", session), \
                patch("app.media.tmdbv3api.tmdb._recognition_remaining_seconds",
                      return_value=0.2):
            with self.assertRaises(requests.exceptions.Timeout):
                TMDb.cached_request(key, "GET", "https://tmdb.local/test", None, None)

        session.request.assert_called_once()
        self.assertGreater(session.request.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(session.request.call_args.kwargs["timeout"], 0.2)

    def test_expired_budget_skips_network_request(self):
        session = Mock()
        client = TMDb(session=session)

        with patch("app.media.tmdbv3api.tmdb._recognition_remaining_seconds", return_value=0):
            with self.assertRaisesRegex(requests.exceptions.Timeout,
                                        "recognition_deadline_exceeded"):
                client.session_request(session, "GET", "https://tmdb.local/test", None, None)

        session.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
