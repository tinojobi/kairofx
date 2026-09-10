"""SQLite logging for signals and trades."""
import sqlite3
import json
from datetime import datetime, timezone
import config


def _conn():
    return sqlite3.connect(config.DB_PATH)


def init():
    with _conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
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
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            instrument TEXT,
            direction TEXT,
            units INTEGER,
            entry REAL,
            stop_loss REAL,
            take_profit REAL,
            oanda_response TEXT
        )""")


def log_signal(signal, ai_result: dict, action_taken: str, details: dict | None = None):
    with _conn() as c:
        c.execute("""
            INSERT INTO signals (ts, direction, pattern, rsi, macd_state, atr, last_close,
                                 ai_score, ai_reasoning, action_taken, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            signal.direction, signal.pattern, signal.rsi, signal.macd_state,
            signal.atr, signal.last_close,
            ai_result.get("score"), ai_result.get("reasoning"),
            action_taken, json.dumps(details or {}),
        ))


def log_trade(instrument: str, direction: str, units: int, entry: float, sl: float, tp: float, oanda_resp: dict):
    with _conn() as c:
        c.execute("""
            INSERT INTO trades (ts, instrument, direction, units, entry, stop_loss, take_profit, oanda_response)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            instrument, direction, units, entry, sl, tp, json.dumps(oanda_resp),
        ))


def get_today_trades() -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM trades WHERE ts >= ? ORDER BY ts", (today,)).fetchall()
        return [dict(r) for r in rows]


def get_recent_alert_signals(limit: int = 10) -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            """
            SELECT id, ts, direction, pattern, ai_score, action_taken, details
            FROM signals
            WHERE action_taken IN ('skip_low_ai_score', 'trade_placed')
               OR action_taken LIKE 'blocked_%'
               OR action_taken LIKE 'order_error:%'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(item["details"] or "{}")
        except json.JSONDecodeError:
            item["details"] = {"raw": item["details"]}
        out.append(item)
    return out
