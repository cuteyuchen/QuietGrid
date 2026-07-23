from __future__ import annotations

from types import SimpleNamespace

from scripts.cross_era_extended_development import (
    _candidate_pairs,
    _cell_checks,
    _cell_specs,
    _registered_btc_filters,
    _registered_eth_filters,
    _select_candidate,
    _selection_metrics,
)


def _summary(**overrides):
    payload = {
        "positive_seed_count": 6,
        "worst_seed_total_pnl": 2.0,
        "mean_seed_total_pnl": 5.0,
        "symbol_pnl": {"BTCUSDT": 6.0, "ETHUSDT": 4.0},
        "minimum_seed_profit_factor": 1.2,
        "worst_5pct_window_mean_pnl": -1.0,
        "max_drawdown_pct": 0.04,
        "worst_best_window_concentration": 0.25,
        "fee_to_gross_profit_ratio": 0.20,
    }
    payload.update(overrides)
    return payload


def test_round4_registered_pairs_match_frozen_protocol() -> None:
    assert [item.filter_id for item in _registered_btc_filters()] == [
        "de0.40_ve0.95_rr0.35",
        "de0.40_ve1.05_rr0.35",
        "de0.55_ve0.95_rr0.35",
        "de0.55_ve1.05_rr0.35",
    ]
    assert [item.filter_id for item in _registered_eth_filters()] == [
        "de0.35_ve1.00_rr0.55",
        "de0.35_ve1.05_rr0.55",
        "de0.45_ve1.00_rr0.55",
    ]
    pairs = _candidate_pairs()
    assert len(pairs) == 12
    assert len({item[0] for item in pairs}) == 12


def test_round4_cells_exclude_final_oos() -> None:
    split = SimpleNamespace(
        development=("dev",),
        validation=("val",),
        final_oos=("final",),
    )

    cells = _cell_specs(split)

    assert [item[0] for item in cells] == [
        "DEV_BASE",
        "DEV_COST50",
        "VAL_BASE",
        "VAL_COST50",
    ]
    assert {window_id for _, _, window_ids, _ in cells for window_id in window_ids} == {
        "dev",
        "val",
    }


def test_cell_checks_require_same_cell_retention_and_symbol_coverage() -> None:
    baseline = _summary(
        mean_seed_total_pnl=8.0,
        worst_5pct_window_mean_pnl=-2.0,
        fee_to_gross_profit_ratio=0.18,
    )
    passed = _cell_checks(
        baseline,
        _summary(mean_seed_total_pnl=6.0),
        seed_count=6,
        btc_coverage=0.30,
        eth_coverage=0.25,
    )
    failed = _cell_checks(
        baseline,
        _summary(mean_seed_total_pnl=5.9),
        seed_count=6,
        btc_coverage=0.30,
        eth_coverage=0.24,
    )

    assert all(passed.values())
    assert failed["mean_pnl_retention_ge_75pct"] is False
    assert failed["eth_trade_coverage_ge_25pct"] is False


def test_selection_uses_registered_minimax_order() -> None:
    stronger_worst_seed = {
        "DEV_BASE": _summary(worst_seed_total_pnl=3.0, symbol_pnl={"BTCUSDT": 2.0, "ETHUSDT": 2.0}),
        "DEV_COST50": _summary(worst_seed_total_pnl=3.0, symbol_pnl={"BTCUSDT": 2.0, "ETHUSDT": 2.0}),
        "VAL_BASE": _summary(worst_seed_total_pnl=3.0, symbol_pnl={"BTCUSDT": 2.0, "ETHUSDT": 2.0}),
        "VAL_COST50": _summary(worst_seed_total_pnl=3.0, symbol_pnl={"BTCUSDT": 2.0, "ETHUSDT": 2.0}),
    }
    weaker_worst_seed = {
        name: _summary(worst_seed_total_pnl=2.0, symbol_pnl={"BTCUSDT": 20.0, "ETHUSDT": 20.0})
        for name in stronger_worst_seed
    }
    candidates = {
        "a": {"all_cells_passed": True, "selection_metrics": _selection_metrics(weaker_worst_seed)},
        "b": {"all_cells_passed": True, "selection_metrics": _selection_metrics(stronger_worst_seed)},
        "c": {"all_cells_passed": False, "selection_metrics": _selection_metrics(stronger_worst_seed)},
    }

    assert _select_candidate(candidates) == "b"


def test_selection_uses_weakest_symbol_after_worst_seed() -> None:
    cells_a = {
        name: _summary(symbol_pnl={"BTCUSDT": 3.0, "ETHUSDT": 1.0})
        for name in ("DEV_BASE", "DEV_COST50", "VAL_BASE", "VAL_COST50")
    }
    cells_b = {
        name: _summary(symbol_pnl={"BTCUSDT": 2.0, "ETHUSDT": 2.0})
        for name in cells_a
    }
    candidates = {
        "a": {"all_cells_passed": True, "selection_metrics": _selection_metrics(cells_a)},
        "b": {"all_cells_passed": True, "selection_metrics": _selection_metrics(cells_b)},
    }

    assert _select_candidate(candidates) == "b"
