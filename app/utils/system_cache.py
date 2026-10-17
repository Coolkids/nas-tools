"""系统缓存管理入口，仅管理可重建的数据。"""

import math
import time
from copy import deepcopy

from app.utils import cache_manager
from app.utils.cache_memory import cache_memory_info
from app.utils.cache_policy import CACHE_GROUPS, MAX_TTL_SECONDS, cache_category
from app.utils.persistent_cache import FUNCTION_CACHES, STORE
from app.utils.storage_jobs import STORAGE_JOBS


LABELS = {
    "tmdb_supply": "TMDB 补充信息", "TmdbWebSearchCache": "TMDB 网站搜索",
    "TokenCache": "API 认证令牌", "ConfigLoadCache": "配置加载",
    "TorznabCache": "Torznab 搜索", "WebSearchCache": "媒体搜索",
    "WebMediaInfoCache": "媒体详情", "TmdbEnTitleCache": "TMDB 英文标题",
    "TmdbHotMoviesCache": "TMDB 热门电影", "TmdbHotTvsCache": "TMDB 热门电视剧",
    "TmdbNewMoviesCache": "TMDB 最新电影", "TmdbNewTvsCache": "TMDB 最新电视剧",
    "TmdbUpcomingMoviesCache": "TMDB 即将上映", "TmdbTrendingCache": "TMDB 趋势",
    "TmdbSeasonDetailCache": "TMDB 季详情", "SubtitleCache": "字幕搜索",
    "bangumi": "Bangumi 番剧", "douban_api": "豆瓣 API",
    "douban_detail": "豆瓣详情", "douban_user": "豆瓣用户信息",
    "fanart": "Fanart 图片", "web_request": "图片与网页请求",
    "wallpaper": "登录壁纸", "tmdb_request": "TMDB API 请求",
}
GROUP_LABELS = {"tmdb_discover": "TMDB 榜单"}


def _caches():
    # 确保还未使用的函数缓存也出现在管理列表中。
    from app.media import bangumi, fanart  # noqa: F401
    from app.media.doubanapi import apiv2, webapi  # noqa: F401
    from app.media.tmdbv3api.tmdb import TMDb
    from web.backend import wallpaper, web_utils  # noqa: F401

    caches = {name: getattr(cache_manager, name) for name in LABELS
              if hasattr(cache_manager, name)}
    caches.update(dict(cache_manager.cacheman))
    caches.update(FUNCTION_CACHES)
    caches["tmdb_request"] = TMDb._parsed_cache
    return caches


def cache_info():
    from app.helper.meta_helper import MetaHelper
    from app.media.recognition import cache

    result = []
    for name, instance in _caches().items():
        result.append({"name": name, "label": LABELS.get(name, name),
                       **cache_memory_info(instance), "max_entries": instance.maxsize,
                       "max_bytes": None, "ttl_seconds": instance.ttl,
                       "stats_at": time.time(), "stats_estimated": True})
    for namespace, label in [("parse", "AI 解析"), ("tmdb", "识别 TMDB 查询"),
                             ("decision", "识别名称证据")]:
        result.append({"name": f"recognition_{namespace}", "label": label,
                       **cache.info(namespace, include_memory=True),
                       "stats_at": time.time(), "stats_estimated": namespace == "parse"})
    result.append({"name": "tmdb_metadata", "label": "TMDB 媒体识别",
                   **MetaHelper().cache_info(include_memory=True),
                   "max_entries": None, "max_bytes": None,
                   "stats_at": time.time(), "stats_estimated": True})
    for group, members in CACHE_GROUPS.items():
        items = [item for item in result if item["name"] in members]
        if not items:
            continue
        combined = {**items[0], "name": group, "label": GROUP_LABELS[group],
                    "description": "热门电影/电视剧、最新电影/电视剧、即将上映、趋势",
                    "members": [item["name"] for item in items],
                    "entries": sum(item["entries"] for item in items),
                    "stats_at": max(item["stats_at"] for item in items)}
        for field in ("memory_bytes", "bytes"):
            values = [item.get(field) for item in items]
            combined[field] = sum(values) if all(isinstance(value, (int, float))
                                                for value in values) else None
        # 本地文件由多个命名空间共用，文件大小只计一次。
        sizes = [item["store_bytes"] for item in items
                 if isinstance(item.get("store_bytes"), (int, float))]
        combined["store_bytes"] = max(sizes) if sizes else None
        result = [item for item in result if item["name"] not in members]
        result.append(combined)
    for item in result:
        if item.get("name") == "wallpaper":
            item["max_entries"] = 1
        else:
            item["max_entries"] = None
        item.setdefault("max_bytes", None)
    return result


