from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from math import isfinite
from typing import Any

from core.models import GridDirectionMode, GridParams, OrderIntent, OrderSide
from data_sources.models import FundingEvent


class LookAheadViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class BacktestConfig:
    capital: float = 200.0
    leverage: float = 10.0
    maker_fee_rate: float = 0.0
    stop_on_range_break: bool = False
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
    direction_mode: GridDirectionMode = GridDirectionMode.NEUTRAL
    seed_slippage_bps: float = 0.0
    retention_score_threshold: float = 65.0
    retention_soft_breach_limit: int = 3


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
    position_side: str = ""
    order_intent: str = "OPEN"


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
    direction_mode: str = "NEUTRAL"
    seed_qty: float = 0.0
    seed_entry_price: float | None = None
    seed_fee: float = 0.0
    defensive_entry_count: int = 0


@dataclass
class _BacktestOrder:
    grid_index: int
    side: OrderSide
    price: float
    qty: float
    entry_price: float | None = None
    position_side: str = ""
    order_intent: OrderIntent = OrderIntent.OPEN


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
        direction_mode=GridDirectionMode(
            str(trading.get("direction_mode", "NEUTRAL")).upper()
        ),
        seed_slippage_bps=float(backtest.get("seed_slippage_bps", 0.0)),
        retention_score_threshold=float(backtest.get("retention_score_threshold", 65.0)),
        retention_soft_breach_limit=int(backtest.get("retention_soft_breach_limit", 3)),
    )


def run_grid_backtest(
    params: GridParams,
    klines: list[dict[str, Any]],
    current_price: float,
    config: BacktestConfig | None = None,
    *,
    funding_events: list[FundingEvent] | None = None,
) -> BacktestResult:
    config = config or BacktestConfig()
    _validate_backtest_config(config)
    _validate_grid_params(params)
    current_price = _positive_finite(current_price, "current_price")
    if not klines:
        raise ValueError("回测K线不能为空。")
    # 资金费按真实结算事件扣除：只有当某根 Bar 跨过 funding_time 且当时存在库存时
    # 才按净名义价值计费，而不是把资金费平摊进每根 Bar（计划 §9.3）。
    # 显式传入 funding_events（含空列表）即进入事件模式，不再回退到 per-bar 估算；
    # 传 None 表示未提供事件数据，沿用旧的 funding_rate_per_bar 兼容路径。
    funding_event_mode = funding_events is not None
    funding_schedule = _sorted_funding_events(funding_events)
    funding_cursor = 0

    qty = config.capital * config.leverage / current_price / params.grid_num
    qty = _positive_finite(qty, "qty")
    mode = (
        config.direction_mode
        if isinstance(config.direction_mode, GridDirectionMode)
        else GridDirectionMode(str(config.direction_mode).upper())
    )
    if mode == GridDirectionMode.NEUTRAL and params.direction_mode != GridDirectionMode.NEUTRAL:
        mode = params.direction_mode
    seed_entry_price = _seed_entry_price(current_price, mode, config.seed_slippage_bps)
    open_orders = _initial_orders(params, current_price, qty, mode, seed_entry_price)
    if not open_orders:
        raise ValueError("回测初始网格没有可挂订单。")

    fills: list[BacktestFill] = []
    long_lots: list[_PositionLot] = []
    short_lots: list[_PositionLot] = []
    gross_grid_pnl = 0.0
    fees_paid = 0.0
    seed_qty = sum(order.qty for order in open_orders if order.order_intent == OrderIntent.REDUCE)
    seed_fee = 0.0
    if mode == GridDirectionMode.LONG and seed_qty > 0:
        long_lots.append(_PositionLot(seed_entry_price, seed_qty))
    elif mode == GridDirectionMode.SHORT and seed_qty > 0:
        short_lots.append(_PositionLot(seed_entry_price, seed_qty))
    if seed_qty > 0:
        seed_fee = seed_entry_price * seed_qty * config.taker_fee_rate
        fees_paid += seed_fee
        seed_side = OrderSide.BUY if mode == GridDirectionMode.LONG else OrderSide.SELL
        fills.append(
            BacktestFill(
                symbol=params.symbol,
                side=seed_side.value,
                grid_index=-1,
                price=seed_entry_price,
                qty=seed_qty,
                fee=seed_fee,
                grid_pnl=None,
                realized_pnl_after=-seed_fee,
                bar_index=-1,
                position_side=mode.value,
                order_intent=OrderIntent.SEED.value,
            )
        )
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
    soft_breach_count = 0
    defensive = False
    defensive_cancelled: list[_BacktestOrder] = []
    defensive_entry_count = 0
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

        regime_score = _optional_regime_score(row)
        if regime_score is not None:
            if regime_score < config.retention_score_threshold:
                soft_breach_count += 1
                if (
                    not defensive
                    and soft_breach_count >= config.retention_soft_breach_limit
                ):
                    defensive = True
                    defensive_entry_count += 1
                    has_inventory = bool(long_lots or short_lots)
                    retained: list[_BacktestOrder] = []
                    for pending in open_orders:
                        if has_inventory and pending.order_intent == OrderIntent.REDUCE:
                            retained.append(pending)
                        else:
                            defensive_cancelled.append(pending)
                    open_orders = retained
            else:
                soft_breach_count = 0
                if defensive:
                    open_orders.extend(
                        order
                        for order in defensive_cancelled
                        if not _has_equivalent_order(open_orders, order)
                    )
                    defensive_cancelled.clear()
                    defensive = False

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
                    position_side=order.position_side,
                    order_intent=order.order_intent.value,
                )
            )
            next_order = _next_order(params, order)
            if next_order is not None:
                if defensive and next_order.order_intent == OrderIntent.OPEN:
                    defensive_cancelled.append(next_order)
                else:
                    open_orders.append(next_order)
        if funding_event_mode:
            # 显式传入事件列表即进入事件模式：只按跨越的真实结算事件扣费，
            # 不回退到 per-bar 平摊（空列表表示该区间无资金费事件）。
            if conservative and funding_schedule:
                funding_delta, funding_cursor = _event_funding_cost(
                    funding_schedule,
                    funding_cursor,
                    _bar_close_ms(row),
                    long_lots,
                    short_lots,
                    close,
                )
                funding_paid += funding_delta
        elif conservative and config.funding_rate_per_bar != 0:
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
        direction_mode=mode.value,
        seed_qty=seed_qty,
        seed_entry_price=seed_entry_price if seed_qty > 0 else None,
        seed_fee=seed_fee,
        defensive_entry_count=defensive_entry_count,
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
    if config.seed_slippage_bps < 0:
        raise ValueError("seed_slippage_bps不能为负。")
    if not 0 <= config.retention_score_threshold <= 100:
        raise ValueError("retention_score_threshold必须在0到100之间。")
    if config.retention_soft_breach_limit < 1:
        raise ValueError("retention_soft_breach_limit必须大于等于1。")
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


