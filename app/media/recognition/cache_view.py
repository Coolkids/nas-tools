"""Read-only cache management views over backend-owned metadata."""

import time

from sqlalchemy import select
from app.db.models import RECOGNITIONTASK as TaskRow, RECOGNITIONTRACE as TraceRow
from app.media.recognition.cache_repository import LAYERS
from app.media.recognition.query_service import HistoryQuery
from app.media.recognition.refresh_worker import RefreshWorker
from app.media.recognition.repository import RecognitionRepository
from app.utils.cache_backend import STORE


class CacheView:
    def __init__(self, store=None, repository=None, settings=None):
        self.store, self.repository, self.settings = STORE if store is None else store, repository or RecognitionRepository(), settings

    def metadata(self, layer):
        now = time.time()
        rows = self.store.envelope_metadata("recognition_"+layer, now=now)
        ranks, offset, total = {}, 0, 0
        while True:
            snapshot = self.store.rank_snapshot("recognition_"+layer, now=now, offset=offset, limit=512)
            total = snapshot["total"]
            for item in snapshot["items"]:
                ranks[item["cache_key"]] = item
            offset += len(snapshot["items"])
            if not snapshot["items"] or offset >= total:
                break
        for row in rows:
            rank = ranks.get(row["cache_key"])
            row.update(layer=layer, rank=rank["rank"] if rank else None, hot=bool(rank and rank["hot"]),
                freshness="forever" if row["fresh_until"] is None else "fresh" if row["fresh_until"] > now else "stale")
        return rows

    def entries(self, actor, layer=None, key=None, search=None, page=1, page_size=50):
        rows = [item for selected in ([layer] if layer else LAYERS) for item in self.metadata(selected)]
        rows = [item for item in rows if (key is None or item["cache_key"] == key) and (not search or
            search.casefold() in (str(item.get("label") or "")+item["cache_key"]).casefold())]
        rows.sort(key=lambda item: (item["layer"], item["cache_key"]))
        selected = rows[(page-1)*page_size:page*page_size]
        sources = {item["source_trace_id"] for item in selected if item.get("source_trace_id")}
        with self.repository.transaction(lock=False) as (session, _state):
            retained = set(session.execute(select(TraceRow.REQUEST_ID).where(TraceRow.REQUEST_ID.in_(sources), *HistoryQuery.permission(actor))).scalars()) if sources else set()
        for item in selected:
            source = item.get("source_trace_id")
            item["source_trace_available"] = source in retained
            if source not in retained:
                item["source_trace_id"] = None
        return {"items": selected, "total": len(rows), "page": page, "page_size": page_size}

    def stats(self):
        layers = {}
        for layer in LAYERS:
            rows = self.metadata(layer)
            reads = self.store.envelope_read_stats("recognition_"+layer)
            layers[layer] = {"entries": len(rows), "hits": reads["hits"] if reads else None,
                "miss": reads["miss"] if reads else None, "miss_available": reads is not None,
                "read_stats": reads, "reads_scope": "touch_enabled_reads_since_initialization",
                "cycle_hits": sum(item["hit_count"] for item in rows),
                "stale": sum(item["freshness"] == "stale" for item in rows), "hot": sum(item["hot"] for item in rows),
                "cooldown": sum((item.get("next_retry_at") or 0) > time.time() for item in rows)}
        return {"layers": layers, "capabilities": RefreshWorker(self.settings, store=self.store).capabilities()}

    def jobs(self, actor, page=1, page_size=50):
        import json
        from app.media.recognition.query_service import JSONField
        conditions = [TaskRow.KIND == "recognition_async", JSONField(TaskRow.CONTROL, "operation").in_({"refresh", "invalidate"})]
        if str(actor) != "0":
            conditions.append(JSONField(TaskRow.CONTROL, "actor_id") == str(actor))
        from sqlalchemy import func
        with self.repository.transaction(lock=False) as (session, _state):
            total = session.execute(select(func.count()).select_from(TaskRow).where(*conditions)).scalar_one()
            rows = session.execute(select(TaskRow.TASK_ID, TaskRow.STATE, TaskRow.CONTROL, TaskRow.CREATED_AT).where(*conditions)
                .order_by(TaskRow.CREATED_AT.desc(), TaskRow.TASK_ID.desc()).offset((page-1)*page_size).limit(page_size)).all()
        return {"items": [{"job_id": row[0], "state": row[1], "control": json.loads(row[2]), "created_at": row[3]} for row in rows],
                "total": total, "page": page, "page_size": page_size, "retention": "short_lived_controls"}
