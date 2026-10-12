"""缓存存储检查、迁移、配置提交和启动后校验。"""

import copy
import hashlib
import hmac
import json
import logging
import os
import pickle
import shutil
import sqlite3
import sys
import threading
import time

from app.utils.cache_backend import (
    CACHE_NAMESPACE_REGISTRY, DEFAULT_SETTINGS, CacheStore, STORE, _SQLiteCache, validate_settings,
)
from app.utils.storage_jobs import STORAGE_JOBS, TERMINAL_STATES, schedule_restart

logger = logging.getLogger(__name__)


_WORKERS = {}
_WORKERS_LOCK = threading.Lock()
_CHECK_TTL_SECONDS = 600
_STORAGE_FIELDS = tuple(key for key in DEFAULT_SETTINGS if key != "storage_generation")
_EXCLUDED_CACHES = [
    {"name": "TokenCache", "reason": "认证令牌只保存在本机内存，重启后重新获取"},
    {"name": "ConfigLoadCache", "reason": "配置加载缓存只保存在本机内存，重启后重新加载"},
]


def _candidate(values):
    from config import Config

    current = Config().get_config("cache") or {}
    if not isinstance(current, dict):
        current = {}
    candidate = {**DEFAULT_SETTINGS, **current}
    submitted = values if isinstance(values, dict) else {}
    for key in _STORAGE_FIELDS:
        if key in submitted:
            candidate[key] = submitted[key]
    # 同一远程目标的空用户名/密码表示保持原值，不能把凭据带到新地址。
    same_endpoint = all(str(candidate.get(key, DEFAULT_SETTINGS.get(key)))
                        == str(current.get(key, DEFAULT_SETTINGS.get(key)))
                        for key in ("backend", "host", "port", "db", "auth_mode"))
    if not candidate.get("username") and current.get("username") and same_endpoint \
            and candidate.get("auth_mode") == "password":
        candidate["username"] = current["username"]
    same_identity = same_endpoint and str(candidate.get("username") or "") == str(
        current.get("username") or "")
    if (not candidate.get("password") and current.get("password") and same_identity
            and candidate.get("auth_mode") == "password"):
        candidate["password"] = current["password"]
    validate_settings(candidate)
    return candidate


def _safe_candidate(candidate):
    result = {key: candidate.get(key) for key in DEFAULT_SETTINGS
              if key not in {"password", "username"}}
    result["username_set"] = bool(candidate.get("username"))
    result["password_set"] = bool(candidate.get("password"))
    return result


def _safe_error(error, *candidates):
    """Keep driver errors useful without exposing configured cache passwords."""
    value = str(error)
    passwords = [candidate.get("password") for candidate in candidates
                 if isinstance(candidate, dict) and candidate.get("password")]
    try:
        from config import Config
        active = Config().get_config("cache") or {}
        if isinstance(active, dict) and active.get("password"):
            passwords.append(active["password"])
    except Exception:
        pass
    for password in sorted({str(secret) for secret in passwords}, key=len, reverse=True):
        value = value.replace(password, "***")
    return value[:1500]


def _fingerprint(candidate):
    encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_store(left, right):
    if left.get("backend") != right.get("backend"):
        return False
    if left.get("backend") == "memory":
        return False
    if left.get("backend") == "memory_disk":
        return True
    keys = ("host", "port", "db", "auth_mode", "username", "password", "storage_generation")
    return all(str(left.get(key)) == str(right.get(key)) for key in keys)


def _same_configuration(left, right):
    return all(str(left.get(key, DEFAULT_SETTINGS.get(key)))
               == str(right.get(key, DEFAULT_SETTINGS.get(key)))
               for key in DEFAULT_SETTINGS)


def _snapshot_directory(job_id):
    from config import Config
    return os.path.join(Config().get_config_path(), "cache", ".staging", job_id)


def _target_cache_directory(job_id):
    return os.path.join(_snapshot_directory(job_id), "target-cache")


def _snapshot_path(job_id):
    directory = _snapshot_directory(job_id)
    os.makedirs(os.path.dirname(directory), mode=0o700, exist_ok=True)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for private_directory in (os.path.dirname(directory), directory):
        try:
            os.chmod(private_directory, 0o700)
        except OSError:
            pass
    return os.path.join(directory, "snapshot.sqlite3")


def _snapshot_manifest_path(job_id):
    return os.path.join(_snapshot_directory(job_id), "snapshot.complete.json")


def _validate_snapshot_database(path):
    if not os.path.isfile(path):
        raise ValueError("缓存快照文件缺失")
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("缓存快照 SQLite 完整性检查失败")
        known_namespaces = {row[0] for row in connection.execute(
            "SELECT DISTINCT namespace FROM cache_entries_v2").fetchall()}
        if not known_namespaces.issubset(set(CACHE_NAMESPACE_REGISTRY)):
            raise ValueError("缓存快照包含未知命名空间")
    except sqlite3.DatabaseError as error:
        raise ValueError("缓存快照数据库损坏") from error
    finally:
        connection.close()


def _snapshot_namespace_digest(path, namespace):
    """Hash every committed snapshot row, including rows that expired since capture."""
    if not os.path.isfile(path):
        raise ValueError("缓存快照文件缺失")
    uri = f"file:{os.path.abspath(path)}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        cursor = connection.execute(
            "SELECT cache_key, payload, expires FROM cache_entries_v2 "
            "WHERE namespace=? ORDER BY cache_key", (namespace,))

        def records():
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for cache_key, payload, expires in rows:
                    record = _SQLiteCache._decode(payload)
                    if (CacheStore._key(record[0]) != cache_key or record[2] != expires):
                        raise ValueError(f"缓存快照命名空间 {namespace} 的记录结构无效")
                    yield record

        return _record_digest(records(), now=float("-inf"))
    except sqlite3.DatabaseError as error:
        raise ValueError("缓存快照数据库损坏") from error
    finally:
        connection.close()


