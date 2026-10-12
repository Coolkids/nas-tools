"""SQLite 固定路径与旧版双库到单库的启动迁移。"""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
import logging

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import URL, create_engine

from app.db.models import Base
from app.db.schema import MEDIA_TABLES, current_schema_revision
from app.utils.storage_jobs import STORAGE_JOBS


_RETIRED_TABLES = {
    "SITE_STATISTICS_HISTORY", "SITE_USER_INFO_STATS", "SITE_USER_SEEDING_INFO",
    "SITE_FAVICON", "CONFIG_SITE", "SITE_BRUSH_TASK", "SITE_BRUSH_TORRENTS",
}
_VERSION_TABLES = {"alembic_version", "alembic_version_media"}
_MEDIA_UPGRADE_BASE = "b3c4d5e6f7a8"
_MEDIA_TABLES_REQUIRED = {
    "MEDIASYNC_ITEMS": {"ID", "SERVER", "ITEM_ID", "ITEM_TYPE", "TITLE"},
    "MEDIASYNC_STATISTICS": {"ID", "SERVER", "TOTAL_COUNT", "MOVIE_COUNT",
                              "TV_COUNT", "UPDATE_TIME"},
}
_MEDIA_TABLE_COLUMNS = {
    "MEDIASYNC_ITEMS": _MEDIA_TABLES_REQUIRED["MEDIASYNC_ITEMS"] | {
        "LIBRARY", "ORGIN_TITLE", "YEAR", "TMDBID", "IMDBID", "PATH", "NOTE", "JSON",
        "GENERATION_ID",
    },
    "MEDIASYNC_STATISTICS": _MEDIA_TABLES_REQUIRED["MEDIASYNC_STATISTICS"] | {
        "ACTIVE_GENERATION",
    },
}
_LEGACY_ARCHIVE_RETENTION_SECONDS = 30 * 24 * 60 * 60
logger = logging.getLogger(__name__)


def database_path(config_path=None):
    if config_path is None:
        from config import Config
        config_path = Config().get_config_path()
    return os.path.join(config_path, "db", "nastools.db")


def _database_type():
    from config import Config
    value = Config().get_config("database")
    if value is None:
        return "sqlite"
    if not isinstance(value, dict):
        raise ValueError("database 配置必须是对象")
    return str(value.get("type") or "sqlite").lower()


def _sqlite_uri(path, read_only=False):
    from pathlib import Path
    uri = Path(path).resolve().as_uri()
    return f"{uri}?mode=ro" if read_only else uri


def _connect(path, read_only=False):
    if read_only:
        return sqlite3.connect(_sqlite_uri(path, read_only=True), uri=True, timeout=30)
    return sqlite3.connect(path, timeout=30)


