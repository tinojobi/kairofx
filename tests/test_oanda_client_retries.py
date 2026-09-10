import unittest
from unittest import mock

import config
from oanda_client import OandaClient


class FakeAPI:
    def __init__(self, responses):
        self._responses = responses
        self.calls = 0

    def request(self, req):
        self.calls += 1
        if not self._responses:
            raise RuntimeError("no more fake responses configured")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        req.response = item
        return item


class FakeEndpoint:
    def __init__(self, responses):
        self._responses = list(responses)
        self.instances = []

    def build_api(self):
        api = FakeAPI(self._responses)
        self.instances.append(api)
        return api


class OandaClientRetryTests(unittest.TestCase):
    def setUp(self):
        self.token_patch = mock.patch.object(config, "OANDA_API_TOKEN", "test-token")
        self.account_patch = mock.patch.object(config, "OANDA_ACCOUNT_ID", "test-account")
        self.env_patch = mock.patch.object(config, "OANDA_ENV", "practice")
        self.token_patch.start()
        self.account_patch.start()
        self.env_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.addCleanup(self.account_patch.stop)
        self.addCleanup(self.env_patch.stop)

    def make_client(self, responses):
        endpoint = FakeEndpoint(responses)
        with mock.patch("oanda_client.API", side_effect=lambda **kwargs: endpoint.build_api()):
            client = OandaClient()
        client._build_api = endpoint.build_api
        client._test_endpoint = endpoint
        return client

    def test_account_summary_retries_after_transient_failure(self):
        client = self.make_client([
            RuntimeError('{"errorMessage":"Insufficient authorization to perform request."}'),
            {"account": {
                "balance": "1000.0",
                "NAV": "1005.5",
                "unrealizedPL": "5.5",
                "openPositionCount": "1",
                "currency": "USD",
            }},
        ])

        with mock.patch("oanda_client.time.sleep") as sleep_mock:
            summary = client.get_account_summary()

        self.assertEqual(summary["balance"], 1000.0)
        self.assertEqual(summary["nav"], 1005.5)
        self.assertEqual(summary["open_position_count"], 1)
        self.assertEqual(sum(api.calls for api in client._test_endpoint.instances), 2)
        self.assertEqual(len(client._test_endpoint.instances), 2)
        sleep_mock.assert_called_once_with(5)

    def test_get_current_price_retries_and_returns_bid_ask(self):
        client = self.make_client([
            RuntimeError("temporary pricing failure"),
            {"prices": [{"bids": [{"price": "1.14400"}], "asks": [{"price": "1.14420"}]}]},
        ])

        with mock.patch("oanda_client.time.sleep") as sleep_mock:
            price = client.get_current_price("EUR_USD")

        self.assertEqual(price, {"bid": 1.144, "ask": 1.1442})
        self.assertEqual(sum(api.calls for api in client._test_endpoint.instances), 2)
        self.assertEqual(len(client._test_endpoint.instances), 1)
        sleep_mock.assert_called_once_with(5)

    def test_open_positions_raises_after_exhausting_retries(self):
        client = self.make_client([
            RuntimeError("fail-1"),
            RuntimeError("fail-2"),
            RuntimeError("fail-3"),
        ])

        with mock.patch("oanda_client.time.sleep") as sleep_mock:
            with self.assertRaises(RuntimeError):
                client.get_open_positions()

        self.assertEqual(sum(api.calls for api in client._test_endpoint.instances), 3)
        self.assertEqual(len(client._test_endpoint.instances), 1)
        self.assertEqual(sleep_mock.call_args_list, [mock.call(5), mock.call(10)])


    def test_auth_like_failure_rebuilds_api_before_retry(self):
        client = self.make_client([
            RuntimeError('{"errorMessage":"Insufficient authorization to perform request."}'),
            {"account": {
                "balance": "1000.0",
                "NAV": "1001.0",
                "unrealizedPL": "1.0",
                "openPositionCount": "0",
                "currency": "USD",
            }},
        ])

        with mock.patch("oanda_client.time.sleep") as sleep_mock:
            summary = client.get_account_summary()

        self.assertEqual(summary["balance"], 1000.0)
        self.assertEqual(len(client._test_endpoint.instances), 2)
        self.assertEqual(client._test_endpoint.instances[0].calls, 1)
        self.assertEqual(client._test_endpoint.instances[1].calls, 1)
        sleep_mock.assert_called_once_with(5)


if __name__ == "__main__":
    unittest.main()
