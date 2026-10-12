"""统一业务数据库统计、SQLite 空间回收与完整备份恢复。"""

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, inspect, or_, select, text, update

from app.db.manager import DATABASE
from app.db.models import Base, MEDIASYNCSYNCJOB, validate_string_lengths
from app.db.schema import LOGICAL_DOMAIN_TABLES, current_schema_revision
from app.utils.storage_jobs import STORAGE_JOBS, schedule_restart


_RECLAIM_KIND = "database_space_reclaim"
_BACKUP_KIND = "database_backup"
_BACKUP_CHECK_KIND = "database_backup_check"
_RESTORE_KIND = "database_restore"
_STATISTICS_KIND = "database_statistics"
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 200 * 1024 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 2000
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_BATCH_SIZE = 500
_ARTIFACT_RETENTION_SECONDS = 30 * 24 * 60 * 60
_WORKERS = {}
_WORKERS_LOCK = threading.RLock()
_LOCAL_PATH_RE = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^\s:/]+/)*[^\s:,;]+")
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\[^\s:,;]+")


def _config():
    from config import Config
    return Config()


def _settings():
    from app.db.migration import _current_settings, _fingerprint, _safe_settings
    value = _current_settings()
    return value, _fingerprint(value), _safe_settings(value)


def _safe_database_error(error, settings=None):
    from app.db.migration import _safe_error

    value = _safe_error(error, settings)
    value = re.sub(r"(?i)(://)[^/@\s]+@", r"\1***@", value)
    value = _LOCAL_PATH_RE.sub("[服务端路径]", value)
    value = _WINDOWS_PATH_RE.sub("[服务端路径]", value)
    return value[:1500]


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


def _maintenance_root():
    return os.path.join(_config().get_config_path(), ".maintenance")


def _backup_root():
    path = os.path.join(_maintenance_root(), "database-backups")
    _secure_directory(path)
    return path


def _upload_root():
    path = os.path.join(_maintenance_root(), "database-uploads")
    _secure_directory(path)
    return path


def cleanup_database_artifacts(now=None):
    """删除过期上传和备份；独占维护锁会等待正在传输的下载完成。"""
    with STORAGE_JOBS.maintenance_lock(exclusive=True):
        return _cleanup_database_artifacts_locked(now)


def _cleanup_database_artifacts_locked(now=None):
    now = time.time() if now is None else float(now)
    cutoff = now - _ARTIFACT_RETENTION_SECONDS
    for job in STORAGE_JOBS.active(_BACKUP_CHECK_KIND):
        if job.get("status") == "READY" and now - float(job.get("created_at") or now) > 600:
            STORAGE_JOBS.update(job["job_id"], status="CANCELLED", stage="CANCELLED",
                                message="备份检查凭证已过期，请重新检查文件")
    active = STORAGE_JOBS.active()
    protected_ids = {job["job_id"] for job in active}
    for job in active:
        protected_ids.add(f"pre-restore-{job['job_id']}")
        payload = job.get("payload") or {}
        result = job.get("result") or {}
        if payload.get("upload_id"):
            protected_ids.add(payload["upload_id"])
        if result.get("upload_id"):
            protected_ids.add(result["upload_id"])
    removed = 0
    for root in (_backup_root(), _upload_root()):
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            identifier = entry.name.removesuffix(".zip")
            if identifier in protected_ids:
                continue
            try:
                if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(entry.path)
                    removed += 1
            except OSError:
                continue
    return removed


def _sqlite_path():
    from app.db.sqlite_files import database_path
    return database_path()


def _file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _sqlite_stats(path, checkpoint=False):
    if not os.path.isfile(path):
        raise RuntimeError("固定 SQLite 数据库文件不存在")
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        if checkpoint:
            checkpoint_result = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        else:
            checkpoint_result = None
        values = {}
        for key in ("page_size", "page_count", "freelist_count"):
            values[key] = int(connection.execute(f"PRAGMA {key}").fetchone()[0])
        values["integrity"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        connection.close()
    values.update({
        "database_size_bytes": _file_size(path),
        "wal_size_bytes": _file_size(path + "-wal"),
        "shm_size_bytes": _file_size(path + "-shm"),
        "free_pages_bytes": values["page_size"] * values["freelist_count"],
        "checkpoint": list(checkpoint_result) if checkpoint_result is not None else None,
    })
    return values


def _table_counts(engine):
    return DATABASE.table_counts(Base.metadata, engine=engine)


def _estimated_table_counts(engine):
    """Read planner/catalog estimates through the active backend adapter."""
    try:
        return DATABASE.estimated_table_counts(Base.metadata, engine=engine)
    except Exception as error:
        reason = _safe_database_error(error)[:500]
        return ({table.name: None for table in Base.metadata.sorted_tables}, {"_all": reason})


def _sqlite_reclaim_availability(engine, path, stats):
    if STORAGE_JOBS.maintenance_gate("database"):
        return False, "数据库已有维护任务正在运行"
    if any(job.get("status") not in {"CHECKING", "READY"}
           for job in STORAGE_JOBS.active()):
        return False, "另一个存储维护任务正在运行"
    if stats.get("integrity") != "ok":
        return False, "SQLite 完整性检查未通过"
    if not os.path.isfile(path) or not os.access(path, os.W_OK):
        return False, "数据库文件不存在或当前用户没有写入权限"
    directory = os.path.dirname(path)
    if not os.access(directory, os.W_OK):
        return False, "数据库目录不可写"
    try:
        available = shutil.disk_usage(directory).free
    except OSError as error:
        return False, f"无法检查数据库卷可用空间：{str(error)[:300]}"
    required = int(stats.get("database_size_bytes") or 0) * 2 + 64 * 1024 * 1024
    if available < required:
        return False, f"可用空间不足，约需 {required} 字节，当前 {available} 字节"
    return True, None


def _count_tables_with_progress(engine, job_id):
    inspector = inspect(engine)
    available = {name.upper() for name in inspector.get_table_names()}
    required = {name.upper() for name in Base.metadata.tables}
    missing = required - available
    if missing:
        raise RuntimeError(f"数据库缺少应用表：{', '.join(sorted(missing))}")
    tables = Base.metadata.sorted_tables
    counts = {}
    with engine.connect() as connection:
        for index, table in enumerate(tables, start=1):
            counts[table.name] = int(connection.execute(
                select(func.count()).select_from(table)).scalar_one())
            STORAGE_JOBS.update(job_id, status="COUNTING", stage="COUNTING",
                                progress=index / len(tables),
                                message=f"正在统计 {table.name}（{index}/{len(tables)}）")
    return counts


def _run_exact_statistics(job_id, fingerprint):
    try:
        with STORAGE_JOBS.maintenance_lock(exclusive=False, timeout=5):
            if STORAGE_JOBS.maintenance_gate("database"):
                raise RuntimeError("数据库正在维护，精确统计未启动")
            settings = DATABASE._read_settings()
            from app.db.migration import _fingerprint
            if _fingerprint(settings) != fingerprint:
                raise RuntimeError("数据库配置已变化，请重新发起精确统计")
            engine = DATABASE.engine
            if DATABASE._settings != settings:
                raise RuntimeError("活动数据库配置与已保存配置不一致")
            counts = _count_tables_with_progress(engine, job_id)
            STORAGE_JOBS.update(job_id, status="SUCCEEDED", stage="SUCCEEDED",
                                result={"fingerprint": fingerprint, "tables": counts,
                                        "statistics_at": datetime.utcnow().isoformat(
                                            timespec="seconds") + "Z"},
                                progress=1, message="所有应用表的精确记录数统计完成")
    except Exception as error:
        STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                            error=_safe_database_error(error), message="精确统计失败")


def start_database_statistics_refresh(values=None):
    """Start a persistent background task for exact per-table row counts."""
    values = dict(values or {})
    try:
        with STORAGE_JOBS.maintenance_lock(exclusive=False, timeout=2):
            if STORAGE_JOBS.maintenance_gate("database"):
                return {"code": 1, "msg": "数据库正在维护，暂不能刷新精确统计"}
            active = STORAGE_JOBS.active(_STATISTICS_KIND)
            if active:
                return {"code": 0, "job_id": active[0]["job_id"], "job": active[0]}
            settings, fingerprint, _ = _settings()
            if DATABASE._settings is not None and DATABASE._settings != settings:
                return {"code": 1, "msg": "活动数据库配置与已保存配置不一致，暂不能统计"}
            key = str(values.get("idempotency_key") or "").strip()[:128] or None
            job, created = STORAGE_JOBS.create(
                _STATISTICS_KIND, {"fingerprint": fingerprint}, idempotency_key=key)
        if created:
            STORAGE_JOBS.update(job["job_id"], status="COUNTING", stage="COUNTING",
                                message="精确统计任务已启动")
            worker = threading.Thread(target=_run_exact_statistics,
                                      args=(job["job_id"], fingerprint),
                                      name=f"database-statistics-{job['job_id'][:8]}",
                                      daemon=True)
            with _WORKERS_LOCK:
                _WORKERS[job["job_id"]] = worker
            worker.start()
        return {"code": 0, "job_id": job["job_id"],
                "job": STORAGE_JOBS.get(job["job_id"])}
    except Exception as error:
        return {"code": 1, "msg": _safe_database_error(error)}


