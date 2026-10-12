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
