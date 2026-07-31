from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.models import GridDirectionMode
from scripts.semiconductor_grid_backtest_v28 import _backtest_config
from strategy.semiconductor_grid_v28 import (
    ANCHOR_IDS,
    Combination,
    catalog_sha256,
    factor_snapshot,
    generate_phase1_covering_array,
    local_region,
    neighbors,
    pairwise_audit,
    phase1_profile_summaries,
    select_phase2_profiles,
    select_local_regions,
)


def test_combination_round_trip_and_rejects_invalid_values() -> None:
    item = Combination.parse("22333")
    assert item.id == "22333"
    assert item.with_direction("n") == "22333-N"
    with pytest.raises(ValueError):
        Combination.parse("22336")
    with pytest.raises(ValueError):
        Combination.parse("2233")


def test_phase1_covering_array_is_deterministic_and_covers_pairs() -> None:
    first = generate_phase1_covering_array()
    second = generate_phase1_covering_array()
    assert len(first) == 96
    assert tuple(item.id for item in first) == tuple(item.id for item in second)
    assert set(ANCHOR_IDS).issubset({item.id for item in first})
    assert pairwise_audit(first)["passed"] is True
    assert catalog_sha256(first) == catalog_sha256(second)


def test_neighbors_change_only_one_adjacent_factor() -> None:
    result = {item.id for item in neighbors(Combination.parse("22333"))}
    assert result == {
        "12333", "32333", "21333", "23333", "22233",
        "22433", "22323", "22343", "22332", "22334",
    }


def test_factor_snapshot_matches_registered_controls() -> None:
    snapshot = factor_snapshot(Combination.parse("31445"))
    assert snapshot["range"]["multiplier"] == 2.0
    assert snapshot["grid"]["max_grid_num"] == 10
    assert snapshot["profit"]["mode"] == "INVENTORY_AWARE"
    assert snapshot["inventory"]["reduce_only"] == 0.40
    assert snapshot["stop"]["confirm_bars"] == 30


def test_all_registered_factor_levels_map_to_frozen_controls() -> None:
    assert [factor_snapshot(Combination(a, 1, 1, 1, 1))["range"]["multiplier"] for a in range(1, 5)] == [1.0, 1.5, 2.0, None]
    assert [factor_snapshot(Combination(1, b, 1, 1, 1))["grid"]["max_grid_num"] for b in range(1, 5)] == [10, 20, 50, 100]
    assert [factor_snapshot(Combination(1, 1, c, 1, 1))["profit"]["mode"] for c in range(1, 5)] == ["NONE", "FIXED", "PEAK", "INVENTORY_AWARE"]
    assert [factor_snapshot(Combination(1, 1, 1, d, 1))["inventory"]["reduce_only"] for d in range(1, 5)] == [0.80, 0.80, 0.50, 0.40]
    assert [factor_snapshot(Combination(1, 1, 1, 1, e))["stop"]["mode"] for e in range(1, 6)] == ["BASELINE", "HALF_ATR", "ONE_ATR", "TWO_ATR", "TIME_CONFIRMED"]


def test_c_d_e_levels_are_injected_into_backtest_config() -> None:
    scenario = SimpleNamespace(
        maker_fee_rate=0.0,
        taker_fee_rate=0.001,
        max_fills_per_bar=2,
        maker_fill_probability=1.0,
        stop_slippage_bps=5.0,
    )
    rule = SimpleNamespace(tick_size=0.01, step_size=0.001)

    c2 = _backtest_config(Combination.parse("11211"), scenario=scenario, capital=500.0, leverage=1.0, rule=rule, direction=GridDirectionMode.NEUTRAL, seed=3)
    c3 = _backtest_config(Combination.parse("11311"), scenario=scenario, capital=500.0, leverage=1.0, rule=rule, direction=GridDirectionMode.NEUTRAL, seed=3)
    c4 = _backtest_config(Combination.parse("11411"), scenario=scenario, capital=500.0, leverage=1.0, rule=rule, direction=GridDirectionMode.NEUTRAL, seed=3)
    assert c2.fixed_take_profit_usdt == 5.0
    assert c3.profit_protection is not None
    assert c3.profit_protection.minimum_locked_profit_ratio == 0.25
    assert c4.profit_inventory_activation_usdt == 2.5
    assert c4.profit_inventory_drag_close_ratio == 0.60
    assert c4.profit_peak_close_drawdown_pct == 0.40

    d2 = _backtest_config(Combination.parse("11121"), scenario=scenario, capital=500.0, leverage=1.0, rule=rule, direction=GridDirectionMode.NEUTRAL, seed=3)
    d3 = _backtest_config(Combination.parse("11131"), scenario=scenario, capital=500.0, leverage=1.0, rule=rule, direction=GridDirectionMode.NEUTRAL, seed=3)
    d4 = _backtest_config(Combination.parse("11141"), scenario=scenario, capital=500.0, leverage=1.0, rule=rule, direction=GridDirectionMode.NEUTRAL, seed=3)
    assert (d2.inventory_caution_utilization, d2.inventory_reduce_only_utilization) == (0.35, 0.80)
    assert (d3.inventory_caution_utilization, d3.inventory_reduce_only_utilization) == (0.35, 0.50)
    assert (d4.inventory_caution_utilization, d4.inventory_reduce_only_utilization, d4.inventory_drag_close_ratio) == (0.25, 0.40, 0.60)

    stops = [
        _backtest_config(Combination(1, 1, 1, 1, e), scenario=scenario, capital=500.0, leverage=1.0, rule=rule, direction=GridDirectionMode.NEUTRAL, seed=3)
        for e in range(2, 6)
    ]
    assert [(item.stop_atr_buffer, item.stop_time_confirm_bars) for item in stops] == [(0.5, 0), (1.0, 0), (2.0, 0), (1.0, 30)]