def get_database_info():
    """返回活动连接、统一结构、表记录数及物理空间统计。"""
    from app.db.migration import _current_settings, _safe_settings

    configured = _current_settings()
    active_settings = DATABASE._settings
    engine = DATABASE._engine
    connected = False
    tables = {}
    table_count_mode = "estimated"
    table_count_reasons = {}
    exact_statistics_at = None
    error = None
    actual_revision = None
    server_version = None
    object_sizes = {"application_data_bytes": None, "application_index_bytes": None,
                    "database_size_bytes": None,
                    "measurement_reason": "数据库未连接"}
    try:
        with STORAGE_JOBS.maintenance_lock(exclusive=False):
            if STORAGE_JOBS.maintenance_gate("database"):
                raise RuntimeError("数据库维护中，暂不读取业务表统计")
            if engine is None:
                engine = DATABASE.engine
                active_settings = DATABASE._settings
            with engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
                server_version = engine.dialect.server_version_info
                available = {name.upper() for name in inspect(engine).get_table_names()}
                if "ALEMBIC_VERSION" in available:
                    revisions = [row[0] for row in connection.execute(
                        text("SELECT version_num FROM alembic_version"))]
                    actual_revision = revisions[0] if len(revisions) == 1 else None
            connected = True
            tables, table_count_reasons = _estimated_table_counts(engine)
            exact_job = STORAGE_JOBS.latest(_STATISTICS_KIND)
            if exact_job and exact_job.get("status") == "SUCCEEDED":
                from app.db.migration import _fingerprint
                exact_result = exact_job.get("result") or {}
                if exact_result.get("fingerprint") == _fingerprint(active_settings or configured):
                    tables = exact_result.get("tables") or tables
                    table_count_mode = "exact"
                    exact_statistics_at = exact_result.get("statistics_at")
            object_sizes = DATABASE.storage_usage(Base.metadata, engine=engine)
    except Exception as exception:
        error = _safe_database_error(exception, configured)

    maintenance_jobs = {}
    for kind in (_RECLAIM_KIND, _BACKUP_KIND, _BACKUP_CHECK_KIND, _RESTORE_KIND,
                 _STATISTICS_KIND):
        job = STORAGE_JOBS.latest(kind)
        if job:
            maintenance_jobs[kind] = job
    status = {
        "active": _safe_settings(active_settings or configured),
        "configured": _safe_settings(configured),
        "connected": connected,
        "schema_revision": actual_revision,
        "expected_schema_revision": current_schema_revision(),
        "server_version": str(server_version) if server_version is not None else None,
        "tables": tables,
        "table_count_mode": table_count_mode,
        "table_count_reasons": table_count_reasons,
        "exact_statistics_at": exact_statistics_at,
        "logical_domains": {domain: {name: tables.get(name) for name in names}
                            for domain, names in LOGICAL_DOMAIN_TABLES.items()},
        "supports_space_reclaim": bool(DATABASE._backend and DATABASE._backend.supports_space_reclaim),
        "statistics_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "current_job": STORAGE_JOBS.latest(_RECLAIM_KIND),
        "statistics_job": STORAGE_JOBS.latest(_STATISTICS_KIND),
        "pending_database_job": STORAGE_JOBS.latest("database_migration"),
        "maintenance_jobs": maintenance_jobs,
        "storage": object_sizes,
    }
    if error:
        status["error"] = error
    if connected and engine.dialect.name == "sqlite":
        try:
            with STORAGE_JOBS.maintenance_lock(exclusive=False):
                if STORAGE_JOBS.maintenance_gate("database"):
                    raise RuntimeError("数据库维护中，暂不读取 SQLite 空间统计")
                path = _sqlite_path()
                sqlite_status = _sqlite_stats(path)
                reclaim_allowed, reclaim_reason = _sqlite_reclaim_availability(
                    engine, path, sqlite_status)
                latest_reclaim = STORAGE_JOBS.latest(_RECLAIM_KIND)
                sqlite_status.update({
                    "path": path,
                    "database_file": os.path.basename(path),
                    "reclaim_supported": True,
                    "reclaim_allowed": reclaim_allowed,
                    "reclaim_reason": reclaim_reason,
                    "error": reclaim_reason,
                    "last_reclaim": latest_reclaim,
                    "connections_to_drain": engine.pool.checkedout(),
                    "estimated_reclaimable_bytes": sqlite_status["free_pages_bytes"],
                })
                status["sqlite"] = sqlite_status
        except Exception as exception:
            status["sqlite"] = {"reclaim_supported": True, "reclaim_allowed": False,
                                "error": str(exception)[:1000],
                                "reclaim_reason": str(exception)[:1000]}
    else:
        status["sqlite"] = None
    return {"code": 0, "database": status}


def _digest_tables(engine):
    from app.db.migration import _table_digest
    digests = {}
    with engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            digests[table.name] = _table_digest(connection, table)
    return digests


def _verify_revision(engine):
    head = current_schema_revision()
    inspector = inspect(engine)
    tables = {name.upper() for name in inspector.get_table_names()}
    if "ALEMBIC_VERSION" not in tables:
        raise RuntimeError("SQLite 数据库缺少结构版本表")
    with engine.connect() as connection:
        versions = [row[0] for row in connection.execute(
            text("SELECT version_num FROM alembic_version"))]
    if versions != [head]:
        raise RuntimeError("SQLite 数据库结构版本不是当前版本")
    _table_counts(engine)
    return head


def _check_checkpoint(result, stage):
    if not result or int(result[0]) != 0:
        raise RuntimeError(f"{stage} WAL checkpoint 未完成：{result}")
    if len(result) > 2 and int(result[1]) >= 0 and int(result[2]) < int(result[1]):
        raise RuntimeError(f"{stage} WAL checkpoint 未完整写入：{result}")


def start_database_space_reclaim(values=None):
    values = dict(values or {})
    if any(key in values for key in ("path", "table", "domain", "sql")):
        return {"code": 1, "msg": "空间回收只作用于当前 SQLite 整库，不接受路径或范围参数"}
    settings, fingerprint, safe = _settings()
    if str(settings.get("type") or "sqlite").lower() != "sqlite":
        return {"code": 1, "msg": "当前活动数据库不支持 SQLite 空间回收"}
    idempotency_key = f"sqlite-reclaim:{fingerprint}"
    job, created = STORAGE_JOBS.create(
        _RECLAIM_KIND, {"fingerprint": fingerprint, "database": safe}, idempotency_key)
    if not created:
        return {"code": 0, "job_id": job["job_id"], "job": job}
    thread = threading.Thread(target=_run_space_reclaim, args=(job["job_id"], fingerprint),
                              name=f"database-reclaim-{job['job_id'][:8]}", daemon=True)
    with _WORKERS_LOCK:
        _WORKERS[job["job_id"]] = thread
    thread.start()
    return {"code": 0, "job_id": job["job_id"], "job": STORAGE_JOBS.get(job["job_id"])}


def _run_space_reclaim(job_id, fingerprint):
    started = time.monotonic()
    gate_set = False
    vacuum_started = False
    before = after = None
    try:
        from app.db.migration import _current_settings, _ensure_other_jobs_idle, _fingerprint

        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            if not _fingerprint(_current_settings()) == fingerprint:
                raise RuntimeError("数据库配置已变化，请重新检查后再回收")
            if _current_settings()["type"] != "sqlite":
                raise RuntimeError("空间回收仅支持当前活动 SQLite 数据库")
            _ensure_other_jobs_idle(job_id)
            _cleanup_database_artifacts_locked()
            STORAGE_JOBS.update(job_id, status="QUIESCING", stage="QUIESCING",
                                message="正在暂停数据库访问并检查整库", progress=0.02)
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "QUIESCING")
            gate_set = True
            engine = DATABASE.engine
            _verify_revision(engine)
            before_digests = _digest_tables(engine)
            preliminary = _sqlite_stats(_sqlite_path())
            if preliminary["integrity"] != "ok":
                raise RuntimeError("VACUUM 前 SQLite 完整性检查失败")
            database_path = _sqlite_path()
            DATABASE.close_session()
            if engine.pool.checkedout():
                raise RuntimeError("仍有业务连接未排空，无法安全回收空间")
            DATABASE.dispose()

            connection = sqlite3.connect(database_path, timeout=60, isolation_level=None)
            try:
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                _check_checkpoint(checkpoint, "VACUUM 前")
                before = _sqlite_stats(database_path)
                before["checkpoint"] = list(checkpoint)
                free_bytes = shutil.disk_usage(os.path.dirname(database_path)).free
                required_bytes = before["database_size_bytes"] * 2 + 64 * 1024 * 1024
                if free_bytes < required_bytes:
                    raise RuntimeError(
                        f"可用空间不足：需要约 {required_bytes} 字节，当前 {free_bytes} 字节")
                if STORAGE_JOBS.cancel_requested(job_id):
                    STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                        result={"before": before, "vacuum_completed": False},
                                        message="VACUUM 开始前已取消，数据库内容未改变")
                    connection.close()
                    DATABASE.engine
                    STORAGE_JOBS.clear_maintenance_gate("database", job_id)
                    gate_set = False
                    return
                claimed = STORAGE_JOBS.update(
                    job_id, status="VACUUMING", stage="VACUUMING",
                    message="VACUUM 正在重建 SQLite 数据文件；此阶段不支持取消",
                    expected_status="QUIESCING")
                if not claimed:
                    raise RuntimeError("回收任务状态已变化，拒绝开始 VACUUM")
                if STORAGE_JOBS.cancel_requested(job_id):
                    STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                        result={"before": before, "vacuum_completed": False},
                                        message="VACUUM 开始前已取消，数据库内容未改变")
                    connection.close()
                    DATABASE.engine
                    STORAGE_JOBS.clear_maintenance_gate("database", job_id)
                    gate_set = False
                    return
                vacuum_started = True
                if connection.in_transaction:
                    raise RuntimeError("VACUUM 维护连接存在未结束事务")
                connection.execute("VACUUM")
                after_vacuum_checkpoint = connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                _check_checkpoint(after_vacuum_checkpoint, "VACUUM 后")
            finally:
                connection.close()

            after = _sqlite_stats(database_path)
            if after["integrity"] != "ok":
                raise RuntimeError("VACUUM 后 SQLite 完整性检查失败")
            reopened = DATABASE.engine
            _verify_revision(reopened)
            after_digests = _digest_tables(reopened)
            if before_digests != after_digests:
                raise RuntimeError("VACUUM 前后表记录内容或显式主键摘要不一致")
            with reopened.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
            after["freed_bytes"] = max(
                0, before["database_size_bytes"] + before["wal_size_bytes"]
                - after["database_size_bytes"] - after["wal_size_bytes"])
            result = {"before": before, "after": after,
                      "elapsed_seconds": round(time.monotonic() - started, 3),
                      "vacuum_completed": True,
                      "checkpoint_before": before.get("checkpoint"),
                      "checkpoint_after": after_vacuum_checkpoint,
                      "volume_free_bytes": free_bytes}
            STORAGE_JOBS.update(job_id, status="SUCCEEDED", stage="SUCCEEDED", result=result,
                                message="SQLite 整库空间回收和完整性校验完成", progress=1)
            STORAGE_JOBS.clear_maintenance_gate("database", job_id)
            gate_set = False
    except Exception as error:
        current = STORAGE_JOBS.get(job_id) or {}
        healthy = False
        try:
            if DATABASE._engine is None:
                DATABASE.engine
            with DATABASE.engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
            healthy = _sqlite_stats(_sqlite_path())["integrity"] == "ok"
        except Exception:
            pass
        if gate_set and not healthy:
            STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                                error=_safe_database_error(error),
                                result={"before": before, "after": after,
                                        "vacuum_completed": vacuum_started},
                                message="SQLite 回收后数据库健康状态未确认，系统保持维护模式")
        else:
            if gate_set:
                try:
                    with STORAGE_JOBS.maintenance_lock(exclusive=True):
                        STORAGE_JOBS.clear_maintenance_gate("database", job_id)
                    gate_set = False
                except Exception:
                    pass
            STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                error=_safe_database_error(error),
                                result={"before": before, "after": after,
                                        "vacuum_completed": vacuum_started,
                                        "elapsed_seconds": round(time.monotonic() - started, 3)},
                                message="SQLite 空间回收失败；已记录 VACUUM 和校验实际阶段")
    finally:
        with _WORKERS_LOCK:
            _WORKERS.pop(job_id, None)


def _content_digests_match(engine, expected):
    if not isinstance(expected, dict) or set(expected) != set(Base.metadata.tables):
        return False
    actual = _digest_tables(engine)
    for name, digest in actual.items():
        reference = expected.get(name)
        if isinstance(reference, dict):
            rows, sha256 = reference.get("rows"), reference.get("sha256")
        elif isinstance(reference, (tuple, list)) and len(reference) == 2:
            rows, sha256 = reference
        else:
            return False
        if int(rows) != digest[0] or sha256 != digest[1]:
            return False
    return True


