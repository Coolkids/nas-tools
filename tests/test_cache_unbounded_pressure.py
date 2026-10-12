"""大缓存压力回归；单独运行，不计入默认快速测试集。"""

import os

import pytest

from app.media.recognition.cache import CACHE_MISS, _ParseCache
from app.utils.cache_backend import CacheStore


pytestmark = pytest.mark.skipif(
    os.environ.get("NASTOOLS_RUN_CACHE_PRESSURE") != "1",
    reason="设置 NASTOOLS_RUN_CACHE_PRESSURE=1 运行较大的隔离压力用例")

SETTINGS = {"parse": {"ttl_seconds": 3600}}


def test_ai_cache_retains_more_than_4096_live_entries():
    store = CacheStore(settings={"backend": "memory"})
    cache = _ParseCache(store=store)
    try:
        for index in range(4100):
            assert cache.put(f"key-{index}", {"value": index}, SETTINGS)
        assert cache.info(SETTINGS)["entries"] == 4100
        assert cache.get("key-0", SETTINGS) == {"value": 0}
        assert cache.get("key-4099", SETTINGS) == {"value": 4099}
    finally:
        cache.clear(SETTINGS)
        store.close()


def test_ai_cache_retains_single_and_total_payloads_over_legacy_byte_limits():
    store = CacheStore(settings={"backend": "memory"})
    cache = _ParseCache(store=store)
    try:
        oversized = b"x" * (1024 * 1024 + 1)
        assert cache.put("over-1mib", {"payload": oversized}, SETTINGS)
        assert cache.get("over-1mib", SETTINGS)["payload"] == oversized

        payload = b"y" * (1024 * 1024)
        for index in range(65):
            assert cache.put(f"over-64mib-{index}", {"payload": payload}, SETTINGS)
        info = cache.info(SETTINGS)
        assert info["entries"] == 66
        assert info["bytes"] > 64 * 1024 * 1024
        assert cache.get("over-64mib-0", SETTINGS)["payload"] == payload
        assert cache.get("over-64mib-64", SETTINGS)["payload"] == payload
        assert cache.get("over-1mib", SETTINGS) is not CACHE_MISS
    finally:
        cache.clear(SETTINGS)
        store.close()
