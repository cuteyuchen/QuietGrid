from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor, isfinite
from statistics import fmean, pstdev
from typing import Any, Iterable

from core.models import GridParams
from strategy.volatility import OHLC_VOLATILITY_METHODS, VolatilityCalculationError, estimate_ohlc_volatility


SUPPORTED_RANGE_METHODS = {"std", "quantile", *OHLC_VOLATILITY_METHODS}


@dataclass(frozen=True)
class GridConfig:
    range_method: str = "std"
    std_k: float = 1.8
    quantile_upper: float = 0.95
    quantile_lower: float = 0.05
    min_step_pct: float = 0.0015
    safety_multiplier: float = 3.5
    max_grid_num: int = 20
    max_range_pct: float = 0.05
    atr_period: int = 14
    stop_buffer_pct: float = 0.015
    min_samples: int = 30
    volatility_refresh_seconds: float = 60.0


class GridCalculationError(ValueError):
    pass


def calculate_grid_params(
    symbol: str,
    klines: Iterable[dict[str, Any]],
    current_price: float,
    funding_rate: float,
    config: GridConfig,
    calculated_at: datetime | None = None,
) -> GridParams:
    _validate_grid_config(config)
    current_price = _positive_value(current_price, "当前价格")
    funding_rate = _finite_value(funding_rate, "资金费率")
    rows = list(klines)
    if len(rows) < config.min_samples:
        raise GridCalculationError(f"K线样本不足: {len(rows)} < {config.min_samples}")

    closes = [_positive_number(row, "close") for row in rows]
    highs = [_positive_number(row, "high") for row in rows]
    lows = [_positive_number(row, "low") for row in rows]
    _validate_kline_price_relationships(highs, lows, closes)

    method = _normalized_range_method(config.range_method)
    center = fmean(closes)
    lower, upper, volatility_value = _range_from_prices(rows, closes, center, method, config)
    if lower <= 0 or upper <= lower:
        raise GridCalculationError("区间计算结果非法。")
    if not lower <= current_price <= upper:
        raise GridCalculationError("当前价格已漂移出计算区间。")

    range_pct = (upper - lower) / lower
    min_step_pct = max(abs(funding_rate) * config.safety_multiplier, config.min_step_pct)
    if range_pct < min_step_pct:
        raise GridCalculationError("区间宽度小于最小每格价差。")
    if range_pct > config.max_range_pct:
        raise GridCalculationError("区间宽度超过低波动策略上限。")

    grid_num = min(floor(range_pct / min_step_pct), config.max_grid_num)
    if grid_num < 1:
        raise GridCalculationError("可用网格数量小于 1。")

    step_pct = range_pct / grid_num
    grid_prices = [lower * ((1 + step_pct) ** index) for index in range(grid_num + 1)]
    baseline_atr = calculate_atr(highs, lows, closes, config.atr_period)
    stop_loss_price = lower * (1 - config.stop_buffer_pct)

    return GridParams(
        symbol=symbol,
        upper=upper,
        lower=lower,
        center=center,
        grid_num=grid_num,
        step_pct=step_pct,
        grid_prices=grid_prices,
        baseline_atr=baseline_atr,
        stop_loss_price=stop_loss_price,
        calculated_at=calculated_at or datetime.now(timezone.utc),
        volatility_method=method,
        volatility_value=volatility_value,
        volatility_window=len(rows),
    )


def calculate_volatility_metric(klines: Iterable[dict[str, Any]], config: GridConfig) -> tuple[str, float, int]:
    _validate_grid_config(config)
    rows = list(klines)
    if len(rows) < config.min_samples:
        raise GridCalculationError(f"K线样本不足: {len(rows)} < {config.min_samples}")
    closes = [_positive_number(row, "close") for row in rows]
    highs = [_positive_number(row, "high") for row in rows]
    lows = [_positive_number(row, "low") for row in rows]
    _validate_kline_price_relationships(highs, lows, closes)
    method = _normalized_range_method(config.range_method)
    _lower, _upper, volatility_value = _range_from_prices(rows, closes, fmean(closes), method, config)
    return method, volatility_value, len(rows)