def _content_digests_match_except_sync_jobs(engine, expected):
    """恢复事务可有意重置同步租约，其余备份表仍须逐表完全匹配。"""
    if not isinstance(expected, dict) or set(expected) != set(Base.metadata.tables):
        return False
    actual = _digest_tables(engine)
    for name, digest in actual.items():
        if name == MEDIASYNCSYNCJOB.__tablename__:
            continue
        reference = expected.get(name)
        if isinstance(reference, dict):
            rows, sha256 = reference.get("rows"), reference.get("sha256")
        elif isinstance(reference, (tuple, list)) and len(reference) == 2:
            rows, sha256 = reference
        else:
            return False
        if int(rows) != digest[0] or sha256 != digest[1]:
            return False
    return True


def _restore_expected_digests(job):
    result = job.get("result") or {}
    payload = job.get("payload") or {}
    return result.get("restored_digests") or payload.get("content_digests")


def _invalidate_restored_media_sync_leases(connection):
    """恢复导入时废弃备份中的租约，后续启动只清理未发布代次。"""
    now = time.time()
    result = connection.execute(update(MEDIASYNCSYNCJOB).values(
        LEASE_TOKEN=MEDIASYNCSYNCJOB.LEASE_TOKEN + 1,
        LEASE_UNTIL=0,
        STATUS="CLEANUP_PENDING",
        UPDATED_AT=now,
    ))
    return max(0, result.rowcount or 0)


def _restored_media_sync_leases_invalidated(engine):
    with engine.connect() as connection:
        active = connection.execute(select(func.count()).select_from(MEDIASYNCSYNCJOB).where(
            or_(MEDIASYNCSYNCJOB.LEASE_UNTIL != 0,
                MEDIASYNCSYNCJOB.STATUS != "CLEANUP_PENDING"))).scalar_one()
    return int(active) == 0


def _pre_restore_backup_path(job_id):
    return os.path.join(_backup_root(), f"pre-restore-{job_id}.zip")


def _pre_restore_matches(path, engine):
    if not path or not os.path.isfile(path):
        return False
    checked = _validate_backup_archive(path)
    return _content_digests_match(engine, checked["content_digests"])


def _restore_sqlite_rollback(job_id, result):
    from app.db.migration import _new_engine

    database_path = _sqlite_path()
    stage_dir = os.path.realpath(os.path.join(
        _config().get_config_path(), "db", ".staging", f"database-restore-{job_id}"))
    rollback_path = os.path.realpath(str(result.get("rollback_path") or ""))
    if (not rollback_path or os.path.commonpath((stage_dir, rollback_path)) != stage_dir
            or not os.path.isfile(rollback_path)
            or _sha256_path(rollback_path) != result.get("rollback_sha256")):
        raise RuntimeError("SQLite 恢复回滚副本缺失或摘要不匹配")
    candidate = _new_engine({"type": "sqlite"}, sqlite_path=rollback_path)
    try:
        _verify_revision(candidate)
        _sqlite_integrity(rollback_path)
        if not _pre_restore_matches(_pre_restore_backup_path(job_id), candidate):
            raise RuntimeError("SQLite 回滚副本与恢复前完整备份不一致")
    finally:
        candidate.dispose()
    DATABASE.dispose()
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(database_path + suffix)
        except FileNotFoundError:
            pass
    os.replace(rollback_path, database_path)
    _fsync_file_and_directory(database_path)
    restored = DATABASE.engine
    _verify_revision(restored)
    if not _pre_restore_matches(_pre_restore_backup_path(job_id), restored):
        raise RuntimeError("SQLite 回滚后的活动数据库与恢复前备份不一致")
    return restored


def _restore_old_sqlite_sidecars(job_id, result):
    stage_dir = os.path.realpath(os.path.join(
        _config().get_config_path(), "db", ".staging", f"database-restore-{job_id}"))
    database_path = _sqlite_path()
    sidecars = result.get("old_sidecars") or [
        {"path": os.path.join(stage_dir, "old-database" + suffix), "suffix": suffix}
        for suffix in ("-wal", "-shm")]
    for item in sidecars:
        suffix = item.get("suffix")
        retired = os.path.realpath(str(item.get("path") or ""))
        if suffix not in {"-wal", "-shm"} or os.path.commonpath((stage_dir, retired)) != stage_dir:
            raise RuntimeError("恢复暂存目录中的旧数据库辅助文件路径无效")
        active = database_path + suffix
        if os.path.isfile(retired) and not os.path.exists(active):
            os.replace(retired, active)


def _mark_maintenance_recovered(job, status, message, result=None):
    STORAGE_JOBS.update(job["job_id"], status=status, stage=status,
                        result=result, message=message, progress=1 if status == "SUCCEEDED" else None)
    STORAGE_JOBS.clear_maintenance_gate("database", job["job_id"])


def _recover_sqlite_restore(job):
    job_id = job["job_id"]
    result = job.get("result") or {}
    payload = job.get("payload") or {}
    status = job.get("status")
    database_path = _sqlite_path()
    if status in {"CHECKING", "BACKING_UP_CURRENT", "BUILDING_STAGE", "CANCELLED", "FAILED"}:
        engine = DATABASE.engine
        _verify_revision(engine)
        if result.get("pre_restore_sha256") and not _pre_restore_matches(
                _pre_restore_backup_path(job_id), engine):
            raise RuntimeError("恢复前活动数据库与已生成的完整备份不一致")
        recovered_status = status if status in {"FAILED", "CANCELLED"} else "INTERRUPTED"
        _mark_maintenance_recovered(job, recovered_status,
                                    "恢复发布前进程中断；原活动数据库已验证并保留")
        stage = os.path.join(_config().get_config_path(), "db", ".staging",
                             f"database-restore-{job_id}")
        shutil.rmtree(stage, ignore_errors=True)
        return True

    if status == "PUBLISHING_RESTORE":
        actual = _file_identity(database_path)
        result_old = result.get("old_identity") or {}
        result_staged = result.get("staged_identity") or {}
        if (actual and actual.get("device") == result_old.get("device")
                and actual.get("inode") == result_old.get("inode")):
            _restore_old_sqlite_sidecars(job_id, result)
            engine = DATABASE.engine
            _verify_revision(engine)
            if not _pre_restore_matches(_pre_restore_backup_path(job_id), engine):
                raise RuntimeError("恢复发布中断后的原 SQLite 数据与恢复前备份不一致")
            _mark_maintenance_recovered(job, "INTERRUPTED",
                                        "SQLite 尚未替换活动文件；原数据库已验证并保留")
            shutil.rmtree(os.path.join(_config().get_config_path(), "db", ".staging",
                                       f"database-restore-{job_id}"), ignore_errors=True)
            return True
        if (actual and actual.get("device") == result_staged.get("device")
                and actual.get("inode") == result_staged.get("inode")):
            engine = _restore_sqlite_rollback(job_id, result)
            _mark_maintenance_recovered(job, "INTERRUPTED",
                                        "进程在 SQLite 发布后中断；已从完整恢复前备份回滚")
            shutil.rmtree(os.path.join(_config().get_config_path(), "db", ".staging",
                                       f"database-restore-{job_id}"), ignore_errors=True)
            return True
        if actual is None and os.path.isfile(str(result.get("rollback_path") or "")):
            _restore_sqlite_rollback(job_id, result)
            _mark_maintenance_recovered(job, "INTERRUPTED",
                                        "SQLite 活动文件在恢复中断时缺失；已从恢复前副本恢复")
            shutil.rmtree(os.path.join(_config().get_config_path(), "db", ".staging",
                                       f"database-restore-{job_id}"), ignore_errors=True)
            return True
        raise RuntimeError("SQLite 活动文件与恢复前及暂存文件身份均不匹配")

    if status in {"VERIFYING_RESTORE", "RESTART_PENDING", "RECOVERY_REQUIRED"}:
        engine = DATABASE.engine
        _verify_revision(engine)
        if not _content_digests_match(engine, _restore_expected_digests(job)):
            raise RuntimeError("恢复后的 SQLite 内容摘要与经检查的备份不一致")
        from app.db.migration import _calibrate_sequences
        _calibrate_sequences(engine)
        _verify_revision(engine)
        retired_spool = _clear_restore_derived_state(job_id)
        final_result = dict(result)
        final_result.update({"restore_validated": True, "retired_spool": retired_spool,
                             "restart_required": False, "startup_recovered": True})
        _mark_maintenance_recovered(job, "SUCCEEDED",
                                    "重启后已确认 SQLite 恢复数据和结构，恢复业务访问",
                                    final_result)
        shutil.rmtree(os.path.join(_config().get_config_path(), "db", ".staging",
                                   f"database-restore-{job_id}"), ignore_errors=True)
        return True
    raise RuntimeError(f"不支持从数据库恢复阶段 {status} 自动恢复")


def _recover_remote_restore(job):
    from app.db.migration import _calibrate_sequences

    job_id = job["job_id"]
    payload = job.get("payload") or {}
    result = job.get("result") or {}
    engine = DATABASE.engine
    _verify_revision(engine)
    if job.get("status") in {"CHECKING", "BACKING_UP_CURRENT", "FAILED", "CANCELLED"}:
        if result.get("pre_restore_sha256") and not _pre_restore_matches(
                _pre_restore_backup_path(job_id), engine):
            raise RuntimeError("远程活动数据库与已生成的恢复前完整备份不一致")
        recovered_status = job.get("status") if job.get("status") in {"FAILED", "CANCELLED"} \
            else "INTERRUPTED"
        _mark_maintenance_recovered(job, recovered_status,
                                    "远程恢复事务尚未提交；原活动数据库已验证并保留")
        return True
    backup_digests = payload.get("content_digests")
    if _content_digests_match(engine, result.get("restored_digests")):
        restored_digests = result["restored_digests"]
    elif _content_digests_match(engine, backup_digests):
        # 兼容恢复提交成功、进程在持久化任务结果前崩溃的窗口。
        with engine.begin() as connection:
            _invalidate_restored_media_sync_leases(connection)
        restored_digests = _digest_tables(engine)
    elif (_content_digests_match_except_sync_jobs(engine, backup_digests)
          and _restored_media_sync_leases_invalidated(engine)):
        # 同步作业表在恢复事务中被重置，其他表仍须与检查通过的备份一致。
        restored_digests = _digest_tables(engine)
    else:
        restored_digests = None

    if restored_digests is not None:
        _calibrate_sequences(engine)
        _verify_revision(engine)
        if not _content_digests_match(engine, restored_digests):
            raise RuntimeError("远程数据库自增校准后数据摘要不一致")
        retired_spool = _clear_restore_derived_state(job_id)
        final_result = dict(result)
        final_result.update({"restored_digests": restored_digests,
                             "restore_validated": True, "retired_spool": retired_spool,
                             "restart_required": False, "startup_recovered": True})
        _mark_maintenance_recovered(job, "SUCCEEDED",
                                    "重启后已确认远程数据库恢复内容，恢复业务访问",
                                    final_result)
        return True
    if _pre_restore_matches(_pre_restore_backup_path(job_id), engine):
        _mark_maintenance_recovered(job, "INTERRUPTED",
                                    "远程恢复事务未提交；恢复前数据库已验证并保留")
        return True
    raise RuntimeError("远程数据库既不匹配恢复备份，也不匹配恢复前完整备份")


