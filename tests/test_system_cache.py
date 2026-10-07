"""缓存重启恢复、TTL 和管理接口的回归验证。"""

import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase
from unittest.mock import Mock, patch

from cacheout import Cache

from app.media.recognition.cache import CACHE_MISS, _ParseCache
from app.utils.persistent_cache import CacheStore, PersistentCache, persistent_memoize
from app.utils.cache_memory import cache_memory_info
from app.utils import system_cache
from web.action import WebAction


class PersistentCacheTest(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "cache.sqlite3")
        self.store = CacheStore(self.path)

    def tearDown(self):
        if self.store._connection is not None:
            self.store._connection.close()
        self.directory.cleanup()

    def make_cache(self, name="test", **kwargs):
        return PersistentCache(name, store=self.store, **kwargs)

    def test_default_path_migrates_legacy_wal_and_keeps_existing_database(self):
        legacy = CacheStore(os.path.join(self.directory.name, "cache", "system.sqlite3"))
        migrated = CacheStore()
        existing = CacheStore()
        try:
            legacy.put("test", "title", {"name": "Example"}, time.time() + 60)
            self.assertTrue(os.path.exists(legacy.path + "-wal"))
            configured = Mock()
            configured.get_config_path.return_value = self.directory.name
            with patch("config.Config", return_value=configured):
                self.assertEqual("Example", migrated.load("test")[0][1]["name"])
                self.assertEqual(os.path.join(self.directory.name, "system.sqlite3"), migrated.path)
                migrated.clear("test")
                self.assertEqual([], existing.load("test"))
            self.assertTrue(os.path.exists(legacy.path))
            self.assertEqual(1, len(legacy.load("test")))
        finally:
            for store in (legacy, migrated, existing):
                if store._connection is not None:
                    store._connection.close()

    def test_new_connection_recovers_values_types_and_original_ttl(self):
        cache = self.make_cache(ttl=60)
        cache.set(("a", 1), {"values": [1, 2], "image": b"image"})
        original_expiration = cache.expire_times()[("a", 1)]
        second_store = CacheStore(self.path)
        try:
            recovered = PersistentCache("test", store=second_store, ttl=600)
            self.assertEqual({"values": [1, 2], "image": b"image"}, recovered.get(("a", 1)))
            self.assertAlmostEqual(original_expiration, recovered.expire_times()[("a", 1)], places=2)
        finally:
            second_store._connection.close()

    def test_another_process_recovers_then_clears_disk(self):
        self.make_cache(ttl=60).set("title", {"name": "Example"})
        code = """
import sys
from app.utils.persistent_cache import CacheStore, PersistentCache
store = CacheStore(sys.argv[1])
cache = PersistentCache('test', store=store, ttl=60)
assert cache.get('title') == {'name': 'Example'}
cache.clear()
store._connection.close()
"""
        process = subprocess.run([sys.executable, "-c", code, self.path],
                                 capture_output=True, text=True, timeout=20)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertIsNone(self.make_cache().get("title"))

    def test_memoized_methods_share_stable_keys_after_recreation(self):
        def provider(cls, title):
            return {"name": title}

        with patch("app.utils.persistent_cache.STORE", self.store), \
                patch.dict("app.utils.persistent_cache.FUNCTION_CACHES", {}, clear=True):
            first = persistent_memoize("provider")(provider)
            self.assertEqual({"name": "Example"}, first(type("Provider", (), {}), "Example"))
            restored = persistent_memoize("provider")(provider)
            self.assertEqual({"name": "Example"}, restored(type("Provider", (), {}), "Example"))
            self.assertEqual(1, restored.cache_info().hits)
            self.assertEqual(0, restored.cache_info().misses)
            restored.cache_clear()
            self.assertEqual(0, restored.cache_info().currsize)

    def test_expired_values_are_not_restored_or_counted(self):
        self.store.put("test", "old", "old", time.time() - 1)
        self.store.put("test", "new", "new", time.time() + 60)
        cache = self.make_cache()
        cache.delete_expired()
        self.assertEqual(1, len(cache))
        self.assertIsNone(cache.get("old"))
        self.assertEqual([("new", "new")], [(key, value) for key, value, _ in self.store.load("test")])

    def test_clear_before_first_read_deletes_disk_and_isolates_namespaces(self):
        self.make_cache().set("key", "value")
        self.make_cache("other").set("key", "other")
        self.make_cache().clear()
        self.assertIsNone(self.make_cache().get("key"))
        self.assertEqual("other", self.make_cache("other").get("key"))

    def test_eviction_and_delete_remain_effective_after_restart(self):
        cache = self.make_cache(maxsize=2)
        cache.set_many({"a": 1, "b": 2})
        cache.get("a")
        cache.set("c", 3)
        recovered = self.make_cache(maxsize=2)
        self.assertIsNone(recovered.get("b"))
        self.assertEqual({"a": 1, "c": 3}, recovered.copy())
        recovered.delete("a")
        self.assertEqual({"c": 3}, self.make_cache().copy())

    def test_restore_enforces_reduced_capacity(self):
        self.make_cache(maxsize=5).set_many({"a": 1, "b": 2, "c": 3})
        recovered = self.make_cache(maxsize=1)
        self.assertEqual({"c": 3}, recovered.copy())
        self.assertEqual(1, len(self.store.load("test")))

    def test_memory_statistics_restore_persisted_entries_and_clear_disk(self):
        self.make_cache().set("payload", bytes(8192))
        restored = self.make_cache()
        measured = cache_memory_info(restored)
        self.assertEqual(1, measured["entries"])
        self.assertGreater(measured["memory_bytes"], 8192)
        restored.clear()
        self.assertEqual(0, cache_memory_info(restored)["memory_bytes"])
        self.assertEqual(0, cache_memory_info(self.make_cache())["memory_bytes"])

    def test_corrupt_entry_does_not_hide_valid_entries(self):
        self.make_cache().set("good", 1)
        with self.store._connection as connection:
            connection.execute("INSERT INTO cache_entries VALUES (?, ?, ?, ?)",
                               ("test", "bad", b"broken", None))
        self.assertEqual({"good": 1}, self.make_cache().copy())

    def test_concurrent_writes_recover_all_entries(self):
        cache = self.make_cache(maxsize=100)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda i: cache.set(i, {"value": i}), range(40)))
        self.assertEqual(40, len(self.make_cache()))

    def test_parse_cache_restores_and_clears_with_generation_protection(self):
        settings = {"parse": {"ttl_seconds": 60}}
        first = _ParseCache(store=self.store)
        first.put("title", {"name": "Example"}, settings)
        restored = _ParseCache(store=self.store)
        self.assertEqual({"name": "Example"}, restored.get("title", settings))
        generation = restored.generation(settings)
        restored.clear(settings)
        self.assertFalse(restored.put("late", {}, settings, generation=generation))
        self.assertIs(CACHE_MISS, _ParseCache(store=self.store).get("title", settings))

    def test_parse_cache_settings_change_invalidates_persisted_results(self):
        first = _ParseCache(store=self.store)
        first.put("title", {"name": "Example"}, {"parse": {"ttl_seconds": 60}})
        restored = _ParseCache(store=self.store)
        self.assertIs(CACHE_MISS, restored.get("title", {"parse": {"ttl_seconds": 120}}))
        self.assertEqual([], self.store.load("recognition_parse"))


