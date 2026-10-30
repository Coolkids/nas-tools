"""V3 保留策略、有界事件和独立于缓存的崩溃恢复。"""

from dataclasses import asdict
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock, Thread
import time
import uuid
import weakref

from app.media.recognition.budget import RecognitionBudget, total_timeout_ms
from app.media.recognition.outcome import admit_outcome
from app.media.recognition.repository import RecognitionRepository, utc_now
from app.media.recognition.settings import recognition_record_mode
from app.media.recognition.llm_usage import combine_usage


_active = set()
_lock = RLock()
_heartbeats, _heartbeat_thread = weakref.WeakValueDictionary(), None


def _after_fork():
    global _active, _heartbeats, _heartbeat_thread, _lock
    _active, _heartbeats, _heartbeat_thread, _lock = set(), weakref.WeakValueDictionary(), None, RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
FAILED = {"FAILED", "TIMEOUT", "REVIEW_REQUIRED"}
SENSITIVE = re.compile(r"(?i)(?:api[_-]?key|authorization|password|secret|token|client[_-]?secret|credential|access[_-]?token|refresh[_-]?token)")


def _heartbeat_loop():
    last_gc = time.monotonic()
    while True:
        time.sleep(1)
        with _lock:
            traces = list(_heartbeats.values())
        for trace in traces:
            with trace._mutex:
                if trace.finished_at is None:
                    trace._task("RUNNING", time.time()+30)
        if time.monotonic()-last_gc >= 60:
            try:
                RecognitionRepository().prune_tasks()
            except Exception:
                pass  # 仅用于重试运维垃圾回收，不会改变历史记录策略
            last_gc = time.monotonic()


def _register_heartbeat(trace):
    global _heartbeat_thread
    with _lock:
        _heartbeats[trace.request_id] = trace
        if _heartbeat_thread is None:
            _heartbeat_thread = Thread(target=_heartbeat_loop, daemon=True, name="recognition-task-heartbeat")
            _heartbeat_thread.start()


