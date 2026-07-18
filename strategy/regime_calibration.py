"""Regime 分数校准报告（计划 §9.4）。

把每个回测窗口的 Regime 分数与其后续结果配对，按分数桶聚合，用于回答
"分数越高是否真的对应更好结果"。若高分桶未呈现更优表现，应据此调整权重或
取消对应评分维度，而不是让分数与结果脱节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from statistics import median
from typing import Sequence


DEFAULT_SCORE_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.0, 75.0),
    (75.0, 80.0),
    (80.0, 85.0),
    (85.0, 90.0),
    (90.0, 100.01),
)


@dataclass(frozen=True)
class WindowOutcome:
    """单个回测窗口的 Regime 分数与其实际结果。"""

    grid_score: float
    pnl: float
    stopped: bool = False
    max_adverse_excursion: float = 0.0
    inventory_critical: bool = False


@dataclass(frozen=True)
class ScoreBucketStats:
    lower: float
    upper: float
    window_count: int
    average_pnl: float
    median_pnl: float
    stop_rate: float
    max_adverse_excursion: float
    inventory_critical_rate: float

    @property
    def label(self) -> str:
        return f"{self.lower:.0f}-{self.upper:.0f}"

    def to_mapping(self) -> dict[str, object]:
        return {
            "bucket": self.label,
            "lower": self.lower,
            "upper": self.upper,
            "window_count": self.window_count,
            "average_pnl": self.average_pnl,
            "median_pnl": self.median_pnl,
            "stop_rate": self.stop_rate,
            "max_adverse_excursion": self.max_adverse_excursion,
            "inventory_critical_rate": self.inventory_critical_rate,
        }


@dataclass(frozen=True)
class RegimeCalibrationReport:
    buckets: tuple[ScoreBucketStats, ...]
    total_windows: int
    monotonic_pnl: bool
    warnings: tuple[str, ...] = field(default=())

    def to_mapping(self) -> dict[str, object]:
        return {
            "total_windows": self.total_windows,
            "monotonic_pnl": self.monotonic_pnl,
            "warnings": list(self.warnings),
            "buckets": [bucket.to_mapping() for bucket in self.buckets],
        }


def build_regime_calibration_report(
    outcomes: Sequence[WindowOutcome],
    *,
    buckets: Sequence[tuple[float, float]] = DEFAULT_SCORE_BUCKETS,
) -> RegimeCalibrationReport:
    """把窗口结果按分数桶聚合，并检查分数与平均 PnL 是否单调（越高越好）。"""
    if not buckets:
        raise ValueError("分数桶不能为空。")
    for lower, upper in buckets:
        if not (isfinite(lower) and isfinite(upper)) or upper <= lower:
            raise ValueError("分数桶区间无效。")

    grouped: list[list[WindowOutcome]] = [[] for _ in buckets]
    for outcome in outcomes:
        index = _bucket_index(outcome.grid_score, buckets)
        if index is not None:
            grouped[index].append(outcome)

    stats: list[ScoreBucketStats] = []
    for (lower, upper), members in zip(buckets, grouped):
        stats.append(_bucket_stats(lower, upper, members))

    populated = [bucket for bucket in stats if bucket.window_count > 0]
    monotonic = _is_non_decreasing([bucket.average_pnl for bucket in populated])
    warnings: list[str] = []
    if len(populated) >= 2 and not monotonic:
        warnings.append(
            "分数越高并未对应更高平均 PnL，建议复核 Regime 权重或取消无区分度的评分维度。"
        )
    return RegimeCalibrationReport(
        buckets=tuple(stats),
        total_windows=sum(bucket.window_count for bucket in stats),
        monotonic_pnl=monotonic,
        warnings=tuple(warnings),
    )


def _bucket_index(
    score: float,
    buckets: Sequence[tuple[float, float]],
) -> int | None:
    if not isfinite(score):
        return None
    for index, (lower, upper) in enumerate(buckets):
        if lower <= score < upper:
            return index
    return None


def _bucket_stats(
    lower: float,
    upper: float,
    members: list[WindowOutcome],
) -> ScoreBucketStats:
    if not members:
        return ScoreBucketStats(
            lower=lower,
            upper=upper,
            window_count=0,
            average_pnl=0.0,
            median_pnl=0.0,
            stop_rate=0.0,
            max_adverse_excursion=0.0,
            inventory_critical_rate=0.0,
        )
    pnls = [item.pnl for item in members]
    count = len(members)
    return ScoreBucketStats(
        lower=lower,
        upper=upper,
        window_count=count,
        average_pnl=sum(pnls) / count,
        median_pnl=median(pnls),
        stop_rate=sum(1 for item in members if item.stopped) / count,
        max_adverse_excursion=max(item.max_adverse_excursion for item in members),
        inventory_critical_rate=sum(1 for item in members if item.inventory_critical) / count,
    )


def _is_non_decreasing(values: list[float]) -> bool:
    return all(earlier <= later + 1e-9 for earlier, later in zip(values, values[1:]))
