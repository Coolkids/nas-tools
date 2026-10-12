"""缓存迁移进程在完整快照边界退出后恢复纯内存源。"""

import multiprocessing
import os
import time
from unittest.mock import patch

import pytest

from app.utils import cache_backend, cache_migration
from app.utils.cache_backend import CacheStore, DEFAULT_SETTINGS
from app.utils.storage_jobs import StorageJobStore


class _Config:
    def __init__(self, directory):
        self.directory = str(directory)
        self.value = {"cache": {**DEFAULT_SETTINGS, "backend": "memory"}}

    def get_config_path(self):
        return self.directory

    def get_config(self, node=None):
        return self.value if node is None else self.value.get(node)

    def save_config(self, value):
        self.value = value
        return True


def _run_until_complete_snapshot(config_path, snapshot_ready):
    config_dir = os.path.abspath(config_path)
    config = _Config(config_dir)
    jobs = StorageJobStore(os.path.join(config_dir, ".maintenance", "storage-jobs.sqlite3"))
    settings = {**DEFAULT_SETTINGS, "backend": "memory"}
    source = CacheStore(path=os.path.join(config_dir, "cache"), settings=settings)
    source.initialize()
    source.put("recognition_tmdb", "process-key", {"value": "restored"})

    with patch("config.Config", lambda: config), \
            patch.object(cache_migration, "STORAGE_JOBS", jobs), \
            patch.object(cache_backend, "STORAGE_JOBS", jobs), \
            patch.object(cache_migration, "STORE", source), \
            patch.object(cache_backend, "STORE", source):
        check = cache_migration.check_cache_storage({"backend": "memory_disk"})
        if check.get("code"):
            raise RuntimeError(check["msg"])
        original_manifest = cache_migration._write_snapshot_manifest

        def pause_after_complete_snapshot(*args, **kwargs):
            result = original_manifest(*args, **kwargs)
            snapshot_ready.set()
            while True:
                time.sleep(1)

        with patch.object(cache_migration, "_write_snapshot_manifest",
                          side_effect=pause_after_complete_snapshot):
            started = cache_migration.start_cache_migration(
                {"backend": "memory_disk", "check_id": check["check_id"]})
            if started.get("code"):
                raise RuntimeError(started["msg"])
            snapshot_ready.wait(30)
            while True:
                time.sleep(1)


def test_killed_cache_migration_restores_memory_source_from_persisted_snapshot(
        tmp_path, monkeypatch):
    config = _Config(tmp_path)
    jobs = StorageJobStore(str(tmp_path / ".maintenance" / "storage-jobs.sqlite3"))
    monkeypatch.setattr("config.Config", lambda: config)
    monkeypatch.setattr(cache_migration, "STORAGE_JOBS", jobs)
    monkeypatch.setattr(cache_backend, "STORAGE_JOBS", jobs)
    parent_store = CacheStore(path=str(tmp_path / "cache"),
                              settings={**DEFAULT_SETTINGS, "backend": "memory"})
    parent_store.initialize()
    monkeypatch.setattr(cache_migration, "STORE", parent_store)
    monkeypatch.setattr(cache_backend, "STORE", parent_store)

    context = multiprocessing.get_context("spawn")
    snapshot_ready = context.Event()
    process = context.Process(target=_run_until_complete_snapshot,
                              args=(str(tmp_path), snapshot_ready))
    process.start()
    try:
        assert snapshot_ready.wait(60), "子进程未写完缓存迁移快照清单"
        database_maintenance = StorageJobStore(
            str(tmp_path / ".maintenance" / "storage-jobs.sqlite3"))
        with pytest.raises(TimeoutError, match="维护锁超时"):
            with database_maintenance.maintenance_lock(exclusive=True, timeout=0.15):
                pytest.fail("缓存迁移持锁期间数据库维护意外进入")
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode != 0

    interrupted = jobs.latest("cache_migration")
    assert interrupted["status"] == "SNAPSHOTTING"
    assert jobs.maintenance_gate("cache")["job_id"] == interrupted["job_id"]
    cache_migration.verify_cache_startup()

    recovered = jobs.get(interrupted["job_id"])
    assert recovered["status"] == "INTERRUPTED"
    assert jobs.maintenance_gate("cache") is None
    assert not parent_store._frozen
    assert parent_store.get("recognition_tmdb", "process-key")[1] == {"value": "restored"}
    parent_store.close()
