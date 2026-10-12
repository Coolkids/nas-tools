"""缓存与数据库存储维护任务的持久状态。"""

import json
import os
import sqlite3
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


ACTIVE_STATES = {"CHECKING", "READY", "QUIESCING", "SNAPSHOTTING", "MIGRATING",
                 "VALIDATING", "VALIDATED", "SAVING_CONFIG", "RESTART_PENDING",
                 "VERIFYING_STARTUP", "RECOVERY_REQUIRED", "PREPARING_TARGET",
                 "TARGET_BACKED_UP", "RETIRING_SQLITE", "OLD_SQLITE_REMOVED",
                 "EMPTY_TARGET_READY", "PUBLISHING", "VACUUMING", "EXPORTING",
                 "CHECKING_BACKUP", "BACKING_UP_CURRENT", "BUILDING_STAGE",
                 "RESTORING", "PUBLISHING_RESTORE", "CALIBRATING",
                 "VERIFYING_RESTORE", "COUNTING"}
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


def _stable_error_code(message):
    """Map implementation-specific failures to a stable public task category."""
    value = str(message or "").lower()
    if not value:
        return None
    if any(token in value for token in ("cancel", "已取消", "取消")):
        return "CANCELLED"
    if any(token in value for token in ("permission", "access denied", "权限不足", "无权")):
        return "PERMISSION_DENIED"
    if any(token in value for token in ("authentication", "password", "access denied for user",
                                        "认证", "口令", "密码")):
        return "AUTHENTICATION_FAILED"
    if any(token in value for token in ("disk full", "no space", "insufficient space",
                                        "空间不足", "磁盘已满")):
        return "INSUFFICIENT_SPACE"
    if any(token in value for token in ("lock", "busy", "deadlock", "锁冲突", "锁定")):
        return "LOCK_CONFLICT"
    if any(token in value for token in ("not supported", "unsupported", "不支持")):
        return "UNSUPPORTED"
    if any(token in value for token in ("connection", "connect", "timeout", "dns", "refused",
                                        "连接", "超时", "解析失败")):
        return "CONNECTION_FAILED"
    if any(token in value for token in ("checksum", "digest", "integrity", "校验", "摘要",
                                        "不一致", "损坏")):
        return "VALIDATION_FAILED"
    if any(token in value for token in ("zip", "manifest", "archive", "备份文件", "压缩包")):
        return "INVALID_BACKUP"
    if any(token in value for token in ("non-empty", "not empty", "already exists",
                                        "target conflict", "非空", "已存在", "冲突对象")):
        return "TARGET_CONFLICT"
    if any(token in value for token in ("configuration", "fingerprint", "config changed",
                                        "配置已变化", "配置已更改", "检查已过期")):
        return "CONFIG_CHANGED"
    return "OPERATION_FAILED"


