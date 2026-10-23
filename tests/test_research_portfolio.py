import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from research import portfolio as pf


def synth_df(n=1200, seed=1, drift=0.0, vol=0.007, **kw):
    raw = pf.make_synthetic("EUR_USD", n=n, seed=seed, drift=drift, vol=vol, **kw)
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "EUR_USD_D.csv"
        raw.to_csv(p, index=False)
        return pf.load_instrument(p)


def cfg_by_id(cid):
    return next(c for c in pf.build_configs(1) if c["id"] == cid)


def trade(inst, group, entry, exit_, r=1.0):
    return {"instrument": inst, "group": group, "entry_time": pd.Timestamp(entry, tz="UTC"),
            "exit_time": pd.Timestamp(exit_, tz="UTC"), "r_net": r, "r_gross": r, "mae_r": 0.5}


class EntryAndCostTests(unittest.TestCase):
    def test_entry_at_next_open_and_cost_from_trailing_median(self):
        df = synth_df(600, seed=3, drift=0.08)
        df["spread"] = 0.001
        df.iloc[300:, df.columns.get_loc("spread")] = 0.01  # spread jumps late
        sp, _ = pf.trailing_spread(df)
        # causal: the jump at bar 300 does not affect the estimate at bar 300
        self.assertAlmostEqual(sp[300], 0.001)
        self.assertAlmostEqual(sp[599], 0.01)
        cfg = cfg_by_id("donchian_N20_stop3")
        sig = pf.make_signals(df, cfg)
        trades = pf.simulate_instrument("EUR_USD", df, sig, pf.atr(df).to_numpy(float), sp)
        self.assertGreater(len(trades), 3)
        for t in trades:
            e = t["entry_i"]
            self.assertEqual(t["entry"], df["open"].iloc[e])
            d = 1 if t["direction"] == "long" else -1
            self.assertTrue((sig["entry_long"][e - 1] and d == 1) or (sig["entry_short"][e - 1] and d == -1))
            expected_cost = (sp[e] + (0.5 * sp[t["exit_i"]] if t["exit_reason"] == "stop" else 0)) / t["risk_dist"]
            self.assertAlmostEqual(t["r_cost"], expected_cost)
            self.assertAlmostEqual(t["r_net"], t["r_gross"] - t["r_cost"])

    def test_missing_bid_ask_fallback_and_skip(self):
        df = synth_df(600, seed=4)
        df.iloc[:50, df.columns.get_loc("spread")] = np.nan
        sp, fb = pf.trailing_spread(df)
        self.assertEqual(fb, 50)  # bars 1..50 have no prior observation
        med = df["spread"].median()
        self.assertAlmostEqual(sp[10], med)
        self.assertAlmostEqual(sp[100], df["spread"].iloc[:100].median())
        df2 = df.copy()
        df2["spread"] = np.nan
        self.assertEqual(pf.trailing_spread(df2)[0], None)
        res, info = pf.run_all_configs({"EUR_USD": df2}, pf.build_configs(1)[:1])
        self.assertIn("EUR_USD", info["skipped"])
        self.assertEqual(res[pf.build_configs(1)[0]["id"]]["trades"], [])

    def test_load_instrument_nan_bidask(self):
        raw = pf.make_synthetic("X", n=500, seed=2)
        raw["bid_open"] = np.nan
        raw["ask_open"] = np.nan
        with tempfile.TemporaryDirectory() as d:
            raw.to_csv(Path(d) / "X_D.csv", index=False)
            df = pf.load_instrument(Path(d) / "X_D.csv")
        self.assertTrue(df["spread"].isna().all())


