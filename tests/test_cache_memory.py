import sys
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from cacheout import Cache

from app.media.recognition.cache import _ParseCache
from app.utils.cache_memory import cache_memory_info, estimate_memory
from app.utils import system_cache


class CacheMemoryTest(TestCase):
    def test_nested_data_and_object_attributes_are_included(self):
        payload = bytes(8192)
        value = SimpleNamespace(nested={"items": [payload]})
        self.assertGreater(estimate_memory(value), sys.getsizeof(payload))

    def test_shared_references_and_cycles_are_counted_once(self):
        payload = bytes(8192)
        self.assertEqual(sys.getsizeof(payload), estimate_memory(payload, payload))
        recursive = []
        recursive.append(recursive)
        self.assertEqual(sys.getsizeof(recursive), estimate_memory(recursive))

    def test_objects_with_slots_are_included(self):
        class Slotted:
            __slots__ = ("payload",)

        value = Slotted()
        value.payload = bytes(8192)
        self.assertEqual(sys.getsizeof(value) + sys.getsizeof(value.payload), estimate_memory(value))

    def test_memoryviews_include_shared_backing_buffer_once(self):
        payload = bytes(8192)
        view = memoryview(payload)
        self.assertEqual(sys.getsizeof(view) + sys.getsizeof(payload), estimate_memory(view, payload))

    def test_class_and_function_objects_do_not_traverse_global_state(self):
        def callback():
            pass

        self.assertEqual(sys.getsizeof(callback), estimate_memory(callback))
        self.assertEqual(sys.getsizeof(SimpleNamespace), estimate_memory(SimpleNamespace))

    def test_cache_grows_with_payload_and_clear_reports_zero(self):
        instance = Cache()
        self.assertEqual({"entries": 0, "memory_bytes": 0}, cache_memory_info(instance))
        instance.set("key", {"payload": bytes(1024)})
        before = cache_memory_info(instance)["memory_bytes"]
        instance.set("key", {"payload": bytes(8192)})
        self.assertGreater(cache_memory_info(instance)["memory_bytes"], before + 7000)
        instance.clear()
        self.assertEqual({"entries": 0, "memory_bytes": 0}, cache_memory_info(instance))

    def test_expired_entries_are_excluded(self):
        now = [10]
        instance = Cache(ttl=1, timer=lambda: now[0])
        instance.set("key", bytes(8192))
        now[0] = 12
        self.assertEqual({"entries": 0, "memory_bytes": 0}, cache_memory_info(instance))

    def test_ai_memory_is_separate_from_serialized_byte_budget(self):
        instance = _ParseCache()
        settings = {"parse": {"ttl_seconds": 60}}
        instance.put("key", {"items": list(range(200))}, settings)
        original = instance.info(settings)
        measured = instance.info(settings, include_memory=True)
        self.assertEqual(original["bytes"], measured["bytes"])
        self.assertGreater(measured["memory_bytes"], measured["bytes"])
        instance.clear(settings)
        self.assertEqual(0, instance.info(settings, include_memory=True)["memory_bytes"])

    def test_metadata_cache_memory_uses_object_data(self):
        from app.helper.meta_helper import MetaHelper

        helper = MetaHelper()
        with patch.object(helper, "_meta_data", {"key": {"title": "Example", "image": bytes(8192)}}):
            self.assertGreater(helper.cache_info(include_memory=True)["memory_bytes"], 8192)
        with patch.object(helper, "_meta_data", {}):
            self.assertEqual(0, helper.cache_info(include_memory=True)["memory_bytes"])

    def test_management_api_provides_memory_for_each_cache_category(self):
        instance = Cache()
        instance.set("key", bytes(8192))
        helper = Mock()
        helper.cache_info.return_value = {"entries": 1, "memory_bytes": 1024}
        with patch.object(system_cache, "_caches", return_value={"test": instance}), \
                patch("app.helper.meta_helper.MetaHelper", return_value=helper), \
                patch("app.media.recognition.cache.info",
                      return_value={"entries": 1, "memory_bytes": 512}) as info:
            result = system_cache.cache_info()
        self.assertEqual(5, len(result))
        self.assertTrue(all(isinstance(item["memory_bytes"], int) for item in result))
        self.assertGreater(result[0]["memory_bytes"], 8192)
        self.assertTrue(all(call.kwargs["include_memory"] for call in info.call_args_list))
