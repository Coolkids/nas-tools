# -*- coding: utf-8 -*-

import log
import os
import time
import random
import requests
import requests.exceptions
import threading
import ast
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .as_obj import AsObj
from .exceptions import TMDbException
from app.utils import ExceptionUtils
from app.utils.persistent_cache import PersistentCache


def _recognition_remaining_seconds():
    """Read the active media-recognition network budget without creating a cycle."""
    try:
        from app.media.recognition.records import recognition_remaining_seconds
        return recognition_remaining_seconds()
    except Exception:
        return None


def _note_recognition_tmdb_timeout(error):
    try:
        from app.media.recognition.records import note_tmdb_timeout
        note_tmdb_timeout(error)
    except Exception:
        pass


def _recognition_budget_active():
    from app.media.recognition.records import recognition_budget_active
    return recognition_budget_active()


def _check_recognition_budget():
    remaining = _recognition_remaining_seconds()
    if remaining is not None and remaining <= 0:
        from app.media.recognition.records import recognition_timeout_reason
        raise requests.exceptions.Timeout(recognition_timeout_reason())


def _recognition_request(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        from app.media.recognition.records import recognition_stage
        with recognition_stage("tmdb_request"):
            _check_recognition_budget()
            response = method(*args, **kwargs)
            _check_recognition_budget()
            return response
    return wrapped


class TMDb(object):
    TMDB_API_KEY = "TMDB_API_KEY"
    TMDB_LANGUAGE = "TMDB_LANGUAGE"
    TMDB_WAIT_ON_RATE_LIMIT = "TMDB_WAIT_ON_RATE_LIMIT"
    TMDB_DEBUG_ENABLED = "TMDB_DEBUG_ENABLED"
    TMDB_CACHE_ENABLED = "TMDB_CACHE_ENABLED"
    TMDB_PROXIES = "TMDB_PROXIES"
    TMDB_DOMAIN = "TMDB_DOMAIN"
    _parsed_cache = PersistentCache("tmdb_request", ttl=86400)
    _parsed_cache_lock = threading.Lock()
    _cache_session = requests.Session()
    _cache_hits = 0
    _cache_misses = 0

    def __init__(self, obj_cached=True, session=None):
        self._session = requests.Session() if session is None else session
        self._remaining = 40
        self._reset = None
        self.obj_cached = obj_cached
        self._language = "zh-CN"
        self._domain = "https://api.themoviedb.org/3"
        self._api_key = None
        self._proxies = None
        self._wait_on_rate_limit = True
        self._debug = False
        self._cache = True
        self._metadata = {}
        self.include_adult = False

        self._api_keys = []
        self._load_api_keys()

    def _load_api_keys(self):
        raw = os.environ.get(self.TMDB_API_KEY)
        if not raw:
            self._api_keys = []
            return

        self._api_keys = [k.strip() for k in raw.split(";") if k.strip()]

    def _random_api_key(self):
        if not self._api_keys:
            raise TMDbException("No API key found.")
        return random.choice(self._api_keys)

    @property
    def page(self):
        return self._metadata.get("page")

    @property
    def total_results(self):
        return self._metadata.get("total_results")

    @property
    def total_pages(self):
        return self._metadata.get("total_pages")

    @property
    def api_key(self):
        if not self._api_keys and self._api_key:
            self._api_keys = [key.strip() for key in str(self._api_key).split(";") if key.strip()]
        if not self._api_keys:
            self._load_api_keys()
        return self._random_api_key()

    @property
    def domain(self):
        return self._domain

    @domain.setter
    def domain(self, domain):
        if domain:
            if not str(domain).startswith('http'):
                domain = "https://%s" % domain
            if not str(domain).endswith('/3'):
                domain = "%s/3" % domain
            self._domain = str(domain)
        else:
            self._domain = ''

    @property
    def proxies(self):
        return self._proxies

    @proxies.setter
    def proxies(self, proxies):
        if proxies:
            proxies_strs = []
            for key, value in proxies.items():
                if not value:
                    continue
                proxies_strs.append("'%s': '%s'" % (key, value))
            if proxies_strs:
                self._proxies = "{%s}" % ",".join(proxies_strs)
            else:
                self._proxies = 'None'
        else:
            self._proxies = None

    @api_key.setter
    def api_key(self, api_key):
        previous = self._api_key
        self._api_key = str(api_key) if api_key else None
        self._api_keys = [key.strip() for key in str(api_key).split(";") if key.strip()] if api_key else []
        if previous is not None and previous != self._api_key:
            self.cache_clear()

    @property
    def language(self):
        return self._language

    @language.setter
    def language(self, language):
        self._language = language or "zh-CN"

    @property
    def wait_on_rate_limit(self):
        return self._wait_on_rate_limit

    @wait_on_rate_limit.setter
    def wait_on_rate_limit(self, wait_on_rate_limit):
        self._wait_on_rate_limit = bool(wait_on_rate_limit)

    @property
    def debug(self):
        return self._debug

    @debug.setter
    def debug(self, debug):
        self._debug = bool(debug)

    @property
    def cache(self):
        return self._cache

    @cache.setter
    def cache(self, cache):
        self._cache = bool(cache)

    @staticmethod
    def _get_obj(result, key="results", all_details=False):
        if "success" in result and result["success"] is False:
            raise TMDbException(result["status_message"])
        if all_details is True or key is None:
            return AsObj(**result)
        else:
            return [AsObj(**res) for res in result[key]]

    @classmethod
    @_recognition_request
    def cached_request(cls, cache_key, method, url, data, proxies):
        _check_recognition_budget()
        with cls._parsed_cache_lock:
            cached = cls._parsed_cache.get(cache_key)
        if cached is not None:
            try:
                cls._validate_payload(cached, url)
            except TMDbException:
                with cls._parsed_cache_lock:
                    cls._parsed_cache.pop(cache_key, None)
            else:
                cls._cache_hits += 1
                return cached
        cls._cache_misses += 1

        def _request_once(timeout):
            proxies_dict = ast.literal_eval(proxies) if proxies else None
            from app.media.recognition.runtime import supervised_enabled, execute_http
            if supervised_enabled():
                response = execute_http({"method": method, "url": url, "data": data,
                                         "proxies": proxies_dict, "timeout": timeout})
            else:
                response = cls._cache_session.request(method=method, url=url, data=data,
                    proxies=proxies_dict, verify=True, timeout=timeout)
            if response.status_code >= 400:
                response.raise_for_status()
            payload = response.json()
            cls._validate_payload(payload, url)
            _check_recognition_budget()
            with cls._parsed_cache_lock:
                # 容量由统一缓存适配层控制，避免远程模式每次写入遍历所有键。
                cls._parsed_cache[cache_key] = payload
            return payload

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type(
                (requests.exceptions.Timeout,
                 requests.exceptions.ConnectTimeout,
                 requests.exceptions.ConnectionError)
            )
        )
        def _request_with_retry():
            return _request_once(timeout=30)

        try:
            remaining = _recognition_remaining_seconds()
            if remaining is not None or _recognition_budget_active():
                _check_recognition_budget()
                # Recognition requests use the remaining end-to-end budget and
                # never sleep/retry beyond that request's deadline.
                return _request_once(timeout=remaining)
            return _request_with_retry()
        except requests.exceptions.RequestException as e:
            if isinstance(e, requests.exceptions.Timeout):
                _note_recognition_tmdb_timeout(e)
            log.error("【TMDB-API】 cached_request请求失败：%s" % type(e).__name__)
            raise

    @staticmethod
    def _validate_payload(payload, url):
        if not isinstance(payload, dict) or payload.get("success") is False or "errors" in payload or "status_code" in payload and payload.get("success") is not True:
            raise TMDbException("invalid_tmdb_response")
        if "/search/" in urlsplit(url).path and (not isinstance(payload.get("results"), list) or any(not isinstance(x, dict) for x in payload["results"])):
            raise TMDbException("invalid_tmdb_search_response")
        return payload

    def cache_clear(self):
        with self._parsed_cache_lock:
            self._parsed_cache.clear()

    @classmethod
    def cache_info(cls):
        with cls._parsed_cache_lock:
            return {
                "size": len(cls._parsed_cache),
                "hits": cls._cache_hits,
                "misses": cls._cache_misses,
            }

    @staticmethod
    def _cache_key(url, data, proxies):
        parsed = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                 if key != "api_key"]
        normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                 urlencode(sorted(query)), ""))
        return normalized, data, proxies

    @staticmethod
    def _session_request_once(session, method, url, data, proxies, timeout):
        proxies_dict = ast.literal_eval(proxies) if proxies else None
        try:
            from app.media.recognition.runtime import supervised_enabled, execute_http
            if supervised_enabled():
                response = execute_http({"method": method, "url": url, "data": data,
                                         "proxies": proxies_dict, "timeout": timeout})
            else:
                response = session.request(method, url, data=data, proxies=proxies_dict,
                                           timeout=timeout, verify=True)
            if response.status_code >= 400:
                response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if isinstance(e, requests.exceptions.Timeout):
                _note_recognition_tmdb_timeout(e)
            log.error("【TMDB-API】 session_request请求失败：%s" % type(e).__name__)
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout,
             requests.exceptions.ConnectTimeout,
                 requests.exceptions.ConnectionError)
        )
    )
    def _session_request_with_retry(self, session, method, url, data, proxies):
        return self._session_request_once(session, method, url, data, proxies, timeout=10)

    @_recognition_request
    def session_request(self, session, method, url, data, proxies):
        remaining = _recognition_remaining_seconds()
        if remaining is None and not _recognition_budget_active():
            return self._session_request_with_retry(session, method, url, data, proxies)
        if remaining is not None and remaining <= 0:
            error = requests.exceptions.Timeout("recognition_deadline_exceeded")
            _note_recognition_tmdb_timeout(error)
            raise error
        # Avoid Tenacity's retry backoff while an end-to-end recognition
        # deadline is active; retries are available for non-recognition calls.
        response = self._session_request_once(
            session, method, url, data, proxies, timeout=remaining)
        _check_recognition_budget()
        return response

    def _call(
            self, action, append_to_response, call_cached=True, method="GET", data=None
    ):
        parameters = dict(parse_qsl(append_to_response, keep_blank_values=True))
        parameters.update(api_key=self.api_key, language=self.language)
        parameters.setdefault("include_adult", str(self.include_adult).lower())
        url = self.domain + action + "?" + urlencode(parameters)

        if self.cache and self.obj_cached and call_cached and method != "POST":
            req = self.cached_request(self._cache_key(url, data, self.proxies),
                                      method, url, data, self.proxies)
        else:
            response = self.session_request(self._session, method, url, data, self.proxies)
            req = response.json()
            self._validate_payload(req, url)

        headers = response.headers if 'response' in locals() else {}

        if "X-RateLimit-Remaining" in headers:
            self._remaining = int(headers["X-RateLimit-Remaining"])

        if "X-RateLimit-Reset" in headers:
            self._reset = int(headers["X-RateLimit-Reset"])

        if self._remaining < 1:
            current_time = int(time.time())
            sleep_time = self._reset - current_time

            recognition_remaining = _recognition_remaining_seconds()
            if self.wait_on_rate_limit and recognition_remaining is None and not _recognition_budget_active():
                log.warn("【TMDB-API】 Rate limit reached. Sleeping for: %d" % sleep_time)
                time.sleep(abs(sleep_time))
                return self._call(action, append_to_response, call_cached, method, data)
            else:
                raise TMDbException(
                    "Rate limit reached. Try again in %d seconds." % sleep_time
                )

        json = req

        if "page" in json:
            self._metadata["page"] = str(json["page"])

        if "total_results" in json:
            self._metadata["total_results"] = str(json["total_results"])

        if "total_pages" in json:
            self._metadata["total_pages"] = str(json["total_pages"])

        # if self.debug:
        #     log.debug(json)
        #     log.info(self.cached_request.cache_info())

        if "errors" in json:
            raise TMDbException(json["errors"])

        return json
