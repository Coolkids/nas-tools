import time
import uuid
from contextlib import contextmanager
from sqlalchemy import and_, func, text, or_
from sqlalchemy.exc import IntegrityError

from app.db.models import (Base, MEDIA_SYNC_LEGACY_GENERATION, MEDIASYNCITEMS,
                           MEDIASYNCSYNCJOB, MEDIASYNCSTATISTIC)
from app.db.manager import DATABASE
from app.utils.storage_jobs import STORAGE_JOBS
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
            with self.transaction() as session:
                self._assert_server_not_syncing(session, server_type)
                generation, _ = self._active_generation(session, server_type, lock=True)
                generation = generation or MEDIA_SYNC_LEGACY_GENERATION
                session.query(MEDIASYNCITEMS).filter(
                    MEDIASYNCITEMS.SERVER == server_type,
                    MEDIASYNCITEMS.GENERATION_ID == generation,
                    MEDIASYNCITEMS.ITEM_ID == iteminfo.get("id")).delete(synchronize_session=False)
                session.add(MEDIASYNCITEMS(
                    SERVER=server_type,
                    GENERATION_ID=generation,
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
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    @contextmanager
    def _database_maintenance_batch(self):
        """在维护闸门开放时持有共享锁，维护期间停在批次边界等待恢复。"""
        while True:
            retry = False
            try:
                with STORAGE_JOBS.maintenance_lock(exclusive=False, timeout=5):
                    if STORAGE_JOBS.maintenance_gate("database"):
                        retry = True
                    else:
                        yield
                        return
            except TimeoutError:
                retry = True
            if retry:
                time.sleep(0.5)

    @contextmanager
    def transaction(self):
        """媒体数据库事务在维护窗口暂停，并在每个事务结束后释放共享锁。"""
        with self._database_maintenance_batch():
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
                expected_generation, _ = self._active_generation(
                    session, server_type, lock=True)
                payload = {
                    "server": str(server_type),
                    "job_id": uuid.uuid4().hex,
                    "generation_id": uuid.uuid4().hex,
                    "lease_token": int(job.LEASE_TOKEN) + 1 if job else 1,
                    "expected_active_generation": expected_generation,
                    "lease_until": now + max(1.0, float(lease_seconds)),
                    "lease_seconds": max(1.0, float(lease_seconds)),
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
            duration = max(1.0, float(lease_seconds))
            renewed_until = now + duration
            job.LEASE_UNTIL = renewed_until
            job.UPDATED_AT = now
        lease["lease_until"] = renewed_until
        lease["lease_seconds"] = duration
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

    @staticmethod
    def _active_generation(session, server_type, lock=False):
        query = session.query(MEDIASYNCSTATISTIC).filter(
            MEDIASYNCSTATISTIC.SERVER == server_type)
        if lock:
            query = query.with_for_update()
        statistic = query.first()
        return (statistic.ACTIVE_GENERATION if statistic else MEDIA_SYNC_LEGACY_GENERATION,
                statistic)

    def write_sync_batch(self, lease, items):
        """将一批规范化记录写入不可见的代次，事务提交后释放写入机会。"""
        rows = [{
            "SERVER": lease["server"],
            "GENERATION_ID": lease["generation_id"],
            "LIBRARY": item.get("library"),
            "ITEM_ID": item["id"],
            "ITEM_TYPE": item["type"],
            "TITLE": item["title"],
            "ORGIN_TITLE": item.get("originalTitle"),
            "YEAR": item.get("year"),
            "TMDBID": item.get("tmdbid"),
            "IMDBID": item.get("imdbid"),
            "PATH": item.get("path"),
        } for item in items]
        if not rows:
            return 0
        with self.transaction() as session:
            if not self.sync_lease_is_current(lease, session=session):
                raise RuntimeError("媒体同步租约已过期或已被接管")
            job = session.query(MEDIASYNCSYNCJOB).filter(
                MEDIASYNCSYNCJOB.SERVER == lease["server"]).with_for_update().one()
            job.STATUS = "WRITING"
            job.UPDATED_AT = time.time()
            session.bulk_insert_mappings(MEDIASYNCITEMS, rows)
        return len(rows)

    def publish_sync_generation(self, lease, movie_count, series_count):
        """校验写入结果后，在一个短事务中同时切换可见代次和统计。"""
        expected_counts = {"movie": int(movie_count), "series": int(series_count)}
        now = time.time()
        update_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        with self.transaction() as session:
            if not self.sync_lease_is_current(lease, session=session):
                raise RuntimeError("媒体同步租约已过期或已被接管，拒绝发布")
            active_generation, statistic = self._active_generation(
                session, lease["server"], lock=True)
            if active_generation != lease.get("expected_active_generation"):
                raise RuntimeError("活动媒体代次已变化，拒绝发布过期快照")

            item_filter = (MEDIASYNCITEMS.SERVER == lease["server"],
                           MEDIASYNCITEMS.GENERATION_ID == lease["generation_id"])
            actual_counts = {
                "movie": int(session.query(func.count()).filter(
                    *item_filter, func.lower(MEDIASYNCITEMS.ITEM_TYPE) == "movie").scalar()),
                "series": int(session.query(func.count()).filter(
                    *item_filter, func.lower(MEDIASYNCITEMS.ITEM_TYPE).in_(("series", "show"))
                ).scalar()),
            }
            if actual_counts != expected_counts:
                raise RuntimeError("媒体新代次条数与已校验快照不一致，拒绝发布")
            total_count = actual_counts["movie"] + actual_counts["series"]
            if statistic:
                statistic.ACTIVE_GENERATION = lease["generation_id"]
                statistic.TOTAL_COUNT = str(total_count)
                statistic.MOVIE_COUNT = str(actual_counts["movie"])
                statistic.TV_COUNT = str(actual_counts["series"])
                statistic.UPDATE_TIME = update_time
            else:
                session.add(MEDIASYNCSTATISTIC(
                    SERVER=lease["server"],
                    ACTIVE_GENERATION=lease["generation_id"],
                    TOTAL_COUNT=str(total_count),
                    MOVIE_COUNT=str(actual_counts["movie"]),
                    TV_COUNT=str(actual_counts["series"]),
                    UPDATE_TIME=update_time,
                ))
            job = session.query(MEDIASYNCSYNCJOB).filter(
                MEDIASYNCSYNCJOB.SERVER == lease["server"]).with_for_update().one()
            job.STATUS = "CLEANUP_PENDING"
            job.UPDATED_AT = now
            job.LEASE_UNTIL = now + float(lease.get("lease_seconds") or 60)
        lease["published"] = True
        lease["lease_until"] = now + float(lease.get("lease_seconds") or 60)
        return {"generation_id": lease["generation_id"], "total_count": total_count,
                **actual_counts}

    def cleanup_sync_generations(self, lease, batch_size=500, generation_id=None):
        """分批删除非活动代次；每批重验租约并绝不删除当前活动代次。"""
        removed = 0
        while True:
            now = time.time()
            next_lease_until = now + float(lease.get("lease_seconds") or 60)
            with self.transaction() as session:
                if not self.sync_lease_is_current(lease, session=session):
                    raise RuntimeError("媒体同步租约已失效，停止代次清理")
                active_generation, _ = self._active_generation(
                    session, lease["server"], lock=True)
                if generation_id and generation_id == active_generation:
                    raise RuntimeError("拒绝清理当前活动媒体代次")
                query = session.query(MEDIASYNCITEMS.ID).filter(
                    MEDIASYNCITEMS.SERVER == lease["server"])
                if generation_id is not None:
                    query = query.filter(MEDIASYNCITEMS.GENERATION_ID == generation_id)
                if active_generation is not None:
                    query = query.filter(MEDIASYNCITEMS.GENERATION_ID != active_generation)
                item_ids = [row[0] for row in query.order_by(MEDIASYNCITEMS.ID)
                            .limit(max(1, int(batch_size))).all()]
                if not item_ids:
                    return removed
                deleted = session.query(MEDIASYNCITEMS).filter(
                    MEDIASYNCITEMS.ID.in_(item_ids),
                    MEDIASYNCITEMS.SERVER == lease["server"])
                if active_generation is not None:
                    deleted = deleted.filter(MEDIASYNCITEMS.GENERATION_ID != active_generation)
                removed += deleted.delete(synchronize_session=False)
                job = session.query(MEDIASYNCSYNCJOB).filter(
                    MEDIASYNCSYNCJOB.SERVER == lease["server"]).with_for_update().one()
                job.STATUS = "CLEANING"
                job.LEASE_UNTIL = next_lease_until
                job.UPDATED_AT = now
            lease["lease_until"] = next_lease_until

    def recover_stale_sync_jobs(self):
        """启动时接管已过期的作业并清理未发布代次，活动索引始终保留。"""
        now = time.time()
        recoverable = ("FETCHING", "WRITING", "CLEANING", "CLEANUP_PENDING", "FAILED")
        with self.transaction() as session:
            servers = [row[0] for row in session.query(MEDIASYNCSYNCJOB.SERVER).filter(
                MEDIASYNCSYNCJOB.LEASE_UNTIL <= now,
                MEDIASYNCSYNCJOB.STATUS.in_(recoverable))]

        results = []
        for server in servers:
            lease = self.acquire_sync_lease(server, lease_seconds=3600)
            if not lease:
                continue
            try:
                removed = self.cleanup_sync_generations(lease)
                status = "INTERRUPTED"
                error = None
            except Exception as exception:
                removed = 0
                status = "CLEANUP_PENDING"
                error = str(exception)[:500]
            try:
                self.release_sync_lease(lease, status=status)
            except Exception as exception:
                error = str(exception)[:500]
                status = "RECOVERY_REQUIRED"
            results.append({"server": server, "status": status,
                            "removed_rows": removed, "error": error})
        return results

    def _assert_server_not_syncing(self, session, server_type):
        job = session.query(MEDIASYNCSYNCJOB).filter(
            MEDIASYNCSYNCJOB.SERVER == server_type).with_for_update().first()
        if not job:
            active_generation, _ = self._active_generation(session, server_type, lock=True)
            now = time.time()
            session.add(MEDIASYNCSYNCJOB(
                SERVER=server_type,
                JOB_ID=f"idle-{uuid.uuid4().hex}",
                GENERATION_ID=active_generation,
                LEASE_TOKEN=0,
                LEASE_UNTIL=0,
                EXPECTED_ACTIVE_GENERATION=active_generation,
                STATUS="IDLE",
                CREATED_AT=now,
                UPDATED_AT=now,
            ))
            session.flush()
            return
        if job and float(job.LEASE_UNTIL) > time.time():
            raise RuntimeError("媒体库正在执行全量同步，暂时不能修改活动索引")

    def insert_many(self, server_type, items):
        """批量插入媒体记录，提交由调用方负责。"""
        if not server_type or not items:
            return 0
        self._assert_server_not_syncing(self.session, server_type)
        generation, _ = self._active_generation(self.session, server_type, lock=True)
        generation = generation or MEDIA_SYNC_LEGACY_GENERATION
        rows = [{
            "SERVER": server_type,
            "GENERATION_ID": generation,
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
                self._assert_server_not_syncing(self.session, server_type)
                generation, _ = self._active_generation(self.session, server_type, lock=True)
                generation = generation or MEDIA_SYNC_LEGACY_GENERATION
                self.session.query(MEDIASYNCITEMS).filter(
                    MEDIASYNCITEMS.SERVER == server_type,
                    MEDIASYNCITEMS.GENERATION_ID == generation,
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
            with self.transaction() as session:
                if server_type:
                    servers = [server_type]
                else:
                    servers = [row[0] for row in session.query(
                        MEDIASYNCSTATISTIC.SERVER).filter(MEDIASYNCSTATISTIC.SERVER.is_not(None))]
                for server in servers:
                    self._assert_server_not_syncing(session, server)
                    generation, _ = self._active_generation(session, server, lock=True)
                    if generation is None:
                        continue
                    query = session.query(MEDIASYNCITEMS).filter(
                        MEDIASYNCITEMS.SERVER == server,
                        MEDIASYNCITEMS.GENERATION_ID == generation)
                    if library:
                        query = query.filter(MEDIASYNCITEMS.LIBRARY == library)
                    query.delete(synchronize_session=False)
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    def statistics(self, server_type, total_count, movie_count, tv_count):
        if not server_type:
            return False
        try:
            with self.transaction() as session:
                self._assert_server_not_syncing(session, server_type)
                generation, statistic = self._active_generation(session, server_type, lock=True)
                values = {
                    "ACTIVE_GENERATION": generation or MEDIA_SYNC_LEGACY_GENERATION,
                    "TOTAL_COUNT": total_count,
                    "MOVIE_COUNT": movie_count,
                    "TV_COUNT": tv_count,
                    "UPDATE_TIME": time.strftime('%Y-%m-%d %H:%M:%S',
                                                 time.localtime(time.time())),
                }
                if statistic:
                    for name, value in values.items():
                        setattr(statistic, name, value)
                else:
                    session.add(MEDIASYNCSTATISTIC(SERVER=server_type, **values))
            return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            self.session.rollback()
        return False

    def exists(self, server_type, title, year, tmdbid):
        if not server_type or not title:
            return False
        active_items = self.session.query(MEDIASYNCITEMS.ID).join(
            MEDIASYNCSTATISTIC,
            MEDIASYNCSTATISTIC.SERVER == MEDIASYNCITEMS.SERVER, isouter=True).filter(or_(
                and_(MEDIASYNCSTATISTIC.SERVER.is_(None),
                     MEDIASYNCITEMS.GENERATION_ID == MEDIA_SYNC_LEGACY_GENERATION),
                MEDIASYNCITEMS.GENERATION_ID == MEDIASYNCSTATISTIC.ACTIVE_GENERATION))
        if tmdbid:
            match = active_items.filter(
                MEDIASYNCITEMS.TMDBID == str(tmdbid)).first()
            if match:
                return True
        query = active_items.filter(
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
