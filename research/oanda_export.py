"""READ-ONLY OANDA historical candle exporter for long-horizon research.

Only GET endpoints are used (AccountInstruments, InstrumentsCandles). The API
token and account id are never printed, logged or written; all error text is
scrubbed before it leaves this module.

Run: .venv/bin/python -m research.oanda_export [--granularities D H4]
"""
import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("oanda_export")

DEFAULT_INSTRUMENTS = (
    "EUR_USD GBP_USD USD_JPY AUD_USD USD_CAD USD_CHF NZD_USD EUR_GBP EUR_JPY GBP_JPY "
    "AUD_JPY CAD_JPY CHF_JPY NZD_JPY EUR_AUD EUR_CAD EUR_CHF GBP_AUD GBP_CAD GBP_CHF "
    "AUD_NZD AUD_CAD "
    "XAU_USD XAG_USD "
    "SPX500_USD NAS100_USD US30_USD DE30_EUR UK100_GBP JP225_USD "
    "BCO_USD WTICO_USD "
    "USB10Y_USD"
).split()

HEADER = ["time", "open", "high", "low", "close", "volume",
          "bid_open", "ask_open", "bid_close", "ask_close"]
PAGE_COUNT = 4500
PAUSE = 0.15
ATTEMPTS = 5
BACKOFF_BASE = 1.0
DEFAULT_OUT = "research/data/oanda"


def _secrets():
    import config
    return [s for s in (getattr(config, "OANDA_API_TOKEN", None),
                        getattr(config, "OANDA_ACCOUNT_ID", None)) if s]


def scrub(text, secrets=None):
    text = str(text)
    for s in (secrets if secrets is not None else _secrets()):
        text = text.replace(s, "***")
    return text


def _sleep(seconds):
    time.sleep(seconds)


def _is_transient(exc):
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            code = int(code)
        except (TypeError, ValueError):
            return True
        return code == 429 or code >= 500
    return True  # network-level errors


def request_with_retry(api, endpoint, secrets=None):
    last = None
    for attempt in range(ATTEMPTS):
        try:
            return api.request(endpoint)
        except Exception as exc:  # scrubbed below; never re-raise the original
            last = exc
            if not _is_transient(exc):
                break
            if attempt < ATTEMPTS - 1:
                _sleep(BACKOFF_BASE * (2 ** attempt))
    raise RuntimeError("%s: %s" % (type(last).__name__, scrub(last, secrets))) from None


