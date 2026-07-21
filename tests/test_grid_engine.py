from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from core.models import (
    GridDirectionMode,
    GridState,
    OrderIntent,
    OrderSide,
    OrderStatus,
    SymbolSession,
)
from exchange.mock import MockExchangeClient
from strategy.grid_calculator import GridConfig, calculate_grid_params
from strategy.grid_engine import GridEngine, GridEngineConfig


def _session() -> SymbolSession:
    klines = [
        {
            "high": (close := 100 + ((idx % 10) - 5) * 0.05) + 0.08,
            "low": close - 0.08,
            "close": close,
        }
        for idx in range(60)
    ]
    params = calculate_grid_params("AAPLUSDT", klines, 100.0, 0.0001, GridConfig())
    return SymbolSession(
        session_id=42,
        symbol="AAPLUSDT",
        state=GridState.RUNNING,
        params=params,
        orders=[],
        realized_pnl=0,
        capital=200,
        leverage=10,
        open_time=datetime.now(timezone.utc),
    )


class RejectOnePostOnlyExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.rejected = False

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        if not self.rejected:
            self.rejected = True
            raise RuntimeError("Order would immediately match and take. Post only rejected.")
        return await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)


class RejectAllPostOnlyExchange(MockExchangeClient):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        raise RuntimeError("Order would immediately match and take. Post only rejected.")


class ExcessiveSeedSlippageExchange(MockExchangeClient):
    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ):
        response = await super().place_market_order(
            symbol,
            side,
            qty,
            reduce_only,
            position_side,
            client_id,
        )
        response["avgPrice"] = 100.3
        return response


class MissingOrderIdExchange(MockExchangeClient):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        return {"symbol": symbol}


class MissingRefillOrderIdExchange(MockExchangeClient):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        if "-re-" in client_id:
            return {"symbol": symbol}
        return {"client_id": client_id}


class MismatchedOrderClientIdExchange(MockExchangeClient):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        return {"orderId": "exchange-order-1", "clientOrderId": "wrong-client-id"}


class MismatchedRefillClientIdExchange(MockExchangeClient):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        if "-re-" in client_id:
            return {"orderId": "exchange-refill-1", "clientOrderId": "wrong-client-id"}
        return {"client_id": client_id}


class DelayedInitialOrderLookupExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed_client_id: str | None = None
        self.lookup_calls = 0

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        if self.failed_client_id is None:
            self.failed_client_id = client_id
            raise RuntimeError(
                "APIError(code=-1007): Timeout waiting for response from backend server. "
                "Send status unknown; execution status unknown."
            )
        return {"client_id": client_id}

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        if client_id == self.failed_client_id:
            self.lookup_calls += 1
            if self.lookup_calls < 3:
                return {"symbol": symbol, "orderId": "", "client_id": client_id, "status": "UNKNOWN"}
        return await super().get_order(symbol, order_id, client_id)


class DelayedRefillOrderLookupExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed_refill_client_id: str | None = None
        self.refill_lookup_calls = 0

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        if "-re-" in client_id and self.failed_refill_client_id is None:
            self.failed_refill_client_id = client_id
            raise RuntimeError(
                "APIError(code=-1007): Timeout waiting for response from backend server. "
                "Send status unknown; execution status unknown."
            )
        return {"client_id": client_id}

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        if client_id == self.failed_refill_client_id:
            self.refill_lookup_calls += 1
            if self.refill_lookup_calls < 3:
                return {"symbol": symbol, "orderId": "", "client_id": client_id, "status": "UNKNOWN"}
        return await super().get_order(symbol, order_id, client_id)


class CoarseRulesExchange(MockExchangeClient):
    async def get_symbol_rules(self, symbol: str):
        return {"tick_size": 0.05, "step_size": 0.1, "min_qty": 0.1}


class TooLargeMinQtyExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.margin_type_calls = 0
        self.leverage_calls = 0

    async def get_symbol_rules(self, symbol: str):
        return {"tick_size": 0.01, "step_size": 0.1, "min_qty": 999999}

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.margin_type_calls += 1
        return await super().set_margin_type(symbol, margin_type)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage_calls += 1
        return await super().set_leverage(symbol, leverage)


class TooLargeMinNotionalExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.margin_type_calls = 0
        self.leverage_calls = 0

    async def get_symbol_rules(self, symbol: str):
        return {"tick_size": 0.01, "step_size": 0.1, "min_qty": 0.1, "min_notional": 1_000_000}

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.margin_type_calls += 1
        return await super().set_margin_type(symbol, margin_type)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage_calls += 1
        return await super().set_leverage(symbol, leverage)


class InvalidRulesExchange(MockExchangeClient):
    def __init__(self, rules: dict) -> None:
        super().__init__()
        self.rules = rules
        self.margin_type_calls = 0
        self.leverage_calls = 0

    async def get_symbol_rules(self, symbol: str):
        return self.rules

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.margin_type_calls += 1
        return await super().set_margin_type(symbol, margin_type)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage_calls += 1
        return await super().set_leverage(symbol, leverage)


class CoarseTickTrackingExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.margin_type_calls = 0
        self.leverage_calls = 0

    async def get_symbol_rules(self, symbol: str):
        return {"tick_size": 1.0, "step_size": 0.1, "min_qty": 0.1}

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.margin_type_calls += 1
        return await super().set_margin_type(symbol, margin_type)

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage_calls += 1
        return await super().set_leverage(symbol, leverage)


class MissingPositionQtyExchange(MockExchangeClient):
    async def get_position(self, symbol: str):
        return {"symbol": symbol}


class NonFinitePositionQtyExchange(MockExchangeClient):
    async def get_position(self, symbol: str):
        return {"symbol": symbol, "qty": "nan"}


class HedgePositionExchange(MockExchangeClient):
    async def get_position(self, symbol: str):
        return {
            "symbol": symbol,
            "qty": 0.0,
            "long_qty": 0.3,
            "short_qty": 0.2,
        }


class MissingMarketOrderIdExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.positions["AAPLUSDT"] = 1.0

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ):
        await super().place_market_order(symbol, side, qty, reduce_only, position_side, client_id)
        return {"symbol": symbol, "status": "filled"}


class DelayedMarketOrderLookupExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.positions["AAPLUSDT"] = 1.0
        self.failed_market_client_id: str | None = None
        self.market_lookup_calls = 0

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ):
        response = await super().place_market_order(symbol, side, qty, reduce_only, position_side, client_id)
        self.market_orders[-1]["orderId"] = response["orderId"]
        if self.failed_market_client_id is None:
            self.failed_market_client_id = client_id
            raise RuntimeError(
                "APIError(code=-1007): Timeout waiting for response from backend server. "
                "Send status unknown; execution status unknown."
            )
        return response

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        if client_id == self.failed_market_client_id:
            self.market_lookup_calls += 1
            if self.market_lookup_calls < 3:
                return {"symbol": symbol, "orderId": "", "client_id": client_id, "status": "UNKNOWN"}
            for order in self.market_orders:
                if order.get("client_id") == client_id:
                    return {**order, "status": "FILLED"}
        return await super().get_order(symbol, order_id, client_id)


class FailingStopOrderExchange(MockExchangeClient):
    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        raise RuntimeError("stop order rejected")


class FailingSecondStopOrderExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.stop_attempts = 0

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        self.stop_attempts += 1
        if self.stop_attempts == 2:
            raise RuntimeError("short stop order rejected")
        return await super().place_stop_market_order(symbol, side, stop_price, client_id, close_position)


class OpenPositionRequiredSecondStopOrderExchange(FailingSecondStopOrderExchange):
    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        self.stop_attempts += 1
        if self.stop_attempts == 2:
            raise RuntimeError(
                "APIError(code=-4509): Time in Force (TIF) GTE can only be used with open positions. "
                "Please ensure that positions are available."
            )
        return await MockExchangeClient.place_stop_market_order(self, symbol, side, stop_price, client_id, close_position)


class MissingStopOrderIdExchange(MockExchangeClient):
    def __init__(self, fail_on_attempt: int = 1) -> None:
        super().__init__()
        self.fail_on_attempt = fail_on_attempt
        self.stop_attempts = 0

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        self.stop_attempts += 1
        await super().place_stop_market_order(symbol, side, stop_price, client_id, close_position)
        if self.stop_attempts == self.fail_on_attempt:
            return {"symbol": symbol}
        return {"client_id": client_id}


class MismatchedStopClientIdExchange(MissingStopOrderIdExchange):
    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        self.stop_attempts += 1
        await MockExchangeClient.place_stop_market_order(self, symbol, side, stop_price, client_id, close_position)
        if self.stop_attempts == self.fail_on_attempt:
            return {"orderId": f"stop-{self.stop_attempts}", "clientOrderId": "wrong-client-id"}
        return {"client_id": client_id}


class DelayedStopOrderLookupExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed_stop_client_id: str | None = None
        self.stop_lookup_calls = 0

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        await super().place_stop_market_order(symbol, side, stop_price, client_id, close_position)
        if self.failed_stop_client_id is None:
            self.failed_stop_client_id = client_id
            raise RuntimeError(
                "APIError(code=-1007): Timeout waiting for response from backend server. "
                "Send status unknown; execution status unknown."
            )
        return {"client_id": client_id}

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        if client_id == self.failed_stop_client_id:
            self.stop_lookup_calls += 1
            if self.stop_lookup_calls < 3:
                return {"symbol": symbol, "orderId": "", "client_id": client_id, "status": "UNKNOWN"}
            for order in self.stop_orders.get(symbol, []):
                if order.get("client_id") == client_id:
                    return {**order, "status": "open"}
        return await super().get_order(symbol, order_id, client_id)


class FailingStopAndCancelExchange(FailingStopOrderExchange):
    async def cancel_all_orders(self, symbol: str) -> None:
        raise RuntimeError("cancel all unavailable")


class FailingGetOrderExchange(MockExchangeClient):
    async def get_order(self, symbol: str, order_id: str, client_id: str):
        raise RuntimeError("order lookup timed out")


class UnderfilledFilledOrderExchange(MockExchangeClient):
    async def get_order(self, symbol: str, order_id: str, client_id: str):
        return {
            "symbol": symbol,
            "orderId": order_id,
            "client_id": client_id,
            "status": "FILLED",
            "avgPrice": "99.5",
            "executedQty": "0.123",
        }


class InvalidFilledDetailsExchange(MockExchangeClient):
    def __init__(self, response: dict) -> None:
        super().__init__()
        self.response = response

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        return {
            "symbol": symbol,
            "orderId": order_id,
            "client_id": client_id,
            "status": "FILLED",
            **self.response,
        }


def test_grid_engine_places_post_only_orders() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        orders = await engine.start(session, current_price=100.0)

        assert orders
        assert len(exchange.orders["AAPLUSDT"]) == len(orders)
        assert len(exchange.stop_orders["AAPLUSDT"]) == 2
        assert all(raw["timeInForce"] == "GTX" for raw in exchange.orders["AAPLUSDT"])
        assert all(
            raw["position_side"] == ("LONG" if raw["side"] == "BUY" else "SHORT")
            for raw in exchange.orders["AAPLUSDT"]
        )
        assert {order["side"] for order in exchange.stop_orders["AAPLUSDT"]} == {"SELL", "BUY"}
        assert all(order["type"] == "STOP_MARKET" for order in exchange.stop_orders["AAPLUSDT"])
        assert all(order["closePosition"] is True for order in exchange.stop_orders["AAPLUSDT"])
        stop_by_side = {order["side"]: order for order in exchange.stop_orders["AAPLUSDT"]}
        assert stop_by_side["SELL"]["stopPrice"] < session.params.lower  # type: ignore[union-attr]
        assert stop_by_side["BUY"]["stopPrice"] > session.params.upper  # type: ignore[union-attr]
        assert all(order.status == OrderStatus.OPEN for order in orders)
        assert {order.side.value for order in orders} == {"BUY", "SELL"}

    asyncio.run(run())


