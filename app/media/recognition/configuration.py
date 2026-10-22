"""多引擎配置约定及升级转换。

迁移是纯操作：调用方必须在现有配置锁内显式保存结果。导入模块或处理 GET 请求都不会升级正在运行的安装。
"""

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from urllib.parse import urlsplit

from app.media.recognition.settings import (
    migrate_legacy_ai_provider, migrate_legacy_parse_ttl, recognition_record_mode,
)


TIMEOUT_STAGES = (
    "total", "queue", "preprocess", "anitopy", "anitopy_ml", "openai",
    "query_plan", "tmdb_request", "tmdb_stage", "rank", "episode", "fallback",
)
CACHE_LAYERS = ("identity", "search", "detail", "episode")
CACHE_BACKENDS = ("memory", "memory_disk", "redis", "valkey")
MAX_DURATION_SECONDS = 36500 * 86400


class RecognitionConfigurationError(ValueError):
    """无效字段；错误信息有意不包含字段值。"""

    def __init__(self, field, requirement):
        self.field = field
        super().__init__(f"{field}: {requirement}")


class RecognitionConfigurationConflict(ValueError):
    pass


def recognition_v3_defaults():
    return {
        "recognition": {
            "schema_version": 3,
            "execution": {
                "mode": "complete_parallel",
                "progressive_early_stop": False,
                "allow_partial_success_on_deadline": False,
                "max_running_tasks": 4, "max_queued_tasks": 100,
                "max_inflight_provider_requests": 4,
            },
            "timeouts": {f"{name}_ms": None for name in TIMEOUT_STAGES},
            "providers": {
                "anitopy": {"enabled": True},
                "anitopy_ml": {"enabled": False, "endpoint": "", "model_revision": "unknown"},
                "openai": {
                    "enabled": False, "service_type": "compatible", "extraction_mode": "core", "base_url": "", "model": "", "temperature": 0,
                    "max_output_tokens": 256, "response_format_mode": "auto",
                    "token_parameter": "auto",
                },
            },
            "profiles": {
                "parse_only": {"network_allowed": True, "tmdb_allowed": False},
                "resolve": {"network_allowed": True, "tmdb_allowed": True},
            },
            "query": {
                "max_queries": 8, "max_pages_per_query": 2, "candidates_per_query": 40,
                "detail_top_k": 5, "tmdb_concurrency": 4, "tmdb_qps": 4, "tmdb_retry_max": 2,
                "episode_details_enabled": False, "prefer_english": True,
            },
            "matching": {
                "min_name_similarity": 0.85, "match_threshold": 0.90, "min_score_gap": 0.08,
                "preferred_type": "tv", "preferred_genre_id": 16, "adult_policy": "exclude",
                "weights": {"title": .55, "type_episode": .20, "year": .15, "consensus": .07, "popularity": .03},
            },
            "cache": {"enabled": True, "parse": {"enabled": True, "singleflight": True}},
            "cache_v3": {
                "stale_grace_seconds": 3600, "hot_refresh_enabled": True,
                "refresh_ahead_seconds": 1800, "refresh_scan_interval_seconds": 300,
                "refresh_concurrency": 2,
            },
            "records": {
                "mode": "failed", "cleanup": {
                    "enabled": True, "interval_hours": 24,
                    "retention_value": 30, "retention_unit": "day", "max_records": 200,
                },
            },
            "tracing": {"stream_enabled": True},
        },
        "cache": {"backend": "memory_disk", "ttl_seconds": {
            "recognition_identity": 0, "recognition_search": 86400,
            "recognition_detail": 604800, "recognition_episode": 259200,
        }},
    }


def _object(value, field):
    if not isinstance(value, dict):
        raise RecognitionConfigurationError(field, "必须是对象")
    return value


