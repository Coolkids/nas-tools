"""Explicit TMDB parameters, supervised I/O, validated caches and bounded retry."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
from threading import Lock
import time
from urllib.parse import urlsplit
from requests import Response

from app.media.recognition.budget import RecognitionBudget, effective_deadline_reason
from app.media.recognition.cache_repository import CacheRepository
from app.media.recognition.contracts import SearchResponse
from app.media.recognition.runtime import WorkItem, get_runtime, ResourceBusy
from app.media.recognition.provider_schema import strict_json_loads


class TMDBRequestError(RuntimeError):
    def __init__(self, code, status=None, retry_after_seconds=None):
        self.code, self.status, self.retry_after_seconds = code, status, retry_after_seconds
        super().__init__(code)


class RateLimiter:
    def __init__(self, qps):
        self.interval, self.next_at, self.lock = 1/float(qps), 0, Lock()

    def acquire(self, budget):
        while True:
            budget.check()
            with self.lock:
                delay = self.next_at-time.monotonic()
                if delay <= 0:
                    self.next_at = time.monotonic()+self.interval
                    return
            wait_budget(budget, delay)


_rates, _rates_lock = {}, Lock()


def wait_budget(budget, delay):
    end = time.monotonic()+max(0, delay)
    while time.monotonic() < end:
        budget.check()
        left = budget.remaining_seconds()
        time.sleep(max(0, min(0.05, end-time.monotonic(), left if left is not None else 0.05)))
    budget.check()


def retry_after(headers, attempt):
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is not None:
        try:
            number = float(raw)
            if math.isfinite(number):
                return max(0, number)
        except ValueError:
            try:
                return max(0, (parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    return min(2**attempt, 10)


@dataclass
class TMDBResult:
    payload: dict
    request_params: dict
    http_status: int = 200
    retries: int = 0
    duration_ms: float = 0
    cache_source: str | None = None


class TMDBClient:
    def __init__(self, settings, *, api_key=None, base_url=None, runtime=None, cache=None, budget=None, proxies=None,
                 language="zh-CN", append_to_response=None):
        self.settings = settings.get("recognition", settings)
        if api_key is None or base_url is None:
            from config import Config
            app = Config().get_config("app") or {}
            api_key = api_key if api_key is not None else app.get("rmt_tmdbkey", "")
            if base_url is None:
                from config import DEFAULT_TMDB_PROXY
                base_url = DEFAULT_TMDB_PROXY if (Config().get_config("laboratory") or {}).get("tmdb_proxy") else app.get("tmdb_domain") or "https://api.themoviedb.org/3"
            if proxies is None:
                proxies = Config().get_proxies()
        if not str(base_url).startswith(("http://", "https://")):
            base_url = "https://"+str(base_url)
        self.base_url = str(base_url).rstrip("/")
        if not self.base_url.endswith("/3"):
            self.base_url += "/3"
        parts = urlsplit(self.base_url)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("invalid_tmdb_endpoint")
        self.api_keys = [x.strip() for x in str(api_key or "").split(";") if x.strip()]
        self.language = language
        extra = (append_to_response.split(",") if append_to_response.strip() else ()) if isinstance(append_to_response, str) else append_to_response or ()
        if not isinstance(extra, (list, tuple, set)) or len(extra) > 32 or any(not isinstance(x, str) or not re.fullmatch(r"[a-z_]{1,64}", x.strip()) for x in extra):
            raise ValueError("invalid_tmdb_append_fields")
        self.append_to_response = ",".join(sorted({"alternative_titles", "translations", "external_ids", *(x.strip() for x in extra)}))
        self.api_key, self.runtime, self.proxies = self.api_keys[0] if self.api_keys else "", runtime, proxies
        self.cache = cache if cache is not None else CacheRepository(settings=settings if "recognition" in settings else {"recognition": settings})
        self.budget = budget or RecognitionBudget(self.settings["timeouts"].get("total_ms"))
        self.http_attempt_count, self._counter_lock = 0, Lock()
        qps = self.settings["query"]["tmdb_qps"]
        with _rates_lock:
            if len(_rates) >= 128:
                _rates.clear()
            self.rate = _rates.setdefault((self.base_url, float(qps)), RateLimiter(qps))

    @staticmethod
    def _validate(payload, layer, expected_id=None):
        if not isinstance(payload, dict) or payload.get("success") is False or "errors" in payload or "status_code" in payload:
            raise TMDBRequestError("tmdb_invalid_response")
        if layer == "search":
            if (not isinstance(payload.get("results"), list) or any(not isinstance(x, dict) for x in payload["results"]) or
                any(type(payload.get(k)) is not int or payload[k] < (1 if k == "page" else 0) for k in ("page", "total_pages", "total_results"))):
                raise TMDBRequestError("tmdb_invalid_search_response")
        elif type(payload.get("id")) is not int or payload["id"] <= 0 or expected_id is not None and payload["id"] != expected_id:
            raise TMDBRequestError("tmdb_invalid_detail_response")
        return payload

    def request(self, path, params, *, layer="search", cache=True, expected_id=None, budget=None):
        if not path.startswith("/") or "?" in path or ".." in path:
            raise ValueError("invalid_tmdb_path")
        started = time.monotonic()
        stage = (budget or self.budget).stage("tmdb_request", self.settings["timeouts"].get("tmdb_request_ms"))
        key_params = {"origin": self.base_url, "path": path, **params, "schema": "tmdb-v3.1"}
        key = self.cache.key(layer, key_params)
        def validate(payload):
            self._validate(payload, layer, expected_id)
            if layer == "detail":
                title = payload.get("title" if path.startswith("/movie/") else "name")
                if not isinstance(title, str) or not title.strip():
                    raise TMDBRequestError("tmdb_invalid_detail_title")
            if layer == "episode":
                parts = path.split("/")
                if (type(payload.get("season_number")) is not int or type(payload.get("episode_number")) is not int or
                        payload["season_number"] != int(parts[-3]) or payload["episode_number"] != int(parts[-1])):
                    raise TMDBRequestError("tmdb_invalid_episode_coordinates")
            return payload
        def cached_valid(payload):
            try:
                validate(payload)
            except TMDBRequestError:
                return False
            return True
        if cache:
            stage.check()
            existing = self.cache.get(layer, key)
            if existing is not None and not existing.needs_revalidation and cached_valid(existing.payload):
                return TMDBResult(existing.payload, key_params, cache_source="cache")
        retries = 0
        def load():
            nonlocal retries
            retry_max = self.settings["query"]["tmdb_retry_max"]
            for attempt in range(retry_max+1):
                stage.check()
                self.rate.acquire(stage)
                left = stage.remaining_seconds()
                try:
                    result = (self.runtime or get_runtime()).submit("tmdb", WorkItem("http", {
                        "url": self.base_url+path, "params": {**params, "api_key": self.api_key},
                        "timeout": left, "verify": True, "proxies": self.proxies, "allow_redirects": False}),
                        deadline=time.monotonic()+left if left is not None else None,
                        cancellation=self.budget.cancellation, deadline_reason=effective_deadline_reason(stage),
                        queue_deadline=(time.monotonic()+self.settings["timeouts"]["queue_ms"]/1000
                                        if self.settings["timeouts"].get("queue_ms") is not None else None)).result()
                except ResourceBusy:
                    raise TMDBRequestError("tmdb_busy") from None
                if result.dispatched_at is not None:
                    with self._counter_lock:
                        self.http_attempt_count += 1
                stage.check()
                if result.status == "timeout":
                    raise TMDBRequestError(result.reason or "tmdb_request_timeout")
                if result.status == "cancelled":
                    self.budget.cancellation.check()
                    raise TMDBRequestError(result.reason)
                if result.status != "success":
                    if attempt < retry_max:
                        retries += 1
                        wait_budget(stage, min(2**attempt, 10))
                        continue
                    raise TMDBRequestError("tmdb_network_error")
                value, status = result.value, result.value["status"]
                if status == 429 or status >= 500:
                    if attempt < retry_max:
                        retries += 1
                        wait_budget(stage, retry_after(value["headers"], attempt))
                        continue
                if status != 200:
                    raise TMDBRequestError(f"tmdb_http_{status}", status,
                        retry_after(value["headers"], attempt) if status == 429 or status >= 500 else None)
                try:
                    payload = strict_json_loads(value["content"])
                except (ValueError, TypeError):
                    raise TMDBRequestError("tmdb_invalid_json") from None
                return validate(payload)
            raise TMDBRequestError("tmdb_retry_exhausted")
        if cache:
            payload = self.cache.load(layer, key, load, cancellation=self.budget.cancellation,
                budget=stage, request_params=key_params, validator=cached_valid,
                ttl_for_payload=lambda value: 60 if layer == "search" and not value["results"] else None)
        else:
            payload = load()
        stage.check()
        validate(payload)
        return TMDBResult(payload, key_params, retries=retries, duration_ms=(time.monotonic()-started)*1000)

    def search(self, item, page=1, *, cache=True, budget=None):
        params = {"query": item.query, "language": item.language, "include_adult": str(item.include_adult).lower(), "page": page}
        if item.year_filter is not None and item.endpoint_type == "movie":
            params["primary_release_year"] = item.year_filter
        result = self.request("/search/"+item.endpoint_type, params, cache=cache, budget=budget)
        payload = result.payload
        return SearchResponse(item.query_id, result.request_params, payload, payload["page"], payload["total_pages"],
            payload["total_results"], result.http_status, result.retries, result.duration_ms, result.cache_source)

    def detail(self, kind, tmdb_id, language=None, *, cache=True, budget=None):
        if kind not in {"tv", "movie"} or type(tmdb_id) is not int or tmdb_id <= 0:
            raise ValueError("invalid_tmdb_identity")
        result = self.request(f"/{kind}/{tmdb_id}", {"language": language or self.language,
            "append_to_response": self.append_to_response}, layer="detail", cache=cache, expected_id=tmdb_id, budget=budget)
        if not isinstance(result.payload.get("title" if kind == "movie" else "name"), str):
            raise TMDBRequestError("tmdb_invalid_detail_title")
        return {**result.payload, "media_type": kind}

    def episode(self, tv_id, season, episode, language="zh-CN", *, cache=True, budget=None):
        if any(type(value) is not int or value < minimum for value, minimum in ((tv_id, 1), (season, 0), (episode, 1))):
            raise ValueError("invalid_episode_coordinates")
        result = self.request(f"/tv/{tv_id}/season/{season}/episode/{episode}", {"language": language},
                              layer="episode", cache=cache, budget=budget)
        if result.payload.get("season_number") != season or result.payload.get("episode_number") != episode:
            raise TMDBRequestError("tmdb_invalid_episode_coordinates")
        return result.payload
