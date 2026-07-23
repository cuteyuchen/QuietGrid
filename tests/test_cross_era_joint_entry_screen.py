from __future__ import annotations

from scripts.cross_era_joint_entry_screen import (
    FIXED_ETH_FILTER,
    _joint_checks,
    _registered_btc_filters,
    _select_btc_filter,
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


def test_round3_filters_and_fixed_eth_match_protocol() -> None:
    filters = _registered_btc_filters()

    assert len(filters) == 48
    assert FIXED_ETH_FILTER.filter_id == "de0.35_ve1.00_rr0.55"


def test_joint_checks_add_btc_coverage_gate() -> None:
    baseline = _summary(
        mean_seed_total_pnl=-20.0,
        worst_5pct_window_mean_pnl=-2.0,
        fee_to_gross_profit_ratio=0.18,
    )
    passed = _joint_checks(
        baseline,
        _summary(),
        seed_count=6,
        eth_coverage=0.26,
        btc_coverage=0.30,
    )
    failed = _joint_checks(
        baseline,
        _summary(),
        seed_count=6,
        eth_coverage=0.26,
        btc_coverage=0.20,
    )

    assert all(passed.values())
    assert failed["btc_trade_coverage_ge_25pct"] is False


def test_select_btc_filter_prefers_higher_worst_seed() -> None:
    baseline = _summary(worst_5pct_window_mean_pnl=-2.0)
    candidates = {
        "a": _summary(worst_seed_total_pnl=2.0),
        "b": _summary(worst_seed_total_pnl=3.0),
    }
    checks = {key: {"eligible": True} for key in candidates}
    coverages = {"a": 0.4, "b": 0.3}

    assert _select_btc_filter(baseline, candidates, checks, coverages) == "b"
