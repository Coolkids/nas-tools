"""Strict per-layer hot refresh, using backend metadata and fenced publication."""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
import re
from threading import RLock
import time
from types import SimpleNamespace
import uuid
import weakref

from app.media.recognition.budget import RecognitionBudget, RecognitionCancelled, BudgetExceeded, CancellationReason
from app.media.recognition.cache_repository import CacheRepository, LAYERS
from app.media.recognition.configuration import recognition_cache_capabilities, recognition_settings_version
from app.media.recognition.contracts import RecognitionOutcome, RecognitionStatus
from app.media.recognition.coordinator import effective_settings
from app.media.recognition.identity import IdentityService, IdentityScope, OverrideRepository
from app.media.recognition.lease import CacheLease, pause_refreshes, resume_refreshes, refresh_paused
from app.media.recognition.runtime import get_runtime, ResourceBusy
from app.media.recognition.tmdb_client import TMDBClient, TMDBRequestError
from app.media.recognition.trace import Trace
from app.utils.cache_backend import STORE
from app.utils.storage_jobs import STORAGE_JOBS


_workers, _workers_lock = weakref.WeakSet(), RLock()
_default_worker = None
_service_stopping = False


def start_refresh_service():
    global _service_stopping
    _service_stopping = False


def stop_refresh_service():
    global _service_stopping
    _service_stopping = True
    cancel_refreshes(reason=CancellationReason.SERVICE_SHUTDOWN)


def cancel_refreshes(store=None, reason=CancellationReason.USER_CANCELLED):
    with _workers_lock:
        workers = list(_workers)
    for worker in workers:
        if store is None or worker.store is store:
            worker.cancel(reason)


def refresh_window(envelope, requested, configured_ttl):
    if envelope.fresh_until is None:
        return None
    ttl = (envelope.fresh_until-envelope.created_at if envelope.created_at is not None else configured_ttl)
    return max(0, ttl/2) if requested >= ttl else requested


class _RefreshRuntime:
    def __init__(self, runtime):
        self.runtime = runtime

    def submit(self, _resource, item, **options):
        return self.runtime.submit("refresh", item, **options)


