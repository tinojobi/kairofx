"""Broker adapter boundary for broker-neutral performance reporting.

Adapters normalize broker-specific closed-trade payloads into the ClosedTrade
shape used by performance_report.py. This keeps reporting portable if Kairo FX
moves from OANDA to MT5 later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ClosedTrade:
    broker: str
    broker_trade_id: str | None
    instrument: str
    open_time: str
    close_time: str | None
    direction: str
    units: float
    open_price: float | None
    average_close_price: float | None
    realized_pl: float
    take_profit_state: str | None = None
    stop_loss_state: str | None = None


class BrokerAdapter(Protocol):
    name: str

    def fetch_closed_trades(self, *, count: int = 100) -> list[ClosedTrade]:
        ...


class OandaBrokerAdapter:
    name = "oanda"

    def normalize_closed_trade(self, raw: dict[str, Any]) -> ClosedTrade:
        units = float(raw.get("initialUnits") or raw.get("currentUnits") or 0)
        return ClosedTrade(
            broker=self.name,
            broker_trade_id=str(raw.get("id")) if raw.get("id") is not None else None,
            instrument=raw.get("instrument"),
            open_time=raw.get("openTime"),
            close_time=raw.get("closeTime"),
            direction="long" if units > 0 else "short",
            units=units,
            open_price=float(raw["price"]) if raw.get("price") is not None else None,
            average_close_price=float(raw["averageClosePrice"]) if raw.get("averageClosePrice") is not None else None,
            realized_pl=float(raw.get("realizedPL") or 0),
            take_profit_state=(raw.get("takeProfitOrder") or {}).get("state"),
            stop_loss_state=(raw.get("stopLossOrder") or {}).get("state"),
        )

    def fetch_closed_trades(self, *, count: int = 100) -> list[ClosedTrade]:
        from oandapyV20.endpoints.trades import TradesList

        import config
        from oanda_client import OandaClient

        client = OandaClient()
        req = TradesList(accountID=config.OANDA_ACCOUNT_ID, params={"state": "CLOSED", "count": count})
        response = client._request_with_retry(req, operation="closed trades list")
        return [self.normalize_closed_trade(t) for t in response.get("trades", [])]


class Mt5BrokerAdapter:
    name = "mt5"

    def fetch_closed_trades(self, *, count: int = 100) -> list[ClosedTrade]:
        raise NotImplementedError(
            "MT5 closed-trade adapter is not implemented yet. The reporting boundary is ready; "
            "add an MT5 implementation that returns broker_adapters.ClosedTrade records."
        )


def get_broker_adapter(name: str) -> BrokerAdapter:
    broker = name.lower().strip()
    if broker == "oanda":
        return OandaBrokerAdapter()
    if broker == "mt5":
        raise NotImplementedError(
            "MT5 closed-trade adapter is not implemented yet. The reporting boundary is ready; "
            "add an MT5 implementation that returns broker_adapters.ClosedTrade records."
        )
    raise ValueError(f"Unsupported broker: {name}")
