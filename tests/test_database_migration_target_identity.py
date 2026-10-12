"""目标检查凭证绑定到实际响应的数据库实例。"""

from unittest import TestCase
from unittest.mock import patch

from app.db.migration import _engine_target_identity


class DatabaseMigrationTargetIdentityTest(TestCase):
    def test_identity_includes_endpoint_and_server_capabilities(self):
        settings = {"type": "postgresql", "host": "db.example", "port": 5432,
                    "name": "nastools", "username": "nastools", "password": "secret"}
        capabilities = {"product": "PostgreSQL", "version_num": "180000",
                        "database": "nastools", "server_address": "192.0.2.10",
                        "server_port": 5432}
        with patch("app.db.migration._server_capabilities", return_value=capabilities), \
                patch("app.db.migration._resolved_addresses",
                      return_value=["192.0.2.10"]):
            identity = _engine_target_identity(settings, object())

        self.assertEqual({"192.0.2.10"}, set(identity["resolved_addresses"]))
        self.assertEqual(capabilities, identity["capabilities"])
        self.assertNotIn("secret", repr(identity))
