import os
import time
import threading
from contextlib import contextmanager
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
from sqlalchemy.exc import OperationalError

from app.db.models import Base
from app.utils import ExceptionUtils, PathUtils
from config import Config

lock = threading.Lock()
_db_metrics = {
    "commits": 0,
    "retries": 0,
}
_Engine = create_engine(
    f"sqlite:///{os.path.join(Config().get_config_path(), 'user.db')}?check_same_thread=False",
    echo=False,
    poolclass=QueuePool,
    pool_pre_ping=True,
    pool_size=30,
    pool_recycle=1800,
    max_overflow=60,
    pool_timeout=60,
    connect_args={
        'timeout': 60,              # 等待数据库锁释放的超时时间
        'check_same_thread': False  # 允许在不同线程中复用连接
    }
)
_Session = scoped_session(sessionmaker(bind=_Engine,
                                       autoflush=True,
                                       autocommit=False,
                                       expire_on_commit=False))


class MainDb:

    @property
    def session(self):
        return _Session()

    @staticmethod
    def close_session():
        _Session.remove()

    @staticmethod
    def init_db():
        with lock:
            # 新增唯一索引必须兼容旧数据库中的重复历史记录。
            # 迁移脚本会再次执行该步骤，init_db 则保证 create_all 本身安全。
            with _Engine.begin() as conn:
                for table, columns in (
                        ("RSS_TORRENTS", ("ENCLOSURE",)),
                        ("SYNC_HISTORY", ("PATH", "DEST")),
                        ("SITE_BRUSH_TORRENTS", ("TASK_ID", "TORRENT_NAME", "ENCLOSURE")),
                        ("SITE_STATISTICS_HISTORY", ("DATE", "URL"))):
                    try:
                        joined = ", ".join(columns)
                        predicate = "ENCLOSURE IS NOT NULL AND " if table == "RSS_TORRENTS" else ""
                        conn.execute(text(
                            f"DELETE FROM {table} WHERE {predicate}ID NOT IN "
                            f"(SELECT MIN(ID) FROM {table} GROUP BY {joined})"))
                    except Exception:
                        # 表可能需要在下面首次创建。
                        pass
            Base.metadata.create_all(_Engine)
            with _Engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL;"))
                conn.execute(text("PRAGMA SYNCHRONOUS=NORMAL;"))
                conn.execute(text("PRAGMA wal_autocheckpoint=1000;"))
        MainDb.wal_checkpoint()

    @staticmethod
    def wal_checkpoint():
        try:
            with _Engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE);"))
        except Exception as e:
            ExceptionUtils.exception_traceback(e)

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
        """执行 SQLite 批量 upsert，事务由调用方负责。"""
        if not rows:
            return 0
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        statement = sqlite_insert(model.__table__).values(rows)
        if update_columns:
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={column: getattr(statement.excluded, column)
                      for column in update_columns}
            )
        else:
            statement = statement.on_conflict_do_nothing(
                index_elements=list(conflict_columns)
            )
        self.session.execute(statement)
        return len(rows)

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
        self.session.execute(sql)

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

    def __init__(self, db, retries=3):
        self.db = db
        self.retries = retries

    def __call__(self, f):
        def persist(*args, **kwargs):
            last_exception = None
            for attempt in range(self.retries):
                try:
                    ret = f(*args, **kwargs)
                    self.db.commit()
                    return True if ret is None else ret
                except OperationalError as e:
                    last_exception = e
                    _db_metrics["retries"] += 1
                    self.db.rollback()
                    if attempt < self.retries - 1:
                        time.sleep(0.5 * (2 ** attempt))
                    else:
                        ExceptionUtils.exception_traceback(e)
                        return False
                except Exception as e:
                    ExceptionUtils.exception_traceback(e)
                    self.db.rollback()
                    return False
            if last_exception:
                ExceptionUtils.exception_traceback(last_exception)
            return False

        return persist


def get_db_metrics():
    """返回用于性能日志和诊断的指标快照。"""
    return dict(_db_metrics)
