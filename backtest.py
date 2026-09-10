"""Minimal candle-replay backtest foundation for KairoFX.

This is a research/measurement tool only. It does not place trades and does not
change live bot settings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd

from signals import detect_signal
from risk import calc_sl_tp


@dataclass(frozen=True)
class BacktestTrade:
    instrument: str
    direction: str
    open_time: str
    close_time: str
    entry: float
    exit_price: float
    units: int
    realized_pl: float
    exit_reason: str


def _row_time(df: pd.DataFrame, idx: int) -> str:
    value = df.index[idx]
    return str(value)


def _calc_units(balance: float, risk_per_trade_pct: float, entry: float, stop_loss: float) -> int:
    risk_amount = balance * (risk_per_trade_pct / 100.0)
    distance = abs(entry - stop_loss)
    if distance <= 0:
        return 0
    return int(risk_amount / distance)


def _find_exit(df: pd.DataFrame, start_idx: int, direction: str, sl: float, tp: float) -> tuple[int, float, str]:
    for idx in range(start_idx + 1, len(df)):
        row = df.iloc[idx]
        if direction == "long":
            sl_hit = row["low"] <= sl
            tp_hit = row["high"] >= tp
            if sl_hit and tp_hit:
                return idx, sl, "ambiguous_stop_first"
            if sl_hit:
                return idx, sl, "stop_loss"
            if tp_hit:
                return idx, tp, "take_profit"
        else:
            sl_hit = row["high"] >= sl
            tp_hit = row["low"] <= tp
            if sl_hit and tp_hit:
                return idx, sl, "ambiguous_stop_first"
            if sl_hit:
                return idx, sl, "stop_loss"
            if tp_hit:
                return idx, tp, "take_profit"
    return len(df) - 1, float(df.iloc[-1]["close"]), "end_of_data"


def summarize_backtest(trades: list[BacktestTrade], *, starting_balance: float) -> dict[str, Any]:
    wins = [t for t in trades if t.realized_pl > 0]
    losses = [t for t in trades if t.realized_pl < 0]
    balance = starting_balance
    peak = starting_balance
    max_dd = 0.0
    for t in trades:
        balance += t.realized_pl
        peak = max(peak, balance)
        if peak:
            max_dd = max(max_dd, ((peak - balance) / peak) * 100)
    net = sum(t.realized_pl for t in trades)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / len(trades)) * 100, 1) if trades else 0.0,
        "net_pl": round(net, 2),
        "expectancy": round(net / len(trades), 2) if trades else 0.0,
        "max_drawdown_pct": round(max_dd, 2),
        "ending_balance": round(balance, 2),
    }


def run_backtest(
    candles: pd.DataFrame,
    *,
    instrument: str = "EUR_USD",
    starting_balance: float = 10000.0,
    risk_per_trade_pct: float = 1.0,
    min_history: int = 50,
) -> dict[str, Any]:
    df = candles.copy()
    trades: list[BacktestTrade] = []
    balance = starting_balance
    idx = min_history
    while idx < len(df) - 1:
        window = df.iloc[: idx + 1]
        signal = detect_signal(window)
        if signal.direction == "none":
            idx += 1
            continue
        sl, tp = calc_sl_tp(signal)
        units = _calc_units(balance, risk_per_trade_pct, signal.last_close, sl)
        if units <= 0:
            idx += 1
            continue
        signed_units = units if signal.direction == "long" else -units
        exit_idx, exit_price, exit_reason = _find_exit(df, idx, signal.direction, sl, tp)
        if signal.direction == "long":
            pl = (exit_price - signal.last_close) * units
        else:
            pl = (signal.last_close - exit_price) * units
        trade = BacktestTrade(
            instrument=instrument,
            direction=signal.direction,
            open_time=_row_time(df, idx),
            close_time=_row_time(df, exit_idx),
            entry=float(signal.last_close),
            exit_price=float(exit_price),
            units=signed_units,
            realized_pl=round(pl, 2),
            exit_reason=exit_reason,
        )
        trades.append(trade)
        balance += trade.realized_pl
        idx = exit_idx + 1
    return {
        "summary": summarize_backtest(trades, starting_balance=starting_balance),
        "trades": trades,
    }


def serialize_backtest(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": result["summary"],
        "trades": [asdict(t) if isinstance(t, BacktestTrade) else t for t in result["trades"]],
    }
