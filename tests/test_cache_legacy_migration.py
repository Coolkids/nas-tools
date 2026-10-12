"""旧 CacheLib 文件与迁移标记只能接管一次，清理中断后可安全续跑。"""

import os
import hashlib
import pickle
import sqlite3
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


def test_tmdb_dat_imports_to_fixed_cache_and_clear_does_not_resurrect(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_backend, "STORAGE_JOBS",
                        StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3")))
    metadata = {"movie-1": {"id": "1", "title": "旧电影"},
                "empty": {"id": 0, "title": "不导入"}}
    with open(tmp_path / "tmdb.dat", "wb") as stream:
        pickle.dump(metadata, stream)
    cache_path = tmp_path / "cache"

    store = CacheStore(str(cache_path), settings={"backend": "memory_disk"})
    try:
        store.initialize()
        assert store.get("tmdb_metadata", "movie-1")[1] == metadata["movie-1"]
        assert store.get("tmdb_metadata", "empty") is None
        assert CacheStore._is_current_sqlite(str(cache_path / "system.sqlite3"))
        assert not (tmp_path / "system.sqlite3").exists()
        store.clear("tmdb_metadata")
    finally:
        store.close()

    restarted = CacheStore(str(cache_path), settings={"backend": "memory_disk"})
    try:
        restarted.initialize()
        assert restarted.load("tmdb_metadata") == []
        assert (tmp_path / "tmdb.dat").is_file()
    finally:
        restarted.close()


def test_cache_directory_legacy_sqlite_wal_is_imported_into_current_file(
        tmp_path, monkeypatch):
    monkeypatch.setattr(cache_backend, "STORAGE_JOBS",
                        StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3")))
    cache_path = tmp_path / "cache"
    cache_path.mkdir()
    legacy_path = cache_path / "system.sqlite3"
    writer = sqlite3.connect(legacy_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE cache_entries "
                       "(namespace TEXT, key_hash TEXT, payload BLOB, expires REAL)")
        expires = time.time() + 3600
        writer.execute("INSERT INTO cache_entries VALUES (?, ?, ?, ?)",
                       ("recognition_tmdb", "legacy-key",
                        pickle.dumps(("legacy-key", {"title": "旧缓存"})), expires))
        writer.commit()
        assert (cache_path / "system.sqlite3-wal").is_file()

        store = CacheStore(str(cache_path), settings={"backend": "memory_disk"})
        try:
            store.initialize()
            record = store.get("recognition_tmdb", "legacy-key")
            assert record == ("legacy-key", {"title": "旧缓存"}, expires)
            assert CacheStore._is_current_sqlite(str(legacy_path))
            assert (cache_path / ".staging" / "legacy-system.sqlite3").is_file()
        finally:
            store.close()
    finally:
        writer.close()


def test_same_key_conflict_between_legacy_file_and_sqlite_blocks_import(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_backend, "STORAGE_JOBS",
                        StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3")))
    namespace = "recognition_tmdb"
    key = "conflicting-key"
    expires = time.time() + 3600
    files_directory = tmp_path / "cache" / "files" / CacheStore._key(namespace)
    files_directory.mkdir(parents=True)
    old_file_cache = _DiskCache(str(files_directory), threshold=0, default_timeout=0)
    assert old_file_cache.set(CacheStore._key(key), (key, {"title": "文件值"}, expires),
                              timeout=CacheStore._timeout(expires))
    root_database = tmp_path / "system.sqlite3"
    with sqlite3.connect(root_database) as connection:
        connection.execute("CREATE TABLE cache_entries "
                           "(namespace TEXT, key_hash TEXT, payload BLOB, expires REAL)")
        connection.execute("INSERT INTO cache_entries VALUES (?, ?, ?, ?)",
                           (namespace, CacheStore._key(key),
                            pickle.dumps((key, {"title": "SQLite 值"})), expires))
    root_hash = hashlib.sha256(root_database.read_bytes()).hexdigest()
    store = CacheStore(str(tmp_path / "cache"), settings={"backend": "memory_disk"})
    try:
        with pytest.raises(ValueError, match="多个旧来源中存在冲突"):
            store.initialize()
    finally:
        store.close()

    assert root_database.is_file()
    assert hashlib.sha256(root_database.read_bytes()).hexdigest() == root_hash
    assert list(files_directory.iterdir())
    target = tmp_path / "cache" / "system.sqlite3"
    if target.exists():
        with sqlite3.connect(target) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM cache_entries_v2 WHERE namespace=?",
                (namespace,)).fetchone() == (0,)


def test_corrupt_legacy_file_record_blocks_cache_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(cache_backend, "STORAGE_JOBS",
                        StorageJobStore(str(tmp_path / ".maintenance" / "jobs.sqlite3")))
    path = tmp_path / "cache"
    namespace = "recognition_tmdb"
    _seed_legacy_cache(path, namespace, "valid-key", {"title": "keep"})
    files_directory = path / "files" / CacheStore._key(namespace)
    legacy_filename = next(files_directory.iterdir())
    legacy_filename.write_bytes(b"not a CacheLib entry")
    store = CacheStore(str(path), settings={"backend": "memory_disk"})

    try:
        with pytest.raises(ValueError, match="无法完整读取旧文件缓存"):
            store.initialize()
    finally:
        store.close()

    assert legacy_filename.read_bytes() == b"not a CacheLib entry"
    target = path / "system.sqlite3"
    if target.exists():
        with sqlite3.connect(target) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM cache_entries_v2 WHERE namespace=?",
                (namespace,)).fetchone() == (0,)