class NoLookaheadTests(unittest.TestCase):
    def test_signals_unchanged_by_truncation(self):
        df = synth_df(900, seed=5, drift=0.03, regime_len=200)
        for cfg in pf.build_configs(1):
            full = pf.make_signals(df, cfg)
            for i in (260, 400, 463, 700):
                part = pf.make_signals(df.iloc[:i + 1], cfg)
                for k in ("entry_long", "entry_short", "exit_long", "exit_short"):
                    self.assertEqual(part[k][i], full[k][i], (cfg["id"], k, i))

    def test_trades_unchanged_by_future_data(self):
        df = synth_df(900, seed=6, drift=0.03, regime_len=200)
        cfg = cfg_by_id("donchian_N20_stop3")
        sp, _ = pf.trailing_spread(df)
        a = pf.atr(df).to_numpy(float)
        full = pf.simulate_instrument("X", df, pf.make_signals(df, cfg), a, sp)
        cut = df.iloc[:600]
        part = pf.simulate_instrument("X", cut, pf.make_signals(cut, cfg), a[:600], sp[:600])
        closed = [t for t in part if t["exit_reason"] != "eod"]
        self.assertGreater(len(closed), 2)
        for t in closed:
            m = [u for u in full if u["entry_i"] == t["entry_i"]][0]
            self.assertEqual(m["exit_i"], t["exit_i"])
            self.assertAlmostEqual(m["r_net"], t["r_net"])


class PortfolioCapTests(unittest.TestCase):
    def test_total_and_group_caps(self):
        ts = [trade("I%d" % i, "indices", "2020-01-01", "2020-02-01") for i in range(10)]
        res = pf.run_portfolio(ts, max_pos=12, group_cap=4)
        self.assertEqual(len(res["trades"]), 4)
        self.assertEqual(res["rejected"], 6)
        groups = ["indices", "metals", "energy", "fx_cross", "fx_usd_base"]
        ts = [trade("I%d_%s" % (i, g), g, "2020-01-01", "2020-02-01") for g in groups for i in range(4)]
        res = pf.run_portfolio(ts, max_pos=12, group_cap=4)
        self.assertEqual(len(res["trades"]), 12)
        for g in groups:
            self.assertLessEqual(sum(t["group"] == g for t in res["trades"]), 4)

    def test_slot_freed_on_exit_and_equity_compounds(self):
        ts = [trade("A", "metals", "2020-01-01", "2020-01-10", r=2.0),
              trade("B", "metals", "2020-01-10", "2020-01-20", r=2.0)]
        res = pf.run_portfolio(ts, max_pos=1, group_cap=4, risk_pct=0.01)
        self.assertEqual(len(res["trades"]), 2)
        self.assertAlmostEqual(res["steps"][-1][1], 1.02 * 1.02)

    def test_caps_hold_on_random_overlap(self):
        rng = np.random.default_rng(0)
        ts = []
        for i in range(300):
            s = pd.Timestamp("2020-01-01") + pd.Timedelta(days=int(rng.integers(0, 300)))
            ts.append(trade("I%d" % (i % 25), ["metals", "indices", "fx_cross"][i % 3], s,
                            s + pd.Timedelta(days=int(rng.integers(1, 60)))))
        res = pf.run_portfolio(ts, max_pos=12, group_cap=4)
        ev = [(t["entry_time"], 1, t) for t in res["trades"]] + [(t["exit_time"], 0, t) for t in res["trades"]]
        ev.sort(key=lambda x: (x[0], x[1]))
        open_, g = 0, {}
        for _, kind, t in ev:
            d = 1 if kind else -1
            open_ += d
            g[t["group"]] = g.get(t["group"], 0) + d
            self.assertLessEqual(open_, 12)
            self.assertLessEqual(g[t["group"]], 4)


class SelectionTests(unittest.TestCase):
    def test_selection_function_only_sees_train(self):
        seen = []

        def spy(train_by, min_trades):
            for trades in train_by.values():
                seen.extend(t["exit_time"] for t in trades)
            return pf.select_config(train_by, min_trades)

        data = _frames(4200, 4)
        out = pf.walk_forward(data, "D", selector=spy, min_train=5, n_boot=50)
        self.assertTrue(seen)
        self.assertLessEqual(max(pd.Timestamp(t).tz_localize(None) if pd.Timestamp(t).tzinfo is None
                                 else pd.Timestamp(t).tz_convert("UTC").tz_localize(None) for t in seen),
                             pf.TRAIN_END + pd.Timedelta(days=1))
        self.assertEqual(out["n_configs"], 13)

    def test_selection_invariant_to_post_train_data(self):
        data = _frames(4200, 4)
        mutated = {}
        for k, df in data.items():
            m = df.copy()
            post = m.index > pd.Timestamp("2015-01-05", tz="UTC")
            m.loc[post, ["open", "high", "low", "close"]] *= 0.5 + np.linspace(0, 3, post.sum())[:, None]
            mutated[k] = m
        a = pf.walk_forward(data, "D", min_train=5, n_boot=50)
        b = pf.walk_forward(mutated, "D", min_train=5, n_boot=50)
        for s in pf.STRATEGIES:
            self.assertEqual(a["strategies"][s]["chosen"], b["strategies"][s]["chosen"])
        self.assertEqual([c["n"] for c in a["all_configs"]], [c["n"] for c in b["all_configs"]])

    def test_select_config_rule(self):
        tr = lambda r, n: [{"r_net": r}] * n
        self.assertEqual(pf.select_config({"a": tr(0.5, 79), "b": tr(0.1, 80), "c": tr(0.2, 200)}), "c")
        self.assertIsNone(pf.select_config({"a": tr(0.5, 10)}))


