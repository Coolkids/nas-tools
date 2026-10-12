"""数据库空目标检查、迁移、配置切换与启动恢复。"""

import base64
import copy
import hashlib
import hmac
import json
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (Column, Index, Integer, MetaData, String, Table, create_engine,
                        func, inspect, insert, select, text, update)
from sqlalchemy.engine import URL

from app.db.backends import create_backend
from app.db.manager import DATABASE
from app.db.models import Base, DATABASEMIGRATIONMARKER
from app.db.schema import current_schema_revision
from app.utils.storage_jobs import STORAGE_JOBS


_BATCH_SIZE = 500
_BATCH_BYTES = 8 * 1024 * 1024
_CHECK_TTL_SECONDS = 600
_JOB_KIND = "database_migration"
_PREPARE_KIND = "database_target_prepare"
_WORKERS = {}
_WORKERS_LOCK = threading.RLock()


def _normalize_candidate(values):
    values = dict(values or {})
    if any(key in values for key in ("path", "directory", "url", "dsn", "sqlite_path")):
        raise ValueError("数据库文件路径和 DSN 由后端固定生成，不接受路径覆盖")
    kind = str(values.get("type") or "sqlite").strip().lower()
    settings = {"type": kind}
    if kind == "sqlite":
        DATABASE._validate(settings)
        return settings
    if kind not in {"postgresql", "mysql", "mariadb"}:
        raise ValueError("数据库类型必须是 sqlite、postgresql、mysql 或 mariadb")
    settings.update({
        "host": str(values.get("host") or "").strip(),
        "port": int(values.get("port") or (5432 if kind == "postgresql" else 3306)),
        "name": str(values.get("name") or "").strip(),
        "username": str(values.get("username") or "").strip(),
        "password": str(values.get("password") or ""),
        "connect_timeout": int(values.get("connect_timeout") or 5),
        "ssl": copy.deepcopy(values.get("ssl") or {}),
    })
    active = DATABASE._read_settings()
    if (not settings["password"] and active.get("type") == kind
            and all(settings.get(key) == active.get(key) for key in ("host", "name", "username"))
            and active.get("password")):
        settings["password"] = active["password"]
    DATABASE._validate(settings)
    return settings


