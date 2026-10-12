import time
from contextlib import contextmanager
from sqlalchemy import text, or_

from app.db.models import Base, MEDIASYNCITEMS, MEDIASYNCSTATISTIC
from app.db.manager import DATABASE
from app.utils import ExceptionUtils


class MediaDb:

    @property
    def session(self):
        return DATABASE.session

    @staticmethod
    def close_session():
        DATABASE.close_session()

    @staticmethod
    def init_db():
        # 兼容旧调用入口；两个逻辑域共享同一 metadata 和 engine。
        Base.metadata.create_all(DATABASE.engine)

    @staticmethod
    def wal_checkpoint():
        if DATABASE.backend.name != "sqlite":
            return
        with DATABASE.engine.connect() as conn:
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

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
            match = self.session.query(MEDIASYNCITEMS.ID).filter(
                MEDIASYNCITEMS.TMDBID == str(tmdbid)).first()
            if match:
                return True
        query = self.session.query(MEDIASYNCITEMS.ID).filter(
            MEDIASYNCITEMS.SERVER == server_type,
            MEDIASYNCITEMS.TITLE == title)
        if year:
            query = query.filter(MEDIASYNCITEMS.YEAR == str(year))
        if tmdbid:
            query = query.filter(or_(MEDIASYNCITEMS.TMDBID.is_(None),
                                     MEDIASYNCITEMS.TMDBID == "",
                                     MEDIASYNCITEMS.TMDBID == str(tmdbid)))
        return query.limit(1).first() is not None

    def get_statistics(self, server_type):
        if not server_type:
            return None
        return self.session.query(MEDIASYNCSTATISTIC).filter(MEDIASYNCSTATISTIC.SERVER == server_type).first()
