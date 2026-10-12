"""Cache migration diagnostics must not include backend credentials."""

import unittest

from app.utils.cache_migration import _safe_candidate, _safe_error


class CacheMigrationSecurityTests(unittest.TestCase):
    def test_candidate_state_omits_user_name_and_password(self):
        safe = _safe_candidate({
            "backend": "redis", "username": "cache-user", "password": "secret-value",
        })

        self.assertNotIn("username", safe)
        self.assertNotIn("password", safe)
        self.assertTrue(safe["username_set"])
        self.assertTrue(safe["password_set"])

    def test_driver_error_redacts_candidate_password(self):
        message = _safe_error(
            RuntimeError("authentication failed for secret-value"),
            {"password": "secret-value"})

        self.assertEqual("authentication failed for ***", message)
        self.assertNotIn("secret-value", message)


if __name__ == "__main__":
    unittest.main()
