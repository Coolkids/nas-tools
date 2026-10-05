import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from requests import Response
from requests.exceptions import Timeout

from app.network import NetworkTest, _TARGETS
from web.action import WebAction


def response(status=200, payload=None, body=b""):
    result = Response()
    result.status_code = status
    result.reason = {200: "OK", 204: "No Content", 302: "Found", 403: "Forbidden",
                     503: "Service Unavailable"}.get(status, "")
    result._content = json.dumps(payload).encode() if payload is not None else body
    return result


class NetworkTestRequestTest(TestCase):
    def setUp(self):
        config = SimpleNamespace(
            get_config=lambda section: {"rmt_tmdbkey": "first-secret;second-secret"},
            get_proxies=lambda: {"https": "http://proxy.local:7890"})
        config_patch = patch("app.network.Config", return_value=config)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.tester = NetworkTest()
        self.session = MagicMock()
        session_patch = patch("app.network.requests.Session")
        session_factory = session_patch.start()
        session_factory.return_value.__enter__.return_value = self.session
        self.addCleanup(session_patch.stop)

    def test_forbidden_response_is_not_mistaken_for_no_response(self):
        self.session.get.return_value = response(403)

        result = self.tester._check(_TARGETS[0])

        self.assertFalse(result["res"])
        self.assertEqual(403, result["status_code"])
        self.assertEqual("HTTP 403 Forbidden", result["reason"])
        self.session.get.assert_called_once()

    def test_empty_success_response_is_reachable(self):
        self.session.get.return_value = response(204)

        result = self.tester._check(_TARGETS[0])

        self.assertTrue(result["res"])
        self.assertEqual("", result["reason"])

    def test_tmdb_uses_real_authenticated_api_and_minimal_headers(self):
        self.session.get.return_value = response(payload={"images": {"base_url": "https://image.tmdb.org"}})

        result = self.tester._check(_TARGETS[1])

        self.assertTrue(result["res"])
        args, kwargs = self.session.get.call_args
        self.assertEqual(("https://api.themoviedb.org/3/configuration",), args)
        self.assertEqual({"api_key": "first-secret"}, kwargs["params"])
        self.assertNotIn("Content-Type", kwargs["headers"])
        self.assertEqual("NAS-Tools/network-test", kwargs["headers"]["User-Agent"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual({"https": "http://proxy.local:7890"}, kwargs["proxies"])
        self.assertNotIn("first-secret", json.dumps(result))

    def test_api_html_success_page_is_not_a_successful_api_request(self):
        self.session.get.return_value = response(body=b"<html>challenge</html>")

        result = self.tester._check(_TARGETS[1])

        self.assertFalse(result["res"])
        self.assertEqual(200, result["status_code"])
        self.assertIn("API JSON", result["reason"])

    def test_fanart_validates_its_own_response_fields(self):
        self.session.get.return_value = response(payload={"tmdb_id": "550", "movieposter": []})

        result = self.tester._check(_TARGETS[4])

        self.assertTrue(result["res"])

    def test_api_redirect_to_local_address_is_not_followed_or_accepted(self):
        redirect = response(302)
        redirect.headers["Location"] = "http://127.0.0.1/private"
        self.session.get.return_value = redirect

        result = self.tester._check(_TARGETS[1])

        self.assertFalse(result["res"])
        self.assertEqual("HTTP 302 Found", result["reason"])
        self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])
        self.session.get.assert_called_once()

    def test_service_error_keeps_http_status_and_reason(self):
        self.session.get.return_value = response(503)

        result = self.tester._check(_TARGETS[0])

        self.assertFalse(result["res"])
        self.assertEqual("HTTP 503 Service Unavailable", result["reason"])

    def test_exception_reason_hides_api_keys_and_proxy_credentials(self):
        self.session.get.side_effect = Timeout(
            "https://api.themoviedb.org/3/configuration?api_key=first-secret "
            "via socks5h://user:password@proxy.local timed out")

        result = self.tester._check(_TARGETS[1])

        self.assertFalse(result["res"])
        self.assertIsNone(result["status_code"])
        self.assertIn("Timeout", result["reason"])
        self.assertNotIn("first-secret", result["reason"])
        self.assertNotIn("password", result["reason"])
        self.session.get.assert_called_once()

    def test_batch_requests_each_fixed_target_once_and_keeps_other_results_after_timeout(self):
        def get(url, **kwargs):
            if url == "https://www.opensubtitles.org":
                raise Timeout("read timed out")
            return response(payload={"images": {}, "tmdb_id": "550"})

        self.session.get.side_effect = get

        results = self.tester.run()["results"]

        self.assertEqual([target.name for target in _TARGETS], [item["target"] for item in results])
        self.assertEqual(len(_TARGETS), self.session.get.call_count)
        self.assertEqual({target.url for target in _TARGETS},
                         {call.args[0] for call in self.session.get.call_args_list})
        self.assertTrue(all(item["res"] for item in results[:-1]))
        self.assertFalse(results[-1]["res"])
        self.assertIn("Timeout", results[-1]["reason"])


class NetworkTestActionTest(TestCase):
    def test_custom_urls_are_rejected_before_any_request(self):
        with patch("web.action.NetworkTest") as network_test:
            for payload in ("127.0.0.1", "169.254.169.254/latest/meta-data/",
                            {"url": "https://example.com"}, {"target": "[::1]"}):
                with self.subTest(payload=payload):
                    result = WebAction._WebAction__net_test(payload)
                    self.assertEqual(-1, result["code"])
            network_test.assert_not_called()

    def test_empty_request_triggers_one_batch(self):
        expected = {"results": [{"target": "example", "res": True}]}
        with patch("web.action.NetworkTest") as network_test:
            network_test.return_value.run.return_value = expected

            result = WebAction._WebAction__net_test({})

        self.assertEqual(expected, result)
        network_test.return_value.run.assert_called_once_with()

    def test_rest_resource_does_not_forward_url_parameters(self):
        from flask import Flask
        from web.apiv1 import ServiceNetworkTest

        app = Flask(__name__)
        with app.test_request_context("/network/test", method="POST", data={"url": "http://127.0.0.1"}), \
                patch("web.apiv1.WebAction") as action:
            ServiceNetworkTest().post()

        action.return_value.api_action.assert_called_once_with(cmd="net_test")
