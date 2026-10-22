"""为解析器结果和识别证据提供 TTL 缓存。"""

import copy
import hashlib
import json
import time
from threading import RLock

from app.utils.persistent_cache import PersistentCache, STORE
from app.utils.cache_memory import cache_memory_info
from app.utils.cache_policy import cache_ttl

from app.media.recognition.records import (
    current_recorder, recognition_config, recognition_abort_reason, recognition_remaining_seconds,
)


CACHE_MISS = object()
_DEFAULTS = {
    "parse": {"ttl_seconds": 86400, "singleflight": True},
    "tmdb": {"ttl_seconds": 3600},
    "decision": {"ttl_seconds": 3600},
}
_CACHES = {
    "tmdb": PersistentCache("recognition_tmdb", ttl=3600, default=CACHE_MISS),
    "decision": PersistentCache("recognition_decision", ttl=3600, default=CACHE_MISS),
}


class _ParseCache:
    """解析缓存的 TTL、配置失效和代次保护。"""

    def __init__(self, store=None):
        self._lock = RLock()
        self._generation = 0
        self._signature = None
        self._cache = PersistentCache("recognition_parse", maxsize=0,
                                      store=store if store is not None else STORE)
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
            "singleflight": bool(configured.get(
                "singleflight", _DEFAULTS["parse"]["singleflight"])),
        }
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
        return effective

    def _valid_payload(self, payload, effective):
        return (isinstance(payload, tuple) and len(payload) == 4 and payload[0] == 2
                and payload[1] == self._signature
                and (payload[3] is None or isinstance(payload[3], int) and payload[3] >= 0))

    @staticmethod
    def _legacy_payload(payload):
        if not isinstance(payload, tuple) or len(payload) != 3:
            return False
        signature, _, size = payload
        if not isinstance(signature, tuple):
            return False
        keys = {entry[0] for entry in signature if isinstance(entry, tuple) and len(entry) == 2}
        return (keys == {"enabled", "max_entries", "max_bytes", "max_entry_bytes",
                         "singleflight", "ttl_seconds"}
                and (size is None or isinstance(size, int) and size >= 0))

    def _make_payload(self, value, size=None):
        return 2, self._signature, value, size

    def _clear_locked(self):
        self._cache.clear()

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
            entry, expires = self._cache.get_with_expiration(key)
            if entry is None:
                return CACHE_MISS
            if not self._valid_payload(entry, effective):
                if not self._legacy_payload(entry):
                    self._remove(key)
                    return CACHE_MISS
                _, value, size = entry
                entry = self._make_payload(copy.deepcopy(value), size)
                self._cache.set_with_expiration(key, entry, expires)
            return copy.deepcopy(entry[2])

    def put(self, key, value, settings, generation=None):
        with self._lock:
            effective = self._sync(settings)
            if not effective["enabled"] or (generation is not None and
                                              generation != self._generation):
                return False
            ttl = cache_ttl("recognition_parse", effective["ttl_seconds"])
            expires = time.time() + ttl if ttl else None
            self._cache.set_with_expiration(
                key, self._make_payload(copy.deepcopy(value)), expires)
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
            generation = self._generation
        storage = self._cache.memory_info()
        result = {"entries": storage.get("entries", 0),
                  "bytes": storage.get("bytes"),
                  "max_entries": None, "max_bytes": None,
                  "generation": generation, **effective}
        result["ttl_seconds"] = cache_ttl("recognition_parse", effective["ttl_seconds"])
        if include_memory:
            result.update(storage)
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


def generation(namespace="parse", *, settings=None):
    """取得缓存代次；清理或配置变更后旧任务不得回填。"""
    if namespace == "parse":
        return _PARSE_CACHE.generation(_cache_settings() if settings is None else settings)
    return None


def singleflight_enabled(namespace="parse"):
    if namespace != "parse":
        return True
    return _PARSE_CACHE.singleflight_enabled(_cache_settings())


def get(namespace, key, *, settings=None):
    settings = _cache_settings() if settings is None else settings
    if namespace == "parse":
        return _PARSE_CACHE.get(key, settings)
    if not settings.get("enabled", True):
        return CACHE_MISS
    cache = _CACHES.get(namespace)
    if cache is None:
        return CACHE_MISS
    value = cache.get(key, CACHE_MISS)
    return CACHE_MISS if value is CACHE_MISS else copy.deepcopy(value)


def put(namespace, key, value, negative=False, generation=None, *, settings=None):
    remaining = recognition_remaining_seconds()
    if recognition_abort_reason() or remaining is not None and remaining <= 0:
        return False
    settings = _cache_settings() if settings is None else settings
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
    settings = _cache_settings()
    try:
        ttl = max(1, int(settings.get(f"{namespace}_ttl_seconds", _DEFAULTS[namespace]["ttl_seconds"])))
    except (TypeError, ValueError):
        ttl = _DEFAULTS[namespace]["ttl_seconds"]
    ttl = cache_ttl(f"recognition_{namespace}", ttl)
    if include_memory:
        return {**cache_memory_info(cache), "max_entries": cache.maxsize or None,
                "max_bytes": None, "ttl_seconds": ttl}
    cache.delete_expired()
    return {"entries": len(cache), "max_entries": cache.maxsize or None,
            "max_bytes": None, "ttl_seconds": ttl}


def config_version():
    recorder = current_recorder()
    return recorder.context.get("recognition_config_version") if recorder else None
