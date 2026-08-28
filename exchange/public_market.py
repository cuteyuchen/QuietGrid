from __future__ import annotations

import math
from typing import Any

import httpx

from quietgrid_v40.safety import ExecutionLane, ExecutionSafetyPolicy


class PublicMarketRuleParseError(ValueError):
    """Raised when a present public market trading rule cannot be parsed safely."""


_NOTIONAL_FILTER_FIELDS = {
    "MIN_NOTIONAL": ("notional", "minNotional"),
    "NOTIONAL": ("minNotional", "notional"),
}


def _parse_min_notional_filter(filters: dict[str, dict[str, Any]], symbol: str) -> tuple[float, str | None, str | None]:
    for filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
        row = filters.get(filter_type)
        if not isinstance(row, dict):
            continue
        for field in _NOTIONAL_FILTER_FIELDS[filter_type]:
            raw = row.get(field)
            if raw in (None, ""):
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise PublicMarketRuleParseError(
                    f"symbol={symbol} filterType={filter_type} field={field} invalid notional value: {raw!r}"
                ) from exc
            if not math.isfinite(value) or value <= 0:
                raise PublicMarketRuleParseError(
                    f"symbol={symbol} filterType={filter_type} field={field} invalid notional value: {raw!r}"
                )
            return value, filter_type, field
        raise PublicMarketRuleParseError(
            f"symbol={symbol} filterType={filter_type} missing required notional field"
        )
    return 0.0, None, None


class ProductionPublicMarketData:
    """Binance Futures production public market data without private credentials."""

    def __init__(self, *, base_url: str = "https://fapi.binance.com", client: httpx.AsyncClient | None = None, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._timeout = timeout
        self._owns_client = client is None
        self._exchange_info_cache: list[dict[str, Any]] | None = None
        self._policy = ExecutionSafetyPolicy(ExecutionLane.PUBLIC_DATA_ONLY, rest_url=self.base_url)

    async def _get(self, path: str, **params: Any) -> Any:
        endpoint = f"{self.base_url}{path}"
        self._policy.require_public_read(endpoint)
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.get(endpoint, params=params)
            response.raise_for_status()
            return response.json()
        finally:
            if self._owns_client:
                await client.aclose()

    async def get_server_time(self) -> int:
        payload = await self._get("/fapi/v1/time")
        return int(payload["serverTime"])

    async def get_symbols(self) -> list[dict[str, Any]]:
        if self._exchange_info_cache is not None:
            return self._exchange_info_cache
        payload = await self._get("/fapi/v1/exchangeInfo")
        self._exchange_info_cache = list(payload.get("symbols", []))
        return self._exchange_info_cache

    async def get_symbol_rules(self, symbol: str) -> dict[str, Any]:
        for item in await self.get_symbols():
            if str(item.get("symbol")).upper() == symbol.upper():
                return self.get_symbol_rules_from_exchange_info(item)
        raise ValueError(f"未找到公开交易规则: {symbol}")

    def get_symbol_rules_from_exchange_info(self, symbol_info: dict[str, Any]) -> dict[str, Any]:
        symbol = str(symbol_info.get("symbol", "")).upper()
        filters = {
            str(row.get("filterType")): row
            for row in symbol_info.get("filters", [])
            if isinstance(row, dict)
        }
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", filters.get("MARKET_LOT_SIZE", {}))
        min_notional, filter_type, source_field = _parse_min_notional_filter(filters, symbol)
        return {
            "tick_size": float(price.get("tickSize", 0) or 0),
            "step_size": float(lot.get("stepSize", 0) or 0),
            "min_qty": float(lot.get("minQty", 0) or 0),
            "max_qty": float(lot.get("maxQty", 0) or 0),
            "min_notional": min_notional,
            "min_notional_available": filter_type is not None,
            "min_notional_filter_type": filter_type,
            "min_notional_source_field": source_field,
            "price_precision": symbol_info.get("pricePrecision"),
            "quantity_precision": symbol_info.get("quantityPrecision"),
            "contract_type": symbol_info.get("contractType"),
        }

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        rows = await self._get("/fapi/v1/klines", symbol=symbol.upper(), interval=interval, limit=max(1, min(int(limit), 1500)))
        return [
            {
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
                "close_time": int(row[6]),
                "quote_volume": float(row[7]),
                "trades": int(row[8]),
            }
            for row in rows
        ]

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        return dict(await self._get("/fapi/v1/ticker/24hr", symbol=symbol.upper()))

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]:
        return dict(await self._get("/fapi/v1/depth", symbol=symbol.upper(), limit=max(5, min(int(limit), 1000))))

    async def get_funding_rate(self, symbol: str) -> float:
        rows = await self._get("/fapi/v1/fundingRate", symbol=symbol.upper(), limit=1)
        if not rows:
            return 0.0
        return float(rows[-1].get("fundingRate", 0.0))

    async def get_funding_context(self, symbol: str) -> dict[str, Any]:
        payload = await self._get("/fapi/v1/premiumIndex", symbol=symbol.upper())
        return {
            "funding_rate": float(payload.get("lastFundingRate", 0.0) or 0.0),
            "next_funding_time": payload.get("nextFundingTime"),
            "mark_price": payload.get("markPrice"),
        }

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        self._exchange_info_cache = None
