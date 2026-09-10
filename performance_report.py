"""Performance reporting helpers for KairoFX.

This module is deliberately read-only: it aggregates local SQLite logs and can
summarize broker-side closed trades passed in by callers.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from broker_adapters import ClosedTrade

import config


def _parse_dt(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    if "." in text and text.endswith("+00:00"):
        # datetime.fromisoformat handles fractional seconds, this branch is just
        # here to make OANDA's nanosecond timestamps safe by trimming to micros.
        prefix, suffix = text.split("+", 1)
        date_part, frac = prefix.split(".", 1)
        text = f"{date_part}.{frac[:6]}+{suffix}"
    return datetime.fromisoformat(text)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _count_rows(rows: Iterable[sqlite3.Row | dict[str, Any]], key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        value = row[key] if isinstance(row, dict) else row[key]
        counts[str(value)] += 1
    return dict(counts)


def _fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def build_local_performance_report(*, now: str | None = None, recent_days: int = 30) -> dict[str, Any]:
    """Aggregate signal/trade health from the local bot database."""
    all_signals = _fetch_all("SELECT * FROM signals ORDER BY ts")
    all_trades = _fetch_all("SELECT * FROM trades ORDER BY ts")

    if now is None:
        now_dt = datetime.now(timezone.utc)
    else:
        now_dt = _parse_dt(now)
    cutoff = now_dt - timedelta(days=recent_days)
    recent_signals = [s for s in all_signals if _parse_dt(s["ts"]) >= cutoff]

    action_counts = _count_rows(all_signals, "action_taken")
    directional = [s for s in all_signals if s.get("direction") not in (None, "none")]
    trade_placed = action_counts.get("trade_placed", 0)
    directional_count = len(directional)

    local_trade_summary = {
        "total": len(all_trades),
        "first_ts": all_trades[0]["ts"] if all_trades else None,
        "last_ts": all_trades[-1]["ts"] if all_trades else None,
        "by_direction": _count_rows(all_trades, "direction"),
    }

    return {
        "signals": {
            "total": len(all_signals),
            "first_ts": all_signals[0]["ts"] if all_signals else None,
            "last_ts": all_signals[-1]["ts"] if all_signals else None,
            "by_action": action_counts,
            "by_direction": _count_rows(all_signals, "direction"),
            "by_pattern": _count_rows(all_signals, "pattern"),
            "directional_candidates": directional_count,
            "trade_conversion_pct": round((trade_placed / directional_count) * 100, 2) if directional_count else 0.0,
            "candidate_rate_pct": round((directional_count / len(all_signals)) * 100, 2) if all_signals else 0.0,
        },
        "recent_window": {
            "days": recent_days,
            "signals": len(recent_signals),
            "signals_by_action": _count_rows(recent_signals, "action_taken"),
        },
        "local_trades": local_trade_summary,
    }


def _same_open_hour(left_ts: str, right_ts: str) -> bool:
    return _parse_dt(left_ts).strftime("%Y-%m-%dT%H") == _parse_dt(right_ts).strftime("%Y-%m-%dT%H")


def match_closed_trades_to_local_trades(
    local_trades: list[dict[str, Any]], closed_trades: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Match OANDA closed trades to local bot trade records by instrument/hour/direction."""
    matches: list[dict[str, Any]] = []
    used_closed: set[int] = set()
    for local in local_trades:
        local_units = int(local.get("units") or 0)
        local_direction = local.get("direction") or ("long" if local_units > 0 else "short")
        for idx, closed in enumerate(closed_trades):
            if idx in used_closed:
                continue
            if isinstance(closed, ClosedTrade):
                broker = closed.broker
                broker_trade_id = closed.broker_trade_id
                instrument = closed.instrument
                open_time = closed.open_time
                close_time = closed.close_time
                closed_direction = closed.direction
                units = closed.units
                open_price = closed.open_price
                average_close_price = closed.average_close_price
                realized_pl = closed.realized_pl
                take_profit_state = closed.take_profit_state
                stop_loss_state = closed.stop_loss_state
            else:
                broker = "oanda"
                broker_trade_id = str(closed.get("id")) if closed.get("id") is not None else None
                instrument = closed.get("instrument")
                open_time = closed.get("openTime")
                close_time = closed.get("closeTime")
                closed_units = float(closed.get("initialUnits") or closed.get("currentUnits") or 0)
                closed_direction = "long" if closed_units > 0 else "short"
                units = closed.get("initialUnits")
                open_price = closed.get("price")
                average_close_price = closed.get("averageClosePrice")
                realized_pl = float(closed.get("realizedPL") or 0)
                take_profit_state = (closed.get("takeProfitOrder") or {}).get("state")
                stop_loss_state = (closed.get("stopLossOrder") or {}).get("state")
            if instrument != local.get("instrument"):
                continue
            if closed_direction != local_direction:
                continue
            if not _same_open_hour(local["ts"], open_time):
                continue
            used_closed.add(idx)
            matches.append({
                "broker": broker,
                "broker_trade_id": broker_trade_id,
                "local_ts": local["ts"],
                "open_time": open_time,
                "close_time": close_time,
                "instrument": instrument,
                "direction": closed_direction,
                "units": units,
                "open_price": open_price,
                "average_close_price": average_close_price,
                "realized_pl": float(realized_pl or 0),
                "take_profit_state": take_profit_state,
                "stop_loss_state": stop_loss_state,
            })
            break
    return matches


