import os
import time
import threading
from contextlib import contextmanager
from sqlalchemy import MetaData, Table, delete, inspect, text, update

from app.db.models import Base
from app.db.manager import DATABASE
from app.utils import ExceptionUtils, PathUtils
from config import Config

lock = threading.Lock()
_db_metrics = {
    "commits": 0,
    "retries": 0,
}
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
            if engine.dialect.name == "sqlite":
                with engine.connect() as conn:
                    conn.execute(text("PRAGMA journal_mode=WAL"))
                    conn.execute(text("PRAGMA synchronous=NORMAL"))
                    conn.execute(text("PRAGMA wal_autocheckpoint=1000"))
        MainDb.wal_checkpoint()

    @staticmethod
    def wal_checkpoint():
        if DATABASE.backend.name != "sqlite":
            return
        with DATABASE.engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    def init_data(self):
        """
        读取config目录下的sql文件，并初始化到数据库，只处理一次
        """
        config = Config().get_config()
        init_files = Config().get_config("app").get("init_files") or []
        config_dir = os.path.join(Config().get_root_path(), "config")
        sql_files = PathUtils.get_dir_level1_files(in_path=config_dir, exts=".sql")
        config_flag = False
        for sql_file in sql_files:
            if os.path.basename(sql_file) not in init_files:
                config_flag = True
                with open(sql_file, "r", encoding="utf-8") as f:
                    sql_list = f.read().split(';\n')
                    for sql in sql_list:
                        if not sql.strip():
                            continue
                        try:
                            self.excute(sql)
                            self.commit()
                        except Exception as err:
                            print(str(err))
                init_files.append(os.path.basename(sql_file))
        if config_flag:
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

    @contextmanager
    def transaction(self):
        """成功时只提交一次，失败时回滚整个批次。"""
        try:
            yield self.session
            self.commit()
        except Exception:
            self.rollback()
            raise

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
        _db_metrics["commits"] += 1

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
            last_exception = None
            for attempt in range(self.retries):
                try:
                    ret = f(*args, **kwargs)
                    self.db.commit()
                    return True if ret is None else ret
                except Exception as e:
                    from app.db.manager import DATABASE

                    if not self.retry_safe or not DATABASE.is_retryable_transaction_error(e):
                        ExceptionUtils.exception_traceback(e)
                        self.db.rollback()
                        return False
                    last_exception = e
                    _db_metrics["retries"] += 1
                    self.db.rollback()
                    if attempt < self.retries - 1:
                        time.sleep(0.5 * (2 ** attempt))
                    else:
                        ExceptionUtils.exception_traceback(e)
                        return False
            if last_exception:
                ExceptionUtils.exception_traceback(last_exception)
            return False

        return persist


def get_db_metrics():
    """返回用于性能日志和诊断的指标快照。"""
    return dict(_db_metrics)
