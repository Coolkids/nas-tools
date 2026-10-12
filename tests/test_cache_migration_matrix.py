"""缓存后端切换任务在四种存储方式间的端到端迁移矩阵。"""

import itertools
import os
import shutil
import sqlite3
import threading
import time

import pytest

from app.utils import cache_backend, cache_migration
from app.utils.cache_backend import CacheStore, DEFAULT_SETTINGS, _SQLiteCache
from app.utils.storage_jobs import StorageJobStore
from tests.test_cache_backends import TemporaryValkey


_BACKENDS = ("memory", "memory_disk", "redis", "valkey")
_DIRECTIONS = tuple((source, target) for source, target in
                    itertools.permutations(_BACKENDS, 2))


class _Config:
    def __init__(self, directory, cache):
        self.directory = str(directory)
        self.config = {"cache": dict(cache)}
        self.job_store = None
        self.saved_states = []

    def get_config_path(self):
        return self.directory

    def get_config(self, node=None):
        return self.config if node is None else self.config.get(node)

    def save_config(self, config):
        job = self.job_store.latest("cache_migration") if self.job_store else None
        self.saved_states.append((job.get("status") if job else None,
                                  config.get("cache", {}).get("backend")))
        self.config = config
        return True


@pytest.fixture
def cache_migration_environment(tmp_path, monkeypatch):
    jobs = StorageJobStore(str(tmp_path / ".maintenance" / "storage-jobs.sqlite3"))
    config = _Config(tmp_path, {**DEFAULT_SETTINGS, "backend": "memory"})
    config.job_store = jobs
    monkeypatch.setattr("config.Config", lambda: config)
    monkeypatch.setattr(cache_migration, "STORAGE_JOBS", jobs)
    monkeypatch.setattr(cache_backend, "STORAGE_JOBS", jobs)
    restart_calls = []
    monkeypatch.setattr(cache_migration, "_schedule_restart",
                        lambda: restart_calls.append("scheduled"))
    return config, jobs, restart_calls


@pytest.fixture
def cache_valkey(tmp_path):
    binary = os.environ.get("NASTOOLS_TEST_VALKEY_SERVER") or shutil.which("valkey-server")
    if not binary:
        pytest.skip("未安装 Valkey 测试服务；可设置 NASTOOLS_TEST_VALKEY_SERVER")
    directory = tmp_path / "valkey"
    directory.mkdir()
    server = TemporaryValkey(binary, directory, password="")
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def two_cache_valkey_servers(tmp_path):
    binary = os.environ.get("NASTOOLS_TEST_VALKEY_SERVER") or shutil.which("valkey-server")
    if not binary:
        pytest.skip("未安装 Valkey 测试服务；可设置 NASTOOLS_TEST_VALKEY_SERVER")
    source_directory = tmp_path / "source-server"
    target_directory = tmp_path / "target-server"
    source_directory.mkdir()
    target_directory.mkdir()
    source = TemporaryValkey(binary, source_directory, password="")
    target = TemporaryValkey(binary, target_directory, password="")
    try:
        yield source, target
    finally:
        source.stop()
        target.stop()


def _remote_settings(backend, server):
    return {"backend": backend, "host": "127.0.0.1", "port": server.port, "db": 0,
            "auth_mode": "none", "username": "", "password": "",
            "connect_timeout": 0.5, "retry_interval": 1}


