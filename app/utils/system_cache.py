"""系统缓存管理入口，仅管理可重建的数据。"""

import time

from app.utils import cache_manager
from app.utils.cache_memory import cache_memory_info
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
    for item in result:
        if item.get("name") == "wallpaper":
            item["max_entries"] = 1
        else:
            item["max_entries"] = None
        item.setdefault("max_bytes", None)
    return result


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
        if not isinstance(name, str) or name not in valid | {"all"}:
            raise ValueError("缓存类型无效")
        names = sorted(valid) if name == "all" else [name]
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
