from concurrent.futures import Future
from threading import Event, Thread
from unittest import TestCase
from unittest.mock import patch

from app.media.recognition import cache
from app.media.recognition.contracts import (
    MediaNameRecognizer,
    ParseResult,
    RecognitionRequest,
    RecognizerDescriptor,
)
from app.media.recognition.service import RecognitionService, _ProviderConcurrencyGate


class RecognitionServiceTest(TestCase):
    def test_provider_concurrency_limit_times_out_waiter_and_releases_slot(self):
        entered = Event()
        release = Event()
        calls = []

        class Provider(MediaNameRecognizer):
            descriptor = RecognizerDescriptor("limited_provider", "Limited", evidence_family="test")

            def parse(self, request):
                calls.append(request.title)
                if request.title == "first":
                    entered.set()
                    release.wait(timeout=2)
                return ParseResult("limited_provider", "success", parsed={"name": request.title})

        service = RecognitionService(concurrency_gate=_ProviderConcurrencyGate(
            limit_provider=lambda: 1))
        with patch("app.media.recognition.service.registry.discover",
                      return_value={"limited_provider": Provider}), \
                patch("app.media.recognition.service.registry.create",
                      side_effect=lambda *_args, **_kwargs: Provider()), \
                patch("app.media.recognition.service.registry.release"):
            first_result = []
            first = Thread(target=lambda: first_result.append(service.run_provider(
                "limited_provider", RecognitionRequest(title="first"), timeout=2)))
            first.start()
            self.assertTrue(entered.wait(timeout=1))
            blocked = service.run_provider(
                "limited_provider", RecognitionRequest(title="second"), timeout=0.05)
            self.assertEqual(["first"], calls)
            self.assertEqual("timeout", blocked.status)
            self.assertEqual("provider_concurrency_wait_timeout", blocked.error)
            release.set()
            first.join(timeout=2)
            after_release = service.run_provider(
                "limited_provider", RecognitionRequest(title="third"), timeout=1)

        self.assertFalse(first.is_alive())
        self.assertEqual("success", first_result[0].status)
        self.assertEqual("success", after_release.status)
        self.assertEqual(["first", "third"], calls)

    def test_provider_exception_releases_concurrency_slot_for_retry(self):
        calls = []

        class Provider(MediaNameRecognizer):
            descriptor = RecognizerDescriptor("exception_provider", "Exception", evidence_family="test")

            def parse(self, request):
                calls.append(request.title)
                if len(calls) == 1:
                    raise RuntimeError("provider failed")
                return ParseResult("exception_provider", "success", parsed={"name": request.title})

        service = RecognitionService(concurrency_gate=_ProviderConcurrencyGate(
            limit_provider=lambda: 1))
        with patch("app.media.recognition.service.registry.discover",
                      return_value={"exception_provider": Provider}), \
                patch("app.media.recognition.service.registry.create",
                      side_effect=lambda *_args, **_kwargs: Provider()), \
                patch("app.media.recognition.service.registry.release"):
            failed = service.run_provider(
                "exception_provider", RecognitionRequest(title="retry"))
            retried = service.run_provider(
                "exception_provider", RecognitionRequest(title="retry"))

        self.assertEqual("error", failed.status)
        self.assertEqual("success", retried.status)
        self.assertEqual(["retry", "retry"], calls)

    def test_force_refresh_skips_cached_parse_and_replaces_it(self):
        calls = []

        class Provider(MediaNameRecognizer):
            descriptor = RecognizerDescriptor("refresh_provider", "Refresh", evidence_family="test")

            def parse(self, request):
                calls.append(len(calls) + 1)
                return ParseResult("refresh_provider", "success", parsed={"attempt": len(calls)})

        settings = {"cache": {"enabled": True, "parse": {
            "enabled": True, "ttl_seconds": 60, "max_entries": 8,
            "max_bytes": 4096, "max_entry_bytes": 1024, "singleflight": True,
        }}}
        service = RecognitionService()
        with patch("app.media.recognition.service.registry.discover",
                   return_value={"refresh_provider": Provider}), \
                patch("app.media.recognition.service.registry.create",
                      side_effect=lambda *_args, **_kwargs: Provider()), \
                patch("app.media.recognition.service.registry.release"), \
                patch("app.media.recognition.cache.recognition_config",
                      return_value=settings):
            cache.clear("parse")
            first = service.run_provider(
                "refresh_provider", RecognitionRequest(title="Refresh me"))
            refreshed = service.run_provider(
                "refresh_provider", RecognitionRequest(title="Refresh me"),
                force_refresh=True)
            cached = service.run_provider(
                "refresh_provider", RecognitionRequest(title="Refresh me"))

        self.assertEqual([1, 2], calls)
        self.assertEqual({"attempt": 1}, first.parsed)
        self.assertEqual({"attempt": 2}, refreshed.parsed)
        self.assertEqual({"attempt": 2}, cached.parsed)
        self.assertEqual("cache", cached.cache_source)

    def test_successful_parse_is_shared_through_cache_across_service_instances(self):
        calls = []
        store = {}

        class Provider(MediaNameRecognizer):
            descriptor = RecognizerDescriptor("test_provider", "Test", evidence_family="test")

            def parse(self, request):
                calls.append(request.title)
                return ParseResult("test_provider", "success", parsed={"name": request.title})

        with patch("app.media.recognition.service.registry.discover",
                   return_value={"test_provider": Provider}), \
                patch("app.media.recognition.service.registry.create", side_effect=lambda *_args, **_kwargs: Provider()), \
                patch("app.media.recognition.service.registry.release"), \
                patch("app.media.recognition.service.cache.get",
                      side_effect=lambda _namespace, key: store.get(key, cache.CACHE_MISS)), \
                patch("app.media.recognition.service.cache.put",
                      side_effect=lambda _namespace, key, value, **_kwargs:
                      store.setdefault(key, value) is value):
            first = RecognitionService().run_provider(
                "test_provider", RecognitionRequest(title="Example"))
            second = RecognitionService().run_provider(
                "test_provider", RecognitionRequest(title="Example"))

        self.assertEqual(["Example"], calls)
        self.assertEqual({"name": "Example"}, first.parsed)
        self.assertEqual({"name": "Example"}, second.parsed)
        self.assertEqual("cache", second.cache_source)

    def test_concurrent_identical_requests_share_one_provider_execution(self):
        entered = Event()
        release = Event()
        waiter_started = Event()
        calls = []
        store = {}

        class Provider(MediaNameRecognizer):
            descriptor = RecognizerDescriptor("test_provider", "Test", evidence_family="test")

            def parse(self, request):
                calls.append(request.title)
                entered.set()
                if not release.wait(timeout=2):
                    return ParseResult("test_provider", "timeout", error="test_timeout")
                return ParseResult("test_provider", "success", parsed={"name": request.title})

        service = RecognitionService()
        original_result = Future.result

        def wait_for_shared_result(future, timeout=None):
            waiter_started.set()
            return original_result(future, timeout=timeout)

        with patch("app.media.recognition.service.registry.discover",
                   return_value={"test_provider": Provider}), \
                patch("app.media.recognition.service.registry.create", side_effect=lambda *_args, **_kwargs: Provider()), \
                patch("app.media.recognition.service.registry.release"), \
                patch("app.media.recognition.service.cache.get",
                      side_effect=lambda _namespace, key: store.get(key, cache.CACHE_MISS)), \
                patch("app.media.recognition.service.cache.put",
                      side_effect=lambda _namespace, key, value, **_kwargs:
                      store.setdefault(key, value) is value), \
                patch.object(Future, "result", wait_for_shared_result):
            results = []
            leader = Thread(target=lambda: results.append(service.run_provider(
                "test_provider", RecognitionRequest(title="Same"), timeout=1)))
            follower = Thread(target=lambda: results.append(service.run_provider(
                "test_provider", RecognitionRequest(title="Same"), timeout=1)))
            leader.start()
            self.assertTrue(entered.wait(timeout=1))
            follower.start()
            self.assertTrue(waiter_started.wait(timeout=1))
            release.set()
            leader.join(timeout=2)
            follower.join(timeout=2)

        self.assertFalse(leader.is_alive())
        self.assertFalse(follower.is_alive())
        self.assertEqual(["Same"], calls)
        self.assertEqual(2, len(results))
        self.assertEqual({"success"}, {result.status for result in results})
        self.assertIn("shared", {result.cache_source for result in results})