class SystemCacheApiTest(TestCase):
    def test_catalog_includes_all_named_and_function_caches(self):
        names = set(system_cache._caches())
        self.assertTrue(set(system_cache.LABELS) <= names)

    def test_info_expires_entries_and_includes_disabled_ai_cache(self):
        instance = Cache(maxsize=10, ttl=30)
        instance.set("value", 1)
        helper = Mock()
        helper.cache_info.return_value = {"entries": 4}
        with patch.object(system_cache, "_caches", return_value={"example": instance}), \
                patch("app.helper.meta_helper.MetaHelper", return_value=helper), \
                patch("app.media.recognition.cache.info", return_value={"entries": 2}):
            result = WebAction._WebAction__get_system_cache_info()
        self.assertEqual(0, result["code"])
        self.assertEqual(1, result["caches"][0]["entries"])
        self.assertIn("recognition_parse", [item["name"] for item in result["caches"]])

    def test_clear_one_does_not_clear_other_caches(self):
        first, second = Cache(), Cache()
        first.set("a", 1)
        second.set("b", 2)
        with patch.object(system_cache, "_caches", return_value={"first": first, "second": second}):
            result = WebAction._WebAction__clear_system_cache({"name": "first"})
        self.assertEqual({"code": 0, "cleared_entries": 1}, result)
        self.assertEqual(0, len(first))
        self.assertEqual(1, len(second))

    def test_invalid_name_never_clears_anything(self):
        instance = Mock()
        with patch.object(system_cache, "_caches", return_value={"test": instance}):
            for name in (None, "unknown", ["all"], {"name": "all"}):
                result = WebAction._WebAction__clear_system_cache({"name": name})
                self.assertEqual(1, result["code"])
        instance.clear.assert_not_called()

    def test_clear_all_covers_memory_disk_and_recognition(self):
        instance = Cache()
        instance.set("a", 1)
        helper = Mock()
        helper.cache_info.return_value = {"entries": 4}
        with patch.object(system_cache, "_caches", return_value={"test": instance}), \
                patch("app.helper.meta_helper.MetaHelper", return_value=helper), \
                patch("app.media.recognition.cache.info", return_value={"entries": 2}), \
                patch("app.media.recognition.cache.clear") as clear:
            result = WebAction._WebAction__clear_system_cache({"name": "all"})
        self.assertEqual(11, result["cleared_entries"])
        self.assertEqual(0, len(instance))
        self.assertEqual({"parse", "tmdb", "decision"}, {call.args[0] for call in clear.call_args_list})
        helper.clear_meta_data.assert_called_once()

    def test_cache_actions_require_login(self):
        from web.main import App

        with patch.object(system_cache, "clear_cache") as clear, \
                patch.object(system_cache, "cache_info") as info:
            for command in ("get_system_cache_info", "clear_system_cache"):
                response = App.test_client().post("/do", data={"cmd": command, "data": '{"name":"all"}'})
                self.assertEqual(-1, response.json["code"])
        clear.assert_not_called()
        info.assert_not_called()
