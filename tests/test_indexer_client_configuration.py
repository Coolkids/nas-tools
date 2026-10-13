import unittest
from unittest.mock import patch

from app.indexer.client.jackett import Jackett
from app.indexer.client.prowlarr import Prowlarr


class IndexerClientConfigurationTest(unittest.TestCase):
    def test_unconfigured_jackett_returns_no_indexers_without_http_request(self):
        client = object.__new__(Jackett)
        client._client_config = {"host": "", "api_key": "", "password": ""}
        client.init_config()

        with patch("app.indexer.client.jackett.RequestUtils") as request_utils:
            self.assertEqual([], client.get_indexers())

        request_utils.assert_not_called()

    def test_unconfigured_prowlarr_returns_no_indexers_without_http_request(self):
        client = object.__new__(Prowlarr)
        client._client_config = {"host": "", "api_key": ""}
        client.init_config()

        with patch("app.indexer.client.prowlarr.RequestUtils") as request_utils:
            self.assertEqual([], client.get_indexers())

        request_utils.assert_not_called()


if __name__ == "__main__":
    unittest.main()
