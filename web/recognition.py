"""Session-authenticated V3 task, history, SSE and administration endpoints."""

from dataclasses import asdict
from functools import wraps
import json
import math
import re
import time

from flask import Blueprint, Response, current_app, request, stream_with_context
from flask_login import current_user
from werkzeug.exceptions import HTTPException

from web.browser_security import BrowserAPIError, session_route
from app.media.recognition.cache_repository import LAYERS
from app.media.recognition.cache_view import CacheView
from app.media.recognition.cleanup import CleanupService, CleanupBusy, CleanupConflict, CleanupConfirmationError
from app.media.recognition.configuration import RecognitionConfigurationConflict, RecognitionConfigurationError
from app.media.recognition.coordinator import effective_settings
from app.media.recognition.identity import IdentityScope, OverrideRepository, OverrideConflict
from app.media.recognition.query_service import HistoryQuery
from app.media.recognition.repository import TERMINAL, ACTIVE_TASKS
from app.media.recognition.runtime import ResourceBusy
from app.media.recognition.tasks import get_task_service, TaskUnavailable
from app.media.recognition.trace_ipc import get_active_trace


routes = Blueprint("recognition", __name__)


def service():
    return current_app.extensions.get("recognition_v3_service") or get_task_service()


def history():
    return HistoryQuery(service().repository)


def store():
    from app.utils.cache_backend import STORE
    return current_app.extensions.get("recognition_v3_store", STORE)


def actor():
    return str(current_user.get_id())


