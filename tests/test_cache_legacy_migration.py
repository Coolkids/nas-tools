"""旧 CacheLib 文件与迁移标记只能接管一次，清理中断后可安全续跑。"""

import os
import time

import pytest

from app.utils import cache_backend
from app.utils.cache_backend import CacheStore, _DiskCache
from app.utils.storage_jobs import StorageJobStore


def _seed_legacy_cache(path, namespace, key, value):
    files_directory = path / "files" / CacheStore._key(namespace)
    files_directory.mkdir(parents=True)
    legacy = _DiskCache(str(files_directory), threshold=0, default_timeout=0)
    expires = time.time() + 3600
    assert legacy.set(CacheStore._key(key), (key, value, expires),
                      timeout=CacheStore._timeout(expires))

    marker_directory = path / "migrations"
    marker_directory.mkdir(exist_ok=True)
    marker_path = marker_directory / CacheStore._key(namespace)
    marker_path.write_text("imported-by-legacy-version", encoding="utf-8")
    return expires, marker_path


def test_legacy_marker_cleanup_resumes_and_cleared_values_do_not_return(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_backend, "STORAGE_JOBS",
                        StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3")))
    path = tmp_path / "cache"
    namespace = "recognition_tmdb"
    key = "legacy-cache-key"
    value = {"title": "旧缓存", "payload": b"preserve"}
    expires, marker_path = _seed_legacy_cache(path, namespace, key, value)
    third_party_migration = tmp_path / "db_scripts" / "vendor-migration.py"
    third_party_migration.parent.mkdir()
    third_party_migration.write_text("keep", encoding="utf-8")

    real_unlink = os.unlink
    failed_once = False

    def fail_marker_cleanup_once(filename, *args, **kwargs):
        nonlocal failed_once
        if os.fspath(filename) == str(marker_path) and not failed_once:
            failed_once = True
            raise PermissionError("injected migration marker cleanup failure")
        return real_unlink(filename, *args, **kwargs)

    monkeypatch.setattr(cache_backend.os, "unlink", fail_marker_cleanup_once)
    interrupted = CacheStore(str(path), settings={"backend": "memory_disk"})
    try:
        with pytest.raises(PermissionError, match="injected"):
            interrupted.initialize()
    finally:
        interrupted.close()
    assert failed_once
    assert marker_path.is_file()

    monkeypatch.setattr(cache_backend.os, "unlink", real_unlink)
    resumed = CacheStore(str(path), settings={"backend": "memory_disk"})
    try:
        resumed.initialize()
        assert resumed.get(namespace, key) == (key, value, expires)
        assert not (path / "migrations").exists()
        assert third_party_migration.read_text(encoding="utf-8") == "keep"
    finally:
        resumed.close()

    cleared = CacheStore(str(path), settings={"backend": "memory_disk"})
    try:
        cleared.initialize()
        cleared.clear(namespace)
    finally:
        cleared.close()

    restarted = CacheStore(str(path), settings={"backend": "memory_disk"})
    try:
        restarted.initialize()
        assert restarted.load(namespace) == []
        assert not (path / "migrations").exists()
        assert third_party_migration.read_text(encoding="utf-8") == "keep"
    finally:
        restarted.close()
