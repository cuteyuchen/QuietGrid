from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from math import isfinite
from typing import Any

from core.models import GridParams, OrderSide


class LookAheadViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class BacktestConfig:
    capital: float = 200.0
    leverage: float = 10.0
    maker_fee_rate: float = 0.0
    stop_on_range_break: bool = True
    stop_on_stop_loss: bool = True
    fill_model: str = "LEGACY"
    min_tick_size: float = 0.0
    max_fills_per_bar: int = 0
    maker_fill_probability: float = 1.0
    fill_probability_seed: int = 17
    taker_fee_rate: float = 0.0
    stop_slippage_bps: float = 0.0
    funding_rate_per_bar: float = 0.0
    force_close_at_end: bool = False


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
    gross_inventory_notional: float = 0.0
    inventory_utilization: float = 0.0


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
    funding_paid: float = 0.0
    stop_exit_cost: float = 0.0
    stop_exit_pnl: float = 0.0
    attempted_fill_count: int = 0
    rejected_fill_count: int = 0
    pair_completion_count: int = 0
    max_inventory_utilization: float = 0.0


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


def backtest_config_from_mapping(raw: dict[str, Any]) -> BacktestConfig:
    trading = raw.get("trading", {})
    backtest = raw.get("backtest", {})
    return BacktestConfig(
        capital=float(trading.get("capital_per_symbol", 200)),
        leverage=float(trading.get("leverage", 1)),
        maker_fee_rate=float(trading.get("max_maker_fee_rate", 0.0)),
        stop_on_range_break=bool(backtest.get("stop_on_range_break", True)),
        stop_on_stop_loss=bool(backtest.get("stop_on_stop_loss", True)),
        fill_model=str(backtest.get("fill_model", "L0_CONSERVATIVE")),
        min_tick_size=float(backtest.get("min_tick_size", 0.0)),
        max_fills_per_bar=int(backtest.get("max_fills_per_bar", 2)),
        maker_fill_probability=float(backtest.get("maker_fill_probability", 0.65)),
        fill_probability_seed=int(backtest.get("fill_probability_seed", 17)),
        taker_fee_rate=float(backtest.get("taker_fee_rate", 0.0005)),
        stop_slippage_bps=float(backtest.get("stop_slippage_bps", 10.0)),
        funding_rate_per_bar=float(backtest.get("funding_rate_per_bar", 0.0)),
        force_close_at_end=bool(backtest.get("force_close_at_end", False)),
    )


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
    max_inventory_utilization = 0.0
    funding_paid = 0.0
    stop_exit_cost = 0.0
    stop_exit_pnl = 0.0
    attempted_fill_count = 0
    pair_completion_count = 0
    stopped_reason: str | None = None
    stopped_at_index: int | None = None
    stopped_at_price: float | None = None
    last_price = current_price
    previous_event_time: datetime | None = None
    conservative = config.fill_model.upper() == "L0_CONSERVATIVE"
    min_tick_size = (
        config.min_tick_size
        if config.min_tick_size > 0
        else max(min(params.grid_prices) * 1e-8, 1e-12)
    )

    for bar_index, row in enumerate(klines):
        previous_event_time = _validate_bar_time(row, previous_event_time)
        high, low, close = _bar_prices(row)
        last_price = close

        risk_reason, risk_price = _risk_stop(params, high, low, config)
        if risk_reason is not None:
            exit_price = float(risk_price)
            if conservative and config.stop_slippage_bps > 0:
                slippage = config.stop_slippage_bps / 10_000
                exit_price *= (
                    1 - slippage
                    if exit_price <= params.center
                    else 1 + slippage
                )
            if conservative:
                stop_exit_pnl, stop_exit_cost = _close_all_lots(
                    long_lots,
                    short_lots,
                    exit_price,
                    config.taker_fee_rate,
                )
                gross_grid_pnl += stop_exit_pnl
                fees_paid += stop_exit_cost
            stopped_reason = risk_reason
            stopped_at_index = bar_index
            stopped_at_price = exit_price
            last_price = exit_price
            (
                _equity,
                max_equity,
                max_drawdown,
                max_inventory_utilization,
            ) = _append_equity_point(
                equity_curve,
                bar_index,
                _bar_timestamp(row),
                last_price,
                gross_grid_pnl - fees_paid - funding_paid,
                long_lots,
                short_lots,
                max_equity,
                max_drawdown,
                config.capital * config.leverage,
                max_inventory_utilization,
            )
            break

        touched = [
            order
            for order in list(open_orders)
            if _order_touched(
                order,
                high,
                low,
                min_tick_size if conservative else 0.0,
            )
        ]
        attempted_fill_count += len(touched)
        if conservative:
            touched = [
                order
                for order in touched
                if _deterministic_fill_allowed(
                    params.symbol,
                    bar_index,
                    order,
                    config.maker_fill_probability,
                    config.fill_probability_seed,
                )
            ]
            touched.sort(key=_worst_case_order_key)
            if config.max_fills_per_bar > 0:
                touched = touched[: config.max_fills_per_bar]
        for order in touched:
            if order not in open_orders:
                continue
            open_orders.remove(order)
            fee = order.price * order.qty * config.maker_fee_rate
            fees_paid += fee
            grid_pnl = _grid_pnl(order)
            if grid_pnl is not None:
                gross_grid_pnl += grid_pnl
                pair_completion_count += 1
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
        if conservative and config.funding_rate_per_bar != 0:
            funding_paid += _funding_cost(
                long_lots,
                short_lots,
                close,
                config.funding_rate_per_bar,
            )
        (
            _equity,
            max_equity,
            max_drawdown,
            max_inventory_utilization,
        ) = _append_equity_point(
            equity_curve,
            bar_index,
            _bar_timestamp(row),
            close,
            gross_grid_pnl - fees_paid - funding_paid,
            long_lots,
            short_lots,
            max_equity,
            max_drawdown,
            config.capital * config.leverage,
            max_inventory_utilization,
        )

    if config.force_close_at_end and stopped_reason is None:
        stop_exit_pnl, stop_exit_cost = _close_all_lots_at_market(
            long_lots,
            short_lots,
            last_price,
            config.taker_fee_rate,
            config.stop_slippage_bps,
        )
        gross_grid_pnl += stop_exit_pnl
        fees_paid += stop_exit_cost
        stopped_reason = "window_force_close"
        stopped_at_index = len(klines)
        stopped_at_price = last_price
        open_orders.clear()
        (
            _equity,
            max_equity,
            max_drawdown,
            max_inventory_utilization,
        ) = _append_equity_point(
            equity_curve,
            len(klines),
            _bar_timestamp(klines[-1]),
            last_price,
            gross_grid_pnl - fees_paid - funding_paid,
            long_lots,
            short_lots,
            max_equity,
            max_drawdown,
            config.capital * config.leverage,
            max_inventory_utilization,
        )

    unrealized_pnl = _unrealized_pnl(long_lots, short_lots, last_price)
    realized_pnl = gross_grid_pnl - fees_paid - funding_paid
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
        funding_paid=funding_paid,
        stop_exit_cost=stop_exit_cost,
        stop_exit_pnl=stop_exit_pnl,
        attempted_fill_count=attempted_fill_count,
        rejected_fill_count=max(0, attempted_fill_count - len(fills)),
        pair_completion_count=pair_completion_count,
        max_inventory_utilization=max_inventory_utilization,
    )


