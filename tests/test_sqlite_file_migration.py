"""旧双库在隔离目录中升级并合并到固定单库。"""

import hashlib
import multiprocessing
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import URL, create_engine, text

from app.db.models import Base
from app.db.schema import LOGICAL_DOMAIN_TABLES, current_schema_revision
from app.db.sqlite_files import (finalize_sqlite_migration, prepare_sqlite_database,
                                 _upgrade_legacy_media_snapshot)
from app.utils.storage_jobs import StorageJobStore


_MEDIA_BASE_REVISION = "b3c4d5e6f7a8"


class _Config:
    def __init__(self, directory):
        self.directory = str(directory)

    def get_config_path(self):
        return self.directory

    @staticmethod
    def get_config(node=None):
        return {"type": "sqlite"} if node == "database" else {}


def _create_user_database(path, user_id=17):
    engine = create_engine(URL.create("sqlite", database=str(path)))
    main_tables = [Base.metadata.tables[name]
                   for name in LOGICAL_DOMAIN_TABLES["main"]]
    try:
        Base.metadata.create_all(engine, tables=main_tables)
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            connection.execute(text("INSERT INTO alembic_version(version_num) VALUES (:version)"),
                               {"version": _MEDIA_BASE_REVISION})
            connection.execute(text("""INSERT INTO CONFIG_USERS
                (ID, NAME, PASSWORD, PRIS) VALUES (:user_id, '管理员', 'hash', 'all')"""),
                               {"user_id": user_id})
    finally:
        engine.dispose()


