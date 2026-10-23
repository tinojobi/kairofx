"""Event-driven causal backtest engine producing trades in R multiples.

Conventions
- Signal computed on bar i close; entry at bar i+1 OPEN.
- Prices are mid; costs are charged in R: full spread round trip on every trade plus
  0.2 pip slippage on every stop exit. cost_R = cost_price / stop_distance.
- If stop and TP are both reachable inside one bar, the STOP is assumed hit first.
- One position per pair at a time (signals while in a trade are ignored).
"""
import numpy as np
import pandas as pd

SPREAD_PIPS = {"EURUSD": 0.8, "GBPUSD": 1.2, "USDJPY": 1.0, "AUDUSD": 1.2, "USDCAD": 1.5,
               "USDCHF": 1.5, "NZDUSD": 1.5, "EURGBP": 1.5, "EURJPY": 1.8, "GBPJPY": 2.5}
STOP_SLIPPAGE_PIPS = 0.2


def pip_size(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


def _arr(df):
    return (df["open"].to_numpy(float), df["high"].to_numpy(float),
            df["low"].to_numpy(float), df["close"].to_numpy(float))


def simulate_trade(df_arrays, times, atr, sig, pair, strategy="", params_id="",
                   exit_long=None, exit_short=None):
    """Simulate one signal. sig: dict(i, direction, sl_dist, rr|None, trail_mult|None, max_bars|None).
    Returns (trade dict, exit_bar_index) or None if no room for an entry bar."""
    o, h, l, c = df_arrays
    n = len(o)
    i = int(sig["i"])
    e = i + 1
    if e >= n:
        return None
    d = 1 if sig["direction"] == "long" else -1
    sl_dist = float(sig["sl_dist"])
    if not sl_dist > 0:
        return None
    rr = sig.get("rr")
    trail = sig.get("trail_mult")
    max_bars = sig.get("max_bars")
    entry = o[e]
    stop = entry - d * sl_dist
    tp = entry + d * rr * sl_dist if rr else None
    pip = pip_size(pair)
    spread = SPREAD_PIPS.get(pair, 1.5) * pip

    exit_px, reason, x = None, None, None
    for j in range(e, n):
        # gap handling for bars after entry: stop filled at open if open is through it
        if j > e:
            if d * (o[j] - stop) <= 0:
                exit_px, reason, x = o[j], "stop", j
                break
            if tp is not None and d * (o[j] - tp) >= 0:
                exit_px, reason, x = o[j], "tp", j
                break
        stop_hit = (l[j] <= stop) if d == 1 else (h[j] >= stop)
        tp_hit = tp is not None and ((h[j] >= tp) if d == 1 else (l[j] <= tp))
        if stop_hit:  # pessimistic: stop first even if TP also inside the bar
            exit_px, reason, x = stop, "stop", j
            break
        if tp_hit:
            exit_px, reason, x = tp, "tp", j
            break
        # bar j closed without exit: decide on next-open exits / trail updates
        if trail:
            a = atr[j]
            if a == a:
                new = (h[j] - trail * a) if d == 1 else (l[j] + trail * a)
                stop = max(stop, new) if d == 1 else min(stop, new)
        if max_bars is not None and (j - e + 1) >= max_bars and j + 1 < n:
            exit_px, reason, x = o[j + 1], "time", j + 1
            break
        ind = exit_long if d == 1 else exit_short
        if ind is not None and ind[j] and j + 1 < n:
            exit_px, reason, x = o[j + 1], "signal", j + 1
            break
    if exit_px is None:
        exit_px, reason, x = c[n - 1], "eod", n - 1
    is_stop = reason == "stop"
    cost = spread + (STOP_SLIPPAGE_PIPS * pip if is_stop else 0.0)
    gross = d * (exit_px - entry) / sl_dist
    net = gross - cost / sl_dist
    trade = {
        "pair": pair, "strategy": strategy, "params_id": params_id,
        "direction": sig["direction"], "entry_time": times[e], "exit_time": times[x],
        "entry": float(entry), "exit": float(exit_px),
        "r_multiple_gross": float(gross), "r_multiple_net": float(net),
        "pips_gross": float(d * (exit_px - entry) / pip),
        "exit_reason": reason, "bars_held": int(x - e),
    }
    return trade, x


def run_backtest(df, out, pair, strategy="", params_id=""):
    """out: strategy output dict {'signals': [...], 'exit_long':..., 'exit_short':...}.
    df must contain an 'atr' column (see strategies.prepare)."""
    arrays = _arr(df)
    times = df.index
    atr = df["atr"].to_numpy(float)
    trades = []
    free_from = 0  # first bar index at which a new position may be entered
    for sig in sorted(out["signals"], key=lambda s: s["i"]):
        if sig["i"] + 1 < free_from:
            continue
        r = simulate_trade(arrays, times, atr, sig, pair, strategy, params_id,
                           out.get("exit_long"), out.get("exit_short"))
        if r is None:
            continue
        t, x = r
        trades.append(t)
        # stop/tp exit inside bar x -> next entry at bar x+1 earliest; open-exits at bar x -> entry at bar x ok
        free_from = x + 1 if t["exit_reason"] in ("stop", "tp") else x
    return trades


# ---------------- metrics ----------------

def bootstrap_ci(r, n_boot=2000, seed=42, alpha=0.05):
    r = np.asarray(r, float)
    if len(r) == 0:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(r), size=(n_boot, len(r)))
    means = r[idx].mean(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def metrics(trades, n_boot=2000, seed=42):
    df = trades if isinstance(trades, pd.DataFrame) else pd.DataFrame(trades)
    if len(df) == 0:
        return {"n": 0, "win_rate": float("nan"), "expectancy": float("nan"),
                "profit_factor": float("nan"), "total_r": 0.0, "max_dd_r": 0.0,
                "longest_losing_streak": 0, "ci_low": float("nan"), "ci_high": float("nan"),
                "gross_expectancy": float("nan")}
    df = df.sort_values("exit_time", kind="stable")
    r = df["r_multiple_net"].to_numpy(float)
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    dd = float((peak - eq).max())
    streak = best = 0
    for v in r:
        streak = streak + 1 if v <= 0 else 0
        best = max(best, streak)
    lo, hi = bootstrap_ci(r, n_boot, seed)
    return {"n": int(len(r)), "win_rate": float((r > 0).mean()), "expectancy": float(r.mean()),
            "gross_expectancy": float(df["r_multiple_gross"].mean()),
            "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
            "total_r": float(r.sum()), "max_dd_r": dd, "longest_losing_streak": int(best),
            "ci_low": lo, "ci_high": hi}


def breakdown(trades, by):
    """by: 'pair' or 'year'. Returns {key: metrics} (smaller bootstrap for speed)."""
    df = trades if isinstance(trades, pd.DataFrame) else pd.DataFrame(trades)
    if len(df) == 0:
        return {}
    key = df["pair"] if by == "pair" else pd.to_datetime(df["entry_time"], utc=True).dt.year
    return {str(k): metrics(g, n_boot=500) for k, g in df.groupby(key)}
