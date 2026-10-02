# -*- coding: utf-8 -*-
import unittest

from app.media.recognition import RecognitionRequest
from app.media.recognition.registry import RecognizerRegistry
from app.media.media import Media


class RecognitionRegistryExtensionTest(unittest.TestCase):
    def test_new_provider_module_is_discovered_and_instantiated_without_registry_edits(self):
        registry = RecognizerRegistry("tests.fixtures.recognition_providers")

        providers = registry.discover(refresh=True)
        provider = registry.create("fixture_third")
        result = provider.parse(RecognitionRequest("Example S01E02", subtitle="Part 2"))

        self.assertIn("fixture_third", providers)
        self.assertEqual("fixture_third", result.provider_id)
        self.assertEqual("success", result.status)
        self.assertEqual("Example S01E02", result.parsed["title"])
        self.assertEqual("Part 2", result.parsed["subtitle"])
        self.assertEqual("test-1", providers["fixture_third"].descriptor.version)
        configured_provider = registry.create("fixture_third", mode="strict")
        self.assertEqual("strict", configured_provider.mode)
        registry.release(configured_provider)
        self.assertEqual([], registry.diagnostics())
        registry.release(provider)
        self.assertTrue(provider.closed)

    def test_duplicate_ids_are_quarantined_and_invalid_descriptors_reported(self):
        registry = RecognizerRegistry("tests.fixtures.recognition_bad_providers")

        providers = registry.discover(refresh=True)

        self.assertEqual({}, providers)
        self.assertGreaterEqual(len(registry.diagnostics()), 2)

    def test_provider_settings_only_pass_descriptor_fields_to_constructor(self):
        registry = RecognizerRegistry("tests.fixtures.recognition_providers")
        provider_class = registry.discover(refresh=True)["fixture_third"]

        options = Media._Media__recognition_provider_options(provider_class, {
            "enabled": True, "reliability": 0.9, "mode": "strict", "unrecognized": "ignored"
        })

        self.assertEqual({"mode": "strict"}, options)
        configured = registry.create("fixture_third", **options)
        self.assertEqual("strict", configured.mode)
        registry.release(configured)


if __name__ == "__main__":
    unittest.main()
