from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from datetime import datetime, timezone
from math import exp, floor, isfinite, log, sqrt
from statistics import pstdev
from typing import Any

from core.models import GridDirectionMode, GridParams
from strategy.grid_calculator import GridCalculationError, calculate_atr


ADAPTIVE_GRID_VERSION = "adaptive-grid-v2.1.2"


@dataclass(frozen=True)
class AdaptiveGridConfig:
    center_half_life_minutes: float = 30.0
    k_atr_range: float = 2.0
    k_sigma_range: float = 2.0
    max_range_pct: float = 0.03
    min_step_pct: float = 0.0015
    max_step_pct: float = 0.01
    k_atr_step: float = 0.50
    k_sigma_step: float = 0.80
    min_grid_num: int = 3
    max_grid_num: int = 20
    expansion_rate: float = 0.08
    stop_buffer_pct: float = 0.015
    adverse_selection_buffer_pct: float = 0.0005
    slippage_buffer_pct: float = 0.0005
    safety_margin_pct: float = 0.0005
    # 区间与格距共用同一波动率预测尺度：先在 horizon_bars 窗口上用同一估计器算出
    # 单根 Bar 波动率，再分别缩放到区间（乘 sqrt(horizon)）与格距（每根）。避免此前
    # 区间用 60 窗口、格距用 15 窗口造成的口径不一致（计划 §9.2）。
    horizon_bars: int = 60
    volatility_estimator: str = "ewma"


