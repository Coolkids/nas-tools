"""Bounded owner-driven tasks. SQL stores controls; owners keep request bodies."""

import atexit
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from threading import Condition, Thread, RLock
import time
import uuid
import weakref

from sqlalchemy import select, delete

from app.db.models import RECOGNITIONTASK as TaskRow
from app.media.recognition import admission
from app.media.recognition.budget import RecognitionBudget, RecognitionCancelled, BudgetExceeded, CancellationReason
from app.media.recognition.contracts import RecognitionOutcome, RecognitionStatus
from app.media.recognition.coordinator import RecognitionCoordinator, effective_settings
from app.media.recognition.repository import RecognitionRepository, ACTIVE_TASKS, encode, utc_now
from app.media.recognition.runtime import ResourceBusy
from app.media.recognition.trace import Trace, recover_spool, _generation
from app.media.recognition.trace_ipc import TraceHub, owner_command, get_active_trace


KIND = "recognition_async"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


class TaskUnavailable(RuntimeError):
    pass


def validate_input(value):
    allowed = {"title", "subtitle", "name", "year", "season", "mtype", "cache", "force", "chinese", "bypass_identity_cache"}
    if not isinstance(value, dict) or set(value)-allowed or not isinstance(value.get("title"), str) or not value["title"].strip():
        raise ValueError("invalid_recognition_input")
    if len(encode(value).encode()) > 65536:
        raise ValueError("recognition_input_too_large")
    for field in ("title", "subtitle", "name"):
        if value.get(field) is not None and (not isinstance(value[field], str) or len(value[field]) > 16384):
            raise ValueError("invalid_recognition_input")
    for field in ("cache", "force", "chinese", "bypass_identity_cache"):
        if field in value and type(value[field]) is not bool:
            raise ValueError("invalid_recognition_input")
    if value.get("mtype") is not None and (not isinstance(value["mtype"], str) or value["mtype"] not in {"movie", "tv", "anime", "unknown"}):
        raise ValueError("invalid_recognition_input")
    for field, maximum in (("year", 2200), ("season", 999)):
        if value.get(field) is not None and (type(value[field]) is not int or not 0 <= value[field] <= maximum):
            raise ValueError("invalid_recognition_input")
    return deepcopy(value)


