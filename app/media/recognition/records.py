"""记录一次识别请求及其有序动作和结果。"""

import contextvars
import copy
import datetime
import hashlib
import json
import os
import time
import uuid
import requests.exceptions
from functools import wraps
from contextlib import contextmanager
from enum import Enum
from threading import Condition, Lock
from app.media.recognition.settings import (
    provider_enabled, profile_tmdb_allowed, recognition_record_mode,
)
from app.media.recognition.budget import CancellationToken, RecognitionBudget, total_timeout_ms


_current = contextvars.ContextVar("media_recognition_recorder", default=None)
_stage_budget = contextvars.ContextVar("media_recognition_stage_budget", default=None)
_spool_lock = Lock()
_active_spool_paths = set()
_recognition_condition = Condition()
_recognition_paused = False
_active_recognitions = 0


def quiesce_recognition(timeout=120):
    """暂停新识别请求并等待在途请求完成，供数据库快照和切换使用。"""
    global _recognition_paused
    deadline = time.monotonic() + max(0, timeout)
    with _recognition_condition:
        if _recognition_paused:
            raise RuntimeError("识别请求已处于维护暂停状态")
        _recognition_paused = True
    # Do not hold the condition while joining workers: finishing recognition
    # needs the same condition to release its maintenance lease.
    from app.media.recognition.runtime import shutdown_runtime
    tasks_quiesced = False
    try:
        from app.media.recognition.tasks import quiesce_tasks
        quiesce_tasks(timeout=max(0, deadline-time.monotonic()))
        tasks_quiesced = True
        shutdown_runtime()
    except BaseException:
        if tasks_quiesced:
            from app.media.recognition.tasks import resume_tasks
            resume_tasks()
        with _recognition_condition:
            _recognition_paused = False
            _recognition_condition.notify_all()
        raise
    with _recognition_condition:
        while _active_recognitions:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _recognition_paused = False
                _recognition_condition.notify_all()
                from app.media.recognition.tasks import resume_tasks
                resume_tasks()
                raise TimeoutError("等待在途识别请求结束超时，数据库维护已中止")
            _recognition_condition.wait(remaining)


def resume_recognition():
    """解除数据库维护期间的识别请求暂停。"""
    global _recognition_paused
    from app.media.recognition.tasks import resume_tasks
    resume_tasks()
    with _recognition_condition:
        _recognition_paused = False
        _recognition_condition.notify_all()


@contextmanager
def v3_recognition_lease(budget):
    """Maintenance admission with the request budget, including unlimited waits."""
    from app.utils.storage_jobs import STORAGE_JOBS
    from app.media.recognition.admission import gate
    global _active_recognitions
    lock = None
    while lock is None:
        budget.check()
        with _recognition_condition:
            if _recognition_paused or gate():
                _recognition_condition.wait(.05)
                continue
        candidate = STORAGE_JOBS.maintenance_lock(exclusive=False, timeout=.1)
        try:
            candidate.__enter__()
        except TimeoutError:
            continue
        with _recognition_condition:
            if _recognition_paused or gate() or STORAGE_JOBS.maintenance_gate("database"):
                candidate.__exit__(None, None, None)
            else:
                lock = candidate
                _active_recognitions += 1
        if lock is None:
            time.sleep(.02)
    try:
        budget.check()
        yield
    finally:
        lock.__exit__(None, None, None)
        with _recognition_condition:
            _active_recognitions -= 1
            _recognition_condition.notify_all()


def _recognition_generation():
    """按本进程实际连接配置标记 spool，避免跨数据库切换回放旧记录。"""
    try:
        from app.db.manager import DATABASE
        settings = DATABASE._settings
        if settings is None:
            from app.db.migration import _current_settings
            settings = _current_settings()
        from app.db.migration import _fingerprint
        from app.db.recognition_upgrade import generation
        from config import Config
        return generation(_fingerprint(settings), Config().get_config_path())
    except Exception:
        return None


def _retire_mismatched_spool(path, generation):
    from config import Config
    directory = os.path.join(Config().get_config_path(), ".maintenance",
                             "recognition-spool-retired", str(generation or "unknown"))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = os.path.join(directory, os.path.basename(path))
    if os.path.exists(target):
        target = os.path.join(directory, f"{uuid.uuid4().hex}-{os.path.basename(path)}")
    os.replace(path, target)
    return target


