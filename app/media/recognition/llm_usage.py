"""Reported token usage only; unknown, partial and no-call cases stay distinct."""

from copy import deepcopy
import uuid

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens", "reasoning_tokens")
PRIMARY_FIELDS = TOKEN_FIELDS[:3]


def token_count(value):
    return value if type(value) is int and 0 <= value <= 9007199254740991 else None


def extract_usage(response):
    data = response.get("usage") if isinstance(response, dict) else None
    data = data if isinstance(data, dict) else {}
    result = {name: token_count(data.get(name)) for name in TOKEN_FIELDS}
    aliases = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens"}
    for name, alias in aliases.items():
        if result[name] is None:
            result[name] = token_count(data.get(alias))
    for name, containers, field in (
        ("prompt_cache_hit_tokens", ("prompt_tokens_details", "input_tokens_details"), "cached_tokens"),
        ("reasoning_tokens", ("completion_tokens_details", "output_tokens_details"), "reasoning_tokens"),
    ):
        for container in containers:
            nested = data.get(container)
            if result[name] is None and isinstance(nested, dict):
                result[name] = token_count(nested.get(field))
    return result


def sum_usage(items):
    items = list(items)
    result = {}
    for name in TOKEN_FIELDS:
        values = [token_count(item.get(name)) for item in items]
        known = [value for value in values if value is not None]
        result[name] = sum(known) if known else None if items else 0
    return result


def attempt_usage(attempts):
    result = sum_usage(item["usage"] for item in attempts)
    unknown = sum(any(item["usage"].get(name) is None for name in PRIMARY_FIELDS) for item in attempts)
    return {**result, "api_calls": len(attempts), "unknown_calls": unknown, "usage_complete": unknown == 0}


def combine_usage(items):
    items = list(items)
    result = sum_usage(items)
    result["api_calls"] = None if any(item.get("api_calls") is None for item in items) else sum(item["api_calls"] for item in items)
    result["unknown_calls"] = sum(item.get("unknown_calls", 0) for item in items)
    result["usage_complete"] = all(item.get("usage_complete") is True for item in items)
    return result


def parser_metadata(options, raw=None, *, cached=False, skipped=False):
    """Only this adapter's bounded diagnostic allowlist can enter a trace."""
    from app.media.recognition.llm_protocol import prompt_version
    raw = raw if isinstance(raw, dict) else {}
    mode = options.get("extraction_mode", "core")
    attempts = []
    for item in (raw.get("attempts") or [])[:4]:
        if not isinstance(item, dict):
            continue
        attempts.append({"attempt_index": len(attempts)+1, "http_status": token_count(item.get("http_status")),
            "finish_reason": item.get("finish_reason") if item.get("finish_reason") in {"stop", "length", "content_filter", "tool_calls", "insufficient_system_resource"} else None,
            "latency_ms": token_count(item.get("latency_ms")), "usage": extract_usage({"usage": item.get("usage")})})
    usage = attempt_usage(attempts)
    if not attempts and not cached and not skipped and "attempts" not in raw:
        usage = {**{name: None for name in TOKEN_FIELDS}, "api_calls": None, "unknown_calls": 1, "usage_complete": False}
    return {"call_id": uuid.uuid4().hex, "service_type": options.get("service_type", "compatible"),
        "model": str(options.get("model", ""))[:512], "extraction_mode": mode, "prompt_version": prompt_version(mode),
        "usage": usage, "attempts": attempts, "cache_hit": cached,
        **{name: deepcopy(raw[name]) for name in ("format", "token_parameter", "downgrades", "metadata_warnings") if name in raw}}
