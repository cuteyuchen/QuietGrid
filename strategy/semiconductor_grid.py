"""High-liquidity semiconductor grid profiles used by v2.7 research.

The strategy keeps the original exchange-closed-window thesis, but changes the
monetisation rule from "quiet means tradable" to "only open when the observed
path can support enough executable grid crossings".  Neutral and directional
profiles are deliberately separated so LONG results cannot be presented as
proof of the neutral strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite, log
from typing import Any, Mapping

from core.models import GridDirectionMode, GridParams
from strategy.adaptive_grid import AdaptiveGridConfig, AdaptiveGridGenerator
from strategy.grid_viability import (
    GridViabilityConfig,
    GridViabilityDecision,
    evaluate_grid_viability,
)


STRATEGY_VERSION = "semiconductor-grid-v2.7.0"
RESEARCH_SYMBOLS = (
    "SNDKUSDT",
    "MUUSDT",
    "SOXLUSDT",
    "SKHYNIXUSDT",
)


@dataclass(frozen=True)
class GridStrategyProfile:
    name: str
    direction_mode: GridDirectionMode
    min_grid_num: int
    max_grid_num: int
    min_step_pct: float
    requires_long_signal: bool = False

    def __post_init__(self) -> None:
        if self.min_grid_num < 1 or self.max_grid_num < self.min_grid_num:
            raise ValueError("网格数量必须满足 1 <= min_grid_num <= max_grid_num。")
        if not _positive(self.min_step_pct):
            raise ValueError("min_step_pct 必须为正数。")
        if self.requires_long_signal and self.direction_mode != GridDirectionMode.LONG:
            raise ValueError("requires_long_signal 只适用于 LONG profile。")


@dataclass(frozen=True)
class SymbolResearchProfile:
    symbol: str
    market_group: str
    calendar_name: str
    market_timezone: str
    reference_open_time: str | None
    capital_multiplier: float = 1.0
    allow_neutral: bool = True
    allow_long: bool = False
    assumed_spread_pct: float = 0.0002
    normal_min_step_pct: float = 0.0015
    dense_min_step_pct: float = 0.0006

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol 不能为空。")
        if not _positive(self.capital_multiplier):
            raise ValueError("capital_multiplier 必须为正数。")
        if not _non_negative(self.assumed_spread_pct):
            raise ValueError("assumed_spread_pct 不能为负数。")
        if not _positive(self.normal_min_step_pct):
            raise ValueError("normal_min_step_pct 必须为正数。")
        if not _positive(self.dense_min_step_pct):
            raise ValueError("dense_min_step_pct 必须为正数。")


@dataclass(frozen=True)
class LongSignalConfig:
    short_window: int = 15
    long_window: int = 60
    minimum_long_return_pct: float = 0.001
    minimum_short_return_pct: float = 0.0
    minimum_directional_efficiency: float = 0.10
    maximum_directional_efficiency: float = 0.65
    minimum_reversal_ratio: float = 0.20
    maximum_short_move_sigma: float = 3.0

    def __post_init__(self) -> None:
        if self.short_window < 2 or self.long_window <= self.short_window:
            raise ValueError("方向窗口必须满足 2 <= short < long。")
        if not 0 <= self.minimum_directional_efficiency <= self.maximum_directional_efficiency <= 1:
            raise ValueError("方向效率上下限必须满足 0 <= min <= max <= 1。")
        if not 0 <= self.minimum_reversal_ratio <= 1:
            raise ValueError("minimum_reversal_ratio 必须在 [0, 1] 内。")
        if not _positive(self.maximum_short_move_sigma):
            raise ValueError("maximum_short_move_sigma 必须为正数。")


@dataclass(frozen=True)
class LongSignalDecision:
    allowed: bool
    reasons: tuple[str, ...]
    long_return_pct: float
    short_return_pct: float
    directional_efficiency: float
    reversal_ratio: float
    short_move_sigma: float

    def to_mapping(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "long_return_pct": self.long_return_pct,
            "short_return_pct": self.short_return_pct,
            "directional_efficiency": self.directional_efficiency,
            "reversal_ratio": self.reversal_ratio,
            "short_move_sigma": self.short_move_sigma,
        }


@dataclass(frozen=True)
class SemiconductorGridCandidate:
    symbol: str
    profile: GridStrategyProfile
    params: GridParams
    viability: GridViabilityDecision
    long_signal: LongSignalDecision | None
    strategy_version: str = STRATEGY_VERSION


class StrategyAdmissionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        viability: GridViabilityDecision | None = None,
        long_signal: LongSignalDecision | None = None,
    ) -> None:
        super().__init__(message)
        self.viability = viability
        self.long_signal = long_signal


def default_grid_profiles() -> tuple[GridStrategyProfile, ...]:
    return (
        GridStrategyProfile(
            name="N20",
            direction_mode=GridDirectionMode.NEUTRAL,
            min_grid_num=3,
            max_grid_num=20,
            min_step_pct=0.0015,
        ),
        GridStrategyProfile(
            name="N100",
            direction_mode=GridDirectionMode.NEUTRAL,
            min_grid_num=20,
            max_grid_num=100,
            min_step_pct=0.0006,
        ),
        GridStrategyProfile(
            name="L20",
            direction_mode=GridDirectionMode.LONG,
            min_grid_num=3,
            max_grid_num=20,
            min_step_pct=0.0015,
            requires_long_signal=True,
        ),
        GridStrategyProfile(
            name="L100",
            direction_mode=GridDirectionMode.LONG,
            min_grid_num=20,
            max_grid_num=100,
            min_step_pct=0.0006,
            requires_long_signal=True,
        ),
    )


def profiles_from_mapping(raw: Mapping[str, Any]) -> tuple[GridStrategyProfile, ...]:
    result: list[GridStrategyProfile] = []
    for name, values in raw.items():
        spec = dict(values or {})
        result.append(
            GridStrategyProfile(
                name=str(name).strip().upper(),
                direction_mode=GridDirectionMode(
                    str(spec.get("direction_mode", "NEUTRAL")).strip().upper()
                ),
                min_grid_num=int(spec.get("min_grid_num", 3)),
                max_grid_num=int(spec.get("max_grid_num", 20)),
                min_step_pct=float(spec.get("min_step_pct", 0.0015)),
                requires_long_signal=bool(spec.get("requires_long_signal", False)),
            )
        )
    if not result:
        return default_grid_profiles()
    return tuple(result)


def symbol_profiles_from_mapping(
    raw: Mapping[str, Any],
) -> dict[str, SymbolResearchProfile]:
    result: dict[str, SymbolResearchProfile] = {}
    for raw_symbol, values in raw.items():
        symbol = str(raw_symbol).strip().upper()
        spec = dict(values or {})
        result[symbol] = SymbolResearchProfile(
            symbol=symbol,
            market_group=str(spec.get("market_group", "US_STOCK")),
            calendar_name=str(spec.get("calendar_name", "NYSE")),
            market_timezone=str(
                spec.get("market_timezone", "America/New_York")
            ),
            reference_open_time=(
                None
                if spec.get("reference_open_time") in (None, "", "null")
                else str(spec.get("reference_open_time"))
            ),
            capital_multiplier=float(spec.get("capital_multiplier", 1.0)),
            allow_neutral=bool(spec.get("allow_neutral", True)),
            allow_long=bool(spec.get("allow_long", False)),
            assumed_spread_pct=float(spec.get("assumed_spread_pct", 0.0002)),
            normal_min_step_pct=float(spec.get("normal_min_step_pct", 0.0015)),
            dense_min_step_pct=float(spec.get("dense_min_step_pct", 0.0006)),
        )
    return result


def long_signal_from_mapping(raw: Mapping[str, Any]) -> LongSignalConfig:
    return LongSignalConfig(
        short_window=int(raw.get("short_window", 15)),
        long_window=int(raw.get("long_window", 60)),
        minimum_long_return_pct=float(
            raw.get("minimum_long_return_pct", 0.001)
        ),
        minimum_short_return_pct=float(
            raw.get("minimum_short_return_pct", 0.0)
        ),
        minimum_directional_efficiency=float(
            raw.get("minimum_directional_efficiency", 0.10)
        ),
        maximum_directional_efficiency=float(
            raw.get("maximum_directional_efficiency", 0.65)
        ),
        minimum_reversal_ratio=float(
            raw.get("minimum_reversal_ratio", 0.20)
        ),
        maximum_short_move_sigma=float(
            raw.get("maximum_short_move_sigma", 3.0)
        ),
    )


def evaluate_long_signal(
    klines: list[dict[str, Any]],
    config: LongSignalConfig | None = None,
) -> LongSignalDecision:
    cfg = config or LongSignalConfig()
    if len(klines) < cfg.long_window + 1:
        raise ValueError(
            f"做多信号至少需要 {cfg.long_window + 1} 根 K 线。"
        )
    closes = [_positive_float(row.get("close"), "close") for row in klines]
    closes = closes[-(cfg.long_window + 1) :]
    returns = [log(closes[index] / closes[index - 1]) for index in range(1, len(closes))]
    short_returns = returns[-cfg.short_window :]
    long_return = closes[-1] / closes[0] - 1.0
    short_anchor = closes[-(cfg.short_window + 1)]
    short_return = closes[-1] / short_anchor - 1.0
    absolute_path = sum(abs(value) for value in returns)
    directional_efficiency = abs(sum(returns)) / max(absolute_path, 1e-12)
    reversal_count = sum(
        1
        for previous, current in zip(returns, returns[1:])
        if previous * current < 0
    )
    reversal_ratio = reversal_count / max(1, len(returns) - 1)
    mean_short = sum(short_returns) / len(short_returns)
    variance = sum((value - mean_short) ** 2 for value in short_returns) / len(short_returns)
    sigma = variance**0.5
    short_move_sigma = abs(sum(short_returns)) / max(sigma * len(short_returns) ** 0.5, 1e-12)

    reasons: list[str] = []
    if long_return < cfg.minimum_long_return_pct:
        reasons.append(
            "长窗收益不足 "
            f"({long_return:.4%} < {cfg.minimum_long_return_pct:.4%})"
        )
    if short_return < cfg.minimum_short_return_pct:
        reasons.append(
            "短窗收益不足 "
            f"({short_return:.4%} < {cfg.minimum_short_return_pct:.4%})"
        )
    if directional_efficiency < cfg.minimum_directional_efficiency:
        reasons.append(
            "方向效率不足以支持做多 "
            f"({directional_efficiency:.4f} < "
            f"{cfg.minimum_directional_efficiency:.4f})"
        )
    if directional_efficiency > cfg.maximum_directional_efficiency:
        reasons.append(
            "方向效率过高，可能处于单边加速 "
            f"({directional_efficiency:.4f} > "
            f"{cfg.maximum_directional_efficiency:.4f})"
        )
    if reversal_ratio < cfg.minimum_reversal_ratio:
        reasons.append(
            "趋势中回撤不足，不适合方向网格 "
            f"({reversal_ratio:.4f} < {cfg.minimum_reversal_ratio:.4f})"
        )
    if short_move_sigma > cfg.maximum_short_move_sigma:
        reasons.append(
            "短窗上涨过度加速 "
            f"({short_move_sigma:.3f} > {cfg.maximum_short_move_sigma:.3f}σ)"
        )

    return LongSignalDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        long_return_pct=long_return,
        short_return_pct=short_return,
        directional_efficiency=directional_efficiency,
        reversal_ratio=reversal_ratio,
        short_move_sigma=short_move_sigma,
    )


def build_semiconductor_grid_candidate(
    *,
    symbol_profile: SymbolResearchProfile,
    strategy_profile: GridStrategyProfile,
    klines: list[dict[str, Any]],
    current_price: float,
    funding_rate: float,
    projected_funding_pct: float,
    maker_fee_rate: float,
    regime_score: float,
    capital: float,
    leverage: float,
    tick_size: float,
    step_size: float,
    min_qty: float,
    min_notional: float,
    taker_fee_rate: float,
    base_grid_config: AdaptiveGridConfig,
    viability_config: GridViabilityConfig,
    long_signal_config: LongSignalConfig | None = None,
) -> SemiconductorGridCandidate:
    symbol = symbol_profile.symbol.upper()
    if symbol not in RESEARCH_SYMBOLS:
        raise StrategyAdmissionError(f"{symbol} 不在 v2.7 研究标的池。")
    if strategy_profile.direction_mode == GridDirectionMode.NEUTRAL:
        if not symbol_profile.allow_neutral:
            raise StrategyAdmissionError(f"{symbol} 未启用中性 profile。")
        long_signal = None
    elif strategy_profile.direction_mode == GridDirectionMode.LONG:
        if not symbol_profile.allow_long:
            raise StrategyAdmissionError(f"{symbol} 未启用做多 profile。")
        long_signal = evaluate_long_signal(klines, long_signal_config)
        if strategy_profile.requires_long_signal and not long_signal.allowed:
            raise StrategyAdmissionError(
                "做多信号未通过：" + "；".join(long_signal.reasons),
                long_signal=long_signal,
            )
    else:
        raise StrategyAdmissionError("v2.7 暂不启用 SHORT profile。")

    symbol_step = (
        symbol_profile.dense_min_step_pct
        if strategy_profile.max_grid_num > 20
        else symbol_profile.normal_min_step_pct
    )
    # A strategy profile is a lower bound shared by every symbol; the symbol
    # profile may widen it for products such as SOXL.  Never make a symbol
    # denser than either pre-registered limit.
    effective_step = max(strategy_profile.min_step_pct, symbol_step)
    effective_grid_config = replace(
        base_grid_config,
        min_grid_num=strategy_profile.min_grid_num,
        max_grid_num=strategy_profile.max_grid_num,
        min_step_pct=effective_step,
    )
    params = AdaptiveGridGenerator(effective_grid_config).generate(
        symbol,
        klines,
        current_price=current_price,
        funding_rate=funding_rate,
        funding_cost_rate=projected_funding_pct,
        maker_fee_rate=maker_fee_rate,
        regime_score=regime_score,
        capital=capital * symbol_profile.capital_multiplier,
        leverage=leverage,
        tick_size=tick_size,
        step_size=step_size,
        min_qty=min_qty,
        min_notional=min_notional,
        direction_mode=strategy_profile.direction_mode,
        taker_fee_rate=taker_fee_rate,
    )
    viability = evaluate_grid_viability(
        klines,
        grid_prices=params.grid_prices,
        step_pct=params.step_pct,
        spread_pct=symbol_profile.assumed_spread_pct,
        maker_fee_rate=maker_fee_rate,
        projected_funding_pct=projected_funding_pct,
        config=viability_config,
    )
    if not viability.allowed:
        raise StrategyAdmissionError(
            "Grid Viability Gate 未通过：" + "；".join(viability.reasons),
            viability=viability,
            long_signal=long_signal,
        )
    economics = dict(params.economics)
    economics.update(
        {
            "strategy_version": STRATEGY_VERSION,
            "strategy_profile": strategy_profile.name,
            "market_group": symbol_profile.market_group,
            "viability": viability.to_mapping(),
            "long_signal": long_signal.to_mapping() if long_signal else None,
        }
    )
    params = replace(
        params,
        direction_mode=strategy_profile.direction_mode,
        economics=economics,
    )
    return SemiconductorGridCandidate(
        symbol=symbol,
        profile=strategy_profile,
        params=params,
        viability=viability,
        long_signal=long_signal,
    )


def _positive_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须为正的有限数。") from exc
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{name} 必须为正的有限数。")
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
