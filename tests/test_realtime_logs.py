from collections import deque
from unittest import TestCase
from unittest.mock import patch

import log
from web.action import WebAction


class RealtimeLogsTest(TestCase):
    def setUp(self):
        self.patches = [
            patch.object(log, "LOG_QUEUE", deque(maxlen=log.LOG_QUEUE.maxlen)),
            patch.object(log, "LOG_INDEX", 0),
            patch.object(log, "LOG_SEQUENCE", 0),
            patch.object(log, "LOG_STREAM_ID", "test-stream"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def append(text, level="INFO"):
        getattr(log, "__append_log_queue")(level, text)

    @staticmethod
    def query(after_id=0, source="", stream_id="test-stream"):
        return WebAction._WebAction__logging({
            "after_id": after_id, "source": source, "stream_id": stream_id})

    def test_initial_load_returns_latest_2000_entries_in_order(self):
        for number in range(2100):
            self.append(str(number))
        result = self.query()
        self.assertEqual(2000, len(result["loglist"]))
        self.assertEqual("100", result["loglist"][0]["text"])
        self.assertEqual("2099", result["loglist"][-1]["text"])
        self.assertEqual(2100, result["last_id"])

    def test_independent_viewers_do_not_consume_each_others_updates(self):
        self.append("first")
        cursor = self.query()["last_id"]
        self.append("second", "ERROR")
        first = self.query(cursor)
        second = self.query(cursor)
        self.assertEqual(first, second)
        self.assertEqual("ERROR", first["loglist"][0]["level"])
        self.assertEqual([], self.query(first["last_id"])["loglist"])

    def test_source_filter_keeps_receiving_updates_after_queue_rollover(self):
        self.append("【Rss】first")
        cursor = self.query(source="Rss")["last_id"]
        for number in range(2100):
            self.append(str(number))
        self.append("【Rss】latest")
        result = self.query(cursor, source="Rss")
        self.assertEqual(["latest"], [item["text"] for item in result["loglist"]])
        self.assertEqual(2102, result["last_id"])

    def test_empty_source_results_still_advance_cursor(self):
        self.append("other")
        result = self.query(source="Rss")
        self.assertEqual([], result["loglist"])
        self.assertEqual(1, result["last_id"])

    def test_server_restart_resets_cursor_even_if_new_sequence_is_larger(self):
        self.append("first")
        self.append("second")
        result = self.query(after_id=1, stream_id="previous-stream")
        self.assertTrue(result["reset"])
        self.assertEqual(2, len(result["loglist"]))
        self.assertEqual("test-stream", result["stream_id"])

    def test_invalid_cursor_does_not_consume_legacy_updates(self):
        self.append("first")
        result = self.query(after_id="invalid")
        self.assertEqual(1, result["code"])
        self.assertEqual(1, log.LOG_INDEX)

    def test_legacy_logging_request_remains_available(self):
        self.append("first")
        result = WebAction._WebAction__logging({"refresh_new": 0})
        self.assertEqual("first", result["loglist"][0]["text"])
        self.append("second")
        self.assertEqual(["second"], [item["text"] for item in
                         WebAction._WebAction__logging({"refresh_new": 1})["loglist"]])
