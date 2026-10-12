import time
import uuid
from contextlib import contextmanager
from sqlalchemy import text, or_
from sqlalchemy.exc import IntegrityError

from app.db.models import (Base, MEDIA_SYNC_LEGACY_GENERATION, MEDIASYNCITEMS,
                           MEDIASYNCSYNCJOB, MEDIASYNCSTATISTIC)
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
                GENERATION_ID=MEDIA_SYNC_LEGACY_GENERATION,
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

    @staticmethod
    def _lease_payload(job):
        return {
            "server": job.SERVER,
            "job_id": job.JOB_ID,
            "generation_id": job.GENERATION_ID,
            "lease_token": int(job.LEASE_TOKEN),
            "expected_active_generation": job.EXPECTED_ACTIVE_GENERATION,
            "lease_until": float(job.LEASE_UNTIL),
        }

    def acquire_sync_lease(self, server_type, lease_seconds=60):
        """原子取得或接管过期的服务器同步租约，返回带 fencing token 的作业凭证。"""
        if not server_type:
            return None
        server_type = str(server_type).strip()
        if not server_type:
            return None
        now = time.time()
        try:
            with self.transaction() as session:
                job = session.query(MEDIASYNCSYNCJOB).filter(
                    MEDIASYNCSYNCJOB.SERVER == server_type).with_for_update().first()
                if job and float(job.LEASE_UNTIL) > now:
                    return None
                statistic = session.query(MEDIASYNCSTATISTIC).filter(
                    MEDIASYNCSTATISTIC.SERVER == server_type).with_for_update().first()
                expected_generation = statistic.ACTIVE_GENERATION if statistic else None
                payload = {
                    "server": str(server_type),
                    "job_id": uuid.uuid4().hex,
                    "generation_id": uuid.uuid4().hex,
                    "lease_token": int(job.LEASE_TOKEN) + 1 if job else 1,
                    "expected_active_generation": expected_generation,
                    "lease_until": now + max(1.0, float(lease_seconds)),
                }
                if job:
                    job.JOB_ID = payload["job_id"]
                    job.GENERATION_ID = payload["generation_id"]
                    job.LEASE_TOKEN = payload["lease_token"]
                    job.LEASE_UNTIL = payload["lease_until"]
                    job.EXPECTED_ACTIVE_GENERATION = expected_generation
                    job.STATUS = "FETCHING"
                    job.CREATED_AT = now
                    job.UPDATED_AT = now
                else:
                    session.add(MEDIASYNCSYNCJOB(
                        SERVER=payload["server"],
                        JOB_ID=payload["job_id"],
                        GENERATION_ID=payload["generation_id"],
                        LEASE_TOKEN=payload["lease_token"],
                        LEASE_UNTIL=payload["lease_until"],
                        EXPECTED_ACTIVE_GENERATION=expected_generation,
                        STATUS="FETCHING",
                        CREATED_AT=now,
                        UPDATED_AT=now,
                    ))
            return payload
        except IntegrityError:
            # 两个进程同时创建服务器首个租约时，唯一主键只允许一个成功。
            self.session.rollback()
            return None

    def renew_sync_lease(self, lease, lease_seconds=60):
        """仅有效且未过期的持有者可以续租。"""
        if not isinstance(lease, dict):
            return False
        now = time.time()
        renewed_until = None
        with self.transaction() as session:
            job = session.query(MEDIASYNCSYNCJOB).filter(
                MEDIASYNCSYNCJOB.SERVER == lease.get("server")).with_for_update().first()
            if (not job or job.JOB_ID != lease.get("job_id")
                    or job.GENERATION_ID != lease.get("generation_id")
                    or int(job.LEASE_TOKEN) != lease.get("lease_token")
                    or float(job.LEASE_UNTIL) <= now):
                return False
            renewed_until = now + max(1.0, float(lease_seconds))
            job.LEASE_UNTIL = renewed_until
            job.UPDATED_AT = now
        lease["lease_until"] = renewed_until
        return True

    def release_sync_lease(self, lease, status="FAILED"):
        """释放当前凭证；过期或被接管的旧任务不能修改新租约状态。"""
        if not isinstance(lease, dict):
            return False
        now = time.time()
        with self.transaction() as session:
            job = session.query(MEDIASYNCSYNCJOB).filter(
                MEDIASYNCSYNCJOB.SERVER == lease.get("server")).with_for_update().first()
            if (not job or job.JOB_ID != lease.get("job_id")
                    or job.GENERATION_ID != lease.get("generation_id")
                    or int(job.LEASE_TOKEN) != lease.get("lease_token")):
                return False
            job.STATUS = str(status)[:32]
            job.LEASE_UNTIL = 0
            job.UPDATED_AT = now
            return True

    def sync_lease_is_current(self, lease, session=None):
        """校验租约身份与有效期；写入批次可传入已锁定的事务会话。"""
        if not isinstance(lease, dict):
            return False
        session = session or self.session
        job = session.query(MEDIASYNCSYNCJOB).filter(
            MEDIASYNCSYNCJOB.SERVER == lease.get("server")).with_for_update().first()
        return bool(job and job.JOB_ID == lease.get("job_id")
                    and job.GENERATION_ID == lease.get("generation_id")
                    and int(job.LEASE_TOKEN) == lease.get("lease_token")
                    and float(job.LEASE_UNTIL) > time.time())

    def insert_many(self, server_type, items):
        """批量插入媒体记录，提交由调用方负责。"""
        if not server_type or not items:
            return 0
        rows = [{
            "SERVER": server_type,
            "GENERATION_ID": MEDIA_SYNC_LEGACY_GENERATION,
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
                ACTIVE_GENERATION=MEDIA_SYNC_LEGACY_GENERATION,
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
