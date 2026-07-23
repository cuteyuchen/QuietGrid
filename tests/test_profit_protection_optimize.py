from __future__ import annotations

import pytest

from scripts.profit_protection_optimize import (
    _candidate,
    _candidate_checks,
    _tail_mean,
)
from scripts.profit_protection_p3_optimize import _p3_checks, _with_p3
from scripts.volatility_defense_optimize import (
    _checks as _volatility_checks,
    _volatility_candidate,
)


def _combined(**overrides):
    payload = {
        "profitable_to_losing_ratio": 0.50,
        "state_pnl": {"RANGE": 100.0},
        "fee_to_gross_profit_ratio": 0.10,
        "symbol_pnl": {"BTCUSDT": 20.0, "ETHUSDT": 10.0},
        "max_drawdown_pct": 0.10,
        "worst_5pct_window_mean_pnl": -10.0,
        "median_peak_profit_giveback_pct": 0.40,
        "positive_seed_count": 4,
        "worst_best_window_concentration": 0.30,
        "profit_protection_activation_count": 1,
    }
    payload.update(overrides)
    return {"combined": payload}


def test_tail_mean_uses_at_least_one_worst_observation() -> None:
    assert _tail_mean([4.0, -3.0, 2.0, -1.0]) == pytest.approx(-3.0)


def test_candidate_rejects_invalid_drawdown_order() -> None:
    with pytest.raises(ValueError, match="suppress < reduce < close"):
        _candidate(
            "invalid",
            "test",
            suppress_drawdown_pct=0.40,
            reduce_drawdown_pct=0.35,
        )


def test_candidate_checks_accept_all_documented_thresholds_at_boundary() -> None:
    baseline = _combined()
    candidate = _combined(
        profitable_to_losing_ratio=0.35,
        state_pnl={"RANGE": 75.0},
        fee_to_gross_profit_ratio=0.15,
        max_drawdown_pct=0.105,
        worst_5pct_window_mean_pnl=-8.0,
        median_peak_profit_giveback_pct=0.45,
        positive_seed_count=4,
        worst_best_window_concentration=0.35,
        symbol_pnl={"BTCUSDT": 15.0, "ETHUSDT": 5.0},
    )

    checks = _candidate_checks(baseline, candidate, tests_passed=True)

    assert all(checks.values())


def test_candidate_checks_rejects_untriggered_policy() -> None:
    checks = _candidate_checks(
        _combined(),
        _combined(profit_protection_activation_count=0),
        tests_passed=True,
    )

    assert checks["activation_observed"] is False


def test_p3_candidate_keeps_p2_thresholds_locked() -> None:
    core = _candidate(
        "P2",
        "locked",
        activation_usdt=2.0,
        close_drawdown_pct=0.40,
    )

    candidate = _with_p3(
        core,
        "P3",
        "test",
        passive_after=60,
        active_after=120,
        passive_fraction=0.20,
        active_fraction=0.35,
    )

    assert candidate.activation_usdt == core.activation_usdt
    assert candidate.close_drawdown_pct == core.close_drawdown_pct
    assert candidate.passive_reduce_after_bars == 60
    assert candidate.active_reduce_after_bars == 120
    assert candidate.active_reduce_fraction == pytest.approx(0.35)


def test_p3_checks_require_observed_target_reduction() -> None:
    candidate = {
        "candidate": {"active_reduce_fraction": 0.20},
        "combined": {
            "profit_active_reduce_count": 2,
            "median_profit_active_reduce_inventory_reduction_pct": 0.18,
        },
    }

    assert all(_p3_checks(candidate).values())

    candidate["combined"]["median_profit_active_reduce_inventory_reduction_pct"] = 0.17
    assert not _p3_checks(candidate)[
        "active_inventory_reduction_ge_90pct_target"
    ]


def test_volatility_candidate_keeps_profit_protection_off() -> None:
    candidate = _volatility_candidate(
        "P4",
        "test",
        expansion_ratio=1.75,
        breaches=5,
        fraction=0.35,
    )

    assert candidate.enabled is False
    assert candidate.mode == "OFF"
    assert candidate.volatility_reduce_expansion_ratio == pytest.approx(1.75)
    assert candidate.volatility_reduce_after_breaches == 5
    assert candidate.volatility_reduce_fraction == pytest.approx(0.35)


def test_volatility_checks_accept_all_thresholds_at_boundary() -> None:
    baseline = {
        "combined": {
            "mean_seed_total_pnl": 100.0,
            "state_pnl": {"RANGE": 100.0, "VOLATILITY_EXPANSION": -10.0},
            "fee_to_gross_profit_ratio": 0.10,
            "symbol_pnl": {"BTCUSDT": 20.0, "ETHUSDT": 10.0},
            "max_drawdown_pct": 0.10,
            "worst_5pct_window_mean_pnl": -10.0,
            "positive_seed_count": 6,
            "worst_best_window_concentration": 0.30,
        },
    }
    candidate = {
        "candidate": {"volatility_reduce_fraction": 0.20},
        "combined": {
            "mean_seed_total_pnl": 80.0,
            "state_pnl": {"RANGE": 75.0, "VOLATILITY_EXPANSION": -8.0},
            "fee_to_gross_profit_ratio": 0.15,
            "symbol_pnl": {"BTCUSDT": 15.0, "ETHUSDT": 5.0},
            "max_drawdown_pct": 0.105,
            "worst_5pct_window_mean_pnl": -8.0,
            "positive_seed_count": 4,
            "worst_best_window_concentration": 0.35,
            "volatility_reduce_count": 1,
            "median_volatility_reduce_inventory_reduction_pct": 0.18,
        },
    }

    assert all(
        _volatility_checks(baseline, candidate, tests_passed=True).values()
    )
