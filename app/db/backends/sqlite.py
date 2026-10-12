"""SQLite 单库后端。"""

import os
import sqlite3

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

    @staticmethod
    def capabilities(engine):
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
        return {"product": "SQLite", "version": sqlite3.sqlite_version}

    def estimated_table_counts(self, engine, metadata):
        estimates = {}
        reasons = {}
        with engine.connect() as connection:
            has_stats = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'").first()
            if has_stats:
                for table_name, statistic in connection.exec_driver_sql(
                        "SELECT tbl, stat FROM sqlite_stat1").all():
                    try:
                        rows = int(str(statistic).split()[0])
                    except (TypeError, ValueError, IndexError):
                        continue
                    key = str(table_name).upper()
                    estimates[key] = max(estimates.get(key, 0), rows)
            else:
                reasons["_all"] = "尚无 SQLite 查询规划统计；可异步刷新精确记录数"
        for table in metadata.sorted_tables:
            if table.name.upper() not in estimates:
                reasons[table.name] = reasons.get(
                    "_all", "数据库未提供该表的记录数估算；可异步刷新精确记录数")
        return ({table.name: estimates.get(table.name.upper())
                 for table in metadata.sorted_tables}, reasons)

    def storage_usage(self, engine, metadata):
        path = engine.url.database
        connection = sqlite3.connect(path, timeout=10)
        try:
            rows = connection.execute("""SELECT objects.type, SUM(pages.bytes)
                FROM sqlite_master AS objects
                JOIN (SELECT name, SUM(pgsize) AS bytes FROM dbstat GROUP BY name) AS pages
                  ON pages.name=objects.name
                WHERE objects.name NOT LIKE 'sqlite_%'
                  AND objects.type IN ('table', 'index')
                GROUP BY objects.type""").fetchall()
            values = {kind: int(size or 0) for kind, size in rows}
            return {"application_data_bytes": values.get("table", 0),
                    "application_index_bytes": values.get("index", 0),
                    "database_size_bytes": os.path.getsize(path),
                    "measurement_reason": None}
        except (OSError, sqlite3.Error) as error:
            return {"application_data_bytes": None, "application_index_bytes": None,
                    "database_size_bytes": os.path.getsize(path) if os.path.isfile(path) else None,
                    "measurement_reason": f"SQLite 空间统计不可用：{str(error)[:300]}"}
        finally:
            connection.close()

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
