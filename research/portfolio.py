"""Long-horizon, multi-market trend / momentum research with a walk-forward protocol.

Reads OANDA history exported by research.oanda_export (research/data/oanda/{INST}_{GRAN}.csv).
OFFLINE ONLY: no network, no OANDA calls, no secrets.

Conventions
- Signals are computed at bar close from causal indicators; entry at the NEXT bar open.
- open/high/low/close are MID. Cost per round trip = trailing median (previous 250 bars,
  causal) of (ask_open - bid_open) at entry, charged as a price cost, plus 0.5x that
  spread as extra slippage on stop exits. Works for any instrument (no pip tables).
- One position per instrument per config, long and short.
- Position sizing: each position risks `risk_pct` of equity at inception, with the
  sizing stop distance = stop_mult * ATR(20) (3 ATR when a strategy has no hard stop).
  pnl_pct_equity = r_multiple_net * risk_pct (return based; no currency conversion).
- Portfolio caps: max 12 concurrent positions, max 4 per asset-class group.
- Walk-forward: TRAIN <= 2014-12-31, VALIDATE 2015-01-01..2019-12-31, TEST >= 2020-01-01.
  Trades are assigned to a split by EXIT date, so every trade in TRAIN was fully resolved
  using TRAIN data only. One config per strategy is chosen on TRAIN only.

Run: .venv/bin/python -m research.portfolio --data research/data/oanda --granularity D
"""
import argparse
import heapq
import json
import math
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent

BARS_PER_DAY = {"D": 1, "H12": 2, "H8": 3, "H6": 4, "H4": 6, "H2": 12, "H1": 24}
SPREAD_WINDOW = 250
STOP_SLIP_FRAC = 0.5
ATR_N = 20
DEFAULT_SIZING_STOP_ATR = 3.0
MIN_TRAIN_TRADES = 80
MAX_POSITIONS = 12
GROUP_CAP = 4
RISK_PCT = 0.0025
TRAIN_END = pd.Timestamp("2014-12-31")
VAL_END = pd.Timestamp("2019-12-31")
TRADING_DAYS = 252
FX_CCY = {"EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "NOK", "SEK", "DKK", "PLN",
          "HKD", "SGD", "ZAR", "MXN", "TRY", "CNH", "CZK", "HUF", "THB"}
INDEX_PREFIX = ("SPX500", "NAS100", "US30", "US2000", "DE30", "DE40", "UK100", "JP225", "FR40",
                "EU50", "HK33", "AU200", "CN50", "NL25", "ES35", "IT40", "CH20", "SG30")
GROUPS = ("fx_usd_base", "fx_cross", "metals", "indices", "energy", "other")
MIN_BARS = 400


# ------------------------------------------------------------------ data

def asset_group(inst: str) -> str:
    """fx_usd_base = FX pairs with a USD leg (USD base OR quote: all share the USD factor)."""
    parts = inst.upper().split("_")
    base = parts[0]
    if base in ("XAU", "XAG", "XPT", "XPD"):
        return "metals"
    if base in ("BCO", "WTICO", "NATGAS"):
        return "energy"
    if base.startswith(INDEX_PREFIX):
        return "indices"
    if len(parts) == 2 and base in FX_CCY and parts[1] in FX_CCY:
        return "fx_usd_base" if "USD" in parts else "fx_cross"
    return "other"


def load_instrument(path):
    """Load one CSV -> DataFrame(open, high, low, close, spread) indexed by UTC time."""
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.drop_duplicates("time").sort_values("time").set_index("time")
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    bo = pd.to_numeric(df.get("bid_open"), errors="coerce") if "bid_open" in df else np.nan
    ao = pd.to_numeric(df.get("ask_open"), errors="coerce") if "ask_open" in df else np.nan
    sp = (ao - bo)
    if not isinstance(sp, pd.Series):
        sp = pd.Series(np.nan, index=df.index)
    sp = sp.where(sp >= 0)
    out = df[["open", "high", "low", "close"]].copy()
    out["spread"] = sp
    return out


def load_dir(data_dir, granularity="D", instruments=None):
    """Returns ({inst: df}, skipped{inst: reason}, manifest|None)."""
    data_dir = Path(data_dir)
    out, skipped = {}, {}
    suffix = "_%s.csv" % granularity
    files = sorted(data_dir.glob("*" + suffix))
    for p in files:
        inst = p.name[:-len(suffix)]
        if instruments and inst not in instruments:
            continue
        try:
            df = load_instrument(p)
        except Exception as exc:  # report, don't hide
            skipped[inst] = "unreadable: %s" % type(exc).__name__
            continue
        if len(df) < MIN_BARS:
            skipped[inst] = "too few bars (%d)" % len(df)
            continue
        out[inst] = df
    manifest = None
    mp = data_dir / "MANIFEST.json"
    if mp.exists():
        try:
            manifest = json.loads(mp.read_text())
        except Exception:
            manifest = None
    return out, skipped, manifest


