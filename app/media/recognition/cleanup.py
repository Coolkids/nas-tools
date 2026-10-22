"""清理过期记录和清理经明确确认的全部已完成记录共用一把互斥锁。"""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import uuid

from dateutil.relativedelta import relativedelta
from sqlalchemy import delete, exists, func, select

from app.db.models import (
    RECOGNITIONTRACE as TraceRow, RECOGNITIONEVENT as EventRow,
    RECOGNITIONCANDIDATE as CandidateRow, RECOGNITIONTASK as TaskRow,
)
from app.media.recognition.repository import RecognitionRepository, TERMINAL, ACTIVE_TASKS, utc_now, encode


class CleanupBusy(RuntimeError):
    pass


class CleanupConflict(ValueError):
    pass


class CleanupConfirmationError(ValueError):
    pass


def retention_cutoff(value, unit="day", now=None):
    if type(value) is not int or not 0 < value <= (36500 if unit == "day" else 1200) or unit not in {"day", "month"}:
        raise ValueError("invalid retention duration")
    now = datetime.now(timezone.utc) if now is None else now
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    value = now - (timedelta(days=value) if unit == "day" else relativedelta(months=value))
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class CleanupService:
    _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recognition-cleanup")

    def __init__(self, repository=None, *, root=None, batch_size=500):
        self.repository = RecognitionRepository() if repository is None else repository
        if type(batch_size) is not int or not 1 <= batch_size <= 2000:
            raise ValueError("cleanup batch must be 1..2000")
        self.batch_size, self.root = batch_size, root

    def _recover(self):
        from app.media.recognition.tasks import recover_tasks
        return recover_tasks(self.repository, self.root)

    def _secret(self):
        if self.root is None:
            from config import Config
            root = Config().get_config_path()
        else:
            root = self.root
        directory = Path(root) / ".maintenance"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / "recognition-cleanup.key"
        if path.is_symlink():
            raise ValueError("invalid confirmation key path")
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
        secret = path.read_bytes()
        if len(secret) != 32:
            raise ValueError("invalid confirmation key")
        os.chmod(path, 0o600)
        return secret

    @staticmethod
    def _eligible(cutoff):
        active = exists(select(TaskRow.TASK_ID).where(TaskRow.REQUEST_ID == TraceRow.REQUEST_ID,
            TaskRow.STATE.in_(ACTIVE_TASKS), TaskRow.LEASE_UNTIL > time.time()))
        return (TraceRow.STATUS.in_(TERMINAL), TraceRow.FINISHED_AT <= cutoff,
                TraceRow.EXPORT_UNTIL <= time.time(), ~active)

    @staticmethod
    def _claim(state, owner):
        if state.CLEANUP_OWNER and state.CLEANUP_OWNER != owner and state.CLEANUP_UNTIL > time.time():
            raise CleanupBusy("recognition cleanup already running")
        state.CLEANUP_OWNER, state.CLEANUP_UNTIL = owner, time.time() + 30

    @staticmethod
    def _protection_counts(session):
        active = exists(select(TaskRow.TASK_ID).where(TaskRow.REQUEST_ID == TraceRow.REQUEST_ID,
            TaskRow.STATE.in_(ACTIVE_TASKS), TaskRow.LEASE_UNTIL > time.time()))
        running = session.execute(select(func.count()).select_from(TraceRow).where(
            (TraceRow.STATUS.not_in(TERMINAL)) | active)).scalar_one()
        exported = session.execute(select(func.count()).select_from(TraceRow).where(
            TraceRow.STATUS.in_(TERMINAL), ~active, TraceRow.EXPORT_UNTIL > time.time())).scalar_one()
        return {"protected_active": running, "protected_export": exported}

    def preview(self, owner, scope="all_completed", policy=None):
        if scope not in {"expired", "all_completed"}:
            raise ValueError("invalid cleanup scope")
        self._recover()
        cutoff = utc_now()
        retention, maximum = None, None
        if scope == "expired":
            policy = policy or {}
            retention = retention_cutoff(policy.get("retention_value", 30), policy.get("retention_unit", "day"))
            maximum = policy.get("max_records", 200)
            if type(maximum) is not int or maximum < 1:
                raise ValueError("invalid record limit")
        with self.repository.transaction() as (session, _state):
            eligible = session.execute(select(func.count()).select_from(TraceRow).where(*self._eligible(cutoff))).scalar_one()
            total = session.execute(select(func.count()).select_from(TraceRow)).scalar_one()
            protections = self._protection_counts(session)
            if retention is not None:
                old = session.execute(select(func.count()).select_from(TraceRow).where(
                    *self._eligible(cutoff), TraceRow.STARTED_AT < retention)).scalar_one()
                eligible = old + min(max(total-old-maximum, 0), eligible-old)
        payload = {"owner": str(owner), "scope": scope, "cutoff": cutoff,
                   "expires": time.time()+300, "nonce": uuid.uuid4().hex}
        if retention is not None:
            payload.update(retention_cutoff=retention, max_records=maximum)
        body = base64.urlsafe_b64encode(encode(payload).encode()).decode().rstrip("=")
        signature = hmac.new(self._secret(), body.encode(), hashlib.sha256).hexdigest()
        return {"scope": scope, "finished_before": cutoff, "eligible": eligible, **protections,
                "protected": total-eligible, "confirmation_token": body+"."+signature, "expires_at": payload["expires"]}

    def _confirmation(self, token, owner):
        if not isinstance(token, str) or len(token) > 2048:
            raise CleanupConfirmationError("invalid confirmation token")
        try:
            body, signature = token.split(".")
            expected = hmac.new(self._secret(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            if (payload["owner"] != str(owner) or payload["scope"] not in {"all_completed", "expired"}
                    or payload["expires"] <= time.time()):
                raise ValueError
            datetime.strptime(payload["cutoff"], "%Y-%m-%dT%H:%M:%S.%fZ")
            return payload
        except (ValueError, KeyError, TypeError):
            raise CleanupConfirmationError("confirmation is invalid or expired") from None

    def submit(self, token, owner, idempotency_key, *, scope="all_completed", background=True):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ValueError("idempotency key required")
        job_id = hashlib.sha256((str(owner)+":"+idempotency_key).encode()).hexdigest()
        fingerprint = hashlib.sha256(str(token).encode()).hexdigest()
        existing = self.repository.get_task(job_id, owner)
        if existing:
            if existing["control"].get("idempotency_hash") != fingerprint or existing["control"].get("scope") != scope:
                raise CleanupConflict("idempotency key was used for a different confirmation")
            return {"job_id": job_id, "status": existing["state"], "accepted": True}
        payload = self._confirmation(token, owner)
        if payload["scope"] != scope:
            raise ValueError("cleanup scope differs from confirmation")
        self._recover()
        with self.repository.transaction() as (session, state):
            existing_row = session.get(TaskRow, job_id)
            if existing_row is not None:
                if json.loads(existing_row.CONTROL).get("idempotency_hash") != fingerprint:
                    raise CleanupConflict("idempotency key conflict")
                return {"job_id": job_id, "status": existing_row.STATE, "accepted": True}
            self._claim(state, job_id)
            if scope == "all_completed":
                state.CLEAR_FINISHED_BEFORE = max(state.CLEAR_FINISHED_BEFORE or "", payload["cutoff"])
                # 不含请求正文的证据，用于证明此所有者在清理水位时
                # 仍在运行。之后发生的崩溃属于新的中断，不是
                # 应被抑制的旧已完成暂存记录。
                from app.media.recognition.admission import process_alive
                active = session.execute(select(TaskRow).where(TaskRow.KIND == "recognition_async",
                    TaskRow.STATE.in_(ACTIVE_TASKS), TaskRow.LEASE_UNTIL > time.time())).scalars()
                for task in active:
                    metadata = json.loads(task.CONTROL or "{}")
                    if process_alive(metadata.get("owner_pid"), metadata.get("process_start")):
                        metadata["clear_protected_at"] = state.CLEAR_FINISHED_BEFORE
                        task.CONTROL = encode(metadata)
            state.VERSION += 1
            control = {"scope": scope, "cutoff": payload["cutoff"], "deleted": 0,
                       "deleted_traces": 0, "deleted_events": 0, "deleted_candidates": 0,
                       "idempotency_hash": fingerprint, "skipped": 0, "remaining": 0}
            if scope == "expired":
                control.update(retention_cutoff=payload["retention_cutoff"], max_records=payload["max_records"])
            session.add(TaskRow(TASK_ID=job_id, OWNER_ID=str(owner), KIND="cleanup", STATE="QUEUED",
                LEASE_UNTIL=time.time()+30, CREATED_AT=utc_now(), CONTROL=encode(control)))
        if background:
            self._executor.submit(self.run_job, job_id, owner)
        else:
            self.run_job(job_id, owner)
        return {"job_id": job_id, "status": "QUEUED", "accepted": True}

    def submit_all(self, token, owner, idempotency_key, **options):
        return self.submit(token, owner, idempotency_key, scope="all_completed", **options)

    def resume(self, job_id, owner, *, background=True):
        with self.repository.transaction() as (session, state):
            job = session.get(TaskRow, job_id)
            if job is None or job.OWNER_ID != str(owner) or job.KIND != "cleanup":
                raise ValueError("cleanup job not found")
            if job.STATE == "SUCCEEDED":
                return {"job_id": job_id, "status": job.STATE}
            if job.STATE in {"QUEUED", "RUNNING"} and job.LEASE_UNTIL > time.time():
                raise CleanupBusy("cleanup job is still active")
            self._claim(state, job_id)
            job.STATE, job.LEASE_UNTIL = "QUEUED", time.time()+30
            control = json.loads(job.CONTROL)
            control.pop("error_code", None)
            job.CONTROL = encode(control)
        if background:
            self._executor.submit(self.run_job, job_id, owner)
        else:
            self.run_job(job_id, owner)
        return {"job_id": job_id, "status": "QUEUED"}

    @staticmethod
    def _delete(session, request_ids, counters=None):
        if not request_ids:
            return 0
        for model, name in ((CandidateRow, "deleted_candidates"), (EventRow, "deleted_events"), (TraceRow, "deleted_traces")):
            if counters is not None:
                count = session.execute(select(func.count()).select_from(model).where(model.REQUEST_ID.in_(request_ids))).scalar_one()
                counters[name] = counters.get(name, 0)+count
            session.execute(delete(model).where(model.REQUEST_ID.in_(request_ids)))
        return len(request_ids)

    def _batch(self, session, cutoff, *, retention=None, limit=None, counters=None):
        query = select(TraceRow.REQUEST_ID).where(*self._eligible(cutoff))
        if retention is not None:
            query = query.where(TraceRow.STARTED_AT < retention)
        ids = session.execute(query.order_by(TraceRow.STARTED_AT, TraceRow.REQUEST_ID).limit(
            min(self.batch_size, limit) if limit is not None else self.batch_size)).scalars().all()
        return self._delete(session, ids, counters)

    def run_job(self, job_id, owner):
        try:
            self._recover()
            while True:
                with self.repository.transaction() as (session, state):
                    job = session.get(TaskRow, job_id)
                    if job is None or job.OWNER_ID != str(owner):
                        raise ValueError("cleanup job not found")
                    if job.STATE == "SUCCEEDED":
                        return json.loads(job.CONTROL)
                    self._claim(state, job_id)
                    job.STATE, job.LEASE_UNTIL = "RUNNING", time.time()+30
                    control = json.loads(job.CONTROL)
                    deleted = self._batch(session, control["cutoff"], retention=control.get("retention_cutoff"), counters=control)
                    if not deleted and control["scope"] == "expired":
                        total = session.execute(select(func.count()).select_from(TraceRow)).scalar_one()
                        deleted = self._batch(session, control["cutoff"], limit=total-control["max_records"], counters=control) if total > control["max_records"] else 0
                    control["deleted"] += deleted
                    control.update(self._protection_counts(session))
                    if not deleted:
                        control["remaining"] = session.execute(select(func.count()).select_from(TraceRow)).scalar_one()
                        control["skipped"] = control["remaining"]
                        job.STATE, job.LEASE_UNTIL = "SUCCEEDED", 0
                        state.CLEANUP_OWNER, state.CLEANUP_UNTIL = None, 0
                    job.CONTROL = encode(control)
                if not deleted:
                    return control
        except Exception as error:
            with self.repository.transaction() as (session, state):
                job = session.get(TaskRow, job_id)
                if job is not None:
                    control = json.loads(job.CONTROL)
                    control["error_code"] = type(error).__name__
                    job.STATE, job.LEASE_UNTIL, job.CONTROL = "FAILED", 0, encode(control)
                if state.CLEANUP_OWNER == job_id:
                    state.CLEANUP_OWNER, state.CLEANUP_UNTIL = None, 0
            return None

    def expired(self, *, retention_value=30, retention_unit="day", max_records=200, now=None):
        if type(max_records) is not int or max_records < 1:
            raise ValueError("max_records must be positive")
        retention = retention_cutoff(retention_value, retention_unit, now)
        self._recover()
        owner, cutoff, deleted = uuid.uuid4().hex, utc_now(), 0
        try:
            while True:
                with self.repository.transaction() as (session, state):
                    self._claim(state, owner)
                    batch = self._batch(session, cutoff, retention=retention)
                    if not batch:
                        total = session.execute(select(func.count()).select_from(TraceRow)).scalar_one()
                        batch = self._batch(session, cutoff, limit=total-max_records) if total > max_records else 0
                    deleted += batch
                if not batch:
                    return deleted
        finally:
            with self.repository.transaction() as (_session, state):
                if state.CLEANUP_OWNER == owner:
                    state.CLEANUP_OWNER, state.CLEANUP_UNTIL = None, 0
