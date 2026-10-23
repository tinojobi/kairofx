"""Strategies: (df, params) -> {'signals': [...], 'exit_long', 'exit_short'}.

Each signal: dict(i, direction, sl_dist(price), rr|None, trail_mult|None, max_bars|None),
computed using only data at or before bar i. `prepare(df)` adds causal indicator columns.
"""
import itertools

import numpy as np
import pandas as pd

import signals as bot_signals  # read-only reuse of bot indicator/pattern formulas

# ---------------------------------------------------------------- indicators

def prepare(df: pd.DataFrame) -> pd.DataFrame:
    if "atr" in df.columns and "_prepared" in df.columns:
        return df
    d = df.copy()
    c = d["close"]
    d["atr"] = bot_signals.atr(d, 14)
    d["rsi14"] = bot_signals.rsi(c, 14)
    d["rsi2"] = bot_signals.rsi(c, 2)
    m, s, _ = bot_signals.macd(c, 12, 26, 9)
    d["macd"], d["macd_sig"] = m, s
    d["ema20"] = c.ewm(span=20, adjust=False).mean()
    d["ema50"] = c.ewm(span=50, adjust=False).mean()
    d["ema200"] = c.ewm(span=200, adjust=False).mean()
    # candlestick patterns, same priority order as signals.detect_signal (all toggles on, doji off)
    o, h, l, cl = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    n = len(d)
    bull = np.zeros(n, bool)
    bear = np.zeros(n, bool)
    rows = d[["open", "high", "low", "close"]].to_dict("records")
    for i in range(1, n):
        prev, cur = rows[i - 1], rows[i]
        if bot_signals.is_bullish_engulfing(prev, cur):
            bull[i] = True
        elif bot_signals.is_bearish_engulfing(prev, cur):
            bear[i] = True
        elif bot_signals.is_hammer(cur):
            bull[i] = True
        elif bot_signals.is_shooting_star(cur):
            bear[i] = True
    d["bull_pat"], d["bear_pat"] = bull, bear
    d["_prepared"] = True
    return d


def _sig(i, direction, sl_dist, rr=None, trail_mult=None, max_bars=None):
    return {"i": int(i), "direction": direction, "sl_dist": float(sl_dist), "rr": rr,
            "trail_mult": trail_mult, "max_bars": max_bars}


def _out(sigs, exit_long=None, exit_short=None):
    return {"signals": sigs, "exit_long": exit_long, "exit_short": exit_short}


# ---------------------------------------------------------------- 1. baseline

def baseline_pattern_rsi(df, p):
    """Current bot logic (signals.detect_signal) as a parameterised, config-free wrapper."""
    d = prepare(df)
    idx = d.index
    atr = d["atr"].to_numpy(float)
    rsi = d["rsi14"].to_numpy(float)
    macd_bear = (d["macd"] < d["macd_sig"]).to_numpy()
    macd_bull = (d["macd"] > d["macd_sig"]).to_numpy()
    bull, bear = d["bull_pat"].to_numpy(), d["bear_pat"].to_numpy()
    wd, hr = idx.weekday, idx.hour
    sigs = []
    for i in range(50, len(d)):  # detect_signal needs >=50 candles
        if p.get("no_weekend", True) and wd[i] >= 5:
            continue
        if wd[i] == 4 and hr[i] >= p.get("fri_cutoff_hour", 20):
            continue
        if not atr[i] > 0:
            continue
        direction = None
        if bull[i] and rsi[i] < p["rsi_long_max"]:
            if not (p["macd_align"] and macd_bear[i]):
                direction = "long"
        elif bear[i] and rsi[i] > p["rsi_short_min"]:
            if not (p["macd_align"] and macd_bull[i]):
                direction = "short"
        if direction:
            sigs.append(_sig(i, direction, p["sl_atr"] * atr[i], rr=p["rr"]))
    return _out(sigs)


# ---------------------------------------------------------------- 2. trend pullback

def trend_pullback(df, p):
    d = prepare(df)
    c, atr, e20 = (d[k].to_numpy(float) for k in ("close", "atr", "ema20"))
    up = (d["ema50"] > d["ema200"]).to_numpy()
    dn = (d["ema50"] < d["ema200"]).to_numpy()
    rsi = d["rsi14"].to_numpy(float)
    sigs = []
    dist = (c - e20) / np.where(atr > 0, atr, np.nan)
    for i in range(205, len(d)):
        a = atr[i]
        if not a > 0:
            continue
        prior = dist[i - 5:i]
        near = abs(dist[i]) <= 0.5
        direction = None
        if up[i]:
            ema_pb = near and np.nanmax(prior) > 1.0
            rsi_pb = rsi[i] < 40
            if (ema_pb and p["mode"] == "either") or rsi_pb:
                direction = "long"
        elif dn[i]:
            ema_pb = near and np.nanmin(prior) < -1.0
            rsi_pb = rsi[i] > 60
            if (ema_pb and p["mode"] == "either") or rsi_pb:
                direction = "short"
        if direction:
            sigs.append(_sig(i, direction, p["sl_atr"] * a, rr=p["rr"]))
    return _out(sigs)