def test_grid_engine_recovers_initial_order_after_unknown_create(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr("strategy.grid_engine.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        exchange = DelayedInitialOrderLookupExchange()
        session = _session()
        engine = GridEngine(exchange)

        orders = await engine.start(session, current_price=100.0)

        assert orders
        assert exchange.lookup_calls == 3
        assert exchange.failed_client_id in {order.client_id for order in orders}
        assert all(order.status == OrderStatus.OPEN for order in orders)
        assert len(exchange.stop_orders["AAPLUSDT"]) == 2

    asyncio.run(run())


def test_grid_engine_cancels_grid_orders_when_stop_order_fails() -> None:
    async def run() -> None:
        exchange = FailingStopOrderExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=100.0)
        except RuntimeError as exc:
            assert "stop order rejected" in str(exc)
        else:
            raise AssertionError("stop order failure should abort grid start")

        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert session.orders
        assert all(order.status == OrderStatus.CANCELLED for order in session.orders)

    asyncio.run(run())


def test_grid_engine_delays_missing_side_stop_when_exchange_requires_open_position() -> None:
    async def run() -> None:
        exchange = OpenPositionRequiredSecondStopOrderExchange()
        session = _session()
        engine = GridEngine(exchange)

        orders = await engine.start(session, current_price=100.0)

        assert orders
        assert all(order.status == OrderStatus.OPEN for order in session.orders)
        assert session.stop_protection_sides == {"long"}
        assert len(exchange.orders["AAPLUSDT"]) == len(orders)
        assert len(exchange.stop_orders["AAPLUSDT"]) == 1

    asyncio.run(run())


def test_grid_engine_recovers_stop_order_after_unknown_create(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr("strategy.grid_engine.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        exchange = DelayedStopOrderLookupExchange()
        session = _session()
        engine = GridEngine(exchange)

        orders = await engine.start(session, current_price=100.0)

        assert orders
        assert exchange.stop_lookup_calls == 3
        assert exchange.failed_stop_client_id == "qg-42-stop-long"
        assert session.stop_protection_sides == {"long", "short"}
        assert len(exchange.stop_orders["AAPLUSDT"]) == 2
        assert all(order.status == OrderStatus.OPEN for order in orders)

    asyncio.run(run())


def test_grid_engine_recovers_delayed_stop_protection_after_unknown_create(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr("strategy.grid_engine.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        exchange = DelayedStopOrderLookupExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.ensure_stop_protection_for_position(session, 0.1)

        assert exchange.stop_lookup_calls == 3
        assert exchange.failed_stop_client_id == "qg-42-stop-long-pos"
        assert session.stop_protection_sides == {"long"}
        assert len(exchange.stop_orders["AAPLUSDT"]) == 1

    asyncio.run(run())


def test_grid_engine_cancels_grid_orders_when_order_response_lacks_id() -> None:
    async def run() -> None:
        exchange = MissingOrderIdExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=100.0)
        except ValueError as exc:
            assert "订单响应缺少订单标识" in str(exc)
        else:
            raise AssertionError("missing exchange order id should abort grid start")

        assert session.orders == []
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []

    asyncio.run(run())


def test_grid_engine_cancels_grid_orders_when_order_response_client_id_mismatches() -> None:
    async def run() -> None:
        exchange = MismatchedOrderClientIdExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=100.0)
        except ValueError as exc:
            assert "client id 不匹配" in str(exc)
        else:
            raise AssertionError("mismatched order client id should abort grid start")

        assert session.orders == []
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []

    asyncio.run(run())


def test_grid_engine_cancels_grid_orders_when_second_stop_order_fails() -> None:
    async def run() -> None:
        exchange = FailingSecondStopOrderExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=100.0)
        except RuntimeError as exc:
            assert "short stop order rejected" in str(exc)
        else:
            raise AssertionError("second stop order failure should abort grid start")

        assert exchange.stop_attempts == 2
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert session.orders
        assert all(order.status == OrderStatus.CANCELLED for order in session.orders)

    asyncio.run(run())


def test_grid_engine_cancels_grid_orders_when_stop_order_response_lacks_id() -> None:
    async def run() -> None:
        for fail_on_attempt in (1, 2):
            exchange = MissingStopOrderIdExchange(fail_on_attempt=fail_on_attempt)
            session = _session()
            engine = GridEngine(exchange)

            try:
                await engine.start(session, current_price=100.0)
            except ValueError as exc:
                assert "订单响应缺少订单标识" in str(exc)
            else:
                raise AssertionError("missing stop order id should abort grid start")

            assert exchange.stop_attempts == fail_on_attempt
            assert exchange.orders["AAPLUSDT"] == []
            assert exchange.stop_orders["AAPLUSDT"] == []
            assert session.orders
            assert all(order.status == OrderStatus.CANCELLED for order in session.orders)

    asyncio.run(run())


def test_grid_engine_cancels_grid_orders_when_stop_order_response_client_id_mismatches() -> None:
    async def run() -> None:
        for fail_on_attempt in (1, 2):
            exchange = MismatchedStopClientIdExchange(fail_on_attempt=fail_on_attempt)
            session = _session()
            engine = GridEngine(exchange)

            try:
                await engine.start(session, current_price=100.0)
            except ValueError as exc:
                assert "client id 不匹配" in str(exc)
            else:
                raise AssertionError("mismatched stop order client id should abort grid start")

            assert exchange.stop_attempts == fail_on_attempt
            assert exchange.orders["AAPLUSDT"] == []
            assert exchange.stop_orders["AAPLUSDT"] == []
            assert session.orders
            assert all(order.status == OrderStatus.CANCELLED for order in session.orders)

    asyncio.run(run())


def test_grid_engine_keeps_created_orders_open_when_stop_failure_cleanup_fails() -> None:
    async def run() -> None:
        logs = []
        exchange = FailingStopAndCancelExchange()
        session = _session()
        engine = GridEngine(exchange, log_system=lambda *args: logs.append(args))

        try:
            await engine.start(session, current_price=100.0)
        except RuntimeError as exc:
            assert "cancel all unavailable" in str(exc)
        else:
            raise AssertionError("cleanup failure should abort grid start")

        assert exchange.orders["AAPLUSDT"]
        assert session.orders
        assert all(order.status == OrderStatus.OPEN for order in session.orders)
        assert logs
        assert logs[0][0] == "WARN"
        assert logs[0][1] == "force_close"
        assert "grid orders may still be open" in logs[0][2]

    asyncio.run(run())


def test_grid_engine_uses_exchange_symbol_rules_for_rounding() -> None:
    async def run() -> None:
        exchange = CoarseRulesExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)

        first_order = exchange.orders["AAPLUSDT"][0]
        assert round(first_order["price"] / 0.05) == first_order["price"] / 0.05
        qty_units = first_order["qty"] / 0.1
        assert abs(round(qty_units) - qty_units) < 1e-9
        for stop_order in exchange.stop_orders["AAPLUSDT"]:
            stop_units = stop_order["stopPrice"] / 0.05
            assert abs(round(stop_units) - stop_units) < 1e-9

    asyncio.run(run())


def test_grid_engine_round_to_step_avoids_binary_precision_tails() -> None:
    assert GridEngine._round_to_step(230.68313099034734, 0.01) == 230.68
    assert str(GridEngine._round_to_step(230.68313099034734, 0.01)) == "230.68"
    assert GridEngine._round_to_step(0.006451234, 0.001) == 0.006
    assert str(GridEngine._round_to_step(0.006451234, 0.001)) == "0.006"


def test_grid_engine_rejects_qty_below_exchange_minimum() -> None:
    async def run() -> None:
        exchange = TooLargeMinQtyExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=100.0)
        except ValueError as exc:
            assert "最小下单量" in str(exc)
        else:
            raise AssertionError("qty below minQty should be rejected")

        assert exchange.margin_type_calls == 0
        assert exchange.leverage_calls == 0

    asyncio.run(run())


