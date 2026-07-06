from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable


class ExchangeClient(ABC):
    @abstractmethod
    async def get_symbols(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_symbol_rules(self, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...

    @abstractmethod
    async def set_margin_type(self, symbol: str, margin_type: str) -> None: ...

    @abstractmethod
    async def get_account_balance(self) -> float: ...

    @abstractmethod
    async def get_position(self, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str, client_id: str) -> dict[str, Any]: ...

    @abstractmethod
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> None: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> None: ...

    @abstractmethod
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]: ...

    @abstractmethod
    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]: ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> float: ...

    @abstractmethod
    async def get_commission_rate(self, symbol: str) -> dict[str, float]: ...

    def on_order_filled(self, callback: Callable[[dict[str, Any]], None]) -> None:
        raise NotImplementedError

    def on_price_update(self, callback: Callable[[dict[str, Any]], None]) -> None:
        raise NotImplementedError
