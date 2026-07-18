from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp, floor, isfinite, log, sqrt
from statistics import pstdev
from typing import Any

from core.models import GridParams
from strategy.grid_calculator import GridCalculationError, calculate_atr


ADAPTIVE_GRID_VERSION = "adaptive-grid-v2.1.0"


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
    min_grid_num: int = 6
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
        maker_fee_rate: float,
        regime_score: float,
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
        maker_fee = _non_negative(maker_fee_rate, "maker_fee_rate")
        score = _bounded(regime_score, 0.0, 100.0, "regime_score")

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
        half_width_pct = max(
            config.k_atr_range * atr_pct,
            config.k_sigma_range * sigma_horizon,
            quantile_band,
            config.min_step_pct * config.min_grid_num / 2,
        )
        regime_multiplier = 0.75 + 0.25 * (score / 100.0)
        half_width_pct = min(config.max_range_pct / 2, half_width_pct * regime_multiplier)
        lower = center * (1.0 - half_width_pct)
        upper = center * (1.0 + half_width_pct)
        if not lower <= price <= upper:
            raise GridCalculationError("当前价格已漂移出自适应网格区间。")

        cost_floor = (
            maker_fee * 2
            + abs(funding)
            + config.adverse_selection_buffer_pct
            + config.slippage_buffer_pct
            + config.safety_margin_pct
        )
        step = max(
            cost_floor,
            config.k_atr_step * atr_pct,
            config.k_sigma_step * sigma_per_bar,
            config.min_step_pct,
        )
        step = min(step, config.max_step_pct)
        raw_count = floor(log(upper / lower) / log(1.0 + step))
        grid_num = min(config.max_grid_num, max(config.min_grid_num, raw_count))
        grid_prices = _generate_expanding_prices(
            center,
            lower,
            upper,
            grid_num,
            config.expansion_rate,
        )
        while grid_num > config.min_grid_num and _minimum_step_pct(grid_prices) < cost_floor:
            grid_num -= 1
            grid_prices = _generate_expanding_prices(
                center,
                lower,
                upper,
                grid_num,
                config.expansion_rate,
            )
        actual_step = _minimum_step_pct(grid_prices)
        if actual_step < cost_floor:
            raise GridCalculationError("自适应网格净价差低于成本地板。")
        qty_weights = _decreasing_level_weights(grid_prices, center, config.expansion_rate)
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
            cost_floor_pct=cost_floor,
            qty_weights=qty_weights,
            parameter_version=ADAPTIVE_GRID_VERSION,
        )


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