def test_grid_engine_rejects_invalid_start_inputs_before_exchange_side_effects() -> None:
    async def run() -> None:
        invalid_cases = [
            ("nan", "当前价格"),
            (0.0, "当前价格"),
        ]
        for current_price, message in invalid_cases:
            exchange = MockExchangeClient()
            session = _session()
            engine = GridEngine(exchange)
            try:
                await engine.start(session, current_price=current_price)  # type: ignore[arg-type]
            except ValueError as exc:
                assert message in str(exc)
            else:
                raise AssertionError("invalid current price should fail closed")
            assert exchange.orders.get("AAPLUSDT", []) == []
            assert exchange.stop_orders.get("AAPLUSDT", []) == []

        for field, value, message in (
            ("capital", float("nan"), "会话本金"),
            ("leverage", 0, "杠杆倍数"),
            ("grid_num", 0, "网格数量"),
        ):
            exchange = MockExchangeClient()
            session = _session()
            if field == "grid_num":
                assert session.params is not None
                session.params = replace(session.params, grid_num=value)
            else:
                setattr(session, field, value)
            engine = GridEngine(exchange)
            try:
                await engine.start(session, current_price=100.0)
            except ValueError as exc:
                assert message in str(exc)
            else:
                raise AssertionError("invalid grid start input should fail closed")
            assert exchange.orders.get("AAPLUSDT", []) == []
            assert exchange.stop_orders.get("AAPLUSDT", []) == []

    asyncio.run(run())


def test_grid_engine_rejects_notional_below_exchange_minimum_before_placing_orders() -> None:
    async def run() -> None:
        exchange = TooLargeMinNotionalExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=100.0)
        except ValueError as exc:
            assert "最小名义金额" in str(exc)
        else:
            raise AssertionError("notional below minimum should be rejected")

        assert exchange.orders.get("AAPLUSDT", []) == []
        assert exchange.stop_orders.get("AAPLUSDT", []) == []
        assert exchange.margin_type_calls == 0
        assert exchange.leverage_calls == 0
        assert session.orders == []

    asyncio.run(run())


def test_grid_engine_rejects_invalid_symbol_rules_before_placing_orders() -> None:
    async def run() -> None:
        invalid_rules = [
            {"tick_size": "nan", "step_size": 0.1, "min_qty": 0.1},
            {"tick_size": 0.01, "step_size": "inf", "min_qty": 0.1},
            {"tick_size": 0.01, "step_size": 0.1, "min_qty": "-1"},
            {"tick_size": 0.01, "step_size": 0.1, "min_qty": 0.1, "min_notional": "inf"},
        ]
        for rules in invalid_rules:
            exchange = InvalidRulesExchange(rules)
            session = _session()
            engine = GridEngine(exchange)

            try:
                await engine.start(session, current_price=100.0)
            except ValueError as exc:
                assert "symbol rule" in str(exc)
            else:
                raise AssertionError("invalid exchange symbol rules should fail closed")

            assert exchange.orders.get("AAPLUSDT", []) == []
            assert exchange.stop_orders.get("AAPLUSDT", []) == []
            assert exchange.margin_type_calls == 0
            assert exchange.leverage_calls == 0
            assert session.orders == []

    asyncio.run(run())


