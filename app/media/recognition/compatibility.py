"""旧版 API 结构只读取 V3 历史记录，绝不访问旧表。"""

from datetime import datetime, timezone
import json
import os
import tempfile
from types import SimpleNamespace
from enum import Enum

from sqlalchemy import select, func, or_, and_, delete
from sqlalchemy.orm import Session

from app.db.models import RECOGNITIONTRACE as TraceRow, RECOGNITIONEVENT as EventRow, RECOGNITIONCANDIDATE as CandidateRow
from app.media.recognition.repository import RecognitionRepository, TERMINAL, utc_now, encode
from app.media.recognition.query_service import JSONField, HistoryQuery
from app.media.recognition.trace import redact


def timestamp(value):
    if not value:
        return utc_now()
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    # 历史界面日期使用本地时间；新记录器生成的日期包含 UTC。
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def summary(payload):
    """构建精简的列表视图投影，不包含原始输入或响应。"""
    overall = payload.get("overall_result") or {}

    def tmdb_identity(value):
        if not isinstance(value, dict):
            return None
        media_type = value.get("media_type")
        if isinstance(media_type, Enum):
            media_type = media_type.value
        return {"id": value.get("id"), "media_type": media_type,
                "title": value.get("title") or value.get("name")}

    summary = {
        "overall_result": {
            "status": overall.get("status"),
            "reason": overall.get("reason"),
            "business_result": overall.get("business_result"),
            "selected_provider": overall.get("selected_provider"),
            "elapsed_ms": overall.get("elapsed_ms"),
            "tmdb_result": tmdb_identity(overall.get("tmdb_result")),
        },
        "provider_results": [{
            key: item.get(key) for key in (
                "attempt_id", "provider_id", "status", "normalized_result",
                "elapsed_ms", "error")
        } for item in (payload.get("provider_results") or [])],
        "tmdb_results": [{
            "provider_id": item.get("provider_id"),
            "status": item.get("status"),
            "result": tmdb_identity(item.get("result")),
        } for item in (payload.get("tmdb_results") or [])],
        "actions": [{key: item.get(key) for key in ("sequence", "action_type", "status")}
                    for item in (payload.get("actions") or [])],
    }
    if "candidate_similarities" in overall:
        summary["overall_result"]["selection_method"] = overall.get("selection_method")
        summary["overall_result"]["candidate_similarities"] = [{
            key: item.get(key) for key in (
                "id", "media_type", "title", "provider_ids", "similarity_score",
                "comparison_group", "retained", "selected")
        } for item in overall["candidate_similarities"]]
    return summary