class StorageJobStore:
    """在配置目录的独立 SQLite 中保存任务进度，不依赖业务数据库或缓存后端。"""

    def __init__(self, path=None):
        self.path = path
        self._thread_state = threading.local()

    def _database_path(self):
        if self.path:
            return self.path
        from config import Config
        return os.path.join(Config().get_config_path(), ".maintenance", "storage-jobs.sqlite3")

    def _maintenance_directory(self):
        return os.path.dirname(self._database_path())

    @contextmanager
    def maintenance_lock(self, exclusive=False, timeout=30):
        """跨进程读写维护锁；独占任务可在本线程内调用缓存接口。"""
        depth = getattr(self._thread_state, "depth", 0)
        if depth:
            if exclusive and not getattr(self._thread_state, "exclusive", False):
                raise RuntimeError("不能在共享维护锁中升级为独占锁")
            self._thread_state.depth = depth + 1
            try:
                yield
            finally:
                self._thread_state.depth -= 1
            return

        directory = self._maintenance_directory()
        os.makedirs(directory, exist_ok=True)
        lock_path = os.path.join(directory, "storage-maintenance.lock")
        stream = open(lock_path, "a+b")
        deadline = time.monotonic() + timeout
        exclusive_started_at = None
        try:
            while True:
                try:
                    if fcntl is not None:
                        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                        fcntl.flock(stream.fileno(), mode | fcntl.LOCK_NB)
                    elif msvcrt is not None:  # pragma: no cover - Windows
                        stream.seek(0)
                        if not stream.read(1):
                            stream.write(b"0")
                            stream.flush()
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - unsupported platform
                        raise OSError("平台不支持跨进程文件锁")
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("等待存储维护锁超时")
                    time.sleep(0.05)
            self._thread_state.depth = 1
            self._thread_state.exclusive = bool(exclusive)
            if exclusive:
                exclusive_started_at = time.perf_counter()
            try:
                yield
            finally:
                self._thread_state.depth = 0
                self._thread_state.exclusive = False
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                if exclusive_started_at is not None:
                    try:
                        from app.db.manager import DATABASE
                        DATABASE.record_exclusive_maintenance(
                            time.perf_counter() - exclusive_started_at)
                    except Exception:
                        # Diagnostics must not interfere with releasing a lock.
                        pass
        finally:
            stream.close()

    def owns_exclusive_lock(self):
        return bool(getattr(self._thread_state, "exclusive", False))

    def _gate_path(self):
        return os.path.join(self._maintenance_directory(), "storage-maintenance.json")

    def maintenance_gate(self, kind):
        """读取持久化维护闸门；损坏时按仍在维护处理。"""
        try:
            with open(self._gate_path(), "r", encoding="utf-8") as stream:
                gates = json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"kind": kind, "job_id": None, "phase": "UNKNOWN"}
        if not isinstance(gates, dict):
            return {"kind": kind, "job_id": None, "phase": "UNKNOWN"}
        return gates.get(kind)

    def maintenance_gates(self):
        """读取全部维护闸门；状态损坏时返回未知闸门并拒绝并行任务。"""
        try:
            with open(self._gate_path(), "r", encoding="utf-8") as stream:
                gates = json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {"unknown": {"job_id": None, "phase": "UNKNOWN"}}
        return gates if isinstance(gates, dict) else {
            "unknown": {"job_id": None, "phase": "UNKNOWN"}}

    def set_maintenance_gate(self, kind, job_id, phase):
        if not self.owns_exclusive_lock():
            raise RuntimeError("设置维护闸门需要独占维护锁")
        gates = {}
        try:
            with open(self._gate_path(), "r", encoding="utf-8") as stream:
                current = json.load(stream)
            if isinstance(current, dict):
                gates.update(current)
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            raise RuntimeError("维护闸门状态损坏，拒绝开始维护任务")
        gates[kind] = {"job_id": job_id, "phase": phase, "updated_at": time.time()}
        self._write_gates(gates)

    def clear_maintenance_gate(self, kind, job_id=None):
        if not self.owns_exclusive_lock():
            raise RuntimeError("解除维护闸门需要独占维护锁")
        path = self._gate_path()
        try:
            with open(path, "r", encoding="utf-8") as stream:
                gates = json.load(stream)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            raise RuntimeError("维护闸门状态损坏，无法安全解除")
        if not isinstance(gates, dict):
            raise RuntimeError("维护闸门状态损坏，无法安全解除")
        gate = gates.get(kind)
        if gate is None or (job_id is not None and gate.get("job_id") != job_id):
            return
        gates.pop(kind, None)
        if gates:
            self._write_gates(gates)
        else:
            os.unlink(path)

    def _write_gates(self, gates):
        path = self._gate_path()
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(gates, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)

    @contextmanager
    def _connection(self):
        path = self._database_path()
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS storage_jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                idempotency_key TEXT,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT,
                error TEXT,
                error_code TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(storage_jobs)")}
            if "error_code" not in columns:
                try:
                    connection.execute("ALTER TABLE storage_jobs ADD COLUMN error_code TEXT")
                except sqlite3.OperationalError:
                    columns = {row[1] for row in connection.execute(
                        "PRAGMA table_info(storage_jobs)")}
                    if "error_code" not in columns:
                        raise
            connection.execute("CREATE INDEX IF NOT EXISTS idx_storage_jobs_status "
                               "ON storage_jobs(status, updated_at)")
            connection.execute("""CREATE TABLE IF NOT EXISTS storage_job_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                stage TEXT NOT NULL,
                message TEXT NOT NULL,
                progress REAL,
                FOREIGN KEY(job_id) REFERENCES storage_jobs(job_id) ON DELETE CASCADE
            )""")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"] or "{}")
        result["result"] = json.loads(result["result"]) if result.get("result") else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        return result

    def create(self, kind, payload, idempotency_key=None):
        now = time.time()
        job_id = uuid.uuid4().hex
        encoded = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                row = connection.execute("""SELECT * FROM storage_jobs
                    WHERE kind=? AND idempotency_key=? AND status NOT IN (?, ?, ?, ?)
                    ORDER BY created_at DESC LIMIT 1""",
                                         (kind, idempotency_key, *sorted(TERMINAL_STATES))).fetchone()
                if row:
                    connection.commit()
                    return self._decode(row), False
            connection.execute("""INSERT INTO storage_jobs
                (job_id, kind, idempotency_key, status, stage, payload, created_at, updated_at)
                VALUES (?, ?, ?, 'CHECKING', 'CHECKING', ?, ?, ?)""",
                               (job_id, kind, idempotency_key, encoded, now, now))
            connection.execute("INSERT INTO storage_job_events(job_id, created_at, stage, message) "
                               "VALUES (?, ?, 'CHECKING', '任务已创建')", (job_id, now))
            row = connection.execute("SELECT * FROM storage_jobs WHERE job_id=?", (job_id,)).fetchone()
            connection.commit()
            return self._decode(row), True

    def get(self, job_id):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM storage_jobs WHERE job_id=?", (job_id,)).fetchone()
            job = self._decode(row)
            if job:
                job["events"] = [dict(event) for event in connection.execute(
                    "SELECT created_at, stage, message, progress FROM storage_job_events "
                    "WHERE job_id=? ORDER BY event_id", (job_id,))]
            return job

    def active(self, kind=None):
        placeholders = ",".join("?" for _ in ACTIVE_STATES)
        query = f"SELECT * FROM storage_jobs WHERE status IN ({placeholders})"
        values = list(sorted(ACTIVE_STATES))
        if kind:
            query += " AND kind=?"
            values.append(kind)
        query += " ORDER BY created_at DESC"
        with self._connection() as connection:
            return [self._decode(row) for row in connection.execute(query, values)]

    def latest(self, kind=None):
        query = "SELECT * FROM storage_jobs"
        values = []
        if kind:
            query += " WHERE kind=?"
            values.append(kind)
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self._connection() as connection:
            return self._decode(connection.execute(query, values).fetchone())

    def update(self, job_id, status=None, stage=None, payload=None, result=None,
               error=None, error_code=None, progress=None, message=None, expected_status=None):
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM storage_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                connection.rollback()
                return None
            if expected_status and row["status"] != expected_status:
                connection.rollback()
                return None
            next_stage = stage or row["stage"]
            next_error_code = error_code or (_stable_error_code(error) if error else None)
            connection.execute("""UPDATE storage_jobs SET status=?, stage=?, payload=?, result=?,
                error=?, error_code=?, updated_at=? WHERE job_id=?""",
                               (status or row["status"], next_stage,
                                json.dumps(payload, ensure_ascii=False, sort_keys=True)
                                if payload is not None else row["payload"],
                                json.dumps(result, ensure_ascii=False, sort_keys=True)
                                if result is not None else row["result"],
                                error, next_error_code, now, job_id))
            if message is not None:
                connection.execute("""INSERT INTO storage_job_events
                    (job_id, created_at, stage, message, progress) VALUES (?, ?, ?, ?, ?)""",
                                   (job_id, now, next_stage, str(message)[:1000], progress))
            updated = connection.execute("SELECT * FROM storage_jobs WHERE job_id=?", (job_id,)).fetchone()
            connection.commit()
            return self._decode(updated)

    def request_cancel(self, job_id):
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM storage_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["status"] in TERMINAL_STATES | {"SAVING_CONFIG", "RESTART_PENDING",
                                                                  "VERIFYING_STARTUP", "RECOVERY_REQUIRED",
                                                                  "RETIRING_SQLITE", "OLD_SQLITE_REMOVED",
                                                                  "EMPTY_TARGET_READY", "PUBLISHING",
                                                                  "VACUUMING", "RESTORING",
                                                                  "PUBLISHING_RESTORE", "CALIBRATING",
                                                                  "VERIFYING_RESTORE"}:
                connection.rollback()
                return False
            connection.execute("UPDATE storage_jobs SET cancel_requested=1, updated_at=? WHERE job_id=?",
                               (now, job_id))
            connection.execute("""INSERT INTO storage_job_events
                (job_id, created_at, stage, message) VALUES (?, ?, ?, '已请求取消')""",
                               (job_id, now, row["status"]))
            connection.commit()
            return True

    def cancel_requested(self, job_id):
        with self._connection() as connection:
            row = connection.execute("SELECT cancel_requested FROM storage_jobs WHERE job_id=?",
                                     (job_id,)).fetchone()
            return bool(row and row[0])

    def recover_interrupted(self, kind=None):
        """标记未完成且没有恢复器的任务，调用方可据阶段继续或安全失败。"""
        now = time.time()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in ACTIVE_STATES)
            values = [now, *sorted(ACTIVE_STATES)]
            query = f"UPDATE storage_jobs SET status='INTERRUPTED', stage='INTERRUPTED', updated_at=? " \
                f"WHERE status IN ({placeholders})"
            if kind:
                query += " AND kind=?"
                values.append(kind)
            connection.execute(query, values)
            connection.commit()


STORAGE_JOBS = StorageJobStore()


def restart_process():
    """Restart the current deployment through its existing process supervisor."""
    executable_name = os.path.basename(sys.argv[0]).lower()
    if "gunicorn" in executable_name:
        os.kill(os.getppid(), signal.SIGHUP)
        return
    if os.path.basename(sys.argv[0]) in {"run.py", "run"}:
        os.execv(sys.executable, [sys.executable, *sys.argv])
        return
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
        return
    os.kill(os.getpid(), signal.SIGTERM)


def schedule_restart(delay=1.5):
    """Schedule one controlled restart after the caller can persist and respond."""
    timer = threading.Timer(delay, restart_process)
    timer.start()
    return timer
