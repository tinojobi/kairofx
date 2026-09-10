import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bot
import config
import db
import telegram_notify as tg
from signals import Signal


class FakeResponse:
    def __init__(self, *, ok, status_code=200, payload=None, text=""):
        self.ok = ok
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TelegramNotifyTests(unittest.TestCase):
    def setUp(self):
        self.token_patch = mock.patch.object(config, "TELEGRAM_BOT_TOKEN", "test-token")
        self.chat_patch = mock.patch.object(config, "TELEGRAM_CHAT_ID", "12345")
        self.token_patch.start()
        self.chat_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.addCleanup(self.chat_patch.stop)

    def test_send_html_returns_delivery_receipt(self):
        response = FakeResponse(
            ok=True,
            payload={
                "ok": True,
                "result": {"message_id": 77, "date": 1710000000, "chat": {"id": 12345}},
            },
        )
        with mock.patch.object(tg, "_post", return_value=response) as post_mock:
            delivery = tg.send_html("<b>hello</b>")

        self.assertTrue(delivery["delivered"])
        self.assertEqual(delivery["mode"], "html")
        self.assertEqual(delivery["message_id"], 77)
        self.assertEqual(delivery["chat_id"], 12345)
        self.assertFalse(delivery["fallback_used"])
        self.assertEqual(post_mock.call_count, 1)

    def test_send_html_falls_back_to_plain_text_on_parse_error(self):
        parse_error = FakeResponse(
            ok=False,
            status_code=400,
            payload={"ok": False, "description": "Bad Request: can't parse entities"},
            text='{"ok":false,"description":"Bad Request: can\'t parse entities"}',
        )
        fallback_ok = FakeResponse(
            ok=True,
            payload={
                "ok": True,
                "result": {"message_id": 88, "date": 1710001234, "chat": {"id": 12345}},
            },
        )
        with mock.patch.object(tg, "_post", side_effect=[parse_error, fallback_ok]) as post_mock:
            delivery = tg.send_html("<b>bad</b> & text")

        self.assertTrue(delivery["delivered"])
        self.assertEqual(delivery["mode"], "plain")
        self.assertTrue(delivery["fallback_used"])
        self.assertEqual(delivery["fallback_from"], "html")
        self.assertEqual(delivery["message_id"], 88)
        self.assertEqual(post_mock.call_count, 2)


class AlertLoggingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.orig_db_path = config.DB_PATH
        config.DB_PATH = str(Path(self.tempdir.name) / "fx_bot.db")
        db.init()
        self.addCleanup(self._restore_db_path)

    def _restore_db_path(self):
        config.DB_PATH = self.orig_db_path

    def test_send_and_log_signal_alert_persists_delivery_details(self):
        sig = Signal(
            direction="short",
            pattern="bearish_engulfing",
            rsi=61.2,
            macd_state="bearish",
            atr=0.0012,
            last_close=1.08234,
            last_high=1.08300,
            last_low=1.08100,
            notes="MACD supports short",
        )
        ai_result = {"score": 5, "reasoning": "Mean reversion setup but weak conviction."}
        delivery = {
            "delivered": True,
            "mode": "html",
            "fallback_used": False,
            "message_id": 345,
            "chat_id": 12345,
            "status_code": 200,
        }

        with mock.patch.object(tg, "send_html", return_value=delivery):
            result = bot.send_and_log_signal_alert(
                sig,
                ai_result,
                action_taken="skip_low_ai_score",
                message="test message",
                details={"branch": "skip_low_ai_score"},
            )

        self.assertEqual(result, delivery)
        recent = db.get_recent_alert_signals(limit=1)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["action_taken"], "skip_low_ai_score")
        self.assertEqual(recent[0]["details"]["branch"], "skip_low_ai_score")
        self.assertEqual(recent[0]["details"]["telegram_delivery"]["message_id"], 345)
        self.assertTrue(recent[0]["details"]["telegram_delivery"]["delivered"])


if __name__ == "__main__":
    unittest.main()
