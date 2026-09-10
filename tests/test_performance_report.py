import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
from performance_report import (
    build_daily_digest,
    build_local_performance_report,
    match_closed_trades_to_local_trades,
    render_operator_summary,
    summarize_closed_trade_matches,
)


def make_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = Path(tmp.name)
    with sqlite3.connect(path) as c:
        c.execute(
            """
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                direction TEXT,
                pattern TEXT,
                rsi REAL,
                macd_state TEXT,
                atr REAL,
                last_close REAL,
                ai_score INTEGER,
                ai_reasoning TEXT,
                action_taken TEXT,
                details TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                instrument TEXT,
                direction TEXT,
                units INTEGER,
                entry REAL,
                stop_loss REAL,
                take_profit REAL,
                oanda_response TEXT
            )
            """
        )
    return path


class PerformanceReportTests(unittest.TestCase):
    def setUp(self):
        self.db_path = make_db()
        self.addCleanup(lambda: self.db_path.unlink(missing_ok=True))
        self.patch_db = mock.patch.object(config, "DB_PATH", str(self.db_path))
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)

    def insert_signal(self, ts, direction, pattern, ai_score, action_taken):
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                """
                INSERT INTO signals (ts, direction, pattern, rsi, macd_state, atr, last_close, ai_score, ai_reasoning, action_taken, details)
                VALUES (?, ?, ?, 50.0, 'bullish', 0.001, 1.1, ?, '', ?, '{}')
                """,
                (ts, direction, pattern, ai_score, action_taken),
            )

    def insert_trade(self, ts, direction, units=1000):
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                """
                INSERT INTO trades (ts, instrument, direction, units, entry, stop_loss, take_profit, oanda_response)
                VALUES (?, 'EUR_USD', ?, ?, 1.1000, 1.0990, 1.1020, '{}')
                """,
                (ts, direction, units),
            )

    def test_local_report_counts_signal_bottlenecks_and_recent_window(self):
        self.insert_signal("2026-09-01T00:00:00+00:00", "none", "none", 0, "skip_no_signal")
        self.insert_signal("2026-09-01T01:00:00+00:00", "long", "hammer", 4, "skip_low_ai_score")
        self.insert_signal("2026-09-01T02:00:00+00:00", "long", "hammer", 7, "trade_placed")
        self.insert_signal("2026-08-01T02:00:00+00:00", "short", "bearish_engulfing", 7, "trade_placed")
        self.insert_trade("2026-09-01T02:00:30+00:00", "long")

        report = build_local_performance_report(now="2026-09-06T00:00:00+00:00", recent_days=30)

        self.assertEqual(report["signals"]["total"], 4)
        self.assertEqual(report["signals"]["by_action"]["skip_no_signal"], 1)
        self.assertEqual(report["signals"]["by_action"]["skip_low_ai_score"], 1)
        self.assertEqual(report["signals"]["directional_candidates"], 3)
        self.assertEqual(report["signals"]["trade_conversion_pct"], 66.67)
        self.assertEqual(report["recent_window"]["days"], 30)
        self.assertEqual(report["recent_window"]["signals_by_action"]["trade_placed"], 1)
        self.assertEqual(report["local_trades"]["total"], 1)
        self.assertNotIn("records", report["local_trades"])

    def test_closed_trade_matching_summarizes_realized_pl_and_exit_types(self):
        local_trades = [
            {"ts": "2026-09-01T02:00:30+00:00", "instrument": "EUR_USD", "direction": "long", "units": 1000},
            {"ts": "2026-09-02T03:00:30+00:00", "instrument": "EUR_USD", "direction": "short", "units": -1000},
        ]
        closed_trades = [
            {
                "openTime": "2026-09-01T02:00:35.000000000Z",
                "instrument": "EUR_USD",
                "initialUnits": "1000",
                "realizedPL": "20.50",
                "takeProfitOrder": {"state": "FILLED"},
                "stopLossOrder": {"state": "CANCELLED"},
            },
            {
                "openTime": "2026-09-02T03:00:35.000000000Z",
                "instrument": "EUR_USD",
                "initialUnits": "-1000",
                "realizedPL": "-10.25",
                "takeProfitOrder": {"state": "CANCELLED"},
                "stopLossOrder": {"state": "FILLED"},
            },
        ]

        matches = match_closed_trades_to_local_trades(local_trades, closed_trades)
        summary = summarize_closed_trade_matches(matches)

        self.assertEqual(len(matches), 2)
        self.assertEqual(summary["matched_bot_trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["net_realized_pl"], 10.25)
        self.assertEqual(summary["tp_fills"], 1)
        self.assertEqual(summary["sl_fills"], 1)
    def test_render_operator_summary_keeps_daily_report_short_and_actionable(self):
        report = {
            "signals": {
                "total": 100,
                "candidate_rate_pct": 6.0,
                "trade_conversion_pct": 10.0,
                "by_action": {"skip_no_signal": 90, "skip_low_ai_score": 9, "trade_placed": 1},
            },
            "recent_window": {
                "days": 30,
                "signals": 50,
                "signals_by_action": {"skip_no_signal": 45, "skip_low_ai_score": 4, "trade_placed": 1},
            },
            "local_trades": {"total": 3, "by_direction": {"long": 2, "short": 1}},
        }
        closed = {
            "matched_bot_trades": 3,
            "wins": 1,
            "losses": 2,
            "win_rate_pct": 33.3,
            "net_realized_pl": -12.5,
            "tp_fills": 1,
            "sl_fills": 2,
        }

        text = render_operator_summary(report, closed_trade_summary=closed)

        self.assertIn("Kairo FX performance snapshot", text)
        self.assertIn("Signals: 100 total", text)
        self.assertIn("Recent 30d: 50 signals", text)
        self.assertIn("Closed P/L: -12.5 USD", text)
        self.assertIn("Diagnosis:", text)

    def test_matches_accept_broker_neutral_closed_trade_objects(self):
        from broker_adapters import ClosedTrade

        local_trades = [
            {"ts": "2026-09-01T02:00:30+00:00", "instrument": "EUR_USD", "direction": "long", "units": 1000},
        ]
        closed_trades = [
            ClosedTrade(
                broker="mt5",
                broker_trade_id="42",
                instrument="EUR_USD",
                open_time="2026-09-01T02:00:35+00:00",
                close_time="2026-09-01T03:00:00+00:00",
                direction="long",
                units=1000,
                open_price=1.1,
                average_close_price=1.102,
                realized_pl=20.0,
                take_profit_state="FILLED",
                stop_loss_state="CANCELLED",
            )
        ]

        matches = match_closed_trades_to_local_trades(local_trades, closed_trades)

        self.assertEqual(matches[0]["broker"], "mt5")
        self.assertEqual(matches[0]["broker_trade_id"], "42")
        self.assertEqual(matches[0]["realized_pl"], 20.0)
    def test_daily_digest_combines_status_positions_signals_and_closed_pl(self):
        status = {
            "trading_enabled": True,
            "env": "practice",
            "instrument": "EUR_USD",
            "timeframe": "H1",
            "account": {"nav": 98478.36, "currency": "USD"},
            "open_position_count": 0,
            "risk_per_trade_pct": 0.25,
            "min_ai_score": 6,
        }
        positions = []
        local = {
            "signals": {
                "total": 100,
                "candidate_rate_pct": 6.0,
                "trade_conversion_pct": 10.0,
                "by_action": {"skip_no_signal": 90, "skip_low_ai_score": 9, "trade_placed": 1},
            },
            "recent_window": {
                "days": 30,
                "signals": 50,
                "signals_by_action": {"skip_no_signal": 45, "skip_low_ai_score": 4, "trade_placed": 1},
            },
            "local_trades": {"total": 3, "by_direction": {"long": 2, "short": 1}},
        }
        closed = {
            "broker": "oanda",
            "matched_bot_trades": 3,
            "wins": 1,
            "losses": 2,
            "win_rate_pct": 33.3,
            "net_realized_pl": -12.5,
            "tp_fills": 1,
            "sl_fills": 2,
        }

        digest = build_daily_digest(status=status, positions=positions, local_report=local, closed_report=closed)

        self.assertIn("Kairo FX Daily Digest", digest)
        self.assertIn("Mode: practice", digest)
        self.assertIn("NAV: 98478.36 USD", digest)
        self.assertIn("Open positions: 0", digest)
        self.assertIn("Closed P/L: -12.5 USD", digest)
        self.assertIn("Primary bottleneck:", digest)


if __name__ == "__main__":
    unittest.main()