class RefreshWorker:
    def __init__(self, settings=None, *, store=None, runtime=None, repository=None, rules=None,
                 api_key=None, tmdb_base_url=None, clock=None, trace_root=None):
        self.settings, self.store, self.runtime = settings, STORE if store is None else store, runtime
        self.repository, self.rules = repository, rules
        self.api_key, self.tmdb_base_url = api_key, tmdb_base_url
        self.clock, self.trace_root = clock or time.time, trace_root
        self._mutex, self._scan_lock = RLock(), RLock()
        self._tokens, self._cursor, self.results = set(), {layer: 0 for layer in LAYERS}, deque(maxlen=128)
        self._layer_cursor = 0
        with _workers_lock:
            _workers.add(self)

    def config(self):
        return effective_settings(self.settings() if callable(self.settings) else self.settings)

    def capabilities(self, config=None):
        config = config or self.config()
        backend = self.store.capabilities()
        result = recognition_cache_capabilities(config, backend["actual_backend"], backend["storage_generation"])
        result["configured_backend"] = backend["configured_backend"]
        from app.media.recognition import records
        paused = refresh_paused(self.store)
        if paused or backend["maintenance"] or records._recognition_paused or STORAGE_JOBS.maintenance_gate("database"):
            result.update(effective_enabled=False, reason="storage_maintenance")
        elif backend["restart_required"]:
            result.update(effective_enabled=False, reason="backend_change_pending")
        elif self.store is STORE and _service_stopping or self.runtime is not None and self.runtime._closing:
            result.update(effective_enabled=False, reason="service_shutdown")
        return result

    def cancel(self, reason=CancellationReason.USER_CANCELLED):
        with self._mutex:
            tokens = list(self._tokens)
        for token in tokens:
            token.cancel(reason)

    def _allowed(self, config, generation, backend, manual=False):
        current = self.config()
        caps = self.capabilities(current)
        enabled = caps["effective_enabled"] if not manual else caps["cache_enabled"] and caps["reason"] not in {
            "storage_maintenance", "backend_change_pending", "service_shutdown"}
        return (enabled and caps["storage_generation"] == generation and caps["actual_backend"] == backend
                and recognition_settings_version(current) == recognition_settings_version(config))

    def scan(self, *, limit=None):
        # A second local scheduler/manual call must not build an unbounded queue.
        if not self._scan_lock.acquire(blocking=False):
            return {"state": "busy", "jobs": []}
        try:
            config = self.config()
            caps = self.capabilities(config)
            if not caps["effective_enabled"]:
                self.cancel()
                return {"state": "disabled", "reason": caps["reason"], "jobs": []}
            rec = config["recognition"]
            concurrency = min(32, rec["cache_v3"]["refresh_concurrency"])
            limit = concurrency*4 if limit is None else limit
            if type(limit) is not int or not 1 <= limit <= 128:
                raise ValueError("invalid_refresh_batch_limit")
            now, selected = self.clock(), []
            layers = LAYERS[self._layer_cursor:]+LAYERS[:self._layer_cursor]
            self._layer_cursor = (self._layer_cursor+1) % len(LAYERS)
            for layer in layers:
                if len(selected) >= limit:
                    break
                snapshot = self.store.rank_snapshot("recognition_"+layer, now=now, offset=self._cursor[layer], limit=128)
                examined = 0
                for row in snapshot["items"]:
                    if len(selected) >= limit:
                        break
                    examined += 1
                    if (not isinstance(row["cache_key"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["cache_key"])
                            or not row["hot"] or row["fresh_until"] is None or (row.get("next_retry_at") or 0) > now):
                        continue
                    hint = SimpleNamespace(fresh_until=row["fresh_until"], created_at=row.get("created_at"))
                    ahead = refresh_window(hint, rec["cache_v3"]["refresh_ahead_seconds"],
                        config["cache"]["ttl_seconds"]["recognition_"+layer])
                    if row["fresh_until"]-now <= ahead:
                        selected.append((layer, row))
                self._cursor[layer] += examined
                if not snapshot["items"] or self._cursor[layer] >= snapshot["total"]:
                    self._cursor[layer] = 0
            runtime = self.runtime or (get_runtime() if selected else None)
            if runtime is not None and not runtime.refresh_available():
                return {"state": "yielded_to_foreground", "jobs": []}
            with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="recognition-refresh") as executor:
                jobs = list(executor.map(lambda item: self.refresh(item[0], item[1]["cache_key"],
                    expected=item[1], config=config, runtime=runtime), selected, buffersize=concurrency))
            return {"state": "completed", "jobs": jobs}
        finally:
            self._scan_lock.release()

    def refresh(self, layer, key, *, expected=None, config=None, runtime=None, manual=False, task_trace=None, task_budget=None):
        if layer not in LAYERS or not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key) or type(manual) is not bool:
            raise ValueError("invalid_refresh_key")
        config = config or self.config()
        caps = self.capabilities(config)
        enabled = caps["effective_enabled"] if not manual else caps["cache_enabled"] and caps["reason"] not in {
            "storage_maintenance", "backend_change_pending", "service_shutdown"}
        if not enabled:
            return {"state": "disabled", "reason": caps["reason"]}
        cache = CacheRepository(self.store, settings=config, clock=self.clock)
        budget = task_budget or RecognitionBudget(config["recognition"]["timeouts"].get("total_ms"))
        runtime = runtime or self.runtime or get_runtime()
        result = {"job_id": uuid.uuid4().hex, "layer": layer, "cache_key": key, "state": "skipped"}
        allowed = lambda: self._allowed(config, caps["storage_generation"], caps["actual_backend"], manual)
        with self._mutex:
            self._tokens.add(budget.cancellation)
        trace, entry = None, None
        try:
            from app.media.recognition.records import v3_recognition_lease
            with runtime.background(budget.cancellation), v3_recognition_lease(budget), CacheLease(
                    self.store, "recognition_"+layer, key, budget, allowed=allowed) as lease:
                if not lease.fence:
                    result["state"] = "leased"
                    return result
                entry = cache.get(layer, key, hit=False, allow_stale=manual)
                if entry is None or entry.needs_revalidation or expected is not None and any(
                        getattr(entry, field) != expected[field] for field in ("payload_version", "cycle_id", "storage_generation")):
                    result["state"] = "superseded"
                    return result
                if entry.next_retry_at > self.clock() and not manual:
                    result["state"] = "cooldown"
                    return result
                trace = task_trace or Trace({"layer": layer, "cache_key": key, "manual": manual}, config["recognition"], repository=self.repository,
                    source="cache_refresh", budget=budget, root=self.trace_root)
                trace.emit("refresh", "refresh_started", "running", {"expected_version": entry.payload_version})
                try:
                    payload, needs, guard = self._load(layer, key, entry, config, cache, runtime, budget)
                    lease.check()
                    if not guard():
                        result["state"] = "superseded"
                        if task_trace is None:
                            trace.finish(RecognitionOutcome(trace.request_id, RecognitionStatus.PARSED, failure_code="refresh_superseded"))
                        return result
                    trace.emit("refresh", "refresh_validated", "success", {"needs_revalidation": needs})
                    if task_trace is None:
                        finished = trace.finish(RecognitionOutcome(trace.request_id, RecognitionStatus.PARSED))
                        if finished.status != RecognitionStatus.PARSED:
                            result["state"] = "cancelled" if finished.status == RecognitionStatus.CANCELLED else "timeout"
                            return result
                    else:
                        budget.check()
                    lease.check()
                    if not guard():
                        result["state"] = "superseded"
                        return result
                    if needs:
                        updated = replace(entry, needs_revalidation=True, refresh_state="needs_revalidation", refresh_error=None)
                        committed = self.store.envelope_put("recognition_"+layer, key, updated,
                            expected=asdict(entry), owner=lease.owner, fence=lease.fence)
                    else:
                        ttl = 60 if layer == "search" and not payload["results"] else None
                        committed = cache.put(layer, key, payload, ttl=ttl, expected=entry,
                            owner=lease.owner, fence=lease.fence, request_params=entry.request_params,
                            source_trace_id=entry.source_trace_id, override_version=entry.override_version)
                    result["state"] = ("needs_revalidation" if needs else "refreshed") if committed else "superseded"
                except Exception as error:
                    cancelled = budget.cancellation.reason is not None
                    timeout = isinstance(error, BudgetExceeded) or isinstance(error, TMDBRequestError) and "deadline" in error.code
                    code = error.code if isinstance(error, TMDBRequestError) else str(error) if isinstance(error, (BudgetExceeded, RecognitionCancelled)) else "refresh_failed"
                    if not cancelled and allowed():
                        delay = max(min(3600, 30*2**min(entry.refresh_failures, 7)),
                            min(36500*86400, getattr(error, "retry_after_seconds", None) or 0))
                        try:
                            self.store.envelope_refresh_failure("recognition_"+layer, key, asdict(entry),
                                owner=lease.owner, fence=lease.fence, delay=delay, code=code)
                        except Exception:
                            pass  # Backend loss must not discard the original stable failure code.
                    result.update(state="cancelled" if cancelled else "timeout" if timeout else "failed", error=code)
                    if trace.finished_at is None and task_trace is None:
                        trace.finish(RecognitionOutcome(trace.request_id, RecognitionStatus.CANCELLED if cancelled else RecognitionStatus.TIMEOUT if timeout else RecognitionStatus.FAILED, failure_code=code))
        except (RecognitionCancelled, BudgetExceeded, ResourceBusy):
            result["state"] = "cancelled" if budget.cancellation.reason else "busy"
        except Exception:
            result.update(state="failed", error="refresh_backend_unavailable")
        finally:
            if trace is not None and trace.finished_at is None and task_trace is None:
                trace.finish(RecognitionOutcome(trace.request_id, RecognitionStatus.FAILED, failure_code="refresh_backend_unavailable"))
            with self._mutex:
                self._tokens.discard(budget.cancellation)
                self.results.append(dict(result))
        return result

    def _load(self, layer, key, entry, config, cache, runtime, budget):
        params = entry.request_params
        if layer == "identity":
            scope = IdentityScope(**params["scope"])
            overrides = OverrideRepository(self.repository)
            identities = IdentityService(cache, config, overrides=overrides)
            current = overrides.get(scope)
            identity = identities.get(key, scope, current["version"], hit=False)
            # Refresh the verified title -> TMDB ID mapping independently of
            # mutable TMDB details. The detail layer owns its own refresh/I/O.
            needs = identity is None or current["choice"] is not None
            return entry.payload, needs, lambda: overrides.get(scope)["version"] == entry.override_version
        client_config = deepcopy(config)
        client_config["recognition"]["query"]["tmdb_retry_max"] = 0
        client = TMDBClient(client_config, runtime=_RefreshRuntime(runtime), cache=cache, budget=budget,
            api_key=self.api_key, base_url=self.tmdb_base_url)
        if not client.api_key or not isinstance(params, dict) or params.get("origin") != client.base_url:
            raise ValueError("refresh_origin_changed")
        path = params.get("path", "")
        patterns = {"search": r"/search/(?:tv|movie|multi)", "detail": r"/(?:tv|movie)/[1-9]\d*",
                    "episode": r"/tv/[1-9]\d*/season/\d+/episode/[1-9]\d*"}
        if not re.fullmatch(patterns[layer], path) or cache.key(layer, params) != key:
            raise ValueError("invalid_refresh_request")
        arguments = {field: value for field, value in params.items() if field not in {"origin", "path", "schema"}}
        if set(arguments) - {"query", "language", "include_adult", "page", "primary_release_year", "append_to_response"}:
            raise ValueError("invalid_refresh_parameters")
        response = client.request(path, arguments, layer=layer, cache=False,
            expected_id=int(path.rsplit("/", 1)[1]) if layer == "detail" else None)
        return response.payload, False, lambda: True


