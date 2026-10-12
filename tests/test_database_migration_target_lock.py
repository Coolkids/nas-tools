"""SQLite 旧目标文件必须在快照与退役期间排除并发写入。"""

import os
import sqlite3
import tempfile
from unittest import TestCase

from app.db.migration import _lock_sqlite_source


class DatabaseMigrationTargetLockTest(TestCase):
    def test_refuses_to_lock_a_sqlite_file_with_an_active_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "legacy.db")
            initial = sqlite3.connect(path)
            initial.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
            initial.commit()
            initial.close()

            writer = sqlite3.connect(path, isolation_level=None)
            writer.execute("BEGIN EXCLUSIVE")
            try:
                with self.assertRaisesRegex(RuntimeError, "仍被其他进程占用"):
                    _lock_sqlite_source(path, timeout=0.01)
            finally:
                writer.rollback()
                writer.close()

    def test_returns_a_locking_connection_until_caller_releases_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "legacy.db")
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE example (id INTEGER PRIMARY KEY)")
            connection.commit()
            connection.close()

            locked = _lock_sqlite_source(path, timeout=0.1)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    contender = sqlite3.connect(path, timeout=0.01, isolation_level=None)
                    try:
                        contender.execute("BEGIN EXCLUSIVE")
                    finally:
                        contender.close()
            finally:
                locked.rollback()
                locked.close()
