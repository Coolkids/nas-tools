# -*- coding: utf-8 -*-
import os
import time
from pathlib import Path
from unittest import TestCase


ROOT_PATH = Path(__file__).resolve().parents[1]
os.environ.setdefault("NASTOOL_CONFIG", str(ROOT_PATH / "config" / "config.yaml"))

from app.media.meta.release_groups import ReleaseGroupsMatcher
from config import Config


class ReleaseGroupsMatcherTest(TestCase):
    def setUp(self):
        self.laboratory = Config().get_config("laboratory")
        self.original_release_groups = self.laboratory.get("release_groups")

    def tearDown(self):
        self.laboratory["release_groups"] = self.original_release_groups

    def test_pathological_custom_pattern_does_not_block_matching(self):
        # "(a|aa)+" is a typical catastrophic-backtracking pattern when the
        # expected suffix is absent. It must be stopped by the match timeout.
        self.laboratory["release_groups"] = "(a|aa)+&LoliHouse;LoliHouse"
        matcher = ReleaseGroupsMatcher()

        start = time.monotonic()
        result = matcher.match(title="[" + "a" * 1000 + " ")
        elapsed = time.monotonic() - start

        self.assertEqual("", result)
        self.assertLess(elapsed, 0.5)

    def test_invalid_custom_pattern_is_ignored(self):
        self.laboratory["release_groups"] = "("

        self.assertEqual("", ReleaseGroupsMatcher().match(title="[Example]"))

    def test_valid_custom_pattern_is_still_matched(self):
        self.laboratory["release_groups"] = "CustomGroup"

        self.assertEqual("CustomGroup", ReleaseGroupsMatcher().match(title="[CustomGroup]"))