def _phase1_row(
    combination_id: str,
    scenario: str,
    window: str,
    seed: int,
    pnl: float,
) -> dict[str, object]:
    return {
        "combination_id": combination_id,
        "direction": "NEUTRAL",
        "scenario": scenario,
        "symbol": "SNDKUSDT",
        "window_key": window,
        "force_close_at": (
            "2026-07-01T00:00:00+00:00"
            if window == "only-window"
            else f"2026-07-{int(window[1:]):02d}T00:00:00+00:00"
        ),
        "seed": seed,
        "net_pnl": pnl,
        "paired_grid_pnl": max(pnl, 1.0),
        "inventory_drag": 0.1,
    }


def test_phase2_gate_averages_seeds_and_uses_exposed_early_only() -> None:
    rows = []
    for scenario in ("PRIMARY_ZERO_MAKER", "EXECUTION_STRESS"):
        rows.extend(
            [
                _phase1_row("22222", scenario, "w1", 3, 4.0),
                _phase1_row("22222", scenario, "w1", 10, 0.0),
                _phase1_row("22222", scenario, "w2", 3, 1.0),
                _phase1_row("22222", scenario, "w2", 10, 1.0),
                _phase1_row("22222", scenario, "w3", 3, 1.0),
                _phase1_row("22222", scenario, "w3", 10, 1.0),
                _phase1_row("22222", scenario, "w4", 3, -20.0),
                _phase1_row("22222", scenario, "w4", 10, -20.0),
                _phase1_row("22222", scenario, "w5", 3, -20.0),
                _phase1_row("22222", scenario, "w5", 10, -20.0),
            ]
        )

    summary = next(
        row
        for row in phase1_profile_summaries(rows)
        if row["scenario"] == "PRIMARY_ZERO_MAKER"
    )
    selected = select_phase2_profiles(rows)

    assert summary["window_count"] == 5
    assert summary["total_pnl"] == -36.0
    assert any(row["combination_id"] == "22222" for row in selected)


def test_phase2_always_includes_anchor_profiles() -> None:
    selected = select_phase2_profiles([])
    anchors = {(row["combination_id"], row["direction"]) for row in selected}
    assert ("11111", "NEUTRAL") in anchors
    assert ("11111", "LONG") in anchors


def test_local_region_is_complete_adjacent_factorial() -> None:
    members = local_region(Combination.parse("22333"))
    assert len(members) == 32
    assert {item.a for item in members} == {1, 2}
    assert {item.b for item in members} == {1, 2}
    assert {item.c for item in members} == {2, 3}
    assert {item.d for item in members} == {2, 3}
    assert {item.e for item in members} == {2, 3}


def test_local_region_rejects_single_window_even_with_positive_neighbors() -> None:
    rows = []
    for combination_id in ("11111", "11112"):
        for scenario in ("PRIMARY_ZERO_MAKER", "EXECUTION_STRESS"):
            for seed in (3, 10, 17, 31, 59, 97):
                rows.append(
                    _phase1_row(combination_id, scenario, "only-window", seed, 1.0)
                )

    assert select_local_regions(rows) == []
