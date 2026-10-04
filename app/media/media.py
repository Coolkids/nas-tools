import json
import os
import random
import re
import sys
import time
import traceback
import uuid
import unicodedata
import copy
from difflib import SequenceMatcher
from urllib.parse import urlencode

import zhconv
import anitopy
from requests.exceptions import Timeout as HttpTimeout
from lxml import etree
from app.utils import ExceptionUtils
import log
from app.helper import DbHelper, MetaHelper
from app.media.meta.metainfo import MetaInfo, prepare_media_title, _infer_recognition_source
from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.media.recognition.records import (
    current_recorder, note_tmdb_failure, note_tmdb_timeout, recognition_config, recognition_remaining_seconds,
    record_tmdb_call, recognition_scope,
)
from app.media.recognition.settings import (
    profile_allows_provider, profile_network_allowed, profile_tmdb_allowed,
    provider_disabled_reason, provider_enabled, provider_endpoint,
    provider_requires_network,
)
from app.media.recognition import RecognitionRequest, registry
from app.media.recognition import cache as recognition_cache
from app.media.recognition.service import recognition_service
from app.media.recognition.decision import (
    candidate_features, select_title_evidence, weighted_score,
)
from app.media.tmdbv3api import TMDb, Search, Movie, TV, Person, Find, TMDbException, Discover, Trending, Episode, Genre
from app.utils import PathUtils, EpisodeFormat, RequestUtils, NumberUtils, StringUtils, TmdbWebSearchCache
from app.utils.types import MediaType, MatchMode
from config import Config, TMDB_IMAGE_ORIGINAL_URL, DEFAULT_TMDB_PROXY, \
    TMDB_IMAGE_FACE_URL, TMDB_PEOPLE_PROFILE_URL, TMDB_IMAGE_W500_URL


_TmdbWebSearchCache_SENTINEL = object()