def _fingerprint(settings):
    encoded = json.dumps(settings, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_settings(settings):
    def scrub(value):
        if isinstance(value, dict):
            return {key: scrub(item) for key, item in value.items()
                    if "password" not in str(key).lower()
                    and "secret" not in str(key).lower()
                    and "token" not in str(key).lower()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    safe = {key: scrub(value) for key, value in settings.items()
            if key != "password" and "password" not in key.lower()
            and "secret" not in key.lower() and "token" not in key.lower()}
    safe["password_set"] = bool(settings.get("password"))
    return safe


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        return


def _secure_directory(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _secure_file(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _current_settings():
    settings = DATABASE._read_settings()
    settings.setdefault("type", "sqlite")
    settings["type"] = str(settings.get("type") or "sqlite").lower()
    if settings["type"] != "sqlite":
        settings["port"] = int(settings.get("port") or (
            5432 if settings["type"] == "postgresql" else 3306))
        settings["connect_timeout"] = int(settings.get("connect_timeout") or 5)
        settings["ssl"] = copy.deepcopy(settings.get("ssl") or {})
    DATABASE._validate(settings)
    return settings


def _same_database(left, right):
    if left["type"] != right["type"]:
        return False
    if left["type"] == "sqlite":
        from app.db.sqlite_files import database_path
        return os.path.realpath(database_path()) == os.path.realpath(database_path())
    host_left = str(left.get("host") or "").strip().rstrip(".").lower()
    host_right = str(right.get("host") or "").strip().rstrip(".").lower()
    return (host_left == host_right
            and int(left.get("port") or 0) == int(right.get("port") or 0)
            and str(left.get("name") or "") == str(right.get("name") or ""))


def _new_engine(settings, sqlite_path=None):
    backend = create_backend(settings["type"])
    if settings["type"] == "sqlite" and sqlite_path is not None:
        os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)
        engine = create_engine(URL.create("sqlite", database=sqlite_path),
                               connect_args={"timeout": 60, "check_same_thread": False})
    elif settings["type"] == "sqlite":
        engine = backend.create_engine(settings)
    else:
        engine = backend.create_engine(settings)
    from sqlalchemy import event
    event.listen(engine, "connect", lambda connection, record: backend.configure_connection(connection))
    initialize = getattr(backend, "initialize", None)
    if initialize and sqlite_path is None:
        initialize(engine)
    try:
        backend.validate_product(backend.capabilities(engine))
    except Exception:
        backend.release(engine)
        raise
    return engine


def _database_objects(engine):
    inspector = inspect(engine)
    dialect = engine.dialect.name
    schemas = [None]
    if dialect == "postgresql":
        schemas = [schema for schema in inspector.get_schema_names()
                   if schema not in {"information_schema", "pg_catalog"}
                   and not schema.startswith("pg_toast")]
    objects = []
    for schema in schemas:
        for name in inspector.get_table_names(schema=schema):
            objects.append(f"table:{schema + '.' if schema else ''}{name}")
        for name in inspector.get_view_names(schema=schema):
            objects.append(f"view:{schema + '.' if schema else ''}{name}")
        try:
            materialized_views = inspector.get_materialized_view_names(schema=schema)
        except (AttributeError, NotImplementedError):
            materialized_views = []
        for name in materialized_views:
            objects.append(f"materialized_view:{schema + '.' if schema else ''}{name}")
        try:
            for name in inspector.get_sequence_names(schema=schema):
                objects.append(f"sequence:{schema + '.' if schema else ''}{name}")
        except NotImplementedError:
            if dialect == "postgresql":
                raise
    return sorted(set(objects))


def _sqlite_objects(path):
    if not os.path.exists(path):
        return []
    if not os.path.isfile(path):
        raise RuntimeError(f"SQLite 目标路径不是普通文件：{path}")
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        return [f"{row[0]}:{row[1]}" for row in connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name")]
    finally:
        connection.close()


def _probe_engine(engine):
    suffix = uuid.uuid4().hex
    metadata = MetaData()
    table = Table(
        f"__nt_probe_{suffix}", metadata,
        Column("ID", Integer, primary_key=True, autoincrement=True),
        Column("VALUE", String(128), nullable=False),
    )
    Index(f"__nt_probe_idx_{suffix}", table.c.VALUE, unique=True)
    try:
        metadata.create_all(engine)
        with engine.connect() as connection:
            transaction = connection.begin()
            inserted = connection.execute(insert(table).values(VALUE="before"))
            probe_id = inserted.inserted_primary_key[0]
            found = connection.execute(select(table.c.VALUE).where(
                table.c.ID == probe_id)).scalar_one()
            if found != "before":
                raise RuntimeError("探测表读取结果异常")
            connection.execute(update(table).where(table.c.ID == probe_id).values(VALUE="after"))
            connection.execute(table.delete().where(table.c.ID == probe_id))
            transaction.rollback()
            if connection.execute(select(func.count()).select_from(table)).scalar_one() != 0:
                raise RuntimeError("目标数据库未按预期回滚探测事务")
            connection.rollback()
            lock_name = engine.dialect.identifier_preparer.format_table(table)
            if engine.dialect.name == "postgresql":
                lock_transaction = connection.begin()
                connection.execute(text("SET LOCAL lock_timeout = '10s'"))
                connection.exec_driver_sql(
                    f"LOCK TABLE {lock_name} IN ACCESS EXCLUSIVE MODE")
                lock_transaction.rollback()
            elif engine.dialect.name in {"mysql", "mariadb"}:
                connection.exec_driver_sql(f"LOCK TABLES {lock_name} WRITE")
                connection.exec_driver_sql("UNLOCK TABLES")
                connection.rollback()
        metadata.drop_all(engine, checkfirst=True)
    except Exception:
        try:
            metadata.drop_all(engine, checkfirst=True)
        except Exception:
            raise RuntimeError("数据库能力探测失败且临时探测对象未能清理")
        raise
    if _database_objects(engine):
        raise RuntimeError("数据库能力探测结束后仍存在用户对象")


def _server_capabilities(engine):
    return create_backend(engine.dialect.name).capabilities(engine)


def _probe_candidate(settings):
    if settings["type"] != "sqlite":
        engine = _new_engine(settings)
        try:
            objects = _database_objects(engine)
            if objects:
                return {"objects": objects, "empty": False}
            _probe_engine(engine)
            return {"objects": [], "empty": True}
        finally:
            engine.dispose()

    from app.db.sqlite_files import database_path
    target = database_path()
    objects = _sqlite_objects(target)
    if objects:
        return {"objects": objects, "empty": False}
    probe_dir = os.path.join(os.path.dirname(target), ".staging", "database-probes")
    os.makedirs(probe_dir, exist_ok=True)
    probe_path = os.path.join(probe_dir, f"probe-{uuid.uuid4().hex}.sqlite3")
    engine = _new_engine(settings, sqlite_path=probe_path)
    try:
        _probe_engine(engine)
    finally:
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(probe_path + suffix)
            except FileNotFoundError:
                pass
        try:
            os.rmdir(probe_dir)
        except OSError:
            pass
    return {"objects": [], "empty": True}


def _sqlite_target_files(config_path=None):
    from config import Config
    from app.db.sqlite_files import database_path

    config_path = config_path or Config().get_config_path()
    fixed = database_path(config_path)
    locations = [
        fixed,
        os.path.join(config_path, "user.db"), os.path.join(config_path, "media.db"),
        os.path.join(config_path, "db", "user.db"), os.path.join(config_path, "db", "media.db"),
    ]
    database_dir = os.path.dirname(fixed)
    if os.path.exists(database_dir) and not os.path.isdir(database_dir):
        raise RuntimeError(f"SQLite 数据库目录路径已被普通文件占用：{database_dir}")
    legacy_locations = []
    for root, media in ((config_path, False), (os.path.join(config_path, "db"), False)):
        user_path = os.path.join(root, "user.db")
        media_path = os.path.join(root, "media.db")
        if any(os.path.exists(path + suffix) for path in (user_path, media_path)
               for suffix in ("", "-wal", "-shm")):
            legacy_locations.append((user_path, media_path))
    if len(legacy_locations) > 1:
        raise RuntimeError("配置目录根部与 db/ 同时存在旧 SQLite 库，拒绝选择删除范围")
    paths = []
    if os.path.exists(fixed) or any(os.path.exists(fixed + suffix) for suffix in ("-wal", "-shm")):
        paths.append(fixed)
    if legacy_locations:
        for path in legacy_locations[0]:
            if os.path.exists(path) or any(os.path.exists(path + suffix) for suffix in ("-wal", "-shm")):
                paths.append(path)
    for path in paths:
        if os.path.exists(path) and not os.path.isfile(path):
            raise RuntimeError(f"待准备的 SQLite 路径不是普通文件：{path}")
        if not os.path.isfile(path) and any(os.path.exists(path + suffix) for suffix in ("-wal", "-shm")):
            raise RuntimeError(f"SQLite 主文件缺失但存在 WAL/SHM：{path}")
    return list(dict.fromkeys(paths))


def _file_identity(path):
    try:
        stat = os.stat(path)
        return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns}
    except FileNotFoundError:
        return None


def _sqlite_file_set_identity(path):
    return {suffix: _file_identity(path + suffix) for suffix in ("", "-wal", "-shm")}


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _target_identity(settings):
    if settings["type"] != "sqlite":
        return {"fingerprint": _fingerprint(settings)}
    from app.db.sqlite_files import database_path
    path = database_path()
    return {"path": path, "identity": _file_identity(path),
            "objects": _sqlite_objects(path)}


def _current_database_status():
    engine = DATABASE.engine
    with engine.connect() as connection:
        connection.execute(text("SELECT 1")).scalar_one()
    url = engine.url.set(username=None, password=None).render_as_string()
    return {"type": DATABASE.backend.name, "url": url, "fingerprint": _fingerprint(_current_settings())}


def _candidate_file_plan():
    return _sqlite_target_files()


def _database_check(settings, target_prepared=False):
    current = _current_settings()
    source = _current_database_status()
    if _same_database(current, settings):
        raise ValueError("目标数据库与当前活动数据库相同，拒绝对自身执行切换")
    if settings["type"] == "sqlite":
        from app.db.sqlite_files import database_path
        target_path = database_path()
        old_files = _candidate_file_plan()
        legacy_files = [path for path in old_files
                        if os.path.basename(path) in {"user.db", "media.db"}]
        fixed_exists = os.path.exists(target_path)
        if current["type"] != "sqlite" and (legacy_files or (fixed_exists and not target_prepared)):
            return {
                "ok": False, "target_preparation_required": True,
                "reason": "固定 SQLite 位置存在旧业务数据库，需先备份并准备空目标",
                "target_files": old_files,
                "target_file_identities": {path: _sqlite_file_set_identity(path)
                                           for path in old_files},
                "source": source,
                "target": {"type": "sqlite", "path": target_path},
            }
        objects = _sqlite_objects(target_path)
        if objects:
            if current["type"] == "sqlite":
                raise ValueError("SQLite 目标包含业务对象，且当前活动后端也是 SQLite；不能按远程迁回流程删除")
            return {"ok": False, "target_preparation_required": True,
                    "reason": "固定 SQLite 目标不是空库，需先备份并准备空目标",
                    "target_files": [target_path],
                    "target_file_identities": {
                        target_path: _sqlite_file_set_identity(target_path)},
                    "source": source,
                    "target": {"type": "sqlite", "path": target_path, "objects": objects}}
        if os.path.exists(target_path):
            _sqlite_integrity(target_path)
        probe = _probe_candidate(settings)
        free_space = shutil.disk_usage(os.path.dirname(target_path)).free
        identity = _target_identity(settings)
        return {"ok": True, "target_preparation_required": False,
                "source": source, "target": {"type": "sqlite", "path": target_path,
                                               "objects": probe["objects"]},
                "capabilities": {"product": "SQLite", "version": sqlite3.sqlite_version,
                                 "directory_writable": True,
                                 "free_space_bytes": free_space,
                                 "target_size_bytes": os.path.getsize(target_path)
                                 if os.path.isfile(target_path) else 0},
                "target_identity": identity}

    engine = _new_engine(settings)
    try:
        objects = _database_objects(engine)
        if objects:
            return {"ok": False, "target_preparation_required": False,
                    "reason": "远程目标不是空库；请使用新的空数据库",
                    "source": source, "target": _safe_settings(settings),
                    "conflicts": objects[:200], "conflict_count": len(objects)}
        capabilities = _server_capabilities(engine)
        _probe_engine(engine)
        if _database_objects(engine):
            raise RuntimeError("探测对象清理后远程目标仍非空")
        return {"ok": True, "target_preparation_required": False,
                "source": source, "target": _safe_settings(settings),
                "capabilities": capabilities,
                "target_identity": {"fingerprint": _fingerprint(settings)}}
    finally:
        engine.dispose()


def check_database(values):
    settings = None
    try:
        settings = _normalize_candidate(values)
        source_settings = _current_settings()
        config_fingerprint = _fingerprint(source_settings)
        report = _database_check(settings)
        job, _ = STORAGE_JOBS.create(
            _JOB_KIND,
            {"candidate": _safe_settings(settings),
             "fingerprint": _fingerprint(settings),
             "source_fingerprint": config_fingerprint,
             "target_identity": report.get("target_identity"),
             "target_file_identities": report.get("target_file_identities")},
            idempotency_key=uuid.uuid4().hex)
        state = "READY" if report.get("ok") or report.get("target_preparation_required") else "FAILED"
        updated = STORAGE_JOBS.update(job["job_id"], status=state, stage=state,
                                      result=report, message="数据库目标检查完成",
                                      progress=1)
        report["check_id"] = job["job_id"]
        return {"code": 0, "check": report, "job": updated}
    except Exception as error:
        return {"code": 1, "msg": _safe_error(error, settings)}


def _safe_error(error, settings=None):
    value = str(error)
    if settings and settings.get("password"):
        value = value.replace(settings["password"], "***")
    return value[:1500]


def prepare_sqlite_migration_target(values):
    """先归档固定位置的旧 SQLite 库，再删除并复检空目标。"""
    try:
        check_id = (values or {}).get("check_id")
        check = STORAGE_JOBS.get(check_id) if check_id else None
        if not check or check.get("kind") != _JOB_KIND or check.get("status") != "READY":
            raise ValueError("数据库检查已失效，请重新检查 SQLite 目标")
        if time.time() - float(check.get("created_at") or 0) > _CHECK_TTL_SECONDS:
            STORAGE_JOBS.update(check_id, status="CANCELLED", stage="EXPIRED",
                                message="数据库检查凭证已过期，请重新检查",
                                expected_status="READY")
            raise ValueError("数据库检查凭证已过期，请重新检查 SQLite 目标")
        result = check.get("result") or {}
        if not result.get("target_preparation_required"):
            raise ValueError("当前检查结果不需要准备 SQLite 目标")
        candidate = _normalize_candidate(values)
        if candidate["type"] != "sqlite":
            raise ValueError("仅 SQLite 目标支持旧文件准备流程")
        if not hmac.compare_digest(check["payload"].get("fingerprint", ""),
                                   _fingerprint(candidate)):
            raise ValueError("目标参数已变化，请重新检查")
        if _current_settings()["type"] == "sqlite":
            raise ValueError("当前活动数据库为 SQLite，拒绝删除活动数据库文件")
        job, created = STORAGE_JOBS.create(
            _PREPARE_KIND, {"check_id": check_id, "candidate": _safe_settings(candidate),
                            "fingerprint": _fingerprint(candidate),
                            "source_fingerprint": _fingerprint(_current_settings()),
                            "target_files": result.get("target_files") or [],
                            "target_file_identities": result.get("target_file_identities") or {}},
            idempotency_key=check_id)
        if not created:
            return {"code": 0, "job_id": job["job_id"], "job": job}
        thread = threading.Thread(target=_run_target_preparation,
                                  args=(job["job_id"], candidate),
                                  name=f"database-target-prepare-{job['job_id'][:8]}", daemon=True)
        with _WORKERS_LOCK:
            _WORKERS[job["job_id"]] = thread
        thread.start()
        return {"code": 0, "job_id": job["job_id"], "job": STORAGE_JOBS.get(job["job_id"])}
    except Exception as error:
        return {"code": 1, "msg": str(error)}


def _snapshot_sqlite_file(source_path, target_path):
    uri = f"file:{os.path.abspath(source_path)}?mode=ro"
    source = sqlite3.connect(uri, uri=True, timeout=30)
    target = sqlite3.connect(target_path, timeout=30)
    try:
        source.backup(target, pages=1000, sleep=0.01)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target.commit()
        result = target.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite 备份完整性检查失败：{source_path}")
    finally:
        target.close()
        source.close()


def _sqlite_integrity(path):
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite 完整性检查失败：{path}")
    finally:
        connection.close()


def _issue_prepared_check(job_id, candidate, payload, archive, manifest):
    refreshed = _database_check(candidate, target_prepared=True)
    if not refreshed.get("ok"):
        raise RuntimeError("SQLite 空目标复检未通过")
    new_check, _ = STORAGE_JOBS.create(
        _JOB_KIND,
        {"candidate": _safe_settings(candidate), "fingerprint": _fingerprint(candidate),
         "source_fingerprint": payload["source_fingerprint"],
         "target_identity": refreshed.get("target_identity"),
         "preparation_job_id": job_id}, idempotency_key=f"{job_id}:prepared")
    refreshed["check_id"] = new_check["job_id"]
    STORAGE_JOBS.update(new_check["job_id"], status="READY", stage="READY",
                        result=refreshed, message="SQLite 空目标能力与空库检查通过",
                        progress=1)
    STORAGE_JOBS.update(job_id, status="SUCCEEDED", stage="SUCCEEDED",
                        result={"archive": archive, "files": manifest,
                                "new_check_id": new_check["job_id"]},
                        message="SQLite 目标准备完成，可开始迁移", progress=1)


def _recover_target_preparation(job):
    """恢复崩溃的 SQLite 目标准备；远程源仍是唯一活动数据库。"""
    job_id = job["job_id"]
    payload = job.get("payload") or {}
    result = job.get("result") or {}
    status = result.get("resume_stage") or job.get("status")
    if not hmac.compare_digest(payload.get("source_fingerprint", ""),
                               _fingerprint(_current_settings())):
        STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                            error="活动数据库配置已变化，未自动处理旧 SQLite 文件",
                            message="目标准备来源已变化；旧文件与归档均保留，需人工核对")
        return
    if status in {"PREPARING_TARGET", "TARGET_BACKED_UP"}:
        STORAGE_JOBS.update(job_id, status="INTERRUPTED", stage="INTERRUPTED",
                            message="进程在退役边界前中断；旧 SQLite 文件仍保留，可重新检查")
        return

    candidate = {"type": "sqlite"}
    archive = result.get("archive")
    manifest = result.get("files") or []
    expected_paths = payload.get("target_files") or []
    if set(item.get("source") for item in manifest) != set(expected_paths):
        raise RuntimeError("目标准备恢复失败：归档清单不完整")
    for item in manifest:
        backup = item.get("backup")
        if (not backup or not os.path.isfile(backup)
                or _sha256_file(backup) != item.get("backup_sha256")):
            raise RuntimeError("目标准备恢复失败：归档备份缺失或摘要不符")

    from app.db.sqlite_files import database_path
    target_path = database_path()
    allow_empty_target = status in {"OLD_SQLITE_REMOVED", "EMPTY_TARGET_READY"}
    manifest_by_path = {item["source"]: item for item in manifest}
    for path in expected_paths:
        expected = manifest_by_path[path]["identity"]
        actual = _sqlite_file_set_identity(path)
        for suffix, identity in actual.items():
            if identity is None or identity == expected.get(suffix):
                continue
            if path == target_path and allow_empty_target and not suffix:
                if _sqlite_objects(path):
                    raise RuntimeError("目标准备恢复失败：固定 SQLite 目标已出现业务对象")
                continue
            raise RuntimeError(f"目标准备恢复失败：文件身份变化：{path + suffix}")
        for suffix in ("-wal", "-shm", ""):
            try:
                os.unlink(path + suffix)
            except FileNotFoundError:
                pass
        _fsync_directory(os.path.dirname(path))

    if status == "RETIRING_SQLITE":
        STORAGE_JOBS.update(job_id, status="OLD_SQLITE_REMOVED", stage="OLD_SQLITE_REMOVED",
                            result={"archive": archive, "files": manifest},
                            message="启动恢复已完成旧 SQLite 文件退役")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if not os.path.exists(target_path):
        with open(target_path, "xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    elif not allow_empty_target and target_path not in manifest_by_path:
        raise RuntimeError("目标准备恢复失败：出现任务清单外的 SQLite 目标")
    if _sqlite_objects(target_path):
        raise RuntimeError("目标准备恢复失败：固定 SQLite 目标不是空库")
    connection = sqlite3.connect(target_path, timeout=30)
    try:
        connection.execute("PRAGMA user_version=0")
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("目标准备恢复失败：空目标完整性检查未通过")
    finally:
        connection.close()
    with open(target_path, "rb") as stream:
        os.fsync(stream.fileno())
    _fsync_directory(os.path.dirname(target_path))
    STORAGE_JOBS.update(job_id, status="EMPTY_TARGET_READY", stage="EMPTY_TARGET_READY",
                        result={"archive": archive, "files": manifest,
                                "target_identity": _target_identity(candidate)},
                        message="启动恢复已重建固定 SQLite 空目标")
    _issue_prepared_check(job_id, candidate, payload, archive, manifest)


def _run_target_preparation(job_id, candidate):
    try:
        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            job = STORAGE_JOBS.get(job_id)
            if not job:
                return
            _ensure_other_jobs_idle(job_id)
            payload = job["payload"]
            if not hmac.compare_digest(payload["source_fingerprint"],
                                       _fingerprint(_current_settings())):
                raise ValueError("活动数据库配置已变化，请重新检查来源")
            STORAGE_JOBS.update(job_id, status="PREPARING_TARGET", stage="PREPARING_TARGET",
                                message="正在复核并备份旧 SQLite 文件", progress=0.05)
            from config import Config
            config_path = Config().get_config_path()
            current_files = _sqlite_target_files(config_path)
            expected_files = payload.get("target_files") or []
            if current_files != expected_files:
                raise ValueError("待备份 SQLite 文件清单已变化，请重新检查")
            for path in current_files:
                expected_identity = (payload.get("target_file_identities") or {}).get(path)
                if _sqlite_file_set_identity(path) != expected_identity:
                    raise ValueError(f"检查后 SQLite 文件身份发生变化：{path}")
            archive = os.path.join(config_path, ".maintenance", "database-retired", job_id)
            _secure_directory(os.path.dirname(archive))
            os.makedirs(archive, mode=0o700, exist_ok=False)
            try:
                os.chmod(archive, 0o700)
            except OSError:
                pass
            total_bytes = sum(os.path.getsize(path + suffix)
                              for path in current_files
                              for suffix in ("", "-wal", "-shm")
                              if os.path.isfile(path + suffix))
            if shutil.disk_usage(archive).free < total_bytes * 2 + 64 * 1024 * 1024:
                raise RuntimeError("旧 SQLite 文件归档空间不足，未删除任何文件")
            manifest = []
            for index, path in enumerate(current_files, start=1):
                if not os.path.isfile(path):
                    raise RuntimeError(f"旧 SQLite 主文件不存在：{path}")
                before = _sqlite_file_set_identity(path)
                _sqlite_integrity(path)
                backup_path = os.path.join(archive, f"{index}-{os.path.basename(path)}.sqlite3")
                _snapshot_sqlite_file(path, backup_path)
                _secure_file(backup_path)
                after = _sqlite_file_set_identity(path)
                if before != after:
                    raise RuntimeError(f"备份期间 SQLite 文件身份变化：{path}")
                manifest.append({"source": path, "backup": backup_path,
                                 "identity": before, "backup_size": os.path.getsize(backup_path),
                                 "backup_sha256": _sha256_file(backup_path)})
                STORAGE_JOBS.update(job_id, status="TARGET_BACKED_UP", stage="TARGET_BACKED_UP",
                                    result={"archive": archive, "files": manifest},
                                    message=f"已备份 {index}/{len(current_files)} 个 SQLite 文件",
                                    progress=0.1 + index / max(len(current_files), 1) * 0.35)
            if _sqlite_target_files(config_path) != current_files:
                raise RuntimeError("删除前 SQLite 文件清单发生变化，保留全部源文件")
            for path in current_files:
                if _sqlite_file_set_identity(path) != next(item["identity"] for item in manifest
                                                           if item["source"] == path):
                    raise RuntimeError(f"删除前 SQLite 文件身份变化：{path}")
            expected_status = "TARGET_BACKED_UP" if manifest else "PREPARING_TARGET"
            retiring = STORAGE_JOBS.update(
                job_id, status="RETIRING_SQLITE", stage="RETIRING_SQLITE",
                message="已进入旧 SQLite 文件退役边界", expected_status=expected_status)
            if not retiring:
                raise RuntimeError("SQLite 目标准备状态已变化，拒绝删除旧文件")
            if STORAGE_JOBS.cancel_requested(job_id):
                STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                    result={"archive": archive, "files": manifest},
                                    message="在删除旧 SQLite 文件前取消；原文件保持不变")
                return
            for path in current_files:
                for suffix in ("-wal", "-shm", ""):
                    try:
                        os.unlink(path + suffix)
                    except FileNotFoundError:
                        pass
            remaining = [path for path in current_files
                         if any(os.path.exists(path + suffix) for suffix in ("", "-wal", "-shm"))]
            if remaining:
                raise RuntimeError(f"旧 SQLite 文件未能全部删除：{', '.join(remaining)}")
            for path in current_files:
                _fsync_directory(os.path.dirname(path))
            STORAGE_JOBS.update(job_id, status="OLD_SQLITE_REMOVED", stage="OLD_SQLITE_REMOVED",
                                result={"archive": archive, "files": manifest},
                                message="旧 SQLite 文件已备份并删除", progress=0.75)
            from app.db.sqlite_files import database_path
            target_path = database_path(config_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with open(target_path, "xb") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            connection = sqlite3.connect(target_path, timeout=30)
            try:
                connection.execute("PRAGMA user_version=0")
                connection.commit()
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise RuntimeError("新建 SQLite 空目标完整性检查失败")
            finally:
                connection.close()
            with open(target_path, "rb") as stream:
                os.fsync(stream.fileno())
            _fsync_directory(os.path.dirname(target_path))
            if _sqlite_objects(target_path):
                raise RuntimeError("新建 SQLite 文件不是空目标")
            STORAGE_JOBS.update(job_id, status="EMPTY_TARGET_READY", stage="EMPTY_TARGET_READY",
                                result={"archive": archive, "files": manifest,
                                        "target_identity": _target_identity(candidate)},
                                message="固定 SQLite 空目标已准备完成，正在签发新检查凭证",
                                progress=0.9)
            _issue_prepared_check(job_id, candidate, payload, archive, manifest)
    except Exception as error:
        current = STORAGE_JOBS.get(job_id) or {}
        current_status = current.get("status")
        if current_status in {"RETIRING_SQLITE", "OLD_SQLITE_REMOVED", "EMPTY_TARGET_READY",
                              "RECOVERY_REQUIRED"}:
            result = dict(current.get("result") or {})
            result.setdefault("resume_stage", current.get("stage") or current_status)
            STORAGE_JOBS.update(
                job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                error=_safe_error(error, candidate), result=result,
                message="SQLite 目标准备在旧文件退役后失败；归档保留，远程源配置不变")
        else:
            STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                error=_safe_error(error, candidate),
                                result=current.get("result"),
                                message="SQLite 目标准备失败；远程活动数据库配置保持不变")
    finally:
        with _WORKERS_LOCK:
            _WORKERS.pop(job_id, None)


def _normalize_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return {"float": value.hex()}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, (datetime, date)):
        return {"date": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": base64.b64encode(bytes(value)).decode("ascii")}
    return {"type": type(value).__name__, "value": str(value)}


def _ordered_table_select(connection, table):
    return create_backend(connection.dialect.name).ordered_select(table)


def _table_digest(connection, table):
    digest = hashlib.sha256()
    count = 0
    backend = create_backend(connection.dialect.name)
    for batch in backend.read_batches(connection, table):
        for row in batch:
            encoded = json.dumps(
                [_normalize_value(row[column.name]) for column in table.columns],
                ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            digest.update(encoded.encode("utf-8", "surrogatepass"))
            digest.update(b"\n")
            count += 1
    return count, digest.hexdigest()


def _copy_table(source_connection, target_engine, table):
    source_backend = create_backend(source_connection.dialect.name)
    target_backend = create_backend(target_engine.dialect.name)
    copied = 0
    batch = []
    batch_bytes = 0
    max_packet = target_backend.max_payload_bytes(target_engine)
    byte_budget = _BATCH_BYTES
    if max_packet is not None:
        byte_budget = max(64 * 1024, int(max_packet * 0.5))

    def flush():
        nonlocal copied, batch, batch_bytes
        if not batch:
            return
        with target_engine.begin() as target_connection:
            target_backend.insert_many(target_connection, table, batch)
        copied += len(batch)
        batch = []
        batch_bytes = 0

    for source_batch in source_backend.read_batches(source_connection, table):
        for mapping in source_batch:
            row_bytes = 64
            for value in mapping.values():
                if value is None:
                    row_bytes += 4
                elif isinstance(value, (bytes, bytearray, memoryview)):
                    row_bytes += len(value)
                elif isinstance(value, str):
                    row_bytes += len(value.encode("utf-8", "surrogatepass"))
                else:
                    row_bytes += len(str(value).encode("utf-8", "surrogatepass"))
            if max_packet is not None and row_bytes > max_packet * 0.9:
                raise RuntimeError(f"表 {table.name} 存在超过目标 max_allowed_packet 的单行记录")
            if batch and (len(batch) >= _BATCH_SIZE or batch_bytes + row_bytes > byte_budget):
                flush()
            batch.append(mapping)
            batch_bytes += row_bytes
    flush()
    return copied


def _validate_source_shape(connection, metadata):
    inspector = inspect(connection)
    available = {name.upper() for name in inspector.get_table_names()}
    required = {name.upper() for name in metadata.tables}
    missing = required - available
    if missing:
        raise RuntimeError(f"源数据库缺少应用表：{', '.join(sorted(missing))}")
    unexpected = available - required - {"ALEMBIC_VERSION", "__NASTOOLS_DATABASE_LAYOUT"}
    if unexpected:
        raise RuntimeError(f"源数据库含未纳入迁移清单的表：{', '.join(sorted(unexpected))}")
    views = inspector.get_view_names()
    if views:
        raise RuntimeError("源数据库含未纳入迁移清单的视图：" + ", ".join(sorted(views)[:100]))
    try:
        materialized_views = inspector.get_materialized_view_names()
    except (AttributeError, NotImplementedError):
        materialized_views = []
    if materialized_views:
        raise RuntimeError("源数据库含未纳入迁移清单的物化视图："
                           + ", ".join(sorted(materialized_views)[:100]))
    if connection.dialect.name == "postgresql":
        default_schema = inspector.default_schema_name
        for schema in inspector.get_schema_names():
            if (schema in {"information_schema", "pg_catalog", default_schema}
                    or schema.startswith("pg_toast")):
                continue
            tables = inspector.get_table_names(schema=schema)
            views = inspector.get_view_names(schema=schema)
            if tables or views:
                names = [f"{schema}.{name}" for name in [*tables, *views]]
                raise RuntimeError("源数据库含未纳入迁移清单的 schema 对象："
                                   + ", ".join(sorted(names)[:100]))
    for table in metadata.sorted_tables:
        columns = {column["name"].upper() for column in inspector.get_columns(table.name)}
        expected = {column.name.upper() for column in table.columns}
        if columns != expected:
            raise RuntimeError(f"源表 {table.name} 字段与统一结构不一致，拒绝静默丢列")
    if "ALEMBIC_VERSION" not in available:
        raise RuntimeError("源数据库缺少统一结构版本，拒绝迁移")
    head = current_schema_revision()
    versions = [row[0] for row in connection.execute(
        text("SELECT version_num FROM alembic_version"))]
    if versions != [head]:
        raise RuntimeError("源数据库结构版本不是当前版本，请先完成结构升级")


def _lock_target_database(engine):
    """在远程目标完成校验前锁住所有应用表，阻止外部并发写入。"""
    if engine.dialect.name == "sqlite":
        return None, None
    connection = engine.connect()
    transaction = None
    try:
        preparer = engine.dialect.identifier_preparer
        table_names = [preparer.format_table(table) for table in Base.metadata.sorted_tables]
        table_names.append(preparer.quote("alembic_version"))
        if engine.dialect.name == "postgresql":
            transaction = connection.begin()
            connection.execute(text("SET LOCAL lock_timeout = '10s'"))
            for table_name in table_names:
                connection.exec_driver_sql(
                    f"LOCK TABLE {table_name} IN ACCESS EXCLUSIVE MODE")
        elif engine.dialect.name in {"mysql", "mariadb"}:
            locks = ", ".join(f"{table_name} WRITE" for table_name in table_names)
            connection.exec_driver_sql(f"LOCK TABLES {locks}")
        else:
            raise RuntimeError(f"无法保证目标数据库写入隔离：{engine.dialect.name}")
        return connection, transaction
    except Exception as error:
        if transaction is not None:
            transaction.rollback()
        connection.close()
        raise RuntimeError("无法独占锁定远程迁移目标，拒绝提交切换") from error


def _unlock_target_database(engine, connection, transaction):
    if connection is None:
        return
    try:
        if engine.dialect.name in {"mysql", "mariadb"}:
            connection.exec_driver_sql("UNLOCK TABLES")
        if transaction is not None and transaction.is_active:
            transaction.rollback()
    finally:
        connection.close()


def _snapshot_source(settings, job_id):
    from config import Config
    if settings["type"] != "sqlite":
        engine = DATABASE.engine
        if engine.pool.checkedout():
            raise RuntimeError("仍有数据库连接未排空，不能建立迁移快照")
        connection = engine.connect().execution_options(isolation_level="REPEATABLE READ")
        transaction = connection.begin()
        return engine, connection, transaction, None

    from app.db.backends.sqlite import SQLiteBackend
    source_path = SQLiteBackend.path()
    snapshot_dir = os.path.join(Config().get_config_path(), ".maintenance", "database-snapshots")
    _secure_directory(snapshot_dir)
    snapshot_path = os.path.join(snapshot_dir, f"{job_id}.sqlite3")
    source = sqlite3.connect(f"file:{os.path.abspath(source_path)}?mode=ro", uri=True, timeout=60)
    target = sqlite3.connect(snapshot_path, timeout=60)
    try:
        source.backup(target, pages=1000, sleep=0.01)
        target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target.commit()
        if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("SQLite 源数据库快照完整性检查失败")
    finally:
        target.close()
        source.close()
    _secure_file(snapshot_path)
    engine = create_engine(URL.create("sqlite", database=snapshot_path),
                           connect_args={"timeout": 60})
    connection = engine.connect()
    transaction = connection.begin()
    return engine, connection, transaction, snapshot_path


def _calibrate_sequences(engine):
    create_backend(engine.dialect.name).calibrate_sequences(engine, Base.metadata)


def _verify_probe(engine):
    with engine.begin() as connection:
        connection.execute(text("SELECT 1")).scalar_one()
        for table_name in ("CONFIG_USERS", "SYSTEM_DICT", "RECOGNITION_REQUEST",
                           "MEDIASYNC_ITEMS", "MEDIASYNC_STATISTICS"):
            table = Base.metadata.tables[table_name]
            connection.execute(select(func.count()).select_from(table)).scalar_one()


def _write_migration_marker(engine, job_id, source_fp, target_fp):
    table = DATABASEMIGRATIONMARKER.__table__
    values = {"ID": job_id, "STATUS": "complete", "SOURCE_FINGERPRINT": source_fp,
              "TARGET_FINGERPRINT": target_fp,
              "COMPLETED_AT": datetime.utcnow().isoformat(timespec="seconds") + "Z"}
    with engine.begin() as connection:
        connection.execute(insert(table).values(**values))


def _write_schema_revision(engine):
    """给按当前 metadata 新建的迁移目标写入 Alembic 当前 head。"""
    revision_table = Table(
        "alembic_version", MetaData(),
        Column("version_num", String(32), primary_key=True),
    )
    with engine.begin() as connection:
        if inspect(connection).has_table("alembic_version"):
            raise RuntimeError("迁移目标意外包含结构版本表")
        revision_table.create(connection)
        connection.execute(insert(revision_table).values(version_num=current_schema_revision()))


def _publish_sqlite(staged_path, job_id, expected_identity):
    from app.db.sqlite_files import database_path
    target_path = database_path()
    identity = _target_identity({"type": "sqlite"})
    if identity != expected_identity:
        raise RuntimeError("SQLite 目标文件身份在检查后变化，拒绝发布")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if os.path.exists(target_path):
        # 目标准备可能生成了经过空库检查的空占位文件；保留回滚副本。
        previous = os.path.join(os.path.dirname(staged_path), "previous-empty-target.sqlite3")
        os.replace(target_path, previous)
    else:
        previous = None
    try:
        os.replace(staged_path, target_path)
        descriptor = os.open(target_path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory_fd = os.open(os.path.dirname(target_path), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _sqlite_integrity(target_path)
        return {"path": target_path, "previous_empty": previous}
    except Exception:
        if os.path.exists(target_path):
            os.unlink(target_path)
        if previous and os.path.exists(previous):
            os.replace(previous, target_path)
        raise


def _rollback_sqlite_publish(published):
    if not published:
        return
    path = published["path"]
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass
    previous = published.get("previous_empty")
    if previous and os.path.exists(previous):
        os.replace(previous, path)


def _save_database_config(candidate, source_fingerprint):
    from config import CONFIG_LOCK, Config
    import ruamel.yaml

    config_path = Config()._config_path
    with CONFIG_LOCK:
        with open(config_path, "r", encoding="utf-8") as stream:
            fresh = ruamel.yaml.YAML().load(stream) or {}
        current = fresh.get("database") or {"type": "sqlite"}
        current.setdefault("type", "sqlite")
        current["type"] = str(current.get("type") or "sqlite").lower()
        if current["type"] != "sqlite":
            current["port"] = int(current.get("port") or (
                5432 if current["type"] == "postgresql" else 3306))
            current["connect_timeout"] = int(current.get("connect_timeout") or 5)
            current["ssl"] = copy.deepcopy(current.get("ssl") or {})
        if not hmac.compare_digest(_fingerprint(current), source_fingerprint):
            raise RuntimeError("活动数据库配置已变化，拒绝覆盖并发设置修改")
        fresh["database"] = copy.deepcopy(candidate)
        if not Config().save_config(fresh):
            raise RuntimeError("数据库配置保存失败")


def _run_migration(job_id, candidate, source_fingerprint, target_fingerprint,
                   target_identity, check_id):
    published = None
    source_engine = source_connection = source_transaction = None
    target_engine = None
    target_lock_connection = target_lock_transaction = None
    snapshot_path = None
    staging_dir = None
    remote_target_touched = False
    config_committed = False
    try:
        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            job = STORAGE_JOBS.get(job_id)
            if not job or job.get("status") != "QUIESCING":
                raise RuntimeError("数据库迁移任务状态已变化")
            if not hmac.compare_digest(_fingerprint(_current_settings()), source_fingerprint):
                raise RuntimeError("活动数据库配置已变化，迁移已取消")
            if STORAGE_JOBS.cancel_requested(job_id):
                STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                    message="迁移开始前已取消")
                return
            _ensure_other_jobs_idle(job_id)
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "QUIESCING")
            STORAGE_JOBS.update(job_id, status="SNAPSHOTTING", stage="SNAPSHOTTING",
                                message="正在建立源数据库一致性快照", progress=0.03)
            source_engine, source_connection, source_transaction, snapshot_path = \
                _snapshot_source(_current_settings(), job_id)
            _validate_source_shape(source_connection, Base.metadata)

            if candidate["type"] == "sqlite":
                from config import Config
                staging_dir = os.path.join(Config().get_config_path(), "db", ".staging",
                                           f"database-migration-{job_id}")
                os.makedirs(staging_dir, mode=0o700, exist_ok=False)
                try:
                    os.chmod(staging_dir, 0o700)
                except OSError:
                    pass
                staged_path = os.path.join(staging_dir, "nastools.staged.sqlite3")
                target_engine = _new_engine(candidate, sqlite_path=staged_path)
            else:
                target_engine = _new_engine(candidate)
            if candidate["type"] != "sqlite":
                objects = _database_objects(target_engine)
                if objects:
                    raise RuntimeError("目标数据库检查后出现新对象，迁移已中止且未清理外部对象")
            if candidate["type"] != "sqlite":
                remote_target_touched = True
            Base.metadata.create_all(target_engine)
            expected_counts = {}
            tables = Base.metadata.sorted_tables
            total_rows = 0
            for index, table in enumerate(tables, start=1):
                if STORAGE_JOBS.cancel_requested(job_id):
                    if remote_target_touched:
                        result = {**(STORAGE_JOBS.get(job_id).get("result") or {}),
                                  "target_may_contain_partial_data": True}
                        message = "已取消；远程目标保留本任务创建的部分数据，请勿复用该目标"
                    else:
                        result = None
                        message = "在数据表边界取消，活动数据库配置保持原值"
                    STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                        result=result, message=message)
                    return
                count = _copy_table(source_connection, target_engine, table)
                expected_counts[table.name] = count
                total_rows += count
                STORAGE_JOBS.update(job_id, status="MIGRATING", stage="MIGRATING",
                                    message=f"已复制 {index}/{len(tables)} 张表，共 {total_rows} 条记录",
                                    progress=0.1 + index / len(tables) * 0.58)

            STORAGE_JOBS.update(job_id, status="VALIDATING", stage="VALIDATING",
                                message="正在逐表校验行数和完整内容摘要", progress=0.72)
            validation = {}
            for index, table in enumerate(tables, start=1):
                source_count, source_digest = _table_digest(source_connection, table)
                with target_engine.connect() as target_connection:
                    target_count, target_digest = _table_digest(target_connection, table)
                if (source_count, source_digest) != (target_count, target_digest):
                    raise RuntimeError(f"表 {table.name} 的行数或内容摘要不一致")
                if source_count != expected_counts.get(table.name):
                    raise RuntimeError(f"表 {table.name} 的复制进度与源行数不一致")
                validation[table.name] = {"rows": source_count, "sha256": source_digest}
                STORAGE_JOBS.update(job_id, stage="VALIDATING",
                                    message=f"已校验 {index}/{len(tables)} 张表",
                                    progress=0.72 + index / len(tables) * 0.2)

            _write_schema_revision(target_engine)
            _calibrate_sequences(target_engine)
            _verify_probe(target_engine)
            _write_migration_marker(target_engine, job_id, source_fingerprint,
                                    target_fingerprint)
            _validate_source_shape(target_engine, Base.metadata)
            target_lock_connection, target_lock_transaction = _lock_target_database(target_engine)
            if target_lock_connection is None:
                target_lock_connection = target_engine.connect()
                close_target_validation_connection = True
            else:
                close_target_validation_connection = False
            try:
                for table in tables:
                    if table is DATABASEMIGRATIONMARKER.__table__:
                        continue
                    source_count, source_digest = _table_digest(source_connection, table)
                    target_count, target_digest = _table_digest(target_lock_connection, table)
                    if (source_count, source_digest) != (target_count, target_digest):
                        raise RuntimeError(f"独占锁定后表 {table.name} 的内容发生变化")
                marker = target_lock_connection.execute(select(DATABASEMIGRATIONMARKER.__table__).where(
                    DATABASEMIGRATIONMARKER.ID == job_id)).mappings().one()
                if (marker["STATUS"] != "complete"
                        or marker["SOURCE_FINGERPRINT"] != source_fingerprint
                        or marker["TARGET_FINGERPRINT"] != target_fingerprint):
                    raise RuntimeError("目标数据库迁移完成标记校验失败")
            finally:
                if close_target_validation_connection:
                    target_lock_connection.close()
                    target_lock_connection = None
            STORAGE_JOBS.update(job_id, status="VALIDATED", stage="VALIDATED",
                                result={"source_fingerprint": source_fingerprint,
                                        "target_fingerprint": target_fingerprint,
                                        "source": _safe_settings(_current_settings()),
                                        "target": _safe_settings(candidate),
                                        "rows": total_rows, "tables": validation,
                                        "snapshot": snapshot_path},
                                message="目标结构、数据摘要和业务读取探针均已通过",
                                progress=0.95)
            if STORAGE_JOBS.cancel_requested(job_id):
                result = STORAGE_JOBS.get(job_id).get("result") or {}
                if remote_target_touched:
                    result = {**result, "target_validated": True}
                    message = "已取消；远程目标数据已校验但配置未切换，目标不能作为空库复用"
                else:
                    message = "配置提交前已取消，活动数据库保持原值"
                STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                    result=result, message=message)
                return
            next_status = "VALIDATED"
            if candidate["type"] == "sqlite":
                claimed = STORAGE_JOBS.update(
                    job_id, status="PUBLISHING", stage="PUBLISHING",
                    message="已进入 SQLite 原子发布边界", expected_status="VALIDATED")
                if not claimed:
                    raise RuntimeError("迁移任务状态已变化，拒绝发布 SQLite 目标")
                if STORAGE_JOBS.cancel_requested(job_id):
                    STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                        message="在 SQLite 发布前取消，活动数据库配置保持原值")
                    return
                target_engine.dispose()
                target_engine = None
                _secure_file(staged_path)
                _sqlite_integrity(staged_path)
                with open(staged_path, "rb") as stream:
                    os.fsync(stream.fileno())
                published = _publish_sqlite(staged_path, job_id, target_identity)
                STORAGE_JOBS.update(job_id, status="PUBLISHING", stage="PUBLISHING",
                                    result={**(STORAGE_JOBS.get(job_id).get("result") or {}),
                                            "sqlite_publish": published},
                                    message="SQLite 单库已原子发布，准备保存配置",
                                    progress=0.97)
                next_status = "PUBLISHING"

            STORAGE_JOBS.set_maintenance_gate("database", job_id, "SAVING_CONFIG")
            claimed = STORAGE_JOBS.update(
                job_id, status="SAVING_CONFIG", stage="SAVING_CONFIG",
                message="正在原子保存新的活动数据库配置", progress=0.98,
                expected_status=next_status)
            if not claimed:
                raise RuntimeError("迁移任务状态已变化，拒绝提交数据库配置")
            if STORAGE_JOBS.cancel_requested(job_id):
                if published:
                    _rollback_sqlite_publish(published)
                    published = None
                STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                    message="配置提交前收到取消请求，活动数据库保持原值")
                return
            _save_database_config(candidate, source_fingerprint)
            config_committed = True
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "RESTART_PENDING")
            STORAGE_JOBS.update(job_id, status="RESTART_PENDING", stage="RESTART_PENDING",
                                result={**(STORAGE_JOBS.get(job_id).get("result") or {}),
                                        "target_fingerprint": target_fingerprint},
                                message="数据库配置已保存，等待受控重启", progress=0.99)
            from app.utils.cache_migration import _schedule_restart
            _schedule_restart()
    except Exception as error:
        current = STORAGE_JOBS.get(job_id) or {}
        try:
            config_committed = config_committed or hmac.compare_digest(
                _fingerprint(_current_settings()), target_fingerprint)
        except Exception:
            pass
        if config_committed:
            STORAGE_JOBS.update(job_id, status="RESTART_PENDING", stage="RESTART_PENDING",
                                error=_safe_error(error, candidate),
                                message="配置已提交但重启未完成；维护状态保持，请重试重启")
        else:
            error_message = "数据库迁移失败；原活动配置保持不变"
            result = current.get("result") or {}
            if remote_target_touched:
                result = {**result, "target_may_contain_partial_data": True}
                error_message = "数据库迁移失败；目标可能含本任务部分数据，请勿复用该目标，活动配置保持不变"
            if published:
                _rollback_sqlite_publish(published)
            STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                error=_safe_error(error, candidate),
                                result=result, message=error_message)
    finally:
        if source_transaction is not None:
            try:
                source_transaction.rollback()
            except Exception:
                pass
        if source_connection is not None:
            source_connection.close()
        if source_engine is not None and snapshot_path:
            source_engine.dispose()
        if target_lock_connection is not None and target_engine is not None:
            try:
                _unlock_target_database(target_engine, target_lock_connection,
                                        target_lock_transaction)
            except Exception:
                pass
        if target_engine is not None:
            target_engine.dispose()
        current = STORAGE_JOBS.get(job_id) or {}
        if staging_dir and (config_committed or current.get("status") in {"FAILED", "CANCELLED"}):
            shutil.rmtree(staging_dir, ignore_errors=True)
        current = STORAGE_JOBS.get(job_id) or {}
        if current.get("status") in {"FAILED", "CANCELLED"}:
            try:
                with STORAGE_JOBS.maintenance_lock(exclusive=True):
                    gate = STORAGE_JOBS.maintenance_gate("database")
                    if gate and gate.get("job_id") == job_id:
                        STORAGE_JOBS.clear_maintenance_gate("database", job_id)
            except Exception:
                pass
        with _WORKERS_LOCK:
            _WORKERS.pop(job_id, None)


