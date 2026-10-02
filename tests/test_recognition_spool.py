# -*- coding: utf-8 -*-
import json
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media.recognition.records import (
    RecognitionRecorder,
    _write_spool,
    recognition_config,
    replay_recognition_spool,
)


class RecognitionSpoolTest(TestCase):
    def test_real_process_crash_recovers_actions_and_provider_attempt(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as directory:
            config_path = os.path.join(directory, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as config_file:
                config_file.write("app: {}\n")
            environment = os.environ.copy()
            environment["NASTOOL_CONFIG"] = config_path
            environment["PYTHONPATH"] = os.pathsep.join(filter(None, [
                os.path.join(root, "third_party", "anitopy"),
                environment.get("PYTHONPATH"),
            ]))

            crash_code = """
import os
from app.db.main_db import MainDb
from app.media.recognition.records import recognition_scope
MainDb.init_db()
with recognition_scope('Crash.Show.S01E01.mkv', source='crash-test') as recorder:
    recorder.add_provider_result(
        'local_rules', 'success', input={'title': 'Crash.Show.S01E01.mkv'},
        raw_result={'parsed': 'Crash Show'},
        normalized_result={'title': 'Crash Show'}, attempt_id='crash-attempt')
    recorder.add_tmdb_result(
        'local_rules', {'query': 'Crash Show'}, {'id': 987, 'media_type': 'tv'},
        status='success')
    os._exit(17)
"""
            crashed = subprocess.run(
                [sys.executable, "-c", crash_code], cwd=root, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            self.assertEqual(17, crashed.returncode, crashed.stderr)
            spool_dir = os.path.join(directory, "recognition_spool")
            spool_files = [name for name in os.listdir(spool_dir) if name.endswith(".json")]
            self.assertEqual(1, len(spool_files))
            with open(os.path.join(spool_dir, spool_files[0]), encoding="utf-8") as spool_file:
                interrupted_payload = json.load(spool_file)
            self.assertEqual(1, len(interrupted_payload["provider_results"][0]["tmdb_results"]))
            self.assertTrue(any(action["action_type"] == "provider_parse"
                                for action in interrupted_payload["actions"]))
            self.assertTrue(any(action["action_type"] == "tmdb_query"
                                for action in interrupted_payload["actions"]))

            recovery_code = """
import json
from app.db.main_db import MainDb
from app.helper.db_helper import DbHelper
from app.media.recognition.records import replay_recognition_spool
MainDb.init_db()
recovery = replay_recognition_spool()
helper = DbHelper()
request, attempts = helper.get_recognition_record(%r)
print(json.dumps({
    'recovery': recovery,
    'status': json.loads(request.OVERALL_RESULT)['status'],
    'reason': json.loads(request.OVERALL_RESULT)['reason'],
    'lifecycle': json.loads(request.OVERALL_RESULT)['lifecycle'],
    'actions': [action['action_type'] for action in json.loads(request.ACTIONS)],
    'provider_results': json.loads(request.PROVIDER_RESULTS),
    'attempt_count': len(attempts),
}))
DbHelper.release_session()
""" % interrupted_payload["request_id"]
            recovered = subprocess.run(
                [sys.executable, "-c", recovery_code], cwd=root, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
            self.assertEqual(0, recovered.returncode, recovered.stderr)
            result = json.loads(recovered.stdout.strip().splitlines()[-1])
            self.assertEqual({"replayed": 1, "pending": 0, "invalid": 0}, result["recovery"])
            self.assertEqual("failed", result["status"])
            self.assertEqual("process_interrupted", result["reason"])
            self.assertEqual("interrupted", result["lifecycle"])
            self.assertIn("provider_parse", result["actions"])
            self.assertIn("tmdb_query", result["actions"])
            self.assertEqual("Crash Show", result["provider_results"][0]["normalized_result"]["title"])
            self.assertEqual(1, result["attempt_count"])
            self.assertEqual([], os.listdir(spool_dir))

    def test_record_is_written_atomically_as_json(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("config.Config", return_value=SimpleNamespace(get_config_path=lambda: directory)):
            payload = {"request_id": "req-1", "original_name": "Example.mkv"}

            path = _write_spool(payload)

            with open(path, encoding="utf-8") as spool_file:
                self.assertEqual(payload, json.load(spool_file))
            self.assertEqual(["req-1.json"], os.listdir(os.path.dirname(path)))

    def test_spool_replay_removes_only_successfully_persisted_records(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("config.Config", return_value=SimpleNamespace(get_config_path=lambda: directory)):
            _write_spool({"request_id": "saved", "actions": [], "overall_result": {}})
            _write_spool({"request_id": "pending", "actions": [], "overall_result": {}})
            db = Mock()
            db.insert_recognition_record.side_effect = lambda record: (
                record["request_id"] if record["request_id"] == "saved" else False
            )

            with patch("app.helper.db_helper.DbHelper", return_value=db):
                result = replay_recognition_spool()

            self.assertEqual({"replayed": 1, "pending": 1, "invalid": 0}, result)
            self.assertEqual(["pending.json"], os.listdir(os.path.join(directory, "recognition_spool")))

    def test_replay_is_idempotent_for_a_request_already_in_the_database(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("config.Config", return_value=SimpleNamespace(get_config_path=lambda: directory)):
            _write_spool({"request_id": "already-saved", "actions": [], "overall_result": {}})
            db = Mock()
            db.insert_recognition_record.return_value = "already-saved"

            with patch("app.helper.db_helper.DbHelper", return_value=db):
                first = replay_recognition_spool()
                second = replay_recognition_spool()

            self.assertEqual(1, first["replayed"])
            self.assertEqual(0, second["pending"])
            db.insert_recognition_record.assert_called_once()

    def test_replay_marks_running_spool_as_interrupted(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("config.Config", return_value=SimpleNamespace(get_config_path=lambda: directory)):
            _write_spool({
                "request_id": "interrupted",
                "actions": [{"sequence": 1, "action_type": "request_start"}],
                "overall_result": {"status": "running", "lifecycle": "running"},
            })
            db = Mock()
            db.insert_recognition_record.side_effect = lambda payload: payload["request_id"]

            with patch("app.helper.db_helper.DbHelper", return_value=db):
                result = replay_recognition_spool()

            recovered = db.insert_recognition_record.call_args.args[0]
            self.assertEqual("interrupted", recovered["overall_result"]["lifecycle"])
            self.assertEqual("process_interrupted", recovered["overall_result"]["reason"])
            self.assertEqual("request_recovered", recovered["actions"][-1]["action_type"])
            self.assertEqual(1, result["replayed"])

    def test_invalid_spool_is_counted_and_left_for_inspection(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("config.Config", return_value=SimpleNamespace(get_config_path=lambda: directory)):
            spool_dir = os.path.join(directory, "recognition_spool")
            os.makedirs(spool_dir)
            with open(os.path.join(spool_dir, "broken.json"), "w", encoding="utf-8") as file:
                file.write("not-json")

            result = replay_recognition_spool()

            self.assertEqual(1, result["invalid"])
            self.assertTrue(os.path.exists(os.path.join(spool_dir, "broken.json")))

    def test_request_start_is_durable_and_success_removes_spool(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("config.Config", return_value=SimpleNamespace(get_config_path=lambda: directory)):
            db = Mock()
            db.insert_recognition_record.side_effect = lambda payload: payload["request_id"]
            with patch("app.helper.db_helper.DbHelper", return_value=db):
                with RecognitionRecorder("Example.mkv") as recorder:
                    spool_file = os.path.join(directory, "recognition_spool", recorder.request_id + ".json")
                    self.assertTrue(os.path.exists(spool_file))
                    recorder.set_overall("success", parsed_result={"name": "Example"})

            self.assertFalse(os.path.exists(spool_file))
            self.assertEqual("success", db.insert_recognition_record.call_args.args[0]["overall_result"]["status"])

    def test_request_captures_immutable_redacted_recognition_config_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            live_config = {
                "recognition": {
                    "decision": {"strategy": "weighted", "weights": {"title": 0.8}},
                    "providers": {"ai": {"enabled": True, "api_key": "private"}},
                    "service_url": "https://private.example/api",
                },
                "laboratory": {"ai_inference": True,
                               "ai_inference_url": "https://private.example/model"},
            }
            config = SimpleNamespace(get_config_path=lambda: directory,
                                     get_config=lambda: live_config)
            db = Mock()
            db.insert_recognition_record.side_effect = lambda payload: payload["request_id"]
            with patch("config.Config", return_value=config), \
                    patch("app.helper.db_helper.DbHelper", return_value=db):
                with RecognitionRecorder("Example.mkv") as recorder:
                    first_version = recorder.context["recognition_config_version"]
                    self.assertEqual("weighted", recognition_config("recognition")["decision"]["strategy"])
                    live_config["recognition"]["decision"]["weights"]["title"] = 0.1
                    self.assertEqual(0.8, recognition_config("recognition")["decision"]["weights"]["title"])
                    recorder.set_overall("success")

            saved = db.insert_recognition_record.call_args.args[0]
            snapshot = saved["context"]["recognition_config_snapshot"]
            self.assertEqual(64, len(first_version))
            self.assertEqual(first_version, saved["context"]["recognition_config_version"])
            self.assertEqual(0.8, snapshot["recognition"]["decision"]["weights"]["title"])
            self.assertTrue(snapshot["runtime"]["ai_inference_enabled"])
            self.assertNotIn("api_key", snapshot["recognition"]["providers"]["ai"])
            self.assertNotIn("service_url", snapshot["recognition"])
            self.assertNotIn("ai_inference_url", snapshot)
