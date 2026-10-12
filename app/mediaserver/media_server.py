import threading
import time

import log
from app.conf import ModuleConf
from app.db import MediaDb
from app.helper import ProgressHelper, SubmoduleHelper
from app.utils import ExceptionUtils
from app.utils.commons import singleton
from app.utils.types import MediaServerType
from config import Config

lock = threading.Lock()
server_lock = threading.Lock()


_MEDIA_SYNC_BATCH_ROWS = 500
_MEDIA_SYNC_BATCH_BYTES = 4 * 1024 * 1024
_MEDIA_SYNC_BATCH_MIN_ROWS = 25
_MEDIA_SYNC_BATCH_MAX_ROWS = 2000
_MEDIA_SYNC_BATCH_TARGET_SECONDS = 0.25
_MEDIA_SYNC_LEASE_SECONDS = 3600
_MEDIA_SYNC_HEARTBEAT_SECONDS = 60


class _MediaSyncBatchSizer:
    """Bound transaction size and adapt future batches to observed write time."""

    def __init__(self, max_rows=_MEDIA_SYNC_BATCH_ROWS,
                 target_seconds=_MEDIA_SYNC_BATCH_TARGET_SECONDS,
                 min_rows=_MEDIA_SYNC_BATCH_MIN_ROWS,
                 max_limit=_MEDIA_SYNC_BATCH_MAX_ROWS):
        self.max_rows = max(min_rows, min(int(max_rows), max_limit))
        self.target_seconds = max(0.01, float(target_seconds))
        self.min_rows = max(1, int(min_rows))
        self.max_limit = max(self.min_rows, int(max_limit))

    def observe(self, row_count, transaction_seconds):
        """Adjust only subsequent batches; a slow transaction halves the limit."""
        try:
            elapsed = float(transaction_seconds)
            rows = int(row_count)
        except (TypeError, ValueError):
            return self.max_rows
        if rows <= 0 or elapsed <= 0:
            return self.max_rows
        if elapsed > self.target_seconds:
            ratio = max(0.5, min(0.9, self.target_seconds / elapsed))
            self.max_rows = max(self.min_rows, int(min(self.max_rows, rows) * ratio))
        elif elapsed < self.target_seconds * 0.6:
            self.max_rows = min(self.max_limit,
                                max(self.max_rows + 1, int(self.max_rows * 1.1)))
        return self.max_rows


def _media_sync_batches(items, max_rows=_MEDIA_SYNC_BATCH_ROWS,
                        max_bytes=_MEDIA_SYNC_BATCH_BYTES, batch_sizer=None):
    """按行数和持久化字段字节量切分批次，超大单条仍单独提交。"""
    batch = []
    batch_bytes = 0
    fields = ("id", "library", "type", "title", "originalTitle", "year",
              "tmdbid", "imdbid", "path")
    for item in items:
        item_bytes = sum(len(str(item.get(name) or "").encode("utf-8")) for name in fields)
        row_limit = min(max_rows, batch_sizer.max_rows) if batch_sizer else max_rows
        if batch and (len(batch) >= row_limit or batch_bytes + item_bytes > max_bytes):
            yield batch
            batch = []
            batch_bytes = 0
        batch.append(item)
        batch_bytes += item_bytes
    if batch:
        yield batch