def _write_snapshot_manifest(job_id, snapshot_path, source_status):
    _checkpoint_sqlite(snapshot_path)
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(snapshot_path + suffix)
        except FileNotFoundError:
            pass
    descriptor = os.open(snapshot_path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.chmod(snapshot_path, 0o600)
    except OSError:
        pass
    _validate_snapshot_database(snapshot_path)
    namespaces = {name: _snapshot_namespace_digest(snapshot_path, name)
                  for name in CACHE_NAMESPACE_REGISTRY}
    manifest = {"version": 1, "job_id": job_id,
                "source": {key: source_status.get(key)
                           for key in ("active_backend", "generation", "config_generation")},
                "namespaces": namespaces}
    path = _snapshot_manifest_path(job_id)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)
    _fsync_directory(os.path.dirname(path))
    return manifest


def _validate_snapshot_manifest(job_id, snapshot_path, expected_source=None):
    path = _snapshot_manifest_path(job_id)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, ValueError) as error:
        raise ValueError("缓存完整快照清单缺失或损坏，无法恢复内存缓存") from error
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or manifest.get("job_id") != job_id
            or not isinstance(manifest.get("namespaces"), dict)
            or set(manifest["namespaces"]) != set(CACHE_NAMESPACE_REGISTRY)):
        raise ValueError("缓存完整快照清单不完整，无法恢复内存缓存")
    manifest_source = manifest.get("source") or {}
    if manifest_source.get("active_backend") not in {"memory", "memory_disk", "redis", "valkey"}:
        raise ValueError("缓存快照来源后端无效")
    if expected_source:
        for key in ("active_backend", "generation", "config_generation"):
            expected = expected_source.get(key)
            if expected is not None and manifest_source.get(key) != expected:
                raise ValueError("缓存快照代次与迁移任务不匹配")
    _validate_snapshot_database(snapshot_path)
    for namespace in CACHE_NAMESPACE_REGISTRY:
        actual = _snapshot_namespace_digest(snapshot_path, namespace)
        expected = manifest["namespaces"].get(namespace)
        if not isinstance(expected, (list, tuple)) or tuple(expected) != actual:
            raise ValueError(f"缓存快照命名空间 {namespace} 不完整")
    return manifest


def _write_snapshot_namespace(path, namespace, records):
    cache = _SQLiteCache(path, namespace)
    source_count = 0
    saved_count = 0
    wallpaper_record = None
    batch = []
    for key, value, expires in records:
        source_count += 1
        record = (key, value, expires)
        if namespace == "wallpaper":
            if wallpaper_record is None or (expires or 0, CacheStore._key(key)) > (
                    wallpaper_record[2] or 0, CacheStore._key(wallpaper_record[0])):
                wallpaper_record = record
            continue
        if expires is not None and expires <= time.time():
            continue
        batch.append((CacheStore._key(key), record))
        if len(batch) == 500:
            valid = [item for item in batch if item[1][2] is None or item[1][2] > time.time()]
            if valid:
                cache.set_many(valid)
                saved_count += len(valid)
            batch.clear()
    if namespace == "wallpaper" and wallpaper_record is not None:
        if wallpaper_record[2] is None or wallpaper_record[2] > time.time():
            cache.set_many([(CacheStore._key(wallpaper_record[0]), wallpaper_record)])
            saved_count = 1
    elif batch:
        valid = [item for item in batch if item[1][2] is None or item[1][2] > time.time()]
        if valid:
            cache.set_many(valid)
            saved_count += len(valid)
    limited = max(0, source_count - 1) if namespace == "wallpaper" else 0
    return source_count, saved_count, limited


def _snapshot_records(path, namespace):
    return _SQLiteCache(path, namespace).records(strict=True)


def _remove_job_staging(job_id):
    shutil.rmtree(_snapshot_directory(job_id), ignore_errors=True)


def _publish_manifest_path(job_id):
    from config import Config
    return os.path.join(Config().get_config_path(), ".maintenance", f"cache-publish-{job_id}.json")