def verify_database_maintenance_startup(job):
    """在业务 ORM 初始化前检查中断的空间回收、备份和恢复任务。"""
    job_id = job["job_id"]
    kind = job.get("kind")
    if kind == _RECLAIM_KIND:
        try:
            from app.db.migration import _current_settings, _fingerprint
            if not hmac_compare(_fingerprint(_current_settings()),
                                (job.get("payload") or {}).get("fingerprint")):
                raise RuntimeError("活动数据库配置与中断的 SQLite 回收任务不匹配")
            path = _sqlite_path()
            stats = _sqlite_stats(path, checkpoint=True)
            if stats["integrity"] != "ok":
                raise RuntimeError("SQLite 完整性检查失败")
            if stats.get("checkpoint"):
                _check_checkpoint(stats["checkpoint"], "启动恢复")
            engine = DATABASE.engine
            _verify_revision(engine)
            if job.get("status") not in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
                STORAGE_JOBS.update(job_id, status="INTERRUPTED", stage="INTERRUPTED",
                                    result={"after": stats, "vacuum_completed": None},
                                    message="进程在空间回收任务中中断；数据库已通过启动完整性检查")
            STORAGE_JOBS.clear_maintenance_gate("database", job_id)
            return True
        except Exception as error:
            STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                                error=_safe_database_error(error),
                                message="空间回收中断后的数据库检查失败，保持维护模式")
            raise RuntimeError("SQLite 空间回收中断后的数据库恢复检查失败") from error
    if kind in {_BACKUP_KIND, _BACKUP_CHECK_KIND}:
        if job.get("status") not in {"SUCCEEDED", "READY", "FAILED", "CANCELLED", "INTERRUPTED"}:
            STORAGE_JOBS.update(job_id, status="INTERRUPTED", stage="INTERRUPTED",
                                message="数据库备份任务随进程中断；活动数据库保持不变")
        if kind == _BACKUP_KIND and job.get("status") not in {"SUCCEEDED", "READY"}:
            try:
                os.unlink(os.path.join(_backup_root(), f"{job_id}.zip"))
            except OSError:
                pass
        STORAGE_JOBS.clear_maintenance_gate("database", job_id)
        return True
    if kind == _RESTORE_KIND:
        from app.db.migration import _current_settings, _fingerprint
        if not hmac_compare(_fingerprint(_current_settings()),
                            (job.get("payload") or {}).get("fingerprint")):
            STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                                error="活动数据库配置与恢复任务不匹配",
                                message="数据库配置已变化，不能自动确认恢复结果，保持维护模式")
            raise RuntimeError("活动数据库配置与恢复任务不匹配，保持维护状态")
        if job.get("status") in {"RESTORING", "CALIBRATING"}:
            return _recover_remote_restore(job)
        current_settings = DATABASE._settings or _settings()[0]
        if current_settings.get("type") == "sqlite":
            return _recover_sqlite_restore(job)
        return _recover_remote_restore(job)
    return False


def _encode_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return {"$nt_type": "float", "value": value.hex()}
    if isinstance(value, Decimal):
        return {"$nt_type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"$nt_type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$nt_type": "date", "value": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$nt_type": "bytes", "value": base64.b64encode(bytes(value)).decode("ascii")}
    raise ValueError(f"备份遇到未支持的数据类型：{type(value).__name__}")


def _decode_value(value):
    if not isinstance(value, dict) or "$nt_type" not in value:
        return value
    if set(value) != {"$nt_type", "value"}:
        raise ValueError("备份中的类型标记格式无效")
    kind, raw = value["$nt_type"], value["value"]
    if not isinstance(raw, str):
        raise ValueError("备份中的类型值必须是文本")
    if kind == "float":
        return float.fromhex(raw)
    if kind == "decimal":
        return Decimal(raw)
    if kind == "datetime":
        return datetime.fromisoformat(raw)
    if kind == "date":
        return date.fromisoformat(raw)
    if kind == "bytes":
        return base64.b64decode(raw, validate=True)
    raise ValueError("备份中包含不支持的类型标记")


def _sha256_path(path, job_id=None):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        blocks = 0
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            blocks += 1
            if blocks % 16 == 0 and job_id and STORAGE_JOBS.cancel_requested(job_id):
                raise InterruptedError("已取消备份文件摘要检查")
            digest.update(block)
    return digest.hexdigest()


def _manifest_schema():
    from app.db.schema import LOGICAL_DOMAIN_TABLES
    domains = {table: domain for domain, names in LOGICAL_DOMAIN_TABLES.items()
               for table in names}
    schema = {}
    for table in Base.metadata.sorted_tables:
        schema[table.name] = {
            "domain": domains.get(table.name, "main"),
            "columns": [{"name": column.name, "type": str(column.type),
                         "nullable": bool(column.nullable),
                         "primary_key": bool(column.primary_key)}
                        for column in table.columns],
        }
    return schema


