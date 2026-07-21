from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.models import GridDirectionMode, GridState, OrderStatus, RiskAction
from db.database import init_db
from db.repository import Repository
from exchange.mock import MockExchangeClient
from strategy.controller import (
    ControllerConfig,
    RunOnceResult,
    TradingController,
    _position_qty,
    _positive_price,
    _ticker_last_price,
)
from strategy.cooldown import CooldownDecision
from strategy.grid_calculator import GridConfig
from strategy.observer import ObservationAborted, ObserverConfig
from strategy.selector import SelectionConfig


NY = ZoneInfo("America/New_York")


class FakeScheduler:
    def __init__(self, in_window: bool = True, force_close: bool = False) -> None:
        self.in_window = in_window
        self.force_close = force_close

    def is_in_window(self, now_utc=None) -> bool:
        return self.in_window

    def should_force_close(self, now_utc=None) -> bool:
        return self.force_close


class ForceCloseOnSecondCheckScheduler(FakeScheduler):
    def __init__(self) -> None:
        super().__init__(in_window=True, force_close=False)
        self.calls = 0

    def should_force_close(self, now_utc=None) -> bool:
        self.calls += 1
        return self.calls >= 2


class CooldownRecoveryExchange(MockExchangeClient):
    async def get_klines(self, symbol: str, interval: str, limit: int):
        # v2 recovery preflight asks for enough rows for both the cooldown
        # evaluator and the long Regime window.  Keep the recovery sample calm
        # at either size so this fixture continues to exercise recovery rather
        # than accidentally testing the default mock's volatile history.
        if limit in {30, 62}:
            return [
                {"open": 100, "high": 100.01, "low": 99.99, "close": 100 + ((idx % 3) - 1) * 0.002}
                for idx in range(limit)
            ]
        return await super().get_klines(symbol, interval, limit)


class CooldownRecoveryCalculationFailureExchange(CooldownRecoveryExchange):
    def __init__(self) -> None:
        super().__init__()
        self.full_observation_calls = 0

    async def get_klines(self, symbol: str, interval: str, limit: int):
        if limit == 30:
            return await super().get_klines(symbol, interval, limit)
        self.full_observation_calls += 1
        if self.full_observation_calls == 1:
            return await super().get_klines(symbol, interval, limit)
        return [{"open": 90, "high": 90.1, "low": 89.9, "close": 90.0} for _ in range(limit)]


class CooldownRecoveryFailureWithCloseFailureExchange(CooldownRecoveryCalculationFailureExchange):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_calls += 1
        if self.cancel_calls >= 2:
            raise RuntimeError("cancel all unavailable during cooldown recovery failure")
        await super().cancel_all_orders(symbol)


class PositionedExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.positions["AAPLUSDT"] = 1.5


class InvalidRangeExchange(MockExchangeClient):
    async def get_klines(self, symbol: str, interval: str, limit: int):
        return [{"open": 90, "high": 90.1, "low": 89.9, "close": 90.0} for _ in range(limit)]


class CountingVolatilityExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.kline_calls: list[tuple[str, str, int]] = []

    async def get_klines(self, symbol: str, interval: str, limit: int):
        self.kline_calls.append((symbol, interval, limit))
        return await super().get_klines(symbol, interval, limit)


class FailingVolatilityRefreshExchange(CountingVolatilityExchange):
    async def get_klines(self, symbol: str, interval: str, limit: int):
        self.kline_calls.append((symbol, interval, limit))
        if len(self.kline_calls) > 1:
            return [{"high": 100.1, "low": 99.9, "close": 100.0} for _ in range(limit)]
        return await MockExchangeClient.get_klines(self, symbol, interval, limit)


class RollingRegridExchange(CountingVolatilityExchange):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_all_calls = 0

    async def get_klines(self, symbol: str, interval: str, limit: int):
        self.kline_calls.append((symbol, interval, limit))
        if len(self.kline_calls) == 1:
            return await MockExchangeClient.get_klines(self, symbol, interval, limit)
        return [
            {"open": 100, "high": 100.6, "low": 99.4, "close": 100 + ((idx % 5) - 2) * 0.12}
            for idx in range(limit)
        ]

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_all_calls += 1
        await super().cancel_all_orders(symbol)


class FailingRollingRegridCalculationExchange(RollingRegridExchange):
    async def get_klines(self, symbol: str, interval: str, limit: int):
        self.kline_calls.append((symbol, interval, limit))
        if len(self.kline_calls) == 1:
            return await MockExchangeClient.get_klines(self, symbol, interval, limit)
        return [{"open": 100, "high": 100.01, "low": 99.99, "close": 100.0} for _ in range(limit)]


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


class RequiresPositionStopExchange(MockExchangeClient):
    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        position = self.positions.get(symbol, 0.0)
        if (side == "SELL" and position <= 0) or (side == "BUY" and position >= 0):
            raise RuntimeError(
                "APIError(code=-4509): Time in Force (TIF) GTE can only be used with open positions. Please ensure that positions are available."
            )
        return await super().place_stop_market_order(symbol, side, stop_price, client_id, close_position)


class CancelRacePartialFillExchange(RequiresPositionStopExchange):
    def __init__(self, initial_qty: float, final_qty: float) -> None:
        super().__init__()
        self.initial_qty = initial_qty
        self.final_qty = final_qty
        self.cancelled = False
        self.trade_lookup_calls = 0

    async def get_order_trades(self, symbol: str, order_id: str):
        self.trade_lookup_calls += 1
        trades = [
            {
                "symbol": symbol,
                "orderId": order_id,
                "side": "BUY",
                "price": "99.5",
                "qty": str(self.initial_qty),
                "commission": "0.02",
                "realizedPnl": "0",
                "time": 1780000001000,
            }
        ]
        if self.cancelled and self.trade_lookup_calls >= 3:
            trades.append(
                {
                    "symbol": symbol,
                    "orderId": order_id,
                    "side": "BUY",
                    "price": "99.6",
                    "qty": str(self.final_qty - self.initial_qty),
                    "commission": "0.01",
                    "realizedPnl": "0",
                    "time": 1780000001100,
                }
            )
        return trades

    async def cancel_order(self, symbol: str, order_id: str):
        response = await super().cancel_order(symbol, order_id)
        self.cancelled = True
        self.positions[symbol] = self.final_qty
        return {**response, "executedQty": str(self.final_qty), "avgPrice": "99.52462121212121"}


class UnknownOrderAfterFullFillExchange(RequiresPositionStopExchange):
    def __init__(self, initial_qty: float, final_qty: float) -> None:
        super().__init__()
        self.initial_qty = initial_qty
        self.final_qty = final_qty
        self.cancel_attempted = False

    async def get_order_trades(self, symbol: str, order_id: str):
        qty = self.final_qty if self.cancel_attempted else self.initial_qty
        return [
            {
                "symbol": symbol,
                "orderId": order_id,
                "side": "BUY",
                "price": "99.5",
                "qty": str(qty),
                "commission": "0.02",
                "realizedPnl": "0",
                "time": 1780000001000,
            }
        ]

    async def cancel_order(self, symbol: str, order_id: str):
        self.cancel_attempted = True
        self.positions[symbol] = self.final_qty
        raise RuntimeError("APIError(code=-2011): Unknown order sent.")

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        return {
            "symbol": symbol,
            "orderId": order_id,
            "clientOrderId": client_id,
            "status": "FILLED",
            "executedQty": str(self.final_qty),
        }


class FailingDelayedStopProtectionExchange(RequiresPositionStopExchange):
    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        position = self.positions.get(symbol, 0.0)
        if position == 0:
            return await super().place_stop_market_order(symbol, side, stop_price, client_id, close_position)
        raise RuntimeError("stop protection unavailable after fill")


class FailingCancelAllExchange(MockExchangeClient):
    async def cancel_all_orders(self, symbol: str) -> None:
        raise RuntimeError("cancel all unavailable")


class FlakyCancelAllExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_attempts = 0

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_attempts += 1
        if self.cancel_attempts == 1:
            raise RuntimeError("cancel all unavailable")
        await super().cancel_all_orders(symbol)


class FlakyCancelAfterStopFailureExchange(FailingStopOrderExchange):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_attempts = 0

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_attempts += 1
        if self.cancel_attempts == 1:
            raise RuntimeError("cancel all unavailable")
        await super().cancel_all_orders(symbol)


class PartialFillLookupExchange(MockExchangeClient):
    def __init__(self, executed_qty: float) -> None:
        super().__init__()
        self.executed_qty = executed_qty

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        return {
            "symbol": symbol,
            "orderId": order_id,
            "client_id": client_id,
            "status": "PARTIALLY_FILLED",
            "avgPrice": "99.5",
            "executedQty": str(self.executed_qty),
        }


class UnderfilledLookupExchange(PartialFillLookupExchange):
    async def get_order(self, symbol: str, order_id: str, client_id: str):
        response = await super().get_order(symbol, order_id, client_id)
        response["status"] = "FILLED"
        return response


class OverfilledLookupExchange(UnderfilledLookupExchange):
    pass


def test_controller_rejects_invalid_ticker_prices_and_price_events() -> None:
    invalid_values = ("nan", "inf", "-inf", "0", "-1", "bad")

    for value in invalid_values:
        try:
            _ticker_last_price({"lastPrice": value})
        except ValueError:
            pass
        else:
            raise AssertionError("invalid ticker price should fail closed")

        try:
            _positive_price(value, "price event")
        except ValueError:
            pass
        else:
            raise AssertionError("invalid price event should fail closed")


def test_controller_rejects_non_finite_position_quantity() -> None:
    for value in ("nan", "inf", "-inf", "bad"):
        try:
            _position_qty({"qty": value})
        except ValueError:
            pass
        else:
            raise AssertionError("invalid position quantity should fail closed")

    assert _position_qty({"positionAmt": "-0.5"}) == -0.5


class PartialFillCloseFailureExchange(PartialFillLookupExchange):
    def __init__(self, executed_qty: float) -> None:
        super().__init__(executed_qty)
        self.fail_ticker = False

    async def cancel_all_orders(self, symbol: str) -> None:
        raise RuntimeError("cancel all unavailable")

    async def get_24h_ticker(self, symbol: str):
        if self.fail_ticker:
            raise RuntimeError("ticker should not be queried after partial fill close failure")
        return await super().get_24h_ticker(symbol)


class RejectRefillPostOnlyExchange(MockExchangeClient):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        if "-re-" in client_id:
            raise RuntimeError("Order would immediately match and take. Post only rejected.")
        return await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)


class FailingRefillExchange(MockExchangeClient):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        if "-re-" in client_id:
            raise RuntimeError("exchange unavailable during refill")
        return await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)


class MixedCommissionExchange(MockExchangeClient):
    async def get_commission_rate(self, symbol: str) -> dict[str, float]:
        if symbol == "AAPLUSDT":
            return {"maker": 0.0002, "taker": 0.0005}
        return {"maker": 0.0, "taker": 0.0005}


class ThreeSymbolExchange(MockExchangeClient):
    def __init__(self, failing_symbol: str | None = None) -> None:
        super().__init__()
        self.failing_symbol = failing_symbol
        self.symbols = [
            {"symbol": symbol, "status": "TRADING", "contractType": "PERPETUAL"}
            for symbol in ("BTCUSDT", "ETHUSDT", "BCHUSDT")
        ]

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        if symbol == self.failing_symbol:
            raise RuntimeError(f"{symbol} isolated order failure")
        return await super().place_limit_order_post_only(
            symbol,
            side,
            price,
            qty,
            client_id,
            position_side,
        )


class ChangingCommissionExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.maker_fee = 0.0
        self.commission_calls = 0

    async def get_commission_rate(self, symbol: str) -> dict[str, float]:
        self.commission_calls += 1
        return {"maker": self.maker_fee, "taker": 0.0005}


class FailingTickerAfterStartExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail_ticker = False

    async def get_24h_ticker(self, symbol: str):
        if self.fail_ticker:
            raise RuntimeError("ticker unavailable")
        return await super().get_24h_ticker(symbol)


class MissingPositionQtyAfterStartExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.missing_position_qty = False

    async def get_position(self, symbol: str):
        if self.missing_position_qty:
            return {"symbol": symbol}
        return await super().get_position(symbol)


class HedgeExposureExchange(MockExchangeClient):
    def __init__(self, symbol: str, long_qty: float, short_qty: float) -> None:
        super().__init__()
        self.hedge_symbol = symbol
        self.long_qty = long_qty
        self.short_qty = short_qty

    async def get_position(self, symbol: str):
        if symbol == self.hedge_symbol:
            return {
                "symbol": symbol,
                "qty": self.long_qty - self.short_qty,
                "long_qty": self.long_qty,
                "short_qty": self.short_qty,
            }
        return await super().get_position(symbol)


class MissingUntrackedMarketOrderIdExchange(MockExchangeClient):
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
        return {"symbol": symbol, "side": side, "qty": qty, "status": "filled"}


class AccountingMarketCloseExchange(MockExchangeClient):
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
            symbol, side, qty, reduce_only, position_side, client_id
        )
        order_id = str(response["orderId"])
        self.order_trades[(symbol, order_id)] = [
            {
                "symbol": symbol,
                "orderId": order_id,
                "side": side,
                "price": "99.0",
                "qty": str(qty),
                "commission": "0.04",
                "realizedPnl": "-0.10",
                "time": 1780000002000,
            }
        ]
        return response


class FilledDuringRoundStartFailureExchange(AccountingMarketCloseExchange):
    def __init__(self) -> None:
        super().__init__()
        self.injected_fill = False

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        if not self.injected_fill:
            self.injected_fill = True
            order = next(item for item in self.orders[symbol] if item["side"] == "BUY")
            order_id = str(order.get("orderId", order["client_id"]))
            fill_qty = 0.5
            self.positions[symbol] = fill_qty
            self.order_trades[(symbol, order_id)] = [
                {
                    "symbol": symbol,
                    "orderId": order_id,
                    "side": "BUY",
                    "price": "99.5",
                    "qty": str(fill_qty),
                    "commission": "0.02",
                    "realizedPnl": "0",
                    "time": 1780000001000,
                }
            ]
        raise RuntimeError("stop protection rejected after entry fill")


