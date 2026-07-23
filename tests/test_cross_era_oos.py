from __future__ import annotations

from scripts.cross_era_oos import (
    _development_checks,
    _market_states,
    _registered_candidates,
    _scenario_checks,
    _select_candidate,
)


class _Window:
    def __init__(self, window_id: str) -> None:
        self.window_id = window_id


def _summary(**overrides):
    payload = {
        "positive_seed_count": 6,
        "symbol_pnl": {"BTCUSDT": 12.0, "ETHUSDT": 8.0},
        "worst_5pct_window_mean_pnl": -1.5,
        "max_drawdown_pct": 0.02,
        "mean_seed_total_pnl": 20.0,
        "worst_seed_total_pnl": 12.0,
        "state_pnl": {"RANGE": 30.0},
        "fee_to_gross_profit_ratio": 0.20,
        "worst_best_window_concentration": 0.25,
        "minimum_seed_profit_factor": 1.20,
    }
    payload.update(overrides)
    return payload


def test_registered_candidates_match_protocol() -> None:
    candidates = {item.candidate_id: item for item in _registered_candidates()}

    assert tuple(candidates) == (
        "X0_BASELINE",
        "X1_P3_ACTIVE_PROFIT",
        "X2_P4_VOLATILITY",
        "X3_P9_EVENT_FREEZE",
        "X4_P3_PLUS_P4",
        "X5_P3_PLUS_P9",
    )
    hybrid = candidates["X5_P3_PLUS_P9"]
    assert hybrid.enabled is True
    assert hybrid.activation_usdt == 2.0
    assert hybrid.passive_reduce_after_bars == 30
    assert hybrid.volatility_reduce_expansion_ratio == 1.75
    assert hybrid.volatility_wind_down_after_reduce is True
    assert hybrid.volatility_resume_after_normal_bars == 10


def test_development_checks_enforce_tail_and_concentration() -> None:
    baseline = _summary(
        worst_5pct_window_mean_pnl=-2.0,
        max_drawdown_pct=0.03,
        mean_seed_total_pnl=24.0,
        state_pnl={"RANGE": 40.0},
        fee_to_gross_profit_ratio=0.16,
    )
    candidate = _summary()

    checks = _development_checks(baseline, candidate, seed_count=6)

    assert all(checks.values())
    failed = _development_checks(
        baseline,
        _summary(worst_best_window_concentration=0.36),
        seed_count=6,
    )
    assert failed["best_window_concentration_le_35pct"] is False


def test_selection_uses_registered_tie_break_order() -> None:
    baseline = _summary(worst_5pct_window_mean_pnl=-2.0)
    candidates = {
        "B": _summary(max_drawdown_pct=0.019),
        "A": _summary(max_drawdown_pct=0.018),
    }
    checks = {
        candidate_id: {"eligible": True}
        for candidate_id in candidates
    }

    assert _select_candidate(baseline, candidates, checks) == "A"


def test_scenario_checks_require_every_seed_profit_factor() -> None:
    baseline = _summary()
    passed = _scenario_checks(baseline, _summary(), seed_count=6)
    failed = _scenario_checks(
        baseline,
        _summary(minimum_seed_profit_factor=0.99),
        seed_count=6,
    )

    assert all(passed.values())
    assert failed["all_seed_profit_factors_gt_1"] is False


def test_market_states_only_classifies_requested_windows(monkeypatch) -> None:
    seen = []

    def classify(window):
        seen.append(window.window_id)
        return "RANGE"

    monkeypatch.setattr("scripts.cross_era_oos._classify_market_state", classify)

    states = _market_states(
        [_Window("development"), _Window("validation"), _Window("final")],
        ("development",),
    )

    assert states == {"development": "RANGE"}
    assert seen == ["development"]