class GridEconomicsError(GridCalculationError):
    """网格几何可计算，但所有候选都没有正的手续费后边际。"""

    def __init__(self, message: str, *, economics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.economics = dict(economics or {})


class AdaptiveGridGenerator:
    def __init__(self, config: AdaptiveGridConfig | None = None) -> None:
        self.config = config or AdaptiveGridConfig()
        _validate_config(self.config)

    def generate(
        self,
        symbol: str,
        klines: list[dict[str, Any]],
        *,
        current_price: float,
        funding_rate: float,
        funding_cost_rate: float | None = None,
        maker_fee_rate: float,
        regime_score: float,
        capital: float | None = None,
        leverage: float = 1.0,
        tick_size: float = 0.0,
        step_size: float = 0.0,
        min_qty: float = 0.0,
        min_notional: float = 0.0,
        direction_mode: GridDirectionMode = GridDirectionMode.NEUTRAL,
        risk_budget: float | None = None,
        taker_fee_rate: float = 0.0,
        calculated_at: datetime | None = None,
    ) -> GridParams:
        config = self.config
        if len(klines) < 31:
            raise GridCalculationError("自适应网格K线样本不足。")
        closes = [_positive(row.get("close"), "close") for row in klines]
        highs = [_positive(row.get("high"), "high") for row in klines]
        lows = [_positive(row.get("low"), "low") for row in klines]
        price = _positive(current_price, "current_price")
        funding = _finite(funding_rate, "funding_rate")
        funding_cost = abs(funding) if funding_cost_rate is None else _non_negative(
            funding_cost_rate,
            "funding_cost_rate",
        )
        maker_fee = _non_negative(maker_fee_rate, "maker_fee_rate")
        score = _bounded(regime_score, 0.0, 100.0, "regime_score")
        sizing_enabled = capital is not None
        total_notional = (
            _positive(capital, "capital") * _positive(leverage, "leverage")
            if sizing_enabled
            else 0.0
        )
        price_tick = _non_negative(tick_size, "tick_size")
        quantity_step = _non_negative(step_size, "step_size")
        minimum_qty = _non_negative(min_qty, "min_qty")
        minimum_notional = _non_negative(min_notional, "min_notional")
        mode = (
            direction_mode
            if isinstance(direction_mode, GridDirectionMode)
            else GridDirectionMode(str(direction_mode).upper())
        )
        session_risk_budget = (
            _non_negative(risk_budget, "risk_budget")
            if risk_budget is not None
            else 0.0
        )
        taker_fee = _non_negative(taker_fee_rate, "taker_fee_rate")

        center = _ewma(closes, config.center_half_life_minutes)
        atr = calculate_atr(highs, lows, closes, 14)
        atr_pct = atr / center
        log_returns = [log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
        # 单根 Bar 波动率（同一估计器、同一 horizon 窗口），区间与格距都从它推导，
        # 避免此前区间用 60 根、格距用 15 根导致两者时间尺度不一致（§9.2）。
        horizon = max(1, min(config.horizon_bars, len(log_returns)))
        sigma_per_bar = _per_bar_volatility(
            log_returns,
            horizon,
            config.volatility_estimator,
            config.center_half_life_minutes,
        )
        sigma_horizon = sigma_per_bar * sqrt(horizon)
        ordered = sorted(closes)
        q_low = _quantile(ordered, 0.05)
        q_high = _quantile(ordered, 0.95)
        quantile_band = max(center - q_low, q_high - center) / center
        base_half_width_pct = max(
            config.k_atr_range * atr_pct,
            config.k_sigma_range * sigma_horizon,
            quantile_band,
            config.min_step_pct * 1.5,
        )
        regime_multiplier = 0.75 + 0.25 * (score / 100.0)
        base_half_width_pct *= regime_multiplier
        price_distance_pct = abs(price / center - 1.0)
        if price_distance_pct >= config.max_range_pct / 2:
            raise GridCalculationError("当前价格已漂移出自适应网格区间。")

        hard_cost = maker_fee * 2 + funding_cost
        risk_discount = (
            config.adverse_selection_buffer_pct
            + config.slippage_buffer_pct
            + config.safety_margin_pct
        )
        candidate_half_widths = _candidate_half_widths(
            base_half_width_pct,
            price_distance_pct,
            config.max_range_pct / 2,
        )
        evaluated: list[dict[str, Any]] = []
        feasible: list[dict[str, Any]] = []
        hours = max(len(klines) / 60.0, 1.0 / 60.0)
        for half_width_pct in candidate_half_widths:
            lower = center * (1.0 - half_width_pct)
            upper = center * (1.0 + half_width_pct)
            if not lower <= price <= upper:
                continue
            for grid_num in range(config.min_grid_num, config.max_grid_num + 1):
                grid_prices = _generate_expanding_prices(
                    center,
                    lower,
                    upper,
                    grid_num,
                    config.expansion_rate,
                )
                actual_step = _minimum_step_pct(grid_prices)
                fee_net_edge = actual_step - hard_cost
                crossings_per_hour = _estimated_crossings_per_hour(
                    highs,
                    lows,
                    grid_prices,
                    hours=hours,
                )
                inventory_penalty = 0.35 + 0.65 * grid_num / config.max_grid_num
                execution_penalty = 1.0 / max(crossings_per_hour + 1.0, 1.0)
                inventory_risk_discount = risk_discount * inventory_penalty
                execution_risk_discount = risk_discount * 0.25 * execution_penalty
                objective_value = (
                    crossings_per_hour * fee_net_edge
                    - inventory_risk_discount
                    - execution_risk_discount
                )
                qty_weights = _decreasing_level_weights(
                    grid_prices,
                    center,
                    config.expansion_rate,
                )
                sizing = _candidate_order_sizing(
                    grid_prices,
                    qty_weights,
                    current_price=price,
                    total_notional=total_notional,
                    tick_size=price_tick,
                    step_size=quantity_step,
                    min_qty=minimum_qty,
                    min_notional=minimum_notional,
                    configured_capital=float(capital or 0.0),
                    leverage=float(leverage),
                ) if sizing_enabled else {
                    "planned_order_count": max(0, len(grid_prices) - 1),
                    "planned_min_order_qty": None,
                    "planned_min_order_notional": None,
                    "minimum_order_qty": minimum_qty,
                    "minimum_order_notional": minimum_notional,
                    "sizing_rejected_reason": "",
                }
                stop_loss_price = lower * (1.0 - config.stop_buffer_pct)
                upper_stop_loss_price = upper * (1.0 + config.stop_buffer_pct)
                risk = _candidate_worst_case_risk(
                    sizing.get("_sized_orders", []),
                    current_price=price,
                    lower_stop=stop_loss_price,
                    upper_stop=upper_stop_loss_price,
                    direction_mode=mode,
                    taker_fee_rate=taker_fee,
                )
                seed_execution_cost_pct = (
                    risk["estimated_seed_fee"] / total_notional
                    if total_notional > 0
                    else 0.0
                )
                objective_value -= seed_execution_cost_pct
                rejected_reason = ""
                if actual_step > config.max_step_pct:
                    rejected_reason = "实际格距超过上限"
                elif fee_net_edge <= 0:
                    rejected_reason = "手续费后净边际不为正"
                elif sizing["sizing_rejected_reason"]:
                    rejected_reason = str(sizing["sizing_rejected_reason"])
                elif (
                    session_risk_budget > 0
                    and risk["worst_case_stop_loss"] > session_risk_budget
                ):
                    rejected_reason = "最坏止损损失超过会话风险预算"
                candidate = {
                    "lower": lower,
                    "upper": upper,
                    "grid_count": grid_num,
                    "level_count": grid_num + 1,
                    "gross_step_pct": actual_step,
                    "maker_fee_rate": maker_fee,
                    "maker_round_trip_pct": maker_fee * 2,
                    "projected_funding_pct": funding_cost,
                    "hard_cost_pct": hard_cost,
                    "fee_net_edge_pct": fee_net_edge,
                    "risk_discount_pct": risk_discount,
                    "inventory_risk_discount_pct": inventory_risk_discount,
                    "execution_risk_discount_pct": execution_risk_discount,
                    "estimated_crossings_per_hour": crossings_per_hour,
                    "objective_value": objective_value,
                    "seed_execution_cost_pct": seed_execution_cost_pct,
                    "taker_fee_rate": taker_fee,
                    "rejected_reason": rejected_reason,
                    "direction_mode": mode.value,
                    "risk_budget": session_risk_budget,
                    "grid_prices": grid_prices,
                    "qty_weights": qty_weights,
                    **sizing,
                    **risk,
                }
                evaluated.append(candidate)
                if not rejected_reason:
                    feasible.append(candidate)

        if not feasible:
            best = max(evaluated, key=_candidate_sort_key, default=None)
            economics = _public_economics(best, len(evaluated))
            reasons = {
                str(candidate["rejected_reason"])
                for candidate in evaluated
                if candidate["rejected_reason"]
            }
            if reasons and all("手续费后净边际" in reason for reason in reasons):
                message = "没有网格候选具备正的手续费后净边际。"
            elif reasons and all(
                "最小下单量" in reason or "最小名义金额" in reason
                for reason in reasons
            ):
                message = "没有网格候选满足交易所最小下单量或最小名义金额。"
            else:
                message = "没有网格候选同时满足经济性与交易所下单约束。"
            raise GridEconomicsError(
                message,
                economics=economics,
            )

        selected = max(feasible, key=_candidate_sort_key)
        lower = float(selected["lower"])
        upper = float(selected["upper"])
        grid_num = int(selected["grid_count"])
        grid_prices = list(selected["grid_prices"])
        actual_step = float(selected["gross_step_pct"])
        economics = _public_economics(selected, len(evaluated))
        qty_weights = tuple(float(value) for value in selected["qty_weights"])
        return GridParams(
            symbol=str(symbol).strip().upper(),
            upper=upper,
            lower=lower,
            center=center,
            grid_num=grid_num,
            step_pct=actual_step,
            grid_prices=grid_prices,
            baseline_atr=atr,
            stop_loss_price=lower * (1.0 - config.stop_buffer_pct),
            calculated_at=calculated_at or datetime.now(timezone.utc),
            volatility_method="adaptive_v2",
            volatility_value=max(atr_pct, sigma_horizon),
            volatility_window=len(klines),
            upper_stop_loss_price=upper * (1.0 + config.stop_buffer_pct),
            grid_mode="adaptive_v2",
            regime_score=score,
            cost_floor_pct=hard_cost,
            qty_weights=qty_weights,
            parameter_version=ADAPTIVE_GRID_VERSION,
            economics=economics,
            direction_mode=mode,
        )


def _candidate_half_widths(base: float, price_distance: float, maximum: float) -> list[float]:
    minimum = max(price_distance * 1.05, 1e-9)
    raw = [
        max(minimum, base * multiplier)
        for multiplier in (0.75, 1.0, 1.25, 1.5, 2.0)
    ]
    raw.append(maximum)
    return sorted({min(maximum, value) for value in raw if min(maximum, value) >= minimum})


def _estimated_crossings_per_hour(
    highs: list[float],
    lows: list[float],
    prices: list[float],
    *,
    hours: float,
) -> float:
    internal_levels = prices[1:-1]
    if not internal_levels:
        return 0.0
    crossings = sum(
        1
        for high, low in zip(highs, lows)
        for level in internal_levels
        if low <= level <= high
    )
    return crossings / len(internal_levels) / hours


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(candidate["objective_value"]),
        float(candidate["fee_net_edge_pct"]),
        -float(candidate["grid_count"]),
    )


def _public_economics(candidate: dict[str, Any] | None, evaluated_count: int) -> dict[str, Any]:
    if candidate is None:
        return {"evaluated_candidates": evaluated_count}
    return {
        key: value
        for key, value in candidate.items()
        if key not in {"grid_prices", "qty_weights", "_sized_orders"}
    } | {"evaluated_candidates": evaluated_count}


def _candidate_order_sizing(
    grid_prices: list[float],
    qty_weights: tuple[float, ...],
    *,
    current_price: float,
    total_notional: float,
    tick_size: float,
    step_size: float,
    min_qty: float,
    min_notional: float,
    configured_capital: float,
    leverage: float,
) -> dict[str, Any]:
    order_levels = [
        (index, _round_to_step(price, tick_size))
        for index, price in enumerate(grid_prices)
        if price != current_price
    ]
    selected_weight = sum(qty_weights[index] for index, _price in order_levels)
    if not order_levels or selected_weight <= 0:
        return {
            "planned_order_count": 0,
            "planned_min_order_qty": 0.0,
            "planned_min_order_notional": 0.0,
            "minimum_order_qty": min_qty,
            "minimum_order_notional": min_notional,
            "configured_capital": configured_capital,
            "minimum_required_capital": 0.0,
            "_sized_orders": [],
            "sizing_rejected_reason": "网格没有可提交的订单层",
        }
    sized_orders = [
        (
            price,
            _round_to_step(
                total_notional * (qty_weights[index] / selected_weight) / price,
                step_size,
            ),
        )
        for index, price in order_levels
    ]
    quantities = [qty for _price, qty in sized_orders]
    notionals = [price * qty for price, qty in sized_orders]
    rejected_reason = ""
    if any(qty <= 0 or qty < min_qty for qty in quantities):
        rejected_reason = "每格下单量小于交易所最小下单量"
    elif min_notional > 0 and any(notional < min_notional for notional in notionals):
        rejected_reason = "每格名义金额小于交易所最小名义金额"
    minimum_required_capital = 0.0
    if configured_capital > 0 and leverage > 0:
        required_scales: list[float] = []
        if min_qty > 0:
            required_scales.extend(
                min_qty / qty
                for qty in quantities
                if qty > 0
            )
        if min_notional > 0:
            required_scales.extend(
                min_notional / notional
                for notional in notionals
                if notional > 0
            )
        if required_scales:
            minimum_required_capital = (
                configured_capital * max(required_scales)
            )
    return {
        "planned_order_count": len(sized_orders),
        "planned_min_order_qty": min(quantities),
        "planned_min_order_notional": min(notionals),
        "minimum_order_qty": min_qty,
        "minimum_order_notional": min_notional,
        "configured_capital": configured_capital,
        "minimum_required_capital": minimum_required_capital,
        "_sized_orders": [
            {
                "index": index,
                "price": price,
                "qty": qty,
                "side": "BUY" if price < current_price else "SELL",
            }
            for (index, _rounded_price), (price, qty) in zip(order_levels, sized_orders)
        ],
        "sizing_rejected_reason": rejected_reason,
    }


def _candidate_worst_case_risk(
    sized_orders: list[dict[str, Any]],
    *,
    current_price: float,
    lower_stop: float,
    upper_stop: float,
    direction_mode: GridDirectionMode,
    taker_fee_rate: float,
) -> dict[str, float]:
    buys = [order for order in sized_orders if order["side"] == "BUY"]
    sells = [order for order in sized_orders if order["side"] == "SELL"]
    buy_qty = sum(float(order["qty"]) for order in buys)
    sell_qty = sum(float(order["qty"]) for order in sells)
    seed_qty = 0.0
    seed_fee = 0.0
    long_loss = 0.0
    short_loss = 0.0
    if direction_mode == GridDirectionMode.LONG:
        seed_qty = sell_qty
        seed_fee = seed_qty * current_price * taker_fee_rate
        long_loss = seed_qty * max(0.0, current_price - lower_stop)
        long_loss += sum(
            float(order["qty"]) * max(0.0, float(order["price"]) - lower_stop)
            for order in buys
        )
        close_fee = (seed_qty + buy_qty) * lower_stop * taker_fee_rate
    elif direction_mode == GridDirectionMode.SHORT:
        seed_qty = buy_qty
        seed_fee = seed_qty * current_price * taker_fee_rate
        short_loss = seed_qty * max(0.0, upper_stop - current_price)
        short_loss += sum(
            float(order["qty"]) * max(0.0, upper_stop - float(order["price"]))
            for order in sells
        )
        close_fee = (seed_qty + sell_qty) * upper_stop * taker_fee_rate
    else:
        long_loss = sum(
            float(order["qty"]) * max(0.0, float(order["price"]) - lower_stop)
            for order in buys
        )
        short_loss = sum(
            float(order["qty"]) * max(0.0, upper_stop - float(order["price"]))
            for order in sells
        )
        close_fee = (
            buy_qty * lower_stop + sell_qty * upper_stop
        ) * taker_fee_rate
    return {
        "seed_qty": seed_qty,
        "estimated_seed_fee": seed_fee,
        "estimated_close_fee": close_fee,
        "worst_long_loss": long_loss,
        "worst_short_loss": short_loss,
        "worst_case_stop_loss": long_loss + short_loss + seed_fee + close_fee,
    }


def _round_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    value_decimal = Decimal(str(value))
    step_decimal = Decimal(str(step))
    units = (value_decimal / step_decimal).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * step_decimal)


