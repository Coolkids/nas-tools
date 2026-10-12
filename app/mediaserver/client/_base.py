import time
from abc import ABCMeta, abstractmethod

import requests


def _get_media_sync_page(url, client_name, request_utils, attempts=3):
    """对媒体全量同步的瞬时网络错误和服务端拥塞作有限重试。"""
    last_error = None
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        try:
            response = request_utils().get_res(url)
        except requests.exceptions.RequestException as error:
            last_error = error
        else:
            if response is None:
                last_error = RuntimeError("远程服务未返回分页响应")
            elif response.status_code in {408, 429} or response.status_code >= 500:
                last_error = RuntimeError(f"远程服务返回 HTTP {response.status_code}")
            else:
                return response
        if attempt + 1 < attempts:
            time.sleep(min(0.2 * (2 ** attempt), 1.0))
    raise RuntimeError(f"{client_name} 条目分页请求失败，已重试 {attempts} 次") from last_error


MEDIA_SYNC_PAGE_SIZE = 500


class _IMediaClient(metaclass=ABCMeta):

    @abstractmethod
    def match(self, ctype):
        """
        匹配实例
        """
        pass

    @abstractmethod
    def get_status(self):
        """
        检查连通性
        """
        pass

    @abstractmethod
    def get_user_count(self):
        """
        获得用户数量
        """
        pass

    @abstractmethod
    def get_activity_log(self, num):
        """
        获取Emby活动记录
        """
        pass

    @abstractmethod
    def get_medias_count(self):
        """
        获得电影、电视剧、动漫媒体数量
        :return: MovieCount SeriesCount SongCount
        """
        pass

    @abstractmethod
    def get_movies(self, title, year):
        """
        根据标题和年份，检查电影是否在存在，存在则返回列表
        :param title: 标题
        :param year: 年份，可以为空，为空时不按年份过滤
        :return: 含title、year属性的字典列表
        """
        pass

    @abstractmethod
    def get_no_exists_episodes(self, meta_info, season, total_num):
        """
        根据标题、年份、季、总集数，查询缺少哪几集
        :param meta_info: 已识别的需要查询的媒体信息
        :param season: 季号，数字
        :param total_num: 该季的总集数
        :return: 该季不存在的集号列表
        """
        pass

    @abstractmethod
    def get_image_by_id(self, item_id, image_type):
        """
        根据ItemId查询图片地址
        :param item_id: 在服务器中的ID
        :param image_type: 图片的类弄地，poster或者backdrop等
        :return: 图片对应在TMDB中的URL
        """
        pass

    @abstractmethod
    def refresh_root_library(self):
        """
        刷新整个媒体库
        """
        pass

    @abstractmethod
    def refresh_library_by_items(self, items):
        """
        按类型、名称、年份来刷新媒体库
        :param items: 已识别的需要刷新媒体库的媒体信息列表
        """
        pass

    @abstractmethod
    def get_libraries(self):
        """
        获取媒体服务器所有媒体库列表
        """
        pass

    @abstractmethod
    def get_items(self, parent):
        """
        获取媒体库中的所有媒体
        :param parent: 上一级的ID
        """
        pass

    @abstractmethod
    def get_playing_sessions(self):
        """
        获取正在播放的会话
        """
        pass