def _collect_media_sync_items(media_counts, libraries, get_items, on_library=None,
                              on_batch=None, batch_sizer=None):
    """校验完整拉取结果并按批交付，支持直接写入隐藏代次以控制内存。"""
    if not isinstance(media_counts, dict):
        raise ValueError("媒体服务器未返回有效的媒体统计")

    expected = {}
    for key, kind in (("MovieCount", "movie"), ("SeriesCount", "series")):
        value = media_counts.get(key)
        if isinstance(value, bool):
            raise ValueError("媒体服务器媒体统计格式无效")
        try:
            count = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("媒体服务器媒体统计不完整") from error
        if (count < 0 or (isinstance(value, float) and not value.is_integer())
                or (isinstance(value, str) and value.strip() != str(count))):
            raise ValueError("媒体服务器媒体统计格式无效")
        expected[kind] = count

    if not isinstance(libraries, (list, tuple)):
        raise ValueError("媒体服务器未返回有效的媒体库列表")
    if sum(expected.values()) and not libraries:
        raise ValueError("媒体服务器媒体统计非空，但未返回媒体库")

    rows = [] if on_batch is None else None
    actual = {"movie": 0, "series": 0}
    seen_item_ids = set() if on_batch is None else None
    seen_library_ids = set()

    def normalized_items():
        for library in libraries:
            if not isinstance(library, dict):
                raise ValueError("媒体服务器返回了无效的媒体库记录")
            library_id = library.get("id")
            if library_id is None or not str(library_id).strip():
                raise ValueError("媒体服务器返回的媒体库缺少标识")
            library_id = str(library_id)
            if library_id in seen_library_ids:
                raise ValueError("媒体服务器重复返回媒体库")
            seen_library_ids.add(library_id)
            library_name = library.get("name") or library_id
            items = get_items(library_id)
            if items is None:
                raise ValueError("媒体服务器未返回媒体条目列表")
            for item in items:
                # Emby、Jellyfin 和 Plex 的旧适配器会以空对象结束生成器。
                if not item:
                    continue
                if not isinstance(item, dict):
                    raise ValueError("媒体服务器返回了无效的媒体条目")
                item_id = item.get("id")
                title = item.get("title")
                item_type = str(item.get("type") or "").strip().lower()
                if item_id is None or not str(item_id).strip() or not title:
                    raise ValueError("媒体服务器返回的媒体条目缺少标识或标题")
                if item_type == "movie":
                    kind = "movie"
                elif item_type in {"series", "show"}:
                    kind = "series"
                else:
                    raise ValueError("媒体服务器返回了不支持的媒体类型")
                item_id = str(item_id)
                if seen_item_ids is not None:
                    if item_id in seen_item_ids:
                        raise ValueError("媒体服务器重复返回媒体条目")
                    seen_item_ids.add(item_id)
                normalized = dict(item)
                normalized["id"] = item_id
                normalized.setdefault("library", library_name)
                actual[kind] += 1
                yield normalized
            if on_library:
                on_library(library_name, sum(actual.values()))

    if rows is not None:
        rows.extend(item for batch in _media_sync_batches(normalized_items()) for item in batch)
    else:
        for batch in _media_sync_batches(normalized_items(), batch_sizer=batch_sizer):
            on_batch(batch)

    if actual != expected:
        raise ValueError("媒体条目数量与服务器统计不一致，可能存在分页或拉取失败")
    return rows, actual["movie"], actual["series"]


