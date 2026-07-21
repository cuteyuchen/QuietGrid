from __future__ import annotations

from datetime import datetime, timezone

from core.models import GridOrder, OrderSide, OrderStatus
from strategy.inventory import InventoryAction, InventoryLevel, InventoryManager


NOW = datetime.now(timezone.utc)


def _filled(
    order_id: str,
    side: OrderSide,
    price: float,
    qty: float,
    *,
    entry_price: float | None = None,
) -> GridOrder:
    return GridOrder(
        symbol="BTCUSDT",
        order_id=order_id,
        client_id=f"client-{order_id}",
        grid_index=1,
        side=side,
        price=price,
        qty=qty,
        status=OrderStatus.FILLED,
        created_at=NOW,
        filled_at=NOW,
        fill_price=price,
        entry_price=entry_price,
    )


def test_inventory_pairs_open_and_close_fills() -> None:
    manager = InventoryManager()
    orders = [
        _filled("open", OrderSide.BUY, 100, 1),
        _filled("close", OrderSide.SELL, 101, 1, entry_price=100),
    ]

    snapshot = manager.snapshot(orders, mark_price=101, max_inventory_notional=200)

    assert snapshot.net_qty == 0
    assert snapshot.gross_notional == 0
    assert snapshot.unpaired_lots == ()
    assert snapshot.level == InventoryLevel.NORMAL


def test_caution_suppresses_orders_that_increase_long_inventory() -> None:
    decision = InventoryManager().evaluate(
        [_filled("open", OrderSide.BUY, 100, 1)],
        mark_price=100,
        max_inventory_notional=200,
    )

    assert decision.action == InventoryAction.SUPPRESS_LONG
    assert decision.snapshot.level == InventoryLevel.CAUTION
    assert decision.snapshot.utilization == 0.5


def test_directional_seed_is_baseline_for_inventory_risk() -> None:
    decision = InventoryManager().evaluate(
        [_filled("seed", OrderSide.BUY, 100, 1)],
        mark_price=100,
        max_inventory_notional=200,
        baseline_inventory_notional=100,
    )

    assert decision.action == InventoryAction.ALLOW
    assert decision.snapshot.utilization == 0.0
    assert decision.snapshot.level == InventoryLevel.NORMAL


def test_critical_inventory_closes_session() -> None:
    decision = InventoryManager().evaluate(
        [_filled("open", OrderSide.SELL, 100, 2)],
        mark_price=100,
        max_inventory_notional=200,
        trend_direction=1,
        minutes_to_close=10,
    )

    assert decision.action == InventoryAction.CLOSE
    assert decision.snapshot.level == InventoryLevel.CRITICAL
    assert decision.snapshot.net_qty == -2
    assert decision.snapshot.risk_score > 70


def test_hedged_gross_inventory_still_consumes_risk_budget() -> None:
    snapshot = InventoryManager().snapshot(
        [
            _filled("long", OrderSide.BUY, 100, 1),
            _filled("short", OrderSide.SELL, 100, 1),
        ],
        mark_price=100,
        max_inventory_notional=200,
    )

    assert snapshot.net_qty == 0
    assert snapshot.gross_notional == 200
    assert snapshot.utilization == 0.5