class PartialDirectionalSeedExchange(AccountingMarketCloseExchange):
    def __init__(self) -> None:
        super().__init__()
        self.seed_response: dict | None = None

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ):
        if not reduce_only and position_side == "LONG":
            response = await MockExchangeClient.place_market_order(
                self,
                symbol,
                side,
                qty,
                reduce_only,
                position_side,
                client_id,
            )
            partial_qty = qty / 2
            self.hedge_positions[symbol]["LONG"] = partial_qty
            response.update(
                status="PARTIALLY_FILLED",
                executedQty=partial_qty,
                avgPrice=100.0,
            )
            self.seed_response = response
            self.order_trades[(symbol, str(response["orderId"]))] = [
                {
                    "symbol": symbol,
                    "orderId": response["orderId"],
                    "side": side,
                    "price": "100.0",
                    "qty": str(partial_qty),
                    "commission": "0.02",
                    "realizedPnl": "0",
                    "time": 1780000001000,
                }
            ]
            return response
        return await super().place_market_order(
            symbol,
            side,
            qty,
            reduce_only,
            position_side,
            client_id,
        )

    async def get_order(self, symbol: str, order_id: str, client_id: str):
        if self.seed_response is not None and client_id == self.seed_response.get("clientOrderId"):
            return self.seed_response
        return await super().get_order(symbol, order_id, client_id)


class DelayedUntrackedMarketOrderLookupExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
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


class MissingUntrackedMarketOrderIdInvalidRulesExchange(MissingUntrackedMarketOrderIdExchange):
    async def get_symbol_rules(self, symbol: str):
        return {"tick_size": 0.000001, "step_size": 0.000001, "min_qty": -0.001}


class InvalidPositionToleranceRulesExchange(MockExchangeClient):
    def __init__(self, invalid_symbol: str, min_qty="nan", step_size=0.000001) -> None:
        super().__init__()
        self.invalid_symbol = invalid_symbol
        self.min_qty = min_qty
        self.step_size = step_size

    async def get_symbol_rules(self, symbol: str):
        if symbol == self.invalid_symbol:
            return {"tick_size": 0.000001, "step_size": self.step_size, "min_qty": self.min_qty}
        return await super().get_symbol_rules(symbol)