def _write_publish_manifest(manifest):
    path = _publish_manifest_path(manifest["job_id"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return path


def _publish_plan(job_id, source_path):
    from config import Config
    target_path = os.path.join(Config().get_config_path(), "cache", "system.sqlite3")
    return {"job_id": job_id, "source_path": source_path, "target_path": target_path,
            "backup_path": os.path.join(_snapshot_directory(job_id), "previous-system.sqlite3"),
            "target_existed": os.path.exists(target_path), "phase": "PREPARED"}


def _checkpoint_sqlite(path):
    if not os.path.exists(path):
        return
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=10000")
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result and result[0]:
            raise sqlite3.OperationalError("缓存 SQLite WAL 检查点未完成")
    finally:
        connection.close()


def _fsync_directory(path):
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _publish_sqlite_target(manifest):
    source_path = manifest["source_path"]
    target_path = manifest["target_path"]
    backup_path = manifest["backup_path"]
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    _write_publish_manifest(manifest)
    if manifest["target_existed"] and os.path.exists(target_path):
        _checkpoint_sqlite(target_path)
        os.replace(target_path, backup_path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(target_path + suffix):
                os.replace(target_path + suffix, backup_path + suffix)
    manifest["phase"] = "BACKED_UP"
    _write_publish_manifest(manifest)
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(source_path + suffix)
        except FileNotFoundError:
            pass
    os.replace(source_path, target_path)
    _fsync_directory(os.path.dirname(target_path))
    manifest["phase"] = "PUBLISHED"
    _write_publish_manifest(manifest)
    return manifest


def _read_publish_manifest(job_id, result=None):
    path = _publish_manifest_path(job_id)
    try:
        with open(path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        if isinstance(manifest, dict) and manifest.get("job_id") == job_id:
            return manifest
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        raise RuntimeError("缓存 SQLite 发布日志损坏，保持维护状态")
    manifest = (result or {}).get("target_publish")
    return manifest if isinstance(manifest, dict) else None


def _rollback_sqlite_target(job_id, result=None):
    manifest = _read_publish_manifest(job_id, result)
    if not manifest:
        return
    source_path = manifest["source_path"]
    target_path = manifest["target_path"]
    backup_path = manifest["backup_path"]
    source_exists = os.path.exists(source_path)
    backup_exists = os.path.exists(backup_path)
    published = not source_exists and os.path.exists(target_path)
    if manifest.get("target_existed") and not backup_exists:
        if published or not os.path.exists(target_path):
            raise RuntimeError("旧缓存 SQLite 备份缺失，无法安全回滚")
    if published:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(target_path + suffix)
            except FileNotFoundError:
                pass
    if backup_exists:
        os.replace(backup_path, target_path)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(backup_path + suffix):
                os.replace(backup_path + suffix, target_path + suffix)
    _fsync_directory(os.path.dirname(target_path))
    try:
        os.unlink(_publish_manifest_path(job_id))
    except FileNotFoundError:
        pass
    try:
        os.unlink(_publish_manifest_path(job_id) + ".tmp")
    except FileNotFoundError:
        pass


def _commit_sqlite_target(job_id):
    manifest = _read_publish_manifest(job_id)
    if not manifest:
        return
    backup_path = manifest["backup_path"]
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(backup_path + suffix)
        except FileNotFoundError:
            pass
    try:
        os.unlink(_publish_manifest_path(job_id))
    except FileNotFoundError:
        pass
    try:
        os.unlink(_publish_manifest_path(job_id) + ".tmp")
    except FileNotFoundError:
        pass


def _record_digest(records, now=None):
    now = time.time() if now is None else now
    count = 0
    xor_digest = 0
    sum_digest = 0
    modulus = 1 << 256
    for key, value, expires in records:
        if expires is not None and expires <= now:
            continue
        item_digest = int.from_bytes(
            hashlib.sha256(pickle.dumps((key, value, expires), protocol=4)).digest(), "big")
        xor_digest ^= item_digest
        sum_digest = (sum_digest + item_digest) % modulus
        count += 1
    combined = count.to_bytes(8, "big") + xor_digest.to_bytes(32, "big") + sum_digest.to_bytes(32, "big")
    return count, hashlib.sha256(combined).hexdigest()


def _iter_limited_records(namespace, records):
    if namespace != "wallpaper":
        yield from records
        return
    best = None
    for record in records:
        if best is None or (record[2] or 0, CacheStore._key(record[0])) > (
                best[2] or 0, CacheStore._key(best[0])):
            best = record
    if best is not None:
        yield best


def _ensure_storage_idle(except_job_id=None):
    for job in STORAGE_JOBS.active():
        if job.get("job_id") == except_job_id or job.get("status") in {"CHECKING", "READY"}:
            continue
        raise ValueError("已有缓存或数据库维护任务正在运行，请等待任务结束")
    for kind, gate in STORAGE_JOBS.maintenance_gates().items():
        if kind == "cache" and gate.get("job_id") == except_job_id:
            continue
        raise ValueError("已有存储维护闸门未解除，请先完成或恢复对应任务")


def _source_summary(except_job_id=None):
    _ensure_storage_idle(except_job_id)
    initial_status = STORE.status()
    if initial_status.get("fallback"):
        raise ValueError("当前缓存后端处于降级状态，无法确认完整源数据")
    if initial_status.get("restart_required"):
        raise ValueError("已有缓存后端配置等待重启，请先完成或恢复该次切换")
    if initial_status.get("maintenance") and not STORAGE_JOBS.owns_exclusive_lock():
        raise ValueError("已有缓存维护任务正在运行，请等待任务结束")
    if (initial_status.get("active_backend") == "memory"
            and not _memory_source_is_single_process()):
        raise ValueError("无法确认内存缓存是否分布在多个 worker，不能保证完整迁移")
    namespaces = {}
    for namespace in CACHE_NAMESPACE_REGISTRY:
        namespaces[namespace] = STORE.memory_info(namespace).get("entries", 0)
    # memory_disk 在首次访问命名空间时可能创建 SQLite 结构，并更新本地 revision。
    # 采样 revision 必须放在统计后，否则迁移会把这次只读检查自身误判为源变化。
    status = STORE.status()
    if status.get("fallback"):
        raise ValueError("当前缓存后端处于降级状态，无法确认完整源数据")
    if status.get("restart_required"):
        raise ValueError("已有缓存后端配置等待重启，请先完成或恢复该次切换")
    if status.get("maintenance") and not STORAGE_JOBS.owns_exclusive_lock():
        raise ValueError("已有缓存维护任务正在运行，请等待任务结束")
    if (status.get("active_backend") != initial_status.get("active_backend")
            or status.get("config_generation") != initial_status.get("config_generation")):
        raise ValueError("读取缓存统计期间源缓存后端发生变化，不能确认完整源数据")
    if status.get("active_backend") == "memory" and not _memory_source_is_single_process():
        raise ValueError("无法确认内存缓存是否分布在多个 worker，不能保证完整迁移")
    return status, namespaces


def _memory_source_is_single_process():
    """多进程内存缓存互不共享；Gunicorn worker 数未知或大于一时拒绝全量迁移。"""
    if "gunicorn" not in os.path.basename(sys.argv[0]).lower():
        return True
    try:
        configured = int(os.environ.get("WEB_CONCURRENCY", ""))
        return configured == 1
    except ValueError:
        pass
    try:
        with open(f"/proc/{os.getppid()}/cmdline", "rb") as stream:
            arguments = [item.decode("utf-8", "ignore")
                         for item in stream.read().split(b"\0") if item]
    except OSError:
        arguments = sys.argv
    for index, argument in enumerate(arguments):
        if argument in {"-w", "--workers"} and index + 1 < len(arguments):
            try:
                return int(arguments[index + 1]) == 1
            except ValueError:
                return False
        if argument.startswith("--workers="):
            try:
                return int(argument.split("=", 1)[1]) == 1
            except ValueError:
                return False
    return False


def _probe(candidate, path=None):
    target = CacheStore(path=path, settings=candidate)
    namespace = f"__storage_check__:{os.urandom(8).hex()}"
    key = os.urandom(16).hex()
    value = os.urandom(24)
    try:
        status = target.status()
        if status.get("fallback"):
            raise ValueError(status.get("reason") or "目标缓存后端连接失败")
        expires = time.time() + 60
        target.put(namespace, key, value, expires)
        record = target.get(namespace, key)
        if record is None or record[1] != value or record[2] != expires:
            raise ValueError("目标缓存读写校验失败")
        if candidate["backend"] in {"redis", "valkey"}:
            remote_key = target._prefix(namespace) + target._key(key)
            if target._client.ttl(remote_key) <= 0:
                raise ValueError("目标缓存不支持带有效期写入")
        if not any(item[0] == key for item in target.load(namespace)):
            raise ValueError("目标缓存扫描校验失败")
        if not target.delete(namespace, key):
            raise ValueError("目标缓存删除校验失败")
        target.clear(namespace)
        return {"backend": candidate["backend"], "write": True, "read": True,
                "delete": True, "scan": True}
    finally:
        target.close()


def check_cache_storage(values):
    """检查候选后端和目标范围；检查记录中不保存连接密码。"""
    try:
        candidate = _candidate(values)
        source_status, source_counts = _source_summary()
        fingerprint = _fingerprint(candidate)
        job, created = STORAGE_JOBS.create(
            "cache_migration", {"fingerprint": fingerprint, "candidate": _safe_candidate(candidate)},
            idempotency_key=fingerprint)
        if not created and job.get("status") == "READY":
            if time.time() - float(job.get("created_at") or 0) > _CHECK_TTL_SECONDS:
                STORAGE_JOBS.update(job["job_id"], status="CANCELLED", stage="EXPIRED",
                                    message="缓存目标检查已过期，请重新检查",
                                    expected_status="READY")
                job, created = STORAGE_JOBS.create(
                    "cache_migration",
                    {"fingerprint": fingerprint, "candidate": _safe_candidate(candidate)},
                    idempotency_key=fingerprint)
            else:
                return {"check_id": job["job_id"], "unchanged": job.get("result", {}).get("unchanged", False),
                        "same_store": job.get("result", {}).get("same_store", False),
                        "source": job.get("result", {}).get("source"), "target": job.get("result", {}).get("target"),
                        "summary": job.get("result", {}).get("summary", []),
                        "excluded": _EXCLUDED_CACHES}
        if not created and job.get("status") not in TERMINAL_STATES:
            return {"code": 1, "msg": "相同缓存目标的检查任务正在运行"}
        try:
            same_store = _same_store(STORE._settings, candidate)
            unchanged = _same_configuration(STORE._settings, candidate)
            probe_path = (_target_cache_directory(job["job_id"])
                          if candidate["backend"] == "memory_disk" and not same_store else None)
            try:
                target_status = _probe(candidate, path=probe_path)
                target_counts = {}
                if not same_store and not unchanged:
                    target_settings = dict(candidate)
                    if candidate["backend"] in {"redis", "valkey"}:
                        target_settings["storage_generation"] = job["job_id"]
                    target_store = CacheStore(path=probe_path, settings=target_settings)
                    try:
                        for namespace in CACHE_NAMESPACE_REGISTRY:
                            target_counts[namespace] = target_store.memory_info(namespace).get(
                                "entries", 0)
                    finally:
                        target_store.close()
                    if any(target_counts.values()):
                        raise ValueError("目标缓存已有应用数据；请先使用空目标或清理目标缓存")
            finally:
                if probe_path:
                    shutil.rmtree(_snapshot_directory(job["job_id"]), ignore_errors=True)
            summary = [{"namespace": name, "entries": count}
                       for name, count in source_counts.items() if count]
            result = {"unchanged": unchanged, "same_store": same_store, "source": source_status,
                      "target": target_status, "summary": summary,
                      "source_counts": source_counts}
            STORAGE_JOBS.update(job["job_id"], status="READY", stage="READY",
                                payload={"fingerprint": fingerprint,
                                         "candidate": _safe_candidate(candidate),
                                         "same_store": same_store,
                                         "source_counts": source_counts},
                                result=result, message="目标检查完成", progress=1)
            return {"check_id": job["job_id"], "unchanged": unchanged, "same_store": same_store,
                    "source": source_status, "target": target_status, "summary": summary,
                    "excluded": _EXCLUDED_CACHES}
        except Exception as error:
            STORAGE_JOBS.update(job["job_id"], status="FAILED", stage="CHECK_FAILED",
                                error=_safe_error(error, candidate), message="目标检查失败")
            raise
    except Exception as error:
        return {"code": 1, "msg": _safe_error(error)}


def start_cache_migration(values):
    try:
        candidate = _candidate(values)
        check_id = (values or {}).get("check_id")
        job = STORAGE_JOBS.get(check_id) if check_id else None
        if not job or job.get("kind") != "cache_migration" or job.get("status") != "READY":
            raise ValueError("缓存目标检查已失效，请重新检查")
        if time.time() - float(job.get("created_at") or 0) > _CHECK_TTL_SECONDS:
            STORAGE_JOBS.update(check_id, status="CANCELLED", stage="EXPIRED",
                                message="缓存目标检查已过期，请重新检查",
                                expected_status="READY")
            raise ValueError("缓存目标检查已过期，请重新检查")
        if not hmac.compare_digest(job["payload"].get("fingerprint", ""), _fingerprint(candidate)):
            raise ValueError("缓存连接参数已变化，请重新检查目标")
        if (job.get("result") or {}).get("unchanged"):
            completed = STORAGE_JOBS.update(check_id, status="SUCCEEDED", stage="SUCCEEDED",
                                            message="缓存保存方式没有变化，无需迁移", progress=1,
                                            expected_status="READY")
            if not completed:
                raise ValueError("缓存迁移任务已经启动或状态已变化")
            return {"code": 0, "job_id": check_id, "job": completed}
        claimed = STORAGE_JOBS.update(check_id, status="QUIESCING", stage="QUIESCING",
                                      message="等待缓存写入进入安全边界", expected_status="READY")
        if not claimed:
            raise ValueError("缓存迁移任务已经启动或状态已变化")
        if not _same_store(STORE._settings, candidate) and candidate["backend"] in {"redis", "valkey"}:
            candidate["storage_generation"] = check_id
        thread = threading.Thread(target=_run_migration, args=(check_id, candidate),
                                  name=f"cache-migration-{check_id[:8]}", daemon=True)
        with _WORKERS_LOCK:
            _WORKERS[check_id] = thread
        thread.start()
        return {"code": 0, "job_id": check_id, "job": STORAGE_JOBS.get(check_id)}
    except Exception as error:
        return {"code": 1, "msg": _safe_error(error, candidate if "candidate" in locals() else None)}


def _run_migration(job_id, candidate):
    from config import CONFIG_LOCK, Config

    same_store = _same_store(STORE._settings, candidate)
    staged_sqlite = candidate.get("backend") == "memory_disk" and not same_store
    target_path = _target_cache_directory(job_id) if staged_sqlite else None
    target = CacheStore(path=target_path, settings=candidate)
    try:
        with STORAGE_JOBS.maintenance_lock(exclusive=True), STORE._lock:
            _ensure_storage_idle(job_id)
            STORAGE_JOBS.set_maintenance_gate("cache", job_id, "QUIESCING")
            STORE.freeze()
            if STORAGE_JOBS.cancel_requested(job_id):
                STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                    message="迁移开始前已取消")
                return
            source_status, source_counts = _source_summary(job_id)
            checked_job = STORAGE_JOBS.get(job_id) or {}
            checked_source = ((checked_job.get("result") or {}).get("source") or {})
            if any(source_status.get(key) != checked_source.get(key)
                   for key in ("active_backend", "generation", "config_generation")):
                raise ValueError("源缓存代次已变化，请重新检查目标")
            _probe(candidate, path=target_path)
            if not same_store:
                for namespace in CACHE_NAMESPACE_REGISTRY:
                    if target.memory_info(namespace).get("entries", 0):
                        raise ValueError("目标缓存检查后出现新数据，请重新检查目标")
            STORAGE_JOBS.update(job_id, status="SNAPSHOTTING", stage="SNAPSHOTTING",
                                message="正在冻结并读取缓存快照", progress=0)
            snapshot_path = _snapshot_path(job_id)
            snapshot_counts = {}
            wallpaper_entries_limited = 0
            for index, namespace in enumerate(CACHE_NAMESPACE_REGISTRY, start=1):
                if STORAGE_JOBS.cancel_requested(job_id):
                    STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                        message="在缓存快照边界取消")
                    return
                _source_count, snapshot_count, limited = _write_snapshot_namespace(
                    snapshot_path, namespace, STORE.iter_records(namespace, strict=True))
                snapshot_counts[namespace] = snapshot_count
                wallpaper_entries_limited += limited
                STORAGE_JOBS.update(job_id, stage="SNAPSHOTTING",
                                    message=f"已读取 {index}/{len(CACHE_NAMESPACE_REGISTRY)} 个缓存命名空间",
                                    progress=index / len(CACHE_NAMESPACE_REGISTRY) * 0.2)
            snapshot_status = STORE.status()
            if (snapshot_status.get("fallback")
                    or any(snapshot_status.get(key) != source_status.get(key)
                           for key in ("active_backend", "generation", "config_generation"))):
                raise ValueError("读取快照期间源缓存后端发生变化，不能确认完整源数据")
            _write_snapshot_manifest(job_id, snapshot_path, source_status)
            total = sum(snapshot_counts.values())
            STORAGE_JOBS.update(job_id, stage="SNAPSHOTTING",
                                result={"source": source_status, "snapshot_path": snapshot_path},
                                message=f"缓存快照已写入临时文件，共 {total} 条有效记录",
                                progress=0.2)
            if not same_store:
                STORAGE_JOBS.update(job_id, status="MIGRATING", stage="MIGRATING",
                                    message="正在分批写入目标缓存", progress=0.2)
                for index, namespace in enumerate(CACHE_NAMESPACE_REGISTRY, start=1):
                    if STORAGE_JOBS.cancel_requested(job_id):
                        STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                            message="在缓存命名空间边界取消")
                        return
                    batch = []
                    for record in _snapshot_records(snapshot_path, namespace):
                        if record[2] is None or record[2] > time.time():
                            batch.append(record)
                        if len(batch) == 500:
                            target.put_many(namespace, batch)
                            batch.clear()
                    if batch:
                        target.put_many(namespace, batch)
                    STORAGE_JOBS.update(job_id, stage="MIGRATING",
                                        message=f"已写入 {index}/{len(CACHE_NAMESPACE_REGISTRY)} 个缓存命名空间",
                                        progress=0.2 + index / len(CACHE_NAMESPACE_REGISTRY) * 0.55)
            STORAGE_JOBS.update(job_id, status="VALIDATING", stage="VALIDATING",
                                message="正在逐命名空间校验缓存", progress=0.78)
            expected = {}
            for namespace in CACHE_NAMESPACE_REGISTRY:
                validation_time = time.time()
                expected[namespace] = _record_digest(
                    _snapshot_records(snapshot_path, namespace), validation_time)
                actual = _record_digest(_iter_limited_records(
                    namespace, target.iter_records(namespace, strict=False)), validation_time)
                if actual != expected[namespace]:
                    # Give entries expiring during target validation the same boundary.
                    validation_time = time.time()
                    expected[namespace] = _record_digest(
                        _snapshot_records(snapshot_path, namespace), validation_time)
                    actual = _record_digest(_iter_limited_records(
                        namespace, target.iter_records(namespace, strict=False)), validation_time)
                if actual != expected[namespace]:
                    raise ValueError(f"缓存命名空间 {namespace} 的目标校验失败")
            target_status = target.status()
            if target_status.get("fallback") or target_status.get("active_backend") != candidate["backend"]:
                raise ValueError("目标缓存迁移期间发生连接降级，不能保存配置")
            result = {"fingerprint": _fingerprint(candidate), "expected": expected,
                      "source_backend": source_status.get("active_backend"),
                      "target_backend": candidate["backend"], "same_store": same_store,
                      "entries": sum(count for count, _ in expected.values()),
                      "snapshot_counts": snapshot_counts,
                      "expired_during_migration": sum(
                          max(0, source_counts.get(namespace, 0)
                              - expected.get(namespace, (0, ""))[0]
                              - (wallpaper_entries_limited if namespace == "wallpaper" else 0))
                          for namespace in CACHE_NAMESPACE_REGISTRY),
                      "wallpaper_entries_limited": wallpaper_entries_limited,
                      "snapshot_path": snapshot_path}
            STORAGE_JOBS.update(job_id, status="VALIDATED", stage="VALIDATED", result=result,
                                message="缓存数据校验完成", progress=0.95)
            if staged_sqlite:
                target.checkpoint()
                for suffix in ("-wal", "-shm"):
                    try:
                        os.unlink(os.path.join(target_path, "system.sqlite3") + suffix)
                    except FileNotFoundError:
                        pass
            if STORAGE_JOBS.cancel_requested(job_id):
                STORAGE_JOBS.update(job_id, status="CANCELLED", stage="CANCELLED",
                                    message="配置保存前已取消", progress=0.95)
                return
            if staged_sqlite:
                result["target_publish"] = _publish_plan(
                    job_id, os.path.join(target_path, "system.sqlite3"))
            STORAGE_JOBS.update(job_id, status="SAVING_CONFIG", stage="SAVING_CONFIG",
                                result=result, message="正在发布并原子保存缓存配置", progress=0.97)
            if staged_sqlite:
                result["target_publish"] = _publish_sqlite_target(result["target_publish"])
                STORAGE_JOBS.update(job_id, status="SAVING_CONFIG", stage="SAVING_CONFIG",
                                    result=result, message="缓存 SQLite 已发布，正在保存配置",
                                    progress=0.985)
            with CONFIG_LOCK:
                config = copy.deepcopy(Config().get_config())
                saved_cache = dict(config.get("cache") or {})
                saved_cache.update(candidate)
                config["cache"] = saved_cache
                Config().save_config(config)
            STORE.freeze()
            STORAGE_JOBS.update(job_id, status="RESTART_PENDING", stage="RESTART_PENDING",
                                result=result, message="配置已保存，等待受控重启", progress=0.99)
            _schedule_restart()
    except Exception as error:
        current = STORAGE_JOBS.get(job_id)
        if current and current.get("status") == "SAVING_CONFIG":
            saved_result = current.get("result") or {}
            try:
                saved_candidate = _candidate(Config().get_config("cache") or {})
                config_committed = hmac.compare_digest(
                    saved_result.get("fingerprint", ""), _fingerprint(saved_candidate))
            except Exception:
                config_committed = False
            if config_committed:
                STORE.freeze()
                STORAGE_JOBS.update(job_id, status="RESTART_PENDING", stage="RESTART_PENDING",
                                    message="配置已提交，恢复受控重启", progress=0.99)
                _schedule_restart()
            else:
                try:
                    _rollback_sqlite_target(job_id, saved_result)
                except Exception as rollback_error:
                    STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED",
                                        stage="RECOVERY_REQUIRED",
                                        error=(f"{_safe_error(error, candidate)}；SQLite 回滚失败："
                                               f"{_safe_error(rollback_error, candidate)}"),
                                        message="缓存目标回滚未完成，系统保持维护状态")
                else:
                    STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                        error=_safe_error(error, candidate),
                                        message="配置保存失败；活动配置保持原值")
        current = STORAGE_JOBS.get(job_id)
        if current and current.get("status") not in TERMINAL_STATES | {"RESTART_PENDING", "RECOVERY_REQUIRED"}:
            STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED",
                                error=_safe_error(error, candidate),
                                message="缓存迁移失败；活动配置保持原值")
    finally:
        current = STORAGE_JOBS.get(job_id)
        if current and current.get("status") in TERMINAL_STATES:
            _remove_job_staging(job_id)
            with STORAGE_JOBS.maintenance_lock(exclusive=True):
                if (current.get("status") != "SUCCEEDED" and not same_store
                        and candidate.get("backend") in {"redis", "valkey"}):
                    try:
                        target_status = target.status()
                        if target_status.get("active_backend") == candidate["backend"]:
                            for namespace in CACHE_NAMESPACE_REGISTRY:
                                target.clear(namespace)
                        else:
                            logger.warning("失败缓存任务的远程隔离代次暂时不可用：%s", job_id)
                    except Exception:
                        logger.warning("无法立即清理失败缓存任务的远程隔离代次：%s", job_id)
                gate = STORAGE_JOBS.maintenance_gate("cache")
                if gate and gate.get("job_id") == job_id:
                    STORAGE_JOBS.clear_maintenance_gate("cache", job_id)
                if not STORAGE_JOBS.maintenance_gate("cache"):
                    STORE.unfreeze()
        target.close()
        with _WORKERS_LOCK:
            _WORKERS.pop(job_id, None)


