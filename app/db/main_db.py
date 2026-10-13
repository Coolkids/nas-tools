import logging
import os
import threading
import time
from sqlalchemy import text

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
            Base.metadata.create_all(engine)
        MainDb.wal_checkpoint()

    @staticmethod
    def wal_checkpoint():
        from app.utils.storage_jobs import STORAGE_JOBS
        with STORAGE_JOBS.maintenance_lock(exclusive=False):
            if STORAGE_JOBS.maintenance_gate("database"):
                return
            result = DATABASE.checkpoint("PASSIVE")
            if result is not None:
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
            self._calibrate_seed_sequences()
            config['app']['init_files'] = init_files
            Config().save_config(config)

    @staticmethod
    def _calibrate_seed_sequences():
        """Explicit seed IDs must not leave PostgreSQL sequences behind."""
        if DATABASE.backend.name == "postgresql":
            DATABASE.calibrate_sequences(Base.metadata)

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
            return DATABASE.insert_many(model, rows)
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
                    DATABASE.record_retry()
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
    return DATABASE.metrics()
