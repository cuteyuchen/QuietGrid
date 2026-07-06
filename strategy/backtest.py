from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from core.models import GridParams, OrderSide


@dataclass(frozen=True)
class BacktestConfig:
    capital: float = 200.0
    leverage: float = 10.0
    maker_fee_rate: float = 0.0
    stop_on_range_break: bool = True
    stop_on_stop_loss: bool = True


@dataclass(frozen=True)
class BacktestFill:
    symbol: str
    side: str
    grid_index: int
    price: float
    qty: float
    fee: float
    grid_pnl: float | None
    realized_pnl_after: float
    bar_index: int
    timestamp: Any = None


@dataclass(frozen=True)
class BacktestEquityPoint:
    bar_index: int
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown: float
    close: float
    timestamp: Any = None


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    fills: list[BacktestFill]
    equity_curve: list[BacktestEquityPoint]
    gross_grid_pnl: float
    fees_paid: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    max_equity: float
    max_drawdown: float
    open_order_count: int
    net_position_qty: float
    stopped_reason: str | None
    stopped_at_index: int | None
    stopped_at_price: float | None
    last_price: float


@dataclass
class _BacktestOrder:
    grid_index: int
    side: OrderSide
    price: float
    qty: float
    entry_price: float | None = None


@dataclass
class _PositionLot:
    entry_price: float
    qty: float


