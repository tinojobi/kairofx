import unittest
from unittest import mock

from broker_adapters import ClosedTrade, get_broker_adapter


class FakeBrokerAdapter:
    name = "fake"

    def __init__(self, trades):
        self.trades = trades

    def fetch_closed_trades(self, *, count=100):
        return self.trades[:count]


class BrokerAdapterTests(unittest.TestCase):
    def test_closed_trade_has_broker_neutral_shape(self):
        trade = ClosedTrade(
            broker="mt5",
            broker_trade_id="42",
            instrument="EUR_USD",
            open_time="2026-09-01T02:00:00+00:00",
            close_time="2026-09-01T03:00:00+00:00",
            direction="long",
            units=1000.0,
            open_price=1.1,
            average_close_price=1.102,
            realized_pl=20.0,
            take_profit_state="FILLED",
            stop_loss_state="CANCELLED",
        )

        self.assertEqual(trade.broker, "mt5")
        self.assertEqual(trade.direction, "long")
        self.assertEqual(trade.realized_pl, 20.0)

    def test_oanda_adapter_normalizes_raw_oanda_closed_trades(self):
        raw = {
            "id": "1123",
            "instrument": "EUR_USD",
            "openTime": "2026-08-31T07:00:42.708508106Z",
            "closeTime": "2026-08-31T14:54:09.255974905Z",
            "initialUnits": "199838",
            "price": "1.15897",
            "averageClosePrice": "1.16132",
            "realizedPL": "469.6193",
            "takeProfitOrder": {"state": "FILLED"},
            "stopLossOrder": {"state": "CANCELLED"},
        }

        adapter = get_broker_adapter("oanda")
        normalized = adapter.normalize_closed_trade(raw)

        self.assertEqual(normalized.broker, "oanda")
        self.assertEqual(normalized.broker_trade_id, "1123")
        self.assertEqual(normalized.instrument, "EUR_USD")
        self.assertEqual(normalized.direction, "long")
        self.assertEqual(normalized.units, 199838.0)
        self.assertEqual(normalized.realized_pl, 469.6193)
        self.assertEqual(normalized.take_profit_state, "FILLED")

    def test_get_broker_adapter_rejects_unimplemented_mt5_with_clear_message(self):
        with self.assertRaisesRegex(NotImplementedError, "MT5"):
            get_broker_adapter("mt5")


if __name__ == "__main__":
    unittest.main()