def _schedule_restart():
    return schedule_restart()


def _restart_process():
    from app.utils.storage_jobs import restart_process

    return restart_process()


def get_cache_job(job_id):
    return STORAGE_JOBS.get(job_id)


def cancel_cache_job(job_id):
    return STORAGE_JOBS.request_cancel(job_id)


def retry_cache_restart(job_id):
    job = STORAGE_JOBS.get(job_id)
    if not job or job.get("kind") != "cache_migration" or job.get("status") != "RESTART_PENDING":
        return False
    _schedule_restart()
    return True


def _job_source_backend(job):
    result = job.get("result") or {}
    source = result.get("source")
    if isinstance(source, dict):
        return source.get("active_backend"), source
    return result.get("source_backend"), None


def _restore_interrupted_memory_snapshot(job):
    result = job.get("result") or {}
    expected_path = os.path.abspath(os.path.join(_snapshot_directory(job["job_id"]),
                                                  "snapshot.sqlite3"))
    snapshot_path = result.get("snapshot_path") or expected_path
    if os.path.abspath(snapshot_path) != expected_path:
        raise ValueError("缓存快照路径与任务不匹配")
    source_backend, source_status = _job_source_backend(job)
    if source_backend != "memory":
        raise ValueError("缓存迁移源后端信息缺失，无法安全恢复")
    _validate_snapshot_manifest(job["job_id"], expected_path,
                                source_status or {"active_backend": source_backend})

    current = STORE.status()
    if (current.get("fallback") or current.get("active_backend") != "memory"
            or current.get("configured_backend") != "memory"):
        raise ValueError("当前缓存配置已变化，无法恢复原内存缓存")

    # Startup runs behind the persistent maintenance gate, so no business writes can
    # race this exact replacement of the process-local cache contents.
    for namespace in CACHE_NAMESPACE_REGISTRY:
        STORE.clear(namespace)
    for namespace in CACHE_NAMESPACE_REGISTRY:
        batch = []
        for key, value, expires in _snapshot_records(expected_path, namespace):
            if expires is not None and expires <= time.time():
                continue
            batch.append((key, value, expires))
            if len(batch) == 500:
                STORE.put_many(namespace, batch)
                batch.clear()
        if batch:
            STORE.put_many(namespace, batch)

    for namespace in CACHE_NAMESPACE_REGISTRY:
        validation_time = time.time()
        expected = _record_digest(_snapshot_records(expected_path, namespace), validation_time)
        actual = _record_digest(STORE.iter_records(namespace, strict=True), validation_time)
        if actual != expected:
            validation_time = time.time()
            expected = _record_digest(_snapshot_records(expected_path, namespace), validation_time)
            actual = _record_digest(STORE.iter_records(namespace, strict=True), validation_time)
        if actual != expected:
            raise ValueError(f"恢复后的内存缓存校验失败：{namespace}")