class TaskControls:
    def __init__(self, repository=None):
        self.repository = repository or RecognitionRepository()

    @staticmethod
    def view(row):
        return {"request_id": row.TASK_ID, "state": row.STATE, "owner": row.OWNER_ID,
                "lease_until": row.LEASE_UNTIL, "cancel_reason": row.CANCEL_REASON,
                "control": json.loads(row.CONTROL or "{}")}

    def get(self, task_id, actor=None):
        with self.repository.transaction(lock=False) as (session, _state):
            row = session.get(TaskRow, task_id)
            if row is None or row.KIND != KIND:
                return None
            value = self.view(row)
            if actor is not None and str(actor) not in {"0", value["control"].get("actor_id")}:
                return None
            return value

    def create(self, task_id, owner, actor, generation, mode, lease, retry_of=None, operation="recognition", idempotency=None):
        control = {"schema_version": 3, "generation": generation, "actor_id": str(actor),
                   "owner_pid": os.getpid(), "process_start": admission.process_start(os.getpid()),
                   "record_mode": mode, "operation": operation, "updated_at": utc_now()}
        if retry_of is not None:
            control["retry_of"] = retry_of
        with self.repository.transaction() as (session, _state):
            # Opening the transaction may reconnect after a configuration change.
            # Recheck the actual database here, before publishing the new owner.
            if _generation() != generation:
                raise TaskUnavailable("database_generation_changed")
            if idempotency is not None:
                reservation_id, fingerprint = idempotency
                reservation = session.get(TaskRow, reservation_id)
                if reservation is not None and reservation.LEASE_UNTIL > time.time():
                    if json.loads(reservation.CONTROL).get("idempotency_hash") != fingerprint:
                        raise TaskUnavailable("idempotency_conflict")
                    existing = session.get(TaskRow, reservation.REQUEST_ID)
                    if existing is None:
                        raise TaskUnavailable("idempotency_expired")
                    return self.view(existing)
                if reservation is None:
                    reservation = TaskRow(TASK_ID=reservation_id, OWNER_ID=str(actor), KIND="recognition_idempotency",
                        STATE="SUCCEEDED", CREATED_AT=utc_now())
                    session.add(reservation)
                reservation.REQUEST_ID, reservation.LEASE_UNTIL = task_id, time.time()+86400
                reservation.CONTROL = encode({"idempotency_hash": fingerprint})
            session.add(TaskRow(TASK_ID=task_id, REQUEST_ID=task_id, OWNER_ID=owner, KIND=KIND,
                STATE="QUEUED", CREATED_AT=utc_now(), LEASE_UNTIL=time.time()+lease, CONTROL=encode(control)))
        return None

    @staticmethod
    def owned(row, owner, generation):
        if row is None or row.KIND != KIND or row.OWNER_ID != owner or row.LEASE_UNTIL <= time.time():
            return False
        control = json.loads(row.CONTROL or "{}")
        return control.get("schema_version") == 3 and control.get("generation") == generation

    def heartbeat(self, task_id, owner, generation, lease):
        with self.repository.transaction() as (session, _state):
            row = session.get(TaskRow, task_id)
            if not self.owned(row, owner, generation):
                return False, "service_shutdown"
            control = json.loads(row.CONTROL)
            if control.get("resource_released_at"):
                return False, "service_shutdown"
            row.LEASE_UNTIL = time.time()+lease
            control["updated_at"] = utc_now()
            row.CONTROL = encode(control)
            return True, row.CANCEL_REASON

    def poll(self, owner, generation, lease, renew=False):
        """One bounded control query per owner, instead of one query per task."""
        result = {}
        with self.repository.transaction(lock=renew) as (session, _state):
            rows = session.execute(select(TaskRow).where(TaskRow.KIND == KIND, TaskRow.OWNER_ID == owner,
                TaskRow.LEASE_UNTIL > 0)).scalars()
            for row in rows:
                control = json.loads(row.CONTROL or "{}")
                owned = self.owned(row, owner, generation) and not control.get("resource_released_at")
                result[row.TASK_ID] = (owned, row.CANCEL_REASON)
                if renew and owned:
                    row.LEASE_UNTIL = time.time()+lease
                    control["updated_at"] = utc_now()
                    row.CONTROL = encode(control)
        return result

    def start(self, task_id, owner, generation):
        with self.repository.transaction() as (session, _state):
            row = session.get(TaskRow, task_id)
            if not self.owned(row, owner, generation) or row.STATE not in {"QUEUED", "CANCELLING"}:
                raise TaskUnavailable("task_lease_lost")
            if not row.CANCEL_REASON:
                row.STATE = "RUNNING"
            return row.CANCEL_REASON

    def cancel(self, task_id, actor, reason="user_cancelled"):
        if reason not in {"user_cancelled", "service_shutdown"}:
            raise ValueError("invalid_cancellation_reason")
        with self.repository.transaction() as (session, _state):
            row = session.get(TaskRow, task_id)
            if row is None or row.KIND != KIND or str(actor) not in {"0", json.loads(row.CONTROL)["actor_id"]}:
                raise TaskUnavailable("task_not_found")
            if row.STATE not in TERMINAL:
                control = json.loads(row.CONTROL)
                control.setdefault("cancel_requested_at", utc_now())
                row.CANCEL_REASON = row.CANCEL_REASON or reason
                row.STATE, row.CONTROL = "CANCELLING", encode(control)
            return self.view(row)

    def can_write(self, task_id, owner, generation):
        value = self.get(task_id)
        return bool(value and value["owner"] == owner and value["lease_until"] > time.time()
                    and value["control"].get("generation") == generation
                    and value["control"].get("schema_version") == 3)

    def finish(self, task_id, owner, generation, outcome):
        with self.repository.transaction() as (session, _state):
            row = session.get(TaskRow, task_id)
            if not self.owned(row, owner, generation) or row.STATE in TERMINAL:
                return replace(outcome, status=RecognitionStatus.FAILED, result=None, failure_code="task_lease_lost")
            reason = row.CANCEL_REASON or ("service_shutdown" if outcome.failure_code == "service_shutdown" else None)
            if reason:
                outcome = replace(outcome, status=RecognitionStatus.CANCELLED if reason == "user_cancelled" else RecognitionStatus.FAILED,
                    result=None, failure_code="user_cancelled" if reason == "user_cancelled" else "service_interrupted")
            row.STATE = ("INTERRUPTED" if outcome.failure_code == "service_interrupted" else
                "CANCELLED" if outcome.status == RecognitionStatus.CANCELLED else
                "FAILED" if outcome.status in {RecognitionStatus.FAILED, RecognitionStatus.TIMEOUT, RecognitionStatus.REVIEW_REQUIRED} else "SUCCEEDED")
            control = json.loads(row.CONTROL)
            control.update(updated_at=utc_now(), finished_at=utc_now())
            if outcome.failure_code:
                import re
                control["error_code"] = outcome.failure_code if re.fullmatch(r"[A-Za-z0-9_]{1,128}", outcome.failure_code) else "operation_failed"
            row.CONTROL = encode(control)
            return outcome

    def release(self, task_id, owner, generation):
        with self.repository.transaction() as (session, _state):
            row = session.get(TaskRow, task_id)
            if not self.owned(row, owner, generation):
                return False
            control = json.loads(row.CONTROL)
            if row.STATE not in TERMINAL:
                row.STATE, row.CANCEL_REASON = "INTERRUPTED", row.CANCEL_REASON or "service_shutdown"
            control.update(resource_released_at=utc_now(), updated_at=utc_now())
            row.LEASE_UNTIL, row.CONTROL = 0, encode(control)
            return True

    def recover(self, generation, limit=500):
        count = 0
        with self.repository.transaction() as (session, _state):
            rows = session.execute(select(TaskRow).where(TaskRow.KIND == KIND,
                TaskRow.LEASE_UNTIL > 0).order_by(TaskRow.LEASE_UNTIL, TaskRow.TASK_ID).limit(limit)).scalars()
            for row in rows:
                control = json.loads(row.CONTROL or "{}")
                if (control.get("schema_version") != 3 or control.get("generation") != generation
                        or row.LEASE_UNTIL <= time.time()
                        or not admission.process_alive(control.get("owner_pid"), control.get("process_start"))):
                    if row.STATE not in TERMINAL:
                        row.STATE, row.CANCEL_REASON = "INTERRUPTED", "service_shutdown"
                    control.update(resource_released_at=utc_now(), updated_at=utc_now())
                    row.LEASE_UNTIL, row.CONTROL = 0, encode(control)
                    count += 1
        return count

    def inflight(self):
        with self.repository.transaction(lock=False) as (session, _state):
            return session.execute(select(TaskRow.TASK_ID).where(TaskRow.KIND == KIND,
                TaskRow.LEASE_UNTIL > time.time()).limit(1)).first() is not None