def _integrity(path):
    if not os.path.isfile(path):
        raise RuntimeError(f"数据库文件不存在：{path}")
    connection = _connect(path, read_only=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite 完整性检查失败：{path}")
    finally:
        connection.close()


def _source_layout(config_path, allow_partial_archive=False):
    root = config_path
    database_dir = os.path.join(config_path, "db")
    locations = {
        "root": (os.path.join(root, "user.db"), os.path.join(root, "media.db")),
        "db": (os.path.join(database_dir, "user.db"), os.path.join(database_dir, "media.db")),
    }
    present = {name: tuple(path for path in paths if os.path.exists(path)
                           or os.path.exists(path + "-wal") or os.path.exists(path + "-shm"))
               for name, paths in locations.items()}
    active = [name for name, paths in present.items() if paths]
    if len(active) > 1:
        raise RuntimeError("配置目录根部和 db/ 同时存在旧数据库，来源冲突，已保留全部文件")
    if not active:
        return None
    name = active[0]
    user_path, media_path = locations[name]
    for path in (user_path, media_path):
        if os.path.exists(path) and not os.path.isfile(path):
            raise RuntimeError(f"旧数据库路径不是普通文件：{path}")
    user_exists = os.path.isfile(user_path)
    media_exists = os.path.isfile(media_path)
    user_sidecars = any(os.path.exists(user_path + suffix) for suffix in ("-wal", "-shm"))
    media_sidecars = any(os.path.exists(media_path + suffix) for suffix in ("-wal", "-shm"))
    if not user_exists and (media_exists or media_sidecars):
        if allow_partial_archive:
            return {"location": name, "user": user_path if user_sidecars else None,
                    "media": media_path if media_exists or media_sidecars else None,
                    "partial_archive": True}
        raise RuntimeError("只发现旧媒体库而缺少主数据库，拒绝创建空主库")
    if not user_exists and user_sidecars:
        if allow_partial_archive:
            return {"location": name, "user": user_path, "media": None,
                    "partial_archive": True}
        raise RuntimeError("发现孤立的主数据库 WAL/SHM 文件，拒绝自动升级")
    if not media_exists and media_sidecars and not allow_partial_archive:
        raise RuntimeError("发现孤立的媒体数据库 WAL/SHM 文件，拒绝自动升级")
    return {"location": name, "user": user_path if user_exists else None,
            "media": media_path if media_exists or (allow_partial_archive and media_sidecars) else None,
            "partial_archive": bool(allow_partial_archive and (not user_exists or
                                    (not media_exists and media_sidecars)))}


def _copy_snapshot(source, target):
    original = _connect(source, read_only=True)
    destination = _connect(target)
    try:
        original.backup(destination, pages=1000, sleep=0.01)
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        destination.commit()
    finally:
        destination.close()
        original.close()
    _integrity(target)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Windows 和部分挂载文件系统不允许打开目录句柄进行 fsync。
        return


def _fsync_file(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_manifest(path):
    manifest = {}
    for suffix in ("", "-wal", "-shm"):
        file_path = path + suffix
        if os.path.isfile(file_path):
            manifest[os.path.basename(file_path)] = {
                "size": os.path.getsize(file_path),
                "sha256": _sha256(file_path),
            }
    return manifest


def _upgrade_snapshot(path, config_path, target_revision="head"):
    script_location = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "db_scripts")
    script_location = os.path.abspath(script_location)
    from alembic.script import ScriptDirectory
    script = ScriptDirectory(script_location)
    source = _connect(path, read_only=True)
    try:
        tables = _table_names(source)
        if "alembic_version" not in tables:
            raise RuntimeError("旧主数据库缺少 Alembic 版本记录，无法确定安全升级路径")
        versions = [row[0] for row in source.execute("SELECT version_num FROM alembic_version")]
        if len(versions) != 1:
            raise RuntimeError("旧主数据库结构版本记录数量异常")
        try:
            known_revision = script.get_revision(versions[0])
        except Exception as error:
            raise RuntimeError(f"旧主数据库结构版本不受支持：{versions[0]}") from error
        if known_revision is None:
            raise RuntimeError(f"旧主数据库结构版本不受支持：{versions[0]}")
    finally:
        source.close()
    engine = create_engine(URL.create("sqlite", database=path), connect_args={"timeout": 30})
    try:
        with engine.connect() as connection:
            alembic_cfg = AlembicConfig()
            alembic_cfg.set_main_option("script_location", script_location)
            alembic_cfg.attributes["connection"] = connection
            command.upgrade(alembic_cfg, target_revision)
        source = _connect(path, read_only=True)
        try:
            versions = [row[0] for row in source.execute(
                "SELECT version_num FROM alembic_version")]
            expected_revision = (current_schema_revision(script_location)
                                 if target_revision == "head" else target_revision)
            if versions != [expected_revision]:
                raise RuntimeError("旧主数据库结构升级后版本校验失败")
        finally:
            source.close()
        _integrity(path)
    finally:
        engine.dispose()


def _upgrade_legacy_media_snapshot(path, config_path):
    """仅升级经结构指纹识别的旧媒体库，不在媒体快照上运行主库迁移。"""
    script_location = os.path.abspath(os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "..", "db_scripts"))
    from alembic.script import ScriptDirectory
    script = ScriptDirectory(script_location)
    parent = script.get_revision(_MEDIA_UPGRADE_BASE)
    if parent is None:
        raise RuntimeError("缺少已知的媒体结构升级基线")

    connection = _connect(path, read_only=True)
    try:
        tables = {name.upper(): name for name in _table_names(connection)}
        for expected, required_columns in _MEDIA_TABLES_REQUIRED.items():
            actual = tables.get(expected)
            if not actual:
                raise RuntimeError(f"旧媒体库缺少已知结构表：{expected}")
            columns = {row[1].upper() for row in connection.execute(
                f"PRAGMA table_info({_quote(actual)})")}
            if not required_columns.issubset(columns):
                missing = sorted(required_columns - columns)
                raise RuntimeError(
                    f"旧媒体库结构指纹不匹配，{actual} 缺少字段：{', '.join(missing)}")
            unexpected_columns = columns - _MEDIA_TABLE_COLUMNS[expected]
            if unexpected_columns:
                raise RuntimeError(
                    f"旧媒体库结构指纹不匹配，{actual} 含未识别字段："
                    f"{', '.join(sorted(unexpected_columns))}")
        allowed = set(_MEDIA_TABLES_REQUIRED) | _VERSION_TABLES
        unknown = set(tables) - allowed
        if unknown:
            raise RuntimeError(f"旧媒体库包含未识别表：{', '.join(sorted(unknown))}")
        version_table = tables.get("ALEMBIC_VERSION")
        media_version_table = tables.get("ALEMBIC_VERSION_MEDIA")
        for candidate in (version_table, media_version_table):
            if not candidate:
                continue
            versions = [row[0] for row in connection.execute(
                f"SELECT version_num FROM {_quote(candidate)}")]
            known = {revision.revision for revision in script.walk_revisions()}
            if len(versions) != 1 or versions[0] not in known:
                raise RuntimeError("旧媒体库结构版本无法识别，拒绝猜测升级路径")
            if versions[0] not in {_MEDIA_UPGRADE_BASE, "f2a3b4c5d6e7", "f3a4b5c6d7e8",
                                    "f4a5b6c7d8e9"}:
                raise RuntimeError(f"旧媒体库结构版本不受支持：{versions[0]}")
    finally:
        connection.close()

    # 媒体库的旧版本没有独立 Alembic 链。其表/字段指纹确认后，从只
    # 影响媒体表的 f2 revision 开始升级，避免对它套用主库历史迁移。
    connection = _connect(path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL)")
        connection.execute("DELETE FROM alembic_version")
        connection.execute("INSERT INTO alembic_version(version_num) VALUES (?)",
                           (_MEDIA_UPGRADE_BASE,))
        connection.commit()
    finally:
        connection.close()

    engine = create_engine(URL.create("sqlite", database=path), connect_args={"timeout": 30})
    try:
        with engine.connect() as connection:
            alembic_cfg = AlembicConfig()
            alembic_cfg.set_main_option("script_location", script_location)
            alembic_cfg.attributes["connection"] = connection
            command.upgrade(alembic_cfg, "head")
        _integrity(path)
    finally:
        engine.dispose()


