from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

from core.models import GridParams, OrderSide


@dataclass(frozen=True)
class InitialGridOrderPlan:
    grid_index: int
    side: OrderSide
    price: float
    qty: float


def build_initial_grid_order_plan(
    params: GridParams,
    current_price: float,
    *,
    capital: float,
    leverage: float,
    tick_size: float,
    quantity_step_size: float,
) -> tuple[InitialGridOrderPlan, ...]:
    """Build the exact initial order geometry shared by live and backtest paths."""
    specs: list[tuple[int, OrderSide, float]] = []
    for index, raw_price in enumerate(params.grid_prices):
        if raw_price == current_price:
            continue
        side = OrderSide.BUY if raw_price < current_price else OrderSide.SELL
        price = round_down_to_step(raw_price, tick_size)
        if price <= 0:
            raise ValueError("网格价格按交易所精度取整后必须大于 0。")
        specs.append((index, side, price))
    if not specs:
        raise ValueError("初始网格没有可挂订单。")

    if not params.qty_weights:
        qty = equal_order_quantity(
            capital,
            leverage,
            current_price,
            params.grid_num,
            quantity_step_size,
        )
        quantities = {index: qty for index, _side, _price in specs}
    else:
        if len(params.qty_weights) != len(params.grid_prices):
            raise ValueError("qty_weights 长度必须等于 grid_prices 长度。")
        selected_weight = sum(params.qty_weights[index] for index, _side, _price in specs)
        if selected_weight <= 0:
            raise ValueError("qty_weights 合计必须为正数。")
        total_notional = capital * leverage
        quantities = {
            index: weighted_order_quantity(
                total_notional,
                params.qty_weights[index],
                selected_weight,
                price,
                quantity_step_size,
            )
            for index, _side, price in specs
        }

    return tuple(
        InitialGridOrderPlan(index, side, price, quantities[index])
        for index, side, price in specs
    )


def round_down_to_step(value: float, step_size: float) -> float:
    if step_size <= 0:
        return value
    value_decimal = Decimal(str(value))
    step_decimal = Decimal(str(step_size))
    units = (value_decimal / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_decimal)


def equal_order_quantity(
    capital: float,
    leverage: float,
    current_price: float,
    grid_num: int,
    step_size: float,
) -> float:
    raw = (
        Decimal(str(capital))
        * Decimal(str(leverage))
        / Decimal(str(current_price))
        / Decimal(str(grid_num))
    )
    return _round_decimal_down_to_step(raw, step_size)


def weighted_order_quantity(
    total_notional: float,
    weight: float,
    selected_weight: float,
    price: float,
    step_size: float,
) -> float:
    raw = (
        Decimal(str(total_notional))
        * Decimal(str(weight))
        / Decimal(str(selected_weight))
        / Decimal(str(price))
    )
    return _round_decimal_down_to_step(raw, step_size)


def _round_decimal_down_to_step(value: Decimal, step_size: float) -> float:
    if step_size <= 0:
        return float(value)
    step_decimal = Decimal(str(step_size))
    units = (value / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_decimal)