def _keep_cache_job_in_recovery(job_id, error, message):
    gate = STORAGE_JOBS.maintenance_gate("cache")
    if gate is None:
        STORAGE_JOBS.set_maintenance_gate("cache", job_id, "RECOVERY_REQUIRED")
    elif gate.get("job_id") != job_id:
        raise RuntimeError("缓存维护闸门属于另一项任务，保持维护状态")
    else:
        STORAGE_JOBS.set_maintenance_gate("cache", job_id, "RECOVERY_REQUIRED")
    STORE.freeze()
    STORAGE_JOBS.update(job_id, status="RECOVERY_REQUIRED", stage="RECOVERY_REQUIRED",
                        error=_safe_error(error), message=message)


def _finish_uncommitted_cache_job(job, terminal_status, message):
    job_id = job["job_id"]
    try:
        _rollback_sqlite_target(job_id, job.get("result") or {})
        source_backend, _source_status = _job_source_backend(job)
        if source_backend == "memory":
            _restore_interrupted_memory_snapshot(job)
            message = "配置提交前进程中断；已从完整快照恢复原内存缓存"
        elif source_backend not in {"memory_disk", "redis", "valkey"}:
            raise ValueError("无法确认缓存迁移源后端，拒绝清理恢复数据")
    except Exception as error:
        _keep_cache_job_in_recovery(job_id, error,
                                    "缓存迁移源数据无法安全恢复，系统保持维护状态")
        raise RuntimeError("缓存迁移恢复失败，系统保持维护状态") from error
    STORAGE_JOBS.update(job_id, status=terminal_status, stage=terminal_status,
                        message=message)
    _remove_job_staging(job_id)
    STORAGE_JOBS.clear_maintenance_gate("cache", job_id)
    STORE.unfreeze()


