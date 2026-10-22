"""Validated task operations; callers cannot supply a URL or executable work."""

from app.media.recognition.contracts import RecognitionOutcome, IdentityEntry


class OperationFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def run_operation(operation, input, trace, budget, config):
    budget.check()
    if operation == "confirm":
        from app.media.recognition.coordinator import RecognitionCoordinator
        engine = RecognitionCoordinator(config, repository=trace.repository, trace_root=trace.root)
        result = engine.confirm(input["scope"], media_type=input.get("media_type"), tmdb_id=input.get("tmdb_id"),
            expected_version=input["expected_version"], actor=trace.input["actor_id"], request_id=trace.request_id,
            clear=input.get("clear", False), budget=budget,
            task_fence={"owner": trace._task_owner, "generation": trace.task_generation})
        trace.emit("override", "override_updated", "success", {"override_version": result["version"], "cleared": result["choice"] is None})
        return RecognitionOutcome(trace.request_id, "MATCHED" if result["choice"] else "PARSED",
            IdentityEntry(**result["choice"]) if result["choice"] else None, cache_source="manual_override")
    from app.utils.cache_backend import STORE
    namespace = "recognition_"+input["layer"]
    if not config["recognition"]["cache"]["enabled"]:
        raise OperationFailure("CACHE_DISABLED")
    if operation == "invalidate":
        budget.check()
        if not STORE.envelope_delete(namespace, input["cache_key"], expected=input["expected"]):
            raise OperationFailure("CACHE_VERSION_CONFLICT")
        trace.emit("cache", "cache_invalidated", "success", {"layer": input["layer"], "cache_key": input["cache_key"]})
        return RecognitionOutcome(trace.request_id, "PARSED")
    if operation == "refresh":
        from app.media.recognition.refresh_worker import RefreshWorker
        result = RefreshWorker(config, store=STORE, repository=trace.repository, trace_root=trace.root).refresh(input["layer"], input["cache_key"],
            expected=input["expected"], manual=True, task_trace=trace, task_budget=budget)
        budget.check()
        trace.emit("cache", "refresh_result", result["state"], {"state": result["state"], "error": result.get("error")})
        if result["state"] not in {"refreshed", "needs_revalidation"}:
            raise OperationFailure({"superseded": "CACHE_VERSION_CONFLICT", "leased": "RESOURCE_BUSY",
                "busy": "RESOURCE_BUSY", "disabled": "CACHE_UNAVAILABLE"}.get(result["state"], "CACHE_REFRESH_FAILED"))
        return RecognitionOutcome(trace.request_id, "PARSED")
    raise ValueError("invalid_operation")
