"""媒体统计唯一索引字段升级必须先检查旧数据长度。"""

import os
import tempfile
from unittest import TestCase

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, String, Table, Text, create_engine, inspect

from db_scripts.versions.f6a7b8c9d0e1_bound_media_server_key import upgrade


class DatabaseMediaSchemaUpgradeTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "database.sqlite3")
        self.engine = create_engine(f"sqlite:///{self.path}")
        self.metadata = MetaData()
        self.table = Table(
            "MEDIASYNC_STATISTICS", self.metadata,
            Column("ID", Integer, primary_key=True),
            Column("SERVER", Text, nullable=True),
        )
        self.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.directory.cleanup()

    def _upgrade(self):
        with self.engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                upgrade()

    def test_upgrade_bounds_server_key_and_preserves_values(self):
        with self.engine.begin() as connection:
            connection.execute(self.table.insert(), [
                {"ID": 1, "SERVER": "emby"},
                {"ID": 2, "SERVER": None},
            ])

        self._upgrade()

        server = next(column for column in inspect(self.engine).get_columns(
            "MEDIASYNC_STATISTICS") if column["name"] == "SERVER")
        self.assertEqual(128, server["type"].length)
        with self.engine.connect() as connection:
            self.assertEqual([("emby",), (None,)], connection.exec_driver_sql(
                "SELECT SERVER FROM MEDIASYNC_STATISTICS ORDER BY ID").all())

    def test_upgrade_refuses_overlong_legacy_server_value(self):
        with self.engine.begin() as connection:
            connection.execute(self.table.insert(), {"ID": 1, "SERVER": "x" * 129})

        with self.assertRaisesRegex(RuntimeError, "拒绝截断"):
            self._upgrade()

        server = next(column for column in inspect(self.engine).get_columns(
            "MEDIASYNC_STATISTICS") if column["name"] == "SERVER")
        self.assertIsInstance(server["type"], Text)
        with self.engine.connect() as connection:
            self.assertEqual(129, len(connection.exec_driver_sql(
                "SELECT SERVER FROM MEDIASYNC_STATISTICS").scalar_one()))


if __name__ == "__main__":
    import unittest

    unittest.main()
