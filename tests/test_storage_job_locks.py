"""跨进程存储维护锁隔离数据库和缓存迁移任务。"""

import multiprocessing
import os
import tempfile
import time
from unittest import TestCase

from app.utils.storage_jobs import StorageJobStore


def _hold_exclusive_maintenance_lock(path, ready, release):
    jobs = StorageJobStore(path)
    with jobs.maintenance_lock(exclusive=True, timeout=10):
        ready.set()
        release.wait(10)


def _claim_ready_job(path, job_id, barrier, results):
    jobs = StorageJobStore(path)
    barrier.wait(10)
    claimed = jobs.update(job_id, status="QUIESCING", stage="QUIESCING",
                          expected_status="READY")
    results.put(bool(claimed))


class StorageJobLockTest(TestCase):
    def test_separate_processes_share_one_database_cache_maintenance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, ".maintenance", "storage-jobs.sqlite3")
            parent_store = StorageJobStore(path)
            child_store = StorageJobStore(path)
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            child = context.Process(
                target=_hold_exclusive_maintenance_lock,
                args=(path, ready, release),
            )
            child.start()
            self.assertTrue(ready.wait(10), "子进程未能取得维护锁")
            try:
                started = time.monotonic()
                with self.assertRaisesRegex(TimeoutError, "维护锁超时"):
                    with parent_store.maintenance_lock(exclusive=True, timeout=0.15):
                        self.fail("持锁期间第二个进程意外进入维护窗口")
                self.assertGreaterEqual(time.monotonic() - started, 0.1)
            finally:
                release.set()
                child.join(10)
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            self.assertEqual(0, child.exitcode)
            with parent_store.maintenance_lock(exclusive=True, timeout=1):
                self.assertTrue(parent_store.owns_exclusive_lock())
                self.assertTrue(child_store.maintenance_gate("database") is None)

    def test_only_one_process_can_claim_a_ready_migration_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, ".maintenance", "storage-jobs.sqlite3")
            jobs = StorageJobStore(path)
            for kind in ("cache_migration", "database_migration"):
                with self.subTest(kind=kind):
                    job, created = jobs.create(kind, {"fingerprint": "same"},
                                               idempotency_key=f"one-check-{kind}")
                    self.assertTrue(created)
                    jobs.update(job["job_id"], status="READY", stage="READY")

                    context = multiprocessing.get_context("spawn")
                    barrier = context.Barrier(3)
                    results = context.Queue()
                    processes = [context.Process(target=_claim_ready_job,
                                                 args=(path, job["job_id"], barrier, results))
                                 for _ in range(2)]
                    try:
                        for process in processes:
                            process.start()
                        barrier.wait(10)
                        outcomes = [results.get(timeout=10) for _ in processes]
                    finally:
                        for process in processes:
                            process.join(10)
                            if process.is_alive():
                                process.terminate()
                                process.join(5)
                        results.close()

                    self.assertEqual([False, True], sorted(outcomes))
                    self.assertTrue(all(process.exitcode == 0 for process in processes))
                    self.assertEqual("QUIESCING", jobs.get(job["job_id"])["status"])
