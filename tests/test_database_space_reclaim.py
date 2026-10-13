"""SQLite 整库空间回收的隔离回归测试。"""

import os
import multiprocessing
import sqlite3
import tempfile
import time
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import text

import app.db.maintenance as maintenance
from app.db.maintenance import _check_checkpoint, start_database_space_reclaim
from app.db.manager import DATABASE
from app.db.models import Base
from app.db.schema import LOGICAL_DOMAIN_TABLES, current_schema_revision
from app.utils.storage_jobs import StorageJobStore


def _pause_inside_vacuum(db_path, progress_connection):
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    announced = False

    def report_progress():
        nonlocal announced
        if not announced:
            announced = True
            progress_connection.send("vacuum-active")
            progress_connection.recv()
        return 0

    connection.set_progress_handler(report_progress, 1000)
    connection.execute("VACUUM")
    connection.close()


def _hold_sqlite_read_transaction(db_path, ready, release):
    connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        connection.execute("BEGIN")
        connection.execute("SELECT count(*) FROM MEDIASYNC_ITEMS").fetchone()
        ready.set()
        release.wait(30)
        connection.rollback()
    finally:
        connection.close()


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

    def _wait_for_job(self, job_id):
        deadline = time.monotonic() + 30
        job = self.jobs.get(job_id)
        while job["status"] not in {
                "SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED", "RECOVERY_REQUIRED"}:
            if time.monotonic() >= deadline:
                self.fail(f"空间回收任务超时：{job}")
            time.sleep(0.05)
            job = self.jobs.get(job_id)
        worker = maintenance._WORKERS.get(job_id)
        if worker:
            worker.join(timeout=5)
        return self.jobs.get(job_id)

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
        job = self._wait_for_job(job_id)

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

    def test_empty_database_without_free_pages_is_a_valid_reclaim(self):
        with sqlite3.connect(self.database_path, isolation_level=None) as connection:
            connection.execute("DELETE FROM MEDIASYNC_ITEMS")
            connection.execute("VACUUM")
            self.assertEqual(0, connection.execute("PRAGMA freelist_count").fetchone()[0])

        result = start_database_space_reclaim()
        self.assertEqual(0, result["code"])
        job = self._wait_for_job(result["job_id"])

        self.assertEqual("SUCCEEDED", job["status"], job.get("error"))
        self.assertTrue(job["result"]["vacuum_completed"])
        self.assertEqual(0, job["result"]["before"]["freelist_count"])
        self.assertEqual(0, job["result"]["after"]["freelist_count"])
        self.assertEqual(0, job["result"]["after"]["freed_bytes"])
        self.assertIsNone(self.jobs.maintenance_gate("database"))

    def test_insufficient_volume_space_fails_before_vacuum_and_reopens_database(self):
        with patch("app.db.maintenance.shutil.disk_usage",
                   return_value=SimpleNamespace(total=1024, used=1024, free=0)):
            result = start_database_space_reclaim()
            self.assertEqual(0, result["code"])
            job = self._wait_for_job(result["job_id"])

        self.assertEqual("FAILED", job["status"])
        self.assertIn("可用空间不足", job["error"])
        self.assertFalse(job["result"]["vacuum_completed"])
        self.assertIsNone(self.jobs.maintenance_gate("database"))
        with DATABASE.engine.connect() as connection:
            self.assertEqual(5, connection.execute(text(
                "SELECT count(*) FROM MEDIASYNC_ITEMS")).scalar_one())
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])

    def test_long_reader_prevents_truncate_checkpoint_without_losing_database(self):
        reader = sqlite3.connect(self.database_path, timeout=1, isolation_level=None)
        checkpoint_connection = None
        try:
            self.assertEqual("wal", reader.execute("PRAGMA journal_mode").fetchone()[0].lower())
            reader.execute("BEGIN")
            reader.execute("SELECT count(*) FROM MEDIASYNC_ITEMS").fetchone()
            with DATABASE.engine.begin() as connection:
                connection.execute(text("""INSERT INTO MEDIASYNC_ITEMS
                    (ID, SERVER, ITEM_ID, TITLE, JSON)
                    VALUES (101, 'server', 'item-101', 'title-101', 'new')"""))

            checkpoint_connection = sqlite3.connect(
                self.database_path, timeout=0, isolation_level=None)
            checkpoint_connection.execute("PRAGMA busy_timeout=0")
            checkpoint = checkpoint_connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            with self.assertRaisesRegex(RuntimeError, "WAL checkpoint 未完成"):
                _check_checkpoint(checkpoint, "长读事务")
        finally:
            if checkpoint_connection:
                checkpoint_connection.close()
            reader.rollback()
            reader.close()

        with DATABASE.engine.connect() as connection:
            self.assertEqual(6, connection.execute(text(
                "SELECT count(*) FROM MEDIASYNC_ITEMS")).scalar_one())
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])

    def test_other_process_read_lock_blocks_reclaim_before_vacuum(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        reader = context.Process(
            target=_hold_sqlite_read_transaction,
            args=(self.database_path, ready, release))
        reader.start()
        try:
            self.assertTrue(ready.wait(10), "子进程未能持有 SQLite 读事务")
            result = start_database_space_reclaim()
            self.assertEqual(0, result["code"])
            job = self._wait_for_job(result["job_id"])
            self.assertEqual("FAILED", job["status"], job.get("error"))
            self.assertIn("VACUUM 前 WAL checkpoint 未完成", job["error"])
            self.assertFalse(job["result"]["vacuum_completed"])
            self.assertIsNone(self.jobs.maintenance_gate("database"))
        finally:
            release.set()
            reader.join(10)
            if reader.is_alive():
                reader.terminate()
                reader.join(5)
        self.assertEqual(0, reader.exitcode)
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual("ok", connection.execute(
                "PRAGMA integrity_check").fetchone()[0])

    def test_reclaim_preserves_committed_wal_rows_and_restores_wal_mode(self):
        writer = sqlite3.connect(self.database_path, timeout=1, isolation_level=None)
        try:
            self.assertEqual("wal", writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
            writer.execute("UPDATE MEDIASYNC_ITEMS SET TITLE='committed in WAL' WHERE ID=1")
            self.assertTrue(os.path.isfile(self.database_path + "-wal"))
            self.assertEqual("committed in WAL", writer.execute(
                "SELECT TITLE FROM MEDIASYNC_ITEMS WHERE ID=1").fetchone()[0])

            result = start_database_space_reclaim()
            self.assertEqual(0, result["code"])
            job = self._wait_for_job(result["job_id"])
            self.assertEqual("SUCCEEDED", job["status"], job.get("error"))
            self.assertTrue(job["result"]["vacuum_completed"])
            self.assertEqual("committed in WAL", writer.execute(
                "SELECT TITLE FROM MEDIASYNC_ITEMS WHERE ID=1").fetchone()[0])
        finally:
            writer.close()

        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0].lower())
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])

    def test_database_statistics_keep_main_and_media_logical_domains_distinct(self):
        response = maintenance.get_database_info()
        self.assertEqual(0, response["code"])
        database = response["database"]
        self.assertEqual(set(LOGICAL_DOMAIN_TABLES), set(database["logical_domains"]))
        for domain, table_names in LOGICAL_DOMAIN_TABLES.items():
            with self.subTest(domain=domain):
                self.assertEqual(
                    {name: database["tables"].get(name) for name in table_names},
                    database["logical_domains"][domain])
        self.assertNotIn("MEDIASYNC_ITEMS", database["logical_domains"]["main"])
        self.assertIn("MEDIASYNC_ITEMS", database["logical_domains"]["media"])

    def test_cancel_before_vacuum_reopens_database_without_mutation(self):
        original_check = maintenance._check_checkpoint

        def request_cancel_after_initial_checkpoint(result, stage):
            if stage == "VACUUM 前":
                job = self.jobs.latest("database_space_reclaim")
                self.assertIsNotNone(job)
                self.assertTrue(self.jobs.request_cancel(job["job_id"]))
            return original_check(result, stage)

        with patch.object(maintenance, "_check_checkpoint",
                          side_effect=request_cancel_after_initial_checkpoint):
            result = start_database_space_reclaim()
            self.assertEqual(0, result["code"])
            job = self._wait_for_job(result["job_id"])

        self.assertEqual("CANCELLED", job["status"], job.get("error"))
        self.assertFalse(job["result"]["vacuum_completed"])
        self.assertIsNone(self.jobs.maintenance_gate("database"))
        with DATABASE.engine.connect() as connection:
            self.assertEqual(5, connection.execute(text(
                "SELECT count(*) FROM MEDIASYNC_ITEMS")).scalar_one())
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual("ok", connection.execute(
                "PRAGMA integrity_check").fetchone()[0])

    def test_post_vacuum_checkpoint_failure_reports_completed_vacuum(self):
        original_check = maintenance._check_checkpoint
        calls = []

        def fail_after_vacuum_checkpoint(result, stage):
            calls.append(stage)
            if stage == "VACUUM 后":
                raise RuntimeError("injected post-vacuum checkpoint failure")
            return original_check(result, stage)

        with patch.object(maintenance, "_check_checkpoint",
                          side_effect=fail_after_vacuum_checkpoint):
            result = start_database_space_reclaim()
            self.assertEqual(0, result["code"])
            job = self._wait_for_job(result["job_id"])

        self.assertEqual(["VACUUM 前", "VACUUM 后"], calls)
        self.assertEqual("FAILED", job["status"])
        self.assertIn("injected post-vacuum checkpoint failure", job["error"])
        self.assertTrue(job["result"]["vacuum_completed"])
        self.assertIsNone(self.jobs.maintenance_gate("database"))
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        with DATABASE.engine.begin() as connection:
            connection.execute(text("INSERT INTO MEDIASYNC_ITEMS "
                                    "(ID, SERVER, ITEM_ID, TITLE) "
                                    "VALUES (101, 'server', 'after-vacuum', '可写')"))

    def test_startup_recovers_an_interrupted_reclaim_after_integrity_check(self):
        from app.db.migration import _current_settings, _fingerprint

        job, created = self.jobs.create(
            "database_space_reclaim",
            {"fingerprint": _fingerprint(_current_settings())},
            idempotency_key="interrupted-reclaim")
        self.assertTrue(created)
        self.jobs.update(job["job_id"], status="VACUUMING", stage="VACUUMING")
        with self.jobs.maintenance_lock(exclusive=True):
            self.jobs.set_maintenance_gate("database", job["job_id"], "VACUUMING")

        with self.jobs.maintenance_lock(exclusive=True):
            self.assertTrue(maintenance.verify_database_maintenance_startup(
                self.jobs.get(job["job_id"])))

        recovered = self.jobs.get(job["job_id"])
        self.assertEqual("INTERRUPTED", recovered["status"])
        self.assertIsNone(self.jobs.maintenance_gate("database"))
        with DATABASE.engine.connect() as connection:
            self.assertEqual(5, connection.execute(text(
                "SELECT count(*) FROM MEDIASYNC_ITEMS")).scalar_one())

    def test_process_killed_inside_vacuum_recovers_committed_rows_on_startup(self):
        from app.db.migration import _current_settings, _fingerprint

        payload = "vacuum-crash-payload-" + "x" * 32768
        with sqlite3.connect(self.database_path) as connection:
            connection.executemany(
                "INSERT INTO MEDIASYNC_ITEMS (ID, SERVER, ITEM_ID, TITLE, JSON) "
                "VALUES (?, 'server', ?, 'vacuum-crash', ?)",
                ((item_id, f"crash-{item_id}", payload)
                 for item_id in range(1000, 2200)))
            connection.execute(
                "DELETE FROM MEDIASYNC_ITEMS WHERE ID >= 1000 AND ID % 2 = 0")
            self.assertGreater(connection.execute("PRAGMA freelist_count").fetchone()[0], 0)

        job, created = self.jobs.create(
            "database_space_reclaim",
            {"fingerprint": _fingerprint(_current_settings())},
            idempotency_key="kill-during-vacuum")
        self.assertTrue(created)
        self.jobs.update(job["job_id"], status="VACUUMING", stage="VACUUMING")
        with self.jobs.maintenance_lock(exclusive=True):
            self.jobs.set_maintenance_gate("database", job["job_id"], "VACUUMING")

        DATABASE.dispose()
        context = multiprocessing.get_context("spawn")
        parent_progress, child_progress = context.Pipe()
        process = context.Process(
            target=_pause_inside_vacuum,
            args=(self.database_path, child_progress))
        process.start()
        child_progress.close()
        try:
            self.assertTrue(parent_progress.poll(30), "VACUUM 未进入 SQLite 执行进度回调")
            self.assertEqual("vacuum-active", parent_progress.recv())
            process.kill()
            process.join(10)
        finally:
            parent_progress.close()
            if process.is_alive():
                process.kill()
                process.join(5)
        self.assertNotEqual(0, process.exitcode)

        with self.jobs.maintenance_lock(exclusive=True):
            self.assertTrue(maintenance.verify_database_maintenance_startup(
                self.jobs.get(job["job_id"])))
        recovered = self.jobs.get(job["job_id"])
        self.assertEqual("INTERRUPTED", recovered["status"])
        self.assertIsNone(self.jobs.maintenance_gate("database"))
        with sqlite3.connect(self.database_path) as connection:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(605, connection.execute(
                "SELECT count(*) FROM MEDIASYNC_ITEMS").fetchone()[0])

    def test_read_only_database_keeps_gate_until_database_can_be_opened(self):
        DATABASE.dispose()
        os.chmod(self.database_path, 0o444)
        try:
            if os.access(self.database_path, os.W_OK):
                self.skipTest("当前测试用户可绕过只读文件权限")
            result = start_database_space_reclaim()
            self.assertEqual(0, result["code"])
            job = self._wait_for_job(result["job_id"])
            self.assertEqual("RECOVERY_REQUIRED", job["status"], job.get("error"))
            self.assertEqual(job["job_id"], self.jobs.maintenance_gate("database")["job_id"])
        finally:
            os.chmod(self.database_path, 0o600)

    def test_unwritable_database_directory_keeps_maintenance_gate(self):
        with patch.object(maintenance.tempfile, "mkstemp",
                          side_effect=PermissionError("injected read-only database directory")):
            result = start_database_space_reclaim()
            self.assertEqual(0, result["code"])
            job = self._wait_for_job(result["job_id"])

        self.assertEqual("RECOVERY_REQUIRED", job["status"], job.get("error"))
        self.assertIn("数据库不可写", job["error"])
        self.assertEqual(job["job_id"], self.jobs.maintenance_gate("database")["job_id"])

    def test_rejects_file_or_scope_overrides(self):
        for values in ({"path": "/tmp/other.db"}, {"domain": "media"}, {"table": "MEDIASYNC_ITEMS"}):
            with self.subTest(values=values):
                result = start_database_space_reclaim(values)
                self.assertEqual(1, result["code"])
        self.assertIsNone(self.jobs.latest("database_space_reclaim"))

    def test_rejects_remote_database_and_stale_configuration_generation(self):
        with patch("app.db.maintenance._settings", return_value=(
                {"type": "postgresql"}, "remote-fingerprint", {"type": "postgresql"})):
            result = start_database_space_reclaim()
        self.assertEqual(1, result["code"])
        self.assertIn("不支持 SQLite", result["msg"])
        self.assertIsNone(self.jobs.latest("database_space_reclaim"))

        job, created = self.jobs.create(
            "database_space_reclaim", {"fingerprint": "checked-generation"},
            idempotency_key="stale-reclaim-configuration")
        self.assertTrue(created)
        with patch("app.db.migration._current_settings",
                   return_value={"type": "sqlite", "path": "changed"}):
            maintenance._run_space_reclaim(job["job_id"], "checked-generation")
        stale = self.jobs.get(job["job_id"])
        self.assertEqual("FAILED", stale["status"])
        self.assertIn("配置已变化", stale["error"])
        self.assertIsNone(self.jobs.maintenance_gate("database"))

    def test_duplicate_reclaim_submission_reuses_one_persistent_job(self):
        first = start_database_space_reclaim()
        second = start_database_space_reclaim()
        self.assertEqual(0, first["code"])
        self.assertEqual(0, second["code"])
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(1, len(self.jobs.active("database_space_reclaim")))
        self._wait_for_job(first["job_id"])

    def test_checkpoint_must_be_complete(self):
        _check_checkpoint((0, 3, 3), "测试")
        with self.assertRaisesRegex(RuntimeError, "未完成"):
            _check_checkpoint((1, 3, 3), "测试")
        with self.assertRaisesRegex(RuntimeError, "未完整写入"):
            _check_checkpoint((0, 3, 2), "测试")
