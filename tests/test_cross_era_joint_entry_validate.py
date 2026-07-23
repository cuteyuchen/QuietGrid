from __future__ import annotations

from scripts.cross_era_joint_entry_validate import (
    _entry_filter,
    _validation_checks,
)


def _summary(**overrides):
    payload = {
        "positive_seed_count": 6,
        "worst_seed_total_pnl": 2.0,
        "mean_seed_total_pnl": 5.0,
        "symbol_pnl": {"BTCUSDT": 6.0, "ETHUSDT": 24.0},
        "minimum_seed_profit_factor": 1.2,
        "worst_5pct_window_mean_pnl": -1.0,
        "max_drawdown_pct": 0.04,
        "worst_best_window_concentration": 0.25,
        "fee_to_gross_profit_ratio": 0.20,
    }
    payload.update(overrides)
    return payload


def test_entry_filter_rehydrates_locked_payload() -> None:
    entry_filter = _entry_filter({
        "max_directional_efficiency": 0.4,
        "max_volatility_expansion": 0.95,
        "min_reversal_ratio": 0.3,
    })

    assert entry_filter.filter_id == "de0.40_ve0.95_rr0.30"


def test_validation_checks_require_cost_and_coverage_gates() -> None:
    baseline = _summary(mean_seed_total_pnl=-2.0, fee_to_gross_profit_ratio=0.18)
    passed = _validation_checks(
        baseline,
        _summary(),
        seed_count=6,
        btc_coverage=0.4,
        eth_coverage=0.3,
    )
    failed = _validation_checks(
        baseline,
        _summary(fee_to_gross_profit_ratio=0.30),
        seed_count=6,
        btc_coverage=0.4,
        eth_coverage=0.3,
    )

    assert all(passed.values())
    assert failed["fee_ratio_le_125pct_baseline"] is False
