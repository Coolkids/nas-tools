import unittest
from unittest.mock import Mock, patch


class CustomWordStatusApiContractTest(unittest.TestCase):
    def test_status_route_preserves_numeric_flag(self):
        from web.main import App

        client = App.test_client()
        token = "cached-custom-word-token"
        with patch("web.security.TokenCache.get", return_value="signed-custom-word-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            response = client.post("/api/v1/words/item/status", headers={
                "Authorization": f"Bearer {token}"}, data={
                    "ids_info": ["7_19"], "flag": "1"})

        self.assertEqual(200, response.status_code)
        self.assertEqual("check_custom_words", api_action.call_args.kwargs["cmd"])
        self.assertEqual("1", api_action.call_args.kwargs["data"]["flag"])
        self.assertEqual(["7_19"], api_action.call_args.kwargs["data"]["ids_info"])

    def test_action_maps_numeric_and_legacy_status_values(self):
        from web.action import WebAction

        helper = Mock()
        action = object.__new__(WebAction)
        action.dbhelper = helper
        with patch("web.action.WordsHelper"):
            for flag, enabled in (("1", 1), ("0", 0), ("enable", 1), ("disable", 0)):
                with self.subTest(flag=flag):
                    self.assertEqual(0, action._WebAction__check_custom_words({
                        "ids_info": ["7_19"], "flag": flag})["code"])
                    self.assertEqual("19", helper.check_custom_word.call_args.kwargs["wid"])
                    self.assertEqual(enabled, helper.check_custom_word.call_args.kwargs["enabled"])
            self.assertEqual(1, action._WebAction__check_custom_words({
                "ids_info": ["7_19"], "flag": "invalid"})["code"])


if __name__ == "__main__":
    unittest.main()
