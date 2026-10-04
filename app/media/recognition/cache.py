"""为解析器结果和识别证据提供有容量上限及 TTL 的缓存。"""

import copy
import hashlib
import json
import time
from collections import OrderedDict
from threading import RLock

from cacheout import Cache

from app.media.recognition.records import current_recorder, recognition_config


CACHE_MISS = object()
_DEFAULTS = {
    "parse": {"ttl_seconds": 86400, "max_entries": 4096,
              "max_bytes": 67108864, "max_entry_bytes": 1048576,
              "singleflight": True},
    "tmdb": {"ttl_seconds": 3600},
    "decision": {"ttl_seconds": 3600},
}
_CACHES = {
    "tmdb": Cache(maxsize=2048, ttl=3600, default=CACHE_MISS),
    "decision": Cache(maxsize=2048, ttl=3600, default=CACHE_MISS),
}


class _ParseCache:
    """解析缓存的 LRU、字节预算和代次保护。"""

    def __init__(self):
        self._lock = RLock()
        self._entries = OrderedDict()
        self._bytes = 0
        self._generation = 0
        self._signature = None

    @staticmethod
    def _integer(settings, name, default, minimum=1):
        try:
            return max(minimum, int(settings.get(name, default)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _effective_settings(settings):
        configured = settings.get("parse") or {}
        if not isinstance(configured, dict):
            configured = {}
        # 旧 TTL 仅在新字段缺失时兼容。
        ttl_default = settings.get("parse_ttl_seconds", _DEFAULTS["parse"]["ttl_seconds"])
        try:
            ttl_default = max(1, int(ttl_default))
        except (TypeError, ValueError):
            ttl_default = _DEFAULTS["parse"]["ttl_seconds"]
        result = {
            "enabled": bool(settings.get("enabled", True) and configured.get("enabled", True)),
            "ttl_seconds": _ParseCache._integer(
                configured, "ttl_seconds", ttl_default),
            "max_entries": _ParseCache._integer(
                configured, "max_entries", _DEFAULTS["parse"]["max_entries"]),
            "max_bytes": _ParseCache._integer(
                configured, "max_bytes", _DEFAULTS["parse"]["max_bytes"]),
            "max_entry_bytes": _ParseCache._integer(
                configured, "max_entry_bytes", _DEFAULTS["parse"]["max_entry_bytes"]),
            "singleflight": bool(configured.get(
                "singleflight", _DEFAULTS["parse"]["singleflight"])),
        }
        result["max_entry_bytes"] = min(result["max_entry_bytes"], result["max_bytes"])
        return result

    def _sync(self, settings):
        effective = self._effective_settings(settings)
        signature = tuple(sorted(effective.items()))
        if signature != self._signature:
            self._clear_locked()
            self._generation += 1
            self._signature = signature
        return effective

    def _clear_locked(self):
        self._entries.clear()
        self._bytes = 0

    @staticmethod
    def _size(value):
        try:
            return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), default=str).encode("utf-8"))
        except Exception:
            return None

    def generation(self, settings):
        with self._lock:
            self._sync(settings)
            return self._generation

    def singleflight_enabled(self, settings):
        with self._lock:
            return self._sync(settings)["singleflight"]

    def get(self, key, settings):
        with self._lock:
            effective = self._sync(settings)
            if not effective["enabled"]:
                return CACHE_MISS
            entry = self._entries.get(key)
            if entry is None:
                return CACHE_MISS
            expires, value, size = entry
            if expires <= time.monotonic():
                self._entries.pop(key, None)
                self._bytes -= size
                return CACHE_MISS
            self._entries.move_to_end(key)
            return copy.deepcopy(value)

    def put(self, key, value, settings, generation=None):
        with self._lock:
            effective = self._sync(settings)
            if not effective["enabled"] or (generation is not None and
                                              generation != self._generation):
                return False
            size = self._size(value)
            if size is None or size > effective["max_entry_bytes"]:
                return False
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous[2]
            # LRU 淘汰直到新值同时满足条数和总字节预算。
            while self._entries and (
                    len(self._entries) >= effective["max_entries"] or
                    self._bytes + size > effective["max_bytes"]):
                _, old = self._entries.popitem(last=False)
                self._bytes -= old[2]
            self._entries[key] = (
                time.monotonic() + effective["ttl_seconds"], copy.deepcopy(value), size)
            self._bytes += size
            return True

    def clear(self, settings=None):
        with self._lock:
            if settings is not None:
                self._sync(settings)
            self._clear_locked()
            self._generation += 1

    def info(self, settings):
        with self._lock:
            effective = self._sync(settings)
            now = time.monotonic()
            expired = [key for key, item in self._entries.items() if item[0] <= now]
            for key in expired:
                self._bytes -= self._entries.pop(key)[2]
            return {"entries": len(self._entries), "bytes": self._bytes,
                    "generation": self._generation, **effective}


_PARSE_CACHE = _ParseCache()


def cache_key(namespace, payload):
    """生成稳定且不可读的缓存键；名称和服务地址不会写入键中。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"recognition:{namespace}:{digest}"


def _cache_settings():
    recognition = recognition_config("recognition") or {}
    settings = recognition.get("cache") or {}
    return settings if isinstance(settings, dict) else {}


def generation(namespace="parse"):
    """取得缓存代次；清理或配置变更后旧任务不得回填。"""
    if namespace == "parse":
        return _PARSE_CACHE.generation(_cache_settings())
    return None


def singleflight_enabled(namespace="parse"):
    if namespace != "parse":
        return True
    return _PARSE_CACHE.singleflight_enabled(_cache_settings())


def get(namespace, key):
    settings = _cache_settings()
    if namespace == "parse":
        return _PARSE_CACHE.get(key, settings)
    if not settings.get("enabled", True):
        return CACHE_MISS
    cache = _CACHES.get(namespace)
    if cache is None:
        return CACHE_MISS
    value = cache.get(key, CACHE_MISS)
    return CACHE_MISS if value is CACHE_MISS else copy.deepcopy(value)


def put(namespace, key, value, negative=False, generation=None):
    settings = _cache_settings()
    if namespace == "parse":
        return _PARSE_CACHE.put(key, value, settings, generation=generation)
    if not settings.get("enabled", True) or namespace not in _CACHES:
        return False
    ttl_name = "negative_ttl_seconds" if negative else f"{namespace}_ttl_seconds"
    default_ttl = _DEFAULTS.get(namespace, {}).get("ttl_seconds", 3600)
    try:
        ttl = max(1, int(settings.get(ttl_name, default_ttl)))
    except (TypeError, ValueError):
        ttl = default_ttl
    _CACHES[namespace].set(key, copy.deepcopy(value), ttl=ttl)
    return True


def clear(namespace=None):
    """清除指定缓存层，或清除所有识别专用缓存层。"""
    if namespace == "parse":
        _PARSE_CACHE.clear(_cache_settings())
    elif namespace in _CACHES:
        _CACHES[namespace].clear()
    elif namespace is None:
        _PARSE_CACHE.clear(_cache_settings())
        for cache in _CACHES.values():
            cache.clear()


def info(namespace="parse"):
    """返回缓存占用概要，便于受保护的运维入口展示与检查。"""
    if namespace == "parse":
        return _PARSE_CACHE.info(_cache_settings())
    cache = _CACHES.get(namespace)
    if cache is None:
        return None
    return {"entries": len(cache), "max_entries": cache.maxsize}


def config_version():
    recorder = current_recorder()
    return recorder.context.get("recognition_config_version") if recorder else None