def _frames(n, k):
    out = {}
    with tempfile.TemporaryDirectory() as d:
        for i, (inst, drift) in enumerate(pf.SELFTEST_INSTRUMENTS[:k]):
            pf.make_synthetic(inst, n=n, seed=50 + i, drift=drift, regime_len=400 if drift else 0).to_csv(
                Path(d) / ("%s_D.csv" % inst), index=False)
        out, _, _ = pf.load_dir(d, "D")
    return out


class BootstrapAndEdgeTests(unittest.TestCase):
    def test_bootstrap_deterministic(self):
        ts = [trade("A", "metals", "2020-%02d-01" % (1 + i % 12), "2020-%02d-15" % (1 + i % 12),
                    r=float(np.sin(i))) for i in range(60)]
        a = pf.monthly_block_bootstrap(ts, 500, seed=7)
        self.assertEqual(a, pf.monthly_block_bootstrap(ts, 500, seed=7))
        self.assertNotEqual(a, pf.monthly_block_bootstrap(ts, 500, seed=8))
        self.assertLess(a[0], a[1])

    def _pooled(self, drift, seeds):
        gross, net, n = [], [], 0
        for s in seeds:
            df = synth_df(3000, seed=s, drift=drift)
            sp, _ = pf.trailing_spread(df)
            t = pf.simulate_instrument("X", df, pf.make_signals(df, cfg_by_id("donchian_N55_stop4")),
                                       pf.atr(df).to_numpy(float), sp)
            gross += [x["r_gross"] for x in t]
            net += [x["r_net"] for x in t]
        return np.mean(gross), np.mean(net), len(net)

    def test_trending_walk_profitable_gross(self):
        g, n, cnt = self._pooled(0.08, range(10, 18))
        self.assertGreater(cnt, 30)
        self.assertGreater(g, 0.3)
        self.assertGreater(n, 0.2)

    def test_driftless_walk_near_zero(self):
        g, n, cnt = self._pooled(0.0, range(20, 40))
        self.assertGreater(cnt, 100)
        self.assertLess(abs(g), 0.25)
        self.assertLess(n, 0.25)

    def test_verdict_rule(self):
        ok = {"n": 100, "expectancy": 0.2, "sharpe": 0.5}
        comb = {"n": 200, "ci_low": 0.01}
        self.assertEqual(pf.verdict(ok, ok, comb)[0], "candidate edge")
        self.assertEqual(pf.verdict(ok, {**ok, "sharpe": 0.2}, comb)[0], "no demonstrated edge")
        self.assertEqual(pf.verdict(ok, ok, {"n": 149, "ci_low": 0.01})[0], "no demonstrated edge")
        self.assertEqual(pf.verdict(ok, ok, {"n": 200, "ci_low": -0.01})[0], "no demonstrated edge")


class SelftestTests(unittest.TestCase):
    def test_selftest_runs_and_writes_nothing_to_reports(self):
        reports = Path(pf.ROOT) / "reports"
        before = set(reports.glob("*"))
        with mock.patch("builtins.print"):
            rc = pf.main(["--selftest-synthetic"])
        self.assertEqual(rc, 0)
        self.assertEqual(before, set(reports.glob("*")))


if __name__ == "__main__":
    unittest.main()
