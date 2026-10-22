"""分页读取 V3 历史投影。列表和统计中不包含事件或候选项正文。"""

from datetime import datetime, timedelta, timezone
import json
import math

from sqlalchemy import String, Float, BigInteger, cast, func, select, case
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

from app.db.models import RECOGNITIONTRACE as TraceRow, RECOGNITIONEVENT as EventRow, RECOGNITIONCANDIDATE as CandidateRow
from app.media.recognition.repository import RecognitionRepository, TERMINAL
from app.media.recognition.llm_usage import TOKEN_FIELDS


class JSONField(FunctionElement):
    type = String()
    inherit_cache = False

    def __init__(self, column, *path):
        self.path = path
        super().__init__(column)


@compiles(JSONField)
def _json_default(element, compiler, **options):
    column = compiler.process(list(element.clauses)[0], **options)
    path = compiler.render_literal_value("$."+".".join(element.path), String())
    return f"json_extract({column}, {path})"


@compiles(JSONField, "postgresql")
def _json_postgresql(element, compiler, **options):
    column = compiler.process(list(element.clauses)[0], **options)
    path = compiler.render_literal_value("{"+",".join(element.path)+"}", String())
    return f"(CAST({column} AS JSONB) #>> {path})"


@compiles(JSONField, "mysql")
@compiles(JSONField, "mariadb")
def _json_mysql(element, compiler, **options):
    column = compiler.process(list(element.clauses)[0], **options)
    path = compiler.render_literal_value("$."+".".join(element.path), String())
    return f"NULLIF(JSON_UNQUOTE(JSON_EXTRACT({column}, {path})), 'null')"


