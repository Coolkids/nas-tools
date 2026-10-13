import unittest
from unittest.mock import patch


class DownloadConfigApiContractTest(unittest.TestCase):
    def test_update_route_allows_create_without_sid_and_update_with_sid(self):
        from web.main import App

        client = App.test_client()
        token = "cached-download-config-token"
        common = {
            "name": "functional-download-setting",
            "category": "test",
            "content_layout": "0",
            "is_paused": "0",
            "upload_limit": "0",
            "download_limit": "0",
            "ratio_limit": "0",
            "seeding_time_limit": "0",
            "downloader": "Qbittorrent",
        }
        with patch("web.security.TokenCache.get", return_value="signed-download-config-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            created = client.post("/api/v1/download/config/update", headers={
                "Authorization": f"Bearer {token}"}, data=common)
            updated = client.post("/api/v1/download/config/update", headers={
                "Authorization": f"Bearer {token}"}, data=dict(common, sid="42"))

        self.assertEqual(200, created.status_code)
        self.assertEqual(200, updated.status_code)
        self.assertEqual(2, api_action.call_count)
        self.assertEqual("update_download_setting", api_action.call_args_list[0].kwargs["cmd"])
        self.assertIsNone(api_action.call_args_list[0].kwargs["data"]["sid"])
        self.assertEqual("42", api_action.call_args_list[1].kwargs["data"]["sid"])


if __name__ == "__main__":
    unittest.main()