def _generate_expanding_prices(
    center: float,
    lower: float,
    upper: float,
    grid_num: int,
    expansion_rate: float,
) -> list[float]:
    exponent = 1.0 + expansion_rate
    log_center = log(center)
    lower_width = log_center - log(lower)
    upper_width = log(upper) - log_center
    prices: list[float] = []
    for index in range(grid_num + 1):
        normalized = 2.0 * index / grid_num - 1.0
        curved = (abs(normalized) ** exponent) * (-1.0 if normalized < 0 else 1.0)
        offset = curved * (lower_width if curved < 0 else upper_width)
        prices.append(exp(log_center + offset))
    prices[0] = lower
    prices[-1] = upper
    return prices


def _decreasing_level_weights(prices: list[float], center: float, expansion_rate: float) -> tuple[float, ...]:
    distances = [abs(log(price / center)) for price in prices]
    max_distance = max(distances) or 1.0
    raw = [1.0 / (1.0 + 4.0 * expansion_rate * distance / max_distance) for distance in distances]
    total = sum(raw)
    return tuple(value / total for value in raw)


def _minimum_step_pct(prices: list[float]) -> float:
    return min(prices[index] / prices[index - 1] - 1.0 for index in range(1, len(prices)))


def _per_bar_volatility(
    log_returns: list[float],
    horizon: int,
    estimator: str,
    half_life_minutes: float,
) -> float:
    """在统一 horizon 窗口内估计单根 Bar 的收益波动率（§9.2）。

    "std" 直接取窗口内对数收益的总体标准差；"ewma" 用半衰期加权，让近端 Bar
    权重更高。区间与格距都以该单根波动率乘以对应根数换算，时间尺度一致。
    """
    window = log_returns[-horizon:]
    if not window:
        return 0.0
    if estimator == "ewma":
        alpha = 1.0 - exp(log(0.5) / max(half_life_minutes, 1e-9))
        mean = window[0]
        variance = 0.0
        for value in window[1:]:
            variance = (1.0 - alpha) * (variance + alpha * (value - mean) ** 2)
            mean = alpha * value + (1.0 - alpha) * mean
        return sqrt(max(variance, 0.0))
    return pstdev(window)


