import unittest
from unittest.mock import Mock


class UnknownDeleteApiContractTest(unittest.TestCase):
    def test_delete_normalizes_database_boolean_to_numeric_api_code(self):
        from web.action import WebAction

        action = object.__new__(WebAction)
        action.dbhelper = Mock()
        for db_result, expected in ((True, 0), (False, 1)):
            with self.subTest(db_result=db_result):
                action.dbhelper.delete_transfer_unknown.return_value = db_result
                result = action._WebAction__del_unknown_path({"id": "17"})
                self.assertEqual(expected, result["code"])
                self.assertEqual(expected, result["retcode"])


if __name__ == "__main__":
    unittest.main()