def recover_cache_migration(job_id):
    job = STORAGE_JOBS.get(job_id)
    if not job or job.get("kind") != "cache_migration" or job.get("status") != "RECOVERY_REQUIRED":
        return False
    try:
        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            job = STORAGE_JOBS.get(job_id)
            if not job or job.get("status") != "RECOVERY_REQUIRED":
                return False
            source_backend, _source_status = _job_source_backend(job)
            if source_backend == "memory":
                _rollback_sqlite_target(job_id, job.get("result") or {})
                _restore_interrupted_memory_snapshot(job)
                STORAGE_JOBS.update(job_id, status="INTERRUPTED", stage="INTERRUPTED", error=None,
                                    message="已从完整快照恢复原内存缓存，迁移任务中断")
                _remove_job_staging(job_id)
                STORAGE_JOBS.clear_maintenance_gate("cache", job_id)
                STORE.unfreeze()
                return True
            from config import Config
            current = _candidate(Config().get_config("cache") or {})
            expected = (job.get("result") or {}).get("fingerprint")
            if expected and hmac.compare_digest(expected, _fingerprint(current)):
                return False
            _rollback_sqlite_target(job_id, job.get("result") or {})
            STORAGE_JOBS.update(job_id, status="FAILED", stage="FAILED", error=None,
                                message="缓存 SQLite 已恢复，活动配置保持原值")
            _remove_job_staging(job_id)
            STORAGE_JOBS.clear_maintenance_gate("cache", job_id)
            STORE.unfreeze()
        return True
    except Exception:
        return False