def _validate_backtest_config(config: BacktestConfig) -> None:
    _positive_finite(config.capital, "capital")
    _positive_finite(config.leverage, "leverage")
    maker_fee_rate = _finite_float(config.maker_fee_rate, "maker_fee_rate")
    if maker_fee_rate < 0:
        raise ValueError("maker_fee_rate不能为负。")
    fill_model = config.fill_model.strip().upper()
    if fill_model not in {"LEGACY", "L0_CONSERVATIVE"}:
        raise ValueError(f"不支持的fill_model: {config.fill_model}")
    if config.min_tick_size < 0:
        raise ValueError("min_tick_size不能为负。")
    if config.max_fills_per_bar < 0:
        raise ValueError("max_fills_per_bar不能为负。")
    if not 0 <= config.maker_fill_probability <= 1:
        raise ValueError("maker_fill_probability必须在0到1之间。")
    if config.taker_fee_rate < 0:
        raise ValueError("taker_fee_rate不能为负。")
    if config.stop_slippage_bps < 0:
        raise ValueError("stop_slippage_bps不能为负。")
    _finite_float(config.funding_rate_per_bar, "funding_rate_per_bar")


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


def _order_touched(
    order: _BacktestOrder,
    high: float,
    low: float,
    min_tick_size: float = 0.0,
) -> bool:
    if min_tick_size <= 0:
        return low <= order.price <= high
    if order.side == OrderSide.BUY:
        return low <= order.price - min_tick_size
    return high >= order.price + min_tick_size


