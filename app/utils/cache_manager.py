# -*- coding: utf-8 -*-
import time

from cacheout import CacheManager, Cache

from app.utils.persistent_cache import PersistentCache

CACHES = {
    "tmdb_supply": {'maxsize': 2000, 'ttl': 86400}
}

cacheman = CacheManager()
for name, options in CACHES.items():
    cacheman.register(name, PersistentCache(name, **options))

# T12: TMDB网站搜索结果缓存 86400s (1天)
TmdbWebSearchCache = PersistentCache('TmdbWebSearchCache', maxsize=1024, ttl=86400, timer=time.time, default=None)

TokenCache = Cache(maxsize=2048, ttl=4*3600, timer=time.time, default=None)

ConfigLoadCache = Cache(maxsize=1, ttl=10, timer=time.time, default=None)

# T2: Torznab搜索结果缓存 300s
TorznabCache = PersistentCache('TorznabCache', maxsize=2000, ttl=300, timer=time.time, default=None)

# T3: Web后端媒体搜索缓存 300s
WebSearchCache = PersistentCache('WebSearchCache', maxsize=2000, ttl=300, timer=time.time, default=None)

# T3: Web后端媒体详情缓存 300s
WebMediaInfoCache = PersistentCache('WebMediaInfoCache', maxsize=2000, ttl=300, timer=time.time, default=None)

# T6: TMDB英文标题缓存 43200s (12小时)
TmdbEnTitleCache = PersistentCache('TmdbEnTitleCache', maxsize=5000, ttl=43200, timer=time.time, default=None)

# T7: TMDB热门/最新/即将上映/趋势缓存 43200s (12小时)
TmdbHotMoviesCache = PersistentCache('TmdbHotMoviesCache', maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbHotTvsCache = PersistentCache('TmdbHotTvsCache', maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbNewMoviesCache = PersistentCache('TmdbNewMoviesCache', maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbNewTvsCache = PersistentCache('TmdbNewTvsCache', maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbUpcomingMoviesCache = PersistentCache('TmdbUpcomingMoviesCache', maxsize=1000, ttl=43200, timer=time.time, default=None)
TmdbTrendingCache = PersistentCache('TmdbTrendingCache', maxsize=1000, ttl=43200, timer=time.time, default=None)

# T11: TMDB季详情缓存 3600s (1小时)
TmdbSeasonDetailCache = PersistentCache('TmdbSeasonDetailCache', maxsize=2000, ttl=3600, timer=time.time, default=None)

# 字幕搜索结果缓存 3600s (替代opensubtitles.py __parse_opensubtitles_results的lru_cache)
SubtitleCache = PersistentCache('SubtitleCache', maxsize=1000, ttl=3600, timer=time.time, default=None)