def _initial_orders(
    params: GridParams,
    current_price: float,
    qty: float,
    mode: GridDirectionMode = GridDirectionMode.NEUTRAL,
    seed_entry_price: float | None = None,
) -> list[_BacktestOrder]:
    orders: list[_BacktestOrder] = []
    for index, price in enumerate(params.grid_prices):
        if price == current_price:
            continue
        side = OrderSide.BUY if price < current_price else OrderSide.SELL
        position_side, intent, entry_price = _initial_order_metadata(
            mode,
            side,
            seed_entry_price,
        )
        orders.append(
            _BacktestOrder(
                index,
                side,
                price,
                qty,
                entry_price,
                position_side,
                intent,
            )
        )
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
    entry_price = None if order.entry_price is not None else order.price
    if entry_price is None:
        position_side = "LONG" if next_side == OrderSide.BUY else "SHORT"
    else:
        position_side = "SHORT" if next_side == OrderSide.BUY else "LONG"
    return _BacktestOrder(
        next_index,
        next_side,
        params.grid_prices[next_index],
        order.qty,
        entry_price,
        position_side,
        OrderIntent.REDUCE if entry_price is not None else OrderIntent.OPEN,
    )


def _initial_order_metadata(
    mode: GridDirectionMode,
    side: OrderSide,
    seed_entry_price: float | None,
) -> tuple[str, OrderIntent, float | None]:
    if mode == GridDirectionMode.LONG:
        return (
            "LONG",
            OrderIntent.OPEN if side == OrderSide.BUY else OrderIntent.REDUCE,
            None if side == OrderSide.BUY else seed_entry_price,
        )
    if mode == GridDirectionMode.SHORT:
        return (
            "SHORT",
            OrderIntent.REDUCE if side == OrderSide.BUY else OrderIntent.OPEN,
            seed_entry_price if side == OrderSide.BUY else None,
        )
    return (
        "LONG" if side == OrderSide.BUY else "SHORT",
        OrderIntent.OPEN,
        None,
    )