def test_controller_run_once_starts_mock_grid_and_persists_state(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        sessions = repo.recent_rows("sessions")
        orders = repo.recent_rows("orders", limit=50)
        logs = repo.recent_rows("state_logs")
        system_logs = repo.recent_rows("system_logs", limit=10)

        assert result.status == "started"
        assert result.started_symbols == ["AAPLUSDT"]
        assert exchange.orders["AAPLUSDT"]
        assert sessions[0]["grid_num"] is not None
        assert orders
        assert {row["status"] for row in orders} == {"open"}
        assert {row["trigger"] for row in logs} == {"window_open", "grid_started"}
        selection_log = next(row for row in system_logs if row["module"] == "selector")
        selection_detail = json.loads(selection_log["detail"])
        assert selection_detail[0]["symbol"] == "AAPLUSDT"
        assert "score" in selection_detail[0]

    asyncio.run(run())


def test_controller_can_start_three_independent_testnet_sessions(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = ThreeSymbolExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(
                max_concurrent=3,
                scan_candidate_count=3,
                symbol_allowlist=("BTCUSDT", "ETHUSDT", "BCHUSDT"),
            ),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=1,
                max_concurrent=3,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        assert result.status == "started"
        assert set(result.started_symbols) == {"BTCUSDT", "ETHUSDT", "BCHUSDT"}
        assert set(controller.active_sessions) == {"BTCUSDT", "ETHUSDT", "BCHUSDT"}
        assert all(exchange.orders[symbol] for symbol in controller.active_sessions)
        assert sum(session.capital for session in controller.active_sessions.values()) == 600

    asyncio.run(run())


def test_one_symbol_start_failure_does_not_stop_other_sessions(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = ThreeSymbolExchange(failing_symbol="ETHUSDT")
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(
                max_concurrent=3,
                scan_candidate_count=3,
                symbol_allowlist=("BTCUSDT", "ETHUSDT", "BCHUSDT"),
            ),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=1,
                max_concurrent=3,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        assert set(result.started_symbols) == {"BTCUSDT", "BCHUSDT"}
        assert set(controller.active_sessions) == {"BTCUSDT", "BCHUSDT"}
        failed = next(
            row
            for row in Repository(db_path).recent_rows("sessions", limit=10)
            if row["symbol"] == "ETHUSDT"
        )
        assert failed["state"] == "STOPPED"
        assert failed["close_reason"] == "grid_start_failed"

    asyncio.run(run())


def test_controller_scans_configured_candidates_before_applying_trade_limit(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, scan_candidate_count=3),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        sessions = Repository(db_path).recent_rows("sessions", limit=10)
        assert result.selected_symbols == ["AAPLUSDT", "MSFTUSDT", "TSLAPREUSDT"]
        assert result.started_symbols == ["AAPLUSDT"]
        assert len(sessions) == 3
        assert sum(row["state"] == "RUNNING" for row in sessions) == 1
        assert sum(row["close_reason"] == "eligible_but_concurrency_limit_reached" for row in sessions) == 2

    asyncio.run(run())


def test_controller_run_once_respects_paused_new_entries(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.set_control_state("new_entries_paused", True, now)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(now)

        assert result.status == "new_entries_paused"
        assert result.selected_symbols == []
        assert result.started_symbols == []
        assert repo.recent_rows("windows") == []
        log = repo.recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "controller"
        assert log["message"] == "New entries are paused by console control."

    asyncio.run(run())


def test_controller_run_once_skips_disabled_symbols(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.set_symbol_disabled("AAPLUSDT", True, now)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(now)

        assert result.status == "all_symbols_disabled"
        assert result.selected_symbols == ["AAPLUSDT"]
        assert result.started_symbols == []
        assert repo.recent_rows("windows") == []
        log = repo.recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "controller"
        assert log["message"] == "Disabled symbols skipped before opening new grids."

    asyncio.run(run())


def test_controller_poll_applies_session_stop_request(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(now)
        session = controller.active_sessions["AAPLUSDT"]
        repo.request_session_stop(session.session_id, "AAPLUSDT", "网页停止", "stop-1", now)

        actions = await controller.poll_active_sessions_once(now + timedelta(seconds=10))

        assert actions == [("AAPLUSDT", "manual_stop")]
        assert controller.active_sessions == {}
        assert repo.pending_session_stop_requests() == {}
        stop_request = repo.session_stop_requests(include_terminal=True)[str(session.session_id)]
        assert stop_request["status"] == "completed"
        row = repo.get_session(session.session_id)
        assert row is not None
        assert row["state"] == "STOPPED"
        assert row["close_reason"] == "控制台手动停止网格：网页停止"

    asyncio.run(run())


def test_controller_poll_pauses_and_resumes_grid_without_closing_position(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        exchange = MockExchangeClient()
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(now)
        session = controller.active_sessions["AAPLUSDT"]
        exchange.positions["AAPLUSDT"] = 0.0
        stop_orders_before = list(exchange.stop_orders["AAPLUSDT"])
        repo.request_session_control(session.session_id, session.symbol, "pause", "观察行情", "pause-1", now)

        pause_actions = await controller.poll_active_sessions_once(now + timedelta(seconds=10))

        assert pause_actions == [("AAPLUSDT", "paused")]
        assert session.state.value == "PAUSED"
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == stop_orders_before
        repo.request_session_control(session.session_id, session.symbol, "resume", "恢复交易", "resume-1", now)

        resume_actions = await controller.poll_active_sessions_once(now + timedelta(seconds=20))

        assert resume_actions == [("AAPLUSDT", "resumed")]
        assert session.state.value == "RUNNING"
        assert exchange.orders["AAPLUSDT"]
        assert exchange.stop_orders["AAPLUSDT"] == stop_orders_before

    asyncio.run(run())


def test_controller_poll_applies_session_manual_close_request(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(now)
        session = controller.active_sessions["AAPLUSDT"]
        repo.request_session_stop(
            session.session_id,
            "AAPLUSDT",
            "网页平仓",
            "close-1",
            now,
            request_type="manual_close",
        )

        actions = await controller.poll_active_sessions_once(now + timedelta(seconds=10))

        assert actions == [("AAPLUSDT", "manual_close")]
        assert controller.active_sessions == {}
        close_request = repo.session_stop_requests(include_terminal=True)[str(session.session_id)]
        assert close_request["status"] == "completed"
        assert close_request["request_type"] == "manual_close"
        row = repo.get_session(session.session_id)
        assert row is not None
        assert row["state"] == "STOPPED"
        assert row["close_reason"] == "控制台手动平仓：网页平仓"

    asyncio.run(run())


def test_controller_run_once_persists_volatility_snapshot(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=CountingVolatilityExchange(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(range_method="parkinson"),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        row = Repository(db_path).active_session_volatility_rows()[0]

        assert row["volatility_method"] == "parkinson"
        assert row["volatility_value"] > 0
        assert row["volatility_window"] == 60
        assert row["volatility_current_value"] == row["volatility_value"]
        assert row["volatility_current_window"] == 60
        assert row["volatility_current_at"] is not None

    asyncio.run(run())


def test_controller_run_once_uses_strategy_config_draft_for_next_grid(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        repo.set_strategy_config_draft(
            {
                "volatility_method": "yang_zhang",
                "max_concurrent": 1,
                "observe_hours": 0.5,
                "min_step_pct": 0.0015,
                "max_grid_num": 7,
            },
            datetime(2026, 7, 4, 9, 59, tzinfo=NY),
        )
        controller = TradingController(
            exchange=CountingVolatilityExchange(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=3, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(range_method="std", max_grid_num=20),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=3,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        row = repo.active_session_volatility_rows()[0]
        assert result.started_symbols == ["AAPLUSDT"]
        assert row["volatility_method"] == "yang_zhang"
        assert row["volatility_window"] == 30
        assert row["grid_num"] <= 7

    asyncio.run(run())


def test_controller_poll_refreshes_current_volatility_by_interval(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = CountingVolatilityExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(range_method="parkinson", volatility_refresh_seconds=60),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        calculated_at = session.params.calculated_at  # type: ignore[union-attr]

        await controller.poll_active_sessions_once(calculated_at + timedelta(seconds=30))
        assert len(exchange.kline_calls) == 1

        refresh_at = calculated_at + timedelta(seconds=61)
        await controller.poll_active_sessions_once(refresh_at)
        row = Repository(db_path).active_session_volatility_rows()[0]

        assert len(exchange.kline_calls) == 2
        assert row["volatility_current_value"] > 0
        assert row["volatility_current_window"] == 60
        assert row["volatility_current_at"] == refresh_at.isoformat()

    asyncio.run(run())


def test_controller_current_volatility_refresh_failure_warns_without_stopping(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingVolatilityRefreshExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(range_method="parkinson", volatility_refresh_seconds=1),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        calculated_at = session.params.calculated_at  # type: ignore[union-attr]

        actions = await controller.poll_active_sessions_once(calculated_at + timedelta(seconds=2))

        logs = Repository(db_path).recent_rows("system_logs", limit=20)
        volatility_log = next(row for row in logs if row["module"] == "volatility")
        detail = json.loads(volatility_log["detail"])

        assert actions == []
        assert "AAPLUSDT" in controller.active_sessions
        assert len(exchange.kline_calls) == 2
        assert volatility_log["level"] == "WARN"
        assert volatility_log["message"] == "Current volatility refresh failed."
        assert detail["symbol"] == "AAPLUSDT"
        assert detail["method"] == "parkinson"

    asyncio.run(run())


def test_controller_rolls_running_grid_when_regrid_interval_elapsed(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = RollingRegridExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(rolling_regrid_enabled=True, rolling_regrid_seconds=1),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        assert session.params is not None
        old_upper = session.params.upper

        actions = await controller.poll_active_sessions_once(session.params.calculated_at + timedelta(seconds=2))

        repo = Repository(db_path)
        state_log = next(row for row in repo.recent_rows("state_logs", limit=10) if row["trigger"] == "rolling_regrid")
        system_log = next(row for row in repo.recent_rows("system_logs", limit=10) if row["module"] == "rolling_regrid")

        assert actions == [("AAPLUSDT", "rolling_regrid")]
        assert session.params.upper != old_upper
        assert session.orders
        assert all(order.status == OrderStatus.OPEN for order in session.orders)
        assert exchange.cancel_all_calls == 1
        assert exchange.orders["AAPLUSDT"]
        assert state_log["from_state"] == "RUNNING"
        assert state_log["to_state"] == "RUNNING"
        assert system_log["message"] == "Rolling grid recalculated."

    asyncio.run(run())


def test_controller_skips_rolling_regrid_when_session_has_exposure(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = RollingRegridExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(rolling_regrid_enabled=True, rolling_regrid_seconds=1),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        assert session.params is not None
        filled_order = session.orders[0]
        filled_order.status = OrderStatus.FILLED
        exchange.orders["AAPLUSDT"] = [
            order for order in exchange.orders["AAPLUSDT"] if order["client_id"] != filled_order.client_id
        ]
        exchange.positions["AAPLUSDT"] = filled_order.qty if filled_order.side.value == "BUY" else -filled_order.qty
        old_upper = session.params.upper

        actions = await controller.poll_active_sessions_once(session.params.calculated_at + timedelta(seconds=2))

        system_log = next(
            row for row in Repository(db_path).recent_rows("system_logs", limit=10) if row["module"] == "rolling_regrid"
        )
        detail = json.loads(system_log["detail"])

        assert actions == [("AAPLUSDT", "rolling_regrid_skipped")]
        assert session.params.upper == old_upper
        assert exchange.cancel_all_calls == 0
        assert detail["reason"] == "session_has_filled_orders"

    asyncio.run(run())


def test_controller_rolling_regrid_calculation_failure_keeps_current_orders(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingRollingRegridCalculationExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(rolling_regrid_enabled=True, rolling_regrid_seconds=1),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        assert session.params is not None
        old_upper = session.params.upper
        old_client_ids = [order.client_id for order in session.orders]

        actions = await controller.poll_active_sessions_once(session.params.calculated_at + timedelta(seconds=2))

        system_log = next(
            row for row in Repository(db_path).recent_rows("system_logs", limit=10) if row["module"] == "rolling_regrid"
        )

        assert actions == [("AAPLUSDT", "rolling_regrid_failed")]
        assert session.params.upper == old_upper
        assert [order.client_id for order in session.orders] == old_client_ids
        assert exchange.cancel_all_calls == 0
        assert system_log["level"] == "WARN"
        assert system_log["message"] == "Rolling grid recalculation failed before touching orders."

    asyncio.run(run())


def test_controller_skips_symbols_when_maker_fee_exceeds_limit(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MixedCommissionExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=2, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=2,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_maker_fee_rate=0.0,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        commission_log = next(row for row in repo.recent_rows("system_logs", limit=10) if row["module"] == "commission")

        assert result.status == "started"
        assert result.selected_symbols == ["AAPLUSDT", "MSFTUSDT"]
        assert result.started_symbols == ["MSFTUSDT"]
        assert "AAPLUSDT" not in controller.active_sessions
        assert "MSFTUSDT" in controller.active_sessions
        assert commission_log["level"] == "WARN"
        assert "maker=0.0002" in commission_log["detail"]

    asyncio.run(run())


def test_controller_returns_no_fee_eligible_when_all_maker_fees_exceed_limit(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()

        async def charged_commission(symbol: str) -> dict[str, float]:
            return {"maker": 0.0002, "taker": 0.0005}

        exchange.get_commission_rate = charged_commission  # type: ignore[method-assign]
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_maker_fee_rate=0.0,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)

        assert result.status == "no_fee_eligible"
        assert result.started_symbols == []
        assert repo.recent_rows("windows") == []

    asyncio.run(run())


def test_controller_skips_symbol_when_maker_fee_is_missing(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()

        async def missing_maker_commission(symbol: str) -> dict[str, float]:
            return {"taker": 0.0005}

        exchange.get_commission_rate = missing_maker_commission  # type: ignore[method-assign]
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_maker_fee_rate=0.0,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        commission_log = next(row for row in repo.recent_rows("system_logs", limit=10) if row["module"] == "commission")

        assert result.status == "no_fee_eligible"
        assert result.started_symbols == []
        assert repo.recent_rows("windows") == []
        assert commission_log["level"] == "ERROR"
        assert "缺少 maker 字段" in commission_log["detail"]

    asyncio.run(run())


def test_controller_skips_symbol_when_maker_fee_is_negative(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()

        async def negative_maker_commission(symbol: str) -> dict[str, float]:
            return {"maker": -0.0001, "taker": 0.0005}

        exchange.get_commission_rate = negative_maker_commission  # type: ignore[method-assign]
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_maker_fee_rate=0.0,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        commission_log = next(row for row in repo.recent_rows("system_logs", limit=10) if row["module"] == "commission")

        assert result.status == "no_fee_eligible"
        assert result.started_symbols == []
        assert repo.recent_rows("windows") == []
        assert commission_log["level"] == "ERROR"
        assert "maker 必须为非负数" in commission_log["detail"]

    asyncio.run(run())


def test_controller_poll_warns_when_active_maker_fee_changes(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = ChangingCommissionExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_maker_fee_rate=0.0,
                maker_fee_check_interval_seconds=60,
            ),
        )
        start_at = datetime(2026, 7, 4, 10, 0, tzinfo=NY)

        result = await controller.run_once(start_at)
        exchange.maker_fee = 0.0002
        actions = await controller.poll_active_sessions_once(start_at + timedelta(seconds=61))

        health = Repository(db_path).latest_commission_health()
        detail = json.loads(health["detail"])

        assert result.status == "started"
        assert actions[0] == ("*", "commission_warn")
        assert health["level"] == "WARN"
        assert health["message"] == "Maker fee changed."
        assert detail["status"] == "warn"
        assert detail["changed_count"] == 1
        assert detail["symbols"][0]["symbol"] == "AAPLUSDT"
        assert detail["symbols"][0]["previous_maker"] == 0.0
        assert detail["symbols"][0]["maker"] == 0.0002

    asyncio.run(run())


def test_controller_poll_skips_active_maker_fee_check_before_interval(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = ChangingCommissionExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_maker_fee_rate=0.0,
                maker_fee_check_interval_seconds=60,
            ),
        )
        start_at = datetime(2026, 7, 4, 10, 0, tzinfo=NY)

        await controller.run_once(start_at)
        calls_after_start = exchange.commission_calls
        actions = await controller.poll_active_sessions_once(start_at + timedelta(seconds=30))

        assert actions == []
        assert exchange.commission_calls == calls_after_start
        assert Repository(db_path).latest_commission_health() is None

    asyncio.run(run())


def test_controller_startup_check_passes_and_logs(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.validate_startup()

        assert result.ok is True
        assert result.balance == 10000
        assert Repository(db_path).recent_rows("system_logs", limit=1)[0]["level"] == "INFO"

    asyncio.run(run())


def test_controller_startup_check_rejects_low_balance(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        exchange.balance = 10
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.validate_startup()

        assert result.ok is False
        assert "余额" in result.reason
        assert Repository(db_path).recent_rows("system_logs", limit=1)[0]["level"] == "ERROR"

    asyncio.run(run())


def test_controller_startup_check_rejects_non_finite_balance(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        exchange.balance = float("nan")
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.validate_startup()

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]

        assert result.ok is False
        assert "余额" in result.reason
        assert log["level"] == "ERROR"
        assert "不是有限数字" in log["detail"]

    asyncio.run(run())


def test_controller_startup_check_requires_balance_for_configured_concurrency(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        exchange.balance = 500
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=3),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=3,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.validate_startup()

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]

        assert result.ok is False
        assert "并发" in result.reason
        assert "required_budget=600" in log["detail"]

    asyncio.run(run())


def test_controller_startup_check_rejects_non_finite_required_budget(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=float("nan"),
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.validate_startup()

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]

        assert result.ok is False
        assert "资金配置" in result.reason
        assert log["level"] == "ERROR"

    asyncio.run(run())


def test_controller_startup_check_rejects_invalid_trade_parameters(tmp_path) -> None:
    async def run() -> None:
        invalid_configs = [
            {"leverage": 0},
            {"take_profit_usdt": float("nan")},
            {"max_maker_fee_rate": float("nan")},
            {"max_maker_fee_rate": -0.001},
            {"maker_fee_check_interval_seconds": float("nan")},
            {"maker_fee_check_interval_seconds": -1},
        ]
        for index, overrides in enumerate(invalid_configs):
            db_path = tmp_path / f"controller-{index}.db"
            init_db(db_path)
            config_values = {
                "capital_per_symbol": 200,
                "leverage": 10,
                "max_concurrent": 1,
                "take_profit_usdt": 10,
                "total_capital_limit": 1000,
                "max_maker_fee_rate": 0.0,
                "maker_fee_check_interval_seconds": 300,
            }
            config_values.update(overrides)
            controller = TradingController(
                exchange=MockExchangeClient(),
                scheduler=FakeScheduler(),  # type: ignore[arg-type]
                repository=Repository(db_path),
                selector_config=SelectionConfig(max_concurrent=1),
                observer_config=ObserverConfig(observe_hours=1, min_samples=30),
                grid_config=GridConfig(),
                controller_config=ControllerConfig(**config_values),
            )

            result = await controller.validate_startup()

            log = Repository(db_path).recent_rows("system_logs", limit=1)[0]

            assert result.ok is False
            assert "交易参数" in result.reason
            assert log["level"] == "ERROR"

    asyncio.run(run())


def test_controller_startup_check_rejects_capital_limit_mismatch(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=3),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=3,
                take_profit_usdt=10,
                total_capital_limit=300,
            ),
        )

        result = await controller.validate_startup()

        assert result.ok is False
        assert "总资金上限" in result.reason

    asyncio.run(run())


def test_controller_recovers_unclosed_sessions_on_startup(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        window_id = repo.create_window(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session_id = repo.create_session(
            window_id,
            "AAPLUSDT",
            "RUNNING",
            200,
            10,
            datetime(2026, 7, 4, 10, 0, tzinfo=NY),
        )
        exchange = PositionedExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        recovered = await controller.recover_unclosed_sessions(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        session_row = repo.recent_rows("sessions", limit=1)[0]
        state_log = repo.recent_rows("state_logs", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert recovered == [session_id]
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 1.5,
                "reduce_only": True,
                "client_id": f"qg-{session_id}-close-sell",
                "status": "filled",
            }
        ]
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "startup_recovery_force_close"
        assert state_log["trigger"] == "startup_recovery"
        assert system_log["level"] == "WARN"

    asyncio.run(run())


def test_controller_skips_startup_recovery_for_unsupported_symbol(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        window_id = repo.create_window(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session_id = repo.create_session(
            window_id,
            "AAPLUSDT",
            "RUNNING",
            200,
            10,
            datetime(2026, 7, 4, 10, 0, tzinfo=NY),
        )
        exchange = PositionedExchange()
        exchange.positions["AAPLUSDT"] = 1.5
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        recovered = await controller.recover_unclosed_sessions(
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            recoverable_symbols={"BTCUSDT"},
        )

        session_row = repo.recent_rows("sessions", limit=1)[0]
        state_log = repo.recent_rows("state_logs", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert recovered == [session_id]
        assert exchange.market_orders == []
        assert exchange.positions["AAPLUSDT"] == 1.5
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "startup_recovery_skipped_symbol"
        assert state_log["trigger"] == "startup_recovery_skipped_symbol"
        assert system_log["level"] == "WARN"
        assert system_log["message"] == "Skipped startup recovery for unsupported symbol."

    asyncio.run(run())


def test_controller_keeps_unclosed_session_when_startup_recovery_fails(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        window_id = repo.create_window(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session_id = repo.create_session(
            window_id,
            "AAPLUSDT",
            "RUNNING",
            200,
            10,
            datetime(2026, 7, 4, 10, 0, tzinfo=NY),
        )
        exchange = FailingCancelAllExchange()
        exchange.positions["AAPLUSDT"] = 1.0
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        try:
            await controller.recover_unclosed_sessions(datetime(2026, 7, 4, 10, 1, tzinfo=NY))
        except RuntimeError as exc:
            assert "cancel all unavailable" in str(exc)
        else:
            raise AssertionError("startup recovery failure should abort startup")

        session_row = repo.recent_rows("sessions", limit=1)[0]
        state_log = repo.recent_rows("state_logs", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=2)

        assert session_row["id"] == session_id
        assert session_row["state"] == "CLOSING"
        assert session_row["close_time"] is None
        assert session_row["close_reason"] is None
        assert state_log["trigger"] == "startup_recovery_failed"
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 1.0,
                "reduce_only": True,
                "client_id": f"qg-{session_id}-close-sell",
                "status": "filled",
            }
        ]
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert logs[0]["level"] == "ERROR"
        assert logs[0]["message"] == "Startup recovery force close failed; session left unclosed."
        assert logs[1]["module"] == "force_close"

    asyncio.run(run())


def test_controller_run_once_skips_outside_window(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(in_window=False),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        assert result.status == "outside_window"
        assert Repository(db_path).recent_rows("windows") == []

    asyncio.run(run())


def test_controller_aborts_when_force_close_triggers_during_observation(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        scheduler = ForceCloseOnSecondCheckScheduler()
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=0.01, min_samples=30, live_observation=True, observe_check_seconds=1),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        assert result.status == "observation_aborted"
        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "observation_aborted_force_close"
        assert repo.recent_rows("state_logs", limit=1)[0]["trigger"] == "observation_aborted"

    asyncio.run(run())


def test_controller_aborted_parallel_observation_closes_entire_round(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=2, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=2,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        original_observe = controller.observer.observe_then_calculate
        calls = 0

        async def abort_second_observation(
            symbol,
            current_price,
            should_abort=None,
            sleep_fn=asyncio.sleep,
            observer_config=None,
            grid_config=None,
        ):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ObservationAborted("test abort")
            return await original_observe(
                symbol,
                current_price,
                should_abort,
                sleep_fn,
                observer_config=observer_config,
                grid_config=grid_config,
            )

        controller.observer.observe_then_calculate = abort_second_observation  # type: ignore[method-assign]

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        sessions = repo.recent_rows("sessions", limit=10)
        by_symbol = {row["symbol"]: row for row in sessions}
        triggers = {row["trigger"] for row in repo.recent_rows("state_logs", limit=20)}

        assert result.status == "observation_aborted"
        assert result.started_symbols == []
        assert controller.active_sessions == {}
        assert exchange.orders.get("AAPLUSDT", []) == []
        assert exchange.stop_orders.get("AAPLUSDT", []) == []
        assert by_symbol["AAPLUSDT"]["state"] == "STOPPED"
        assert by_symbol["AAPLUSDT"]["close_reason"] == "observation_aborted_force_close"
        assert by_symbol["MSFTUSDT"]["state"] == "STOPPED"
        assert by_symbol["MSFTUSDT"]["close_reason"] == "observation_aborted_force_close"
        assert "observation_aborted" in triggers

    asyncio.run(run())


def test_controller_skips_symbol_when_grid_calculation_fails(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=InvalidRangeExchange(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        window = repo.recent_rows("windows", limit=1)[0]
        state_log = repo.recent_rows("state_logs", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert result.status == "no_started"
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "grid_calculation_failed"
        assert window["status"] == "skipped"
        assert state_log["trigger"] == "grid_calculation_failed"
        assert system_log["level"] == "WARN"

    asyncio.run(run())


def test_controller_skips_symbol_when_grid_start_fails(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingStopOrderExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        window = repo.recent_rows("windows", limit=1)[0]
        state_log = repo.recent_rows("state_logs", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert result.status == "no_started"
        assert result.started_symbols == []
        assert exchange.orders["AAPLUSDT"] == []
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "grid_start_failed"
        assert window["status"] == "skipped"
        assert state_log["trigger"] == "grid_start_failed"
        assert system_log["level"] == "ERROR"
        assert system_log["message"] == "Grid start failed; symbol skipped."

    asyncio.run(run())


def test_controller_keeps_grid_start_cleanup_failure_active_and_retries_close(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FlakyCancelAfterStopFailureExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        window = repo.recent_rows("windows", limit=1)[0]
        orders = repo.recent_rows("orders", limit=50)
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert result.status == "cleanup_pending"
        assert result.started_symbols == []
        assert "AAPLUSDT" in controller.active_sessions
        assert controller.active_sessions["AAPLUSDT"].state == GridState.CLOSING
        assert exchange.orders["AAPLUSDT"]
        assert session["state"] == "CLOSING"
        assert session["close_time"] is None
        assert window["status"] == "open"
        assert orders
        assert {row["status"] for row in orders} == {"open"}
        assert system_log["message"] == "Grid start failed and cleanup is pending; session kept active for retry."

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        session = repo.recent_rows("sessions", limit=1)[0]
        window = repo.recent_rows("windows", limit=1)[0]
        orders = repo.recent_rows("orders", limit=50)

        assert actions == [("AAPLUSDT", "close_retry")]
        assert controller.active_sessions == {}
        assert exchange.cancel_attempts == 2
        assert exchange.orders["AAPLUSDT"] == []
        assert {row["status"] for row in orders} == {"cancelled"}
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "继续清理待关闭会话。"
        assert window["status"] == "closed"

    asyncio.run(run())


def test_controller_fill_event_records_trade_and_updates_pnl(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        sell_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )
        assert sell_order is not None

        await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": sell_order.client_id,
                "price": sell_order.price,
                "qty": sell_order.qty,
                "order_id": sell_order.order_id,
            }
        )

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        orders = repo.recent_rows("orders", limit=50)
        sessions = repo.recent_rows("sessions", limit=1)

        assert len(trades) == 2
        assert "filled" in {row["status"] for row in orders}
        assert "open" in {row["status"] for row in orders}
        assert sessions[0]["realized_pnl"] > 0
        assert controller.active_sessions["AAPLUSDT"].realized_pnl > 0

    asyncio.run(run())


def test_controller_fill_event_is_idempotent_after_reconciliation(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        initial_order_count = len(session.orders)

        buy_order.status = OrderStatus.FILLED
        assert buy_order.fill_price is None

        first_new_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )
        duplicate_new_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        orders = repo.recent_rows("orders", limit=50)

        assert first_new_order is not None
        assert duplicate_new_order is None
        assert len(session.orders) == initial_order_count + 1
        assert len(trades) == 1
        assert sum(1 for row in orders if row["client_id"] == first_new_order.client_id) == 1

    asyncio.run(run())


def test_controller_rejects_invalid_fill_quantity_without_mutating_order(tmp_path) -> None:
    async def run() -> None:
        invalid_quantities = ["nan", "inf", 0]
        for qty in invalid_quantities:
            db_path = tmp_path / f"controller-{qty}.db"
            init_db(db_path)
            exchange = MockExchangeClient()
            controller = TradingController(
                exchange=exchange,
                scheduler=FakeScheduler(),  # type: ignore[arg-type]
                repository=Repository(db_path),
                selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
                observer_config=ObserverConfig(observe_hours=1, min_samples=30),
                grid_config=GridConfig(),
                controller_config=ControllerConfig(
                    capital_per_symbol=200,
                    leverage=10,
                    max_concurrent=1,
                    take_profit_usdt=10,
                    total_capital_limit=1000,
                ),
            )

            await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
            session = controller.active_sessions["AAPLUSDT"]
            buy_order = next(order for order in session.orders if order.side.value == "BUY")
            order_count = len(session.orders)
            exchange_order_count = len(exchange.orders["AAPLUSDT"])

            try:
                await controller.handle_order_filled_event(
                    {
                        "symbol": "AAPLUSDT",
                        "client_id": buy_order.client_id,
                        "price": buy_order.price,
                        "qty": qty,
                        "order_id": buy_order.order_id,
                    }
                )
            except ValueError:
                pass
            else:
                raise AssertionError("invalid fill quantity should fail closed")

            repo = Repository(db_path)
            trades = repo.recent_rows("trades", limit=10)

            assert buy_order.status == OrderStatus.OPEN
            assert buy_order.fill_price is None
            assert len(session.orders) == order_count
            assert len(exchange.orders["AAPLUSDT"]) == exchange_order_count
            assert trades == []

    asyncio.run(run())


def test_controller_closes_session_when_fill_quantity_exceeds_order_quantity(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        over_qty = buy_order.qty * 2
        exchange.positions["AAPLUSDT"] = over_qty

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": over_qty,
                "order_id": buy_order.order_id,
            }
        )

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=5)

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert buy_order.status == OrderStatus.CANCELLED
        assert buy_order.fill_price is None
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert exchange.market_orders[-1]["side"] == "SELL"
        assert trades == []
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "成交数量超过本地订单数量，执行安全平仓。"
        assert any(log["module"] == "order_event" and log["level"] == "ERROR" for log in logs)

    asyncio.run(run())


def test_controller_reconciled_overfilled_order_closes_without_marking_trade_accepted(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = OverfilledLookupExchange(executed_qty=999999)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.executed_qty = buy_order.qty * 2
        exchange.positions["AAPLUSDT"] = exchange.executed_qty
        exchange.orders["AAPLUSDT"] = [
            raw for raw in exchange.orders["AAPLUSDT"] if raw["client_id"] != buy_order.client_id
        ]

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        persisted_order = next(row for row in repo.recent_rows("orders", limit=50) if row["client_id"] == buy_order.client_id)
        logs = repo.recent_rows("system_logs", limit=10)

        assert actions == [("AAPLUSDT", "filled_reconciled")]
        assert "AAPLUSDT" not in controller.active_sessions
        assert buy_order.status == OrderStatus.CANCELLED
        assert buy_order.fill_price is None
        assert persisted_order["status"] == "cancelled"
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert trades == []
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "成交数量超过本地订单数量，执行安全平仓。"
        assert any(log["module"] == "order_event" and log["level"] == "ERROR" for log in logs)

    asyncio.run(run())


def test_controller_fill_event_records_short_cycle_pnl(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        sell_order = next(order for order in session.orders if order.side.value == "SELL")
        buy_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": sell_order.client_id,
                "price": sell_order.price,
                "qty": sell_order.qty,
                "order_id": sell_order.order_id,
            }
        )
        assert buy_order is not None

        await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        sessions = repo.recent_rows("sessions", limit=1)

        assert len(trades) == 2
        assert any(row["side"] == "BUY" and row["grid_pnl"] > 0 for row in trades)
        assert sessions[0]["realized_pnl"] > 0
        assert controller.active_sessions["AAPLUSDT"].realized_pnl > 0

    asyncio.run(run())


def test_controller_closes_session_when_grid_pnl_input_is_invalid(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        sell_order = next(order for order in session.orders if order.side.value == "SELL")
        buy_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": sell_order.client_id,
                "price": sell_order.price,
                "qty": sell_order.qty,
                "order_id": sell_order.order_id,
            }
        )
        assert buy_order is not None
        buy_order.entry_price = float("nan")
        exchange.positions["AAPLUSDT"] = buy_order.qty

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=10)

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert buy_order.status == OrderStatus.CANCELLED
        assert buy_order.fill_price is None
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert [trade["order_id"] for trade in trades] == [sell_order.order_id]
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "网格收益计算输入异常，执行安全平仓。"
        assert any(log["module"] == "grid_pnl" and log["level"] == "ERROR" for log in logs)

    asyncio.run(run())


def test_controller_fill_event_persists_trade_when_refill_post_only_rejected(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = RejectRefillPostOnlyExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")

        new_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )

        repo = Repository(db_path)
        trade = repo.recent_rows("trades", limit=1)[0]
        order_row = next(row for row in repo.recent_rows("orders", limit=50) if row["client_id"] == buy_order.client_id)
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert new_order is None
        assert buy_order.status.value == "filled"
        assert order_row["status"] == "filled"
        assert trade["order_id"] == buy_order.order_id
        assert trade["side"] == "BUY"
        assert system_log["level"] == "WARN"
        assert system_log["module"] == "grid_engine"
        assert system_log["message"] == "Refill post-only order rejected after fill."

    asyncio.run(run())


def test_controller_fill_event_closes_session_after_unexpected_refill_failure(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingRefillExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.positions["AAPLUSDT"] = buy_order.qty

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )

        repo = Repository(db_path)
        trade = repo.recent_rows("trades", limit=1)[0]
        order_row = next(row for row in repo.recent_rows("orders", limit=50) if row["client_id"] == buy_order.client_id)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=5)

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert exchange.market_orders[-1]["side"] == "SELL"
        assert buy_order.status == OrderStatus.FILLED
        assert order_row["status"] == "filled"
        assert trade["order_id"] == buy_order.order_id
        assert trade["side"] == "BUY"
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "成交后补单失败，执行安全平仓。"
        assert any(log["module"] == "grid_engine" and log["level"] == "ERROR" for log in logs)

    asyncio.run(run())


def test_controller_partial_fill_cancel_race_uses_final_cumulative_qty(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-partial-cancel-race.db"
        init_db(db_path)
        initial_qty = 0.199
        final_qty = 0.264
        exchange = CancelRacePartialFillExchange(initial_qty, final_qty)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.positions["AAPLUSDT"] = initial_qty

        await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "status": "PARTIALLY_FILLED",
                "price": 99.5,
                "qty": initial_qty,
                "fee": 0.02,
                "realized_pnl": 0.0,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        refill = next(order for order in session.orders if order.entry_price is not None)

        assert controller.active_sessions["AAPLUSDT"].state == GridState.RUNNING
        assert buy_order.status == OrderStatus.FILLED
        assert buy_order.qty == final_qty
        assert len(trades) == 1
        assert trades[0]["order_id"] == buy_order.order_id
        assert trades[0]["qty"] == final_qty
        assert trades[0]["fee"] == 0.03
        assert refill.side.value == "SELL"
        assert refill.qty == final_qty
        assert exchange.trade_lookup_calls >= 3
        assert exchange.stop_orders["AAPLUSDT"][-1]["side"] == "SELL"
        assert session.stop_protection_sides == {"long"}

    asyncio.run(run())


def test_controller_unknown_order_after_partial_fill_reconciles_full_fill(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-partial-unknown-order.db"
        init_db(db_path)
        exchange = UnknownOrderAfterFullFillExchange(0.1, 0.2)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.final_qty = buy_order.qty
        exchange.initial_qty = buy_order.qty / 2
        exchange.positions["AAPLUSDT"] = exchange.initial_qty

        await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "status": "PARTIALLY_FILLED",
                "price": 99.5,
                "qty": exchange.initial_qty,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        session_row = repo.get_session(session.session_id)
        logs = repo.recent_rows("system_logs", limit=20)

        assert exchange.cancel_attempted is True
        assert controller.active_sessions["AAPLUSDT"].state == GridState.RUNNING
        assert session_row["state"] == "RUNNING"
        assert "AAPLUSDT" not in controller.round_stopped_symbols
        assert len(repo.recent_rows("trades", limit=10)) == 1
        assert any("-2011" in str(log.get("detail") or "") for log in logs)
        assert not exchange.market_orders

    asyncio.run(run())


def test_controller_partial_fill_event_closes_session_safely(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        partial_qty = buy_order.qty / 2
        exchange.positions["AAPLUSDT"] = partial_qty

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "status": "PARTIALLY_FILLED",
                "price": buy_order.price,
                "qty": partial_qty,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        trade = repo.recent_rows("trades", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert exchange.market_orders[-1]["side"] == "SELL"
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert trade["order_id"] == buy_order.order_id
        assert trade["side"] == "BUY"
        assert trade["qty"] == partial_qty
        assert trade["grid_index"] == buy_order.grid_index
        assert trade["grid_pnl"] is None
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "部分成交撤单或累计成交对账失败，执行安全平仓。"
        assert system_log["module"] == "partial_fill"

    asyncio.run(run())


def test_controller_invalid_partial_fill_details_close_session_without_trade(tmp_path) -> None:
    async def run() -> None:
        invalid_events = [
            {"price": "not-a-price", "qty": 0.123},
            {"price": 99.5, "qty": "not-a-qty"},
            {"price": "nan", "qty": 0.123},
            {"price": 99.5, "qty": "inf"},
        ]
        for index, invalid_values in enumerate(invalid_events):
            db_path = tmp_path / f"controller-partial-invalid-{index}.db"
            init_db(db_path)
            exchange = MockExchangeClient()
            controller = TradingController(
                exchange=exchange,
                scheduler=FakeScheduler(),  # type: ignore[arg-type]
                repository=Repository(db_path),
                selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
                observer_config=ObserverConfig(observe_hours=1, min_samples=30),
                grid_config=GridConfig(),
                controller_config=ControllerConfig(
                    capital_per_symbol=200,
                    leverage=10,
                    max_concurrent=1,
                    take_profit_usdt=10,
                    total_capital_limit=1000,
                ),
            )

            await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
            session = controller.active_sessions["AAPLUSDT"]
            buy_order = next(order for order in session.orders if order.side.value == "BUY")
            exchange.positions["AAPLUSDT"] = buy_order.qty / 2

            result = await controller.handle_order_filled_event(
                {
                    "symbol": "AAPLUSDT",
                    "client_id": buy_order.client_id,
                    "status": "PARTIALLY_FILLED",
                    "price": invalid_values["price"],
                    "qty": invalid_values["qty"],
                    "order_id": buy_order.order_id,
                    "side": "BUY",
                    "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
                }
            )

            repo = Repository(db_path)
            trades = repo.recent_rows("trades", limit=10)
            session_row = repo.recent_rows("sessions", limit=1)[0]

            assert result is None
            assert "AAPLUSDT" not in controller.active_sessions
            assert exchange.positions["AAPLUSDT"] == 0.0
            assert trades == []
            assert session_row["state"] == "STOPPED"
            assert session_row["close_reason"] == "部分成交数据异常，执行安全平仓。"

    asyncio.run(run())


def test_controller_overfilled_partial_fill_closes_without_recording_trade(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        over_qty = buy_order.qty * 2
        exchange.positions["AAPLUSDT"] = over_qty

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "status": "PARTIALLY_FILLED",
                "price": buy_order.price,
                "qty": over_qty,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=5)
        error_log = next(row for row in logs if row["module"] == "partial_fill" and row["level"] == "ERROR")

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert buy_order.status == OrderStatus.CANCELLED
        assert buy_order.fill_price is None
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert trades == []
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "部分成交数据异常，执行安全平仓。"
        assert "partial fill qty exceeds local order qty" in error_log["detail"]

    asyncio.run(run())


def test_controller_underfilled_final_event_closes_session_safely(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        fill_qty = buy_order.qty / 2
        exchange.positions["AAPLUSDT"] = fill_qty

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "status": "FILLED",
                "price": buy_order.price,
                "qty": fill_qty,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        trade = repo.recent_rows("trades", limit=1)[0]
        session_row = repo.recent_rows("sessions", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert buy_order.status.value == "cancelled"
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert trade["order_id"] == buy_order.order_id
        assert trade["qty"] == fill_qty
        assert trade["grid_pnl"] is None
        assert session_row["state"] == "STOPPED"
        assert system_log["module"] == "partial_fill"

    asyncio.run(run())


def test_controller_reconciles_missing_open_order_as_fill_and_updates_pnl(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        sell_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "price": buy_order.price,
                "qty": buy_order.qty,
                "order_id": buy_order.order_id,
            }
        )
        assert sell_order is not None

        exchange.orders["AAPLUSDT"] = [
            order for order in exchange.orders["AAPLUSDT"] if order["client_id"] != sell_order.client_id
        ]
        exchange.order_statuses[sell_order.client_id] = "FILLED"
        exchange.positions["AAPLUSDT"] = 0.0

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        orders = repo.recent_rows("orders", limit=50)
        sessions = repo.recent_rows("sessions", limit=1)
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert actions == [("AAPLUSDT", "filled_reconciled")]
        assert sell_order.status.value == "filled"
        assert any(row["order_id"] == sell_order.order_id and row["grid_pnl"] > 0 for row in trades)
        assert any(row["client_id"] == sell_order.client_id and row["status"] == "filled" for row in orders)
        assert any(row["status"] == "open" and row["side"] == "BUY" for row in orders)
        assert sessions[0]["realized_pnl"] > 0
        assert controller.active_sessions["AAPLUSDT"].realized_pnl > 0
        assert system_log["module"] == "order_reconciliation"

    asyncio.run(run())


def test_controller_reconciles_missing_open_order_as_partial_fill_and_closes_session(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        partial_qty = 0.123
        exchange = PartialFillLookupExchange(partial_qty)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.orders["AAPLUSDT"] = [
            order for order in exchange.orders["AAPLUSDT"] if order["client_id"] != buy_order.client_id
        ]
        exchange.positions["AAPLUSDT"] = partial_qty

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        trade = repo.recent_rows("trades", limit=1)[0]
        session_row = repo.recent_rows("sessions", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=5)

        assert actions == [("AAPLUSDT", "partial_fill_reconciled")]
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert trade["order_id"] == buy_order.order_id
        assert trade["side"] == "BUY"
        assert trade["price"] == 99.5
        assert trade["qty"] == partial_qty
        assert trade["grid_index"] == buy_order.grid_index
        assert trade["grid_pnl"] is None
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "部分成交撤单或累计成交对账失败，执行安全平仓。"
        assert any(log["module"] == "order_reconciliation" for log in logs)
        assert any(log["module"] == "partial_fill" for log in logs)

    asyncio.run(run())


def test_controller_reconciles_underfilled_filled_order_as_partial_fill(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        partial_qty = 0.123
        exchange = UnderfilledLookupExchange(partial_qty)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.orders["AAPLUSDT"] = [
            order for order in exchange.orders["AAPLUSDT"] if order["client_id"] != buy_order.client_id
        ]
        exchange.positions["AAPLUSDT"] = partial_qty

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        trade = repo.recent_rows("trades", limit=1)[0]
        order_row = next(row for row in repo.recent_rows("orders", limit=50) if row["client_id"] == buy_order.client_id)
        session_row = repo.recent_rows("sessions", limit=1)[0]

        assert actions == [("AAPLUSDT", "partial_fill_reconciled")]
        assert "AAPLUSDT" not in controller.active_sessions
        assert trade["order_id"] == buy_order.order_id
        assert trade["qty"] == partial_qty
        assert trade["grid_pnl"] is None
        assert order_row["status"] == "cancelled"
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "部分成交撤单或累计成交对账失败，执行安全平仓。"

    asyncio.run(run())


def test_controller_stops_poll_after_partial_fill_close_failure(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        partial_qty = 0.123
        exchange = PartialFillCloseFailureExchange(partial_qty)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.orders["AAPLUSDT"] = [
            order for order in exchange.orders["AAPLUSDT"] if order["client_id"] != buy_order.client_id
        ]
        exchange.positions["AAPLUSDT"] = partial_qty
        exchange.fail_ticker = True

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        session_row = Repository(db_path).recent_rows("sessions", limit=1)[0]

        assert actions == [("AAPLUSDT", "partial_fill_reconciled")]
        assert "AAPLUSDT" in controller.active_sessions
        assert controller.active_sessions["AAPLUSDT"].state == GridState.CLOSING
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert session_row["state"] == "CLOSING"
        assert session_row["close_time"] is None

    asyncio.run(run())


def test_controller_records_force_close_trade_fee_and_realized_pnl(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-force-close-accounting.db"
        init_db(db_path)
        exchange = AccountingMarketCloseExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        partial_qty = buy_order.qty / 2
        exchange.positions["AAPLUSDT"] = partial_qty

        await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "status": "PARTIALLY_FILLED",
                "price": buy_order.price,
                "qty": partial_qty,
                "fee": 0.02,
                "realized_pnl": 0.0,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        trades = sorted(repo.recent_rows("trades", limit=10), key=lambda row: row["id"])
        session_row = repo.recent_rows("sessions", limit=1)[0]

        assert len(trades) == 2
        assert trades[0]["fee"] == 0.02
        assert trades[1]["side"] == "SELL"
        assert trades[1]["grid_pnl"] == -0.10
        assert trades[1]["fee"] == 0.04
        assert session_row["realized_pnl"] == -0.16

    asyncio.run(run())


def test_controller_duplicate_partial_fill_event_does_not_duplicate_trade(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        partial_qty = 0.123
        exchange = FlakyCancelAllExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        event = {
            "symbol": "AAPLUSDT",
            "client_id": buy_order.client_id,
            "status": "PARTIALLY_FILLED",
            "price": buy_order.price,
            "qty": partial_qty,
            "order_id": buy_order.order_id,
            "trade_id": "trade-1",
            "side": "BUY",
            "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        }
        exchange.positions["AAPLUSDT"] = partial_qty

        await controller.handle_order_filled_event(event)
        assert controller.active_sessions["AAPLUSDT"].state == GridState.CLOSING

        await controller.handle_order_filled_event(event)

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        session_row = repo.recent_rows("sessions", limit=1)[0]

        assert len(trades) == 1
        assert trades[0]["order_id"] == buy_order.order_id
        assert "AAPLUSDT" not in controller.active_sessions
        assert session_row["state"] == "STOPPED"

    asyncio.run(run())


def test_controller_close_session_is_idempotent_after_stopped(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        first_close = await controller._close_session(session, "first close", datetime(2026, 7, 4, 10, 1, tzinfo=NY))
        second_close = await controller._close_session(session, "second close", datetime(2026, 7, 4, 10, 2, tzinfo=NY))

        session_row = Repository(db_path).recent_rows("sessions", limit=1)[0]

        assert first_close is True
        assert second_close is True
        assert "AAPLUSDT" not in controller.active_sessions
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "first close"

    asyncio.run(run())


def test_controller_close_session_handles_state_machine_stopped_during_force_close(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        stopped_at = datetime(2026, 7, 4, 10, 1, tzinfo=NY)

        async def force_close_marks_stopped(_, __) -> None:
            controller.repository.close_session(session.session_id, "user stream closed first", stopped_at)
            controller.state_machine.transition(
                session.symbol,
                GridState.STOPPED,
                "user_stream_close",
                "stopped while force_close was running",
                stopped_at,
            )

        controller.engine.force_close = force_close_marks_stopped  # type: ignore[method-assign]

        result = await controller._close_session(session, "outer close", datetime(2026, 7, 4, 10, 2, tzinfo=NY))

        session_row = Repository(db_path).recent_rows("sessions", limit=1)[0]

        assert result is True
        assert "AAPLUSDT" not in controller.active_sessions
        assert session.state == GridState.STOPPED
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "user stream closed first"

    asyncio.run(run())


def test_controller_stop_order_fill_closes_session_and_records_trade(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        exchange.positions["AAPLUSDT"] = 0.0

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": f"qg-{session.session_id}-stop-long",
                "order_id": "stop-order-1",
                "side": "SELL",
                "price": session.params.stop_loss_price,  # type: ignore[union-attr]
                "qty": 1.0,
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        trade = repo.recent_rows("trades", limit=1)[0]
        session_row = repo.recent_rows("sessions", limit=1)[0]
        window_row = repo.recent_rows("windows", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert exchange.market_orders == []
        assert trade["order_id"] == "stop-order-1"
        assert trade["side"] == "SELL"
        assert trade["grid_index"] is None
        assert trade["grid_pnl"] is None
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "交易所端止损单成交，关闭会话。"
        assert window_row["status"] == "closed"
        assert system_log["module"] == "risk"
        assert system_log["message"] == "Exchange stop order filled; closing session."

    asyncio.run(run())


def test_controller_arms_delayed_stop_protection_after_first_fill(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = RequiresPositionStopExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        assert session.stop_protection_sides == set()
        assert exchange.stop_orders.get("AAPLUSDT", []) == []

        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.positions["AAPLUSDT"] = buy_order.qty

        new_order = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "price": buy_order.price,
                "qty": buy_order.qty,
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        assert new_order is not None
        assert session.stop_protection_sides == {"long"}
        assert len(exchange.stop_orders["AAPLUSDT"]) == 1
        stop_order = exchange.stop_orders["AAPLUSDT"][0]
        assert stop_order["client_id"] == f"qg-{session.session_id}-stop-long-pos"
        assert stop_order["side"] == "SELL"
        assert stop_order["closePosition"] is True
        assert "AAPLUSDT" in controller.active_sessions

    asyncio.run(run())


def test_controller_closes_session_when_delayed_stop_protection_fails(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingDelayedStopProtectionExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        exchange.positions["AAPLUSDT"] = buy_order.qty

        result = await controller.handle_order_filled_event(
            {
                "symbol": "AAPLUSDT",
                "client_id": buy_order.client_id,
                "order_id": buy_order.order_id,
                "side": "BUY",
                "price": buy_order.price,
                "qty": buy_order.qty,
                "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        system_logs = repo.recent_rows("system_logs", limit=20)

        assert result is None
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert exchange.market_orders[-1]["side"] == "SELL"
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "成交后交易所端止损保护失败，执行安全平仓。"
        assert any(
            log["module"] == "risk"
            and log["message"] == "Failed to arm exchange stop protection after fill; closing session."
            for log in system_logs
        )

    asyncio.run(run())


def test_controller_invalid_stop_order_fill_details_close_session_without_trade(tmp_path) -> None:
    async def run() -> None:
        invalid_events = [
            {"price": "not-a-price", "qty": 1.0},
            {"price": 95.0, "qty": "not-a-qty"},
            {"price": "nan", "qty": 1.0},
            {"price": 95.0, "qty": "inf"},
        ]
        for index, invalid_values in enumerate(invalid_events):
            db_path = tmp_path / f"controller-stop-invalid-{index}.db"
            init_db(db_path)
            exchange = MockExchangeClient()
            controller = TradingController(
                exchange=exchange,
                scheduler=FakeScheduler(),  # type: ignore[arg-type]
                repository=Repository(db_path),
                selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
                observer_config=ObserverConfig(observe_hours=1, min_samples=30),
                grid_config=GridConfig(),
                controller_config=ControllerConfig(
                    capital_per_symbol=200,
                    leverage=10,
                    max_concurrent=1,
                    take_profit_usdt=10,
                    total_capital_limit=1000,
                ),
            )
            await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
            session = controller.active_sessions["AAPLUSDT"]
            exchange.positions["AAPLUSDT"] = 0.0

            result = await controller.handle_order_filled_event(
                {
                    "symbol": "AAPLUSDT",
                    "client_id": f"qg-{session.session_id}-stop-long",
                    "order_id": "stop-order-1",
                    "side": "SELL",
                    "price": invalid_values["price"],
                    "qty": invalid_values["qty"],
                    "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
                }
            )

            repo = Repository(db_path)
            trades = repo.recent_rows("trades", limit=10)
            session_row = repo.recent_rows("sessions", limit=1)[0]

            assert result is None
            assert "AAPLUSDT" not in controller.active_sessions
            assert exchange.orders["AAPLUSDT"] == []
            assert exchange.stop_orders["AAPLUSDT"] == []
            assert trades == []
            assert session_row["state"] == "STOPPED"
            assert session_row["close_reason"] == "交易所端止损单成交，关闭会话。"

    asyncio.run(run())


def test_controller_stop_order_duplicate_event_does_not_duplicate_trade(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FlakyCancelAllExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        stop_event = {
            "symbol": "AAPLUSDT",
            "client_id": f"qg-{session.session_id}-stop-long",
            "order_id": "stop-order-1",
            "side": "SELL",
            "price": session.params.stop_loss_price,  # type: ignore[union-attr]
            "qty": 1.0,
            "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        }
        exchange.positions["AAPLUSDT"] = 0.0

        await controller.handle_order_filled_event(stop_event)
        assert controller.active_sessions["AAPLUSDT"].state == GridState.CLOSING

        await controller.handle_order_filled_event(stop_event)

        repo = Repository(db_path)
        trades = repo.recent_rows("trades", limit=10)
        session_row = repo.recent_rows("sessions", limit=1)[0]

        assert len(trades) == 1
        assert trades[0]["order_id"] == "stop-order-1"
        assert "AAPLUSDT" not in controller.active_sessions
        assert session_row["state"] == "STOPPED"

    asyncio.run(run())


def test_controller_poll_closes_take_profit_session(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        controller.active_sessions["AAPLUSDT"].realized_pnl = 10
        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        window_row = repo.recent_rows("windows", limit=1)[0]

        assert actions == [("AAPLUSDT", "close")]
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.orders["AAPLUSDT"] == []
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "单标的止盈达标。"
        assert window_row["status"] == "closed"
        assert window_row["window_end"] is not None
        assert {row["status"] for row in repo.recent_rows("orders", limit=50)} == {"cancelled"}

    asyncio.run(run())


def test_controller_force_closes_before_price_lookup_when_scheduler_requires_it(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingTickerAfterStartExchange()
        scheduler = FakeScheduler()
        controller = TradingController(
            exchange=exchange,
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        scheduler.force_close = True
        exchange.fail_ticker = True

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]

        assert actions == [("AAPLUSDT", "force_close")]
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.orders["AAPLUSDT"] == []
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "临近盘前，触发全局强制离场。"

    asyncio.run(run())


def test_controller_poll_force_closes_outside_window_before_price_lookup(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingTickerAfterStartExchange()
        scheduler = FakeScheduler()
        controller = TradingController(
            exchange=exchange,
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        scheduler.in_window = False
        exchange.fail_ticker = True

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        session_row = Repository(db_path).recent_rows("sessions", limit=1)[0]

        assert actions == [("AAPLUSDT", "force_close")]
        assert controller.active_sessions == {}
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "不在休市交易窗口，触发全局强制离场。"

    asyncio.run(run())


def test_controller_run_once_force_closes_existing_sessions_in_force_close_window(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        scheduler = FakeScheduler()
        controller = TradingController(
            exchange=exchange,
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(order for order in session.orders if order.side.value == "BUY")
        buy_order.status = OrderStatus.FILLED
        buy_order.fill_price = buy_order.price
        exchange.positions["AAPLUSDT"] = buy_order.qty
        scheduler.force_close = True

        result = await controller.run_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]

        assert result.status == "force_close_window"
        assert controller.active_sessions == {}
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": buy_order.qty,
                "reduce_only": True,
                "client_id": f"qg-{session.session_id}-close-sell",
                "status": "filled",
            }
        ]
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "临近盘前，触发全局强制离场。"

    asyncio.run(run())


def test_controller_run_once_force_close_preempts_position_reconciliation(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = InvalidPositionToleranceRulesExchange("NOPEUSDT", min_qty="nan")
        scheduler = FakeScheduler()
        controller = TradingController(
            exchange=exchange,
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.invalid_symbol = "AAPLUSDT"
        scheduler.force_close = True

        result = await controller.run_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        session_row = Repository(db_path).recent_rows("sessions", limit=1)[0]

        assert result.status == "force_close_window"
        assert controller.active_sessions == {}
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "临近盘前，触发全局强制离场。"

    asyncio.run(run())


def test_controller_run_once_prioritizes_force_close_when_outside_window(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        scheduler = FakeScheduler()
        controller = TradingController(
            exchange=exchange,
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        scheduler.in_window = False
        scheduler.force_close = True

        result = await controller.run_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        assert result.status == "force_close_window"
        assert controller.active_sessions == {}

    asyncio.run(run())


def test_controller_run_once_closes_existing_sessions_outside_window(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        scheduler = FakeScheduler()
        controller = TradingController(
            exchange=exchange,
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        scheduler.in_window = False

        result = await controller.run_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        session_row = Repository(db_path).recent_rows("sessions", limit=1)[0]

        assert result.status == "outside_window"
        assert controller.active_sessions == {}
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "不在休市交易窗口，触发全局强制离场。"

    asyncio.run(run())


def test_controller_run_once_reconciles_untracked_position_outside_window_without_active_sessions(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        exchange.positions["MSFTUSDT"] = -2.0
        scheduler = FakeScheduler(in_window=False)
        controller = TradingController(
            exchange=exchange,
            scheduler=scheduler,  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        result = await controller.run_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        assert result.status == "outside_window"
        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
            }
        ]
        assert exchange.positions["MSFTUSDT"] == 0.0

    asyncio.run(run())


def test_controller_poll_logs_active_position_mismatch(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.positions["AAPLUSDT"] = 0.5
        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        session = Repository(db_path).recent_rows("sessions", limit=1)[0]

        assert ("AAPLUSDT", "position_mismatch") in actions
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 0.5,
                "reduce_only": True,
                "client_id": "qg-1-close-sell",
                "status": "filled",
            }
        ]
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "持仓对账异常，强制同步平仓。"
        assert log["level"] == "WARN"
        assert log["module"] == "position_reconciliation"
        assert log["message"] == "Active session position mismatch detected."
        detail = json.loads(log["detail"])
        assert detail["symbol"] == "AAPLUSDT"
        assert detail["actual_qty"] == 0.5

    asyncio.run(run())


def test_controller_poll_detects_hedge_position_mismatch_when_net_is_zero(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = HedgeExposureExchange("NOPEUSDT", long_qty=0.0, short_qty=0.0)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.hedge_symbol = "AAPLUSDT"
        exchange.long_qty = 0.5
        exchange.short_qty = 0.5
        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        logs = Repository(db_path).recent_rows("system_logs", limit=10)
        log = next(
            row
            for row in logs
            if row["module"] == "position_reconciliation"
            and row["message"] == "Active session position mismatch detected."
        )
        detail = json.loads(log["detail"])

        assert ("AAPLUSDT", "position_mismatch") in actions
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 0.5,
                "reduce_only": True,
                "position_side": "LONG",
                "client_id": "qg-1-close-long",
                "status": "filled",
            },
            {
                "symbol": "AAPLUSDT",
                "side": "BUY",
                "qty": 0.5,
                "reduce_only": True,
                "position_side": "SHORT",
                "client_id": "qg-1-close-short",
                "status": "filled",
            },
        ]
        assert detail["expected_long_qty"] == 0.0
        assert detail["expected_short_qty"] == 0.0
        assert detail["actual_long_qty"] == 0.5
        assert detail["actual_short_qty"] == 0.5

    asyncio.run(run())


def test_controller_closes_active_session_when_position_tolerance_rule_is_invalid(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = InvalidPositionToleranceRulesExchange("NOPEUSDT", min_qty="nan")
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.invalid_symbol = "AAPLUSDT"
        exchange.positions["AAPLUSDT"] = 0.5
        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=5)
        reconciliation_log = next(row for row in logs if row["module"] == "position_reconciliation")

        assert actions == [("AAPLUSDT", "position_reconciliation_failed")]
        assert "AAPLUSDT" not in controller.active_sessions
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 0.5,
                "reduce_only": True,
                "client_id": "qg-1-close-sell",
                "status": "filled",
            }
        ]
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "持仓对账异常，强制同步平仓。"
        assert reconciliation_log["level"] == "ERROR"
        assert "min_qty 不是有限数字" in reconciliation_log["detail"]

    asyncio.run(run())


def test_controller_keeps_session_closing_when_position_response_lacks_quantity(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MissingPositionQtyAfterStartExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.missing_position_qty = True
        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert actions == [("AAPLUSDT", "position_reconciliation_failed")]
        assert "AAPLUSDT" in controller.active_sessions
        assert controller.active_sessions["AAPLUSDT"].state == GridState.CLOSING
        assert session["state"] == "CLOSING"
        assert session["close_time"] is None
        assert system_log["level"] == "ERROR"
        assert system_log["message"] == "Force close failed; session kept active for retry."
        assert "持仓响应缺少数量字段" in system_log["detail"]

    asyncio.run(run())


def test_controller_reconcile_closes_untracked_position_when_tolerance_rule_is_invalid(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = InvalidPositionToleranceRulesExchange("MSFTUSDT", min_qty=-0.001)
        exchange.positions["MSFTUSDT"] = -2.0
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        actions = await controller.reconcile_positions_once(
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            include_inactive=True,
        )

        logs = Repository(db_path).recent_rows("system_logs", limit=5)
        tolerance_log = next(row for row in logs if row["message"] == "Position tolerance invalid; closing untracked exchange position.")

        assert actions == [("MSFTUSDT", "closed_untracked_position")]
        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
            }
        ]
        assert exchange.positions["MSFTUSDT"] == 0.0
        assert tolerance_log["level"] == "ERROR"
        assert tolerance_log["module"] == "position_reconciliation"
        assert "min_qty 必须为非负数" in tolerance_log["detail"]

    asyncio.run(run())


def test_controller_reconcile_rejects_untracked_market_response_without_order_id_when_rules_invalid(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MissingUntrackedMarketOrderIdInvalidRulesExchange()
        exchange.symbols = [{"symbol": "MSFTUSDT", "status": "TRADING", "contractType": "PERPETUAL"}]
        exchange.positions["MSFTUSDT"] = -2.0
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        try:
            await controller.reconcile_positions_once(
                datetime(2026, 7, 4, 10, 1, tzinfo=NY),
                include_inactive=True,
            )
        except ValueError as exc:
            assert "订单响应缺少订单标识" in str(exc)
        else:
            raise AssertionError("missing untracked market order id should fail closed")

        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
            }
        ]

    asyncio.run(run())


def test_controller_reconcile_closes_untracked_position(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        exchange.positions["MSFTUSDT"] = -2.0
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        actions = await controller.reconcile_positions_once(
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            include_inactive=True,
        )

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]

        assert actions == [("MSFTUSDT", "closed_untracked_position")]
        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
            }
        ]
        assert exchange.positions["MSFTUSDT"] == 0.0
        assert log["level"] == "WARN"
        assert log["module"] == "position_reconciliation"
        assert log["message"] == "Closed untracked exchange position."

    asyncio.run(run())


def test_controller_poll_closes_untracked_position_while_session_is_active(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(
                max_concurrent=1,
                symbol_allowlist=("AAPLUSDT", "MSFTUSDT"),
            ),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.positions["MSFTUSDT"] = -2.0

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 1, tzinfo=NY))

        assert ("MSFTUSDT", "closed_untracked_position") in actions
        assert "AAPLUSDT" in controller.active_sessions
        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
            }
        ]
        assert exchange.positions["MSFTUSDT"] == 0.0

    asyncio.run(run())


def test_controller_reconcile_recovers_untracked_market_order_after_unknown_create(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        monkeypatch.setattr("strategy.controller.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = DelayedUntrackedMarketOrderLookupExchange()
        exchange.positions["MSFTUSDT"] = -2.0
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        actions = await controller.reconcile_positions_once(
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            include_inactive=True,
        )

        assert actions == [("MSFTUSDT", "closed_untracked_position")]
        assert exchange.market_lookup_calls == 3
        assert exchange.failed_market_client_id == "qgr-msftusdt-buy"
        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
                "orderId": "market-1",
            }
        ]

    asyncio.run(run())


def test_controller_reconcile_closes_untracked_hedge_positions_when_net_is_zero(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = HedgeExposureExchange("MSFTUSDT", long_qty=0.3, short_qty=0.2)
        exchange.symbols = [{"symbol": "MSFTUSDT", "status": "TRADING", "contractType": "PERPETUAL"}]
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_allowlist=("MSFTUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        actions = await controller.reconcile_positions_once(
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            include_inactive=True,
        )

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        detail = json.loads(log["detail"])

        assert actions == [("MSFTUSDT", "closed_untracked_position")]
        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "SELL",
                "qty": 0.3,
                "reduce_only": True,
                "position_side": "LONG",
                "client_id": "qgr-msftusdt-long",
                "status": "filled",
            },
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 0.2,
                "reduce_only": True,
                "position_side": "SHORT",
                "client_id": "qgr-msftusdt-short",
                "status": "filled",
            },
        ]
        assert abs(detail["actual_qty"] - 0.1) < 1e-12
        assert detail["actual_long_qty"] == 0.3
        assert detail["actual_short_qty"] == 0.2
        assert detail["close_specs"] == [
            {"side": "SELL", "qty": 0.3, "position_side": "LONG"},
            {"side": "BUY", "qty": 0.2, "position_side": "SHORT"},
        ]

    asyncio.run(run())


def test_controller_reconcile_rejects_untracked_market_response_without_order_id(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MissingUntrackedMarketOrderIdExchange()
        exchange.positions["MSFTUSDT"] = -2.0
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        try:
            await controller.reconcile_positions_once(
                datetime(2026, 7, 4, 10, 1, tzinfo=NY),
                include_inactive=True,
            )
        except ValueError as exc:
            assert "订单响应缺少订单标识" in str(exc)
        else:
            raise AssertionError("missing untracked market order id should fail closed")

        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
            }
        ]

    asyncio.run(run())


def test_controller_reconcile_ignores_positions_outside_candidate_symbols(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        exchange.symbols.append({"symbol": "BTCUSDT", "status": "TRADING"})
        exchange.positions["BTCUSDT"] = 3.0
        exchange.positions["MSFTUSDT"] = -2.0
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(
                max_concurrent=1,
                symbol_allowlist=("AAPLUSDT", "MSFTUSDT"),
            ),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )

        actions = await controller.reconcile_positions_once(
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            include_inactive=True,
        )

        assert actions == [("MSFTUSDT", "closed_untracked_position")]
        assert exchange.market_orders == [
            {
                "symbol": "MSFTUSDT",
                "side": "BUY",
                "qty": 2.0,
                "reduce_only": True,
                "client_id": "qgr-msftusdt-buy",
                "status": "filled",
            }
        ]
        assert exchange.positions["MSFTUSDT"] == 0.0
        assert exchange.positions["BTCUSDT"] == 3.0

    asyncio.run(run())


def test_controller_close_all_active_sessions_cleans_up_orders_and_positions(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.positions["AAPLUSDT"] = 1.0

        closed = await controller.close_all_active_sessions(
            "binance_once_cleanup",
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        )

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        window = repo.recent_rows("windows", limit=1)[0]

        assert closed == ["AAPLUSDT"]
        assert controller.active_sessions == {}
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 1.0,
                "reduce_only": True,
                "client_id": "qg-1-close-sell",
                "status": "filled",
            }
        ]
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "binance_once_cleanup"
        assert window["status"] == "closed"

    asyncio.run(run())


def test_controller_close_keeps_session_active_when_cancel_fails_but_flattens_position(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FailingCancelAllExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.positions["AAPLUSDT"] = 1.0

        closed = await controller.close_all_active_sessions(
            "binance_once_cleanup",
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        )

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        logs = repo.recent_rows("system_logs", limit=2)

        assert closed == []
        assert "AAPLUSDT" in controller.active_sessions
        assert controller.active_sessions["AAPLUSDT"].state.value == "CLOSING"
        assert exchange.market_orders == [
            {
                "symbol": "AAPLUSDT",
                "side": "SELL",
                "qty": 1.0,
                "reduce_only": True,
                "client_id": "qg-1-close-sell",
                "status": "filled",
            }
        ]
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert session["state"] == "CLOSING"
        assert session["close_time"] is None
        assert logs[0]["level"] == "ERROR"
        assert logs[0]["module"] == "controller"
        assert logs[0]["message"] == "Force close failed; session kept active for retry."
        assert logs[1]["level"] == "WARN"
        assert logs[1]["module"] == "force_close"

    asyncio.run(run())


def test_controller_retries_close_after_previous_cancel_failure(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = FlakyCancelAllExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        exchange.positions["AAPLUSDT"] = 1.0

        first_closed = await controller.close_all_active_sessions(
            "binance_once_cleanup",
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        )
        second_closed = await controller.close_all_active_sessions(
            "binance_once_cleanup_retry",
            datetime(2026, 7, 4, 10, 2, tzinfo=NY),
        )

        repo = Repository(db_path)
        session = repo.recent_rows("sessions", limit=1)[0]
        window = repo.recent_rows("windows", limit=1)[0]

        assert first_closed == []
        assert second_closed == ["AAPLUSDT"]
        assert exchange.cancel_attempts == 2
        assert controller.active_sessions == {}
        assert exchange.orders["AAPLUSDT"] == []
        assert exchange.stop_orders["AAPLUSDT"] == []
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "binance_once_cleanup_retry"
        assert window["status"] == "closed"

    asyncio.run(run())


def test_controller_run_loop_starts_then_polls_active_sessions(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        repo = Repository(db_path)
        repo.register_runtime("runtime-1", datetime(2026, 7, 4, 9, 59, tzinfo=NY))
        repo.request_round_start("网页启动", "round-1", datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                loop_interval_seconds=3,
                scheduler_check_minutes=5,
            ),
        )

        statuses = await controller.run_loop(max_iterations=2, sleep_fn=fake_sleep)

        assert statuses == ["started", "poll:"]
        assert sleeps == [3, 3]
        assert list(controller.active_sessions) == ["AAPLUSDT"]
        assert len(Repository(db_path).recent_rows("windows")) == 1
        request = repo.round_start_request(include_terminal=True)
        assert request is not None
        assert request["status"] == "running"

    asyncio.run(run())


def test_controller_run_loop_waits_for_explicit_round_start(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                loop_interval_seconds=3,
            ),
        )

        statuses = await controller.run_loop(max_iterations=1, sleep_fn=fake_sleep)

        assert statuses == ["idle_waiting_start"]
        assert sleeps == [3]
        assert controller.active_sessions == {}
        assert Repository(db_path).recent_rows("windows") == []

    asyncio.run(run())


def test_round_start_failure_records_fill_and_closes_position_immediately(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-round-start-failure-fill.db"
        init_db(db_path)
        repo = Repository(db_path)
        exchange = FilledDuringRoundStartFailureExchange()
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-start-failure", now)
        repo.request_round_start("启动", "round-start-failure", now)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(
                max_concurrent=1,
                scan_candidate_count=1,
                symbol_blacklist=("TSLAPREUSDT",),
            ),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )

        result = await controller.start_round(now)

        trades = sorted(repo.recent_rows("trades", limit=10), key=lambda row: row["id"])
        session = repo.recent_rows("sessions", limit=1)[0]
        candidate = repo.round_candidates(controller.current_window_id)[0]
        assert result.started_symbols == []
        assert controller.active_sessions == {}
        assert exchange.positions["AAPLUSDT"] == 0.0
        assert exchange.orders["AAPLUSDT"] == []
        assert len(trades) == 2
        assert trades[0]["side"] == "BUY"
        assert trades[0]["qty"] == 0.5
        assert trades[0]["fee"] == 0.02
        assert trades[1]["side"] == "SELL"
        assert trades[1]["qty"] == 0.5
        assert trades[1]["grid_pnl"] == -0.10
        assert trades[1]["fee"] == 0.04
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "grid_start_failed"
        assert session["realized_pnl"] == -0.16
        assert candidate["stage"] == "stopped"

    asyncio.run(run())


def test_partial_directional_seed_is_recorded_and_safely_closed(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-partial-direction-seed.db"
        init_db(db_path)
        repo = Repository(db_path)
        exchange = PartialDirectionalSeedExchange()
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-partial-seed", now)
        repo.request_round_start("启动", "round-partial-seed", now)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(
                max_concurrent=1,
                scan_candidate_count=1,
                symbol_blacklist=("TSLAPREUSDT",),
            ),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=500,
                leverage=1,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=2000,
                direction_mode=GridDirectionMode.LONG,
            ),
        )

        result = await controller.start_round(now)

        session = repo.recent_rows("sessions", limit=1)[0]
        trades = sorted(repo.recent_rows("trades", limit=10), key=lambda row: row["id"])
        assert result.started_symbols == []
        assert controller.active_sessions == {}
        assert exchange.hedge_positions["AAPLUSDT"]["LONG"] == 0.0
        assert exchange.orders.get("AAPLUSDT", []) == []
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "grid_start_failed"
        assert session["direction_mode"] == "LONG"
        assert session["seed_qty"] > 0
        assert session["seed_entry_price"] == 100.0
        assert [trade["side"] for trade in trades] == ["BUY", "SELL"]
        assert trades[0]["qty"] == session["seed_qty"]

    asyncio.run(run())


def test_session_pause_and_resume_update_round_candidate_stage_immediately(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-session-pause-stage.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-session-pause", now)
        repo.request_round_start("启动", "round-session-pause", now)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, scan_candidate_count=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )
        await controller.start_round(now)
        session = controller.active_sessions["AAPLUSDT"]
        repo.request_session_control(session.session_id, session.symbol, "pause", "暂停", "pause-stage", now)

        await controller.poll_active_sessions_once(now + timedelta(seconds=10))

        assert repo.round_candidates(controller.current_window_id)[0]["stage"] == "paused"
        repo.request_session_control(session.session_id, session.symbol, "resume", "恢复", "resume-stage", now)
        await controller.poll_active_sessions_once(now + timedelta(seconds=20))

        candidate = repo.round_candidates(controller.current_window_id)[0]
        assert session.state == GridState.RUNNING
        assert candidate["stage"] == "trading"

    asyncio.run(run())


def test_round_pause_keeps_scanning_and_resume_starts_eligible_symbol(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-round-pause-resume.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-paused-scan", now)
        repo.request_round_start("启动", "round-paused-scan", now)
        repo.set_control_state("new_entries_paused", True, now)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, scan_candidate_count=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )

        first = await controller.start_round(now)

        assert first.started_symbols == []
        assert controller.active_sessions == {}
        candidate = repo.round_candidates(controller.current_window_id)[0]
        assert candidate["threshold_met"] == 1
        assert candidate["stage"] == "eligible"
        assert candidate["error"] == "新开仓已暂停"

        repo.set_control_state("new_entries_paused", False, now + timedelta(seconds=30))
        second = await controller.scan_round_once(now + timedelta(minutes=1))

        assert second.started_symbols == ["AAPLUSDT"]
        assert list(controller.active_sessions) == ["AAPLUSDT"]

    asyncio.run(run())


def test_active_round_candidate_volatility_updates_on_next_scan(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-active-candidate-volatility.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-active-volatility", now)
        repo.request_round_start("启动", "round-active-volatility", now)
        controller = TradingController(
            exchange=CountingVolatilityExchange(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, scan_candidate_count=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )
        await controller.start_round(now)
        candidate = repo.round_candidates(controller.current_window_id)[0]
        assert candidate["calculated_at"] == now.isoformat()

        scanned_at = now + timedelta(minutes=1)
        await controller.scan_round_once(scanned_at)

        candidate = repo.round_candidates(controller.current_window_id)[0]
        session = repo.get_session(controller.active_sessions["AAPLUSDT"].session_id)
        assert candidate["stage"] == "trading"
        assert candidate["calculated_at"] == scanned_at.isoformat()
        assert candidate["volatility_value"] is not None
        assert session["volatility_current_at"] == scanned_at.isoformat()

    asyncio.run(run())


def test_round_without_eligible_symbol_stays_scanning_without_pseudo_sessions(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-scan", now)
        repo.request_round_start("网页启动", "round-scan", now)
        controller = TradingController(
            exchange=InvalidRangeExchange(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, scan_candidate_count=2, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000, loop_interval_seconds=1),
        )

        statuses = await controller.run_loop(max_iterations=1, sleep_fn=lambda _seconds: asyncio.sleep(0))

        assert statuses == ["scanning"]
        assert controller.round_active is True
        assert controller.active_sessions == {}
        assert repo.recent_rows("sessions") == []
        window = repo.recent_rows("windows", limit=1)[0]
        assert window["status"] == "SCANNING"
        candidates = repo.round_candidates(int(window["id"]))
        assert candidates
        assert {row["stage"] for row in candidates} == {"below_threshold"}

    asyncio.run(run())


def test_round_start_immediately_calculates_from_historical_one_minute_klines(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        exchange = CountingVolatilityExchange()
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-history", now)
        repo.request_round_start("网页启动", "round-history", now)
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, scan_candidate_count=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30, live_observation=True),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )

        result = await controller.start_round(now)

        assert result.status == "started"
        # required_bars=60 + buffer_bars=2 for closed-bar prefill
        assert exchange.kline_calls == [("AAPLUSDT", "1m", 62)]
        candidate = repo.round_candidates(controller.current_window_id)[0]
        assert candidate["calculated_at"] == now.isoformat()

    asyncio.run(run())


def test_closed_one_minute_kline_is_processed_once(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        window_id = repo.create_window(now)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )
        controller.current_window_id = window_id
        controller.round_active = True
        controller.round_candidate_symbols.add("AAPLUSDT")
        repo.upsert_round_candidate(window_id, "AAPLUSDT", now, stage="scanning")
        calls = 0

        async def fake_scan(_now=None, **_kwargs):
            nonlocal calls
            calls += 1
            return RunOnceResult("scanning", [], [])

        controller.scan_round_once = fake_scan  # type: ignore[method-assign]
        event = {"symbol": "AAPLUSDT", "closed": True, "close_time": now}

        assert await controller.handle_kline_closed_event(event) == "scanned"
        assert await controller.handle_kline_closed_event(event) is None
        assert await controller.handle_kline_closed_event({**event, "closed": False}) is None
        assert calls == 1

    asyncio.run(run())


def test_same_minute_multi_symbol_kline_events_trigger_one_full_scan(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        window_id = repo.create_window(now)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=3),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 1, 3, 10, 1000),
        )
        controller.current_window_id = window_id
        controller.round_active = True
        symbols = ("BTCUSDT", "BCHUSDT", "ETHUSDT")
        controller.round_candidate_symbols.update(symbols)
        for symbol in symbols:
            repo.upsert_round_candidate(window_id, symbol, now, stage="scanning")
        calls = 0

        async def fake_scan(_now=None):
            nonlocal calls
            calls += 1
            return RunOnceResult("scanning", list(symbols), [])

        controller._scan_round_once_locked = fake_scan  # type: ignore[method-assign]
        results = [
            await controller.handle_kline_closed_event(
                {"symbol": symbol, "closed": True, "close_time": now}
            )
            for symbol in symbols
        ]

        assert results == ["scanned", "coalesced", "coalesced"]
        assert calls == 1

    asyncio.run(run())


def test_stopped_symbol_is_not_restarted_and_next_candidate_fills_slot(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        now = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
        repo.register_runtime("runtime-fill", now)
        repo.request_round_start("网页启动", "round-fill", now)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, scan_candidate_count=2, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )

        first = await controller.start_round(now)
        assert first.started_symbols == ["AAPLUSDT"]
        await controller._close_session(controller.active_sessions["AAPLUSDT"], "手动停止", now)
        second = await controller.scan_round_once(now + timedelta(minutes=1))

        assert second.started_symbols == ["MSFTUSDT"]
        assert set(controller.active_sessions) == {"MSFTUSDT"}
        assert "AAPLUSDT" in controller.round_stopped_symbols
        assert [row["symbol"] for row in repo.recent_rows("sessions", limit=10)] == ["MSFTUSDT", "AAPLUSDT"]

    asyncio.run(run())


def test_controller_poll_applies_runtime_take_profit_draft(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        start_at = datetime(2026, 7, 4, 10, 0, tzinfo=NY)

        await controller.run_once(start_at)
        session = controller.active_sessions["AAPLUSDT"]
        session.realized_pnl = 5
        repo.set_strategy_config_draft(
            {
                "volatility_method": "std",
                "max_concurrent": 1,
                "observe_hours": 1,
                "min_step_pct": 0.0015,
                "max_grid_num": 20,
                "take_profit_usdt": 4,
                "total_capital_limit": 1000,
                "max_maker_fee_rate": 0,
            },
            start_at,
        )

        actions = await controller.poll_active_sessions_once(start_at + timedelta(seconds=10))

        session_row = repo.get_session(session.session_id)
        log = next(row for row in repo.recent_rows("system_logs", limit=10) if row["message"] == "Runtime strategy config draft applied.")

        assert controller.config.take_profit_usdt == 4
        assert controller.risk.config.take_profit_usdt == 4
        assert actions == [("AAPLUSDT", "close")]
        assert session_row is not None
        assert session_row["close_reason"] == "单标的止盈达标。"
        assert log["level"] == "INFO"

    asyncio.run(run())


def test_controller_poll_applies_runtime_maker_fee_limit_draft(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        repo = Repository(db_path)
        exchange = ChangingCommissionExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_maker_fee_rate=0.0003,
                maker_fee_check_interval_seconds=1,
            ),
        )
        start_at = datetime(2026, 7, 4, 10, 0, tzinfo=NY)

        await controller.run_once(start_at)
        exchange.maker_fee = 0.0002
        repo.set_strategy_config_draft(
            {
                "volatility_method": "std",
                "max_concurrent": 1,
                "observe_hours": 1,
                "min_step_pct": 0.0015,
                "max_grid_num": 20,
                "take_profit_usdt": 10,
                "total_capital_limit": 1000,
                "max_maker_fee_rate": 0,
            },
            start_at,
        )

        actions = await controller.poll_active_sessions_once(start_at + timedelta(seconds=2))

        health = repo.latest_commission_health()
        assert controller.config.max_maker_fee_rate == 0
        assert actions[0] == ("*", "commission_warn")
        assert health is not None
        assert health["status"] == "warn"
        assert health["symbols"][0]["max_maker_fee_rate"] == 0

    asyncio.run(run())


def test_controller_runtime_draft_defers_risk_increases(tmp_path) -> None:
    db_path = tmp_path / "controller.db"
    init_db(db_path)
    repo = Repository(db_path)
    controller = TradingController(
        exchange=MockExchangeClient(),
        scheduler=FakeScheduler(),  # type: ignore[arg-type]
        repository=repo,
        selector_config=SelectionConfig(max_concurrent=1),
        observer_config=ObserverConfig(observe_hours=1, min_samples=30),
        grid_config=GridConfig(),
        controller_config=ControllerConfig(
            capital_per_symbol=200,
            leverage=1,
            max_concurrent=1,
            take_profit_usdt=10,
            total_capital_limit=1000,
            max_maker_fee_rate=0,
        ),
    )
    at = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
    repo.set_strategy_config_draft(
        {
            "capital_per_symbol": 400,
            "leverage": 2,
            "max_concurrent": 3,
            "take_profit_usdt": 20,
            "total_capital_limit": 2000,
            "max_maker_fee_rate": 0.001,
        },
        at,
    )

    controller._apply_runtime_config_draft(at)

    assert controller.config.capital_per_symbol == 200
    assert controller.config.leverage == 1
    assert controller.config.max_concurrent == 1
    assert controller.config.take_profit_usdt == 10
    assert controller.config.total_capital_limit == 1000
    assert controller.config.max_maker_fee_rate == 0
    log = repo.recent_rows("system_logs", limit=1)[0]
    assert log["message"] == "Risk-increasing runtime config changes were deferred."


def test_controller_runtime_draft_applies_risk_reductions(tmp_path) -> None:
    db_path = tmp_path / "controller.db"
    init_db(db_path)
    repo = Repository(db_path)
    controller = TradingController(
        exchange=MockExchangeClient(),
        scheduler=FakeScheduler(),  # type: ignore[arg-type]
        repository=repo,
        selector_config=SelectionConfig(max_concurrent=3),
        observer_config=ObserverConfig(observe_hours=1, min_samples=30),
        grid_config=GridConfig(),
        controller_config=ControllerConfig(
            capital_per_symbol=300,
            leverage=3,
            max_concurrent=3,
            take_profit_usdt=20,
            total_capital_limit=2000,
            max_maker_fee_rate=0.001,
        ),
    )
    at = datetime(2026, 7, 4, 10, 0, tzinfo=NY)
    repo.set_strategy_config_draft(
        {
            "capital_per_symbol": 200,
            "leverage": 1,
            "max_concurrent": 1,
            "take_profit_usdt": 10,
            "total_capital_limit": 1000,
            "max_maker_fee_rate": 0,
        },
        at,
    )

    controller._apply_runtime_config_draft(at)

    assert controller.config.capital_per_symbol == 200
    assert controller.config.leverage == 1
    assert controller.config.max_concurrent == 1
    assert controller.config.take_profit_usdt == 10
    assert controller.config.total_capital_limit == 1000
    assert controller.config.max_maker_fee_rate == 0


def test_controller_marks_session_closing_before_force_close_io(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-closing-before-io.db"
        init_db(db_path)
        repo = Repository(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=repo,
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]

        async def force_close(_session, _reason):
            assert session.state == GridState.CLOSING
            assert repo.recent_rows("sessions", limit=1)[0]["state"] == "CLOSING"
            return []

        controller.engine.force_close = force_close  # type: ignore[method-assign]

        assert await controller._close_session(session, "test close", datetime(2026, 7, 4, 10, 1, tzinfo=NY))
        assert repo.recent_rows("sessions", limit=1)[0]["state"] == "STOPPED"

    asyncio.run(run())


def test_controller_risk_decision_ignores_closing_and_stopped_sessions(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-risk-terminal-state.db"
        init_db(db_path)
        controller = TradingController(
            exchange=MockExchangeClient(),
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        calls: list[str] = []

        async def enter_cooldown(_session, _reason, _at):
            calls.append("cooldown")

        controller._enter_cooldown = enter_cooldown  # type: ignore[method-assign]
        session.state = GridState.CLOSING
        await controller._apply_risk_decision(
            session,
            RiskAction.COOLDOWN,
            "closing",
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        )
        session.state = GridState.STOPPED
        await controller._apply_risk_decision(
            session,
            RiskAction.COOLDOWN,
            "stopped",
            datetime(2026, 7, 4, 10, 2, tzinfo=NY),
        )

        assert calls == []

    asyncio.run(run())


def test_controller_cooldown_records_force_close_trade(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-cooldown-accounting.db"
        init_db(db_path)
        exchange = AccountingMarketCloseExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(200, 10, 1, 10, 1000),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        exchange.positions["AAPLUSDT"] = 0.5

        await controller._enter_cooldown(
            session,
            "range break",
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        )

        trade = Repository(db_path).recent_rows("trades", limit=1)[0]
        assert session.state == GridState.COOLDOWN
        assert trade["side"] == "SELL"
        assert trade["qty"] == 0.5
        assert trade["fee"] == 0.04
        assert trade["grid_pnl"] == -0.10
        assert session.realized_pnl == -0.14

    asyncio.run(run())


def test_controller_price_event_enters_defensive_without_force_close(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]

        action = await controller.handle_price_update_event(
            {
                "symbol": "AAPLUSDT",
                "price": session.params.lower - 0.01,  # type: ignore[union-attr]
                "event_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        assert action == "defend"
        assert session.state.value == "DEFENSIVE"
        assert exchange.market_orders == []
        assert Repository(db_path).recent_rows("sessions", limit=1)[0]["state"] == "DEFENSIVE"

    asyncio.run(run())


def test_controller_price_event_enters_cooldown_on_upper_hard_stop(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        assert session.params is not None
        stop_buffer_pct = 1 - session.params.stop_loss_price / session.params.lower

        action = await controller.handle_price_update_event(
            {
                "symbol": "AAPLUSDT",
                "price": session.params.upper * (1 + stop_buffer_pct),
                "event_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        window_row = repo.recent_rows("windows", limit=1)[0]

        assert action == "cooldown"
        assert "AAPLUSDT" in controller.active_sessions
        assert exchange.orders["AAPLUSDT"] == []
        assert session_row["state"] == "COOLDOWN"
        assert session_row["close_reason"] is None
        assert window_row["status"] == "open"

    asyncio.run(run())


def test_controller_recovers_from_cooldown_and_restarts_grid(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = CooldownRecoveryExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        await controller.handle_price_update_event(
            {
                "symbol": "AAPLUSDT",
                "price": session.params.stop_loss_price - 0.01,  # type: ignore[union-attr]
                "event_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 20, tzinfo=NY))

        assert actions == [("AAPLUSDT", "recovered")]
        assert session.state.value == "RUNNING"
        assert exchange.orders["AAPLUSDT"]
        triggers = {row["trigger"] for row in Repository(db_path).recent_rows("state_logs", limit=10)}
        assert {"cooldown_recovered", "grid_restarted"}.issubset(triggers)

    asyncio.run(run())


def test_controller_cooldown_recovery_uses_session_actual_grid_step(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = CooldownRecoveryExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(min_step_pct=0.0015),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        assert session.params is not None
        session.params = replace(session.params, step_pct=0.007068)
        captured: dict[str, float] = {}

        def capture_step(*_args, **kwargs):
            captured["min_step_pct"] = float(kwargs["min_step_pct"])
            return CooldownDecision(False, "captured")

        controller.cooldown.evaluate = capture_step  # type: ignore[method-assign]
        await controller._enter_cooldown(
            session,
            "hard stop",
            datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        )

        recovered = await controller._try_recover_from_cooldown(
            session,
            datetime(2026, 7, 4, 10, 20, tzinfo=NY),
        )

        assert recovered is False
        assert captured["min_step_pct"] == 0.007068
        assert captured["min_step_pct"] != controller.grid_config.min_step_pct
        persisted = Repository(db_path).get_session(session.session_id)
        assert persisted is not None
        assert persisted["cooldown_amplitude_limit_pct"] == 0.007068 * controller.cooldown.config.amplitude_multiplier
        assert persisted["cooldown_reason"] == "captured"

    asyncio.run(run())


def test_controller_stops_session_when_cooldown_recovery_fails(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = CooldownRecoveryCalculationFailureExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        await controller.handle_price_update_event(
            {
                "symbol": "AAPLUSDT",
                "price": session.params.stop_loss_price - 0.01,  # type: ignore[union-attr]
                "event_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 20, tzinfo=NY))

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        window_row = repo.recent_rows("windows", limit=1)[0]
        triggers = {row["trigger"] for row in repo.recent_rows("state_logs", limit=10)}
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert actions == []
        assert "AAPLUSDT" not in controller.active_sessions
        assert session_row["state"] == "STOPPED"
        assert session_row["close_reason"] == "cooldown_recovery_failed"
        assert window_row["status"] == "closed"
        assert "cooldown_recovery_failed" in triggers
        assert system_log["level"] == "ERROR"
        assert system_log["message"] == "Cooldown recovery failed; session stopped."

    asyncio.run(run())


def test_controller_keeps_session_closing_when_cooldown_recovery_failure_close_fails(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller.db"
        init_db(db_path)
        exchange = CooldownRecoveryFailureWithCloseFailureExchange()
        controller = TradingController(
            exchange=exchange,
            scheduler=FakeScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(max_concurrent=1, symbol_blacklist=("TSLAPREUSDT",)),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        await controller.handle_price_update_event(
            {
                "symbol": "AAPLUSDT",
                "price": session.params.stop_loss_price - 0.01,  # type: ignore[union-attr]
                "event_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
            }
        )

        actions = await controller.poll_active_sessions_once(datetime(2026, 7, 4, 10, 20, tzinfo=NY))

        repo = Repository(db_path)
        session_row = repo.recent_rows("sessions", limit=1)[0]
        window_row = repo.recent_rows("windows", limit=1)[0]
        triggers = {row["trigger"] for row in repo.recent_rows("state_logs", limit=10)}
        system_log = repo.recent_rows("system_logs", limit=1)[0]

        assert actions == []
        assert "AAPLUSDT" in controller.active_sessions
        assert controller.active_sessions["AAPLUSDT"].state == GridState.CLOSING
        assert session_row["state"] == "CLOSING"
        assert session_row["close_time"] is None
        assert window_row["status"] == "open"
        assert "cooldown_recovery_force_close_failed" in triggers
        assert system_log["level"] == "ERROR"
        assert system_log["message"] == "Force close failed after cooldown recovery failure."

    asyncio.run(run())
