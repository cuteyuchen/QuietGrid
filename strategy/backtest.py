from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from math import ceil, isfinite
from typing import Any

from core.models import GridDirectionMode, GridParams, OrderIntent, OrderSide
from data_sources.models import FundingEvent
from strategy.order_plan import InitialGridOrderPlan, build_initial_grid_order_plan
from strategy.profit_protection import (
    ProfitProtectionAction,
    ProfitProtectionConfig,
    ProfitProtectionTracker,
)


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
    quantity_step_size: float = 0.0
    wind_down_bars: int = 0
    inventory_wind_down_bars: int = 0
    inventory_wind_down_utilization: float = 0.0
    inventory_wind_down_only_when_losing: bool = False
    max_inventory_notional: float = 0.0
    inventory_caution_utilization: float = 0.40
    inventory_critical_utilization: float = 0.80
    wind_down_reprice_interval_bars: int = 0
    wind_down_initial_offset_steps: float = 0.0
    wind_down_urgency_exponent: float = 1.0
    wind_down_unwind_fraction: float = 1.0
    max_unpaired_lots_per_side: int = 0
    reduce_target_step_fraction: float = 1.0
    unpaired_lot_cap_enforcement: str = "INTRABAR"
    profit_protection_enabled: bool = False
    profit_protection_mode: str = "PEAK_DRAWDOWN"
    profit_activation_usdt: float = 10.0
    profit_minimum_locked_ratio: float = 0.25
    profit_suppress_drawdown_pct: float = 0.25
    profit_reduce_drawdown_pct: float = 0.35
    profit_close_drawdown_pct: float = 0.50
    profit_estimated_exit_cost_rate: float = 0.0007
    profit_passive_reduce_after_bars: int = 0
    profit_active_reduce_after_bars: int = 0
    profit_passive_reduce_fraction: float = 0.25
    profit_active_reduce_fraction: float = 0.25
    volatility_reduce_expansion_ratio: float = 0.0
    volatility_reduce_after_breaches: int = 0
    volatility_reduce_fraction: float = 0.20
    volatility_reduce_mode: str = "BOTH"
    volatility_reduce_only_when_losing: bool = False
    volatility_wind_down_after_reduce: bool = False
    volatility_resume_after_normal_bars: int = 0


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
    exit_slippage_cost: float = 0.0
    attempted_fill_count: int = 0
    rejected_fill_count: int = 0
    pair_completion_count: int = 0
    max_inventory_utilization: float = 0.0
    direction_mode: str = "NEUTRAL"
    seed_qty: float = 0.0
    seed_entry_price: float | None = None
    seed_fee: float = 0.0
    defensive_entry_count: int = 0
    wind_down_entry_count: int = 0
    inventory_suppression_count: int = 0
    inventory_critical_exit_count: int = 0
    wind_down_reprice_count: int = 0
    wind_down_maker_fill_count: int = 0
    wind_down_maker_pnl: float = 0.0
    max_unpaired_lot_age_bars: int = 0
    exit_oldest_lot_age_bars: int = 0
    exit_long_qty: float = 0.0
    exit_short_qty: float = 0.0
    exit_hedged_fraction: float = 0.0
    profit_protection_activation_count: int = 0
    profit_suppress_count: int = 0
    profit_reduce_count: int = 0
    profit_close_count: int = 0
    profitable_to_losing_count: int = 0
    profit_peak_net_pnl: float = 0.0
    peak_profit_giveback_usdt: float = 0.0
    peak_profit_giveback_pct: float = 0.0
    locked_profit_usdt: float = 0.0
    profit_exit_cost: float = 0.0
    bars_from_activation_to_close: int | None = None
    profit_reduce_to_close_bars: int | None = None
    profit_close_estimated_net_pnl: float | None = None
    profit_close_actual_net_pnl: float | None = None
    profit_close_net_pnl_error: float | None = None
    profit_suppress_inventory_growth_usdt: float = 0.0
    profit_reduce_inventory_reduction_30_pct: float | None = None
    profit_reduce_inventory_reduction_60_pct: float | None = None
    profit_reduce_inventory_reduction_120_pct: float | None = None
    profit_passive_reduce_reprice_count: int = 0
    profit_passive_reduce_fill_count: int = 0
    profit_active_reduce_count: int = 0
    profit_active_reduce_pnl: float = 0.0
    profit_active_reduce_cost: float = 0.0
    profit_active_reduce_inventory_reduction_pct: float | None = None
    volatility_breach_count: int = 0
    volatility_max_consecutive_breaches: int = 0
    volatility_reduce_count: int = 0
    volatility_reduce_pnl: float = 0.0
    volatility_reduce_cost: float = 0.0
    volatility_reduce_inventory_reduction_pct: float | None = None


@dataclass
class _BacktestOrder:
    grid_index: int
    side: OrderSide
    price: float
    qty: float
    entry_price: float | None = None
    position_side: str = ""
    order_intent: OrderIntent = OrderIntent.OPEN
    wind_down_reduce: bool = False
    profit_reduce: bool = False


