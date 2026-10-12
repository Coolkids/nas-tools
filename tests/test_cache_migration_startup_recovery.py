"""纯内存缓存迁移中断后，只从经校验的完整快照恢复。"""

import os
import tempfile
from contextlib import contextmanager
from unittest import TestCase
from unittest.mock import patch

from app.utils import cache_migration
from app.utils.cache_backend import CACHE_NAMESPACE_REGISTRY


class _MemoryStore:
    def __init__(self):
        self.records = {namespace: [] for namespace in CACHE_NAMESPACE_REGISTRY}
        self.frozen = False

    def status(self):
        return {"active_backend": "memory", "configured_backend": "memory", "fallback": False}

    def clear(self, namespace):
        self.records[namespace] = []

    def put_many(self, namespace, records):
        self.records[namespace].extend(records)

    def iter_records(self, namespace, strict=False):
        now = cache_migration.time.time()
        yield from (record for record in self.records[namespace]
                    if record[2] is None or record[2] > now)

    def freeze(self):
        self.frozen = True

    def unfreeze(self):
        self.frozen = False


class _Jobs:
    def __init__(self, job):
        self.job = job
        self.gate = None
        self.updates = []

    @contextmanager
    def maintenance_lock(self, exclusive=False):
        yield

    def active(self, kind=None):
        return [self.job] if self.job else []

    def maintenance_gate(self, kind):
        return self.gate

    def set_maintenance_gate(self, kind, job_id, phase):
        self.gate = {"job_id": job_id, "phase": phase}

    def clear_maintenance_gate(self, kind, job_id=None):
        if self.gate and (job_id is None or self.gate["job_id"] == job_id):
            self.gate = None

    def update(self, job_id, **values):
        self.updates.append(values)
        self.job.update(values)
        return self.job


class CacheMigrationStartupRecoveryTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.job_id = "b" * 32
        self.source_status = {
            "active_backend": "memory", "generation": 7, "config_generation": None,
        }
        self.snapshot_path = os.path.join(
            self.directory.name, "cache", ".staging", self.job_id, "snapshot.sqlite3")
        self.memory = _MemoryStore()
        self.config = type("ConfigStub", (), {
            "get_config_path": lambda _self: self.directory.name,
        })()
        self.config_jobs = {
            "job_id": self.job_id,
            "status": "SNAPSHOTTING",
            "stage": "SNAPSHOTTING",
            "result": {"source": self.source_status, "snapshot_path": self.snapshot_path},
        }
        self.jobs = _Jobs(self.config_jobs)
        self.patches = [
            patch("config.Config", return_value=self.config),
            patch.object(cache_migration, "STORE", self.memory),
            patch.object(cache_migration, "STORAGE_JOBS", self.jobs),
        ]
        for mocked in self.patches:
            mocked.start()

    def tearDown(self):
        for mocked in reversed(self.patches):
            mocked.stop()
        self.directory.cleanup()

    def _create_complete_snapshot(self):
        os.makedirs(os.path.dirname(self.snapshot_path), mode=0o700, exist_ok=True)
        for namespace in CACHE_NAMESPACE_REGISTRY:
            records = [("cache-key", {"value": 1}, None)] if namespace == "recognition_tmdb" else []
            cache_migration._write_snapshot_namespace(self.snapshot_path, namespace, records)
        cache_migration._write_snapshot_manifest(
            self.job_id, self.snapshot_path, self.source_status)

    def test_startup_restores_memory_source_from_complete_snapshot(self):
        self._create_complete_snapshot()

        cache_migration.verify_cache_startup()

        self.assertEqual("INTERRUPTED", self.config_jobs["status"])
        self.assertEqual([("cache-key", {"value": 1}, None)],
                         self.memory.records["recognition_tmdb"])
        self.assertIsNone(self.jobs.gate)
        self.assertFalse(self.memory.frozen)
        self.assertFalse(os.path.exists(os.path.dirname(self.snapshot_path)))

    def test_startup_keeps_maintenance_gate_when_snapshot_manifest_is_missing(self):
        os.makedirs(os.path.dirname(self.snapshot_path), mode=0o700, exist_ok=True)
        for namespace in CACHE_NAMESPACE_REGISTRY:
            cache_migration._write_snapshot_namespace(self.snapshot_path, namespace, [])

        with self.assertRaisesRegex(RuntimeError, "保持维护状态"):
            cache_migration.verify_cache_startup()

        self.assertEqual("RECOVERY_REQUIRED", self.config_jobs["status"])
        self.assertEqual(self.job_id, self.jobs.gate["job_id"])
        self.assertTrue(self.memory.frozen)
        self.assertTrue(os.path.isfile(self.snapshot_path))

    def test_startup_rejects_snapshot_modified_after_completion_manifest(self):
        self._create_complete_snapshot()
        cache_migration._write_snapshot_namespace(
            self.snapshot_path, "recognition_tmdb", [("unexpected", {"value": 2}, None)])

        with self.assertRaisesRegex(RuntimeError, "保持维护状态"):
            cache_migration.verify_cache_startup()

        self.assertEqual("RECOVERY_REQUIRED", self.config_jobs["status"])
        self.assertEqual([], self.memory.records["recognition_tmdb"])
        self.assertEqual(self.job_id, self.jobs.gate["job_id"])


if __name__ == "__main__":
    import unittest

    unittest.main()
