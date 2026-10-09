"""为解析器结果和识别证据提供有容量上限及 TTL 的缓存。"""

import copy
import hashlib
import json
import time
from threading import RLock

from app.utils.persistent_cache import CacheStore, PersistentCache, STORE
from app.utils.cache_memory import cache_memory_info

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
    "tmdb": PersistentCache("recognition_tmdb", maxsize=2048, ttl=3600, default=CACHE_MISS),
    "decision": PersistentCache("recognition_decision", maxsize=2048, ttl=3600, default=CACHE_MISS),
}


class _ParseCache:
    """解析缓存的 LRU、字节预算和代次保护。"""

    def __init__(self, store=None):
        self._lock = RLock()
        self._generation = 0
        self._signature = None
        self._cache = PersistentCache("recognition_parse", maxsize=0,
                                      store=store if store is not None else CacheStore(settings={"backend": "memory"}))
        self._store_revision = None

    def _remove(self, key):
        self._cache.delete(key)

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
        revision = self._cache.store.revision
        backend_changed = revision != self._store_revision
        first_load = self._signature is None
        if backend_changed:
            self._store_revision = revision
            self._generation += 1
        if signature != self._signature:
            if not first_load:
                self._clear_locked()
            self._generation += 1
            self._signature = signature
        if first_load or backend_changed:
            for key, payload in self._cache.copy().items():
                if not effective["enabled"] or not self._valid_payload(payload, effective):
                    self._remove(key)
            entries = self._cache.copy()
            while entries and (len(entries) > effective["max_entries"] or
                               sum(item[2] for item in entries.values()) > effective["max_bytes"]):
                key = next(iter(entries))
                self._remove(key)
                entries.pop(key)
        return effective

    def _valid_payload(self, payload, effective):
        return (isinstance(payload, tuple) and len(payload) == 3 and payload[0] == self._signature
                and isinstance(payload[2], int) and 0 <= payload[2] <= effective["max_entry_bytes"])

    def _clear_locked(self):
        self._cache.clear()

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
            entry = self._cache.get(key)
            if entry is None:
                return CACHE_MISS
            if not self._valid_payload(entry, effective):
                self._remove(key)
                return CACHE_MISS
            signature, value, size = entry
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
            entries = self._cache.copy()
            if key in entries:
                self._remove(key)
                entries.pop(key)
            # LRU 淘汰直到新值同时满足条数和总字节预算。
            total_bytes = sum(item[2] for item in entries.values())
            while entries and (len(entries) >= effective["max_entries"] or
                               total_bytes + size > effective["max_bytes"]):
                oldest = next(iter(entries))
                total_bytes -= entries.pop(oldest)[2]
                self._remove(oldest)
            self._cache.set(key, (self._signature, copy.deepcopy(value), size), ttl=effective["ttl_seconds"])
            return True

    def clear(self, settings=None):
        with self._lock:
            if settings is not None:
                self._sync(settings)
            self._clear_locked()
            self._generation += 1

    def info(self, settings, include_memory=False):
        with self._lock:
            effective = self._sync(settings)
            entries = self._cache.copy()
            result = {"entries": len(entries), "bytes": sum(item[2] for item in entries.values()),
                      "generation": self._generation, **effective}
            if include_memory:
                result.update(self._cache.memory_info())
            return result


_PARSE_CACHE = _ParseCache(store=STORE)


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


def info(namespace="parse", include_memory=False):
    """返回缓存占用概要，便于受保护的运维入口展示与检查。"""
    if namespace == "parse":
        return _PARSE_CACHE.info(_cache_settings(), include_memory=include_memory)
    cache = _CACHES.get(namespace)
    if cache is None:
        return None
    if include_memory:
        return {**cache_memory_info(cache), "max_entries": cache.maxsize}
    cache.delete_expired()
    return {"entries": len(cache), "max_entries": cache.maxsize}


def config_version():
    recorder = current_recorder()
    return recorder.context.get("recognition_config_version") if recorder else None
