from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, log
from statistics import pstdev
from typing import Any

from strategy.grid_calculator import GridCalculationError, calculate_atr


REGIME_MODEL_VERSION = "regime-rules-v2.0.0"


@dataclass(frozen=True)
class RegimeWeights:
    volatility: float = 0.25
    trend: float = 0.25
    liquidity: float = 0.20
    mean_reversion: float = 0.15
    cost: float = 0.10
    event: float = 0.05


@dataclass(frozen=True)
class RegimeConfig:
    short_window: int = 15
    long_window: int = 60
    enter_threshold: float = 75.0
    stay_threshold: float = 65.0
    max_data_age_seconds: float = 90.0
    max_spread_pct: float = 0.001
    max_vol_expansion_ratio: float = 2.5
    min_depth_usdt: float = 10_000.0
    weights: RegimeWeights = RegimeWeights()


@dataclass(frozen=True)
class FeatureSnapshot:
    symbol: str
    as_of: datetime
    close: float
    atr_pct: float
    volatility_short: float
    volatility_long: float
    volatility_expansion: float
    directional_efficiency: float
    trend_direction: int
    reversal_ratio: float
    spread_pct: float
    depth_usdt: float
    funding_rate: float
    data_age_seconds: float


@dataclass(frozen=True)
class RegimeDecision:
    symbol: str
    as_of: datetime
    state: str
    grid_score: float
    allowed: bool
    reasons: tuple[str, ...]
    hard_blocks: tuple[str, ...]
    component_scores: dict[str, float]
    features: FeatureSnapshot
    model_version: str = REGIME_MODEL_VERSION