def _acquire_recognition_lease(timeout=120):
    """Hold a shared cross-process maintenance lease for the full request."""
    from app.utils.storage_jobs import STORAGE_JOBS
    from app.media.recognition.admission import gate

    deadline = time.monotonic() + timeout
    while True:
        with _recognition_condition:
            while _recognition_paused or gate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("数据库维护期间等待识别请求恢复超时")
                _recognition_condition.wait(min(remaining, 1.0))
        lock = STORAGE_JOBS.maintenance_lock(exclusive=False, timeout=1)
        try:
            lock.__enter__()
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise TimeoutError("等待数据库维护任务结束超时")
            continue

        with _recognition_condition:
            paused = _recognition_paused
        if paused or gate() or STORAGE_JOBS.maintenance_gate("database"):
            lock.__exit__(None, None, None)
            if time.monotonic() >= deadline:
                raise TimeoutError("数据库维护期间等待识别请求恢复超时")
            time.sleep(0.25)
            continue
        return lock


def _spool_directory():
    from config import Config
    return os.path.join(Config().get_config_path(), "recognition_v3_spool")


def _write_spool(payload):
    from app.media.recognition.compatibility import bundle
    from app.media.recognition.trace import write_spool
    return str(write_spool(bundle(payload)))


def replay_recognition_spool():
    """Recover current-schema history; never import retired legacy spool bodies."""
    from app.media.recognition.trace import recover_spool
    result = recover_spool()
    return {"replayed": result["recovered"], "pending": result["pending"],
            "invalid": result["invalid"], "retired": result["retired"]}


def json_safe(value):
    if isinstance(value, Enum):
        return json_safe(value.value)
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
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


_SENSITIVE_CONFIG_KEY_PARTS = (
    "url", "endpoint", "token", "password", "secret", "api_key", "apikey",
    "access_key", "authorization", "credential",
)


def _public_recognition_config(value):
    """复制识别配置，同时排除连接地址和凭据信息。"""
    if isinstance(value, dict):
        public = {}
        for key, item in value.items():
            normalized_key = "".join(character for character in str(key).lower()
                                     if character.isalnum())
            if any(part.replace("_", "") in normalized_key
                   for part in _SENSITIVE_CONFIG_KEY_PARTS):
                continue
            public[str(key)] = _public_recognition_config(item)
        return public
    if isinstance(value, (list, tuple)):
        return [_public_recognition_config(item) for item in value]
    return json_safe(value)


def _recognition_config_snapshot():
    """返回当前生效识别配置的稳定脱敏快照。"""
    try:
        from config import Config
        config = Config().get_config()
        recognition_config = config.get("recognition", {}) if isinstance(config, dict) else {}
        laboratory = config.get("laboratory", {}) if isinstance(config, dict) else {}
        snapshot = {
            "recognition": _public_recognition_config(recognition_config),
            "runtime": {"ai_inference_enabled": provider_enabled(
                "anitopy_ml", recognition_config, laboratory)},
        }
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), snapshot
    except Exception:
        # 记录过程不能影响媒体查询；启动期间配置尚不可用时也必须如此。
        return None, {}


def _runtime_config_snapshot():
    try:
        from config import Config
        config = Config().get_config()
        return copy.deepcopy(config) if isinstance(config, dict) else None
    except Exception:
        return None


def recognition_config(section=None):
    """存在识别作用域时，读取该请求固定下来的配置快照。"""
    recorder = current_recorder()
    snapshot = getattr(recorder, "_runtime_config", None) if recorder else None
    if snapshot is not None:
        if section is None:
            return copy.deepcopy(snapshot)
        value = snapshot.get(section, {})
        return copy.deepcopy(value) if isinstance(value, dict) else value
    from config import Config
    return Config().get_config(section) if section else Config().get_config()


