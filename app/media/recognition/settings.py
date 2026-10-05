"""识别器的有效配置读取与旧字段兼容。"""


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def provider_settings(provider_id, recognition=None):
    recognition = recognition if isinstance(recognition, dict) else {}
    providers = recognition.get("providers") or {}
    value = providers.get(provider_id) if isinstance(providers, dict) else None
    return value if isinstance(value, dict) else {}


def profile_settings(stage, recognition=None):
    recognition = recognition if isinstance(recognition, dict) else {}
    profiles = recognition.get("profiles") or {}
    configured = profiles.get(stage) if isinstance(profiles, dict) else None
    configured = configured if isinstance(configured, dict) else {}
    defaults = {
        "providers": "all_enabled",
        "network_allowed": True,
        "tmdb_allowed": stage == "resolve",
        "conflict_policy": "unresolved" if stage == "parse_only" else "title_evidence",
    }
    return {**defaults, **configured}


def profile_allows_provider(stage, provider_id, recognition=None):
    # parse_only 必须始终运行本地基础解析器，即使旧配置白名单没有列出它。
    if stage == "parse_only" and provider_id == "local_rules":
        return True
    selected = profile_settings(stage, recognition).get("providers", "all_enabled")
    if selected == "all_enabled":
        return True
    if isinstance(selected, (list, tuple, set)):
        return provider_id in selected
    return False


def profile_network_allowed(stage, recognition=None):
    return _as_bool(profile_settings(stage, recognition).get("network_allowed", False))


def profile_tmdb_allowed(stage, recognition=None):
    return _as_bool(profile_settings(stage, recognition).get(
        "tmdb_allowed", stage == "resolve"))


def provider_requires_network(provider_class):
    return bool(getattr(getattr(provider_class, "descriptor", None),
                        "network_required", False))


def provider_enabled(provider_id, recognition=None, laboratory=None):
    """显式 provider 开关是唯一新配置；仅迁移前缺字段时回退到旧 AI 开关。"""
    configured = provider_settings(provider_id, recognition)
    if "enabled" in configured:
        return _as_bool(configured["enabled"])
    if provider_id == "anitopy_ml":
        laboratory = laboratory if isinstance(laboratory, dict) else {}
        return _as_bool(laboratory.get("ai_inference", False))
    return provider_id == "local_rules"


def provider_endpoint(provider_id, recognition=None, laboratory=None):
    configured = provider_settings(provider_id, recognition)
    options = configured.get("options") or {}
    endpoint = configured.get("endpoint")
    if not endpoint and isinstance(options, dict):
        endpoint = options.get("endpoint")
    if not endpoint and provider_id == "anitopy_ml":
        laboratory = laboratory if isinstance(laboratory, dict) else {}
        endpoint = laboratory.get("ai_inference_url")
    return str(endpoint).strip() if endpoint else ""


def provider_disabled_reason(provider_id, recognition=None, laboratory=None):
    configured = provider_settings(provider_id, recognition)
    if "enabled" in configured and not _as_bool(configured["enabled"]):
        return "anitopy_ml_provider_disabled" if provider_id == "anitopy_ml" \
            else "provider_disabled"
    if provider_id == "anitopy_ml" and not provider_enabled(
            provider_id, recognition, laboratory):
        return "ai_inference_disabled"
    return "provider_disabled"


def migrate_legacy_ai_provider(config):
    """将旧实验室开关/地址一次性归并到 anitopy-ml provider 配置。"""
    if not isinstance(config, dict):
        return False
    recognition = config.get("recognition")
    if not isinstance(recognition, dict):
        recognition = {}
        config["recognition"] = recognition
    try:
        schema_version = int(recognition.get("schema_version", 0))
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version >= 2:
        return False

    providers = recognition.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        recognition["providers"] = providers
    provider = providers.get("anitopy_ml")
    if not isinstance(provider, dict):
        provider = {}
        providers["anitopy_ml"] = provider
    laboratory = config.get("laboratory")
    if not isinstance(laboratory, dict):
        laboratory = {}

    legacy_enabled = _as_bool(laboratory.get("ai_inference", False))
    provider_enabled_before = provider.get("enabled", True)
    provider["enabled"] = legacy_enabled and _as_bool(provider_enabled_before)
    options = provider.get("options") or {}
    endpoint = (provider.get("endpoint")
                or (options.get("endpoint") if isinstance(options, dict) else None)
                or laboratory.get("ai_inference_url"))
    if endpoint:
        provider["endpoint"] = endpoint
        laboratory["ai_inference_url"] = endpoint
    # 让保留的旧设置页面展示迁移后的实际状态，不留下相互矛盾的两个开关。
    laboratory["ai_inference"] = provider["enabled"]
    provider.setdefault("model_revision", "unknown")
    recognition["schema_version"] = 2
    return True


def migrate_legacy_parse_ttl(config):
    """将旧 parse_ttl_seconds 搬到新独立解析缓存 TTL，且只在新值缺失时。"""
    if not isinstance(config, dict):
        return False
    recognition = config.get("recognition")
    if not isinstance(recognition, dict):
        return False
    cache = recognition.get("cache")
    if not isinstance(cache, dict):
        return False
    parse_cache = cache.get("parse")
    if not isinstance(parse_cache, dict):
        parse_cache = {}
        cache["parse"] = parse_cache
    if "ttl_seconds" in parse_cache or "parse_ttl_seconds" not in cache:
        return False
    parse_cache["ttl_seconds"] = cache["parse_ttl_seconds"]
    return True
