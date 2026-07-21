from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Any

from core.models import GridOrder, OrderIntent, OrderStatus


class InventoryLevel(Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class InventoryAction(Enum):
    ALLOW = "ALLOW"
    SUPPRESS_LONG = "SUPPRESS_LONG"
    SUPPRESS_SHORT = "SUPPRESS_SHORT"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class InventoryConfig:
    caution_utilization: float = 0.40
    high_utilization: float = 0.60
    critical_utilization: float = 0.80
    suppress_same_side_orders: bool = True
    passive_reduce_first: bool = True


@dataclass(frozen=True)
class InventoryLot:
    side: str
    entry_price: float
    qty: float
    entry_grid_index: int
    target_exit_price: float | None
    opened_at: Any = None


@dataclass(frozen=True)
class InventorySnapshot:
    net_qty: float
    net_notional: float
    gross_notional: float
    avg_entry_price: float | None
    unrealized_pnl: float
    utilization: float
    risk_score: float
    level: InventoryLevel
    unpaired_lots: tuple[InventoryLot, ...]


@dataclass(frozen=True)
class InventoryDecision:
    action: InventoryAction
    reason: str
    snapshot: InventorySnapshot


class InventoryManager:
    def __init__(self, config: InventoryConfig | None = None) -> None:
        self.config = config or InventoryConfig()
        _validate_config(self.config)

    def evaluate(
        self,
        orders: list[GridOrder],
        *,
        mark_price: float,
        max_inventory_notional: float,
        baseline_inventory_notional: float = 0.0,
        trend_direction: int = 0,
        minutes_to_close: float | None = None,
    ) -> InventoryDecision:
        snapshot = self.snapshot(
            orders,
            mark_price=mark_price,
            max_inventory_notional=max_inventory_notional,
            baseline_inventory_notional=baseline_inventory_notional,
            trend_direction=trend_direction,
            minutes_to_close=minutes_to_close,
        )
        if snapshot.level == InventoryLevel.CRITICAL:
            return InventoryDecision(InventoryAction.CLOSE, "库存利用率达到 CRITICAL。", snapshot)
        if snapshot.level == InventoryLevel.HIGH:
            return InventoryDecision(InventoryAction.REDUCE, "库存利用率达到 HIGH，只允许减仓。", snapshot)
        if snapshot.level == InventoryLevel.CAUTION and self.config.suppress_same_side_orders:
            action = InventoryAction.SUPPRESS_LONG if snapshot.net_qty > 0 else InventoryAction.SUPPRESS_SHORT
            if abs(snapshot.net_qty) <= 1e-12:
                action = InventoryAction.REDUCE
            return InventoryDecision(action, "库存利用率达到 CAUTION，抑制增加同向库存的订单。", snapshot)
        return InventoryDecision(InventoryAction.ALLOW, "库存风险正常。", snapshot)

    def snapshot(
        self,
        orders: list[GridOrder],
        *,
        mark_price: float,
        max_inventory_notional: float,
        baseline_inventory_notional: float = 0.0,
        trend_direction: int = 0,
        minutes_to_close: float | None = None,
    ) -> InventorySnapshot:
        mark = _positive(mark_price, "mark_price")
        cap = _positive(max_inventory_notional, "max_inventory_notional")
        baseline = _non_negative(baseline_inventory_notional, "baseline_inventory_notional")
        long_lots: list[InventoryLot] = []
        short_lots: list[InventoryLot] = []
        filled = sorted(
            (order for order in orders if order.status == OrderStatus.FILLED),
            key=lambda order: order.filled_at or order.created_at,
        )
        for order in filled:
            fill_price = _positive(order.fill_price or order.price, "fill_price")
            qty = _positive(order.qty, "qty")
            intent = order.order_intent
            if intent == OrderIntent.OPEN and order.entry_price is not None:
                intent = OrderIntent.REDUCE
            position_side = str(order.position_side or "").upper()
            if not position_side:
                if intent == OrderIntent.REDUCE:
                    position_side = "SHORT" if order.side.value == "BUY" else "LONG"
                else:
                    position_side = "LONG" if order.side.value == "BUY" else "SHORT"
            if intent in {OrderIntent.OPEN, OrderIntent.SEED}:
                lot = InventoryLot(
                    side=position_side,
                    entry_price=fill_price,
                    qty=qty,
                    entry_grid_index=order.grid_index,
                    target_exit_price=None,
                    opened_at=order.filled_at,
                )
                (long_lots if position_side == "LONG" else short_lots).append(lot)
                continue
            if intent == OrderIntent.REDUCE:
                target = long_lots if position_side == "LONG" else short_lots
                _consume_lots(target, qty, float(order.entry_price or fill_price))

        long_qty = sum(lot.qty for lot in long_lots)
        short_qty = sum(lot.qty for lot in short_lots)
        net_qty = long_qty - short_qty
        net_notional = net_qty * mark
        gross_notional = (long_qty + short_qty) * mark
        directional_notional = max(abs(net_notional), gross_notional * 0.5)
        # Directional grids intentionally seed inventory before placing their
        # reduce-side orders.  Treat that planned seed as consumed baseline
        # rather than immediately classifying the fresh session as HIGH.
        baseline = min(baseline, cap)
        incremental_notional = max(0.0, directional_notional - baseline)
        incremental_capacity = cap - baseline
        utilization = (
            incremental_notional / incremental_capacity
            if incremental_capacity > 1e-12
            else (1.0 if incremental_notional > 1e-12 else 0.0)
        )
        if net_qty > 0:
            avg_entry = _weighted_average(long_lots)
        elif net_qty < 0:
            avg_entry = _weighted_average(short_lots)
        else:
            avg_entry = None
        unrealized = sum((mark - lot.entry_price) * lot.qty for lot in long_lots)
        unrealized += sum((lot.entry_price - mark) * lot.qty for lot in short_lots)
        unrealized_loss_score = _clamp(-unrealized / cap)
        trend_alignment = 0.0
        if (net_qty > 0 and trend_direction < 0) or (net_qty < 0 and trend_direction > 0):
            trend_alignment = 1.0
        time_score = 0.0
        if minutes_to_close is not None:
            time_score = _clamp(1.0 - max(0.0, float(minutes_to_close)) / 120.0)
        risk_score = 100.0 * (
            0.45 * _clamp(utilization / self.config.critical_utilization)
            + 0.25 * unrealized_loss_score
            + 0.20 * trend_alignment
            + 0.10 * time_score
        )
        level = _level(utilization, self.config)
        return InventorySnapshot(
            net_qty=net_qty,
            net_notional=net_notional,
            gross_notional=gross_notional,
            avg_entry_price=avg_entry,
            unrealized_pnl=unrealized,
            utilization=utilization,
            risk_score=risk_score,
            level=level,
            unpaired_lots=tuple(long_lots + short_lots),
        )


def _consume_lots(lots: list[InventoryLot], qty: float, preferred_entry: float) -> None:
    remaining = qty
    ordered = sorted(lots, key=lambda lot: abs(lot.entry_price - preferred_entry))
    for lot in ordered:
        if remaining <= 1e-12:
            break
        consumed = min(lot.qty, remaining)
        remaining -= consumed
        lots.remove(lot)
        leftover = lot.qty - consumed
        if leftover > 1e-12:
            lots.append(
                InventoryLot(
                    side=lot.side,
                    entry_price=lot.entry_price,
                    qty=leftover,
                    entry_grid_index=lot.entry_grid_index,
                    target_exit_price=lot.target_exit_price,
                    opened_at=lot.opened_at,
                )
            )


def _weighted_average(lots: list[InventoryLot]) -> float | None:
    qty = sum(lot.qty for lot in lots)
    if qty <= 1e-12:
        return None
    return sum(lot.entry_price * lot.qty for lot in lots) / qty


def _level(utilization: float, config: InventoryConfig) -> InventoryLevel:
    if utilization >= config.critical_utilization:
        return InventoryLevel.CRITICAL
    if utilization >= config.high_utilization:
        return InventoryLevel.HIGH
    if utilization >= config.caution_utilization:
        return InventoryLevel.CAUTION
    return InventoryLevel.NORMAL


def _validate_config(config: InventoryConfig) -> None:
    values = (
        config.caution_utilization,
        config.high_utilization,
        config.critical_utilization,
    )
    if not 0 < values[0] < values[1] < values[2] <= 1:
        raise ValueError("库存风险阈值必须满足 0 < caution < high < critical <= 1。")


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(upper, max(lower, value))


def _positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须为正的有限数。") from exc
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{label} 必须为正的有限数。")
    return number


def _non_negative(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须为非负有限数。") from exc
    if not isfinite(number) or number < 0:
        raise ValueError(f"{label} 必须为非负有限数。")
    return number
