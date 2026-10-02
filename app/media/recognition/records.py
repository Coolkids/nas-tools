"""Capture one recognition request and its ordered actions/results."""

import contextvars
import datetime
import json
import time
import uuid
from functools import wraps
from enum import Enum


_current = contextvars.ContextVar("media_recognition_recorder", default=None)


def json_safe(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return json_safe(value.to_dict())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {str(key): json_safe(item) for key, item in value.__dict__.items()
                if not str(key).startswith("_") and not callable(item)}
    return str(value)


class RecognitionRecorder:
    def __init__(self, original_name, source="unknown", stage="resolve", context=None):
        self.request_id = str(uuid.uuid4())
        self.original_name = original_name
        self.source = source
        self.stage = stage
        self.context = json_safe(context or {})
        self.started = time.monotonic()
        self.actions = []
        self.provider_results = []
        self.tmdb_results = []
        self.overall_result = {}
        self._sequence = 0
        self._token = None
        self._delegate = None

    def __enter__(self):
        parent = _current.get()
        if parent is not None:
            # Internal resolve calls belong to the caller's request. In
            # particular, a batch-file request must not be replaced by the
            # nested get_media_info() used for its AI path.
            self._delegate = parent
            return parent
        self._token = _current.set(self)
        self.action("request_start", status="running", input={"original_name": self.original_name})
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._delegate is not None:
            return False
        if exc_type:
            self.overall_result = {"status": "failed", "reason": str(exc_value)}
            self.action("request_error", status="error", output={"error": str(exc_value)})
        if not self.overall_result:
            self.overall_result = {"status": "failed", "reason": "recognition_not_completed"}
        self.overall_result.setdefault("lifecycle", "error" if exc_type else "completed")
        self.overall_result["elapsed_ms"] = int((time.monotonic() - self.started) * 1000)
        self.action("request_finish", status=self.overall_result["status"],
                    output=self.overall_result)
        try:
            from app.helper.db_helper import DbHelper
            saved = DbHelper().insert_recognition_record(self.as_dict())
            if not saved:
                try:
                    import log
                    log.error(f"【Recognition】识别记录写入失败：{self.request_id}")
                except Exception:
                    pass
        except Exception as error:
            # Recognition logging must not break existing media lookup behavior.
            try:
                import log
                log.error(f"【Recognition】保存识别记录失败：{error}")
            except Exception:
                pass
        finally:
            if self._token is not None:
                _current.reset(self._token)
        return False

    def action(self, action_type, status="success", input=None, output=None,
               provider_id=None, attempt_id=None, reason=None):
        self._sequence += 1
        action = {
            "action_id": f"{self.request_id}:{self._sequence}",
            "sequence": self._sequence,
            "action_type": action_type,
            "provider_id": provider_id,
            "attempt_id": attempt_id,
            "status": status,
            "input": json_safe(input or {}),
            "output": json_safe(output or {}),
        }
        if reason:
            action["reason"] = str(reason)
        action["time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.actions.append(action)
        return action

    def add_provider_result(self, provider_id, status, input=None, raw_result=None,
                            normalized_result=None, version=None, elapsed_ms=None,
                            error=None, attempt_id=None):
        attempt_id = attempt_id or str(uuid.uuid4())
        item = {
            "attempt_id": attempt_id,
            "provider_id": provider_id,
            "version": version,
            "status": status,
            "input": json_safe(input or {}),
            "raw_result": json_safe(raw_result),
            "normalized_result": json_safe(normalized_result),
            "elapsed_ms": elapsed_ms,
            "error": str(error) if error else None,
        }
        self.provider_results.append(item)
        self.action("provider_parse", status=status, input=item["input"],
                    output={"attempt_id": attempt_id, "result": item["normalized_result"]},
                    provider_id=provider_id, attempt_id=attempt_id, reason=error)
        return item

    def add_tmdb_result(self, provider_id, query, result, status=None, reason=None):
        status = status or ("success" if result else "no_result")
        item = {"provider_id": provider_id, "query": json_safe(query),
                "status": status, "result": json_safe(result), "reason": reason}
        self.tmdb_results.append(item)
        for attempt in reversed(self.provider_results):
            if attempt.get("provider_id") == provider_id:
                attempt.setdefault("tmdb_results", []).append(item)
                break
        self.action("tmdb_query", status=status, input=query, output=item["result"],
                    provider_id=provider_id, reason=reason)
        return item

    def set_overall(self, status, reason=None, parsed_result=None,
                    selected_provider=None, tmdb_result=None):
        self.overall_result = {
            "status": status,
            "reason": reason,
            "parsed_result": json_safe(parsed_result),
            "selected_provider": selected_provider,
            "tmdb_result": json_safe(tmdb_result),
        }
        if self.stage == "parse_only":
            self.overall_result["tmdb_status"] = "not_requested"
        if self.context.get("shadow_result") is not None:
            self.overall_result["shadow_result"] = json_safe(self.context["shadow_result"])
        self.action("decision", status=status, output=self.overall_result, reason=reason)

    def as_dict(self):
        public_context = {key: value for key, value in self.context.items()
                          if key not in {"active_provider_id", "decision_reason",
                                         "selected_provider", "shadow_result"}}
        return {
            "request_id": self.request_id,
            "original_name": self.original_name,
            "source": self.source,
            "stage": self.stage,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
            "context": public_context,
            "actions": self.actions,
            "provider_results": self.provider_results,
            "overall_result": self.overall_result,
            "tmdb_results": self.tmdb_results,
        }


def current_recorder():
    return _current.get()


def recognition_scope(original_name, source="unknown", stage="resolve", context=None):
    return RecognitionRecorder(original_name, source=source, stage=stage, context=context)


def record_tmdb_call(method):
    """Keep the actual TMDB lookup result attached to its recognition request."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        recorder = current_recorder()
        if recorder is None:
            return method(self, *args, **kwargs)
        provider_id = recorder.context.get("active_provider_id") or "local_rules"
        query = {"method": method.__name__, "args": json_safe(args), "kwargs": json_safe(kwargs)}
        started = time.monotonic()
        try:
            result = method(self, *args, **kwargs)
        except Exception as error:
            recorder.add_tmdb_result(
                provider_id=provider_id,
                query=query,
                result=None,
                status="error",
                reason=str(error),
            )
            raise
        recorder.add_tmdb_result(
            provider_id=provider_id,
            query=query,
            result=result,
            status="success" if result else "no_result",
            reason=f"elapsed_ms={int((time.monotonic() - started) * 1000)}",
        )
        return result
    return wrapped
