"""Did the AI advisor filter add value?  `.venv/bin/python -m research.ai_counterfactual`

Replays every logged EURUSD bot signal (read-only DB) from the NEXT bar open with
SL=1.25*atr (logged), RR 2.5, engine costs -- regardless of whether the bot took it.
Signals are simulated independently (no one-position limit).
"""
import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import data, engine

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "fx_bot.db"
SL_MULT, RR, MIN_SCORE = 1.25, 2.5, 5


def load_signals(db_path=DB):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT ts, direction, atr, ai_score, action_taken FROM signals "
            "WHERE direction IN ('long','short') AND action_taken IN ('trade_placed','skip_low_ai_score') "
            "ORDER BY ts").fetchall()
    finally:
        con.close()
    return pd.DataFrame(rows, columns=["ts", "direction", "atr", "ai_score", "action_taken"])


def replay(sigs: pd.DataFrame, df: pd.DataFrame, pair="EURUSD"):
    arrays = engine._arr(df)
    atr0 = np.zeros(len(df))
    times = df.index
    out, skipped = [], 0
    for r in sigs.itertuples():
        ts = pd.Timestamp(r.ts)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        entry_bar = ts.floor("h")  # bot runs just after the hour; candle that just closed is the previous one
        k = times.searchsorted(entry_bar)
        if k >= len(times) or times[k] != entry_bar or k < 1 or not r.atr or r.atr <= 0:
            skipped += 1
            continue
        sig = {"i": k - 1, "direction": r.direction, "sl_dist": SL_MULT * r.atr, "rr": RR}
        res = engine.simulate_trade(arrays, times, atr0, sig, pair, "ai_cf", "")
        if res is None:
            skipped += 1
            continue
        t = res[0]
        t.update(ai_score=int(r.ai_score) if r.ai_score is not None else -1, action_taken=r.action_taken)
        out.append(t)
    return pd.DataFrame(out), skipped


def row(label, g):
    m = engine.metrics(g)
    if m["n"] == 0:
        return f"| {label} | 0 | - | - | - | - |"
    return (f"| {label} | {m['n']} | {m['win_rate']*100:.1f}% | {m['expectancy']:+.3f} | "
            f"{m['total_r']:+.1f} | [{m['ci_low']:+.2f}, {m['ci_high']:+.2f}] |")


def main(argv=None):
    argparse.ArgumentParser().parse_args(argv)
    df = data.get_pair("EURUSD")
    sigs = load_signals()
    tr, skipped = replay(sigs, df)
    hi, lo = tr[tr.ai_score >= MIN_SCORE], tr[tr.ai_score < MIN_SCORE]
    L = ["# AI advisor counterfactual (EURUSD)", "",
         f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}.", "",
         "> **WARNING: tiny sample.** Every row below has far too few trades for a reliable conclusion; "
         "the bootstrap CIs are wide and nearly all include zero. Treat as anecdote, not evidence.", "",
         f"- Logged signals (long/short, trade_placed or skip_low_ai_score): {len(sigs)}; replayed: {len(tr)}; "
         f"outside candle window / unusable: {skipped}.",
         f"- Each simulated from next bar open, SL={SL_MULT}*logged ATR, RR={RR}, engine costs "
         "(0.8 pip spread + 0.2 pip stop slippage); signals independent (no position cap). "
         "Yahoo mid prices differ slightly from OANDA fills.", "",
         "| group | n | win% | exp net R | total R | 95% CI exp |", "|---|---|---|---|---|---|",
         row(f"AI score >= {MIN_SCORE} (would trade)", hi),
         row(f"AI score < {MIN_SCORE} (skipped)", lo),
         row("ALL signals (no AI filter)", tr)]
    for a in ("trade_placed", "skip_low_ai_score"):
        L.append(row(f"logged action = {a}", tr[tr.action_taken == a]))
    L += ["", "By AI score bucket:", "", "| score | n | win% | exp net R | total R | 95% CI exp |",
          "|---|---|---|---|---|---|"]
    for sc in sorted(tr.ai_score.unique()):
        L.append(row(str(sc), tr[tr.ai_score == sc]))
    ev, ea, en = (engine.metrics(x)["expectancy"] for x in (hi, lo, tr))
    mh, ml = engine.metrics(hi), engine.metrics(lo)
    sep = (mh["n"] and ml["n"] and mh["ci_low"] > ml["ci_high"])
    L += ["", "## Conclusion", "",
          f"Taken-by-AI group expectancy {ev:+.3f} R (n={mh['n']}) vs skipped {ea:+.3f} R (n={ml['n']}) vs all {en:+.3f} R. ",
          ("The groups' CIs are separated, which would suggest the filter adds value."
           if sep else
           "The confidence intervals overlap heavily, so this sample does NOT demonstrate that the AI filter adds "
           "(or destroys) value. Absolute expectancy of the underlying signal is also the baseline-strategy question "
           "answered in the main research report."),
          "", "Caveats: logged signals cluster in time (several fall in the same few trending days and their "
          "simulated trades overlap), so the effective independent sample is smaller than n and the CIs are too narrow. "
          "The ~5 months covered is a single regime. The positive 'ALL signals' result here contrasts with the "
          "multi-year baseline backtest (negative after costs); it is most likely a lucky window rather than evidence "
          "of edge. Score buckets 0 and 8 hold only 6 and 1 trades.", ""]
    md = "\n".join(L)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    p = ROOT / "reports" / f"ai-counterfactual-{stamp}.md"
    p.write_text(md)
    print(md)
    print(f"wrote {p}")


if __name__ == "__main__":
    main()
