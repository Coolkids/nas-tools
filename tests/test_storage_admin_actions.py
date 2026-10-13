"""Storage management actions must be protected at the dispatcher boundary."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from web.action import WebAction


class StorageAdminActionTests(unittest.TestCase):
    def test_cache_management_actions_require_the_admin_account(self):
        action = WebAction.__new__(WebAction)
        user = SimpleNamespace(is_authenticated=True, get_id=lambda: "1")
        with patch("web.action.current_user", new=user):

            for command in sorted(WebAction._cache_admin_actions):
                with self.subTest(command=command):
                    result = action.action(command, {})
                    self.assertEqual(403, result["code"])
                    self.assertIn("缓存", result["msg"])

    def test_database_management_actions_require_the_admin_account(self):
        action = WebAction.__new__(WebAction)
        user = SimpleNamespace(is_authenticated=True, get_id=lambda: "1")
        with patch("web.action.current_user", new=user):

            for command in sorted(WebAction._database_admin_actions):
                with self.subTest(command=command):
                    result = action.action(command, {})
                    self.assertEqual(403, result["code"])
                    self.assertIn("数据库", result["msg"])

    def test_storage_management_actions_require_same_origin(self):
        from web.main import App, _is_same_origin_request

        with App.test_request_context(
                "/do", base_url="https://nas.example", headers={
                    "Origin": "https://attacker.example"}):
            self.assertFalse(_is_same_origin_request())

        with App.test_request_context(
                "/do", base_url="https://nas.example", headers={
                    "Origin": "https://nas.example"}):
            self.assertTrue(_is_same_origin_request())


if __name__ == "__main__":
    unittest.main()
