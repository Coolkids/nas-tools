# -*- coding: utf-8 -*-
import time

from app.utils.persistent_cache import CacheStore, PersistentCache

CACHES = {
    "tmdb_supply": {'ttl': 86400}
}

cacheman = {}
for name, options in CACHES.items():
    cacheman[name] = PersistentCache(name, **options)

# T12: TMDB网站搜索结果缓存 86400s (1天)
TmdbWebSearchCache = PersistentCache('TmdbWebSearchCache', ttl=86400, timer=time.time, default=None)

_LOCAL_STORE = CacheStore(settings={"backend": "memory"}, maintenance=False)
TokenCache = PersistentCache('TokenCache', store=_LOCAL_STORE, ttl=4*3600)

ConfigLoadCache = PersistentCache('ConfigLoadCache', store=_LOCAL_STORE, ttl=10)

# T2: Torznab搜索结果缓存 300s
TorznabCache = PersistentCache('TorznabCache', ttl=300, timer=time.time, default=None)

# T3: Web后端媒体搜索缓存 300s
WebSearchCache = PersistentCache('WebSearchCache', ttl=300, timer=time.time, default=None)

# T3: Web后端媒体详情缓存 300s
WebMediaInfoCache = PersistentCache('WebMediaInfoCache', ttl=300, timer=time.time, default=None)

# T6: TMDB英文标题缓存 43200s (12小时)
TmdbEnTitleCache = PersistentCache('TmdbEnTitleCache', ttl=43200, timer=time.time, default=None)

# T7: TMDB热门/最新/即将上映/趋势缓存 43200s (12小时)
TmdbHotMoviesCache = PersistentCache('TmdbHotMoviesCache', ttl=43200, timer=time.time, default=None)
TmdbHotTvsCache = PersistentCache('TmdbHotTvsCache', ttl=43200, timer=time.time, default=None)
TmdbNewMoviesCache = PersistentCache('TmdbNewMoviesCache', ttl=43200, timer=time.time, default=None)
TmdbNewTvsCache = PersistentCache('TmdbNewTvsCache', ttl=43200, timer=time.time, default=None)
TmdbUpcomingMoviesCache = PersistentCache('TmdbUpcomingMoviesCache', ttl=43200, timer=time.time, default=None)
TmdbTrendingCache = PersistentCache('TmdbTrendingCache', ttl=43200, timer=time.time, default=None)

# T11: TMDB季详情缓存 3600s (1小时)
TmdbSeasonDetailCache = PersistentCache('TmdbSeasonDetailCache', ttl=3600, timer=time.time, default=None)

# 字幕搜索结果缓存 3600s (替代opensubtitles.py __parse_opensubtitles_results的lru_cache)
SubtitleCache = PersistentCache('SubtitleCache', ttl=3600, timer=time.time, default=None)
