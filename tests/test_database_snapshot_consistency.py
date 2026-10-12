"""数据库迁移快照提供跨逻辑域的一致多语句读取视图。"""

import os
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import text

from app.db.manager import DATABASE
from app.db.migration import _snapshot_source
from app.db.models import Base


class DatabaseSnapshotConsistencyTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config = SimpleNamespace(
            get_config_path=lambda: self.directory.name,
            get_config=lambda node=None: {"type": "sqlite"} if node == "database" else {},
        )
        self.config_patch = patch("config.Config", return_value=config)
        self.config_patch.start()
        DATABASE.dispose()
        Base.metadata.create_all(DATABASE.engine)
        with DATABASE.engine.begin() as connection:
            connection.execute(text("INSERT INTO TRANSFER_HISTORY "
                                    "(ID, TITLE) VALUES (901, 'snapshot title')"))
            connection.execute(text("INSERT INTO MEDIASYNC_ITEMS "
                                    "(ID, SERVER, ITEM_ID, TITLE) "
                                    "VALUES (901, 'emby', 'snapshot-item', 'snapshot media')"))

    def tearDown(self):
        DATABASE.dispose()
        self.config_patch.stop()
        self.directory.cleanup()

    def test_sqlite_snapshot_keeps_both_logical_domains_on_one_revision(self):
        source_engine = source_connection = source_transaction = None
        snapshot_path = None
        try:
            source_engine, source_connection, source_transaction, snapshot_path = \
                _snapshot_source({"type": "sqlite"}, "snapshot-consistency")
            first_read = source_connection.execute(text(
                "SELECT TITLE FROM TRANSFER_HISTORY WHERE ID=901")).scalar_one()

            with DATABASE.engine.begin() as connection:
                connection.execute(text("UPDATE TRANSFER_HISTORY SET TITLE='changed after snapshot' "
                                        "WHERE ID=901"))
                connection.execute(text("UPDATE MEDIASYNC_ITEMS SET TITLE='changed after snapshot' "
                                        "WHERE ID=901"))

            second_read = source_connection.execute(text(
                "SELECT TITLE FROM MEDIASYNC_ITEMS WHERE ID=901")).scalar_one()
            self.assertEqual("snapshot title", first_read)
            self.assertEqual("snapshot media", second_read)
            with DATABASE.engine.connect() as connection:
                self.assertEqual("changed after snapshot", connection.execute(text(
                    "SELECT TITLE FROM TRANSFER_HISTORY WHERE ID=901")).scalar_one())
                self.assertEqual("changed after snapshot", connection.execute(text(
                    "SELECT TITLE FROM MEDIASYNC_ITEMS WHERE ID=901")).scalar_one())
        finally:
            if source_transaction is not None and source_transaction.is_active:
                source_transaction.rollback()
            if source_connection is not None:
                source_connection.close()
            if source_engine is not None:
                source_engine.dispose()
            if snapshot_path:
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(snapshot_path + suffix)
                    except FileNotFoundError:
                        pass
