import unittest
from unittest.mock import patch


class ApiAuthorizationHeaderTest(unittest.TestCase):
    def test_client_api_accepts_bearer_and_legacy_raw_tokens(self):
        from web.main import App

        token = "cached-test-token"
        for authorization in (token, f"Bearer {token}", f"bearer {token}"):
            with self.subTest(authorization=authorization), \
                    patch("web.security.TokenCache.get", return_value="signed-test-token") as get_token, \
                    patch("web.security.identify", return_value=(True, "test-user")), \
                    patch("web.apiv1.WebAction.api_action", return_value={"code": 0, "success": True}):
                response = App.test_client().post(
                    "/api/v1/filterrule/list",
                    headers={"Authorization": authorization},
                )

            self.assertEqual(200, response.status_code)
            self.assertEqual(0, response.json["code"])
            get_token.assert_called_once_with(token)

    def test_client_api_rejects_unsupported_authorization_schemes(self):
        from web.main import App

        with patch("web.security.TokenCache.get") as get_token:
            response = App.test_client().post(
                "/api/v1/filterrule/list",
                headers={"Authorization": "Basic cached-test-token"},
            )

        self.assertEqual(403, response.json["code"])
        get_token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