def rfc3339(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def iso_time(raw):
    """Normalise OANDA time ('2024-01-01T00:00:00.000000000Z') to ISO seconds UTC."""
    return raw[:19] + "Z"


def candle_row(c):
    mid, bid, ask = c["mid"], c["bid"], c["ask"]
    return [iso_time(c["time"]), mid["o"], mid["h"], mid["l"], mid["c"], c.get("volume", 0),
            bid["o"], ask["o"], bid["c"], ask["c"]]


def fetch_candles(api, instrument, granularity, start, include_first=True,
                  now=None, secrets=None, out=None):
    """Page forward from `start` (datetime or RFC3339 str). Appends rows to `out`."""
    from oandapyV20.endpoints.instruments import InstrumentsCandles
    rows = out if out is not None else []
    cursor = start if isinstance(start, str) else rfc3339(start)
    first = include_first
    now = now or datetime.now(timezone.utc)
    while True:
        params = {"granularity": granularity, "price": "BAM",
                  "from": cursor, "count": PAGE_COUNT}
        if not first:
            params["includeFirst"] = False
        resp = request_with_retry(api, InstrumentsCandles(instrument=instrument, params=params),
                                  secrets)
        first = False
        raw = resp.get("candles", []) if resp else []
        if not raw:
            break
        complete = [c for c in raw if c.get("complete")]
        rows.extend(candle_row(c) for c in complete)
        if len(raw) < PAGE_COUNT or not complete:
            break
        new_cursor = complete[-1]["time"]
        if new_cursor == cursor:
            break
        cursor = new_cursor
        _sleep(PAUSE)
        if datetime.strptime(iso_time(cursor), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc) >= now:
            break
    return rows


def read_last_time(path):
    last = None
    with open(path, newline="") as f:
        for row in csv.reader(f):
            if row:
                last = row[0]
    return None if last in (None, "time") else last


def write_rows(path, new_rows):
    """Merge new_rows into CSV at path: dedupe on time, sort ascending."""
    path = Path(path)
    existing = {}
    if path.exists():
        with open(path, newline="") as f:
            for r in csv.reader(f):
                if r and r[0] != "time":
                    existing[r[0]] = r
    for r in new_rows:
        existing[r[0]] = [str(x) for x in r]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for t in sorted(existing):
            w.writerow(existing[t])
    return len(existing)


def export_one(api, instrument, granularity, start, out_dir, secrets=None, now=None):
    path = Path(out_dir) / ("%s_%s.csv" % (instrument, granularity))
    last = read_last_time(path) if path.exists() else None
    rows = []
    try:
        if last:
            fetch_candles(api, instrument, granularity, last, include_first=False,
                          now=now, secrets=secrets, out=rows)
        else:
            fetch_candles(api, instrument, granularity, start, include_first=True,
                          now=now, secrets=secrets, out=rows)
        ok = True
    except Exception as exc:
        log.error("fetch failed for %s %s: %s", instrument, granularity, scrub(exc, secrets))
        ok = False
    if rows:
        write_rows(path, rows)
    return path if path.exists() else None, ok


def manifest_entry(path, instrument, granularity):
    with open(path, newline="") as f:
        data = list(csv.DictReader(f))
    if not data:
        return None
    tail = data[-250:]
    spreads = sorted(float(r["ask_open"]) - float(r["bid_open"]) for r in tail)
    n = len(spreads)
    median = spreads[n // 2] if n % 2 else (spreads[n // 2 - 1] + spreads[n // 2]) / 2
    return {"instrument": instrument, "granularity": granularity, "rows": len(data),
            "first_time": data[0]["time"], "last_time": data[-1]["time"],
            "median_spread_last_250": round(median, 8)}


def write_manifest(out_dir, entries):
    p = Path(out_dir) / "MANIFEST.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2))
    return p


def available_instruments(api, account_id, secrets=None):
    from oandapyV20.endpoints.accounts import AccountInstruments
    resp = request_with_retry(api, AccountInstruments(accountID=account_id), secrets)
    return {i["name"] for i in resp.get("instruments", [])}


def resolve_universe(requested, available):
    use = [i for i in requested if i in available]
    skipped = [i for i in requested if i not in available]
    return use, skipped


def make_api():
    import config
    from oandapyV20 import API
    return API(access_token=config.OANDA_API_TOKEN,
               environment=config.OANDA_ENV or "practice")


def run(args, api=None, secrets=None, now=None):
    import config
    secrets = secrets if secrets is not None else _secrets()
    api = api or make_api()
    account_id = config.OANDA_ACCOUNT_ID
    try:
        avail = available_instruments(api, account_id, secrets)
    except Exception as exc:
        print("Could not list account instruments: %s" % scrub(exc, secrets))
        return 1
    requested = args.instruments or DEFAULT_INSTRUMENTS
    use, skipped = resolve_universe(requested, avail)
    print("Requested: %d  Available: %d" % (len(requested), len(use)))
    print("Available: %s" % " ".join(use))
    if skipped:
        print("Skipped (unavailable): %s" % " ".join(skipped))
    if args.list_only:
        return 0
    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    entries, failures = [], 0
    for inst in use:
        for gran in args.granularities:
            path, ok = export_one(api, inst, gran, start, args.out, secrets, now)
            failures += 0 if ok else 1
            if path:
                e = manifest_entry(path, inst, gran)
                if e:
                    entries.append(e)
                    print("%s %s: %d rows %s -> %s" % (inst, gran, e["rows"],
                                                       e["first_time"], e["last_time"]))
    write_manifest(args.out, entries)
    print("Manifest written: %d files, %d failures" % (len(entries), failures))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Read-only OANDA history exporter")
    p.add_argument("--granularities", nargs="+", default=["D", "H4"])
    p.add_argument("--start", default="2005-01-01")
    p.add_argument("--instruments", nargs="+", default=None)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--list-only", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