def _ewma(values: list[float], half_life: float) -> float:
    alpha = 1.0 - exp(log(0.5) / half_life)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def _quantile(values: list[float], q: float) -> float:
    position = (len(values) - 1) * q
    lower_index = floor(position)
    upper_index = min(lower_index + 1, len(values) - 1)
    weight = position - lower_index
    return values[lower_index] * (1.0 - weight) + values[upper_index] * weight


def _validate_config(config: AdaptiveGridConfig) -> None:
    for label, value in (
        ("center_half_life_minutes", config.center_half_life_minutes),
        ("k_atr_range", config.k_atr_range),
        ("k_sigma_range", config.k_sigma_range),
        ("max_range_pct", config.max_range_pct),
        ("min_step_pct", config.min_step_pct),
        ("max_step_pct", config.max_step_pct),
        ("k_atr_step", config.k_atr_step),
        ("k_sigma_step", config.k_sigma_step),
    ):
        _positive(value, label)
    if config.max_step_pct < config.min_step_pct:
        raise ValueError("max_step_pct 不能小于 min_step_pct。")
    if config.min_grid_num < 1 or config.max_grid_num < config.min_grid_num:
        raise ValueError("自适应网格数量上下限无效。")
    if config.expansion_rate < 0:
        raise ValueError("expansion_rate 不能为负数。")
    if not 0 <= config.stop_buffer_pct < 1:
        raise ValueError("stop_buffer_pct 无效。")
    if config.horizon_bars < 2:
        raise ValueError("horizon_bars 至少为 2。")
    if config.volatility_estimator not in {"ewma", "std"}:
        raise ValueError("volatility_estimator 仅支持 ewma 或 std。")


def _bounded(value: Any, lower: float, upper: float, label: str) -> float:
    number = _finite(value, label)
    if not lower <= number <= upper:
        raise ValueError(f"{label} 超出允许范围。")
    return number


def _positive(value: Any, label: str) -> float:
    number = _finite(value, label)
    if number <= 0:
        raise ValueError(f"{label} 必须为正数。")
    return number


def _non_negative(value: Any, label: str) -> float:
    number = _finite(value, label)
    if number < 0:
        raise ValueError(f"{label} 必须为非负数。")
    return number


def _finite(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须为有限数。") from exc
    if not isfinite(number):
        raise ValueError(f"{label} 必须为有限数。")
    return number
