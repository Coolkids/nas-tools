"""SQLite 单库后端。"""

import os

from sqlalchemy import URL, create_engine, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.pool import QueuePool

from app.db.contracts import DatabaseBackend


class SQLiteBackend(DatabaseBackend):
    name = "sqlite"
    supports_space_reclaim = True

    @staticmethod
    def path():
        from config import Config
        return os.path.join(Config().get_config_path(), "db", "nastools.db")

    def create_engine(self, settings):
        path = self.path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return create_engine(
            URL.create("sqlite", database=path),
            echo=False,
            poolclass=QueuePool,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
            pool_timeout=60,
            connect_args={"timeout": 60, "check_same_thread": False},
        )

    def configure_connection(self, connection):
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
        finally:
            cursor.close()

    def initialize(self, engine):
        with engine.connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))

    def reclaim_space(self, engine):
        if engine.pool.checkedout():
            raise RuntimeError("SQLite 仍有业务连接，不能回收空间")
        connection = engine.raw_connection()
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    def upsert_many(self, session, model, rows, conflict_columns, update_columns=None):
        if not rows:
            return 0
        rows, conflict_columns = self._portable_rows(
            session, model, rows, conflict_columns)
        statement = sqlite_insert(model.__table__).values(rows)
        if update_columns:
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={column: getattr(statement.excluded, column)
                      for column in update_columns})
        else:
            statement = statement.on_conflict_do_nothing(
                index_elements=list(conflict_columns))
        session.execute(statement)
        return len(rows)