def _merge(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _integer(value, field, minimum=1, maximum=None, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        raise RecognitionConfigurationError(field, "必须是允许范围内的整数" + ("或null" if nullable else ""))


def _boolean(value, field):
    if type(value) is not bool:
        raise RecognitionConfigurationError(field, "必须是布尔值")


def _choice(value, field, choices):
    if not isinstance(value, str) or value not in choices:
        raise RecognitionConfigurationError(field, "不支持的选项")


def _number(value, field, minimum, maximum=None, exclusive_min=False):
    if type(value) not in (int, float) or type(value) is float and not math.isfinite(value):
        raise RecognitionConfigurationError(field, "必须是有限数值")
    if value < minimum or exclusive_min and value == minimum or maximum is not None and value > maximum:
        raise RecognitionConfigurationError(field, "数值超出允许范围")


def _endpoint(value, field):
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
        valid = valid and parsed.username is None and parsed.password is None and not parsed.fragment and not parsed.query
        parsed.port  # 校验端口语法是否有误，不发送请求。
    except ValueError:
        valid = False
    if not valid:
        raise RecognitionConfigurationError(field, "启用时必须提供有效的HTTP(S)地址，不在URL内放凭据")


def validate_recognition_v3(config):
    """校验规范化后的有效设置，不将 null 或布尔值强制转换。"""
    root = _object(config, "config")
    rec = _object(root.get("recognition"), "recognition")
    reject_retired_settings(root)
    if type(rec.get("schema_version")) is not int or rec["schema_version"] != 3:
        raise RecognitionConfigurationError("recognition.schema_version", "必须是3")
    for section in ("execution", "timeouts", "providers", "query", "matching", "cache",
                    "cache_v3", "records", "tracing"):
        _object(rec.get(section), f"recognition.{section}")
    execution = rec["execution"]
    if "implementation" in execution:
        raise RecognitionConfigurationError("recognition.execution.implementation", "已移除此选项，统一使用多引擎识别")
    _choice(execution.get("mode"), "recognition.execution.mode", ("complete_parallel",))
    for key in ("max_running_tasks", "max_queued_tasks", "max_inflight_provider_requests"):
        _integer(execution.get(key), f"recognition.execution.{key}")
    for key in ("progressive_early_stop", "allow_partial_success_on_deadline"):
        _boolean(execution.get(key), f"recognition.execution.{key}")
    if execution["progressive_early_stop"]:
        raise RecognitionConfigurationError("recognition.execution.progressive_early_stop", "首版只支持完整并行")
    if set(rec["timeouts"]) != {f"{stage}_ms" for stage in TIMEOUT_STAGES}:
        raise RecognitionConfigurationError("recognition.timeouts", "必须包含且仅包含已定义的12个阶段")
    for key, value in rec["timeouts"].items():
        _integer(value, f"recognition.timeouts.{key}", nullable=True)
    for provider in ("anitopy", "anitopy_ml", "openai"):
        options = _object(rec["providers"].get(provider), f"recognition.providers.{provider}")
        _boolean(options.get("enabled"), f"recognition.providers.{provider}.enabled")
    if not rec["providers"]["anitopy"]["enabled"]:
        raise RecognitionConfigurationError("recognition.providers.anitopy.enabled", "Anitopy必须启用")
    for key in ("endpoint", "model_revision"):
        if not isinstance(rec["providers"]["anitopy_ml"].get(key), str):
            raise RecognitionConfigurationError(f"recognition.providers.anitopy_ml.{key}", "必须是字符串")
    if rec["providers"]["anitopy_ml"]["enabled"] or rec["providers"]["anitopy_ml"]["endpoint"]:
        _endpoint(rec["providers"]["anitopy_ml"]["endpoint"], "recognition.providers.anitopy_ml.endpoint")
    llm = rec["providers"]["openai"]
    _choice(llm.get("service_type"), "recognition.providers.openai.service_type",
            ("compatible", "ollama", "llama_cpp", "deepseek"))
    _choice(llm.get("extraction_mode"), "recognition.providers.openai.extraction_mode", ("core", "full"))
    for key in ("base_url", "model"):
        if not isinstance(llm.get(key), str):
            raise RecognitionConfigurationError(f"recognition.providers.openai.{key}", "必须是字符串")
    if llm["enabled"] or llm["base_url"]:
        _endpoint(llm["base_url"], "recognition.providers.openai.base_url")
    if llm["enabled"]:
        if not llm["model"].strip():
            raise RecognitionConfigurationError("recognition.providers.openai.model", "启用时必须指定模型")
    _number(llm.get("temperature"), "recognition.providers.openai.temperature", 0, 2)
    _integer(llm.get("max_output_tokens"), "recognition.providers.openai.max_output_tokens")
    _choice(llm.get("response_format_mode"), "recognition.providers.openai.response_format_mode",
            ("auto", "json_schema", "json_object", "prompt"))
    _choice(llm.get("token_parameter"), "recognition.providers.openai.token_parameter",
            ("auto", "max_tokens", "max_completion_tokens"))
    query = rec["query"]
    for key in ("max_queries", "max_pages_per_query", "candidates_per_query", "detail_top_k", "tmdb_concurrency"):
        _integer(query.get(key), f"recognition.query.{key}", nullable=key == "max_queries")
    _integer(query.get("tmdb_retry_max"), "recognition.query.tmdb_retry_max", minimum=0)
    _number(query.get("tmdb_qps"), "recognition.query.tmdb_qps", 0, exclusive_min=True)
    _boolean(query.get("episode_details_enabled"), "recognition.query.episode_details_enabled")
    _boolean(query.get("prefer_english"), "recognition.query.prefer_english")
    matching = rec["matching"]
    for key in ("min_name_similarity", "match_threshold", "min_score_gap"):
        _number(matching.get(key), f"recognition.matching.{key}", 0, 1, exclusive_min=key != "min_score_gap")
    _choice(matching.get("preferred_type"), "recognition.matching.preferred_type", ("movie", "tv", "unknown"))
    _integer(matching.get("preferred_genre_id"), "recognition.matching.preferred_genre_id", minimum=0)
    _choice(matching.get("adult_policy"), "recognition.matching.adult_policy", ("exclude", "lowest", "include"))
    weights = _object(matching.get("weights"), "recognition.matching.weights")
    if set(weights) != {"title", "type_episode", "year", "consensus", "popularity"}:
        raise RecognitionConfigurationError("recognition.matching.weights", "必须包含已定义的五种权重")
    for key, value in weights.items():
        _number(value, f"recognition.matching.weights.{key}", 0, 1)
    if weights["title"] <= 0 or not math.isclose(sum(weights.values()), 1, abs_tol=1e-9):
        raise RecognitionConfigurationError("recognition.matching.weights", "标题权重必须大于零，权重总和必须为1")
    _boolean(rec["cache"].get("enabled"), "recognition.cache.enabled")
    parse = _object(rec["cache"].get("parse"), "recognition.cache.parse")
    for key in ("enabled", "singleflight"):
        _boolean(parse.get(key), f"recognition.cache.parse.{key}")
    policy = rec["cache_v3"]
    if "enabled" in policy:
        raise RecognitionConfigurationError("recognition.cache_v3.enabled", "请使用现有recognition.cache.enabled总开关")
    _boolean(policy.get("hot_refresh_enabled"), "recognition.cache_v3.hot_refresh_enabled")
    for key in ("stale_grace_seconds", "refresh_ahead_seconds", "refresh_scan_interval_seconds"):
        _integer(policy.get(key), f"recognition.cache_v3.{key}",
                 minimum=0 if key == "stale_grace_seconds" else 1, maximum=MAX_DURATION_SECONDS)
    _integer(policy.get("refresh_concurrency"), "recognition.cache_v3.refresh_concurrency", maximum=32)
    _choice(rec["records"].get("mode"), "recognition.records.mode", ("all", "failed", "none"))
    cleanup = _object(rec["records"].get("cleanup"), "recognition.records.cleanup")
    _boolean(cleanup.get("enabled"), "recognition.records.cleanup.enabled")
    _integer(cleanup.get("interval_hours"), "recognition.records.cleanup.interval_hours", maximum=8760)
    _choice(cleanup.get("retention_unit"), "recognition.records.cleanup.retention_unit", ("day", "month"))
    _integer(cleanup.get("retention_value"), "recognition.records.cleanup.retention_value",
             maximum=36500 if cleanup["retention_unit"] == "day" else 1200)
    _integer(cleanup.get("max_records"), "recognition.records.cleanup.max_records")
    _boolean(rec["tracing"].get("stream_enabled"), "recognition.tracing.stream_enabled")
    cache = _object(root.get("cache"), "cache")
    _choice(cache.get("backend"), "cache.backend", CACHE_BACKENDS)
    ttls = _object(cache.get("ttl_seconds"), "cache.ttl_seconds")
    for layer in CACHE_LAYERS:
        _integer(ttls.get(f"recognition_{layer}"), f"cache.ttl_seconds.recognition_{layer}",
                 minimum=0, maximum=MAX_DURATION_SECONDS)


def migrate_recognition_v3(config):
    """将旧设置升级为统一的多引擎运行配置，不修改原数据。"""
    current = deepcopy(_object(config, "config"))
    rec = _object(current.setdefault("recognition", {}), "recognition")
    schema = rec.get("schema_version", 0)
    if type(schema) is not int or not 0 <= schema <= 3:
        raise RecognitionConfigurationError("recognition.schema_version", "不支持此结构版本")
    migrate_legacy_ai_provider(current)
    migrate_legacy_parse_ttl(current)
    rec = current["recognition"]
    if schema < 3:
        execution = _object(rec.setdefault("execution", {}), "recognition.execution")
        timeouts = _object(rec.setdefault("timeouts", {}), "recognition.timeouts")
        if "total_ms" not in timeouts:
            seconds = execution.get("total_timeout_seconds", 30)
            if seconds is None:
                timeouts["total_ms"] = None
            else:
                try:
                    millis = Decimal(str(seconds)) * 1000
                    if isinstance(seconds, bool) or not millis.is_finite() or millis <= 0 or millis != millis.to_integral_value():
                        raise InvalidOperation
                    timeouts["total_ms"] = int(millis)
                except (InvalidOperation, ValueError):
                    raise RecognitionConfigurationError("recognition.execution.total_timeout_seconds", "无法无损转换为正整数毫秒") from None
        records = _object(rec.setdefault("records", {}), "recognition.records")
        records["mode"] = recognition_record_mode(rec)
        cleanup = _object(records.setdefault("cleanup", {}), "recognition.records.cleanup")
        if "retention_value" not in cleanup and "retention_days" in cleanup:
            cleanup["retention_value"] = cleanup["retention_days"]
            cleanup.setdefault("retention_unit", "day")
    rec["schema_version"] = 3
    # 将原实验室开关迁移到规范的识别设置中。
    # 保留 recognition.query 中显式设置的优先级；旧安装
    # 沿用原有实验室开关值，新安装默认启用。
    query = _object(rec.setdefault("query", {}), "recognition.query")
    if "prefer_english" not in query:
        laboratory = current.get("laboratory") or {}
        query["prefer_english"] = laboratory.get("search_en_title", True)
    execution = _object(rec.setdefault("execution", {}), "recognition.execution")
    for key in ("implementation", "max_concurrency", "total_timeout_seconds"):
        execution.pop(key, None)
    execution["mode"] = "complete_parallel"
    rec.pop("decision", None)
    providers = _object(rec.setdefault("providers", {}), "recognition.providers")
    providers.pop("local_rules", None)
    for options in providers.values():
        if isinstance(options, dict):
            options.pop("reliability", None)
    _object(providers.setdefault("anitopy", {}), "recognition.providers.anitopy")["enabled"] = True
    for profile in (rec.get("profiles") or {}).values():
        if isinstance(profile, dict):
            profile.pop("providers", None)
            profile.pop("conflict_policy", None)
    (rec.get("records", {}).get("cleanup") or {}).pop("retention_days", None)
    migrated = _merge(recognition_v3_defaults(), current)
    validate_recognition_v3(migrated)
    return migrated


def recognition_settings_version(config):
    relevant = {"recognition": config.get("recognition", {}), "cache": config.get("cache", {})}
    canonical = json.dumps(relevant, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prepare_recognition_settings_update(current, patch, expected_version):
    """准备经过校验的更新；持久化和权限检查由调用方负责。"""
    if not isinstance(expected_version, str) or expected_version != recognition_settings_version(current):
        raise RecognitionConfigurationConflict("识别配置已变化，请重新读取后保存")
    patch = _object(patch, "patch")
    reject_retired_settings(patch)
    if set(patch) - {"recognition", "cache"}:
        raise RecognitionConfigurationError("patch", "只能修改识别与共享缓存设置")
    if "cache" in patch and set(_object(patch["cache"], "cache")) - {"ttl_seconds"}:
        raise RecognitionConfigurationError("cache", "缓存连接与迁移通过缓存管理页面修改")
    provider_patch = _object(patch.get("recognition", {}), "recognition").get("providers", {})
    def contains_secret(value):
        if not isinstance(value, dict):
            return False
        return any(str(key).rsplit(".", 1)[-1].replace("_", "").replace("-", "").casefold()
                   in {"apikey", "authorization", "token", "password", "secret"}
                   or contains_secret(item) for key, item in value.items())
    for provider, options in _object(provider_patch, "recognition.providers").items():
        if contains_secret(options):
            raise RecognitionConfigurationError(f"recognition.providers.{provider}", "密钥必须通过加密凭据接口保存")
    if contains_secret(patch):
        raise RecognitionConfigurationError("patch", "密钥必须通过加密凭据接口保存")
    merged = _merge(current, patch)
    validate_recognition_v3(merged)
    try:
        json.dumps(patch, allow_nan=False)
    except (TypeError, ValueError):
        raise RecognitionConfigurationError("patch", "必须包含有效的JSON值") from None
    return merged


def reject_retired_settings(value, prefix=""):
    """拒绝写入已移除的运行时控制项；迁移仍可读取这些旧项。"""
    if not isinstance(value, dict):
        return
    retired = ("recognition.execution.implementation", "recognition.decision",
               "recognition.providers.local_rules", "recognition.execution.max_concurrency",
               "recognition.execution.total_timeout_seconds")
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if any(path == name or path.startswith(name + ".") for name in retired) or (
                path.startswith("recognition.profiles.") and path.endswith((".providers", ".conflict_policy"))):
            raise RecognitionConfigurationError(path, "已移除此选项，统一使用多引擎识别")
        reject_retired_settings(item, path)


def recognition_cache_capabilities(config, actual_backend, storage_generation=None):
    _choice(actual_backend, "actual_backend", CACHE_BACKENDS)
    requested = config["recognition"]["cache_v3"]["hot_refresh_enabled"]
    enabled = config["recognition"]["cache"]["enabled"]
    supported = actual_backend != "memory"
    effective = enabled and requested and supported
    reason = "cache_disabled" if not enabled else "backend_unsupported" if not supported else "disabled_by_user" if not requested else None
    ahead = config["recognition"]["cache_v3"]["refresh_ahead_seconds"]
    windows = {}
    for layer in CACHE_LAYERS:
        ttl = config["cache"]["ttl_seconds"]["recognition_"+layer]
        windows[layer] = {"requested_seconds": ahead, "effective_seconds": None if ttl == 0 else ttl/2 if ahead >= ttl else ahead,
                          "reason": "never_expires" if ttl == 0 else "limited_to_half_ttl" if ahead >= ttl else None}
    return {"configured_backend": config["cache"]["backend"], "actual_backend": actual_backend,
            "cache_enabled": enabled, "hot_refresh_supported": supported,
            "requested_enabled": requested, "effective_enabled": effective,
            "reason": reason, "storage_generation": storage_generation, "refresh_windows": windows}