def date_value(value, end=False):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid_date")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if end and len(value) == 10:
        parsed += timedelta(days=1)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class HistoryQuery:
    def __init__(self, repository=None):
        self.repository = repository or RecognitionRepository()

    @staticmethod
    def permission(actor):
        return [] if str(actor) == "0" else [JSONField(TraceRow.INPUT, "actor_id") == str(actor)]

    def conditions(self, actor, filters=None):
        filters = filters or {}
        result = self.permission(actor)
        legacy_filters = {key: filters[key] for key in ("source", "provider_id", "action_type", "reason") if filters.get(key)}
        if legacy_filters:
            from app.media.recognition.compatibility import LegacyHistory
            result += LegacyHistory(self.repository).conditions(actor, **legacy_filters)
        if filters.get("title"):
            result.append(func.lower(JSONField(TraceRow.INPUT, "title")).contains(filters["title"].lower(), autoescape=True))
        if filters.get("status"):
            if filters["status"] not in TERMINAL | {"RUNNING", "QUEUED"}:
                raise ValueError("invalid_status")
            result.append(TraceRow.STATUS == filters["status"])
        if filters.get("type"):
            if filters["type"] not in {"movie", "tv"}:
                raise ValueError("invalid_media_type")
            result.append(JSONField(TraceRow.OUTCOME, "result", "media_type") == filters["type"])
        if "cache_hit" in filters:
            source = JSONField(TraceRow.OUTCOME, "cache_source")
            result.append(source.is_not(None) if filters["cache_hit"] else source.is_(None))
        duration = cast(JSONField(TraceRow.OUTCOME, "duration_ms"), Float)
        for field, compare in (("duration_min_ms", duration.__ge__), ("duration_max_ms", duration.__le__)):
            if field in filters:
                value = filters[field]
                if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                    raise ValueError("invalid_duration")
                result.append(compare(value))
        if "date_from" in filters:
            result.append(TraceRow.STARTED_AT >= date_value(filters["date_from"]))
        if "date_to" in filters:
            result.append(TraceRow.STARTED_AT < date_value(filters["date_to"], end=True))
        return result

    def listing(self, actor, page=1, page_size=50, filters=None):
        conditions = self.conditions(actor, filters)
        fields = {"title": JSONField(TraceRow.INPUT, "title"), "media_type": JSONField(TraceRow.OUTCOME, "result", "media_type"),
            "result_title": JSONField(TraceRow.OUTCOME, "result", "title"),
            "tmdb_id": JSONField(TraceRow.OUTCOME, "result", "tmdb_id"), "score": JSONField(TraceRow.OUTCOME, "score"),
            "duration_ms": JSONField(TraceRow.OUTCOME, "duration_ms"), "cache_source": JSONField(TraceRow.OUTCOME, "cache_source"),
            "failure_code": JSONField(TraceRow.OUTCOME, "failure_code"),
            "source_trace_id": JSONField(TraceRow.OUTCOME, "result", "source_trace_id"),
            "llm_usage": JSONField(TraceRow.OUTCOME, "llm_usage")}
        with self.repository.transaction(lock=False) as (session, _state):
            total = session.execute(select(func.count()).select_from(TraceRow).where(*conditions)).scalar_one()
            rows = session.execute(select(TraceRow.REQUEST_ID.label("request_id"), TraceRow.STARTED_AT.label("started_at"),
                TraceRow.FINISHED_AT.label("finished_at"), TraceRow.STATUS.label("status"), TraceRow.RECORD_MODE.label("record_mode"),
                TraceRow.SOURCE.label("source"), *(value.label(key) for key, value in fields.items())).where(*conditions)
                .order_by(TraceRow.STARTED_AT.desc(), TraceRow.REQUEST_ID.desc()).offset((page-1)*page_size).limit(page_size)).mappings()
            items = [dict(row) for row in rows]
        for item in items:
            item["llm_usage"] = json.loads(item["llm_usage"]) if item["llm_usage"] is not None else None
            for field in ("score", "duration_ms"):
                if item[field] is not None:
                    item[field] = float(item[field])
            identity = item["tmdb_id"]
            identity = int(identity) if isinstance(identity, str) and identity.isdecimal() else identity
            item["tmdb_id"] = identity
            item["tmdb_url"] = (f"https://www.themoviedb.org/{item['media_type']}/{identity}"
                if item["status"] == "MATCHED" and item["media_type"] in {"movie", "tv"} and type(identity) is int and identity > 0 else None)
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def detail(self, request_id, actor):
        maximum = select(func.max(EventRow.SEQ)).where(EventRow.REQUEST_ID == TraceRow.REQUEST_ID).correlate(TraceRow).scalar_subquery()
        with self.repository.transaction(lock=False) as (session, _state):
            row = session.execute(select(TraceRow.REQUEST_ID, TraceRow.STARTED_AT, TraceRow.FINISHED_AT, TraceRow.STATUS,
                TraceRow.RECORD_MODE, TraceRow.SOURCE, TraceRow.VERSION, TraceRow.INPUT, TraceRow.OUTCOME, maximum)
                .where(TraceRow.REQUEST_ID == request_id, *self.permission(actor))).first()
            if row is None:
                return None
            nodes = session.execute(select(EventRow.NODE_ID, EventRow.STATUS, EventRow.SEQ).where(
                EventRow.REQUEST_ID == request_id, EventRow.SEQ <= (row[9] or 0)).order_by(EventRow.SEQ)).all()
            states = {}
            for node_id, status, seq in nodes:
                states[node_id] = {"status": status, "seq": seq}
            return {"request_id": row[0], "started_at": row[1], "finished_at": row[2], "status": row[3],
                "retention": row[4], "source": row[5], "version": row[6], "input": json.loads(row[7]),
                "outcome": json.loads(row[8]), "last_seq": row[9] or 0, "nodes": states, "durability": "persistent"}

    def events(self, request_id, after_seq=0, limit=100):
        with self.repository.transaction(lock=False) as (session, _state):
            bounds = session.execute(select(func.min(EventRow.SEQ), func.max(EventRow.SEQ)).where(EventRow.REQUEST_ID == request_id)).one()
            rows = session.execute(select(EventRow.PAYLOAD).where(EventRow.REQUEST_ID == request_id, EventRow.SEQ > after_seq)
                .order_by(EventRow.SEQ).limit(limit+1)).scalars().all()
        items = [json.loads(value) for value in rows[:limit]]
        return {"events": items, "next_seq": items[-1]["seq"] if items else after_seq,
                "has_more": len(rows) > limit, "first_available_seq": bounds[0] or 0, "last_seq": bounds[1] or 0,
                "reset_required": bool(bounds[0] and after_seq < bounds[0]-1), "durability": "persistent"}

    def candidates(self, request_id, page=1, page_size=50):
        with self.repository.transaction(lock=False) as (session, _state):
            total = session.execute(select(func.count()).select_from(CandidateRow).where(CandidateRow.REQUEST_ID == request_id)).scalar_one()
            rows = session.execute(select(CandidateRow.PAYLOAD).where(CandidateRow.REQUEST_ID == request_id)
                .order_by(func.coalesce(CandidateRow.RANK, 2147483647), CandidateRow.MEDIA_TYPE, CandidateRow.TMDB_ID)
                .offset((page-1)*page_size).limit(page_size)).scalars().all()
        return {"items": [json.loads(value) for value in rows], "total": total, "page": page, "page_size": page_size}

    def stats(self, actor, mode, filters=None):
        conditions = [*self.conditions(actor, filters), TraceRow.STATUS.in_(TERMINAL-{"PARSED", "SKIPPED"}),
                      TraceRow.SOURCE.not_in({"cache_refresh", "recognition_refresh", "cache_invalidate"})]
        duration = cast(JSONField(TraceRow.OUTCOME, "duration_ms"), Float)
        with self.repository.transaction(lock=False) as (session, _state):
            counts = dict(session.execute(select(TraceRow.STATUS, func.count()).where(*conditions).group_by(TraceRow.STATUS)).all())
            total = sum(counts.values())
            durations = session.execute(select(func.count()).select_from(TraceRow).where(*conditions, duration.is_not(None))).scalar_one()
            p95 = session.execute(select(duration).where(*conditions, duration.is_not(None)).order_by(duration, TraceRow.REQUEST_ID)
                .offset(max(0, math.ceil(durations*.95)-1)).limit(1)).scalar_one_or_none() if mode == "all" and durations else None
            # 令牌覆盖率会纳入已保存的纯解析结果，并使用与
            # 列表和统计完全相同的归属判断与筛选条件。列表或统计时绝不读取事件正文。
            usage_conditions = [*self.conditions(actor, filters), TraceRow.STATUS.in_(TERMINAL),
                TraceRow.SOURCE.not_in({"cache_refresh", "recognition_refresh", "cache_invalidate"})]
            usage_columns = [func.sum(cast(JSONField(TraceRow.OUTCOME, "llm_usage", name), BigInteger)).label(name)
                for name in (*TOKEN_FIELDS, "api_calls", "unknown_calls")]
            complete = cast(JSONField(TraceRow.OUTCOME, "llm_usage", "usage_complete"), String).in_(("1", "true"))
            usage_row = session.execute(select(*usage_columns, func.count().label("samples"),
                func.sum(case((complete, 0), else_=1)).label("incomplete_records"))
                .select_from(TraceRow).where(*usage_conditions)).mappings().one()
            usage = {name: usage_row[name] if usage_row["samples"] else 0
                for name in (*TOKEN_FIELDS, "api_calls", "unknown_calls")}
            usage["usage_complete"] = not usage_row["incomplete_records"]
        decisions = sum(counts.get(key, 0) for key in ("MATCHED", "FAILED", "TIMEOUT", "REVIEW_REQUIRED"))
        return {"counts": counts, "saved_count": total, "record_mode": mode, "coverage": "saved_records",
                "population_complete": False, "metrics_scope": "saved_records_only",
                "success_rate": counts.get("MATCHED", 0)/decisions if mode == "all" and decisions else None,
                "p95_duration_ms": p95,
                "llm_usage": usage, "llm_usage_incomplete_records": usage_row["incomplete_records"] or 0,
                "coverage_description": "仅统计实际保留的识别记录；不包含未保留、已清理记录及缓存维护任务"}