def _owner_alive(bundle):
    pid = bundle.get("owner_pid")
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def redact(value):
    if isinstance(value, dict):
        return {str(key): "[redacted]" if SENSITIVE.fullmatch(str(key)) else redact(item)
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [redacted]", value)
        value = re.sub(r"(?i)((?:api[_-]?key|password|token)=)[^&\s]+", r"\1[redacted]", value)
        value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", value)
        return value[:16384]
    if value is None or type(value) in (int, float, bool):
        return value
    raise ValueError("trace payload must contain JSON values")


def _generation():
    from app.media.recognition.records import _recognition_generation
    return _recognition_generation()


def _directory(root=None):
    if root is None:
        from config import Config
        root = Config().get_config_path()
    path = Path(root) / "recognition_v3_spool"
    if path.is_symlink():
        raise ValueError("spool directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_spool(bundle, root=None, max_bytes=2 * 1024 * 1024):
    wire = json.dumps(bundle, ensure_ascii=False, allow_nan=False).encode()
    if len(wire) > max_bytes:
        raise ValueError("spool size limit exceeded")
    request_id = bundle["trace"]["request_id"]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", request_id):
        raise ValueError("invalid spool request id")
    directory = _directory(root)
    # 磁盘占用有上限，也包括崩溃进程留下的数据。目录达到配额后，
    # 在接纳更多需保留的请求前先进行恢复。
    files = list(directory.glob("*.json"))
    target = directory / f"{request_id}.json"
    total = sum(p.stat().st_size for p in files if not p.is_symlink())
    previous = target.stat().st_size if target.exists() and not target.is_symlink() else 0
    if (not target.exists() and len(files) >= 1000) or total-previous+len(wire) > 64*1024*1024:
        raise OSError("recognition spool capacity reached")
    descriptor, temporary = tempfile.mkstemp(prefix=request_id + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(wire)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


class Trace:
    def __init__(self, input, settings, *, repository=None, source="recognition", request_id=None,
                 root=None, publish=None, max_events=512, budget=None, started_at=None,
                 managed=False, initial_status="RUNNING", owner_id=None, write_guard=None, finish_transform=None,
                 task_generation=None):
        self.request_id = request_id or uuid.uuid4().hex
        self.repository = RecognitionRepository() if repository is None else repository
        self.mode = recognition_record_mode(settings)
        self.settings = deepcopy(settings)
        self.root, self.publish = root, publish
        self.started_at = started_at or utc_now()
        self.started_ns = budget.started_ns if budget is not None else time.monotonic_ns()
        self.budget = budget or RecognitionBudget(total_timeout_ms(settings), started_ns=self.started_ns)
        self.events, self.candidates = [], []
        self._llm_calls = {}
        self.node_states = {}
        self._event_sizes, self._buffer_bytes, self._events_truncated = [], 0, False
        self.max_events = max(4, min(max_events, 4096))
        self.finished_at, self.status, self.outcome = None, initial_status, None
        self._result = None
        self.source, self.input = source, redact(input)
        if len(json.dumps(self.input).encode()) > 65536:
            raise ValueError("trace input exceeds 64 KiB")
        self.persisted, self.spool_error, self.publish_error = False, None, None
        self._path, self._seq, self._mutex = None, 0, RLock()
        self.managed, self.write_guard, self.finish_transform = managed, write_guard, finish_transform
        self.task_generation = task_generation
        self._task_owner = owner_id or f"pid:{os.getpid()}"
        self._task("RUNNING", time.time()+30)
        if not managed:
            _register_heartbeat(self)
        self._hub = None
        if self.mode == "none" and not managed:
            from app.media.recognition.trace_ipc import get_hub
            self._hub = get_hub()
            self._hub.register(self)
        with _lock:
            _active.add(self.request_id)
        self.emit("request", "request_started", "running", {"input": self.input})

    def _task(self, state, lease_until=0, *, retain=True):
        if self.managed:
            return
        try:
            self.repository.task(self.request_id, owner=self._task_owner, state=state,
                                 request_id=self.request_id, lease_until=lease_until, retain=retain)
        except Exception as error:
            self.spool_error = type(error).__name__

    def outcome_snapshot(self):
        outcome = deepcopy(self.outcome)
        if outcome is not None or self._llm_calls:
            outcome = {**(outcome or {}), "llm_usage": self.llm_usage}
        return outcome

    def bundle(self):
        return {"schema_version": 3, "generation": _generation(), "updated_at": utc_now(),
                "owner_pid": os.getpid(), "owner_id": self._task_owner, "managed_task": self.managed,
                "task_generation": self.task_generation,
                "trace": {"request_id": self.request_id, "started_at": self.started_at,
                    "started_monotonic_ns": self.started_ns, "finished_at": self.finished_at,
                    "status": self.status, "mode": self.mode, "source": self.source,
                    "input": self.input, "outcome": self.outcome_snapshot()},
                "events": self.events, "candidates": self.candidates}

    @property
    def llm_usage(self):
        return combine_usage(self._llm_calls.values())

    def _spool(self):
        if self.mode == "none":
            return False
        try:
            if self.write_guard is not None and not self.write_guard():
                return False
            self._path = write_spool(self.bundle(), self.root)
            return True
        except (OSError, ValueError) as error:
            self.spool_error = type(error).__name__
            return False

    def _persist(self):
        bundle = self.bundle()
        try:
            if self.write_guard is not None and not self.write_guard():
                return False
            options = {"task_fence": {"owner": self._task_owner, "generation": self.task_generation}} if self.managed else {}
            self.persisted = self.repository.save(bundle["trace"], bundle["events"], bundle["candidates"], **options)
            return self.persisted
        except Exception as error:
            self.spool_error = type(error).__name__
            return False

    def _publish(self, event):
        if self.publish is not None:
            try:
                self.publish(deepcopy(event))
            except Exception as error:
                self.publish_error = type(error).__name__

    def emit(self, node_id, event_type, status, payload=None, *, _final=False):
        with self._mutex:
            # 在有界事件缓冲区丢弃诊断信息前先计数。
            # parser_finished 和 parser_result 共用一个 call_id，只计一次。
            llm = (payload or {}).get("llm")
            if node_id == "openai" and isinstance(llm, dict) and isinstance(llm.get("call_id"), str) and isinstance(llm.get("usage"), dict):
                self._llm_calls[llm["call_id"]] = deepcopy(llm["usage"])
            if self._events_truncated and not _final:
                return None
            if len(self.events) >= self.max_events and not _final:
                if self.mode != "none":
                    return None
                self.events.pop(0)
                self._buffer_bytes -= self._event_sizes.pop(0)
            self._seq += 1
            if len(self.events) == self.max_events - 1 and self.mode != "none" and not _final:
                event_type, payload = "events_truncated", {"reason": "event_buffer_limit"}
                self._events_truncated = True
            event = {"schema_version": 3, "request_id": self.request_id, "seq": self._seq,
                     "node_id": node_id, "event_type": event_type, "status": status,
                     "emitted_at_utc": utc_now(), "offset_ns": max(0, time.monotonic_ns()-self.started_ns),
                     "durability": "volatile" if self.mode == "none" else "temporary",
                     "payload": redact(payload or {})}
            if len(json.dumps(event).encode()) > 65536:
                event["payload"] = {"truncated": True, "reason": "event_payload_limit"}
            size = len(json.dumps(event).encode())+64
            if self.mode == "none":
                while self.events and self._buffer_bytes+size > 1024*1024:
                    self.events.pop(0)
                    self._buffer_bytes -= self._event_sizes.pop(0)
            elif self._buffer_bytes+size > 1024*1024 and not _final:
                event["event_type"], event["payload"] = "events_truncated", {"reason": "event_byte_limit"}
                self._events_truncated = True
                size = len(json.dumps(event).encode())+64
            self.events.append(event)
            self.node_states[node_id] = {"status": status, "seq": self._seq,
                "event_type": event_type, "offset_ns": event["offset_ns"]}
            if len(self.node_states) > 128:
                self.node_states.pop(next(iter(self.node_states)))
            self._event_sizes.append(size)
            self._buffer_bytes += size
            if self.mode == "all":
                event["durability"] = "persistent"
                if not self._persist():
                    event["durability"] = "temporary"
            spooled = self._spool()
            if not self.persisted and not spooled:
                event["durability"] = "volatile"
            self._publish(event)
            return deepcopy(event)

    def finish(self, outcome, *, validated_at_ns=None):
        with self._mutex:
            if self.finished_at is not None:
                return self._result
            outcome = admit_outcome(outcome, self.budget, validated_at_ns=validated_at_ns,
                allow_partial_success=(self.settings.get("execution") or {}).get("allow_partial_success_on_deadline", False))
            if self.finish_transform is not None:
                outcome = self.finish_transform(outcome)
            self.finished_at, self.status = utc_now(), outcome.status.value
            self._result = outcome
            self.outcome = redact(asdict(outcome))
            self.outcome["llm_usage"] = self.llm_usage
            keep = self.mode == "all" or self.mode == "failed" and (
                self.status in FAILED or self.status == "CANCELLED" and outcome.failure_code == "service_shutdown")
            publish = self.publish
            if self.mode == "failed" and keep:
                self.publish = None
            try:
                final_event = self.emit("request", "request_finished", self.status,
                                        {"failure_code": outcome.failure_code, "llm_usage": self.llm_usage}, _final=True)
            finally:
                self.publish = publish
            if keep:
                for event in self.events:
                    event["durability"] = "persistent"
                if not self._persist():
                    spooled = self._spool()
                    self.events[-1]["durability"] = "temporary" if spooled else "volatile"
            if publish is not None and self.mode == "failed" and keep and final_event is not None:
                self._publish(self.events[-1])
            if self._path is not None and (not keep or self.persisted):
                try:
                    self._path.unlink(missing_ok=True)
                except OSError as error:
                    self.spool_error = type(error).__name__
            with _lock:
                _active.discard(self.request_id)
                _heartbeats.pop(self.request_id, None)
            task_state = ("INTERRUPTED" if outcome.failure_code == "service_shutdown" else "CANCELLED") if self.status == "CANCELLED" else (
                "FAILED" if self.status in FAILED else "SUCCEEDED")
            self._task(task_state, retain=keep)
            if self._hub is not None:
                self._hub.release(self)
            return outcome

    @property
    def source_trace_id(self):
        return self.request_id if self.persisted else None

    def detach(self):
        """释放最终控制信息写入失败的所有者；保留其暂存记录。"""
        with self._mutex:
            if self.finished_at is None:
                self.finished_at, self.status = utc_now(), "FAILED"
                self.outcome = {"status": "FAILED", "failure_code": "service_interrupted"}
                self.persisted = False
            with _lock:
                _active.discard(self.request_id)
                _heartbeats.pop(self.request_id, None)


def recover_spool(repository=None, root=None, limit=500):
    repository = RecognitionRepository() if repository is None else repository
    counts = {"recovered": 0, "retired": 0, "pending": 0, "invalid": 0}
    if root is None:
        from config import Config
        root = Config().get_config_path()
    from app.media.recognition.admission import upgrade_gate
    if upgrade_gate(root):
        raise RuntimeError("recognition schema upgrade in progress; spool recovery refused")
    if not (Path(root) / "recognition_v3_spool").exists():
        return counts
    path = _directory(root)
    generation = _generation()
    if not generation:
        raise RuntimeError("recognition database generation unavailable; spool recovery refused")
    with _lock:
        active = set(_active)
    for file in sorted(path.glob("*.json"))[:limit]:
        if file.stem in active:
            continue
        if file.is_symlink() or file.stat().st_size > 2 * 1024 * 1024:
            counts["invalid"] += 1
            continue
        try:
            bundle = json.loads(file.read_text())
            if bundle.get("schema_version") != 3 or bundle.get("generation") != generation:
                file.rename(file.with_suffix(".retired"))
                counts["retired"] += 1
                continue
            trace = bundle["trace"]
            if bundle.get("managed_task"):
                task = repository.get_task(trace["request_id"], bundle.get("owner_id", ""))
                if (task is None or task["control"].get("generation") != bundle.get("task_generation")
                        or task["control"].get("schema_version") != 3):
                    file.rename(file.with_suffix(".retired"))
                    counts["retired"] += 1
                    continue
            if trace["mode"] == "none":
                file.unlink()
                counts["retired"] += 1
                continue
            alive = _owner_alive(bundle)
            if bundle.get("managed_task") and task["state"] == "INTERRUPTED" and task["lease_until"] <= time.time():
                alive = False
            if repository.active_task(trace["request_id"]) and (alive or "owner_pid" not in bundle):
                counts["pending"] += 1
                continue
            if not alive and bundle.get("owner_id") and not bundle.get("managed_task"):
                repository.task(trace["request_id"], owner=bundle["owner_id"], state="INTERRUPTED")
            task = repository.get_task(trace["request_id"], bundle.get("owner_id", ""))
            if alive and (trace["status"] in {"RUNNING", "QUEUED"} or task is not None and task["state"] in {"QUEUED", "RUNNING", "CANCELLING"}):
                counts["pending"] += 1
                continue
            if trace["status"] in {"RUNNING", "QUEUED"}:
                finished_at = bundle["updated_at"]
                if bundle.get("managed_task") and task["control"].get("clear_protected_at"):
                    from app.db.models import RECOGNITIONRECORDSTATE
                    with repository.transaction(lock=False) as (session, _state):
                        state = session.get(RECOGNITIONRECORDSTATE, "history")
                        if state is not None and state.CLEAR_FINISHED_BEFORE == task["control"]["clear_protected_at"]:
                            finished_at = utc_now()
                outcome = trace.get("outcome") or {}
                outcome.update(failure_code="service_interrupted", status="FAILED", result=None)
                if outcome.get("compatibility"):
                    from app.media.recognition.compatibility import summary
                    compatibility = outcome["compatibility"]
                    compatibility.setdefault("overall_result", {}).update(
                        status="failed", reason="process_interrupted", lifecycle="interrupted")
                    compatibility["summary"] = summary(compatibility)
                trace.update(status="FAILED", finished_at=finished_at, outcome=outcome)
                events = bundle["events"]
                events.append({"schema_version": 3, "request_id": trace["request_id"],
                    "seq": max((event["seq"] for event in events), default=0)+1,
                    "node_id": "request", "event_type": "request_interrupted", "status": "FAILED",
                    "emitted_at_utc": utc_now(), "offset_ns": max((event["offset_ns"] for event in events), default=0),
                    "durability": "persistent", "payload": {"failure_code": "service_interrupted"}})
            if trace["mode"] == "failed" and trace["status"] not in FAILED and not (
                    trace["status"] == "CANCELLED" and (trace.get("outcome") or {}).get("failure_code") == "service_shutdown"):
                file.unlink()
                counts["retired"] += 1
                continue
            if repository.save(trace, bundle["events"], bundle.get("candidates", ())):
                counts["recovered"] += 1
            else:
                counts["retired"] += 1  # 水位或保留策略拒绝了该记录
            file.unlink()
        except (KeyError, ValueError, TypeError):
            counts["invalid"] += 1
        except Exception:
            counts["pending"] += 1
    return counts
