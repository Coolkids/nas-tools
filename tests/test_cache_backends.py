"""CacheLib 后端切换、磁盘恢复及 Redis/Valkey 故障降级。"""

import os
import shutil
import socket
import subprocess
import time
from unittest.mock import patch

import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
from cachelib import SimpleCache, FileSystemCache, RedisCache, ValkeyCache

from app.utils.cache_backend import CacheStore, PREFIX
from app.utils.persistent_cache import PersistentCache


def test_memory_mode_never_writes_disk_and_copies_values(tmp_path):
    store = CacheStore(str(tmp_path / "cache"), settings={"backend": "memory"})
    cache = PersistentCache("test", store=store)
    cache.set("a", {"items": [1]})
    value = cache.get("a")
    value["items"].append(2)
    assert cache.get("a") == {"items": [1]}
    assert isinstance(store._backends["test"][0], SimpleCache)
    assert not (tmp_path / "cache").exists()
    assert CacheStore(str(tmp_path / "cache"), settings={"backend": "memory"}).get("test", "a") is None


def test_disk_mode_uses_cachelib_and_other_process_changes_invalidate_memory(tmp_path):
    path = str(tmp_path / "cache")
    first = CacheStore(path, settings={"backend": "memory_disk"})
    second = CacheStore(path, settings={"backend": "memory_disk"})
    first.put("test", "a", {"items": [1]}, time.time() + 60)
    assert isinstance(first._backends["test"][1], FileSystemCache)
    assert second.get("test", "a")[1] == {"items": [1]}
    second.put("test", "a", {"items": [2]}, time.time() + 60)
    assert first.get("test", "a")[1] == {"items": [2]}
    second.clear("test")
    assert first.get("test", "a") is None


def test_configuration_changes_take_effect_without_recreating_cache(tmp_path):
    settings = {"backend": "memory"}
    store = CacheStore(str(tmp_path / "cache"), settings=settings)
    cache = PersistentCache("test", store=store)
    cache.set("before", 1)
    settings["backend"] = "memory_disk"
    assert cache.get("before") is None
    cache.set("after", 2)
    restored = CacheStore(str(tmp_path / "cache"), settings={"backend": "memory_disk"})
    assert restored.get("test", "after")[1] == 2


def test_ai_cache_revalidates_persisted_settings_when_switching_back_to_disk(tmp_path):
    from app.media.recognition.cache import CACHE_MISS, _ParseCache

    settings = {"backend": "memory_disk"}
    store = CacheStore(str(tmp_path / "cache"), settings=settings)
    cache = _ParseCache(store=store)
    cache.put("old", {"value": 1}, {"parse": {"ttl_seconds": 60}})
    settings["backend"] = "memory"
    cache.put("new", {"value": 2}, {"parse": {"ttl_seconds": 120}})
    settings["backend"] = "memory_disk"
    assert cache.info({"parse": {"ttl_seconds": 120}})["entries"] == 0
    assert cache.get("old", {"parse": {"ttl_seconds": 120}}) is CACHE_MISS


def test_invalid_manual_configuration_and_connection_failure_fall_back_without_leaking_password():
    store = CacheStore(settings={"backend": "redis", "port": "invalid"})
    cache = PersistentCache("test", store=store)
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert store.status()["fallback"]
    assert store.status()["backend"] == "memory"
    store = CacheStore(settings={"backend": "redis", "password": "do-not-log-this"})
    with patch.object(store, "_connect_remote", side_effect=redis.ConnectionError("do-not-log-this")):
        store.put("test", "a", 1)
        store._retry_at = 0
        assert store.get("test", "a")[1] == 1  # 失败的恢复探测不能清空内存缓存。
        assert "do-not-log-this" not in str(store.status())


@pytest.mark.parametrize("settings", [{"backend": []}, {"auth_mode": {}},
                                      {"backend": "valkey", "auth_mode": "password", "password": ""}])
def test_malformed_manual_settings_still_allow_memory_cache(settings):
    store = CacheStore(settings=settings)
    cache = PersistentCache("test", store=store)
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert store.status()["backend"] == "memory"


class TemporaryValkey:
    def __init__(self, binary, directory, password):
        self.binary, self.directory, self.password = binary, str(directory), password
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = listener.getsockname()[1]
        self.process = None
        self.start()

    def start(self):
        command = [self.binary, "--bind", "127.0.0.1", "--port", str(self.port),
                   "--dir", self.directory, "--appendonly", "yes", "--appendfsync", "always",
                   "--save", "", "--loglevel", "warning"]
        if self.password:
            command += ["--requirepass", self.password]
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                self.client().ping()
                return
            except redis.ConnectionError:
                if self.process.poll() is not None:
                    raise RuntimeError(self.process.stderr.read().decode())
                time.sleep(0.02)
        raise RuntimeError("测试 Valkey 启动超时")

    def client(self):
        return redis.Redis(host="127.0.0.1", port=self.port, password=self.password,
                           socket_connect_timeout=0.2, socket_timeout=0.2,
                           retry=Retry(NoBackoff(), 0))

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        if self.process is not None:
            self.process.stderr.close()


