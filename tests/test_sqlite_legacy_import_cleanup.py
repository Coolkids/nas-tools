"""合并旧 SQLite 快照时应用一次性站点数据清理。"""

import sqlite3
import os
import tempfile
from unittest import TestCase

from app.db.sqlite_files import cleanup_legacy_startup_data


class SQLiteLegacyImportCleanupTest(TestCase):
    def test_clears_retired_site_selections_and_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = sqlite3.connect(os.path.join(directory, "legacy.sqlite3"))
            try:
                connection.executescript("""
                    CREATE TABLE RSS_MOVIES (ID INTEGER PRIMARY KEY, RSS_SITES TEXT);
                    CREATE TABLE RSS_TVS (ID INTEGER PRIMARY KEY, RSS_SITES TEXT);
                    CREATE TABLE SYSTEM_DICT (ID INTEGER PRIMARY KEY, TYPE TEXT, KEY TEXT, VALUE TEXT);
                    INSERT INTO RSS_MOVIES VALUES (1, '["site-a"]');
                    INSERT INTO RSS_TVS VALUES (2, '["site-b"]');
                    INSERT INTO SYSTEM_DICT VALUES (3, 'SystemConfig', 'CookieUserInfo', 'secret');
                    INSERT INTO SYSTEM_DICT VALUES (4, 'SystemConfig', 'CookieCloud', 'secret');
                    INSERT INTO SYSTEM_DICT VALUES (5, 'Other', 'CookieCloud', 'keep');
                """)

                cleanup_legacy_startup_data(connection)

                self.assertEqual([(None,)], connection.execute(
                    "SELECT RSS_SITES FROM RSS_MOVIES").fetchall())
                self.assertEqual([(None,)], connection.execute(
                    "SELECT RSS_SITES FROM RSS_TVS").fetchall())
                self.assertEqual([(5,)], connection.execute(
                    "SELECT ID FROM SYSTEM_DICT ORDER BY ID").fetchall())
            finally:
                connection.close()
