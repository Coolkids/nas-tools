import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.subscribe import Subscribe
from app.utils.types import MediaType
from web.action import WebAction


class RssTaskActionContractTest(unittest.TestCase):
    def _update_task(self, data):
        helper = Mock()
        helper.update_userrss_task.return_value = True
        action = object.__new__(WebAction)
        action.dbhelper = helper
        with patch("web.action.RssChecker") as checker:
            result = action._WebAction__update_userrss_task({"uses": "D", **data})
        return result, helper, checker

    def test_rest_filterrule_field_is_saved_as_filter_rule(self):
        result, helper, checker = self._update_task({"filterrule": 17})

        self.assertEqual(0, result["code"])
        self.assertEqual(17, helper.update_userrss_task.call_args.args[0]["filter_rule"])
        checker.return_value.init_config.assert_called_once_with()

    def test_do_rule_field_remains_supported(self):
        result, helper, _ = self._update_task({"rule": 23})

        self.assertEqual(0, result["code"])
        self.assertEqual(23, helper.update_userrss_task.call_args.args[0]["filter_rule"])

    def test_rest_subscription_detail_accepts_type_and_legacy_rsstype(self):
        subscribe = Mock()
        subscribe.get_subscribe_movies.return_value = {"17": {"id": 17}}

        with patch("web.action.Subscribe", return_value=subscribe):
            result = WebAction._WebAction__rss_detail({"rssid": 17, "type": "MOV"})

        self.assertEqual(0, result["code"])
        self.assertEqual("MOV", result["detail"]["type"])
        subscribe.get_subscribe_movies.assert_called_once_with(rid=17)
        subscribe.get_subscribe_tvs.assert_not_called()

        subscribe.reset_mock()
        subscribe.get_subscribe_movies.return_value = {"17": {"id": 17}}
        with patch("web.action.Subscribe", return_value=subscribe):
            legacy_result = WebAction._WebAction__rss_detail({"rssid": 17, "rsstype": "MOV"})

        self.assertEqual(0, legacy_result["code"])
        subscribe.get_subscribe_movies.assert_called_once_with(rid=17)

    def test_successful_tv_update_returns_recreated_subscription_id(self):
        helper = Mock()
        helper.get_rss_tv_id.return_value = 91
        action = object.__new__(WebAction)
        action.dbhelper = helper
        media_info = SimpleNamespace(year=2025, tmdb_id=None,
                                     get_season_string=lambda: "S02")
        subscribe = Mock()
        subscribe.add_rss_subscribe.return_value = (0, "添加订阅成功", media_info)

        with patch("web.action.Subscribe", return_value=subscribe):
            result = action._WebAction__add_rss_media({
                "type": "TV", "name": "Example", "year": "2025",
                "season": 2, "rssid": 17,
            })

        self.assertEqual(91, result["rssid"])
        helper.get_rss_tv_id.assert_called_once_with(
            title="Example", year=2025, season="S02", tmdbid=None)

    def test_fuzzy_subscriptions_keep_the_requested_year(self):
        for media_type, method in ((MediaType.MOVIE, "insert_rss_movie"),
                                   (MediaType.TV, "insert_rss_tv")):
            with self.subTest(media_type=media_type):
                helper = Mock()
                getattr(helper, method).return_value = 0
                media_info = SimpleNamespace(title="", year=None, type=None,
                                             begin_season=None, tmdb_id=None)
                subscribe = object.__new__(Subscribe)
                subscribe.dbhelper = helper

                with patch("app.subscribe.MetaInfo", return_value=media_info):
                    result = subscribe.add_rss_subscribe(
                        mtype=media_type, name="Example", year="2023",
                        season=2 if media_type == MediaType.TV else None,
                        fuzzy_match=True)

                self.assertEqual(0, result[0])
                self.assertEqual(2023, media_info.year)
                getattr(helper, method).assert_called_once()


if __name__ == "__main__":
    unittest.main()