def _ensure_other_jobs_idle(except_job_id):
    for job in STORAGE_JOBS.active():
        if job["job_id"] == except_job_id or job.get("status") in {"CHECKING", "READY"}:
            continue
        raise RuntimeError("已有缓存或数据库维护任务正在运行")
    for kind, gate in STORAGE_JOBS.maintenance_gates().items():
        if kind == "database" and gate.get("job_id") == except_job_id:
            continue
        raise RuntimeError("已有存储维护闸门未解除")


def start_database_migration(values):
    candidate = None
    try:
        values = dict(values or {})
        check_id = values.get("check_id")
        check = STORAGE_JOBS.get(check_id) if check_id else None
        if not check or check.get("kind") != _JOB_KIND or check.get("status") != "READY":
            raise ValueError("数据库目标检查已失效，请重新检查")
        if time.time() - float(check.get("created_at") or 0) > 600:
            raise ValueError("数据库目标检查已过期，请重新检查")
        if STORAGE_JOBS.cancel_requested(check_id):
            STORAGE_JOBS.update(check_id, status="CANCELLED", stage="CANCELLED",
                                message="已取消的检查凭证不能启动迁移")
            raise ValueError("数据库目标检查已取消")
        candidate = _normalize_candidate(values)
        payload = check.get("payload") or {}
        report = check.get("result") or {}
        if report.get("target_preparation_required"):
            raise ValueError("SQLite 目标需先完成备份和空库准备")
        if not report.get("ok"):
            raise ValueError("目标检查未通过")
        if not hmac.compare_digest(payload.get("fingerprint", ""), _fingerprint(candidate)):
            raise ValueError("数据库连接参数已变化，请重新检查目标")
        source_fingerprint = _fingerprint(_current_settings())
        if not hmac.compare_digest(payload.get("source_fingerprint", ""), source_fingerprint):
            raise ValueError("活动数据库配置已变化，请重新检查迁移来源")
        if _target_identity(candidate) != payload.get("target_identity"):
            raise ValueError("目标数据库身份在检查后变化，请重新检查")
        claimed = STORAGE_JOBS.update(check_id, status="QUIESCING", stage="QUIESCING",
                                      message="数据库检查凭证已锁定，准备启动迁移",
                                      expected_status="READY")
        if not claimed:
            raise ValueError("数据库检查凭证已经使用")
        thread = threading.Thread(
            target=_run_migration,
            args=(check_id, candidate, source_fingerprint, _fingerprint(candidate),
                  payload.get("target_identity"), check_id),
            name=f"database-migration-{check_id[:8]}", daemon=True)
        with _WORKERS_LOCK:
            _WORKERS[check_id] = thread
        thread.start()
        return {"code": 0, "job_id": check_id, "job": STORAGE_JOBS.get(check_id)}
    except Exception as error:
        return {"code": 1, "msg": _safe_error(error, candidate)}


