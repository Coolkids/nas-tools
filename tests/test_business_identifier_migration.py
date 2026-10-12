"""业务标识符长度收敛迁移的隔离回归测试。"""

import importlib.util
from pathlib import Path
import unittest

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db_scripts" / "versions" / \
    "f7a8b9c0d1e2_bound_business_identifiers.py"
SPEC = importlib.util.spec_from_file_location("business_identifier_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class BusinessIdentifierMigrationTest(unittest.TestCase):
    @staticmethod
    def _run_upgrade(connection):
        with Operations.context(MigrationContext.configure(connection)):
            MIGRATION.upgrade()

    def test_overlong_legacy_identifier_aborts_before_any_column_is_altered(self):
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE DOWNLOAD_HISTORY (ID INTEGER PRIMARY KEY, TMDBID TEXT)")
                connection.exec_driver_sql(
                    "CREATE TABLE SEARCH_RESULT_INFO "
                    "(ID INTEGER PRIMARY KEY, TMDBID TEXT, SIZE INTEGER)")
                connection.exec_driver_sql(
                    "INSERT INTO DOWNLOAD_HISTORY (ID, TMDBID) VALUES (1, :value)",
                    {"value": "x" * 65})

                with self.assertRaisesRegex(RuntimeError, "拒绝截断"):
                    self._run_upgrade(connection)

                columns = {
                    table: {item["name"]: item["type"]
                            for item in sa.inspect(connection).get_columns(table)}
                    for table in ("DOWNLOAD_HISTORY", "SEARCH_RESULT_INFO")
                }
                self.assertEqual("TEXT", str(columns["DOWNLOAD_HISTORY"]["TMDBID"]).upper())
                self.assertEqual("INTEGER", str(columns["SEARCH_RESULT_INFO"]["SIZE"]).upper())
        finally:
            engine.dispose()

    def test_valid_identifiers_are_bounded_and_size_is_text(self):
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE TABLE DOWNLOAD_HISTORY (ID INTEGER PRIMARY KEY, TMDBID TEXT)")
                connection.exec_driver_sql(
                    "CREATE TABLE SEARCH_RESULT_INFO "
                    "(ID INTEGER PRIMARY KEY, TMDBID TEXT, SIZE INTEGER)")
                connection.exec_driver_sql(
                    "INSERT INTO DOWNLOAD_HISTORY (ID, TMDBID) VALUES (1, '12345')")
                connection.exec_driver_sql(
                    "INSERT INTO SEARCH_RESULT_INFO (ID, TMDBID, SIZE) "
                    "VALUES (1, '12345', 42)")
                self._run_upgrade(connection)

                columns = {
                    table: {item["name"]: item["type"]
                            for item in sa.inspect(connection).get_columns(table)}
                    for table in ("DOWNLOAD_HISTORY", "SEARCH_RESULT_INFO")
                }
                self.assertEqual("VARCHAR(64)",
                                 str(columns["DOWNLOAD_HISTORY"]["TMDBID"]).upper())
                self.assertEqual("VARCHAR(64)",
                                 str(columns["SEARCH_RESULT_INFO"]["TMDBID"]).upper())
                self.assertEqual("TEXT", str(columns["SEARCH_RESULT_INFO"]["SIZE"]).upper())
                self.assertEqual("42", connection.exec_driver_sql(
                    "SELECT SIZE FROM SEARCH_RESULT_INFO WHERE ID = 1").scalar_one())
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