@dataclass
class _PositionLot:
    entry_price: float
    qty: float
    opened_bar_index: int = -1


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
        quantity_step_size=float(backtest.get("quantity_step_size", 0.0)),
        wind_down_bars=int(backtest.get("wind_down_bars", 0)),
        inventory_wind_down_bars=int(
            backtest.get("inventory_wind_down_bars", 0)
        ),
        inventory_wind_down_utilization=float(
            backtest.get("inventory_wind_down_utilization", 0.0)
        ),
        inventory_wind_down_only_when_losing=bool(
            backtest.get("inventory_wind_down_only_when_losing", False)
        ),
        max_inventory_notional=float(backtest.get("max_inventory_notional", 0.0)),
        inventory_caution_utilization=float(
            backtest.get("inventory_caution_utilization", 0.40)
        ),
        inventory_critical_utilization=float(
            backtest.get("inventory_critical_utilization", 0.80)
        ),
        wind_down_reprice_interval_bars=int(
            backtest.get("wind_down_reprice_interval_bars", 0)
        ),
        wind_down_initial_offset_steps=float(
            backtest.get("wind_down_initial_offset_steps", 0.0)
        ),
        wind_down_urgency_exponent=float(
            backtest.get("wind_down_urgency_exponent", 1.0)
        ),
        wind_down_unwind_fraction=float(
            backtest.get("wind_down_unwind_fraction", 1.0)
        ),
        max_unpaired_lots_per_side=int(
            backtest.get("max_unpaired_lots_per_side", 0)
        ),
        reduce_target_step_fraction=float(
            backtest.get("reduce_target_step_fraction", 1.0)
        ),
        unpaired_lot_cap_enforcement=str(
            backtest.get("unpaired_lot_cap_enforcement", "INTRABAR")
        ).upper(),
        profit_protection_enabled=bool(
            backtest.get("profit_protection_enabled", False)
        ),
        profit_protection_mode=str(
            backtest.get("profit_protection_mode", "PEAK_DRAWDOWN")
        ).upper(),
        profit_activation_usdt=float(
            backtest.get(
                "profit_activation_usdt",
                trading.get("take_profit_usdt", 10.0),
            )
        ),
        profit_minimum_locked_ratio=float(
            backtest.get("profit_minimum_locked_ratio", 0.25)
        ),
        profit_suppress_drawdown_pct=float(
            backtest.get("profit_suppress_drawdown_pct", 0.25)
        ),
        profit_reduce_drawdown_pct=float(
            backtest.get("profit_reduce_drawdown_pct", 0.35)
        ),
        profit_close_drawdown_pct=float(
            backtest.get("profit_close_drawdown_pct", 0.50)
        ),
        profit_estimated_exit_cost_rate=float(
            backtest.get("profit_estimated_exit_cost_rate", 0.0007)
        ),
        profit_passive_reduce_after_bars=int(
            backtest.get("profit_passive_reduce_after_bars", 0)
        ),
        profit_active_reduce_after_bars=int(
            backtest.get("profit_active_reduce_after_bars", 0)
        ),
        profit_passive_reduce_fraction=float(
            backtest.get("profit_passive_reduce_fraction", 0.25)
        ),
        profit_active_reduce_fraction=float(
            backtest.get("profit_active_reduce_fraction", 0.25)
        ),
        volatility_reduce_expansion_ratio=float(
            backtest.get("volatility_reduce_expansion_ratio", 0.0)
        ),
        volatility_reduce_after_breaches=int(
            backtest.get("volatility_reduce_after_breaches", 0)
        ),
        volatility_reduce_fraction=float(
            backtest.get("volatility_reduce_fraction", 0.20)
        ),
        volatility_reduce_mode=str(
            backtest.get("volatility_reduce_mode", "BOTH")
        ),
        volatility_reduce_only_when_losing=bool(
            backtest.get("volatility_reduce_only_when_losing", False)
        ),
        volatility_wind_down_after_reduce=bool(
            backtest.get("volatility_wind_down_after_reduce", False)
        ),
        volatility_resume_after_normal_bars=int(
            backtest.get("volatility_resume_after_normal_bars", 0)
        ),
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

    order_plan = build_initial_grid_order_plan(
        params,
        current_price,
        capital=config.capital,
        leverage=config.leverage,
        tick_size=config.min_tick_size,
        quantity_step_size=config.quantity_step_size,
    )
    mode = (
        config.direction_mode
        if isinstance(config.direction_mode, GridDirectionMode)
        else GridDirectionMode(str(config.direction_mode).upper())
    )
    if mode == GridDirectionMode.NEUTRAL and params.direction_mode != GridDirectionMode.NEUTRAL:
        mode = params.direction_mode
    seed_entry_price = _seed_entry_price(current_price, mode, config.seed_slippage_bps)
    open_orders = _initial_orders_from_plan(order_plan, mode, seed_entry_price)
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
        long_lots.append(_PositionLot(seed_entry_price, seed_qty, -1))
    elif mode == GridDirectionMode.SHORT and seed_qty > 0:
        short_lots.append(_PositionLot(seed_entry_price, seed_qty, -1))
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
    exit_slippage_cost = 0.0
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
    profit_cancelled: list[_BacktestOrder] = []
    defensive_entry_count = 0
    wind_down_active = False
    wind_down_started_remaining_bars: int | None = None
    wind_down_entry_count = 0
    inventory_suppression_count = 0
    inventory_critical_exit_count = 0
    wind_down_reprice_count = 0
    wind_down_maker_fill_count = 0
    wind_down_maker_pnl = 0.0
    max_unpaired_lot_age_bars = 0
    exit_oldest_lot_age_bars = 0
    exit_long_qty = 0.0
    exit_short_qty = 0.0
    exit_hedged_fraction = 0.0
    conservative = config.fill_model.upper() == "L0_CONSERVATIVE"
    min_tick_size = (
        config.min_tick_size
        if config.min_tick_size > 0
        else max(min(params.grid_prices) * 1e-8, 1e-12)
    )
    profit_mode = config.profit_protection_mode.strip().upper()
    profit_tracker = ProfitProtectionTracker(
        ProfitProtectionConfig(
            activation_profit_usdt=config.profit_activation_usdt,
            enabled=(
                config.profit_protection_enabled
                and profit_mode != "OFF"
            ),
            minimum_locked_profit_ratio=config.profit_minimum_locked_ratio,
            suppress_drawdown_pct=config.profit_suppress_drawdown_pct,
            reduce_drawdown_pct=config.profit_reduce_drawdown_pct,
            close_drawdown_pct=config.profit_close_drawdown_pct,
            estimated_exit_cost_rate=config.profit_estimated_exit_cost_rate,
        )
    )
    profit_session_id = 1
    profit_activated = False
    profit_activation_bar: int | None = None
    profit_last_action = ProfitProtectionAction.NONE
    profit_protection_activation_count = 0
    profit_suppress_count = 0
    profit_reduce_count = 0
    profit_close_count = 0
    profit_reduce_bar: int | None = None
    profit_reduce_inventory_notional: float | None = None
    profit_reduce_inventory_reductions: dict[int, float | None] = {
        30: None,
        60: None,
        120: None,
    }
    profit_suppress_inventory_notional: float | None = None
    profit_suppress_inventory_growth_usdt = 0.0
    bars_from_activation_to_close: int | None = None
    profit_reduce_to_close_bars: int | None = None
    profit_close_estimated_net_pnl: float | None = None
    profit_close_actual_net_pnl: float | None = None
    profit_close_net_pnl_error: float | None = None
    profit_exit_cost = 0.0
    profit_passive_reduce_placed = False
    profit_passive_reduce_reprice_count = 0
    profit_passive_reduce_fill_count = 0
    profit_active_reduce_executed = False
    profit_active_reduce_count = 0
    profit_active_reduce_pnl = 0.0
    profit_active_reduce_cost = 0.0
    profit_active_reduce_inventory_reduction_pct: float | None = None
    volatility_consecutive_breaches = 0
    volatility_breach_count = 0
    volatility_max_consecutive_breaches = 0
    volatility_reduce_executed = False
    volatility_reduce_count = 0
    volatility_reduce_pnl = 0.0
    volatility_reduce_cost = 0.0
    volatility_reduce_inventory_reduction_pct: float | None = None
    volatility_wind_down_active = False
    volatility_normal_bars = 0
    volatility_cancelled: list[_BacktestOrder] = []

    for bar_index, row in enumerate(klines):
        previous_event_time = _validate_bar_time(row, previous_event_time)
        high, low, close = _bar_prices(row)
        last_price = close
        max_unpaired_lot_age_bars = max(
            max_unpaired_lot_age_bars,
            _oldest_lot_age_bars(long_lots, short_lots, bar_index),
        )
        bar_start_long_lot_count = len(long_lots)
        bar_start_short_lot_count = len(short_lots)

        remaining_bars = len(klines) - bar_index
        if (
            not wind_down_active
            and config.wind_down_bars > 0
            and remaining_bars <= config.wind_down_bars
        ):
            wind_down_active = True
            wind_down_started_remaining_bars = remaining_bars
            wind_down_entry_count += 1
            open_orders = [
                order
                for order in open_orders
                if order.order_intent == OrderIntent.REDUCE
            ]
            defensive_cancelled.clear()

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
                if defensive and not wind_down_active:
                    if not volatility_wind_down_active:
                        open_orders.extend(
                            order
                            for order in defensive_cancelled
                            if not _has_equivalent_order(open_orders, order)
                        )
                    defensive_cancelled.clear()
                    defensive = False

        volatility_expansion = row.get("volatility_expansion")
        volatility_breach = (
            config.volatility_reduce_expansion_ratio > 0
            and volatility_expansion is not None
            and float(volatility_expansion)
            >= config.volatility_reduce_expansion_ratio
        )
        if volatility_breach:
            volatility_breach_count += 1
            volatility_consecutive_breaches += 1
            volatility_max_consecutive_breaches = max(
                volatility_max_consecutive_breaches,
                volatility_consecutive_breaches,
            )
        else:
            volatility_consecutive_breaches = 0
        if volatility_wind_down_active:
            volatility_normal_bars = (
                0 if volatility_breach else volatility_normal_bars + 1
            )
            if (
                config.volatility_resume_after_normal_bars > 0
                and volatility_normal_bars
                >= config.volatility_resume_after_normal_bars
            ):
                volatility_wind_down_active = False
                volatility_normal_bars = 0
                if wind_down_active:
                    volatility_cancelled.clear()
                elif defensive:
                    defensive_cancelled.extend(
                        order
                        for order in volatility_cancelled
                        if not _has_equivalent_order(defensive_cancelled, order)
                    )
                    volatility_cancelled.clear()
                else:
                    open_orders.extend(
                        order
                        for order in volatility_cancelled
                        if not _has_equivalent_order(open_orders, order)
                    )
                    volatility_cancelled.clear()
        if (
            config.volatility_reduce_after_breaches > 0
            and not volatility_reduce_executed
            and volatility_consecutive_breaches
            >= config.volatility_reduce_after_breaches
            and (long_lots or short_lots)
        ):
            execution_price = _positive_finite(
                row.get("open", close),
                "open",
            )
            current_net_pnl = (
                gross_grid_pnl
                - fees_paid
                - funding_paid
                + _unrealized_pnl(long_lots, short_lots, execution_price)
            )
            if (
                config.volatility_reduce_only_when_losing
                and current_net_pnl >= 0
            ):
                continue_volatility_reduce = False
            else:
                continue_volatility_reduce = True
        else:
            continue_volatility_reduce = False
        if continue_volatility_reduce:
            before_notional = _gross_inventory_notional(
                long_lots,
                short_lots,
                execution_price,
            )
            partial_exits = _close_lot_fraction_at_market(
                long_lots,
                short_lots,
                execution_price,
                fraction=config.volatility_reduce_fraction,
                quantity_step_size=config.quantity_step_size,
                taker_fee_rate=config.taker_fee_rate,
                slippage_bps=config.stop_slippage_bps,
                mode=config.volatility_reduce_mode,
            )
            for side, position_side, qty, exit_price, pnl, fee, slippage_cost in partial_exits:
                gross_grid_pnl += pnl
                fees_paid += fee
                exit_slippage_cost += slippage_cost
                volatility_reduce_pnl += pnl
                volatility_reduce_cost += fee + slippage_cost
                fills.append(BacktestFill(
                    symbol=params.symbol,
                    side=side.value,
                    grid_index=-3_000,
                    price=exit_price,
                    qty=qty,
                    fee=fee,
                    grid_pnl=pnl,
                    realized_pnl_after=(gross_grid_pnl - fees_paid - funding_paid),
                    bar_index=bar_index,
                    timestamp=_bar_timestamp(row),
                    position_side=position_side,
                    order_intent=OrderIntent.REDUCE.value,
                ))
            if partial_exits:
                volatility_reduce_count += 1
                volatility_reduce_executed = True
                if config.volatility_wind_down_after_reduce:
                    volatility_wind_down_active = True
                    volatility_normal_bars = 0
                    for pending in (
                        open_orders + defensive_cancelled + profit_cancelled
                    ):
                        if (
                            pending.order_intent == OrderIntent.OPEN
                            and not _has_equivalent_order(
                                volatility_cancelled,
                                pending,
                            )
                        ):
                            volatility_cancelled.append(pending)
                    open_orders = [
                        order
                        for order in open_orders
                        if order.order_intent == OrderIntent.REDUCE
                    ]
                    defensive_cancelled.clear()
                    profit_cancelled.clear()
                after_notional = _gross_inventory_notional(
                    long_lots,
                    short_lots,
                    execution_price,
                )
                volatility_reduce_inventory_reduction_pct = (
                    (before_notional - after_notional) / before_notional
                    if before_notional > 0
                    else None
                )
                open_orders = _trim_reduce_orders_to_inventory(
                    open_orders,
                    long_lots,
                    short_lots,
                )

        risk_reason, risk_price = _risk_stop(params, high, low, config)
        if risk_reason is not None:
            (
                exit_oldest_lot_age_bars,
                exit_long_qty,
                exit_short_qty,
                exit_hedged_fraction,
            ) = _exit_inventory_snapshot(long_lots, short_lots, bar_index)
            mark_exit_price = float(risk_price)
            exit_price = mark_exit_price
            if conservative and config.stop_slippage_bps > 0:
                slippage = config.stop_slippage_bps / 10_000
                exit_price *= (
                    1 - slippage
                    if exit_price <= params.center
                    else 1 + slippage
                )
            if conservative:
                closed_qty = sum(lot.qty for lot in long_lots) + sum(
                    lot.qty for lot in short_lots
                )
                stop_exit_pnl, stop_exit_cost = _close_all_lots(
                    long_lots,
                    short_lots,
                    exit_price,
                    config.taker_fee_rate,
                )
                gross_grid_pnl += stop_exit_pnl
                fees_paid += stop_exit_cost
                exit_slippage_cost += (
                    abs(mark_exit_price - exit_price) * closed_qty
                )
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
            if config.unpaired_lot_cap_enforcement == "BAR_BOUNDARY":
                long_lot_count = bar_start_long_lot_count
                short_lot_count = bar_start_short_lot_count
            else:
                long_lot_count = len(long_lots)
                short_lot_count = len(short_lots)
            if _unpaired_lot_limit_reached(
                order,
                long_lot_count,
                short_lot_count,
                config.max_unpaired_lots_per_side,
            ):
                inventory_suppression_count += 1
                continue
            open_orders.remove(order)
            fee = order.price * order.qty * config.maker_fee_rate
            fees_paid += fee
            grid_pnl = _grid_pnl(order)
            if grid_pnl is not None:
                gross_grid_pnl += grid_pnl
                pair_completion_count += 1
                if order.wind_down_reduce:
                    wind_down_maker_fill_count += 1
                    wind_down_maker_pnl += grid_pnl
                if order.profit_reduce:
                    profit_passive_reduce_fill_count += 1
            _apply_position_fill(order, long_lots, short_lots, bar_index)
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
            next_order = _next_order(
                params,
                order,
                reduce_target_step_fraction=config.reduce_target_step_fraction,
                tick_size=min_tick_size,
            )
            if next_order is not None:
                if (
                    (defensive or wind_down_active or volatility_wind_down_active)
                    and next_order.order_intent == OrderIntent.OPEN
                ):
                    if not volatility_wind_down_active:
                        defensive_cancelled.append(next_order)
                    elif not _has_equivalent_order(
                        volatility_cancelled,
                        next_order,
                    ):
                        volatility_cancelled.append(next_order)
                else:
                    open_orders.append(next_order)

        if config.max_inventory_notional > 0:
            inventory_utilization = _inventory_utilization(
                long_lots,
                short_lots,
                close,
                config.max_inventory_notional,
                baseline_notional=(
                    seed_qty * close
                    if mode != GridDirectionMode.NEUTRAL
                    else 0.0
                ),
            )
            max_inventory_utilization = max(
                max_inventory_utilization,
                inventory_utilization,
            )
            if inventory_utilization >= config.inventory_critical_utilization:
                (
                    exit_oldest_lot_age_bars,
                    exit_long_qty,
                    exit_short_qty,
                    exit_hedged_fraction,
                ) = _exit_inventory_snapshot(long_lots, short_lots, bar_index)
                market_exit_cost = _market_exit_cost(
                    long_lots,
                    short_lots,
                    close,
                    config.taker_fee_rate,
                    config.stop_slippage_bps,
                )
                stop_exit_pnl, stop_exit_cost = _close_all_lots_at_market(
                    long_lots,
                    short_lots,
                    close,
                    config.taker_fee_rate,
                    config.stop_slippage_bps,
                )
                gross_grid_pnl += stop_exit_pnl
                fees_paid += stop_exit_cost
                exit_slippage_cost += max(
                    0.0,
                    market_exit_cost - stop_exit_cost,
                )
                stopped_reason = "inventory_critical"
                stopped_at_index = bar_index
                stopped_at_price = close
                inventory_critical_exit_count += 1
                open_orders.clear()
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
                break
            if (
                not wind_down_active
                and config.inventory_wind_down_bars > 0
                and config.wind_down_bars < remaining_bars
                <= config.inventory_wind_down_bars
                and inventory_utilization
                >= config.inventory_wind_down_utilization
                and (
                    not config.inventory_wind_down_only_when_losing
                    or _unrealized_pnl(long_lots, short_lots, close) < 0
                )
            ):
                wind_down_active = True
                wind_down_started_remaining_bars = remaining_bars
                wind_down_entry_count += 1
                open_orders = [
                    order
                    for order in open_orders
                    if order.order_intent == OrderIntent.REDUCE
                ]
                defensive_cancelled.clear()
            if inventory_utilization >= config.inventory_caution_utilization:
                net_qty = sum(lot.qty for lot in long_lots) - sum(
                    lot.qty for lot in short_lots
                )
                increasing_side = (
                    OrderSide.BUY
                    if net_qty > 1e-12
                    else OrderSide.SELL
                    if net_qty < -1e-12
                    else None
                )
                retained_orders: list[_BacktestOrder] = []
                for pending in open_orders:
                    suppress = (
                        pending.order_intent == OrderIntent.OPEN
                        and pending.entry_price is None
                        and (
                            mode != GridDirectionMode.NEUTRAL
                            or pending.side == increasing_side
                        )
                    )
                    if suppress:
                        inventory_suppression_count += 1
                    else:
                        retained_orders.append(pending)
                open_orders = retained_orders
        active_wind_down_bars = (
            wind_down_started_remaining_bars or config.wind_down_bars
        )
        if (
            wind_down_active
            and config.wind_down_reprice_interval_bars > 0
            and remaining_bars > 1
            and (
                active_wind_down_bars - remaining_bars
            ) % config.wind_down_reprice_interval_bars == 0
            and (long_lots or short_lots)
        ):
            open_orders = _wind_down_reduce_orders(
                long_lots,
                short_lots,
                close,
                remaining_bars=max(0, remaining_bars - 1),
                wind_down_bars=active_wind_down_bars,
                initial_offset_steps=config.wind_down_initial_offset_steps,
                urgency_exponent=config.wind_down_urgency_exponent,
                step_pct=params.step_pct,
                tick_size=min_tick_size,
                grid_prices=params.grid_prices,
                quantity_step_size=config.quantity_step_size,
                unwind_fraction=config.wind_down_unwind_fraction,
            )
            defensive_cancelled.clear()
            wind_down_reprice_count += 1
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

        realized_before_profit_exit = gross_grid_pnl - fees_paid - funding_paid
        gross_inventory_notional = _gross_inventory_notional(
            long_lots,
            short_lots,
            close,
        )
        profit_decision = profit_tracker.evaluate(
            profit_session_id,
            realized_pnl=realized_before_profit_exit,
            unrealized_pnl=_unrealized_pnl(long_lots, short_lots, close),
            gross_inventory_notional=gross_inventory_notional,
        )
        if profit_decision.snapshot.activated and not profit_activated:
            profit_activated = True
            profit_protection_activation_count += 1
            profit_activation_bar = bar_index

        profit_action = (
            ProfitProtectionAction.CLOSE
            if (
                profit_mode == "FIXED_CLOSE"
                and profit_decision.snapshot.activated
                and profit_decision.snapshot.current_net_pnl
                >= config.profit_activation_usdt
            )
            else (
                profit_decision.action
                if profit_mode == "PEAK_DRAWDOWN"
                else ProfitProtectionAction.NONE
            )
        )
        if (
            profit_action != ProfitProtectionAction.NONE
            and profit_action != profit_last_action
        ):
            if profit_action == ProfitProtectionAction.SUPPRESS:
                profit_suppress_count += 1
            elif profit_action == ProfitProtectionAction.REDUCE:
                profit_reduce_count += 1
            elif profit_action == ProfitProtectionAction.CLOSE:
                profit_close_count += 1

        if profit_action == ProfitProtectionAction.SUPPRESS:
            retained_orders: list[_BacktestOrder] = []
            for pending in open_orders:
                if pending.order_intent == OrderIntent.OPEN:
                    profit_cancelled.append(pending)
                else:
                    retained_orders.append(pending)
            open_orders = retained_orders
            if profit_suppress_inventory_notional is None:
                profit_suppress_inventory_notional = gross_inventory_notional
        elif profit_action == ProfitProtectionAction.REDUCE:
            open_orders, cancelled = _suppress_inventory_increasing_orders(
                open_orders,
                net_qty=(
                    sum(lot.qty for lot in long_lots)
                    - sum(lot.qty for lot in short_lots)
                ),
                direction_mode=mode,
            )
            profit_cancelled.extend(cancelled)
            if profit_reduce_bar is None:
                profit_reduce_bar = bar_index
                profit_reduce_inventory_notional = gross_inventory_notional
        elif profit_action == ProfitProtectionAction.NONE:
            if (
                not defensive
                and not wind_down_active
                and not volatility_wind_down_active
            ):
                open_orders.extend(
                    pending
                    for pending in profit_cancelled
                    if not _has_equivalent_order(open_orders, pending)
                )
                profit_cancelled.clear()

        if profit_suppress_inventory_notional is not None:
            profit_suppress_inventory_growth_usdt = max(
                profit_suppress_inventory_growth_usdt,
                gross_inventory_notional - profit_suppress_inventory_notional,
            )
        if (
            profit_reduce_bar is not None
            and profit_reduce_inventory_notional is not None
            and profit_reduce_inventory_notional > 0
        ):
            elapsed = bar_index - profit_reduce_bar
            if (
                config.profit_passive_reduce_after_bars > 0
                and not profit_passive_reduce_placed
                and elapsed >= config.profit_passive_reduce_after_bars
                and (long_lots or short_lots)
                and profit_action != ProfitProtectionAction.CLOSE
            ):
                open_orders = [
                    order
                    for order in open_orders
                    if order.order_intent != OrderIntent.REDUCE
                ]
                open_orders.extend(_profit_passive_reduce_orders(
                    long_lots,
                    short_lots,
                    close,
                    step_pct=params.step_pct,
                    tick_size=min_tick_size,
                    grid_prices=params.grid_prices,
                    quantity_step_size=config.quantity_step_size,
                    fraction=config.profit_passive_reduce_fraction,
                ))
                profit_passive_reduce_placed = True
                profit_passive_reduce_reprice_count += 1

            if (
                config.profit_active_reduce_after_bars > 0
                and not profit_active_reduce_executed
                and elapsed >= config.profit_active_reduce_after_bars
                and profit_action in {
                    ProfitProtectionAction.SUPPRESS,
                    ProfitProtectionAction.REDUCE,
                }
                and (long_lots or short_lots)
            ):
                before_notional = _gross_inventory_notional(
                    long_lots,
                    short_lots,
                    close,
                )
                partial_exits = _close_lot_fraction_at_market(
                    long_lots,
                    short_lots,
                    close,
                    fraction=config.profit_active_reduce_fraction,
                    quantity_step_size=config.quantity_step_size,
                    taker_fee_rate=config.taker_fee_rate,
                    slippage_bps=config.stop_slippage_bps,
                )
                for side, position_side, qty, exit_price, pnl, fee, slippage_cost in partial_exits:
                    gross_grid_pnl += pnl
                    fees_paid += fee
                    exit_slippage_cost += slippage_cost
                    profit_active_reduce_pnl += pnl
                    profit_active_reduce_cost += fee + slippage_cost
                    fills.append(BacktestFill(
                        symbol=params.symbol,
                        side=side.value,
                        grid_index=-2_000,
                        price=exit_price,
                        qty=qty,
                        fee=fee,
                        grid_pnl=pnl,
                        realized_pnl_after=(
                            gross_grid_pnl - fees_paid - funding_paid
                        ),
                        bar_index=bar_index,
                        timestamp=_bar_timestamp(row),
                        position_side=position_side,
                        order_intent=OrderIntent.REDUCE.value,
                    ))
                if partial_exits:
                    profit_active_reduce_count += 1
                    profit_active_reduce_executed = True
                    after_notional = _gross_inventory_notional(
                        long_lots,
                        short_lots,
                        close,
                    )
                    profit_active_reduce_inventory_reduction_pct = (
                        (before_notional - after_notional) / before_notional
                        if before_notional > 0
                        else None
                    )
                    open_orders = [
                        order
                        for order in open_orders
                        if order.order_intent != OrderIntent.REDUCE
                    ]
                    open_orders.extend(_profit_passive_reduce_orders(
                        long_lots,
                        short_lots,
                        close,
                        step_pct=params.step_pct,
                        tick_size=min_tick_size,
                        grid_prices=params.grid_prices,
                        quantity_step_size=config.quantity_step_size,
                        fraction=config.profit_passive_reduce_fraction,
                    ))
                    profit_passive_reduce_reprice_count += 1
                    gross_inventory_notional = after_notional
            for horizon in profit_reduce_inventory_reductions:
                if (
                    profit_reduce_inventory_reductions[horizon] is None
                    and elapsed >= horizon
                ):
                    profit_reduce_inventory_reductions[horizon] = (
                        profit_reduce_inventory_notional
                        - gross_inventory_notional
                    ) / profit_reduce_inventory_notional

        if profit_action == ProfitProtectionAction.CLOSE:
            (
                exit_oldest_lot_age_bars,
                exit_long_qty,
                exit_short_qty,
                exit_hedged_fraction,
            ) = _exit_inventory_snapshot(long_lots, short_lots, bar_index)
            profit_close_estimated_net_pnl = (
                profit_decision.snapshot.current_net_pnl
            )
            profit_exit_cost = _market_exit_cost(
                long_lots,
                short_lots,
                close,
                config.taker_fee_rate,
                config.stop_slippage_bps,
            )
            stop_exit_pnl, stop_exit_cost = _close_all_lots_at_market(
                long_lots,
                short_lots,
                close,
                config.taker_fee_rate,
                config.stop_slippage_bps,
            )
            gross_grid_pnl += stop_exit_pnl
            fees_paid += stop_exit_cost
            exit_slippage_cost += max(
                0.0,
                profit_exit_cost - stop_exit_cost,
            )
            profit_close_actual_net_pnl = (
                gross_grid_pnl - fees_paid - funding_paid
            )
            profit_close_net_pnl_error = (
                profit_close_actual_net_pnl
                - profit_close_estimated_net_pnl
            )
            stopped_reason = (
                "profit_fixed_close"
                if profit_mode == "FIXED_CLOSE"
                else "profit_protection_close"
            )
            stopped_at_index = bar_index
            stopped_at_price = close
            open_orders.clear()
            profit_cancelled.clear()
            defensive_cancelled.clear()
            bars_from_activation_to_close = (
                bar_index - profit_activation_bar
                if profit_activation_bar is not None
                else None
            )
            profit_reduce_to_close_bars = (
                bar_index - profit_reduce_bar
                if profit_reduce_bar is not None
                else None
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
            profit_last_action = profit_action
            break

        profit_last_action = profit_action
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
        (
            exit_oldest_lot_age_bars,
            exit_long_qty,
            exit_short_qty,
            exit_hedged_fraction,
        ) = _exit_inventory_snapshot(long_lots, short_lots, len(klines))
        market_exit_cost = _market_exit_cost(
            long_lots,
            short_lots,
            last_price,
            config.taker_fee_rate,
            config.stop_slippage_bps,
        )
        stop_exit_pnl, stop_exit_cost = _close_all_lots_at_market(
            long_lots,
            short_lots,
            last_price,
            config.taker_fee_rate,
            config.stop_slippage_bps,
        )
        gross_grid_pnl += stop_exit_pnl
        fees_paid += stop_exit_cost
        exit_slippage_cost += max(0.0, market_exit_cost - stop_exit_cost)
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
    final_profit_snapshot = profit_tracker.evaluate(
        profit_session_id,
        realized_pnl=realized_pnl,
        unrealized_pnl=unrealized_pnl,
        gross_inventory_notional=_gross_inventory_notional(
            long_lots,
            short_lots,
            last_price,
        ),
    ).snapshot
    # 未强平的通用回测也必须按与实时状态机相同的预计退出成本口径统计
    # 最终净利润；研究窗口强平后库存为零，此处自然等于实际退出后的净利润。
    final_net_pnl = final_profit_snapshot.current_net_pnl
    profit_peak_net_pnl = max(0.0, final_profit_snapshot.peak_net_pnl)
    peak_profit_giveback_usdt = max(0.0, profit_peak_net_pnl - final_net_pnl)
    peak_profit_giveback_pct = (
        peak_profit_giveback_usdt / profit_peak_net_pnl
        if profit_peak_net_pnl > 0
        else 0.0
    )
    locked_profit_usdt = (
        max(0.0, final_net_pnl)
        if profit_protection_activation_count > 0
        else 0.0
    )
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
        exit_slippage_cost=exit_slippage_cost,
        attempted_fill_count=attempted_fill_count,
        rejected_fill_count=max(0, attempted_fill_count - len(fills)),
        pair_completion_count=pair_completion_count,
        max_inventory_utilization=max_inventory_utilization,
        direction_mode=mode.value,
        seed_qty=seed_qty,
        seed_entry_price=seed_entry_price if seed_qty > 0 else None,
        seed_fee=seed_fee,
        defensive_entry_count=defensive_entry_count,
        wind_down_entry_count=wind_down_entry_count,
        inventory_suppression_count=inventory_suppression_count,
        inventory_critical_exit_count=inventory_critical_exit_count,
        wind_down_reprice_count=wind_down_reprice_count,
        wind_down_maker_fill_count=wind_down_maker_fill_count,
        wind_down_maker_pnl=wind_down_maker_pnl,
        max_unpaired_lot_age_bars=max_unpaired_lot_age_bars,
        exit_oldest_lot_age_bars=exit_oldest_lot_age_bars,
        exit_long_qty=exit_long_qty,
        exit_short_qty=exit_short_qty,
        exit_hedged_fraction=exit_hedged_fraction,
        profit_protection_activation_count=(
            profit_protection_activation_count
        ),
        profit_suppress_count=profit_suppress_count,
        profit_reduce_count=profit_reduce_count,
        profit_close_count=profit_close_count,
        profitable_to_losing_count=int(
            profit_peak_net_pnl > 0 and final_net_pnl <= 0
        ),
        profit_peak_net_pnl=profit_peak_net_pnl,
        peak_profit_giveback_usdt=peak_profit_giveback_usdt,
        peak_profit_giveback_pct=peak_profit_giveback_pct,
        locked_profit_usdt=locked_profit_usdt,
        profit_exit_cost=profit_exit_cost,
        bars_from_activation_to_close=bars_from_activation_to_close,
        profit_reduce_to_close_bars=profit_reduce_to_close_bars,
        profit_close_estimated_net_pnl=profit_close_estimated_net_pnl,
        profit_close_actual_net_pnl=profit_close_actual_net_pnl,
        profit_close_net_pnl_error=profit_close_net_pnl_error,
        profit_suppress_inventory_growth_usdt=max(
            0.0,
            profit_suppress_inventory_growth_usdt,
        ),
        profit_reduce_inventory_reduction_30_pct=(
            profit_reduce_inventory_reductions[30]
        ),
        profit_reduce_inventory_reduction_60_pct=(
            profit_reduce_inventory_reductions[60]
        ),
        profit_reduce_inventory_reduction_120_pct=(
            profit_reduce_inventory_reductions[120]
        ),
        profit_passive_reduce_reprice_count=(
            profit_passive_reduce_reprice_count
        ),
        profit_passive_reduce_fill_count=profit_passive_reduce_fill_count,
        profit_active_reduce_count=profit_active_reduce_count,
        profit_active_reduce_pnl=profit_active_reduce_pnl,
        profit_active_reduce_cost=profit_active_reduce_cost,
        profit_active_reduce_inventory_reduction_pct=(
            profit_active_reduce_inventory_reduction_pct
        ),
        volatility_breach_count=volatility_breach_count,
        volatility_max_consecutive_breaches=(
            volatility_max_consecutive_breaches
        ),
        volatility_reduce_count=volatility_reduce_count,
        volatility_reduce_pnl=volatility_reduce_pnl,
        volatility_reduce_cost=volatility_reduce_cost,
        volatility_reduce_inventory_reduction_pct=(
            volatility_reduce_inventory_reduction_pct
        ),
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
    if config.quantity_step_size < 0:
        raise ValueError("quantity_step_size不能为负。")
    if config.wind_down_bars < 0:
        raise ValueError("wind_down_bars不能为负。")
    if config.inventory_wind_down_bars < 0:
        raise ValueError("inventory_wind_down_bars不能为负。")
    inventory_wind_down_utilization = _finite_float(
        config.inventory_wind_down_utilization,
        "inventory_wind_down_utilization",
    )
    if config.inventory_wind_down_bars == 0:
        if inventory_wind_down_utilization != 0:
            raise ValueError("未启用库存条件 wind-down 时利用率阈值必须为0。")
        if config.inventory_wind_down_only_when_losing:
            raise ValueError("仅亏损触发需要先启用库存条件 wind-down。")
    else:
        if config.wind_down_bars <= 0:
            raise ValueError("库存条件 wind-down 需要固定 wind_down_bars 兜底。")
        if config.inventory_wind_down_bars <= config.wind_down_bars:
            raise ValueError("库存条件 wind-down 必须早于固定 wind-down。")
        if not (
            0
            < inventory_wind_down_utilization
            < config.inventory_critical_utilization
        ):
            raise ValueError("库存条件 wind-down 利用率阈值无效。")
        if config.max_inventory_notional <= 0:
            raise ValueError("库存条件 wind-down 需要正的库存上限。")
    if config.wind_down_reprice_interval_bars < 0:
        raise ValueError("wind_down_reprice_interval_bars不能为负。")
    if config.wind_down_initial_offset_steps < 0:
        raise ValueError("wind_down_initial_offset_steps不能为负。")
    if (
        not isfinite(config.wind_down_urgency_exponent)
        or config.wind_down_urgency_exponent <= 0
    ):
        raise ValueError("wind_down_urgency_exponent必须为正的有限数。")
    if not 0 < config.wind_down_unwind_fraction <= 1:
        raise ValueError("wind_down_unwind_fraction必须在(0, 1]内。")
    if config.max_unpaired_lots_per_side < 0:
        raise ValueError("max_unpaired_lots_per_side不能为负。")
    if not 0 < config.reduce_target_step_fraction <= 1:
        raise ValueError("reduce_target_step_fraction必须在(0, 1]内。")
    if config.unpaired_lot_cap_enforcement not in {"INTRABAR", "BAR_BOUNDARY"}:
        raise ValueError(
            "unpaired_lot_cap_enforcement必须为INTRABAR或BAR_BOUNDARY。"
        )
    if config.max_inventory_notional < 0:
        raise ValueError("max_inventory_notional不能为负。")
    if not (
        0 < config.inventory_caution_utilization
        < config.inventory_critical_utilization
        <= 1
    ):
        raise ValueError("库存 CAUTION/CRITICAL 阈值无效。")
    _finite_float(config.funding_rate_per_bar, "funding_rate_per_bar")
    profit_mode = str(config.profit_protection_mode).strip().upper()
    if profit_mode not in {"OFF", "FIXED_CLOSE", "PEAK_DRAWDOWN"}:
        raise ValueError(
            "profit_protection_mode必须为OFF、FIXED_CLOSE或PEAK_DRAWDOWN。"
        )
    if config.profit_protection_enabled and profit_mode == "OFF":
        raise ValueError("利润保护启用时不能使用OFF模式。")
    if config.profit_protection_enabled and config.profit_activation_usdt <= 0:
        raise ValueError("profit_activation_usdt必须大于0。")
    ProfitProtectionConfig(
        activation_profit_usdt=config.profit_activation_usdt,
        enabled=(config.profit_protection_enabled and profit_mode != "OFF"),
        minimum_locked_profit_ratio=config.profit_minimum_locked_ratio,
        suppress_drawdown_pct=config.profit_suppress_drawdown_pct,
        reduce_drawdown_pct=config.profit_reduce_drawdown_pct,
        close_drawdown_pct=config.profit_close_drawdown_pct,
        estimated_exit_cost_rate=config.profit_estimated_exit_cost_rate,
    )
    if config.profit_passive_reduce_after_bars < 0:
        raise ValueError("profit_passive_reduce_after_bars不能为负。")
    if config.profit_active_reduce_after_bars < 0:
        raise ValueError("profit_active_reduce_after_bars不能为负。")
    if (
        config.profit_active_reduce_after_bars > 0
        and (
            config.profit_passive_reduce_after_bars <= 0
            or config.profit_active_reduce_after_bars
            <= config.profit_passive_reduce_after_bars
        )
    ):
        raise ValueError("主动利润减仓必须晚于已启用的被动减仓。")
    if not 0 < config.profit_passive_reduce_fraction <= 1:
        raise ValueError("profit_passive_reduce_fraction必须在(0, 1]内。")
    if not 0 < config.profit_active_reduce_fraction <= 1:
        raise ValueError("profit_active_reduce_fraction必须在(0, 1]内。")
    if config.volatility_reduce_expansion_ratio < 0:
        raise ValueError("volatility_reduce_expansion_ratio不能为负。")
    if config.volatility_reduce_after_breaches < 0:
        raise ValueError("volatility_reduce_after_breaches不能为负。")
    if (
        config.volatility_reduce_after_breaches > 0
        and config.volatility_reduce_expansion_ratio <= 1.0
    ):
        raise ValueError("启用波动减仓时扩张比阈值必须大于1。")
    if not 0 < config.volatility_reduce_fraction <= 1:
        raise ValueError("volatility_reduce_fraction必须在(0, 1]内。")
    if str(config.volatility_reduce_mode).strip().upper() not in {
        "BOTH",
        "WORST_SIDE",
    }:
        raise ValueError("volatility_reduce_mode必须为BOTH或WORST_SIDE。")
    if config.volatility_resume_after_normal_bars < 0:
        raise ValueError("volatility_resume_after_normal_bars不能为负。")
    if (
        config.volatility_resume_after_normal_bars > 0
        and not config.volatility_wind_down_after_reduce
    ):
        raise ValueError("波动恢复需要先启用减仓后只减不增。")


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


def _initial_orders_from_plan(
    plan: tuple[InitialGridOrderPlan, ...],
    mode: GridDirectionMode,
    seed_entry_price: float | None,
) -> list[_BacktestOrder]:
    orders: list[_BacktestOrder] = []
    for item in plan:
        position_side, intent, entry_price = _initial_order_metadata(
            mode,
            item.side,
            seed_entry_price,
        )
        orders.append(
            _BacktestOrder(
                item.grid_index,
                item.side,
                item.price,
                item.qty,
                entry_price,
                position_side,
                intent,
            )
        )
    return orders


def _wind_down_reduce_orders(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
    *,
    remaining_bars: int,
    wind_down_bars: int,
    initial_offset_steps: float,
    urgency_exponent: float,
    step_pct: float,
    tick_size: float,
    grid_prices: list[float],
    quantity_step_size: float,
    unwind_fraction: float,
) -> list[_BacktestOrder]:
    """Create next-bar POST_ONLY reductions with urgency increasing toward close."""

    remaining_ratio = max(0.0, min(1.0, remaining_bars / max(1, wind_down_bars)))
    urgency_ratio = remaining_ratio ** urgency_exponent
    offset_pct = max(0.0, initial_offset_steps * step_pct * urgency_ratio)
    raw_sell = mark_price * (1.0 + offset_pct)
    raw_buy = mark_price * (1.0 - offset_pct)
    sell_price = _round_post_only_sell(raw_sell, mark_price, tick_size)
    buy_price = _round_post_only_buy(raw_buy, mark_price, tick_size)
    orders: list[_BacktestOrder] = []
    for index, (lot, adaptive_qty, patient_qty) in enumerate(
        _unwind_allocations(long_lots, unwind_fraction, quantity_step_size)
    ):
        patient_price = _round_post_only_sell(
            _grid_exit_target(grid_prices, lot.entry_price, OrderSide.SELL, step_pct),
            mark_price,
            tick_size,
        )
        orders.extend(_layered_reduce_orders(
            base_index=-1_000 - index * 2,
            side=OrderSide.SELL,
            adaptive_price=sell_price,
            patient_price=patient_price,
            adaptive_qty=adaptive_qty,
            patient_qty=patient_qty,
            entry_price=lot.entry_price,
            position_side="LONG",
        ))
    for index, (lot, adaptive_qty, patient_qty) in enumerate(
        _unwind_allocations(short_lots, unwind_fraction, quantity_step_size)
    ):
        patient_price = _round_post_only_buy(
            _grid_exit_target(grid_prices, lot.entry_price, OrderSide.BUY, step_pct),
            mark_price,
            tick_size,
        )
        orders.extend(_layered_reduce_orders(
            base_index=1_000 + index * 2,
            side=OrderSide.BUY,
            adaptive_price=buy_price,
            patient_price=patient_price,
            adaptive_qty=adaptive_qty,
            patient_qty=patient_qty,
            entry_price=lot.entry_price,
            position_side="SHORT",
        ))
    return orders


def _unwind_allocations(
    lots: list[_PositionLot],
    unwind_fraction: float,
    quantity_step_size: float,
) -> list[tuple[_PositionLot, float, float]]:
    total_qty = sum(lot.qty for lot in lots)
    adaptive_budget = _round_down_to_step(
        total_qty * unwind_fraction,
        quantity_step_size,
    )
    allocations: list[tuple[_PositionLot, float, float]] = []
    for lot in lots:
        adaptive = min(lot.qty, adaptive_budget)
        adaptive = _round_down_to_step(adaptive, quantity_step_size)
        patient = max(0.0, lot.qty - adaptive)
        adaptive_budget = max(0.0, adaptive_budget - adaptive)
        allocations.append((lot, adaptive, patient))
    return allocations


def _layered_reduce_orders(
    *,
    base_index: int,
    side: OrderSide,
    adaptive_price: float,
    patient_price: float,
    adaptive_qty: float,
    patient_qty: float,
    entry_price: float,
    position_side: str,
) -> list[_BacktestOrder]:
    if abs(adaptive_price - patient_price) <= 1e-12:
        return [_BacktestOrder(
            base_index,
            side,
            adaptive_price,
            adaptive_qty + patient_qty,
            entry_price,
            position_side,
            OrderIntent.REDUCE,
            True,
        )]
    orders = []
    if adaptive_qty > 1e-12:
        orders.append(_BacktestOrder(
            base_index,
            side,
            adaptive_price,
            adaptive_qty,
            entry_price,
            position_side,
            OrderIntent.REDUCE,
            True,
        ))
    if patient_qty > 1e-12:
        orders.append(_BacktestOrder(
            base_index - 1 if base_index < 0 else base_index + 1,
            side,
            patient_price,
            patient_qty,
            entry_price,
            position_side,
            OrderIntent.REDUCE,
            True,
        ))
    return orders


def _profit_passive_reduce_orders(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
    *,
    step_pct: float,
    tick_size: float,
    grid_prices: list[float],
    quantity_step_size: float,
    fraction: float,
) -> list[_BacktestOrder]:
    orders = _wind_down_reduce_orders(
        long_lots,
        short_lots,
        mark_price,
        remaining_bars=0,
        wind_down_bars=1,
        initial_offset_steps=0.0,
        urgency_exponent=1.0,
        step_pct=step_pct,
        tick_size=tick_size,
        grid_prices=grid_prices,
        quantity_step_size=quantity_step_size,
        unwind_fraction=fraction,
    )
    for order in orders:
        order.wind_down_reduce = False
        order.profit_reduce = True
    return orders


def _close_lot_fraction_at_market(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
    *,
    fraction: float,
    quantity_step_size: float,
    taker_fee_rate: float,
    slippage_bps: float,
    mode: str = "BOTH",
) -> list[tuple[OrderSide, str, float, float, float, float, float]]:
    slippage = max(0.0, slippage_bps) / 10_000
    exits: list[tuple[OrderSide, str, float, float, float, float, float]] = []
    positions = [
        (
            long_lots,
            OrderSide.SELL,
            "LONG",
            mark_price * (1 - slippage),
        ),
        (
            short_lots,
            OrderSide.BUY,
            "SHORT",
            mark_price * (1 + slippage),
        ),
    ]
    normalized_mode = str(mode).strip().upper()
    if normalized_mode == "WORST_SIDE":
        available = [item for item in positions if item[0]]
        if not available:
            return exits
        selected = min(
            available,
            key=lambda item: (
                sum(
                    (
                        mark_price - lot.entry_price
                        if item[2] == "LONG"
                        else lot.entry_price - mark_price
                    )
                    * lot.qty
                    for lot in item[0]
                ),
                item[2],
            ),
        )
        total_qty = sum(
            lot.qty
            for lots, _side, _position_side, _exit_price in positions
            for lot in lots
        )
        targets = [(*selected, total_qty * fraction)]
    else:
        targets = [
            (*item, sum(lot.qty for lot in item[0]) * fraction)
            for item in positions
        ]
    for lots, side, position_side, exit_price, raw_target_qty in targets:
        total_qty = sum(lot.qty for lot in lots)
        target_qty = _round_down_to_step(
            raw_target_qty,
            quantity_step_size,
        )
        if (
            target_qty <= 1e-12
            and quantity_step_size > 0
            and total_qty + 1e-12 >= quantity_step_size
        ):
            target_qty = quantity_step_size
        target_qty = min(total_qty, target_qty)
        if target_qty <= 1e-12:
            continue
        remaining = target_qty
        pnl = 0.0
        for lot in list(lots):
            consumed = min(lot.qty, remaining)
            if position_side == "LONG":
                pnl += (exit_price - lot.entry_price) * consumed
            else:
                pnl += (lot.entry_price - exit_price) * consumed
            lot.qty -= consumed
            remaining -= consumed
            if lot.qty <= 1e-12:
                lots.remove(lot)
            if remaining <= 1e-12:
                break
        closed_qty = target_qty - max(0.0, remaining)
        if closed_qty <= 1e-12:
            continue
        fee = exit_price * closed_qty * taker_fee_rate
        slippage_cost = mark_price * slippage * closed_qty
        exits.append((
            side,
            position_side,
            closed_qty,
            exit_price,
            pnl,
            fee,
            slippage_cost,
        ))
    return exits


def _trim_reduce_orders_to_inventory(
    orders: list[_BacktestOrder],
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
) -> list[_BacktestOrder]:
    """Cap resting reduce quantities after an out-of-band partial market exit."""

    budgets = {
        "LONG": [[lot.entry_price, lot.qty] for lot in long_lots],
        "SHORT": [[lot.entry_price, lot.qty] for lot in short_lots],
    }
    retained: list[_BacktestOrder] = []
    for order in orders:
        if order.order_intent != OrderIntent.REDUCE:
            retained.append(order)
            continue
        position_side = str(order.position_side or "").upper()
        if position_side not in budgets:
            position_side = "SHORT" if order.side == OrderSide.BUY else "LONG"
        available = budgets[position_side]
        match = next(
            (
                item
                for item in available
                if order.entry_price is not None
                and abs(float(item[0]) - order.entry_price) <= 1e-12
                and float(item[1]) > 1e-12
            ),
            None,
        )
        if match is None:
            continue
        quantity = min(order.qty, float(match[1]))
        if quantity <= 1e-12:
            continue
        order.qty = quantity
        match[1] = max(0.0, float(match[1]) - quantity)
        retained.append(order)
    return retained


def _grid_exit_target(
    grid_prices: list[float],
    entry_price: float,
    exit_side: OrderSide,
    step_pct: float,
) -> float:
    if grid_prices:
        entry_index = min(
            range(len(grid_prices)),
            key=lambda index: abs(grid_prices[index] - entry_price),
        )
        target_index = entry_index + (1 if exit_side == OrderSide.SELL else -1)
        if 0 <= target_index < len(grid_prices):
            return grid_prices[target_index]
    return entry_price * (
        1.0 + step_pct if exit_side == OrderSide.SELL else 1.0 - step_pct
    )


def _round_post_only_sell(value: float, mark_price: float, tick_size: float) -> float:
    tick = _positive_finite(tick_size, "tick_size")
    rounded = ceil(value / tick - 1e-12) * tick
    if rounded <= mark_price:
        rounded = (int(mark_price / tick + 1e-12) + 1) * tick
    return _positive_finite(rounded, "wind_down_sell_price")


def _round_post_only_buy(value: float, mark_price: float, tick_size: float) -> float:
    tick = _positive_finite(tick_size, "tick_size")
    rounded = _round_down_to_step(value, tick)
    if rounded >= mark_price:
        rounded = int(mark_price / tick - 1e-12) * tick
    return _positive_finite(rounded, "wind_down_buy_price")


def _round_down_to_step(value: float, step_size: float) -> float:
    if step_size <= 0:
        return value
    return int(value / step_size + 1e-12) * step_size


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
    if (
        config.stop_on_stop_loss
        and params.upper_stop_loss_price is not None
        and high >= params.upper_stop_loss_price
    ):
        return "stop_loss_upper", params.upper_stop_loss_price
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
        f"{seed}|{symbol}|{bar_index}|{_fill_identity(order)}".encode(),
        digest_size=8,
    ).digest()
    sample = int.from_bytes(digest, "big") / float(2**64 - 1)
    return sample < probability


