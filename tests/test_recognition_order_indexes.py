"""识别记录稳定分页索引升级回归。"""

import importlib.util
from pathlib import Path
import unittest

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "db_scripts" / "versions" / \
    "f8b9c0d1e2f3_recognition_order_indexes.py"
SPEC = importlib.util.spec_from_file_location("recognition_order_indexes", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class RecognitionOrderIndexesTest(unittest.TestCase):
    def test_upgrade_replaces_redundant_single_column_indexes_repeatably(self):
        engine = sa.create_engine("sqlite:///:memory:")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("""CREATE TABLE RECOGNITION_REQUEST (
                    REQUEST_ID VARCHAR(128) PRIMARY KEY, SOURCE TEXT, CREATED_AT TEXT)""")
                connection.exec_driver_sql(
                    "CREATE INDEX ix_recognition_request_source "
                    "ON RECOGNITION_REQUEST(SOURCE)")
                connection.exec_driver_sql(
                    "CREATE INDEX ix_recognition_request_created_at "
                    "ON RECOGNITION_REQUEST(CREATED_AT)")
                connection.exec_driver_sql("""INSERT INTO RECOGNITION_REQUEST VALUES
                    ('request-1', 'manual', '2026-10-13 10:00:00')""")

                with Operations.context(MigrationContext.configure(connection)):
                    MIGRATION.upgrade()
                    MIGRATION.upgrade()

                indexes = {item["name"]: item for item in
                           sa.inspect(connection).get_indexes("RECOGNITION_REQUEST")}
                self.assertEqual(
                    ["CREATED_AT", "REQUEST_ID"],
                    indexes["IDX_RECOG_CREATED_REQUEST"]["column_names"])
                self.assertEqual(
                    ["SOURCE", "CREATED_AT", "REQUEST_ID"],
                    indexes["IDX_RECOG_SOURCE_CREATED_REQUEST"]["column_names"])
                self.assertNotIn("ix_recognition_request_source", indexes)
                self.assertNotIn("ix_recognition_request_created_at", indexes)
                self.assertEqual(
                    ("request-1", "manual", "2026-10-13 10:00:00"),
                    connection.exec_driver_sql(
                        "SELECT REQUEST_ID, SOURCE, CREATED_AT FROM RECOGNITION_REQUEST"
                    ).one())

                with Operations.context(MigrationContext.configure(connection)):
                    MIGRATION.downgrade()
                downgraded = {item["name"] for item in
                              sa.inspect(connection).get_indexes("RECOGNITION_REQUEST")}
                self.assertIn("ix_recognition_request_source", downgraded)
                self.assertIn("ix_recognition_request_created_at", downgraded)
                self.assertNotIn("IDX_RECOG_CREATED_REQUEST", downgraded)
                self.assertNotIn("IDX_RECOG_SOURCE_CREATED_REQUEST", downgraded)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