def bundle(payload):
    """将实时旧版算法报告转换为当前暂存格式。"""
    from app.media.recognition.records import _recognition_generation, recognition_config, json_safe
    payload = redact(json_safe(payload))
    overall = payload.get("overall_result") or {}
    lower = str(overall.get("status") or "failed").lower()
    running = lower == "running" or overall.get("lifecycle") == "running" or payload.get("in_progress") is True
    kind = "RUNNING" if running else {"success": "PARSED", "skipped": "SKIPPED", "cancelled": "CANCELLED"}.get(lower, "FAILED")
    result = overall.get("tmdb_result") or {}
    identity = result.get("id")
    media_type = result.get("media_type")
    media_type = {"电视剧": "tv", "动漫": "tv", "anime": "tv", "电影": "movie"}.get(media_type, media_type)
    if isinstance(identity, str) and identity.isdecimal():
        identity = int(identity)
    if not running and lower == "success" and type(identity) is int and identity > 0 and media_type in {"movie", "tv"}:
        kind = "MATCHED"
    reason = overall.get("reason")
    if kind == "FAILED" and reason and ("timeout" in reason or "deadline" in reason):
        kind = "TIMEOUT"
    if kind == "FAILED" and reason in {"ambiguous_tmdb", "insufficient_evidence", "review_required"}:
        kind = "REVIEW_REQUIRED"
    mode = payload.get("record_mode") or (recognition_config("recognition").get("records") or {}).get("mode", "failed")
    if mode not in {"all", "failed", "none"}:
        mode = "failed"
    request_id = str(payload["request_id"])
    created = timestamp(payload.get("created_at"))
    events = []
    for sequence, action in enumerate(payload.get("actions") or [], 1):
        events.append({"schema_version": 3, "request_id": request_id, "seq": sequence,
            "node_id": "legacy/"+str(action.get("action_type", "action")), "event_type": str(action.get("action_type", "action")),
            "status": action.get("status", "unknown"), "offset_ns": int(action.get("offset_ns", sequence)),
            "emitted_at_utc": timestamp(action.get("time") or created), "durability": "persistent" if mode == "all" else "temporary",
            "payload": {"legacy_action": action, "provider_id": action.get("provider_id")}})
    compatibility = {key: payload.get(key) for key in ("stage", "context", "provider_results", "tmdb_results", "overall_result")}
    compatibility["summary"] = summary(payload)
    outcome = {"request_id": request_id, "status": kind, "failure_code": reason,
        "duration_ms": overall.get("elapsed_ms"), "result": {"media_type": media_type, "tmdb_id": identity,
            "title": result.get("title") or result.get("name")} if kind == "MATCHED" else None,
        "compatibility": compatibility}
    if "llm_usage" in overall:
        outcome["llm_usage"] = overall["llm_usage"]
    trace = {"request_id": request_id, "started_at": created, "started_monotonic_ns": int(payload.get("started_monotonic_ns", 0)),
        "finished_at": None if running else timestamp(payload.get("finished_at")), "mode": mode, "status": kind,
        "source": payload.get("source") or "unknown", "input": {"title": payload.get("original_name"),
            "stage": payload.get("stage", "resolve"), "actor_id": str(payload.get("actor_id", "0"))}, "outcome": outcome}
    context = payload.get("context") or {}
    if isinstance(context.get("title"), str) and context["title"].strip():
        trace["input"]["title"] = context["title"]
    for key in ("subtitle", "name", "cache", "force", "chinese"):
        if context.get(key) is not None:
            trace["input"][key] = context[key]
    for key in ("year", "season"):
        value = context.get(key)
        if type(value) is int or isinstance(value, str) and value.isdecimal():
            trace["input"][key] = int(value)
    media_hint = context.get("mtype") or context.get("media_type")
    media_hint = {"电视剧": "tv", "动漫": "anime", "电影": "movie", "未知": "unknown"}.get(media_hint,media_hint)
    if media_hint in {"movie", "tv", "anime", "unknown"}:
        trace["input"]["mtype"] = media_hint
    return {"schema_version": 3, "generation": _recognition_generation(), "updated_at": utc_now(),
        "owner_pid": os.getpid(), "owner_id": f"pid:{os.getpid()}", "trace": trace, "events": events, "candidates": []}


def retained(value):
    trace = value["trace"]
    return trace["mode"] == "all" or trace["mode"] == "failed" and trace["status"] in {"FAILED", "TIMEOUT", "REVIEW_REQUIRED"}


def legacy_payload(trace):
    """为旧版算法轨迹和原生 V3 轨迹提供统一的详情投影。"""
    outcome = trace.get("outcome") or {}
    compatibility = outcome.get("compatibility") or {}
    actions = []
    for event in trace.get("events", []):
        body = event.get("payload") or {}
        actions.append(body.get("legacy_action") or {"action_id": f"{trace['request_id']}:{event['seq']}",
            "sequence": event["seq"], "action_type": event.get("event_type"), "status": event.get("status"),
            "input": body.get("input", {}), "output": body, "time": event.get("emitted_at_utc")})
    result = outcome.get("result") or {}
    overall = compatibility.get("overall_result") or {"status": {"MATCHED": "success", "PARSED": "success",
        "SKIPPED": "skipped", "CANCELLED": "cancelled", "RUNNING": "running", "QUEUED": "running"}.get(trace["status"], "failed"),
        "reason": outcome.get("failure_code"), "elapsed_ms": outcome.get("duration_ms"),
        "tmdb_result": {"id": result.get("tmdb_id"), "media_type": result.get("media_type"), "title": result.get("title")} if trace["status"] == "MATCHED" else None}
    if "llm_usage" in outcome:
        overall = {**overall, "llm_usage": outcome["llm_usage"]}
    return {"schema_version": 3, "request_id": trace["request_id"], "original_name": trace["input"].get("title"),
        "source": trace["source"], "stage": trace["input"].get("stage", "resolve"), "created_at": trace["started_at"],
        "context": compatibility.get("context") or {}, "actions": actions,
        "provider_results": compatibility.get("provider_results") or [], "tmdb_results": compatibility.get("tmdb_results") or [],
        "overall_result": overall, "trace": trace}