def calculate_atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float:
    if len(highs) != len(lows) or len(lows) != len(closes):
        raise GridCalculationError("ATR 输入长度不一致。")
    if len(closes) < period + 1:
        raise GridCalculationError("ATR 样本不足。")

    true_ranges: list[float] = []
    for index in range(1, len(closes)):
        high = highs[index]
        low = lows[index]
        previous_close = closes[index - 1]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return fmean(true_ranges[-period:])


def _validate_kline_price_relationships(highs: list[float], lows: list[float], closes: list[float]) -> None:
    for high, low, close in zip(highs, lows, closes):
        if high < low or close < low or close > high:
            raise GridCalculationError("K线价格关系非法。")


def _validate_grid_config(config: GridConfig) -> None:
    _positive_value(config.std_k, "std_k")
    _positive_value(config.min_step_pct, "min_step_pct")
    _non_negative_value(config.safety_multiplier, "safety_multiplier")
    _positive_value(config.max_range_pct, "max_range_pct")
    if config.max_grid_num < 1:
        raise GridCalculationError("max_grid_num无效。")
    if config.atr_period < 1:
        raise GridCalculationError("atr_period无效。")
    if config.min_samples < 1:
        raise GridCalculationError("min_samples无效。")
    _positive_value(config.volatility_refresh_seconds, "volatility_refresh_seconds")
    stop_buffer_pct = _finite_value(config.stop_buffer_pct, "stop_buffer_pct")
    if stop_buffer_pct < 0 or stop_buffer_pct >= 1:
        raise GridCalculationError("stop_buffer_pct无效。")
    lower = _finite_value(config.quantile_lower, "分位数")
    upper = _finite_value(config.quantile_upper, "分位数")
    if not 0 <= lower < upper <= 1:
        raise GridCalculationError("分位数必须满足 0 <= lower < upper <= 1。")
    _normalized_range_method(config.range_method)


def _range_from_prices(
    rows: list[dict[str, Any]],
    closes: list[float],
    center: float,
    method: str,
    config: GridConfig,
) -> tuple[float, float, float]:
    if method == "std":
        sigma = pstdev(closes)
        return center - config.std_k * sigma, center + config.std_k * sigma, sigma / center
    if method == "quantile":
        lower = _quantile(closes, config.quantile_lower)
        upper = _quantile(closes, config.quantile_upper)
        return lower, upper, (upper - lower) / (2 * center)
    try:
        volatility_value = estimate_ohlc_volatility(rows, method)
    except VolatilityCalculationError as exc:
        raise GridCalculationError(str(exc)) from exc
    half_width_pct = config.std_k * volatility_value
    return center * (1 - half_width_pct), center * (1 + half_width_pct), volatility_value


def _normalized_range_method(method: str) -> str:
    normalized = str(method).strip().lower()
    if normalized not in SUPPORTED_RANGE_METHODS:
        raise GridCalculationError(f"不支持的区间算法: {method}")
    return normalized


def _quantile(values: list[float], q: float) -> float:
    if not 0 <= q <= 1:
        raise GridCalculationError("分位数必须在 0 到 1 之间。")
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lower = floor(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _positive_number(row: dict[str, Any], key: str) -> float:
    try:
        raw_value = row[key]
    except KeyError as exc:
        raise GridCalculationError(f"K线缺少有效字段: {key}") from exc
    value = _positive_value(raw_value, f"K线字段 {key}")
    return value


def _positive_value(raw_value: Any, label: str) -> float:
    value = _finite_value(raw_value, label)
    if value <= 0:
        raise GridCalculationError(f"{label}无效。")
    return value


def _non_negative_value(raw_value: Any, label: str) -> float:
    value = _finite_value(raw_value, label)
    if value < 0:
        raise GridCalculationError(f"{label}无效。")
    return value


def _finite_value(raw_value: Any, label: str) -> float:
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise GridCalculationError(f"{label}无效。") from exc
    if not isfinite(value):
        raise GridCalculationError(f"{label}无效。")
    return value
