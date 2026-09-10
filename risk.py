"""Risk management: position size, stop loss, take profit.

For EUR/USD on a USD-denominated account: 1 pip = 0.0001, value per unit per pip = 0.0001 USD.
So pip value for N units = N * 0.0001 USD.
"""
import math
import logging
import config
from signals import Signal

log = logging.getLogger(__name__)


def calc_sl_tp(signal: Signal) -> tuple[float, float]:
    """Calculate stop loss and take profit prices based on ATR."""
    entry = signal.last_close
    sl_distance = signal.atr * config.ATR_MULTIPLIER_SL
    tp_distance = sl_distance * config.REWARD_TO_RISK

    if signal.direction == "long":
        sl = entry - sl_distance
        tp = entry + tp_distance
    elif signal.direction == "short":
        sl = entry + sl_distance
        tp = entry - tp_distance
    else:
        raise ValueError(f"Bad direction: {signal.direction}")

    return round(sl, 5), round(tp, 5)


def calc_position_size(account_balance: float, entry: float, stop_loss: float, account_currency: str = "USD") -> int:
    """
    Returns number of units to trade so that loss-at-SL equals RISK_PER_TRADE_PCT of balance.

    For EUR/USD with USD account:
      loss_per_unit = abs(entry - stop_loss)
      units = risk_amount / loss_per_unit
    """
    risk_amount = account_balance * (config.RISK_PER_TRADE_PCT / 100.0)
    price_distance = abs(entry - stop_loss)

    if price_distance == 0:
        log.warning("SL distance is zero, refusing to size position")
        return 0

    units = risk_amount / price_distance
    units = int(math.floor(units))
    # OANDA min trade is 1 unit. Cap at sensible max for a small practice account.
    units = max(0, min(units, 1_000_000))
    return units


def validate_trade_conditions(account: dict, open_positions: list, day_start_balance: float | None) -> tuple[bool, str]:
    """Return (ok, reason). Checks position limits and daily loss limit."""
    if len(open_positions) >= config.MAX_OPEN_POSITIONS:
        return False, f"max open positions reached ({len(open_positions)})"

    if day_start_balance is not None:
        nav = account["nav"]
        drawdown_pct = ((day_start_balance - nav) / day_start_balance) * 100
        if drawdown_pct >= config.DAILY_LOSS_LIMIT_PCT:
            return False, f"daily loss limit hit ({drawdown_pct:.2f}% >= {config.DAILY_LOSS_LIMIT_PCT}%)"

    return True, "ok"
