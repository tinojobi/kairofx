import unittest
from unittest import mock

import pandas as pd

from backtest import BacktestTrade, run_backtest, summarize_backtest
from signals import Signal


def candle(open_, high, low, close):
    return {"open": open_, "high": high, "low": low, "close": close, "volume": 1000}


class BacktestFoundationTests(unittest.TestCase):
    def test_run_backtest_replays_candles_and_records_take_profit(self):
        candles = [candle(1.1000, 1.1005, 1.0995, 1.1000) for _ in range(60)]
        candles.append(candle(1.1000, 1.1002, 1.0998, 1.1000))
        candles.append(candle(1.1000, 1.1022, 1.0999, 1.1010))
        df = pd.DataFrame(candles)

        signal = Signal("long", "hammer", 45.0, "bullish", 0.001, 1.1000, 1.1002, 1.0998, "test")
        with mock.patch("backtest.detect_signal", return_value=signal), \
             mock.patch("backtest.calc_sl_tp", return_value=(1.0990, 1.1020)):
            result = run_backtest(df, starting_balance=10000, risk_per_trade_pct=1.0, min_history=60)

        self.assertEqual(result["trades"][0].direction, "long")
        self.assertEqual(result["trades"][0].exit_reason, "take_profit")
        self.assertGreater(result["trades"][0].realized_pl, 0)
        self.assertEqual(result["summary"]["wins"], 1)

    def test_summarize_backtest_reports_expectancy_and_drawdown(self):
        trades = [
            BacktestTrade("EUR_USD", "long", "2026-01-01", "2026-01-02", 1.1, 1.2, 1000, 100.0, "take_profit"),
            BacktestTrade("EUR_USD", "short", "2026-01-03", "2026-01-04", 1.2, 1.25, -1000, -50.0, "stop_loss"),
        ]

        summary = summarize_backtest(trades, starting_balance=1000)

        self.assertEqual(summary["trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["net_pl"], 50.0)
        self.assertEqual(summary["expectancy"], 25.0)
        self.assertGreaterEqual(summary["max_drawdown_pct"], 0)


if __name__ == "__main__":
    unittest.main()