def test_grid_engine_rejects_zero_rounded_grid_price_before_exchange_side_effects() -> None:
    async def run() -> None:
        exchange = CoarseTickTrackingExchange()
        session = _session()
        assert session.params is not None
        session.params = replace(
            session.params,
            lower=0.4,
            upper=0.6,
            center=0.5,
            grid_num=2,
            grid_prices=[0.4, 0.6],
            stop_loss_price=0.3,
        )
        session.capital = 200
        session.leverage = 10
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=0.5)
        except ValueError as exc:
            assert "网格价格" in str(exc)
        else:
            raise AssertionError("zero rounded grid price should fail closed")

        assert exchange.orders.get("AAPLUSDT", []) == []
        assert exchange.stop_orders.get("AAPLUSDT", []) == []
        assert exchange.margin_type_calls == 0
        assert exchange.leverage_calls == 0
        assert session.orders == []

    asyncio.run(run())


def test_grid_engine_rejects_zero_rounded_stop_price_before_exchange_side_effects() -> None:
    async def run() -> None:
        exchange = CoarseTickTrackingExchange()
        session = _session()
        assert session.params is not None
        session.params = replace(
            session.params,
            lower=1.1,
            upper=1.3,
            center=1.2,
            grid_num=2,
            grid_prices=[1.1, 1.3],
            stop_loss_price=0.4,
        )
        session.capital = 200
        session.leverage = 10
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=1.2)
        except ValueError as exc:
            assert "止损价格" in str(exc)
        else:
            raise AssertionError("zero rounded stop price should fail closed")

        assert exchange.orders.get("AAPLUSDT", []) == []
        assert exchange.stop_orders.get("AAPLUSDT", []) == []
        assert exchange.margin_type_calls == 0
        assert exchange.leverage_calls == 0
        assert session.orders == []

    asyncio.run(run())


def test_grid_engine_skips_post_only_rejections() -> None:
    async def run() -> None:
        exchange = RejectOnePostOnlyExchange()
        session = _session()
        engine = GridEngine(exchange)

        orders = await engine.start(session, current_price=100.0)

        assert exchange.rejected is True
        assert orders
        assert len(orders) == len(exchange.orders["AAPLUSDT"])
        assert len(exchange.stop_orders["AAPLUSDT"]) == 2

    asyncio.run(run())


def test_grid_engine_rejects_start_when_all_post_only_orders_are_rejected() -> None:
    async def run() -> None:
        exchange = RejectAllPostOnlyExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.start(session, current_price=100.0)
        except ValueError as exc:
            assert "POST_ONLY" in str(exc)
        else:
            raise AssertionError("all post-only rejections should abort grid start")

        assert exchange.orders.get("AAPLUSDT", []) == []
        assert exchange.stop_orders.get("AAPLUSDT", []) == []
        assert session.orders == []

    asyncio.run(run())


def test_grid_engine_stop_cancels_open_orders() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        await engine.stop(session, reason="test")

        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert all(order.status == OrderStatus.CANCELLED for order in session.orders)

    asyncio.run(run())


def test_grid_engine_force_close_rejects_position_without_quantity() -> None:
    async def run() -> None:
        exchange = MissingPositionQtyExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.force_close(session, reason="test")
        except ValueError as exc:
            assert "持仓响应缺少数量字段" in str(exc)
        else:
            raise AssertionError("missing position quantity should fail closed")

        assert exchange.market_orders == []

    asyncio.run(run())


def test_grid_engine_force_close_rejects_non_finite_position_quantity() -> None:
    async def run() -> None:
        exchange = NonFinitePositionQtyExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.force_close(session, reason="test")
        except ValueError as exc:
            assert "有限数字" in str(exc)
        else:
            raise AssertionError("non-finite position quantity should fail closed")

        assert exchange.market_orders == []

    asyncio.run(run())


def test_grid_engine_force_close_closes_hedge_long_and_short_positions() -> None:
    async def run() -> None:
        exchange = HedgePositionExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.force_close(session, reason="test")

        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 0.3,
                "reduce_only": True,
                "position_side": "LONG",
                "client_id": "qg-42-close-long",
                "status": "filled",
            },
            {
                "symbol": "AAPLUSDT",
                "side": "BUY",
                "qty": 0.2,
                "reduce_only": True,
                "position_side": "SHORT",
                "client_id": "qg-42-close-short",
                "status": "filled",
            },
        ]

    asyncio.run(run())


def test_grid_engine_force_close_recovers_market_order_after_unknown_create(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr("strategy.grid_engine.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        exchange = DelayedMarketOrderLookupExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.force_close(session, reason="test")

        assert exchange.market_lookup_calls == 3
        assert exchange.failed_market_client_id == "qg-42-close-sell"
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 1.0,
                "reduce_only": True,
                "client_id": "qg-42-close-sell",
                "status": "filled",
                "orderId": "market-1",
            }
        ]

    asyncio.run(run())


def test_grid_engine_force_close_rejects_market_response_without_order_id() -> None:
    async def run() -> None:
        exchange = MissingMarketOrderIdExchange()
        session = _session()
        engine = GridEngine(exchange)

        try:
            await engine.force_close(session, reason="test")
        except ValueError as exc:
            assert "订单响应缺少订单标识" in str(exc)
        else:
            raise AssertionError("missing market order id should fail closed")

        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 1.0,
                "reduce_only": True,
                "client_id": "qg-42-close-sell",
                "status": "filled",
            }
        ]

    asyncio.run(run())


def test_grid_engine_handles_fill_by_placing_opposite_order() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        new_order = await engine.handle_order_filled(session, buy_order.client_id, fill_price=buy_order.price)

        assert buy_order.status == OrderStatus.FILLED
        assert new_order is not None
        assert new_order.side.value == "SELL"
        assert new_order.grid_index == buy_order.grid_index + 1
        assert new_order.status == OrderStatus.OPEN
        assert exchange.orders["AAPLUSDT"][-1]["timeInForce"] == "GTX"
        assert exchange.orders["AAPLUSDT"][-1]["position_side"] == "LONG"

    asyncio.run(run())


