from __future__ import annotations

from scripts.cross_era_entry_screen import (
    _entry_checks,
    _filtered_evidence_for_symbols,
    _registered_filters,
    _select_filter,
)


def _summary(**overrides):
    payload = {
        "positive_seed_count": 6,
        "worst_seed_total_pnl": 3.0,
        "mean_seed_total_pnl": 5.0,
        "symbol_pnl": {"BTCUSDT": 6.0, "ETHUSDT": 24.0},
        "minimum_seed_profit_factor": 1.2,
        "worst_5pct_window_mean_pnl": -1.0,
        "max_drawdown_pct": 0.03,
        "worst_best_window_concentration": 0.25,
        "fee_to_gross_profit_ratio": 0.20,
    }
    payload.update(overrides)
    return payload


def test_registered_filters_are_fixed_and_no_looser_than_current() -> None:
    filters = _registered_filters()

    assert len(filters) == 64
    assert all(item.max_directional_efficiency <= 0.50 for item in filters)
    assert all(item.max_volatility_expansion <= 1.05 for item in filters)
    assert all(item.min_reversal_ratio >= 0.25 for item in filters)


def test_entry_checks_require_positive_robust_portfolio() -> None:
    baseline = _summary(
        mean_seed_total_pnl=-20.0,
        worst_5pct_window_mean_pnl=-2.0,
        fee_to_gross_profit_ratio=0.18,
    )
    passed = _entry_checks(
        baseline,
        _summary(),
        seed_count=6,
        eth_trade_coverage=0.30,
    )
    failed = _entry_checks(
        baseline,
        _summary(worst_seed_total_pnl=-0.1),
        seed_count=6,
        eth_trade_coverage=0.30,
    )

    assert all(passed.values())
    assert failed["worst_seed_positive"] is False


def test_select_filter_uses_worst_seed_after_tail() -> None:
    baseline = _summary(worst_5pct_window_mean_pnl=-2.0)
    candidates = {
        "a": _summary(worst_seed_total_pnl=2.0),
        "b": _summary(worst_seed_total_pnl=3.0),
    }
    checks = {key: {"eligible": True} for key in candidates}
    coverages = {"a": 0.4, "b": 0.3}

    assert _select_filter(baseline, candidates, checks, coverages) == "b"


def test_filtered_evidence_helper_is_exposed_for_joint_round() -> None:
    assert callable(_filtered_evidence_for_symbols)