class RegimeEngine:
    def __init__(self, config: RegimeConfig | None = None) -> None:
        self.config = config or RegimeConfig()
        _validate_config(self.config)

    def evaluate(
        self,
        symbol: str,
        klines: list[dict[str, Any]],
        *,
        spread_pct: float,
        depth_usdt: float,
        funding_rate: float = 0.0,
        data_age_seconds: float = 0.0,
        expected_step_pct: float = 0.0015,
        cost_floor_pct: float = 0.0,
        event_risk: bool = False,
        running: bool = False,
        as_of: datetime | None = None,
    ) -> RegimeDecision:
        features = self.calculate_features(
            symbol,
            klines,
            spread_pct=spread_pct,
            depth_usdt=depth_usdt,
            funding_rate=funding_rate,
            data_age_seconds=data_age_seconds,
            as_of=as_of,
        )
        config = self.config
        hard_blocks: list[str] = []
        if features.data_age_seconds > config.max_data_age_seconds:
            hard_blocks.append("行情数据已过期")
        if features.spread_pct > config.max_spread_pct:
            hard_blocks.append("买卖价差超过硬上限")
        if features.depth_usdt < config.min_depth_usdt:
            hard_blocks.append("订单簿深度低于最低要求")
        if features.volatility_expansion > config.max_vol_expansion_ratio:
            hard_blocks.append("短窗波动率快速扩张")
        if event_risk:
            hard_blocks.append("存在事件风险硬阻断")

        volatility_score = _volatility_score(
            features.volatility_expansion,
            config.max_vol_expansion_ratio,
        )
        trend_score = _clamp(100.0 * (1.0 - features.directional_efficiency))
        spread_score = _clamp(100.0 * (1.0 - features.spread_pct / config.max_spread_pct))
        depth_score = _clamp(100.0 * features.depth_usdt / config.min_depth_usdt)
        liquidity_score = 0.55 * spread_score + 0.45 * depth_score
        mean_reversion_score = _clamp(
            55.0 * features.reversal_ratio
            + 45.0 * (1.0 - features.directional_efficiency)
        )
        cost_score = _cost_score(expected_step_pct, cost_floor_pct)
        event_score = 0.0 if event_risk else 100.0
        component_scores = {
            "volatility": volatility_score,
            "trend": trend_score,
            "liquidity": liquidity_score,
            "mean_reversion": mean_reversion_score,
            "cost": cost_score,
            "event": event_score,
        }
        weights = config.weights
        grid_score = sum(
            (
                weights.volatility * volatility_score,
                weights.trend * trend_score,
                weights.liquidity * liquidity_score,
                weights.mean_reversion * mean_reversion_score,
                weights.cost * cost_score,
                weights.event * event_score,
            )
        )
        threshold = config.stay_threshold if running else config.enter_threshold
        allowed = not hard_blocks and grid_score >= threshold
        state = _regime_state(features, hard_blocks, allowed)
        reasons = (
            f"网格适配度 {grid_score:.1f}，门槛 {threshold:.1f}",
            f"波动扩张比 {features.volatility_expansion:.2f}",
            f"方向效率 {features.directional_efficiency:.2f}，反转比例 {features.reversal_ratio:.2f}",
            f"点差 {features.spread_pct:.4%}，前档深度 {features.depth_usdt:.2f} USDT",
        )
        return RegimeDecision(
            symbol=symbol,
            as_of=features.as_of,
            state=state,
            grid_score=grid_score,
            allowed=allowed,
            reasons=reasons,
            hard_blocks=tuple(hard_blocks),
            component_scores=component_scores,
            features=features,
        )

    def calculate_features(
        self,
        symbol: str,
        klines: list[dict[str, Any]],
        *,
        spread_pct: float,
        depth_usdt: float,
        funding_rate: float = 0.0,
        data_age_seconds: float = 0.0,
        as_of: datetime | None = None,
    ) -> FeatureSnapshot:
        config = self.config
        if len(klines) < config.long_window + 1:
            raise ValueError(f"Regime K线样本不足: {len(klines)} < {config.long_window + 1}")
        closes = [_positive(row.get("close"), "close") for row in klines]
        highs = [_positive(row.get("high"), "high") for row in klines]
        lows = [_positive(row.get("low"), "low") for row in klines]
        returns = [log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
        short_returns = returns[-config.short_window :]
        long_returns = returns[-config.long_window :]
        volatility_short = pstdev(short_returns)
        volatility_long = pstdev(long_returns)
        volatility_expansion = volatility_short / max(volatility_long, 1e-12)
        absolute_path = sum(abs(value) for value in short_returns)
        directional_efficiency = abs(sum(short_returns)) / max(absolute_path, 1e-12)
        reversal_count = sum(
            1
            for previous, current in zip(short_returns, short_returns[1:])
            if previous * current < 0
        )
        reversal_ratio = reversal_count / max(1, len(short_returns) - 1)
        try:
            atr = calculate_atr(highs, lows, closes, min(14, config.long_window - 1))
        except GridCalculationError as exc:
            raise ValueError(str(exc)) from exc
        return FeatureSnapshot(
            symbol=str(symbol).strip().upper(),
            as_of=as_of or datetime.now(timezone.utc),
            close=closes[-1],
            atr_pct=atr / closes[-1],
            volatility_short=volatility_short,
            volatility_long=volatility_long,
            volatility_expansion=volatility_expansion,
            directional_efficiency=directional_efficiency,
            trend_direction=1 if sum(short_returns) > 0 else -1 if sum(short_returns) < 0 else 0,
            reversal_ratio=reversal_ratio,
            spread_pct=_non_negative(spread_pct, "spread_pct"),
            depth_usdt=_non_negative(depth_usdt, "depth_usdt"),
            funding_rate=_finite(funding_rate, "funding_rate"),
            data_age_seconds=_non_negative(data_age_seconds, "data_age_seconds"),
        )


def _regime_state(features: FeatureSnapshot, hard_blocks: list[str], allowed: bool) -> str:
    if any("过期" in item for item in hard_blocks):
        return "UNKNOWN"
    if any("深度" in item or "价差" in item for item in hard_blocks):
        return "ILLIQUID"
    if any("事件" in item for item in hard_blocks):
        return "EVENT_RISK"
    if features.volatility_expansion > 2.0:
        return "VOLATILE"
    if features.directional_efficiency > 0.70:
        direction = "UP" if features.trend_direction > 0 else "DOWN"
        return f"TREND_{direction}"
    return "QUIET_RANGE" if allowed else "UNKNOWN"


def _volatility_score(expansion: float, hard_limit: float) -> float:
    if expansion <= 1.0:
        return 100.0
    return _clamp(100.0 * (hard_limit - expansion) / max(hard_limit - 1.0, 1e-12))


def _cost_score(expected_step_pct: float, cost_floor_pct: float) -> float:
    step = _positive(expected_step_pct, "expected_step_pct")
    cost = _non_negative(cost_floor_pct, "cost_floor_pct")
    if cost >= step:
        return 0.0
    return _clamp(100.0 * (step - cost) / step)


def _validate_config(config: RegimeConfig) -> None:
    if config.short_window < 3 or config.long_window <= config.short_window:
        raise ValueError("Regime 短长窗口配置无效。")
    if not 0 <= config.stay_threshold <= config.enter_threshold <= 100:
        raise ValueError("Regime 进入/保持阈值无效。")
    _positive(config.max_data_age_seconds, "max_data_age_seconds")
    _positive(config.max_spread_pct, "max_spread_pct")
    _positive(config.max_vol_expansion_ratio, "max_vol_expansion_ratio")
    _positive(config.min_depth_usdt, "min_depth_usdt")
    weight_values = (
        config.weights.volatility,
        config.weights.trend,
        config.weights.liquidity,
        config.weights.mean_reversion,
        config.weights.cost,
        config.weights.event,
    )
    if any(value < 0 for value in weight_values) or abs(sum(weight_values) - 1.0) > 1e-9:
        raise ValueError("Regime 权重之和必须为 1。")


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return min(upper, max(lower, value))


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