@singleton
class MediaServer:
    _mediaserver_schemas = []
    _server_type = None
    _server = None
    mediadb = None
    progress = None

    def __init__(self):
        self._mediaserver_schemas = SubmoduleHelper.import_submodules(
            'app.mediaserver.client',
            filter_func=lambda _, obj: hasattr(obj, 'schema')
        )
        log.debug(f"【MediaServer】: 已经加载的媒体服务器：{self._mediaserver_schemas}")
        self.init_config()

    def init_config(self):
        self.mediadb = MediaDb()
        self.progress = ProgressHelper()
        # 当前使用的媒体库服务器
        _type = Config().get_config('media').get('media_server') or 'emby'
        self._server_type = ModuleConf.MEDIASERVER_DICT.get(_type)
        self._server = None

    def __build_class(self, ctype, conf):
        for mediaserver_schema in self._mediaserver_schemas:
            try:
                if mediaserver_schema.match(ctype):
                    return mediaserver_schema(conf)
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
        return None

    @property
    def server(self):
        with server_lock:
            if not self._server:
                self._server = self.__get_server(self._server_type)
            return self._server

    def __get_server(self, ctype: MediaServerType, conf=None):
        return self.__build_class(ctype=ctype.value, conf=conf)

    def get_type(self):
        """
        当前使用的媒体库服务器
        """
        return self._server_type

    def get_activity_log(self, limit):
        """
        获取媒体服务器的活动日志
        :param limit: 条数限制
        """
        if not self.server:
            return []
        return self.server.get_activity_log(limit)

    def get_user_count(self):
        """
        获取媒体服务器的总用户数
        """
        if not self.server:
            return 0
        return self.server.get_user_count()

    def get_medias_count(self):
        """
        获取媒体服务器各类型的媒体库
        :return: MovieCount SeriesCount SongCount
        """
        if not self.server:
            return None
        return self.server.get_medias_count()

    def refresh_root_library(self):
        """
        刷新媒体服务器整个媒体库
        """
        if not self.server:
            return
        return self.server.refresh_root_library()

    def get_image_by_id(self, item_id, image_type):
        """
        根据ItemId从媒体服务器查询图片地址
        :param item_id: 在Emby中的ID
        :param image_type: 图片的类弄地，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        if not self.server:
            return None
        return self.server.get_image_by_id(item_id, image_type)

    def get_no_exists_episodes(self, meta_info,
                               season_number,
                               episode_count):
        """
        根据标题、年份、季、总集数，查询媒体服务器中缺少哪几集
        :param meta_info: 已识别的需要查询的媒体信息
        :param season_number: 季号，数字
        :param episode_count: 该季的总集数
        :return: 该季不存在的集号列表
        """
        if not self.server:
            return None
        return self.server.get_no_exists_episodes(meta_info,
                                                  season_number,
                                                  episode_count)

    def get_movies(self, title, year=None):
        """
        根据标题和年份，检查电影是否在媒体服务器中存在，存在则返回列表
        :param title: 标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :return: 含title、year属性的字典列表
        """
        if not self.server:
            return None
        return self.server.get_movies(title, year)

    def refresh_library_by_items(self, items):
        """
        按类型、名称、年份来刷新媒体库
        :param items: 已识别的需要刷新媒体库的媒体信息列表
        """
        if not self.server:
            return
        return self.server.refresh_library_by_items(items)

    def get_libraries(self):
        """
        获取媒体服务器所有媒体库列表
        """
        if not self.server:
            return []
        return self.server.get_libraries()

    def get_items(self, parent):
        """
        获取媒体库中的所有媒体
        :param parent: 上一级的ID
        """
        if not self.server:
            return []
        return self.server.get_items(parent)

    def sync_mediaserver(self):
        """
        同步媒体库所有数据到本地数据库
        """
        if not self.server:
            return
        with lock:
            started_at = time.perf_counter()
            fetch_started_at = started_at
            # 开始进度条
            log.info("【MediaServer】开始同步媒体库数据...")
            self.progress.start("mediasync")
            self.progress.update(ptype="mediasync", text="请稍候...")
            try:
                lease = self.mediadb.acquire_sync_lease(
                    self._server_type.value, lease_seconds=_MEDIA_SYNC_LEASE_SECONDS)
            except Exception:
                log.error("【MediaServer】无法取得媒体同步租约，本次同步未启动")
                self.progress.update(ptype="mediasync", text="无法开始媒体同步，请稍后重试")
                self.progress.end("mediasync")
                return
            if not lease:
                log.info("【MediaServer】同一服务器已有有效的同步作业，本次跳过")
                self.progress.update(ptype="mediasync", text="媒体库已有同步任务正在运行")
                self.progress.end("mediasync")
                return

            heartbeat_stop = threading.Event()
            heartbeat_lost = threading.Event()

            def renew_lease():
                try:
                    while not heartbeat_stop.wait(_MEDIA_SYNC_HEARTBEAT_SECONDS):
                        if not self.mediadb.renew_sync_lease(
                                lease, lease_seconds=_MEDIA_SYNC_LEASE_SECONDS):
                            heartbeat_lost.set()
                            return
                except Exception:
                    heartbeat_lost.set()
                finally:
                    self.mediadb.close_session()

            heartbeat = threading.Thread(target=renew_lease,
                                         name="mediasync-lease-heartbeat", daemon=True)
            heartbeat.start()
            published = False
            cleanup_elapsed = 0.0

            def finish_lease(status):
                try:
                    self.mediadb.release_sync_lease(lease, status=status)
                except Exception:
                    log.warning("【MediaServer】同步租约释放失败，将在过期后由后续任务接管")

            def cleanup_generations(**kwargs):
                nonlocal cleanup_elapsed
                cleanup_started = time.perf_counter()
                try:
                    return self.mediadb.cleanup_sync_generations(lease, **kwargs)
                finally:
                    cleanup_elapsed += time.perf_counter() - cleanup_started

            def log_sync_metrics():
                metrics = lease.get("_sync_metrics", {})
                log.info("【MediaServer】本次同步数据库指标：写入批次 %s，最长批次事务 %.3fs，"
                         "批次维护锁等待 %.3fs，最终批大小上限 %s 行，代次清理 %.3fs" % (
                             metrics.get("batch_count", 0),
                             metrics.get("longest_transaction_seconds", 0.0),
                             metrics.get("maintenance_lock_wait_seconds", 0.0),
                             batch_sizer.max_rows,
                             cleanup_elapsed))

            written_count = 0
            stream_write_elapsed = 0.0
            batch_sizer = _MediaSyncBatchSizer()

            def write_sync_batch(batch):
                nonlocal written_count, stream_write_elapsed
                if heartbeat_lost.is_set():
                    raise RuntimeError("媒体同步租约已失效，停止写入新代次")
                batch_started = time.perf_counter()
                written_count += self.mediadb.write_sync_batch(lease, batch)
                stream_write_elapsed += time.perf_counter() - batch_started
                metrics = lease.get("_sync_metrics", {})
                batch_sizer.observe(len(batch), metrics.get("last_transaction_seconds"))

            try:
                cleanup_generations()
                _, movie_count, tv_count = _collect_media_sync_items(
                    self.get_medias_count(), self.get_libraries(), self.get_items,
                    on_library=lambda name, count: self.progress.update(
                        ptype="mediasync", text="已拉取 %s，累计：%s ..." % (name, count)),
                    on_batch=write_sync_batch, batch_sizer=batch_sizer)
                if written_count != movie_count + tv_count:
                    raise RuntimeError("媒体新代次写入条数与拉取快照不一致")
                if heartbeat_lost.is_set():
                    raise RuntimeError("媒体同步租约已失效")
            except Exception:
                log.error("【MediaServer】媒体库全量拉取失败或数据不完整，保留旧索引")
                self.progress.update(ptype="mediasync", text="媒体库数据不完整，同步已取消")
                try:
                    cleanup_generations(generation_id=lease["generation_id"])
                except Exception:
                    log.warning("【MediaServer】未发布代次清理失败，将由后续同步恢复")
                finish_lease("FAILED")
                log_sync_metrics()
                self.progress.end("mediasync")
                heartbeat_stop.set()
                heartbeat.join(timeout=2)
                return
            fetch_elapsed = max(0.0, time.perf_counter() - fetch_started_at - stream_write_elapsed)

            db_started_at = time.perf_counter()
            try:
                if (heartbeat_lost.is_set()
                        or not self.mediadb.renew_sync_lease(
                            lease, lease_seconds=_MEDIA_SYNC_LEASE_SECONDS)):
                    raise RuntimeError("媒体同步租约已失效，拒绝发布新代次")
                publish_result = self.mediadb.publish_sync_generation(
                    lease, movie_count=movie_count, series_count=tv_count)
                published = True
            except Exception:
                log.error("【MediaServer】媒体新代次写入或发布失败，保留旧活动索引")
                try:
                    cleanup_generations(generation_id=lease["generation_id"])
                except Exception:
                    log.warning("【MediaServer】未发布代次清理失败，将由后续同步恢复")
                finish_lease("CLEANUP_PENDING" if published else "FAILED")
                log_sync_metrics()
                self.progress.update(ptype="mediasync", text="媒体库同步失败，旧索引保持可用")
                self.progress.end("mediasync")
                heartbeat_stop.set()
                heartbeat.join(timeout=2)
                return

            total_count = publish_result["total_count"]
            db_elapsed = stream_write_elapsed + time.perf_counter() - db_started_at
            cleanup_error = None
            for attempt in range(3):
                try:
                    cleanup_generations()
                    cleanup_error = None
                    break
                except Exception:
                    cleanup_error = True
                    if attempt < 2:
                        time.sleep(2 ** attempt)
            if cleanup_error:
                finish_lease("CLEANUP_PENDING")
                log.warning("【MediaServer】新代次已发布，旧代次清理失败，保留待恢复状态")
            else:
                finish_lease("SUCCEEDED")
            # 结束进度条
            self.progress.update(ptype="mediasync",
                                 value=100,
                                 text=("媒体库数据同步完成，旧数据待清理，同步数量：%s" if cleanup_error
                                       else "媒体库数据同步完成，同步数量：%s") % total_count)
            self.progress.end("mediasync")
            log.info("【MediaServer】媒体库数据同步完成，同步数量：%s" % total_count)
            total_elapsed = time.perf_counter() - started_at
            avg_elapsed = total_elapsed / total_count if total_count else 0
            log.info("【MediaServer】同步阶段耗时：拉取 %.3fs，数据库写入 %.3fs，总计 %.3fs，"
                     "条数 %s，单条 %.6fs" %
                     (fetch_elapsed, db_elapsed, total_elapsed, total_count, avg_elapsed))
            log_sync_metrics()
            self.mediadb.close_session()
            heartbeat_stop.set()
            heartbeat.join(timeout=2)

    def check_item_exists(self, title, year=None, tmdbid=None):
        """
        检查媒体库是否已存在某项目，非实时同步数据，仅用于展示
        """
        return self.mediadb.exists(server_type=self._server_type.value,
                                   title=title,
                                   year=year,
                                   tmdbid=tmdbid)

    def get_mediasync_status(self):
        """
        获取当前媒体库同步状态
        """
        status = self.mediadb.get_statistics(server_type=self._server_type.value)
        if not status:
            return {}
        else:
            return {"movie_count": status.MOVIE_COUNT, "tv_count": status.TV_COUNT, "time": status.UPDATE_TIME}

    def get_iteminfo(self, itemid):
        """
        根据ItemId从媒体服务器查询项目详情
        :param itemid: 在Emby中的ID
        :return: 图片对应在TMDB中的URL
        """
        if not self.server:
            return None
        return self.server.get_iteminfo(itemid)

    def get_playing_sessions(self):
        """
        获取正在播放的会话
        """
        if not self.server:
            return None
        return self.server.get_playing_sessions()