def record_view(payload, compact=False):
    row = {"REQUEST_ID": payload["request_id"], "ORIGINAL_NAME": payload.get("original_name"), "SOURCE": payload.get("source"),
        "STAGE": payload.get("stage"), "CREATED_AT": payload.get("created_at"), "SUMMARY": encode(summary(payload))}
    if not compact:
        for key in ("context", "actions", "provider_results", "overall_result", "tmdb_results"):
            row[key.upper()] = encode(payload.get(key))
    return SimpleNamespace(**row)


class LegacyHistory:
    def __init__(self, repository=None):
        self.repository = repository or RecognitionRepository()

    def insert(self, payload):
        value = bundle(payload)
        if not retained(value):
            return False
        return payload["request_id"] if self.repository.save(value["trace"], value["events"], value["candidates"]) else False

    def conditions(self, actor="0", **filters):
        conditions = HistoryQuery.permission(actor)
        # 原生界面和旧版导出对这些新筛选条件使用相同判断，
        # 因此导出筛选列表时不会悄悄扩大范围。
        extra = {key: filters[key] for key in ("type", "cache_hit", "duration_min_ms", "duration_max_ms") if key in filters}
        if extra:
            conditions += HistoryQuery(self.repository).conditions(actor, extra)
        def field(*path):
            return JSONField(TraceRow.OUTCOME, "compatibility", *path)
        if filters.get("title"):
            conditions.append(func.lower(JSONField(TraceRow.INPUT, "title")).contains(filters["title"].lower(), autoescape=True))
        if filters.get("source"):
            conditions.append(TraceRow.SOURCE == filters["source"])
        if filters.get("status"):
            status = filters["status"].lower()
            native = {"success": ["MATCHED", "PARSED"], "failed": ["FAILED", "TIMEOUT", "REVIEW_REQUIRED"],
                "skipped": ["SKIPPED"], "cancelled": ["CANCELLED"], "running": ["RUNNING", "QUEUED"]}.get(status, [status.upper()])
            conditions.append(or_(TraceRow.STATUS.in_(native), func.lower(field("overall_result", "status")) == status,
                func.lower(field("overall_result", "business_result", "status")) == status))
        if filters.get("reason"):
            reason = filters["reason"]
            conditions.append(or_(JSONField(TraceRow.OUTCOME, "failure_code") == reason,
                field("overall_result", "reason") == reason, field("overall_result", "business_result", "reason") == reason))
        for key, column, operator in (("created_from", TraceRow.STARTED_AT, "ge"), ("created_to", TraceRow.STARTED_AT, "le"), ("snapshot_at", TraceRow.STARTED_AT, "le")):
            if filters.get(key):
                value = timestamp(filters[key])
                conditions.append(column >= value if operator == "ge" else column <= value)
        if filters.get("before_cursor"):
            created, identity = filters["before_cursor"]
            created = timestamp(created)
            conditions.append(or_(TraceRow.STARTED_AT < created, and_(TraceRow.STARTED_AT == created, TraceRow.REQUEST_ID < identity)))
        for key in ("provider_id", "action_type"):
            if filters.get(key):
                column = JSONField(EventRow.PAYLOAD, "payload", "provider_id") if key == "provider_id" else func.lower(EventRow.NODE_ID)
                value = str(filters[key]) if key == "provider_id" else "legacy/"+str(filters[key]).lower()
                native = EventRow.NODE_ID == value.removeprefix("legacy/") if key == "action_type" else column == value
                conditions.append(select(EventRow.SEQ).where(EventRow.REQUEST_ID == TraceRow.REQUEST_ID, or_(column == value, native)).exists())
        return conditions

    def listing(self, *, actor="0", page=1, page_size=20, include_total=True, **filters):
        conditions = self.conditions(actor, **filters)
        with self.repository.transaction(lock=False) as (session, _):
            total = session.execute(select(func.count()).select_from(TraceRow).where(*conditions)).scalar_one() if include_total else None
            rows = session.execute(select(TraceRow.REQUEST_ID, TraceRow.STARTED_AT, TraceRow.SOURCE, TraceRow.STATUS,
                JSONField(TraceRow.INPUT, "title"), JSONField(TraceRow.INPUT, "stage"),
                JSONField(TraceRow.OUTCOME, "compatibility", "summary"), JSONField(TraceRow.OUTCOME, "failure_code"),
                JSONField(TraceRow.OUTCOME, "result", "media_type"), JSONField(TraceRow.OUTCOME, "result", "tmdb_id"),
                JSONField(TraceRow.OUTCOME, "result", "title"), JSONField(TraceRow.OUTCOME, "duration_ms"))
                .where(*conditions).order_by(TraceRow.STARTED_AT.desc(), TraceRow.REQUEST_ID.desc())
                .offset((max(1,int(page))-1)*min(100,int(page_size))).limit(min(100,int(page_size)))).all()
        items = []
        for row in rows:
            value = json.loads(row[6]) if isinstance(row[6], str) else row[6]
            if not value:
                payload = legacy_payload({"request_id": row[0], "started_at": row[1], "source": row[2], "status": row[3],
                    "input": {"title": row[4], "stage": row[5]}, "outcome": {"failure_code": row[7],
                    "result": {"media_type": row[8], "tmdb_id": row[9], "title": row[10]}, "duration_ms": row[11]}})
                value = summary(payload)
            items.append(SimpleNamespace(REQUEST_ID=row[0], CREATED_AT=row[1], SOURCE=row[2], ORIGINAL_NAME=row[4], STAGE=row[5] or "resolve", SUMMARY=encode(value)))
        return total, items

    def detail(self, request_id, actor="0"):
        # 必须先完成授权，再读取完整事件和候选项正文。
        if HistoryQuery(self.repository).detail(request_id, actor) is None:
            return None, []
        trace = self.repository.get(request_id)
        if trace is None:
            return None, []
        payload = legacy_payload(trace)
        attempts = [SimpleNamespace(ATTEMPT_ID=str(item.get("attempt_id")), PROVIDER_ID=str(item.get("provider_id")),
            STATUS=item.get("status"), INPUT=encode(item.get("input")), RAW_RESULT=encode(item.get("raw_result")),
            NORMALIZED_RESULT=encode(item.get("normalized_result")), TMDB_RESULTS=encode(item.get("tmdb_results")),
            ERROR=item.get("error"), ELAPSED_MS=item.get("elapsed_ms")) for item in payload["provider_results"]]
        return record_view(payload), attempts

    def append(self, request_id, action_type, reason, details=None, business_status="skipped"):
        with self.repository.transaction() as (session, _state):
            row = session.get(TraceRow, str(request_id))
            if row is None:
                return False
            outcome = json.loads(row.OUTCOME or "{}")
            if not outcome.get("compatibility"):
                projected = legacy_payload({"request_id": row.REQUEST_ID, "source": row.SOURCE,
                    "started_at": row.STARTED_AT, "status": row.STATUS, "input": json.loads(row.INPUT), "outcome": outcome})
                outcome["compatibility"] = {key: projected[key] for key in ("stage", "context", "provider_results", "tmdb_results", "overall_result")}
            compatibility = outcome["compatibility"]
            overall = compatibility.setdefault("overall_result", {})
            overall["business_result"] = {"status": business_status, "reason": reason, "details": redact(details or {})}
            attempts = compatibility.get("provider_results") or []
            attempt_id = None
            for attempt in reversed(attempts):
                if attempt.get("provider_id") == "anitopy_ml" and attempt.get("status") == "skipped":
                    attempt_id = attempt.get("attempt_id")
                    if str(attempt.get("error") or "").startswith("ai_deferred"):
                        attempt["error"] = reason
                    break
            seq = (session.execute(select(func.max(EventRow.SEQ)).where(EventRow.REQUEST_ID == row.REQUEST_ID)).scalar_one() or 0)+1
            previous_event = session.execute(select(EventRow.PAYLOAD).where(EventRow.REQUEST_ID == row.REQUEST_ID, EventRow.SEQ == seq-1)).scalar_one_or_none()
            offset = json.loads(previous_event).get("offset_ns", 0) if previous_event else 0
            action = {"action_id": f"{request_id}:{seq}", "sequence": seq, "action_type": action_type,
                "provider_id": "anitopy_ml", "attempt_id": attempt_id, "status": business_status, "input": {}, "output": redact(details or {}), "reason": reason, "time": utc_now()}
            session.add(EventRow(REQUEST_ID=row.REQUEST_ID, SEQ=seq, NODE_ID="legacy/"+action_type, STATUS=business_status,
                PAYLOAD=encode({"schema_version": 3, "request_id": row.REQUEST_ID, "seq": seq, "node_id": "legacy/"+action_type,
                    "event_type": action_type, "status": business_status, "emitted_at_utc": utc_now(), "offset_ns": offset,
                    "durability": "persistent", "payload": {"legacy_action": action, "provider_id": "anitopy_ml", "after_completion": row.STATUS in TERMINAL}})))
            previous = compatibility.get("summary") or summary({"overall_result": overall})
            previous["overall_result"]["business_result"] = overall["business_result"]
            previous["provider_results"] = summary({"provider_results": attempts})["provider_results"]
            previous.setdefault("actions", []).append({key: action[key] for key in ("sequence", "action_type", "status")})
            compatibility["summary"] = previous
            row.OUTCOME, row.VERSION = encode(redact(outcome)), row.VERSION+1
            return True

    def export(self, *, actor="0", page_size=100, **filters):
        """开始 HTTP 流式传输前先生成一致的读取快照。"""
        from app.utils.storage_jobs import STORAGE_JOBS
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as output:
            with STORAGE_JOBS.maintenance_lock(exclusive=False):
                if STORAGE_JOBS.maintenance_gate("database"):
                    raise RuntimeError("database maintenance in progress")
                engine = self.repository.manager.engine
                with engine.connect() as connection:
                    if engine.dialect.name in {"postgresql", "mysql", "mariadb"}:
                        connection = connection.execution_options(isolation_level="REPEATABLE READ")
                    with connection.begin(), Session(bind=connection) as session:
                        driver = getattr(connection.connection, "driver_connection", None)
                        if engine.dialect.name == "sqlite" and driver is not None and not driver.in_transaction:
                            connection.exec_driver_sql("BEGIN")
                        conditions = self.conditions(actor, **filters)
                        cursor = None
                        while True:
                            query = select(TraceRow).where(*conditions)
                            if cursor:
                                query = query.where(or_(TraceRow.STARTED_AT < cursor[0], and_(TraceRow.STARTED_AT == cursor[0], TraceRow.REQUEST_ID < cursor[1])))
                            rows = session.execute(query.order_by(TraceRow.STARTED_AT.desc(), TraceRow.REQUEST_ID.desc()).limit(min(100,page_size))).scalars().all()
                            if not rows:
                                break
                            identities = [row.REQUEST_ID for row in rows]
                            events, candidates = {}, {}
                            for identity, value in session.execute(select(EventRow.REQUEST_ID, EventRow.PAYLOAD).where(
                                    EventRow.REQUEST_ID.in_(identities)).order_by(EventRow.REQUEST_ID, EventRow.SEQ)):
                                events.setdefault(identity, []).append(json.loads(value))
                            for identity, value in session.execute(select(CandidateRow.REQUEST_ID, CandidateRow.PAYLOAD).where(
                                    CandidateRow.REQUEST_ID.in_(identities)).order_by(CandidateRow.REQUEST_ID, CandidateRow.RANK, CandidateRow.TMDB_ID)):
                                candidates.setdefault(identity, []).append(json.loads(value))
                            for row in rows:
                                trace = {"request_id": row.REQUEST_ID, "started_at": row.STARTED_AT, "finished_at": row.FINISHED_AT,
                                    "status": row.STATUS, "mode": row.RECORD_MODE, "source": row.SOURCE, "version": row.VERSION,
                                    "input": json.loads(row.INPUT), "outcome": json.loads(row.OUTCOME),
                                    "events": events.get(row.REQUEST_ID, []), "candidates": candidates.get(row.REQUEST_ID, [])}
                                output.write(json.dumps(legacy_payload(trace), ensure_ascii=False, separators=(",", ":"))+"\n")
                            cursor = rows[-1].STARTED_AT, rows[-1].REQUEST_ID
                            session.expunge_all()
            output.seek(0)
            yield from output

    def archive(self, older_than, archive_path, delete_archived=False, batch_size=500):
        if type(delete_archived) is not bool:
            raise ValueError("delete_archived must be boolean")
        directory = os.path.dirname(os.path.abspath(archive_path))
        descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".recognition-archive-", suffix=".tmp")
        count = deleted = 0
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                for line in self.export(created_to=older_than):
                    output.write(line)
                    count += 1
                output.flush()
                os.fsync(output.fileno())
            if not count:
                return {"archived_count": 0, "deleted_count": 0, "archive_path": None}
            os.link(temporary, archive_path)
            if delete_archived:
                with open(archive_path, encoding="utf-8") as source:
                    for line in source:
                        item = json.loads(line)
                        with self.repository.transaction() as (session, _):
                            row = session.get(TraceRow, item["request_id"])
                            if row is None or row.STATUS not in TERMINAL or row.VERSION != item["trace"]["version"] or self.repository._protected(session,row):
                                continue
                            for model in (CandidateRow, EventRow, TraceRow):
                                session.execute(delete(model).where(model.REQUEST_ID==row.REQUEST_ID))
                            deleted += 1
            return {"archived_count": count, "deleted_count": deleted, "archive_path": archive_path}
        finally:
            os.unlink(temporary)