def update_cache_ttl(name, value, unit):
    """只更新有效期策略，不修改缓存连接和迁移配置。"""
    from config import CONFIG_LOCK, Config

    valid = set(_caches()) | set(CACHE_GROUPS) | {
        "recognition_parse", "recognition_tmdb", "recognition_decision", "tmdb_metadata"}
    if not isinstance(name, str) or name not in valid:
        raise ValueError("缓存类型无效")
    factors = {"minute": 60, "hour": 3600, "day": 86400}
    if not isinstance(unit, str) or unit not in {*factors, "forever"}:
        raise ValueError("有效期单位无效")
    if unit == "forever":
        seconds = 0
    else:
        try:
            duration = float(value) * factors[unit]
            if isinstance(value, bool) or not math.isfinite(duration) or not 1 <= duration <= MAX_TTL_SECONDS:
                raise ValueError
            seconds = round(duration)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("有效期必须大于 0，且不超过 36500 天") from None
    with STORAGE_JOBS.maintenance_lock(exclusive=False):
        if STORE.status().get("maintenance"):
            raise ValueError("缓存迁移期间不能修改有效期")
        with CONFIG_LOCK:
            config = deepcopy(Config().get_config())
            settings = config.get("cache") or {}
            policies = dict(settings.get("ttl_seconds") or {})
            policies[cache_category(name)] = seconds
            config["cache"] = {**settings, "ttl_seconds": policies}
            Config().save_config(config)
    return seconds


def backend_status():
    from app.utils.storage_jobs import STORAGE_JOBS

    status = STORE.status()
    tasks = STORAGE_JOBS.active("cache_migration")
    latest = STORAGE_JOBS.latest("cache_migration")
    current = next((job for job in tasks if job.get("status") not in {"CHECKING", "READY"}), None)
    if (current is None and latest and latest.get("stage") != "CHECK_FAILED"
            and latest.get("status") not in {"CHECKING", "READY"}):
        current = latest
    task_info = ({"job_id": current["job_id"], "status": current["status"],
                  "stage": current["stage"], "updated_at": current["updated_at"],
                  "error": current.get("error"),
                  "result": {key: (current.get("result") or {}).get(key)
                             for key in ("entries", "expired_during_migration",
                                         "wallpaper_entries_limited")
                             if key in (current.get("result") or {})}}
                 if current else None)
    status["current_task"] = task_info
    status["recent_task"] = task_info
    return status


def clear_cache(name):
    from app.helper.meta_helper import MetaHelper
    from app.media.recognition import cache

    with STORAGE_JOBS.maintenance_lock(exclusive=False):
        if STORE.status().get("maintenance"):
            raise ValueError("缓存迁移期间不能清理缓存")
        caches = _caches()
        valid = set(caches) | {"recognition_parse", "recognition_tmdb",
                               "recognition_decision", "tmdb_metadata"}
        if not isinstance(name, str) or name not in valid | set(CACHE_GROUPS) | {"all"}:
            raise ValueError("缓存类型无效")
        names = sorted(valid) if name == "all" else (
            [member for member in CACHE_GROUPS[name] if member in caches]
            if name in CACHE_GROUPS else [name])
        cleared = 0
        for target in names:
            if target.startswith("recognition_"):
                namespace = target.removeprefix("recognition_")
                cleared += cache.info(namespace)["entries"]
                cache.clear(namespace)
            elif target == "tmdb_metadata":
                helper = MetaHelper()
                cleared += helper.cache_info()["entries"]
                helper.clear_meta_data()
            else:
                instance = caches[target]
                cleared += instance.memory_info()["entries"]
                instance.clear()
        return cleared
