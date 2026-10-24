import calendar
import datetime
import os
import os.path
import tempfile
import time
import json
import uuid
from enum import Enum
from sqlalchemy import func, or_, and_, case

import log
from app.db import MainDb, DbPersist
from app.db.models import *
from app.utils import StringUtils
from app.utils.types import MediaType, RmtMode


DEFAULT_HISTORY_LIMITS = {
    "transfer_history": 200,
    "subscription_history": 100,
    "download_history": 100,
    "recognition": 200,
}


def history_record_limit(value):
    """记录上限必须为正整数，不能将小数或布尔值静默转换为条数。"""
    if not str(value).strip().isdigit() or int(value) < 1:
        raise ValueError("记录上限必须为大于 0 的整数")
    return int(value)


class DbHelper:
    _db = MainDb()

    @staticmethod
    def _identifier_string(value):
        return None if value is None else str(value)

    @staticmethod
    def _optional_integer(value):
        if value is None or not str(value).strip():
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _history_retention_cutoff(retention_value, retention_unit, now=None):
        """计算历史数据保留期限的字符串时间边界。"""
        try:
            retention_value = int(retention_value)
        except (TypeError, ValueError) as error:
            raise ValueError("历史记录保留时间无效") from error
        if retention_value < 1:
            raise ValueError("历史记录保留时间必须大于 0")
        if retention_unit == "day":
            if retention_value > 36500:
                raise ValueError("历史记录保留天数不能超过 36500 天")
            cutoff = (now or datetime.datetime.now()) - datetime.timedelta(days=retention_value)
        elif retention_unit == "month":
            if retention_value > 1200:
                raise ValueError("历史记录保留月数不能超过 1200 个月")
            now = now or datetime.datetime.now()
            month_index = now.year * 12 + now.month - 1 - retention_value
            year, month_index = divmod(month_index, 12)
            month = month_index + 1
            day = min(now.day, calendar.monthrange(year, month)[1])
            cutoff = now.replace(year=year, month=month, day=day)
        else:
            raise ValueError("历史记录保留单位无效")
        return cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")

    def _delete_expired_history(self, model, date_column, retention_value, retention_unit,
                                now=None, batch_size=500, max_records=200):
        """按保留期限和条数分批清理历史，优先保留时间最新的记录。"""
        older_than = self._history_retention_cutoff(retention_value, retention_unit, now=now)
        max_records = history_record_limit(max_records)
        batch_size = min(max(int(batch_size or 500), 1), 2000)
        label = {"TRANSFER_HISTORY": "转移历史", "RSS_HISTORY": "订阅历史",
                 "DOWNLOAD_HISTORY": "最近下载"}.get(model.__tablename__, model.__tablename__)
        log.info("【历史清理】开始清理%s，保留 %s %s，最多保留 %s 条，清理 %s 之前的记录" % (
            label, retention_value, "天" if retention_unit == "day" else "个月", max_records, older_than))
        deleted_count = 0
        last_id = 0
        while True:
            record_ids = [row[0] for row in self._db.query(model.ID).filter(
                date_column < older_than, model.ID > last_id
            ).order_by(model.ID.asc()).limit(batch_size).all()]
            if not record_ids:
                break
            last_id = record_ids[-1]
            batch_deleted = self._db.query(model).filter(
                model.ID.in_(record_ids)).delete(synchronize_session=False)
            self._db.commit()
            deleted_count += batch_deleted
            log.info("【历史清理】%s本批次删除 %s 条，累计删除 %s 条过期记录" % (
                label, batch_deleted, deleted_count))
        overflow_count = 0
        while True:
            # NULL 日期排在最后，日期相同时用主键稳定排序。
            record_ids = [row[0] for row in self._db.query(model.ID).order_by(
                case((date_column.is_(None), 1), else_=0), date_column.desc(), model.ID.desc()
            ).offset(max_records).limit(batch_size).all()]
            if not record_ids:
                break
            batch_deleted = self._db.query(model).filter(
                model.ID.in_(record_ids)).delete(synchronize_session=False)
            self._db.commit()
            overflow_count += batch_deleted
            log.info("【历史清理】%s本批次删除 %s 条超出上限记录，累计删除 %s 条超出上限记录" % (
                label, batch_deleted, overflow_count))
        log.info("【历史清理】%s清理完成，共删除 %s 条记录（过期 %s 条，超出上限 %s 条），记录上限 %s 条" % (
            label, deleted_count + overflow_count, deleted_count, overflow_count, max_records))
        return deleted_count + overflow_count

    @staticmethod
    def _search_task_query(keyword):
        digest = portable_key_digest((keyword,))
        return DbHelper._db.query(SEARCHTASK).filter(
            SEARCHTASK.KEYWORD_HASH == digest, SEARCHTASK.KEYWORD == keyword)

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
        return json.dumps({} if value is None else value, ensure_ascii=False,
                          default=cls._json_default)

    @staticmethod
    def recognition_summary(payload):
        from app.media.recognition.compatibility import summary
        return summary(payload)

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
                    YEAR=self._identifier_string(media_item.year) if ident_flag else '',
                    SEASON=media_item.get_season_string() if ident_flag else '',
                    EPISODE=media_item.get_episode_string() if ident_flag else '',
                    ES_STRING=media_item.get_season_episode_string() if ident_flag else '',
                    VOTE=media_item.vote_average or "0",
                    IMAGE=media_item.get_backdrop_image(default=False, original=True),
                    POSTER=media_item.get_poster_image(),
                    TMDBID=self._identifier_string(media_item.tmdb_id),
                    OVERVIEW=media_item.overview,
                    RES_TYPE=json.dumps({
                        "respix": media_item.resource_pix,
                        "restype": media_item.resource_type,
                        "reseffect": media_item.resource_effect,
                        "video_encode": media_item.video_encode
                    }),
                    RES_ORDER=media_item.res_order,
                    SIZE=StringUtils.str_filesize(int(media_item.size)),
                    SEEDERS=self._optional_integer(media_item.seeders),
                    PEERS=self._optional_integer(media_item.peers),
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
        task = self._search_task_query(keyword).first()
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
        return self._search_task_query(keyword).first()

    def get_search_tasks(self, limit=20):
        """
        查询所有搜索任务，按开始时间倒序
        """
        return self._db.query(SEARCHTASK).order_by(SEARCHTASK.ID.desc()).limit(limit).all()

    def _recognition_history(self):
        from app.media.recognition.compatibility import LegacyHistory
        from app.media.recognition.repository import RecognitionRepository
        return LegacyHistory(RecognitionRepository(self._db.database_manager))

    def insert_ai_recognition_record(self, title, anitopy_result, ai_result,
                                     anitopy_tmdb, ai_tmdb, status):
        identity = uuid.uuid4().hex
        self.insert_recognition_record({"request_id": identity, "original_name": title,
            "source": "ai_compatibility", "stage": "resolve", "actions": [],
            "provider_results": [{"provider_id": "local_rules", "normalized_result": anitopy_result},
                {"provider_id": "anitopy_ml", "normalized_result": ai_result}],
            "tmdb_results": [{"provider_id": "local_rules", "result": anitopy_tmdb},
                {"provider_id": "anitopy_ml", "result": ai_tmdb}], "overall_result": {"status": status}})
        return identity

    def get_ai_recognition_records(self, title=None, page=1, page_size=20,
                                   before_id=None, snapshot_id=None, include_total=True, actor="0"):
        from types import SimpleNamespace
        history = self._recognition_history()
        total, rows = history.listing(title=title, source="ai_compatibility", page=page, page_size=page_size,
            include_total=include_total, actor=actor,
            before_cursor=before_id if isinstance(before_id, tuple) else None)
        result = []
        for row in rows:
            detail, _ = history.detail(row.REQUEST_ID, actor)
            if detail is None:
                continue
            providers = {item.get("provider_id"): item.get("normalized_result") for item in json.loads(detail.PROVIDER_RESULTS)}
            tmdb = {item.get("provider_id"): item.get("result") for item in json.loads(detail.TMDB_RESULTS)}
            result.append(SimpleNamespace(ID=row.REQUEST_ID, CURSOR=(row.CREATED_AT,row.REQUEST_ID), TITLE=row.ORIGINAL_NAME,
                STATUS=json.loads(detail.OVERALL_RESULT).get("status"), ADD_TIME=row.CREATED_AT,
                ANITOPY_RESULT=self._json_dumps(providers.get("local_rules")), AI_RESULT=self._json_dumps(providers.get("anitopy_ml")),
                ANITOPY_TMDB=self._json_dumps(tmdb.get("local_rules")), AI_TMDB=self._json_dumps(tmdb.get("anitopy_ml"))))
        return total, result

    def insert_recognition_record(self, payload):
        return self._recognition_history().insert(payload)

    def append_recognition_business_action(self, request_id, action_type, reason,
                                           details=None, business_status="skipped"):
        return self._recognition_history().append(str(request_id), action_type, reason, details, business_status)

    def get_recognition_records(self, **filters):
        return self._recognition_history().listing(**filters)

    def get_recognition_record(self, request_id, actor="0"):
        return self._recognition_history().detail(str(request_id), actor)

    @DbPersist(_db)
    def delete_expired_transfer_history(self, retention_value, retention_unit="day",
                                        now=None, batch_size=500, max_records=200):
        """清理过期或超出条数上限的文件转移历史。"""
        return self._delete_expired_history(
            TRANSFERHISTORY, TRANSFERHISTORY.DATE, retention_value,
            retention_unit, now=now, batch_size=batch_size, max_records=max_records)

    @DbPersist(_db)
    def delete_expired_subscription_history(self, retention_value, retention_unit="day",
                                            now=None, batch_size=500, max_records=100):
        """清理过期或超出条数上限的订阅历史。"""
        return self._delete_expired_history(
            RSSHISTORY, RSSHISTORY.FINISH_TIME, retention_value,
            retention_unit, now=now, batch_size=batch_size, max_records=max_records)

    @DbPersist(_db)
    def delete_expired_download_history(self, retention_value, retention_unit="day",
                                        now=None, batch_size=500, max_records=100):
        """清理过期或超出条数上限的最近下载记录。"""
        return self._delete_expired_history(
            DOWNLOADHISTORY, DOWNLOADHISTORY.DATE, retention_value,
            retention_unit, now=now, batch_size=batch_size, max_records=max_records)

    def delete_expired_recognition_records(self, retention_days=None, now=None, batch_size=500,
                                           retention_unit="day", retention_value=None, max_records=200):
        from app.media.recognition.cleanup import CleanupService, CleanupBusy
        try:
            return CleanupService(self._recognition_history().repository,
                batch_size=min(max(int(batch_size or 500), 1), 2000)).expired(
                retention_value=int(retention_value if retention_value is not None else retention_days or 30),
                retention_unit=retention_unit, max_records=history_record_limit(max_records), now=now)
        except CleanupBusy:
            return 0

    def archive_recognition_records(self, older_than, archive_path, delete_archived=False, batch_size=500):
        return self._recognition_history().archive(older_than, archive_path, delete_archived, batch_size)

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
        self._search_task_query(keyword).delete()

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
            "YEAR": self._identifier_string(media_info.year),
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
                "YEAR": self._identifier_string(media_info.year),
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
            # 已保存的种子标题可能由订阅源标题规范化或识别得到。
            # 如果存在下载链接，则应以它作为稳定标识，并据此清除已完成标记。
            self._db.query(RSSTORRENTS).filter(RSSTORRENTS.ENCLOSURE == enclosure).delete()
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
                    YEAR=str(media.year) if media.year is not None else None,
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
                                            DOUBANMEDIAS.YEAR == str(media.year)).update(
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
                YEAR=self._identifier_string(media_info.year),
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
            matches = ((TRANSFERHISTORY.SOURCE_FILENAME.ilike(search))
                       | (TRANSFERHISTORY.TITLE.ilike(search)))
            count = self._db.query(TRANSFERHISTORY).filter(matches).count()
            data = self._db.query(TRANSFERHISTORY).filter(matches).order_by(
                case((TRANSFERHISTORY.DATE.is_(None), 1), else_=0).asc(),
                TRANSFERHISTORY.DATE.desc(), TRANSFERHISTORY.ID.desc()
            ).limit(int(rownum)).offset(begin_pos).all()
            # 页面随后可能逐条请求 TMDB 海报；释放只读事务，避免网络等待占住 SQLite 快照。
            self._db.commit()
            return count, data
        else:
            count = self._db.query(TRANSFERHISTORY).count()
            data = self._db.query(TRANSFERHISTORY).order_by(
                case((TRANSFERHISTORY.DATE.is_(None), 1), else_=0).asc(),
                TRANSFERHISTORY.DATE.desc(), TRANSFERHISTORY.ID.desc()
            ).limit(int(rownum)).offset(begin_pos).all()
            self._db.commit()
            return count, data

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
        query = self._db.query(CONFIGFILTERRULES)
        if groupid:
            query = query.filter(CONFIGFILTERRULES.GROUP_ID == str(groupid))
        rules = query.all()

        def priority_key(value):
            normalized = str(value or "").strip()
            if not normalized:
                return 0, 0
            try:
                return 0, int(normalized)
            except (TypeError, ValueError):
                return 1, normalized.casefold()

        return sorted(rules, key=lambda rule: (
            str(rule.GROUP_ID or ""), priority_key(rule.PRIORITY), rule.ID))

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

    @DbPersist(_db, retry_safe=True)
    def update_rss_movie_tmdb(self, rid, tmdbid, title, year, image, desc, note):
        """
        更新订阅电影的部分信息
        """
        if not tmdbid:
            return
        self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rid)).update({
            "TMDBID": self._identifier_string(tmdbid),
            "NAME": title,
            "YEAR": str(year) if year is not None else None,
            "IMAGE": image,
            "NOTE": note,
            "DESC": desc
        })

    @DbPersist(_db, retry_safe=True)
    def update_rss_movie_desc(self, rid, desc):
        """
        更新订阅电影的DESC
        """
        self._db.query(RSSMOVIES).filter(RSSMOVIES.ID == int(rid)).update({
            "DESC": desc
        })

    @DbPersist(_db, retry_safe=True)
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

    @DbPersist(_db, retry_safe=True)
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
            YEAR=str(media_info.year) if media_info.year is not None else None,
            TMDBID=self._identifier_string(media_info.tmdb_id),
            IMAGE=media_info.get_message_image(),
            SEARCH_SITES=json.dumps(search_sites),
            OVER_EDITION=self._optional_integer(over_edition) or 0,
            FILTER_RESTYPE=filter_restype,
            FILTER_PIX=filter_pix,
            FILTER_RULE=self._optional_integer(filter_rule),
            FILTER_TEAM=filter_team,
            SAVE_PATH=save_path,
            DOWNLOAD_SETTING=self._optional_integer(download_setting),
            FUZZY_MATCH=self._optional_integer(fuzzy_match) or 0,
            STATE=state,
            DESC=desc,
            NOTE=note,
            KEYWORD=keyword
        ))
        return 0

    @DbPersist(_db, retry_safe=True)
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
                self._db.query(RSSMOVIES).filter(
                    RSSMOVIES.TMDBID == self._identifier_string(tmdbid)).delete()
            self._db.query(RSSMOVIES).filter(RSSMOVIES.NAME == title,
                                             RSSMOVIES.YEAR == str(year)).delete()

    @DbPersist(_db, retry_safe=True)
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
                ret = self._db.query(RSSTVS.ID).filter(
                    RSSTVS.TMDBID == self._identifier_string(tmdbid),
                    RSSTVS.SEASON == str(season)).first()
            else:
                ret = self._db.query(RSSTVS.ID).filter(
                    RSSTVS.TMDBID == self._identifier_string(tmdbid)).first()
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

    @DbPersist(_db, retry_safe=True)
    def update_rss_tv_tmdb(self, rid, tmdbid, title, year, total, lack, image, desc, note):
        """
        更新订阅电影的TMDBID
        """
        if not tmdbid:
            return
        self._db.query(RSSTVS).filter(RSSTVS.ID == int(rid)).update(
            {
                "TMDBID": self._identifier_string(tmdbid),
                "NAME": title,
                "YEAR": str(year) if year is not None else None,
                "TOTAL": total,
                "LACK": lack,
                "IMAGE": image,
                "DESC": desc,
                "NOTE": note
            }
        )

    @DbPersist(_db, retry_safe=True)
    def update_rss_tv_desc(self, rid, desc):
        """
        更新订阅电视剧的DESC
        """
        self._db.query(RSSTVS).filter(RSSTVS.ID == int(rid)).update(
            {
                "DESC": desc
            }
        )

    def is_exists_rss_tv(self, title, year, season=None, rssid=None):
        """
        判断RSS电视剧是否存在
        """
        if not title:
            return False
        filters = [RSSTVS.NAME == title, RSSTVS.YEAR == str(year)]
        if season:
            filters.append(RSSTVS.SEASON == str(season))
        if rssid:
            filters.append(RSSTVS.ID != int(rssid))
        return self._db.query(RSSTVS).filter(*filters).count() > 0

    @DbPersist(_db, retry_safe=True)
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
                      keyword=None,
                      rssid=None):
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
        if self.is_exists_rss_tv(media_info.title, media_info.year, season_str, rssid=rssid):
            return 9
        if rssid:
            self.delete_rss_tv(rssid=rssid)
        self._db.insert(RSSTVS(
            NAME=media_info.title,
            YEAR=str(media_info.year) if media_info.year is not None else None,
            SEASON=season_str,
            TMDBID=self._identifier_string(media_info.tmdb_id),
            IMAGE=media_info.get_message_image(),
            SEARCH_SITES=json.dumps(search_sites),
            OVER_EDITION=self._optional_integer(over_edition) or 0,
            FILTER_RESTYPE=filter_restype,
            FILTER_PIX=filter_pix,
            FILTER_RULE=self._optional_integer(filter_rule),
            FILTER_TEAM=filter_team,
            SAVE_PATH=save_path,
            DOWNLOAD_SETTING=self._optional_integer(download_setting),
            FUZZY_MATCH=self._optional_integer(fuzzy_match) or 0,
            TOTAL_EP=self._optional_integer(total_ep),
            CURRENT_EP=self._optional_integer(current_ep),
            TOTAL=self._optional_integer(total),
            LACK=self._optional_integer(lack),
            STATE=state,
            DESC=desc,
            NOTE=note,
            KEYWORD=keyword
        ))
        return 0

    @DbPersist(_db, retry_safe=True)
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
                                          RSSTVS.SEASON == str(season)).update(
                {
                    "LACK": lack
                }
            )

    @DbPersist(_db, retry_safe=True)
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
        count = self._db.query(RSSTVEPISODES).filter(
            RSSTVEPISODES.RSSID == self._identifier_string(rid)).count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db, retry_safe=True)
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
            self._db.query(RSSTVEPISODES).filter(
                RSSTVEPISODES.RSSID == self._identifier_string(rid)).update(
                {
                    "EPISODES": ",".join(episodes)
                }
            )
        else:
            self._db.insert(RSSTVEPISODES(
                RSSID=self._identifier_string(rid),
                EPISODES=",".join(episodes)
            ))

    def get_rss_tv_episodes(self, rid):
        """
        查询电视剧订阅缺失剧集
        """
        if not rid:
            return []
        ret = self._db.query(RSSTVEPISODES.EPISODES).filter(
            RSSTVEPISODES.RSSID == self._identifier_string(rid)).first()
        if ret:
            return [int(epi) for epi in str(ret[0]).split(',')]
        else:
            return None

    @DbPersist(_db, retry_safe=True)
    def delete_rss_tv_episodes(self, rid):
        """
        删除电视剧订阅缺失剧集
        """
        if not rid:
            return
        self._db.query(RSSTVEPISODES).filter(
            RSSTVEPISODES.RSSID == self._identifier_string(rid)).delete()

    @DbPersist(_db, retry_safe=True)
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
                                          RSSTVS.SEASON == str(season)).update(
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

    def _build_transfer_statistics_query(self, begin_date):
        day = func.substr(TRANSFERHISTORY.DATE, 1, 10).label("day")
        return self._db.query(TRANSFERHISTORY.TYPE, day, func.count()).filter(
            TRANSFERHISTORY.DATE > begin_date
        ).group_by(TRANSFERHISTORY.TYPE, day).order_by(day, TRANSFERHISTORY.TYPE)

    def get_transfer_statistics(self, days=30):
        """
        查询历史记录统计
        """
        begin_date = (datetime.datetime.now() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        return self._build_transfer_statistics_query(begin_date).all()

    def is_exists_download_history(self, title, tmdbid, mtype=None):
        """
        查询下载历史是否存在
        """
        if not title or not tmdbid:
            return False
        tmdbid = self._identifier_string(tmdbid)
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
                                                   DOWNLOADHISTORY.TMDBID == self._identifier_string(
                                                       media_info.tmdb_id),
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
                YEAR=self._identifier_string(media_info.year),
                TYPE=media_info.type.value,
                TMDBID=self._identifier_string(media_info.tmdb_id),
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
                DOWNLOADHISTORY.DATE > date).order_by(
                    case((DOWNLOADHISTORY.DATE.is_(None), 1), else_=0).asc(),
                    DOWNLOADHISTORY.DATE.desc(), DOWNLOADHISTORY.ID.desc()).all()
        else:
            offset = (int(page) - 1) * int(num)
            return self._db.query(DOWNLOADHISTORY).order_by(
                case((DOWNLOADHISTORY.DATE.is_(None), 1), else_=0).asc(),
                DOWNLOADHISTORY.DATE.desc(), DOWNLOADHISTORY.ID.desc()
            ).limit(num).offset(offset).all()

    def get_download_history_page(self, page_size=30, cursor=None):
        """Read a stable keyset page without counting the full download history."""
        try:
            page_size = int(page_size)
        except (TypeError, ValueError) as error:
            raise ValueError("下载历史分页大小无效") from error
        if not 1 <= page_size <= 200:
            raise ValueError("下载历史分页大小必须介于 1 和 200 之间")

        query = self._db.query(DOWNLOADHISTORY)
        if cursor is not None:
            if not isinstance(cursor, dict) or set(cursor) != {"date", "id"}:
                raise ValueError("下载历史游标格式无效")
            cursor_date = cursor.get("date")
            cursor_id = cursor.get("id")
            if cursor_date is not None and not isinstance(cursor_date, str):
                raise ValueError("下载历史游标日期无效")
            if type(cursor_id) is not int or cursor_id < 1:
                raise ValueError("下载历史游标 ID 无效")
            if cursor_date is None:
                query = query.filter(DOWNLOADHISTORY.DATE.is_(None),
                                     DOWNLOADHISTORY.ID < cursor_id)
            else:
                query = query.filter(or_(
                    DOWNLOADHISTORY.DATE < cursor_date,
                    and_(DOWNLOADHISTORY.DATE == cursor_date,
                         DOWNLOADHISTORY.ID < cursor_id),
                    DOWNLOADHISTORY.DATE.is_(None),
                ))

        records = query.order_by(
            case((DOWNLOADHISTORY.DATE.is_(None), 1), else_=0).asc(),
            DOWNLOADHISTORY.DATE.desc(), DOWNLOADHISTORY.ID.desc()
        ).limit(page_size + 1).all()
        has_more = len(records) > page_size
        records = records[:page_size]
        next_cursor = None
        if has_more and records:
            next_cursor = {"date": records[-1].DATE, "id": int(records[-1].ID)}
        return records, next_cursor

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
        self._db.query(CONFIGFILTERRULES).filter(
            CONFIGFILTERRULES.GROUP_ID == str(groupid)).delete()
        self._db.query(CONFIGFILTERGROUP).filter(CONFIGFILTERGROUP.ID == int(groupid)).delete()

    def restore_filter_groups(self, groups):
        """Atomically replace selected, server-provided built-in filter templates."""
        if not isinstance(groups, list) or not groups:
            raise ValueError("没有可恢复的内置规则组")
        with self._db.transaction() as session:
            for group in groups:
                group_id = int(group["id"])
                session.query(CONFIGFILTERRULES).filter(
                    CONFIGFILTERRULES.GROUP_ID == str(group_id)).delete(
                        synchronize_session=False)
                session.query(CONFIGFILTERGROUP).filter(
                    CONFIGFILTERGROUP.ID == group_id).delete(synchronize_session=False)
                session.add(CONFIGFILTERGROUP(
                    ID=group_id, GROUP_NAME=str(group["name"]),
                    IS_DEFAULT=group.get("default") or "N", NOTE=group.get("note")))
                rows = []
                for rule in group.get("rules") or []:
                    rows.append(CONFIGFILTERRULES(
                        ID=int(rule["id"]), GROUP_ID=str(group_id),
                        ROLE_NAME=str(rule.get("name") or ""),
                        PRIORITY=str(rule.get("priority") or ""),
                        INCLUDE=rule.get("include"), EXCLUDE=rule.get("exclude"),
                        SIZE_LIMIT=rule.get("size_limit"), NOTE=rule.get("note")))
                session.add_all(rows)
        return True

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
                    "PRIORITY": self._identifier_string(item.get("pri")),
                    "INCLUDE": item.get("include"),
                    "EXCLUDE": item.get("exclude"),
                    "SIZE_LIMIT": item.get("size"),
                    "NOTE": item.get("free")
                }
            )
        else:
            self._db.insert(CONFIGFILTERRULES(
                GROUP_ID=self._identifier_string(item.get("group")),
                ROLE_NAME=item.get("name"),
                PRIORITY=self._identifier_string(item.get("pri")),
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
        download_setting = self._optional_integer(item.get("download_setting"))
        over_edition = self._optional_integer(item.get("over_edition")) or 0
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
                    "DOWNLOAD_SETTING": download_setting,
                    "RECOGNIZATION": item.get("recognization"),
                    "OVER_EDITION": over_edition,
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
                DOWNLOAD_SETTING=download_setting,
                RECOGNIZATION=item.get("recognization"),
                OVER_EDITION=over_edition,
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
        raise RuntimeError("业务数据库 helper 已禁用原始 SQL；请使用受控仓储方法")

    @DbPersist(_db)
    def drop_table(self, table_name):
        raise RuntimeError("业务数据库 helper 不允许动态删除表")

    @DbPersist(_db)
    def insert_userrss_task_history(self, task_id, title, downloader):
        """
        增加自定义RSS订阅任务的下载记录
        """
        self._db.insert(USERRSSTASKHISTORY(
            TASK_ID=self._identifier_string(task_id),
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
        return self._db.query(USERRSSTASKHISTORY).filter(
            USERRSSTASKHISTORY.TASK_ID == self._identifier_string(task_id)) \
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

    def is_exists_rss_history(self, rssid, rtype=None):
        """
        判断指定订阅类型的RSS历史是否存在。
        """
        if not rssid:
            return False
        query = self._db.query(RSSHISTORY).filter(
            RSSHISTORY.RSSID == self._identifier_string(rssid))
        if rtype:
            query = query.filter(RSSHISTORY.TYPE == rtype)
        count = query.count()
        if count > 0:
            return True
        else:
            return False

    @DbPersist(_db, retry_safe=True)
    def insert_rss_history(self, rssid, rtype, name, year, tmdbid, image, desc, season=None, total=None, start=None):
        """
        登记RSS历史
        """
        if not self.is_exists_rss_history(rssid, rtype=rtype):
            self._db.insert(RSSHISTORY(
                TYPE=rtype,
                RSSID=self._identifier_string(rssid),
                NAME=name,
                YEAR=self._identifier_string(year),
                TMDBID=self._identifier_string(tmdbid),
                SEASON=season,
                IMAGE=image,
                DESC=desc,
                TOTAL=self._optional_integer(total),
                START=self._optional_integer(start),
                FINISH_TIME=time.strftime('%Y-%m-%d %H:%M:%S',
                                          time.localtime(time.time()))
            ))

    @DbPersist(_db, retry_safe=True)
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
            YEAR=self._identifier_string(year),
            TYPE=int(gtype),
            TMDBID=int(tmdbid),
            SEASON_COUNT=int(season_count),
            NOTE=note
        ))

    @DbPersist(_db)
    def update_custom_word_group(self, gid, title, year, tmdbid, season_count):
        """更新识别词组的媒体信息，保留组内识别词。"""
        self._db.query(CUSTOMWORDGROUPS).filter(CUSTOMWORDGROUPS.ID == int(gid)).update({
            "TITLE": title,
            "YEAR": self._identifier_string(year),
            "TMDBID": int(tmdbid),
            "SEASON_COUNT": int(season_count),
        })

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
