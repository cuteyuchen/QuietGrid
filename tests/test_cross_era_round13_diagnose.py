from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.cross_era_round13_diagnose import (
    _aggregate_windows,
    _compare_windows,
    _persistent_loss_windows,
    _validate_round13_result,
)
from scripts.profit_protection_optimize import DEFAULT_SEEDS
from scripts.robustness import WindowResult


def _result(
    window_id: str,
    pnl: float,
    *,
    fills: int = 2,
    pairs: int = 1,
    stopped_at: int | None = None,
) -> WindowResult:
    return WindowResult(
        parameter_id="test",
        symbol="BTCUSDT",
        window_id=window_id,
        market_close=f"2020-01-{window_id[-2:]}T21:00:00+00:00",
        status="TRADED",
        reason="stop_loss" if stopped_at is not None else "completed",
        pnl=pnl,
        gross_grid_pnl=max(pnl, 0.0) + 1.0,
        paired_grid_pnl=max(pnl, 0.0) + 0.5,
        stop_exit_pnl=min(pnl, 0.0),
        stop_exit_cost=0.1,
        fees_paid=0.2,
        exit_slippage_cost=0.05,
        fill_count=fills,
        pair_count=pairs,
        stopped_at_index=stopped_at,
        max_inventory_utilization=0.4,
    )


def test_aggregate_windows_requires_complete_seed_coverage() -> None:
    with pytest.raises(ValueError, match="种子覆盖不完整"):
        _aggregate_windows(
            {
                3: [_result("w01", -1.0), _result("w02", 1.0)],
                17: [_result("w01", -2.0)],
            },
            symbol="BTCUSDT",
        )


def test_aggregate_windows_counts_all_seed_loss_and_early_stops() -> None:
    rows = _aggregate_windows(
        {
            3: [_result("w01", -2.0, fills=1, pairs=0, stopped_at=60)],
            17: [_result("w01", -4.0, fills=1, pairs=0, stopped_at=121)],
        },
        symbol="BTCUSDT",
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["mean_pnl"] == pytest.approx(-3.0)
    assert row["all_traded_seeds_negative"] is True
    assert row["negative_seed_count"] == 2
    assert row["stopped_within_120_bars"] == 1
    assert row["zero_pair_seed_count"] == 2
    assert row["single_fill_stop_seed_count"] == 2


def test_compare_windows_reports_candidate_delta_and_loss_concentration() -> None:
    reference = _aggregate_windows(
        {
            3: [_result("w01", -1.0), _result("w02", 2.0)],
            17: [_result("w01", -1.0), _result("w02", 2.0)],
        },
        symbol="BTCUSDT",
    )
    candidate = _aggregate_windows(
        {
            3: [_result("w01", -3.0), _result("w02", 1.0)],
            17: [_result("w01", -3.0), _result("w02", 1.0)],
        },
        symbol="BTCUSDT",
    )

    comparison = _compare_windows(reference, candidate)

    first = comparison["windows"][0]
    assert first["candidate_minus_reference_total_pnl"] == pytest.approx(-4.0)
    assert comparison["summary"]["candidate_total_pnl"] == pytest.approx(-4.0)
    assert comparison["summary"]["all_seed_negative_window_count"] == 1
    assert comparison["summary"]["worst_window_loss_share"] == pytest.approx(1.0)


def test_persistent_loss_windows_requires_both_cost_scenarios_negative() -> None:
    base_comparison = _compare_windows(
        _aggregate_windows({3: [_result("w01", 0.0)]}, symbol="BTCUSDT"),
        _aggregate_windows({3: [_result("w01", -1.0)]}, symbol="BTCUSDT"),
    )
    cost_comparison = _compare_windows(
        _aggregate_windows({3: [_result("w01", 0.0)]}, symbol="BTCUSDT"),
        _aggregate_windows({3: [_result("w01", -2.0)]}, symbol="BTCUSDT"),
    )

    rows = _persistent_loss_windows({
        "BASE": {"comparison": base_comparison},
        "COST50": {"comparison": cost_comparison},
    })

    assert [row["window_id"] for row in rows] == ["w01"]
    assert rows[0]["cost50_mean_pnl"] == pytest.approx(-2.0)


def test_validate_round13_result_preserves_sealed_oos() -> None:
    payload = {
        "eligible_candidate_ids": [],
        "phase_b_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "final_oos_authorized": False,
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "seeds": list(DEFAULT_SEEDS),
    }

    _validate_round13_result(payload)

    with pytest.raises(ValueError, match="Final OOS"):
        _validate_round13_result(replace_payload(
            payload,
            final_oos_status="EVALUATED",
        ))


def replace_payload(payload: dict[str, object], **changes: object) -> dict[str, object]:
    return payload | changes


def test_aggregate_windows_ignores_other_symbols() -> None:
    eth = replace(_result("w01", 99.0), symbol="ETHUSDT")
    rows = _aggregate_windows(
        {
            3: [_result("w01", -1.0), eth],
            17: [_result("w01", -3.0), eth],
        },
        symbol="BTCUSDT",
    )

    assert rows[0]["mean_pnl"] == pytest.approx(-2.0)
