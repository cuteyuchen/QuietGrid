"""Pre-trade activity gate for dense grid strategies.

The original QuietGrid admission model focuses on low directionality, liquidity,
and cost.  Dense grids also need a minimum rate of *tradable crossings*; a quiet
market with no volume is not automatically suitable.  This module keeps that
extra decision deterministic and reusable by research and runtime code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite, log
from typing import Any, Iterable


@dataclass(frozen=True)
class GridViabilityConfig:
    lookback_bars: int = 60
    bars_per_hour: float = 60.0
    min_crossings_per_hour: float = 1.0
    min_reversal_ratio: float = 0.25
    max_zero_activity_ratio: float = 0.20
    min_trade_count_per_hour: float = 0.0
    min_quote_volume_per_hour: float = 0.0
    max_spread_to_step_ratio: float = 0.50
    min_net_capacity_per_hour: float = 0.00025

    def __post_init__(self) -> None:
        if self.lookback_bars < 3:
            raise ValueError("lookback_bars 至少为 3。")
        if not _positive(self.bars_per_hour):
            raise ValueError("bars_per_hour 必须为正数。")
        if not _non_negative(self.min_crossings_per_hour):
            raise ValueError("min_crossings_per_hour 不能为负数。")
        if not _ratio(self.min_reversal_ratio):
            raise ValueError("min_reversal_ratio 必须在 [0, 1] 内。")
        if not _ratio(self.max_zero_activity_ratio):
            raise ValueError("max_zero_activity_ratio 必须在 [0, 1] 内。")
        if not _non_negative(self.min_trade_count_per_hour):
            raise ValueError("min_trade_count_per_hour 不能为负数。")
        if not _non_negative(self.min_quote_volume_per_hour):
            raise ValueError("min_quote_volume_per_hour 不能为负数。")
        if not _non_negative(self.max_spread_to_step_ratio):
            raise ValueError("max_spread_to_step_ratio 不能为负数。")
        if not _non_negative(self.min_net_capacity_per_hour):
            raise ValueError("min_net_capacity_per_hour 不能为负数。")


@dataclass(frozen=True)
class GridViabilitySnapshot:
    bars: int
    hours: float
    internal_level_count: int
    crossings_per_hour: float
    reversal_ratio: float
    directional_efficiency: float
    zero_activity_ratio: float
    trade_count_per_hour: float
    quote_volume_per_hour: float
    spread_to_step_ratio: float
    gross_step_pct: float
    hard_cost_pct: float
    net_edge_pct: float
    net_capacity_per_hour: float


@dataclass(frozen=True)
class GridViabilityDecision:
    allowed: bool
    reasons: tuple[str, ...]
    snapshot: GridViabilitySnapshot
    version: str = "grid-viability-v2.7.0"

    def to_mapping(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "version": self.version,
            "snapshot": asdict(self.snapshot),
        }


def evaluate_grid_viability(
    klines: Iterable[dict[str, Any]],
    *,
    grid_prices: Iterable[float],
    step_pct: float,
    spread_pct: float,
    maker_fee_rate: float,
    projected_funding_pct: float = 0.0,
    config: GridViabilityConfig | None = None,
) -> GridViabilityDecision:
    cfg = config or GridViabilityConfig()
    rows = list(klines)[-cfg.lookback_bars :]
    if len(rows) < 3:
        raise ValueError("网格可交易性评估至少需要 3 根 K 线。")

    step = _finite_non_negative(step_pct, "step_pct")
    if step <= 0:
        raise ValueError("step_pct 必须大于 0。")
    spread = _finite_non_negative(spread_pct, "spread_pct")
    maker_fee = _finite_non_negative(maker_fee_rate, "maker_fee_rate")
    funding = _finite_non_negative(
        projected_funding_pct,
        "projected_funding_pct",
    )

    prices = [_positive_float(value, "grid_price") for value in grid_prices]
    if len(prices) < 3:
        raise ValueError("grid_prices 至少需要 3 个价位。")
    if prices != sorted(prices) or len(set(prices)) != len(prices):
        raise ValueError("grid_prices 必须严格递增。")
    internal_levels = prices[1:-1]

    highs = [_positive_float(row.get("high"), "high") for row in rows]
    lows = [_positive_float(row.get("low"), "low") for row in rows]
    closes = [_positive_float(row.get("close"), "close") for row in rows]
    if any(high < low or not low <= close <= high for high, low, close in zip(highs, lows, closes)):
        raise ValueError("K 线价格关系非法。")

    hours = max(len(rows) / cfg.bars_per_hour, 1.0 / cfg.bars_per_hour)
    raw_crossings = sum(
        1
        for high, low in zip(highs, lows)
        for level in internal_levels
        if low <= level <= high
    )
    crossings_per_hour = raw_crossings / len(internal_levels) / hours

    returns = [log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    reversal_count = sum(
        1
        for previous, current in zip(returns, returns[1:])
        if previous * current < 0
    )
    reversal_ratio = reversal_count / max(1, len(returns) - 1)
    absolute_path = sum(abs(value) for value in returns)
    directional_efficiency = abs(sum(returns)) / max(absolute_path, 1e-12)

    zero_activity = 0
    total_trade_count = 0.0
    total_quote_volume = 0.0
    for row in rows:
        volume = _optional_non_negative(row.get("volume"))
        trade_count = _optional_non_negative(row.get("trade_count"))
        quote_volume = _optional_non_negative(row.get("quote_volume"))
        total_trade_count += trade_count
        total_quote_volume += quote_volume
        if volume <= 0 and trade_count <= 0:
            zero_activity += 1
    zero_activity_ratio = zero_activity / len(rows)
    trade_count_per_hour = total_trade_count / hours
    quote_volume_per_hour = total_quote_volume / hours

    spread_to_step_ratio = spread / step
    hard_cost_pct = 2 * maker_fee + funding
    net_edge_pct = max(0.0, step - hard_cost_pct)
    net_capacity_per_hour = crossings_per_hour * net_edge_pct

    snapshot = GridViabilitySnapshot(
        bars=len(rows),
        hours=hours,
        internal_level_count=len(internal_levels),
        crossings_per_hour=crossings_per_hour,
        reversal_ratio=reversal_ratio,
        directional_efficiency=directional_efficiency,
        zero_activity_ratio=zero_activity_ratio,
        trade_count_per_hour=trade_count_per_hour,
        quote_volume_per_hour=quote_volume_per_hour,
        spread_to_step_ratio=spread_to_step_ratio,
        gross_step_pct=step,
        hard_cost_pct=hard_cost_pct,
        net_edge_pct=net_edge_pct,
        net_capacity_per_hour=net_capacity_per_hour,
    )

    reasons: list[str] = []
    if crossings_per_hour < cfg.min_crossings_per_hour:
        reasons.append(
            "预计穿越频率不足 "
            f"({crossings_per_hour:.4f} < {cfg.min_crossings_per_hour:.4f}/h)"
        )
    if reversal_ratio < cfg.min_reversal_ratio:
        reasons.append(
            "反转比例不足 "
            f"({reversal_ratio:.4f} < {cfg.min_reversal_ratio:.4f})"
        )
    if zero_activity_ratio > cfg.max_zero_activity_ratio:
        reasons.append(
            "零成交分钟比例过高 "
            f"({zero_activity_ratio:.4f} > {cfg.max_zero_activity_ratio:.4f})"
        )
    if trade_count_per_hour < cfg.min_trade_count_per_hour:
        reasons.append(
            "每小时成交笔数不足 "
            f"({trade_count_per_hour:.2f} < {cfg.min_trade_count_per_hour:.2f})"
        )
    if quote_volume_per_hour < cfg.min_quote_volume_per_hour:
        reasons.append(
            "每小时成交额不足 "
            f"({quote_volume_per_hour:.2f} < {cfg.min_quote_volume_per_hour:.2f})"
        )
    if spread_to_step_ratio > cfg.max_spread_to_step_ratio:
        reasons.append(
            "点差占格距比例过高 "
            f"({spread_to_step_ratio:.4f} > {cfg.max_spread_to_step_ratio:.4f})"
        )
    if net_capacity_per_hour < cfg.min_net_capacity_per_hour:
        reasons.append(
            "预计费后循环容量不足 "
            f"({net_capacity_per_hour:.6f} < "
            f"{cfg.min_net_capacity_per_hour:.6f}/h)"
        )

    return GridViabilityDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        snapshot=snapshot,
    )


def _optional_non_negative(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if isfinite(number) and number >= 0 else 0.0


def _positive_float(value: Any, name: str) -> float:
    number = _finite_float(value, name)
    if number <= 0:
        raise ValueError(f"{name} 必须为正数。")
    return number


def _finite_non_negative(value: Any, name: str) -> float:
    number = _finite_float(value, name)
    if number < 0:
        raise ValueError(f"{name} 不能为负数。")
    return number


def _finite_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须为有限数。") from exc
    if not isfinite(number):
        raise ValueError(f"{name} 必须为有限数。")
    return number


def _positive(value: Any) -> bool:
    try:
        return isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _non_negative(value: Any) -> bool:
    try:
        return isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False


def _ratio(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and 0 <= number <= 1