def get_database_job(job_id):
    return STORAGE_JOBS.get(job_id) if job_id else None


def cancel_database_job(job_id):
    job = get_database_job(job_id)
    if not job or job.get("kind") not in {
            _JOB_KIND, _PREPARE_KIND, "database_space_reclaim", "database_backup",
            "database_backup_check", "database_restore"}:
        return False
    if job.get("kind") == "database_backup_check" and job.get("status") == "READY":
        return bool(STORAGE_JOBS.update(
            job_id, status="CANCELLED", stage="CANCELLED",
            message="已取消备份检查凭证，不能再使用该检查结果恢复数据库"))
    return STORAGE_JOBS.request_cancel(job_id)


def retry_database_restart(job_id):
    job = get_database_job(job_id)
    if (not job or job.get("kind") not in {_JOB_KIND, "database_restore"}
            or job.get("status") != "RESTART_PENDING"):
        if job and job.get("kind") == "database_restore":
            from app.db.maintenance import retry_database_restore_restart
            try:
                return retry_database_restore_restart(job_id)
            except Exception:
                return False
        return False
    if job.get("kind") == "database_restore":
        from app.db.maintenance import retry_database_restore_restart
        try:
            return retry_database_restore_restart(job_id)
        except Exception:
            return False
    from app.utils.cache_migration import _schedule_restart
    _schedule_restart()
    return True