def verify_cache_startup():
    """新进程确认配置和缓存快照匹配后才开放后台任务。"""
    from config import Config

    with STORAGE_JOBS.maintenance_lock(exclusive=True):
        for job in STORAGE_JOBS.active("cache_migration"):
            if job.get("status") == "RECOVERY_REQUIRED":
                raise RuntimeError("缓存 SQLite 回滚需要人工恢复；系统保持维护状态")
            if job.get("status") in {"SAVING_CONFIG", "RESTART_PENDING", "VERIFYING_STARTUP"}:
                gate = STORAGE_JOBS.maintenance_gate("cache")
                if gate is None:
                    STORAGE_JOBS.set_maintenance_gate("cache", job["job_id"], job["status"])
                elif gate.get("job_id") != job["job_id"]:
                    raise RuntimeError("缓存维护闸门属于另一项任务，保持维护状态")
            if job.get("status") not in {"SAVING_CONFIG", "RESTART_PENDING", "VERIFYING_STARTUP"}:
                _finish_uncommitted_cache_job(
                    job, "INTERRUPTED", "配置提交前进程中断；活动配置未确认变化")
                continue
            expected_fingerprint = (job.get("result") or {}).get("fingerprint")
            current = _candidate(Config().get_config("cache") or {})
            matches = expected_fingerprint and hmac.compare_digest(expected_fingerprint,
                                                                   _fingerprint(current))
            if not matches and job.get("status") == "SAVING_CONFIG":
                _finish_uncommitted_cache_job(
                    job, "FAILED", "配置保存前进程中断，活动配置保持原值")
                continue
            if not matches:
                raise RuntimeError("缓存迁移配置与待重启任务不匹配，保持维护状态")
            STORAGE_JOBS.update(job["job_id"], status="VERIFYING_STARTUP", stage="VERIFYING_STARTUP",
                                message="新进程正在确认缓存目标")
            result = job.get("result") or {}
            expected_snapshot_path = os.path.abspath(os.path.join(
                _snapshot_directory(job["job_id"]), "snapshot.sqlite3"))
            snapshot_path = result.get("snapshot_path") or expected_snapshot_path
            if (os.path.abspath(snapshot_path) != expected_snapshot_path
                    or not os.path.isfile(snapshot_path)):
                raise RuntimeError("缓存迁移快照缺失，保持维护状态")
            source_backend, source_status = _job_source_backend(job)
            if source_backend not in {"memory", "memory_disk", "redis", "valkey"}:
                raise RuntimeError("缓存迁移源后端信息缺失，保持维护状态")
            try:
                _validate_snapshot_manifest(
                    job["job_id"], snapshot_path,
                    source_status or {"active_backend": source_backend})
            except Exception as error:
                _keep_cache_job_in_recovery(job["job_id"], error,
                                            "缓存迁移快照完整性无法确认，系统保持维护状态")
                raise RuntimeError("缓存迁移快照校验失败，系统保持维护状态") from error
            if current["backend"] == "memory":
                for namespace in CACHE_NAMESPACE_REGISTRY:
                    batch = []
                    for key, value, expires in _snapshot_records(snapshot_path, namespace):
                        if expires is not None and expires <= time.time():
                            continue
                        batch.append((key, value, expires))
                        if len(batch) == 500:
                            STORE.put_many(namespace, batch)
                            batch.clear()
                    if batch:
                        STORE.put_many(namespace, batch)
            for namespace in CACHE_NAMESPACE_REGISTRY:
                validation_time = time.time()
                count, digest = _record_digest(
                    _snapshot_records(snapshot_path, namespace), validation_time)
                actual_count, actual_digest = _record_digest(
                    STORE.iter_records(namespace, strict=True), validation_time)
                if (actual_count, actual_digest) != (count, digest):
                    validation_time = time.time()
                    count, digest = _record_digest(
                        _snapshot_records(snapshot_path, namespace), validation_time)
                    actual_count, actual_digest = _record_digest(
                        STORE.iter_records(namespace, strict=True), validation_time)
                if (actual_count, actual_digest) != (count, digest):
                    raise RuntimeError(f"缓存迁移启动校验失败：{namespace}")
            if current["backend"] != "memory_disk":
                local_path = os.path.join(STORE._directory(), "system.sqlite3")
                if STORE._is_current_sqlite(local_path):
                    STORE._archive_current_database(local_path)
            _commit_sqlite_target(job["job_id"])
            STORAGE_JOBS.update(job["job_id"], status="SUCCEEDED", stage="SUCCEEDED",
                                message="缓存目标已验证，缓存服务恢复", progress=1)
            _remove_job_staging(job["job_id"])
            STORAGE_JOBS.clear_maintenance_gate("cache", job["job_id"])
        gate = STORAGE_JOBS.maintenance_gate("cache")
        if gate:
            gated_job = STORAGE_JOBS.get(gate.get("job_id")) if gate.get("job_id") else None
            if gated_job and gated_job.get("status") in TERMINAL_STATES:
                STORAGE_JOBS.clear_maintenance_gate("cache", gate.get("job_id"))
            else:
                raise RuntimeError("缓存维护闸门没有可恢复任务，保持维护状态")
        STORE.unfreeze()
