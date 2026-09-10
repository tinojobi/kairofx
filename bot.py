"""Main trading loop. Runs forever, ticks on H1 candle close."""
import logging
import time
from datetime import datetime, timezone, timedelta

import config
import db
import telegram_notify as tg
from oanda_client import OandaClient
from signals import detect_signal
from risk import calc_sl_tp, calc_position_size, validate_trade_conditions
from ai_advisor import score_setup, write_daily_summary


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(config.LOG_PATH), logging.StreamHandler()],
)
log = logging.getLogger("bot")


def fmt_code(text: object) -> str:
    return f"<code>{tg.escape(text)}</code>"


def fmt_italic(text: object) -> str:
    return f"<i>{tg.escape(text)}</i>"


def send_and_log_signal_alert(signal, ai_result: dict, action_taken: str, message: str,
                              details: dict | None = None) -> dict:
    delivery = tg.send_html(message)
    merged_details = dict(details or {})
    merged_details["telegram_delivery"] = delivery
    db.log_signal(signal, ai_result, action_taken=action_taken, details=merged_details)
    log.info("Alert delivery for %s: %s", action_taken, delivery)
    return delivery


def is_trading_window(now: datetime) -> tuple[bool, str]:
    """Block weekend and Friday-late trading."""
    weekday = now.weekday()  # Mon=0, Sun=6
    if config.NO_TRADE_WEEKEND and weekday in (5, 6):
        return False, "weekend"
    if weekday == 4 and now.hour >= config.NO_TRADE_FRI_AFTER_UTC_HOUR:
        return False, "friday late"
    return True, "ok"


def seconds_until_next_h1_close() -> float:
    now = datetime.now(timezone.utc)
    # next top-of-hour
    next_close = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    # candle is "closed" a few seconds after the hour to ensure OANDA has it
    target = next_close + timedelta(seconds=10)
    return max(1.0, (target - now).total_seconds())


def run_one_tick(client: OandaClient, day_start_balance: float | None) -> None:
    now = datetime.now(timezone.utc)

    if not config.TRADING_ENABLED:
        log.info("TRADING_ENABLED=False, skipping tick (agent paused trading)")
        return

    ok, reason = is_trading_window(now)
    if not ok:
        log.info("Outside trading window (%s), skipping tick", reason)
        return

    # 1. Fetch candles
    df = client.get_candles(config.INSTRUMENT, config.TIMEFRAME, config.CANDLE_HISTORY)
    log.info("Fetched %d candles, last close %s @ %.5f", len(df), df.index[-1], df["close"].iloc[-1])

    # 2. Detect signal
    sig = detect_signal(df)
    log.info("Signal: %s", sig.to_dict())

    if sig.direction == "none":
        db.log_signal(sig, {"score": 0, "reasoning": "no signal"}, action_taken="skip_no_signal")
        return

    # 3. AI advisor scores it
    ai = score_setup(sig, df["close"].tolist())
    log.info("AI score=%d reasoning=%s", ai["score"], ai["reasoning"])

    if ai["score"] < config.MIN_AI_SCORE:
        send_and_log_signal_alert(
            sig,
            ai,
            action_taken="skip_low_ai_score",
            message=(
                f"⏭ <b>Skipped signal</b> {fmt_code(sig.direction)} ({tg.escape(sig.pattern)})\n"
                f"AI score: {ai['score']}/10 (need {config.MIN_AI_SCORE})\n"
                f"{fmt_italic(ai['reasoning'][:200])}"
            ),
        )
        return

    # 4. Risk checks
    account = client.get_account_summary()
    positions = client.get_open_positions()
    ok, reason = validate_trade_conditions(account, positions, day_start_balance)
    if not ok:
        log.warning("Trade blocked: %s", reason)
        send_and_log_signal_alert(
            sig,
            ai,
            action_taken=f"blocked_{reason}",
            message=f"⛔ <b>Trade blocked</b>: {tg.escape(reason)}",
        )
        return

    # 5. Size + SL/TP
    sl, tp = calc_sl_tp(sig)
    units = calc_position_size(account["balance"], sig.last_close, sl)
    if sig.direction == "short":
        units = -units
    if units == 0:
        log.warning("Position size zero, skipping")
        db.log_signal(sig, ai, action_taken="skip_zero_size")
        return

    # 6. Place order
    log.info("Placing %s %d units, entry~%.5f, SL=%.5f, TP=%.5f",
             sig.direction, units, sig.last_close, sl, tp)
    try:
        resp = client.place_market_order(config.INSTRUMENT, units, sl, tp)
        db.log_trade(config.INSTRUMENT, sig.direction, units, sig.last_close, sl, tp, resp)
        send_and_log_signal_alert(
            sig,
            ai,
            action_taken="trade_placed",
            message=(
                f"✅ <b>TRADE OPENED</b> {tg.escape(sig.direction.upper())} {fmt_code(config.INSTRUMENT)}\n"
                f"Pattern: {tg.escape(sig.pattern)}\n"
                f"Units: {units}\n"
                f"Entry: ~{sig.last_close:.5f}\n"
                f"SL: {sl:.5f}  TP: {tp:.5f}\n"
                f"AI score: {ai['score']}/10\n"
                f"{fmt_italic(ai['reasoning'][:200])}"
            ),
            details={"units": units, "sl": sl, "tp": tp}
        )
    except Exception as e:
        log.exception("Order failed: %s", e)
        send_and_log_signal_alert(
            sig,
            ai,
            action_taken=f"order_error:{e}",
            message=f"❌ Order error: {fmt_code(e)}",
        )