class RecognitionRecorder:
    def __init__(self, original_name, source="unknown", stage="resolve", context=None):
        self.request_id = uuid.uuid4().hex
        self.original_name = original_name
        self.source = source
        self.stage = stage
        self.context = json_safe(context or {})
        self.actor_id = "0"
        from flask import has_request_context
        if has_request_context():
            from flask_login import current_user
            if current_user.is_authenticated:
                self.actor_id = str(current_user.get_id())
        self.started = time.monotonic()
        from app.media.recognition.repository import utc_now
        self.created_at = utc_now()
        self.started_ns = time.monotonic_ns()
        self.finished_at = None
        self._payload_bytes = 0
        self._actions_truncated = False
        self.actions = []
        self.provider_results = []
        self.tmdb_results = []
        self.overall_result = {}
        self._sequence = 0
        self._token = None
        self._delegate = None
        self._spool_path = None
        self._runtime_config = None
        self._record_mode = "failed"
        self.deadline_monotonic = None
        self._budget_initialized = False
        self.budget = None
        self.cancellation = CancellationToken()
        self._tmdb_timeout_count = 0
        self._tmdb_timeout_reason = None
        self._tmdb_failure_events = []
        self._registered = False
        self._maintenance_lock = None
        self._runtime_foreground = None

    def __enter__(self):
        parent = _current.get()
        if parent is not None:
            # 内部解析调用属于外层请求；尤其是批量文件请求，不能被 AI 流程中的
            # 嵌套 get_media_info() 替换。
            self._delegate = parent
            return parent
        global _active_recognitions
        self._maintenance_lock = _acquire_recognition_lease()
        with _recognition_condition:
            _active_recognitions += 1
            self._registered = True
        self._runtime_config = _runtime_config_snapshot()
        recognition = (self._runtime_config or {}).get("recognition", {})
        self._record_mode = recognition_record_mode(recognition)
        if recognition:
            try:
                initialize_recognition_budget(self, recognition)
                from app.media.recognition.runtime import get_runtime
                self._runtime_foreground = get_runtime().foreground(deadline=self.deadline_monotonic, cancellation=self.cancellation)
                self._runtime_foreground.__enter__()
            except BaseException:
                self._runtime_foreground = None
                self._maintenance_lock.__exit__(None, None, None)
                self._maintenance_lock = None
                with _recognition_condition:
                    _active_recognitions -= 1
                    self._registered = False
                    _recognition_condition.notify_all()
                raise
        config_version, config_snapshot = _recognition_config_snapshot()
        if self._runtime_config is not None:
            recognition_config_value = self._runtime_config.get("recognition", {})
            laboratory = self._runtime_config.get("laboratory", {})
            config_snapshot = {
                "recognition": _public_recognition_config(recognition_config_value),
                "runtime": {"ai_inference_enabled": provider_enabled(
                    "anitopy_ml", recognition_config_value, laboratory)},
            }
            canonical = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))
            config_version = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.context["recognition_config_version"] = config_version
        self.context["recognition_config_snapshot"] = config_snapshot
        replay_recognition_spool()
        self._token = _current.set(self)
        self.action("request_start", status="running", input={"original_name": self.original_name})
        self.overall_result = {"status": "running", "lifecycle": "running"}
        if self._record_mode != "none":
            try:
                self._spool_path = _write_spool(self.as_dict())
                with _spool_lock:
                    _active_spool_paths.add(self._spool_path)
            except Exception as error:
                try:
                    import log
                    log.warn(f"【Recognition】无法写入识别开始暂存 {self.request_id}：{error}")
                except Exception:
                    pass
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._delegate is not None:
            return False
        from app.media.recognition.repository import utc_now
        self.finished_at = utc_now()
        if exc_type:
            # 保留异常发生前已经收集的解析、TMDB 和决策数据。整体替换结果会丢失
            # 部分完成请求中最有用的证据。
            self.overall_result = dict(self.overall_result or {})
            self.overall_result.update({"status": "failed", "reason": str(exc_value)})
            self.action("request_error", status="error", output={"error": str(exc_value)})
        if not self.overall_result:
            self.overall_result = {"status": "failed", "reason": "recognition_not_completed"}
        self.overall_result.setdefault("lifecycle", "error" if exc_type else "completed")
        self.overall_result["elapsed_ms"] = int((time.monotonic() - self.started) * 1000)
        self.action("request_finish", status=self.overall_result["status"],
                    output=self.overall_result)
        status = str(self.overall_result.get("status") or "").lower()
        should_persist = (self._record_mode == "all" or
                          self._record_mode == "failed" and
                          status in {"failed", "error", "interrupted"})
        try:
            if not should_persist:
                if self._spool_path:
                    self._remove_spool()
            else:
                if self._spool_path:
                    try:
                        self._spool_path = _write_spool(self.as_dict())
                    except Exception as error:
                        try:
                            import log
                            log.error(f"【Recognition】更新识别暂存失败 {self.request_id}：{error}")
                        except Exception:
                            pass
                from app.helper.db_helper import DbHelper
                saved = DbHelper().insert_recognition_record(self.as_dict())
                if saved and self._spool_path:
                    self._remove_spool()
                elif not saved and not self._spool_path:
                    self._spool_path = _write_spool(self.as_dict())
                if not saved:
                    import log
                    log.warn(f"【Recognition】数据库暂不可写，记录已暂存：{self._spool_path}")
        except Exception as error:
            # 识别记录失败不能影响现有媒体查询行为。
            try:
                import log
                log.error(f"【Recognition】保存识别记录失败：{error}")
            except Exception:
                pass
            if should_persist and not self._spool_path:
                try:
                    self._spool_path = _write_spool(self.as_dict())
                except Exception as spool_error:
                    try:
                        import log
                        log.error(f"【Recognition】记录持久化失败 {self.request_id}：{spool_error}")
                    except Exception:
                        pass
        finally:
            if self._runtime_foreground is not None:
                self._runtime_foreground.__exit__(None, None, None)
                self._runtime_foreground = None
            if self._maintenance_lock is not None:
                self._maintenance_lock.__exit__(None, None, None)
                self._maintenance_lock = None
            if self._spool_path:
                with _spool_lock:
                    _active_spool_paths.discard(self._spool_path)
            if self._token is not None:
                _current.reset(self._token)
            if self._registered:
                global _active_recognitions
                with _recognition_condition:
                    _active_recognitions = max(0, _active_recognitions - 1)
                    self._registered = False
                    _recognition_condition.notify_all()
        return False

    def _remove_spool(self):
        path = self._spool_path
        if not path:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        with _spool_lock:
            _active_spool_paths.discard(path)
        self._spool_path = None

    def action(self, action_type, status="success", input=None, output=None,
               provider_id=None, attempt_id=None, reason=None):
        if self._actions_truncated and action_type != "request_finish":
            return None
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
        from app.media.recognition.trace import redact
        action = redact(action)
        action["offset_ns"] = max(0, time.monotonic_ns()-self.started_ns)
        size = len(json.dumps(action).encode())
        if (self._payload_bytes+size > 256*1024 or len(self.actions) >= 510) and action_type != "request_finish":
            self._actions_truncated = True
            action.update(action_type="events_truncated", input={}, output={"reason": "legacy_audit_limit"})
        self._payload_bytes += min(size, 256*1024)
        self.actions.append(action)
        if self._record_mode == "all" and self.overall_result:
            try:
                from app.media.recognition.compatibility import LegacyHistory
                LegacyHistory().insert(self.as_dict())
            except Exception:
                pass  # The bounded current-schema spool remains recoverable.
        # 每完成一个阶段就写入暂存，确保进程意外退出后能保留有效的部分轨迹，
        # 而不只是 request_start 标记。
        if self._spool_path:
            try:
                self._spool_path = _write_spool(self.as_dict())
            except Exception as error:
                try:
                    import log
                    log.error(f"【Recognition】更新识别暂存失败 {self.request_id}：{error}")
                except Exception:
                    pass
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
        from app.media.recognition.trace import redact
        item = redact(item)
        if len(json.dumps(item).encode()) > 32768:
            item["raw_result"] = {"truncated": True}
        size = len(json.dumps(item).encode())
        if self._payload_bytes+size <= 256*1024 and len(self.provider_results) < 64:
            self.provider_results.append(item)
            self._payload_bytes += size
        self.action("provider_parse", status=status, input=item["input"],
                    output={"attempt_id": attempt_id, "result": item["normalized_result"]},
                    provider_id=provider_id, attempt_id=attempt_id, reason=error)
        return item

    def add_tmdb_result(self, provider_id, query, result, status=None, reason=None):
        status = status or ("success" if result else "no_result")
        item = {"provider_id": provider_id, "query": json_safe(query),
                "status": status, "result": json_safe(result), "reason": reason}
        from app.media.recognition.trace import redact
        item = redact(item)
        size = len(json.dumps(item).encode())
        if self._payload_bytes+size <= 256*1024 and len(self.tmdb_results) < 64:
            self.tmdb_results.append(item)
            self._payload_bytes += size
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
        if "llm_usage" in self.context:
            self.overall_result["llm_usage"] = json_safe(self.context["llm_usage"])
        if self.context.get("shadow_result") is not None:
            self.overall_result["shadow_result"] = json_safe(self.context["shadow_result"])
        metrics = self.context.get("decision_metrics") or {}
        if "candidate_similarities" in metrics:
            self.overall_result["candidate_similarities"] = json_safe(metrics["candidate_similarities"])
            self.overall_result["selection_method"] = metrics.get("selection_method")
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
            "database_generation": _recognition_generation(),
            "schema_version": 3, "record_mode": self._record_mode, "actor_id": self.actor_id,
            "created_at": self.created_at, "finished_at": self.finished_at,
            "started_monotonic_ns": self.started_ns, "in_progress": self.finished_at is None,
            "context": public_context,
            "actions": self.actions,
            "provider_results": self.provider_results,
            "overall_result": self.overall_result,
            "tmdb_results": self.tmdb_results,
        }