def test_grid_engine_uses_symbol_specific_fractional_reduce_target() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(
            exchange,
            GridEngineConfig(
                reduce_target_step_fraction_by_symbol={"AAPLUSDT": 0.5},
            ),
        )

        await engine.start(session, current_price=100.0)
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        new_order = await engine.handle_order_filled(
            session,
            buy_order.client_id,
            fill_price=buy_order.price,
        )

        assert new_order is not None
        full_step_target = session.params.grid_prices[buy_order.grid_index + 1]  # type: ignore[union-attr]
        expected = buy_order.price + (full_step_target - buy_order.price) * 0.5
        assert new_order.price == pytest.approx(expected, abs=0.01)
        assert new_order.order_intent == OrderIntent.REDUCE

    asyncio.run(run())


def test_grid_engine_enforces_and_restores_symbol_lot_cap() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)
        await engine.start(session, current_price=100.0)
        initial_buy_count = sum(
            1 for order in session.orders
            if order.side == OrderSide.BUY and order.order_intent == OrderIntent.OPEN
        )

        cancelled = await engine.enforce_unpaired_lot_cap(
            session,
            long_lot_count=1,
            short_lot_count=0,
            max_lots_per_side=1,
            client_id_tag="one",
        )
        assert cancelled
        assert all(
            order.status != OrderStatus.OPEN
            for order in session.orders
            if order.side == OrderSide.BUY and order.order_intent == OrderIntent.OPEN
        )

        restored = await engine.enforce_unpaired_lot_cap(
            session,
            long_lot_count=0,
            short_lot_count=0,
            max_lots_per_side=1,
            client_id_tag="two",
        )
        assert len(restored) == initial_buy_count
        assert sum(
            1 for order in session.orders
            if order.side == OrderSide.BUY
            and order.order_intent == OrderIntent.OPEN
            and order.status == OrderStatus.OPEN
        ) == initial_buy_count

    asyncio.run(run())


def test_grid_engine_rounds_reconciled_refill_quantity_to_symbol_step_size() -> None:
    async def run() -> None:
        exchange = CoarseRulesExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        buy_order.qty = 0.30000000000000004

        new_order = await engine.handle_order_filled(
            session,
            buy_order.client_id,
            fill_price=buy_order.price,
        )

        assert new_order is not None
        assert new_order.qty == 0.3
        assert exchange.orders["AAPLUSDT"][-1]["qty"] == 0.3

    asyncio.run(run())


