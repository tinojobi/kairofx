"""Tests for the offline strategy-variant sweeper (variants.py).

Follows repo conventions: unittest + unittest.mock, pandas DataFrames for
candles, direct imports from the modules under test.

Scope: measurement-only tool; never places trades. It must:
  - apply per-variant config overrides around run_backtest
  - restore config exactly as found (even on failure)
  - keep a baseline variant identical to a plain run_backtest call
  - rank variants and render a readable report
"""

import unittest
from unittest import mock

import pandas as pd

import config
import variants
from backtest import run_backtest, serialize_backtest
from signals import Signal


def make_candles(n=60, start=1.0000, step=0.0002, seed_close=None):
    """Deterministic, non-flat candle frame matching bot shape (close-based)."""
    if seed_close is None:
        close = [start + step * i for i in range(n)]
    else:
        close = list(seed_close)
    return pd.DataFrame({
        "time": pd.date_range("2026-08-01", periods=n, freq="1h"),
        "open": close,
        "high": [c + 0.0001 for c in close],
        "low": [c - 0.0001 for c in close],
        "close": close,
        "volume": [1000] * n,
    })


def fixed_signal(direction="long", last_close=1.01, atr=0.0005):
    return Signal(
        direction=direction,
        pattern="bullish_engulfing",
        rsi=40.0,
        macd_state="bullish",
        atr=atr,
        last_close=last_close,
        last_high=last_close + 0.0002,
        last_low=last_close - 0.0002,
        notes="test",
    )


class VariantSweeperTests(unittest.TestCase):

    def setUp(self):
        self.candles = make_candles()

    # ------------------------------------------------------------------ #
    # 1. Overrides are applied around run_backtest and config is restored #
    # ------------------------------------------------------------------ #
    def test_run_variant_applies_overrides_and_restores_config(self):
        before = {k: getattr(config, k) for k in variants.SWEEP_KEYS}

        with mock.patch("backtest.detect_signal", return_value=fixed_signal()), \
             mock.patch("backtest.calc_sl_tp") as calc:
            calc.return_value = (1.008, 1.016)  # (sl, tp)
            variants.run_variant(
                self.candles,
                overrides={"RSI_LONG_MAX": 50, "REQUIRE_MACD_ALIGNMENT": False},
                starting_balance=10000.0,
                min_history=5,
            )

        after = {k: getattr(config, k) for k in variants.SWEEP_KEYS}
        self.assertEqual(before, after, "config must be restored after run_variant")

    def test_config_restored_even_when_backtest_raises(self):
        before = {k: getattr(config, k) for k in variants.SWEEP_KEYS}
        with mock.patch("backtest.detect_signal", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                variants.run_variant(
                    self.candles,
                    overrides={"RSI_LONG_MAX": 50},
                    min_history=5,
                )
        after = {k: getattr(config, k) for k in variants.SWEEP_KEYS}
        self.assertEqual(before, after, "config must be restored even on failure")

    # ------------------------------------------------------------------ #
    # 2. Baseline variant matches a plain run_backtest call               #
    # ------------------------------------------------------------------ #
    def test_baseline_matches_direct_run_backtest(self):
        with mock.patch("backtest.detect_signal", return_value=fixed_signal()), \
             mock.patch("backtest.calc_sl_tp") as calc:
            calc.return_value = (1.008, 1.016)
            direct = serialize_backtest(
                run_backtest(self.candles, starting_balance=10000.0, min_history=5)
            )
        with mock.patch("backtest.detect_signal", return_value=fixed_signal()), \
             mock.patch("backtest.calc_sl_tp") as calc:
            calc.return_value = (1.008, 1.016)
            swept = variants.run_variant(
                self.candles,
                overrides={},
                starting_balance=10000.0,
                min_history=5,
            )

        self.assertEqual(direct["summary"], swept["summary"])
        self.assertEqual(direct["trades"], swept["trades"])

    # ------------------------------------------------------------------ #
    # 3. Sweep ranks by net P&L and labels each variant                   #
    # ------------------------------------------------------------------ #
    def test_sweep_ranks_by_net_pl_descending(self):
        # Inject a fake runner so ranking is deterministic and independent
        # of trade dynamics on synthetic candles.
        def fake_runner(candles, overrides=None, **kw):
            rr = overrides.get("REWARD_TO_RISK", 2.0)
            net = 100.0 if rr > 2.0 else 40.0
            return {
                "summary": {
                    "trades": 3, "wins": 2, "losses": 1,
                    "win_rate_pct": 66.7, "net_pl": net,
                    "expectancy": net / 3, "max_drawdown_pct": 1.0,
                    "ending_balance": 10000.0 + net,
                },
                "trades": [],
            }

        results = variants.sweep_variants(
            self.candles,
            grid=[
                {"label": "A", "overrides": {}},
                {"label": "B", "overrides": {"REWARD_TO_RISK": 2.5}},
            ],
            runner=fake_runner,
            min_trades=1,
        )
        self.assertEqual(len(results), 2)
        labels = [r["label"] for r in results]
        self.assertEqual(labels, ["B", "A"])
        self.assertGreater(results[0]["net_pl"], results[1]["net_pl"])
        for r in results:
            self.assertIn("label", r)
            self.assertIn("net_pl", r)
            self.assertIn("trades", r)

    def test_sweep_drops_variants_below_min_trades(self):
        def fake_runner(candles, overrides=None, **kw):
            trades = 0 if overrides.get("RSI_LONG_MAX", 47) == 50 else 4
            return {
                "summary": {"trades": trades, "wins": 0, "losses": 0,
                            "win_rate_pct": 0.0, "net_pl": 0.0,
                            "expectancy": 0.0, "max_drawdown_pct": 0.0,
                            "ending_balance": 10000.0},
                "trades": [],
            }

        results = variants.sweep_variants(
            self.candles,
            grid=[
                {"label": "zero", "overrides": {"RSI_LONG_MAX": 50}},
                {"label": "four", "overrides": {}},
            ],
            runner=fake_runner,
            min_trades=2,
        )
        self.assertEqual([r["label"] for r in results], ["four"])

    # ------------------------------------------------------------------ #
    # 4. Markdown render contains the essentials                         #
    # ------------------------------------------------------------------ #
    def test_render_markdown_includes_labels_and_headers(self):
        results = [
            {"label": "baseline", "net_pl": 55.5, "trades": 3, "wins": 2,
             "losses": 1, "win_rate_pct": 66.7, "expectancy": 18.5,
             "max_drawdown_pct": 1.2, "ending_balance": 10055.5},
        ]
        md = variants.render_markdown(results, meta={"candles": 60})
        for token in ("baseline", "net_pl", "win", "trades", "60"):
            self.assertIn(token, md, f"missing token: {token}")


if __name__ == "__main__":
    unittest.main()