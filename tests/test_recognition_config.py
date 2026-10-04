# -*- coding: utf-8 -*-
from copy import deepcopy
import os
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from web.action import WebAction
from config import Config
from app.media.recognition.settings import (
    migrate_legacy_ai_provider, migrate_legacy_parse_ttl, provider_enabled,
)
from ruamel.yaml import YAML


def config_snapshot():
    return {
        "recognition": {
            "execution": {"mode": "all"},
            "decision": {
                "strategy": "legacy",
                "title_evidence": {
                    "on_multiple_matches": "fail",
                    "min_cjk_chars_for_strong": 3,
                    "min_latin_chars_for_strong": 4,
                    "fuzzy_min_score": 0.88,
                },
                "weights": {
                    "title_match": 0.55,
                    "year_match": 0.15,
                    "type_match": 0.1,
                    "season_episode_match": 0.15,
                    "input_evidence": 0.05,
                    "provider_reliability": 0.0,
                },
            },
        },
        "laboratory": {"ai_inference": False, "ai_inference_url": ""},
    }


class RecognitionConfigTest(TestCase):
    def test_legacy_ai_settings_migrate_once_using_effective_enablement(self):
        migrated = {
            "laboratory": {"ai_inference": True, "ai_inference_url": "http://legacy.local"},
            "recognition": {"schema_version": 1, "providers": {
                "anitopy_ml": {"enabled": False, "endpoint": "http://provider.local"},
            }},
        }

        self.assertTrue(migrate_legacy_ai_provider(migrated))
        recognition = migrated["recognition"]
        ai_provider = recognition["providers"]["anitopy_ml"]
        self.assertEqual(2, recognition["schema_version"])
        self.assertFalse(ai_provider["enabled"])
        self.assertEqual("http://provider.local", ai_provider["endpoint"])
        self.assertEqual("unknown", ai_provider["model_revision"])
        self.assertFalse(migrated["laboratory"]["ai_inference"])
        self.assertEqual("http://provider.local", migrated["laboratory"]["ai_inference_url"])
        self.assertFalse(provider_enabled("anitopy_ml", recognition, migrated["laboratory"]))

        migrated["laboratory"]["ai_inference"] = True
        self.assertFalse(migrate_legacy_ai_provider(migrated))
        self.assertFalse(ai_provider["enabled"])

    def test_legacy_ai_migration_uses_lab_endpoint_and_preserves_old_disable(self):
        migrated = {
            "laboratory": {"ai_inference": False, "ai_inference_url": "http://legacy.local"},
            "recognition": {"providers": {"anitopy_ml": {"enabled": True}}},
        }

        self.assertTrue(migrate_legacy_ai_provider(migrated))
        ai_provider = migrated["recognition"]["providers"]["anitopy_ml"]
        self.assertFalse(ai_provider["enabled"])
        self.assertEqual("http://legacy.local", ai_provider["endpoint"])

    def test_legacy_parse_ttl_migrates_only_when_new_ttl_is_missing(self):
        legacy = {"recognition": {"cache": {"parse_ttl_seconds": 321}}}
        self.assertTrue(migrate_legacy_parse_ttl(legacy))
        self.assertEqual(321, legacy["recognition"]["cache"]["parse"]["ttl_seconds"])
        self.assertFalse(migrate_legacy_parse_ttl(legacy))

        explicit = {"recognition": {"cache": {
            "parse_ttl_seconds": 321, "parse": {"ttl_seconds": 654},
        }}}
        self.assertFalse(migrate_legacy_parse_ttl(explicit))
        self.assertEqual(654, explicit["recognition"]["cache"]["parse"]["ttl_seconds"])

    def test_config_save_atomically_replaces_file_and_publishes_snapshot(self):
        config_type = type(Config())
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.yaml")
            config = object.__new__(config_type)
            config._config_path = path
            config._config = {"old": True}

            config.save_config({"recognition": {"strategy": "title_evidence"}})

            with open(path, encoding="utf-8") as config_file:
                self.assertEqual({"recognition": {"strategy": "title_evidence"}},
                                 YAML().load(config_file))
            self.assertEqual({"recognition": {"strategy": "title_evidence"}}, config._config)

    def test_failed_atomic_replace_keeps_file_and_memory_snapshot(self):
        config_type = type(Config())
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.yaml")
            with open(path, "w", encoding="utf-8") as config_file:
                config_file.write("old: true\n")
            config = object.__new__(config_type)
            config._config_path = path
            config._config = {"old": True}

            with patch("config.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    config.save_config({"new": True})

            with open(path, encoding="utf-8") as config_file:
                self.assertEqual({"old": True}, YAML().load(config_file))
            self.assertEqual({"old": True}, config._config)
            self.assertEqual(["config.yaml"], os.listdir(directory))

    def test_test_update_does_not_mutate_live_config_or_save(self):
        live_config = config_snapshot()
        original = deepcopy(live_config)
        save = Mock()
        config = SimpleNamespace(get_config=lambda: live_config, save_config=save)
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({
                "recognition.decision.weights.title_match": 0.8,
                "recognition.decision.title_evidence.min_latin_chars_for_strong": 6,
                "test": True,
            })

        self.assertEqual(0, response["code"])
        self.assertEqual(original, live_config)
        save.assert_not_called()

    def test_deep_recognition_settings_are_saved(self):
        live_config = config_snapshot()
        saved = {}
        config = SimpleNamespace(get_config=lambda: live_config,
                                 save_config=lambda value: saved.update(value))
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({
                "recognition.decision.weights.title_match": 0.7,
                "recognition.decision.title_evidence.min_latin_chars_for_strong": "5",
            })

        self.assertEqual(0, response["code"])
        self.assertEqual(0.7, saved["recognition"]["decision"]["weights"]["title_match"])
        self.assertEqual(5, saved["recognition"]["decision"]["title_evidence"]["min_latin_chars_for_strong"])
        self.assertEqual(0.55, live_config["recognition"]["decision"]["weights"]["title_match"])

    def test_service_schedule_settings_are_saved_and_restart_scheduler(self):
        live_config = config_snapshot()
        saved = {}
        config = SimpleNamespace(get_config=lambda: live_config,
                                 save_config=lambda value: saved.update(value))
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config), \
                patch("web.action.restart_scheduler") as restart:
            response = action._WebAction__update_config({
                "pt.search_rss_interval": 12,
                "recognition.records.cleanup.enabled": "true",
                "recognition.records.cleanup.retention_days": "45",
            })

        self.assertEqual(0, response["code"])
        self.assertEqual(12, saved["pt"]["search_rss_interval"])
        self.assertTrue(saved["recognition"]["records"]["cleanup"]["enabled"])
        self.assertEqual(45, saved["recognition"]["records"]["cleanup"]["retention_days"])
        restart.assert_called_once_with()

    def test_service_schedule_values_are_validated(self):
        live_config = config_snapshot()
        save = Mock()
        config = SimpleNamespace(get_config=lambda: live_config, save_config=save)
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({"pt.search_rss_interval": 2})

        self.assertEqual(1, response["code"])
        self.assertIn("6 小时", response["msg"])
        save.assert_not_called()

    def test_zero_total_weights_are_rejected_without_saving(self):
        live_config = config_snapshot()
        save = Mock()
        config = SimpleNamespace(get_config=lambda: live_config, save_config=save)
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({
                "recognition.decision.weights.title_match": 0,
                "recognition.decision.weights.year_match": 0,
                "recognition.decision.weights.type_match": 0,
                "recognition.decision.weights.season_episode_match": 0,
                "recognition.decision.weights.input_evidence": 0,
                "recognition.decision.weights.provider_reliability": 0,
            })

        self.assertEqual(1, response["code"])
        save.assert_not_called()

    def test_execution_budget_and_provider_reliability_are_configurable(self):
        live_config = config_snapshot()
        saved = {}
        config = SimpleNamespace(get_config=lambda: live_config,
                                 save_config=lambda value: saved.update(value))
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({
                "recognition.execution.total_timeout_seconds": "12.5",
                "recognition.decision.agreement_bonus": "0.2",
                "recognition.providers.local_rules.reliability": "0.8",
                "recognition.providers.anitopy_ml.reliability": "0.6",
            })

        self.assertEqual(0, response["code"])
        self.assertEqual(12.5, saved["recognition"]["execution"]["total_timeout_seconds"])
        self.assertEqual(0.2, saved["recognition"]["decision"]["agreement_bonus"])
        self.assertEqual(0.8, saved["recognition"]["providers"]["local_rules"]["reliability"])

    def test_legacy_ai_settings_page_updates_canonical_provider_settings(self):
        live_config = config_snapshot()
        live_config["recognition"]["providers"] = {
            "anitopy_ml": {"enabled": False, "endpoint": ""},
        }
        saved = {}
        config = SimpleNamespace(get_config=lambda: live_config,
                                 save_config=lambda value: saved.update(value))
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({
                "laboratory.ai_inference": "true",
                "laboratory.ai_inference_url": "http://ai.local",
            })

        self.assertEqual(0, response["code"])
        self.assertTrue(saved["recognition"]["providers"]["anitopy_ml"]["enabled"])
        self.assertEqual("http://ai.local",
                         saved["recognition"]["providers"]["anitopy_ml"]["endpoint"])
        self.assertTrue(saved["laboratory"]["ai_inference"])

    def test_canonical_ai_settings_update_legacy_page_aliases(self):
        live_config = config_snapshot()
        live_config["recognition"]["providers"] = {
            "anitopy_ml": {"enabled": True, "endpoint": "http://old.local"},
        }
        saved = {}
        config = SimpleNamespace(get_config=lambda: live_config,
                                 save_config=lambda value: saved.update(value))
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({
                "recognition.providers.anitopy_ml.enabled": "false",
                "recognition.providers.anitopy_ml.endpoint": "http://new.local",
            })

        self.assertEqual(0, response["code"])
        self.assertFalse(saved["laboratory"]["ai_inference"])
        self.assertEqual("http://new.local", saved["laboratory"]["ai_inference_url"])

    def test_unsupported_execution_mode_and_invalid_timeout_are_rejected(self):
        for values in (
                {"recognition.execution.mode": "cascade"},
                {"recognition.execution.total_timeout_seconds": 301},
                {"recognition.execution.max_inflight_provider_requests": 0},
                {"recognition.cache.parse.ttl_seconds": 0},
                {"recognition.cache.parse.max_entry_bytes": 70000000},
                {"recognition.profiles.parse_only.tmdb_allowed": True},
                {"recognition.profiles.resolve.providers": ["anitopy_ml"]},
                {"recognition.providers.anitopy_ml.reliability": 1.1}):
            live_config = config_snapshot()
            save = Mock()
            config = SimpleNamespace(get_config=lambda: live_config, save_config=save)
            action = object.__new__(WebAction)
            with patch("web.action.Config", return_value=config):
                response = action._WebAction__update_config(values)
            self.assertEqual(1, response["code"])
            save.assert_not_called()

    def test_profile_provider_allowlists_are_saved_with_required_local_parser(self):
        live_config = config_snapshot()
        saved = {}
        config = SimpleNamespace(
            get_config=lambda: live_config,
            save_config=lambda value: saved.update(value))
        action = object.__new__(WebAction)

        with patch("web.action.Config", return_value=config):
            response = action._WebAction__update_config({
                "recognition.profiles.parse_only.providers": ["local_rules", "anitopy_ml"],
                "recognition.profiles.resolve.providers": ["local_rules"],
            })

        self.assertEqual(0, response["code"])
        self.assertEqual(["local_rules", "anitopy_ml"],
                         saved["recognition"]["profiles"]["parse_only"]["providers"])
        self.assertEqual(["local_rules"],
                         saved["recognition"]["profiles"]["resolve"]["providers"])