def _create_media_database(path):
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            CREATE TABLE MEDIASYNC_ITEMS (
                ID INTEGER PRIMARY KEY, SERVER TEXT, ITEM_ID TEXT, ITEM_TYPE TEXT,
                TITLE TEXT, LIBRARY TEXT, ORGIN_TITLE TEXT, YEAR TEXT, TMDBID TEXT,
                IMDBID TEXT, PATH TEXT, NOTE TEXT, JSON TEXT
            );
            CREATE TABLE MEDIASYNC_STATISTICS (
                ID INTEGER PRIMARY KEY, SERVER TEXT, TOTAL_COUNT TEXT,
                MOVIE_COUNT TEXT, TV_COUNT TEXT, UPDATE_TIME TEXT
            );
            INSERT INTO MEDIASYNC_ITEMS
                (ID, SERVER, ITEM_ID, ITEM_TYPE, TITLE, PATH, JSON)
                VALUES (31, 'emby', 'movie-31', 'Movie', '旧电影', '/movie/31', '{"id":31}');
            INSERT INTO MEDIASYNC_ITEMS
                (ID, SERVER, ITEM_ID, ITEM_TYPE, TITLE, PATH, JSON)
                VALUES (32, 'emby', 'series-32', 'Series', '旧剧集', '/series/32', '{"id":32}');
        """)
        connection.commit()
    finally:
        connection.close()


def _advance_media_database_to_revision(path, revision):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES (?)", (_MEDIA_BASE_REVISION,))
        connection.commit()
    finally:
        connection.close()
    if revision == _MEDIA_BASE_REVISION:
        return

    engine = create_engine(URL.create("sqlite", database=str(path)))
    try:
        config = AlembicConfig()
        config.set_main_option("script_location", str(Path(__file__).parents[1] / "db_scripts"))
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def _configure_migration(config_dir, monkeypatch):
    jobs = StorageJobStore(str(config_dir / ".maintenance" / "jobs.sqlite3"))
    monkeypatch.setattr("config.Config", lambda: _Config(config_dir))
    monkeypatch.setattr("app.db.sqlite_files.STORAGE_JOBS", jobs)
    return config_dir / "db" / "nastools.db"


def _prepare_migration_process(config_path, start, results):
    from app.db.sqlite_files import prepare_sqlite_database

    config_dir = Path(config_path)
    jobs = StorageJobStore(str(config_dir / ".maintenance" / "jobs.sqlite3"))
    with patch("config.Config", lambda: _Config(config_dir)), \
            patch("app.db.sqlite_files.STORAGE_JOBS", jobs):
        start.wait(10)
        try:
            results.put(("ok", prepare_sqlite_database()))
        except Exception as error:
            results.put(("error", repr(error)))


def _interrupt_after_legacy_snapshot(config_path, snapshot_ready):
    import time

    import app.db.sqlite_files as sqlite_files

    config_dir = Path(config_path)
    jobs = StorageJobStore(str(config_dir / ".maintenance" / "jobs.sqlite3"))
    original_copy_snapshot = sqlite_files._copy_snapshot

    def pause_after_snapshot(source, target):
        original_copy_snapshot(source, target)
        snapshot_ready.set()
        while True:
            time.sleep(1)

    with patch("config.Config", lambda: _Config(config_dir)), \
            patch("app.db.sqlite_files.STORAGE_JOBS", jobs), \
            patch.object(sqlite_files, "_copy_snapshot", side_effect=pause_after_snapshot):
        prepare_sqlite_database()


def _interrupt_during_legacy_archive(config_path, moved_source):
    import time

    import app.db.sqlite_files as sqlite_files

    config_dir = Path(config_path)
    jobs = StorageJobStore(str(config_dir / ".maintenance" / "jobs.sqlite3"))
    original_replace = sqlite_files.os.replace

    def pause_after_source_move(source, target):
        original_replace(source, target)
        if Path(source).name in {"user.db", "media.db"}:
            moved_source.set()
            while True:
                time.sleep(1)

    with patch("config.Config", lambda: _Config(config_dir)), \
            patch("app.db.sqlite_files.STORAGE_JOBS", jobs), \
            patch.object(sqlite_files.os, "replace", side_effect=pause_after_source_move):
        finalize_sqlite_migration()


@pytest.mark.parametrize("layout", ("root", "db"))
def test_old_main_and_media_databases_merge_into_fixed_single_database(
        layout, tmp_path, monkeypatch):
    config_dir = tmp_path
    source_dir = config_dir if layout == "root" else config_dir / "db"
    source_dir.mkdir(exist_ok=True)
    user_path = source_dir / "user.db"
    media_path = source_dir / "media.db"
    _create_user_database(user_path, user_id=31)
    _create_media_database(media_path)
    user_hash = hashlib.sha256(user_path.read_bytes()).hexdigest()
    media_hash = hashlib.sha256(media_path.read_bytes()).hexdigest()
    target_path = _configure_migration(config_dir, monkeypatch)

    assert prepare_sqlite_database() == str(target_path)

    assert target_path.is_file()
    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT ID, NAME FROM CONFIG_USERS WHERE ID=31").fetchone() == (31, "管理员")
        assert connection.execute("""SELECT ID, ITEM_ID, GENERATION_ID
            FROM MEDIASYNC_ITEMS ORDER BY ID""").fetchall() == [
            (31, "movie-31", "legacy"), (32, "series-32", "legacy")]
        assert connection.execute("""SELECT SERVER, ACTIVE_GENERATION,
            TOTAL_COUNT, MOVIE_COUNT, TV_COUNT FROM MEDIASYNC_STATISTICS""").fetchone() == (
                "emby", "legacy", "2", "1", "1")
        assert connection.execute(
            "SELECT value FROM __nastools_database_layout WHERE key='merge_status'").fetchone() == (
                "complete",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        connection.execute("INSERT INTO CONFIG_USERS (NAME, PASSWORD, PRIS) "
                           "VALUES ('迁移后用户', 'hash-new', 'all')")
        new_user_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.execute("INSERT INTO MEDIASYNC_ITEMS "
                           "(SERVER, ITEM_ID, ITEM_TYPE, TITLE) "
                           "VALUES ('emby', 'after-migration', 'Movie', '迁移后媒体记录')")
        new_media_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
        connection.commit()
        assert new_user_id > 31
        assert new_media_id > 32

    # A subsequent startup sees the completed single database and does not
    # re-import either legacy source over writes made after publication.
    assert prepare_sqlite_database() == str(target_path)
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT NAME FROM CONFIG_USERS WHERE ID=?",
                                  (new_user_id,)).fetchone() == ("迁移后用户",)
        assert connection.execute("SELECT ITEM_ID FROM MEDIASYNC_ITEMS WHERE ID=?",
                                  (new_media_id,)).fetchone() == ("after-migration",)

    assert user_path.is_file()
    assert media_path.is_file()
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == user_hash
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == media_hash


@pytest.mark.parametrize("revision", (
    "b3c4d5e6f7a8", "f2a3b4c5d6e7", "f3a4b5c6d7e8", "f4a5b6c7d8e9",
))
def test_each_supported_legacy_media_revision_upgrades_without_losing_rows(
        tmp_path, revision):
    database_path = tmp_path / f"legacy-media-{revision}.sqlite3"
    _create_media_database(database_path)
    _advance_media_database_to_revision(database_path, revision)

    _upgrade_legacy_media_snapshot(str(database_path), str(tmp_path))

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            current_schema_revision(),)
        assert connection.execute(
            "SELECT ID, ITEM_ID, TITLE, GENERATION_ID FROM MEDIASYNC_ITEMS ORDER BY ID"
        ).fetchall() == [
            (31, "movie-31", "旧电影", "legacy"),
            (32, "series-32", "旧剧集", "legacy"),
        ]
        assert connection.execute(
            "SELECT SERVER, ACTIVE_GENERATION, TOTAL_COUNT FROM MEDIASYNC_STATISTICS"
        ).fetchone() == ("emby", "legacy", "2")
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name='MEDIASYNC_SYNC_JOBS'").fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.parametrize("revision", ("f3a4b5c6d7e8", "f4a5b6c7d8e9"))
def test_merge_drops_stale_sync_leases_from_supported_legacy_media_revisions(
        tmp_path, monkeypatch, revision):
    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    _advance_media_database_to_revision(media_path, revision)
    with sqlite3.connect(media_path) as connection:
        connection.execute("""INSERT INTO MEDIASYNC_SYNC_JOBS
            (SERVER, JOB_ID, GENERATION_ID, LEASE_TOKEN, LEASE_UNTIL,
             EXPECTED_ACTIVE_GENERATION, STATUS, CREATED_AT, UPDATED_AT)
            VALUES ('emby', 'stale-job', 'stale-generation', 1, 1.0,
                    'legacy', 'RUNNING', 1.0, 1.0)""")

    target_path = _configure_migration(tmp_path, monkeypatch)
    assert prepare_sqlite_database() == str(target_path)

    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT count(*) FROM MEDIASYNC_ITEMS").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM MEDIASYNC_SYNC_JOBS").fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_unknown_media_sync_lease_schema_is_rejected_without_mutating_source(tmp_path):
    database_path = tmp_path / "legacy-media-with-unknown-lease-schema.sqlite3"
    _create_media_database(database_path)
    _advance_media_database_to_revision(database_path, "f3a4b5c6d7e8")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE MEDIASYNC_SYNC_JOBS ADD COLUMN UNKNOWN_FIELD TEXT")

    original_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="同步租约表结构不匹配"):
        _upgrade_legacy_media_snapshot(str(database_path), str(tmp_path))

    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == original_hash


def test_main_database_without_legacy_media_database_migrates(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    target_path = _configure_migration(tmp_path, monkeypatch)

    assert prepare_sqlite_database() == str(target_path)

    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT ID, NAME FROM CONFIG_USERS WHERE ID=17").fetchone() == (17, "管理员")
        assert connection.execute(
            "SELECT value FROM __nastools_database_layout WHERE key='merge_status'").fetchone() == (
                "complete",)


def test_committed_wal_data_is_included_in_legacy_snapshot(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    user_writer = sqlite3.connect(user_path)
    media_writer = sqlite3.connect(media_path)
    try:
        user_writer.execute("PRAGMA journal_mode=WAL")
        user_writer.execute("UPDATE CONFIG_USERS SET NAME='WAL 管理员' WHERE ID=17")
        user_writer.commit()
        media_writer.execute("PRAGMA journal_mode=WAL")
        media_writer.execute("UPDATE MEDIASYNC_ITEMS SET TITLE='WAL 媒体记录' WHERE ID=31")
        media_writer.commit()
        assert (tmp_path / "user.db-wal").is_file()
        assert (tmp_path / "media.db-wal").is_file()

        target_path = _configure_migration(tmp_path, monkeypatch)
        assert prepare_sqlite_database() == str(target_path)
    finally:
        user_writer.close()
        media_writer.close()

    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT NAME FROM CONFIG_USERS WHERE ID=17").fetchone() == ("WAL 管理员",)
        assert connection.execute(
            "SELECT TITLE FROM MEDIASYNC_ITEMS WHERE ID=31").fetchone() == ("WAL 媒体记录",)


def test_media_database_without_main_database_is_rejected(tmp_path, monkeypatch):
    media_path = tmp_path / "media.db"
    _create_media_database(media_path)
    target_path = _configure_migration(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="缺少主数据库"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert media_path.is_file()


def test_database_directory_occupied_by_file_does_not_touch_legacy_source(
        tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    database_dir = tmp_path / "db"
    database_dir.write_text("keep this file", encoding="utf-8")
    target_path = _configure_migration(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="普通文件占用"):
        prepare_sqlite_database()

    assert database_dir.read_text(encoding="utf-8") == "keep this file"
    assert user_path.is_file()
    assert not target_path.exists()


def test_insufficient_disk_space_preserves_legacy_source(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    source_hash = hashlib.sha256(user_path.read_bytes()).hexdigest()
    target_path = _configure_migration(tmp_path, monkeypatch)
    monkeypatch.setattr(sqlite_files.shutil, "disk_usage",
                        lambda _path: SimpleNamespace(free=0))

    with pytest.raises(RuntimeError, match="可用空间不足"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hash


def test_invalid_legacy_media_signature_preserves_both_sources(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    with sqlite3.connect(media_path) as connection:
        connection.executescript("""
            CREATE TABLE MEDIASYNC_ITEMS (ID INTEGER PRIMARY KEY, ITEM_ID TEXT);
            CREATE TABLE MEDIASYNC_STATISTICS (
                ID INTEGER PRIMARY KEY, SERVER TEXT, TOTAL_COUNT TEXT,
                MOVIE_COUNT TEXT, TV_COUNT TEXT, UPDATE_TIME TEXT
            );
        """)
    source_hashes = (hashlib.sha256(user_path.read_bytes()).hexdigest(),
                     hashlib.sha256(media_path.read_bytes()).hexdigest())
    target_path = _configure_migration(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="结构指纹不匹配"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hashes[0]
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == source_hashes[1]


def test_legacy_sources_in_both_locations_are_rejected(tmp_path, monkeypatch):
    database_dir = tmp_path / "db"
    database_dir.mkdir()
    root_user = tmp_path / "user.db"
    nested_media = database_dir / "media.db"
    _create_user_database(root_user)
    _create_media_database(nested_media)
    target_path = _configure_migration(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="来源冲突"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert root_user.is_file()
    assert nested_media.is_file()


def test_corrupt_legacy_main_database_is_preserved_and_not_published(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    user_path.write_bytes(b"not a SQLite database")
    target_path = _configure_migration(tmp_path, monkeypatch)

    with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert user_path.read_bytes() == b"not a SQLite database"


def test_same_named_legacy_view_is_rejected_without_publishing_or_mutating_source(
        tmp_path, monkeypatch):
    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    with sqlite3.connect(user_path) as connection:
        connection.execute("DROP TABLE CONFIG_USERS")
        connection.execute("CREATE VIEW CONFIG_USERS AS "
                           "SELECT 17 AS ID, 'view user' AS NAME")
    source_hash = hashlib.sha256(user_path.read_bytes()).hexdigest()
    target_path = _configure_migration(tmp_path, monkeypatch)
    copy_domain = sqlite_files._copy_domain
    reached_import = []

    def check_rejected_before_domain_copy(*args, **kwargs):
        reached_import.append(True)
        return copy_domain(*args, **kwargs)

    monkeypatch.setattr(sqlite_files, "_copy_domain", check_rejected_before_domain_copy)
    with pytest.raises(RuntimeError, match="与应用表同名的视图"):
        prepare_sqlite_database()

    assert reached_import == [True]
    assert not target_path.exists()
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hash


def test_unmarked_target_and_legacy_sources_are_rejected(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    target_path = _configure_migration(tmp_path, monkeypatch)
    target_path.parent.mkdir(parents=True)
    engine = create_engine(URL.create("sqlite", database=str(target_path)))
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="没有完成记录"):
        prepare_sqlite_database()

    assert user_path.is_file()
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM CONFIG_USERS").fetchone() == (0,)


def test_repeated_startup_does_not_reimport_or_mutate_published_database(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    _create_media_database(tmp_path / "media.db")
    target_path = _configure_migration(tmp_path, monkeypatch)
    assert prepare_sqlite_database() == str(target_path)
    with sqlite3.connect(target_path) as connection:
        connection.execute("INSERT INTO CONFIG_USERS (ID, NAME, PASSWORD, PRIS) VALUES "
                           "(18, '新用户', 'hash-2', 'all')")
        connection.commit()
    expected_hash = hashlib.sha256(target_path.read_bytes()).hexdigest()

    assert prepare_sqlite_database() == str(target_path)

    assert hashlib.sha256(target_path.read_bytes()).hexdigest() == expected_hash
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM CONFIG_USERS").fetchone() == (2,)


def test_failure_while_copying_second_domain_does_not_publish_partial_database(
        tmp_path, monkeypatch):
    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    source_hashes = (hashlib.sha256(user_path.read_bytes()).hexdigest(),
                     hashlib.sha256(media_path.read_bytes()).hexdigest())
    target_path = _configure_migration(tmp_path, monkeypatch)
    copy_domain = sqlite_files._copy_domain

    def fail_after_second_domain_copy(connection, source_path, domain, expected_tables,
                                      excluded_tables=(), attached_sources=None):
        copy_domain(connection, source_path, domain, expected_tables,
                    excluded_tables=excluded_tables, attached_sources=attached_sources)
        if domain == "media":
            raise RuntimeError("injected failure after media import")

    monkeypatch.setattr(sqlite_files, "_copy_domain", fail_after_second_domain_copy)
    with pytest.raises(RuntimeError, match="injected failure"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hashes[0]
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == source_hashes[1]


def test_read_only_legacy_sources_can_be_snapshotted_without_rewriting_them(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    user_path.chmod(0o444)
    media_path.chmod(0o444)
    hashes = (hashlib.sha256(user_path.read_bytes()).hexdigest(),
              hashlib.sha256(media_path.read_bytes()).hexdigest())
    target_path = _configure_migration(tmp_path, monkeypatch)

    assert prepare_sqlite_database() == str(target_path)

    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == hashes[0]
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == hashes[1]
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("SELECT NAME FROM CONFIG_USERS WHERE ID=17").fetchone() \
            == ("管理员",)
        assert connection.execute("SELECT count(*) FROM MEDIASYNC_ITEMS").fetchone() == (2,)


def test_retired_brush_downloader_data_remains_in_legacy_archive(tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    with sqlite3.connect(user_path) as connection:
        connection.execute("""CREATE TABLE SITE_BRUSH_DOWNLOADERS (
            ID INTEGER PRIMARY KEY, NAME TEXT, TYPE TEXT, HOST TEXT, PORT TEXT,
            USERNAME TEXT, PASSWORD TEXT, SAVE_DIR TEXT, NOTE TEXT)""")
        connection.execute("""INSERT INTO SITE_BRUSH_DOWNLOADERS
            (ID, NAME, TYPE, HOST, PORT, USERNAME, PASSWORD, SAVE_DIR, NOTE)
            VALUES (1, 'legacy-test', 'test', 'localhost', '1', 'user', 'secret',
                    '/archive-only', 'preserved')""")
    target_path = _configure_migration(tmp_path, monkeypatch)

    assert prepare_sqlite_database() == str(target_path)
    finalize_sqlite_migration()

    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='SITE_BRUSH_DOWNLOADERS'").fetchone() == (0,)
    archive_user = next((tmp_path / "db" / ".legacy-archive").glob(
        "legacy-merge-*/user.db"))
    with sqlite3.connect(archive_user) as connection:
        assert connection.execute(
            "SELECT NAME, PASSWORD, SAVE_DIR, NOTE FROM SITE_BRUSH_DOWNLOADERS "
            "WHERE ID=1").fetchone() == (
                "legacy-test", "secret", "/archive-only", "preserved")


def test_merge_discards_retired_rss_sites_column_and_keeps_tv_subscription(
        tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    with sqlite3.connect(user_path) as connection:
        connection.execute("ALTER TABLE RSS_TVS ADD COLUMN RSS_SITES TEXT")
        connection.execute("""INSERT INTO RSS_TVS
            (ID, NAME, YEAR, TMDBID, STATE, RSS_SITES)
            VALUES (92, 'migration-test-tv', '2025', 'tmdb-migration-test', 'R',
                    '["retired-site-selection"]')""")
    target_path = _configure_migration(tmp_path, monkeypatch)

    assert prepare_sqlite_database() == str(target_path)

    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT ID, NAME, STATE FROM RSS_TVS WHERE ID=92").fetchone() == (
                92, "migration-test-tv", "R")
        assert "RSS_SITES" not in {
            row[1].upper() for row in connection.execute("PRAGMA table_info(RSS_TVS)")}


def test_merge_digest_accounts_for_legacy_integer_to_text_affinity(tmp_path, monkeypatch):
    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    with sqlite3.connect(user_path) as connection:
        connection.execute("ALTER TABLE SEARCH_RESULT_INFO RENAME TO search_result_info_new")
        connection.execute("CREATE TABLE SEARCH_RESULT_INFO (ID INTEGER PRIMARY KEY, SIZE INTEGER)")
        connection.execute("INSERT INTO SEARCH_RESULT_INFO (ID, SIZE) VALUES (1, 123)")
        connection.execute("DROP TABLE search_result_info_new")
    monkeypatch.setattr(sqlite_files, "_upgrade_snapshot", lambda *_args, **_kwargs: None)
    target_path = _configure_migration(tmp_path, monkeypatch)

    assert prepare_sqlite_database() == str(target_path)

    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT SIZE, typeof(SIZE) FROM SEARCH_RESULT_INFO WHERE ID=1").fetchone() \
            == ("123", "text")


def test_process_interruption_leaves_no_partial_target_and_next_startup_recovers(
        tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    target_path = tmp_path / "db" / "nastools.db"
    _configure_migration(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    snapshot_ready = context.Event()
    process = context.Process(target=_interrupt_after_legacy_snapshot,
                              args=(str(tmp_path), snapshot_ready))
    process.start()
    try:
        assert snapshot_ready.wait(60), "子进程未到达旧库快照边界"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode != 0
    assert not target_path.exists()
    orphan_staging = list((tmp_path / "db" / ".staging").glob("legacy-merge-*"))
    assert len(orphan_staging) == 1

    assert prepare_sqlite_database() == str(target_path)

    assert not orphan_staging[0].exists()
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT NAME FROM CONFIG_USERS WHERE ID=17").fetchone() \
            == ("管理员",)


def test_completion_marker_failure_rolls_back_both_domains_and_preserves_sources(
        tmp_path, monkeypatch):
    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    source_hashes = (hashlib.sha256(user_path.read_bytes()).hexdigest(),
                     hashlib.sha256(media_path.read_bytes()).hexdigest())
    target_path = _configure_migration(tmp_path, monkeypatch)
    write_completion = sqlite_files._write_completion

    def fail_after_completion_record(connection, job_id, sources):
        write_completion(connection, job_id, sources)
        raise RuntimeError("injected completion marker failure")

    monkeypatch.setattr(sqlite_files, "_write_completion", fail_after_completion_record)
    with pytest.raises(RuntimeError, match="completion marker failure"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert not list((tmp_path / "db" / ".staging").glob("legacy-merge-*"))
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hashes[0]
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == source_hashes[1]


def test_legacy_schema_conversion_failure_preserves_original_databases(
        tmp_path, monkeypatch):
    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    source_hashes = (hashlib.sha256(user_path.read_bytes()).hexdigest(),
                     hashlib.sha256(media_path.read_bytes()).hexdigest())
    target_path = _configure_migration(tmp_path, monkeypatch)

    def fail_schema_conversion(*_args, **_kwargs):
        raise RuntimeError("injected legacy schema conversion failure")

    monkeypatch.setattr(sqlite_files, "_upgrade_snapshot", fail_schema_conversion)
    with pytest.raises(RuntimeError, match="schema conversion failure"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert not list((tmp_path / "db" / ".staging").glob("legacy-merge-*"))
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hashes[0]
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == source_hashes[1]


def test_atomic_publish_failure_keeps_legacy_sources_and_no_active_database(
        tmp_path, monkeypatch):
    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    source_hashes = (hashlib.sha256(user_path.read_bytes()).hexdigest(),
                     hashlib.sha256(media_path.read_bytes()).hexdigest())
    target_path = _configure_migration(tmp_path, monkeypatch)
    original_replace = sqlite_files.os.replace

    def fail_target_publish(source, destination):
        if os.path.basename(source) == "nastools.staged.sqlite3":
            raise OSError("injected SQLite target publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(sqlite_files.os, "replace", fail_target_publish)
    with pytest.raises(OSError, match="target publication failure"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert not list((tmp_path / "db" / ".staging").glob("legacy-merge-*"))
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hashes[0]
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == source_hashes[1]


def test_failure_after_single_file_publish_removes_unverified_target_and_keeps_sources(
        tmp_path, monkeypatch):
    import app.db.sqlite_files as sqlite_files

    user_path = tmp_path / "user.db"
    _create_user_database(user_path)
    source_hash = hashlib.sha256(user_path.read_bytes()).hexdigest()
    target_path = _configure_migration(tmp_path, monkeypatch)
    verify_integrity = sqlite_files._integrity

    def fail_on_published_target(path):
        if os.path.realpath(path) == os.path.realpath(target_path):
            raise RuntimeError("injected published target integrity failure")
        return verify_integrity(path)

    monkeypatch.setattr(sqlite_files, "_integrity", fail_on_published_target)
    with pytest.raises(RuntimeError, match="published target integrity failure"):
        prepare_sqlite_database()

    assert not target_path.exists()
    assert user_path.is_file()
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == source_hash


def test_two_processes_migrating_same_legacy_layout_publish_one_fixed_database(
        tmp_path):
    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    target_path = tmp_path / "db" / "nastools.db"

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [context.Process(target=_prepare_migration_process,
                                 args=(str(tmp_path), start, results))
                 for _ in range(2)]
    for process in processes:
        process.start()
    start.set()
    try:
        messages = [results.get(timeout=60) for _ in processes]
    finally:
        for process in processes:
            process.join(60)
            if process.is_alive():
                process.terminate()
                process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)

    assert [message[0] for message in messages] == ["ok", "ok"]
    assert {message[1] for message in messages} == {str(target_path)}
    assert all(process.exitcode == 0 for process in processes)
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT count(*) FROM CONFIG_USERS").fetchone() == (1,)


def test_process_interruption_during_legacy_archive_finishes_on_next_startup(
        tmp_path, monkeypatch):
    user_path = tmp_path / "user.db"
    media_path = tmp_path / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    user_hash = hashlib.sha256(user_path.read_bytes()).hexdigest()
    media_hash = hashlib.sha256(media_path.read_bytes()).hexdigest()
    target_path = _configure_migration(tmp_path, monkeypatch)
    assert prepare_sqlite_database() == str(target_path)
    with sqlite3.connect(target_path) as connection:
        job_id = connection.execute(
            "SELECT value FROM __nastools_database_layout WHERE key='job_id'").fetchone()[0]

    context = multiprocessing.get_context("spawn")
    moved_source = context.Event()
    process = context.Process(target=_interrupt_during_legacy_archive,
                              args=(str(tmp_path), moved_source))
    process.start()
    try:
        assert moved_source.wait(60), "子进程未到达旧库归档边界"
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        if process.is_alive():
            process.kill()
            process.join(5)
    assert process.exitcode != 0
    assert user_path.exists() != media_path.exists()

    finalize_sqlite_migration()

    archive = tmp_path / "db" / ".legacy-archive" / f"legacy-merge-{job_id}"
    assert not user_path.exists()
    assert not media_path.exists()
    assert hashlib.sha256((archive / "user.db").read_bytes()).hexdigest() == user_hash
    assert hashlib.sha256((archive / "media.db").read_bytes()).hexdigest() == media_hash
    with sqlite3.connect(target_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT NAME FROM CONFIG_USERS WHERE ID=17").fetchone() \
            == ("管理员",)
