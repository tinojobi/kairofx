import unittest
from unittest import mock

import pandas as pd

import config
from signals import detect_signal


def candle(open_, high, low, close):
    return {"open": open_, "high": high, "low": low, "close": close, "volume": 1000}


def base_frame_with_bullish_engulfing():
    rows = []
    price = 1.2000
    # Downtrend to create low RSI and bearish MACD context.
    for _ in range(60):
        rows.append(candle(price, price + 0.0002, price - 0.0006, price - 0.0004))
        price -= 0.0004
    # Previous red candle then bullish engulfing candle.
    rows.append(candle(1.1750, 1.1752, 1.1738, 1.1740))
    rows.append(candle(1.1738, 1.1758, 1.1735, 1.1754))
    return pd.DataFrame(rows)


class SignalQualityFilterTests(unittest.TestCase):
    def setUp(self):
        self.settings = dict(config.DEFAULTS)
        self.settings.update(
            {
                "RSI_LONG_MAX": 60,
                "RSI_SHORT_MIN": 40,
                "PATTERN_BULLISH_ENGULFING": True,
                "PATTERN_BEARISH_ENGULFING": True,
                "PATTERN_HAMMER": True,
                "PATTERN_SHOOTING_STAR": True,
                "PATTERN_DOJI": False,
                "REQUIRE_MACD_ALIGNMENT": False,
            }
        )
        self.load_patch = mock.patch.object(config, "load_settings", side_effect=lambda: dict(self.settings))
        self.load_patch.start()
        self.addCleanup(self.load_patch.stop)

    def test_macd_conflict_remains_advisory_by_default(self):
        sig = detect_signal(base_frame_with_bullish_engulfing())

        self.assertEqual(sig.pattern, "bullish_engulfing")
        self.assertEqual(sig.macd_state, "bearish")
        self.assertEqual(sig.direction, "long")
        self.assertIn("PATTERN+RSI_LONG", sig.notes)
        self.assertIn("MACD conflicts with long", sig.notes)

    def test_required_macd_alignment_blocks_conflicting_long(self):
        self.settings["REQUIRE_MACD_ALIGNMENT"] = True

        sig = detect_signal(base_frame_with_bullish_engulfing())

        self.assertEqual(sig.pattern, "bullish_engulfing")
        self.assertEqual(sig.macd_state, "bearish")
        self.assertEqual(sig.direction, "none")
        self.assertIn("macd_conflict_block", sig.notes)
        self.assertIn("MACD conflicts with long", sig.notes)


if __name__ == "__main__":
    unittest.main()
