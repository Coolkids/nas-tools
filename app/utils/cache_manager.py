# -*- coding: utf-8 -*-
import time

from cacheout import CacheManager, LRUCache, Cache

CACHES = {
    "tmdb_supply": {'maxsize': 2000}
}

cacheman = CacheManager(CACHES, cache_class=LRUCache)

# T12: TMDB网站搜索结果缓存 86400s (1天)
TmdbWebSearchCache = Cache(maxsize=1024, ttl=86400, timer=time.time, default=None)

TokenCache = Cache(maxsize=2048, ttl=4*3600, timer=time.time, default=None)

ConfigLoadCache = Cache(maxsize=1, ttl=10, timer=time.time, default=None)

# T2: Torznab搜索结果缓存 300s
TorznabCache = Cache(maxsize=2000, ttl=300, timer=time.time, default=None)

# T3: Web后端媒体搜索缓存 300s
WebSearchCache = Cache(maxsize=2000, ttl=300, timer=time.time, default=None)

# T3: Web后端媒体详情缓存 300s
WebMediaInfoCache = Cache(maxsize=2000, ttl=300, timer=time.time, default=None)

# T4: 站点数据缓存 300s
SiteDataCache = Cache(maxsize=2000, ttl=300, timer=time.time, default=None)

# T6: TMDB英文标题缓存 43200s (12小时)
TmdbEnTitleCache = Cache(maxsize=5000, ttl=43200, timer=time.time, default=None)

# T7: TMDB热门/最新/即将上映/趋势缓存 43200s (12小时)
TmdbHotMoviesCache = Cache(maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbHotTvsCache = Cache(maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbNewMoviesCache = Cache(maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbNewTvsCache = Cache(maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbUpcomingMoviesCache = Cache(maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbTrendingCache = Cache(maxsize=1000, ttl=43200, timer=time.time, default=None)

# T8: 内置索引器搜索结果缓存 300s
BuiltinSearchCache = Cache(maxsize=2000, ttl=300, timer=time.time, default=None)

# T11: TMDB季详情缓存 3600s (1小时)
TmdbSeasonDetailCache = Cache(maxsize=2000, ttl=3600, timer=time.time, default=None)

# 站点页面HTML缓存 300s (替代sites.py __get_site_page_html的lru_cache)
SitePageHtmlCache = Cache(maxsize=2000, ttl=300, timer=time.time, default=None)

# 字幕搜索结果缓存 3600s (替代opensubtitles.py __parse_opensubtitles_results的lru_cache)
SubtitleCache = Cache(maxsize=1000, ttl=3600, timer=time.time, default=None)
