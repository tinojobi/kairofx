"""OANDA REST API wrapper. Handles candles, orders, account info, positions."""
import logging
import time
from typing import Optional
import pandas as pd
from oandapyV20 import API
from oandapyV20.endpoints import instruments, orders, accounts, positions, trades

import config

log = logging.getLogger(__name__)


class OandaClient:
    def __init__(self):
        if not config.OANDA_API_TOKEN or not config.OANDA_ACCOUNT_ID:
            raise RuntimeError("Missing OANDA credentials in .env")
        self.account_id = config.OANDA_ACCOUNT_ID
        self.api = self._build_api()

    def _build_api(self):
        return API(access_token=config.OANDA_API_TOKEN, environment=config.OANDA_ENV)

    @staticmethod
    def _is_auth_like_error(error: Exception) -> bool:
        text = str(error).lower()
        return (
            "insufficient authorization" in text
            or '"401"' in text
            or " 401" in text
            or text.startswith("401")
        )

    def _request_with_retry(self, req, *, operation: str, attempts: int = 3):
        """Run an OANDA request with bounded retry/backoff for transient failures."""
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                self.api.request(req)
                return req.response
            except Exception as e:
                last_error = e
                auth_like = self._is_auth_like_error(e)
                log.warning(
                    "OANDA %s failed on attempt %d/%d%s: %s",
                    operation,
                    attempt,
                    attempts,
                    " (auth-like; rebuilding API client)" if auth_like else "",
                    e,
                )
                if attempt == attempts:
                    raise
                if auth_like:
                    self.api = self._build_api()
                time.sleep(5 * attempt)
        raise last_error

    # ----- Market data -----
    def get_candles(self, instrument: str, granularity: str, count: int) -> pd.DataFrame:
        """Return last N completed candles as a DataFrame."""
        params = {"granularity": granularity, "count": count, "price": "M"}  # mid prices
        req = instruments.InstrumentsCandles(instrument=instrument, params=params)
        self._request_with_retry(req, operation=f"candle request for {instrument} {granularity}")
        rows = []
        for c in req.response["candles"]:
            if not c["complete"]:
                continue
            mid = c["mid"]
            rows.append({
                "time": pd.to_datetime(c["time"]),
                "open": float(mid["o"]),
                "high": float(mid["h"]),
                "low": float(mid["l"]),
                "close": float(mid["c"]),
                "volume": int(c["volume"]),
            })
        df = pd.DataFrame(rows).set_index("time")
        return df

    def get_current_price(self, instrument: str) -> dict:
        """Return current bid/ask."""
        from oandapyV20.endpoints.pricing import PricingInfo
        req = PricingInfo(accountID=self.account_id, params={"instruments": instrument})
        self._request_with_retry(req, operation=f"pricing request for {instrument}")
        p = req.response["prices"][0]
        return {"bid": float(p["bids"][0]["price"]), "ask": float(p["asks"][0]["price"])}

    # ----- Account -----
    def get_account_summary(self) -> dict:
        req = accounts.AccountSummary(accountID=self.account_id)
        self._request_with_retry(req, operation="account summary request")
        s = req.response["account"]
        return {
            "balance": float(s["balance"]),
            "nav": float(s["NAV"]),
            "unrealized_pl": float(s["unrealizedPL"]),
            "open_position_count": int(s["openPositionCount"]),
            "currency": s["currency"],
        }

    def get_open_positions(self) -> list:
        req = positions.OpenPositions(accountID=self.account_id)
        self._request_with_retry(req, operation="open positions request")
        return req.response.get("positions", [])

    # ----- Orders -----
    def place_market_order(
        self,
        instrument: str,
        units: int,             # positive = long, negative = short
        stop_loss_price: float,
        take_profit_price: float,
    ) -> dict:
        """Place a market order with SL and TP attached."""
        data = {
            "order": {
                "type": "MARKET",
                "instrument": instrument,
                "units": str(units),
                "timeInForce": "FOK",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {"price": f"{stop_loss_price:.5f}"},
                "takeProfitOnFill": {"price": f"{take_profit_price:.5f}"},
            }
        }
        req = orders.OrderCreate(accountID=self.account_id, data=data)
        self.api.request(req)
        log.info("Order placed: %s", req.response)
        return req.response
