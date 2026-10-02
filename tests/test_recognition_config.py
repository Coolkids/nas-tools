# -*- coding: utf-8 -*-
from copy import deepcopy
import os
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from web.action import WebAction
from config import Config
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

    def test_unsupported_execution_mode_and_invalid_timeout_are_rejected(self):
        for values in (
                {"recognition.execution.mode": "cascade"},
                {"recognition.execution.total_timeout_seconds": 301},
                {"recognition.providers.anitopy_ml.reliability": 1.1}):
            live_config = config_snapshot()
            save = Mock()
            config = SimpleNamespace(get_config=lambda: live_config, save_config=save)
            action = object.__new__(WebAction)
            with patch("web.action.Config", return_value=config):
                response = action._WebAction__update_config(values)
            self.assertEqual(1, response["code"])
            save.assert_not_called()
