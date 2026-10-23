"""Walk-forward strategy evaluation.  `.venv/bin/python -m research.run [--refresh] [--pairs ...]`

Protocol: per pair, chronological split TRAIN 50% / VALIDATE 25% / TEST 25% (by bar count).
Trades are assigned to a split by ENTRY time. Config selection (select_best) receives ONLY
train-split trades. Validate is reported for the frozen selection; test is reported once after.
"""
import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import data, engine, strategies

ROOT = Path(__file__).resolve().parent.parent
MIN_TRAIN_TRADES = 60
SPLITS = ("train", "validate", "test")


def split_bounds(index: pd.DatetimeIndex):
    """Return (t1, t2): train = time < t1, validate = t1 <= time < t2, test = time >= t2."""
    n = len(index)
    return index[int(n * 0.50)], index[int(n * 0.75)]


def assign_split(entry_time, bounds):
    t1, t2 = bounds
    return "train" if entry_time < t1 else ("validate" if entry_time < t2 else "test")


def select_best(train_trades_by_config: dict, min_trades: int = MIN_TRAIN_TRADES):
    """Pick config id with best TRAIN expectancy (n >= min_trades). Takes train data ONLY."""
    best, best_e = None, -math.inf
    for cid, trades in train_trades_by_config.items():
        if len(trades) < min_trades:
            continue
        e = sum(t["r_multiple_net"] for t in trades) / len(trades)
        if e > best_e:
            best, best_e = cid, e
    return best


def verdict(val_trades, test_trades):
    mv, mt = engine.metrics(val_trades), engine.metrics(test_trades)
    comb = engine.metrics(list(val_trades) + list(test_trades))
    ok = (mv["n"] > 0 and mt["n"] > 0 and mv["expectancy"] > 0 and mt["expectancy"] > 0
          and comb["n"] >= 100 and comb["ci_low"] >= 0)
    return ("candidate edge" if ok else "no demonstrated edge"), comb


def collect(pairs, refresh=False):
    """Run every config on every pair. Returns {strategy: {config_id: {split: [trades]}}}, bounds, info."""
    res = {s: {} for s in strategies.STRATEGIES}
    info = {}
    for pair in pairs:
        raw = data.get_pair(pair, refresh)
        df = strategies.prepare(raw)
        bounds = split_bounds(df.index)
        info[pair] = {"bars": len(df), "start": str(df.index[0]), "end": str(df.index[-1]),
                      "train_end": str(bounds[0]), "validate_end": str(bounds[1])}
        for sname, (fn, grid) in strategies.STRATEGIES.items():
            for p in grid:
                cid = strategies.params_id(p)
                out = fn(df, p)
                trades = engine.run_backtest(df, out, pair, sname, cid)
                bucket = res[sname].setdefault(cid, {s: [] for s in SPLITS})
                for t in trades:
                    bucket[assign_split(t["entry_time"], bounds)].append(t)
    return res, info


def fmt(m):
    if m["n"] == 0:
        return "| 0 | - | - | - | - | - | - |"
    pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
    return (f"| {m['n']} | {m['win_rate']*100:.1f}% | {m['expectancy']:+.3f} | {pf} | "
            f"{m['total_r']:+.1f} | {m['max_dd_r']:.1f} | [{m['ci_low']:+.3f}, {m['ci_high']:+.3f}] |")


HDR = "| split | trades | win% | exp net R | PF | total R | maxDD R | 95% CI exp |\n|---|---|---|---|---|---|---|---|"


def build(res, info):
    summary, sel_trades = {}, {}
    for sname, cfgs in res.items():
        train_only = {cid: sp["train"] for cid, sp in cfgs.items()}
        cid = select_best(train_only)  # <- only train data passed
        entry = {"selected": cid, "n_configs": len(cfgs)}
        if cid is not None:
            sp = cfgs[cid]
            entry["metrics"] = {s: engine.metrics(sp[s]) for s in SPLITS}
            v, comb = verdict(sp["validate"], sp["test"])
            entry["verdict"], entry["combined_validate_test"] = v, comb
            entry["per_pair"] = {s: engine.breakdown(sp[s], "pair") for s in SPLITS}
            entry["per_year"] = engine.breakdown(sp["train"] + sp["validate"] + sp["test"], "year")
            sel_trades[sname] = sp
        else:
            entry["verdict"] = "no demonstrated edge"
            entry["note"] = f"no config reached {MIN_TRAIN_TRADES} train trades; selection impossible"
        entry["train_table"] = {c: engine.metrics(sp["train"], n_boot=300) for c, sp in cfgs.items()}
        summary[sname] = entry
    return summary