class Media:
    # TheMovieDB
    tmdb = None
    search = None
    movie = None
    tv = None
    episode = None
    person = None
    find = None
    trending = None
    discover = None
    genre = None
    meta = None
    _rmt_match_mode = None
    _ai_inference = False
    _ai_inference_url = None
    _search_tmdbweb = None
    _tmdb_clients = None

    def __init__(self):
        self.init_config()

    def init_config(self):
        self._tmdb_clients = []
        app = Config().get_config('app')
        laboratory = Config().get_config('laboratory')
        if app:
            if app.get('rmt_tmdbkey'):
                self.tmdb = TMDb()
                if laboratory.get('tmdb_proxy'):
                    self.tmdb.domain = DEFAULT_TMDB_PROXY
                else:
                    self.tmdb.domain = app.get("tmdb_domain")
                self.tmdb.cache = True
                self.tmdb.api_key = app.get('rmt_tmdbkey')
                self.tmdb.language = 'zh'
                self.tmdb.proxies = Config().get_proxies()
                self.tmdb.debug = True
                clients = [Search, Movie, TV, Episode, Find, Person, Trending, Discover, Genre]
                instances = []
                for client_type in clients:
                    client = client_type(session=self.tmdb._session)
                    client.api_key = app.get('rmt_tmdbkey')
                    client.domain = self.tmdb.domain
                    client.proxies = Config().get_proxies()
                    client.cache = True
                    instances.append(client)
                (self.search, self.movie, self.tv, self.episode, self.find,
                 self.person, self.trending, self.discover, self.genre) = instances
                self._tmdb_clients = [self.tmdb, *instances]
                self.meta = MetaHelper()
            rmt_match_mode = app.get('rmt_match_mode', 'normal')
            if rmt_match_mode:
                rmt_match_mode = rmt_match_mode.upper()
            else:
                rmt_match_mode = "NORMAL"
            if rmt_match_mode == "STRICT":
                self._rmt_match_mode = MatchMode.STRICT
            else:
                self._rmt_match_mode = MatchMode.NORMAL
        laboratory = Config().get_config('laboratory')
        if laboratory:
            self._ai_inference = laboratory.get("ai_inference", False)
            self._ai_inference_url = laboratory.get("ai_inference_url")
            self._search_tmdbweb = laboratory.get("search_tmdbweb")

    @staticmethod
    def __tmdb_search_type(media_type):
        """Map the library's anime category to TMDB's TV media type."""
        if media_type == MediaType.ANIME:
            return MediaType.TV
        return media_type

    @staticmethod
    def __compare_tmdb_names(file_name, tmdb_names):
        """
        比较文件名是否匹配，忽略大小写和特殊字符
        :param file_name: 识别的文件名或者种子名
        :param tmdb_names: TMDB返回的译名
        :return: True or False
        """
        if not file_name or not tmdb_names:
            return False
        if not isinstance(tmdb_names, list):
            tmdb_names = [tmdb_names]
        file_name = StringUtils.handler_special_chars(file_name).upper()
        for tmdb_name in tmdb_names:
            tmdb_name = StringUtils.handler_special_chars(tmdb_name).strip().upper()
            if file_name == tmdb_name:
                return True
        return False

    def __select_tmdb_search_candidates(self, query, results, default_media_type=None):
        """Select TMDB search results only when their names occur in the raw title."""
        # The TMDB client wraps result rows in AsObj. Keep ordinary mappings
        # and convert the client's object rows before applying the common
        # candidate-selection logic; filtering to dict silently discarded
        # every real API result.
        normalized_results = []
        for item in results or []:
            if isinstance(item, dict):
                normalized_results.append(dict(item))
                continue
            items = getattr(item, "items", None)
            if callable(items):
                try:
                    normalized_results.append(dict(items()))
                    continue
                except (TypeError, ValueError):
                    pass
            attributes = getattr(item, "__dict__", None)
            if isinstance(attributes, dict):
                normalized_results.append(dict(attributes))
        results = normalized_results
        recorder = current_recorder()
        original_title = (recorder.original_name if recorder else None) or query
        pending = []
        examined = []
        seen_entities = set()
        for result in results:
            candidate = dict(result)
            media_type = candidate.get("media_type") or default_media_type
            if media_type == "movie":
                media_type = MediaType.MOVIE
            elif media_type == "tv":
                media_type = MediaType.TV
            if not media_type:
                continue
            candidate["media_type"] = media_type
            evidence = self.__tmdb_title_evidence(
                original_title, candidate, include_aliases=False)
            if evidence.get("level") != "strong":
                evidence = self.__tmdb_title_evidence(original_title, candidate)
            matched = evidence.get("level") == "strong"
            entity = (str(getattr(media_type, "value", media_type)), str(candidate.get("id")))
            examined.append({"id": candidate.get("id"), "media_type": media_type,
                             "matched": matched, "evidence": evidence})
            if matched and candidate.get("id") and entity not in seen_entities:
                seen_entities.add(entity)
                pending.append(candidate)
                if len(pending) > 1:
                    break

        if len(pending) == 1:
            status, reason, selected = "success", None, pending[0]
        elif len(pending) > 1:
            status, reason, selected = "ambiguous", "ambiguous_tmdb", None
            if recorder is not None:
                recorder.context["decision_reason"] = "ambiguous_tmdb"
            note_tmdb_failure("ambiguous_tmdb")
        elif not results:
            status, reason, selected = "no_result", "tmdb_no_results", {}
            note_tmdb_failure("tmdb_no_results")
        else:
            status, reason, selected = "no_name_match", "no_tmdb_match", {}
            note_tmdb_failure("no_tmdb_match")

        if recorder is not None:
            recorder.action(
                "tmdb_candidate_selection", status=status,
                input={"query": query, "original_title": original_title},
                output={"candidate_count": len(results), "examined": examined,
                        "pending_count": len(pending),
                        "pending_ids": [item.get("id") for item in pending],
                        "selected_id": selected.get("id") if selected else None,
                        "results": results},
                reason=reason)
        return selected

    @staticmethod
    def __recognition_tmdb_failure_reason(recorder):
        if not recorder:
            return None
        priority = {"tmdb_no_results": 1, "no_tmdb_match": 2,
                    "tmdb_network_error": 3, "ambiguous_tmdb": 4}
        failures = getattr(recorder, "_tmdb_failure_events", [])
        if not failures:
            return None
        return max(failures, key=lambda item: priority.get(item["reason"], 0))["reason"]

    @record_tmdb_call
    def __search_tmdb_allnames(self, mtype: MediaType, tmdb_id, alias_sources=None):
        """
        检索tmdb中所有的标题和译名，用于名称匹配
        :param mtype: 类型：电影、电视剧、动漫
        :param tmdb_id: TMDB的ID
        :return: 所有译名的清单
        """
        if not mtype or not tmdb_id:
            return {}, []
        ret_names = []
        tmdb_info = self.get_tmdb_info(mtype=mtype, tmdbid=tmdb_id)
        if not tmdb_info:
            return tmdb_info, []
        alias_sources = set(alias_sources or ("alternative", "translation"))
        if mtype == MediaType.MOVIE:
            if "alternative" in alias_sources:
                alternative_data = tmdb_info.get("alternative_titles") or {}
                alternative_titles = alternative_data.get("titles", []) or [] \
                    if callable(getattr(alternative_data, "get", None)) else []
                for alternative_title in alternative_titles:
                    if not callable(getattr(alternative_title, "get", None)):
                        continue
                    title = alternative_title.get("title")
                    if title and title not in ret_names:
                        ret_names.append(title)
            if "translation" in alias_sources:
                translation_data = tmdb_info.get("translations") or {}
                translations = translation_data.get("translations", []) or [] \
                    if callable(getattr(translation_data, "get", None)) else []
                for translation in translations:
                    translation_value = translation.get("data") or {} \
                        if callable(getattr(translation, "get", None)) else {}
                    title = translation_value.get("title") \
                        if callable(getattr(translation_value, "get", None)) else None
                    if title and title not in ret_names:
                        ret_names.append(title)
        else:
            if "alternative" in alias_sources:
                alternative_data = tmdb_info.get("alternative_titles") or {}
                alternative_titles = alternative_data.get("results", []) or [] \
                    if callable(getattr(alternative_data, "get", None)) else []
                for alternative_title in alternative_titles:
                    if not callable(getattr(alternative_title, "get", None)):
                        continue
                    name = alternative_title.get("title")
                    if name and name not in ret_names:
                        ret_names.append(name)
            if "translation" in alias_sources:
                translation_data = tmdb_info.get("translations") or {}
                translations = translation_data.get("translations", []) or [] \
                    if callable(getattr(translation_data, "get", None)) else []
                for translation in translations:
                    translation_value = translation.get("data") or {} \
                        if callable(getattr(translation, "get", None)) else {}
                    name = translation_value.get("name") \
                        if callable(getattr(translation_value, "get", None)) else None
                    if name and name not in ret_names:
                        ret_names.append(name)
        return tmdb_info, ret_names

    @record_tmdb_call
    def __search_tmdb(self, file_media_name,
                      search_type,
                      first_media_year=None,
                      media_year=None,
                      season_number=None,
                      language=None):
        """
        检索tmdb中的媒体信息，匹配返回一条尽可能正确的信息
        :param file_media_name: 剑索的名称
        :param search_type: 类型：电影、电视剧、动漫
        :param first_media_year: 年份，如要是季集需要是首播年份(first_air_date)
        :param media_year: 当前季集年份
        :param season_number: 季集，整数
        :param language: 语言，默认是zh-CN
        :return: TMDB的INFO，同时会将search_type赋值到media_type中
        """
        if not self.search:
            return None
        if not file_media_name:
            return None
        recorder = current_recorder()
        language = language or 'zh-CN'
        cache_key = recognition_cache.cache_key("tmdb", {
            "title": file_media_name,
            "original_title": recorder.original_name if recorder else file_media_name,
            "search_type": getattr(search_type, "value", search_type),
            "first_media_year": first_media_year,
            "media_year": media_year,
            "season_number": season_number,
            "language": language,
            "config_version": recognition_cache.config_version(),
        })
        cached_result = recognition_cache.get("tmdb", cache_key)
        if recorder is not None and cached_result is not recognition_cache.CACHE_MISS \
                and not cached_result:
            # Negative cache entries have no reason/candidate trace to replay;
            # recorded requests perform the query again so failure causes stay accurate.
            cached_result = recognition_cache.CACHE_MISS
        if cached_result is not recognition_cache.CACHE_MISS:
            if recorder is not None:
                recorder.action("cache_hit", input={"layer": "tmdb", "cache_key": cache_key},
                                provider_id=recorder.context.get("active_provider_id"))
            return cached_result
        failure_count = len(recorder._tmdb_failure_events) if recorder is not None else 0
        try:
            result = self.__search_tmdb_uncached(
                file_media_name, search_type, first_media_year,
                media_year, season_number, language)
        except TMDbException:
            # 网络、限流等暂态失败不能污染批次缓存。
            raise
        selection_failure = (recorder is not None and not result and
                             len(recorder._tmdb_failure_events) > failure_count)
        if not selection_failure:
            recognition_cache.put("tmdb", cache_key, result, negative=not result)
        return result

    def _set_tmdb_language(self, language):
        for client in self._tmdb_clients or []:
            client.language = language

    def __search_tmdb_uncached(self, file_media_name,
                      search_type,
                      first_media_year=None,
                      media_year=None,
                      season_number=None,
                      language=None):
        if not self.search:
            return None
        if not file_media_name:
            return None
        if language:
            self._set_tmdb_language(language)
        else:
            self._set_tmdb_language('zh-CN')
        # TMDB检索
        info = {}
        if search_type == MediaType.MOVIE:
            year_range = [first_media_year]
            if first_media_year:
                year_range.append(str(int(first_media_year) + 1))
                year_range.append(str(int(first_media_year) - 1))
            for year in year_range:
                log.debug(
                    f"【Meta】正在识别{search_type.value}：{file_media_name}, 年份={year} ...")
                info = self.__search_movie_by_name(file_media_name, year)
                if (current_recorder() and current_recorder().context.get("decision_reason")
                        == "ambiguous_tmdb"):
                    return {}
                if info:
                    info['media_type'] = MediaType.MOVIE
                    log.info("【Meta】%s 识别到 电影：TMDBID=%s, 名称=%s, 上映日期=%s" % (
                        file_media_name,
                        info.get('id'),
                        info.get('title'),
                        info.get('release_date')))
                    break
        else:
            # 有当前季和当前季集年份，使用精确匹配
            if media_year and season_number:
                log.debug(
                    f"【Meta】正在识别{search_type.value}：{file_media_name}, 季集={season_number}, 季集年份={media_year} ...")
                info = self.__search_tv_by_season(file_media_name,
                                                  media_year,
                                                  season_number)
                if (current_recorder() and current_recorder().context.get("decision_reason")
                        == "ambiguous_tmdb"):
                    return {}
            if not info:
                log.debug(
                    f"【Meta】正在识别{search_type.value}：{file_media_name}, 年份={StringUtils.xstr(first_media_year)} ...")
                info = self.__search_tv_by_name(file_media_name,
                                                first_media_year)
                if (current_recorder() and current_recorder().context.get("decision_reason")
                        == "ambiguous_tmdb"):
                    return {}
            if info:
                info['media_type'] = MediaType.TV
                log.info("【Meta】%s 识别到 电视剧：TMDBID=%s, 名称=%s, 首播日期=%s" % (
                    file_media_name,
                    info.get('id'),
                    info.get('name'),
                    info.get('first_air_date')))
        # 返回
        if info:
            return info
        else:
            log.info("【Meta】%s 以年份 %s 在TMDB中未找到%s信息!" % (
                file_media_name, StringUtils.xstr(first_media_year), search_type.value if search_type else ""))
            return info

    def __search_movie_by_name(self, file_media_name, first_media_year):
        """Find the sole movie whose primary name or alias occurs in the title."""
        try:
            if first_media_year:
                movies = self.search.movies({"query": file_media_name, "year": first_media_year})
            else:
                movies = self.search.movies({"query": file_media_name})
        except Exception as error:
            log.error(f"【Meta】连接TMDB出错：{str(error)}")
            note_tmdb_failure("tmdb_network_error", error)
            return None
        log.debug(f"【Meta】API返回：{str(self.search.total_results)}")
        return self.__select_tmdb_search_candidates(
            file_media_name, movies, default_media_type=MediaType.MOVIE)

    def __search_tv_by_name(self, file_media_name, first_media_year):
        """Find the sole TV result whose primary name or alias occurs in the title."""
        try:
            if first_media_year:
                tvs = self.search.tv_shows({"query": file_media_name,
                                            "first_air_date_year": first_media_year})
            else:
                tvs = self.search.tv_shows({"query": file_media_name})
        except Exception as error:
            ExceptionUtils.exception_traceback(error)
            log.error(f"【Meta】连接TMDB出错：{str(error)}")
            note_tmdb_failure("tmdb_network_error", error)
            return None
        log.debug(f"【Meta】API返回：{str(self.search.total_results)}")
        return self.__select_tmdb_search_candidates(
            file_media_name, tvs, default_media_type=MediaType.TV)

    def __search_tv_by_season(self, file_media_name, media_year, season_number):
        """Search a season query, then resolve by title evidence across results."""
        try:
            tvs = self.search.tv_shows({"query": file_media_name})
        except Exception as error:
            log.error(f"【Meta】连接TMDB出错：{error}")
            note_tmdb_failure("tmdb_network_error", error)
            return None
        return self.__select_tmdb_search_candidates(
            file_media_name, tvs, default_media_type=MediaType.TV)

    @record_tmdb_call
    def __search_multi_tmdb(self, file_media_name):
        """Resolve multi-search results using title and alias evidence."""
        try:
            results = self.search.multi({"query": file_media_name}) or []
        except Exception as error:
            log.error(f"【Meta】连接TMDB出错：{str(error)}")
            note_tmdb_failure("tmdb_network_error", error)
            return None
        log.debug(f"【Meta】API返回：{str(self.search.total_results)}")
        result = self.__select_tmdb_search_candidates(file_media_name, results)
        if result:
            result["media_type"] = MediaType.MOVIE if result.get("media_type") == "movie" \
                else MediaType.TV if result.get("media_type") == "tv" else result.get("media_type")
        return result

    @record_tmdb_call
    def __search_tmdb_web(self, file_media_name, mtype: MediaType):
        """
        检索TMDB网站，直接抓取结果；仅结果唯一时才返回。

        中英文混合标题同样允许查询，由调用方依次提供解析出的标题别名。
        :param file_media_name: 名称
        """
        if not file_media_name:
            return None
        # 缓存键：避免mtype枚举无法哈希的问题
        recorder = current_recorder()
        original_title = (recorder.original_name if recorder else None) or file_media_name
        cache_key = (file_media_name, mtype.value if mtype else None, original_title)
        cached = TmdbWebSearchCache.get(cache_key, _TmdbWebSearchCache_SENTINEL)
        if cached is not _TmdbWebSearchCache_SENTINEL:
            log.info("【Meta】正在从TheDbMovie缓存查询：%s ..." % file_media_name)
            if recorder is not None:
                recorder.action("cache_hit", input={"layer": "tmdb_web", "cache_key": cache_key},
                                provider_id=recorder.context.get("active_provider_id"))
            return cached
        failure_count = len(recorder._tmdb_failure_events) if recorder is not None else 0
        result = None
        log.info("【Meta】正在从TheDbMovie网站查询：%s ..." % file_media_name)
        tmdb_url = "https://www.themoviedb.org/search?%s" % urlencode({"query": file_media_name})
        remaining = recognition_remaining_seconds()
        if remaining is not None and remaining <= 0:
            note_tmdb_timeout(HttpTimeout("recognition_deadline_exceeded"))
            return None
        request_timeout = min(5, remaining) if remaining is not None else 5
        res = RequestUtils(timeout=request_timeout).get_res(url=tmdb_url)
        if (not res and remaining is not None
                and recognition_remaining_seconds() <= 0):
            note_tmdb_timeout(HttpTimeout("recognition_deadline_exceeded"))
        elif not res:
            note_tmdb_failure("tmdb_network_error", "TMDB 网站请求失败")
        if res and res.status_code == 200:
            html_text = res.text
            if html_text:
                try:
                    tmdb_links = []
                    html = etree.HTML(html_text)
                    links = html.xpath("//a[@data-id]/@href")
                    for link in links:
                        if not link or (not link.startswith("/tv") and not link.startswith("/movie")):
                            continue
                        if link not in tmdb_links:
                            tmdb_links.append(link)
                    if tmdb_links:
                        candidates = []
                        for link in tmdb_links:
                            candidate_type = (MediaType.TV if link.startswith("/tv")
                                              else MediaType.MOVIE)
                            tmdbinfo = self.get_tmdb_info(
                                mtype=candidate_type, tmdbid=link.split("/")[-1])
                            if tmdbinfo:
                                tmdbinfo["media_type"] = candidate_type
                                candidates.append(tmdbinfo)
                        result = self.__select_tmdb_search_candidates(file_media_name, candidates)
                    else:
                        log.info("【Meta】%s TMDB网站未查询到媒体信息！" % file_media_name)
                        note_tmdb_failure("tmdb_no_results")
                except Exception as err:
                    log.error("【Meta】TMDB网站候选处理失败：%s" % str(err))
                    note_tmdb_failure("tmdb_network_error", err)
            else:
                note_tmdb_failure("tmdb_no_results")
        elif res:
            note_tmdb_failure("tmdb_network_error", f"HTTP {res.status_code}")
        has_failure = (recorder is not None and
                       len(recorder._tmdb_failure_events) > failure_count)
        if result or not has_failure:
            TmdbWebSearchCache.set(cache_key, result)
        return result

    @staticmethod
    def __get_search_names(meta_info, primary_name=None):
        """
        返回用于兜底检索的标题候选，保留中英文别名和其中的数字。

        主标题未命中后，常规 TMDB 查询和实验室回退都会依次尝试这些候选，
        避免中英文混合发布名只留下其中一部分。
        """
        names = []
        candidates = [primary_name]
        if meta_info:
            candidates.extend([
                meta_info.get_name(),
                meta_info.cn_name,
                meta_info.en_name
            ])
            alternative_names = getattr(meta_info, "alternative_names", []) or []
            if isinstance(alternative_names, str):
                candidates.append(alternative_names)
            else:
                candidates.extend(alternative_names)
        for name in candidates:
            name = name.strip() if isinstance(name, str) else name
            if name and name not in names:
                names.append(name)
        return names

    def __search_by_title_aliases(self, meta_info):
        """
        主标题未命中 TMDB 时，依次按解析出的中英文和动漫别名查询。

        这属于同一发布名的别名兜底，不依赖搜索引擎或 TMDB WEB 实验室开关。
        """
        if not meta_info:
            return None
        primary_name = meta_info.get_name()
        search_type = self.__tmdb_search_type(meta_info.type)
        for search_name in self.__get_search_names(meta_info):
            if search_name == primary_name:
                continue
            recorder = current_recorder()
            if recorder is not None:
                recorder.action("fallback", input={"primary_name": primary_name},
                                output={"alias": search_name, "type": meta_info.type},
                                provider_id=recorder.context.get("active_provider_id"))
            if search_type == MediaType.MOVIE:
                media_info = self.__search_tmdb(file_media_name=search_name,
                                                search_type=MediaType.MOVIE)
            elif search_type == MediaType.TV:
                media_info = self.__search_tmdb(file_media_name=search_name,
                                                search_type=MediaType.TV)
            else:
                media_info = self.__search_multi_tmdb(file_media_name=search_name)
            if media_info:
                return media_info
        # 动漫的罗马字标题可能没有被 TMDB 收录为别名，但 API 仍会返回唯一
        # 候选。此时采用该唯一同类型结果，和 TMDB WEB 的唯一结果策略一致。
        if getattr(meta_info, "alternative_names", None):
            for search_name in self.__get_search_names(meta_info):
                recorder = current_recorder()
                if recorder is not None:
                    recorder.action("fallback", input={"primary_name": primary_name},
                                    output={"unique_alias": search_name, "type": meta_info.type},
                                    provider_id=recorder.context.get("active_provider_id"))
                media_info = self.__search_single_tmdb_result(search_name, search_type)
                if media_info:
                    return media_info
        return None

    def __search_single_tmdb_result(self, file_media_name, mtype):
        """返回 TMDB API 的唯一媒体结果，避免把模糊查询结果误认为匹配。"""
        if not self.search or not file_media_name:
            return None
        mtype = self.__tmdb_search_type(mtype)
        try:
            if mtype == MediaType.MOVIE:
                results = self.search.movies({"query": file_media_name}) or []
                media_type = MediaType.MOVIE
            elif mtype == MediaType.TV:
                results = self.search.tv_shows({"query": file_media_name}) or []
                media_type = MediaType.TV
            else:
                results = [result for result in (self.search.multi({"query": file_media_name}) or [])
                           if result.get("media_type") in ("movie", "tv")]
                media_type = MediaType.MOVIE if results and results[0].get("media_type") == "movie" else MediaType.TV
        except TMDbException as err:
            log.error(f"【Meta】连接TMDB出错：{str(err)}")
            return None
        except Exception as err:
            log.error(f"【Meta】连接TMDB出错：{str(err)}")
            return None
        if len(results) != 1:
            return None
        result = results[0]
        if not result:
            return None
        if mtype not in (MediaType.MOVIE, MediaType.TV):
            media_type = MediaType.MOVIE if result.get("media_type") == "movie" else MediaType.TV
        result["media_type"] = media_type
        log.info("【Meta】%s 从TMDB唯一结果识别到%s：TMDBID=%s, 名称=%s" % (
            file_media_name,
            media_type.value,
            result.get("id"),
            result.get("title") if media_type == MediaType.MOVIE else result.get("name")
        ))
        return result

    def __search_fallback(self, meta_info, mtype=None, primary_name=None):
        """
        执行实验室中的识别增强回退。

        使用 TMDB 网页的唯一结果，并尝试中英文标题候选，以免数字或另一种
        语言在分词时丢失。
        """
        search_names = self.__get_search_names(meta_info, primary_name)
        mtype = self.__tmdb_search_type(mtype or getattr(meta_info, "type", None))
        if self._search_tmdbweb:
            for search_name in search_names:
                media_info = self.__search_tmdb_web(file_media_name=search_name, mtype=mtype)
                if media_info:
                    return media_info
        return None

    @staticmethod
    def __json_safe(value):
        """将 TMDB 返回值和枚举转换为可保存到 JSON 的结构。"""
        if isinstance(value, MediaType):
            return value.value
        if isinstance(value, dict):
            return {key: Media.__json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [Media.__json_safe(item) for item in value]
        # tmdbv3api 返回的媒体对象是 AsObj，字段保存在 __dict__ 中，
        # 不能直接交给 json.dumps。
        if hasattr(value, "__dict__"):
            return {key: Media.__json_safe(item) for key, item in value.__dict__.items()}
        return value

    @staticmethod
    def __meta_snapshot(meta_info):
        if not meta_info:
            return {}
        return Media.__json_safe({
            "type": meta_info.type,
            "recognition_source": meta_info.recognition_source,
            "name": meta_info.get_name(),
            "cn_name": meta_info.cn_name,
            "en_name": meta_info.en_name,
            "alternative_names": getattr(meta_info, "alternative_names", []),
            "year": meta_info.year,
            "season": meta_info.get_season_string(),
            "episode": meta_info.get_episode_string(),
            "part": meta_info.part,
            "resource_type": meta_info.resource_type,
            "resource_effect": meta_info.resource_effect,
            "resource_pix": meta_info.resource_pix,
            "resource_team": meta_info.resource_team,
            "video_encode": meta_info.video_encode,
            "audio_encode": meta_info.audio_encode,
        })

    def __request_ai_parse(self, title, timeout=None):
        """调用已注册的 anitopy-ml 识别器，并记录原始响应。"""
        laboratory = recognition_config("laboratory") or {}
        recognition = recognition_config("recognition") or {}
        provider_settings = ((recognition.get("providers") or {}).get("anitopy_ml") or {})
        effective_endpoint = provider_endpoint("anitopy_ml", recognition, laboratory)
        provider_enabled = self.__recognition_provider_enabled("anitopy_ml")
        recorder = current_recorder()
        stage = recorder.stage if recorder is not None else "resolve"
        if not profile_allows_provider(stage, "anitopy_ml", recognition):
            skip_reason = "provider_excluded_by_profile"
        elif provider_requires_network(registry.discover().get("anitopy_ml")) \
                and not profile_network_allowed(stage, recognition):
            skip_reason = "network_disallowed_by_profile"
        elif not provider_enabled:
            skip_reason = provider_disabled_reason("anitopy_ml", recognition, laboratory)
        elif not effective_endpoint:
            skip_reason = "ai_inference_endpoint_missing"
        elif not title:
            skip_reason = "empty_title"
        else:
            skip_reason = None
        if skip_reason:
            if recorder is not None:
                recorder.add_provider_result(
                    provider_id="anitopy_ml", status="skipped",
                    input={"title": title}, error=skip_reason)
            return None
        provider_options = {"endpoint": effective_endpoint}
        if timeout is not None:
            provider_options["timeout"] = max(min(10.0, float(timeout)), 0.001)
        result = recognition_service.run_provider(
            provider_id="anitopy_ml",
            request=RecognitionRequest(title=title),
            options=provider_options,
            cache_payload={
                "model_revision": provider_settings.get(
                    "model_revision", (provider_settings.get("options") or {}).get(
                        "model_revision", "unknown")),
                "title": title,
            },
            timeout=timeout,
        )
        return result.parsed if result.status == "success" else None

    @staticmethod
    def __recognition_provider_enabled(provider_id):
        recognition = recognition_config("recognition") or {}
        laboratory = recognition_config("laboratory") or {}
        return provider_enabled(provider_id, recognition, laboratory)

    def __record_anitopy_skip(self, title, reason=None):
        """为未进入 AI 调用流程的完整识别请求记录跳过原因。"""
        recorder = current_recorder()
        if recorder is None or recorder.stage != "resolve":
            return
        if any(item.get("provider_id") == "anitopy_ml" for item in recorder.provider_results):
            return
        if reason is None:
            recognition = recognition_config("recognition") or {}
            laboratory = recognition_config("laboratory") or {}
            endpoint = provider_endpoint("anitopy_ml", recognition, laboratory)
            if not self.__recognition_provider_enabled("anitopy_ml"):
                reason = provider_disabled_reason("anitopy_ml", recognition, laboratory)
            elif not endpoint:
                reason = "ai_inference_endpoint_missing"
            else:
                reason = "anitopy_ml_not_invoked"
        recorder.add_provider_result(
            provider_id="anitopy_ml", status="skipped",
            input={"title": title}, error=reason)

    @staticmethod
    def __start_recognition_deadline(recorder):
        """Pin one end-to-end network budget to the current resolve request."""
        if recorder is None or recorder.deadline_monotonic is not None:
            return recorder.deadline_monotonic if recorder else None
        execution_cfg = ((recognition_config("recognition") or {}).get("execution") or {})
        try:
            total_timeout = max(float(execution_cfg.get("total_timeout_seconds", 30)), 0.001)
        except (TypeError, ValueError):
            total_timeout = 30.0
        recorder.deadline_monotonic = time.monotonic() + total_timeout
        return recorder.deadline_monotonic

    @staticmethod
    def __recognition_provider_options(provider_class, provider_settings):
        """Pass only descriptor-declared extension settings to provider constructors."""
        provider_schema = getattr(getattr(provider_class, "descriptor", None), "config_schema", {}) or {}
        return {
            key: provider_settings[key]
            for key in provider_schema
            if key in provider_settings and key not in {"enabled", "reliability"}
        }

    @classmethod
    def __has_additional_recognizer(cls):
        from app.media.recognition.registry import registry as recognizer_registry
        recognition = recognition_config("recognition") or {}
        recorder = current_recorder()
        stage = recorder.stage if recorder is not None else "resolve"
        return any(
            provider_id not in ("local_rules", "anitopy_ml")
            and cls.__recognition_provider_enabled(provider_id)
            and profile_allows_provider(stage, provider_id, recognition)
            and (not provider_requires_network(provider_class)
                 or profile_network_allowed(stage, recognition))
            for provider_id, provider_class in recognizer_registry.discover().items()
        )

    @staticmethod
    def __ai_media_type(value):
        if isinstance(value, MediaType):
            return value
        mapping = {
            "movie": MediaType.MOVIE,
            "tv": MediaType.TV,
            "anime": MediaType.ANIME,
            "电影": MediaType.MOVIE,
            "电视剧": MediaType.TV,
            "动漫": MediaType.ANIME,
        }
        normalized = str(value or "").lower()
        return mapping.get(normalized) or mapping.get(str(value or ""))

    @staticmethod
    def __first_extracted_value(value):
        if isinstance(value, (list, tuple)):
            return value[0] if value else None
        return value

    def __meta_from_ai_result(self, title, result, subtitle=None, used_info=None):
        from app.media.recognition.adapters import meta_from_anitopy_result
        return meta_from_anitopy_result(title, result, subtitle, used_info)

    def __meta_from_standard_result(self, title, parsed, subtitle=None, used_info=None):
        """Adapt a provider's normalized dictionary without invoking parsing again."""
        from app.media.recognition.adapters import meta_from_standard_result
        return meta_from_standard_result(title, parsed, subtitle, used_info)

    def __search_meta_tmdb(self, meta_info, strict=None, cache=True,
                           chinese=True, append_to_response=None):
        """按一套解析结果查询 TMDB，并维护同现有识别一致的缓存。"""
        if not meta_info or not meta_info.get_name():
            return None
        tmdb_type = self.__tmdb_search_type(meta_info.type)
        base_key = self.__make_cache_key(meta_info)
        laboratory = recognition_config("laboratory") or {}
        media_key = recognition_cache.cache_key("tmdb", {
            "resolver_input": base_key,
            "strict": strict,
            "language": "zh-CN",
            "chinese": chinese,
            "append_to_response": self.__json_safe(append_to_response),
            "match_mode": getattr(self._rmt_match_mode, "value", self._rmt_match_mode),
            "web_fallback": laboratory.get("search_tmdbweb", self._search_tmdbweb),
            "config_version": recognition_cache.config_version(),
        })
        # Recognition requests must recompute the winning entity from this
        # request's provider results. The lower-level TMDB query cache remains
        # available, but a cached previous winner must not bypass arbitration.
        if cache and current_recorder() is None and self.meta.get_meta_data_by_key(media_key):
            cache_info = self.meta.get_meta_data_by_key(media_key)
            recorder = current_recorder()
            if recorder is not None:
                recorder.action("tmdb_cache", status="hit", input={"cache_key": media_key},
                                output={"id": cache_info.get("id"), "type": cache_info.get("type")})
            file_media_info = self.get_tmdb_info(mtype=cache_info.get("type"), tmdbid=cache_info.get("id"),
                                                 chinese=chinese, append_to_response=append_to_response) \
                if cache_info.get("id") else None
            meta_info.set_tmdb_info(file_media_info)
            return file_media_info
        recorder = current_recorder()

        def is_ambiguous():
            return bool(recorder and recorder.context.get("decision_reason") == "ambiguous_tmdb")

        if tmdb_type != MediaType.TV and not meta_info.year:
            file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
            if is_ambiguous():
                return None
        elif tmdb_type == MediaType.TV:
            file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                 first_media_year=meta_info.year,
                                                 search_type=tmdb_type,
                                                 media_year=meta_info.year,
                                                 season_number=meta_info.begin_season)
            if is_ambiguous():
                return None
            if not file_media_info and meta_info.year and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(), search_type=tmdb_type)
                if is_ambiguous():
                    return None
        else:
            file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                 first_media_year=meta_info.year,
                                                 search_type=MediaType.MOVIE)
            if is_ambiguous():
                return None
            if not file_media_info:
                file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                     first_media_year=meta_info.year,
                                                     search_type=MediaType.TV)
                if is_ambiguous():
                    return None
            if not file_media_info and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
                if is_ambiguous():
                    return None
        if not file_media_info and not strict:
            file_media_info = self.__search_by_title_aliases(meta_info)
            if is_ambiguous():
                return None
        if not file_media_info and self._search_tmdbweb:
            file_media_info = self.__search_fallback(meta_info=meta_info, mtype=meta_info.type)
            if is_ambiguous():
                return None
        if file_media_info and not file_media_info.get("genres"):
            file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                 tmdbid=file_media_info.get("id"), chinese=chinese,
                                                 append_to_response=append_to_response)
        if file_media_info is not None:
            self.__insert_media_cache(media_key=media_key, file_media_info=file_media_info)
        meta_info.set_tmdb_info(file_media_info)
        return file_media_info

    @staticmethod
    def __same_tmdb(left, right):
        if not left or not right:
            return False
        left_type = getattr(left.get("media_type"), "value", left.get("media_type"))
        right_type = getattr(right.get("media_type"), "value", right.get("media_type"))
        return str(left.get("id")) == str(right.get("id")) and left_type == right_type

    def __tmdb_title_evidence(self, original_title, tmdb_info, release_groups=None,
                              include_aliases=True):
        """Cache title evidence only; the winning TMDB entity is always resolved per request."""
        cfg = recognition_config("recognition") or {}
        decision_cfg = cfg.get("decision") or {}
        title_cfg = decision_cfg.get("title_evidence") or {}
        key = recognition_cache.cache_key("decision", {
            "original_title": original_title,
            "tmdb_info": tmdb_info,
            "release_groups": release_groups,
            "include_aliases": include_aliases,
            "title_evidence": title_cfg,
            "config_version": recognition_cache.config_version(),
        })
        cached = recognition_cache.get("decision", key)
        if cached is not recognition_cache.CACHE_MISS:
            recorder = current_recorder()
            if recorder is not None:
                recorder.action("cache_hit", input={"layer": "decision", "cache_key": key},
                                provider_id=recorder.context.get("active_provider_id"))
            return cached
        result = self.__tmdb_title_evidence_uncached(
            original_title, tmdb_info, release_groups, include_aliases=include_aliases)
        if result.get("level") != "unavailable":
            recognition_cache.put("decision", key, result,
                                  negative=result.get("level") in ("none", "weak"))
        return result

    def __tmdb_title_evidence_uncached(self, original_title, tmdb_info, release_groups=None,
                                       include_aliases=True):
        """Find whether a complete TMDB title occurs in the raw media title."""
        if not original_title or not tmdb_info:
            return {"level": "unavailable", "matched_name": None, "names": []}

        cfg = recognition_config("recognition") or {}
        decision_cfg = cfg.get("decision") or {}
        title_cfg = decision_cfg.get("title_evidence") or {}
        name_sources = set(title_cfg.get(
            "name_sources", ["primary", "original", "alternative", "translation"]))
        names = []
        source_fields = []
        if "primary" in name_sources:
            source_fields.extend(("title", "name"))
        if "original" in name_sources:
            source_fields.extend(("original_title", "original_name"))
        for key in source_fields:
            value = tmdb_info.get(key)
            if isinstance(value, str) and value.strip() and value.strip() not in names:
                names.append(value.strip())
        mtype = tmdb_info.get("media_type")
        tmdb_id = tmdb_info.get("id")
        alias_sources = (name_sources.intersection(("alternative", "translation"))
                         if include_aliases else set())
        if mtype and tmdb_id and alias_sources:
            try:
                _, aliases = self.__search_tmdb_allnames(
                    mtype, tmdb_id, alias_sources=alias_sources)
                for alias in aliases:
                    if isinstance(alias, str) and alias.strip() and alias.strip() not in names:
                        names.append(alias.strip())
            except Exception as error:
                log.warn("【Meta】读取 TMDB 译名失败：%s" % str(error))

        normalization = set(title_cfg.get(
            "normalization", ["nfkc", "casefold", "simplified_chinese",
                              "separators", "whitespace"]))

        def normalize(value, with_positions=False):
            value = str(value)
            output = []
            positions = []
            cluster = ""
            cluster_start = 0

            def emit_cluster(cluster_value, source_start, source_end):
                if not cluster_value:
                    return
                transformed = cluster_value
                if "nfkc" in normalization:
                    transformed = unicodedata.normalize("NFKC", transformed)
                if "simplified_chinese" in normalization:
                    transformed = zhconv.convert(transformed, "zh-cn")
                if "casefold" in normalization:
                    transformed = transformed.casefold()
                for character in transformed:
                    separator = (character == "_" or
                                 not re.match(r"[\w\u3400-\u9fff]", character,
                                              flags=re.UNICODE))
                    if not title_cfg.get("preserve_title_numbers", True) \
                            and character.isdigit():
                        separator = True
                    if "separators" in normalization and separator:
                        character = " "
                    if "whitespace" in normalization and character.isspace():
                        character = " "
                        if output and output[-1] == " ":
                            positions[-1] = (positions[-1][0], source_end)
                            continue
                    output.append(character)
                    positions.append((source_start, source_end))

            for index, character in enumerate(value):
                if cluster and not unicodedata.combining(character):
                    emit_cluster(cluster, cluster_start, index)
                    cluster = ""
                if not cluster:
                    cluster_start = index
                cluster += character
            if cluster:
                emit_cluster(cluster, cluster_start, len(value))
            if "whitespace" in normalization:
                while output and output[0] == " ":
                    output.pop(0)
                    positions.pop(0)
                while output and output[-1] == " ":
                    output.pop()
                    positions.pop()
            normalized = "".join(output)
            return (normalized, positions) if with_positions else normalized

        normalized_title, title_positions = normalize(original_title, with_positions=True)

        if isinstance(release_groups, str):
            release_group_values = release_groups.split("@")
        elif isinstance(release_groups, (list, tuple, set)):
            release_group_values = release_groups
        else:
            release_group_values = []
        normalized_release_groups = {
            re.sub(r"\W", "", normalize(value), flags=re.UNICODE).casefold()
            for value in release_group_values if isinstance(value, str) and value.strip()
        }

        def raw_match_span(start, end):
            if start >= end or end > len(title_positions):
                return None, None, None
            raw_start = title_positions[start][0]
            raw_end = title_positions[end - 1][1]
            return raw_start, raw_end, original_title[raw_start:raw_end]
        min_cjk = max(int(title_cfg.get("min_cjk_chars_for_strong", 3)), 1)
        min_latin = max(int(title_cfg.get("min_latin_chars_for_strong", 4)), 1)
        require_latin_boundary = title_cfg.get("require_latin_word_boundary", True)
        default_technical_tokens = {
            "360p", "480p", "720p", "1080p", "1440p", "2160p", "4k", "8k",
            "8bit", "10bit", "xvid", "x264", "x265", "h264", "h265", "hevc", "av1", "avc",
            "hdr", "hdr10", "dv", "dolby vision", "webdl", "webrip", "bluray", "bdrip",
            "brrip", "hdtv", "pdtv", "dsr", "remux", "uhd", "aac", "ac3", "eac3",
            "dts", "dts-hd", "dts-hd-ma", "dts-x", "truehd", "flac", "atmos", "ddp",
            "dd+", "ddp5.1",
        }
        configured_technical_tokens = title_cfg.get(
            "weak_technical_tokens", default_technical_tokens)
        if not isinstance(configured_technical_tokens, (list, tuple, set)):
            configured_technical_tokens = default_technical_tokens
        technical_title_tokens = {
            re.sub(r"\W", "", normalize(token), flags=re.UNICODE).casefold()
            for token in configured_technical_tokens if isinstance(token, str) and token.strip()
        }
        strong_matches = []
        weak_matches = []
        for name in names:
            normalized_name = normalize(name)
            if not normalized_name:
                continue
            contains_cjk = bool(re.search(r"[\u3400-\u9fff]", normalized_name))
            letter_count = len(re.sub(r"\W", "", normalized_name, flags=re.UNICODE))
            compact_name = re.sub(r"\W", "", normalized_name, flags=re.UNICODE)
            technical_only = compact_name.casefold() in technical_title_tokens
            release_group_only = compact_name.casefold() in normalized_release_groups
            numeric_only = bool(compact_name) and compact_name.isdigit()
            if contains_cjk:
                match = re.search(re.escape(normalized_name), normalized_title)
                strong = (letter_count >= min_cjk and not technical_only and
                          not release_group_only and not numeric_only)
            elif require_latin_boundary:
                match = re.search(r"(?<![\w])" + re.escape(normalized_name) + r"(?![\w])",
                                  normalized_title, flags=re.UNICODE)
                strong = (letter_count >= min_latin and not technical_only and
                          not release_group_only and not numeric_only)
            else:
                match = re.search(re.escape(normalized_name), normalized_title, flags=re.UNICODE)
                strong = (letter_count >= min_latin and not technical_only and
                          not release_group_only and not numeric_only)
            if not match:
                continue
            after = normalized_title[match.end():].lstrip()
            sequel_check = title_cfg.get("check_sequel_prefix", True)
            sequel_prefix = bool(sequel_check and
                                 re.match(r"\d{1,2}\b", after) and
                                 not normalized_name[-1:].isdigit())
            raw_start, raw_end, raw_match = raw_match_span(match.start(), match.end())
            item = {"name": name, "normalized_name": normalized_name,
                    "match": match.group(0), "match_text": raw_match,
                    "match_start": raw_start, "match_end": raw_end,
                    "level": "strong" if strong and not sequel_prefix else "weak",
                    "sequel_prefix": sequel_prefix}
            (strong_matches if item["level"] == "strong" else weak_matches).append(item)
        if strong_matches:
            return {"level": "strong", "matched_names": strong_matches, "weak_matches": weak_matches,
                    "names": names, "input": original_title}
        if weak_matches:
            return {"level": "weak", "matched_names": [], "weak_matches": weak_matches,
                    "names": names, "input": original_title}
        fuzzy_cfg = title_cfg.get("allow_fuzzy_fallback", False)
        if fuzzy_cfg:
            try:
                fuzzy_threshold = float(title_cfg.get("fuzzy_min_score", 0.88))
            except (TypeError, ValueError):
                fuzzy_threshold = 0.88
            title_tokens = normalized_title.split()
            fuzzy_matches = []
            for name in names:
                normalized_name = normalize(name)
                name_tokens = normalized_name.split()
                compact_name = re.sub(r"\W", "", normalized_name, flags=re.UNICODE).casefold()
                char_count = len(compact_name)
                if compact_name in technical_title_tokens or compact_name in normalized_release_groups \
                        or compact_name.isdigit():
                    continue
                if not name_tokens or char_count < (min_cjk if re.search(r"[\u3400-\u9fff]", normalized_name)
                                                    else min_latin):
                    continue
                best = 0.0
                width = len(name_tokens)
                for size in range(max(1, width - 1), width + 2):
                    for start in range(max(1, len(title_tokens) - size + 1)):
                        candidate = " ".join(title_tokens[start:start + size])
                        best = max(best, SequenceMatcher(None, normalized_name, candidate).ratio())
                if best >= fuzzy_threshold:
                    fuzzy_matches.append({"name": name, "normalized_name": normalized_name,
                                          "score": round(best, 4), "level": "fuzzy"})
            if fuzzy_matches:
                best_match = max(fuzzy_matches, key=lambda item: item["score"])
                return {"level": "fuzzy", "matched_names": [], "weak_matches": [],
                        "fuzzy_matches": fuzzy_matches, "fuzzy_score": best_match["score"],
                        "names": names, "input": original_title}
        return {"level": "none", "matched_names": [], "weak_matches": [],
                "names": names, "input": original_title}

    def __get_media_info_with_providers(self, title, subtitle=None, mtype=None, strict=None,
                                        cache=True, chinese=True, append_to_response=None,
                                        name_override=None, year_override=None,
                                        season_override=None, pre_parsed=None):
        """Run all enabled recognizers, then resolve their TMDB evidence."""
        # 自定义识别词必须先于所有解析方式执行，保证本地解析、anitopy
        # 和 AI 推理使用同一份处理后的标题。
        if pre_parsed is not None:
            processed_title = getattr(pre_parsed, "_recognition_processed_title", title)
            processed_subtitle = getattr(pre_parsed, "_recognition_processed_subtitle", subtitle)
            used_info = getattr(pre_parsed, "_recognition_used_info", {}) or {}
        else:
            processed_title, processed_subtitle, used_info = prepare_media_title(title, subtitle)
        recorder = current_recorder()
        recognition_settings = recognition_config("recognition") or {}
        execution_cfg = recognition_settings.get("execution") or {}
        try:
            total_timeout = max(float(execution_cfg.get("total_timeout_seconds", 30)), 0.001)
        except (TypeError, ValueError):
            total_timeout = 30.0
        recognition_deadline = (recorder.deadline_monotonic if recorder is not None
                                and recorder.deadline_monotonic is not None
                                else time.monotonic() + total_timeout)
        if recorder is not None and recorder.deadline_monotonic is None:
            recorder.deadline_monotonic = recognition_deadline
        if recorder is not None:
            recorder.action("preprocess", input={"title": title, "subtitle": subtitle},
                            output={"title": processed_title, "subtitle": processed_subtitle,
                                    "rules": used_info})
        local_meta = copy.copy(pre_parsed) if pre_parsed is not None else MetaInfo(
            processed_title, subtitle=processed_subtitle, mtype=mtype, apply_custom_words=False)
        if pre_parsed is not None and recorder is not None:
            local_meta.recognition_request_id = recorder.request_id
        local_meta.ignored_words = used_info.get("ignored", [])
        local_meta.replaced_words = used_info.get("replaced", [])
        local_meta.offset_words = used_info.get("offset", [])
        if name_override is not None:
            local_meta.cn_name = name_override
        if mtype:
            local_meta.type = mtype

        def resolver_meta(candidate):
            """Apply explicit lookup overrides to a copy, preserving parse evidence."""
            if not candidate:
                return None
            query_meta = copy.copy(candidate)
            if name_override is not None:
                query_meta.cn_name = name_override
            if year_override is not None:
                query_meta.year = str(year_override)
            if season_override is not None:
                query_meta.begin_season = season_override
            if mtype:
                query_meta.type = mtype
            return query_meta

        if recorder is not None and any(value is not None for value in
                                        (name_override, year_override, season_override, mtype)):
            recorder.action("resolution_overrides", input={
                "name": name_override, "year": year_override,
                "season": season_override, "media_type": getattr(mtype, "value", mtype),
            }, output={"title": processed_title})
        if recorder is not None and pre_parsed is not None:
            recorder.context["active_provider_id"] = "local_rules"
            recorder.add_provider_result(
                "local_rules", "success" if local_meta and local_meta.get_name() else "no_result",
                input={"title": processed_title, "subtitle": processed_subtitle,
                       "mtype": getattr(mtype, "value", mtype)},
                normalized_result=self.__meta_snapshot(local_meta))
        ai_remaining = recognition_deadline - time.monotonic()
        ai_result = self.__request_ai_parse(processed_title, timeout=ai_remaining) \
            if ai_remaining > 0 else None
        if ai_remaining <= 0 and recorder is not None:
            recorder.add_provider_result(
                provider_id="anitopy_ml", status="timeout", input={"title": processed_title},
                error="recognition_deadline_exceeded")
        local_tmdb = self.__search_meta_tmdb(resolver_meta(local_meta), strict=strict, cache=cache,
                                             chinese=chinese, append_to_response=append_to_response)
        ai_meta = self.__meta_from_ai_result(processed_title, ai_result,
                                             subtitle=processed_subtitle, used_info=used_info)
        # AI 接口只负责从文件名推断标题。转移时指定的媒体类型只参与
        # TMDB 查询和后续转移，不作为 AI 推理输入。
        if ai_meta and mtype:
            ai_meta.type = mtype
        if ai_meta and name_override is not None:
            ai_meta.cn_name = name_override
        if recorder is not None:
            recorder.context["active_provider_id"] = "anitopy_ml"
        ai_tmdb = self.__search_meta_tmdb(resolver_meta(ai_meta), strict=strict, cache=cache,
                                          chinese=chinese, append_to_response=append_to_response) if ai_meta else None
        additional_candidates = []
        # New providers are discovered from the package and enabled through
        # recognition.providers.<provider_id>. They do not require changes to
        # this orchestration method or a provider-specific UI branch.
        for provider_id, provider_class in sorted(registry.discover().items()):
            if provider_id in ("local_rules", "anitopy_ml") \
                    or not self.__recognition_provider_enabled(provider_id):
                continue
            profile_stage = recorder.stage if recorder is not None else "resolve"
            if not profile_allows_provider(profile_stage, provider_id, recognition_settings):
                if recorder is not None:
                    recorder.add_provider_result(
                        provider_id, "skipped", input={"title": processed_title},
                        error="provider_excluded_by_profile")
                continue
            if provider_requires_network(provider_class) and not profile_network_allowed(
                    profile_stage, recognition_settings):
                if recorder is not None:
                    recorder.add_provider_result(
                        provider_id, "skipped", input={"title": processed_title},
                        error="network_disallowed_by_profile")
                continue
            started = time.monotonic()
            remaining = recognition_deadline - started
            if remaining <= 0:
                if recorder is not None:
                    recorder.add_provider_result(
                        provider_id=provider_id, status="timeout",
                        input={"title": processed_title, "subtitle": processed_subtitle},
                        error="recognition_deadline_exceeded")
                continue
            try:
                provider_settings = ((recognition_config("recognition") or {}).get("providers") or {}).get(
                    provider_id) or {}
                provider_options = self.__recognition_provider_options(provider_class, provider_settings)
                parsed_result = recognition_service.run_provider(
                    provider_id,
                    RecognitionRequest(
                        title=processed_title, subtitle=processed_subtitle,
                        context={"mtype": getattr(mtype, "value", mtype),
                                 "deadline_monotonic": recognition_deadline,
                                 "remaining_timeout_seconds": remaining}),
                    options=provider_options,
                    cache_payload={"title": processed_title, "subtitle": processed_subtitle,
                                   "mtype": getattr(mtype, "value", mtype),
                                   "model_revision": provider_settings.get(
                                       "model_revision", (provider_settings.get("options") or {}).get(
                                           "model_revision", "unknown"))},
                    timeout=remaining,
                )
                parsed_value = parsed_result.parsed
                if parsed_result.status != "success":
                    provider_meta = None
                elif hasattr(parsed_value, "get_name") and hasattr(parsed_value, "type"):
                    provider_meta = parsed_value
                elif isinstance(parsed_value, dict) and isinstance(parsed_value.get("extracted"), dict):
                    provider_meta = self.__meta_from_ai_result(
                        processed_title, {"extracted": parsed_value["extracted"]},
                        subtitle=processed_subtitle, used_info=used_info)
                elif isinstance(parsed_value, dict):
                    provider_meta = self.__meta_from_standard_result(
                        processed_title, parsed_value, processed_subtitle, used_info)
                else:
                    provider_meta = None
                if provider_meta and mtype:
                    provider_meta.type = mtype
                if provider_meta and name_override is not None:
                    provider_meta.cn_name = name_override
                if provider_meta and provider_meta.get_name():
                    if recorder is not None:
                        recorder.context["active_provider_id"] = provider_id
                    provider_tmdb = self.__search_meta_tmdb(
                        resolver_meta(provider_meta), strict=strict, cache=cache, chinese=chinese,
                        append_to_response=append_to_response)
                    additional_candidates.append((provider_id, provider_meta, provider_tmdb))
            except Exception as error:
                if recorder is not None:
                    timed_out = (isinstance(error, (HttpTimeout, TimeoutError))
                                 or time.monotonic() >= recognition_deadline)
                    recorder.add_provider_result(
                        provider_id=provider_id, status="timeout" if timed_out else "error",
                        input={"title": processed_title, "subtitle": processed_subtitle},
                        error=("recognition_deadline_exceeded" if timed_out else str(error)),
                        elapsed_ms=int((time.monotonic() - started) * 1000))
                log.error("【Recognition】识别方式 %s 执行失败：%s" % (provider_id, str(error)))
        if recorder is not None and recorder.context.get("decision_reason") == "ambiguous_tmdb":
            # Complete all configured parser attempts for the recognition trace,
            # but never let a later provider silently override a multi-hit TMDB query.
            return local_meta
        if recorder is not None and time.monotonic() >= recognition_deadline:
            recorder.context["decision_reason"] = "recognition_deadline_exceeded"
            recorder.action("deadline_exceeded", status="timeout",
                            output={"total_timeout_seconds": total_timeout})
        decision_cfg = (recognition_config("recognition") or {}).get("decision") or {}
        strategy = decision_cfg.get("strategy", "legacy")
        shadow = (decision_cfg.get("shadow") or {}).get("enabled", False)
        if strategy != "title_evidence" and not shadow:
            if local_tmdb and (not ai_tmdb or self.__same_tmdb(local_tmdb, ai_tmdb)):
                selected = local_meta
                selected_tmdb = local_tmdb
                selected_provider = "local_rules"
            elif ai_tmdb:
                selected = ai_meta
                selected_tmdb = ai_tmdb
                selected_provider = "anitopy_ml"
            else:
                selected = local_meta if local_meta.get_name() else ai_meta
                selected_tmdb = None
                selected_provider = "local_rules" if selected is local_meta else "anitopy_ml"
            if selected and selected_tmdb:
                selected.set_tmdb_info(selected_tmdb)
                if mtype:
                    selected.type = mtype
            recorder = current_recorder()
            if recorder is not None:
                recorder.context["selected_provider"] = selected_provider
            return selected
        candidates = []
        if local_tmdb:
            candidates.append(("local_rules", local_meta, local_tmdb))
        if ai_tmdb:
            candidates.append(("anitopy_ml", ai_meta, ai_tmdb))
        candidates.extend(candidate for candidate in additional_candidates if candidate[2])
        candidate_evidence = []
        for provider_id, candidate_meta, tmdb_info in candidates:
            if recorder is not None:
                recorder.context["active_provider_id"] = provider_id
            evidence = self.__tmdb_title_evidence(
                (recorder.original_name if recorder is not None else None) or title,
                tmdb_info, release_groups=getattr(candidate_meta, "resource_team", None))
            candidate_evidence.append(((provider_id, candidate_meta, tmdb_info), evidence))
            weights = (((recognition_config("recognition") or {}).get("decision") or {}).get("weights") or {})
            provider_settings = (recognition_settings.get("providers") or {}).get(provider_id) or {}
            try:
                provider_reliability = min(1.0, max(0.0, float(
                    provider_settings.get("reliability", 0.5))))
            except (TypeError, ValueError):
                provider_reliability = 0.5
            features = candidate_features(candidate_meta, tmdb_info, evidence,
                                          provider_reliability=provider_reliability)
            active_weight = sum(max(float(weights.get(key, 0)), 0.0) for key in features)
            evidence["features"] = features
            evidence["score"] = weighted_score(features, weights) if active_weight else None
            if recorder is not None:
                recorder.action("title_match", status=evidence["level"],
                                input={"original_name": title},
                                output={"provider_id": provider_id, **evidence},
                                provider_id=provider_id)
        try:
            agreement_bonus = float(decision_cfg.get("agreement_bonus", 0.0))
        except (TypeError, ValueError):
            agreement_bonus = 0.0
        title_decision = select_title_evidence(candidate_evidence,
                                               agreement_bonus=agreement_bonus)
        entities = title_decision["entities"]
        if recorder is not None:
            recorder.context["decision_metrics"] = {
                "confidence_score": title_decision.get("confidence"),
                "agreement_count": title_decision.get("agreement_count", 0),
                "distinct_tmdb_entities": len(entities),
            }
        if strategy != "title_evidence":
            if recorder is not None:
                recorder.context["shadow_result"] = {
                    "status": title_decision["status"],
                    "reason": title_decision["reason"],
                    "candidate_ids": [key[1] for key in entities],
                    "selected_candidate": (title_decision["selected"][2].get("id")
                                            if title_decision["selected"] else None),
                }
            if local_tmdb and (not ai_tmdb or self.__same_tmdb(local_tmdb, ai_tmdb)):
                selected = local_meta
                selected_tmdb = local_tmdb
                selected_provider = "local_rules"
            elif ai_tmdb:
                selected = ai_meta
                selected_tmdb = ai_tmdb
                selected_provider = "anitopy_ml"
            else:
                selected = local_meta if local_meta.get_name() else ai_meta
                selected_tmdb = None
                selected_provider = "local_rules" if selected is local_meta else "anitopy_ml"
            if selected and selected_tmdb:
                selected.set_tmdb_info(selected_tmdb)
                if mtype:
                    selected.type = mtype
            if recorder is not None:
                recorder.context["selected_provider"] = selected_provider
            return selected
        if title_decision["reason"] == "ambiguous_tmdb":
            if recorder is not None:
                recorder.context["decision_reason"] = "ambiguous_tmdb"
            return None
        if title_decision["status"] == "success":
            selected_provider, selected, selected_tmdb = title_decision["selected"]
        elif candidates:
            if recorder is not None:
                recorder.context["decision_reason"] = title_decision["reason"]
            return None
        elif not candidates:
            if recorder is not None:
                recorder.context["decision_reason"] = (
                    self.__recognition_tmdb_failure_reason(recorder) or "no_tmdb_match")
            selected = local_meta if local_meta.get_name() else ai_meta
            selected_tmdb = None
            selected_provider = "local_rules" if selected is local_meta else "anitopy_ml"
        if selected and (local_tmdb or ai_tmdb):
            selected.set_tmdb_info(selected_tmdb or ai_tmdb)
            if mtype:
                # set_tmdb_info 会按 TMDB 返回值重置 type；转移时仍以用户
                # 选择的类型为准，避免 AI/TMDB 的分类影响转移路径。
                selected.type = mtype
        if recorder is not None:
            recorder.context["selected_provider"] = selected_provider
        return selected

    @record_tmdb_call
    def get_tmdb_info(self, mtype: MediaType,
                      tmdbid,
                      language=None,
                      append_to_response=None,
                      chinese=True):
        """
        给定TMDB号，查询一条媒体信息
        :param mtype: 类型：电影、电视剧、动漫，为空时都查（此时用不上年份）
        :param tmdbid: TMDB的ID，有tmdbid时优先使用tmdbid，否则使用年份和标题
        :param language: 语种
        :param append_to_response: 附加信息
        :param chinese: 是否转换中文标题
        """
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return None
        if language:
            self._set_tmdb_language(language)
        else:
            self._set_tmdb_language('zh-CN')
        if mtype == MediaType.MOVIE:
            tmdb_info = self.__get_tmdb_movie_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.MOVIE
        else:
            tmdb_info = self.__get_tmdb_tv_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.TV
        if tmdb_info:
            # 转换genreid
            tmdb_info['genre_ids'] = self.__get_genre_ids_from_detail(tmdb_info.get('genres'))
            # 转换中文标题
            if chinese:
                tmdb_info = self.__update_tmdbinfo_cn_title(tmdb_info)

        return tmdb_info

    def __update_tmdbinfo_cn_title(self, tmdb_info):
        """
        更新TMDB信息中的中文名称
        """
        # 查找中文名
        org_title = tmdb_info.get("title") if tmdb_info.get("media_type") == MediaType.MOVIE else tmdb_info.get(
            "name")
        if not StringUtils.is_chinese(org_title) and (self.tmdb.language == 'zh-CN'):
            cn_title = self.__get_tmdb_chinese_title(tmdbinfo=tmdb_info)
            if cn_title and cn_title != org_title:
                if tmdb_info.get("media_type") == MediaType.MOVIE:
                    tmdb_info['title'] = cn_title
                else:
                    tmdb_info['name'] = cn_title
        return tmdb_info

    def get_tmdb_infos(self, title, year=None, mtype: MediaType = None, page=1):
        """
        查询名称中有关键字的所有的TMDB信息并返回
        """
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return []
        if not title:
            return []
        if not mtype and not year:
            results = self.__search_multi_tmdbinfos(title)
        else:
            if not mtype:
                results = list(
                    set(self.__search_movie_tmdbinfos(title, year)).union(set(self.__search_tv_tmdbinfos(title, year))))
                # 组合结果的情况下要排序
                results = sorted(results,
                                 key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                                 reverse=True)
            elif mtype == MediaType.MOVIE:
                results = self.__search_movie_tmdbinfos(title, year)
            else:
                results = self.__search_tv_tmdbinfos(title, year)
        return results[(page - 1) * 20:page * 20]

    def __search_multi_tmdbinfos(self, title):
        """
        同时查询模糊匹配的电影、电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        multis = self.search.multi({"query": title}) or []
        for multi in multis:
            if multi.get("media_type") in ["movie", "tv"]:
                multi['media_type'] = MediaType.MOVIE if multi.get("media_type") == "movie" else MediaType.TV
                ret_infos.append(multi)
        return ret_infos

    def __search_movie_tmdbinfos(self, title, year):
        """
        查询模糊匹配的所有电影TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            movies = self.search.movies({"query": title, "year": year}) or []
        else:
            movies = self.search.movies({"query": title}) or []
        for movie in movies:
            if title in movie.get("title"):
                movie['media_type'] = MediaType.MOVIE
                ret_infos.append(movie)
        return ret_infos

    def __search_tv_tmdbinfos(self, title, year):
        """
        查询模糊匹配的所有电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            tvs = self.search.tv_shows({"query": title, "first_air_date_year": year}) or []
        else:
            tvs = self.search.tv_shows({"query": title}) or []
        for tv in tvs:
            if title in tv.get("name"):
                tv['media_type'] = MediaType.TV
                ret_infos.append(tv)
        return ret_infos

    @staticmethod
    def __make_cache_key(meta_info):
        """
        生成缓存的key
        """
        if not meta_info:
            return None
        cache_name = meta_info.get_name()
        alternative_names = getattr(meta_info, "alternative_names", []) or []
        if isinstance(alternative_names, str):
            alternative_names = [alternative_names]
        if len(alternative_names) > 1:
            # 多别名标题此前只以最后一个别名建缓存；将完整候选写入新键，
            # 使历史的“未识别”缓存不会阻止本次别名重试。
            cache_name = "|".join(alternative_names)
        return f"[{meta_info.type.value}]{cache_name}-{meta_info.year}-{meta_info.begin_season}"

    def get_cache_info(self, meta_info):
        """
        根据名称查询是否已经有缓存
        """
        if not meta_info:
            return {}
        return self.meta.get_meta_data_by_key(self.__make_cache_key(meta_info))

    def get_media_info(self, title,
                       subtitle=None,
                       mtype=None,
                       strict=None,
                       cache=True,
                       chinese=True,
                       append_to_response=None,
                       pre_parsed=None):
        """Resolve a media title and persist its complete recognition trace."""
        caller_module = sys._getframe(1).f_globals.get("__name__", "")
        source = _infer_recognition_source(caller_module)
        if source == "unknown":
            source = "media.get_media_info"
        with recognition_scope(
                title,
                source=source,
                stage="resolve",
                context={"subtitle": subtitle, "mtype": getattr(mtype, "value", mtype),
                         "strict": strict, "cache": cache, "chinese": chinese,
                         "caller_module": caller_module}) as recorder:
            self.__start_recognition_deadline(recorder)
            tmdb_allowed = profile_tmdb_allowed(
                "resolve", recognition_config("recognition") or {})
            result = self._get_media_info_impl(
                title=title, subtitle=subtitle, mtype=mtype, strict=strict,
                cache=cache, chinese=chinese, append_to_response=append_to_response,
                pre_parsed=pre_parsed)
            parsed = self.__meta_snapshot(result) if result else None
            tmdb_result = self.__json_safe(result.tmdb_info) if result and result.tmdb_info else None
            if not title:
                status, reason = "failed", "empty_title"
            elif not tmdb_allowed:
                status, reason = "skipped", "tmdb_disallowed_by_profile"
            elif not self.tmdb:
                status, reason = "failed", "tmdb_unavailable"
            elif recorder.context.get("decision_reason"):
                status, reason = "failed", recorder.context["decision_reason"]
            elif recorder.deadline_monotonic is not None \
                    and time.monotonic() >= recorder.deadline_monotonic:
                status, reason = "failed", "recognition_deadline_exceeded"
            elif not parsed or not parsed.get("name"):
                status, reason = "failed", "no_name_parsed"
            elif tmdb_result:
                status, reason = "success", None
            else:
                status, reason = "failed", (
                    self.__recognition_tmdb_failure_reason(recorder) or "no_tmdb_match")
            recorder.set_overall(
                status=status,
                reason=reason,
                parsed_result=parsed,
                selected_provider=recorder.context.get("selected_provider") or
                                  ("anitopy_ml" if result and result.recognition_source == "ai" else "local_rules"),
                tmdb_result=tmdb_result,
            )
            if not tmdb_allowed:
                recorder.overall_result["tmdb_status"] = "not_requested"
            if result is not None:
                result.recognition_request_id = recorder.request_id
            return result

    def _get_media_info_impl(self, title,
                       subtitle=None,
                       mtype=None,
                       strict=None,
                       cache=True,
                       chinese=True,
                       append_to_response=None,
                       pre_parsed=None):
        """
        只有名称信息，判别是电影还是电视剧并搜刮TMDB信息，用于种子名称识别
        :param title: 种子名称
        :param subtitle: 种子副标题
        :param mtype: 类型：电影、电视剧、动漫
        :param strict: 是否严格模式，为true时，不会再去掉年份再查一次
        :param cache: 是否使用缓存，默认TRUE
        :param chinese: 原标题为英文时是否从别名中检索中文名称
        :param append_to_response: 额外查询的信息
        :return: 带有TMDB信息的MetaInfo对象
        """
        if not title:
            return None
        if not profile_tmdb_allowed("resolve", recognition_config("recognition") or {}):
            parsed = MetaInfo(title, subtitle=subtitle, mtype=mtype,
                              pre_parsed=pre_parsed)
            return parsed
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return None
        laboratory = recognition_config("laboratory") or {}
        recognition = recognition_config("recognition") or {}
        ai_endpoint = provider_endpoint("anitopy_ml", recognition, laboratory)
        if ((ai_endpoint
             and self.__recognition_provider_enabled("anitopy_ml"))
                or self.__has_additional_recognizer() or pre_parsed is not None):
            return self.__get_media_info_with_providers(title=title, subtitle=subtitle, mtype=mtype,
                                                 strict=strict, cache=cache, chinese=chinese,
                                                 append_to_response=append_to_response,
                                                 pre_parsed=pre_parsed)
        self.__record_anitopy_skip(title)
        # 识别
        recorder = current_recorder()
        if recorder is not None:
            recorder.context["active_provider_id"] = "local_rules"
        meta_info = pre_parsed if pre_parsed is not None else MetaInfo(title, subtitle=subtitle)
        if not meta_info.get_name() or not meta_info.type:
            log.warn("【Rmt】%s 未识别出有效信息！" % meta_info.org_string)
            return None
        if mtype:
            meta_info.type = mtype
        tmdb_type = self.__tmdb_search_type(meta_info.type)
        media_key = self.__make_cache_key(meta_info)
        if not cache or not self.meta.get_meta_data_by_key(media_key):
            # 缓存没有或者强制不使用缓存
            if tmdb_type != MediaType.TV and not meta_info.year:
                file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
            else:
                if tmdb_type == MediaType.TV:
                    # 确定是电视
                    file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                         first_media_year=meta_info.year,
                                                         search_type=tmdb_type,
                                                         media_year=meta_info.year,
                                                         season_number=meta_info.begin_season
                                                         )
                    if not file_media_info and meta_info.year and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                        # 非严格模式下去掉年份再查一次
                        file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                             search_type=tmdb_type
                                                             )
                else:
                    # 有年份先按电影查
                    file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                         first_media_year=meta_info.year,
                                                         search_type=MediaType.MOVIE
                                                         )
                    # 没有再按电视剧查
                    if not file_media_info:
                        file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                             first_media_year=meta_info.year,
                                                             search_type=MediaType.TV
                                                             )
                    if not file_media_info and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                        # 非严格模式下去掉年份和类型再查一次
                        file_media_info = self.__search_multi_tmdb(file_media_name=meta_info.get_name())
            if recorder is not None and recorder.context.get("decision_reason") == "ambiguous_tmdb":
                return meta_info
            if not file_media_info and not strict:
                file_media_info = self.__search_by_title_aliases(meta_info)
            if recorder is not None and recorder.context.get("decision_reason") == "ambiguous_tmdb":
                return meta_info
            if not file_media_info and self._search_tmdbweb:
                file_media_info = self.__search_fallback(meta_info=meta_info,
                                                         mtype=meta_info.type)
            # 补充全量信息
            if file_media_info and not file_media_info.get("genres"):
                file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                     tmdbid=file_media_info.get("id"),
                                                     chinese=chinese,
                                                     append_to_response=append_to_response)
            # 保存到缓存
            if file_media_info is not None:
                self.__insert_media_cache(media_key=media_key,
                                          file_media_info=file_media_info)
        else:
            # 使用缓存信息
            cache_info = self.meta.get_meta_data_by_key(media_key)
            if cache_info.get("id"):
                file_media_info = self.get_tmdb_info(mtype=cache_info.get("type"),
                                                     tmdbid=cache_info.get("id"),
                                                     chinese=chinese,
                                                     append_to_response=append_to_response)
            else:
                file_media_info = None
            if recorder is not None:
                recorder.action("tmdb_cache", status="hit", input={"cache_key": media_key},
                                output={"id": cache_info.get("id"),
                                        "type": cache_info.get("type")})
        # 普通本地解析也遵循可配置的 TMDB 标题证据策略。
        recorder = current_recorder()
        recognition_cfg = recognition_config("recognition") or {}
        decision_cfg = recognition_cfg.get("decision") or {}
        strategy = decision_cfg.get("strategy", "legacy")
        shadow_enabled = (decision_cfg.get("shadow") or {}).get("enabled", False)
        if file_media_info:
            evidence = self.__tmdb_title_evidence(
                title, file_media_info, release_groups=getattr(meta_info, "resource_team", None))
            if recorder is not None:
                recorder.action("title_match", status=evidence["level"],
                                input={"original_name": title}, output=evidence,
                                provider_id="local_rules")
            if strategy == "title_evidence" and evidence["level"] not in ("strong", "fuzzy"):
                if recorder is not None:
                    recorder.context["decision_reason"] = "insufficient_title_evidence"
                file_media_info = None
            elif strategy != "title_evidence" and shadow_enabled and recorder is not None:
                recorder.context["shadow_result"] = {
                    "status": "success" if evidence["level"] in ("strong", "fuzzy") else "failed",
                    "reason": None if evidence["level"] in ("strong", "fuzzy") else "insufficient_title_evidence",
                    "candidate_ids": [file_media_info.get("id")],
                    "selected_candidate": file_media_info.get("id") if evidence["level"] in ("strong", "fuzzy") else None,
                }
        # 赋值TMDB信息并返回
        meta_info.set_tmdb_info(file_media_info)
        return meta_info

    def get_media_info_original_title(self, title,
                        name=None,
                        subtitle=None,
                        year=None,
                        season=None,
                        mtype=None,
                        strict=None,
                        cache=True,
                        chinese=True,
                        append_to_response=None,
                        pre_parsed=None):
        """Record and resolve a title while preserving its supplied name context."""
        caller_module = sys._getframe(1).f_globals.get("__name__", "")
        source = _infer_recognition_source(caller_module)
        if source == "unknown":
            source = "media.get_media_info_original_title"
        with recognition_scope(
                name or title, source=source, stage="resolve",
                context={"title": title, "name": name, "subtitle": subtitle, "year": year,
                         "season": season, "mtype": getattr(mtype, "value", mtype),
                         "strict": strict, "cache": cache, "chinese": chinese,
                         "pre_parsed": pre_parsed is not None,
                         "caller_module": caller_module}) as recorder:
            self.__start_recognition_deadline(recorder)
            tmdb_allowed = profile_tmdb_allowed(
                "resolve", recognition_config("recognition") or {})
            result = self._get_media_info_original_title_impl(
                title=title, name=name, subtitle=subtitle, year=year, season=season,
                mtype=mtype, strict=strict, cache=cache, chinese=chinese,
                append_to_response=append_to_response, pre_parsed=pre_parsed)
            parsed = self.__meta_snapshot(result) if result else None
            tmdb_result = self.__json_safe(result.tmdb_info) if result and result.tmdb_info else None
            reason = recorder.context.get("decision_reason")
            if not title:
                status, reason = "failed", "empty_title"
            elif not tmdb_allowed:
                status, reason = "skipped", "tmdb_disallowed_by_profile"
            elif not self.tmdb:
                status, reason = "failed", "tmdb_unavailable"
            elif reason:
                status = "failed"
            elif recorder.deadline_monotonic is not None \
                    and time.monotonic() >= recorder.deadline_monotonic:
                status, reason = "failed", "recognition_deadline_exceeded"
            elif tmdb_result:
                status, reason = "success", None
            else:
                status, reason = "failed", (
                    self.__recognition_tmdb_failure_reason(recorder) or "no_tmdb_match")
            recorder.set_overall(status, reason, parsed,
                                 recorder.context.get("selected_provider") or "local_rules",
                                 tmdb_result)
            if not tmdb_allowed:
                recorder.overall_result["tmdb_status"] = "not_requested"
            return result

    def _get_media_info_original_title_impl(self, title,
                        name=None,
                        subtitle=None,
                        year=None,
                        season=None,
                        mtype=None,
                        strict=None,
                        cache=True,
                        chinese=True,
                        append_to_response=None,
                        pre_parsed=None):
        """
        只有名称信息，判别是电影还是电视剧并搜刮TMDB信息，用于种子名称识别
        :param title: 用于格式化查询的名称
        :param name: 原始名称
        :param subtitle: 种子副标题
        :param year: 年份
        :param season: 季
        :param mtype: 类型：电影、电视剧、动漫
        :param strict: 是否严格模式，为true时，不会再去掉年份再查一次
        :param cache: 是否使用缓存，默认TRUE
        :param chinese: 原标题为英文时是否从别名中检索中文名称
        :param append_to_response: 额外查询的信息
        :return: 带有TMDB信息的MetaInfo对象
        """
        if not title:
            return None
        if not profile_tmdb_allowed("resolve", recognition_config("recognition") or {}):
            parsed = copy.copy(pre_parsed) if pre_parsed is not None else MetaInfo(
                title, subtitle=subtitle, mtype=mtype)
            if name is not None:
                parsed.cn_name = name
            if year is not None:
                parsed.year = str(year)
            if season is not None:
                parsed.begin_season = season
            return parsed
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return None
        laboratory = recognition_config("laboratory") or {}
        recognition = recognition_config("recognition") or {}
        ai_endpoint = provider_endpoint("anitopy_ml", recognition, laboratory)
        if (pre_parsed is not None or (ai_endpoint
             and self.__recognition_provider_enabled("anitopy_ml"))
                or self.__has_additional_recognizer()):
            return self.__get_media_info_with_providers(
                title=title, subtitle=subtitle, mtype=mtype, strict=strict,
                cache=cache, chinese=chinese, append_to_response=append_to_response,
                name_override=name, year_override=year, season_override=season,
                pre_parsed=pre_parsed)
        self.__record_anitopy_skip(title)
        meta_info = copy.copy(pre_parsed) if pre_parsed is not None else MetaInfo(
            title, subtitle=subtitle)
        meta_info.cn_name = name
        tmdb_type = self.__tmdb_search_type(mtype or meta_info.type)
        recorder = current_recorder()
        if recorder is not None:
            recorder.context["active_provider_id"] = "local_rules"

        media_key = self.__make_cache_key(meta_info)
        if not cache or not self.meta.get_meta_data_by_key(media_key):
            # 缓存没有或者强制不使用缓存
            if tmdb_type != MediaType.TV and not year:
                file_media_info = self.__search_multi_tmdb(file_media_name=name)
            else:
                if tmdb_type == MediaType.TV:
                    # 确定是电视
                    file_media_info = self.__search_tmdb(file_media_name=name,
                                                         first_media_year=year,
                                                         search_type=tmdb_type,
                                                         media_year=year,
                                                         season_number=season
                                                         )
                    if not file_media_info and year and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                        # 非严格模式下去掉年份再查一次
                        file_media_info = self.__search_tmdb(file_media_name=name,
                                                             search_type=tmdb_type
                                                             )
                else:
                    # 有年份先按电影查
                    file_media_info = self.__search_tmdb(file_media_name=name,
                                                         first_media_year=year,
                                                         search_type=MediaType.MOVIE
                                                         )
                    # 没有再按电视剧查
                    if not file_media_info:
                        file_media_info = self.__search_tmdb(file_media_name=name,
                                                             first_media_year=year,
                                                             search_type=MediaType.TV
                                                             )
                    if not file_media_info and self._rmt_match_mode == MatchMode.NORMAL and not strict:
                        # 非严格模式下去掉年份和类型再查一次
                        file_media_info = self.__search_multi_tmdb(file_media_name=name)
            if recorder is not None and recorder.context.get("decision_reason") == "ambiguous_tmdb":
                return meta_info
            if not file_media_info and self._search_tmdbweb:
                file_media_info = self.__search_fallback(meta_info=meta_info,
                                                         mtype=mtype,
                                                         primary_name=name)
            # 补充全量信息
            if file_media_info and not file_media_info.get("genres"):
                file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                     tmdbid=file_media_info.get("id"),
                                                     chinese=chinese,
                                                     append_to_response=append_to_response)
                # 保存到缓存
            if file_media_info is not None:
                    self.__insert_media_cache(media_key=media_key,
                                              file_media_info=file_media_info)
        else:
            # 使用缓存信息
            cache_info = self.meta.get_meta_data_by_key(media_key)
            if recorder is not None:
                recorder.action("tmdb_cache", status="hit", input={"cache_key": media_key},
                                output={"id": cache_info.get("id"), "type": cache_info.get("type")})
            if cache_info.get("id"):
                file_media_info = self.get_tmdb_info(mtype=cache_info.get("type"),
                                                     tmdbid=cache_info.get("id"),
                                                     chinese=chinese,
                                                     append_to_response=append_to_response)
            else:
                file_media_info = None
        strategy_cfg = (recognition_config("recognition") or {}).get("decision") or {}
        strategy = strategy_cfg.get("strategy", "legacy")
        shadow_enabled = (strategy_cfg.get("shadow") or {}).get("enabled", False)
        if file_media_info:
            evidence = self.__tmdb_title_evidence(
                name or title, file_media_info, release_groups=getattr(meta_info, "resource_team", None))
            if recorder is not None:
                recorder.action("title_match", status=evidence["level"],
                                input={"original_name": name or title}, output=evidence,
                                provider_id="local_rules")
            if strategy == "title_evidence" and evidence["level"] not in ("strong", "fuzzy"):
                if recorder is not None:
                    recorder.context["decision_reason"] = "insufficient_title_evidence"
                file_media_info = None
            elif strategy != "title_evidence" and shadow_enabled and recorder is not None:
                recorder.context["shadow_result"] = {
                    "status": "success" if evidence["level"] in ("strong", "fuzzy") else "failed",
                    "reason": None if evidence["level"] in ("strong", "fuzzy") else "insufficient_title_evidence",
                    "candidate_ids": [file_media_info.get("id")],
                    "selected_candidate": file_media_info.get("id") if evidence["level"] in ("strong", "fuzzy") else None,
                }
        # 赋值TMDB信息并返回
        meta_info.set_tmdb_info(file_media_info)
        return meta_info

    def __insert_media_cache(self, media_key, file_media_info):
        """
        将TMDB信息插入缓存
        """
        if file_media_info:
            # 缓存标题
            cache_title = file_media_info.get(
                "title") if file_media_info.get(
                "media_type") == MediaType.MOVIE else file_media_info.get("name")
            # 缓存年份
            cache_year = file_media_info.get('release_date') if file_media_info.get(
                "media_type") == MediaType.MOVIE else file_media_info.get('first_air_date')
            if cache_year:
                cache_year = cache_year[:4]
            self.meta.update_meta_data({
                media_key: {
                    "id": file_media_info.get("id"),
                    "type": file_media_info.get("media_type"),
                    "year": cache_year,
                    "title": cache_title,
                    "poster_path": file_media_info.get("poster_path"),
                    "backdrop_path": file_media_info.get("backdrop_path")
                }
            })
        else:
            self.meta.update_meta_data({media_key: {'id': 0}})

    def get_media_info_on_files(self,
                                file_list,
                                tmdb_info=None,
                                media_type=None,
                                season=None,
                                episode_format: EpisodeFormat = None,
                                chinese=True):
        """Create one complete recognition record for each file in a batch."""
        paths = file_list if isinstance(file_list, list) else [file_list]
        results = {}
        batch_id = str(uuid.uuid4())
        caller_module = sys._getframe(1).f_globals.get("__name__", "")
        source = _infer_recognition_source(caller_module)
        if source == "unknown":
            source = "media.get_media_info_on_files"
        for file_path in paths:
            with recognition_scope(
                    os.path.basename(str(file_path)), source=source,
                    stage="resolve", context={"file_path": str(file_path), "batch_id": batch_id,
                                               "tmdb_info_supplied": bool(tmdb_info),
                                               "media_type": getattr(media_type, "value", media_type),
                                               "season": season, "caller_module": caller_module}) as recorder:
                self.__start_recognition_deadline(recorder)
                resolved = self._get_media_info_on_files_impl(
                    file_path, tmdb_info=tmdb_info, media_type=media_type, season=season,
                    episode_format=episode_format, chinese=chinese)
                result = resolved.get(file_path) if resolved else None
                if result:
                    result.recognition_request_id = recorder.request_id
                parsed = self.__meta_snapshot(result) if result else None
                tmdb_result = self.__json_safe(result.tmdb_info) if result and result.tmdb_info else None
                status = "success" if tmdb_result else "failed"
                reason = None if status == "success" else (
                    "tmdb_unavailable" if not self.tmdb else
                    (self.__recognition_tmdb_failure_reason(recorder) or "no_tmdb_match"))
                if recorder.context.get("decision_reason"):
                    status, reason = "failed", recorder.context["decision_reason"]
                elif status != "success" and recorder.deadline_monotonic is not None \
                        and time.monotonic() >= recorder.deadline_monotonic:
                    reason = "recognition_deadline_exceeded"
                recorder.set_overall(status, reason, parsed,
                                     recorder.context.get("selected_provider") or "local_rules",
                                     tmdb_result)
                if result:
                    results[file_path] = result
        return results

    def _get_media_info_on_files_impl(self,
                                file_list,
                                tmdb_info=None,
                                media_type=None,
                                season=None,
                                episode_format: EpisodeFormat = None,
                                chinese=True):
        """
        根据文件清单，搜刮TMDB信息，用于文件名称的识别
        :param file_list: 文件清单，如果是列表也可以是单个文件，也可以是一个目录
        :param tmdb_info: 如有传入TMDB信息则以该TMDB信息赋于所有文件，否则按名称从TMDB检索，用于手工识别时传入
        :param media_type: 媒体类型：电影、电视剧、动漫，如有传入以该类型赋于所有文件，否则按名称从TMDB检索并识别
        :param season: 季号，如有传入以该季号赋于所有文件，否则从名称中识别
        :param episode_format: EpisodeFormat
        :param chinese: 原标题为英文时是否从别名中检索中文名称
        :return: 带有TMDB信息的每个文件对应的MetaInfo对象字典
        """
        # 存储文件路径与媒体的对应关系
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return {}
        return_media_infos = {}
        # 不是list的转为list
        if not isinstance(file_list, list):
            file_list = [file_list]
        # 遍历每个文件，看得出来的名称是不是不一样，不一样的先搜索媒体信息
        for file_path in file_list:
            try:
                if not os.path.exists(file_path):
                    log.warn("【Meta】%s 不存在" % file_path)
                    recorder = current_recorder()
                    if recorder is not None:
                        recorder.context["decision_reason"] = "file_not_found"
                        recorder.action("file_skip", status="skipped", input={"file_path": file_path},
                                        reason="file_not_found")
                    continue
                # 解析媒体名称
                # 先用自己的名称
                file_name = os.path.basename(file_path)
                parent_name = os.path.basename(os.path.dirname(file_path))
                parent_parent_name = os.path.basename(PathUtils.get_parent_paths(file_path, 2))
                # 过滤掉蓝光原盘目录下的子文件
                if not os.path.isdir(file_path) \
                        and PathUtils.get_bluray_dir(file_path):
                    log.info("【Meta】%s 跳过蓝光原盘文件：" % file_path)
                    recorder = current_recorder()
                    if recorder is not None:
                        recorder.context["decision_reason"] = "bluray_directory_child"
                        recorder.action("file_skip", status="skipped", input={"file_path": file_path},
                                        reason="bluray_directory_child")
                    continue
                # 没有自带 TMDB 信息时，尝试从标题识别并查询候选。
                if not tmdb_info:
                    laboratory = recognition_config("laboratory") or {}
                    recognition = recognition_config("recognition") or {}
                    if ((provider_endpoint("anitopy_ml", recognition, laboratory)
                         and self.__recognition_provider_enabled("anitopy_ml"))
                            or self.__has_additional_recognizer()):
                        # AI 只接收文件名；media_type 仅在 AI 返回后用于 TMDB 查询
                        # 和转移类型，不参与 AI 推理。
                        ai_media_info = self.get_media_info(title=file_name, mtype=media_type,
                                                             chinese=chinese)
                        # 识别页面可以展示没有 TMDB 的解析结果，但文件转移必须
                        # 有完整 TMDB 信息，否则 FileTransfer 会判定为未识别。
                        if ai_media_info and ai_media_info.tmdb_info:
                            return_media_infos[file_path] = ai_media_info
                            continue
                        recorder = current_recorder()
                        if recorder is not None and recorder.context.get("decision_reason"):
                            # A title-evidence conflict is terminal for this
                            # file; directory fallback must not silently pick
                            # a different entity after an ambiguous decision.
                            continue
                    else:
                        self.__record_anitopy_skip(file_name)
                    # 识别名称
                    meta_info = MetaInfo(title=file_name, include_ai=False)
                    # 识别不到则使用上级的名称
                    if not meta_info.get_name() or not meta_info.year:
                        recorder = current_recorder()
                        if recorder is not None:
                            recorder.action("fallback", input={"file_name": file_name},
                                            output={"parent_name": parent_name},
                                            reason="file_name_insufficient")
                        parent_info = MetaInfo(parent_name, include_ai=False)
                        if not parent_info.get_name() or not parent_info.year:
                            if recorder is not None:
                                recorder.action("fallback", input={"parent_name": parent_name},
                                                output={"parent_parent_name": parent_parent_name},
                                                reason="parent_name_insufficient")
                            parent_parent_info = MetaInfo(parent_parent_name, include_ai=False)
                            parent_info.type = parent_parent_info.type if parent_parent_info.type and parent_info.type != MediaType.TV else parent_info.type
                            parent_info.cn_name = parent_parent_info.cn_name if parent_parent_info.cn_name else parent_info.cn_name
                            parent_info.en_name = parent_parent_info.en_name if parent_parent_info.en_name else parent_info.en_name
                            parent_info.year = parent_parent_info.year if parent_parent_info.year else parent_info.year
                            parent_info.begin_season = NumberUtils.max_ele(parent_info.begin_season,
                                                                           parent_parent_info.begin_season)
                        if not meta_info.get_name():
                            meta_info.cn_name = parent_info.cn_name
                            meta_info.en_name = parent_info.en_name
                        if not meta_info.year:
                            meta_info.year = parent_info.year
                        if parent_info.type and parent_info.type == MediaType.TV \
                                and meta_info.type != MediaType.TV:
                            meta_info.type = parent_info.type
                        if meta_info.type == MediaType.TV:
                            meta_info.begin_season = NumberUtils.max_ele(parent_info.begin_season,
                                                                         meta_info.begin_season)
                    if not meta_info.get_name() or not meta_info.type:
                        log.warn("【Rmt】%s 未识别出有效信息！" % meta_info.org_string)
                        recorder = current_recorder()
                        if recorder is not None:
                            recorder.context["decision_reason"] = "no_name_parsed"
                        continue
                    # 区配缓存及TMDB
                    media_key = self.__make_cache_key(meta_info)
                    recorder = current_recorder()
                    cached_info = self.meta.get_meta_data_by_key(media_key)
                    # Recorded recognition/transfer requests must arbitrate
                    # against current parser and TMDB evidence. In particular,
                    # a legacy id=0 negative cache can survive a previous
                    # failed lookup and otherwise prevent file transfer from
                    # seeing a later successful recognition.
                    if recorder is not None or not cached_info:
                        # 没有缓存数据
                        file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                             first_media_year=meta_info.year,
                                                             search_type=meta_info.type,
                                                             media_year=meta_info.year,
                                                             season_number=meta_info.begin_season)
                        if not file_media_info:
                            if self._rmt_match_mode == MatchMode.NORMAL:
                                # 去掉年份再查一次，有可能是年份错误
                                file_media_info = self.__search_tmdb(file_media_name=meta_info.get_name(),
                                                                     search_type=meta_info.type)
                        if not file_media_info:
                            file_media_info = self.__search_by_title_aliases(meta_info)
                        if not file_media_info and self._search_tmdbweb:
                            file_media_info = self.__search_fallback(meta_info=meta_info,
                                                                     mtype=meta_info.type)
                        recorder = current_recorder()
                        if recorder is not None and recorder.context.get("decision_reason") == "ambiguous_tmdb":
                            continue
                        # 补全TMDB信息
                        if file_media_info and not file_media_info.get("genres"):
                            file_media_info = self.get_tmdb_info(mtype=file_media_info.get("media_type"),
                                                                 tmdbid=file_media_info.get("id"),
                                                                 chinese=chinese)
                        # 保存到缓存
                        if file_media_info is not None:
                            self.__insert_media_cache(media_key=media_key,
                                                      file_media_info=file_media_info)
                    else:
                        # 使用缓存信息
                        cache_info = cached_info
                        if cache_info.get("id"):
                            file_media_info = self.get_tmdb_info(mtype=cache_info.get("type"),
                                                                 tmdbid=cache_info.get("id"),
                                                                 chinese=chinese)
                        else:
                            # 缓存为未识别
                            file_media_info = None
                    recorder = current_recorder()
                    recognition_decision = (recognition_config("recognition") or {}).get("decision") or {}
                    strategy = recognition_decision.get("strategy", "legacy")
                    shadow_enabled = (recognition_decision.get("shadow") or {}).get("enabled", False)
                    if file_media_info:
                        evidence = self.__tmdb_title_evidence(
                            file_name, file_media_info,
                            release_groups=getattr(meta_info, "resource_team", None))
                        if recorder is not None:
                            recorder.action("title_match", status=evidence["level"],
                                            input={"original_name": file_name}, output=evidence,
                                            provider_id="local_rules")
                        if strategy == "title_evidence" and evidence["level"] not in ("strong", "fuzzy"):
                            if recorder is not None:
                                recorder.context["decision_reason"] = "insufficient_title_evidence"
                            continue
                        if strategy != "title_evidence" and shadow_enabled and recorder is not None:
                            recorder.context["shadow_result"] = {
                                "status": "success" if evidence["level"] in ("strong", "fuzzy") else "failed",
                                "reason": None if evidence["level"] in ("strong", "fuzzy") else "insufficient_title_evidence",
                                "candidate_ids": [file_media_info.get("id")],
                                "selected_candidate": file_media_info.get("id") if evidence["level"] in ("strong", "fuzzy") else None,
                            }
                    # 赋值TMDB信息
                    meta_info.set_tmdb_info(file_media_info)
                # 自带TMDB信息
                else:
                    self.__record_anitopy_skip(file_name, reason="provided_tmdb")
                    if current_recorder() is not None:
                        current_recorder().action("provided_tmdb", input={"tmdb_info": tmdb_info},
                                                  output={"id": tmdb_info.get("id"),
                                                          "media_type": tmdb_info.get("media_type")})
                    meta_info = MetaInfo(title=file_name, mtype=media_type, include_ai=False)
                    meta_info.set_tmdb_info(tmdb_info)
                    if season and meta_info.type != MediaType.MOVIE:
                        meta_info.begin_season = int(season)
                    if episode_format:
                        begin_ep, end_ep = episode_format.split_episode(file_name)
                        if begin_ep is not None:
                            meta_info.begin_episode = begin_ep
                        if end_ep is not None:
                            meta_info.end_episode = end_ep
                    # 加入缓存
                    self.save_rename_cache(file_name, tmdb_info)
                # 按文件路程存储
                return_media_infos[file_path] = meta_info
            except Exception as err:
                print(str(err))
                log.error("【Rmt】发生错误：%s - %s" % (str(err), traceback.format_exc()))
                recorder = current_recorder()
                if recorder is not None:
                    recorder.context["decision_reason"] = "file_resolution_error"
                    recorder.action("file_resolution_error", status="error",
                                    input={"file_path": file_path},
                                    output={"error": str(err)},
                                    reason="file_resolution_error")
        # 循环结束
        return return_media_infos

    def search_media_info_force(self, meta_info):
        """Retry TMDB lookup and keep the retry trace as a recognition request."""
        if not meta_info:
            return None
        title = getattr(meta_info, "org_string", None) or meta_info.get_name()
        parsed_name = meta_info.get_name()
        recognition_provider = getattr(meta_info, "recognition_source", None) or "local_rules"
        if recognition_provider == "ai":
            recognition_provider = "anitopy_ml"
        caller_module = sys._getframe(1).f_globals.get("__name__", "")
        source = _infer_recognition_source(caller_module)
        if source == "unknown":
            source = "media.search_media_info_force"
        with recognition_scope(
                title, source=source, stage="resolve",
                context={"parsed_name": parsed_name, "year": meta_info.year,
                         "mtype": getattr(meta_info.type, "value", meta_info.type),
                         "season": meta_info.begin_season, "forced_retry": True,
                         "parent_request_id": getattr(meta_info, "recognition_request_id", None),
                         "caller_module": caller_module}) as recorder:
            self.__start_recognition_deadline(recorder)
            recorder.context["active_provider_id"] = recognition_provider
            recorder.action("forced_search", status="running",
                            input={"title": title, "parsed_name": parsed_name})
            result = self._search_media_info_force_impl(meta_info)
            tmdb_result = self.__json_safe(result) if result else None
            timed_out = (recorder.deadline_monotonic is not None
                         and time.monotonic() >= recorder.deadline_monotonic)
            recorder.set_overall(
                status="failed" if timed_out else ("success" if result else "failed"),
                reason=("recognition_deadline_exceeded" if timed_out else
                        (None if result else
                         recorder.context.get("decision_reason") or
                         self.__recognition_tmdb_failure_reason(recorder) or
                         "forced_tmdb_no_result")),
                parsed_result=self.__meta_snapshot(meta_info),
                selected_provider=recognition_provider,
                tmdb_result=tmdb_result,
            )
            return result

    def _search_media_info_force_impl(self, meta_info):
        """
        强制重新搜索媒体信息，跳过缓存，针对网络问题或API限流进行重试
        :param meta_info: 已解析但识别失败的 MetaInfo 对象
        :return: TMDB info dict 或 None
        """
        name = meta_info.get_name()
        year = meta_info.year
        mtype = meta_info.type
        season = meta_info.begin_season
        if not name:
            return None

        # 清除缓存，强制重新搜索
        cache_key = self.__make_cache_key(meta_info)
        if cache_key:
            self.meta.delete_meta_data(cache_key)

        file_media_info = None

        # 策略1：立即重试（解决网络抖动问题）
        file_media_info = self.__search_tmdb(
            file_media_name=name,
            first_media_year=year,
            search_type=mtype,
            media_year=year,
            season_number=season
        )
        recorder = current_recorder()
        if recorder is not None and recorder.context.get("decision_reason") == "ambiguous_tmdb":
            return None

        # 策略2：等待2秒后重试（解决API限流问题）
        if not file_media_info:
            remaining = recognition_remaining_seconds()
            # Preserve the legacy delayed retry for normal calls. Under a
            # resolve deadline, skip it unless enough time remains for both
            # the delay and at least a minimal follow-up request.
            if remaining is None or remaining > 2:
                time.sleep(2)
            else:
                return None
            file_media_info = self.__search_tmdb(
                file_media_name=name,
                first_media_year=year,
                search_type=mtype,
                media_year=year,
                season_number=season
            )

        # 策略3：去掉年份再查一次（解决年份信息有误的问题，仅在严格模式下有意义）
        if not file_media_info and self._rmt_match_mode == MatchMode.STRICT:
            file_media_info = self.__search_tmdb(
                file_media_name=name,
                search_type=mtype
            )

        # 补全TMDB信息
        if file_media_info and not file_media_info.get("genres"):
            file_media_info = self.get_tmdb_info(
                mtype=file_media_info.get("media_type"),
                tmdbid=file_media_info.get("id")
            )

        # 缓存结果
        self.__insert_media_cache(cache_key, file_media_info)

        if file_media_info:
            log.info("【Meta】额外重试成功，识别到媒体信息")

        return file_media_info

    @staticmethod
    def __dict_tmdbinfos(infos, mtype=None):
        """
        TMDB电影信息转为字典
        """
        if not infos:
            return []
        ret_infos = []
        for info in infos:
            tmdbid = info.get("id")
            vote = round(float(info.get("vote_average")), 1) if info.get("vote_average") else 0
            image = TMDB_IMAGE_W500_URL % info.get("poster_path")
            overview = info.get("overview")
            if mtype:
                media_type = mtype.value
                year = info.get("release_date")[0:4] if info.get(
                    "release_date") and mtype == MediaType.MOVIE else info.get(
                    "first_air_date")[0:4] if info.get(
                    "first_air_date") else ""
                typestr = 'MOV' if mtype == MediaType.MOVIE else 'TV'
                title = info.get("title") if mtype == MediaType.MOVIE else info.get("name")
            else:
                media_type = MediaType.MOVIE.value if info.get(
                    "media_type") == "movie" else MediaType.TV.value
                year = info.get("release_date")[0:4] if info.get(
                    "release_date") and info.get(
                    "media_type") == "movie" else info.get(
                    "first_air_date")[0:4] if info.get(
                    "first_air_date") else ""
                typestr = 'MOV' if info.get("media_type") == "movie" else 'TV'
                title = info.get("title") if info.get("media_type") == "movie" else info.get("name")

            ret_infos.append({
                'id': tmdbid,
                'orgid': tmdbid,
                'tmdbid': tmdbid,
                'title': title,
                'type': typestr,
                'media_type': media_type,
                'year': year,
                'vote': vote,
                'image': image,
                'overview': overview
            })

        return ret_infos

    def get_tmdb_hot_movies(self, page):
        """
        获取热门电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbHotMoviesCache
        cache_key = f"hot_movies:{page}"
        cached = TmdbHotMoviesCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.movie.popular(page), MediaType.MOVIE)
        TmdbHotMoviesCache.set(cache_key, result)
        return result

    def get_tmdb_hot_tvs(self, page):
        """
        获取热门电视剧
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.tv:
            return []
        from app.utils import TmdbHotTvsCache
        cache_key = f"hot_tvs:{page}"
        cached = TmdbHotTvsCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.tv.popular(page), MediaType.TV)
        TmdbHotTvsCache.set(cache_key, result)
        return result

    def get_tmdb_new_movies(self, page):
        """
        获取最新电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbNewMoviesCache
        cache_key = f"new_movies:{page}"
        cached = TmdbNewMoviesCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.movie.now_playing(page), MediaType.MOVIE)
        TmdbNewMoviesCache.set(cache_key, result)
        return result

    def get_tmdb_new_tvs(self, page):
        """
        获取最新电视剧
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.tv:
            return []
        from app.utils import TmdbNewTvsCache
        cache_key = f"new_tvs:{page}"
        cached = TmdbNewTvsCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.tv.on_the_air(page), MediaType.TV)
        TmdbNewTvsCache.set(cache_key, result)
        return result

    def get_tmdb_upcoming_movies(self, page):
        """
        获取即将上映电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbUpcomingMoviesCache
        cache_key = f"upcoming_movies:{page}"
        cached = TmdbUpcomingMoviesCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.movie.upcoming(page), MediaType.MOVIE)
        TmdbUpcomingMoviesCache.set(cache_key, result)
        return result

    def get_tmdb_trending_all_week(self, page=1):
        """
        获取即将上映电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbTrendingCache
        cache_key = f"trending_week:{page}"
        cached = TmdbTrendingCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.trending.all_week(page=page))
        TmdbTrendingCache.set(cache_key, result)
        return result

    def __get_tmdb_movie_detail(self, tmdbid, append_to_response=None):
        """
        获取电影的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/r9PkFnRUIthgBp2JZZzD380MWZy.jpg",
          "belongs_to_collection": {
            "id": 94602,
            "name": "穿靴子的猫（系列）",
            "poster_path": "/anHwj9IupRoRZZ98WTBvHpTiE6A.jpg",
            "backdrop_path": "/feU1DWV5zMWxXUHJyAIk3dHRQ9c.jpg"
          },
          "budget": 90000000,
          "genres": [
            {
              "id": 16,
              "name": "动画"
            },
            {
              "id": 28,
              "name": "动作"
            },
            {
              "id": 12,
              "name": "冒险"
            },
            {
              "id": 35,
              "name": "喜剧"
            },
            {
              "id": 10751,
              "name": "家庭"
            },
            {
              "id": 14,
              "name": "奇幻"
            }
          ],
          "homepage": "",
          "id": 315162,
          "imdb_id": "tt3915174",
          "original_language": "en",
          "original_title": "Puss in Boots: The Last Wish",
          "overview": "时隔11年，臭屁自大又爱卖萌的猫大侠回来了！如今的猫大侠（安东尼奥·班德拉斯 配音），依旧幽默潇洒又不拘小节、数次“花式送命”后，九条命如今只剩一条，于是不得不请求自己的老搭档兼“宿敌”——迷人的软爪妞（萨尔玛·海耶克 配音）来施以援手来恢复自己的九条生命。",
          "popularity": 8842.129,
          "poster_path": "/rnn30OlNPiC3IOoWHKoKARGsBRK.jpg",
          "production_companies": [
            {
              "id": 33,
              "logo_path": "/8lvHyhjr8oUKOOy2dKXoALWKdp0.png",
              "name": "Universal Pictures",
              "origin_country": "US"
            },
            {
              "id": 521,
              "logo_path": "/kP7t6RwGz2AvvTkvnI1uteEwHet.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "release_date": "2022-12-07",
          "revenue": 260725470,
          "runtime": 102,
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            },
            {
              "english_name": "Spanish",
              "iso_639_1": "es",
              "name": "Español"
            }
          ],
          "status": "Released",
          "tagline": "",
          "title": "穿靴子的猫2",
          "video": false,
          "vote_average": 8.614,
          "vote_count": 2291
        }
        """
        if not self.movie:
            return {}
        try:
            log.info("【Meta】正在查询TMDB电影：%s ..." % tmdbid)
            tmdbinfo = self.movie.details(tmdbid, append_to_response)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def __get_tmdb_tv_detail(self, tmdbid, append_to_response=None):
        """
        获取电视剧的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/uDgy6hyPd82kOHh6I95FLtLnj6p.jpg",
          "created_by": [
            {
              "id": 35796,
              "credit_id": "5e84f06a3344c600153f6a57",
              "name": "Craig Mazin",
              "gender": 2,
              "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
            },
            {
              "id": 1295692,
              "credit_id": "5e84f03598f1f10016a985c0",
              "name": "Neil Druckmann",
              "gender": 2,
              "profile_path": "/bVUsM4aYiHbeSYE1xAw2H5Z1ANU.jpg"
            }
          ],
          "episode_run_time": [],
          "first_air_date": "2023-01-15",
          "genres": [
            {
              "id": 18,
              "name": "剧情"
            },
            {
              "id": 10765,
              "name": "Sci-Fi & Fantasy"
            },
            {
              "id": 10759,
              "name": "动作冒险"
            }
          ],
          "homepage": "https://www.hbo.com/the-last-of-us",
          "id": 100088,
          "in_production": true,
          "languages": [
            "en"
          ],
          "last_air_date": "2023-01-15",
          "last_episode_to_air": {
            "air_date": "2023-01-15",
            "episode_number": 1,
            "id": 2181581,
            "name": "当你迷失在黑暗中",
            "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
            "production_code": "",
            "runtime": 81,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
            "vote_average": 8,
            "vote_count": 33
          },
          "name": "最后生还者",
          "next_episode_to_air": {
            "air_date": "2023-01-22",
            "episode_number": 2,
            "id": 4071039,
            "name": "虫草变异菌",
            "overview": "",
            "production_code": "",
            "runtime": 55,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/jkUtYTmeap6EvkHI4n0j5IRFrIr.jpg",
            "vote_average": 10,
            "vote_count": 1
          },
          "networks": [
            {
              "id": 49,
              "name": "HBO",
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "origin_country": "US"
            }
          ],
          "number_of_episodes": 9,
          "number_of_seasons": 1,
          "origin_country": [
            "US"
          ],
          "original_language": "en",
          "original_name": "The Last of Us",
          "overview": "不明真菌疫情肆虐之后的美国，被真菌感染的人都变成了可怕的怪物，乔尔（Joel）为了换回武器答应将小女孩儿艾莉（Ellie）送到指定地点，由此开始了两人穿越美国的漫漫旅程。",
          "popularity": 5585.639,
          "poster_path": "/nOY3VBFO0VnlN9nlRombnMTztyh.jpg",
          "production_companies": [
            {
              "id": 3268,
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "name": "HBO",
              "origin_country": "US"
            },
            {
              "id": 11073,
              "logo_path": "/aCbASRcI1MI7DXjPbSW9Fcv9uGR.png",
              "name": "Sony Pictures Television Studios",
              "origin_country": "US"
            },
            {
              "id": 23217,
              "logo_path": "/kXBZdQigEf6QiTLzo6TFLAa7jKD.png",
              "name": "Naughty Dog",
              "origin_country": "US"
            },
            {
              "id": 115241,
              "logo_path": null,
              "name": "The Mighty Mint",
              "origin_country": "US"
            },
            {
              "id": 119645,
              "logo_path": null,
              "name": "Word Games",
              "origin_country": "US"
            },
            {
              "id": 125281,
              "logo_path": "/3hV8pyxzAJgEjiSYVv1WZ0ZYayp.png",
              "name": "PlayStation Productions",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "seasons": [
            {
              "air_date": "2023-01-15",
              "episode_count": 9,
              "id": 144593,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
              "season_number": 1
            }
          ],
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            }
          ],
          "status": "Returning Series",
          "tagline": "",
          "type": "Scripted",
          "vote_average": 8.924,
          "vote_count": 601
        }
        """
        if not self.tv:
            return {}
        try:
            log.info("【Meta】正在查询TMDB电视剧：%s ..." % tmdbid)
            tmdbinfo = self.tv.details(tmdbid, append_to_response)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def get_tmdb_tv_season_detail(self, tmdbid, season: int):
        """
        获取电视剧季的详情
        :param tmdbid: TMDB ID
        :param season: 季，数字
        :return: TMDB信息
        """
        if not self.tv:
            return {}
        from app.utils import TmdbSeasonDetailCache
        cache_key = f"season_detail:{tmdbid}:{season}"
        cached = TmdbSeasonDetailCache.get(cache_key)
        if cached is not None:
            return cached
        try:
            log.info("【Meta】正在查询TMDB电视剧：%s，季：%s ..." % (tmdbid, season))
            tmdbinfo = self.tv.season_details(tmdbid, season)
            if tmdbinfo:
                TmdbSeasonDetailCache.set(cache_key, tmdbinfo)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return {}

    def get_tmdb_tv_seasons_byid(self, tmdbid):
        """
        根据TMDB查询TMDB电视剧的所有季
        """
        if not tmdbid:
            return []
        return self.get_tmdb_tv_seasons(
            tv_info=self.__get_tmdb_tv_detail(
                tmdbid=tmdbid
            )
        )

    @staticmethod
    def get_tmdb_tv_seasons(tv_info):
        """
        查询TMDB电视剧的所有季
        :param tv_info: TMDB 的季信息
        :return: 带有season_number、episode_count 的每季总集数的字典列表
        """
        """
        "seasons": [
            {
              "air_date": "2006-01-08",
              "episode_count": 11,
              "id": 3722,
              "name": "特别篇",
              "overview": "",
              "poster_path": "/snQYndfsEr3Sto2jOmkmsQuUXAQ.jpg",
              "season_number": 0
            },
            {
              "air_date": "2005-03-27",
              "episode_count": 9,
              "id": 3718,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/foM4ImvUXPrD2NvtkHyixq5vhPx.jpg",
              "season_number": 1
            }
        ]
        """
        if not tv_info:
            return []
        return tv_info.get("seasons") or []

    def get_tmdb_season_episodes(self, tmdbid, season: int):
        """
        :param: tmdbid: TMDB ID
        :param: season: 季号
        """
        """
        从TMDB的季集信息中获得某季的集信息
        """
        """
        "episodes": [
            {
              "air_date": "2023-01-15",
              "episode_number": 1,
              "id": 2181581,
              "name": "当你迷失在黑暗中",
              "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
              "production_code": "",
              "runtime": 81,
              "season_number": 1,
              "show_id": 100088,
              "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
              "vote_average": 8,
              "vote_count": 33
            },
          ]
        """
        if not tmdbid:
            return []
        season_info = self.get_tmdb_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info:
            return []
        return season_info.get("episodes") or []

    @staticmethod
    def get_tmdb_backdrops(tmdbinfo):
        """
        获取TMDB的背景图
        """
        """
        {
          "backdrops": [
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/qUroDlCDUMwRWbkyjZGB9THkMgZ.jpg",
              "vote_average": 5.312,
              "vote_count": 1,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/iyxvxEQIfQjzJJTfszZxmH5UV35.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 720,
              "iso_639_1": "en",
              "file_path": "/8SRY6IcMKO1E5p83w7bjvcqklp9.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1280
            },
            {
              "aspect_ratio": 1.778,
              "height": 1080,
              "iso_639_1": "en",
              "file_path": "/erkJ7OxJWFdLBOcn2MvIdhTLHTu.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1920
            }
          ]
        }
        """
        if not tmdbinfo:
            return []
        backdrops = tmdbinfo.get("images", {}).get("backdrops") or []
        result = [TMDB_IMAGE_ORIGINAL_URL % backdrop.get("file_path") for backdrop in backdrops]
        result.append(TMDB_IMAGE_ORIGINAL_URL % tmdbinfo.get("backdrop_path"))
        return result

    @staticmethod
    def get_tmdb_season_episodes_num(tv_info, season: int):
        """
        从TMDB的季信息中获得具体季有多少集
        :param season: 季号，数字
        :param tv_info: 已获取的TMDB季的信息
        :return: 该季的总集数
        """
        if not tv_info:
            return 0
        seasons = tv_info.get("seasons")
        if not seasons:
            return 0
        for sea in seasons:
            if sea.get("season_number") == int(season):
                return int(sea.get("episode_count"))
        return 0

    @staticmethod
    def __dict_media_crews(crews):
        """
        字典化媒体工作人员
        """
        return [{
            "id": crew.get("id"),
            "gender": crew.get("gender"),
            "known_for_department": crew.get("known_for_department"),
            "name": crew.get("name"),
            "original_name": crew.get("original_name"),
            "popularity": crew.get("popularity"),
            "image": TMDB_IMAGE_FACE_URL % crew.get("profile_path"),
            "credit_id": crew.get("credit_id"),
            "department": crew.get("department"),
            "job": crew.get("job"),
            "profile": TMDB_PEOPLE_PROFILE_URL % crew.get('id')
        } for crew in crews or []]

    @staticmethod
    def __dict_media_casts(casts):
        """
        字典化媒体演职人员
        """
        return [{
            "id": cast.get("id"),
            "gender": cast.get("gender"),
            "known_for_department": cast.get("known_for_department"),
            "name": cast.get("name"),
            "original_name": cast.get("original_name"),
            "popularity": cast.get("popularity"),
            "image": TMDB_IMAGE_FACE_URL % cast.get("profile_path"),
            "cast_id": cast.get("cast_id"),
            "role": cast.get("character"),
            "credit_id": cast.get("credit_id"),
            "order": cast.get("order"),
            "profile": TMDB_PEOPLE_PROFILE_URL % cast.get('id')
        } for cast in casts or []]

    def get_tmdb_directors_actors(self, tmdbinfo):
        """
        查询导演和演员
        :param tmdbinfo: TMDB元数据
        :return: 导演列表，演员列表
        """
        """
        "cast": [
          {
            "adult": false,
            "gender": 2,
            "id": 3131,
            "known_for_department": "Acting",
            "name": "Antonio Banderas",
            "original_name": "Antonio Banderas",
            "popularity": 60.896,
            "profile_path": "/iWIUEwgn2KW50MssR7tdPeFoRGW.jpg",
            "cast_id": 2,
            "character": "Puss in Boots (voice)",
            "credit_id": "6052480e197de4006bb47b9a",
            "order": 0
          }
        ],
        "crew": [
          {
            "adult": false,
            "gender": 2,
            "id": 5524,
            "known_for_department": "Production",
            "name": "Andrew Adamson",
            "original_name": "Andrew Adamson",
            "popularity": 9.322,
            "profile_path": "/qqIAVKAe5LHRbPyZUlptsqlo4Kb.jpg",
            "credit_id": "63b86b2224b33300a0585bf1",
            "department": "Production",
            "job": "Executive Producer"
          }
        ]
        """
        if not tmdbinfo:
            return [], []
        _credits = tmdbinfo.get("credits")
        if not _credits:
            return [], []
        directors = []
        actors = []
        for cast in self.__dict_media_casts(_credits.get("cast")):
            if cast.get("known_for_department") == "Acting":
                actors.append(cast)
        for crew in self.__dict_media_crews(_credits.get("crew")):
            if crew.get("job") == "Director":
                directors.append(crew)
        return directors, actors

    def get_tmdb_cats(self, mtype, tmdbid):
        """
        获取TMDB的演员列表
        :param: mtype: 媒体类型
        :param: tmdbid: TMDBID
        """
        try:
            if mtype == MediaType.MOVIE:
                if not self.movie:
                    return []
                return self.__dict_media_casts(self.movie.credits(tmdbid).get("cast"))
            else:
                if not self.tv:
                    return []
                return self.__dict_media_casts(self.tv.credits(tmdbid).get("cast"))
        except Exception as err:
            print(str(err))
        return []

    @staticmethod
    def get_tmdb_genres_names(tmdbinfo):
        """
        从TMDB数据中获取风格名称
        """
        """
        "genres": [
            {
              "id": 16,
              "name": "动画"
            },
            {
              "id": 28,
              "name": "动作"
            },
            {
              "id": 12,
              "name": "冒险"
            },
            {
              "id": 35,
              "name": "喜剧"
            },
            {
              "id": 10751,
              "name": "家庭"
            },
            {
              "id": 14,
              "name": "奇幻"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        genres = tmdbinfo.get("genres") or []
        genres_list = [genre.get("name") for genre in genres]
        return ", ".join(genres_list) if genres_list else ""

    def get_tmdb_genres(self, mtype):
        """
        获取TMDB的风格列表
        :param: mtype: 媒体类型
        """
        if not self.genre:
            return []
        try:
            if mtype == MediaType.MOVIE:
                return self.genre.movie_list()
            else:
                return self.genre.tv_list()
        except Exception as err:
            print(str(err))
        return []

    @staticmethod
    def get_get_production_country_names(tmdbinfo):
        """
        从TMDB数据中获取制片国家名称
        """
        """
        "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "美国"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        countries = tmdbinfo.get("production_countries") or []
        countries_list = [country.get("name") for country in countries]
        return ", ".join(countries_list) if countries_list else ""

    @staticmethod
    def get_tmdb_production_company_names(tmdbinfo):
        """
        从TMDB数据中获取制片公司名称
        """
        """
        "production_companies": [
            {
              "id": 2,
              "logo_path": "/wdrCwmRnLFJhEoH8GSfymY85KHT.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        companies = tmdbinfo.get("production_companies") or []
        companies_list = [company.get("name") for company in companies]
        return ", ".join(companies_list) if companies_list else ""

    @staticmethod
    def get_tmdb_crews(tmdbinfo, nums=None):
        """
        从TMDB数据中获取制片人员
        """
        if not tmdbinfo:
            return ""
        crews = tmdbinfo.get("credits", {}).get("crew") or []
        result = [{crew.get("name"): crew.get("job")} for crew in crews]
        if nums:
            return result[:nums]
        else:
            return result

    def get_tmdb_en_title(self, media_info):
        """
        获取TMDB的英文名称
        """
        from app.utils import TmdbEnTitleCache
        cache_key = f"en_title:{media_info.type.value}:{media_info.tmdb_id}"
        cached = TmdbEnTitleCache.get(cache_key)
        if cached is not None:
            return cached
        en_info = self.get_tmdb_info(mtype=media_info.type,
                                     tmdbid=media_info.tmdb_id,
                                     language="en-US")
        if en_info:
            title = en_info.get("title") if media_info.type == MediaType.MOVIE else en_info.get("name")
            TmdbEnTitleCache.set(cache_key, title)
            return title
        return None

    def get_episode_title(self, media_info):
        """
        获取剧集的标题
        """
        if media_info.type == MediaType.MOVIE:
            return None
        if media_info.tmdb_id:
            if not media_info.begin_episode:
                return None
            episodes = self.get_tmdb_season_episodes(tmdbid=media_info.tmdb_id,
                                                     season=int(media_info.get_season_seq()))
            for episode in episodes:
                if episode.get("episode_number") == media_info.begin_episode:
                    return episode.get("name")
        return None

    def get_movie_discover(self, page=1):
        """
        发现电影
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.discover(page)
            if movies:
                return movies.get("results")
        except Exception as e:
            print(str(e))
        return []

    def get_movie_similar(self, tmdbid, page=1):
        """
        查询类似电影
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.similar(movie_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(movies, MediaType.MOVIE)
        except Exception as e:
            print(str(e))
            return []

    def get_movie_recommendations(self, tmdbid, page=1):
        """
        查询电影关联推荐
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.recommendations(movie_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(movies, MediaType.MOVIE)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_similar(self, tmdbid, page=1):
        """
        查询类似电视剧
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.similar(tv_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(tvs, MediaType.TV)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_recommendations(self, tmdbid, page=1):
        """
        查询电视剧关联推荐
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.recommendations(tv_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(tvs, MediaType.TV)
        except Exception as e:
            print(str(e))
            return []

    def get_tmdb_discover(self, mtype, params=None, page=1):
        """
        浏览电影、电视剧（复杂过滤条件）
        """
        if not self.discover:
            return []
        try:
            if mtype == MediaType.MOVIE:
                movies = self.discover.discover_movies(params=params, page=page)
                return self.__dict_tmdbinfos(movies, mtype)
            elif mtype == MediaType.TV:
                tvs = self.discover.discover_tv_shows(params=params, page=page)
                return self.__dict_tmdbinfos(tvs, mtype)
        except Exception as e:
            print(str(e))
        return []

    def get_person_medias(self, personid, mtype, page=1):
        """
        查询人物相关影视作品
        """
        if not self.person:
            return []
        result = []
        try:
            if mtype == MediaType.MOVIE:
                movies = self.person.movie_credits(person_id=personid) or []
                result = self.__dict_tmdbinfos(movies, mtype)
            elif mtype == MediaType.TV:
                tvs = self.person.tv_credits(person_id=personid) or []
                result = self.__dict_tmdbinfos(tvs, mtype)
            return result[(page - 1) * 20: page * 20]
        except Exception as e:
            print(str(e))
        return []

    @staticmethod
    def __get_genre_ids_from_detail(genres):
        """
        从TMDB详情中获取genre_id列表
        """
        if not genres:
            return []
        genre_ids = []
        for genre in genres:
            genre_ids.append(genre.get('id'))
        return genre_ids

    @staticmethod
    def __get_tmdb_chinese_title(tmdbinfo):
        """
        从别名中获取中文标题
        """
        if not tmdbinfo:
            return None
        if tmdbinfo.get("media_type") == MediaType.MOVIE:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("titles", [])
        else:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("results", [])
        for alternative_title in alternative_titles:
            iso_3166_1 = alternative_title.get("iso_3166_1")
            if iso_3166_1 == "CN":
                title = alternative_title.get("title")
                if title and StringUtils.is_chinese(title) and zhconv.convert(title, "zh-hans") == title:
                    return title
        return tmdbinfo.get("title") if tmdbinfo.get("media_type") == MediaType.MOVIE else tmdbinfo.get("name")

    def get_tmdbperson_chinese_name(self, person_id):
        """
        查询TMDB人物中文名称
        """
        if not self.person:
            return ""
        alter_names = []
        name = ""
        try:
            aka_names = self.person.details(person_id).get("also_known_as", []) or []
        except Exception as err:
            print(str(err))
            return ""
        for aka_name in aka_names:
            if StringUtils.is_chinese(aka_name):
                alter_names.append(aka_name)
        if len(alter_names) == 1:
            name = alter_names[0]
        elif len(alter_names) > 1:
            for alter_name in alter_names:
                if alter_name == zhconv.convert(alter_name, 'zh-hans'):
                    name = alter_name
        return name

    def get_tmdbperson_aka_names(self, person_id):
        """
        查询人物又名
        """
        if not self.person:
            return []
        try:
            aka_names = self.person.details(person_id).get("also_known_as", []) or []
            return aka_names
        except Exception as err:
            print(str(err))
            return []

    def get_random_discover_backdrop(self):
        """
        获取TMDB热门电影随机一张背景图
        """
        movies = self.get_movie_discover()
        if movies:
            backdrops = [movie.get("backdrop_path") for movie in movies]
            return TMDB_IMAGE_ORIGINAL_URL % backdrops[round(random.uniform(0, len(backdrops) - 1))]
        return ""

    def save_rename_cache(self, file_name, cache_info):
        """
        将手动识别的信息加入缓存
        """
        if not file_name or not cache_info:
            return
        meta_info = MetaInfo(title=file_name, include_ai=False, record=False)
        self.__insert_media_cache(self.__make_cache_key(meta_info), cache_info)

    @staticmethod
    def merge_media_info(target, source):
        """
        将soruce中有效的信息合并到target中并返回
        """
        target.set_tmdb_info(source.tmdb_info)
        target.fanart_poster = source.get_poster_image()
        target.fanart_backdrop = source.get_backdrop_image()
        target.set_download_info(download_setting=source.download_setting,
                                 save_path=source.save_path)
        return target

    def get_tmdbid_by_imdbid(self, imdbid):
        """
        根据IMDBID查询TMDB信息
        """
        if not self.find:
            return None
        try:
            result = self.find.find_by_imdbid(imdbid) or {}
            tmdbinfo = result.get('movie_results') or result.get("tv_results")
            if tmdbinfo:
                tmdbinfo = tmdbinfo[0]
                return tmdbinfo.get("id")
        except Exception as err:
            print(str(err))
        return None

    @staticmethod
    def get_detail_url(mtype, tmdbid):
        """
        获取TMDB/豆瓣详情页地址
        """
        if not tmdbid:
            return ""
        if str(tmdbid).startswith("DB:"):
            return "https://movie.douban.com/subject/%s" % str(tmdbid).replace("DB:", "")
        elif mtype == MediaType.MOVIE:
            return "https://www.themoviedb.org/movie/%s" % tmdbid
        else:
            return "https://www.themoviedb.org/tv/%s" % tmdbid

    def get_episode_images(self, tv_id, season_id, episode_id, orginal=False):
        """
        获取剧集中某一集封面
        """
        if not self.episode:
            return ""
        res = self.episode.images(tv_id, season_id, episode_id)
        if res:
            if orginal:
                return TMDB_IMAGE_ORIGINAL_URL % res[0].get("file_path")
            else:
                return TMDB_IMAGE_W500_URL % res[0].get("file_path")
        else:
            return ""

    def get_tmdb_factinfo(self, media_info):
        """
        获取TMDB发布信息
        """
        result = []
        if media_info.vote_average:
            result.append({"评分": media_info.vote_average})
        if media_info.original_title:
            result.append({"原始标题": media_info.original_title})
        status = media_info.tmdb_info.get("status")
        if status:
            result.append({"状态": status})
        if media_info.release_date:
            result.append({"上映日期": media_info.release_date})
        revenue = media_info.tmdb_info.get("revenue")
        if revenue:
            result.append({"收入": StringUtils.str_amount(revenue)})
        budget = media_info.tmdb_info.get("budget")
        if budget:
            result.append({"成本": StringUtils.str_amount(budget)})
        if media_info.original_language:
            result.append({"原始语言": media_info.original_language})
        production_country = self.get_get_production_country_names(tmdbinfo=media_info.tmdb_info)
        if production_country:
            result.append({"出品国家": production_country})
        production_company = self.get_tmdb_production_company_names(tmdbinfo=media_info.tmdb_info)
        if production_company:
            result.append({"制作公司": production_company})

        return result
