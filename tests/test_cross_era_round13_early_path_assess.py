from __future__ import annotations

import pytest

from scripts.cross_era_round13_early_path_assess import _assess_horizon, _trigger


def _checkpoint(*, zero_pair: bool, adverse: float, favorable: float) -> dict[str, object]:
    return {
        "zero_pair": zero_pair,
        "adverse_excursion_pct": adverse,
        "favorable_excursion_pct": favorable,
        "equity": -0.2,
        "gross_inventory_notional": 100.0,
    }


def _record(seed: int, pnl: float, checkpoint: dict[str, object]) -> dict[str, object]:
    return {
        "seed": seed,
        "pnl": pnl,
        "checkpoints": {str(horizon): checkpoint for horizon in (30, 60, 120)},
    }


def test_trigger_requires_zero_pair_and_dominant_adverse_excursion() -> None:
    assert _trigger(_checkpoint(zero_pair=True, adverse=0.02, favorable=0.01)) is True
    assert _trigger(_checkpoint(zero_pair=False, adverse=0.02, favorable=0.01)) is False
    assert _trigger(_checkpoint(zero_pair=True, adverse=0.01, favorable=0.02)) is False


def test_assess_horizon_reports_zero_cost_upper_bound_and_false_positive() -> None:
    seeds = (3, 10, 17, 31, 59, 97)
    trigger = _checkpoint(zero_pair=True, adverse=0.02, favorable=0.01)
    windows = [
        {
            "window_id": "loss",
            "label": "PERSISTENT_LOSS",
            "seed_records": [_record(seed, -2.0, trigger) for seed in seeds],
        },
        {
            "window_id": "win",
            "label": "PROFITABLE",
            "seed_records": [_record(seed, 1.0, trigger) for seed in seeds],
        },
    ]

    result = _assess_horizon(windows, 30)

    assert result["baseline_btc_total_pnl"] == -6.0
    assert result["zero_cost_guard_btc_total_pnl"] == pytest.approx(-2.4)
    assert result["covered_persistent_loss_windows"] == ["loss"]
    assert result["profitable_false_positive_windows"] == ["win"]
    assert result["zero_cost_upper_bound_btc_positive"] is False
