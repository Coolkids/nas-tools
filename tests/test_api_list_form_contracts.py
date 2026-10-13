import json
import unittest
from unittest.mock import patch


class ApiListFormContractTest(unittest.TestCase):
    def test_form_lists_keep_items_instead_of_splitting_each_string(self):
        from web.main import App

        token = "cached-list-contract-token"
        cases = [
            ("/api/v1/filterrule/group/restore", {"groupids": ["1000", "1001"]},
             "groupids", ["1000", "1001"]),
            ("/api/v1/organization/history/delete", {"logids": ["17", "19"]},
             "logids", ["17", "19"]),
            ("/api/v1/organization/unknown/redo", {"flag": "unknow", "ids": ["3", "4"]},
             "ids", ["3", "4"]),
            ("/api/v1/words/item/status", {"ids_info": ["7_19"], "flag": "1"},
             "ids_info", ["7_19"]),
            ("/api/v1/message/client/update", {
                "name": "test", "type": "bark", "config": "{}",
                "switchs": ["search", "subscribe"], "interactive": "0", "enabled": "0",
            }, "switchs", ["search", "subscribe"]),
        ]
        with patch("web.security.TokenCache.get", return_value="signed-list-contract-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            client = App.test_client()
            for path, data, field, expected in cases:
                with self.subTest(path=path):
                    response = client.post(path, headers={"Authorization": f"Bearer {token}"}, data=data)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual(expected, api_action.call_args.kwargs["data"][field])

    def test_rss_article_form_entries_decode_as_json_objects(self):
        from web.main import App

        article = {"title": "functional test", "enclosure": "https://example.invalid/test.torrent"}
        token = "cached-rss-article-token"
        with patch("web.security.TokenCache.get", return_value="signed-rss-article-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            response = App.test_client().post("/api/v1/rss/item/set", headers={
                "Authorization": f"Bearer {token}"}, data={
                    "flag": "set_finished", "articles": [json.dumps(article)]})

        self.assertEqual(200, response.status_code)
        self.assertEqual([article], api_action.call_args.kwargs["data"]["articles"])

    def test_search_download_id_is_parsed_as_integer(self):
        from web.main import App

        token = "cached-search-download-id-token"
        with patch("web.security.TokenCache.get", return_value="signed-search-download-id-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            response = App.test_client().post("/api/v1/download/search", headers={
                "Authorization": f"Bearer {token}"}, data={"id": "42"})

        self.assertEqual(200, response.status_code)
        self.assertEqual(42, api_action.call_args.kwargs["data"]["id"])

    def test_recommend_list_preserves_subtype_and_search_parameters(self):
        from web.main import App

        token = "cached-recommend-list-token"
        with patch("web.security.TokenCache.get", return_value="signed-recommend-list-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            response = App.test_client().post("/api/v1/recommend/list", headers={
                "Authorization": f"Bearer {token}"}, data={
                    "type": "MOV", "subtype": "hm", "page": "2",
                    "keyword": "中文电影", "source": "tmdb",
                    "params": json.dumps({"with_genres": "28"}),
                })

        self.assertEqual(200, response.status_code)
        self.assertEqual({
            "type": "MOV", "subtype": "hm", "tmdbid": None,
            "personid": None, "week": None, "page": 2,
            "keyword": "中文电影", "source": "tmdb", "params": {"with_genres": "28"},
        }, api_action.call_args.kwargs["data"])

    def test_media_search_preserves_search_source(self):
        from web.main import App

        token = "cached-media-search-source-token"
        with patch("web.security.TokenCache.get", return_value="signed-media-search-source-token"), \
                patch("web.security.identify", return_value=(True, "test-admin")), \
                patch("web.apiv1.WebAction.api_action", return_value={"code": 0}) as api_action:
            response = App.test_client().post("/api/v1/media/search", headers={
                "Authorization": f"Bearer {token}"}, data={
                    "keyword": "中文片名", "searchtype": "tmdb"})

        self.assertEqual(200, response.status_code)
        self.assertEqual({"keyword": "中文片名", "searchtype": "tmdb"},
                         api_action.call_args.kwargs["data"])


if __name__ == "__main__":
    unittest.main()