def identifier(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise BrowserAPIError("REQUEST_NOT_FOUND", "识别请求不存在", 404)
    return value


def body(allowed):
    value = request.get_json(silent=True)
    if not isinstance(value, dict) or set(value)-set(allowed):
        raise ValueError("invalid_json_fields")
    return value


def integer(value, default=None, minimum=0, maximum=2**63-1):
    if value is None and default is not None:
        return default
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def boolean(value):
    if value not in {"true", "false"}:
        raise ValueError("invalid_boolean")
    return value == "true"


def paging():
    return integer(request.args.get("page"), 1, 1, 1000000), integer(request.args.get("page_size"), 50, 1, 200)


def filters(paged=True):
    allowed = {"title", "status", "type", "cache_hit", "duration_min_ms", "duration_max_ms", "date_from", "date_to",
               "source", "provider_id", "action_type", "reason"}
    if set(request.args)-allowed-({"page", "page_size"} if paged else set()):
        raise ValueError("invalid_query_fields")
    value = {key: request.args[key] for key in allowed if key in request.args}
    if any(len(value.get(key, "")) > 1024 for key in ("title", "source", "provider_id", "action_type", "reason")):
        raise ValueError("invalid_history_filter")
    if "cache_hit" in value:
        value["cache_hit"] = boolean(value["cache_hit"])
    for field in ("duration_min_ms", "duration_max_ms"):
        if field in value:
            value[field] = float(value[field])
            if not math.isfinite(value[field]) or value[field] < 0:
                raise ValueError("invalid_duration")
    return value


def accepted(result):
    result = dict(result)
    status = result.pop("http_status", 202)
    result.setdefault("job_id", result.get("request_id"))
    return result, status


def idempotency(data):
    header = request.headers.get("Idempotency-Key")
    value = data.get("idempotency_key")
    if header and value and header != value:
        raise ValueError("conflicting_idempotency_keys")
    return header or value


@routes.errorhandler(BrowserAPIError)
def browser_error(error):
    value = {"code": error.code, "message": error.message}
    if error.details is not None:
        value["details"] = error.details
    return value, error.status


@routes.errorhandler(Exception)
def operation_error(error):
    if isinstance(error, HTTPException):
        return {"code": "HTTP_ERROR", "message": "请求无法处理"}, error.code
    if isinstance(error, CleanupConflict):
        return {"code": "IDEMPOTENCY_CONFLICT", "message": "该幂等标识已用于另一项清理请求"}, 409
    if isinstance(error, CleanupConfirmationError):
        return {"code": "CONFIRMATION_INVALID_OR_EXPIRED", "message": "清理确认凭据无效或已过期，请重新预览"}, 400
    if isinstance(error, (RecognitionConfigurationConflict, OverrideConflict, CleanupBusy)):
        return {"code": "VALIDATION_CONFLICT" if isinstance(error, OverrideConflict) else "RESOURCE_CONFLICT", "message": "状态已变化，请刷新后重试"}, 409
    if isinstance(error, ResourceBusy):
        return {"code": "RESOURCE_BUSY", "message": "任务队列已满，请稍后重试"}, 429
    if isinstance(error, TaskUnavailable):
        key = str(error)
        if key in {"history_not_retained"}:
            return {"code": "HISTORY_NOT_RETAINED", "message": "此记录未保留，无法从历史重试"}, 410
        if key in {"idempotency_conflict", "idempotency_expired", "task_not_terminal", "task_not_retryable"}:
            return {"code": key.upper(), "message": "请求状态或幂等标识已变化"}, 409
        if key == "task_not_found":
            return {"code": "REQUEST_NOT_FOUND", "message": "识别请求不存在"}, 404
        return {"code": "SERVICE_UNAVAILABLE", "message": "识别服务正在维护或关闭"}, 503
    if isinstance(error, RecognitionConfigurationError):
        return {"code": "INVALID_ARGUMENT", "message": str(error), "field": error.field}, 400
    if isinstance(error, (ValueError, TypeError)):
        return {"code": "INVALID_ARGUMENT", "message": "请求参数不符合接口要求"}, 400
    from sqlalchemy.exc import SQLAlchemyError
    if isinstance(error, SQLAlchemyError) or isinstance(error, RuntimeError) and "maintenance" in str(error):
        return {"code": "STORAGE_UNAVAILABLE", "message": "存储暂时不可用，请稍后重试"}, 503
    current_app.logger.error("recognition API error: %s", type(error).__name__)
    return {"code": "INTERNAL_ERROR", "message": "识别请求处理失败"}, 500


def read_detail(request_id, who):
    identifier(request_id)
    query, tasks = history(), service()
    saved = query.detail(request_id, who)
    if saved is not None:
        return saved
    task = tasks.controls.get(request_id, who)
    if task is None:
        raise BrowserAPIError("REQUEST_NOT_FOUND", "识别请求不存在或无权访问", 404)
    mode = task["control"].get("record_mode")
    retained_terminal = mode == "all" or mode == "failed" and task["state"] in {"FAILED", "INTERRUPTED"}
    if task["control"].get("resource_released_at") and retained_terminal:
        raise BrowserAPIError("HISTORY_EXPIRED", "识别记录已清理或已不可用", 410)
    active = get_active_trace(request_id, tasks.root)
    if active is not None:
        return active
    if task["state"] in ACTIVE_TASKS:
        raise BrowserAPIError("OWNER_UNAVAILABLE", "任务所属进程暂时不可达", 503)
    if mode == "none" or mode == "failed" and task["state"] in {"SUCCEEDED", "CANCELLED"}:
        raise BrowserAPIError("HISTORY_NOT_RETAINED", "当前记录策略没有保留该请求详情", 410)
    raise BrowserAPIError("HISTORY_EXPIRED", "识别记录已清理或已不可用", 410)


def events_data(request_id, who, after_seq, limit=100):
    detail = read_detail(request_id, who)
    if detail["durability"] == "persistent":
        result = history().events(request_id, after_seq, limit)
    else:
        window = detail["events"]
        remaining = [event for event in window if event["seq"] > after_seq]
        items = remaining[:limit]
        result = {"events": items, "next_seq": items[-1]["seq"] if items else after_seq, "has_more": len(remaining) > limit,
            "last_seq": detail["last_seq"], "first_available_seq": window[0]["seq"] if window else 0,
            "reset_required": bool(window and after_seq < window[0]["seq"]-1), "durability": detail["durability"]}
    result.update(terminal=detail["status"] in TERMINAL, status=detail["status"], retention=detail["retention"], nodes=detail["nodes"])
    return result


@routes.route("/recognitions", methods=["POST"])
@session_route()
def submit():
    data = body({"title", "secondary_title", "context", "idempotency_key"})
    context = data.get("context", {})
    if not isinstance(context, dict) or set(context)-{"name", "year", "season", "media_type", "cache", "force", "chinese"}:
        raise ValueError("invalid_context")
    input = {**context, "title": data.get("title")}
    if "media_type" in input:
        input["mtype"] = input.pop("media_type")
    if "secondary_title" in data:
        input["subtitle"] = data["secondary_title"]
    return accepted(service().submit(input, actor(), idempotency_key=idempotency(data)))


@routes.route("/recognitions", methods=["GET"])
@session_route()
def listing():
    page, size = paging()
    return {"code": 0, **history().listing(actor(), page, size, filters())}


@routes.route("/recognitions/stats", methods=["GET"])
@session_route()
def stats():
    mode = effective_settings(service().settings)["recognition"]["records"]["mode"]
    return {"code": 0, "can_manage": actor() == "0", **history().stats(actor(), mode, filters(False))}


@routes.route("/recognitions/<request_id>", methods=["GET"])
@session_route()
def detail(request_id):
    return {"code": 0, **read_detail(request_id, actor())}


@routes.route("/recognitions/<request_id>/events", methods=["GET"])
@session_route()
def events(request_id):
    if set(request.args)-{"after_seq", "limit"}:
        raise ValueError("invalid_query_fields")
    after = integer(request.args.get("after_seq"), 0)
    return {"code": 0, **events_data(request_id, actor(), after, integer(request.args.get("limit"), 100, 1, 200))}


@routes.route("/recognitions/<request_id>/candidates", methods=["GET"])
@session_route()
def candidates(request_id):
    if set(request.args)-{"page", "page_size"}:
        raise ValueError("invalid_query_fields")
    page, size = paging()
    detail = read_detail(request_id, actor())
    if detail["durability"] == "persistent":
        result = history().candidates(request_id, page, size)
    else:
        rows = detail.get("candidates", [])
        result = {"items": rows[(page-1)*size:page*size], "total": len(rows), "page": page, "page_size": size,
                  "truncated": detail.get("candidates_truncated", False)}
    return {"code": 0, **result}


@routes.route("/recognitions/<request_id>/cancel", methods=["POST"])
@session_route()
def cancel(request_id):
    identifier(request_id)
    body(set())
    return accepted(service().cancel(request_id, actor()))


@routes.route("/recognitions/<request_id>/retry", methods=["POST"])
@session_route()
def retry(request_id):
    identifier(request_id)
    data = body({"bypass_identity_cache", "idempotency_key"})
    return accepted(service().retry(request_id, actor(), bypass_identity_cache=data.get("bypass_identity_cache", False), idempotency_key=idempotency(data)))


@routes.route("/recognitions/<request_id>/confirm", methods=["POST"])
@session_route(administrator=True)
def confirm(request_id):
    parent = read_detail(request_id, actor())
    data = body({"media_type", "tmdb_id", "scope", "expected_override_version", "clear", "idempotency_key"})
    scope = IdentityScope(**data.get("scope", {}))
    if parent["input"].get("scope") != asdict(scope):
        raise BrowserAPIError("SCOPE_MISMATCH", "确认范围必须与原识别请求一致", 409)
    version = integer(data.get("expected_override_version"))
    clear = data.get("clear", False)
    if type(clear) is not bool or not clear and data.get("media_type") not in {"movie", "tv"}:
        raise ValueError("invalid_confirmation")
    identity = integer(data.get("tmdb_id"), minimum=1) if not clear else None
    input = {"parent_request_id": request_id, "scope": asdict(scope), "expected_version": version,
        "media_type": data.get("media_type"), "tmdb_id": identity, "clear": clear}
    key = idempotency(data)
    existing = service().existing(input, actor(), operation="confirm", idempotency_key=key)
    if existing:
        return accepted(existing)
    if OverrideRepository(service().repository).get(scope)["version"] != version:
        raise OverrideConflict("VALIDATION_CONFLICT")
    return accepted(service().enqueue(input, actor(), operation="confirm", idempotency_key=key))


def frame(event, data, seq=None):
    return (f"id: {seq}\n" if seq is not None else "")+f"event: {event}\ndata: "+json.dumps(data, ensure_ascii=False, separators=(",", ":"))+"\n\n"


@routes.route("/recognitions/<request_id>/stream", methods=["GET"])
@session_route()
def stream(request_id):
    if set(request.args)-{"after_seq"}:
        raise ValueError("invalid_query_fields")
    who = actor()
    cursor = integer(request.args.get("after_seq", request.headers.get("Last-Event-ID")), 0)
    initial = read_detail(request_id, who)
    if cursor > initial["last_seq"]:
        raise BrowserAPIError("EVENT_CURSOR_AHEAD", "事件游标超出当前记录范围", 409)
    heartbeat = max(.05, float(current_app.config.get("RECOGNITION_SSE_HEARTBEAT_SECONDS", 15)))
    poll = max(.01, float(current_app.config.get("RECOGNITION_SSE_POLL_SECONDS", .25)))
    @stream_with_context
    def generate():
        nonlocal cursor
        snapshot = {key: value for key, value in initial.items() if key not in {"events", "candidates"}}
        yield frame("snapshot", snapshot)
        last_heartbeat = time.monotonic()
        while True:
            try:
                page = events_data(request_id, who, cursor, 200)
            except BrowserAPIError as error:
                yield frame("unavailable", {"code": error.code, "message": error.message})
                return
            except Exception as error:
                value, _status = operation_error(error)
                yield frame("unavailable", value)
                return
            if page["reset_required"]:
                yield frame("reset_required", {"first_available_seq": page["first_available_seq"], "last_seq": page["last_seq"],
                    "nodes": page["nodes"], "durability": page["durability"]})
                cursor = max(cursor, page["first_available_seq"]-1)
            for event in page["events"]:
                if event["seq"] > cursor:
                    yield frame("recognition_event", event, event["seq"])
                    cursor = event["seq"]
            if page["terminal"] and not page["has_more"] and cursor >= page["last_seq"]:
                return
            if time.monotonic()-last_heartbeat >= heartbeat:
                yield frame("heartbeat", {"last_seq": cursor})
                last_heartbeat = time.monotonic()
            if not page["has_more"]:
                time.sleep(poll)
    response = Response(generate(), mimetype="text/event-stream")
    response.headers.update({"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no", "Vary": "Cookie"})
    return response


@routes.route("/recognition-settings", methods=["GET", "PUT"])
@session_route(administrator=True)
def settings():
    from app.media.recognition.settings_service import get_settings, save_settings
    if request.method == "GET":
        result = get_settings()
    else:
        data = body({"patch", "expected_version"})
        result = save_settings(data.get("patch", {}), data.get("expected_version"))
    return {"code": 0, **result}


@routes.route("/recognition-settings/openai/credential", methods=["PUT"])
@session_route(administrator=True)
def credential():
    from app.media.recognition.settings_service import save_openai_secret
    data = body({"api_key", "clear_api_key", "expected_version"})
    return {"code": 0, **save_openai_secret(data.get("api_key"), data.get("clear_api_key", False), data.get("expected_version"))}


@routes.route("/recognition-settings/openai/test", methods=["POST"])
@session_route(administrator=True)
def test_openai():
    from app.media.recognition.settings_service import test_openai_connection
    data = body({"options", "api_key", "expected_version"})
    return {"code": 0, **test_openai_connection(data.get("options"), data.get("api_key"), data.get("expected_version"))}


@routes.route("/recognition-records/cleanup/preview", methods=["POST"])
@session_route(administrator=True)
def cleanup_preview():
    data = body({"scope"})
    config = effective_settings(service().settings)
    return {"code": 0, **CleanupService(service().repository, root=service().root).preview(actor(), data.get("scope", "expired"), config["recognition"]["records"]["cleanup"])}


@routes.route("/recognition-records/cleanup", methods=["POST"])
@session_route(administrator=True)
def cleanup_submit():
    data = body({"scope", "confirmation_token", "idempotency_key"})
    return {"code": 0, **CleanupService(service().repository, root=service().root).submit(data.get("confirmation_token"), actor(),
        idempotency(data), scope=data.get("scope", "expired"))}, 202


@routes.route("/recognition-records/cleanup/jobs/<job_id>", methods=["GET"])
@session_route(administrator=True)
def cleanup_job(job_id):
    if not re.fullmatch(r"[0-9a-f]{64}", job_id):
        raise BrowserAPIError("JOB_NOT_FOUND", "清理任务不存在", 404)
    result = service().repository.get_task(job_id, actor())
    if result is None or result["kind"] != "cleanup":
        raise BrowserAPIError("JOB_NOT_FOUND", "清理任务不存在", 404)
    return {"code": 0, **result}


@routes.route("/recognition-records/cleanup/jobs/<job_id>/resume", methods=["POST"])
@session_route(administrator=True)
def cleanup_resume(job_id):
    body(set())
    return {"code": 0, **CleanupService(service().repository, root=service().root).resume(job_id, actor())}, 202


def cache_view():
    return CacheView(store(), service().repository, effective_settings(service().settings))


@routes.route("/recognition-cache/stats", methods=["GET"])
@session_route(administrator=True)
def cache_stats():
    return {"code": 0, **cache_view().stats()}


@routes.route("/recognition-cache/entries", methods=["GET"])
@session_route(administrator=True)
def cache_entries():
    if set(request.args)-{"layer", "key", "search", "page", "page_size"}:
        raise ValueError("invalid_query_fields")
    page, size = paging()
    layer = request.args.get("layer")
    if layer is not None and layer not in LAYERS:
        raise ValueError("invalid_cache_layer")
    search = request.args.get("search")
    if search is not None and len(search) > 1024:
        raise ValueError("invalid_cache_search")
    return {"code": 0, **cache_view().entries(actor(), layer, request.args.get("key"), search, page, size)}


def cache_operation(operation, data, retry_of=None):
    layer, key = data.get("layer"), data.get("cache_key")
    if layer not in LAYERS or not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("invalid_cache_entry")
    version = integer(data.get("expected_version"), minimum=1)
    input = {"layer": layer, "cache_key": key, "expected_version": version}
    idem = idempotency(data)
    existing = service().existing(input, actor(), operation=operation, retry_of=retry_of, idempotency_key=idem)
    if existing:
        return accepted(existing)
    from app.media.recognition.refresh_worker import RefreshWorker
    caps = RefreshWorker(effective_settings(service().settings), store=store()).capabilities()
    if not caps["cache_enabled"]:
        raise BrowserAPIError("CACHE_DISABLED", "识别缓存总开关已关闭", 409)
    if caps["reason"] in {"storage_maintenance", "backend_change_pending", "service_shutdown"}:
        raise BrowserAPIError("CACHE_UNAVAILABLE", "缓存正在维护或切换", 503)
    value = store().envelope_get("recognition_"+layer, key, hit=False, allow_stale=True)
    if value is None:
        raise BrowserAPIError("CACHE_ENTRY_NOT_FOUND", "缓存条目不存在或已过期", 404)
    if value["payload_version"] != version:
        raise BrowserAPIError("CACHE_VERSION_CONFLICT", "缓存版本已变化，请刷新后重试", 409)
    input["expected"] = {key: value[key] for key in ("payload_version", "cycle_id", "storage_generation")}
    return accepted(service().enqueue(input, actor(), operation=operation, retry_of=retry_of, idempotency_key=idem))


@routes.route("/recognition-cache/refresh", methods=["POST"])
@session_route(administrator=True)
def cache_refresh():
    return cache_operation("refresh", body({"layer", "cache_key", "expected_version", "idempotency_key"}))


@routes.route("/recognition-cache/invalidate", methods=["POST"])
@session_route(administrator=True)
def cache_invalidate():
    return cache_operation("invalidate", body({"layer", "cache_key", "expected_version", "idempotency_key"}))


@routes.route("/recognition-cache/refresh-jobs", methods=["GET"])
@session_route(administrator=True)
def cache_jobs():
    if set(request.args)-{"page", "page_size"}:
        raise ValueError("invalid_query_fields")
    page, size = paging()
    return {"code": 0, **cache_view().jobs(actor(), page, size)}


@routes.route("/recognition-cache/refresh-jobs/<job_id>", methods=["GET"])
@session_route(administrator=True)
def cache_job(job_id):
    identifier(job_id)
    value = service().controls.get(job_id, actor())
    if value is None or value["control"].get("operation") not in {"refresh", "invalidate"}:
        raise BrowserAPIError("JOB_NOT_FOUND", "缓存任务不存在", 404)
    return {"code": 0, **value}


@routes.route("/recognition-cache/refresh-jobs/<job_id>/retry", methods=["POST"])
@session_route(administrator=True)
def cache_retry(job_id):
    data = body({"idempotency_key"})
    identifier(job_id)
    previous = history().detail(job_id, actor())
    if previous is None:
        raise BrowserAPIError("HISTORY_NOT_RETAINED", "任务输入未保留，无法从历史重试", 410)
    if previous["source"] not in {"cache_refresh", "cache_invalidate"} or previous["status"] not in TERMINAL:
        raise ValueError("invalid_cache_retry")
    input = previous["input"]
    current = store().envelope_get("recognition_"+input["layer"], input["cache_key"], hit=False, allow_stale=True)
    if current is None:
        raise BrowserAPIError("CACHE_ENTRY_NOT_FOUND", "缓存条目不存在或已过期", 404)
    return cache_operation("refresh" if previous["source"] == "cache_refresh" else "invalidate",
        {**data, "layer": input["layer"], "cache_key": input["cache_key"], "expected_version": current["payload_version"]}, retry_of=job_id)


def register_recognition(app):
    app.register_blueprint(routes, url_prefix="/api/v1/media")