def _fill_identity(order: _BacktestOrder) -> str:
    entry = "" if order.entry_price is None else f"{order.entry_price:.12g}"
    return "|".join((
        order.side.value,
        f"{order.price:.12g}",
        entry,
        order.position_side,
        order.order_intent.value,
        (
            "WIND_DOWN"
            if order.wind_down_reduce
            else "PROFIT_REDUCE"
            if order.profit_reduce
            else "GRID"
        ),
    ))


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
    bar_index: int,
) -> None:
    if order.entry_price is None:
        lots = long_lots if order.side == OrderSide.BUY else short_lots
        lots.append(_PositionLot(order.price, order.qty, bar_index))
        return
    lots = short_lots if order.side == OrderSide.BUY else long_lots
    _reduce_lot(lots, order.entry_price, order.qty)


def _unpaired_lot_limit_reached(
    order: _BacktestOrder,
    long_lot_count: int,
    short_lot_count: int,
    limit: int,
) -> bool:
    if limit <= 0 or order.order_intent != OrderIntent.OPEN:
        return False
    position_side = str(order.position_side or "").upper()
    if position_side == "LONG":
        return long_lot_count >= limit
    if position_side == "SHORT":
        return short_lot_count >= limit
    return False


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


def _next_order(
    params: GridParams,
    order: _BacktestOrder,
    *,
    reduce_target_step_fraction: float = 1.0,
    tick_size: float = 0.0,
) -> _BacktestOrder | None:
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
    next_price = params.grid_prices[next_index]
    if order.entry_price is None and reduce_target_step_fraction < 1:
        next_price = order.price + (
            next_price - order.price
        ) * reduce_target_step_fraction
        if tick_size > 0:
            next_price = (
                ceil(next_price / tick_size - 1e-12) * tick_size
                if next_side == OrderSide.SELL
                else _round_down_to_step(next_price, tick_size)
            )
    return _BacktestOrder(
        next_index,
        next_side,
        next_price,
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


def _gross_inventory_notional(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
) -> float:
    return (
        sum(lot.qty for lot in long_lots)
        + sum(lot.qty for lot in short_lots)
    ) * mark_price


def _market_exit_cost(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
    taker_fee_rate: float,
    slippage_bps: float,
) -> float:
    slippage = max(0.0, slippage_bps) / 10_000
    long_qty = sum(lot.qty for lot in long_lots)
    short_qty = sum(lot.qty for lot in short_lots)
    fee = (
        mark_price * (1 - slippage) * long_qty
        + mark_price * (1 + slippage) * short_qty
    ) * taker_fee_rate
    slippage_cost = mark_price * slippage * (long_qty + short_qty)
    return fee + slippage_cost


def _suppress_inventory_increasing_orders(
    open_orders: list[_BacktestOrder],
    *,
    net_qty: float,
    direction_mode: GridDirectionMode,
) -> tuple[list[_BacktestOrder], list[_BacktestOrder]]:
    if abs(net_qty) <= 1e-12:
        return open_orders, []
    increasing_side = OrderSide.BUY if net_qty > 0 else OrderSide.SELL
    retained: list[_BacktestOrder] = []
    cancelled: list[_BacktestOrder] = []
    for order in open_orders:
        suppress = (
            order.order_intent == OrderIntent.OPEN
            and order.entry_price is None
            and (
                order.side == increasing_side
                or direction_mode != GridDirectionMode.NEUTRAL
            )
        )
        (cancelled if suppress else retained).append(order)
    return retained, cancelled


def _oldest_lot_age_bars(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    bar_index: int,
) -> int:
    opened = [lot.opened_bar_index for lot in long_lots + short_lots]
    if not opened:
        return 0
    return max(0, bar_index - min(opened))


def _exit_inventory_snapshot(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    bar_index: int,
) -> tuple[int, float, float, float]:
    long_qty = sum(lot.qty for lot in long_lots)
    short_qty = sum(lot.qty for lot in short_lots)
    total_qty = long_qty + short_qty
    hedged_fraction = (
        2.0 * min(long_qty, short_qty) / total_qty
        if total_qty > 1e-12
        else 0.0
    )
    return (
        _oldest_lot_age_bars(long_lots, short_lots, bar_index),
        long_qty,
        short_qty,
        hedged_fraction,
    )


def _inventory_utilization(
    long_lots: list[_PositionLot],
    short_lots: list[_PositionLot],
    mark_price: float,
    max_inventory_notional: float,
    *,
    baseline_notional: float = 0.0,
) -> float:
    cap = _positive_finite(max_inventory_notional, "max_inventory_notional")
    long_qty = sum(lot.qty for lot in long_lots)
    short_qty = sum(lot.qty for lot in short_lots)
    net_notional = (long_qty - short_qty) * mark_price
    gross_notional = (long_qty + short_qty) * mark_price
    directional_notional = max(abs(net_notional), gross_notional * 0.5)
    baseline = min(max(0.0, baseline_notional), cap)
    incremental_notional = max(0.0, directional_notional - baseline)
    incremental_capacity = cap - baseline
    if incremental_capacity <= 1e-12:
        return 1.0 if incremental_notional > 1e-12 else 0.0
    return incremental_notional / incremental_capacity


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
