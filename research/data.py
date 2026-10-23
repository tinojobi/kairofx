"""Hourly OHLC data: Yahoo Finance chart API fetch + CSV cache + resampling.

Yahoo prices are mid-ish quotes (not bid/ask) -- spreads are modelled in engine.py.
"""
import argparse
import time
from pathlib import Path

import pandas as pd
import requests

PAIRS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD",
         "USDCHF", "NZDUSD", "EURGBP", "EURJPY", "GBPJPY"]
DATA_DIR = Path(__file__).resolve().parent / "data"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}=X?interval=1h&range=730d"
# Yahoo returns 429 for some UA strings (e.g. the X11/Linux one); this one is accepted.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/124.0 Safari/537.36")


def parse_yahoo(payload: dict) -> pd.DataFrame:
    res = payload["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"],
    }, index=pd.to_datetime(res["timestamp"], unit="s", utc=True))
    df.index.name = "time"
    df = df.dropna()
    df = df[(df.index.minute == 0) & (df.index.second == 0)]  # drop Yahoo's live partial quote row
    now = pd.Timestamp.now(tz="UTC")
    df = df[df.index + pd.Timedelta(hours=1) <= now]            # drop the still-forming current bar
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df.astype(float)


def fetch_yahoo(pair: str, retries: int = 6) -> pd.DataFrame:
    """GET with backoff on 429 and alternating query1/query2 hosts."""
    last = None
    for k in range(retries):
        url = URL.format(sym=pair)
        if k % 2:
            url = url.replace("query1", "query2")
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if r.status_code == 429:
            last = r
            time.sleep(15 * (k + 1))
            continue
        r.raise_for_status()
        return parse_yahoo(r.json())
    last.raise_for_status()


def cache_path(pair: str) -> Path:
    return DATA_DIR / f"{pair}_H1.csv"


def load_csv(path) -> pd.DataFrame:
    """Load an H1 OHLC csv (columns time/open/high/low/close; time parseable, UTC)."""
    df = pd.read_csv(path)
    tcol = "time" if "time" in df.columns else df.columns[0]
    df.index = pd.to_datetime(df[tcol], utc=True)
    df.index.name = "time"
    df = df[["open", "high", "low", "close"]].dropna().astype(float)
    return df[~df.index.duplicated(keep="last")].sort_index()


def get_pair(pair: str, refresh: bool = False) -> pd.DataFrame:
    p = cache_path(pair)
    if p.exists() and not refresh:
        return load_csv(p)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = fetch_yahoo(pair)
    df.to_csv(p)
    return df


def resample_h4(df: pd.DataFrame) -> pd.DataFrame:
    out = df.resample("4h", origin="epoch").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"})
    return out.dropna()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--pairs", nargs="*", default=PAIRS)
    a = ap.parse_args()
    for pr in a.pairs:
        d = get_pair(pr, a.refresh)
        time.sleep(5)
        print(pr, len(d), d.index[0], d.index[-1])
