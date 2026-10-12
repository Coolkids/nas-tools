"""旧双库在隔离目录中升级并合并到固定单库。"""

import hashlib
import sqlite3

import pytest
from sqlalchemy import URL, create_engine, text

from app.db.models import Base
from app.db.schema import LOGICAL_DOMAIN_TABLES
from app.db.sqlite_files import prepare_sqlite_database
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


def _create_user_database(path):
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
                (ID, NAME, PASSWORD, PRIS) VALUES (17, '管理员', 'hash', 'all')"""))
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


def _configure_migration(config_dir, monkeypatch):
    jobs = StorageJobStore(str(config_dir / ".maintenance" / "jobs.sqlite3"))
    monkeypatch.setattr("config.Config", lambda: _Config(config_dir))
    monkeypatch.setattr("app.db.sqlite_files.STORAGE_JOBS", jobs)
    return config_dir / "db" / "nastools.db"


@pytest.mark.parametrize("layout", ("root", "db"))
def test_old_main_and_media_databases_merge_into_fixed_single_database(
        layout, tmp_path, monkeypatch):
    config_dir = tmp_path
    source_dir = config_dir if layout == "root" else config_dir / "db"
    source_dir.mkdir(exist_ok=True)
    user_path = source_dir / "user.db"
    media_path = source_dir / "media.db"
    _create_user_database(user_path)
    _create_media_database(media_path)
    user_hash = hashlib.sha256(user_path.read_bytes()).hexdigest()
    media_hash = hashlib.sha256(media_path.read_bytes()).hexdigest()
    target_path = _configure_migration(config_dir, monkeypatch)

    assert prepare_sqlite_database() == str(target_path)

    assert target_path.is_file()
    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT ID, NAME FROM CONFIG_USERS WHERE ID=17").fetchone() == (17, "管理员")
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

    assert user_path.is_file()
    assert media_path.is_file()
    assert hashlib.sha256(user_path.read_bytes()).hexdigest() == user_hash
    assert hashlib.sha256(media_path.read_bytes()).hexdigest() == media_hash


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
    _create_user_database(user_path)
    writer = sqlite3.connect(user_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE CONFIG_USERS SET NAME='WAL 管理员' WHERE ID=17")
        writer.commit()
        assert (tmp_path / "user.db-wal").is_file()

        target_path = _configure_migration(tmp_path, monkeypatch)
        assert prepare_sqlite_database() == str(target_path)
    finally:
        writer.close()

    with sqlite3.connect(target_path) as connection:
        assert connection.execute(
            "SELECT NAME FROM CONFIG_USERS WHERE ID=17").fetchone() == ("WAL 管理员",)


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
