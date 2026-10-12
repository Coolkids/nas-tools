import logging
import os
import threading
import time
from sqlalchemy import MetaData, Table, delete, inspect, text, update

from app.db.models import Base
from app.db.manager import DATABASE, TRANSACTION_DEPTH_KEY
from app.utils import ExceptionUtils, PathUtils
from config import Config

lock = threading.Lock()
logger = logging.getLogger(__name__)
_INITIALIZATION_SQL_FILES = frozenset({
    "init_filter.sql", "init_userrss_v3.sql", "update_subscribe.sql",
    "update_userpris.sql", "update_userrss.sql", "reset_db_version.sql",
})
_db_metrics = {
    "retries": 0,
}


def _split_sql_script(script):
    """Split trusted seed SQL on semicolons outside quoted string values."""
    statements = []
    current = []
    quote = None
    index = 0
    while index < len(script):
        char = script[index]
        if quote:
            current.append(char)
            if char == quote:
                if index + 1 < len(script) and script[index + 1] == quote:
                    current.append(script[index + 1])
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"', "`"):
            quote = char
            current.append(char)
        elif char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


class MainDb:

    @property
    def session(self):
        return DATABASE.session

    @staticmethod
    def close_session():
        DATABASE.close_session()

    @staticmethod
    def init_db():
        with lock:
            engine = DATABASE.engine
            inspector = inspect(engine)
            tables = {name.upper(): name for name in inspector.get_table_names()}
            with engine.begin() as conn:
                metadata = MetaData()
                # 站点管理功能已移除，同时清理旧版本留下的数据表。
                for table in ("SITE_STATISTICS_HISTORY", "SITE_USER_INFO_STATS",
                              "SITE_USER_SEEDING_INFO", "SITE_FAVICON", "CONFIG_SITE"):
                    actual_name = tables.get(table)
                    if actual_name:
                        Table(actual_name, metadata, autoload_with=conn).drop(conn, checkfirst=True)
                # 订阅改为只保存索引器列表，清除旧版本保存的私有站点选择。
                for table in ("RSS_MOVIES", "RSS_TVS"):
                    actual_name = tables.get(table)
                    columns = inspector.get_columns(actual_name) if actual_name else []
                    rss_sites = next((column["name"] for column in columns
                                      if column["name"].upper() == "RSS_SITES"), None)
                    if actual_name and rss_sites:
                        reflected = Table(actual_name, metadata, autoload_with=conn)
                        conn.execute(update(reflected).values({reflected.c[rss_sites]: None}))
                system_table = tables.get("SYSTEM_DICT")
                system_columns = inspector.get_columns(system_table) if system_table else []
                system_column_names = {column["name"].upper(): column["name"]
                                       for column in system_columns}
                if system_table and {"TYPE", "KEY"}.issubset(system_column_names):
                    # 清理站点自动登录功能曾保存的账户凭据。
                    reflected = Table(system_table, metadata, autoload_with=conn)
                    conn.execute(delete(reflected).where(
                        reflected.c[system_column_names["TYPE"]] == "SystemConfig",
                        reflected.c[system_column_names["KEY"]].in_(
                            ("CookieUserInfo", "CookieCloud"))))
            Base.metadata.create_all(engine)
        MainDb.wal_checkpoint()

    @staticmethod
    def wal_checkpoint():
        from app.utils.storage_jobs import STORAGE_JOBS
        with STORAGE_JOBS.maintenance_lock(exclusive=False):
            if STORAGE_JOBS.maintenance_gate("database"):
                return
            if DATABASE.backend.name != "sqlite":
                return
            with DATABASE.engine.connect() as conn:
                busy, log_frames, checkpointed_frames = conn.execute(
                    text("PRAGMA wal_checkpoint(PASSIVE)")).one()
                result = {
                    "busy": int(busy),
                    "log_frames": int(log_frames),
                    "checkpointed_frames": int(checkpointed_frames),
                }
                if result["busy"] or (
                        result["log_frames"] >= 0
                        and result["checkpointed_frames"] < result["log_frames"]):
                    logger.info(
                        "SQLite WAL PASSIVE checkpoint incomplete: busy=%s, "
                        "log_frames=%s, checkpointed_frames=%s",
                        result["busy"], result["log_frames"], result["checkpointed_frames"])
                return result

    def init_data(self):
        """Run allowlisted, portable seed scripts once; failures stop startup."""
        config = Config().get_config()
        init_files = Config().get_config("app").get("init_files") or []
        config_dir = os.path.join(Config().get_root_path(), "config")
        sql_files = PathUtils.get_dir_level1_files(in_path=config_dir, exts=".sql")
        config_changed = False
        for sql_file in sql_files:
            basename = os.path.basename(sql_file)
            if basename not in _INITIALIZATION_SQL_FILES:
                raise RuntimeError(f"初始化 SQL 不在受控清单中：{basename}")
            if basename in init_files:
                continue
            config_changed = True
            if basename != "reset_db_version.sql":
                with open(sql_file, "r", encoding="utf-8") as stream:
                    statements = _split_sql_script(stream.read())
                with self.transaction() as session:
                    for statement in statements:
                        portable = DATABASE.backend.adapt_seed_sql(
                            statement, Base.metadata)
                        session.execute(text(portable))
            else:
                logger.info("跳过历史 SQLite 结构版本重置脚本：%s", basename)
            init_files.append(basename)
        if config_changed:
            config['app']['init_files'] = init_files
            Config().save_config(config)

    def insert(self, data):
        """
        插入数据
        """
        if isinstance(data, list):
            self.session.add_all(data)
        else:
            self.session.add(data)

    def insert_many(self, model, rows):
        """批量插入数据，不逐条刷写或提交。"""
        if not rows:
            return 0
        if all(isinstance(row, model) for row in rows):
            self.session.add_all(rows)
        else:
            self.session.bulk_insert_mappings(model, rows)
        return len(rows)

    def upsert_many(self, model, rows, conflict_columns, update_columns=None):
        """按活动数据库方言执行批量 upsert，事务由调用方负责。"""
        return DATABASE.upsert_many(model, rows, conflict_columns, update_columns)

    def transaction(self):
        """使用管理器拥有的外层事务；嵌套调用不提前提交。"""
        return DATABASE.transaction()

    def query(self, *obj):
        """
        查询对象
        """
        return self.session.query(*obj)

    def excute(self, sql):
        """
        执行SQL语句
        """
        self.session.execute(text(sql))

    def flush(self):
        """
        刷写
        """
        self.session.flush()

    def commit(self):
        """
        提交事务
        """
        self.session.commit()
        DATABASE.record_commit()

    def rollback(self):
        """
        回滚事务
        """
        self.session.rollback()


class DbPersist(object):
    """
    数据库持久化装饰器，写操作失败时自动重试（解决 SQLite 锁冲突）
    """

    def __init__(self, db, retries=3, retry_safe=False):
        self.db = db
        self.retries = retries
        self.retry_safe = retry_safe

    def __call__(self, f):
        def persist(*args, **kwargs):
            session = self.db.session
            nested = bool(session.info.get(TRANSACTION_DEPTH_KEY, 0))
            for attempt in range(self.retries):
                try:
                    with DATABASE.transaction():
                        ret = f(*args, **kwargs)
                    return True if ret is None else ret
                except Exception as e:
                    if nested:
                        # Preserve the enclosing unit's atomicity and let its owner roll back.
                        raise
                    if not self.retry_safe or not DATABASE.is_retryable_transaction_error(e):
                        ExceptionUtils.exception_traceback(e)
                        self.db.rollback()
                        return False
                    _db_metrics["retries"] += 1
                    self.db.rollback()
                    if attempt < self.retries - 1:
                        time.sleep(0.5 * (2 ** attempt))
                    else:
                        ExceptionUtils.exception_traceback(e)
                        return False
            return False

        return persist


def get_db_metrics():
    """返回用于性能日志和诊断的指标快照。"""
    return {"commits": DATABASE.commit_count, "retries": _db_metrics["retries"]}
