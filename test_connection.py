"""Quick test that all three services work before running the bot."""
import sys
import config
from oanda_client import OandaClient
from ai_advisor import score_setup
from signals import Signal
import telegram_notify as tg


def test_oanda():
    print("→ Testing OANDA...")
    c = OandaClient()
    acct = c.get_account_summary()
    print(f"  ✓ Account: balance={acct['balance']} {acct['currency']}, env={config.OANDA_ENV}")
    df = c.get_candles(config.INSTRUMENT, config.TIMEFRAME, 5)
    print(f"  ✓ Candles: got {len(df)} rows, last close {df['close'].iloc[-1]:.5f}")
    price = c.get_current_price(config.INSTRUMENT)
    print(f"  ✓ Price: bid={price['bid']} ask={price['ask']}")


def test_openai():
    print(f"→ Testing advisor ({config.ADVISOR_MODE})...")
    fake_sig = Signal(
        direction="long", pattern="bullish_engulfing", rsi=28.0, macd_state="bullish_cross",
        atr=0.0012, last_close=1.0850, last_high=1.0865, last_low=1.0830,
        notes="test",
    )
    result = score_setup(fake_sig, [1.08, 1.081, 1.082])
    print(f"  ✓ AI score={result['score']} reasoning={result['reasoning'][:100]}")


def test_telegram():
    print("→ Testing Telegram...")
    if not config.TELEGRAM_BOT_TOKEN:
        print("  ⚠ Telegram not configured, skipping")
        return
    tg.send("✅ FX bot test message. Telegram is working.")
    print("  ✓ Sent test message (check your Telegram)")


if __name__ == "__main__":
    failed = []
    for name, fn in [("OANDA", test_oanda), ("OpenAI", test_openai), ("Telegram", test_telegram)]:
        try:
            fn()
        except Exception as e:
            print(f"  ✗ {name} FAILED: {e}")
            failed.append(name)
    if failed:
        print(f"\n❌ Failed: {', '.join(failed)}")
        sys.exit(1)
    print("\n✅ All checks passed. You can now run: python bot.py")
