"""SQLite 单库后端。"""

import os
import sqlite3

from sqlalchemy import URL, create_engine, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.contracts import DatabaseBackend
from app.db.pool import MeasuredQueuePool


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
            poolclass=MeasuredQueuePool,
            pool_pre_ping=True,
            # SQLite 同一时刻仍只能有一个写入者。保留两个连接以支持并行读取和任务，
            # 同时避免大量工作线程针对同一个 WAL 文件打开过多连接。
            pool_size=2,
            max_overflow=50,
            pool_recycle=1800,
            pool_timeout=60,
            connect_args={"timeout": 60, "check_same_thread": False,
                          "autocommit": False},
        )

    def configure_connection(self, connection):
        # Python 3.14 默认使用 sqlite3 的 PEP 249 事务控制方式。
        # 配置连接的 PRAGMA 必须在事务之外执行。
        previous_autocommit = connection.autocommit
        try:
            connection.autocommit = True
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=60000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA wal_autocheckpoint=1000")
            finally:
                cursor.close()
        finally:
            connection.autocommit = previous_autocommit

    def initialize(self, engine):
        connection = engine.raw_connection()
        try:
            dbapi_connection = connection.driver_connection
            previous_autocommit = dbapi_connection.autocommit
            try:
                dbapi_connection.autocommit = True
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA journal_mode=WAL")
                    row = cursor.fetchone()
                    mode = str(row[0]).lower() if row else "unknown"
                    if mode != "wal":
                        raise RuntimeError(f"SQLite WAL 模式启用失败：{mode}")
                finally:
                    cursor.close()
            finally:
                dbapi_connection.autocommit = previous_autocommit
        finally:
            connection.close()

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
        dbapi_connection = connection.driver_connection
        previous_autocommit = dbapi_connection.autocommit
        try:
            dbapi_connection.autocommit = True
            cursor = dbapi_connection.cursor()
            try:
                before = self._checkpoint_cursor(cursor, "TRUNCATE")
                self._require_checkpoint_complete(before, "VACUUM 前")
                cursor.execute("VACUUM")
                after = self._checkpoint_cursor(cursor, "TRUNCATE")
                self._require_checkpoint_complete(after, "VACUUM 后")
            finally:
                cursor.close()
        finally:
            dbapi_connection.autocommit = previous_autocommit
            connection.close()

    def checkpoint(self, engine, mode="PASSIVE"):
        mode = str(mode or "PASSIVE").upper()
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("SQLite WAL checkpoint 模式无效")
        connection = engine.raw_connection()
        dbapi_connection = connection.driver_connection
        previous_autocommit = dbapi_connection.autocommit
        try:
            dbapi_connection.autocommit = True
            cursor = dbapi_connection.cursor()
            try:
                busy, log_frames, checkpointed_frames = self._checkpoint_cursor(cursor, mode)
            finally:
                cursor.close()
        finally:
            dbapi_connection.autocommit = previous_autocommit
            connection.close()
        return {"busy": busy, "log_frames": log_frames,
                "checkpointed_frames": checkpointed_frames}

    @staticmethod
    def _checkpoint_cursor(cursor, mode):
        cursor.execute(f"PRAGMA wal_checkpoint({mode})")
        result = cursor.fetchone()
        if not result or len(result) != 3:
            raise RuntimeError("SQLite WAL checkpoint 未返回有效状态")
        return tuple(int(value) for value in result)

    @staticmethod
    def _require_checkpoint_complete(result, stage):
        busy, log_frames, checkpointed_frames = result
        if busy or (log_frames >= 0 and checkpointed_frames < log_frames):
            raise RuntimeError(
                f"SQLite {stage} WAL checkpoint 未完成：busy={busy}，"
                f"log_frames={log_frames}，checkpointed_frames={checkpointed_frames}")

    def optimize(self, engine):
        # 在启动时 Alembic 或 metadata 变更完成后、后台任务开始前执行。
        # 是否需要更新查询规划统计信息由 SQLite 决定。
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA optimize")

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
