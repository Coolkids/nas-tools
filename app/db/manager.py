"""延迟创建的统一业务数据库管理器。"""

import copy
from threading import RLock

from sqlalchemy import event, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import scoped_session, sessionmaker

from app.db.backends import create_backend
from app.db.contracts import DatabaseStatus
from app.db.models import portable_key_digest


class DatabaseManager:
    def __init__(self):
        self._lock = RLock()
        self._engine = None
        self._backend = None
        self._settings = None
        self._session = None

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
            self._session = scoped_session(sessionmaker(
                bind=engine, autoflush=True, autocommit=False, expire_on_commit=False))

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
        return self.session.begin()

    def upsert_many(self, model, rows, conflict_columns, update_columns=None):
        if not rows:
            return 0
        table = model.__table__
        original_conflict = tuple(conflict_columns)
        portable_keys = getattr(model, "__portable_unique_keys__", {})
        digest_column_name = portable_keys.get(original_conflict)
        if digest_column_name:
            digest_column = table.c[digest_column_name]
            values_by_digest = {}
            rows = [dict(row) for row in rows]
            for row in rows:
                key_values = tuple(row.get(column) for column in original_conflict)
                digest = portable_key_digest(key_values)
                row[digest_column_name] = digest
                if digest is not None:
                    prior = values_by_digest.setdefault(digest, key_values)
                    if prior != key_values:
                        raise ValueError("摘要唯一键冲突，拒绝覆盖不同的原始值")
            # 在写入前比对摘要对应的原文；即使摘要碰撞也只会明确失败。
            digest_values = list(values_by_digest)
            original_columns = [table.c[column] for column in original_conflict]
            for offset in range(0, len(digest_values), 500):
                batch = digest_values[offset:offset + 500]
                statement = select(digest_column, *original_columns).where(
                    digest_column.in_(batch))
                for existing in self.session.execute(statement):
                    digest = existing[0]
                    existing_values = tuple(existing[1:])
                    if values_by_digest[digest] != existing_values:
                        raise ValueError("数据库中发现摘要唯一键冲突，拒绝覆盖不同记录")
            conflict_columns = (digest_column_name,)

        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(table).values(rows)
        elif dialect == "postgresql":
            statement = postgresql_insert(table).values(rows)
        elif dialect in {"mysql", "mariadb"}:
            statement = mysql_insert(table).values(rows)
            # MySQL 没有可指定冲突键的 on-conflict 子句，拒绝有其他唯一键
            # 的歧义表，避免把非目标唯一键冲突当成成功。
            expected = {tuple(conflict_columns)}
            unique_sets = {tuple(column.name for column in constraint.columns)
                           for constraint in table.constraints
                           if getattr(constraint, "unique", False)}
            unique_sets.update(tuple(index.columns.keys()) for index in table.indexes if index.unique)
            if unique_sets - expected:
                raise ValueError(f"{table.name} 存在无法安全区分的其他唯一键")
            if not update_columns:
                primary_key = next(iter(table.primary_key.columns)).name
                statement = statement.on_duplicate_key_update(
                    **{primary_key: table.c[primary_key]})
                self.session.execute(statement)
                return len(rows)
            statement = statement.on_duplicate_key_update(
                **{column: getattr(statement.inserted, column) for column in update_columns})
            self.session.execute(statement)
            return len(rows)
        else:
            raise ValueError(f"不支持的数据库方言：{dialect}")

        if update_columns:
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={column: getattr(statement.excluded, column) for column in update_columns})
        else:
            statement = statement.on_conflict_do_nothing(index_elements=list(conflict_columns))
        self.session.execute(statement)
        return len(rows)

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