def render_operator_summary(
    local_report: dict[str, Any], *, closed_trade_summary: dict[str, Any] | None = None
) -> str:
    """Render a concise human/operator summary suitable for Telegram or logs."""
    signals = local_report["signals"]
    recent = local_report["recent_window"]
    trades = local_report["local_trades"]
    action = signals.get("by_action", {})
    recent_action = recent.get("signals_by_action", {})

    lines = [
        "Kairo FX performance snapshot",
        f"Signals: {signals['total']} total; {signals['candidate_rate_pct']}% candidate rate; {signals['trade_conversion_pct']}% candidate-to-trade conversion.",
        f"Actions: {action.get('skip_no_signal', 0)} no-signal, {action.get('skip_low_ai_score', 0)} low-score, {action.get('trade_placed', 0)} trades.",
        f"Recent {recent['days']}d: {recent['signals']} signals; {recent_action.get('skip_no_signal', 0)} no-signal, {recent_action.get('skip_low_ai_score', 0)} low-score, {recent_action.get('trade_placed', 0)} trades.",
        f"Local trades: {trades['total']} total; {trades.get('by_direction', {})}.",
    ]
    if closed_trade_summary is not None:
        lines.append(
            "Closed P/L: "
            f"{closed_trade_summary['net_realized_pl']} USD; "
            f"{closed_trade_summary['wins']}W/{closed_trade_summary['losses']}L; "
            f"{closed_trade_summary['win_rate_pct']}% win rate; "
            f"TP {closed_trade_summary['tp_fills']} / SL {closed_trade_summary['sl_fills']}."
        )

    if signals["candidate_rate_pct"] < 10:
        diagnosis = "upstream signal starvation remains the primary bottleneck."
    elif action.get("skip_low_ai_score", 0) > action.get("trade_placed", 0):
        diagnosis = "advisor filtering is the primary bottleneck."
    else:
        diagnosis = "trade outcomes and exit quality are the primary bottleneck."
    lines.append(f"Diagnosis: {diagnosis}")
    return "\n".join(lines)


def summarize_closed_trade_matches(matches: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [m for m in matches if m["realized_pl"] > 0]
    losses = [m for m in matches if m["realized_pl"] < 0]
    return {
        "matched_bot_trades": len(matches),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / len(matches)) * 100, 1) if matches else 0.0,
        "net_realized_pl": round(sum(m["realized_pl"] for m in matches), 2),
        "avg_win": round(sum(m["realized_pl"] for m in wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(m["realized_pl"] for m in losses) / len(losses), 2) if losses else 0.0,
        "tp_fills": sum(1 for m in matches if m.get("take_profit_state") == "FILLED"),
        "sl_fills": sum(1 for m in matches if m.get("stop_loss_state") == "FILLED"),
        "matches": matches,
    }


def build_daily_digest(
    *,
    status: dict[str, Any],
    positions: list[dict[str, Any]],
    local_report: dict[str, Any],
    closed_report: dict[str, Any],
) -> str:
    """Build a concise read-only daily digest for Telegram/operator use."""
    acct = status.get("account", {})
    signals = local_report["signals"]
    recent = local_report["recent_window"]
    recent_actions = recent.get("signals_by_action", {})
    closed_pl = closed_report.get("net_realized_pl", 0)
    bottleneck = (
        "upstream signal starvation"
        if signals.get("candidate_rate_pct", 0) < 10
        else "advisor filtering"
        if signals.get("by_action", {}).get("skip_low_ai_score", 0) > signals.get("by_action", {}).get("trade_placed", 0)
        else "trade outcome / exit quality"
    )
    lines = [
        "Kairo FX Daily Digest",
        f"Mode: {status.get('env')} | Pair: {status.get('instrument')} {status.get('timeframe')}",
        f"Trading enabled: {status.get('trading_enabled')} | Risk/trade: {status.get('risk_per_trade_pct')}% | Min AI: {status.get('min_ai_score')}",
        f"NAV: {acct.get('nav')} {acct.get('currency', '')}".rstrip(),
        f"Open positions: {len(positions)}",
        f"Signals: {signals['total']} total; {signals['candidate_rate_pct']}% candidate rate; {signals['trade_conversion_pct']}% candidate-to-trade conversion.",
        f"Recent {recent['days']}d: {recent['signals']} signals; {recent_actions.get('skip_no_signal', 0)} no-signal, {recent_actions.get('skip_low_ai_score', 0)} low-score, {recent_actions.get('trade_placed', 0)} trades.",
        f"Closed P/L: {closed_pl} USD; {closed_report.get('wins', 0)}W/{closed_report.get('losses', 0)}L; broker={closed_report.get('broker', 'unknown')}.",
        f"Primary bottleneck: {bottleneck}.",
    ]
    if positions:
        lines.append(f"Exposure: {positions}")
    return "\n".join(lines)


def fetch_local_trade_records() -> list[dict[str, Any]]:
    return _fetch_all(
        "SELECT ts, instrument, direction, units, entry, stop_loss, take_profit FROM trades ORDER BY ts"
    )


def build_closed_performance_report(*, broker: str = "oanda", count: int = 100) -> dict[str, Any]:
    from broker_adapters import get_broker_adapter

    adapter = get_broker_adapter(broker)
    local_trades = fetch_local_trade_records()
    closed_trades = adapter.fetch_closed_trades(count=count)
    matches = match_closed_trades_to_local_trades(local_trades, closed_trades)
    summary = summarize_closed_trade_matches(matches)
    summary["broker"] = broker.lower().strip()
    summary["closed_trades_fetched"] = len(closed_trades)
    summary["local_trades"] = len(local_trades)
    return summary
