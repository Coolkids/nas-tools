"""SQLite 整库空间回收的隔离回归测试。"""

import os
import sqlite3
import tempfile
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import text

from app.db.maintenance import _check_checkpoint, start_database_space_reclaim
from app.db.manager import DATABASE
from app.db.models import Base
from app.db.schema import current_schema_revision
from app.utils.storage_jobs import StorageJobStore


class DatabaseSpaceReclaimTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = os.path.join(self.directory.name, "db", "nastools.db")
        config = SimpleNamespace(
            get_config_path=lambda: self.directory.name,
            get_config=lambda node=None: {"type": "sqlite"} if node == "database" else {},
        )
        self.config_patch = patch("config.Config", return_value=config)
        self.config_patch.start()
        self.jobs = StorageJobStore(os.path.join(self.directory.name, ".maintenance", "jobs.sqlite3"))
        self.job_patches = [
            patch("app.db.maintenance.STORAGE_JOBS", self.jobs),
            patch("app.db.migration.STORAGE_JOBS", self.jobs),
            patch("app.db.manager.STORAGE_JOBS", self.jobs),
        ]
        for job_patch in self.job_patches:
            job_patch.start()

        DATABASE.dispose()
        engine = DATABASE.engine
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version(version_num) VALUES (:revision)"),
                               {"revision": current_schema_revision()})
            connection.execute(text("""INSERT INTO MEDIASYNC_ITEMS
                (ID, SERVER, ITEM_ID, TITLE, JSON)
                VALUES (:id, 'server', :item_id, :title, :payload)"""), [
                {"id": item_id, "item_id": f"item-{item_id}",
                 "title": f"title-{item_id}", "payload": "x" * 32768}
                for item_id in range(1, 101)
            ])
            connection.execute(text("DELETE FROM MEDIASYNC_ITEMS WHERE ID > 5"))
        DATABASE.close_session()

    def tearDown(self):
        DATABASE.dispose()
        for job_patch in reversed(self.job_patches):
            job_patch.stop()
        self.config_patch.stop()
        self.directory.cleanup()

    def test_reclaim_compacts_fixed_database_and_preserves_rows(self):
        before_size = sum(os.path.getsize(self.database_path + suffix)
                          for suffix in ("", "-wal", "-shm")
                          if os.path.exists(self.database_path + suffix))
        with sqlite3.connect(self.database_path) as connection:
            before = connection.execute(
                "SELECT ID, ITEM_ID, TITLE, length(JSON) FROM MEDIASYNC_ITEMS ORDER BY ID"
            ).fetchall()
            self.assertGreater(connection.execute(
                "PRAGMA freelist_count").fetchone()[0], 0)

        result = start_database_space_reclaim()
        self.assertEqual(0, result["code"])
        job_id = result["job_id"]
        deadline = time.monotonic() + 30
        job = self.jobs.get(job_id)
        while job["status"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            if time.monotonic() >= deadline:
                self.fail(f"空间回收任务超时：{job}")
            time.sleep(0.05)
            job = self.jobs.get(job_id)

        self.assertEqual("SUCCEEDED", job["status"], job.get("error"))
        self.assertTrue(job["result"]["vacuum_completed"])
        self.assertGreater(job["result"]["before"]["freelist_count"], 0)
        after_size = sum(os.path.getsize(self.database_path + suffix)
                         for suffix in ("", "-wal", "-shm")
                         if os.path.exists(self.database_path + suffix))
        self.assertLess(after_size, before_size)
        with sqlite3.connect(self.database_path) as connection:
            after = connection.execute(
                "SELECT ID, ITEM_ID, TITLE, length(JSON) FROM MEDIASYNC_ITEMS ORDER BY ID"
            ).fetchall()
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(before[:5], after)
        with DATABASE.engine.begin() as connection:
            connection.execute(text("""INSERT INTO MEDIASYNC_ITEMS
                (ID, SERVER, ITEM_ID, TITLE, JSON)
                VALUES (101, 'server', 'item-101', 'title-101', 'new')"""))
            self.assertEqual(6, connection.execute(
                text("SELECT count(*) FROM MEDIASYNC_ITEMS")).scalar_one())

    def test_rejects_file_or_scope_overrides(self):
        for values in ({"path": "/tmp/other.db"}, {"domain": "media"}, {"table": "MEDIASYNC_ITEMS"}):
            with self.subTest(values=values):
                result = start_database_space_reclaim(values)
                self.assertEqual(1, result["code"])
        self.assertIsNone(self.jobs.latest("database_space_reclaim"))

    def test_checkpoint_must_be_complete(self):
        _check_checkpoint((0, 3, 3), "测试")
        with self.assertRaisesRegex(RuntimeError, "未完成"):
            _check_checkpoint((1, 3, 3), "测试")
        with self.assertRaisesRegex(RuntimeError, "未完整写入"):
            _check_checkpoint((0, 3, 2), "测试")
