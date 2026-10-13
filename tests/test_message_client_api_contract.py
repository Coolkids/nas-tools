import unittest
from unittest.mock import patch


class MessageClientApiContractTest(unittest.TestCase):
    def test_status_route_passes_checked_value(self):
        from web.main import App

        client = App.test_client()
        token = "cached-message-client-token"
        with patch("web.security.TokenCache.get", return_value="signed-message-client-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            enabled = client.post("/api/v1/message/client/status", headers={
                "Authorization": f"Bearer {token}"}, data={
                    "cid": "17", "flag": "enable", "checked": "1"})
            disabled = client.post("/api/v1/message/client/status", headers={
                "Authorization": f"Bearer {token}"}, data={
                    "cid": "17", "flag": "enable", "checked": "0"})

        self.assertEqual(200, enabled.status_code)
        self.assertEqual(200, disabled.status_code)
        self.assertEqual(2, api_action.call_count)
        self.assertEqual("check_message_client", api_action.call_args_list[0].kwargs["cmd"])
        self.assertEqual(1, api_action.call_args_list[0].kwargs["data"]["checked"])
        self.assertEqual(0, api_action.call_args_list[1].kwargs["data"]["checked"])


if __name__ == "__main__":
    unittest.main()