def run_hot_refresh():
    global _default_worker
    with _workers_lock:
        if _default_worker is None:
            _default_worker = RefreshWorker()
        worker = _default_worker
    return worker.scan()


def sync_schedule(scheduler=None, *, settings=None, store=None):
    if scheduler is None:
        from app.scheduler import Scheduler
        scheduler = Scheduler().SCHEDULER
    if scheduler is None or STORAGE_JOBS.maintenance_gates():
        return
    worker = RefreshWorker(settings, store=store)
    config, caps = worker.config(), worker.capabilities()
    requested = caps["requested_enabled"] and caps["cache_enabled"] and caps["configured_backend"] != "memory"
    for identity in ("recognition_hot_refresh", "recognition_hot_refresh_control"):
        enabled = caps["effective_enabled"] if identity == "recognition_hot_refresh" else requested
        if enabled:
            scheduler.add_interval_job("app.media.recognition.refresh_worker:"+("run_hot_refresh" if identity == "recognition_hot_refresh" else "sync_schedule"),
                id=identity, seconds=config["recognition"]["cache_v3"]["refresh_scan_interval_seconds"] if identity == "recognition_hot_refresh" else 5)
        elif scheduler.get_job(identity) is not None:
            scheduler.remove_job(identity)
    if not caps["effective_enabled"]:
        cancel_refreshes(worker.store)