_services, _services_lock = weakref.WeakSet(), RLock()


class TaskService:
    def __init__(self, settings=None, *, repository=None, root=None, runner=None, lease_seconds=30,
                 heartbeat_seconds=1, poll_seconds=.05, handlers=None):
        if not 0 < poll_seconds <= heartbeat_seconds < lease_seconds:
            raise ValueError("invalid_task_lease_intervals")
        self.settings = settings
        config = effective_settings(settings)
        self.repository, self.controls = repository or RecognitionRepository(), None
        self.controls = TaskControls(self.repository)
        self.root = admission.root_path(root)
        self.owner = f"pid:{os.getpid()}:{uuid.uuid4().hex}"
        self.generation = _generation()
        if not self.generation:
            raise TaskUnavailable("database_generation_unavailable")
        self.runner = runner or self._resolve
        self.handlers = handlers or {}
        execution = config["recognition"]["execution"]
        self.running_limit, self.queue_limit = min(32, execution["max_running_tasks"]), execution["max_queued_tasks"]
        self.lease, self.heartbeat_interval, self.poll = lease_seconds, heartbeat_seconds, poll_seconds
        self._condition, self._queue, self._tasks = Condition(RLock()), deque(), {}
        self._closing, self._paused = False, False
        self.hub = TraceHub(self.root, control=self._command, max_traces=self.running_limit+self.queue_limit+128)
        self.threads = [Thread(target=self._work, daemon=True, name=f"recognition-task-{i}") for i in range(self.running_limit)]
        self.monitor = Thread(target=self._monitor, daemon=True, name="recognition-task-controls")
        with _services_lock:
            _services.add(self)
        for thread in self.threads+[self.monitor]:
            thread.start()

    @staticmethod
    def _resolve(input, trace, budget, config):
        return RecognitionCoordinator(config, repository=trace.repository, trace_root=trace.root).resolve(
            **input, task_trace=trace, task_budget=budget)

    def submit(self, input, actor, *, retry_of=None, idempotency_key=None):
        input = validate_input(input)
        return self.enqueue(input, actor, retry_of=retry_of, idempotency_key=idempotency_key)

    def enqueue(self, input, actor, *, operation="recognition", retry_of=None, idempotency_key=None):
        if operation not in {"recognition", "confirm", "refresh", "invalidate"}:
            raise ValueError("invalid_task_operation")
        input = deepcopy(input)
        idempotency = None
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
                raise ValueError("invalid_idempotency_key")
            reservation = "idem_"+hashlib.sha256(encode([str(actor), operation, idempotency_key]).encode()).hexdigest()
            fingerprint = self.fingerprint(operation, input, retry_of)
            idempotency = (reservation, fingerprint)
        if not str(actor) or len(str(actor)) > 128:
            raise ValueError("invalid_task_actor")
        config = effective_settings(self.settings)
        with self._condition:
            existing = self.existing(input, actor, operation=operation, retry_of=retry_of, idempotency_key=idempotency_key)
            if existing is not None:
                return existing
            if self._closing or self._paused or admission.gate(self.root) or _generation() != self.generation:
                raise TaskUnavailable("recognition_unavailable")
            if len(self._tasks) >= self.running_limit+self.queue_limit:
                raise ResourceBusy("recognition task queue is full")
            task_id = uuid.uuid4().hex
            budget = RecognitionBudget(config["recognition"]["timeouts"]["total_ms"])
            existing = self.controls.create(task_id, self.owner, actor, self.generation,
                config["recognition"]["records"]["mode"], self.lease, retry_of, operation, idempotency)
            if existing is not None:
                return {"code": 0, "request_id": existing["request_id"], "job_id": existing["request_id"],
                    "status": existing["state"], "http_status": 202, "reused": True}
            trace_input = {**input, "actor_id": str(actor)}
            if retry_of is not None:
                trace_input["retry_of"] = retry_of
            trace = Trace(trace_input, config["recognition"], repository=self.repository, request_id=task_id,
                root=self.root, budget=budget, source={"recognition": "recognition_task", "confirm": "recognition_confirm",
                    "refresh": "cache_refresh", "invalidate": "cache_invalidate"}[operation], managed=True, initial_status="QUEUED",
                owner_id=self.owner, task_generation=self.generation,
                write_guard=lambda: self.controls.can_write(task_id, self.owner, self.generation),
                finish_transform=lambda outcome: self._finish(task_id, outcome))
            self.hub.register(trace)
            self._tasks[task_id] = {"input": input, "trace": trace, "budget": budget, "config": config,
                "heartbeat_at": time.monotonic(), "started": False, "operation": operation}
            self._queue.append(task_id)
            self._condition.notify_all()
            return {"code": 0, "request_id": task_id, "status": "QUEUED", "http_status": 202}

    @staticmethod
    def fingerprint(operation, input, retry_of=None):
        semantic = {key: value for key, value in input.items() if key != "expected"} if operation in {"refresh", "invalidate"} else input
        return hashlib.sha256(json.dumps([operation, semantic, retry_of], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def existing(self, input, actor, *, operation="recognition", retry_of=None, idempotency_key=None):
        if idempotency_key is None:
            return None
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValueError("invalid_idempotency_key")
        reservation_id = "idem_"+hashlib.sha256(encode([str(actor), operation, idempotency_key]).encode()).hexdigest()
        with self.repository.transaction(lock=False) as (session, _state):
            row = session.get(TaskRow, reservation_id)
            if row is None or row.LEASE_UNTIL <= time.time():
                return None
            if json.loads(row.CONTROL).get("idempotency_hash") != self.fingerprint(operation, input, retry_of):
                raise TaskUnavailable("idempotency_conflict")
            task = session.get(TaskRow, row.REQUEST_ID)
            if task is None:
                raise TaskUnavailable("idempotency_expired")
            return {"code": 0, "request_id": task.TASK_ID, "job_id": task.TASK_ID,
                    "status": task.STATE, "http_status": 202, "reused": True}

    def _finish(self, task_id, outcome):
        task = self._tasks.get(task_id)
        outcome = self.controls.finish(task_id, self.owner, self.generation, outcome)
        if task is not None:
            if outcome.failure_code in {"user_cancelled", "service_interrupted", "task_lease_lost"}:
                self._cancel(task, "user_cancelled" if outcome.failure_code == "user_cancelled" else "service_shutdown")
            task["trace"].emit("task", "resource_released", "released", {"remote_cancellation": "unknown"})
        return outcome

    def snapshot(self, task_id, actor, after_seq=None):
        if after_seq is not None and (type(after_seq) is not int or after_seq < 0):
            raise ValueError("invalid_event_cursor")
        value = self.controls.get(task_id, actor)
        if value is None:
            raise TaskUnavailable("task_not_found")
        trace = get_active_trace(task_id, self.root)
        if trace is None:
            trace = self.repository.get(task_id)
        availability = "available" if trace else "owner_unavailable" if value["state"] in ACTIVE_TASKS else (
            "history_expired" if value["control"].get("record_mode") == "all" else "not_retained")
        reset, nodes = False, {}
        if trace is not None:
            trace = deepcopy(trace)
            nodes.update(trace.get("nodes", {}))
            events = trace["events"]
            for event in events:
                nodes[event["node_id"]] = {"status": event["status"], "seq": event["seq"], "payload": event["payload"]}
            reset = bool(after_seq is not None and events and after_seq < events[0]["seq"]-1)
            trace["last_seq"] = max((event["seq"] for event in events), default=0)
            if after_seq is not None:
                trace["events"] = [event for event in events if event["seq"] > after_seq]
        return {"task": value, "trace": trace, "availability": availability, "reset_required": reset, "nodes": nodes}

    def cancel(self, task_id, actor):
        value = self.controls.cancel(task_id, actor)
        if value["state"] not in TERMINAL:
            owner_command("cancel", task_id, root=self.root)
        return {"request_id": task_id, "state": value["state"], "cancellation_requested": value["state"] not in TERMINAL,
                "http_status": 202}

    def retry(self, task_id, actor, *, bypass_identity_cache=False, idempotency_key=None):
        if type(bypass_identity_cache) is not bool:
            raise ValueError("invalid_identity_bypass")
        value = self.controls.get(task_id, actor)
        trace = self.repository.get(task_id)
        if value is None:
            if (trace is None
                    or str(actor) not in {"0", trace["input"].get("actor_id")}):
                raise TaskUnavailable("task_not_found")
        elif value["state"] not in TERMINAL:
            raise TaskUnavailable("task_not_terminal")
        if trace is None:
            raise TaskUnavailable("history_not_retained")
        if trace["source"] in {"cache_refresh", "cache_invalidate", "recognition_confirm"}:
            raise TaskUnavailable("task_not_retryable")
        if trace["status"] in {"QUEUED", "RUNNING"}:
            raise TaskUnavailable("task_not_terminal")
        input = {key: value for key, value in trace["input"].items()
                 if key in {"title", "subtitle", "name", "year", "season", "mtype", "cache", "force", "chinese"}}
        for old, new in (("secondary_title", "subtitle"), ("name_override", "name"), ("year_override", "year"),
                         ("season_override", "season"), ("media_type", "mtype")):
            if old in trace["input"] and new not in input:
                input[new] = trace["input"][old]
        if bypass_identity_cache:
            input["bypass_identity_cache"] = True
        return self.submit(input, actor, retry_of=task_id, idempotency_key=idempotency_key)

    def _command(self, action, task_id):
        with self._condition:
            task = self._tasks.get(task_id)
            if action == "cancel" and task is not None:
                value = self.controls.get(task_id)
                if value and value["cancel_reason"]:
                    self._cancel(task, value["cancel_reason"])
                return {"acknowledged": True}
        return None

    @staticmethod
    def _cancel(task, reason):
        if task["trace"].finished_at is not None:
            return
        first = task["budget"].cancellation.reason is None
        task["budget"].cancellation.cancel(reason)
        if first:
            task["trace"].emit("task", "cancel_requested", "cancelling", {"reason": reason, "remote_cancellation": "unknown"})

    def _monitor(self):
        heartbeat_at = time.monotonic()
        recovered_at = heartbeat_at
        while True:
            with self._condition:
                if self._closing and not self._tasks:
                    return
                tasks = list(self._tasks.items())
            paused = bool(admission.gate(self.root))
            if paused and not self._paused:
                from app.media.recognition.runtime import cancel_runtime
                cancel_runtime()
            self._paused = paused
            try:
                renew = time.monotonic()-heartbeat_at >= self.heartbeat_interval
                statuses = self.controls.poll(self.owner, self.generation, self.lease, renew) if tasks else {}
                if renew:
                    heartbeat_at = time.monotonic()
            except Exception:
                statuses = None
            for task_id, task in tasks:
                try:
                    if statuses is not None:
                        owned, reason = statuses.get(task_id, (False, "service_shutdown"))
                        if renew and owned:
                            task["heartbeat_at"] = heartbeat_at
                        if not owned or reason:
                            self._cancel(task, reason or "service_shutdown")
                    elif time.monotonic()-task["heartbeat_at"] >= self.lease:
                        self._cancel(task, "service_shutdown")
                    if paused or self._closing:
                        self._cancel(task, "service_shutdown")
                except Exception:
                    # A missing database must never grant an infinite owner lease.
                    if time.monotonic()-task["heartbeat_at"] >= self.lease:
                        self._cancel(task, "service_shutdown")
            time.sleep(self.poll)
            if time.monotonic()-recovered_at >= 60 and not paused:
                try:
                    recover_tasks(self.repository, self.root)
                except Exception:
                    pass  # Retry bounded recovery without changing record policy.
                recovered_at = time.monotonic()

    def _work(self):
        while True:
            with self._condition:
                while not self._queue and not self._closing:
                    self._condition.wait(.1)
                if not self._queue:
                    return
                task_id = self._queue.popleft()
                task = self._tasks[task_id]
                task["started"] = True
            trace, budget = task["trace"], task["budget"]
            try:
                reason = self.controls.start(task_id, self.owner, self.generation)
                if reason or admission.gate(self.root) or self._closing:
                    self._cancel(task, reason or "service_shutdown")
                budget.check()
                queued_ms = task["config"]["recognition"]["timeouts"].get("queue_ms")
                if queued_ms is not None and time.monotonic_ns()-budget.started_ns >= queued_ms*1_000_000:
                    raise BudgetExceeded("queue")
                trace.status = "RUNNING"
                trace.emit("task", "task_running", "running", {})
                operation = task["operation"]
                if operation == "recognition":
                    result = self.runner(task["input"], trace, budget, task["config"])
                else:
                    from app.media.recognition.records import v3_recognition_lease
                    from app.media.recognition.operations import run_operation
                    with v3_recognition_lease(budget):
                        result = self.handlers.get(operation, lambda *args: run_operation(operation, *args))(
                            task["input"], trace, budget, task["config"])
                outcome = getattr(result, "outcome", result)
                if not isinstance(outcome, RecognitionOutcome):
                    raise ValueError("invalid_task_result")
            except (RecognitionCancelled, BudgetExceeded) as error:
                outcome = RecognitionOutcome(task_id, RecognitionStatus.CANCELLED if isinstance(error, RecognitionCancelled)
                    else RecognitionStatus.TIMEOUT, failure_code=str(error))
            except Exception as error:
                from app.media.recognition.identity import OverrideConflict
                from app.media.recognition.operations import OperationFailure
                outcome = RecognitionOutcome(task_id, RecognitionStatus.FAILED,
                    failure_code="VALIDATION_CONFLICT" if isinstance(error, OverrideConflict) else error.code if isinstance(error, OperationFailure) else "recognition_task_failed")
            try:
                if trace.finished_at is None:
                    trace.finish(outcome)
            except Exception:
                trace.detach()  # The last valid spool can now be recovered.
            finally:
                try:
                    self.controls.release(task_id, self.owner, self.generation)
                except Exception:
                    pass  # No lease renewal: recovery fences this owner on expiry.
                finally:
                    self.hub.release(trace)
                    from app.db import close_db
                    close_db()
                    with self._condition:
                        self._tasks.pop(task_id, None)
                        task["input"] = None
                        self._condition.notify_all()

    def shutdown(self, timeout=15):
        with self._condition:
            self._closing = True
            for task in self._tasks.values():
                self._cancel(task, "service_shutdown")
            self._condition.notify_all()
        deadline = time.monotonic()+timeout
        for thread in self.threads+[self.monitor]:
            thread.join(max(0, deadline-time.monotonic()))
        if any(thread.is_alive() for thread in self.threads+[self.monitor]):
            raise TimeoutError("recognition task resources not released")
        self.hub.close()


_default, _default_lock, _pause_token = None, RLock(), None


def get_task_service():
    global _default
    with _default_lock:
        if _default is None:
            _default = TaskService()
        return _default


def recover_tasks(repository=None, root=None):
    controls = TaskControls(repository)
    interrupted = controls.recover(_generation())
    spool = recover_spool(controls.repository, root)
    pruned = controls.repository.prune_tasks()
    return {"interrupted": interrupted, "spool": spool, "pruned": pruned}


def quiesce_tasks(timeout=120, root=None):
    global _pause_token
    token = admission.pause(root)
    _pause_token = (token, root)
    deadline = time.monotonic()+timeout
    try:
        from app.media.recognition.runtime import cancel_runtime
        cancel_runtime()
        controls = TaskControls()
        while True:
            recovered = recover_tasks(controls.repository, root)
            if not controls.inflight() and not recovered["spool"]["pending"]:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("recognition tasks did not release maintenance permits")
            time.sleep(.05)
    except BaseException:
        resume_tasks()
        raise


def resume_tasks():
    global _pause_token
    if _pause_token:
        token, root = _pause_token
        admission.resume(token, root)
        _pause_token = None


def shutdown_task_services():
    global _default
    with _services_lock:
        services = list(_services)
    for service in services:
        service.shutdown()
    _default = None


def _after_fork():
    global _services, _services_lock, _default, _default_lock, _pause_token
    for service in list(_services):
        service.hub.socket.close()
    _services, _services_lock = weakref.WeakSet(), RLock()
    _default, _default_lock, _pause_token = None, RLock(), None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


atexit.register(shutdown_task_services)