@pytest.fixture(params=[None, "test-password"])
def server(request, tmp_path):
    binary = os.environ.get("NASTOOL_TEST_VALKEY_SERVER") or shutil.which("valkey-server")
    if not binary:
        pytest.skip("未安装 Valkey 测试服务；可设置 NASTOOL_TEST_VALKEY_SERVER")
    instance = TemporaryValkey(binary, tmp_path, request.param)
    try:
        yield instance
    finally:
        instance.stop()


@pytest.mark.parametrize("backend,cache_type", [("redis", RedisCache), ("valkey", ValkeyCache)])
def test_remote_backends_authentication_sharing_ttl_statistics_and_scoped_clear(server, backend, cache_type):
    settings = {"backend": backend, "port": server.port,
                "auth_mode": "password" if server.password else "none",
                "password": server.password or "ignored-in-none-mode",
                "username": "default" if server.password else ""}
    first, second = CacheStore(settings=settings), CacheStore(settings=settings)
    try:
        first.put("one", ("key", 1), {"payload": bytes(8192)}, time.time() + 60)
        first.put("two", "keep", "other")
        server.client().set("another-application", "untouched")
        assert first.status()["backend"] == backend
        assert isinstance(first._backends["one"][0], cache_type)
        assert second.get("one", ("key", 1))[1]["payload"] == bytes(8192)
        assert second.memory_info("one")["memory_bytes"] > 8192
        first.put("one", "expired", "old", time.time() - 1)
        assert second.get("one", "expired") is None
        assert len(second.load("one")) == 1
        first.clear("one")
        assert second.get("one", ("key", 1)) is None
        assert second.get("two", "keep")[1] == "other"
        assert server.client().get("another-application") == b"untouched"
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("backend", ["redis", "valkey"])
def test_remote_outage_fallback_and_recovery_do_not_resurrect_cleared_cache(server, backend):
    settings = {"backend": backend, "port": server.port,
                "auth_mode": "password" if server.password else "none", "password": server.password or ""}
    store = CacheStore(settings=settings)
    cache = PersistentCache("one", store=store)
    try:
        cache.set("old", {"value": "old"})
        server.client().set("another-application", "untouched")
        server.stop()
        assert cache.get("old") is None
        assert store.status()["fallback"]
        assert store.status()["backend"] == "memory"
        cache.set("during-outage", {"value": "local"})
        store._retry_at = 0
        assert cache.get("during-outage") == {"value": "local"}
        cache.clear()
        server.start()
        assert server.client().get(PREFIX + "one:" + store._key("old")) is not None  # AOF 已恢复。
        store._retry_at = 0
        assert cache.get("old") is None
        assert cache.get("during-outage") is None
        assert store.status()["backend"] == backend
        assert not store.status()["fallback"]
        assert server.client().get("another-application") == b"untouched"
        cache.set("new", 3)
        assert cache.get("new") == 3
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["redis", "valkey"])
def test_wrong_password_falls_back(server, backend):
    if not server.password:
        pytest.skip("此用例需要开启密码认证")
    store = CacheStore(settings={"backend": backend, "port": server.port,
                                 "auth_mode": "password", "password": "incorrect"})
    cache = PersistentCache("test", store=store)
    cache.set("a", 1)
    assert cache.get("a") == 1
    assert store.status()["fallback"]
    assert "AuthenticationError" in store.status()["reason"]
    store.close()


def test_fixing_configuration_after_outage_preserves_pending_invalidation(server):
    settings = {"backend": "valkey", "port": server.port,
                "auth_mode": "password" if server.password else "none", "password": server.password or ""}
    store = CacheStore(settings=settings)
    try:
        store.put("test", "old", 1)
        settings["port"] = 1
        store.clear("test")
        assert store.status()["fallback"]
        settings["port"] = server.port
        assert store.get("test", "old") is None
        assert store.status()["backend"] == "valkey"
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["redis", "valkey"])
def test_missing_memory_statistics_permission_does_not_trigger_fallback(server, backend):
    server.client().execute_command("ACL", "SETUSER", "stats-limited", "on", ">limited-password",
                                   "~nastool:cache:v2:*", "+@all", "-memory")
    store = CacheStore(settings={"backend": backend, "port": server.port, "auth_mode": "password",
                                 "username": "stats-limited", "password": "limited-password"})
    try:
        store.put("test", "key", {"value": 1})
        assert store.memory_info("test")["memory_bytes"] is None
        assert store.status()["backend"] == backend
        assert not store.status()["fallback"]
        assert store.get("test", "key")[1] == {"value": 1}
    finally:
        store.close()
