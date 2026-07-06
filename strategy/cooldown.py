from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from strategy.grid_calculator import GridCalculationError, calculate_atr


@dataclass(frozen=True)
class CooldownConfig:
    atr_period: int = 14
    calm_window_minutes: int = 30
    atr_recovery_ratio: float = 0.80
    amplitude_multiplier: float = 2.0
    min_calm_minutes: int = 15


@dataclass(frozen=True)
class CooldownDecision:
    can_reobserve: bool
    reason: str
    current_atr: float | None = None
    amplitude_pct: float | None = None


class CooldownEvaluator:
    def __init__(self, config: CooldownConfig) -> None:
        self.config = config

    def evaluate(
        self,
        klines: list[dict[str, Any]],
        baseline_atr: float,
        min_step_pct: float,
        cooldown_started_at: datetime,
        now: datetime | None = None,
    ) -> CooldownDecision:
        if not _positive_finite(baseline_atr):
            return CooldownDecision(False, "ATR 基准值异常。")
        if not _positive_finite(min_step_pct):
            return CooldownDecision(False, "最小网格间距异常。")

        current_time = now or datetime.now(timezone.utc)
        elapsed_minutes = (current_time - cooldown_started_at).total_seconds() / 60
        if elapsed_minutes < self.config.min_calm_minutes:
            return CooldownDecision(False, "未达到最短冷静期。")

        window = klines[-self.config.calm_window_minutes :]
        if len(window) < max(self.config.calm_window_minutes, self.config.atr_period + 1):
            return CooldownDecision(False, "冷静期样本不足。")

        try:
            highs = [_positive_float(row["high"]) for row in window]
            lows = [_positive_float(row["low"]) for row in window]
            closes = [_positive_float(row["close"]) for row in window]
        except (KeyError, TypeError, ValueError):
            return CooldownDecision(False, "冷静期K线数据异常。")
        try:
            current_atr = calculate_atr(highs, lows, closes, self.config.atr_period)
        except GridCalculationError as exc:
            return CooldownDecision(False, str(exc))

        if current_atr >= baseline_atr * self.config.atr_recovery_ratio:
            return CooldownDecision(False, "ATR 尚未回落。", current_atr=current_atr)

        high = max(highs)
        low = min(lows)
        center = sum(closes) / len(closes)
        amplitude_pct = (high - low) / center
        limit = min_step_pct * self.config.amplitude_multiplier
        if amplitude_pct >= limit:
            return CooldownDecision(False, "最近窗口振幅仍然过大。", current_atr=current_atr, amplitude_pct=amplitude_pct)

        return CooldownDecision(True, "ATR 和横盘条件已满足。", current_atr=current_atr, amplitude_pct=amplitude_pct)


def _positive_float(value: Any) -> float:
    number = float(value)
    if not isfinite(number) or number <= 0:
        raise ValueError("not a positive finite number")
    return number


def _positive_finite(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0
