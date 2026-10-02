# -*- coding: utf-8 -*-
import io
import json
import unittest
import zipfile
from unittest.mock import patch
from xml.etree import ElementTree

from web.action import WebAction


class RecognitionXlsxExportTest(unittest.TestCase):
    def test_xlsx_export_contains_filtered_recognition_record_fields(self):
        record = {
            "request_id": "request-1",
            "original_name": "Show & Film.mkv",
            "source": "test",
            "stage": "resolve",
            "created_at": "2026-10-02 12:00:00.000000",
            "context": {"input": "raw"},
            "actions": [{"action_type": "decision"}],
            "provider_results": [{"provider_id": "third_party", "raw_result": {"name": "Show"}}],
            "overall_result": {"status": "failed", "reason": "ambiguous_tmdb"},
            "tmdb_results": [{"result": {"id": 42, "title": "Show"}}],
        }

        with patch.object(WebAction, "iter_recognition_jsonl", return_value=iter([
                json.dumps(record, ensure_ascii=False)])) as export_rows:
            with WebAction.get_recognition_xlsx(title="Show") as workbook:
                data = workbook.read()
            export_rows.assert_called_once_with(title="Show")

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml")
            root = ElementTree.fromstring(xml)
        text = "".join(node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
        self.assertIn("Request ID", text)
        self.assertIn("request-1", text)
        self.assertIn("Show & Film.mkv", text)
        self.assertIn("ambiguous_tmdb", text)
        self.assertIn("third_party", text)


if __name__ == "__main__":
    unittest.main()
