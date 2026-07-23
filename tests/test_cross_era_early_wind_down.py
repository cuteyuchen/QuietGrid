from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.cross_era_early_wind_down import (
    FIXED_FILTERS,
    _cell_specs,
    _mechanism_checks,
    _mechanism_summary,
    _registered_wind_downs,
)
from scripts.robustness import WindowResult


def _result(
    *,
    paired: float,
    stop_exit: float,
    status: str = "TRADED",
) -> WindowResult:
    return WindowResult(
        parameter_id="test",
        symbol="BTCUSDT",
        window_id="w1",
        market_close="2026-01-01T00:00:00+00:00",
        status=status,
        reason="",
        pnl=paired + stop_exit,
        paired_grid_pnl=paired,
        stop_exit_pnl=stop_exit,
        gross_grid_pnl=paired + stop_exit,
    )


def test_round5_candidates_and_filters_match_protocol() -> None:
    assert _registered_wind_downs() == (2160, 2880)
    assert FIXED_FILTERS["BTCUSDT"].filter_id == "de0.40_ve1.05_rr0.35"
    assert FIXED_FILTERS["ETHUSDT"].filter_id == "de0.35_ve1.05_rr0.55"


def test_round5_cells_exclude_final_oos() -> None:
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


def test_mechanism_summary_ignores_blocked_stale_economics() -> None:
    summary = _mechanism_summary([
        _result(paired=10.0, stop_exit=-6.0),
        replace(
            _result(paired=999.0, stop_exit=-999.0),
            status="BLOCKED",
            pnl=0.0,
        ),
    ])

    assert summary["paired_grid_pnl"] == 10.0
    assert summary["stop_exit_pnl"] == -6.0


def test_mechanism_checks_require_exit_improvement_and_pair_retention() -> None:
    reference = {"paired_grid_pnl": 100.0, "stop_exit_pnl": -80.0}
    passed = _mechanism_checks(
        reference,
        {"paired_grid_pnl": 70.0, "stop_exit_pnl": -60.0},
    )
    failed = _mechanism_checks(
        reference,
        {"paired_grid_pnl": 59.0, "stop_exit_pnl": -70.0},
    )

    assert all(passed.values())
    assert failed["stop_exit_loss_improvement_ge_20pct"] is False
    assert failed["paired_grid_pnl_retention_ge_60pct"] is False