def main():
    db.init()
    client = OandaClient()
    consecutive_auth_like_tick_failures = 0

    # Startup
    acct = client.get_account_summary()
    log.info("Startup. Account NAV=%.2f %s (env=%s)", acct["nav"], acct["currency"], config.OANDA_ENV)
    tg.send_html(
        f"🤖 <b>FX bot started</b>\n"
        f"Env: {fmt_code(config.OANDA_ENV)}\n"
        f"Pair: {fmt_code(config.INSTRUMENT)} @ {fmt_code(config.TIMEFRAME)}\n"
        f"Balance: {acct['balance']:.2f} {tg.escape(acct['currency'])}\n"
        f"Risk/trade: {config.RISK_PER_TRADE_PCT}%, R:R={config.REWARD_TO_RISK}\n"
        f"⚠️ Paper trading. Do not use real funds yet."
    )

    day_start_balance = acct["balance"]
    current_day = datetime.now(timezone.utc).date()
    trades_today_count = 0

    while True:
        try:
            # Day rollover: reset daily loss baseline, send EOD summary
            today = datetime.now(timezone.utc).date()
            if today != current_day:
                trades = db.get_today_trades()
                acct = client.get_account_summary()
                summary = write_daily_summary(trades, acct)
                tg.send_html(f"📊 <b>Daily summary</b>\n{tg.escape(summary)}\nNAV: {acct['nav']:.2f}")
                current_day = today
                day_start_balance = acct["balance"]
                trades_today_count = 0

            wait = seconds_until_next_h1_close()
            log.info("Sleeping %.0fs until next H1 candle close", wait)
            time.sleep(wait)

            run_one_tick(client, day_start_balance)
            consecutive_auth_like_tick_failures = 0

        except KeyboardInterrupt:
            log.info("Shutdown requested")
            tg.send_html("🛑 FX bot stopped")
            break
        except Exception as e:
            log.exception("Tick error: %s", e)
            auth_like = client._is_auth_like_error(e)
            if auth_like:
                consecutive_auth_like_tick_failures += 1
                log.warning(
                    "Suppressing Telegram auth-like tick alert after failed tick %d (will alert after 2 consecutive failed ticks): %s",
                    consecutive_auth_like_tick_failures,
                    e,
                )
                if consecutive_auth_like_tick_failures >= 2:
                    tg.send_html(
                        f"⚠️ Tick error after repeated auth failures: {fmt_code(e)}\n"
                        f"Consecutive failed ticks: {consecutive_auth_like_tick_failures}"
                    )
            else:
                consecutive_auth_like_tick_failures = 0
                tg.send_html(f"⚠️ Tick error: {fmt_code(e)}")
            time.sleep(60)  # avoid tight error loop


if __name__ == "__main__":
    main()
