import unittest
from unittest.mock import Mock, patch


class UserApiContractTest(unittest.TestCase):
    def test_manage_route_passes_password_for_add_and_allows_delete_without_it(self):
        from web.main import App

        token = "cached-user-management-token"
        client = App.test_client()
        with patch("web.security.TokenCache.get", return_value="signed-user-management-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            added = client.post("/api/v1/user/manage", headers={"Authorization": f"Bearer {token}"}, data={
                "oper": "add",
                "name": "functional-user",
                "password": "test-password-with-special-!@#",
                "pris": "read",
            })
            deleted = client.post("/api/v1/user/manage", headers={"Authorization": f"Bearer {token}"}, data={
                "oper": "del",
                "name": "functional-user",
            })

        self.assertEqual(200, added.status_code)
        self.assertEqual(200, deleted.status_code)
        self.assertEqual(2, api_action.call_count)
        self.assertEqual("user_manager", api_action.call_args_list[0].kwargs["cmd"])
        self.assertEqual("test-password-with-special-!@#",
                         api_action.call_args_list[0].kwargs["data"]["password"])
        self.assertEqual("user_manager", api_action.call_args_list[1].kwargs["cmd"])
        self.assertIsNone(api_action.call_args_list[1].kwargs["data"]["password"])

    def test_add_without_password_is_rejected_before_database_write(self):
        from web.action import WebAction

        action = object.__new__(WebAction)
        action.dbhelper = Mock()
        result = action._WebAction__user_manager({"oper": "add", "name": "functional-user"})

        self.assertEqual(-1, result["code"])
        self.assertIn("必须设置密码", result["message"])
        action.dbhelper.insert_user.assert_not_called()


if __name__ == "__main__":
    unittest.main()
