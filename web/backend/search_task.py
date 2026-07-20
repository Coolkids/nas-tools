import datetime
import json
import threading
import traceback

import log
from app.helper import DbHelper
from web.backend.search_torrents import search_medias_for_web

_MAX_RESULTS = 20
_MAX_WORKERS = 2
_MAX_QUEUE = 10
_TASK_TIMEOUT = 600


class SearchTaskPool:
    _lock = threading.Lock()
    _running = {}
    _queue = []

    @classmethod
    def submit(cls, keyword, ident_flag, filters, tmdbid, media_type):
        db = DbHelper()
        with cls._lock:
            task = db.get_search_task(keyword)
            if task:
                if task.STATUS == 'running':
                    return {"code": 0, "status": "running", "keyword": keyword}
                if task.STATUS == 'success' and task.END_TIME:
                    try:
                        end = datetime.datetime.strptime(task.END_TIME, "%Y-%m-%d %H:%M:%S")
                        if datetime.datetime.now() - end < datetime.timedelta(hours=6):
                            return {"code": 0, "status": "cached", "keyword": keyword}
                    except Exception:
                        pass

            if keyword in cls._running:
                return {"code": 0, "status": "running", "keyword": keyword}
            for qk, _, _, _, _ in cls._queue:
                if qk == keyword:
                    return {"code": 0, "status": "queued", "keyword": keyword}

            db.save_search_task(keyword=keyword, status='pending',
                                start_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                message='等待执行')

            if len(cls._running) < _MAX_WORKERS:
                cls._start_task(keyword, ident_flag, filters, tmdbid, media_type)
                return {"code": 0, "status": "running", "keyword": keyword}
            elif len(cls._queue) < _MAX_QUEUE:
                cls._queue.append((keyword, ident_flag, filters, tmdbid, media_type))
                return {"code": 0, "status": "queued", "keyword": keyword}
            else:
                return {"code": -1, "status": "queue_full", "msg": "队列已满，请稍后再试"}

    @classmethod
    def _start_task(cls, keyword, ident_flag, filters, tmdbid, media_type):
        cancel_event = threading.Event()
        cls._running[keyword] = cancel_event
        thread = threading.Thread(target=cls._run_task,
                                  args=(keyword, ident_flag, filters, tmdbid, media_type, cancel_event),
                                  daemon=True)
        thread.start()

    @classmethod
    def _run_task(cls, keyword, ident_flag, filters, tmdbid, media_type, cancel_event):
        db = DbHelper()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.save_search_task(keyword=keyword, status='running', start_time=now_str, message='搜索中...')
        result = []
        try:
            search_thread = threading.Thread(target=lambda: result.append(
                search_medias_for_web(content=keyword,
                                      ident_flag=ident_flag,
                                      filters=filters,
                                      tmdbid=tmdbid,
                                      media_type=media_type,
                                      keyword=keyword,
                                      cancel_event=cancel_event)
            ))
            search_thread.daemon = True
            search_thread.start()
            search_thread.join(timeout=_TASK_TIMEOUT)

            if search_thread.is_alive():
                cancel_event.set()
                db.save_search_task(keyword=keyword, status='failed',
                                    end_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    message='搜索超时')
            elif not result:
                db.save_search_task(keyword=keyword, status='failed',
                                    end_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    message='搜索异常')
            else:
                ret, ret_msg = result[0]
                if cancel_event.is_set():
                    db.save_search_task(keyword=keyword, status='failed',
                                        end_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        message='搜索超时')
                elif ret != 0:
                    db.save_search_task(keyword=keyword, status='failed',
                                        end_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        message=ret_msg or '搜索失败')
                else:
                    db.save_search_task(keyword=keyword, status='success',
                                        end_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        message='搜索完成')
                    db.cleanup_search_tasks(_MAX_RESULTS)
        except Exception as e:
            db.save_search_task(keyword=keyword, status='failed',
                                end_time=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                message=str(e))
        finally:
            with cls._lock:
                cls._running.pop(keyword, None)
                cls._queue[:] = [q for q in cls._queue if q[0] != keyword]
                while cls._queue and len(cls._running) < _MAX_WORKERS:
                    next_kw, n_ident, n_filters, n_tmdbid, n_mtype = cls._queue.pop(0)
                    cls._start_task(next_kw, n_ident, n_filters, n_tmdbid, n_mtype)

    @classmethod
    def cancel_task(cls, keyword):
        with cls._lock:
            cancel = cls._running.get(keyword)
            if cancel:
                cancel.set()
                return True
            old_len = len(cls._queue)
            cls._queue[:] = [q for q in cls._queue if q[0] != keyword]
            return len(cls._queue) < old_len

    @classmethod
    def get_status(cls, keyword):
        db = DbHelper()
        with cls._lock:
            if keyword in cls._running:
                return "running"
            for qk, _, _, _, _ in cls._queue:
                if qk == keyword:
                    return "queued"
        task = db.get_search_task(keyword)
        return task.STATUS if task else None

    @classmethod
    def get_task_list(cls):
        db = DbHelper()
        tasks = db.get_search_tasks(_MAX_RESULTS)
        queue_keywords = {q[0] for q in cls._queue}
        result = []
        for t in tasks:
            with cls._lock:
                if t.KEYWORD in cls._running:
                    status = 'running'
                elif t.KEYWORD in queue_keywords:
                    status = 'queued'
                else:
                    status = t.STATUS
            result.append({
                "keyword": t.KEYWORD,
                "status": status,
                "start_time": t.START_TIME,
                "end_time": t.END_TIME,
                "message": t.MESSAGE
            })
        return result

    @classmethod
    def get_task_result(cls, keyword):
        db = DbHelper()
        task = db.get_search_task(keyword)
        if not task:
            return None
        raw_results = db.get_search_results_by_keyword(keyword)
        results = []
        for r in raw_results:
            results.append({
                "id": r.ID,
                "torrent_name": r.TORRENT_NAME,
                "enclosure": r.ENCLOSURE,
                "description": r.DESCRIPTION,
                "type": r.TYPE,
                "title": r.TITLE,
                "year": r.YEAR,
                "season": r.SEASON,
                "episode": r.EPISODE,
                "es_string": r.ES_STRING,
                "vote": r.VOTE,
                "image": r.IMAGE,
                "poster": r.POSTER,
                "tmdbid": r.TMDBID,
                "overview": r.OVERVIEW,
                "res_type": r.RES_TYPE,
                "res_order": r.RES_ORDER,
                "size": r.SIZE,
                "seeders": r.SEEDERS,
                "peers": r.PEERS,
                "site": r.SITE,
                "site_order": r.SITE_ORDER,
                "pageurl": r.PAGEURL,
                "otherinfo": r.OTHERINFO,
                "upload_volume_factor": r.UPLOAD_VOLUME_FACTOR,
                "download_volume_factor": r.DOWNLOAD_VOLUME_FACTOR,
                "note": r.NOTE,
            })
        return {
            "task": {
                "keyword": task.KEYWORD,
                "status": task.STATUS,
                "start_time": task.START_TIME,
                "end_time": task.END_TIME,
                "message": task.MESSAGE
            },
            "results": results
        }

    @classmethod
    def delete_task(cls, keyword):
        """
        删除已完成的任务及其结果
        """
        db = DbHelper()
        with cls._lock:
            if keyword in cls._running:
                return {"code": -1, "msg": "任务正在运行，无法删除"}
            for qk, _, _, _, _ in cls._queue:
                if qk == keyword:
                    return {"code": -1, "msg": "任务正在排队，无法删除"}
            task = db.get_search_task(keyword)
            if not task:
                return {"code": 1, "msg": "任务不存在"}
            if task.STATUS not in ('success', 'failed'):
                return {"code": -1, "msg": "只能删除已完成或已失败的任务"}
            db.delete_search_task(keyword)
            return {"code": 0, "msg": "任务已删除"}

    @classmethod
    def recover_crashed_tasks(cls):
        db = DbHelper()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        running = db.get_running_tasks()
        for task in running:
            db.save_search_task(keyword=task.KEYWORD, status='failed',
                                end_time=now_str, message='进程异常退出')
        if running:
            log.info(f"【Task】已恢复 {len(running)} 个异常中断的搜索任务")