def _table_names(connection, schema="main"):
    return {row[0] for row in _execute(
        connection,
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _execute(connection, statement, parameters=()):
    if hasattr(connection, "exec_driver_sql"):
        return connection.exec_driver_sql(statement, parameters)
    return connection.execute(statement, parameters)


def cleanup_legacy_startup_data(connection):
    """Apply the one-time legacy cleanup when importing snapshots into a new layout."""
    tables = {name.casefold(): name for name in _table_names(connection)}
    for table_name in ("RSS_MOVIES", "RSS_TVS"):
        actual = tables.get(table_name.casefold())
        if not actual:
            continue
        columns = {row[1].casefold(): row[1] for row in _execute(
            connection, f"PRAGMA table_info({_quote(actual)})")}
        rss_sites = columns.get("rss_sites")
        if rss_sites:
            _execute(connection,
                     f"UPDATE {_quote(actual)} SET {_quote(rss_sites)}=NULL")

    system_dict = tables.get("system_dict")
    if system_dict:
        columns = {row[1].casefold(): row[1] for row in _execute(
            connection, f"PRAGMA table_info({_quote(system_dict)})")}
        type_column, key_column = columns.get("type"), columns.get("key")
        if type_column and key_column:
            _execute(connection,
                     f"DELETE FROM {_quote(system_dict)} WHERE {_quote(type_column)}=? "
                     f"AND {_quote(key_column)} IN (?, ?)",
                     ("SystemConfig", "CookieUserInfo", "CookieCloud"))


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def _validate_target_lengths(connection, alias, table, source_columns, target_info):
    for column in target_info:
        name = column[1]
        if name not in source_columns:
            continue
        match = re.fullmatch(r"(?:VAR)?CHAR\s*\(\s*(\d+)\s*\)",
                             str(column[2] or ""), flags=re.IGNORECASE)
        if not match:
            continue
        limit = int(match.group(1))
        count = _execute(connection,
            f"SELECT COUNT(*) FROM {_quote(alias)}.{_quote(table)} "
            f"WHERE length({_quote(name)}) > ?", (limit,)).fetchone()[0]
        if count:
            raise RuntimeError(f"旧表 {table}.{name} 有 {count} 条记录超过新字段长度 {limit}，拒绝截断")


def _digest_rows(connection, schema, table, columns, primary_key):
    digest = hashlib.sha256()
    selected = ", ".join(_quote(column) for column in columns)
    order = ", ".join(_quote(column) for column in primary_key)
    cursor = _execute(connection,
                      f"SELECT {selected} FROM {schema}.{_quote(table)} ORDER BY {order}")
    count = 0
    while True:
        rows = cursor.fetchmany(500)
        if not rows:
            break
        for row in rows:
            digest.update(repr(tuple(row)).encode("utf-8", "surrogatepass"))
            digest.update(b"\0")
            count += 1
    return count, digest.hexdigest()


def _copy_domain(connection, source_path, domain, expected_tables, excluded_tables=(),
                 attached_sources=None):
    alias = f"source_{domain}"
    _execute(connection, f"ATTACH DATABASE ? AS {_quote(alias)}", (source_path,))
    if attached_sources is not None:
        attached_sources.append(alias)
    try:
        unsupported_objects = _execute(
            connection,
            f"SELECT type, name FROM {_quote(alias)}.sqlite_master "
            "WHERE type IN ('view', 'trigger') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        conflicts = sorted(name for _kind, name in unsupported_objects
                           if name.upper() in expected_tables)
        if conflicts:
            raise RuntimeError(
                f"旧{domain}数据库包含与应用表同名的视图或触发器：{', '.join(conflicts)}")
        if unsupported_objects:
            names = ", ".join(sorted(name for _kind, name in unsupported_objects))
            raise RuntimeError(f"旧{domain}数据库包含无法迁移的视图或触发器：{names}")
        source_tables = _table_names(connection, alias)
        unknown = source_tables - expected_tables - _RETIRED_TABLES - _VERSION_TABLES
        if unknown:
            raise RuntimeError(f"旧{domain}数据库包含未识别表：{', '.join(sorted(unknown))}")
        wrong_domain = (source_tables & MEDIA_TABLES if domain == "main"
                        else source_tables & (expected_tables - MEDIA_TABLES))
        if wrong_domain:
            raise RuntimeError(f"旧{domain}数据库包含来源不符的业务表：{', '.join(sorted(wrong_domain))}")
        for table in sorted(source_tables & expected_tables - set(excluded_tables)):
            source_columns = [row[1] for row in _execute(
                connection, f"PRAGMA {alias}.table_info({_quote(table)})")]
            target_columns = [row[1] for row in _execute(
                connection, f"PRAGMA main.table_info({_quote(table)})")]
            target_info = _execute(
                connection, f"PRAGMA main.table_info({_quote(table)})").fetchall()
            _validate_target_lengths(connection, alias, table, source_columns, target_info)
            source_only = set(source_columns) - set(target_columns)
            if source_only:
                raise RuntimeError(f"旧表 {table} 有未映射字段：{', '.join(sorted(source_only))}")
            target_only_required = [row[1] for row in target_info
                                    if row[1] not in source_columns and row[3] and row[4] is None
                                    and not row[5]]
            if target_only_required:
                raise RuntimeError(f"旧表 {table} 缺少必需字段：{', '.join(target_only_required)}")
            columns = [column for column in target_columns if column in source_columns]
            if not columns:
                raise RuntimeError(f"旧表 {table} 没有可迁移字段")
            primary_key = [row[1] for row in sorted(target_info, key=lambda item: item[5]) if row[5]]
            if not primary_key:
                raise RuntimeError(f"旧表 {table} 没有稳定主键，无法校验迁移")
            count_before, digest_before = _digest_rows(connection, alias, table, columns, primary_key)
            if count_before:
                joined_columns = ", ".join(_quote(column) for column in columns)
                _execute(connection,
                    f"INSERT INTO main.{_quote(table)} ({joined_columns}) "
                    f"SELECT {joined_columns} FROM {alias}.{_quote(table)}")
            count_after, digest_after = _digest_rows(connection, "main", table, columns, primary_key)
            if (count_before, digest_before) != (count_after, digest_after):
                raise RuntimeError(f"旧表 {table} 导入后摘要不一致")
    finally:
        if attached_sources is None:
            _execute(connection, f"DETACH DATABASE {_quote(alias)}")


def _backfill_recognition_search(connection):
    """合并旧主库后回填识别筛选索引字段。"""
    if "RECOGNITION_REQUEST" not in _table_names(connection):
        return
    from app.db.models import portable_key_digest, recognition_search_projection

    last_request_id = None
    while True:
        statement = "SELECT REQUEST_ID, OVERALL_RESULT, ACTIONS FROM RECOGNITION_REQUEST"
        parameters = ()
        if last_request_id is not None:
            statement += " WHERE REQUEST_ID > ?"
            parameters = (last_request_id,)
        statement += " ORDER BY REQUEST_ID LIMIT 500"
        rows = _execute(connection, statement, parameters).fetchall()
        if not rows:
            break
        for request_id, overall_result, actions in rows:
            projection = recognition_search_projection(overall_result, actions)
            _execute(connection, """UPDATE RECOGNITION_REQUEST SET
                STATUS_SUMMARY=?, BUSINESS_STATUS_SUMMARY=?, REASON_SUMMARY=?, REASON_HASH=?,
                BUSINESS_REASON_SUMMARY=?, BUSINESS_REASON_HASH=? WHERE REQUEST_ID=?""",
                      (projection["STATUS_SUMMARY"], projection["BUSINESS_STATUS_SUMMARY"],
                       projection["REASON_SUMMARY"], projection["REASON_HASH"],
                       projection["BUSINESS_REASON_SUMMARY"],
                       projection["BUSINESS_REASON_HASH"], request_id))
            for action_type in projection["ACTION_TYPES"]:
                action_id = portable_key_digest((request_id, action_type))
                action_hash = portable_key_digest((action_type,))
                existing = _execute(connection, """SELECT REQUEST_ID, ACTION_TYPE
                    FROM RECOGNITION_REQUEST_ACTION WHERE ID=?""", (action_id,)).fetchone()
                if existing and existing != (request_id, action_type):
                    raise RuntimeError("识别动作摘要冲突，拒绝覆盖不同记录")
                _execute(connection, """INSERT OR IGNORE INTO RECOGNITION_REQUEST_ACTION
                    (ID, REQUEST_ID, ACTION_TYPE, ACTION_HASH) VALUES (?, ?, ?, ?)""",
                          (action_id, request_id, action_type, action_hash))
        last_request_id = rows[-1][0]


def _write_completion(connection, job_id, sources):
    connection.execute("""CREATE TABLE IF NOT EXISTS __nastools_database_layout (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")
    values = {
        "layout_version": "1",
        "merge_status": "complete",
        "job_id": job_id,
        "sources": json.dumps(sources, sort_keys=True),
        "completed_at": str(time.time()),
    }
    connection.executemany(
        "INSERT INTO __nastools_database_layout(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        values.items())


def _completed_target(path):
    if not os.path.isfile(path):
        return False
    connection = _connect(path, read_only=True)
    try:
        row = connection.execute(
            "SELECT value FROM __nastools_database_layout WHERE key='merge_status'").fetchone()
        return row == ("complete",)
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def _completion_job_id(path):
    if not os.path.isfile(path):
        return None
    connection = _connect(path, read_only=True)
    try:
        row = connection.execute(
            "SELECT value FROM __nastools_database_layout WHERE key='job_id'").fetchone()
        return row[0] if row else None
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()


def _completion_sources(path):
    if not os.path.isfile(path):
        return None
    connection = _connect(path, read_only=True)
    try:
        row = connection.execute(
            "SELECT value FROM __nastools_database_layout WHERE key='sources'").fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.DatabaseError, ValueError, TypeError):
        return None
    finally:
        connection.close()


def _validate_completed_sources(target_path, source):
    expected = _completion_sources(target_path)
    if not expected:
        raise RuntimeError("合库完成记录缺少来源清单，拒绝归档旧文件")
    for domain in ("user", "media"):
        path = source.get(domain)
        if path and expected.get(domain, {}).get("path") != path:
            raise RuntimeError(f"待归档的旧{domain}库与合库来源记录不符：{path}")


def _cleanup_incomplete_staging(database_dir):
    staging = os.path.join(database_dir, ".staging")
    if not os.path.isdir(staging):
        return
    for entry in os.scandir(staging):
        if entry.is_dir(follow_symlinks=False) and entry.name.startswith("legacy-merge-"):
            shutil.rmtree(entry.path, ignore_errors=True)


def prepare_sqlite_database():
    """旧双库合并成功发布前不创建活动业务数据库。"""
    if _database_type() != "sqlite":
        return None
    with STORAGE_JOBS.maintenance_lock(exclusive=True):
        return _prepare_sqlite_database()


def _prepare_sqlite_database():
    from config import Config
    config_path = Config().get_config_path()
    target_path = database_path(config_path)
    database_dir = os.path.dirname(target_path)
    if os.path.exists(database_dir) and not os.path.isdir(database_dir):
        raise RuntimeError(f"SQLite 数据库目录路径已被普通文件占用：{database_dir}")
    os.makedirs(database_dir, exist_ok=True)
    if os.path.exists(target_path):
        _integrity(target_path)
        completed = _completed_target(target_path)
        source = _source_layout(config_path, allow_partial_archive=completed)
        if completed and source:
            _validate_completed_sources(target_path, source)
        if source and not _completed_target(target_path):
            raise RuntimeError("新单库与旧双库并存但没有完成记录，拒绝猜测或覆盖")
        return target_path
    source = _source_layout(config_path)
    if not source:
        return target_path

    total_source_bytes = 0
    for path in (source.get("user"), source.get("media")):
        if path:
            total_source_bytes += sum(os.path.getsize(path + suffix)
                                      for suffix in ("", "-wal", "-shm")
                                      if os.path.isfile(path + suffix))
    if shutil.disk_usage(database_dir).free < total_source_bytes * 2 + 64 * 1024 * 1024:
        raise RuntimeError("db/ 可用空间不足，无法安全快照并合并旧数据库")
    _cleanup_incomplete_staging(database_dir)
    job_id = uuid.uuid4().hex
    staging_dir = os.path.join(database_dir, ".staging", f"legacy-merge-{job_id}")
    os.makedirs(staging_dir, exist_ok=False)
    user_snapshot = os.path.join(staging_dir, "user.snapshot.sqlite3")
    media_snapshot = os.path.join(staging_dir, "media.snapshot.sqlite3")
    staged_target = os.path.join(staging_dir, "nastools.staged.sqlite3")
    published = False
    try:
        source_records = {}
        _copy_snapshot(source["user"], user_snapshot)
        source_records["user"] = {
            "path": source["user"],
            "files": _file_manifest(source["user"]),
            "snapshot_sha256": _sha256(user_snapshot),
        }
        user_tables = set()
        source_connection = _connect(user_snapshot, read_only=True)
        try:
            user_tables = {name.upper() for name in _table_names(source_connection)}
        finally:
            source_connection.close()
        user_has_media_schema = set(_MEDIA_TABLES_REQUIRED).issubset(user_tables)
        _upgrade_snapshot(
            user_snapshot, config_path,
            target_revision="head" if user_has_media_schema else _MEDIA_UPGRADE_BASE)
        if source.get("media"):
            _copy_snapshot(source["media"], media_snapshot)
            _upgrade_legacy_media_snapshot(media_snapshot, config_path)
            source_records["media"] = {
                "path": source["media"],
                "files": _file_manifest(source["media"]),
                "snapshot_sha256": _sha256(media_snapshot),
            }
        else:
            source_records["media"] = {"path": None, "sha256": None, "empty_media_domain": True}

        engine = create_engine(URL.create("sqlite", database=staged_target),
                               connect_args={"timeout": 30})
        try:
            Base.metadata.create_all(engine)
            with engine.connect() as connection:
                attached_sources = []
                try:
                    with connection.begin():
                        expected_tables = set(Base.metadata.tables)
                        _copy_domain(connection, user_snapshot, "main", expected_tables,
                                     attached_sources=attached_sources)
                        if source.get("media"):
                            # Sync leases are process control state, not media data. A
                            # stale legacy lease must never block the first new sync.
                            _copy_domain(connection, media_snapshot, "media", expected_tables,
                                         excluded_tables={"MEDIASYNC_SYNC_JOBS"},
                                         attached_sources=attached_sources)
                        cleanup_legacy_startup_data(connection)
                        _backfill_recognition_search(connection)
                        head = current_schema_revision()
                        connection.exec_driver_sql(
                            "CREATE TABLE IF NOT EXISTS alembic_version "
                            "(version_num VARCHAR(32) NOT NULL)")
                        connection.exec_driver_sql("DELETE FROM alembic_version")
                        connection.exec_driver_sql(
                            "INSERT INTO alembic_version(version_num) VALUES (?)", (head,))
                        raw = connection.connection.driver_connection
                        _write_completion(raw, job_id, source_records)
                finally:
                    for alias in reversed(attached_sources):
                        _execute(connection, f"DETACH DATABASE {_quote(alias)}")
            with engine.connect() as connection:
                integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalar_one()
                if integrity != "ok":
                    raise RuntimeError("暂存合库完整性检查失败")
        finally:
            engine.dispose()

        _fsync_file(staged_target)
        _fsync_directory(staging_dir)
        if os.path.exists(target_path):
            raise RuntimeError("目标单库在迁移期间被创建，拒绝覆盖")
        os.replace(staged_target, target_path)
        published = True
        _fsync_directory(os.path.dirname(target_path))
        _integrity(target_path)
        return target_path
    except Exception:
        if published:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(target_path + suffix)
                except FileNotFoundError:
                    pass
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def finalize_sqlite_migration():
    """数据库结构检查通过后归档旧源；失败只保留可重试状态。"""
    if _database_type() != "sqlite":
        return
    with STORAGE_JOBS.maintenance_lock(exclusive=True):
        cleanup_legacy_archives()
        _finalize_sqlite_migration()


def cleanup_legacy_archives(now=None):
    """过保留期后仅清理本应用已知布局的旧双库归档。"""
    archive_root = os.path.join(os.path.dirname(database_path()), ".legacy-archive")
    if not os.path.isdir(archive_root) or os.path.islink(archive_root):
        return 0
    current_time = time.time() if now is None else float(now)
    cutoff = current_time - _LEGACY_ARCHIVE_RETENTION_SECONDS
    known_file = re.compile(r"^(?:user|media)\.db(?:-(?:wal|shm))?$")
    archive_name = re.compile(r"^legacy-merge-[0-9a-f]{32}$")
    removed = 0
    try:
        archives = list(os.scandir(archive_root))
    except OSError as error:
        logger.warning("无法枚举旧数据库归档目录 %s：%s", archive_root, error)
        return 0
    for archive in archives:
        if (not archive_name.fullmatch(archive.name)
                or not archive.is_dir(follow_symlinks=False)):
            logger.warning("保留未知数据库归档内容：%s", archive.path)
            continue
        try:
            files = list(os.scandir(archive.path))
        except OSError as error:
            logger.warning("旧数据库归档清理待重试：%s：%s", archive.path, error)
            continue
        manifest_entry = next((entry for entry in files if entry.name == ".retired.json"), None)
        if manifest_entry:
            if not manifest_entry.is_file(follow_symlinks=False):
                logger.warning("旧数据库归档标记不是普通文件，保留目录：%s", archive.path)
                continue
            try:
                with open(manifest_entry.path, "r", encoding="utf-8") as stream:
                    manifest = json.load(stream)
                retired_at = float(manifest.get("retired_at") or 0)
                if manifest.get("job_id") != archive.name.removeprefix("legacy-merge-"):
                    raise ValueError("归档任务标记不匹配")
            except (OSError, TypeError, ValueError) as error:
                logger.warning("旧数据库归档标记无效，保留目录 %s：%s", archive.path, error)
                continue
        else:
            try:
                retired_at = archive.stat(follow_symlinks=False).st_mtime
            except OSError as error:
                logger.warning("无法读取旧数据库归档时间：%s：%s", archive.path, error)
                continue
        if retired_at >= cutoff:
            continue
        if any(not entry.is_file(follow_symlinks=False)
               or (entry.name != ".retired.json" and not known_file.fullmatch(entry.name))
               for entry in files):
            logger.warning("旧数据库归档含未知内容，保留目录：%s", archive.path)
            continue
        try:
            for entry in files:
                if entry.name == ".retired.json":
                    continue
                os.unlink(entry.path)
                removed += 1
            if manifest_entry:
                os.unlink(manifest_entry.path)
            os.rmdir(archive.path)
        except OSError as error:
            logger.warning("旧数据库归档清理未完成，将在后续启动重试：%s：%s",
                           archive.path, error)
    try:
        os.rmdir(archive_root)
    except OSError:
        pass
    return removed


def _finalize_sqlite_migration():
    from config import Config
    config_path = Config().get_config_path()
    target_path = database_path(config_path)
    if not _completed_target(target_path):
        return
    job_id = _completion_job_id(target_path)
    if not job_id:
        return
    source = _source_layout(config_path, allow_partial_archive=True)
    if source:
        _validate_completed_sources(target_path, source)
    archive_root = os.path.join(os.path.dirname(target_path), ".legacy-archive")
    if os.path.islink(archive_root):
        raise RuntimeError("旧数据库归档路径不能是符号链接")
    os.makedirs(archive_root, exist_ok=True)
    target = os.path.join(archive_root, f"legacy-merge-{job_id}")
    if os.path.lexists(target) and (os.path.islink(target) or not os.path.isdir(target)):
        raise RuntimeError("旧数据库归档目标已被非目录内容占用")
    os.makedirs(target, exist_ok=True)
    if not source:
        try:
            if not os.listdir(target):
                os.rmdir(target)
        except OSError as error:
            logger.warning("空数据库归档目录清理待重试：%s：%s", target, error)
        shutil.rmtree(os.path.join(os.path.dirname(target_path), ".staging",
                                   f"legacy-merge-{job_id}"), ignore_errors=True)
        try:
            os.rmdir(archive_root)
        except OSError:
            pass
        return
    manifest_path = os.path.join(target, ".retired.json")
    if not os.path.exists(manifest_path):
        temporary_manifest = manifest_path + ".tmp"
        with open(temporary_manifest, "w", encoding="utf-8") as stream:
            json.dump({"job_id": job_id, "retired_at": time.time()}, stream,
                      ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_manifest, manifest_path)
        _fsync_directory(target)
    moved = []
    try:
        for path in (source.get("user"), source.get("media")):
            if not path:
                continue
            # 先移辅助文件，最后移主文件；重启可识别并继续完成部分归档。
            for suffix in ("-wal", "-shm", ""):
                origin = path + suffix
                if os.path.exists(origin):
                    destination = os.path.join(target, os.path.basename(origin))
                    if os.path.exists(destination):
                        if _sha256(origin) != _sha256(destination):
                            raise RuntimeError(f"归档目标已存在且内容不同：{destination}")
                        os.unlink(origin)
                    else:
                        os.replace(origin, destination)
                        moved.append((destination, origin))
        _fsync_directory(target)
    except Exception:
        for archived, origin in reversed(moved):
            if os.path.exists(archived) and not os.path.exists(origin):
                os.replace(archived, origin)
        raise
    shutil.rmtree(os.path.join(os.path.dirname(target_path), ".staging",
                               f"legacy-merge-{job_id}"), ignore_errors=True)