def current_recorder():
    return _current.get()


def annotate_business_short_circuit(meta_info, action_type, reason, details=None,
                                    business_status="skipped"):
    """补记名称解析结束后才确定的 RSS/索引器业务短路原因。"""
    request_id = getattr(meta_info, "recognition_request_id", None)
    if not request_id:
        return False
    details = json_safe(details or {})
    recorder = current_recorder()
    if recorder is not None and recorder.request_id == request_id:
        attempt_id = None
        for attempt in reversed(recorder.provider_results):
            if attempt.get("provider_id") == "anitopy_ml" \
                    and attempt.get("status") == "skipped":
                attempt_id = attempt.get("attempt_id")
                if str(attempt.get("error") or "").startswith("ai_deferred"):
                    attempt["error"] = reason
                break
        recorder.action(action_type, status=business_status, provider_id="anitopy_ml",
                        attempt_id=attempt_id, output=details, reason=reason)
        recorder.overall_result["business_result"] = {
            "status": business_status, "reason": reason, "details": details,
        }
        return True
    try:
        from app.helper.db_helper import DbHelper
        if DbHelper().append_recognition_business_action(
                request_id, action_type, reason, details, business_status):
            return True
    except Exception as error:
        try:
            import log
            log.warn(f"【Recognition】补记业务短路原因失败 {request_id}：{error}")
        except Exception:
            pass
    # A retained Trace can be annotated transactionally; missing/none/cleared
    # history is not recreated from an old spool or a copied input.
    return False


