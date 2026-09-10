"""Offline strategy-variant sweeper for Kairo FX.

Measurement-only tool: replays historical candles through the SAME bot
signal & risk logic (backtest.run_backtest) under different config
settings, then ranks the variants. It never places trades and never
touches the running bot's config on disk — config attributes are patched
in-process around each backtest and restored immediately after.

Pipeline shape mirrors backtest.py: returns serialize_backtest-shaped
dicts so reports stay consistent with `fxctl backtest`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import config
from backtest import run_backtest, serialize_backtest, summarize_backtest

# Config attributes the sweeper may override (must exist on config module).
SWEEP_KEYS: tuple[str, ...] = (
    "RSI_LONG_MAX",
    "RSI_SHORT_MIN",
    "REQUIRE_MACD_ALIGNMENT",
    "REWARD_TO_RISK",
    "ATR_MULTIPLIER_SL",
)

DEFAULT_GRID: list[dict[str, Any]] = [
    {"label": "baseline", "overrides": {}},
    {"label": "rsi_looser", "overrides": {"RSI_LONG_MAX": 50, "RSI_SHORT_MIN": 50}},
    {"label": "rsi_tighter", "overrides": {"RSI_LONG_MAX": 44, "RSI_SHORT_MIN": 56}},
    {"label": "macd_off", "overrides": {"REQUIRE_MACD_ALIGNMENT": False}},
    {"label": "macd_on", "overrides": {"REQUIRE_MACD_ALIGNMENT": True}},
    {"label": "rr_1_5", "overrides": {"REWARD_TO_RISK": 1.5}},
    {"label": "rr_2_5", "overrides": {"REWARD_TO_RISK": 2.5}},
    {"label": "atr_1_25", "overrides": {"ATR_MULTIPLIER_SL": 1.25}},
    {"label": "atr_1_75", "overrides": {"ATR_MULTIPLIER_SL": 1.75}},
    {"label": "rr_2_5_atr_1_75",
     "overrides": {"REWARD_TO_RISK": 2.5, "ATR_MULTIPLIER_SL": 1.75}},
]


def _snapshot(values: Iterable[str]) -> dict[str, Any]:
    return {k: getattr(config, k) for k in values}


def _restore(snap: dict[str, Any]) -> None:
    for k, v in snap.items():
        setattr(config, k, v)


def _apply(overrides: dict[str, Any]) -> None:
    for k, v in overrides.items():
        if k not in SWEEP_KEYS:
            raise ValueError(f"unsupported sweep key: {k!r}")
        setattr(config, k, v)


def run_variant(
    candles,
    overrides: dict[str, Any] | None = None,
    *,
    instrument: str = "EUR_USD",
    starting_balance: float = 10000.0,
    risk_per_trade_pct: float = 1.0,
    min_history: int = 50,
    max_trades: int | None = None,
) -> dict[str, Any]:
    """Run one backtest with `overrides` applied, then restore config."""
    snap = _snapshot(SWEEP_KEYS)
    try:
        _apply(overrides or {})
        result = run_backtest(
            candles,
            instrument=instrument,
            starting_balance=starting_balance,
            risk_per_trade_pct=risk_per_trade_pct,
            min_history=min_history,
        )
    finally:
        _restore(snap)

    if max_trades is not None:
        trades = result["trades"][:max_trades]
        result = {
            "summary": summarize_backtest(trades, starting_balance=starting_balance),
            "trades": trades,
        }
    return serialize_backtest(result)


def sweep_variants(
    candles,
    grid: list[dict[str, Any]] | None = None,
    *,
    min_trades: int = 1,
    max_trades: int | None = None,
    runner=None,
    **bt_kwargs: Any,
) -> list[dict[str, Any]]:
    """Run every variant in the grid over the same candles and rank them.

    Ranks by net P&L descending, breaking ties on expectancy then trade
    count. Variants producing fewer than `min_trades` closed trades are
    filtered out (small samples are noise). `runner` is injectable for
    tests; defaults to :func:`run_variant`.
    """
    runner = runner if runner is not None else run_variant
    grid = list(grid) if grid is not None else list(DEFAULT_GRID)
    rows: list[dict[str, Any]] = []
    for variant in grid:
        label = variant.get("label", "?")
        overrides = variant.get("overrides", {})
        res = runner(
            candles,
            overrides=overrides,
            max_trades=max_trades,
            **bt_kwargs,
        )
        summary = res["summary"]
        if summary.get("trades", 0) < min_trades:
            continue
        rows.append({
            "label": label,
            "overrides": dict(overrides),
            "net_pl": summary.get("net_pl", 0.0),
            "trades": summary.get("trades", 0),
            "wins": summary.get("wins", 0),
            "losses": summary.get("losses", 0),
            "win_rate_pct": summary.get("win_rate_pct", 0.0),
            "expectancy": summary.get("expectancy", 0.0),
            "max_drawdown_pct": summary.get("max_drawdown_pct", 0.0),
            "ending_balance": summary.get("ending_balance", 0.0),
        })

    rows.sort(key=lambda r: (r["net_pl"], r["expectancy"], r["trades"]),
              reverse=True)
    return rows


def render_markdown(results: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    """Render the ranked sweep as a compact markdown table for reports."""
    lines = [
        "# Kairo FX strategy-variant sweep",
        "",
        f"- Candles: {meta.get('candles', '?')} · instrument {meta.get('instrument', 'EUR_USD')} "
        f"· starting balance {meta.get('starting_balance', 10000.0):,.0f} "
        f"· risk/trade {meta.get('risk_per_trade_pct', 1.0)}%",
        f"- Ranked by net P&L (tie-break: expectancy, trades).",
        "",
        "| variant | trades | wins | loss | win% | net_pl | expectancy | maxDD% | end bal |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['label']} | {r['trades']} | {r['wins']} | {r['losses']} "
            f"| {r['win_rate_pct']:.1f} | {r['net_pl']:,.2f} "
            f"| {r['expectancy']:,.2f} | {r['max_drawdown_pct']:.2f} "
            f"| {r['ending_balance']:,.2f} |"
        )
    if not results:
        lines.append("_No variants met the minimum trade threshold._")
    return "\n".join(lines) + "\n"


def save_results(
    results: list[dict[str, Any]],
    meta: dict[str, Any],
    out_dir: str | Path = "reports",
) -> Path:
    """Persist {label, overrides, …} rows as JSON and a markdown report."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"variants-{stamp}"

    payload = {"meta": meta, "results": results, "generated_at": stamp}
    json_path = out_dir / f"{base.name}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    md_path = out_dir / f"{base.name}.md"
    md_path.write_text(render_markdown(results, meta))
    return md_path