def _deterministic_fill_allowed(
    symbol: str,
    bar_index: int,
    order: _BacktestOrder,
    probability: float,
    seed: int,
) -> bool:
    if probability >= 1:
        return True
    if probability <= 0:
        return False
    digest = blake2b(
        f"{seed}|{symbol}|{bar_index}|{order.grid_index}|{order.side.value}".encode(),
        digest_size=8,
    ).digest()
    sample = int.from_bytes(digest, "big") / float(2**64 - 1)
    return sample < probability


def _worst_case_order_key(order: _BacktestOrder) -> tuple[int, float]:
    opening_rank = 0 if order.entry_price is None else 1
    price_rank = -order.price if order.side == OrderSide.BUY else order.price
    return opening_rank, price_rank


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


def _close_all_lots(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    exit_price: float,
    taker_fee_rate: float,
) -> tuple[float, float]:
    pnl = sum((exit_price - lot.entry_price) * lot.qty for lot in long_lots)
    pnl += sum((lot.entry_price - exit_price) * lot.qty for lot in short_lots)
    closed_qty = sum(lot.qty for lot in long_lots) + sum(lot.qty for lot in short_lots)
    fee = exit_price * closed_qty * taker_fee_rate
    long_lots.clear()
    short_lots.clear()
    return pnl, fee


def _close_all_lots_at_market(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
    taker_fee_rate: float,
    slippage_bps: float,
) -> tuple[float, float]:
    slippage = max(0.0, slippage_bps) / 10_000
    long_exit = mark_price * (1 - slippage)
    short_exit = mark_price * (1 + slippage)
    pnl = sum((long_exit - lot.entry_price) * lot.qty for lot in long_lots)
    pnl += sum((lot.entry_price - short_exit) * lot.qty for lot in short_lots)
    long_qty = sum(lot.qty for lot in long_lots)
    short_qty = sum(lot.qty for lot in short_lots)
    fee = (
        long_exit * long_qty + short_exit * short_qty
    ) * taker_fee_rate
    long_lots.clear()
    short_lots.clear()
    return pnl, fee


def _funding_cost(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
    funding_rate: float,
) -> float:
    long_notional = sum(lot.qty for lot in long_lots) * mark_price
    short_notional = sum(lot.qty for lot in short_lots) * mark_price
    return (long_notional - short_notional) * funding_rate


def _validate_bar_time(
    row: dict[str, Any],
    previous_event_time: datetime | None,
) -> datetime | None:
    event_time = _first_parsed_time(
        row,
        "event_time",
        "timestamp",
        "close_time",
        "open_time",
        "time",
    )
    available_time = _first_parsed_time(row, "available_time", "as_of_time")
    decision_time = _first_parsed_time(row, "decision_time") or event_time
    if available_time is not None and decision_time is not None and available_time > decision_time:
        raise LookAheadViolation(
            "回测读取了决策时点之后才可获得的数据。"
        )
    if (
        previous_event_time is not None
        and event_time is not None
        and event_time < previous_event_time
    ):
        raise LookAheadViolation("回测事件时间倒序，无法保证事件驱动语义。")
    return event_time or previous_event_time


def _first_parsed_time(
    row: dict[str, Any],
    *keys: str,
) -> datetime | None:
    for key in keys:
        raw = row.get(key)
        if raw in (None, ""):
            continue
        parsed = _parse_time(raw)
        if parsed is not None:
            return parsed
    return None


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    risk_capacity: float,
    max_inventory_utilization: float,
) -> tuple[float, float, float, float]:
    unrealized_pnl = _unrealized_pnl(long_lots, short_lots, close)
    equity = realized_pnl + unrealized_pnl
    max_equity = max(max_equity, equity)
    drawdown = max(0.0, max_equity - equity)
    max_drawdown = max(max_drawdown, drawdown)
    gross_inventory_notional = (
        sum(lot.qty for lot in long_lots)
        + sum(lot.qty for lot in short_lots)
    ) * close
    inventory_utilization = (
        gross_inventory_notional / risk_capacity
        if risk_capacity > 0
        else 0.0
    )
    max_inventory_utilization = max(
        max_inventory_utilization,
        inventory_utilization,
    )
    equity_curve.append(
        BacktestEquityPoint(
            bar_index=bar_index,
            equity=equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            drawdown=drawdown,
            close=close,
            timestamp=timestamp,
            gross_inventory_notional=gross_inventory_notional,
            inventory_utilization=inventory_utilization,
        )
    )
    return equity, max_equity, max_drawdown, max_inventory_utilization


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
