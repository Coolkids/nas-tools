import datetime
import os
import os.path
import tempfile
import time
import json
import uuid
from enum import Enum
from sqlalchemy import cast, func, or_, and_

from app.db import MainDb, DbPersist
from app.db.models import *
from app.utils import StringUtils
from app.utils.types import MediaType, RmtMode


class DbHelper:
    _db = MainDb()

    @staticmethod
    def _json_default(value):
        """将 TMDB 的 AsObj 等对象转换为可持久化的 JSON 结构。"""
        if isinstance(value, Enum):
            return value.value
        if hasattr(value, "__dict__"):
            return value.__dict__
        return str(value)

    @classmethod
    def _json_dumps(cls, value):
        return json.dumps(value or {}, ensure_ascii=False, default=cls._json_default)

    @staticmethod
    def recognition_summary(payload):
        """Build the small list-view projection without raw inputs or responses."""
        overall = payload.get("overall_result") or {}

        def tmdb_identity(value):
            if not isinstance(value, dict):
                return None
            media_type = value.get("media_type")
            if isinstance(media_type, Enum):
                media_type = media_type.value
            return {"id": value.get("id"), "media_type": media_type,
                    "title": value.get("title") or value.get("name")}

        return {
            "overall_result": {
                "status": overall.get("status"),
                "reason": overall.get("reason"),
                "business_result": overall.get("business_result"),
                "selected_provider": overall.get("selected_provider"),
                "elapsed_ms": overall.get("elapsed_ms"),
                "tmdb_result": tmdb_identity(overall.get("tmdb_result")),
            },
            "provider_results": [{
                key: item.get(key) for key in (
                    "attempt_id", "provider_id", "status", "normalized_result",
                    "elapsed_ms", "error")
            } for item in (payload.get("provider_results") or [])],
            "tmdb_results": [{
                "provider_id": item.get("provider_id"),
                "status": item.get("status"),
                "result": tmdb_identity(item.get("result")),
            } for item in (payload.get("tmdb_results") or [])],
            "actions": [{key: item.get(key) for key in ("sequence", "action_type", "status")}
                        for item in (payload.get("actions") or [])],
        }

    @staticmethod
    def release_session():
        from app.db import close_db
        close_db()

    @DbPersist(_db)
    def insert_search_results(self, media_items: list, title=None, ident_flag=True, keyword=None):
        """
        将返回信息插入数据库
        """
        if not media_items:
            return
        if keyword:
            self._db.query(SEARCHRESULTINFO).filter(SEARCHRESULTINFO.KEYWORD == keyword).delete()
        data_list = []
        for media_item in media_items:
            if media_item.type == MediaType.TV:
                mtype = "TV"
            elif media_item.type == MediaType.MOVIE:
                mtype = "MOV"
            else:
                mtype = "ANI"
            data_list.append(
                SEARCHRESULTINFO(
                    TORRENT_NAME=media_item.org_string,
                    ENCLOSURE=media_item.enclosure,
                    DESCRIPTION=media_item.description,
                    TYPE=mtype if ident_flag else '',
                    TITLE=media_item.title if ident_flag else title,
                    YEAR=media_item.year if ident_flag else '',
                    SEASON=media_item.get_season_string() if ident_flag else '',
                    EPISODE=media_item.get_episode_string() if ident_flag else '',
                    ES_STRING=media_item.get_season_episode_string() if ident_flag else '',
                    VOTE=media_item.vote_average or "0",
                    IMAGE=media_item.get_backdrop_image(default=False, original=True),
                    POSTER=media_item.get_poster_image(),
                    TMDBID=media_item.tmdb_id,
                    OVERVIEW=media_item.overview,
                    RES_TYPE=json.dumps({
                        "respix": media_item.resource_pix,
                        "restype": media_item.resource_type,
                        "reseffect": media_item.resource_effect,
                        "video_encode": media_item.video_encode
                    }),
                    RES_ORDER=media_item.res_order,
                    SIZE=StringUtils.str_filesize(int(media_item.size)),
                    SEEDERS=media_item.seeders,
                    PEERS=media_item.peers,
                    SITE=media_item.site,
                    SITE_ORDER=media_item.site_order,
                    PAGEURL=media_item.page_url,
                    OTHERINFO=media_item.resource_team,
                    UPLOAD_VOLUME_FACTOR=media_item.upload_volume_factor,
                    DOWNLOAD_VOLUME_FACTOR=media_item.download_volume_factor,
                    KEYWORD=keyword
                ))
        self._db.insert(data_list)

    def get_search_result_by_id(self, dl_id):
        """
        根据ID从数据库中查询检索结果的一条记录
        """
        return self._db.query(SEARCHRESULTINFO).filter(SEARCHRESULTINFO.ID == dl_id).all()

    def get_search_results(self, ):
        """
        查询检索结果的所有记录
        """
        return self._db.query(SEARCHRESULTINFO).all()

    def get_search_results_by_keyword(self, keyword):
        """
        根据关键词查询检索结果
        """
        return self._db.query(SEARCHRESULTINFO).filter(SEARCHRESULTINFO.KEYWORD == keyword).all()

    def is_torrent_rssd(self, enclosure):
        """
        查询RSS是否处理过，根据下载链接
        """
        if not enclosure:
            return True
        if self._db.query(RSSTORRENTS).filter(RSSTORRENTS.ENCLOSURE == enclosure).count() > 0:
            return True
        else:
            return False

    def is_userrss_finished(self, torrent_name, enclosure):
        """
        查询RSS是否处理过，根据名称
        """
        if not torrent_name and not enclosure:
            return True
        if enclosure:
            ret = self._db.query(RSSTORRENTS).filter(RSSTORRENTS.ENCLOSURE == enclosure).count()
        else:
            ret = self._db.query(RSSTORRENTS).filter(RSSTORRENTS.TORRENT_NAME == torrent_name).count()
        return True if ret > 0 else False

    @DbPersist(_db)
    def delete_all_search_torrents(self, ):
        """
        删除所有搜索的记录
        """
        self._db.query(SEARCHRESULTINFO).delete()

    @DbPersist(_db)
    def save_search_task(self, keyword, status, start_time=None, end_time=None, message=None):
        """
        保存或更新搜索任务
        """
        task = self._db.query(SEARCHTASK).filter(SEARCHTASK.KEYWORD == keyword).first()
        if task:
            task.STATUS = status
            if start_time:
                task.START_TIME = start_time
            if end_time:
                task.END_TIME = end_time
            if message:
                task.MESSAGE = message
        else:
            task = SEARCHTASK(KEYWORD=keyword, STATUS=status,
                              START_TIME=start_time, END_TIME=end_time, MESSAGE=message)
            self._db.insert(task)

    def get_search_task(self, keyword):
        """
        根据关键词查询任务
        """
        return self._db.query(SEARCHTASK).filter(SEARCHTASK.KEYWORD == keyword).first()

    def get_search_tasks(self, limit=20):
        """
        查询所有搜索任务，按开始时间倒序
        """
        return self._db.query(SEARCHTASK).order_by(SEARCHTASK.ID.desc()).limit(limit).all()

    @DbPersist(_db)
    def insert_ai_recognition_record(self, title, anitopy_result, ai_result,
                                     anitopy_tmdb, ai_tmdb, status):
        """保存未命中或匹配冲突的双解析结果。"""
        record = AIRECOGNITIONRECORD(
            TITLE=title,
            ANITOPY_RESULT=self._json_dumps(anitopy_result),
            AI_RESULT=self._json_dumps(ai_result),
            ANITOPY_TMDB=self._json_dumps(anitopy_tmdb),
            AI_TMDB=self._json_dumps(ai_tmdb),
            STATUS=status,
            ADD_TIME=datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        )
        self._db.insert(record)
        return record

    def get_ai_recognition_records(self, title=None, page=1, page_size=20):
        """按标题分页查询 AI 识别核对记录。"""
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 20), 1), 100)
        query = self._db.query(AIRECOGNITIONRECORD)
        if title:
            query = query.filter(AIRECOGNITIONRECORD.TITLE.contains(title))
        total = query.count()
        records = query.order_by(AIRECOGNITIONRECORD.ID.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return total, records

    @DbPersist(_db)
    def insert_recognition_record(self, payload):
        """Persist a recognition request and all provider attempts together."""
        request_id = payload.get("request_id")
        if self._db.query(RECOGNITIONREQUEST).filter(
                RECOGNITIONREQUEST.REQUEST_ID == request_id).first():
            return request_id
        request = RECOGNITIONREQUEST(
            REQUEST_ID=request_id,
            ORIGINAL_NAME=payload.get("original_name"),
            SOURCE=payload.get("source"),
            STAGE=payload.get("stage"),
            CREATED_AT=payload.get("created_at"),
            SUMMARY=self._json_dumps(self.recognition_summary(payload)),
            CONTEXT=self._json_dumps(payload.get("context")),
            ACTIONS=self._json_dumps(payload.get("actions")),
            PROVIDER_RESULTS=self._json_dumps(payload.get("provider_results")),
            OVERALL_RESULT=self._json_dumps(payload.get("overall_result")),
            TMDB_RESULTS=self._json_dumps(payload.get("tmdb_results")),
        )
        self._db.insert(request)
        attempts = []
        for result in payload.get("provider_results") or []:
            attempts.append(RECOGNITIONATTEMPT(
                REQUEST_ID=request_id,
                ATTEMPT_ID=result.get("attempt_id"),
                PROVIDER_ID=result.get("provider_id"),
                STATUS=result.get("status"),
                INPUT=self._json_dumps(result.get("input")),
                RAW_RESULT=self._json_dumps(result.get("raw_result")),
                NORMALIZED_RESULT=self._json_dumps(result.get("normalized_result")),
                TMDB_RESULTS=self._json_dumps(result.get("tmdb_results")),
                ERROR=result.get("error"),
                ELAPSED_MS=result.get("elapsed_ms"),
            ))
        self._db.insert_many(RECOGNITIONATTEMPT, attempts)
        return request_id

    @DbPersist(_db)
    def append_recognition_business_action(self, request_id, action_type, reason,
                                           details=None, business_status="skipped"):
        """Append a late business-branch decision to its original parse record."""
        request = self._db.query(RECOGNITIONREQUEST).filter(
            RECOGNITIONREQUEST.REQUEST_ID == request_id).first()
        if not request:
            return False

        def load_json(value, fallback):
            try:
                parsed = json.loads(value) if value else fallback
                return parsed if isinstance(parsed, type(fallback)) else fallback
            except (TypeError, json.JSONDecodeError):
                return fallback

        actions = load_json(request.ACTIONS, [])
        attempts = load_json(request.PROVIDER_RESULTS, [])
        overall = load_json(request.OVERALL_RESULT, {})
        ai_attempt = next((item for item in reversed(attempts)
                           if item.get("provider_id") == "anitopy_ml"
                           and item.get("status") == "skipped"), None)
        update_ai_error = bool(ai_attempt and str(ai_attempt.get("error") or "")
                               .startswith("ai_deferred"))
        sequence = max((int(item.get("sequence", 0)) for item in actions
                        if isinstance(item, dict)), default=0) + 1
        action = {
            "action_id": f"{request_id}:{sequence}",
            "sequence": sequence,
            "action_type": action_type,
            "provider_id": "anitopy_ml",
            "attempt_id": ai_attempt.get("attempt_id") if ai_attempt else None,
            "status": business_status,
            "input": {},
            "output": details or {},
            "reason": reason,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
        }
        actions.append(action)
        if update_ai_error:
            ai_attempt["error"] = reason
        overall["business_result"] = {
            "status": business_status,
            "reason": reason,
            "details": details or {},
        }
        payload = {
            "overall_result": overall,
            "provider_results": attempts,
            "tmdb_results": load_json(request.TMDB_RESULTS, []),
            "actions": actions,
        }
        request.ACTIONS = self._json_dumps(actions)
        request.PROVIDER_RESULTS = self._json_dumps(attempts)
        request.OVERALL_RESULT = self._json_dumps(overall)
        request.SUMMARY = self._json_dumps(self.recognition_summary(payload))

        if update_ai_error:
            attempt_row = self._db.query(RECOGNITIONATTEMPT).filter(
                RECOGNITIONATTEMPT.REQUEST_ID == request_id,
                RECOGNITIONATTEMPT.ATTEMPT_ID == ai_attempt.get("attempt_id")).first()
            if attempt_row:
                attempt_row.ERROR = reason
        return True

    def get_recognition_records(self, title=None, source=None, status=None,
                                provider_id=None, action_type=None, reason=None,
                                created_from=None, created_to=None,
                                page=1, page_size=20, snapshot_at=None,
                                before_cursor=None, include_total=True):
        """Return a paged recognition-record summary query."""
        page = max(int(page or 1), 1)
        page_size = min(max(int(page_size or 20), 1), 100)
        query = self._db.query(RECOGNITIONREQUEST)
        if title:
            query = query.filter(RECOGNITIONREQUEST.ORIGINAL_NAME.contains(title))
        if source:
            query = query.filter(RECOGNITIONREQUEST.SOURCE == source)
        if status:
            query = query.filter(RECOGNITIONREQUEST.OVERALL_RESULT.contains('"status": "' + status + '"'))
        if reason:
            query = query.filter(RECOGNITIONREQUEST.OVERALL_RESULT.contains('"reason": "' + reason + '"'))
        if created_from:
            query = query.filter(RECOGNITIONREQUEST.CREATED_AT >= created_from)
        if created_to:
            query = query.filter(RECOGNITIONREQUEST.CREATED_AT <= created_to)
        if snapshot_at:
            query = query.filter(RECOGNITIONREQUEST.CREATED_AT <= snapshot_at)
        if before_cursor:
            cursor_created_at, cursor_request_id = before_cursor
            query = query.filter(or_(
                RECOGNITIONREQUEST.CREATED_AT < cursor_created_at,
                and_(RECOGNITIONREQUEST.CREATED_AT == cursor_created_at,
                     RECOGNITIONREQUEST.REQUEST_ID < cursor_request_id),
            ))
        if provider_id:
            query = query.filter(RECOGNITIONREQUEST.REQUEST_ID.in_(
                self._db.query(RECOGNITIONATTEMPT.REQUEST_ID)
                .filter(RECOGNITIONATTEMPT.PROVIDER_ID == provider_id).distinct()))
        if action_type:
            query = query.filter(RECOGNITIONREQUEST.ACTIONS.contains('"action_type": "' + action_type + '"'))
        total = query.count() if include_total else None
        records = query.order_by(RECOGNITIONREQUEST.CREATED_AT.desc(),
                                 RECOGNITIONREQUEST.REQUEST_ID.desc()) \
            .offset((page - 1) * page_size).limit(page_size).all()
        return total, records

    def get_recognition_record(self, request_id):
        """Return one complete request and its indexed attempt rows."""
        request = self._db.query(RECOGNITIONREQUEST).filter(
            RECOGNITIONREQUEST.REQUEST_ID == request_id).first()
        if not request:
            return None, []
        attempts = self._db.query(RECOGNITIONATTEMPT).filter(
            RECOGNITIONATTEMPT.REQUEST_ID == request_id).order_by(RECOGNITIONATTEMPT.ID.asc()).all()
        return request, attempts

    @DbPersist(_db)
    def delete_expired_recognition_records(self, retention_days, now=None, batch_size=500):
        """删除超过保留期限的识别请求及其解析器明细，跳过仍在运行的请求。"""
        retention_days = int(retention_days)
        if not 1 <= retention_days <= 36500:
            raise ValueError("识别记录保留天数必须介于 1 和 36500 之间")
        batch_size = min(max(int(batch_size or 500), 1), 2000)
        now = now or datetime.datetime.now()
        older_than = (now - datetime.timedelta(days=retention_days)).strftime(
            "%Y-%m-%d %H:%M:%S.%f")
        deleted_count = 0
        while True:
            request_ids = [row[0] for row in self._db.query(
                RECOGNITIONREQUEST.REQUEST_ID).filter(
                    RECOGNITIONREQUEST.CREATED_AT < older_than,
                    or_(RECOGNITIONREQUEST.OVERALL_RESULT.is_(None),
                        RECOGNITIONREQUEST.OVERALL_RESULT.notlike('%"status"%running%'))
                ).order_by(RECOGNITIONREQUEST.CREATED_AT.asc(),
                           RECOGNITIONREQUEST.REQUEST_ID.asc()).limit(batch_size).all()]
            if not request_ids:
                break
            self._db.query(RECOGNITIONATTEMPT).filter(
                RECOGNITIONATTEMPT.REQUEST_ID.in_(request_ids)).delete(
                    synchronize_session=False)
            deleted_count += self._db.query(RECOGNITIONREQUEST).filter(
                RECOGNITIONREQUEST.REQUEST_ID.in_(request_ids)).delete(
                    synchronize_session=False)
        return deleted_count

    def archive_recognition_records(self, older_than, archive_path, delete_archived=False,
                                    batch_size=250):
        """Write full old records to a new JSONL archive; DB deletion is explicit opt-in.

        This is an operator-invoked maintenance primitive. It is never called by
        request handling, startup, retention timers, or the recognition UI.
        Existing archive paths are rejected to prevent accidental overwrite.
        """
        if not older_than or not archive_path:
            raise ValueError("older_than and archive_path are required")
        batch_size = min(max(int(batch_size or 250), 1), 1000)
        archive_path = os.path.abspath(os.path.expanduser(archive_path))
        archive_dir = os.path.dirname(archive_path)
        os.makedirs(archive_dir, exist_ok=True)
        temporary_path = None
        archived_count = 0
        last_created = ""
        last_request_id = ""
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=".recognition-archive-", suffix=".tmp", dir=archive_dir)
            with os.fdopen(descriptor, "w", encoding="utf-8") as archive_file:
                while True:
                    query = self._db.query(RECOGNITIONREQUEST).filter(
                        RECOGNITIONREQUEST.CREATED_AT < older_than)
                    if last_created:
                        query = query.filter(or_(
                            RECOGNITIONREQUEST.CREATED_AT > last_created,
                            and_(RECOGNITIONREQUEST.CREATED_AT == last_created,
                                 RECOGNITIONREQUEST.REQUEST_ID > last_request_id)))
                    requests = query.order_by(RECOGNITIONREQUEST.CREATED_AT.asc(),
                                              RECOGNITIONREQUEST.REQUEST_ID.asc()) \
                        .limit(batch_size).all()
                    if not requests:
                        break
                    request_ids = [request.REQUEST_ID for request in requests]
                    attempts = self._db.query(RECOGNITIONATTEMPT).filter(
                        RECOGNITIONATTEMPT.REQUEST_ID.in_(request_ids)) \
                        .order_by(RECOGNITIONATTEMPT.ID.asc()).all()
                    attempts_by_request = {}
                    for attempt in attempts:
                        attempts_by_request.setdefault(attempt.REQUEST_ID, []).append({
                            "id": attempt.ID,
                            "attempt_id": attempt.ATTEMPT_ID,
                            "provider_id": attempt.PROVIDER_ID,
                            "status": attempt.STATUS,
                            "input": attempt.INPUT,
                            "raw_result": attempt.RAW_RESULT,
                            "normalized_result": attempt.NORMALIZED_RESULT,
                            "tmdb_results": attempt.TMDB_RESULTS,
                            "error": attempt.ERROR,
                            "elapsed_ms": attempt.ELAPSED_MS,
                        })
                    for request in requests:
                        archive_record = {
                            "request_id": request.REQUEST_ID,
                            "original_name": request.ORIGINAL_NAME,
                            "source": request.SOURCE,
                            "stage": request.STAGE,
                            "created_at": request.CREATED_AT,
                            "summary": request.SUMMARY,
                            "context": request.CONTEXT,
                            "actions": request.ACTIONS,
                            "provider_results": request.PROVIDER_RESULTS,
                            "overall_result": request.OVERALL_RESULT,
                            "tmdb_results": request.TMDB_RESULTS,
                            "attempts": attempts_by_request.get(request.REQUEST_ID, []),
                        }
                        archive_file.write(json.dumps(archive_record, ensure_ascii=False,
                                                      default=self._json_default) + "\n")
                        archived_count += 1
                    last_created = requests[-1].CREATED_AT
                    last_request_id = requests[-1].REQUEST_ID
                archive_file.flush()
                os.fsync(archive_file.fileno())
            if not archived_count:
                os.unlink(temporary_path)
                temporary_path = None
                return {"archived_count": 0, "deleted_count": 0, "archive_path": None}
            # Hard-link publication is atomic and fails if an archive already exists.
            os.link(temporary_path, archive_path)
            os.unlink(temporary_path)
            temporary_path = None
            directory_fd = os.open(archive_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            deleted_count = 0
            if delete_archived:
                try:
                    request_ids = []

                    def delete_batch(batch_ids):
                        nonlocal deleted_count
                        if not batch_ids:
                            return
                        self._db.query(RECOGNITIONATTEMPT).filter(
                            RECOGNITIONATTEMPT.REQUEST_ID.in_(batch_ids)).delete(
                                synchronize_session=False)
                        deleted_count += self._db.query(RECOGNITIONREQUEST).filter(
                            RECOGNITIONREQUEST.REQUEST_ID.in_(batch_ids)).delete(
                                synchronize_session=False)
                    with open(archive_path, "r", encoding="utf-8") as archive_file:
                        for line in archive_file:
                            request_id = json.loads(line).get("request_id")
                            if request_id:
                                request_ids.append(request_id)
                            if len(request_ids) >= 500:
                                delete_batch(request_ids)
                                request_ids = []
                    delete_batch(request_ids)
                    self._db.commit()
                except Exception:
                    self._db.rollback()
                    raise
            return {"archived_count": archived_count, "deleted_count": deleted_count,
                    "archive_path": archive_path}
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def get_running_tasks(self):
        """
        查询所有运行中的任务（用于进程恢复）
        """
        return self._db.query(SEARCHTASK).filter(SEARCHTASK.STATUS == 'running').all()

    @DbPersist(_db)
    def cleanup_search_tasks(self, max_tasks=20):
        """
        清理超出 max_tasks 的旧任务及其结果
        """
        tasks = self._db.query(SEARCHTASK).order_by(SEARCHTASK.ID.desc()).all()
        if len(tasks) > max_tasks:
            for task in tasks[max_tasks:]:
                self._db.query(SEARCHRESULTINFO).filter(SEARCHRESULTINFO.KEYWORD == task.KEYWORD).delete()
                self._db.query(SEARCHTASK).filter(SEARCHTASK.ID == task.ID).delete()

    @DbPersist(_db)
    def delete_search_task(self, keyword):
        """
        删除指定关键词的搜索任务及其结果
        """
        self._db.query(SEARCHRESULTINFO).filter(SEARCHRESULTINFO.KEYWORD == keyword).delete()
        self._db.query(SEARCHTASK).filter(SEARCHTASK.KEYWORD == keyword).delete()

    @DbPersist(_db)
    def insert_rss_torrents(self, media_info):
        """
        将RSS的记录插入数据库
        """
        self._db.upsert_many(RSSTORRENTS, [{
            "TORRENT_NAME": media_info.org_string,
            "ENCLOSURE": media_info.enclosure,
            "TYPE": media_info.type.value,
            "TITLE": media_info.title,
            "YEAR": media_info.year,
            "SEASON": media_info.get_season_string(),
            "EPISODE": media_info.get_episode_string()
        }], ("ENCLOSURE",),
            ("TORRENT_NAME", "TYPE", "TITLE", "YEAR", "SEASON", "EPISODE"))

    @DbPersist(_db)
    def insert_rss_torrents_many(self, media_infos):
        """批量插入 RSS 历史，去重和提交都在批次级别完成。"""
        rows = []
        seen = set()
        for media_info in media_infos or []:
            if not media_info:
                continue
            key = media_info.enclosure or media_info.org_string
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append({
                "TORRENT_NAME": media_info.org_string,
                "ENCLOSURE": media_info.enclosure,
                "TYPE": media_info.type.value,
                "TITLE": media_info.title,
                "YEAR": media_info.year,
                "SEASON": media_info.get_season_string(),
                "EPISODE": media_info.get_episode_string()
            })
        return self._db.upsert_many(
            RSSTORRENTS,
            rows,
            ("ENCLOSURE",),
            ("TORRENT_NAME", "TYPE", "TITLE", "YEAR", "SEASON", "EPISODE")
        )

    @DbPersist(_db)
    def simple_insert_rss_torrents(self, title, enclosure):
        """
        将RSS的记录插入数据库
        """
        if enclosure:
            self._db.upsert_many(RSSTORRENTS, [{
                "TORRENT_NAME": title,
                "ENCLOSURE": enclosure
            }], ("ENCLOSURE",))
        else:
            self._db.insert(RSSTORRENTS(TORRENT_NAME=title, ENCLOSURE=enclosure))

    @DbPersist(_db)
    def simple_delete_rss_torrents(self, title, enclosure):
        """
        删除RSS的记录
        """
        if enclosure:
            self._db.query(RSSTORRENTS).filter(RSSTORRENTS.TORRENT_NAME == title,
                                               RSSTORRENTS.ENCLOSURE == enclosure).delete()
        else:
            self._db.query(RSSTORRENTS).filter(RSSTORRENTS.TORRENT_NAME == title).delete()

    def is_douban_media_exists(self, media):
        """
        查询豆瓣是否存在
        """
        if not media:
            return True
        if self._db.query(DOUBANMEDIAS).filter(DOUBANMEDIAS.NAME == media.get_name()).count() > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_douban_media_state(self, media, state):
        """
        将豆瓣的数据插入数据库
        """
        if not media or not state:
            return
        if self.is_douban_media_exists(media):
            return
        else:
            # 插入
            self._db.insert(
                DOUBANMEDIAS(
                    NAME=media.get_name(),
                    YEAR=media.year,
                    TYPE=media.type.value,
                    RATING=media.vote_average,
                    IMAGE=media.get_poster_image(),
                    STATE=state,
                    ADD_TIME=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
                )
            )

    @DbPersist(_db)
    def update_douban_media_state(self, media, state):
        """
        标记豆瓣数据的状态
        """
        self._db.query(DOUBANMEDIAS).filter(DOUBANMEDIAS.NAME == media.title,
                                            DOUBANMEDIAS.YEAR == media.year).update(
            {
                "STATE": state
            }
        )

    def get_douban_search_state(self, title, year=None):
        """
        查询未检索的豆瓣数据
        """
        if not year:
            return self._db.query(DOUBANMEDIAS.STATE).filter(DOUBANMEDIAS.NAME == title).first()
        else:
            return self._db.query(DOUBANMEDIAS.STATE).filter(DOUBANMEDIAS.NAME == title,
                                                             DOUBANMEDIAS.YEAR == str(year)).first()

    def is_transfer_history_exists(self, source_path, source_filename, dest_path, dest_filename):
        """
        查询识别转移记录
        """
        if not source_path or not source_filename or not dest_path or not dest_filename:
            return False
        ret = self._db.query(TRANSFERHISTORY).filter(TRANSFERHISTORY.SOURCE_PATH == source_path,
                                                     TRANSFERHISTORY.SOURCE_FILENAME == source_filename,
                                                     TRANSFERHISTORY.DEST_PATH == dest_path,
                                                     TRANSFERHISTORY.DEST_FILENAME == dest_filename).count()
        return True if ret > 0 else False

    @DbPersist(_db)
    def insert_transfer_history(self, in_from: Enum, rmt_mode: RmtMode, in_path, out_path, dest, media_info):
        """
        插入识别转移记录
        """
        if not media_info or not media_info.tmdb_info:
            return
        if in_path:
            in_path = os.path.normpath(in_path)
            source_path = os.path.dirname(in_path)
            source_filename = os.path.basename(in_path)
        else:
            return
        if out_path:
            outpath = os.path.normpath(out_path)
            dest_path = os.path.dirname(outpath)
            dest_filename = os.path.basename(outpath)
            season_episode = media_info.get_season_episode_string()
        else:
            dest_path = ""
            dest_filename = ""
            season_episode = media_info.get_season_string()
        title = media_info.title
        if self.is_transfer_history_exists(source_path, source_filename, dest_path, dest_filename):
            return
        dest = dest or ""
        timestr = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
        self._db.insert(
            TRANSFERHISTORY(
                MODE=str(rmt_mode.value),
                TYPE=media_info.type.value,
                CATEGORY=media_info.category,
                TMDBID=int(media_info.tmdb_id),
                TITLE=title,
                YEAR=media_info.year,
                SEASON_EPISODE=season_episode,
                SOURCE=str(in_from.value),
                SOURCE_PATH=source_path,
                SOURCE_FILENAME=source_filename,
                DEST=dest,
                DEST_PATH=dest_path,
                DEST_FILENAME=dest_filename,
                DATE=timestr
            )
        )

    def get_transfer_history(self, search, page, rownum):
        """
        查询识别转移记录
        """
        if int(page) == 1:
            begin_pos = 0
        else:
            begin_pos = (int(page) - 1) * int(rownum)

        if search:
            search = f"%{search}%"
            count = self._db.query(TRANSFERHISTORY).filter((TRANSFERHISTORY.SOURCE_FILENAME.like(search))
                                                           | (TRANSFERHISTORY.TITLE.like(search))).count()
            data = self._db.query(TRANSFERHISTORY).filter((TRANSFERHISTORY.SOURCE_FILENAME.like(search))
                                                          | (TRANSFERHISTORY.TITLE.like(search))).order_by(
                TRANSFERHISTORY.DATE.desc()).limit(int(rownum)).offset(begin_pos).all()
            return count, data
        else:
            return self._db.query(TRANSFERHISTORY).count(), self._db.query(TRANSFERHISTORY).order_by(
                TRANSFERHISTORY.DATE.desc()).limit(int(rownum)).offset(begin_pos).all()

    def get_transfer_path_by_id(self, logid):
        """
        据logid查询PATH
        """
        return self._db.query(TRANSFERHISTORY).filter(TRANSFERHISTORY.ID == int(logid)).all()

    def is_transfer_history_exists_by_source_full_path(self, source_full_path):
        """
        据源文件的全路径查询识别转移记录
        """

        path = os.path.dirname(source_full_path)
        filename = os.path.basename(source_full_path)
        ret = self._db.query(TRANSFERHISTORY).filter(TRANSFERHISTORY.SOURCE_PATH == path,
                                                     TRANSFERHISTORY.SOURCE_FILENAME == filename).count()
        if ret > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def delete_transfer_log_by_id(self, logid):
        """
        根据logid删除记录
        """
        self._db.query(TRANSFERHISTORY).filter(TRANSFERHISTORY.ID == int(logid)).delete()

    def get_transfer_unknown_paths(self, ):
        """
        查询未识别的记录列表
        """
        return self._db.query(TRANSFERUNKNOWN).filter(TRANSFERUNKNOWN.STATE == 'N').all()

    @DbPersist(_db)
    def update_transfer_unknown_state(self, path):
        """
        更新未识别记录为识别
        """
        if not path:
            return
        self._db.query(TRANSFERUNKNOWN).filter(TRANSFERUNKNOWN.PATH == os.path.normpath(path)).update(
            {
                "STATE": "Y"
            }
        )

    @DbPersist(_db)
    def delete_transfer_unknown(self, tid):
        """
        删除未识别记录
        """
        if not tid:
            return []
        self._db.query(TRANSFERUNKNOWN).filter(TRANSFERUNKNOWN.ID == int(tid)).delete()

    def get_unknown_path_by_id(self, tid):
        """
        查询未识别记录
        """
        if not tid:
            return []
        return self._db.query(TRANSFERUNKNOWN).filter(TRANSFERUNKNOWN.ID == int(tid)).all()

    def get_transfer_unknown_by_path(self, path):
        """
        根据路径查询未识别记录
        """
        if not path:
            return []
        return self._db.query(TRANSFERUNKNOWN).filter(TRANSFERUNKNOWN.PATH == path).all()

    def is_transfer_unknown_exists(self, path):
        """
        查询未识别记录是否存在
        """
        if not path:
            return False
        ret = self._db.query(TRANSFERUNKNOWN).filter(TRANSFERUNKNOWN.PATH == os.path.normpath(path)).count()
        if ret > 0:
            return True
        else:
            return False

    def is_need_insert_transfer_unknown(self, path):
        """
        检查是否需要插入未识别记录
        """
        if not path:
            return False

        """
        1) 如果不存在未识别，则插入
        2) 如果存在未处理的未识别，则插入（并不会真正的插入，insert_transfer_unknown里会挡住，主要是标记进行消息推送）
        3) 如果未识别已经全部处理完并且存在转移记录，则不插入
        4) 如果未识别已经全部处理完并且不存在转移记录，则删除并重新插入
        """
        unknowns = self.get_transfer_unknown_by_path(path)
        if unknowns:
            is_all_proceed = True
            for unknown in unknowns:
                if unknown.STATE == 'N':
                    is_all_proceed = False
                    break

            if is_all_proceed:
                is_transfer_history_exists = self.is_transfer_history_exists_by_source_full_path(path)
                if is_transfer_history_exists:
                    # 对应 3)
                    return False
                else:
                    # 对应 4)
                    for unknown in unknowns:
                        self.delete_transfer_unknown(unknown.ID)
                    return True
            else:
                # 对应 2)
                return True
        else:
            # 对应 1)
            return True

    @DbPersist(_db)
    def insert_transfer_unknown(self, path, dest, rmt_mode):
        """
        插入未识别记录
        """
        if not path:
            return
        if self.is_transfer_unknown_exists(path):
            return
        else:
            path = os.path.normpath(path)
            if dest:
                dest = os.path.normpath(dest)
            else:
                dest = ""
            self._db.insert(TRANSFERUNKNOWN(
                PATH=path,
                DEST=dest,
                STATE='N',
                MODE=str(rmt_mode.value)
            ))

    def is_transfer_in_blacklist(self, path):
        """
        查询是否为黑名单
        """
        if not path:
            return False
        ret = self._db.query(TRANSFERBLACKLIST).filter(TRANSFERBLACKLIST.PATH == os.path.normpath(path)).count()
        if ret > 0:
            return True
        else:
            return False

    def is_transfer_notin_blacklist(self, path):
        """
        查询是否为黑名单
        """
        return not self.is_transfer_in_blacklist(path)

    @DbPersist(_db)
    def insert_transfer_blacklist(self, path):
        """
        插入黑名单记录
        """
        if not path:
            return
        if self.is_transfer_in_blacklist(path):
            return
        else:
            self._db.insert(TRANSFERBLACKLIST(
                PATH=os.path.normpath(path)
            ))

    @DbPersist(_db)
    def truncate_transfer_blacklist(self, ):
        """
        清空黑名单记录
        """
        self._db.query(TRANSFERBLACKLIST).delete()
        self._db.query(SYNCHISTORY).delete()

    @DbPersist(_db)
    def truncate_rss_history(self, ):
        """
        清空RSS历史记录
        """
        self._db.query(RSSTORRENTS).delete()

    @DbPersist(_db)
    def truncate_rss_episodes(self, ):
        """
        清空RSS历史记录
        """
        self._db.query(RSSTVEPISODES).delete()

    def get_config_filter_group(self, gid=None):
        """
        查询过滤规则组
        """
        if gid:
            return self._db.query(CONFIGFILTERGROUP).filter(CONFIGFILTERGROUP.ID == int(gid)).all()
        return self._db.query(CONFIGFILTERGROUP).all()

    def get_config_filter_rule(self, groupid=None):
        """
        查询过滤规则
        """
        if not groupid:
            return self._db.query(CONFIGFILTERRULES).order_by(CONFIGFILTERRULES.GROUP_ID,
                                                              cast(CONFIGFILTERRULES.PRIORITY,
                                                                   Integer)).all()
        else:
            return self._db.query(CONFIGFILTERRULES).filter(
                CONFIGFILTERRULES.GROUP_ID == int(groupid)).order_by(CONFIGFILTERRULES.GROUP_ID,
                                                                     cast(CONFIGFILTERRULES.PRIORITY,
                                                                          Integer)).all()

    def get_rss_movies(self, state=None, rssid=None):
        """
        查询订阅电影信息
        """
        if rssid:
            return self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rssid)).all()
        else:
            if not state:
                return self._db.query(RSSMOVIES).all()
            else:
                return self._db.query(RSSMOVIES).filter(RSSMOVIES.STATE == state).all()

    def get_rss_movie_id(self, title, year=None, tmdbid=None):
        """
        获取订阅电影ID
        """
        if not title:
            return ""
        if tmdbid:
            ret = self._db.query(RSSMOVIES.ID).filter(RSSMOVIES.TMDBID == str(tmdbid)).first()
            if ret:
                return ret[0]
        if not year:
            items = self._db.query(RSSMOVIES).filter(RSSMOVIES.NAME == title).all()
        else:
            items = self._db.query(RSSMOVIES).filter(RSSMOVIES.NAME == title,
                                                     RSSMOVIES.YEAR == str(year)).all()
        if items:
            if tmdbid:
                for item in items:
                    if not item.TMDBID or item.TMDBID == str(tmdbid):
                        return item.ID
            else:
                return items[0].ID
        else:
            return ""

    @DbPersist(_db)
    def update_rss_movie_tmdb(self, rid, tmdbid, title, year, image, desc, note):
        """
        更新订阅电影的部分信息
        """
        if not tmdbid:
            return
        self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rid)).update({
            "TMDBID": tmdbid,
            "NAME": title,
            "YEAR": year,
            "IMAGE": image,
            "NOTE": note,
            "DESC": desc
        })

    @DbPersist(_db)
    def update_rss_movie_desc(self, rid, desc):
        """
        更新订阅电影的DESC
        """
        self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rid)).update({
            "DESC": desc
        })

    @DbPersist(_db)
    def update_rss_filter_order(self, rtype, rssid, res_order):
        """
        更新订阅命中的过滤规则优先级
        """
        if rtype == MediaType.MOVIE:
            self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rssid)).update({
                "FILTER_ORDER": res_order
            })
        else:
            self._db.query(RSSTVS).filter(RSSTVS.ID == int(rssid)).update({
                "FILTER_ORDER": res_order
            })

    def get_rss_overedition_order(self, rtype, rssid):
        """
        查询当前订阅的过滤优先级
        """
        if rtype == MediaType.MOVIE:
            res = self._db.query(RSSMOVIES.FILTER_ORDER).filter(RSSMOVIES.ID == int(rssid)).first()
        else:
            res = self._db.query(RSSTVS.FILTER_ORDER).filter(RSSTVS.ID == int(rssid)).first()
        if res and res[0]:
            return int(res[0])
        else:
            return 0

    def is_exists_rss_movie(self, title, year):
        """
        判断RSS电影是否存在
        """
        if not title:
            return False
        count = self._db.query(RSSMOVIES).filter(RSSMOVIES.NAME == title,
                                                 RSSMOVIES.YEAR == str(year)).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_rss_movie(self, media_info,
                         state='D',
                         search_sites=None,
                         over_edition=0,
                         filter_restype=None,
                         filter_pix=None,
                         filter_team=None,
                         filter_rule=None,
                         save_path=None,
                         download_setting=-1,
                         fuzzy_match=0,
                         desc=None,
                         note=None,
                         keyword=None):
        """
        新增RSS电影
        """
        if search_sites is None:
            search_sites = []
        if not media_info:
            return -1
        if not media_info.title:
            return -1
        if self.is_exists_rss_movie(media_info.title, media_info.year):
            return 9
        self._db.insert(RSSMOVIES(
            NAME=media_info.title,
            YEAR=media_info.year,
            TMDBID=media_info.tmdb_id,
            IMAGE=media_info.get_message_image(),
            SEARCH_SITES=json.dumps(search_sites),
            OVER_EDITION=over_edition,
            FILTER_RESTYPE=filter_restype,
            FILTER_PIX=filter_pix,
            FILTER_RULE=filter_rule,
            FILTER_TEAM=filter_team,
            SAVE_PATH=save_path,
            DOWNLOAD_SETTING=download_setting,
            FUZZY_MATCH=fuzzy_match,
            STATE=state,
            DESC=desc,
            NOTE=note,
            KEYWORD=keyword
        ))
        return 0

    @DbPersist(_db)
    def delete_rss_movie(self, title=None, year=None, rssid=None, tmdbid=None):
        """
        删除RSS电影
        """
        if not title and not rssid:
            return
        if rssid:
            self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rssid)).delete()
        else:
            if tmdbid:
                self._db.query(RSSMOVIES).filter(RSSMOVIES.TMDBID == tmdbid).delete()
            self._db.query(RSSMOVIES).filter(RSSMOVIES.NAME == title,
                                             RSSMOVIES.YEAR == str(year)).delete()

    @DbPersist(_db)
    def update_rss_movie_state(self, title=None, year=None, rssid=None, state='R'):
        """
        更新电影订阅状态
        """
        if not title and not rssid:
            return
        if rssid:
            self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rssid)).update(
                {
                    "STATE": state
                })
        else:
            self._db.query(RSSMOVIES).filter(RSSMOVIES.NAME == title,
                                             RSSMOVIES.YEAR == str(year)).update(
                {
                    "STATE": state
                })

    def get_rss_tvs(self, state=None, rssid=None):
        """
        查询订阅电视剧信息
        """
        if rssid:
            return self._db.query(RSSTVS).filter(RSSTVS.ID == int(rssid)).all()
        else:
            if not state:
                return self._db.query(RSSTVS).all()
            else:
                return self._db.query(RSSTVS).filter(RSSTVS.STATE == state).all()

    def get_rss_tv_id(self, title, year=None, season=None, tmdbid=None):
        """
        获取订阅电视剧ID
        """
        if not title:
            return ""
        if tmdbid:
            if season:
                ret = self._db.query(RSSTVS.ID).filter(RSSTVS.TMDBID == tmdbid,
                                                       RSSTVS.SEASON == season).first()
            else:
                ret = self._db.query(RSSTVS.ID).filter(RSSTVS.TMDBID == tmdbid).first()
            if ret:
                return ret[0]
        if season and year:
            items = self._db.query(RSSTVS).filter(RSSTVS.NAME == title,
                                                  RSSTVS.SEASON == str(season),
                                                  RSSTVS.YEAR == str(year)).all()
        elif season and not year:
            items = self._db.query(RSSTVS).filter(RSSTVS.NAME == title,
                                                  RSSTVS.SEASON == str(season)).all()
        elif not season and year:
            items = self._db.query(RSSTVS).filter(RSSTVS.NAME == title,
                                                  RSSTVS.YEAR == str(year)).all()
        else:
            items = self._db.query(RSSTVS).filter(RSSTVS.NAME == title).all()
        if items:
            if tmdbid:
                for item in items:
                    if not item.TMDBID or item.TMDBID == str(tmdbid):
                        return item.ID
            else:
                return items[0].ID
        else:
            return ""

    @DbPersist(_db)
    def update_rss_tv_tmdb(self, rid, tmdbid, title, year, total, lack, image, desc, note):
        """
        更新订阅电影的TMDBID
        """
        if not tmdbid:
            return
        self._db.query(RSSTVS).filter(RSSTVS.ID == int(rid)).update(
            {
                "TMDBID": tmdbid,
                "NAME": title,
                "YEAR": year,
                "TOTAL": total,
                "LACK": lack,
                "IMAGE": image,
                "DESC": desc,
                "NOTE": note
            }
        )

    @DbPersist(_db)
    def update_rss_tv_desc(self, rid, desc):
        """
        更新订阅电视剧的DESC
        """
        self._db.query(RSSTVS).filter(RSSTVS.ID == int(rid)).update(
            {
                "DESC": desc
            }
        )

    def is_exists_rss_tv(self, title, year, season=None):
        """
        判断RSS电视剧是否存在
        """
        if not title:
            return False
        if season:
            count = self._db.query(RSSTVS).filter(RSSTVS.NAME == title,
                                                  RSSTVS.YEAR == str(year),
                                                  RSSTVS.SEASON == season).count()
        else:
            count = self._db.query(RSSTVS).filter(RSSTVS.NAME == title,
                                                  RSSTVS.YEAR == str(year)).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_rss_tv(self,
                      media_info,
                      total,
                      lack=0,
                      state="D",
                      search_sites=None,
                      over_edition=0,
                      filter_restype=None,
                      filter_pix=None,
                      filter_team=None,
                      filter_rule=None,
                      save_path=None,
                      download_setting=-1,
                      total_ep=None,
                      current_ep=None,
                      fuzzy_match=0,
                      desc=None,
                      note=None,
                      keyword=None):
        """
        新增RSS电视剧
        """
        if search_sites is None:
            search_sites = []
        if not media_info:
            return -1
        if not media_info.title:
            return -1
        if fuzzy_match and media_info.begin_season is None:
            season_str = ""
        else:
            season_str = media_info.get_season_string()
        if self.is_exists_rss_tv(media_info.title, media_info.year, season_str):
            return 9
        self._db.insert(RSSTVS(
            NAME=media_info.title,
            YEAR=media_info.year,
            SEASON=season_str,
            TMDBID=media_info.tmdb_id,
            IMAGE=media_info.get_message_image(),
            SEARCH_SITES=json.dumps(search_sites),
            OVER_EDITION=over_edition,
            FILTER_RESTYPE=filter_restype,
            FILTER_PIX=filter_pix,
            FILTER_RULE=filter_rule,
            FILTER_TEAM=filter_team,
            SAVE_PATH=save_path,
            DOWNLOAD_SETTING=download_setting,
            FUZZY_MATCH=fuzzy_match,
            TOTAL_EP=total_ep,
            CURRENT_EP=current_ep,
            TOTAL=total,
            LACK=lack,
            STATE=state,
            DESC=desc,
            NOTE=note,
            KEYWORD=keyword
        ))
        return 0

    @DbPersist(_db)
    def update_rss_tv_lack(self, title=None, year=None, season=None, rssid=None, lack_episodes: list = None):
        """
        更新电视剧缺失的集数
        """
        if not title and not rssid:
            return
        if not lack_episodes:
            lack = 0
        else:
            lack = len(lack_episodes)
        if rssid:
            self.update_rss_tv_episodes(rssid, lack_episodes)
            self._db.query(RSSTVS).filter(RSSTVS.ID == int(rssid)).update(
                {
                    "LACK": lack
                }
            )
        else:
            self._db.query(RSSTVS).filter(RSSTVS.NAME == title,
                                          RSSTVS.YEAR == str(year),
                                          RSSTVS.SEASON == season).update(
                {
                    "LACK": lack
                }
            )

    @DbPersist(_db)
    def delete_rss_tv(self, title=None, season=None, rssid=None, tmdbid=None):
        """
        删除RSS电视剧
        """
        if not title and not rssid:
            return
        if not rssid:
            rssid = self.get_rss_tv_id(title=title, tmdbid=tmdbid, season=season)
        if rssid:
            self.delete_rss_tv_episodes(rssid)
            self._db.query(RSSTVS).filter(RSSTVS.ID == int(rssid)).delete()

    def is_exists_rss_tv_episodes(self, rid):
        """
        判断RSS电视剧是否存在
        """
        if not rid:
            return False
        count = self._db.query(RSSTVEPISODES).filter(RSSTVEPISODES.RSSID == int(rid)).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def update_rss_tv_episodes(self, rid, episodes):
        """
        插入或更新电视剧订阅缺失剧集
        """
        if not rid:
            return
        if not episodes:
            episodes = []
        else:
            episodes = [str(epi) for epi in episodes]
        if self.is_exists_rss_tv_episodes(rid):
            self._db.query(RSSTVEPISODES).filter(RSSTVEPISODES.RSSID == int(rid)).update(
                {
                    "EPISODES": ",".join(episodes)
                }
            )
        else:
            self._db.insert(RSSTVEPISODES(
                RSSID=rid,
                EPISODES=",".join(episodes)
            ))

    def get_rss_tv_episodes(self, rid):
        """
        查询电视剧订阅缺失剧集
        """
        if not rid:
            return []
        ret = self._db.query(RSSTVEPISODES.EPISODES).filter(RSSTVEPISODES.RSSID == rid).first()
        if ret:
            return [int(epi) for epi in str(ret[0]).split(',')]
        else:
            return None

    @DbPersist(_db)
    def delete_rss_tv_episodes(self, rid):
        """
        删除电视剧订阅缺失剧集
        """
        if not rid:
            return
        self._db.query(RSSTVEPISODES).filter(RSSTVEPISODES.RSSID == int(rid)).delete()

    @DbPersist(_db)
    def update_rss_tv_state(self, title=None, year=None, season=None, rssid=None, state='R'):
        """
        更新电视剧订阅状态
        """
        if not title and not rssid:
            return
        if rssid:
            self._db.query(RSSTVS).filter(RSSTVS.ID == int(rssid)).update(
                {
                    "STATE": state
                })
        else:
            self._db.query(RSSTVS).filter(RSSTVS.NAME == title,
                                          RSSTVS.YEAR == str(year),
                                          RSSTVS.SEASON == season).update(
                {
                    "STATE": state
                })

    def is_sync_in_history(self, path, dest):
        """
        查询是否存在同步历史记录
        """
        if not path:
            return False
        count = self._db.query(SYNCHISTORY).filter(SYNCHISTORY.PATH == os.path.normpath(path),
                                                   SYNCHISTORY.DEST == os.path.normpath(dest)).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_sync_history(self, path, src, dest):
        """
        插入黑名单记录
        """
        if not path or not dest:
            return
        self._db.upsert_many(SYNCHISTORY, [{
            "PATH": os.path.normpath(path),
            "SRC": os.path.normpath(src),
            "DEST": os.path.normpath(dest)
        }], ("PATH", "DEST"), ("SRC",))

    def get_sync_history_set(self, dest):
        """按目标目录预加载已同步的规范化路径。"""
        if not dest:
            return set()
        return {os.path.normpath(path) for path, in self._db.query(SYNCHISTORY.PATH)
                .filter(SYNCHISTORY.DEST == os.path.normpath(dest)).all()}

    @DbPersist(_db)
    def insert_sync_history_many(self, records):
        rows = []
        seen = set()
        for path, src, dest in records or []:
            if not path or not dest:
                continue
            key = (os.path.normpath(path), os.path.normpath(dest))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"PATH": key[0], "SRC": os.path.normpath(src), "DEST": key[1]})
        return self._db.upsert_many(SYNCHISTORY, rows, ("PATH", "DEST"), ("SRC",))

    def get_users(self, ):
        """
        查询用户列表
        """
        return self._db.query(CONFIGUSERS).all()

    def is_user_exists(self, name):
        """
        判断用户是否存在
        """
        if not name:
            return False
        count = self._db.query(CONFIGUSERS).filter(CONFIGUSERS.NAME == name).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_user(self, name, password, pris):
        """
        新增用户
        """
        if not name or not password:
            return
        if self.is_user_exists(name):
            return
        else:
            self._db.insert(CONFIGUSERS(
                NAME=name,
                PASSWORD=password,
                PRIS=pris
            ))

    @DbPersist(_db)
    def delete_user(self, name):
        """
        删除用户
        """
        self._db.query(CONFIGUSERS).filter(CONFIGUSERS.NAME == name).delete()

    def get_transfer_statistics(self, days=30):
        """
        查询历史记录统计
        """
        begin_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return self._db.query(TRANSFERHISTORY.TYPE,
                              func.substr(TRANSFERHISTORY.DATE, 1, 10),
                              func.count('*')
                              ).filter(TRANSFERHISTORY.DATE > begin_date).group_by(
            func.substr(TRANSFERHISTORY.DATE, 1, 10)
        ).order_by(TRANSFERHISTORY.DATE).all()

    def is_exists_download_history(self, title, tmdbid, mtype=None):
        """
        查询下载历史是否存在
        """
        if not title or not tmdbid:
            return False
        if mtype:
            count = self._db.query(DOWNLOADHISTORY).filter(
                (DOWNLOADHISTORY.TITLE == title) | (DOWNLOADHISTORY.TMDBID == tmdbid),
                DOWNLOADHISTORY.TYPE == mtype).count()
        else:
            count = self._db.query(DOWNLOADHISTORY).filter(
                (DOWNLOADHISTORY.TITLE == title) | (DOWNLOADHISTORY.TMDBID == tmdbid)).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_download_history(self, media_info):
        """
        新增下载历史
        """
        if not media_info:
            return
        if not media_info.title or not media_info.tmdb_id:
            return
        if self.is_exists_download_history(media_info.title, media_info.tmdb_id, media_info.type.value):
            self._db.query(DOWNLOADHISTORY).filter(DOWNLOADHISTORY.TITLE == media_info.title,
                                                   DOWNLOADHISTORY.TMDBID == media_info.tmdb_id,
                                                   DOWNLOADHISTORY.TYPE == media_info.type.value).update(
                {
                    "TORRENT": media_info.org_string,
                    "ENCLOSURE": media_info.enclosure,
                    "DESC": media_info.description,
                    "DATE": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())),
                    "SITE": media_info.site
                }
            )
        else:
            self._db.insert(DOWNLOADHISTORY(
                TITLE=media_info.title,
                YEAR=media_info.year,
                TYPE=media_info.type.value,
                TMDBID=media_info.tmdb_id,
                VOTE=media_info.vote_average,
                POSTER=media_info.get_poster_image(),
                OVERVIEW=media_info.overview,
                TORRENT=media_info.org_string,
                ENCLOSURE=media_info.enclosure,
                DESC=media_info.description,
                DATE=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())),
                SITE=media_info.site
            ))

    def get_download_history(self, date=None, hid=None, num=30, page=1):
        """
        查询下载历史
        """
        if hid:
            return self._db.query(DOWNLOADHISTORY).filter(DOWNLOADHISTORY.ID == int(hid)).all()
        elif date:
            return self._db.query(DOWNLOADHISTORY).filter(
                DOWNLOADHISTORY.DATE > date).order_by(DOWNLOADHISTORY.DATE.desc()).all()
        else:
            offset = (int(page) - 1) * int(num)
            return self._db.query(DOWNLOADHISTORY).order_by(
                DOWNLOADHISTORY.DATE.desc()).limit(num).offset(offset).all()

    def is_media_downloaded(self, title, tmdbid):
        """
        根据标题和年份检查是否下载过
        """
        if self.is_exists_download_history(title, tmdbid):
            return True
        count = self._db.query(TRANSFERHISTORY).filter(TRANSFERHISTORY.TITLE == title).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def add_filter_group(self, name, default='N'):
        """
        新增规则组
        """
        if default == 'Y':
            self.set_default_filtergroup(0)
        group_id = self.get_filter_groupid_by_name(name)
        if group_id:
            self._db.query(CONFIGFILTERGROUP).filter(CONFIGFILTERGROUP.ID == int(group_id)).update({
                "IS_DEFAULT": default
            })
        else:
            self._db.insert(CONFIGFILTERGROUP(
                GROUP_NAME=name,
                IS_DEFAULT=default
            ))

    def get_filter_groupid_by_name(self, name):
        ret = self._db.query(CONFIGFILTERGROUP.ID).filter(CONFIGFILTERGROUP.GROUP_NAME == name).first()
        if ret:
            return ret[0]
        else:
            return ""

    @DbPersist(_db)
    def set_default_filtergroup(self, groupid):
        """
        设置默认的规则组
        """
        self._db.query(CONFIGFILTERGROUP).filter(CONFIGFILTERGROUP.ID == int(groupid)).update({
            "IS_DEFAULT": 'Y'
        })
        self._db.query(CONFIGFILTERGROUP).filter(CONFIGFILTERGROUP.ID != int(groupid)).update({
            "IS_DEFAULT": 'N'
        })

    @DbPersist(_db)
    def delete_filtergroup(self, groupid):
        """
        删除规则组
        """
        self._db.query(CONFIGFILTERRULES).filter(CONFIGFILTERRULES.GROUP_ID == groupid).delete()
        self._db.query(CONFIGFILTERGROUP).filter(CONFIGFILTERGROUP.ID == int(groupid)).delete()

    @DbPersist(_db)
    def delete_filterrule(self, ruleid):
        """
        删除规则
        """
        self._db.query(CONFIGFILTERRULES).filter(CONFIGFILTERRULES.ID == int(ruleid)).delete()

    @DbPersist(_db)
    def insert_filter_rule(self, item, ruleid=None):
        """
        新增规则
        """
        if ruleid:
            self._db.query(CONFIGFILTERRULES).filter(CONFIGFILTERRULES.ID == int(ruleid)).update(
                {
                    "ROLE_NAME": item.get("name"),
                    "PRIORITY": item.get("pri"),
                    "INCLUDE": item.get("include"),
                    "EXCLUDE": item.get("exclude"),
                    "SIZE_LIMIT": item.get("size"),
                    "NOTE": item.get("free")
                }
            )
        else:
            self._db.insert(CONFIGFILTERRULES(
                GROUP_ID=item.get("group"),
                ROLE_NAME=item.get("name"),
                PRIORITY=item.get("pri"),
                INCLUDE=item.get("include"),
                EXCLUDE=item.get("exclude"),
                SIZE_LIMIT=item.get("size"),
                NOTE=item.get("free")
            ))

    def get_userrss_tasks(self, tid=None):
        if tid:
            return self._db.query(CONFIGUSERRSS).filter(CONFIGUSERRSS.ID == int(tid)).all()
        else:
            return self._db.query(CONFIGUSERRSS).order_by(CONFIGUSERRSS.STATE.desc()).all()

    @DbPersist(_db)
    def delete_userrss_task(self, tid):
        if not tid:
            return
        self._db.query(CONFIGUSERRSS).filter(CONFIGUSERRSS.ID == int(tid)).delete()

    @DbPersist(_db)
    def update_userrss_task_info(self, tid, count):
        if not tid:
            return
        self._db.query(CONFIGUSERRSS).filter(CONFIGUSERRSS.ID == int(tid)).update(
            {
                "PROCESS_COUNT": CONFIGUSERRSS.PROCESS_COUNT + count,
                "UPDATE_TIME": time.strftime('%Y-%m-%d %H:%M:%S',
                                             time.localtime(time.time()))
            }
        )

    @DbPersist(_db)
    def update_userrss_task(self, item):
        if item.get("id") and self.get_userrss_tasks(item.get("id")):
            self._db.query(CONFIGUSERRSS).filter(CONFIGUSERRSS.ID == int(item.get("id"))).update(
                {
                    "NAME": item.get("name"),
                    "ADDRESS": item.get("address"),
                    "PARSER": item.get("parser"),
                    "INTERVAL": item.get("interval"),
                    "USES": item.get("uses"),
                    "INCLUDE": item.get("include"),
                    "EXCLUDE": item.get("exclude"),
                    "FILTER": item.get("filter_rule"),
                    "UPDATE_TIME": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())),
                    "STATE": item.get("state"),
                    "SAVE_PATH": item.get("save_path"),
                    "DOWNLOAD_SETTING": item.get("download_setting"),
                    "RECOGNIZATION": item.get("recognization"),
                    "OVER_EDITION": int(item.get("over_edition")) if str(item.get("over_edition")).isdigit() else 0,
                    "SITES": json.dumps({"search_sites": (item.get("sites") or {}).get("search_sites", [])}),
                    "FILTER_ARGS": json.dumps(item.get("filter_args")),
                    "NOTE": ""
                }
            )
        else:
            self._db.insert(CONFIGUSERRSS(
                NAME=item.get("name"),
                ADDRESS=item.get("address"),
                PARSER=item.get("parser"),
                INTERVAL=item.get("interval"),
                USES=item.get("uses"),
                INCLUDE=item.get("include"),
                EXCLUDE=item.get("exclude"),
                FILTER=item.get("filter_rule"),
                UPDATE_TIME=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time())),
                STATE=item.get("state"),
                SAVE_PATH=item.get("save_path"),
                DOWNLOAD_SETTING=item.get("download_setting"),
                RECOGNIZATION=item.get("recognization"),
                OVER_EDITION=item.get("over_edition"),
                SITES=json.dumps({"search_sites": (item.get("sites") or {}).get("search_sites", [])}),
                FILTER_ARGS=json.dumps(item.get("filter_args")),
                PROCESS_COUNT='0'
            ))

    @DbPersist(_db)
    def insert_userrss_mediainfos(self, tid=None, mediainfo=None):
        if not tid or not mediainfo:
            return
        taskinfo = self._db.query(CONFIGUSERRSS).filter(CONFIGUSERRSS.ID == int(tid)).all()
        if not taskinfo:
            return
        mediainfos = json.loads(taskinfo[0].MEDIAINFOS) if taskinfo[0].MEDIAINFOS else []
        tmdbid = str(mediainfo.tmdb_id)
        season = int(mediainfo.get_season_seq())
        for media in mediainfos:
            if media.get("id") == tmdbid and media.get("season") == season:
                return
        mediainfos.append({
            "id": tmdbid,
            "rssid": "",
            "season": season,
            "name": mediainfo.title
        })
        self._db.query(CONFIGUSERRSS).filter(CONFIGUSERRSS.ID == int(tid)).update(
            {
                "MEDIAINFOS": json.dumps(mediainfos)
            })

    def get_userrss_parser(self, pid=None):
        if pid:
            return self._db.query(CONFIGRSSPARSER).filter(CONFIGRSSPARSER.ID == int(pid)).first()
        else:
            return self._db.query(CONFIGRSSPARSER).all()

    @DbPersist(_db)
    def delete_userrss_parser(self, pid):
        if not pid:
            return
        self._db.query(CONFIGRSSPARSER).filter(CONFIGRSSPARSER.ID == int(pid)).delete()

    @DbPersist(_db)
    def update_userrss_parser(self, item):
        if not item:
            return
        if item.get("id") and self.get_userrss_parser(item.get("id")):
            self._db.query(CONFIGRSSPARSER).filter(CONFIGRSSPARSER.ID == int(item.get("id"))).update(
                {
                    "NAME": item.get("name"),
                    "TYPE": item.get("type"),
                    "FORMAT": item.get("format"),
                    "PARAMS": item.get("params")
                }
            )
        else:
            self._db.insert(CONFIGRSSPARSER(
                NAME=item.get("name"),
                TYPE=item.get("type"),
                FORMAT=item.get("format"),
                PARAMS=item.get("params")
            ))

    @DbPersist(_db)
    def excute(self, sql):
        return self._db.excute(sql)

    @DbPersist(_db)
    def drop_table(self, table_name):
        return self._db.excute(f"""DROP TABLE IF EXISTS {table_name}""")

    @DbPersist(_db)
    def insert_userrss_task_history(self, task_id, title, downloader):
        """
        增加自定义RSS订阅任务的下载记录
        """
        self._db.insert(USERRSSTASKHISTORY(
            TASK_ID=task_id,
            TITLE=title,
            DOWNLOADER=downloader,
            DATE=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
        ))

    def get_userrss_task_history(self, task_id):
        """
        查询自定义RSS订阅任务的下载记录
        """
        if not task_id:
            return []
        return self._db.query(USERRSSTASKHISTORY).filter(USERRSSTASKHISTORY.TASK_ID == task_id) \
            .order_by(USERRSSTASKHISTORY.DATE.desc()).all()

    def get_rss_history(self, rtype=None, rid=None, page=1, page_size=30):
        """
        查询RSS历史（支持分页）
        """
        query = self._db.query(RSSHISTORY)
        if rid:
            return query.filter(RSSHISTORY.ID == int(rid)).all(), 1
        if rtype:
            query = query.filter(RSSHISTORY.TYPE == rtype)
        total = query.count()
        records = query.order_by(RSSHISTORY.FINISH_TIME.desc()) \
            .offset((int(page) - 1) * int(page_size)).limit(int(page_size)).all()
        return records, total

    def is_exists_rss_history(self, rssid):
        """
        判断RSS历史是否存在
        """
        if not rssid:
            return False
        count = self._db.query(RSSHISTORY).filter(RSSHISTORY.RSSID == rssid).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_rss_history(self, rssid, rtype, name, year, tmdbid, image, desc, season=None, total=None, start=None):
        """
        登记RSS历史
        """
        if not self.is_exists_rss_history(rssid):
            self._db.insert(RSSHISTORY(
                TYPE=rtype,
                RSSID=rssid,
                NAME=name,
                YEAR=year,
                TMDBID=tmdbid,
                SEASON=season,
                IMAGE=image,
                DESC=desc,
                TOTAL=total,
                START=start,
                FINISH_TIME=time.strftime('%Y-%m-%d %H:%M:%S',
                                          time.localtime(time.time()))
            ))

    @DbPersist(_db)
    def delete_rss_history(self, rssid):
        """
        删除RSS历史
        """
        if not rssid:
            return
        self._db.query(RSSHISTORY).filter(RSSHISTORY.ID == int(rssid)).delete()

    @DbPersist(_db)
    def insert_custom_word(self, replaced, replace, front, back, offset, wtype, gid, season, enabled, regex, whelp,
                           note=None):
        """
        增加自定义识别词
        """
        self._db.insert(CUSTOMWORDS(
            REPLACED=replaced,
            REPLACE=replace,
            FRONT=front,
            BACK=back,
            OFFSET=offset,
            TYPE=int(wtype),
            GROUP_ID=int(gid),
            SEASON=int(season),
            ENABLED=int(enabled),
            REGEX=int(regex),
            HELP=whelp,
            NOTE=note
        ))

    @DbPersist(_db)
    def delete_custom_word(self, wid):
        """
        删除自定义识别词
        """
        self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.ID == int(wid)).delete()

    @DbPersist(_db)
    def check_custom_word(self, wid, enabled):
        """
        设置自定义识别词状态
        """
        self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.ID == int(wid)).update(
            {
                "ENABLED": int(enabled)
            }
        )

    def get_custom_words(self, wid=None, gid=None, enabled=None, wtype=None, regex=None):
        """
        查询自定义识别词
        """
        if wid:
            return self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.ID == int(wid)) \
                .order_by(CUSTOMWORDS.GROUP_ID).all()
        elif gid:
            return self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.GROUP_ID == int(gid)) \
                .order_by(CUSTOMWORDS.GROUP_ID).all()
        elif wtype and enabled is not None and regex is not None:
            return self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.ENABLED == int(enabled),
                                                      CUSTOMWORDS.TYPE == int(wtype),
                                                      CUSTOMWORDS.REGEX == int(regex)) \
                .order_by(CUSTOMWORDS.GROUP_ID).all()
        return self._db.query(CUSTOMWORDS).all().order_by(CUSTOMWORDS.GROUP_ID)

    def is_custom_words_existed(self, replaced=None, front=None, back=None):
        """
        查询自定义识别词
        """
        if replaced:
            count = self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.REPLACED == replaced).count()
        elif front and back:
            count = self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.FRONT == front,
                                                       CUSTOMWORDS.BACK == back).count()
        else:
            return False
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_custom_word_groups(self, title, year, gtype, tmdbid, season_count, note=None):
        """
        增加自定义识别词组
        """
        self._db.insert(CUSTOMWORDGROUPS(
            TITLE=title,
            YEAR=year,
            TYPE=int(gtype),
            TMDBID=int(tmdbid),
            SEASON_COUNT=int(season_count),
            NOTE=note
        ))

    @DbPersist(_db)
    def delete_custom_word_group(self, gid):
        """
        删除自定义识别词组
        """
        if not gid:
            return
        self._db.query(CUSTOMWORDS).filter(CUSTOMWORDS.GROUP_ID == int(gid)).delete()
        self._db.query(CUSTOMWORDGROUPS).filter(CUSTOMWORDGROUPS.ID == int(gid)).delete()

    def get_custom_word_groups(self, gid=None, tmdbid=None, gtype=None):
        """
        查询自定义识别词组
        """
        if gid:
            return self._db.query(CUSTOMWORDGROUPS).filter(CUSTOMWORDGROUPS.ID == int(gid)).all()
        if tmdbid and gtype:
            return self._db.query(CUSTOMWORDGROUPS).filter(CUSTOMWORDGROUPS.TMDBID == int(tmdbid),
                                                           CUSTOMWORDGROUPS.TYPE == int(gtype)).all()
        return self._db.query(CUSTOMWORDGROUPS).all()

    def is_custom_word_group_existed(self, tmdbid=None, gtype=None):
        """
        查询自定义识别词组
        """
        if not gtype or not tmdbid:
            return False
        count = self._db.query(CUSTOMWORDGROUPS).filter(CUSTOMWORDGROUPS.TMDBID == int(tmdbid),
                                                        CUSTOMWORDGROUPS.TYPE == int(gtype)).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db)
    def insert_config_sync_path(self, source, dest, unknown, mode, rename, enabled, note=None):
        """
        增加目录同步
        """
        return self._db.insert(CONFIGSYNCPATHS(
            SOURCE=source,
            DEST=dest,
            UNKNOWN=unknown,
            MODE=mode,
            RENAME=int(rename),
            ENABLED=int(enabled),
            NOTE=note
        ))

    @DbPersist(_db)
    def delete_config_sync_path(self, sid):
        """
        删除目录同步
        """
        if not sid:
            return
        self._db.query(CONFIGSYNCPATHS).filter(CONFIGSYNCPATHS.ID == int(sid)).delete()

    def get_config_sync_paths(self, sid=None):
        """
        查询目录同步
        """
        if sid:
            return self._db.query(CONFIGSYNCPATHS).filter(CONFIGSYNCPATHS.ID == int(sid)).all()
        return self._db.query(CONFIGSYNCPATHS).all()

    @DbPersist(_db)
    def check_config_sync_paths(self, sid=None, source=None, rename=None, enabled=None):
        """
        设置目录同步状态
        """
        if sid and rename is not None:
            self._db.query(CONFIGSYNCPATHS).filter(CONFIGSYNCPATHS.ID == int(sid)).update(
                {
                    "RENAME": int(rename)
                }
            )
        elif sid and enabled is not None:
            self._db.query(CONFIGSYNCPATHS).filter(CONFIGSYNCPATHS.ID == int(sid)).update(
                {
                    "ENABLED": int(enabled)
                }
            )
        elif source and enabled is not None:
            self._db.query(CONFIGSYNCPATHS).filter(CONFIGSYNCPATHS.SOURCE == source).update(
                {
                    "ENABLED": int(enabled)
                }
            )

    @DbPersist(_db)
    def delete_download_setting(self, sid):
        """
        删除下载设置
        """
        if not sid:
            return
        self._db.query(DOWNLOADSETTING).filter(DOWNLOADSETTING.ID == int(sid)).delete()

    def get_download_setting(self, sid=None):
        """
        查询下载设置
        """
        if sid:
            return self._db.query(DOWNLOADSETTING).filter(DOWNLOADSETTING.ID == int(sid)).all()
        return self._db.query(DOWNLOADSETTING).all()

    @DbPersist(_db)
    def update_download_setting(self,
                                sid,
                                name,
                                category,
                                tags,
                                content_layout,
                                is_paused,
                                upload_limit,
                                download_limit,
                                ratio_limit,
                                seeding_time_limit,
                                downloader):
        """
        设置下载设置
        """
        if sid:
            self._db.query(DOWNLOADSETTING).filter(DOWNLOADSETTING.ID == int(sid)).update(
                {
                    "NAME": name,
                    "CATEGORY": category,
                    "TAGS": tags,
                    "CONTENT_LAYOUT": int(content_layout),
                    "IS_PAUSED": int(is_paused),
                    "UPLOAD_LIMIT": int(float(upload_limit)),
                    "DOWNLOAD_LIMIT": int(float(download_limit)),
                    "RATIO_LIMIT": int(round(float(ratio_limit), 2) * 100),
                    "SEEDING_TIME_LIMIT": int(float(seeding_time_limit)),
                    "DOWNLOADER": downloader
                }
            )
        else:
            self._db.insert(DOWNLOADSETTING(
                NAME=name,
                CATEGORY=category,
                TAGS=tags,
                CONTENT_LAYOUT=int(content_layout),
                IS_PAUSED=int(is_paused),
                UPLOAD_LIMIT=int(float(upload_limit)),
                DOWNLOAD_LIMIT=int(float(download_limit)),
                RATIO_LIMIT=int(round(float(ratio_limit), 2) * 100),
                SEEDING_TIME_LIMIT=int(float(seeding_time_limit)),
                DOWNLOADER=downloader
            ))

    @DbPersist(_db)
    def delete_message_client(self, cid):
        """
        删除消息服务器
        """
        if not cid:
            return
        self._db.query(MESSAGECLIENT).filter(MESSAGECLIENT.ID == int(cid)).delete()

    def get_message_client(self, cid=None):
        """
        查询消息服务器
        """
        if cid:
            return self._db.query(MESSAGECLIENT).filter(MESSAGECLIENT.ID == int(cid)).all()
        return self._db.query(MESSAGECLIENT).order_by(MESSAGECLIENT.TYPE).all()

    @DbPersist(_db)
    def remove_message_client_switch(self, switch_name):
        """从已保存的消息客户端中移除废弃的推送开关。"""
        changed = 0
        for client in self._db.query(MESSAGECLIENT).all():
            try:
                switchs = json.loads(client.SWITCHS) if client.SWITCHS else []
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(switchs, list) and switch_name in switchs:
                client.SWITCHS = json.dumps(
                    [item for item in switchs if item != switch_name], ensure_ascii=False)
                changed += 1
        return changed

    @DbPersist(_db)
    def insert_message_client(self,
                              name,
                              ctype,
                              config,
                              switchs: list,
                              interactive,
                              enabled,
                              note=''):
        """
        设置消息服务器
        """
        self._db.insert(MESSAGECLIENT(
            NAME=name,
            TYPE=ctype,
            CONFIG=config,
            SWITCHS=json.dumps(switchs),
            INTERACTIVE=int(interactive),
            ENABLED=int(enabled),
            NOTE=note
        ))

    @DbPersist(_db)
    def check_message_client(self, cid=None, interactive=None, enabled=None, ctype=None):
        """
        设置目录同步状态
        """
        if cid and interactive is not None:
            self._db.query(MESSAGECLIENT).filter(MESSAGECLIENT.ID == int(cid)).update(
                {
                    "INTERACTIVE": int(interactive)
                }
            )
        elif cid and enabled is not None:
            self._db.query(MESSAGECLIENT).filter(MESSAGECLIENT.ID == int(cid)).update(
                {
                    "ENABLED": int(enabled)
                }
            )
        elif not cid and int(interactive) == 0 and ctype:
            self._db.query(MESSAGECLIENT).filter(MESSAGECLIENT.INTERACTIVE == 1,
                                                 MESSAGECLIENT.TYPE == ctype).update(
                {
                    "INTERACTIVE": 0
                }
            )

    @DbPersist(_db)
    def delete_torrent_remove_task(self, tid):
        """
        删除自动删种策略
        """
        if not tid:
            return
        self._db.query(TORRENTREMOVETASK).filter(TORRENTREMOVETASK.ID == int(tid)).delete()

    def get_torrent_remove_tasks(self, tid=None):
        """
        查询自动删种策略
        """
        if tid:
            return self._db.query(TORRENTREMOVETASK).filter(TORRENTREMOVETASK.ID == int(tid)).all()
        return self._db.query(TORRENTREMOVETASK).order_by(TORRENTREMOVETASK.NAME).all()

    @DbPersist(_db)
    def insert_torrent_remove_task(self,
                                   name,
                                   action,
                                   interval,
                                   enabled,
                                   samedata,
                                   onlynastool,
                                   downloader,
                                   config: dict,
                                   note=None):
        """
        设置自动删种策略
        """
        self._db.insert(TORRENTREMOVETASK(
            NAME=name,
            ACTION=int(action),
            INTERVAL=int(interval),
            ENABLED=int(enabled),
            SAMEDATA=int(samedata),
            ONLYNASTOOL=int(onlynastool),
            DOWNLOADER=downloader,
            CONFIG=json.dumps(config),
            NOTE=note
        ))

    @DbPersist(_db)
    def delete_douban_history(self, hid):
        """
        删除豆瓣同步记录
        """
        if not hid:
            return
        self._db.query(DOUBANMEDIAS).filter(DOUBANMEDIAS.ID == int(hid)).delete()

    def get_douban_history(self):
        """
        查询豆瓣同步记录
        """
        return self._db.query(DOUBANMEDIAS).order_by(DOUBANMEDIAS.ADD_TIME.desc()).all()