def _export_backup(source_connection, backup_path, job_id, source_type,
                   job_status="EXPORTING", stage="EXPORTING"):
    from app.db.migration import _ordered_table_select
    from version import APP_VERSION

    table_info = {}
    schema = _manifest_schema()
    with zipfile.ZipFile(backup_path, mode="w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6, allowZip64=True) as archive:
        for index, table in enumerate(Base.metadata.sorted_tables, start=1):
            if STORAGE_JOBS.cancel_requested(job_id):
                raise InterruptedError("已取消数据库备份导出")
            result = source_connection.execute(
                _ordered_table_select(source_connection, table).execution_options(stream_results=True))
            digest = hashlib.sha256()
            count = 0
            with archive.open(f"tables/{table.name}.jsonl", mode="w", force_zip64=True) as entry:
                while True:
                    row = result.fetchone()
                    if row is None:
                        break
                    record = {column.name: _encode_value(value)
                              for column, value in zip(table.columns, row)}
                    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"), allow_nan=False)
                               + "\n").encode("utf-8", "strict")
                    if len(encoded) > 64 * 1024 * 1024:
                        raise ValueError(f"备份表 {table.name} 中存在超过 64 MiB 的单条记录")
                    entry.write(encoded)
                    digest.update(encoded)
                    count += 1
                    if job_status == "BUILDING_STAGE" and count % _BATCH_SIZE == 0 \
                            and STORAGE_JOBS.cancel_requested(job_id):
                        raise InterruptedError("已取消数据库备份导出")
            result.close()
            table_info[table.name] = {"rows": count, "sha256": digest.hexdigest(),
                                      "file": f"tables/{table.name}.jsonl"}
            STORAGE_JOBS.update(job_id, status=job_status, stage=stage,
                                result={"backup_id": job_id, "current_table": table.name,
                                        "tables_completed": index - 1,
                                        "tables_total": len(Base.metadata.tables)},
                                message=f"已导出 {index}/{len(Base.metadata.tables)} 张表",
                                progress=index / len(Base.metadata.tables) * 0.9)
        manifest = {
            "format": "nastools-full-database",
            "format_version": 1,
            "full_database": True,
            "application_version": str(APP_VERSION),
            "source_database_type": source_type,
            "schema_revision": current_schema_revision(),
            "logical_domains": {key: sorted(value)
                                for key, value in LOGICAL_DOMAIN_TABLES.items()},
            "schema": schema,
            "tables": table_info,
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }
        archive.writestr("manifest.json", json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    with open(backup_path, "rb") as stream:
        os.fsync(stream.fileno())
    _secure_file(backup_path)
    return manifest


def _legacy_dual_backup_entries(archive):
    """Recognize only complete, unambiguous legacy SQLite pairs."""
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(infos) > 8 or len(names) != len(set(names)):
        raise ValueError("旧备份条目数量无效或包含重复文件")
    allowed = {"user.db", "media.db", "db/user.db", "db/media.db",
               "config.yaml", "default-category.yaml", "backup-format.json"}
    allowed_directories = {"db/"}
    total_size = 0
    by_name = {}
    for info in infos:
        name = info.filename
        mode = info.external_attr >> 16
        if info.is_dir():
            if name not in allowed_directories or stat.S_ISLNK(mode):
                raise ValueError("旧备份包含不允许的目录")
            continue
        if (name not in allowed or stat.S_ISLNK(mode)
                or info.file_size < 0 or info.compress_size < 0):
            raise ValueError("旧备份包含不允许的文件")
        total_size += info.file_size
        if total_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("旧备份解压后超过服务器允许大小")
        if info.file_size and info.compress_size == 0:
            raise ValueError("旧备份压缩信息无效")
        if info.compress_size and info.file_size / info.compress_size > 10000:
            raise ValueError("旧备份压缩比异常，拒绝解压")
        by_name[name] = info

    if "backup-format.json" in by_name:
        info = by_name["backup-format.json"]
        if info.file_size > _MAX_MANIFEST_BYTES:
            raise ValueError("旧备份格式说明超过允许大小")
        try:
            marker = json.loads(archive.read(info).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("旧备份格式说明无效") from None
        if not isinstance(marker, dict):
            raise ValueError("旧备份格式说明无效")
        if marker.get("database_included") is False:
            raise ValueError("这是设置备份，不包含完整数据库，不能用于数据库恢复")

    root_pair = {"user.db", "media.db"}
    db_pair = {"db/user.db", "db/media.db"}
    present = set(by_name)
    root_present = root_pair.intersection(present)
    db_present = db_pair.intersection(present)
    if root_present == root_pair and not db_present:
        return {"user.db": by_name["user.db"], "media.db": by_name["media.db"]}
    if db_present == db_pair and not root_present:
        return {"user.db": by_name["db/user.db"], "media.db": by_name["db/media.db"]}
    if root_present and db_present:
        raise ValueError("旧备份同时包含根目录和 db/ 数据库，来源冲突")
    if root_present or db_present:
        if "user.db" in root_present or "db/user.db" in db_present:
            raise ValueError("旧备份缺少 media.db，属于不完整设置备份，不能恢复为完整数据库")
        if "media.db" in root_present or "db/media.db" in db_present:
            raise ValueError("旧备份缺少 user.db，不能恢复为完整数据库")
        raise ValueError("旧备份中的 user.db / media.db 布局不完整或来源冲突")
    raise ValueError("该 ZIP 不是完整数据库备份")


def _extract_legacy_sqlite_member(archive, info, path, job_id):
    written = 0
    header = bytearray()
    with archive.open(info, "r") as source, open(path, "xb") as target:
        while True:
            if STORAGE_JOBS.cancel_requested(job_id):
                raise InterruptedError("已取消旧双库备份转换")
            block = source.read(1024 * 1024)
            if not block:
                break
            written += len(block)
            if written > info.file_size or written > _MAX_ARCHIVE_BYTES:
                raise ValueError("旧备份数据库文件大小与 ZIP 清单不符")
            if len(header) < 16:
                header.extend(block[:16 - len(header)])
            target.write(block)
        target.flush()
        os.fsync(target.fileno())
    if written != info.file_size or bytes(header) != b"SQLite format 3\x00":
        raise ValueError("旧备份中的 SQLite 文件不完整或格式无效")
    _secure_file(path)


def _convert_legacy_dual_backup(path, upload_id, job_id):
    """Upgrade an old complete SQLite pair into the current portable backup format."""
    from app.db.migration import _new_engine, _write_schema_revision
    from app.db.schema import MEDIA_TABLES
    from app.db.sqlite_files import (_MEDIA_UPGRADE_BASE, _backfill_recognition_search,
                                     _copy_domain, _copy_snapshot,
                                     _upgrade_legacy_media_snapshot, _upgrade_snapshot,
                                     cleanup_legacy_startup_data)

    config_path = _config().get_config_path()
    staging_dir = os.path.join(config_path, "db", ".staging",
                               f"legacy-backup-{job_id}")
    os.makedirs(os.path.dirname(staging_dir), mode=0o700, exist_ok=True)
    os.makedirs(staging_dir, mode=0o700, exist_ok=False)
    try:
        os.chmod(staging_dir, 0o700)
    except OSError:
        pass
    source_user = os.path.join(staging_dir, "user.extracted.sqlite3")
    source_media = os.path.join(staging_dir, "media.extracted.sqlite3")
    user_snapshot = os.path.join(staging_dir, "user.snapshot.sqlite3")
    media_snapshot = os.path.join(staging_dir, "media.snapshot.sqlite3")
    staged_database = os.path.join(staging_dir, "nastools.staged.sqlite3")
    canonical_path = os.path.join(_upload_root(), f".{upload_id}.{job_id}.canonical.zip")
    target_engine = None
    try:
        with zipfile.ZipFile(path, "r") as archive:
            entries = _legacy_dual_backup_entries(archive)
            _extract_legacy_sqlite_member(archive, entries["user.db"], source_user, job_id)
            _extract_legacy_sqlite_member(archive, entries["media.db"], source_media, job_id)

        _copy_snapshot(source_user, user_snapshot)
        _copy_snapshot(source_media, media_snapshot)
        source = sqlite3.connect(f"file:{os.path.abspath(user_snapshot)}?mode=ro",
                                 uri=True, timeout=30)
        try:
            user_tables = {str(row[0]).upper() for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        finally:
            source.close()
        if user_tables & {name.upper() for name in MEDIA_TABLES}:
            raise ValueError("旧 user.db 已包含媒体表，不能与 media.db 合并")

        STORAGE_JOBS.update(job_id, status="BUILDING_STAGE", stage="BUILDING_STAGE",
                            message="正在升级旧主库和媒体库快照", progress=0.05)
        _upgrade_snapshot(user_snapshot, config_path, target_revision=_MEDIA_UPGRADE_BASE)
        _upgrade_legacy_media_snapshot(media_snapshot, config_path)
        target_engine = _new_engine({"type": "sqlite"}, sqlite_path=staged_database)
        Base.metadata.create_all(target_engine)
        expected_tables = set(Base.metadata.tables)
        with target_engine.begin() as connection:
            _copy_domain(connection, user_snapshot, "main", expected_tables)
            _copy_domain(connection, media_snapshot, "media", expected_tables,
                         excluded_tables={"MEDIASYNC_SYNC_JOBS"})
            cleanup_legacy_startup_data(connection)
            _backfill_recognition_search(connection)
        _write_schema_revision(target_engine)
        _verify_revision(target_engine)
        target_engine.dispose()
        target_engine = None
        _sqlite_integrity(staged_database)

        target_engine = _new_engine({"type": "sqlite"}, sqlite_path=staged_database)
        with target_engine.connect() as connection:
            _export_backup(connection, canonical_path, job_id, "sqlite",
                           job_status="BUILDING_STAGE", stage="BUILDING_STAGE")
        target_engine.dispose()
        target_engine = None
        if _file_size(canonical_path) > _MAX_UPLOAD_BYTES:
            raise ValueError("转换后的完整数据库备份超过服务器允许大小")
        checked = _validate_backup_archive(canonical_path, job_id=job_id)
        if STORAGE_JOBS.cancel_requested(job_id):
            raise InterruptedError("已取消旧双库备份转换")
        os.replace(canonical_path, path)
        _fsync_directory(os.path.dirname(path))
        checked["legacy_converted"] = True
        return checked
    finally:
        if target_engine is not None:
            target_engine.dispose()
        try:
            os.unlink(canonical_path)
        except FileNotFoundError:
            pass
        shutil.rmtree(staging_dir, ignore_errors=True)


def start_database_backup(values=None):
    values = dict(values or {})
    if any(key in values for key in ("path", "database_path", "config_path")):
        return {"code": 1, "msg": "备份文件路径由服务端生成，不接受路径参数"}
    settings, fingerprint, safe = _settings()
    idempotency_key = str(values.get("idempotency_key") or uuid.uuid4().hex)
    if len(idempotency_key) > 128:
        return {"code": 1, "msg": "幂等请求标识不能超过 128 个字符"}
    job, created = STORAGE_JOBS.create(
        _BACKUP_KIND, {"fingerprint": fingerprint, "database": safe}, idempotency_key)
    if not created:
        return {"code": 0, "job_id": job["job_id"], "job": job}
    thread = threading.Thread(target=_run_database_backup,
                              args=(job["job_id"], settings, fingerprint),
                              name=f"database-backup-{job['job_id'][:8]}", daemon=True)
    with _WORKERS_LOCK:
        _WORKERS[job["job_id"]] = thread
    thread.start()
    return {"code": 0, "job_id": job["job_id"], "job": STORAGE_JOBS.get(job["job_id"])}


def _run_database_backup(job_id, settings, fingerprint):
    source_engine = source_connection = source_transaction = None
    snapshot_path = None
    backup_path = os.path.join(_backup_root(), f"{job_id}.zip")
    gate_set = False
    recognition_quiesced = False
    try:
        from app.db.migration import (_current_settings, _ensure_other_jobs_idle,
                                      _fingerprint, _snapshot_source, _validate_source_shape)
        from app.media.recognition.records import (quiesce_recognition,
                                                   replay_recognition_spool,
                                                   resume_recognition)

        quiesce_recognition(timeout=120)
        recognition_quiesced = True
        spool_result = replay_recognition_spool()
        if spool_result.get("pending"):
            raise RuntimeError("识别暂存记录未能全部写入当前数据库，已中止备份快照")
        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            if not hmac_compare(_fingerprint(_current_settings()), fingerprint):
                raise RuntimeError("活动数据库配置已变化，无法建立备份快照")
            _ensure_other_jobs_idle(job_id)
            _cleanup_database_artifacts_locked()
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "SNAPSHOTTING")
            gate_set = True
            STORAGE_JOBS.update(job_id, status="SNAPSHOTTING", stage="SNAPSHOTTING",
                                message="正在建立覆盖全部逻辑域的一致性快照", progress=0.02)
            source_engine, source_connection, source_transaction, snapshot_path = \
                _snapshot_source(settings, job_id)
            _validate_source_shape(source_connection, Base.metadata)
            if STORAGE_JOBS.cancel_requested(job_id):
                raise InterruptedError("已取消数据库快照")
            STORAGE_JOBS.clear_maintenance_gate("database", job_id)
            gate_set = False
            STORAGE_JOBS.update(job_id, status="EXPORTING", stage="EXPORTING",
                                message="一致性快照已固定，正在流式压缩备份",
                                progress=0.05)
        resume_recognition()
        recognition_quiesced = False

        manifest = _export_backup(source_connection, backup_path, job_id, settings["type"])
        if STORAGE_JOBS.cancel_requested(job_id):
            raise InterruptedError("已取消数据库备份")
        result = {"backup_id": job_id,
                  "filename": f"nastools-database-{job_id[:12]}.zip",
                  "size_bytes": _file_size(backup_path),
                  "sha256": _sha256_path(backup_path),
                  "schema_revision": manifest["schema_revision"],
                  "tables": {name: info["rows"] for name, info in manifest["tables"].items()},
                  "full_database": True}
        STORAGE_JOBS.update(job_id, status="SUCCEEDED", stage="SUCCEEDED", result=result,
                            message="完整数据库备份已生成", progress=1)
    except InterruptedError as error:
        try:
            os.unlink(backup_path)
        except FileNotFoundError:
            pass
        current = STORAGE_JOBS.get(job_id) or {}
        if current.get("status") not in {"CANCELLED", "INTERRUPTED"}:
            STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                message=str(error))
    except Exception as error:
        try:
            os.unlink(backup_path)
        except FileNotFoundError:
            pass
        STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                            error=_safe_database_error(error, settings),
                            message="完整数据库备份失败；活动数据库保持不变")
    finally:
        if recognition_quiesced:
            from app.media.recognition.records import resume_recognition
            resume_recognition()
        if source_transaction is not None:
            try:
                source_transaction.rollback()
            except Exception:
                pass
        if source_connection is not None:
            source_connection.close()
        if snapshot_path and source_engine is not None:
            source_engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(snapshot_path + suffix)
                except FileNotFoundError:
                    pass
        if gate_set:
            try:
                with STORAGE_JOBS.maintenance_lock(exclusive=True):
                    STORAGE_JOBS.clear_maintenance_gate("database", job_id)
            except Exception:
                pass
        with _WORKERS_LOCK:
            _WORKERS.pop(job_id, None)


def hmac_compare(left, right):
    import hmac
    return hmac.compare_digest(str(left or ""), str(right or ""))


def database_backup_path(backup_id):
    if not re.fullmatch(r"[0-9a-f]{32}", str(backup_id or "")):
        return None
    job = STORAGE_JOBS.get(backup_id)
    if not job:
        return None
    result = job.get("result") or {}
    if job.get("kind") == _BACKUP_KIND and job.get("status") == "SUCCEEDED":
        path = os.path.join(_backup_root(), f"{backup_id}.zip")
        filename = result.get("filename") or f"nastools-database-{backup_id[:12]}.zip"
    elif job.get("kind") == _RESTORE_KIND and result.get("pre_restore_sha256"):
        path = _pre_restore_backup_path(backup_id)
        filename = f"nastools-database-before-restore-{backup_id[:12]}.zip"
        try:
            if _sha256_path(path) != result["pre_restore_sha256"]:
                return None
        except OSError:
            return None
    else:
        return None
    root = os.path.realpath(_backup_root())
    target = os.path.realpath(path)
    if os.path.commonpath((root, target)) != root or not os.path.isfile(target):
        return None
    return target, filename


def upload_database_backup(file_stream):
    cleanup_database_artifacts()
    upload_id = uuid.uuid4().hex
    path = os.path.join(_upload_root(), f"{upload_id}.zip")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "xb") as output:
            while True:
                chunk = file_stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise ValueError("上传备份超过服务器允许大小")
                if shutil.disk_usage(os.path.dirname(path)).free < len(chunk) + 16 * 1024 * 1024:
                    raise RuntimeError("服务器剩余空间不足，无法保存上传备份")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if size == 0:
            raise ValueError("上传文件为空")
        _secure_file(path)
        return {"code": 0, "upload_id": upload_id, "size_bytes": size,
                "sha256": digest.hexdigest()}
    except Exception as error:
        try:
            os.unlink(path)
        except OSError:
            pass
        return {"code": 1, "msg": _safe_database_error(error)[:1000]}


def _upload_path(upload_id):
    if not re.fullmatch(r"[0-9a-f]{32}", str(upload_id or "")):
        return None
    path = os.path.join(_upload_root(), f"{upload_id}.zip")
    return path if os.path.isfile(path) else None


def _reject_json_constant(value):
    raise ValueError(f"JSON 中不允许非有限数：{value}")


def _matches_column_type(value, column):
    try:
        expected = column.type.python_type
    except (AttributeError, NotImplementedError):
        return True
    if value is None:
        return bool(column.nullable)
    if expected is int:
        return type(value) is int
    if expected is bool:
        return type(value) is bool
    if expected is float:
        return type(value) is float
    if expected is Decimal:
        return isinstance(value, Decimal)
    if expected is str:
        return isinstance(value, str)
    if expected is bytes:
        return isinstance(value, bytes)
    if expected is datetime:
        return isinstance(value, datetime)
    if expected is date:
        return isinstance(value, date) and not isinstance(value, datetime)
    return isinstance(value, expected)


