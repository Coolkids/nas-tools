"""延迟创建的统一业务数据库管理器。"""

import copy
import os
import time
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from app.db.backends import create_backend
from app.db.contracts import DatabaseStatus
from app.utils.storage_jobs import STORAGE_JOBS


TRANSACTION_DEPTH_KEY = "_nas_tools_database_transaction_depth"


class DatabaseManager:
    def __init__(self):
        self._lock = RLock()
        self._engine = None
        self._backend = None
        self._settings = None
        self._session = None
        self._session_class = None
        self._commit_count = 0
        self._metrics_lock = RLock()
        self._metrics = {
            "sql_count": 0,
            "sql_seconds": 0.0,
            "sql_max_seconds": 0.0,
            "unknown_rowcount": 0,
            "sql_errors": 0,
            "retries": 0,
            "transaction_count": 0,
            "transaction_seconds": 0.0,
            "transaction_max_seconds": 0.0,
            "rollback_count": 0,
            "maintenance_lock_wait_seconds": 0.0,
            "exclusive_maintenance_seconds": 0.0,
            "connection_checkouts": 0,
            "connection_checkins": 0,
            "pool_acquisition_count": 0,
            "pool_acquisition_seconds": 0.0,
            "pool_acquisition_max_seconds": 0.0,
            "reported_rowcount": 0,
            "sqlite_busy_errors": 0,
            "lock_conflict_errors": 0,
        }

    @staticmethod
    def _read_settings():
        from config import Config
        value = Config().get_config("database")
        if value is None:
            return {"type": "sqlite"}
        if not isinstance(value, dict):
            raise ValueError("database 配置必须是对象")
        settings = copy.deepcopy(value)
        settings.setdefault("type", "sqlite")
        return settings

    @staticmethod
    def _validate(settings):
        kind = str(settings.get("type") or "sqlite").lower()
        settings["type"] = kind
        create_backend(kind)
        if kind == "sqlite":
            return
        for key in ("host", "name", "username"):
            if not str(settings.get(key) or "").strip():
                raise ValueError(f"远程数据库缺少 {key} 配置")
        try:
            port = int(settings.get("port") or (5432 if kind == "postgresql" else 3306))
            timeout = int(settings.get("connect_timeout", 5))
            if not 1 <= port <= 65535 or not 1 <= timeout <= 60:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError("数据库端口或连接超时无效") from error

    def _ensure(self):
        with self._lock:
            settings = self._read_settings()
            self._validate(settings)
            if self._engine is not None:
                if settings != self._settings:
                    raise RuntimeError("数据库配置已变化，请通过受控流程重启后生效")
                return
            backend = create_backend(settings["type"])
            engine = backend.create_engine(settings)
            set_wait_observer = getattr(engine.pool, "set_wait_observer", None)
            if set_wait_observer:
                set_wait_observer(self._record_pool_acquisition)
            event.listen(engine, "connect",
                         lambda connection, record: backend.configure_connection(connection))
            event.listen(engine, "before_cursor_execute", self._before_cursor_execute)
            event.listen(engine, "after_cursor_execute", self._after_cursor_execute)
            event.listen(engine, "handle_error", self._handle_sql_error)
            event.listen(engine.pool, "checkout", self._on_connection_checkout)
            event.listen(engine.pool, "checkin", self._on_connection_checkin)
            try:
                initialize = getattr(backend, "initialize", None)
                if initialize:
                    initialize(engine)
                backend.validate_product(backend.capabilities(engine))
            except Exception:
                backend.release(engine)
                raise
            self._backend = backend
            self._engine = engine
            self._settings = settings
            self._session_class = type(f"ManagedDatabaseSession{id(self)}", (Session,), {})
            event.listen(self._session_class, "do_orm_execute", self._before_orm_execute)
            event.listen(self._session_class, "before_flush", self._before_flush)
            event.listen(self._session_class, "after_transaction_end", self._after_transaction_end)
            self._session = scoped_session(sessionmaker(
                bind=engine, class_=self._session_class, autoflush=True,
                autocommit=False, expire_on_commit=False))

    @property
    def engine(self):
        self._ensure()
        return self._engine

    @property
    def backend(self):
        self._ensure()
        return self._backend

    @property
    def session(self):
        self._ensure()
        return self._session()

    def close_session(self):
        if self._session is not None:
            self._session.remove()

    def dispose(self):
        with self._lock:
            if self._session is not None:
                self._session.remove()
            if self._engine is not None:
                if self._backend is not None:
                    self._backend.release(self._engine)
                else:
                    self._engine.dispose()
            self._engine = self._backend = self._settings = self._session = None
            self._session_class = None

    @staticmethod
    def _guard_database_session(session):
        if STORAGE_JOBS.maintenance_gate("database") and not STORAGE_JOBS.owns_exclusive_lock():
            raise RuntimeError("数据库正在维护，暂时拒绝业务读写")
        if STORAGE_JOBS.owns_exclusive_lock() or session.info.get("_storage_lock"):
            return
        lock = STORAGE_JOBS.maintenance_lock(exclusive=False)
        started = time.perf_counter()
        lock.__enter__()
        DATABASE._record_metric("maintenance_lock_wait_seconds",
                                time.perf_counter() - started)
        if STORAGE_JOBS.maintenance_gate("database"):
            lock.__exit__(None, None, None)
            raise RuntimeError("数据库正在维护，暂时拒绝业务读写")
        session.info["_storage_lock"] = lock

    def _before_orm_execute(self, state):
        self._guard_database_session(state.session)

    def _before_flush(self, session, flush_context, instances):
        self._guard_database_session(session)

    @staticmethod
    def _after_transaction_end(session, transaction):
        if transaction.parent is not None:
            return
        lock = session.info.pop("_storage_lock", None)
        if lock:
            lock.__exit__(None, None, None)

    @property
    def supports_space_reclaim(self):
        return self.backend.supports_space_reclaim

    def status(self):
        settings = self._read_settings()
        backend = str(settings.get("type") or "sqlite").lower()
        url = ""
        connected = False
        supports_space_reclaim = False
        try:
            engine = self.engine
            url = engine.url.set(username=None, password=None).render_as_string()
            supports_space_reclaim = bool(self._backend.supports_space_reclaim)
            with engine.connect():
                connected = True
        except Exception:
            connected = False
        return DatabaseStatus(backend=backend, connected=connected, url=url,
                              supports_space_reclaim=supports_space_reclaim)

    def _record_metric(self, key, value=1):
        with self._metrics_lock:
            self._metrics[key] = self._metrics.get(key, 0) + value

    def _record_pool_acquisition(self, elapsed):
        with self._metrics_lock:
            self._metrics["pool_acquisition_count"] += 1
            self._metrics["pool_acquisition_seconds"] += elapsed
            self._metrics["pool_acquisition_max_seconds"] = max(
                self._metrics["pool_acquisition_max_seconds"], elapsed)

    def record_exclusive_maintenance(self, elapsed):
        self._record_metric("exclusive_maintenance_seconds", max(0.0, elapsed))

    def _before_cursor_execute(self, connection, cursor, statement, parameters,
                               context, executemany):
        # Keep only timing and row counts; SQL and bound values may contain secrets.
        context._nas_tools_started_at = time.perf_counter()

    def _after_cursor_execute(self, connection, cursor, statement, parameters,
                              context, executemany):
        elapsed = max(0.0, time.perf_counter() -
                      getattr(context, "_nas_tools_started_at", time.perf_counter()))
        with self._metrics_lock:
            self._metrics["sql_count"] += 1
            self._metrics["sql_seconds"] += elapsed
            self._metrics["sql_max_seconds"] = max(
                self._metrics["sql_max_seconds"], elapsed)
            rowcount = getattr(cursor, "rowcount", -1)
            if isinstance(rowcount, int) and rowcount >= 0:
                self._metrics["reported_rowcount"] += rowcount
            else:
                self._metrics["unknown_rowcount"] += 1

    def _handle_sql_error(self, exception_context):
        original = getattr(exception_context, "original_exception", None)
        with self._metrics_lock:
            self._metrics["sql_errors"] += 1
            code = getattr(original, "sqlite_errorcode", None)
            message = str(original).lower()
            if ((code is not None and (code & 0xFF) in (5, 6))
                    or "database is locked" in message
                    or "database is busy" in message):
                self._metrics["sqlite_busy_errors"] += 1
            arguments = getattr(original, "args", ())
            error_code = arguments[0] if arguments and isinstance(arguments[0], int) else None
            sqlstate = (getattr(original, "sqlstate", None)
                        or getattr(original, "pgcode", None))
            if (sqlstate in {"40001", "40P01"}
                    or error_code in {1205, 1213}):
                self._metrics["lock_conflict_errors"] += 1

    def _on_connection_checkout(self, dbapi_connection, connection_record,
                                connection_proxy):
        self._record_metric("connection_checkouts")

    def _on_connection_checkin(self, dbapi_connection, connection_record):
        self._record_metric("connection_checkins")

    def metrics(self):
        with self._metrics_lock:
            result = dict(self._metrics)
            result["commits"] = self._commit_count
            result["pool_checked_out"] = (
                self._engine.pool.checkedout() if self._engine is not None else 0)
        if self._engine is not None and self._settings.get("type") == "sqlite":
            path = self._engine.url.database
            try:
                result["sqlite_wal_bytes"] = (os.path.getsize(path + "-wal")
                                              if os.path.isfile(path + "-wal") else 0)
            except OSError:
                result["sqlite_wal_bytes"] = None
        else:
            result["sqlite_wal_bytes"] = None
        return result

    def create_schema(self, metadata):
        metadata.create_all(self.engine)

    def insert_many(self, model, rows):
        return self.backend.insert_many(self.session, model.__table__, rows)

    def read_batches(self, connection, table, batch_size=500):
        from app.db.backends import create_backend
        return create_backend(connection.dialect.name).read_batches(
            connection, table, batch_size=batch_size)

    def inspect_schema(self, engine=None):
        engine = engine or self.engine
        from app.db.backends import create_backend
        return create_backend(engine.dialect.name).inspect_schema(engine)

    def table_counts(self, metadata, engine=None):
        engine = engine or self.engine
        from app.db.backends import create_backend
        return create_backend(engine.dialect.name).table_counts(engine, metadata)

    def estimated_table_counts(self, metadata, engine=None):
        engine = engine or self.engine
        from app.db.backends import create_backend
        return create_backend(engine.dialect.name).estimated_table_counts(engine, metadata)

    def storage_usage(self, metadata, engine=None):
        engine = engine or self.engine
        from app.db.backends import create_backend
        return create_backend(engine.dialect.name).storage_usage(engine, metadata)

    def calibrate_sequences(self, metadata, engine=None):
        engine = engine or self.engine
        from app.db.backends import create_backend
        return create_backend(engine.dialect.name).calibrate_sequences(engine, metadata)

    def release(self, engine=None):
        engine = engine or self.engine
        from app.db.backends import create_backend
        return create_backend(engine.dialect.name).release(engine)

    def transaction(self):
        """Own one commit for the outermost unit of work on this scoped session."""
        @contextmanager
        def unit_of_work():
            session = self.session
            depth = int(session.info.get(TRANSACTION_DEPTH_KEY, 0))
            session.info[TRANSACTION_DEPTH_KEY] = depth + 1
            started = time.perf_counter()
            try:
                yield session
                if depth == 0:
                    session.commit()
                    self._commit_count += 1
            except Exception:
                session.rollback()
                if depth == 0:
                    self._record_metric("rollback_count")
                raise
            finally:
                if depth == 0:
                    elapsed = max(0.0, time.perf_counter() - started)
                    with self._metrics_lock:
                        self._metrics["transaction_count"] += 1
                        self._metrics["transaction_seconds"] += elapsed
                        self._metrics["transaction_max_seconds"] = max(
                            self._metrics["transaction_max_seconds"], elapsed)
                if depth:
                    session.info[TRANSACTION_DEPTH_KEY] = depth
                else:
                    session.info.pop(TRANSACTION_DEPTH_KEY, None)
        return unit_of_work()

    @property
    def commit_count(self):
        return self._commit_count

    def record_commit(self):
        """Count explicit batch commits performed through a compatibility facade."""
        self._commit_count += 1

    def record_retry(self):
        """Count safe retry attempts in the same metric snapshot as SQL timings."""
        self._record_metric("retries")

    def upsert_many(self, model, rows, conflict_columns, update_columns=None):
        return self.backend.upsert_many(
            self.session, model, rows, conflict_columns, update_columns)

    def reclaim_space(self):
        if not self.supports_space_reclaim:
            raise ValueError("当前数据库不支持空间回收")
        self.backend.reclaim_space(self.engine)

    def checkpoint(self, mode="PASSIVE"):
        return self.backend.checkpoint(self.engine, mode)

    def optimize(self):
        return self.backend.optimize(self.engine)

    def is_retryable_transaction_error(self, error):
        if not isinstance(error, DBAPIError):
            return False
        original = getattr(error, "orig", None)
        kind = self._settings.get("type") if self._settings else None
        if kind == "sqlite":
            code = getattr(original, "sqlite_errorcode", None)
            if code is not None and (code & 0xFF) in (5, 6):  # SQLITE_BUSY / SQLITE_LOCKED
                return True
            message = str(original).lower()
            return "database is locked" in message or "database is busy" in message
        sqlstate = (getattr(original, "sqlstate", None)
                    or getattr(original, "pgcode", None))
        if sqlstate in {"40001", "40P01"}:  # serialization failure / deadlock
            return True
        arguments = getattr(original, "args", ())
        error_code = arguments[0] if arguments and isinstance(arguments[0], int) else None
        if kind in {"mysql", "mariadb"} and error_code in {1205, 1213}:
            return True  # lock wait timeout / deadlock
        # Connection loss can leave commit outcome unknown, so it is never retried here.
        return False


DATABASE = DatabaseManager()
