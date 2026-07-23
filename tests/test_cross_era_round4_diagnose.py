from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.cross_era_round4_diagnose import (
    _concentration_diagnostics,
    _diagnostic_jobs,
    _symbol_breakdown,
)
from scripts.robustness import WindowResult


def _result(
    symbol: str,
    window_id: str,
    pnl: float,
    *,
    fees: float = 0.0,
    slippage: float = 0.0,
) -> WindowResult:
    return WindowResult(
        parameter_id="test",
        symbol=symbol,
        window_id=window_id,
        market_close="2026-01-01T00:00:00+00:00",
        status="TRADED",
        reason="",
        pnl=pnl,
        fees_paid=fees,
        exit_slippage_cost=slippage,
        fill_count=2,
        pair_count=1,
        step_pct=0.002,
        gross_grid_pnl=max(pnl, 0.0) + fees,
    )


def test_diagnostic_jobs_never_include_final_oos() -> None:
    split = SimpleNamespace(
        development=("dev",),
        validation=("val",),
        final_oos=("final",),
    )

    jobs = _diagnostic_jobs(split)

    assert [item[0] for item in jobs] == [
        "DEV_COST50_SEED97",
        "VAL_BASE_SEED17",
        "VAL_COST50_SEED17",
    ]
    assert {window_id for _, _, window_ids, _, _, _ in jobs for window_id in window_ids} == {
        "dev",
        "val",
    }


def test_symbol_breakdown_sums_cost_components() -> None:
    payload = _symbol_breakdown([
        _result("BTCUSDT", "w1", 3.0, fees=0.4, slippage=0.2),
        _result("BTCUSDT", "w2", -1.0, fees=0.3, slippage=0.1),
        replace(
            _result("BTCUSDT", "blocked", 0.0),
            status="BLOCKED",
            gross_grid_pnl=999.0,
            paired_grid_pnl=999.0,
            stop_exit_pnl=-999.0,
        ),
    ])

    assert payload["pnl"] == 2.0
    assert payload["fees_paid"] == pytest.approx(0.7)
    assert payload["exit_slippage_cost"] == pytest.approx(0.3)
    assert payload["fill_count"] == 4
    assert payload["pair_count"] == 2
    assert payload["gross_grid_pnl"] < 999.0


def test_concentration_diagnostics_identifies_symbol_driver() -> None:
    diagnostics = _concentration_diagnostics([
        _result("BTCUSDT", "w1", 3.0),
        _result("BTCUSDT", "w2", 2.0),
        _result("ETHUSDT", "w1", 9.0),
        _result("ETHUSDT", "w2", 1.0),
    ])

    assert diagnostics["driver"] == "ETHUSDT"
    assert diagnostics["series"]["ETHUSDT"]["concentration"] == pytest.approx(0.9)
    assert diagnostics["series"]["PORTFOLIO"]["concentration"] == pytest.approx(12.0 / 15.0)
