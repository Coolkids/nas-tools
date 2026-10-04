"""统一执行识别器，并处理解析缓存和相同请求合并。"""

import copy
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from threading import Condition, Lock

from config import Config
from app.media.recognition import cache, registry
from app.media.recognition.contracts import ParseResult
from app.media.recognition.records import current_recorder


class _ProviderConcurrencyGate:
    """按识别配置限制进程内实际运行的解析器数量。"""

    def __init__(self, limit_provider=None):
        self._condition = Condition()
        self._active = 0
        self._limit_provider = limit_provider or self._configured_limit

    @staticmethod
    def _configured_limit():
        try:
            # 并发是进程资源限制，必须跟随当前配置，而不是旧请求的快照。
            execution = (Config().get_config("recognition") or {}).get("execution") or {}
            if not isinstance(execution, dict):
                return 4
            return max(1, int(execution.get("max_inflight_provider_requests", 4)))
        except Exception:
            return 4

    def acquire(self, timeout):
        deadline = time.monotonic() + max(float(timeout), 0.001)
        with self._condition:
            while self._active >= self._limit_provider():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                # 周期性重读配置，支持不重启进程调整并发上限。
                self._condition.wait(min(remaining, 0.5))
            self._active += 1
            return True

    def release(self):
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()


_PROVIDER_GATE = _ProviderConcurrencyGate()