def _seed_entry_price(
    current_price: float,
    mode: GridDirectionMode,
    slippage_bps: float,
) -> float:
    slippage = max(0.0, slippage_bps) / 10_000
    if mode == GridDirectionMode.LONG:
        return current_price * (1 + slippage)
    if mode == GridDirectionMode.SHORT:
        return current_price * (1 - slippage)
    return current_price


def _optional_regime_score(row: dict[str, Any]) -> float | None:
    value = row.get("regime_score")
    if value in (None, ""):
        return None
    score = _finite_float(value, "regime_score")
    if not 0 <= score <= 100:
        raise ValueError("regime_score必须在0到100之间。")
    return score


def _has_equivalent_order(
    orders: list[_BacktestOrder],
    candidate: _BacktestOrder,
) -> bool:
    return any(
        order.grid_index == candidate.grid_index
        and order.side == candidate.side
        and order.order_intent == candidate.order_intent
        and abs(order.qty - candidate.qty) <= 1e-12
        for order in orders
    )


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


def _sorted_funding_events(
    funding_events: list[FundingEvent] | None,
) -> list[FundingEvent]:
    if not funding_events:
        return []
    return sorted(funding_events, key=lambda event: event.funding_time)


def _bar_close_ms(row: dict[str, Any]) -> int | None:
    """取本根 Bar 的收盘毫秒时间戳，用于资金费事件跨越判断。

    归档/在线 K 线用整数毫秒时间戳，_parse_time 只认 ISO 字符串，故这里单独解析
    close_time / open_time / event_time 等常见字段。
    """
    for key in ("close_time", "event_time", "timestamp", "open_time", "time"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            parsed = _parse_time(raw)
            if parsed is not None:
                return int(parsed.timestamp() * 1000)
    return None


def _bar_open_ms(row: dict[str, Any]) -> int | None:
    for key in ("open_time", "timestamp", "time", "event_time"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            parsed = _parse_time(raw)
            if parsed is not None:
                return int(parsed.timestamp() * 1000)
    return None


def slice_funding_events_for_klines(
    funding_events: list[FundingEvent],
    klines: list[dict[str, Any]],
) -> list[FundingEvent]:
    """裁出落在这批 K 线时间覆盖内的资金费事件。

    run_grid_backtest 的事件游标从 0 开始，第一根 Bar 会结算所有 funding_time
    不晚于其收盘的事件。若把整段事件原样传给某个子区间（walk-forward 分折、
    NYSE 窗口等），早于该区间的事件会被错误地压到第一根 Bar 上。这里按
    [首根开盘, 末根收盘] 裁剪，保证每个事件只归属于覆盖它的那个区间；对相邻、
    不重叠（contiguous）的 K 线区间而言边界不会重复计费，落在区间之间缝隙里
    （观察期、被跳过的窗口）的事件也会被正确排除。
    """
    if not funding_events or not klines:
        return []
    first_open = _bar_open_ms(klines[0])
    last_close = _bar_close_ms(klines[-1])
    if first_open is None or last_close is None:
        return []
    return [
        event
        for event in funding_events
        if first_open <= event.funding_time <= last_close
    ]


def _event_funding_cost(
    schedule: list[FundingEvent],
    cursor: int,
    bar_ms: int | None,
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    close: float,
) -> tuple[float, int]:
    """结算本根 Bar 跨过的所有真实资金费事件（§9.3）。

    只有当 Bar 时间到达某个 funding_time、且当时存在库存时才按净名义价值扣费；
    无库存或未跨过事件则不扣。cursor 记录已结算到的事件下标，保证每个事件只计一次。
    """
    if bar_ms is None:
        return 0.0, cursor
    charge = 0.0
    while cursor < len(schedule) and schedule[cursor].funding_time <= bar_ms:
        event = schedule[cursor]
        mark_price = event.mark_price if event.mark_price is not None else close
        long_notional = sum(lot.qty for lot in long_lots) * mark_price
        short_notional = sum(lot.qty for lot in short_lots) * mark_price
        charge += (long_notional - short_notional) * event.funding_rate
        cursor += 1
    return charge, cursor


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