def record_business_short_circuit(original_name, source, action_type, reason,
                                  details=None, business_status="skipped"):
    """记录业务短路，同时保留本地基础解析和其他识别器的跳过原因。"""
    from app.media.meta.metainfo import MetaInfo, _meta_snapshot
    from app.media.recognition import registry

    original_name = str(original_name or "")
    details = json_safe(details or {})
    local_meta = None
    local_error = None
    try:
        local_meta = MetaInfo(title=original_name, include_ai=False, record=False)
    except Exception as error:
        local_error = str(error)

    with recognition_scope(original_name, source=source, stage="parse_only") as recorder:
        processed_title = getattr(local_meta, "_recognition_processed_title", original_name)
        processed_subtitle = getattr(local_meta, "_recognition_processed_subtitle", None)
        used_info = getattr(local_meta, "_recognition_used_info", {}) or {}
        if processed_title != original_name or processed_subtitle:
            recorder.action(
                "preprocess", input={"title": original_name},
                output={"title": processed_title, "subtitle": processed_subtitle,
                        "rules": used_info})
        local_status = ("error" if local_error else
                        "success" if local_meta and local_meta.get_name() else "no_result")
        recorder.add_provider_result(
            "local_rules", local_status, input={"title": processed_title},
            normalized_result=_meta_snapshot(local_meta), error=local_error)
        provider_ids = set(registry.discover()) | {"anitopy_ml"}
        for provider_id in sorted(provider_ids - {"local_rules"}):
            recorder.add_provider_result(
                provider_id, "skipped", input={"title": processed_title}, error=reason)
        recorder.action(action_type, status=business_status, provider_id="anitopy_ml",
                        output=details, reason=reason)
        recorder.set_overall(status="skipped", reason=reason,
                             parsed_result=_meta_snapshot(local_meta),
                             selected_provider="local_rules" if local_meta and
                             local_meta.get_name() else None)
        recorder.overall_result["business_result"] = {
            "status": business_status, "reason": reason, "details": details,
        }
        return recorder.request_id


