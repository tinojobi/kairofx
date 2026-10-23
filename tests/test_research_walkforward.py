import unittest
from unittest import mock

import numpy as np
import pandas as pd

from research import run, strategies


def synth(n=1500, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-02", periods=n, freq="h", tz="UTC")
    c = 1.10 + np.cumsum(rng.normal(0, 0.0007, n))
    o = np.roll(c, 1)
    o[0] = c[0]
    h = np.maximum(o, c) + rng.uniform(0, 0.0008, n)
    l = np.minimum(o, c) - rng.uniform(0, 0.0008, n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


class WalkForwardTests(unittest.TestCase):
    def test_split_ordering(self):
        idx = synth().index
        t1, t2 = run.split_bounds(idx)
        self.assertLess(idx[0], t1)
        self.assertLess(t1, t2)
        self.assertLess(t2, idx[-1])
        self.assertEqual(run.assign_split(idx[0], (t1, t2)), "train")
        self.assertEqual(run.assign_split(t1, (t1, t2)), "validate")
        self.assertEqual(run.assign_split(t2, (t1, t2)), "test")
        self.assertAlmostEqual((idx < t1).mean(), 0.5, places=2)
        self.assertAlmostEqual((idx >= t2).mean(), 0.25, places=2)

    def test_selection_never_sees_validate_or_test(self):
        df = synth()
        bounds = run.split_bounds(df.index)
        seen = []
        real = run.select_best

        def spy(train_by_cfg, *a, **k):
            seen.append(train_by_cfg)
            return real(train_by_cfg, *a, **k)

        with mock.patch("research.run.data.get_pair", return_value=df):
            res, _ = run.collect(["EURUSD"])
        with mock.patch("research.run.select_best", spy):
            run.build(res, {})
        self.assertTrue(seen)
        total = 0
        for d in seen:
            for trades in d.values():
                for t in trades:
                    total += 1
                    self.assertLess(t["entry_time"], bounds[0])
        self.assertGreater(total, 0)

    def test_select_best_picks_highest_train_expectancy_with_min_trades(self):
        mk = lambda v, n: [{"r_multiple_net": v}] * n
        pick = run.select_best({"a": mk(0.5, 10), "b": mk(0.1, 70), "c": mk(0.2, 80)})
        self.assertEqual(pick, "c")
        self.assertIsNone(run.select_best({"a": mk(0.5, 10)}))

    def test_verdict_requires_both_splits_positive(self):
        mk = lambda vals: [{"r_multiple_net": v, "r_multiple_gross": v, "exit_time": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=i), "entry_time": pd.Timestamp("2024-01-01", tz="UTC"), "pair": "X"} for i, v in enumerate(vals)]
        good = mk([1.0, 1.0, -0.2] * 40)
        self.assertEqual(run.verdict(good, good)[0], "candidate edge")
        bad = mk([-0.5] * 60)
        self.assertEqual(run.verdict(good, bad)[0], "no demonstrated edge")
        self.assertEqual(run.verdict(good[:10], good[:10])[0], "no demonstrated edge")  # < 100 trades


class CausalityTests(unittest.TestCase):
    def test_signals_do_not_change_when_future_is_truncated(self):
        df = synth(1200, seed=3)
        total_sigs = 0
        for name, (fn, grid) in strategies.STRATEGIES.items():
            p = grid[0]
            full = fn(df, p)["signals"]
            total_sigs += len(full)
            for cut in (400, 650, 900, 1100):
                part = fn(df.iloc[:cut + 1], p)["signals"]
                a = [s for s in full if s["i"] <= cut]
                self.assertEqual(a, part, f"{name} not causal at cut {cut}")
        self.assertGreater(total_sigs, 10)

    def test_all_grid_configs_causal_smoke(self):
        df = synth(900, seed=5)
        for name, (fn, grid) in strategies.STRATEGIES.items():
            for p in grid:
                full = fn(df, p)["signals"]
                part = fn(df.iloc[:701], p)["signals"]
                self.assertEqual([s for s in full if s["i"] <= 700], part, name)

    def test_grids_small(self):
        for name, (fn, grid) in strategies.STRATEGIES.items():
            self.assertLessEqual(len(grid), 8, name)


if __name__ == "__main__":
    unittest.main()