def trailing_spread(df):
    """Causal per-bar spread estimate = median of the previous 250 bars' spreads (NaN-skipping).
    Bars with no prior observation fall back to the instrument's median spread.
    Returns (array, fallback_bars) or (None, 0) if the instrument has no spread data at all."""
    sp = df["spread"]
    if sp.notna().sum() == 0:
        return None, 0
    inst_med = float(sp.median())
    tr = sp.shift(1).rolling(SPREAD_WINDOW, min_periods=1).median()
    fb = int(tr.isna().sum()) - 1  # bar 0 can never have history; it is never traded
    return tr.fillna(inst_med).clip(lower=0).to_numpy(float), max(fb, 0)


# ------------------------------------------------------------------ indicators / signals

def atr(df, n=ATR_N):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()],
                   axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _sig(df, el, es, xl, xs, stop_mult):
    f = lambda s: np.asarray(s, bool)
    return {"entry_long": f(el), "entry_short": f(es), "exit_long": f(xl), "exit_short": f(xs),
            "stop_mult": stop_mult}


def sig_donchian(df, N, stop_mult):
    M = max(2, N // 2)
    c = df["close"]
    hh = df["high"].rolling(N).max().shift(1)
    ll = df["low"].rolling(N).min().shift(1)
    mh = df["high"].rolling(M).max().shift(1)
    ml = df["low"].rolling(M).min().shift(1)
    return _sig(df, (c > hh).fillna(False), (c < ll).fillna(False),
                (c < ml).fillna(False), (c > mh).fillna(False), stop_mult)


def sig_ema_cross(df, fast, slow, stop_mult):
    c = df["close"]
    f = c.ewm(span=fast, adjust=False, min_periods=slow).mean()
    s = c.ewm(span=slow, adjust=False, min_periods=slow).mean()
    up, dn = (f > s), (f < s)
    pu, pdn = up.shift(1, fill_value=False), dn.shift(1, fill_value=False)
    return _sig(df, up & ~pu, dn & ~pdn, dn, up, stop_mult)


def sig_tsmom(df, L, rebalance):
    """Sign of trailing L-bar return, evaluated every `rebalance` bars (bar counter from series
    start). Held until the next rebalance flips/exits. No hard stop."""
    n = len(df)
    ret = (df["close"] / df["close"].shift(L) - 1.0).to_numpy(float)
    reb = (np.arange(n) % rebalance == 0) & ~np.isnan(ret)
    want = np.sign(np.nan_to_num(ret))
    return _sig(df, reb & (want > 0), reb & (want < 0), reb & (want <= 0), reb & (want >= 0), None)


def build_configs(bars_per_day=1):
    """Fixed small grids. Returns list of dicts (strategy, id, params)."""
    k = max(1, int(bars_per_day))
    cfgs = []
    for N in (20, 55, 100):
        for st in (3.0, 4.0):
            cfgs.append({"strategy": "donchian_trend", "id": "donchian_N%d_stop%g" % (N, st),
                         "params": {"N": N * k, "stop_mult": st}})
    for fast, slow in ((20, 100), (50, 200)):
        for st in (3.0, None):
            cfgs.append({"strategy": "ema_cross",
                         "id": "ema_%d_%d_%s" % (fast, slow, "stop3" if st else "nostop"),
                         "params": {"fast": fast * k, "slow": slow * k, "stop_mult": st}})
    for L in (60, 120, 250):
        cfgs.append({"strategy": "tsmom", "id": "tsmom_L%d" % L,
                     "params": {"L": L * k, "rebalance": 21 * k}})
    return cfgs


STRATEGIES = ("donchian_trend", "ema_cross", "tsmom")


def make_signals(df, cfg):
    p, s = cfg["params"], cfg["strategy"]
    if s == "donchian_trend":
        return sig_donchian(df, p["N"], p["stop_mult"])
    if s == "ema_cross":
        return sig_ema_cross(df, p["fast"], p["slow"], p["stop_mult"])
    if s == "tsmom":
        return sig_tsmom(df, p["L"], p["rebalance"])
    raise ValueError(s)


# ------------------------------------------------------------------ per-instrument simulation

def simulate_instrument(inst, df, sig, atr_arr, spread_arr):
    """Walk the bars; returns list of trade dicts (net of costs, in R of the sizing stop)."""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    times = df.index
    n = len(df)
    sm = sig["stop_mult"]
    size_mult = sm if sm else DEFAULT_SIZING_STOP_ATR
    el, es, xl, xs = sig["entry_long"], sig["entry_short"], sig["exit_long"], sig["exit_short"]
    group = asset_group(inst)
    trades = []
    pos = 0
    e = x = 0
    entry = stop = risk = 0.0

    def close_trade(d, e, x, entry, exit_px, risk, reason):
        spread = spread_arr[e]
        slip = STOP_SLIP_FRAC * spread_arr[x] if reason == "stop" else 0.0
        gross = d * (exit_px - entry) / risk
        cost = (spread + slip) / risk
        seg_l, seg_h = l[e:x + 1].min(), h[e:x + 1].max()
        mae = max(0.0, (entry - seg_l) / risk if d == 1 else (seg_h - entry) / risk)
        trades.append({
            "instrument": inst, "group": group, "direction": "long" if d == 1 else "short",
            "entry_i": e, "exit_i": x, "entry_time": times[e], "exit_time": times[x],
            "entry": float(entry), "exit": float(exit_px), "risk_dist": float(risk),
            "r_gross": float(gross), "r_cost": float(cost), "r_net": float(gross - cost),
            "mae_r": float(mae + cost), "exit_reason": reason, "bars_held": int(x - e)})

    def can_enter(j):
        a = atr_arr[j]
        return j + 1 < n and a == a and a > 0

    for j in range(n):
        if pos:
            d = pos
            # stop checks for bar j
            if sm and j > e and d * (o[j] - stop) <= 0:
                close_trade(d, e, j, entry, o[j], risk, "stop")
                pos = 0
            elif sm and ((l[j] <= stop) if d == 1 else (h[j] >= stop)):
                close_trade(d, e, j, entry, stop, risk, "stop")
                pos = 0
            else:
                if sm:
                    a = atr_arr[j]
                    if a == a:
                        new = h[j] - sm * a if d == 1 else l[j] + sm * a
                        stop = max(stop, new) if d == 1 else min(stop, new)
                if j + 1 < n and (xl[j] if d == 1 else xs[j]):
                    close_trade(d, e, j + 1, entry, o[j + 1], risk, "signal")
                    pos = 0
        if pos == 0 and can_enter(j):
            d = 1 if el[j] else (-1 if es[j] else 0)
            if d == 1 and es[j]:
                d = 0  # conflicting signals: stand aside
            if d:
                pos, e = d, j + 1
                entry = o[e]
                risk = size_mult * atr_arr[j]
                stop = entry - d * risk if sm else None
    if pos:
        close_trade(pos, e, n - 1, entry, c[n - 1], risk, "eod")
    return trades


def instrument_trades(inst, df, cfg, spread_arr=None, atr_arr=None, cache=None):
    if spread_arr is None:
        spread_arr, _ = trailing_spread(df)
    if spread_arr is None:
        return []
    if atr_arr is None:
        atr_arr = atr(df).to_numpy(float)
    sig = make_signals(df, cfg)
    return simulate_instrument(inst, df, sig, atr_arr, spread_arr)


# ------------------------------------------------------------------ portfolio

def run_portfolio(trades, risk_pct=RISK_PCT, max_pos=MAX_POSITIONS, group_cap=GROUP_CAP):
    """Apply concurrency caps chronologically and compound equity.
    Returns dict(trades=[accepted trades + pnl], steps=[(time, equity)], stress=[(time, eq)],
    rejected=int)."""
    cands = sorted(trades, key=lambda t: (t["entry_time"], t["instrument"]))
    heap, seq = [], 0
    gcount = {}
    equity = 1.0
    steps, stress, accepted = [], [], []
    rejected = 0

    def realise(upto):
        nonlocal equity
        while heap and heap[0][0] <= upto:
            _, _, t = heapq.heappop(heap)
            gcount[t["group"]] -= 1
            before = equity
            pnl = t["r_net"] * risk_pct * t["equity_entry"]
            equity = before + pnl
            t["pnl_pct"] = t["r_net"] * risk_pct
            steps.append((t["exit_time"], equity))
            trough = before - t["mae_r"] * risk_pct * t["equity_entry"]
            stress.append((t["exit_time"], min(trough, equity)))
            stress.append((t["exit_time"], equity))

    for t in cands:
        realise(t["entry_time"])
        if len(heap) >= max_pos or gcount.get(t["group"], 0) >= group_cap:
            rejected += 1
            continue
        t = dict(t)
        t["equity_entry"] = equity
        gcount[t["group"]] = gcount.get(t["group"], 0) + 1
        seq += 1
        heapq.heappush(heap, (t["exit_time"], seq, t))
        accepted.append(t)
    realise(pd.Timestamp.max.tz_localize("UTC"))
    return {"trades": accepted, "steps": steps, "stress": stress, "rejected": rejected,
            "risk_pct": risk_pct}


def daily_returns(steps, end):
    """Business-day equity returns from closed-trade equity steps (realised at exit date)."""
    if not steps:
        return pd.Series(dtype=float)
    s = pd.Series([v for _, v in steps], index=pd.DatetimeIndex([t for t, _ in steps]))
    s.index = s.index.tz_convert("UTC").tz_localize(None).normalize()
    s = s.groupby(level=0).last()
    s.loc[s.index[0] - pd.Timedelta(days=1)] = 1.0
    s = s.sort_index()
    full = s.resample("D").last().ffill()
    bd = pd.bdate_range(full.index[0], max(pd.Timestamp(end).normalize(), full.index[-1]))
    eq = full.reindex(full.index.union(bd)).ffill().reindex(bd)
    return eq.pct_change().dropna()


# ------------------------------------------------------------------ metrics

def series_stats(r):
    """Stats from a daily return series (business days)."""
    nan = float("nan")
    out = {"days": 0, "cagr": nan, "vol": nan, "sharpe": nan, "max_dd": nan, "calmar": nan,
           "dd_days": 0, "total_return": nan}
    r = pd.Series(r).dropna()
    if len(r) < 2:
        return out
    eq = (1 + r).cumprod()
    days = max(1, (r.index[-1] - r.index[0]).days + 1)
    final = float(eq.iloc[-1])
    cagr = final ** (365.25 / days) - 1 if final > 0 else -1.0
    sd = float(r.std(ddof=1))
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq.to_numpy()]))[1:]
    dd = 1 - eq.to_numpy() / peak
    mdd = float(dd.max())
    # longest drawdown duration (calendar days between peak date and recovery / end)
    longest, start_i = 0, None
    idx = r.index
    for i, v in enumerate(dd):
        if v > 0:
            if start_i is None:
                start_i = i
        elif start_i is not None:
            longest = max(longest, (idx[i] - idx[start_i - 1 if start_i else 0]).days)
            start_i = None
    if start_i is not None:
        longest = max(longest, (idx[-1] - idx[start_i - 1 if start_i else 0]).days)
    out.update({"days": days, "cagr": float(cagr), "vol": sd * math.sqrt(TRADING_DAYS),
                "sharpe": float(r.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else nan,
                "max_dd": mdd, "calmar": float(cagr / mdd) if mdd > 0 else nan,
                "dd_days": int(longest), "total_return": final - 1})
    return out


def stress_dd(stress, start, end, start_equity):
    pts = [v for t, v in stress if start <= _day(t) <= end]
    if not pts:
        return float("nan")
    peak, worst = start_equity, 0.0
    for v in pts:
        peak = max(peak, v)
        worst = max(worst, 1 - v / peak)
    return float(worst)


def _day(t):
    t = pd.Timestamp(t)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return t.normalize()


def trades_in(trades, start=None, end=None):
    """Trades whose EXIT date lies in [start, end]."""
    out = []
    for t in trades:
        d = _day(t["exit_time"])
        if (start is None or d >= pd.Timestamp(start)) and (end is None or d <= pd.Timestamp(end)):
            out.append(t)
    return out


def monthly_block_bootstrap(trades, n_boot=2000, seed=42, alpha=0.05):
    """95% CI on pooled expectancy R, resampling whole exit-months with replacement."""
    if not trades:
        return (float("nan"), float("nan"))
    keys = [(_day(t["exit_time"]).year, _day(t["exit_time"]).month) for t in trades]
    sums, counts = {}, {}
    for k, t in zip(keys, trades):
        sums[k] = sums.get(k, 0.0) + t["r_net"]
        counts[k] = counts.get(k, 0) + 1
    ks = sorted(sums)
    s = np.array([sums[k] for k in ks])
    c = np.array([counts[k] for k in ks], float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(ks), size=(n_boot, len(ks)))
    means = s[idx].sum(axis=1) / c[idx].sum(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def trade_stats(trades, n_boot=2000, seed=42):
    nan = float("nan")
    if not trades:
        return {"n": 0, "win_rate": nan, "expectancy": nan, "gross_expectancy": nan,
                "profit_factor": nan, "total_r": 0.0, "ci_low": nan, "ci_high": nan}
    r = np.array([t["r_net"] for t in trades])
    g = np.array([t["r_gross"] for t in trades])
    w, lo = r[r > 0].sum(), -r[r < 0].sum()
    ci = monthly_block_bootstrap(trades, n_boot, seed)
    return {"n": len(r), "win_rate": float((r > 0).mean()), "expectancy": float(r.mean()),
            "gross_expectancy": float(g.mean()),
            "profit_factor": float(w / lo) if lo > 0 else float("inf"),
            "total_r": float(r.sum()), "ci_low": ci[0], "ci_high": ci[1]}


def window_report(res, start, end, data_end, n_boot=2000):
    """Full metric set for trades exiting in [start, end] and equity returns in that window."""
    tr = trades_in(res["trades"], start, end)
    ts = trade_stats(tr, n_boot)
    rets = daily_returns(res["steps"], data_end)
    if len(rets):
        lo = pd.Timestamp(start) if start is not None else rets.index[0]
        hi = pd.Timestamp(end) if end is not None else rets.index[-1]
        if tr:  # start the clock at first activity inside train-like windows
            first_entry = min(_day(t["entry_time"]) for t in res["trades"])
            lo = max(lo, first_entry)
        rets = rets[(rets.index >= lo) & (rets.index <= hi)]
    st = series_stats(rets)
    eq_start = 1.0
    if res["steps"] and start is not None:
        prior = [v for t, v in res["steps"] if _day(t) < pd.Timestamp(start)]
        eq_start = prior[-1] if prior else 1.0
    st["stressed_max_dd"] = stress_dd(res["stress"], pd.Timestamp(start) if start is not None
                                      else pd.Timestamp.min, pd.Timestamp(end) if end is not None
                                      else pd.Timestamp.max, eq_start)
    return {**ts, **st}


def contributions(trades, risk_pct):
    by_g, by_i = {}, {}
    for t in trades:
        for d, k in ((by_g, t["group"]), (by_i, t["instrument"])):
            e = d.setdefault(k, {"n": 0, "total_r": 0.0, "pnl_pct_sum": 0.0})
            e["n"] += 1
            e["total_r"] += t["r_net"]
            e["pnl_pct_sum"] += t["r_net"] * risk_pct * 100
    return by_g, by_i


def yearly_returns(res, data_end):
    rets = daily_returns(res["steps"], data_end) if res["steps"] else pd.Series(dtype=float)
    if not len(rets):
        return {}
    return {int(y): float((1 + g).prod() - 1) for y, g in rets.groupby(rets.index.year)}


def benchmark_returns(data):
    cols = []
    for inst, df in data.items():
        c = df["close"].copy()
        c.index = c.index.tz_convert("UTC").tz_localize(None).normalize()
        c = c.groupby(level=0).last()
        cols.append(c.pct_change())
    if not cols:
        return pd.Series(dtype=float)
    return pd.concat(cols, axis=1).mean(axis=1, skipna=True).dropna().sort_index()


# ------------------------------------------------------------------ walk-forward

def select_config(train_trades_by_config, min_trades=MIN_TRAIN_TRADES):
    """Rule: highest TRAIN net expectancy R among configs with >= min_trades trades.
    Receives TRAIN trades only (dict cid -> list of trade dicts). Ties -> lexicographic id."""
    best, best_e = None, -math.inf
    for cid in sorted(train_trades_by_config):
        trades = train_trades_by_config[cid]
        if len(trades) < min_trades:
            continue
        e = sum(t["r_net"] for t in trades) / len(trades)
        if e > best_e:
            best, best_e = cid, e
    return best


def verdict(val, test, comb):
    """val/test: window_report dicts; comb: trade_stats over validate+test combined."""
    checks = {
        "validate expectancy > 0": bool(val["n"] > 0 and val["expectancy"] > 0),
        "test expectancy > 0": bool(test["n"] > 0 and test["expectancy"] > 0),
        "validate Sharpe > 0.3": bool(val["sharpe"] == val["sharpe"] and val["sharpe"] > 0.3),
        "test Sharpe > 0.3": bool(test["sharpe"] == test["sharpe"] and test["sharpe"] > 0.3),
        ">= 150 combined validate+test trades": bool(comb["n"] >= 150),
        "combined bootstrap CI lower bound > 0": bool(comb["ci_low"] == comb["ci_low"]
                                                      and comb["ci_low"] > 0),
    }
    return ("candidate edge" if all(checks.values()) else "no demonstrated edge"), checks


def run_all_configs(data, configs, risk_pct=RISK_PCT, max_pos=MAX_POSITIONS, group_cap=GROUP_CAP):
    """Simulate every config over every usable instrument; returns ({cid: portfolio res}, info)."""
    prep, skipped, fallback = {}, {}, {}
    for inst, df in data.items():
        sp, fb = trailing_spread(df)
        if sp is None:
            skipped[inst] = "no bid/ask data (cannot estimate costs)"
            continue
        prep[inst] = (df, sp, atr(df).to_numpy(float))
        if fb:
            fallback[inst] = fb
    results = {}
    for cfg in configs:
        allt = []
        for inst in sorted(prep):
            df, sp, a = prep[inst]
            allt.extend(simulate_instrument(inst, df, make_signals(df, cfg), a, sp))
        results[cfg["id"]] = run_portfolio(allt, risk_pct, max_pos, group_cap)
        results[cfg["id"]]["n_candidates"] = len(allt)
    return results, {"used": sorted(prep), "skipped": skipped, "spread_fallback_bars": fallback}


def walk_forward(data, granularity="D", risk_pct=RISK_PCT, max_pos=MAX_POSITIONS,
                 group_cap=GROUP_CAP, train_end=TRAIN_END, val_end=VAL_END,
                 min_train=MIN_TRAIN_TRADES, n_boot=2000, selector=None):
    selector = selector or select_config
    bpd = BARS_PER_DAY.get(granularity, 1)
    configs = build_configs(bpd)
    results, info = run_all_configs(data, configs, risk_pct, max_pos, group_cap)
    used = {k: data[k] for k in info["used"]}
    data_end = max(df.index[-1] for df in used.values()) if used else pd.Timestamp.now(tz="UTC")
    data_end = _day(data_end)
    data_start = _day(min(df.index[0] for df in used.values())) if used else None
    windows = {"train": (None, train_end), "validate": (train_end + pd.Timedelta(days=1), val_end),
               "test": (val_end + pd.Timedelta(days=1), None)}
    out = {"strategies": {}, "all_configs": [], "n_configs": len(configs), "info": info,
           "granularity": granularity, "data_start": str(data_start), "data_end": str(data_end),
           "windows": {k: [str(v[0]), str(v[1])] for k, v in windows.items()},
           "params": {"risk_pct": risk_pct, "max_positions": max_pos, "group_cap": group_cap,
                      "min_train_trades": min_train}}
    for cfg in configs:  # appendix: TRAIN only
        wr = window_report(results[cfg["id"]], *windows["train"], data_end, n_boot=300)
        out["all_configs"].append({"strategy": cfg["strategy"], "id": cfg["id"], **wr})
    for strat in STRATEGIES:
        cids = [c["id"] for c in configs if c["strategy"] == strat]
        train_by = {cid: trades_in(results[cid]["trades"], None, train_end) for cid in cids}
        chosen = selector(train_by, min_train)
        entry = {"chosen": chosen, "n_configs": len(cids)}
        if chosen is not None:
            res = results[chosen]
            rep = {k: window_report(res, *windows[k], data_end, n_boot) for k in windows}
            vt = trades_in(res["trades"], windows["validate"][0], None)
            comb = trade_stats(vt, n_boot)
            v, checks = verdict(rep["validate"], rep["test"], comb)
            g, i = contributions(vt, risk_pct)
            entry.update({"reports": rep, "combined_val_test": comb, "verdict": v,
                          "checks": checks, "by_group_val_test": g, "by_instrument_val_test": i,
                          "yearly": yearly_returns(res, data_end),
                          "rejected_by_caps": res["rejected"]})
        else:
            entry.update({"verdict": "no demonstrated edge",
                          "note": "no config reached %d TRAIN trades" % min_train})
        out["strategies"][strat] = entry
    bench = benchmark_returns(used)
    out["benchmark"] = {}
    for k, (ws, we) in windows.items():
        b = bench
        if len(b):
            if ws is not None:
                b = b[b.index >= ws]
            if we is not None:
                b = b[b.index <= we]
        out["benchmark"][k] = series_stats(b)
    out["overall"] = ("candidate edge in: " + ", ".join(
        s for s, e in out["strategies"].items() if e["verdict"] == "candidate edge")
        if any(e["verdict"] == "candidate edge" for e in out["strategies"].values())
        else "no demonstrated edge")
    return out


# ------------------------------------------------------------------ report

def _f(v, p=2, pct=False):
    if v is None or (isinstance(v, float) and (v != v)):
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    return ("%.*f%%" % (p, v * 100)) if pct else ("%.*f" % (p, v))


def _row(label, m):
    return "| %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | [%s, %s] |" % (
        label, m["n"], _f(m["win_rate"], 1, True), _f(m["expectancy"], 3), _f(m["gross_expectancy"], 3),
        _f(m["profit_factor"]), _f(m["cagr"], 1, True), _f(m["vol"], 1, True), _f(m["sharpe"]),
        _f(m["max_dd"], 1, True), _f(m["calmar"]), _f(m["ci_low"], 3), _f(m["ci_high"], 3))


HDR = ("| window | trades | win | exp R (net) | exp R (gross) | PF | CAGR | vol | Sharpe | maxDD | Calmar "
       "| 95% CI exp R (month blocks) |\n|---|---|---|---|---|---|---|---|---|---|---|---|")


def render(out, synthetic=False):
    L = ["# Multi-market long-horizon trend/momentum: walk-forward report", ""]
    if synthetic:
        L += ["**SYNTHETIC DATA SELF-TEST. Numbers below say nothing about real markets.**", ""]
    L += ["## Verdict: %s" % out["overall"], "",
          "Rule: a strategy is a 'candidate edge' only if net expectancy R > 0 AND portfolio Sharpe > 0.3 "
          "on BOTH validate and test, >= 150 combined validate+test trades, and the monthly-block-bootstrap "
          "95% CI lower bound on combined validate+test expectancy R is > 0. Otherwise 'no demonstrated edge'.",
          "", "| strategy | frozen config (chosen on TRAIN) | verdict |", "|---|---|---|"]
    for s, e in out["strategies"].items():
        L.append("| %s | %s | %s |" % (s, e["chosen"], e["verdict"]))
    L += ["", "## Protocol", "",
          "- Granularity %s; data %s to %s; instruments used: %d (%s)." % (
              out["granularity"], out["data_start"][:10], out["data_end"][:10],
              len(out["info"]["used"]), " ".join(out["info"]["used"])),
          "- TRAIN: start..%s; VALIDATE: %s..%s; TEST: %s..end. Trades are assigned by EXIT date." % (
              out["windows"]["train"][1][:10], out["windows"]["validate"][0][:10],
              out["windows"]["validate"][1][:10], out["windows"]["test"][0][:10]),
          "- Signals at bar close, entry at next bar open, causal indicators, long and short, one position "
          "per instrument.",
          "- Cost: trailing 250-bar median (ask_open-bid_open) at entry per round trip, plus 0.5x spread "
          "extra on stop exits. Gaps through a stop fill at the open.",
          "- Sizing: %.2f%% of equity risked per position at inception (stop distance 3 or 4 ATR(20); "
          "3 ATR for sizing where the strategy has no hard stop). pnl%% = R x risk%%. Caps: %d total, "
          "%d per asset-class group." % (out["params"]["risk_pct"] * 100, out["params"]["max_positions"],
                                        out["params"]["group_cap"]),
          "- Equity: closed-trade equity, realised at exit date, compounded; daily business-day returns. "
          "Not marked to market, so drawdown and vol are understated; 'stressed maxDD' additionally "
          "charges each trade's worst adverse excursion (ignores stacking of concurrent trades).",
          "- Selection: per strategy, highest TRAIN net expectancy R with >= %d TRAIN trades. The "
          "selection function receives TRAIN trades only." % out["params"]["min_train_trades"],
          "- **Configs tried: %d total** (donchian 6, ema_cross 4, tsmom 3). All are listed in the appendix. "
          "Three strategy families were tried, so one 'candidate edge' among three carries a "
          "multiple-testing discount." % out["n_configs"], ""]
    if out["info"]["skipped"]:
        L.append("- Skipped instruments: " + "; ".join("%s (%s)" % kv for kv in out["info"]["skipped"].items()))
    if out["info"]["spread_fallback_bars"]:
        L.append("- Bars using the instrument-median spread fallback (no prior spread observation): " +
                 ", ".join("%s=%d" % kv for kv in out["info"]["spread_fallback_bars"].items()))
    for s, e in out["strategies"].items():
        L += ["", "## %s" % s, ""]
        if e["chosen"] is None:
            L += ["No config reached the TRAIN trade minimum. %s" % e.get("note", ""), ""]
            continue
        L += ["Frozen config: `%s`. Verdict: **%s**." % (e["chosen"], e["verdict"]), "", HDR]
        for k in ("train", "validate", "test"):
            L.append(_row(k + (" (in-sample)" if k == "train" else ""), e["reports"][k]))
        L.append(_row("validate+test", {**e["combined_val_test"], **{
            x: float("nan") for x in ("cagr", "vol", "sharpe", "max_dd", "calmar")}}))
        L += ["", "Stressed max DD (MAE charged): " + ", ".join(
            "%s %s" % (k, _f(e["reports"][k]["stressed_max_dd"], 1, True)) for k in e["reports"]),
            "Longest drawdown (days): " + ", ".join(
                "%s %d" % (k, e["reports"][k]["dd_days"]) for k in e["reports"]),
            "Trades rejected by concurrency caps: %d" % e["rejected_by_caps"], "",
            "Verdict checks:", ""]
        L += ["- [%s] %s" % ("x" if ok else " ", name) for name, ok in e["checks"].items()]
        L += ["", "Per-year return (TRAIN years are in-sample):", "", "| year | return |", "|---|---|"]
        for y, v in e["yearly"].items():
            L.append("| %d | %s |" % (y, _f(v, 1, True)))
        L += ["", "Contribution by asset class (validate+test):", "",
              "| group | trades | total R | sum pnl % equity |", "|---|---|---|---|"]
        for g, v in sorted(e["by_group_val_test"].items()):
            L.append("| %s | %d | %s | %s |" % (g, v["n"], _f(v["total_r"]), _f(v["pnl_pct_sum"])))
        L += ["", "Contribution by instrument (validate+test, total R):", "",
              "| instrument | trades | total R |", "|---|---|---|"]
        for k, v in sorted(e["by_instrument_val_test"].items(), key=lambda kv: -kv[1]["total_r"]):
            L.append("| %s | %d | %s |" % (k, v["n"], _f(v["total_r"])))
    L += ["", "## Benchmark: equal-weight buy-and-hold (context only)", "",
          "Daily-rebalanced equal weight of all instruments, long only, no costs, no carry. Not "
          "comparable one-for-one with a risk-budgeted long/short book.", "",
          "| window | CAGR | vol | Sharpe | maxDD |", "|---|---|---|---|---|"]
    for k, b in out["benchmark"].items():
        L.append("| %s | %s | %s | %s | %s |" % (k, _f(b["cagr"], 1, True), _f(b["vol"], 1, True),
                                                _f(b["sharpe"]), _f(b["max_dd"], 1, True)))
    L += ["", "## Appendix: all configs, TRAIN window only", "", HDR.replace("window", "config")]
    for c in out["all_configs"]:
        L.append(_row(c["id"], c))
    L += ["", "## Caveats", "",
          "- Regime coverage: the global financial crisis (2008) sits only in TRAIN; VALIDATE and TEST "
          "contain no comparable equity/credit crash except the 2020 COVID shock (TEST). Trend following "
          "performance is dominated by a few episodes, so any split has few independent observations.",
          "- Daily bars give a few hundred trades per config at most. Trades cluster in time and across "
          "correlated markets; the month-block bootstrap only partly reflects that.",
          "- Universe is fixed by what OANDA lists today (survivorship in the instrument list); history "
          "starts at each instrument's export start; spreads in early years may be stale or missing.",
          "- Trades assigned by exit date; a trade may be entered in one window and exit in the next. "
          "Trades blocked by caps are dropped, not queued. Same-timestamp cap conflicts resolve "
          "alphabetically by instrument.",
          "- Non-USD P&L is return-based (R x risk%); no currency conversion and no financing/swap costs "
          "are modelled, which flatters carry-adverse positions.",
          "- A negative result is a result: this harness does not tune after seeing validate/test."]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------ synthetic data

def make_synthetic(name, n=5500, seed=0, drift=0.0, vol=0.007, start="2004-01-05",
                   base=1.1, spread_frac=0.00015, regime_len=0):
    """Synthetic daily instrument in the exporter's CSV schema (mid OHLC + bid/ask)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n, tz="UTC")
    mu = np.full(n, drift)
    if regime_len:
        sign = np.where((np.arange(n) // regime_len) % 2 == 0, 1.0, -1.0)
        mu = drift * sign
    lr = mu * vol + vol * rng.standard_normal(n)
    c = base * np.exp(np.cumsum(lr))
    o = np.concatenate([[base], c[:-1]])
    rng_hi = np.abs(rng.standard_normal(n)) * vol * c * 0.5
    rng_lo = np.abs(rng.standard_normal(n)) * vol * c * 0.5
    h, l = np.maximum(o, c) + rng_hi, np.minimum(o, c) - rng_lo
    half = c * spread_frac / 2
    return pd.DataFrame({"time": idx.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"), "open": o, "high": h,
                         "low": l, "close": c, "volume": 1000, "bid_open": o - half,
                         "ask_open": o + half, "bid_close": c - half, "ask_close": c + half})


SELFTEST_INSTRUMENTS = [
    ("EUR_USD", 0.06), ("GBP_USD", 0.0), ("USD_JPY", 0.05), ("AUD_USD", 0.0), ("EUR_GBP", 0.0),
    ("GBP_JPY", 0.04), ("XAU_USD", 0.07), ("XAG_USD", 0.0), ("SPX500_USD", 0.06),
    ("DE30_EUR", 0.0), ("WTICO_USD", 0.05), ("BCO_USD", 0.0)]


def write_synthetic_dir(path, n=5500, gran="D"):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    for i, (inst, drift) in enumerate(SELFTEST_INSTRUMENTS):
        make_synthetic(inst, n=n, seed=100 + i, drift=drift, regime_len=500 if drift else 0).to_csv(
            path / ("%s_%s.csv" % (inst, gran)), index=False)


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="Multi-market trend/momentum walk-forward research")
    ap.add_argument("--data", default="research/data/oanda")
    ap.add_argument("--granularity", default="D")
    ap.add_argument("--instruments", nargs="*", default=None)
    ap.add_argument("--risk-pct", type=float, default=RISK_PCT)
    ap.add_argument("--max-positions", type=int, default=MAX_POSITIONS)
    ap.add_argument("--group-cap", type=int, default=GROUP_CAP)
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    ap.add_argument("--selftest-synthetic", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest_synthetic:
        with tempfile.TemporaryDirectory() as tmp:
            write_synthetic_dir(Path(tmp) / "data")
            data, skipped, _ = load_dir(Path(tmp) / "data", "D")
            out = walk_forward(data, "D", n_boot=300)
            md = render(out, synthetic=True)
            (Path(tmp) / "selftest.md").write_text(md)
        print(md)
        print("selftest ok: %d instruments, %d configs; nothing written to reports/"
              % (len(data), out["n_configs"]))
        return 0
    data, skipped, manifest = load_dir(Path(a.data), a.granularity, a.instruments)
    if not data:
        print("No usable %s data in %s (skipped: %s)" % (a.granularity, a.data, skipped))
        return 1
    out = walk_forward(data, a.granularity, a.risk_pct, a.max_positions, a.group_cap)
    out["info"]["skipped"].update(skipped)
    out["manifest_entries"] = len(manifest) if isinstance(manifest, (list, dict)) else None
    md = render(out)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    od = Path(a.out_dir)
    od.mkdir(parents=True, exist_ok=True)
    mdp = od / ("portfolio-%s.md" % stamp)
    mdp.write_text(md)
    mdp.with_suffix(".json").write_text(json.dumps(out, indent=1, default=str))
    print(md)
    print("wrote %s" % mdp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
