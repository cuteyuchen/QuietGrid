from __future__ import annotations

import pytest

from strategy.regime_calibration import (
    WindowOutcome,
    build_regime_calibration_report,
)


def test_calibration_groups_outcomes_into_score_buckets() -> None:
    outcomes = [
        WindowOutcome(grid_score=76.0, pnl=1.0),
        WindowOutcome(grid_score=78.0, pnl=3.0),
        WindowOutcome(grid_score=88.0, pnl=5.0),
        WindowOutcome(grid_score=95.0, pnl=9.0),
    ]

    report = build_regime_calibration_report(outcomes)

    assert report.total_windows == 4
    by_label = {bucket.label: bucket for bucket in report.buckets}
    assert by_label["75-80"].window_count == 2
    assert by_label["75-80"].average_pnl == pytest.approx(2.0)
    assert by_label["75-80"].median_pnl == pytest.approx(2.0)
    assert by_label["85-90"].window_count == 1
    assert by_label["90-100"].window_count == 1


def test_calibration_flags_non_monotonic_score_to_pnl() -> None:
    # 高分桶反而 PnL 更低，应产生告警。
    outcomes = [
        WindowOutcome(grid_score=76.0, pnl=10.0),
        WindowOutcome(grid_score=95.0, pnl=-4.0),
    ]

    report = build_regime_calibration_report(outcomes)

    assert report.monotonic_pnl is False
    assert report.warnings
    assert "Regime 权重" in report.warnings[0]


def test_calibration_monotonic_when_higher_scores_do_better() -> None:
    outcomes = [
        WindowOutcome(grid_score=76.0, pnl=1.0),
        WindowOutcome(grid_score=88.0, pnl=4.0),
        WindowOutcome(grid_score=95.0, pnl=7.0),
    ]

    report = build_regime_calibration_report(outcomes)

    assert report.monotonic_pnl is True
    assert report.warnings == ()


def test_calibration_aggregates_stop_and_inventory_rates() -> None:
    outcomes = [
        WindowOutcome(grid_score=76.0, pnl=-1.0, stopped=True, inventory_critical=True),
        WindowOutcome(grid_score=77.0, pnl=2.0, stopped=False, max_adverse_excursion=3.5),
    ]

    report = build_regime_calibration_report(outcomes)
    bucket = next(b for b in report.buckets if b.label == "75-80")

    assert bucket.stop_rate == pytest.approx(0.5)
    assert bucket.inventory_critical_rate == pytest.approx(0.5)
    assert bucket.max_adverse_excursion == pytest.approx(3.5)


def test_calibration_rejects_invalid_buckets() -> None:
    with pytest.raises(ValueError, match="分数桶区间无效"):
        build_regime_calibration_report([], buckets=[(80.0, 80.0)])
