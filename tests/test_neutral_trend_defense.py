from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.models import GridOrder, OrderSide, OrderStatus
from strategy.inventory import InventoryAction, InventoryLevel, InventoryManager
from strategy.regime import RegimeConfig, RegimeEngine


NOW = datetime.now(timezone.utc)


def _range_klines(count: int = 90) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for index in range(count):
        close = 100.0 + (0.08 if index % 2 == 0 else -0.08)
        rows.append({"high": close + 0.06, "low": close - 0.06, "close": close})
    return rows


def _trend_klines(count: int = 90) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for index in range(count):
        close = 100.0 + index * 0.08
        rows.append({"high": close + 0.03, "low": close - 0.03, "close": close})
    return rows


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


def test_range_remains_eligible_after_trend_guard() -> None:
    decision = RegimeEngine().evaluate(
        "BTCUSDT",
        _range_klines(),
        spread_pct=0.0001,
        depth_usdt=20_000,
        expected_step_pct=0.003,
        cost_floor_pct=0.001,
    )

    assert decision.allowed is True
    assert decision.state == "QUIET_RANGE"


def test_entry_trend_guard_is_a_hard_block() -> None:
    decision = RegimeEngine(RegimeConfig(enter_threshold=0, stay_threshold=0)).evaluate(
        "BTCUSDT",
        _trend_klines(),
        spread_pct=0.0001,
        depth_usdt=20_000,
        expected_step_pct=0.003,
        cost_floor_pct=0.001,
    )

    assert decision.allowed is False
    assert decision.verdict == "BLOCKED_TREND"
    assert decision.state == "TREND_UP"
    assert decision.hard_blocks


def test_running_trend_guard_is_soft_for_defensive_transition() -> None:
    decision = RegimeEngine(RegimeConfig(enter_threshold=0, stay_threshold=0)).evaluate(
        "BTCUSDT",
        _trend_klines(),
        spread_pct=0.0001,
        depth_usdt=20_000,
        expected_step_pct=0.003,
        cost_floor_pct=0.001,
        running=True,
    )

    assert decision.allowed is False
    assert decision.verdict == "BLOCKED_SCORE"
    assert decision.state == "TREND_UP"
    assert decision.hard_blocks == ()
    assert any("软违约" in reason for reason in decision.reasons)


def test_wrong_way_inventory_is_suppressed_even_below_caution() -> None:
    decision = InventoryManager().evaluate(
        [_filled("open", OrderSide.BUY, 100, 0.1)],
        mark_price=100,
        max_inventory_notional=200,
        trend_direction=-1,
    )

    assert decision.action == InventoryAction.SUPPRESS_LONG
    assert decision.snapshot.level == InventoryLevel.NORMAL


def test_wrong_way_inventory_reduces_before_high_utilization() -> None:
    decision = InventoryManager().evaluate(
        [_filled("open", OrderSide.BUY, 100, 0.5)],
        mark_price=100,
        max_inventory_notional=200,
        trend_direction=-1,
    )

    assert decision.action == InventoryAction.REDUCE
    assert decision.snapshot.utilization == pytest.approx(0.25)
    assert decision.snapshot.level == InventoryLevel.NORMAL


def test_wrong_way_loss_can_close_before_critical_utilization() -> None:
    decision = InventoryManager().evaluate(
        [_filled("open", OrderSide.SELL, 100, 1.2)],
        mark_price=130,
        max_inventory_notional=200,
        trend_direction=1,
    )

    assert decision.snapshot.level == InventoryLevel.HIGH
    assert decision.snapshot.utilization < 0.80
    assert decision.snapshot.risk_score >= 65
    assert decision.action == InventoryAction.CLOSE
