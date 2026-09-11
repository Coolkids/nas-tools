import os
import threading
import time
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.pool import QueuePool
from sqlalchemy import text

from app.db.models import BaseMedia, MEDIASYNCITEMS, MEDIASYNCSTATISTIC
from app.utils import ExceptionUtils
from config import Config

lock = threading.Lock()
_Engine = create_engine(
    f"sqlite:///{os.path.join(Config().get_config_path(), 'media.db')}?check_same_thread=False",
    echo=False,
    poolclass=QueuePool,
    pool_pre_ping=True,
    pool_size=5,
    pool_recycle=1800,
    max_overflow=10,
    pool_timeout=60,
    connect_args={
        'timeout': 60,              # 等待数据库锁释放的超时时间
        'check_same_thread': False  # 允许在不同线程中复用连接
    }
)
_Session = scoped_session(sessionmaker(bind=_Engine,
                                       autoflush=True,
                                       autocommit=False))


class MediaDb:

    @property
    def session(self):
        return _Session()

    @staticmethod
    def close_session():
        _Session.remove()

    @staticmethod
    def init_db():
        with lock:
            # Base.metadata.create_all 不会为已有表补充索引。
            # 先去重，确保旧版 media.db 升级时安全。
            with _Engine.begin() as conn:
                try:
                    conn.execute(text(
                        "DELETE FROM MEDIASYNC_ITEMS WHERE ID NOT IN "
                        "(SELECT MIN(ID) FROM MEDIASYNC_ITEMS GROUP BY SERVER, ITEM_ID)"
                    ))
                    conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS UN_INDX_MEDIASYNC_ITEMS_SERVER_ITEM "
                        "ON MEDIASYNC_ITEMS(SERVER, ITEM_ID)"
                    ))
                except Exception:
                    # 表可能需要在下面首次创建。
                    pass
            BaseMedia.metadata.create_all(_Engine)
            with _Engine.connect() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL;"))
                conn.execute(text("PRAGMA SYNCHRONOUS=NORMAL;"))
                conn.execute(text("PRAGMA wal_autocheckpoint=1000;"))
        MediaDb.wal_checkpoint()

    @staticmethod
    def wal_checkpoint():
        try:
            with _Engine.connect() as conn:
                conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE);"))
        except Exception as e:
            ExceptionUtils.exception_traceback(e)

    def insert(self, server_type, iteminfo):
        if not server_type or not iteminfo:
            return False
        try:
            self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                      MEDIASYNCITEMS.ITEM_ID == iteminfo.get("id")).delete()
            self.session.flush()
            self.session.add(MEDIASYNCITEMS(
                SERVER=server_type,
                LIBRARY=iteminfo.get("library"),
                ITEM_ID=iteminfo.get("id"),
                ITEM_TYPE=iteminfo.get("type"),
                TITLE=iteminfo.get("title"),
                ORGIN_TITLE=iteminfo.get("originalTitle"),
                YEAR=iteminfo.get("year"),
                TMDBID=iteminfo.get("tmdbid"),
                IMDBID=iteminfo.get("imdbid"),
                PATH=iteminfo.get("path")
            ))
            self.session.commit()
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    @contextmanager
    def transaction(self):
        """为媒体批量写入提供显式事务接口。"""
        try:
            yield self.session
            self.session.commit()
        except Exception:
            self.session.rollback()
            raise

    def insert_many(self, server_type, items):
        """批量插入媒体记录，提交由调用方负责。"""
        if not server_type or not items:
            return 0
        rows = [{
            "SERVER": server_type,
            "LIBRARY": item.get("library"),
            "ITEM_ID": item.get("id"),
            "ITEM_TYPE": item.get("type"),
            "TITLE": item.get("title"),
            "ORGIN_TITLE": item.get("originalTitle"),
            "YEAR": item.get("year"),
            "TMDBID": item.get("tmdbid"),
            "IMDBID": item.get("imdbid"),
            "PATH": item.get("path")
        } for item in items if item and item.get("id")]
        self.session.bulk_insert_mappings(MEDIASYNCITEMS, rows)
        return len(rows)

    def replace(self, server_type, items):
        """完整拉取成功后，原子替换一个服务器的媒体索引。"""
        if not server_type:
            return False
        rows = []
        for iteminfo in items or []:
            if not iteminfo or not iteminfo.get("id"):
                continue
            rows.append({
                "SERVER": server_type,
                "LIBRARY": iteminfo.get("library"),
                "ITEM_ID": iteminfo.get("id"),
                "ITEM_TYPE": iteminfo.get("type"),
                "TITLE": iteminfo.get("title"),
                "ORGIN_TITLE": iteminfo.get("originalTitle"),
                "YEAR": iteminfo.get("year"),
                "TMDBID": iteminfo.get("tmdbid"),
                "IMDBID": iteminfo.get("imdbid"),
                "PATH": iteminfo.get("path")
            })
        try:
            self.session.rollback()
            with self.session.begin():
                self.session.query(MEDIASYNCITEMS).filter(
                    MEDIASYNCITEMS.SERVER == server_type
                ).delete(synchronize_session=False)
                if rows:
                    self.insert_many(server_type, items)
            return len(rows)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
            return False

    def empty(self, server_type=None, library=None):
        try:
            if server_type and library:
                self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                          MEDIASYNCITEMS.LIBRARY == library).delete()
            else:
                self.session.query(MEDIASYNCITEMS).delete()
            self.session.commit()
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    def statistics(self, server_type, total_count, movie_count, tv_count):
        if not server_type:
            return False
        try:
            self.session.query(MEDIASYNCSTATISTIC).filter(MEDIASYNCSTATISTIC.SERVER == server_type).delete()
            self.session.flush()
            self.session.add(MEDIASYNCSTATISTIC(
                SERVER=server_type,
                TOTAL_COUNT=total_count,
                MOVIE_COUNT=movie_count,
                TV_COUNT=tv_count,
                UPDATE_TIME=time.strftime('%Y-%m-%d %H:%M:%S',
                                          time.localtime(time.time()))
            ))
            self.session.commit()
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    def exists(self, server_type, title, year, tmdbid):
        if not server_type or not title:
            return False
        if tmdbid:
            count = self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.TMDBID == str(tmdbid)).count()
            if count:
                return True
        if year:
            items = self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                              MEDIASYNCITEMS.TITLE == title,
                                                              MEDIASYNCITEMS.YEAR == str(year)).all()
        else:
            items = self.session.query(MEDIASYNCITEMS).filter(MEDIASYNCITEMS.SERVER == server_type,
                                                              MEDIASYNCITEMS.TITLE == title).all()
        if items:
            if tmdbid:
                for item in items:
                    if not item.TMDBID or item.TMDBID == str(tmdbid):
                        return True
                return False
            else:
                return True
        else:
            return False

    def get_statistics(self, server_type):
        if not server_type:
            return None
        return self.session.query(MEDIASYNCSTATISTIC).filter(MEDIASYNCSTATISTIC.SERVER == server_type).first()
