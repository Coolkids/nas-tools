"""为解析器结果和识别证据提供有容量上限及 TTL 的缓存。"""

import copy
import hashlib
import json

from cacheout import Cache

from app.media.recognition.records import current_recorder, recognition_config


CACHE_MISS = object()
_CACHES = {
    "parse": Cache(maxsize=512, ttl=86400, default=CACHE_MISS),
    "tmdb": Cache(maxsize=2048, ttl=3600, default=CACHE_MISS),
    "decision": Cache(maxsize=2048, ttl=3600, default=CACHE_MISS),
}


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


def get(namespace, key):
    settings = _cache_settings()
    if not settings.get("enabled", True):
        return CACHE_MISS
    cache = _CACHES.get(namespace)
    if cache is None:
        return CACHE_MISS
    value = cache.get(key, CACHE_MISS)
    return CACHE_MISS if value is CACHE_MISS else copy.deepcopy(value)


def put(namespace, key, value, negative=False):
    settings = _cache_settings()
    if not settings.get("enabled", True) or namespace not in _CACHES:
        return False
    ttl_name = "negative_ttl_seconds" if negative else f"{namespace}_ttl_seconds"
    default_ttl = {"parse": 86400, "tmdb": 3600, "decision": 3600}.get(namespace, 3600)
    try:
        ttl = max(1, int(settings.get(ttl_name, default_ttl)))
    except (TypeError, ValueError):
        ttl = default_ttl
    _CACHES[namespace].set(key, copy.deepcopy(value), ttl=ttl)
    return True


def clear(namespace=None):
    """清除指定缓存层，或清除所有识别专用缓存层。"""
    if namespace in _CACHES:
        _CACHES[namespace].clear()
    elif namespace is None:
        for cache in _CACHES.values():
            cache.clear()


def config_version():
    recorder = current_recorder()
    return recorder.context.get("recognition_config_version") if recorder else None
