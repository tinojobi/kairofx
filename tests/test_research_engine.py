import unittest
import numpy as np
import pandas as pd

from research import engine


def mk(rows, pair_start="2024-01-01"):
    """rows: list of (o,h,l,c)."""
    idx = pd.date_range(pair_start, periods=len(rows), freq="h", tz="UTC")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["atr"] = 0.001
    return df


def bt(df, sig, pair="EURUSD"):
    return engine.run_backtest(df, {"signals": [sig]}, pair, "t", "p")


class EngineTests(unittest.TestCase):
    def test_enters_next_bar_open_not_signal_close(self):
        df = mk([(1.1000, 1.1005, 1.0995, 1.1002),   # signal bar close 1.1002
                 (1.1010, 1.1012, 1.1008, 1.1011),   # entry open 1.1010
                 (1.1011, 1.1050, 1.1010, 1.1040)])
        t = bt(df, {"i": 0, "direction": "long", "sl_dist": 0.0010, "rr": 2.0})[0]
        self.assertEqual(t["entry"], 1.1010)
        self.assertEqual(t["entry_time"], df.index[1])

    def test_stop_first_when_sl_and_tp_same_bar(self):
        df = mk([(1.1, 1.1, 1.1, 1.1),
                 (1.1000, 1.1030, 1.0985, 1.1010)])  # both SL (1.0990) and TP (1.1020) inside
        t = bt(df, {"i": 0, "direction": "long", "sl_dist": 0.0010, "rr": 2.0})[0]
        self.assertEqual(t["exit_reason"], "stop")
        self.assertAlmostEqual(t["r_multiple_gross"], -1.0, places=6)

    def test_costs_reduce_r_by_spread_over_stop(self):
        df = mk([(1.1, 1.1, 1.1, 1.1), (1.1, 1.1, 1.1, 1.1), (1.1, 1.1, 1.1, 1.1)])
        t = bt(df, {"i": 0, "direction": "long", "sl_dist": 0.0010, "max_bars": 1})[0]
        self.assertEqual(t["exit_reason"], "time")
        self.assertAlmostEqual(t["r_multiple_gross"] - t["r_multiple_net"], 0.8 * 0.0001 / 0.0010, places=9)

    def test_stop_exit_adds_slippage(self):
        df = mk([(1.1, 1.1, 1.1, 1.1), (1.1, 1.1, 1.0980, 1.0990)])
        t = bt(df, {"i": 0, "direction": "long", "sl_dist": 0.0010, "rr": 2.0})[0]
        self.assertAlmostEqual(t["r_multiple_gross"] - t["r_multiple_net"], (0.8 + 0.2) * 0.0001 / 0.0010)

    def test_jpy_pip_size(self):
        self.assertEqual(engine.pip_size("USDJPY"), 0.01)
        self.assertEqual(engine.pip_size("GBPJPY"), 0.01)
        self.assertEqual(engine.pip_size("EURUSD"), 0.0001)
        df = mk([(150, 150, 150, 150)] * 3)
        t = bt(df, {"i": 0, "direction": "short", "sl_dist": 0.10, "max_bars": 1}, pair="USDJPY")[0]
        self.assertAlmostEqual(t["r_multiple_gross"] - t["r_multiple_net"], 1.0 * 0.01 / 0.10)

    def test_short_tp(self):
        df = mk([(1.1, 1.1, 1.1, 1.1), (1.1, 1.1005, 1.0975, 1.098)])
        t = bt(df, {"i": 0, "direction": "short", "sl_dist": 0.0010, "rr": 2.0})[0]
        self.assertEqual(t["exit_reason"], "tp")
        self.assertAlmostEqual(t["r_multiple_gross"], 2.0, places=6)

    def test_one_position_at_a_time(self):
        df = mk([(1.1, 1.1, 1.1, 1.1)] * 10)
        out = {"signals": [{"i": 0, "direction": "long", "sl_dist": 0.001, "max_bars": 5},
                           {"i": 2, "direction": "long", "sl_dist": 0.001, "max_bars": 5}]}
        self.assertEqual(len(engine.run_backtest(df, out, "EURUSD")), 1)

    def test_bootstrap_deterministic(self):
        r = np.random.default_rng(1).normal(0.05, 1, 200)
        self.assertEqual(engine.bootstrap_ci(r, seed=7), engine.bootstrap_ci(r, seed=7))
        self.assertNotEqual(engine.bootstrap_ci(r, seed=7), engine.bootstrap_ci(r, seed=8))

    def test_metrics_basic(self):
        ts = pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC")
        tr = [{"r_multiple_net": v, "r_multiple_gross": v, "exit_time": t, "entry_time": t, "pair": "EURUSD"}
              for v, t in zip([1, -1, -1, 2], ts)]
        m = engine.metrics(tr)
        self.assertEqual(m["n"], 4)
        self.assertAlmostEqual(m["expectancy"], 0.25)
        self.assertAlmostEqual(m["profit_factor"], 1.5)
        self.assertEqual(m["longest_losing_streak"], 2)
        self.assertAlmostEqual(m["max_dd_r"], 2.0)


if __name__ == "__main__":
    unittest.main()
