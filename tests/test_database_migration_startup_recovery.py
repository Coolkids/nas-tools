"""数据库目标在开放业务前验证失败时恢复原配置。"""

import os
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

import ruamel.yaml

from app.db.manager import DATABASE
from app.db.migration import (
    _fingerprint,
    _restore_source_database_config_before_startup,
    _write_source_config_backup,
)


class DatabaseMigrationStartupRecoveryTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config_path = os.path.join(self.directory.name, "config.yaml")
        self.source = {"type": "sqlite"}
        self.target = {
            "type": "postgresql", "host": "db.example", "port": 5432,
            "name": "nastools-test", "username": "app", "password": "secret",
            "connect_timeout": 5, "ssl": {},
        }
        self.job_id = "a" * 32
        yaml = ruamel.yaml.YAML()
        with open(self.config_path, "w", encoding="utf-8") as stream:
            yaml.dump({"app": {"debug": True}, "database": self.target}, stream)

        class ConfigStub:
            pass

        self.config_stub = ConfigStub()
        self.config_stub._config_path = self.config_path
        self.config_stub.get_config_path = lambda: self.directory.name

        def save_config(config):
            with open(self.config_path, "w", encoding="utf-8") as stream:
                yaml.dump(config, stream)
            return True

        self.config_stub.save_config = save_config
        self.config_patch = patch("config.Config", return_value=self.config_stub)
        self.config_patch.start()

    def tearDown(self):
        self.config_patch.stop()
        self.directory.cleanup()

    def test_restores_source_database_and_preserves_other_settings(self):
        source_fingerprint = _fingerprint(self.source)
        target_fingerprint = _fingerprint(self.target)
        _write_source_config_backup(self.job_id, self.source, source_fingerprint)
        jobs = Mock()
        with patch("app.db.migration.STORAGE_JOBS", jobs), \
                patch("app.db.manager.DATABASE._engine", None), \
                patch("app.db.migration.schedule_restart") as schedule:
            pending = {
                "job_id": self.job_id,
                "payload": {"source_fingerprint": source_fingerprint},
                "result": {"target_fingerprint": target_fingerprint},
            }
            self.assertTrue(_restore_source_database_config_before_startup(
                pending, RuntimeError("target version mismatch")))

        yaml = ruamel.yaml.YAML(typ="safe")
        with open(self.config_path, "r", encoding="utf-8") as stream:
            restored = yaml.load(stream)
        self.assertEqual(self.source, restored["database"])
        self.assertEqual({"debug": True}, restored["app"])
        jobs.update.assert_called_once()
        jobs.clear_maintenance_gate.assert_called_once_with("database", self.job_id)
        schedule.assert_called_once()

    def test_refuses_rollback_if_configuration_changed_after_target_save(self):
        source_fingerprint = _fingerprint(self.source)
        _write_source_config_backup(self.job_id, self.source, source_fingerprint)
        changed = {**self.target, "name": "externally-changed"}
        yaml = ruamel.yaml.YAML()
        with open(self.config_path, "w", encoding="utf-8") as stream:
            yaml.dump({"app": {"debug": True}, "database": changed}, stream)
        with patch("app.db.manager.DATABASE._engine", None), \
                patch("app.db.migration.STORAGE_JOBS"), \
                patch("app.db.migration.schedule_restart") as schedule:
            pending = {
                "job_id": self.job_id,
                "payload": {"source_fingerprint": source_fingerprint},
                "result": {"target_fingerprint": _fingerprint(self.target)},
            }
            self.assertFalse(_restore_source_database_config_before_startup(
                pending, RuntimeError("target version mismatch")))
        schedule.assert_not_called()