@pytest.mark.parametrize(("source_backend", "target_backend"), _DIRECTIONS)
def test_all_directed_cache_backend_migrations_preserve_data_and_restart_order(
        source_backend, target_backend, tmp_path, monkeypatch, request, cache_valkey,
        cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": source_backend,
                       "storage_generation": "source-generation" if source_backend in
                       {"redis", "valkey"} else ""}
    if source_backend in {"redis", "valkey"}:
        source_settings.update(_remote_settings(source_backend, cache_valkey))
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    stores = [source_store]
    request.addfinalizer(lambda: [store.close() for store in stores])
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()

    expires = time.time() + 3600
    source_store.put("recognition_tmdb", "matrix-key", {"标题": "电影 🎬"}, expires)
    source_store.put("tmdb_supply", ("tuple", 1), b"portable-payload")
    expected_backend = source_store.status()["backend"]
    assert expected_backend == source_backend

    submitted = {"backend": target_backend}
    if target_backend in {"redis", "valkey"}:
        submitted.update(_remote_settings(target_backend, cache_valkey))
    checked = cache_migration.check_cache_storage(submitted)
    assert "code" not in checked, checked
    assert {item["name"] for item in checked["excluded"]} == {
        "TokenCache", "ConfigLoadCache"}
    check_id = checked["check_id"]
    assert config.config["cache"]["backend"] == source_backend
    if source_backend == "memory" and target_backend == "memory_disk":
        assert not (tmp_path / "cache" / "system.sqlite3").exists()

    started = cache_migration.start_cache_migration({**submitted, "check_id": check_id})
    assert started["code"] == 0, started
    job_id = started["job_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job["status"] in {"RESTART_PENDING", "FAILED", "RECOVERY_REQUIRED"}:
            break
        time.sleep(0.02)
    worker = cache_migration._WORKERS.get(job_id)
    if worker:
        worker.join(timeout=5)
    job = jobs.get(job_id)
    assert job["status"] == "RESTART_PENDING", job
    assert config.saved_states == [("SAVING_CONFIG", target_backend)]
    assert restart_calls == ["scheduled"]
    assert job["result"]["expected"]["recognition_tmdb"][0] == 1
    assert config.config["cache"]["backend"] == target_backend

    source_store.close()
    target_settings = dict(config.config["cache"])
    target_store = CacheStore(path=str(tmp_path / "cache"), settings=target_settings)
    stores.append(target_store)
    monkeypatch.setattr(cache_migration, "STORE", target_store)
    monkeypatch.setattr(cache_backend, "STORE", target_store)
    cache_migration.verify_cache_startup()

    completed = jobs.get(job_id)
    assert completed["status"] == "SUCCEEDED"
    assert target_store.status()["backend"] == target_backend
    restored = target_store.get("recognition_tmdb", "matrix-key")
    assert restored == ("matrix-key", {"标题": "电影 🎬"}, expires)
    assert target_store.get("tmdb_supply", ("tuple", 1))[1] == b"portable-payload"
    assert jobs.maintenance_gate("cache") is None
    assert not target_store._frozen
    if target_store._settings["backend"] in {"redis", "valkey"}:
        assert target_store._settings["storage_generation"] == job_id

@pytest.mark.parametrize("backend", ("redis", "valkey"))
def test_remote_migration_to_a_second_instance_uses_a_new_isolated_generation(
        backend, tmp_path, monkeypatch, request, two_cache_valkey_servers,
        cache_migration_environment):
    source_server, target_server = two_cache_valkey_servers
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, **_remote_settings(backend, source_server),
                       "storage_generation": "source-generation"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    stores = [source_store]
    request.addfinalizer(lambda: [store.close() for store in stores])
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-only", "preserved")

    submitted = {**_remote_settings(backend, target_server)}
    checked = cache_migration.check_cache_storage(submitted)
    assert "code" not in checked, checked
    started = cache_migration.start_cache_migration({**submitted,
                                                       "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    job_id = started["job_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job["status"] in {"RESTART_PENDING", "FAILED", "RECOVERY_REQUIRED"}:
            break
        time.sleep(0.02)
    worker = cache_migration._WORKERS.get(job_id)
    if worker:
        worker.join(timeout=5)
    assert jobs.get(job_id)["status"] == "RESTART_PENDING", jobs.get(job_id)

    source_store.close()
    target_store = CacheStore(path=str(tmp_path / "cache"),
                              settings=dict(config.config["cache"]))
    stores.append(target_store)
    monkeypatch.setattr(cache_migration, "STORE", target_store)
    monkeypatch.setattr(cache_backend, "STORE", target_store)
    cache_migration.verify_cache_startup()

    assert jobs.get(job_id)["status"] == "SUCCEEDED"
    assert target_store.get("recognition_tmdb", "source-only")[1] == "preserved"
    assert target_store._settings["storage_generation"] == job_id
    assert source_server.client().keys("nastool:cache:v2:*")
    target_keys = target_server.client().keys("nastool:cache:v2:*")
    assert target_keys
    assert all(job_id.encode() in key for key in target_keys)
    assert restart_calls == ["scheduled"]


def test_migration_reports_entries_expiring_after_snapshot_and_keeps_large_ai_payload(
        tmp_path, monkeypatch, request, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    stores = [source_store]
    request.addfinalizer(lambda: [store.close() for store in stores])
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()

    expiring_key = "expires-after-snapshot"
    source_store.put("recognition_tmdb", expiring_key, {"title": "temporary"},
                     time.time() + 3600)
    large_response = bytes(1024 * 1024 + 1)
    source_store.put("recognition_parse", "large-ai-response", {"response": large_response})

    original_manifest = cache_migration._write_snapshot_manifest

    def expire_snapshot_entry(job_id, snapshot_path, source_status):
        connection = sqlite3.connect(snapshot_path)
        try:
            row = connection.execute(
                "SELECT payload FROM cache_entries_v2 WHERE namespace=? AND cache_key=?",
                ("recognition_tmdb", CacheStore._key(expiring_key))).fetchone()
            assert row
            record = _SQLiteCache._decode(row[0])
            expired = (record[0], record[1], time.time() - 1)
            connection.execute(
                "UPDATE cache_entries_v2 SET payload=?, expires=? "
                "WHERE namespace=? AND cache_key=?",
                (_SQLiteCache._encode(expired), expired[2], "recognition_tmdb",
                 CacheStore._key(expiring_key)))
            connection.commit()
        finally:
            connection.close()
        return original_manifest(job_id, snapshot_path, source_status)

    monkeypatch.setattr(cache_migration, "_write_snapshot_manifest", expire_snapshot_entry)
    checked = cache_migration.check_cache_storage({"backend": "memory_disk"})
    assert "code" not in checked, checked
    started = cache_migration.start_cache_migration({"backend": "memory_disk",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    job_id = started["job_id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = jobs.get(job_id)
        if job["status"] in {"RESTART_PENDING", "FAILED", "RECOVERY_REQUIRED"}:
            break
        time.sleep(0.02)
    worker = cache_migration._WORKERS.get(job_id)
    if worker:
        worker.join(timeout=5)
    job = jobs.get(job_id)
    assert job["status"] == "RESTART_PENDING", job
    assert job["result"]["expired_during_migration"] == 1
    assert job["result"]["expected"]["recognition_tmdb"][0] == 0
    assert job["result"]["expected"]["recognition_parse"][0] == 1
    assert config.saved_states == [("SAVING_CONFIG", "memory_disk")]

    source_store.close()
    target_store = CacheStore(path=str(tmp_path / "cache"),
                              settings=dict(config.config["cache"]))
    stores.append(target_store)
    monkeypatch.setattr(cache_migration, "STORE", target_store)
    monkeypatch.setattr(cache_backend, "STORE", target_store)
    cache_migration.verify_cache_startup()
    assert target_store.get("recognition_tmdb", expiring_key) is None
    restored_ai = target_store.get("recognition_parse", "large-ai-response")
    assert restored_ai == ("large-ai-response", {"response": large_response}, None)
    assert jobs.get(job_id)["status"] == "SUCCEEDED"
    assert restart_calls == ["scheduled"]


def test_config_save_failure_rolls_back_sqlite_target_and_unfreezes_source(
        tmp_path, monkeypatch, request, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    request.addfinalizer(source_store.close)
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-data", {"title": "keep"})

    checked = cache_migration.check_cache_storage({"backend": "memory_disk"})
    assert "code" not in checked, checked

    def fail_save(_config):
        raise OSError("injected config save failure")

    monkeypatch.setattr(config, "save_config", fail_save)
    started = cache_migration.start_cache_migration({"backend": "memory_disk",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    worker.join(timeout=30)

    job = jobs.get(started["job_id"])
    assert job["status"] == "FAILED", job
    assert config.config["cache"]["backend"] == "memory"
    assert source_store.get("recognition_tmdb", "source-data")[1] == {"title": "keep"}
    assert not source_store.status()["maintenance"]
    assert jobs.maintenance_gate("cache") is None
    assert not (tmp_path / "cache" / "system.sqlite3").exists()
    assert restart_calls == []


def test_restart_request_failure_keeps_committed_configuration_pending_for_retry(
        tmp_path, monkeypatch, request, cache_migration_environment):
    config, jobs, _restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    request.addfinalizer(source_store.close)
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-data", {"title": "keep"})

    checked = cache_migration.check_cache_storage({"backend": "memory_disk"})
    assert "code" not in checked, checked
    restart_attempts = []

    def fail_restart():
        restart_attempts.append("failed")
        raise OSError("injected restart request failure")

    monkeypatch.setattr(cache_migration, "_schedule_restart", fail_restart)
    started = cache_migration.start_cache_migration({"backend": "memory_disk",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    worker.join(timeout=30)

    job = jobs.get(started["job_id"])
    assert job["status"] == "RESTART_PENDING", job
    assert config.config["cache"]["backend"] == "memory_disk"
    assert jobs.maintenance_gate("cache")["job_id"] == started["job_id"]
    assert source_store.status()["maintenance"]
    assert restart_attempts == ["failed"]

    retried = []
    monkeypatch.setattr(cache_migration, "_schedule_restart", lambda: retried.append("scheduled"))
    assert cache_migration.retry_cache_restart(started["job_id"])
    assert retried == ["scheduled"]


def test_cancellation_before_snapshot_preserves_source_and_releases_maintenance_gate(
        tmp_path, monkeypatch, request, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    request.addfinalizer(source_store.close)
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-data", {"title": "keep"})

    checked = cache_migration.check_cache_storage({"backend": "memory_disk"})
    assert "code" not in checked, checked
    monkeypatch.setattr(jobs, "cancel_requested", lambda _job_id: True)
    started = cache_migration.start_cache_migration({"backend": "memory_disk",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    worker.join(timeout=30)

    job = jobs.get(started["job_id"])
    assert job["status"] == "CANCELLED", job
    assert config.config["cache"]["backend"] == "memory"
    assert source_store.get("recognition_tmdb", "source-data")[1] == {"title": "keep"}
    assert jobs.maintenance_gate("cache") is None
    assert not source_store.status()["maintenance"]
    assert not (tmp_path / "cache" / "system.sqlite3").exists()
    assert restart_calls == []


def test_target_outage_after_check_fails_without_saving_or_fallback_success(
        tmp_path, monkeypatch, request, cache_valkey, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    request.addfinalizer(source_store.close)
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-data", {"title": "keep"})

    submitted = _remote_settings("valkey", cache_valkey)
    checked = cache_migration.check_cache_storage(submitted)
    assert "code" not in checked, checked
    cache_valkey.stop()
    started = cache_migration.start_cache_migration({**submitted,
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    worker.join(timeout=30)

    job = jobs.get(started["job_id"])
    assert job["status"] == "FAILED", job
    assert config.config["cache"]["backend"] == "memory"
    assert source_store.get("recognition_tmdb", "source-data")[1] == {"title": "keep"}
    assert jobs.maintenance_gate("cache") is None
    assert not source_store.status()["maintenance"]
    assert restart_calls == []


def test_source_outage_after_check_fails_without_committing_fallback_memory(
        tmp_path, monkeypatch, request, cache_valkey, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, **_remote_settings("valkey", cache_valkey),
                       "storage_generation": "source-generation"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    request.addfinalizer(source_store.close)
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "remote-data", {"title": "keep"})

    checked = cache_migration.check_cache_storage({"backend": "memory"})
    assert "code" not in checked, checked
    cache_valkey.stop()
    started = cache_migration.start_cache_migration({"backend": "memory",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    worker.join(timeout=30)

    job = jobs.get(started["job_id"])
    assert job["status"] == "FAILED", job
    assert config.config["cache"]["backend"] == "valkey"
    assert source_store.status()["fallback"]
    assert jobs.maintenance_gate("cache") is None
    assert restart_calls == []


def test_snapshot_disk_full_failure_keeps_memory_source_available(
        tmp_path, monkeypatch, request, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    request.addfinalizer(source_store.close)
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-data", {"title": "keep"})

    checked = cache_migration.check_cache_storage({"backend": "memory_disk"})
    assert "code" not in checked, checked

    def fail_snapshot(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(cache_migration, "_write_snapshot_namespace", fail_snapshot)
    started = cache_migration.start_cache_migration({"backend": "memory_disk",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    worker.join(timeout=30)

    job = jobs.get(started["job_id"])
    assert job["status"] == "FAILED", job
    assert "No space left" in job["error"]
    assert config.config["cache"]["backend"] == "memory"
    assert source_store.get("recognition_tmdb", "source-data")[1] == {"title": "keep"}
    assert jobs.maintenance_gate("cache") is None
    assert not source_store.status()["maintenance"]
    assert not (tmp_path / "cache" / "system.sqlite3").exists()
    assert restart_calls == []


def test_target_validation_mismatch_keeps_old_configuration_and_source(
        tmp_path, monkeypatch, request, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    request.addfinalizer(source_store.close)
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-data", {"title": "keep"})

    checked = cache_migration.check_cache_storage({"backend": "memory_disk"})
    assert "code" not in checked, checked
    original_iter_records = CacheStore.iter_records

    def add_unexpected_target_record(store, namespace, strict=False):
        yield from original_iter_records(store, namespace, strict=strict)
        if store is not source_store and store._settings["backend"] == "memory_disk" \
                and namespace == "recognition_tmdb":
            yield "unexpected", {"title": "not in snapshot"}, None

    monkeypatch.setattr(CacheStore, "iter_records", add_unexpected_target_record)
    started = cache_migration.start_cache_migration({"backend": "memory_disk",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    worker.join(timeout=30)

    job = jobs.get(started["job_id"])
    assert job["status"] == "FAILED", job
    assert "目标校验失败" in job["error"]
    assert config.config["cache"]["backend"] == "memory"
    assert source_store.get("recognition_tmdb", "source-data")[1] == {"title": "keep"}
    assert jobs.maintenance_gate("cache") is None
    assert not source_store.status()["maintenance"]
    assert not (tmp_path / "cache" / "system.sqlite3").exists()
    assert restart_calls == []


def test_cache_clear_is_blocked_during_migration_snapshot_and_data_is_preserved(
        tmp_path, monkeypatch, request, cache_migration_environment):
    config, jobs, restart_calls = cache_migration_environment
    source_settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    config.config["cache"] = dict(source_settings)
    source_store = CacheStore(path=str(tmp_path / "cache"), settings=source_settings)
    stores = [source_store]
    request.addfinalizer(lambda: [store.close() for store in stores])
    monkeypatch.setattr(cache_migration, "STORE", source_store)
    monkeypatch.setattr(cache_backend, "STORE", source_store)
    source_store.initialize()
    source_store.put("recognition_tmdb", "source-data", {"title": "keep"})

    checked = cache_migration.check_cache_storage({"backend": "memory_disk"})
    assert "code" not in checked, checked
    entered_snapshot = threading.Event()
    continue_snapshot = threading.Event()
    write_snapshot_namespace = cache_migration._write_snapshot_namespace

    def pause_snapshot(*args, **kwargs):
        if not entered_snapshot.is_set():
            entered_snapshot.set()
            if not continue_snapshot.wait(timeout=15):
                raise TimeoutError("snapshot test pause timed out")
        return write_snapshot_namespace(*args, **kwargs)

    monkeypatch.setattr(cache_migration, "_write_snapshot_namespace", pause_snapshot)
    started = cache_migration.start_cache_migration({"backend": "memory_disk",
                                                     "check_id": checked["check_id"]})
    assert started["code"] == 0, started
    worker = cache_migration._WORKERS.get(started["job_id"])
    assert worker is not None
    try:
        assert entered_snapshot.wait(timeout=10)
        assert jobs.maintenance_gate("cache")["job_id"] == started["job_id"]
        source_store.clear("recognition_tmdb")
    finally:
        continue_snapshot.set()
    worker.join(timeout=30)

    assert jobs.get(started["job_id"])["status"] == "RESTART_PENDING"
    source_store.close()
    target_store = CacheStore(path=str(tmp_path / "cache"),
                              settings=dict(config.config["cache"]))
    stores.append(target_store)
    monkeypatch.setattr(cache_migration, "STORE", target_store)
    monkeypatch.setattr(cache_backend, "STORE", target_store)
    cache_migration.verify_cache_startup()

    assert jobs.get(started["job_id"])["status"] == "SUCCEEDED"
    assert target_store.get("recognition_tmdb", "source-data")[1] == {"title": "keep"}
    assert restart_calls == ["scheduled"]
