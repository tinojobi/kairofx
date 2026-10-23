import io
import logging
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from research import oanda_export as ox

TOKEN = "FAKE-TOKEN-abc123secret"
ACCT = "101-999-ACCT-001"
SECRETS = [TOKEN, ACCT]


def candle(day, complete=True, hour=0):
    t = "2020-01-%02dT%02d:00:00.000000000Z" % (day, hour)
    p = lambda v: {"o": str(v), "h": str(v + 1), "l": str(v - 1), "c": str(v)}
    return {"time": t, "complete": complete, "volume": 10,
            "mid": p(100 + day), "bid": p(99.9 + day), "ask": p(100.1 + day)}


class FakeAPI:
    def __init__(self, pages=None, instruments=("EUR_USD",), fail=None):
        self.pages = list(pages or [])
        self.calls = []
        self.instruments = instruments
        self.fail = fail

    def request(self, ep):
        if self.fail:
            raise self.fail
        if type(ep).__name__ == "AccountInstruments":
            return {"instruments": [{"name": n} for n in self.instruments]}
        self.calls.append(dict(ep.params))
        return {"candles": self.pages.pop(0) if self.pages else []}


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(ox, "_sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch.object(ox, "PAGE_COUNT", 3)
        q.start()
        self.addCleanup(q.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.start = datetime(2020, 1, 1, tzinfo=timezone.utc)


class TestFetch(Base):
    def test_pagination_include_first_and_incomplete_dropped(self):
        api = FakeAPI([[candle(1), candle(2), candle(3)],
                       [candle(4), candle(5, complete=False)]])
        rows = ox.fetch_candles(api, "EUR_USD", "D", self.start, secrets=SECRETS,
                                now=datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(len(api.calls), 2)
        self.assertNotIn("includeFirst", api.calls[0])
        self.assertIs(api.calls[1]["includeFirst"], False)
        self.assertEqual(api.calls[1]["from"], candle(3)["time"])
        self.assertEqual(api.calls[0]["price"], "BAM")
        self.assertEqual([r[0] for r in rows],
                         ["2020-01-0%dT00:00:00Z" % d for d in (1, 2, 3, 4)])

    def test_dedupe_sort_and_csv(self):
        out = Path(self.tmp.name) / "x.csv"
        ox.write_rows(out, [ox.candle_row(candle(3)), ox.candle_row(candle(1))])
        n = ox.write_rows(out, [ox.candle_row(candle(3)), ox.candle_row(candle(2))])
        lines = out.read_text().splitlines()
        self.assertEqual(n, 3)
        self.assertEqual(lines[0], ",".join(ox.HEADER))
        self.assertEqual([l[:10] for l in lines[1:]],
                         ["2020-01-01", "2020-01-02", "2020-01-03"])

    def test_resume_appends_only_new(self):
        api = FakeAPI([[candle(1), candle(2)]])
        ox.export_one(api, "EUR_USD", "D", self.start, self.tmp.name, SECRETS)
        api2 = FakeAPI([[candle(2), candle(3)]])
        path, ok = ox.export_one(api2, "EUR_USD", "D", self.start, self.tmp.name, SECRETS)
        self.assertTrue(ok)
        self.assertEqual(api2.calls[0]["from"], "2020-01-02T00:00:00Z")
        self.assertIs(api2.calls[0]["includeFirst"], False)
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 4)
        e = ox.manifest_entry(path, "EUR_USD", "D")
        self.assertEqual((e["rows"], e["last_time"]), (3, "2020-01-03T00:00:00Z"))
        self.assertAlmostEqual(e["median_spread_last_250"], 0.2)


class TestRun(Base):
    def args(self, **kw):
        d = dict(instruments=["EUR_USD", "XAU_USD"], granularities=["D"],
                 start="2020-01-01", out=self.tmp.name, list_only=False)
        d.update(kw)
        return SimpleNamespace(**d)

    def test_unavailable_skipped(self):
        api = FakeAPI([[candle(1)]], instruments=("EUR_USD",))
        buf = io.StringIO()
        with mock.patch("config.OANDA_API_TOKEN", TOKEN), \
                mock.patch("config.OANDA_ACCOUNT_ID", ACCT), redirect_stdout(buf):
            ox.run(self.args(), api=api, secrets=SECRETS)
        self.assertIn("Skipped (unavailable): XAU_USD", buf.getvalue())
        self.assertTrue((Path(self.tmp.name) / "EUR_USD_D.csv").exists())
        self.assertFalse((Path(self.tmp.name) / "XAU_USD_D.csv").exists())
        self.assertTrue((Path(self.tmp.name) / "MANIFEST.json").exists())

    def test_token_never_leaks(self):
        err = RuntimeError("boom with %s and %s" % (TOKEN, ACCT))
        api = FakeAPI(fail=err)
        buf = io.StringIO()
        with mock.patch("config.OANDA_API_TOKEN", TOKEN), \
                mock.patch("config.OANDA_ACCOUNT_ID", ACCT), redirect_stdout(buf), \
                self.assertLogs("oanda_export", level="DEBUG") as cm:
            log = logging.getLogger("oanda_export")
            log.error("sentinel")  # ensure assertLogs has a record
            ox.run(self.args(), api=api, secrets=SECRETS)
            with self.assertRaises(RuntimeError) as ctx:
                ox.request_with_retry(api, SimpleNamespace(), SECRETS)
        blob = buf.getvalue() + "\n".join(cm.output) + str(ctx.exception)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(ACCT, blob)
        self.assertIn("***", blob)

    def test_per_instrument_failure_logged_and_continues(self):
        class Flaky(FakeAPI):
            def request(self, ep):
                if type(ep).__name__ == "InstrumentsCandles" and "EUR_USD" in ep._endpoint:
                    raise RuntimeError("bad " + TOKEN)
                return super().request(ep)
        api = Flaky([[candle(1)]], instruments=("EUR_USD", "XAU_USD"))
        buf = io.StringIO()
        with mock.patch("config.OANDA_API_TOKEN", TOKEN), \
                mock.patch("config.OANDA_ACCOUNT_ID", ACCT), redirect_stdout(buf), \
                self.assertLogs("oanda_export", level="ERROR") as cm:
            ox.run(self.args(), api=api, secrets=SECRETS)
        self.assertNotIn(TOKEN, "\n".join(cm.output))
        self.assertTrue((Path(self.tmp.name) / "XAU_USD_D.csv").exists())

    def test_retry_on_429_then_success(self):
        class E(Exception):
            code = 429
        calls = {"n": 0}
        class A:
            def request(self, ep):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise E("rate")
                return {"ok": 1}
        self.assertEqual(ox.request_with_retry(A(), object(), SECRETS), {"ok": 1})
        self.assertEqual(calls["n"], 3)


if __name__ == "__main__":
    unittest.main()
