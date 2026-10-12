import threading
import time

import log
from app.conf import ModuleConf
from app.db import MediaDb, get_db_metrics
from app.helper import ProgressHelper, SubmoduleHelper
from app.utils import ExceptionUtils
from app.utils.commons import singleton
from app.utils.types import MediaServerType
from config import Config

lock = threading.Lock()
server_lock = threading.Lock()


def _collect_media_sync_items(media_counts, libraries, get_items, on_library=None):
    """校验完整的全库拉取结果，禁止把不完整响应当成空索引发布。"""
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

    rows = []
    actual = {"movie": 0, "series": 0}
    seen_item_ids = set()
    seen_library_ids = set()
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
            if item_id in seen_item_ids:
                raise ValueError("媒体服务器重复返回媒体条目")
            seen_item_ids.add(item_id)
            normalized = dict(item)
            normalized["id"] = item_id
            normalized.setdefault("library", library_name)
            rows.append(normalized)
            actual[kind] += 1
        if on_library:
            on_library(library_name, len(rows))

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
            all_items = []
            # 开始进度条
            log.info("【MediaServer】开始同步媒体库数据...")
            self.progress.start("mediasync")
            self.progress.update(ptype="mediasync", text="请稍候...")
            try:
                all_items, movie_count, tv_count = _collect_media_sync_items(
                    self.get_medias_count(), self.get_libraries(), self.get_items,
                    on_library=lambda name, count: self.progress.update(
                        ptype="mediasync", text="已拉取 %s，累计：%s ..." % (name, count)))
            except Exception:
                log.error("【MediaServer】媒体库全量拉取失败或数据不完整，保留旧索引")
                self.progress.update(ptype="mediasync", text="媒体库数据不完整，同步已取消")
                self.progress.end("mediasync")
                return
            fetch_elapsed = time.perf_counter() - fetch_started_at

            db_started_at = time.perf_counter()
            replaced_count = self.mediadb.replace(self._server_type.value, all_items)
            if replaced_count is False:
                log.error("【MediaServer】媒体库索引替换失败，保留旧索引")
                self.progress.end("mediasync")
                return
            total_count = replaced_count
            db_elapsed = time.perf_counter() - db_started_at
            # 更新总体同步情况
            self.mediadb.statistics(server_type=self._server_type.value,
                                    total_count=total_count,
                                    movie_count=movie_count,
                                    tv_count=tv_count)
            # 结束进度条
            self.progress.update(ptype="mediasync",
                                 value=100,
                                 text="媒体库数据同步完成，同步数量：%s" % total_count)
            self.progress.end("mediasync")
            log.info("【MediaServer】媒体库数据同步完成，同步数量：%s" % total_count)
            total_elapsed = time.perf_counter() - started_at
            avg_elapsed = total_elapsed / total_count if total_count else 0
            log.info("【MediaServer】同步阶段耗时：拉取 %.3fs，数据库写入 %.3fs，总计 %.3fs，"
                     "条数 %s，单条 %.6fs" %
                     (fetch_elapsed, db_elapsed, total_elapsed, total_count, avg_elapsed))
            log.info("【MediaServer】SQLite 指标：提交 %s 次，重试 %s 次" %
                     (get_db_metrics().get("commits", 0), get_db_metrics().get("retries", 0)))

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