class RecognitionService:
    """集中管理识别器执行、成功结果缓存及进程内并发请求合并。"""

    def __init__(self, concurrency_gate=None):
        self._lock = Lock()
        self._inflight = {}
        self._concurrency_gate = concurrency_gate or _PROVIDER_GATE

    @staticmethod
    def _safe_copy(value):
        try:
            return copy.deepcopy(value)
        except Exception:
            return copy.copy(value)

    @staticmethod
    def _get_cached(key):
        try:
            return cache.get("parse", key)
        except Exception:
            return cache.CACHE_MISS

    @staticmethod
    def _store_cached(key, value, generation=None):
        try:
            return cache.put("parse", key, value, generation=generation)
        except Exception:
            recorder = current_recorder()
            if recorder is not None:
                recorder.action("cache_write", provider_id=value.get("provider_id"),
                                status="error", reason="cache_write_failed")
            return False

    @staticmethod
    def _record(result, request, version, elapsed_ms, cache_source=None):
        recorder = current_recorder()
        if recorder is None:
            return
        if cache_source == "cache":
            recorder.action("cache_hit", provider_id=result.provider_id,
                            input={"layer": "parse"})
        elif cache_source == "shared":
            recorder.action("parse_shared", provider_id=result.provider_id,
                            status="completed", input={"layer": "parse"},
                            output={"source": "inflight_request"})
        elif cache_source == "shared_timeout":
            recorder.action("parse_shared", provider_id=result.provider_id,
                            status="timeout", input={"layer": "parse"},
                            reason=result.error)
        elif cache_source == "concurrency_timeout":
            recorder.action("provider_queue", provider_id=result.provider_id,
                            status="timeout", input={"layer": "parse"},
                            reason=result.error)
        elif cache_source is None:
            recorder.action("provider_parse", provider_id=result.provider_id,
                            status=result.status,
                            input={"title": request.title},
                            output={"elapsed_ms": elapsed_ms, "version": version},
                            reason=result.error)
        provider_input = {"title": request.title}
        if result.provider_id != "anitopy_ml":
            provider_input["subtitle"] = request.subtitle
        recorder.add_provider_result(
            provider_id=result.provider_id,
            status="cached" if cache_source == "cache" else result.status,
            input=provider_input,
            raw_result=RecognitionService._safe_copy(result.raw_result),
            normalized_result=RecognitionService._safe_copy(result.parsed),
            version=version,
            elapsed_ms=elapsed_ms,
            error=result.error,
        )

    @staticmethod
    def _cached_result(provider_id, value, source):
        return ParseResult(
            provider_id=provider_id,
            status=value.get("status", "success"),
            parsed=RecognitionService._safe_copy(value.get("parsed")),
            raw_result=RecognitionService._safe_copy(value.get("raw_result")),
            error=value.get("error"),
            elapsed_ms=0,
            cache_source=source,
        )

    def run_provider(self, provider_id, request, options=None, cache_payload=None,
                     timeout=None, force_refresh=False):
        """执行解析器；支持成功缓存、强制刷新和同进程相同请求合并。"""
        provider_class = registry.discover().get(provider_id)
        if provider_class is None:
            return ParseResult(provider_id, "error", error="provider_unavailable")

        descriptor = provider_class.descriptor
        version = descriptor.version
        options = copy.deepcopy(options or {})
        payload = {
            "provider_id": provider_id,
            "provider_version": version,
            "request": cache_payload if cache_payload is not None else {
                "title": request.title, "subtitle": request.subtitle,
                "context": {key: value for key, value in request.context.items()
                            if key not in {"deadline_monotonic", "remaining_timeout_seconds"}},
            },
            "options": {key: value for key, value in options.items() if key != "timeout"},
        }
        key = cache.cache_key("parse", payload)
        cache_generation = cache.generation("parse")
        singleflight = cache.singleflight_enabled("parse")
        inflight_key = (key, bool(force_refresh), cache_generation)
        cached = cache.CACHE_MISS if force_refresh else self._get_cached(key)
        if cached is not cache.CACHE_MISS:
            result = self._cached_result(provider_id, cached, "cache")
            self._record(result, request, version, 0, "cache")
            return result

        with self._lock:
            # 二次检查与在途登记使用同一把锁，避免两个线程同时成为请求执行者。
            cached = cache.CACHE_MISS if force_refresh else self._get_cached(key)
            if cached is not cache.CACHE_MISS:
                future = None
                leader = False
            elif not singleflight:
                future = Future()
                leader = True
            else:
                future = self._inflight.get(inflight_key)
                leader = future is None
                if leader:
                    future = Future()
                    self._inflight[inflight_key] = future

        if not leader and cached is not cache.CACHE_MISS:
            result = self._cached_result(provider_id, cached, "cache")
            self._record(result, request, version, 0, "cache")
            return result

        if not leader:
            started = time.monotonic()
            try:
                wait_budget = timeout if timeout is not None else 10
                shared = future.result(timeout=max(float(wait_budget), 0.001))
                result = self._safe_copy(shared)
                result.cache_source = "shared"
                result.elapsed_ms = int((time.monotonic() - started) * 1000)
            except FutureTimeout:
                result = ParseResult(provider_id, "timeout", error="shared_parse_wait_timeout",
                                     elapsed_ms=int((time.monotonic() - started) * 1000),
                                     cache_source="shared_timeout")
            self._record(result, request, version, result.elapsed_ms, result.cache_source)
            return result

        if cached is not cache.CACHE_MISS:
            result = self._cached_result(provider_id, cached, "cache")
            self._record(result, request, version, 0, "cache")
            return result

        started = time.monotonic()
        provider = None
        acquired = False
        try:
            wait_budget = max(float(timeout if timeout is not None else 10), 0.001)
            acquired = self._concurrency_gate.acquire(wait_budget)
            if not acquired:
                result = ParseResult(
                    provider_id, "timeout", error="provider_concurrency_wait_timeout",
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    cache_source="concurrency_timeout")
                self._record(result, request, version, result.elapsed_ms,
                             result.cache_source)
                future.set_result(self._safe_copy(result))
                return result
            provider_options = copy.deepcopy(options)
            if timeout is not None and "timeout" in provider_options:
                remaining = max(float(timeout) - (time.monotonic() - started), 0.001)
                try:
                    provider_options["timeout"] = min(
                        float(provider_options["timeout"]), remaining)
                except (TypeError, ValueError):
                    provider_options["timeout"] = remaining
            provider = registry.create(provider_id, **provider_options)
            if provider is None:
                result = ParseResult(provider_id, "error", error="provider_unavailable")
            else:
                result = provider.parse(request)
                if not isinstance(result, ParseResult):
                    result = ParseResult(provider_id, "invalid",
                                         error="provider_returned_invalid_result")
                if result.provider_id != provider_id:
                    result.provider_id = provider_id
                if not result.elapsed_ms:
                    result.elapsed_ms = int((time.monotonic() - started) * 1000)
                if timeout is not None and time.monotonic() - started > float(timeout):
                    result.status = "timeout"
                    result.error = "recognition_deadline_exceeded"
                if result.status == "success" and result.parsed is not None:
                    self._store_cached(key, {
                        "provider_id": provider_id,
                        "status": result.status,
                        "parsed": result.parsed,
                        "raw_result": result.raw_result,
                        "error": result.error,
                    }, generation=cache_generation)
            self._record(result, request, version, result.elapsed_ms)
            future.set_result(self._safe_copy(result))
            return result
        except Exception as error:
            result = ParseResult(provider_id, "error", error=str(error),
                                 elapsed_ms=int((time.monotonic() - started) * 1000))
            self._record(result, request, version, result.elapsed_ms)
            if not future.done():
                future.set_result(self._safe_copy(result))
            return result
        finally:
            try:
                registry.release(provider)
            finally:
                if acquired:
                    self._concurrency_gate.release()
                with self._lock:
                    if self._inflight.get(inflight_key) is future:
                        self._inflight.pop(inflight_key, None)
                if future is not None and not future.done():
                    future.set_result(ParseResult(provider_id, "error",
                                                  error="provider_aborted"))


recognition_service = RecognitionService()
