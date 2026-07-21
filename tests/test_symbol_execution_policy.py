from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.models import OrderIntent, OrderSide, OrderStatus
from db.database import init_db
from db.repository import Repository
from exchange.mock import MockExchangeClient
from strategy.controller import ControllerConfig, TradingController
from strategy.grid_calculator import GridConfig
from strategy.observer import ObserverConfig
from strategy.selector import SelectionConfig


NY = ZoneInfo("America/New_York")


class _WindowScheduler:
    def is_in_window(self, now_utc=None) -> bool:
        return True

    def should_force_close(self, now_utc=None) -> bool:
        return False


def test_controller_applies_symbol_lot_cap_and_fractional_reduce_target(tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "controller-symbol-lot-cap.db"
        init_db(db_path)
        exchange = MockExchangeClient()
        controller = TradingController(
            exchange=exchange,
            scheduler=_WindowScheduler(),  # type: ignore[arg-type]
            repository=Repository(db_path),
            selector_config=SelectionConfig(
                max_concurrent=1,
                symbol_blacklist=("TSLAPREUSDT",),
            ),
            observer_config=ObserverConfig(observe_hours=1, min_samples=30),
            grid_config=GridConfig(),
            controller_config=ControllerConfig(
                capital_per_symbol=200,
                leverage=10,
                max_concurrent=1,
                take_profit_usdt=10,
                total_capital_limit=1000,
                max_unpaired_lots_per_side_by_symbol={"AAPLUSDT": 1},
                reduce_target_step_fraction_by_symbol={"AAPLUSDT": 0.5},
            ),
        )
        await controller.run_once(datetime(2026, 7, 4, 10, 0, tzinfo=NY))
        session = controller.active_sessions["AAPLUSDT"]
        buy_order = next(
            order for order in session.orders
            if order.side == OrderSide.BUY and order.order_intent == OrderIntent.OPEN
        )
        initial_buy_count = sum(
            1 for order in session.orders
            if order.side == OrderSide.BUY and order.order_intent == OrderIntent.OPEN
        )
        exchange.positions["AAPLUSDT"] = buy_order.qty

        reduce_order = await controller.handle_order_filled_event({
            "symbol": "AAPLUSDT",
            "client_id": buy_order.client_id,
            "price": buy_order.price,
            "qty": buy_order.qty,
            "order_id": buy_order.order_id,
            "trade_time": datetime(2026, 7, 4, 10, 1, tzinfo=NY),
        })

        assert reduce_order is not None
        assert reduce_order.order_intent == OrderIntent.REDUCE
        full_target = session.params.grid_prices[buy_order.grid_index + 1]  # type: ignore[union-attr]
        assert reduce_order.price == pytest.approx(
            buy_order.price + (full_target - buy_order.price) * 0.5,
            abs=0.01,
        )
        assert all(
            order.status != OrderStatus.OPEN
            for order in session.orders
            if order.side == OrderSide.BUY and order.order_intent == OrderIntent.OPEN
        )

        exchange.positions["AAPLUSDT"] = 0.0
        await controller.handle_order_filled_event({
            "symbol": "AAPLUSDT",
            "client_id": reduce_order.client_id,
            "price": reduce_order.price,
            "qty": reduce_order.qty,
            "order_id": reduce_order.order_id,
            "trade_time": datetime(2026, 7, 4, 10, 2, tzinfo=NY),
        })

        assert sum(
            1 for order in session.orders
            if order.side == OrderSide.BUY
            and order.order_intent == OrderIntent.OPEN
            and order.status == OrderStatus.OPEN
        ) == initial_buy_count

    asyncio.run(run())
