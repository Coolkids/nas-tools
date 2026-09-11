# -*- coding: utf-8 -*-

import log
import os
import time
import random
import requests
import requests.exceptions
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .as_obj import AsObj
from .exceptions import TMDbException
from app.utils import ExceptionUtils

class TMDb(object):
    TMDB_API_KEY = "TMDB_API_KEY"
    TMDB_LANGUAGE = "TMDB_LANGUAGE"
    TMDB_WAIT_ON_RATE_LIMIT = "TMDB_WAIT_ON_RATE_LIMIT"
    TMDB_DEBUG_ENABLED = "TMDB_DEBUG_ENABLED"
    TMDB_CACHE_ENABLED = "TMDB_CACHE_ENABLED"
    TMDB_PROXIES = "TMDB_PROXIES"
    TMDB_DOMAIN = "TMDB_DOMAIN"
    REQUEST_CACHE_MAXSIZE = 20000
    _parsed_cache = {}
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
        self._api_key = str(api_key) if api_key else None
        self._api_keys = [key.strip() for key in str(api_key).split(";") if key.strip()] if api_key else []
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
    def cached_request(cls, cache_key, method, url, data, proxies):
        with cls._parsed_cache_lock:
            cached = cls._parsed_cache.get(cache_key)
        if cached is not None:
            cls._cache_hits += 1
            return cached
        cls._cache_misses += 1
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
            # 解析proxies参数
            proxies_dict = eval(proxies) if proxies else None

            # 发送请求
            response = cls._cache_session.request(
                method=method,
                url=url,
                data=data,
                proxies=proxies_dict,
                verify=False,
                timeout=30
            )

            if response.status_code >= 500:
                response.raise_for_status()
            # 缓存解析后的普通数据，避免保留连接、请求头和 Response 对象。
            payload = response.json()
            with cls._parsed_cache_lock:
                if len(cls._parsed_cache) >= cls.REQUEST_CACHE_MAXSIZE:
                    cls._parsed_cache.pop(next(iter(cls._parsed_cache)))
                cls._parsed_cache[cache_key] = payload
            return payload

        try:
            return _request_with_retry()
        except requests.exceptions.RequestException as e:
            log.error("【TMDB-API】 cached_request请求失败！%s" % (ExceptionUtils.exception_traceback(e)))
            raise

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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.Timeout,
             requests.exceptions.ConnectTimeout,
             requests.exceptions.ConnectionError)
        )
    )
    def session_request(self, session, method, url, data, proxies):
        proxies_dict = eval(proxies) if proxies else None
        try:
            response = session.request(method, url, data=data, proxies=proxies_dict, timeout=10, verify=False)
            if response.status_code >= 500:
                response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            log.error("【TMDB-API】 session_request请求失败！%s" % (ExceptionUtils.exception_traceback(e)))
            raise

    def _call(
            self, action, append_to_response, call_cached=True, method="GET", data=None
    ):
        url = "%s%s?api_key=%s&%s&language=%s&include_adult=true" % (
            self.domain,
            action,
            self.api_key,
            append_to_response,
            self.language,
        )

        if self.cache and self.obj_cached and call_cached and method != "POST":
            req = self.cached_request(self._cache_key(url, data, self.proxies),
                                      method, url, data, self.proxies)
        else:
            response = self.session_request(self._session, method, url, data, self.proxies)
            req = response.json()

        headers = response.headers if 'response' in locals() else {}

        if "X-RateLimit-Remaining" in headers:
            self._remaining = int(headers["X-RateLimit-Remaining"])

        if "X-RateLimit-Reset" in headers:
            self._reset = int(headers["X-RateLimit-Reset"])

        if self._remaining < 1:
            current_time = int(time.time())
            sleep_time = self._reset - current_time

            if self.wait_on_rate_limit:
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