def test_grid_engine_recovers_refill_order_after_unknown_create(monkeypatch) -> None:
    async def run() -> None:
        monkeypatch.setattr("strategy.grid_engine.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        exchange = DelayedRefillOrderLookupExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        order_count = len(session.orders)
        new_order = await engine.handle_order_filled(session, buy_order.client_id, fill_price=buy_order.price)

        assert new_order is not None
        assert exchange.refill_lookup_calls == 3
        assert new_order.client_id == exchange.failed_refill_client_id
        assert new_order.status == OrderStatus.OPEN
        assert len(session.orders) == order_count + 1

    asyncio.run(run())


def test_grid_engine_rejects_invalid_fill_price_without_mutating_order() -> None:
    async def run() -> None:
        invalid_fill_prices = ["nan", "inf", 0]
        for fill_price in invalid_fill_prices:
            exchange = MockExchangeClient()
            session = _session()
            engine = GridEngine(exchange)

            await engine.start(session, current_price=100.0)
            buy_order = next(order for order in session.orders if order.side.value == "BUY")
            order_count = len(session.orders)
            exchange_order_count = len(exchange.orders["AAPLUSDT"])

            try:
                await engine.handle_order_filled(session, buy_order.client_id, fill_price=fill_price)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid fill_price should fail closed")

            assert buy_order.status == OrderStatus.OPEN
            assert buy_order.fill_price is None
            assert len(session.orders) == order_count
            assert len(exchange.orders["AAPLUSDT"]) == exchange_order_count

    asyncio.run(run())


def test_grid_engine_rejects_refill_response_without_order_id() -> None:
    async def run() -> None:
        exchange = MissingRefillOrderIdExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        order_count = len(session.orders)

        try:
            await engine.handle_order_filled(session, buy_order.client_id, fill_price=buy_order.price)
        except ValueError as exc:
            assert "订单响应缺少订单标识" in str(exc)
        else:
            raise AssertionError("missing refill order id should fail closed")

        assert buy_order.status == OrderStatus.FILLED
        assert len(session.orders) == order_count

    asyncio.run(run())


def test_grid_engine_rejects_refill_response_with_mismatched_client_id() -> None:
    async def run() -> None:
        exchange = MismatchedRefillClientIdExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        order_count = len(session.orders)

        try:
            await engine.handle_order_filled(session, buy_order.client_id, fill_price=buy_order.price)
        except ValueError as exc:
            assert "client id 不匹配" in str(exc)
        else:
            raise AssertionError("mismatched refill client id should fail closed")

        assert buy_order.status == OrderStatus.FILLED
        assert len(session.orders) == order_count

    asyncio.run(run())


def test_grid_engine_tracks_short_cycle_pnl_and_resets_next_entry() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        sell_order = next(order for order in session.orders if order.side.value == "SELL")
        buy_order = await engine.handle_order_filled(session, sell_order.client_id, fill_price=sell_order.price)

        assert buy_order is not None
        assert buy_order.side.value == "BUY"
        assert buy_order.entry_price == sell_order.price
        assert exchange.orders["AAPLUSDT"][-1]["position_side"] == "SHORT"
        assert engine.grid_pnl_for_fill(sell_order, sell_order.price) is None

        pnl = engine.grid_pnl_for_fill(buy_order, buy_order.price)
        next_sell_order = await engine.handle_order_filled(session, buy_order.client_id, fill_price=buy_order.price)

        assert pnl is not None
        assert pnl > 0
        assert pnl == (buy_order.entry_price - buy_order.price) * buy_order.qty
        assert next_sell_order is not None
        assert next_sell_order.side.value == "SELL"
        assert next_sell_order.entry_price is None
        assert exchange.orders["AAPLUSDT"][-1]["position_side"] == "SHORT"

    asyncio.run(run())


def test_grid_engine_rejects_invalid_grid_pnl_inputs() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        sell_order = next(order for order in session.orders if order.side.value == "SELL")
        buy_order = await engine.handle_order_filled(session, sell_order.client_id, fill_price=sell_order.price)

        assert buy_order is not None
        invalid_cases = [
            ("entry_price", "nan"),
            ("entry_price", "inf"),
            ("entry_price", 0),
            ("qty", "nan"),
            ("qty", -1),
            ("fill_price", "nan"),
            ("fill_price", 0),
        ]
        for field, value in invalid_cases:
            test_order = replace(buy_order)
            fill_price = buy_order.price
            if field == "entry_price":
                test_order.entry_price = value  # type: ignore[assignment]
            elif field == "qty":
                test_order.qty = value  # type: ignore[assignment]
            else:
                fill_price = value

            try:
                engine.grid_pnl_for_fill(test_order, fill_price)  # type: ignore[arg-type]
            except ValueError:
                pass
            else:
                raise AssertionError(f"invalid {field} should fail closed")

    asyncio.run(run())


def test_grid_engine_sync_marks_missing_open_orders_as_filled() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        for order in session.orders:
            exchange.order_statuses[order.client_id] = "FILLED"
        exchange.orders["AAPLUSDT"] = []

        sync_result = await engine.sync_orders(session)

        assert all(order.status == OrderStatus.FILLED for order in session.orders)
        assert [event.order for event in sync_result.filled] == session.orders
        assert sync_result.partially_filled == []

    asyncio.run(run())


def test_grid_engine_sync_does_not_treat_cancelled_orders_as_fills() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        cancelled_order = session.orders[0]
        exchange.order_statuses[cancelled_order.client_id] = "CANCELED"
        exchange.orders["AAPLUSDT"] = [
            raw for raw in exchange.orders["AAPLUSDT"] if raw["client_id"] != cancelled_order.client_id
        ]

        sync_result = await engine.sync_orders(session)

        assert sync_result.filled == []
        assert sync_result.partially_filled == []
        assert cancelled_order.status == OrderStatus.CANCELLED
        assert all(order.status == OrderStatus.OPEN for order in session.orders[1:])

    asyncio.run(run())


def test_grid_engine_sync_reports_partially_filled_orders() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        partial_order = session.orders[0]
        exchange.order_statuses[partial_order.client_id] = "PARTIALLY_FILLED"
        exchange.orders["AAPLUSDT"] = [
            raw for raw in exchange.orders["AAPLUSDT"] if raw["client_id"] != partial_order.client_id
        ]

        sync_result = await engine.sync_orders(session)

        assert sync_result.filled == []
        assert len(sync_result.partially_filled) == 1
        assert sync_result.partially_filled[0].order is partial_order
        assert sync_result.partially_filled[0].price == partial_order.price
        assert sync_result.partially_filled[0].qty == partial_order.qty
        assert partial_order.status == OrderStatus.OPEN

    asyncio.run(run())


def test_grid_engine_sync_detects_partial_fill_while_order_remains_open() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        partial_order = session.orders[0]
        raw_order = next(
            order for order in exchange.orders["AAPLUSDT"] if order["client_id"] == partial_order.client_id
        )
        raw_order["status"] = "PARTIALLY_FILLED"
        raw_order["executedQty"] = partial_order.qty / 2
        raw_order["avgPrice"] = partial_order.price

        sync_result = await engine.sync_orders(session)

        assert sync_result.filled == []
        assert len(sync_result.partially_filled) == 1
        assert sync_result.partially_filled[0].order is partial_order
        assert sync_result.partially_filled[0].qty == partial_order.qty / 2

    asyncio.run(run())


def test_grid_engine_sync_reports_underfilled_filled_orders_as_partial() -> None:
    async def run() -> None:
        exchange = UnderfilledFilledOrderExchange()
        session = _session()
        engine = GridEngine(exchange)

        await engine.start(session, current_price=100.0)
        underfilled_order = session.orders[0]
        exchange.orders["AAPLUSDT"] = [
            raw for raw in exchange.orders["AAPLUSDT"] if raw["client_id"] != underfilled_order.client_id
        ]

        sync_result = await engine.sync_orders(session)

        assert sync_result.filled == []
        assert len(sync_result.partially_filled) == 1
        assert sync_result.partially_filled[0].order is underfilled_order
        assert sync_result.partially_filled[0].price == 99.5
        assert sync_result.partially_filled[0].qty == 0.123
        assert underfilled_order.status == OrderStatus.OPEN

    asyncio.run(run())


def test_grid_engine_sync_keeps_order_open_for_invalid_fill_details() -> None:
    async def run() -> None:
        invalid_responses = [
            {"avgPrice": "nan", "executedQty": "0.2"},
            {"avgPrice": "99.5", "executedQty": "inf"},
            {"avgPrice": "99.5", "executedQty": "0"},
        ]
        for response in invalid_responses:
            logs = []
            exchange = InvalidFilledDetailsExchange(response)
            session = _session()
            engine = GridEngine(exchange, log_system=lambda *args: logs.append(args))

            await engine.start(session, current_price=100.0)
            missing_order = session.orders[0]
            exchange.orders["AAPLUSDT"] = [
                raw for raw in exchange.orders["AAPLUSDT"] if raw["client_id"] != missing_order.client_id
            ]

            sync_result = await engine.sync_orders(session)

            assert sync_result.filled == []
            assert sync_result.partially_filled == []
            assert missing_order.status == OrderStatus.OPEN
            assert len(logs) == 1
            assert logs[0][0] == "WARN"
            assert logs[0][1] == "order_reconciliation"
            assert "Invalid exchange fill details" in logs[0][2]

    asyncio.run(run())


def test_grid_engine_sync_keeps_order_open_when_order_lookup_fails() -> None:
    async def run() -> None:
        logs = []
        exchange = FailingGetOrderExchange()
        session = _session()
        engine = GridEngine(exchange, log_system=lambda *args: logs.append(args))

        await engine.start(session, current_price=100.0)
        missing_order = session.orders[0]
        exchange.orders["AAPLUSDT"] = [
            raw for raw in exchange.orders["AAPLUSDT"] if raw["client_id"] != missing_order.client_id
        ]

        sync_result = await engine.sync_orders(session)

        assert sync_result.filled == []
        assert sync_result.partially_filled == []
        assert missing_order.status == OrderStatus.OPEN
        assert len(logs) == 1
        assert logs[0][0] == "WARN"
        assert logs[0][1] == "order_reconciliation"
        assert "lookup failed" in logs[0][2]
        assert missing_order.client_id in logs[0][3]

    asyncio.run(run())


def test_grid_engine_sync_keeps_order_open_for_unknown_exchange_status() -> None:
    async def run() -> None:
        logs = []
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange, log_system=lambda *args: logs.append(args))

        await engine.start(session, current_price=100.0)
        unknown_order = session.orders[0]
        exchange.order_statuses[unknown_order.client_id] = "NEW_UNKNOWN_STATUS"
        exchange.orders["AAPLUSDT"] = [
            raw for raw in exchange.orders["AAPLUSDT"] if raw["client_id"] != unknown_order.client_id
        ]

        sync_result = await engine.sync_orders(session)

        assert sync_result.filled == []
        assert sync_result.partially_filled == []
        assert unknown_order.status == OrderStatus.OPEN
        assert len(logs) == 1
        assert logs[0][0] == "WARN"
        assert "Unknown exchange order status" in logs[0][2]
        assert "NEW_UNKNOWN_STATUS" in logs[0][3]

    asyncio.run(run())


