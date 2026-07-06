from __future__ import annotations

from datetime import datetime, timedelta, timezone

from strategy.cooldown import CooldownConfig, CooldownEvaluator


def _calm_klines(count: int = 30) -> list[dict[str, float]]:
    return [
        {"high": 100.01, "low": 99.99, "close": 100 + ((idx % 3) - 1) * 0.002}
        for idx in range(count)
    ]


def test_cooldown_rejects_before_minimum_wait() -> None:
    evaluator = CooldownEvaluator(CooldownConfig(min_calm_minutes=15))
    started = datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)

    decision = evaluator.evaluate(
        _calm_klines(),
        baseline_atr=1.0,
        min_step_pct=0.0015,
        cooldown_started_at=started,
        now=started + timedelta(minutes=10),
    )

    assert decision.can_reobserve is False
    assert "最短冷静期" in decision.reason


def test_cooldown_accepts_recovered_atr_and_narrow_amplitude() -> None:
    evaluator = CooldownEvaluator(CooldownConfig(min_calm_minutes=15))
    started = datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)

    decision = evaluator.evaluate(
        _calm_klines(),
        baseline_atr=1.0,
        min_step_pct=0.0015,
        cooldown_started_at=started,
        now=started + timedelta(minutes=20),
    )

    assert decision.can_reobserve is True
    assert decision.current_atr is not None
    assert decision.amplitude_pct is not None


def test_cooldown_rejects_large_amplitude() -> None:
    evaluator = CooldownEvaluator(CooldownConfig(min_calm_minutes=15))
    started = datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)
    volatile = [{"high": 102.0, "low": 98.0, "close": 100.0} for _ in range(30)]

    decision = evaluator.evaluate(
        volatile,
        baseline_atr=10.0,
        min_step_pct=0.0015,
        cooldown_started_at=started,
        now=started + timedelta(minutes=20),
    )

    assert decision.can_reobserve is False
    assert "振幅" in decision.reason


def test_cooldown_rejects_invalid_inputs_without_reobserve() -> None:
    evaluator = CooldownEvaluator(CooldownConfig(min_calm_minutes=15))
    started = datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)
    now = started + timedelta(minutes=20)

    invalid_cases = [
        (_calm_klines(), "nan", 0.0015, "ATR 基准值"),
        (_calm_klines(), 1.0, "inf", "最小网格间距"),
        ([{**row, "high": "nan"} if index == 0 else row for index, row in enumerate(_calm_klines())], 1.0, 0.0015, "K线数据异常"),
        ([{**row, "low": 0.0} if index == 0 else row for index, row in enumerate(_calm_klines())], 1.0, 0.0015, "K线数据异常"),
    ]

    for klines, baseline_atr, min_step_pct, reason in invalid_cases:
        decision = evaluator.evaluate(
            klines,
            baseline_atr=baseline_atr,  # type: ignore[arg-type]
            min_step_pct=min_step_pct,  # type: ignore[arg-type]
            cooldown_started_at=started,
            now=now,
        )

        assert decision.can_reobserve is False
        assert reason in decision.reason