def _decode_backup_row(raw_row, table):
    if not isinstance(raw_row, dict) or set(raw_row) != set(table.columns.keys()):
        raise ValueError(f"备份表 {table.name} 的字段集合无效")
    decoded = {name: _decode_value(value) for name, value in raw_row.items()}
    for column in table.columns:
        if not _matches_column_type(decoded[column.name], column):
            raise ValueError(f"备份表 {table.name} 字段 {column.name} 的类型不匹配")
    validate_string_lengths(table, (decoded,))
    return decoded


def _file_identity(path):
    try:
        info = os.stat(path)
        return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
                "mtime_ns": info.st_mtime_ns}
    except FileNotFoundError:
        return None


def _fsync_file_and_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        pass


def _retire_recognition_spool(job_id):
    config_path = _config().get_config_path()
    source = os.path.join(config_path, "recognition_spool")
    if not os.path.isdir(source):
        return None
    root = os.path.join(_maintenance_root(), "database-retired-spool")
    _secure_directory(root)
    destination = os.path.join(root, job_id)
    if not os.path.exists(destination):
        os.replace(source, destination)
    elif os.path.isdir(source) and os.listdir(source):
        continued = os.path.join(destination, f"continued-{uuid.uuid4().hex}")
        os.replace(source, continued)
    _secure_directory(source)
    return True


def _clear_restore_derived_state(job_id):
    from app.utils.cache_backend import CACHE_NAMESPACE_REGISTRY, STORE
    for namespace in CACHE_NAMESPACE_REGISTRY:
        STORE.clear(namespace)
    return _retire_recognition_spool(job_id)