def test_grid_engine_supports_neutral_long_and_short_direction_modes() -> None:
    async def run() -> None:
        for mode in GridDirectionMode:
            exchange = MockExchangeClient()
            session = _session()
            session.capital = 500
            session.leverage = 1
            session.direction_mode = mode
            assert session.params is not None
            session.params = replace(session.params, direction_mode=mode)

            created = await GridEngine(exchange).start(session, current_price=100.0)

            assert created
            assert all(order.get("timeInForce") == "GTX" for order in exchange.orders[session.symbol])
            seed_orders = [order for order in session.orders if order.order_intent == OrderIntent.SEED]
            if mode == GridDirectionMode.NEUTRAL:
                assert seed_orders == []
                assert exchange.market_orders == []
                assert {
                    (order.side, order.position_side, order.order_intent)
                    for order in created
                } <= {
                    (OrderSide.BUY, "LONG", OrderIntent.OPEN),
                    (OrderSide.SELL, "SHORT", OrderIntent.OPEN),
                }
            elif mode == GridDirectionMode.LONG:
                assert len(seed_orders) == 1
                assert seed_orders[0].side == OrderSide.BUY
                assert seed_orders[0].position_side == "LONG"
                assert seed_orders[0].qty == sum(
                    order.qty for order in created if order.order_intent == OrderIntent.REDUCE
                )
                assert all(order.position_side == "LONG" for order in created)
                assert all(
                    order.order_intent == (
                        OrderIntent.OPEN if order.side == OrderSide.BUY else OrderIntent.REDUCE
                    )
                    for order in created
                )
            else:
                assert len(seed_orders) == 1
                assert seed_orders[0].side == OrderSide.SELL
                assert seed_orders[0].position_side == "SHORT"
                assert seed_orders[0].qty == sum(
                    order.qty for order in created if order.order_intent == OrderIntent.REDUCE
                )
                assert all(order.position_side == "SHORT" for order in created)
                assert all(
                    order.order_intent == (
                        OrderIntent.REDUCE if order.side == OrderSide.BUY else OrderIntent.OPEN
                    )
                    for order in created
                )

    asyncio.run(run())


def test_grid_engine_defensive_mode_keeps_reduce_orders_and_restores_open_orders() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        session.capital = 500
        session.leverage = 1
        session.direction_mode = GridDirectionMode.LONG
        assert session.params is not None
        session.params = replace(session.params, direction_mode=GridDirectionMode.LONG)
        engine = GridEngine(exchange)
        await engine.start(session, current_price=100.0)

        cancelled = await engine.enter_defensive(session, has_inventory=True)

        assert cancelled
        assert all(order.order_intent == OrderIntent.OPEN for order in cancelled)
        assert all(order.status == OrderStatus.CANCELLED for order in cancelled)
        assert any(
            order.status == OrderStatus.OPEN and order.order_intent == OrderIntent.REDUCE
            for order in session.orders
        )

        restored = await engine.restore_defensive_orders(session, client_id_tag="bar4")

        assert len(restored) == len(cancelled)
        assert all(order.order_intent == OrderIntent.OPEN for order in restored)
        assert all(order.position_side == "LONG" for order in restored)

    asyncio.run(run())


def test_grid_engine_rejects_direction_seed_when_slippage_exceeds_limit() -> None:
    async def run() -> None:
        session = _session()
        session.capital = 500
        session.leverage = 1
        session.direction_mode = GridDirectionMode.LONG
        assert session.params is not None
        session.params = replace(session.params, direction_mode=GridDirectionMode.LONG)

        try:
            await GridEngine(ExcessiveSeedSlippageExchange()).start(
                session,
                current_price=100.0,
            )
        except ValueError as exc:
            assert "滑点超过上限" in str(exc)
        else:
            raise AssertionError("excessive seed slippage must reject directional grid")

    asyncio.run(run())
