"""缓存管理分组和可动态修改的有效期策略。"""

CACHE_GROUPS = {
    "tmdb_discover": (
        "TmdbHotMoviesCache", "TmdbHotTvsCache", "TmdbNewMoviesCache",
        "TmdbNewTvsCache", "TmdbUpcomingMoviesCache", "TmdbTrendingCache",
    ),
}
MAX_TTL_SECONDS = 36500 * 86400


def cache_category(name):
    return next((group for group, members in CACHE_GROUPS.items() if name in members), name)


def cache_ttl(name, default):
    # 延迟读取，避免缓存模块加载时初始化配置；每次写入使用最新设置。
    from config import Config

    settings = Config().get_config("cache") or {}
    policies = settings.get("ttl_seconds") or {}
    value = policies.get(cache_category(name)) if isinstance(policies, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and 0 <= value <= MAX_TTL_SECONDS else default