def record_deferred_parse_result(meta_info, original_name, source, action_type, reason,
                                 details=None, business_status="skipped",
                                 overall_status="skipped"):
    """在业务分支确定后保存之前暂存的本地解析和 AI 跳过结果。"""
    from app.media.meta.metainfo import _meta_snapshot
    from app.media.recognition import registry

    processed_title = getattr(meta_info, "_recognition_processed_title", original_name)
    processed_subtitle = getattr(meta_info, "_recognition_processed_subtitle", None)
    used_info = getattr(meta_info, "_recognition_used_info", {}) or {}
    with recognition_scope(original_name, source=source, stage="parse_only") as recorder:
        if processed_title != original_name or processed_subtitle:
            recorder.action(
                "preprocess", input={"title": original_name,
                                      "subtitle": getattr(meta_info, "_recognition_original_subtitle", None)},
                output={"title": processed_title, "subtitle": processed_subtitle,
                        "rules": used_info})
        local_status = "success" if meta_info and meta_info.get_name() else "no_result"
        recorder.add_provider_result(
            "local_rules", local_status,
            input={"title": processed_title, "subtitle": processed_subtitle},
            normalized_result=_meta_snapshot(meta_info))
        provider_ids = set(registry.discover()) | {"anitopy_ml"}
        for provider_id in sorted(provider_ids - {"local_rules"}):
            recorder.add_provider_result(
                provider_id, "skipped", input={"title": processed_title}, error=reason)
        recorder.action(action_type, status=business_status,
                        provider_id="anitopy_ml", output=details or {}, reason=reason)
        recorder.set_overall(
            status=overall_status,
            reason=reason if overall_status != "success" else None,
            parsed_result=_meta_snapshot(meta_info), selected_provider="local_rules")
        recorder.overall_result["business_result"] = {
            "status": business_status, "reason": reason, "details": json_safe(details or {}),
        }
        if meta_info is not None:
            meta_info.recognition_request_id = recorder.request_id
        return recorder.request_id


def initialize_recognition_budget(recorder, settings=None):
    """Pin once, including maintenance/queue time; None is an initialized limit."""
    if recorder is None:
        return None
    if not recorder._budget_initialized:
        settings = (recognition_config("recognition") or {}) if settings is None else settings
        recorder.budget = RecognitionBudget(
            total_timeout_ms(settings), started_ns=int(recorder.started * 1_000_000_000),
            clock_ns=lambda: int(time.monotonic() * 1_000_000_000),
            cancellation=recorder.cancellation)
        if recorder.deadline_monotonic is None:
            deadline_ns = recorder.budget.total_deadline_ns
            recorder.deadline_monotonic = None if deadline_ns is None else deadline_ns / 1_000_000_000
        else:
            recorder.budget.total_deadline_ns = int(recorder.deadline_monotonic * 1_000_000_000)
        recorder._budget_initialized = True
    return recorder.budget


def recognition_budget_active():
    recorder = current_recorder()
    return recorder is not None and (recorder._budget_initialized or recorder.deadline_monotonic is not None)


@contextmanager
def recognition_stage(name):
    """A local stage timeout never cancels other evidence in the request."""
    recorder = current_recorder()
    if recorder is None:
        yield None
        return
    root = initialize_recognition_budget(recorder)
    settings = recognition_config("recognition") or {}
    timeout = (settings.get("timeouts") or {}).get(f"{name}_ms")
    parent = _stage_budget.get() or root
    stage = parent.stage(name, timeout)
    token = _stage_budget.set(stage)
    try:
        yield stage
    finally:
        _stage_budget.reset(token)


def recognition_abort_reason(recorder=None):
    recorder = current_recorder() if recorder is None else recorder
    if recorder is None:
        return None
    if recorder.cancellation.reason is not None:
        return recorder.cancellation.reason.value
    deadline = recorder.deadline_monotonic
    if deadline is not None and time.monotonic() >= deadline:
        return "recognition_deadline_exceeded"
    return None


def recognition_remaining_seconds():
    """返回当前解析请求剩余的网络时间预算；未设置时返回空值。"""
    recorder = current_recorder()
    if recorder is not None and recorder.cancellation.reason is not None:
        return 0.0
    deadline = getattr(recorder, "deadline_monotonic", None) if recorder else None
    limits = [max(deadline - time.monotonic(), 0.0)] if deadline is not None else []
    stage = _stage_budget.get()
    if stage is not None:
        stage_remaining = stage.remaining_seconds()
        if stage_remaining is not None:
            limits.append(stage_remaining)
    return min(limits) if limits else None


