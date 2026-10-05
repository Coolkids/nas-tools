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
from enum import Enum
from threading import Lock
from app.media.recognition.settings import provider_enabled, profile_tmdb_allowed


_current = contextvars.ContextVar("media_recognition_recorder", default=None)
_spool_lock = Lock()
_active_spool_paths = set()


def _spool_directory():
    from config import Config
    return os.path.join(Config().get_config_path(), "recognition_spool")


def _write_spool(payload):
    directory = _spool_directory()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    final_path = os.path.join(directory, f"{payload['request_id']}.json")
    temporary_path = final_path + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(temporary_path, "x", encoding="utf-8") as spool_file:
        spool_file.write(encoded)
        spool_file.flush()
        os.fsync(spool_file.fileno())
    os.replace(temporary_path, final_path)
    try:
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass
    return final_path


def replay_recognition_spool():
    """数据库恢复可写后，回放已暂存的识别记录。"""
    with _spool_lock:
        try:
            directory = _spool_directory()
            paths = [os.path.join(directory, name) for name in os.listdir(directory)
                     if name.endswith(".json") and os.path.join(directory, name)
                     not in _active_spool_paths]
        except FileNotFoundError:
            return {"replayed": 0, "pending": 0, "invalid": 0}
        except Exception as error:
            try:
                import log
                log.error(f"【Recognition】读取识别暂存目录失败：{error}")
            except Exception:
                pass
            return {"replayed": 0, "pending": 0, "invalid": 0}

        replayed = 0
        invalid = 0
        for path in sorted(paths):
            try:
                with open(path, "r", encoding="utf-8") as spool_file:
                    payload = json.load(spool_file)
                if not isinstance(payload, dict) or not payload.get("request_id"):
                    invalid += 1
                    import log
                    log.error(f"【Recognition】识别暂存记录格式无效，保留文件供排查：{path}")
                    continue
                overall = payload.get("overall_result")
                actions = payload.get("actions")
                if not isinstance(overall, dict) or not isinstance(actions, list):
                    invalid += 1
                    import log
                    log.error(f"【Recognition】识别暂存记录结构无效，保留文件供排查：{path}")
                    continue
                if overall.get("lifecycle") == "running" or overall.get("status") == "running":
                    overall.update({
                        "status": "failed",
                        "reason": "process_interrupted",
                        "lifecycle": "interrupted",
                    })
                    sequence = max((int(action.get("sequence", 0)) for action in actions
                                    if isinstance(action, dict)), default=0) + 1
                    actions.append({
                        "action_id": f"{payload['request_id']}:{sequence}",
                        "sequence": sequence,
                        "action_type": "request_recovered",
                        "provider_id": None,
                        "attempt_id": None,
                        "status": "interrupted",
                        "input": {},
                        "output": {"reason": "process_interrupted"},
                        "reason": "process_interrupted",
                        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
                    })
                from app.helper.db_helper import DbHelper
                if DbHelper().insert_recognition_record(payload):
                    os.remove(path)
                    replayed += 1
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                invalid += 1
                try:
                    import log
                    log.error(f"【Recognition】识别暂存记录损坏，保留文件供排查 {os.path.basename(path)}：{error}")
                except Exception:
                    pass
            except Exception as error:
                try:
                    import log
                    log.error(f"【Recognition】回放识别记录失败 {os.path.basename(path)}：{error}")
                except Exception:
                    pass
        pending = len(paths) - replayed
        if pending:
            try:
                import log
                log.warn(f"【Recognition】识别暂存恢复完成：成功 {replayed} 条，待处理 {pending} 条，无效 {invalid} 条；查看 recognition_spool 目录日志")
            except Exception:
                pass
        return {"replayed": replayed, "pending": pending, "invalid": invalid}


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
        self._spool_path = None
        self._runtime_config = None
        self.deadline_monotonic = None
        self._tmdb_timeout_count = 0
        self._tmdb_timeout_reason = None
        self._tmdb_failure_events = []

    def __enter__(self):
        parent = _current.get()
        if parent is not None:
            # 内部解析调用属于外层请求；尤其是批量文件请求，不能被 AI 流程中的
            # 嵌套 get_media_info() 替换。
            self._delegate = parent
            return parent
        self._runtime_config = _runtime_config_snapshot()
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
        try:
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
                os.remove(self._spool_path)
                self._spool_path = None
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
            if not self._spool_path:
                try:
                    self._spool_path = _write_spool(self.as_dict())
                except Exception as spool_error:
                    try:
                        import log
                        log.error(f"【Recognition】记录持久化失败 {self.request_id}：{spool_error}")
                    except Exception:
                        pass
        finally:
            if self._spool_path:
                with _spool_lock:
                    _active_spool_paths.discard(self._spool_path)
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
    # DB 暂不可用时，修改原请求的持久化暂存，不新建重复请求记录。
    try:
        spool_path = os.path.join(_spool_directory(), f"{request_id}.json")
        if not os.path.exists(spool_path):
            return False
        with open(spool_path, "r", encoding="utf-8") as spool_file:
            payload = json.load(spool_file)
        actions = payload.setdefault("actions", [])
        attempts = payload.setdefault("provider_results", [])
        sequence = max((int(item.get("sequence", 0)) for item in actions
                        if isinstance(item, dict)), default=0) + 1
        attempt_id = None
        for attempt in reversed(attempts):
            if attempt.get("provider_id") == "anitopy_ml" \
                    and attempt.get("status") == "skipped":
                attempt_id = attempt.get("attempt_id")
                if str(attempt.get("error") or "").startswith("ai_deferred"):
                    attempt["error"] = reason
                break
        actions.append({
            "action_id": f"{request_id}:{sequence}", "sequence": sequence,
            "action_type": action_type, "provider_id": "anitopy_ml",
            "attempt_id": attempt_id, "status": business_status, "input": {},
            "output": details, "reason": reason,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
        })
        payload.setdefault("overall_result", {})["business_result"] = {
            "status": business_status, "reason": reason, "details": details,
        }
        _write_spool(payload)
        return True
    except Exception:
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


def recognition_remaining_seconds():
    """返回当前解析请求剩余的网络时间预算；未设置时返回空值。"""
    recorder = current_recorder()
    deadline = getattr(recorder, "deadline_monotonic", None) if recorder else None
    return max(deadline - time.monotonic(), 0.0) if deadline is not None else None


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
