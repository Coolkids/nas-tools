"""Short, portable transactions for V3 history and operational control."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import time

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.db.manager import DATABASE
from app.db.models import (
    RECOGNITIONTRACE as TraceRow, RECOGNITIONEVENT as EventRow,
    RECOGNITIONCANDIDATE as CandidateRow, RECOGNITIONTASK as TaskRow,
    RECOGNITIONRECORDSTATE as StateRow,
)
from app.utils.storage_jobs import STORAGE_JOBS


TERMINAL = frozenset({"MATCHED", "PARSED", "FAILED", "TIMEOUT", "REVIEW_REQUIRED", "CANCELLED", "SKIPPED"})
ACTIVE_TASKS = frozenset({"QUEUED", "RUNNING", "CANCELLING"})


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def encode(value):
    wire = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(wire.encode()) > 2 * 1024 * 1024:
        raise ValueError("recognition payload exceeds 2 MiB")
    return wire


class RecognitionRepository:
    def __init__(self, manager=None):
        self.manager = DATABASE if manager is None else manager

    @contextmanager
    def transaction(self, lock=True):
        with STORAGE_JOBS.maintenance_lock(exclusive=False):
            from app.media.recognition.admission import upgrade_gate
            if upgrade_gate() and not STORAGE_JOBS.owns_exclusive_lock():
                raise RuntimeError("recognition schema upgrade in progress")
            if STORAGE_JOBS.maintenance_gate("database") and not STORAGE_JOBS.owns_exclusive_lock():
                raise RuntimeError("database maintenance in progress")
            with Session(self.manager.engine, expire_on_commit=False) as session, session.begin():
                state = None
                if lock:
                    self.manager.backend.upsert_many(session, StateRow, [{"STATE_ID": "history", "VERSION": 0,
                        "CLEANUP_UNTIL": 0}], ["STATE_ID"])
                    state = session.execute(select(StateRow).where(StateRow.STATE_ID == "history").with_for_update()).scalar_one()
                    # SQLite has no SELECT FOR UPDATE. This also serializes
                    # watermark publication, final writes and export leases.
                    session.execute(update(StateRow).where(StateRow.STATE_ID == "history").values(VERSION=StateRow.VERSION))
                yield session, state

    @staticmethod
    def _protected(session, row, now=None):
        now = time.time() if now is None else now
        if row.EXPORT_UNTIL > now:
            return True
        return session.execute(select(TaskRow.TASK_ID).where(
            TaskRow.REQUEST_ID == row.REQUEST_ID, TaskRow.STATE.in_(ACTIVE_TASKS),
            TaskRow.LEASE_UNTIL > now).limit(1)).first() is not None

    def save(self, trace, events=(), candidates=(), *, task_fence=None):
        status = trace["status"]
        if status not in TERMINAL | {"QUEUED", "RUNNING"}:
            raise ValueError("invalid trace status")
        mode = trace["mode"]
        if mode == "none":
            return False
        if mode not in {"all", "failed"}:
            raise ValueError("invalid record mode")
        with self.transaction() as (session, state):
            if task_fence is not None:
                task = session.get(TaskRow, trace["request_id"])
                control = json.loads(task.CONTROL or "{}") if task is not None else {}
                if (task is None or task.OWNER_ID != task_fence["owner"] or task.LEASE_UNTIL <= time.time()
                        or control.get("generation") != task_fence["generation"] or control.get("schema_version") != 3):
                    return False
            existing = session.get(TraceRow, trace["request_id"])
            finished = trace.get("finished_at")
            if (finished and state.CLEAR_FINISHED_BEFORE and finished <= state.CLEAR_FINISHED_BEFORE
                    and (existing is None or not self._protected(session, existing))):
                if existing is not None and existing.STATUS in {"RUNNING", "QUEUED"}:
                    session.execute(delete(EventRow).where(EventRow.REQUEST_ID == existing.REQUEST_ID))
                    session.execute(delete(CandidateRow).where(CandidateRow.REQUEST_ID == existing.REQUEST_ID))
                    session.delete(existing)
                return False
            if existing is None:
                existing = TraceRow(REQUEST_ID=trace["request_id"], STARTED_AT=trace["started_at"],
                    STARTED_MONOTONIC_NS=trace["started_monotonic_ns"], STATUS=status, RECORD_MODE=mode,
                    SOURCE=trace.get("source", "unknown"), VERSION=0, EXPORT_UNTIL=0,
                    INPUT=encode(trace.get("input", {})))
                session.add(existing)
                session.flush()
            elif existing.STARTED_AT != trace["started_at"]:
                raise ValueError("trace start time is immutable")
            elif existing.STATUS in TERMINAL and existing.STATUS != status:
                raise ValueError("terminal trace cannot transition backwards")
            existing.STATUS, existing.FINISHED_AT = status, finished
            existing.OUTCOME = encode(trace.get("outcome"))
            existing.VERSION += 1
            sequence = session.execute(select(func.max(EventRow.SEQ)).where(
                EventRow.REQUEST_ID == existing.REQUEST_ID)).scalar_one() or 0
            for event in events:
                seq = event["seq"]
                if seq <= sequence:
                    old = session.get(EventRow, (existing.REQUEST_ID, seq))
                    previous = json.loads(old.PAYLOAD) if old is not None else {}
                    if {k: v for k, v in previous.items() if k != "durability"} != {k: v for k, v in event.items() if k != "durability"}:
                        raise ValueError("conflicting event sequence")
                    if previous != event:
                        old.PAYLOAD = encode(event)
                    continue
                if seq != sequence + 1:
                    raise ValueError("event sequence gap")
                session.add(EventRow(REQUEST_ID=existing.REQUEST_ID, SEQ=seq,
                    NODE_ID=event["node_id"], STATUS=event["status"], PAYLOAD=encode(event)))
                sequence = seq
            for candidate in candidates:
                if candidate["media_type"] not in {"movie", "tv"} or type(candidate["tmdb_id"]) is not int or candidate["tmdb_id"] <= 0:
                    raise ValueError("invalid candidate identity")
                row = session.get(CandidateRow, (existing.REQUEST_ID, candidate["media_type"], candidate["tmdb_id"]))
                if row is None:
                    row = CandidateRow(REQUEST_ID=existing.REQUEST_ID, MEDIA_TYPE=candidate["media_type"], TMDB_ID=candidate["tmdb_id"])
                    session.add(row)
                row.RANK, row.PAYLOAD = candidate.get("rank"), encode(candidate)
            return True

    def get(self, request_id):
        with self.transaction(lock=False) as (session, _state):
            row = session.get(TraceRow, request_id)
            if row is None:
                return None
            events = session.execute(select(EventRow).where(EventRow.REQUEST_ID == request_id).order_by(EventRow.SEQ)).scalars()
            candidates = session.execute(select(CandidateRow).where(CandidateRow.REQUEST_ID == request_id).order_by(CandidateRow.RANK)).scalars()
            return {"request_id": request_id, "started_at": row.STARTED_AT, "finished_at": row.FINISHED_AT,
                    "started_monotonic_ns": row.STARTED_MONOTONIC_NS, "status": row.STATUS,
                    "mode": row.RECORD_MODE, "source": row.SOURCE, "version": row.VERSION,
                    "input": json.loads(row.INPUT), "outcome": json.loads(row.OUTCOME),
                    "events": [json.loads(item.PAYLOAD) for item in events],
                    "candidates": [json.loads(item.PAYLOAD) for item in candidates]}

    def export_lease(self, request_ids, seconds=60):
        if not 0 < seconds <= 300 or len(request_ids) > 2000:
            raise ValueError("invalid export lease")
        with self.transaction() as (session, _state):
            return session.execute(update(TraceRow).where(TraceRow.REQUEST_ID.in_(request_ids)).values(
                EXPORT_UNTIL=time.time() + seconds)).rowcount

    def task(self, task_id, *, owner, state="QUEUED", kind="recognition", request_id=None,
             lease_until=0, control=None):
        allowed = {"scope", "cutoff", "deleted", "skipped", "remaining", "idempotency_hash", "error_code", "retention_cutoff", "max_records"}
        if control is not None and (not isinstance(control, dict) or set(control) - allowed):
            raise ValueError("task metadata cannot contain recognition input/result")
        for key, value in (control or {}).items():
            if key in {"deleted", "skipped", "remaining", "max_records"} and (type(value) is not int or value < 0):
                raise ValueError("invalid control counter")
            if key == "scope" and value not in {"expired", "all_completed"}:
                raise ValueError("invalid control scope")
            if key in {"cutoff", "retention_cutoff"}:
                datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
            if key == "idempotency_hash" and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("invalid control digest")
            if key == "error_code" and not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
                raise ValueError("invalid control error code")
        if state not in ACTIVE_TASKS | {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            raise ValueError("invalid task state")
        with self.transaction() as (session, _record_state):
            row = session.get(TaskRow, task_id)
            if row is None:
                row = TaskRow(TASK_ID=task_id, OWNER_ID=str(owner), KIND=kind, REQUEST_ID=request_id,
                              STATE=state, LEASE_UNTIL=lease_until, CREATED_AT=utc_now())
                session.add(row)
            elif row.OWNER_ID != str(owner):
                raise ValueError("task owner mismatch")
            elif row.STATE in {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"} and state != row.STATE:
                raise ValueError("terminal task cannot transition backwards")
            row.STATE, row.LEASE_UNTIL = state, lease_until
            if control is not None:
                row.CONTROL = encode(control)
        return task_id

    def get_task(self, task_id, owner):
        with self.transaction(lock=False) as (session, _state):
            row = session.get(TaskRow, task_id)
            if row is None or row.OWNER_ID != str(owner):
                return None
            return {"task_id": task_id, "state": row.STATE, "kind": row.KIND,
                    "control": json.loads(row.CONTROL or "{}"), "lease_until": row.LEASE_UNTIL}

    def active_task(self, request_id):
        with self.transaction(lock=False) as (session, _state):
            return session.execute(select(TaskRow.TASK_ID).where(TaskRow.REQUEST_ID == request_id,
                TaskRow.STATE.in_(ACTIVE_TASKS), TaskRow.LEASE_UNTIL > time.time()).limit(1)).first() is not None

    def prune_tasks(self):
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self.transaction() as (session, _state):
            session.execute(update(TaskRow).where(TaskRow.STATE.in_(ACTIVE_TASKS),
                TaskRow.LEASE_UNTIL <= time.time(), TaskRow.KIND.not_in({"cleanup", "recognition_async"})).values(STATE="INTERRUPTED"))
            ids = session.execute(select(TaskRow.TASK_ID).where(TaskRow.STATE.not_in(ACTIVE_TASKS),
                TaskRow.LEASE_UNTIL <= time.time(), TaskRow.CREATED_AT < cutoff).limit(2000)).scalars().all()
            if ids:
                session.execute(delete(TaskRow).where(TaskRow.TASK_ID.in_(ids)))
            return len(ids)