def verify_database_startup():
    """在启动业务任务之前校验已提交的切换并恢复维护闸门。"""
    with STORAGE_JOBS.maintenance_lock(exclusive=True):
        from app.db.maintenance import cleanup_database_artifacts
        cleanup_database_artifacts()
        for preparation in STORAGE_JOBS.active(_PREPARE_KIND):
            if preparation.get("status") not in {
                    "PREPARING_TARGET", "TARGET_BACKED_UP", "RETIRING_SQLITE",
                    "OLD_SQLITE_REMOVED", "EMPTY_TARGET_READY", "RECOVERY_REQUIRED"}:
                continue
            try:
                _recover_target_preparation(preparation)
            except Exception as error:
                STORAGE_JOBS.update(
                    preparation["job_id"], status="RECOVERY_REQUIRED",
                    stage="RECOVERY_REQUIRED", error=_safe_error(error),
                    message="目标准备中断且无法安全自动恢复；远程活动数据库配置保持不变")
        gate = STORAGE_JOBS.maintenance_gate("database")
        maintenance_kinds = {
            "database_space_reclaim", "database_backup", "database_backup_check",
            "database_restore",
        }
        if gate:
            gated_job = STORAGE_JOBS.get(gate.get("job_id")) if gate.get("job_id") else None
            if gated_job and gated_job.get("kind") in maintenance_kinds:
                from app.db.maintenance import verify_database_maintenance_startup
                verify_database_maintenance_startup(gated_job)
                gate = STORAGE_JOBS.maintenance_gate("database")
        if not gate:
            from app.db.maintenance import verify_database_maintenance_startup
            for orphan in STORAGE_JOBS.active():
                if orphan.get("kind") in maintenance_kinds:
                    verify_database_maintenance_startup(orphan)
            gate = STORAGE_JOBS.maintenance_gate("database")
        jobs = STORAGE_JOBS.active(_JOB_KIND)
        pending = next((job for job in jobs if job.get("status") in {
            "SAVING_CONFIG", "RESTART_PENDING", "VERIFYING_STARTUP", "RECOVERY_REQUIRED"}), None)
        if not gate and not pending:
            orphan = next((job for job in jobs if job.get("status") == "QUIESCING"), None)
            if orphan and hmac.compare_digest(
                    _fingerprint(_current_settings()),
                    (orphan.get("payload") or {}).get("source_fingerprint", "")):
                STORAGE_JOBS.update(orphan["job_id"], status="INTERRUPTED",
                                    stage="INTERRUPTED",
                                    message="进程在取得维护闸门前中断；活动数据库配置保持不变")
                return True
        if not gate and pending and hmac.compare_digest(
                _fingerprint(_current_settings()),
                (pending.get("payload") or {}).get("source_fingerprint", "")):
            STORAGE_JOBS.update(pending["job_id"], status="INTERRUPTED",
                                stage="INTERRUPTED",
                                message="配置提交前维护闸门丢失；原活动数据库配置保持不变")
            return True
        if not gate and not pending:
            return True
        if gate and not pending:
            interrupted = STORAGE_JOBS.get(gate.get("job_id")) if gate.get("job_id") else None
            if interrupted and interrupted.get("kind") == _JOB_KIND:
                payload = interrupted.get("payload") or {}
                if hmac.compare_digest(_fingerprint(_current_settings()),
                                       payload.get("source_fingerprint", "")):
                    result = interrupted.get("result") or {}
                    state = interrupted.get("status")
                    candidate = payload.get("candidate") or {}
                    if candidate.get("type") != "sqlite" and state in {"MIGRATING", "VALIDATING"}:
                        result = {**result, "target_may_contain_partial_data": True}
                        message = "进程中断；远程目标可能含部分数据且不能复用，源数据库配置保持不变"
                    elif candidate.get("type") != "sqlite" and state == "VALIDATED":
                        result = {**result, "target_validated": True}
                        message = "进程在提交配置前中断；远程目标已校验，源数据库仍为活动库"
                    elif candidate.get("type") == "sqlite" and state == "PUBLISHING":
                        message = "进程在 SQLite 发布边界中断；源数据库配置保持不变，请重新检查目标"
                    else:
                        message = "进程在配置提交前中断；源数据库配置保持不变"
                    STORAGE_JOBS.update(interrupted["job_id"], status="INTERRUPTED",
                                        stage="INTERRUPTED", result=result, message=message)
                    STORAGE_JOBS.clear_maintenance_gate("database", interrupted["job_id"])
                    return True
        if not gate or not pending or gate.get("job_id") != pending.get("job_id"):
            raise RuntimeError("数据库维护闸门与持久化任务不匹配，保持业务关闭")
        if pending.get("status") == "RECOVERY_REQUIRED":
            raise RuntimeError("数据库切换需要受控恢复，系统保持维护状态")
        payload = pending.get("payload") or {}
        result = pending.get("result") or {}
        current = _current_settings()
        current_fingerprint = _fingerprint(current)
        source_fingerprint = payload.get("source_fingerprint")
        target_fingerprint = result.get("target_fingerprint") or payload.get("fingerprint")
        if hmac.compare_digest(current_fingerprint, source_fingerprint or ""):
            STORAGE_JOBS.update(pending["job_id"], status="INTERRUPTED", stage="INTERRUPTED",
                                message="重启前未提交目标配置；原活动数据库保持不变")
            STORAGE_JOBS.clear_maintenance_gate("database", pending["job_id"])
            return True
        if not hmac.compare_digest(current_fingerprint, target_fingerprint or ""):
            raise RuntimeError("活动数据库配置与待验证目标不匹配，保持维护状态")
        if current["type"] == "sqlite":
            from app.db.sqlite_files import database_path
            if not os.path.isfile(database_path()):
                raise RuntimeError("待验证 SQLite 目标文件缺失，保持维护状态")
        STORAGE_JOBS.update(pending["job_id"], status="VERIFYING_STARTUP",
                            stage="VERIFYING_STARTUP", message="新进程正在验证活动数据库")
        engine = DATABASE.engine
        head = current_schema_revision()
        inspector = inspect(engine)
        tables = {name.upper() for name in inspector.get_table_names()}
        if "ALEMBIC_VERSION" not in tables:
            raise RuntimeError("目标数据库缺少统一结构版本，保持维护状态")
        with engine.connect() as connection:
            versions = [row[0] for row in connection.execute(
                text("SELECT version_num FROM alembic_version"))]
            marker = connection.execute(select(DATABASEMIGRATIONMARKER.__table__).where(
                DATABASEMIGRATIONMARKER.ID == pending["job_id"])).mappings().one_or_none()
            connection.execute(text("SELECT 1")).scalar_one()
        if versions != [head] or not marker or marker["STATUS"] != "complete" \
                or marker["TARGET_FINGERPRINT"] != target_fingerprint:
            STORAGE_JOBS.update(pending["job_id"], status="RECOVERY_REQUIRED",
                                stage="RECOVERY_REQUIRED",
                                message="新数据库启动校验失败，保持维护闸门")
            STORAGE_JOBS.set_maintenance_gate("database", pending["job_id"],
                                              "RECOVERY_REQUIRED")
            raise RuntimeError("数据库版本或迁移完成标记校验失败，保持维护状态")
        STORAGE_JOBS.update(pending["job_id"], status="SUCCEEDED", stage="SUCCEEDED",
                            message="新进程已确认目标数据库和迁移标记，恢复业务访问",
                            progress=1)
        STORAGE_JOBS.clear_maintenance_gate("database", pending["job_id"])
    return True