def run_grid_backtest(
    params: GridParams,
    klines: list[dict[str, Any]],
    current_price: float,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    config = config or BacktestConfig()
    _validate_backtest_config(config)
    _validate_grid_params(params)
    current_price = _positive_finite(current_price, "current_price")
    if not klines:
        raise ValueError("回测K线不能为空。")

    qty = config.capital * config.leverage / current_price / params.grid_num
    qty = _positive_finite(qty, "qty")
    open_orders = _initial_orders(params, current_price, qty)
    if not open_orders:
        raise ValueError("回测初始网格没有可挂订单。")

    fills: list[BacktestFill] = []
    long_lots: list[_PositionLot] = []
    short_lots: list[_PositionLot] = []
    gross_grid_pnl = 0.0
    fees_paid = 0.0
    equity_curve: list[BacktestEquityPoint] = []
    max_equity = 0.0
    max_drawdown = 0.0
    stopped_reason: str | None = None
    stopped_at_index: int | None = None
    stopped_at_price: float | None = None
    last_price = current_price

    for bar_index, row in enumerate(klines):
        high, low, close = _bar_prices(row)
        last_price = close

        risk_reason, risk_price = _risk_stop(params, high, low, config)
        if risk_reason is not None:
            stopped_reason = risk_reason
            stopped_at_index = bar_index
            stopped_at_price = risk_price
            last_price = risk_price
            equity, max_equity, max_drawdown = _append_equity_point(
                equity_curve,
                bar_index,
                _bar_timestamp(row),
                last_price,
                gross_grid_pnl - fees_paid,
                long_lots,
                short_lots,
                max_equity,
                max_drawdown,
            )
            break

        touched = [order for order in list(open_orders) if _order_touched(order, high, low)]
        for order in touched:
            if order not in open_orders:
                continue
            open_orders.remove(order)
            fee = order.price * order.qty * config.maker_fee_rate
            fees_paid += fee
            grid_pnl = _grid_pnl(order)
            if grid_pnl is not None:
                gross_grid_pnl += grid_pnl
            _apply_position_fill(order, long_lots, short_lots)
            realized_pnl = gross_grid_pnl - fees_paid
            fills.append(
                BacktestFill(
                    symbol=params.symbol,
                    side=order.side.value,
                    grid_index=order.grid_index,
                    price=order.price,
                    qty=order.qty,
                    fee=fee,
                    grid_pnl=grid_pnl,
                    realized_pnl_after=realized_pnl,
                    bar_index=bar_index,
                    timestamp=_bar_timestamp(row),
                )
            )
            next_order = _next_order(params, order)
            if next_order is not None:
                open_orders.append(next_order)
        _equity, max_equity, max_drawdown = _append_equity_point(
            equity_curve,
            bar_index,
            _bar_timestamp(row),
            close,
            gross_grid_pnl - fees_paid,
            long_lots,
            short_lots,
            max_equity,
            max_drawdown,
        )

    unrealized_pnl = _unrealized_pnl(long_lots, short_lots, last_price)
    realized_pnl = gross_grid_pnl - fees_paid
    return BacktestResult(
        symbol=params.symbol,
        fills=fills,
        equity_curve=equity_curve,
        gross_grid_pnl=gross_grid_pnl,
        fees_paid=fees_paid,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        total_pnl=realized_pnl + unrealized_pnl,
        max_equity=max_equity,
        max_drawdown=max_drawdown,
        open_order_count=len(open_orders),
        net_position_qty=sum(lot.qty for lot in long_lots) - sum(lot.qty for lot in short_lots),
        stopped_reason=stopped_reason,
        stopped_at_index=stopped_at_index,
        stopped_at_price=stopped_at_price,
        last_price=last_price,
    )


def _validate_backtest_config(config: BacktestConfig) -> None:
    _positive_finite(config.capital, "capital")
    _positive_finite(config.leverage, "leverage")
    maker_fee_rate = _finite_float(config.maker_fee_rate, "maker_fee_rate")
    if maker_fee_rate < 0:
        raise ValueError("maker_fee_rate不能为负。")


def _validate_grid_params(params: GridParams) -> None:
    _positive_finite(params.lower, "lower")
    _positive_finite(params.upper, "upper")
    if params.upper <= params.lower:
        raise ValueError("网格区间非法。")
    if params.grid_num < 1:
        raise ValueError("grid_num必须为正。")
    if len(params.grid_prices) != params.grid_num + 1:
        raise ValueError("grid_prices长度必须等于grid_num + 1。")
    for price in params.grid_prices:
        _positive_finite(price, "grid_price")
    _positive_finite(params.stop_loss_price, "stop_loss_price")


def _initial_orders(params: GridParams, current_price: float, qty: float) -> list[_BacktestOrder]:
    orders: list[_BacktestOrder] = []
    for index, price in enumerate(params.grid_prices):
        if price == current_price:
            continue
        side = OrderSide.BUY if price < current_price else OrderSide.SELL
        orders.append(_BacktestOrder(index, side, price, qty))
    return orders


def _bar_prices(row: dict[str, Any]) -> tuple[float, float, float]:
    high = _positive_finite(row.get("high"), "high")
    low = _positive_finite(row.get("low"), "low")
    close = _positive_finite(row.get("close"), "close")
    if high < low or close < low or close > high:
        raise ValueError("K线价格关系非法。")
    return high, low, close


def _bar_timestamp(row: dict[str, Any]) -> Any:
    for key in ("timestamp", "close_time", "open_time", "time"):
        if key in row:
            return row[key]
    return None


def _risk_stop(
    params: GridParams,
    high: float,
    low: float,
    config: BacktestConfig,
) -> tuple[str | None, float | None]:
    if config.stop_on_stop_loss and low <= params.stop_loss_price:
        return "stop_loss", params.stop_loss_price
    if config.stop_on_range_break:
        if low < params.lower:
            return "range_break", low
        if high > params.upper:
            return "range_break", high
    return None, None


def _order_touched(order: _BacktestOrder, high: float, low: float) -> bool:
    return low <= order.price <= high


def _grid_pnl(order: _BacktestOrder) -> float | None:
    if order.entry_price is None:
        return None
    if order.side == OrderSide.SELL:
        return (order.price - order.entry_price) * order.qty
    return (order.entry_price - order.price) * order.qty


def _apply_position_fill(
    order: _BacktestOrder,
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
) -> None:
    if order.entry_price is None:
        lots = long_lots if order.side == OrderSide.BUY else short_lots
        lots.append(_PositionLot(order.price, order.qty))
        return
    lots = short_lots if order.side == OrderSide.BUY else long_lots
    _reduce_lot(lots, order.entry_price, order.qty)


def _reduce_lot(lots: list[_PositionLot], entry_price: float, qty: float) -> None:
    remaining = qty
    for lot in list(lots):
        if abs(lot.entry_price - entry_price) > 1e-12:
            continue
        consumed = min(lot.qty, remaining)
        lot.qty -= consumed
        remaining -= consumed
        if lot.qty <= 1e-12:
            lots.remove(lot)
        if remaining <= 1e-12:
            return
    for lot in list(lots):
        consumed = min(lot.qty, remaining)
        lot.qty -= consumed
        remaining -= consumed
        if lot.qty <= 1e-12:
            lots.remove(lot)
        if remaining <= 1e-12:
            return


def _next_order(params: GridParams, order: _BacktestOrder) -> _BacktestOrder | None:
    if order.side == OrderSide.BUY:
        next_index = order.grid_index + 1
        next_side = OrderSide.SELL
    else:
        next_index = order.grid_index - 1
        next_side = OrderSide.BUY
    if next_index < 0 or next_index >= len(params.grid_prices):
        return None
    entry_price = order.entry_price if order.entry_price is not None else order.price
    return _BacktestOrder(next_index, next_side, params.grid_prices[next_index], order.qty, entry_price)


def _unrealized_pnl(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    last_price: float,
) -> float:
    long_pnl = sum((last_price - lot.entry_price) * lot.qty for lot in long_lots)
    short_pnl = sum((lot.entry_price - last_price) * lot.qty for lot in short_lots)
    return long_pnl + short_pnl


def _append_equity_point(
    equity_curve: list[BacktestEquityPoint],
    bar_index: int,
    timestamp: Any,
    close: float,
    realized_pnl: float,
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    max_equity: float,
    max_drawdown: float,
) -> tuple[float, float, float]:
    unrealized_pnl = _unrealized_pnl(long_lots, short_lots, close)
    equity = realized_pnl + unrealized_pnl
    max_equity = max(max_equity, equity)
    drawdown = max(0.0, max_equity - equity)
    max_drawdown = max(max_drawdown, drawdown)
    equity_curve.append(
        BacktestEquityPoint(
            bar_index=bar_index,
            equity=equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            drawdown=drawdown,
            close=close,
            timestamp=timestamp,
        )
    )
    return equity, max_equity, max_drawdown


def _positive_finite(raw_value: Any, label: str) -> float:
    value = _finite_float(raw_value, label)
    if value <= 0:
        raise ValueError(f"{label}必须为正。")
    return value


def _finite_float(raw_value: Any, label: str) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须为有限数。") from exc
    if not isfinite(value):
        raise ValueError(f"{label}必须为有限数。")
    return value
