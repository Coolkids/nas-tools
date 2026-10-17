"""验证管理分组、有效期持久化和不同缓存写入路径的过期行为。"""

from copy import deepcopy
import time
from unittest.mock import Mock

import pytest

from app.media.recognition.cache import _ParseCache
from app.utils import system_cache
from app.utils.cache_policy import CACHE_GROUPS, cache_ttl
from app.utils.persistent_cache import CacheStore, PersistentCache
from config import Config


@pytest.fixture(params=["memory", "memory_disk"])
def policy_config(tmp_path, monkeypatch, request):
    config = Config()
    monkeypatch.setattr(config, "_config_path", str(tmp_path / "config.yaml"))
    monkeypatch.setattr(config, "_config", {
        "cache": {"backend": "memory", "host": "cache.example", "password": "keep-me",
                  "ttl_seconds": {"other": 120}},
    })
    monkeypatch.setattr(system_cache.STORE, "status", lambda: {"maintenance": False})
    store = CacheStore(str(tmp_path / "cache"), settings={"backend": request.param}, maintenance=False)
    caches = {name: PersistentCache(name, store=store, ttl=43200)
              for name in CACHE_GROUPS["tmdb_discover"]}
    caches["SubtitleCache"] = PersistentCache("SubtitleCache", store=store, ttl=3600)
    monkeypatch.setattr(system_cache, "_caches", lambda: caches)
    yield config, store, caches
    store.close()


@pytest.mark.parametrize("value,unit,seconds", [
    (15, "minute", 900), (2, "hour", 7200), (3, "day", 259200),
    (None, "forever", 0), (0.5, "minute", 30),
])
def test_ttl_is_saved_and_reloaded_without_changing_storage(policy_config, value, unit, seconds):
    config, _, caches = policy_config
    assert system_cache.update_cache_ttl("tmdb_discover", value, unit) == seconds
    config.init_config()
    saved = config.get_config("cache")
    assert saved["ttl_seconds"] == {"other": 120, "tmdb_discover": seconds}
    assert saved["backend"] == "memory"
    assert saved["password"] == "keep-me"
    assert all(instance.ttl == seconds for name, instance in caches.items()
               if name in CACHE_GROUPS["tmdb_discover"])


def test_policy_applies_to_explicit_ttl_and_new_instances_but_preserves_existing_expiration(policy_config):
    _, store, caches = policy_config
    instance = caches["TmdbHotMoviesCache"]
    instance.set("existing", 1)
    expiration = instance.expire_times()["existing"]
    system_cache.update_cache_ttl("tmdb_discover", 2, "minute")
    start = time.time()
    instance.set("new", 2, ttl=9999)
    assert start + 120 <= instance.expire_times()["new"] <= time.time() + 120
    assert instance.expire_times()["existing"] == expiration
    recreated = PersistentCache(instance.name, store=store, ttl=43200)
    assert recreated.ttl == 120
    assert recreated.get("new") == 2
    system_cache.update_cache_ttl("tmdb_discover", None, "forever")
    recreated.set("permanent", 3, ttl=1)
    assert store.get(instance.name, "permanent")[2] is None


@pytest.mark.parametrize("name,value,unit", [
    (None, 1, "hour"), ([], 1, "hour"), ("unknown", 1, "hour"),
    ("tmdb_discover", 1, "second"), ("tmdb_discover", 1, []),
    ("tmdb_discover", 0, "minute"), ("tmdb_discover", -1, "day"),
    ("tmdb_discover", float("nan"), "hour"), ("tmdb_discover", float("inf"), "hour"),
    ("tmdb_discover", True, "hour"), ("tmdb_discover", 36501, "day"),
])
def test_invalid_settings_do_not_mutate_config(policy_config, name, value, unit):
    config, _, _ = policy_config
    original = deepcopy(config.get_config())
    with pytest.raises(ValueError):
        system_cache.update_cache_ttl(name, value, unit)
    assert config.get_config() == original