def recognition_timeout_reason():
    """Keep stage expiration distinct from cancellation of the whole request."""
    abort = recognition_abort_reason()
    if abort:
        return abort
    stage = _stage_budget.get()
    if stage is not None and stage.remaining_seconds() == 0:
        return f"{stage.name}_deadline_exceeded"
    return "recognition_deadline_exceeded"


def recognition_delay(seconds, *, sleeper=None):
    """Wait cooperatively; retries cannot outlive their request or stage."""
    sleeper = time.sleep if sleeper is None else sleeper
    deadline = time.monotonic() + seconds
    while True:
        remaining = recognition_remaining_seconds()
        if remaining is not None and remaining <= 0:
            return False
        delay = deadline - time.monotonic()
        if delay <= 0:
            return True
        sleeper(min(delay, 0.1, remaining) if remaining is not None else min(delay, 0.1))


def note_tmdb_timeout(error):
    recorder = current_recorder()
    if recorder is not None:
        recorder._tmdb_timeout_count += 1
        recorder._tmdb_timeout_reason = str(error) or "tmdb_request_timeout"


def note_tmdb_failure(reason, error=None):
    """以明确类型向外层解析流程传递 TMDB 搜索失败。"""
    recorder = current_recorder()
    if recorder is not None:
        recorder._tmdb_failure_events.append({
            "reason": str(reason),
            "error": str(error) if error else None,
            "time": time.monotonic(),
        })


def recognition_scope(original_name, source="unknown", stage="resolve", context=None):
    return RecognitionRecorder(original_name, source=source, stage=stage, context=context)


def record_tmdb_call(method):
    """将实际 TMDB 查询结果关联到当前识别请求。"""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        recorder = current_recorder()
        if recorder is None:
            return method(self, *args, **kwargs)
        recognition = recognition_config("recognition") or {}
        if not profile_tmdb_allowed(recorder.stage, recognition):
            recorder.action(
                "tmdb_query", status="skipped", provider_id=recorder.context.get(
                    "active_provider_id") or "local_rules",
                input={"method": method.__name__},
                reason="tmdb_disallowed_by_profile")
            return None
        timeout_count = recorder._tmdb_timeout_count
        failure_count = len(recorder._tmdb_failure_events)
        provider_id = recorder.context.get("active_provider_id") or "local_rules"
        query = {"method": method.__name__, "args": json_safe(args), "kwargs": json_safe(kwargs)}
        started = time.monotonic()
        try:
            result = method(self, *args, **kwargs)
        except Exception as error:
            timed_out = isinstance(error, requests.exceptions.Timeout)
            if timed_out:
                recorder._tmdb_timeout_count += 1
                recorder._tmdb_timeout_reason = str(error) or "tmdb_request_timeout"
            else:
                recorder._tmdb_failure_events.append({
                    "reason": "tmdb_network_error", "error": str(error),
                    "time": time.monotonic(),
                })
            recorder.add_tmdb_result(
                provider_id=provider_id,
                query=query,
                result=None,
                status="timeout" if timed_out else "error",
                reason=str(error),
            )
            raise
        new_failures = recorder._tmdb_failure_events[failure_count:]
        failure = next((item for item in reversed(new_failures)
                        if item["reason"] == "ambiguous_tmdb"), None)
        if failure is None and new_failures:
            priority = {"tmdb_network_error": 3, "tmdb_no_results": 1,
                        "no_tmdb_match": 2, "ambiguous_tmdb": 4}
            failure = max(new_failures, key=lambda item: priority.get(item["reason"], 0))
        result_status = ("timeout" if recorder._tmdb_timeout_count > timeout_count else
                         "success" if result else
                         failure["reason"] if failure else "no_result")
        recorder.add_tmdb_result(
            provider_id=provider_id,
            query=query,
            result=result,
            status=result_status,
            reason=(recorder._tmdb_timeout_reason if recorder._tmdb_timeout_count > timeout_count else
                    ((failure.get("error") or failure["reason"]) if failure and not result else
                     f"elapsed_ms={int((time.monotonic() - started) * 1000)}")),
        )
        return result
    return wrapped
