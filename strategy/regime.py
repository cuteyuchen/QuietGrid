from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite, log
from statistics import pstdev
from typing import Any

from strategy.grid_calculator import GridCalculationError, calculate_atr


REGIME_MODEL_VERSION = "regime-rules-v2.2.0"
REGIME_FEATURE_VERSION = "regime-features-v2.1.0"


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
    soft_breach_limit: int = 3
    weights: RegimeWeights = RegimeWeights()
    # 中性网格只在低方向效率时入场。运行中阈值更宽，超限后作为软违约，
    # 由 Controller 连续确认后进入 DEFENSIVE，而不是单根 K 线立即市价止损。
    trend_filter_enabled: bool = True
    entry_max_directional_efficiency: float = 0.55
    running_max_directional_efficiency: float = 0.70
    # 没有事件数据 Provider 时不给 event 维度免费满分：置其权重为 0 并把权重
    # 重新归一化到其余维度（计划 §9.1）。接入事件源后再设为 True 启用 event。
    event_source_available: bool = False


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
    component_scores: dict[str, float | None]
    verdict: str
    threshold_used: float
    cost_breakdown: dict[str, float]
    effective_weights: dict[str, float]
    score_contributions: dict[str, float]
    event_source_available: bool
    features: FeatureSnapshot
    model_version: str = REGIME_MODEL_VERSION
    feature_version: str = REGIME_FEATURE_VERSION


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
        include_cost: bool = True,
        cost_breakdown: dict[str, float] | None = None,
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

        entry_trend_blocked = (
            config.trend_filter_enabled
            and not running
            and features.directional_efficiency > config.entry_max_directional_efficiency
        )
        if entry_trend_blocked:
            hard_blocks.append(
                "方向效率超过中性网格入场上限 "
                f"({features.directional_efficiency:.4f} > "
                f"{config.entry_max_directional_efficiency:.4f})"
            )
        running_trend_breached = (
            config.trend_filter_enabled
            and running
            and features.directional_efficiency > config.running_max_directional_efficiency
        )

        volatility_score = _volatility_score(
            features.volatility_expansion,
            config.max_vol_expansion_ratio,
        )
        # trend 只反映方向效率（低方向效率=更适合网格）；mean_reversion 只反映
        # 反转结构（穿越频率），不再共用 1 - directional_efficiency，避免重复计分。
        trend_score = _clamp(100.0 * (1.0 - features.directional_efficiency))
        spread_score = _clamp(100.0 * (1.0 - features.spread_pct / config.max_spread_pct))
        depth_score = _clamp(100.0 * features.depth_usdt / config.min_depth_usdt)
        liquidity_score = 0.55 * spread_score + 0.45 * depth_score
        mean_reversion_score = _mean_reversion_score(
            features.reversal_ratio,
            features.volatility_expansion,
        )
        provided_costs = dict(cost_breakdown or {})
        risk_discount_pct = _non_negative(
            provided_costs.get("risk_discount_pct", 0.0),
            "risk_discount_pct",
        )
        economic_cost_pct = _non_negative(cost_floor_pct, "cost_floor_pct") + risk_discount_pct
        cost_score = _cost_score(expected_step_pct, economic_cost_pct) if include_cost else None
        event_score = (
            0.0 if event_risk else 100.0
        ) if config.event_source_available else None
        component_scores = {
            "volatility": volatility_score,
            "trend": trend_score,
            "liquidity": liquidity_score,
            "mean_reversion": mean_reversion_score,
            "cost": cost_score,
            "event": event_score,
        }
        effective_weights = _effective_weights(config, include_cost=include_cost)
        score_contributions = {
            key: effective_weights[key] * float(score or 0.0)
            for key, score in component_scores.items()
        }
        grid_score = sum(score_contributions.values())
        threshold = config.stay_threshold if running else config.enter_threshold
        cost_blocked = (
            include_cost
            and _non_negative(cost_floor_pct, "cost_floor_pct")
            >= _positive(expected_step_pct, "expected_step_pct")
        )
        allowed = (
            not hard_blocks
            and not cost_blocked
            and not running_trend_breached
            and grid_score >= threshold
        )
        trend_threshold = (
            config.running_max_directional_efficiency
            if running
            else config.entry_max_directional_efficiency
        )
        state = _regime_state(features, hard_blocks, trend_threshold=trend_threshold)
        verdict = _regime_verdict(
            hard_blocks,
            allowed=allowed,
            include_cost=include_cost,
            expected_step_pct=expected_step_pct,
            cost_floor_pct=cost_floor_pct,
        )
        normalized_cost_breakdown = _normalized_cost_breakdown(
            expected_step_pct,
            cost_floor_pct,
            cost_score,
            cost_breakdown,
        )
        reasons_list = [
            f"网格适配度 {grid_score:.1f}，门槛 {threshold:.1f}",
            f"波动扩张比 {features.volatility_expansion:.2f}",
            f"方向效率 {features.directional_efficiency:.2f}，反转比例 {features.reversal_ratio:.2f}",
            f"点差 {features.spread_pct:.4%}，前档深度 {features.depth_usdt:.2f} USDT",
        ]
        if running_trend_breached:
            reasons_list.append(
                "运行中方向效率超过保持上限 "
                f"({features.directional_efficiency:.4f} > "
                f"{config.running_max_directional_efficiency:.4f})，"
                "计为软违约并等待连续确认。"
            )
        if include_cost:
            reasons_list.append(
                "计划格距 "
                f"{normalized_cost_breakdown['planned_step_pct']:.4%}，"
                "硬成本 "
                f"{normalized_cost_breakdown['hard_cost_pct']:.4%}，"
                "手续费后净边际 "
                f"{normalized_cost_breakdown['fee_net_edge_pct']:.4%}，"
                "风险折扣 "
                f"{normalized_cost_breakdown['risk_discount_pct']:.4%}"
            )
        return RegimeDecision(
            symbol=symbol,
            as_of=features.as_of,
            state=state,
            grid_score=grid_score,
            allowed=allowed,
            reasons=tuple(reasons_list),
            hard_blocks=tuple(hard_blocks),
            component_scores=component_scores,
            verdict=verdict,
            threshold_used=threshold,
            cost_breakdown=normalized_cost_breakdown,
            effective_weights=effective_weights,
            score_contributions=score_contributions,
            event_source_available=config.event_source_available,
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


def _regime_state(
    features: FeatureSnapshot,
    hard_blocks: list[str],
    *,
    trend_threshold: float = 0.70,
) -> str:
    if any("过期" in item for item in hard_blocks):
        return "UNKNOWN_DATA"
    if any("深度" in item or "价差" in item for item in hard_blocks):
        return "ILLIQUID"
    if any("事件" in item for item in hard_blocks):
        return "EVENT_RISK"
    if features.volatility_expansion > 2.0:
        return "VOLATILE"
    if features.directional_efficiency > trend_threshold:
        direction = "UP" if features.trend_direction > 0 else "DOWN"
        return f"TREND_{direction}"
    return "QUIET_RANGE"


def _regime_verdict(
    hard_blocks: list[str],
    *,
    allowed: bool,
    include_cost: bool,
    expected_step_pct: float,
    cost_floor_pct: float,
) -> str:
    if any("过期" in item for item in hard_blocks):
        return "BLOCKED_DATA"
    if any("方向效率超过中性网格入场上限" in item for item in hard_blocks):
        return "BLOCKED_TREND"
    if hard_blocks:
        return "BLOCKED_HARD"
    if include_cost and _non_negative(cost_floor_pct, "cost_floor_pct") >= _positive(
        expected_step_pct,
        "expected_step_pct",
    ):
        return "BLOCKED_ECONOMICS"
    return "ALLOWED" if allowed else "BLOCKED_SCORE"


def _mean_reversion_score(reversal_ratio: float, volatility_expansion: float) -> float:
    """均值回归分只由反转比例与波动收敛驱动，不再复用方向效率（§9.1）。

    reversal_ratio 越高说明短窗内价格频繁穿越、更适合网格；volatility_expansion
    低于 1（短窗波动收敛于长窗）再给一档加分，扩张时则扣分。
    """
    reversal_component = _clamp(100.0 * reversal_ratio)
    if volatility_expansion <= 1.0:
        convergence_component = 100.0
    else:
        convergence_component = _clamp(100.0 * (2.0 - volatility_expansion))
    return _clamp(0.7 * reversal_component + 0.3 * convergence_component)


def _effective_weights(config: RegimeConfig, *, include_cost: bool = True) -> dict[str, float]:
    """返回归一化后的实际权重。

    event 维度在没有事件 Provider 时会给出免费满分（event_score=100），这会拉高
    所有标的的分数。若 event_source_available 为 False，则把 event 权重置零并按比例
    重新分摊到其余维度，避免"无事件数据即加分"（计划 §9.1）。
    """
    weights = config.weights
    raw = {
        "volatility": weights.volatility,
        "trend": weights.trend,
        "liquidity": weights.liquidity,
        "mean_reversion": weights.mean_reversion,
        "cost": weights.cost,
        "event": weights.event,
    }
    if not config.event_source_available:
        raw["event"] = 0.0
    if not include_cost:
        raw["cost"] = 0.0
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("Regime 有效权重之和必须为正。")
    return {key: value / total for key, value in raw.items()}


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


def _normalized_cost_breakdown(
    expected_step_pct: float,
    cost_floor_pct: float,
    cost_score: float | None,
    provided: dict[str, float] | None,
) -> dict[str, float]:
    result = {
        str(key): _non_negative(value, str(key))
        for key, value in (provided or {}).items()
    }
    step = _positive(expected_step_pct, "expected_step_pct")
    hard_cost = _non_negative(cost_floor_pct, "cost_floor_pct")
    risk_discount = _non_negative(result.get("risk_discount_pct", 0.0), "risk_discount_pct")
    economic_cost = hard_cost + risk_discount
    result.update(
        {
            "planned_step_pct": step,
            "hard_cost_pct": hard_cost,
            "risk_discount_pct": risk_discount,
            "total_cost_pct": economic_cost,
            "fee_net_edge_pct": step - hard_cost,
            "risk_adjusted_net_edge_pct": step - economic_cost,
            "net_edge_pct": step - economic_cost,
        }
    )
    if cost_score is not None:
        result["cost_score"] = float(cost_score)
    return result


def _validate_config(config: RegimeConfig) -> None:
    if config.short_window < 3 or config.long_window <= config.short_window:
        raise ValueError("Regime 短长窗口配置无效。")
    if not 0 <= config.stay_threshold <= config.enter_threshold <= 100:
        raise ValueError("Regime 进入/保持阈值无效。")
    if config.soft_breach_limit < 1:
        raise ValueError("Regime 连续软违约次数必须至少为 1。")
    if not (
        0
        <= config.entry_max_directional_efficiency
        <= config.running_max_directional_efficiency
        <= 1
    ):
        raise ValueError(
            "Regime 方向效率阈值必须满足 "
            "0 <= entry_max_directional_efficiency <= "
            "running_max_directional_efficiency <= 1。"
        )
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
    # 权重在使用时按有效维度归一化（见 _effective_weights），因此不再强制和为 1，
    # 只要求非负且总和为正，方便配置里直接把 event 权重设为 0。
    if any(value < 0 for value in weight_values):
        raise ValueError("Regime 权重不能为负数。")
    if sum(weight_values) <= 0:
        raise ValueError("Regime 权重之和必须为正。")


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
