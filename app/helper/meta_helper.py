"""TMDB 元数据缓存，沿用业务接口，存储统一交给 CacheLib。"""

import time
from enum import Enum
from threading import RLock

from app.utils.cache_memory import cache_memory_info
from app.utils.commons import singleton
from app.utils.persistent_cache import PersistentCache
from config import Config

lock = RLock()
CACHE_EXPIRE_TIMESTAMP_STR = "cache_expire_timestamp"
EXPIRE_TIMESTAMP = 7 * 24 * 3600


@singleton
class MetaHelper:
    def __init__(self):
        self._cache = PersistentCache("tmdb_metadata", maxsize=0)
        self.init_config()

    def init_config(self):
        laboratory = Config().get_config("laboratory") or {}
        self._tmdb_cache_expire = bool(laboratory.get("tmdb_cache_expire"))

    def clear_meta_data(self):
        with lock:
            self._cache.clear()

    def cache_info(self, include_memory=False):
        with lock:
            return cache_memory_info(self._cache) if include_memory else {"entries": len(self._cache)}

    def get_meta_data_by_key(self, key):
        with lock:
            info = self._cache.get(key) or {}
            expires = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
            if expires and expires <= time.time() and self._tmdb_cache_expire:
                self._cache.delete(key)
                return {}
            if info and (not expires or expires > time.time()):
                info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                self._cache.set(key, info)
            return info

    def dump_meta_data(self, search, page, num):
        with lock:
            values = [(key, {"id": value.get("id"), "title": value.get("title"),
                             "year": value.get("year"),
                             "media_type": value.get("type").value if isinstance(value.get("type"), Enum)
                             else value.get("type"), "poster_path": value.get("poster_path"),
                             "backdrop_path": value.get("backdrop_path")},
                       str(key).replace("[电影]", "").replace("[电视剧]", "")
                       .replace("[未知]", "").replace("-None", ""))
                      for key, value in self._cache.copy().items()
                      if search.lower() in key.lower() and str(value.get("id")) != "0"]
            start = max(0, page - 1) * num
            return len(values), values[start:start + num]

    def delete_meta_data(self, key):
        with lock:
            return self._cache.pop(key)

    def delete_meta_data_by_tmdbid(self, tmdbid):
        with lock:
            for key, value in self._cache.copy().items():
                if str(value.get("id")) == str(tmdbid):
                    self._cache.delete(key)

    def delete_unknown_meta(self):
        with lock:
            for key, value in self._cache.copy().items():
                if str(value.get("id")) == "0":
                    self._cache.delete(key)

    def modify_meta_data(self, key, title):
        with lock:
            value = self._cache.get(key)
            if value:
                value["title"] = title
                value[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                self._cache.set(key, value)
            return value

    def update_meta_data(self, meta_data):
        with lock:
            for key, value in (meta_data or {}).items():
                if not self._cache.get(key):
                    value = dict(value)
                    value[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                    self._cache.set(key, value)

    def save_meta_data(self, force=False):
        # 保留旧的定时任务和 WebAction 入口；CacheLib 写入时已经保存。
        with lock:
            if self._tmdb_cache_expire:
                for key, value in self._cache.copy().items():
                    if value.get(CACHE_EXPIRE_TIMESTAMP_STR, float("inf")) <= time.time():
                        self._cache.delete(key)

    def get_cache_title(self, key):
        value = self._cache.get(key) or {}
        return value.get("title") if value.get("id") else None

    def set_cache_title(self, key, cn_title):
        with lock:
            value = self._cache.get(key)
            if value:
                value["title"] = cn_title
                self._cache.set(key, value)
