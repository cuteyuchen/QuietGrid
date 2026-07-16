from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from core.models import GridState, OrderSide, OrderStatus, SymbolSession
from exchange.mock import MockExchangeClient
from strategy.adaptive_grid import AdaptiveGridGenerator
from strategy.grid_engine import GridEngine


def _session() -> SymbolSession:
    rows = []
    for index in range(90):
        close = 100.0 + ((index % 10) - 5) * 0.03
        rows.append({"high": close + 0.08, "low": close - 0.08, "close": close})
    params = AdaptiveGridGenerator().generate(
        "AAPLUSDT",
        rows,
        current_price=99.97,
        funding_rate=0,
        maker_fee_rate=0,
        regime_score=90,
    )
    return SymbolSession(
        session_id=1,
        symbol="AAPLUSDT",
        state=GridState.RUNNING,
        params=params,
        orders=[],
        realized_pnl=0,
        capital=200,
        leverage=1,
        open_time=datetime.now(timezone.utc),
    )


def test_adaptive_grid_engine_uses_non_uniform_level_quantities() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()

        orders = await GridEngine(exchange).start(session, current_price=99.97)

        quantities = {round(order.qty, 6) for order in orders}
        assert len(quantities) > 1
        total_notional = sum(order.price * order.qty for order in orders)
        assert total_notional == pytest.approx(200, rel=0.01)

    asyncio.run(run())


def test_inventory_suppression_only_cancels_opening_orders_on_risk_side() -> None:
    async def run() -> None:
        exchange = MockExchangeClient()
        session = _session()
        engine = GridEngine(exchange)
        await engine.start(session, current_price=99.97)
        closing_buy = next(order for order in session.orders if order.side == OrderSide.BUY)
        closing_buy.entry_price = 101

        cancelled = await engine.suppress_inventory_increasing_orders(session, net_qty=1)

        assert cancelled
        assert all(order.side == OrderSide.BUY for order in cancelled)
        assert all(order.entry_price is None for order in cancelled)
        assert closing_buy.status == OrderStatus.OPEN
        assert any(order.status == OrderStatus.OPEN and order.side == OrderSide.SELL for order in session.orders)

    asyncio.run(run())