# ---------------------------------------------------------------- 3. donchian

def donchian_breakout(df, p):
    d = prepare(df)
    N = p["N"]
    c, atr = d["close"].to_numpy(float), d["atr"].to_numpy(float)
    hh = d["high"].rolling(N).max().shift(1).to_numpy(float)  # prior N bars, excl. current
    ll = d["low"].rolling(N).min().shift(1).to_numpy(float)
    sigs = []
    for i in range(N + 1, len(d)):
        if not atr[i] > 0:
            continue
        if c[i] > hh[i]:
            sigs.append(_sig(i, "long", p["trail"] * atr[i], trail_mult=p["trail"]))
        elif c[i] < ll[i]:
            sigs.append(_sig(i, "short", p["trail"] * atr[i], trail_mult=p["trail"]))
    return _out(sigs)


# ---------------------------------------------------------------- 4. asian range breakout

def asian_range_breakout(df, p):
    d = prepare(df)
    idx = d.index
    h, l, c, atr = (d[k].to_numpy(float) for k in ("high", "low", "close", "atr"))
    date = idx.normalize()
    hr, wd = idx.hour, idx.weekday
    sigs = []
    cur_date, rng_hi, rng_lo, nbars, done = None, -np.inf, np.inf, 0, False
    for i in range(len(d)):
        if date[i] != cur_date:
            cur_date, rng_hi, rng_lo, nbars, done = date[i], -np.inf, np.inf, 0, False
        if hr[i] < 7:  # Asian session 00:00-07:00 UTC (bars 00..06)
            rng_hi, rng_lo, nbars = max(rng_hi, h[i]), min(rng_lo, l[i]), nbars + 1
            continue
        if done or hr[i] > p["last_hour"] or nbars < 5 or wd[i] > 3 or not atr[i] > 0:
            continue
        # close of bar at hour H is available at H+1; signal bars 07..last_hour
        if c[i] > rng_hi:
            direction, sl = "long", c[i] - rng_lo
        elif c[i] < rng_lo:
            direction, sl = "short", rng_hi - c[i]
        else:
            continue
        sl = min(sl, 2 * atr[i])
        sl = max(sl, 0.5 * atr[i])  # floor so a tiny range cannot create absurd R
        sigs.append(_sig(i, direction, sl, rr=p["rr"]))
        done = True
    return _out(sigs)


# ---------------------------------------------------------------- 5. RSI(2) trend

def rsi2_trend(df, p):
    d = prepare(df)
    c, atr, r2, e200 = (d[k].to_numpy(float) for k in ("close", "atr", "rsi2", "ema200"))
    sigs = []
    for i in range(205, len(d)):
        if not atr[i] > 0 or r2[i] != r2[i]:
            continue
        if r2[i] < p["thr"] and c[i] > e200[i]:
            sigs.append(_sig(i, "long", p["sl_atr"] * atr[i], max_bars=p["max_bars"]))
        elif r2[i] > 100 - p["thr"] and c[i] < e200[i]:
            sigs.append(_sig(i, "short", p["sl_atr"] * atr[i], max_bars=p["max_bars"]))
    return _out(sigs, exit_long=(r2 > 50), exit_short=(r2 < 50))


# ---------------------------------------------------------------- registry / grids

def _grid(**kw):
    keys = list(kw)
    return [dict(zip(keys, v)) for v in itertools.product(*kw.values())]


BASELINE_PARAMS = {"rsi_long_max": 47, "rsi_short_min": 53, "macd_align": True,
                   "sl_atr": 1.25, "rr": 2.5}

STRATEGIES = {
    "baseline_pattern_rsi": (baseline_pattern_rsi, [BASELINE_PARAMS]),
    "trend_pullback": (trend_pullback,
                       _grid(mode=["either", "rsi_only"], sl_atr=[1.5, 2.0], rr=[1.5, 2.5])),
    "donchian_breakout": (donchian_breakout, _grid(N=[20, 55], trail=[2.5, 3.0, 4.0])),
    "asian_range_breakout": (asian_range_breakout, _grid(rr=[1.5, 2.0], last_hour=[8, 10])),
    "rsi2_trend": (rsi2_trend, _grid(thr=[5, 10], sl_atr=[2.0, 3.0], max_bars=[6, 12])),
}


def params_id(p: dict) -> str:
    return ",".join(f"{k}={v}" for k, v in p.items())


def total_configs() -> int:
    return sum(len(g) for _, g in STRATEGIES.values())
