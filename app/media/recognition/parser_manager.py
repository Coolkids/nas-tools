"""同时提交相互独立的解析器；完成顺序不会决定胜出结果。"""

from dataclasses import dataclass
from dataclasses import replace
import time

from app.media.recognition.budget import RecognitionBudget, effective_deadline_reason
from app.media.recognition.contracts import NormalizedParse, ParseResult
from app.media.recognition.provider_schema import normalized, signature
from app.media.recognition.registry import registry
from app.media.recognition.runtime import WorkItem, ResourceBusy, get_runtime
from app.media.recognition import cache as parse_cache
from app.media.recognition.llm_usage import parser_metadata
from app.media.recognition.llm_protocol import RELEASE_FIELDS


@dataclass(frozen=True)
class ParserBatch:
    results: tuple[NormalizedParse, ...]
    signatures: dict[str, str]
    diagnostics: tuple[dict, ...]

    @property
    def successful(self):
        return tuple(item for item in self.results if item.status == "success")


class ParserManager:
    def __init__(self, settings, *, runtime=None, budget=None, emit=None):
        self.settings = settings.get("recognition", settings)
        self.runtime = runtime
        self.budget = budget or RecognitionBudget((self.settings.get("timeouts") or {}).get("total_ms"))
        self.emit = emit or (lambda *args, **kwargs: None)

    def run(self, prepared, *, network_allowed=True, cache=True, provider_ids=None):
        order = ("anitopy", "anitopy_ml", "openai")
        selected = order if provider_ids is None else tuple(provider_ids)
        if not selected or len(set(selected)) != len(selected) or any(name not in order for name in selected):
            raise ValueError("invalid_parser_selection")
        pending, results, signatures, diagnostics, cache_keys, provider_raw = {}, {}, {}, [], {}, {}
        cache_settings = self.settings.get("cache", {})
        providers = registry.discover()
        config = self.settings["providers"]
        for provider_id in selected:
            if provider_id not in providers:
                results[provider_id] = NormalizedParse(provider_id, "unavailable", provider_id, "error", error="provider_unavailable")
                continue
            descriptor = providers[provider_id].descriptor
            configured = dict(config[provider_id])
            enabled = provider_id == "anitopy" or configured.get("enabled") is True
            reason = "provider_disabled" if not enabled else "network_disallowed" if descriptor.network_required and not network_allowed else None
            options = {key: value for key, value in configured.items()
                       if key not in {"enabled", "reliability", "api_key_set", "api_key", "options",
                                      "credential_status", "credential_message"}}
            if provider_id == "anitopy":
                options = {}
            if provider_id == "anitopy_ml":
                options["strict"] = True
            if provider_id == "openai" and reason is None:
                from app.media.recognition.secrets import SecretManager
                try:
                    options["secret_revision"] = SecretManager().metadata()["secret_revision"]
                except Exception:
                    results[provider_id] = NormalizedParse(provider_id, descriptor.version, descriptor.evidence_family,
                        "error", error="credential_store_unavailable")
                    continue
            signatures[provider_id] = signature(provider_id, descriptor.version, prepared, options)
            if reason:
                results[provider_id] = NormalizedParse(provider_id, descriptor.version, descriptor.evidence_family, "skipped", error=reason)
                continue
            key = parse_cache.cache_key("v3-parse", {"title": prepared.clean_title,
                "secondary_title": prepared.clean_secondary_title, "signature": signatures[provider_id]})
            try:
                generation = parse_cache.generation(settings=cache_settings) if cache else None
            except Exception:
                generation = None
                diagnostics.append({"provider_id": provider_id, "error": "parse_cache_unavailable"})
            cache_keys[provider_id] = key, generation
            if cache and generation is not None and not self.budget.failure_reason:
                try:
                    value = parse_cache.get("parse", key, settings=cache_settings)
                except Exception:
                    value = parse_cache.CACHE_MISS
                    diagnostics.append({"provider_id": provider_id, "error": "parse_cache_read_failed"})
                if value is not parse_cache.CACHE_MISS:
                    try:
                        release = None
                        if provider_id == "openai":
                            if not isinstance(value, dict) or set(value) != {"extraction", "release_metadata"}:
                                raise ValueError("invalid_llm_parse_cache")
                            release = value["release_metadata"]
                            expected = set(RELEASE_FIELDS) if configured.get("extraction_mode") == "full" else set()
                            if (not isinstance(release, dict) or set(release) != expected or
                                    any(item is not None and (not isinstance(item, str) or len(item) > 1024) for item in release.values())):
                                raise ValueError("invalid_llm_release_cache")
                            value = value["extraction"]
                        parsed = normalized(value, provider_id, descriptor.version, descriptor.evidence_family, prepared=prepared)
                    except (ValueError, TypeError):
                        pass
                    else:
                        results[provider_id] = replace(parsed, cache_source="cache", release_metadata=release,
                            llm=parser_metadata(configured, cached=True) if provider_id == "openai" else None)
                        continue
            stage = self.budget.stage(provider_id, self.settings["timeouts"].get(provider_id+"_ms"))
            left = stage.remaining_seconds()
            options["timeout"] = left
            if left is not None and left <= 0 or self.budget.cancellation.reason is not None:
                results[provider_id] = NormalizedParse(provider_id, descriptor.version, descriptor.evidence_family,
                    "cancelled" if self.budget.cancellation.reason else "timeout", error=self.budget.failure_reason or provider_id+"_deadline_exceeded")
                continue
            try:
                runtime = self.runtime or get_runtime()
                queue_ms = self.settings["timeouts"].get("queue_ms")
                future = runtime.submit("parser", WorkItem("parse", {"provider_id": provider_id,
                    "title": prepared.clean_title, "subtitle": prepared.clean_secondary_title,
                    "options": options, "context": {"remaining_timeout_seconds": left}}),
                    deadline=time.monotonic()+left if left is not None else None, cancellation=self.budget.cancellation,
                    deadline_reason=effective_deadline_reason(stage),
                    queue_deadline=time.monotonic()+queue_ms/1000 if queue_ms is not None else None)
                pending[provider_id] = future
            except ResourceBusy:
                results[provider_id] = NormalizedParse(provider_id, descriptor.version, descriptor.evidence_family, "error", error="parser_busy")
        # 所有任务都先提交，再开始等待。网络调用由工作进程执行，且可被终止。
        for provider_id, future in pending.items():
            work = future.result()
            descriptor = providers[provider_id].descriptor
            diagnostics.append({"provider_id": provider_id, "status": work.status, "pid": work.pid,
                "resource_released_at": work.resource_released_at})
            value = work.value if work.status == "success" else ParseResult(provider_id, work.status, error=work.reason)
            if not isinstance(value, ParseResult):
                value = ParseResult(provider_id, "invalid", error="invalid_parser_result")
            if provider_id == "openai":
                provider_raw[provider_id] = value.raw_result
            if self.budget.failure_reason:
                value = ParseResult(provider_id, "cancelled" if self.budget.cancellation.reason else "timeout", error=self.budget.failure_reason,
                                    raw_result=value.raw_result, elapsed_ms=value.elapsed_ms)
            if value.status == "success":
                try:
                    results[provider_id] = normalized(value.parsed, provider_id, descriptor.version,
                        descriptor.evidence_family, duration_ms=value.elapsed_ms, prepared=prepared)
                    release = (value.raw_result or {}).get("release_metadata", {}) if provider_id == "openai" else None
                    results[provider_id] = replace(results[provider_id], release_metadata=release)
                    if cache and self.budget.failure_reason is None:
                        key, generation = cache_keys[provider_id]
                        if generation is not None:
                            try:
                                cached = {"extraction": value.parsed, "release_metadata": release} if provider_id == "openai" else value.parsed
                                parse_cache.put("parse", key, cached, generation=generation, settings=cache_settings)
                            except Exception:
                                diagnostics.append({"provider_id": provider_id, "error": "parse_cache_write_failed"})
                except (ValueError, TypeError):
                    value = ParseResult(provider_id, "invalid", error="invalid_parser_extraction")
            if provider_id not in results:
                results[provider_id] = NormalizedParse(provider_id, descriptor.version, descriptor.evidence_family,
                    value.status, error=value.error, duration_ms=value.elapsed_ms)
        if "openai" in results and results["openai"].llm is None:
            results["openai"] = replace(results["openai"], llm=parser_metadata(config["openai"], provider_raw.get("openai"),
                                                                            skipped="openai" not in pending))
        ordered = tuple(results[key] for key in selected)
        for result in ordered:
            self.emit("parser_finished", provider_id=result.provider_id, status=result.status,
                      error=result.error, cache_source=result.cache_source, duration_ms=result.duration_ms,
                      **({"llm": result.llm, "release_metadata": result.release_metadata} if result.provider_id == "openai" else {}))
        return ParserBatch(ordered, signatures, tuple(diagnostics))
