"""延迟创建的统一业务数据库管理器。"""

import copy
from contextlib import contextmanager
from threading import RLock

from sqlalchemy import event, text
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
            event.listen(engine, "connect",
                         lambda connection, record: backend.configure_connection(connection))
            try:
                initialize = getattr(backend, "initialize", None)
                if initialize:
                    initialize(engine)
                if settings["type"] in {"mysql", "mariadb"}:
                    with engine.connect() as connection:
                        version = str(connection.execute(text("SELECT VERSION()")).scalar_one())
                    is_mariadb = "mariadb" in version.lower()
                    if settings["type"] == "mariadb" and not is_mariadb:
                        raise RuntimeError("配置选择 MariaDB，但服务端产品不是 MariaDB")
                    if settings["type"] == "mysql" and is_mariadb:
                        raise RuntimeError("配置选择 MySQL，但检测到 MariaDB 服务端")
            except Exception:
                engine.dispose()
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
        lock.__enter__()
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

    def create_schema(self, metadata):
        metadata.create_all(self.engine)

    def transaction(self):
        """Own one commit for the outermost unit of work on this scoped session."""
        @contextmanager
        def unit_of_work():
            session = self.session
            depth = int(session.info.get(TRANSACTION_DEPTH_KEY, 0))
            session.info[TRANSACTION_DEPTH_KEY] = depth + 1
            try:
                yield session
                if depth == 0:
                    session.commit()
                    self._commit_count += 1
            except Exception:
                session.rollback()
                raise
            finally:
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

    def upsert_many(self, model, rows, conflict_columns, update_columns=None):
        return self.backend.upsert_many(
            self.session, model, rows, conflict_columns, update_columns)

    def reclaim_space(self):
        if not self.supports_space_reclaim:
            raise ValueError("当前数据库不支持空间回收")
        self.backend.reclaim_space(self.engine)

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