def render(summary, info, pairs, ncfg):
    now = datetime.now(timezone.utc)
    L = [f"# KairoFX strategy research - {now:%Y-%m-%d %H:%M UTC}", "",
         "Out-of-sample walk-forward test of whether any strategy has a positive after-cost edge. "
         "A negative result is a valid result.", "",
         "## Setup", "",
         f"- Pairs: {', '.join(pairs)}; H1 bars from Yahoo Finance (mid prices, ~2.8 years: Jan 2024 - Oct 2026).",
         "- Split per pair by bar count: TRAIN first 50%, VALIDATE next 25%, TEST final 25% "
         "(trades assigned by entry time).",
         f"- Selection: best TRAIN expectancy aggregated over pairs, min {MIN_TRAIN_TRADES} trades. "
         "Selection function only receives train trades. VALIDATE/TEST never used for selection.",
         "- Costs: per-pair spread charged round-trip + 0.2 pip slippage on stop exits; entry at next bar open; "
         "stop assumed first when SL and TP are both inside one bar.",
         f"- **Multiple-testing disclosure: {ncfg} configs tried in total** "
         f"({', '.join(f'{s}: {len(strategies.STRATEGIES[s][1])}' for s in strategies.STRATEGIES)}). "
         "The selected config is the best of its grid on train, so even train expectancy is optimistic.",
         "- Verdict rule: 'candidate edge' only if net expectancy > 0 on BOTH validate and test, "
         ">= 100 combined validate+test trades, AND bootstrap 95% CI lower bound of combined "
         "validate+test expectancy >= 0. Otherwise 'no demonstrated edge'.", "",
         "## Headline", "",
         "| strategy | selected config | train exp R (n) | validate exp R (n) | test exp R (n) | combined V+T exp net / gross R | combined V+T CI | verdict |",
         "|---|---|---|---|---|---|---|---|"]
    for s, e in summary.items():
        if e["selected"] is None:
            L.append(f"| {s} | none (<{MIN_TRAIN_TRADES} train trades) | - | - | - | - | - | {e['verdict']} |")
            continue
        m = e["metrics"]
        c = e["combined_validate_test"]
        f = lambda x: f"{x['expectancy']:+.3f} ({x['n']})" if x["n"] else "- (0)"
        L.append(f"| {s} | `{e['selected']}` | {f(m['train'])} | {f(m['validate'])} | {f(m['test'])} | "
                 f"{c['expectancy']:+.3f} / {c['gross_expectancy']:+.3f} | "
                 f"[{c['ci_low']:+.3f}, {c['ci_high']:+.3f}] | **{e['verdict']}** |")
    L += ["", "## Per-strategy detail (selected config)", ""]
    for s, e in summary.items():
        L += [f"### {s}", ""]
        if e["selected"] is None:
            L += ["No config selected.", ""]
            continue
        L += [f"Selected (by train only): `{e['selected']}`  - verdict: **{e['verdict']}**", "", HDR]
        for sp in SPLITS:
            L.append(f"| {sp} " + fmt(e["metrics"][sp]))
        L.append(f"| validate+test " + fmt(e["combined_validate_test"]))
        L += ["", "Per pair (expectancy net R, n):", "",
              "| pair | train | validate | test |", "|---|---|---|---|"]
        for pr in pairs:
            cells = []
            for sp in SPLITS:
                m = e["per_pair"][sp].get(pr)
                cells.append(f"{m['expectancy']:+.3f} ({m['n']})" if m else "-")
            L.append(f"| {pr} | " + " | ".join(cells) + " |")
        L += ["", "Per entry year (all splits): " + ", ".join(
            f"{y}: {m['expectancy']:+.3f} ({m['n']})" for y, m in sorted(e["per_year"].items())), "",
            "All configs, TRAIN split only (for disclosure):", "",
            "| config | trades | exp net R | PF | total R |", "|---|---|---|---|---|"]
        for c, m in e["train_table"].items():
            pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
            ex = "-" if m["n"] == 0 else f"{m['expectancy']:+.3f}"
            L.append(f"| `{c}` | {m['n']} | {ex} | {pf} | {m['total_r']:+.1f} |")
        L.append("")
    L += ["## Data windows", "", "| pair | bars | start | end | train end | validate end |", "|---|---|---|---|---|---|"]
    for pr, i in info.items():
        L.append(f"| {pr} | {i['bars']} | {i['start'][:16]} | {i['end'][:16]} | {i['train_end'][:16]} | {i['validate_end'][:16]} |")
    L += ["", "## Caveats", "",
          "- Yahoo FX data are indicative mid prices with no bid/ask; spreads are assumptions and real "
          "spreads widen at rollover, news and illiquid hours.",
          "- Only ~2 years of H1 history: one or two market regimes. Each split holds ~6 months per pair.",
          "- Pairs are correlated (USD legs), so trades across pairs are not independent; the bootstrap "
          "CI treats them as independent and is therefore too narrow.",
          "- Trades are assigned to splits by entry time; a trade entered late in one split may exit in the next.",
          "- Results with CIs spanning zero cannot distinguish a small edge from noise."]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--pairs", nargs="*", default=data.PAIRS)
    a = ap.parse_args(argv)
    res, info = collect(a.pairs, a.refresh)
    summary = build(res, info)
    md = render(summary, info, a.pairs, strategies.total_configs())
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    (ROOT / "reports").mkdir(exist_ok=True)
    mdp = ROOT / "reports" / f"research-{stamp}.md"
    mdp.write_text(md)
    mdp.with_suffix(".json").write_text(json.dumps(
        {"info": info, "summary": summary, "n_configs": strategies.total_configs()}, indent=1, default=str))
    print(md)
    print(f"wrote {mdp}")


if __name__ == "__main__":
    main()