def _validate_backup_archive(path, job_id=None):
    expected_schema = _manifest_schema()
    expected_tables = set(Base.metadata.tables)
    expected_revision = current_schema_revision()
    with zipfile.ZipFile(path, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ValueError("ZIP 中包含重复文件名")
        if len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise ValueError("备份文件条目过多")
        total_uncompressed = 0
        for info in infos:
            name = info.filename
            if (not name or name.startswith(("/", "\\")) or "\\" in name
                    or "\x00" in name or any(part in {"", ".", ".."}
                                               for part in name.split("/"))):
                raise ValueError("ZIP 中包含非法路径")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("备份中不允许符号链接")
            if info.is_dir():
                raise ValueError("备份中不允许目录条目")
            if info.file_size < 0 or info.compress_size < 0:
                raise ValueError("ZIP 文件大小无效")
            total_uncompressed += info.file_size
            if total_uncompressed > _MAX_ARCHIVE_BYTES:
                raise ValueError("解压后备份体积超过服务器限制")
            if info.file_size and info.compress_size == 0:
                raise ValueError("ZIP 压缩信息无效")
            if info.compress_size and info.file_size / info.compress_size > 10000:
                raise ValueError("ZIP 压缩比异常，拒绝解压")
        if "manifest.json" not in names:
            raise ValueError("备份缺少 manifest.json")
        manifest_info = archive.getinfo("manifest.json")
        if manifest_info.file_size > _MAX_MANIFEST_BYTES:
            raise ValueError("备份清单超过允许大小")
        manifest_bytes = archive.read(manifest_info)
        manifest = json.loads(manifest_bytes.decode("utf-8"),
                              parse_constant=_reject_json_constant)
        if (not isinstance(manifest, dict)
                or manifest.get("format") != "nastools-full-database"
                or manifest.get("format_version") != 1
                or manifest.get("full_database") is not True):
            raise ValueError("该 ZIP 不是受支持的完整数据库备份")
        if manifest.get("schema_revision") != expected_revision:
            raise ValueError("备份结构版本不受支持，需先执行明确的版本转换")
        if manifest.get("schema") != expected_schema:
            raise ValueError("备份表结构与当前统一数据库结构不匹配")
        expected_domains = {key: sorted(value)
                            for key, value in LOGICAL_DOMAIN_TABLES.items()}
        if manifest.get("logical_domains") != expected_domains:
            raise ValueError("备份未覆盖当前要求的全部逻辑数据域")
        if manifest.get("source_database_type") not in {"sqlite", "postgresql", "mysql", "mariadb"}:
            raise ValueError("备份来源数据库类型无效")
        table_entries = manifest.get("tables")
        if not isinstance(table_entries, dict) or set(table_entries) != expected_tables:
            raise ValueError("备份未覆盖全部应用表，不能作为完整数据库恢复")
        expected_files = {"manifest.json"}
        content_digests = {}
        for table in Base.metadata.sorted_tables:
            if job_id and STORAGE_JOBS.cancel_requested(job_id):
                raise InterruptedError("已取消备份结构和数据检查")
            details = table_entries.get(table.name)
            expected_file = f"tables/{table.name}.jsonl"
            if not isinstance(details, dict) or details.get("file") != expected_file:
                raise ValueError(f"备份表 {table.name} 文件映射无效")
            expected_files.add(expected_file)
            info = archive.getinfo(expected_file)
            if info.file_size > _MAX_ARCHIVE_BYTES:
                raise ValueError(f"备份表 {table.name} 文件超过允许大小")
            digest = hashlib.sha256()
            content_digest = hashlib.sha256()
            rows = 0
            with archive.open(info, "r") as source:
                while True:
                    line = source.readline(64 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if rows % _BATCH_SIZE == 0 and job_id \
                            and STORAGE_JOBS.cancel_requested(job_id):
                        raise InterruptedError("已取消备份结构和数据检查")
                    if len(line) > 64 * 1024 * 1024 or not line.endswith(b"\n"):
                        raise ValueError(f"备份表 {table.name} 存在过长或不完整记录")
                    digest.update(line)
                    record = json.loads(line.decode("utf-8"),
                                        parse_constant=_reject_json_constant)
                    decoded = _decode_backup_row(record, table)
                    from app.db.migration import _normalize_value
                    canonical = json.dumps(
                        [_normalize_value(decoded[column.name]) for column in table.columns],
                        ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    content_digest.update(canonical.encode("utf-8", "surrogatepass") + b"\n")
                    rows += 1
            if (digest.hexdigest() != details.get("sha256")
                    or rows != details.get("rows")
                    or type(details.get("rows")) is not int):
                raise ValueError(f"备份表 {table.name} 的记录数或 SHA-256 校验失败")
            content_digests[table.name] = {"rows": rows, "sha256": content_digest.hexdigest()}
        if set(names) != expected_files:
            raise ValueError("ZIP 中包含未知文件或缺少表数据文件")
    return {"manifest": manifest, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "content_digests": content_digests,
            "archive_sha256": _sha256_path(path, job_id=job_id), "size_bytes": _file_size(path)}


def check_database_backup(values=None):
    upload_id = str((values or {}).get("upload_id") or "")
    path = _upload_path(upload_id)
    if not path:
        return {"code": 1, "msg": "上传备份不存在或已过期"}
    info = os.stat(path)
    job, created = STORAGE_JOBS.create(
        _BACKUP_CHECK_KIND, {"upload_id": upload_id, "size_bytes": info.st_size,
                             "mtime_ns": info.st_mtime_ns},
        idempotency_key=f"backup-check:{upload_id}:{info.st_size}:{info.st_mtime_ns}")
    if not created:
        return {"code": 0, "job_id": job["job_id"], "job": job}
    thread = threading.Thread(target=_run_backup_check, args=(job["job_id"], upload_id, path),
                              name=f"database-backup-check-{job['job_id'][:8]}", daemon=True)
    with _WORKERS_LOCK:
        _WORKERS[job["job_id"]] = thread
    thread.start()
    return {"code": 0, "job_id": job["job_id"], "job": STORAGE_JOBS.get(job["job_id"])}


def _run_backup_check(job_id, upload_id, path):
    try:
        STORAGE_JOBS.update(job_id, status="CHECKING_BACKUP", stage="CHECKING_BACKUP",
                            message="正在校验备份文件、结构和逐表数据摘要")
        with zipfile.ZipFile(path, "r") as archive:
            is_current_format = "manifest.json" in archive.namelist()
        result = (_validate_backup_archive(path, job_id=job_id)
                  if is_current_format else
                  _convert_legacy_dual_backup(path, upload_id, job_id))
        manifest = result["manifest"]
        summary = {"upload_id": upload_id, "source_database_type": manifest["source_database_type"],
                   "application_version": manifest.get("application_version"),
                   "schema_revision": manifest["schema_revision"],
                   "created_at": manifest.get("created_at"),
                   "tables": {name: details["rows"] for name, details in manifest["tables"].items()},
                   "content_digests": result["content_digests"],
                   "total_rows": sum(details["rows"] for details in manifest["tables"].values()),
                   "size_bytes": result["size_bytes"],
                   "manifest_sha256": result["manifest_sha256"],
                   "archive_sha256": result["archive_sha256"],
                   "legacy_converted": bool(result.get("legacy_converted")),
                   "full_database": True}
        STORAGE_JOBS.update(job_id, status="READY", stage="READY", result=summary,
                            message=("旧完整双库备份已转换并完成逐表校验，可进入恢复确认"
                                     if result.get("legacy_converted") else
                                     "完整备份与所有表数据校验通过，可进入恢复确认"), progress=1)
    except InterruptedError as error:
        STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                            message=str(error))
    except Exception as error:
        STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                            error=_safe_database_error(error),
                            message="备份检查失败；活动数据库未被触碰")
    finally:
        with _WORKERS_LOCK:
            _WORKERS.pop(job_id, None)


def _import_backup_data(archive_path, connection, job_id, job_status,
                        delete_existing=False):
    from app.db.migration import _normalize_value, _table_digest

    with zipfile.ZipFile(archive_path, "r") as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        if delete_existing:
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())
        # Empty tables are part of a complete backup too; their digest is the
        # SHA-256 of an empty stream and must be compared during restore.
        expected_digests = {
            table.name: hashlib.sha256() for table in Base.metadata.sorted_tables
        }
        for index, table in enumerate(Base.metadata.sorted_tables, start=1):
            details = manifest["tables"][table.name]
            digest = hashlib.sha256()
            count = 0
            batch = []
            with archive.open(details["file"], "r") as source:
                while True:
                    line = source.readline(64 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if job_status == "BUILDING_STAGE" and count % _BATCH_SIZE == 0 \
                            and STORAGE_JOBS.cancel_requested(job_id):
                        raise InterruptedError("已取消 SQLite 恢复暂存导入")
                    if len(line) > 64 * 1024 * 1024 or not line.endswith(b"\n"):
                        raise ValueError(f"备份表 {table.name} 存在过长或不完整记录")
                    digest.update(line)
                    raw = json.loads(line.decode("utf-8"), parse_constant=_reject_json_constant)
                    row = _decode_backup_row(raw, table)
                    encoded = json.dumps(
                        [_normalize_value(row[column.name]) for column in table.columns],
                        ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    expected_digests[table.name].update(
                        encoded.encode("utf-8", "surrogatepass") + b"\n")
                    batch.append(row)
                    count += 1
                    if len(batch) >= _BATCH_SIZE:
                        connection.execute(table.insert(), batch)
                        batch.clear()
                if batch:
                    connection.execute(table.insert(), batch)
            if (count != details["rows"] or digest.hexdigest() != details["sha256"]):
                raise ValueError(f"备份表 {table.name} 导入时摘要或行数发生变化")
            STORAGE_JOBS.update(
                job_id, status=job_status, stage=job_status,
                message=f"已导入 {index}/{len(Base.metadata.tables)} 张表",
                progress=index / len(Base.metadata.tables) * 0.72)

        actual = {}
        for table in Base.metadata.sorted_tables:
            rows, table_digest = _table_digest(connection, table)
            expected_rows = int(manifest["tables"][table.name]["rows"])
            expected_digest = expected_digests[table.name].hexdigest()
            if rows != expected_rows or table_digest != expected_digest:
                raise RuntimeError(f"恢复后表 {table.name} 的内容摘要不一致")
            actual[table.name] = {"rows": rows, "sha256": table_digest}
    return actual


def start_database_restore(values=None):
    values = dict(values or {})
    check_id = str(values.get("check_id") or "")
    if values.get("confirm_replace") is not True:
        return {"code": 1, "msg": "必须在界面确认替换当前数据库后才能恢复"}
    check = STORAGE_JOBS.get(check_id)
    if (not check or check.get("kind") != _BACKUP_CHECK_KIND
            or check.get("status") != "READY"):
        return {"code": 1, "msg": "备份检查凭证无效，请重新上传并检查"}
    if time.time() - float(check.get("created_at") or 0) > 600:
        STORAGE_JOBS.update(check_id, status="CANCELLED", stage="CANCELLED",
                            message="备份检查凭证已过期，请重新检查文件")
        return {"code": 1, "msg": "备份检查凭证已过期，请重新检查文件"}
    upload_id = (check.get("result") or {}).get("upload_id")
    upload_path = _upload_path(upload_id)
    if not upload_path:
        return {"code": 1, "msg": "上传备份在检查后已过期，请重新上传"}
    settings, fingerprint, safe = _settings()
    job, created = STORAGE_JOBS.create(
        _RESTORE_KIND,
        {"check_id": check_id, "upload_id": upload_id,
         "archive_sha256": (check.get("result") or {}).get("archive_sha256"),
         "content_digests": (check.get("result") or {}).get("content_digests"),
         "fingerprint": fingerprint, "database": safe, "confirmed": True},
        idempotency_key=f"restore:{check_id}:{fingerprint}")
    if not created:
        return {"code": 0, "job_id": job["job_id"], "job": job}
    thread = threading.Thread(target=_run_database_restore,
                              args=(job["job_id"], settings, fingerprint, upload_path),
                              name=f"database-restore-{job['job_id'][:8]}", daemon=True)
    with _WORKERS_LOCK:
        _WORKERS[job["job_id"]] = thread
    thread.start()
    return {"code": 0, "job_id": job["job_id"], "job": STORAGE_JOBS.get(job["job_id"])}


def retry_database_restore_restart(job_id):
    job = STORAGE_JOBS.get(job_id)
    if (not job or job.get("kind") != _RESTORE_KIND
            or job.get("status") not in {"RESTART_PENDING", "RECOVERY_REQUIRED"}
            or not (job.get("result") or {}).get("restore_validated")):
        return False
    from app.db.migration import _current_settings, _fingerprint

    with STORAGE_JOBS.maintenance_lock(exclusive=True):
        job = STORAGE_JOBS.get(job_id)
        if (not job or job.get("status") not in {"RESTART_PENDING", "RECOVERY_REQUIRED"}
                or not (job.get("result") or {}).get("restore_validated")):
            return False
        payload = job.get("payload") or {}
        if not hmac_compare(_fingerprint(_current_settings()), payload.get("fingerprint")):
            raise RuntimeError("活动数据库配置与恢复任务不匹配，拒绝重启")
        gate = STORAGE_JOBS.maintenance_gate("database")
        if gate and gate.get("job_id") != job_id:
            raise RuntimeError("数据库维护闸门属于其他任务，拒绝重启")
        if not gate:
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "VERIFYING_RESTORE")
        engine = DATABASE.engine
        _verify_revision(engine)
        if not _content_digests_match(engine, _restore_expected_digests(job)):
            raise RuntimeError("恢复后的数据库内容不再匹配检查通过的备份")
        retired_spool = _clear_restore_derived_state(job_id)
        result = dict(job.get("result") or {})
        result.update({"retired_spool": retired_spool, "restart_required": True,
                       "restore_validated": True})
        STORAGE_JOBS.set_maintenance_gate("database", job_id, "RESTART_PENDING")
        STORAGE_JOBS.update(job_id, status="RESTART_PENDING", stage="RESTART_PENDING",
                            result=result, error=None,
                            message="恢复数据已重新校验，等待受控重启", progress=0.98)
    schedule_restart()
    return True


def _run_database_restore_impl(job_id, settings, fingerprint, upload_path):
    from app.db.migration import (_calibrate_sequences, _current_settings,
                                  _ensure_other_jobs_idle, _fingerprint,
                                  _new_engine, _snapshot_source, _sqlite_integrity,
                                  _validate_source_shape, _write_schema_revision)

    source_engine = source_connection = source_transaction = None
    current_snapshot_path = None
    stage_dir = None
    stage_engine = None
    rollback_path = None
    published = False
    sqlite_quiesced = False
    database_path = None
    gate_set = False
    transaction_committed = False
    restore_validated = False
    try:
        if _sha256_path(upload_path, job_id=job_id) != (STORAGE_JOBS.get(job_id)["payload"] or {}).get(
                "archive_sha256"):
            raise RuntimeError("上傳备份在恢复前发生变化")
        if STORAGE_JOBS.cancel_requested(job_id):
            raise InterruptedError("已取消数据库恢复")
        checked = _validate_backup_archive(upload_path, job_id=job_id)
        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            if not hmac_compare(_fingerprint(_current_settings()), fingerprint):
                raise RuntimeError("活动数据库配置已变化，拒绝使用过期恢复凭证")
            _ensure_other_jobs_idle(job_id)
            if DATABASE._engine is None:
                engine = DATABASE.engine
            else:
                engine = DATABASE._engine
            _verify_revision(engine)
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "BACKING_UP_CURRENT")
            gate_set = True
            STORAGE_JOBS.update(job_id, status="BACKING_UP_CURRENT", stage="BACKING_UP_CURRENT",
                                message="正在创建恢复前完整数据库备份", progress=0.01)

            source_engine, source_connection, source_transaction, current_snapshot_path = \
                _snapshot_source(settings, job_id + "-before-restore")
            _validate_source_shape(source_connection, Base.metadata)
            pre_restore_path = os.path.join(_backup_root(), f"pre-restore-{job_id}.zip")
            pre_restore_manifest = _export_backup(
                source_connection, pre_restore_path, job_id, settings["type"],
                job_status="BACKING_UP_CURRENT", stage="BACKING_UP_CURRENT")
            STORAGE_JOBS.update(
                job_id, status="BACKING_UP_CURRENT", stage="BACKING_UP_CURRENT",
                result={"pre_restore_backup_id": job_id,
                        "pre_restore_sha256": _sha256_path(pre_restore_path),
                        "pre_restore_rows": {name: details["rows"]
                                             for name, details in pre_restore_manifest["tables"].items()},
                        "upload_id": (STORAGE_JOBS.get(job_id)["payload"] or {}).get("upload_id"),
                        "upload_sha256": checked["archive_sha256"]},
                message="恢复前完整备份已生成，开始构建隔离恢复目标", progress=0.1)
            if STORAGE_JOBS.cancel_requested(job_id):
                raise InterruptedError("在修改当前数据库前已取消恢复")
            if source_transaction is not None:
                source_transaction.rollback()
                source_transaction = None
            if source_connection is not None:
                source_connection.close()
                source_connection = None
            if current_snapshot_path and source_engine is not None:
                source_engine.dispose()
                source_engine = None
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(current_snapshot_path + suffix)
                    except FileNotFoundError:
                        pass
                current_snapshot_path = None

            restore_manifest = checked["manifest"]
            if settings["type"] == "sqlite":
                config_path = _config().get_config_path()
                stage_dir = os.path.join(config_path, "db", ".staging",
                                         f"database-restore-{job_id}")
                os.makedirs(stage_dir, mode=0o700, exist_ok=False)
                try:
                    os.chmod(stage_dir, 0o700)
                except OSError:
                    pass
                staged_path = os.path.join(stage_dir, "nastools.restored.sqlite3")
                stage_engine = _new_engine({"type": "sqlite"}, sqlite_path=staged_path)
                Base.metadata.create_all(stage_engine)
                _write_schema_revision(stage_engine)
                STORAGE_JOBS.update(job_id, status="BUILDING_STAGE", stage="BUILDING_STAGE",
                                    message="正在向 SQLite 隔离暂存库流式导入备份",
                                    progress=0.12)
                with stage_engine.begin() as connection:
                    imported_digests = _import_backup_data(
                        upload_path, connection, job_id, "BUILDING_STAGE")
                    if imported_digests != checked["content_digests"]:
                        raise RuntimeError("SQLite 恢复暂存库与已检查备份摘要不一致")
                    _invalidate_restored_media_sync_leases(connection)
                restored_digests = _digest_tables(stage_engine)
                _verify_revision(stage_engine)
                if restored_digests != _digest_tables(stage_engine):
                    raise RuntimeError("SQLite 暂存库逐表内容校验失败")
                if STORAGE_JOBS.cancel_requested(job_id):
                    raise InterruptedError("SQLite 暂存导入完成但尚未发布，已取消恢复")
                stage_engine.dispose()
                stage_engine = None
                stage_connection = sqlite3.connect(staged_path, timeout=60, isolation_level=None)
                try:
                    stage_checkpoint = stage_connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    _check_checkpoint(stage_checkpoint, "SQLite 恢复暂存库")
                    if stage_connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise RuntimeError("SQLite 恢复暂存库完整性检查失败")
                finally:
                    stage_connection.close()
                _secure_file(staged_path)

                database_path = _sqlite_path()
                engine = DATABASE._engine
                DATABASE.close_session()
                if engine is not None and engine.pool.checkedout():
                    raise RuntimeError("仍有活动数据库连接，拒绝发布恢复目标")
                DATABASE.dispose()
                sqlite_quiesced = True
                current_connection = sqlite3.connect(
                    database_path, timeout=60, isolation_level=None)
                try:
                    old_checkpoint = current_connection.execute(
                        "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    _check_checkpoint(old_checkpoint, "当前数据库恢复前")
                    old_identity = _file_identity(database_path)
                    rollback_path = os.path.join(stage_dir, "database-before-restore.sqlite3")
                    rollback_connection = sqlite3.connect(
                        rollback_path, timeout=60, isolation_level=None)
                    try:
                        current_connection.backup(rollback_connection, pages=1000, sleep=0.01)
                        rollback_connection.commit()
                    finally:
                        rollback_connection.close()
                finally:
                    current_connection.close()
                _secure_file(rollback_path)
                staged_identity = _file_identity(staged_path)
                result = STORAGE_JOBS.get(job_id).get("result") or {}
                result.update({"rollback_path": rollback_path,
                               "rollback_sha256": _sha256_path(rollback_path),
                               "old_identity": old_identity,
                               "staged_identity": staged_identity,
                               "staged_path": staged_path,
                               "restored_digests": restored_digests,
                               "publish_intent": True,
                               "restore_archive_sha256": checked["archive_sha256"]})
                claimed = STORAGE_JOBS.update(
                    job_id, status="PUBLISHING_RESTORE", stage="PUBLISHING_RESTORE",
                    result=result, message="校验与回滚副本已就绪，准备原子发布",
                    progress=0.84, expected_status="BUILDING_STAGE")
                if not claimed:
                    raise RuntimeError("恢复任务状态已变化，拒绝发布 SQLite 目标")
                if STORAGE_JOBS.cancel_requested(job_id):
                    raise InterruptedError("SQLite 原子发布前收到取消请求")
                STORAGE_JOBS.set_maintenance_gate("database", job_id, "PUBLISHING_RESTORE")
                STORAGE_JOBS.update(job_id, message="正在原子发布恢复数据库")
                sidecars = []
                for suffix in ("-wal", "-shm"):
                    sidecar = database_path + suffix
                    if os.path.exists(sidecar):
                        moved = os.path.join(stage_dir, "old-database" + suffix)
                        os.replace(sidecar, moved)
                        sidecars.append({"path": moved, "suffix": suffix})
                result["old_sidecars"] = sidecars
                STORAGE_JOBS.update(job_id, result=result,
                                    message="旧 WAL/SHM 已安全隔离，正在替换数据库主文件")
                os.replace(staged_path, database_path)
                published = True
                _fsync_file_and_directory(database_path)
                STORAGE_JOBS.update(job_id, result={**result, "published": True,
                                                    "published_identity": _file_identity(database_path)},
                                    message="恢复数据库已发布，正在做最终校验")
                _sqlite_integrity(database_path)
                restored_engine = DATABASE.engine
                _verify_revision(restored_engine)
                if restored_digests != _digest_tables(restored_engine):
                    raise RuntimeError("发布后的 SQLite 数据与备份内容不一致")
            else:
                if STORAGE_JOBS.cancel_requested(job_id):
                    raise InterruptedError("远程恢复事务开始前已取消")
                claimed = STORAGE_JOBS.update(
                    job_id, status="RESTORING", stage="RESTORING",
                    message="正在事务性替换远程数据库中的应用数据", progress=0.2,
                    expected_status="BACKING_UP_CURRENT")
                if not claimed:
                    raise RuntimeError("恢复任务状态已变化，拒绝写入远程数据库")
                if STORAGE_JOBS.cancel_requested(job_id):
                    raise InterruptedError("远程恢复事务开始前收到取消请求")
                STORAGE_JOBS.set_maintenance_gate("database", job_id, "RESTORING")
                actual_engine = DATABASE.engine
                with actual_engine.begin() as connection:
                    if connection.dialect.name == "postgresql":
                        preparer = actual_engine.dialect.identifier_preparer
                        for table in Base.metadata.sorted_tables:
                            connection.exec_driver_sql(
                                f"LOCK TABLE {preparer.format_table(table)} IN ACCESS EXCLUSIVE MODE")
                    imported_digests = _import_backup_data(
                        upload_path, connection, job_id, "RESTORING", delete_existing=True)
                    if imported_digests != checked["content_digests"]:
                        raise RuntimeError("远程恢复事务与已检查备份摘要不一致")
                    _invalidate_restored_media_sync_leases(connection)
                restored_digests = _digest_tables(actual_engine)
                transaction_committed = True
                STORAGE_JOBS.update(job_id, status="CALIBRATING", stage="CALIBRATING",
                                    result={"pre_restore_backup_id": job_id,
                                            "pre_restore_sha256": _sha256_path(pre_restore_path),
                                            "upload_id": (STORAGE_JOBS.get(job_id)["payload"] or {}).get("upload_id"),
                                            "upload_sha256": checked["archive_sha256"],
                                            "restore_archive_sha256": checked["archive_sha256"],
                                            "restored_digests": restored_digests,
                                            "restore_committed": True},
                                    message="恢复事务已提交，正在校准自增值并验证连接",
                                    progress=0.82)
                _calibrate_sequences(actual_engine)
                _verify_revision(actual_engine)
                if restored_digests != _digest_tables(actual_engine):
                    raise RuntimeError("远程数据库提交后的内容摘要不一致")
                with actual_engine.connect() as connection:
                    connection.execute(text("SELECT 1")).scalar_one()

            restore_validated = True
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "VERIFYING_RESTORE")
            STORAGE_JOBS.update(job_id, status="VERIFYING_RESTORE", stage="VERIFYING_RESTORE",
                                result={**(STORAGE_JOBS.get(job_id).get("result") or {}),
                                        "restore_archive_sha256": checked["archive_sha256"],
                                        "restored_digests": restored_digests,
                                        "restore_validated": True},
                                message="恢复数据和主键校验通过，正在清理旧派生状态",
                                progress=0.93)
            retired_spool = _clear_restore_derived_state(job_id)
            STORAGE_JOBS.set_maintenance_gate("database", job_id, "RESTART_PENDING")
            final_result = STORAGE_JOBS.get(job_id).get("result") or {}
            final_result.update({"restore_archive_sha256": checked["archive_sha256"],
                                 "source_database_type": restore_manifest["source_database_type"],
                                 "schema_revision": restore_manifest["schema_revision"],
                                 "restored_digests": restored_digests,
                                 "retired_spool": retired_spool,
                                 "restart_required": True})
            STORAGE_JOBS.update(job_id, status="RESTART_PENDING", stage="RESTART_PENDING",
                                result=final_result, message="数据库恢复和校验完成，等待受控重启",
                                progress=0.98)
            schedule_restart()
    except InterruptedError as error:
        if sqlite_quiesced and not published:
            try:
                current = STORAGE_JOBS.get(job_id) or {}
                _restore_old_sqlite_sidecars(job_id, current.get("result") or {})
                engine = DATABASE.engine
                _verify_revision(engine)
                if (current.get("result") or {}).get("pre_restore_sha256") and not \
                        _pre_restore_matches(_pre_restore_backup_path(job_id), engine):
                    raise RuntimeError("取消恢复后活动 SQLite 与恢复前完整备份不一致")
            except Exception as recovery_error:
                STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED",
                                    stage="RECOVERY_REQUIRED",
                                    error=f"{str(error)[:700]}；取消后恢复校验失败："
                                          f"{str(recovery_error)[:700]}",
                                    message="恢复取消后活动数据库未能确认，保持维护模式")
                gate_set = True
            else:
                STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                    error=None, message=str(error))
        else:
            STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                error=None, message=str(error))
    except Exception as error:
        current = STORAGE_JOBS.get(job_id) or {}
        if restore_validated:
            STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                                error=_safe_database_error(error, settings),
                                message="恢复数据已校验，但派生状态清理或重启准备失败；保持维护模式")
            gate_set = True
        elif published and rollback_path:
            try:
                DATABASE.dispose()
                path = _sqlite_path()
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(path + suffix)
                    except FileNotFoundError:
                        pass
                if _sha256_path(rollback_path) != (current.get("result") or {}).get(
                        "rollback_sha256"):
                    raise RuntimeError("SQLite 恢复回滚副本摘要不符")
                os.replace(rollback_path, path)
                _fsync_file_and_directory(path)
                restored_engine = DATABASE.engine
                _verify_revision(restored_engine)
                if not _pre_restore_matches(_pre_restore_backup_path(job_id), restored_engine):
                    raise RuntimeError("SQLite 恢复回滚后与恢复前完整备份内容不一致")
                STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                    error=_safe_database_error(error, settings),
                                    message="SQLite 恢复校验失败，已恢复原活动数据库")
                gate_set = True
            except Exception as rollback_error:
                STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED",
                                    stage="RECOVERY_REQUIRED",
                                    error=f"{str(error)[:700]}；回滚失败：{str(rollback_error)[:700]}",
                                    message="SQLite 恢复与回滚均未能确认，保持维护模式")
        elif transaction_committed:
            STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                                error=_safe_database_error(error, settings),
                                message="远程恢复事务已提交但后续校准或验证失败，保持维护模式")
            gate_set = True
        elif settings["type"] == "sqlite" and sqlite_quiesced:
            try:
                _restore_old_sqlite_sidecars(job_id, current.get("result") or {})
                restored_engine = DATABASE.engine
                _verify_revision(restored_engine)
                if (current.get("result") or {}).get("pre_restore_sha256") and not \
                        _pre_restore_matches(_pre_restore_backup_path(job_id), restored_engine):
                    raise RuntimeError("SQLite 活动数据库与恢复前完整备份不一致")
                STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                    error=_safe_database_error(error, settings),
                                    message="SQLite 恢复发布前失败；原活动数据库已确认")
            except Exception as recovery_error:
                STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED",
                                    stage="RECOVERY_REQUIRED",
                                    error=f"{_safe_database_error(error, settings)[:700]}；原数据库校验失败："
                                          f"{_safe_database_error(recovery_error, settings)[:700]}",
                                    message="SQLite 恢复发布前失败且原数据库状态未确认，保持维护模式")
                gate_set = True
        else:
            STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                error=_safe_database_error(error, settings),
                                message="数据库恢复失败；活动数据库内容保持原值")
    finally:
        if source_transaction is not None:
            try:
                source_transaction.rollback()
            except Exception:
                pass
        if source_connection is not None:
            source_connection.close()
        if current_snapshot_path and source_engine is not None:
            source_engine.dispose()
        if stage_engine is not None:
            stage_engine.dispose()
        current = STORAGE_JOBS.get(job_id) or {}
        if gate_set and current.get("status") not in {"RESTART_PENDING", "RECOVERY_REQUIRED"}:
            try:
                with STORAGE_JOBS.maintenance_lock(exclusive=True):
                    STORAGE_JOBS.clear_maintenance_gate("database", job_id)
            except Exception:
                pass
        if current.get("status") in {"FAILED", "CANCELLED"} and stage_dir:
            shutil.rmtree(stage_dir, ignore_errors=True)
        with _WORKERS_LOCK:
            _WORKERS.pop(job_id, None)


def _run_database_restore(job_id, settings, fingerprint, upload_path):
    from app.media.recognition.records import (quiesce_recognition,
                                               replay_recognition_spool,
                                               resume_recognition)
    quiesced = False
    try:
        quiesce_recognition(timeout=120)
        quiesced = True
        spool_result = replay_recognition_spool()
        if spool_result.get("pending"):
            raise RuntimeError("识别暂存记录未能全部写入当前数据库，已中止恢复")
        _run_database_restore_impl(job_id, settings, fingerprint, upload_path)
    except Exception as error:
        current = STORAGE_JOBS.get(job_id) or {}
        if current.get("status") not in {
                "RESTART_PENDING", "VERIFYING_RESTORE", "SUCCEEDED", "RECOVERY_REQUIRED",
                "FAILED", "CANCELLED"}:
            STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                error=_safe_database_error(error, settings),
                                message="数据库恢复在快照前失败；活动数据库保持不变")
    finally:
        current = STORAGE_JOBS.get(job_id) or {}
        if quiesced and current.get("status") not in {"RESTART_PENDING", "RECOVERY_REQUIRED"}:
            resume_recognition()