def test_migration_and_failed_save_keep_policy_unchanged(policy_config, monkeypatch):
    config, _, _ = policy_config
    original = deepcopy(config.get_config())
    monkeypatch.setattr(system_cache.STORE, "status", lambda: {"maintenance": True})
    with pytest.raises(ValueError, match="迁移期间"):
        system_cache.update_cache_ttl("tmdb_discover", 1, "day")
    monkeypatch.setattr(system_cache.STORE, "status", lambda: {"maintenance": False})
    monkeypatch.setattr(config, "save_config", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        system_cache.update_cache_ttl("tmdb_discover", 1, "day")
    assert config.get_config() == original


def test_merged_clear_keeps_unrelated_namespaces(policy_config):
    _, _, caches = policy_config
    for instance in caches.values():
        instance.set("same-key", instance.name)
    assert system_cache.clear_cache("tmdb_discover") == 6
    assert all(len(caches[name]) == 0 for name in CACHE_GROUPS["tmdb_discover"])
    assert caches["SubtitleCache"].get("same-key") == "SubtitleCache"


def test_group_statistics_sum_data_but_count_shared_file_once(policy_config, monkeypatch):
    _, _, caches = policy_config
    for instance in caches.values():
        monkeypatch.setattr(instance, "memory_info", lambda: {
            "entries": 2, "memory_bytes": 10, "bytes": 20,
            "store_bytes": 1000, "persistent": True, "backend": "memory_disk"})
    helper = Mock()
    helper.cache_info.return_value = {"entries": 0, "memory_bytes": 0}
    monkeypatch.setattr("app.helper.meta_helper.MetaHelper", lambda: helper)
    monkeypatch.setattr("app.media.recognition.cache.info",
                        lambda *args, **kwargs: {"entries": 0, "memory_bytes": 0})
    catalog = system_cache.cache_info()
    group = next(item for item in catalog if item["name"] == "tmdb_discover")
    assert group["entries"] == 12
    assert group["memory_bytes"] == 60
    assert group["bytes"] == 120
    assert group["store_bytes"] == 1000
    assert len(group["members"]) == 6
    assert not set(CACHE_GROUPS["tmdb_discover"]) & {item["name"] for item in catalog}


def test_parse_cache_supports_permanent_without_clearing_older_entries(policy_config):
    _, store, _ = policy_config
    instance = _ParseCache(store=store)
    settings = {"parse": {"ttl_seconds": 60}}
    instance.put("existing", 1, settings)
    expiration = store.get("recognition_parse", "existing")[2]
    system_cache.update_cache_ttl("recognition_parse", None, "forever")
    instance.put("permanent", 2, settings)
    assert store.get("recognition_parse", "existing")[2] == expiration
    assert store.get("recognition_parse", "permanent")[2] is None
    assert instance.info(settings)["ttl_seconds"] == 0
    assert instance.get("existing", settings) == 1


def test_metadata_honors_finite_and_permanent_policies(policy_config, monkeypatch):
    from app.helper.meta_helper import MetaHelper, CACHE_EXPIRE_TIMESTAMP_STR

    _, store, _ = policy_config
    helper = MetaHelper()
    monkeypatch.setattr(helper, "_cache", PersistentCache("tmdb_metadata", store=store))
    monkeypatch.setattr(helper, "_tmdb_cache_expire", False)
    system_cache.update_cache_ttl("tmdb_metadata", 2, "minute")
    helper.update_meta_data({"finite": {"id": 1}})
    assert 119 <= store.get("tmdb_metadata", "finite")[2] - time.time() <= 120
    assert helper.cache_info()["ttl_seconds"] == 120
    system_cache.update_cache_ttl("tmdb_metadata", None, "forever")
    helper.update_meta_data({"permanent": {"id": 2}})
    assert store.get("tmdb_metadata", "permanent")[2] is None
    assert helper.get_meta_data_by_key("permanent")[CACHE_EXPIRE_TIMESTAMP_STR] is None
    helper.save_meta_data()
    assert cache_ttl("tmdb_metadata", 100) == 0


def test_recognition_query_layers_apply_policy_even_to_explicit_negative_ttl(policy_config, monkeypatch):
    from app.media.recognition import cache

    _, store, _ = policy_config
    monkeypatch.setattr(cache, "_CACHES", {
        namespace: PersistentCache(f"recognition_{namespace}", store=store, ttl=3600)
        for namespace in ("tmdb", "decision")})
    monkeypatch.setattr(cache, "recognition_config", lambda node: {
        "cache": {"enabled": True, "tmdb_ttl_seconds": 90, "decision_ttl_seconds": 120,
                  "negative_ttl_seconds": 5}})
    system_cache.update_cache_ttl("recognition_tmdb", None, "forever")
    system_cache.update_cache_ttl("recognition_decision", 2, "hour")
    assert cache.put("tmdb", "negative", {"found": False}, negative=True)
    assert store.get("recognition_tmdb", "negative")[2] is None
    assert cache.info("tmdb", include_memory=True)["ttl_seconds"] == 0
    assert cache.put("decision", "result", {"found": True})
    assert 7199 <= store.get("recognition_decision", "result")[2] - time.time() <= 7200
    assert cache.info("decision", include_memory=True)["ttl_seconds"] == 7200
