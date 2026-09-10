"""Candlestick patterns and indicator-based signals.

Returns a Signal dict with: direction (long/short/none), pattern, rsi, macd_state, confidence_notes.
"""
import logging
from dataclasses import dataclass, asdict
from typing import Optional
import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)


@dataclass
class Signal:
    direction: str            # "long", "short", or "none"
    pattern: str              # name of candlestick pattern
    rsi: float
    macd_state: str           # "bullish_cross", "bearish_cross", "bullish", "bearish", "neutral"
    atr: float
    last_close: float
    last_high: float
    last_low: float
    notes: str

    def to_dict(self):
        return asdict(self)


# ---------- Indicators (manual implementation, no TA-Lib needed) ----------

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()


# ---------- Candlestick patterns ----------
# All take the last two candles (prev, curr) as pd.Series rows.

def is_bullish_engulfing(prev, curr) -> bool:
    return (
        prev["close"] < prev["open"] and          # prev red
        curr["close"] > curr["open"] and          # curr green
        curr["close"] >= prev["open"] and
        curr["open"] <= prev["close"]
    )


def is_bearish_engulfing(prev, curr) -> bool:
    return (
        prev["close"] > prev["open"] and          # prev green
        curr["close"] < curr["open"] and          # curr red
        curr["open"] >= prev["close"] and
        curr["close"] <= prev["open"]
    )


def is_hammer(c) -> bool:
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]
    if rng == 0:
        return False
    lower_wick = min(c["open"], c["close"]) - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    return (
        lower_wick >= 2 * body and
        upper_wick <= body and
        body / rng < 0.35
    )


def is_shooting_star(c) -> bool:
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]
    if rng == 0:
        return False
    lower_wick = min(c["open"], c["close"]) - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    return (
        upper_wick >= 2 * body and
        lower_wick <= body and
        body / rng < 0.35
    )


def is_doji(c) -> bool:
    body = abs(c["close"] - c["open"])
    rng = c["high"] - c["low"]
    return rng > 0 and (body / rng) < 0.1


# ---------- Main signal logic ----------

def detect_signal(df: pd.DataFrame) -> Signal:
    """Analyze the just-closed candle and return a Signal."""
    if len(df) < 50:
        return Signal("none", "insufficient_data", 0, "neutral", 0, 0, 0, 0, "need more candles")

    df = df.copy()
    df["rsi"] = rsi(df["close"], config.RSI_PERIOD)
    macd_line, signal_line, hist = macd(df["close"], config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    df["macd"] = macd_line
    df["macd_sig"] = signal_line
    df["atr"] = atr(df, config.ATR_PERIOD)

    prev = df.iloc[-2]
    curr = df.iloc[-1]
    last_rsi = float(curr["rsi"])
    last_atr = float(curr["atr"])

    # MACD state
    macd_prev = df.iloc[-2]
    if curr["macd"] > curr["macd_sig"] and macd_prev["macd"] <= macd_prev["macd_sig"]:
        macd_state = "bullish_cross"
    elif curr["macd"] < curr["macd_sig"] and macd_prev["macd"] >= macd_prev["macd_sig"]:
        macd_state = "bearish_cross"
    elif curr["macd"] > curr["macd_sig"]:
        macd_state = "bullish"
    elif curr["macd"] < curr["macd_sig"]:
        macd_state = "bearish"
    else:
        macd_state = "neutral"

    # Pattern detection (priority order, honoring toggles)
    pattern = "none"
    bullish_pattern = False
    bearish_pattern = False

    if config.PATTERN_BULLISH_ENGULFING and is_bullish_engulfing(prev, curr):
        pattern, bullish_pattern = "bullish_engulfing", True
    elif config.PATTERN_BEARISH_ENGULFING and is_bearish_engulfing(prev, curr):
        pattern, bearish_pattern = "bearish_engulfing", True
    elif config.PATTERN_HAMMER and is_hammer(curr):
        pattern, bullish_pattern = "hammer", True
    elif config.PATTERN_SHOOTING_STAR and is_shooting_star(curr):
        pattern, bearish_pattern = "shooting_star", True
    elif config.PATTERN_DOJI and is_doji(curr):
        pattern = "doji"  # neutral

    # Combine: pattern + RSI extreme (mean-reversion mode)
    # MACD is informational only — not a hard gate.
    # Buy dips in ranging markets: bullish pattern + RSI oversold.
    # Sell rallies in ranging markets: bearish pattern + RSI overbought.
    direction = "none"
    macd_bonus = ""
    if bullish_pattern:
        if macd_state in ("bullish", "bullish_cross"):
            macd_bonus = " [MACD supports long]"
        elif macd_state in ("bearish", "bearish_cross"):
            macd_bonus = " [MACD conflicts with long]"
        else:
            macd_bonus = " [MACD neutral]"
    elif bearish_pattern:
        if macd_state in ("bearish", "bearish_cross"):
            macd_bonus = " [MACD supports short]"
        elif macd_state in ("bullish", "bullish_cross"):
            macd_bonus = " [MACD conflicts with short]"
        else:
            macd_bonus = " [MACD neutral]"
    else:
        macd_bonus = " [MACD neutral]"
    notes_parts = [f"pattern={pattern}", f"rsi={last_rsi:.1f}", f"macd={macd_state}"]

    macd_conflicts_long = macd_state in ("bearish", "bearish_cross")
    macd_conflicts_short = macd_state in ("bullish", "bullish_cross")
    require_macd_alignment = bool(getattr(config, "REQUIRE_MACD_ALIGNMENT", False))

    if bullish_pattern and last_rsi < config.RSI_LONG_MAX:
        if require_macd_alignment and macd_conflicts_long:
            notes_parts.append("macd_conflict_block" + macd_bonus)
        else:
            direction = "long"
            notes_parts.append("PATTERN+RSI_LONG" + macd_bonus)
    elif bearish_pattern and last_rsi > config.RSI_SHORT_MIN:
        if require_macd_alignment and macd_conflicts_short:
            notes_parts.append("macd_conflict_block" + macd_bonus)
        else:
            direction = "short"
            notes_parts.append("PATTERN+RSI_SHORT" + macd_bonus)
    else:
        notes_parts.append("signals_misaligned")

    return Signal(
        direction=direction,
        pattern=pattern,
        rsi=last_rsi,
        macd_state=macd_state,
        atr=last_atr,
        last_close=float(curr["close"]),
        last_high=float(curr["high"]),
        last_low=float(curr["low"]),
        notes=" | ".join(notes_parts),
    )
