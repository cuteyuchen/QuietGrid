from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_cycle_capacity_upper_bound as capacity
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_round13_diagnose import _sha256
from scripts.robustness import WindowResult


UTC = timezone.utc


def _window_result(window_id: str, pnl: float) -> WindowResult:
    index = int(window_id.removeprefix("w"))
    return WindowResult(
        parameter_id="P",
        symbol="BTCUSDT",
        window_id=window_id,
        market_close=(datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
        status="TRADED",
        reason="window_force_close",
        pnl=pnl,
        max_drawdown=max(0.0, -pnl),
        fill_count=2,
        pair_count=1,
        step_pct=0.01,
    )


def _cache(policies: dict) -> dict[tuple[object, ...], object]:
    maker_fee, taker_fee, slippage_bps = asset_audit.SCENARIOS["BASE"]
    return {
        (
            policy.parameter.parameter_id,
            symbol,
            window_id,
            0.65,
            2160,
            policy.max_inventory_notional,
            5,
            1.10,
            1.0,
            capacity.EXPECTED_UNPAIRED_LOTS[symbol],
            capacity.EXPECTED_REDUCE_TARGETS[symbol],
            maker_fee,
            taker_fee,
            slippage_bps,
            2.0,
            3,
        ): object()
        for window_id in ("w1", "w2")
        for symbol, policy in policies.items()
    }


def test_protocol_hash_matches_frozen_file() -> None:
    assert _sha256(capacity.PROTOCOL_PATH.resolve()) == capacity.PROTOCOL_SHA256


def test_base_components_restore_unfiltered_round16_policy() -> None:
    _parameters, policies, config, maker_policy = capacity._base_components(
        profit_opt._base_research_config()
    )

    assert policies["BTCUSDT"].entry_filter is None
    assert policies["ETHUSDT"].entry_filter is None
    assert policies["BTCUSDT"].max_unpaired_lots_per_side == 1
    assert policies["ETHUSDT"].max_unpaired_lots_per_side == 0
    assert policies["BTCUSDT"].reduce_target_step_fraction == pytest.approx(0.50)
    assert policies["ETHUSDT"].reduce_target_step_fraction == pytest.approx(1.00)
    assert config.wind_down_bars == 2160
    assert maker_policy.urgency_exponent == pytest.approx(2.0)


def test_completed_step_cycles_ignores_one_way_or_small_noise() -> None:
    assert capacity._completed_step_cycles((100.0, 102.0, 104.0), 0.01) == 0
    assert capacity._completed_step_cycles(
        (100.0, 100.2, 100.1, 100.3, 100.2),
        0.01,
    ) == 0


def test_completed_step_cycles_counts_full_step_sized_round_trips() -> None:
    assert capacity._completed_step_cycles((100.0, 102.0, 100.0, 102.0), 0.01) == 1
    assert capacity._completed_step_cycles(
        (100.0, 102.0, 100.0, 102.0, 100.0),
        0.01,
    ) == 2


def test_extract_target_histories_stops_at_entry_and_requires_continuity() -> None:
    rows = [
        (0, 100.0),
        (60_000, 101.0),
        (120_000, 100.0),
        (180_000, 101.0),
        (240_000, 999.0),
    ]

    histories, audit = capacity._extract_target_histories(
        rows,
        {180_000: "w1"},
        (2, 3),
    )

    assert histories["w1"][2] == (101.0, 100.0, 101.0)
    assert histories["w1"][3] == (100.0, 101.0, 100.0, 101.0)
    assert audit["last_read_open_time"] == 180_000
    assert audit["self_reference_count"] == 0
    assert audit["targets"]["w1"]["max_used_open_time"] == 180_000

    broken, broken_audit = capacity._extract_target_histories(
        [(0, 100.0), (60_000, 101.0), (180_000, 100.0)],
        {180_000: "w2"},
        (2,),
    )
    assert broken["w2"][2] is None
    assert broken_audit["gap_reset_count"] == 1


def test_capacity_filter_blocks_missing_or_below_threshold() -> None:
    result = _window_result("w1", 1.0)

    unavailable = capacity._apply_capacity_filter(
        result,
        capacity=None,
        threshold=0.0,
        lookback=180,
    )
    below = capacity._apply_capacity_filter(
        result,
        capacity=0.1,
        threshold=0.2,
        lookback=180,
    )
    kept = capacity._apply_capacity_filter(
        result,
        capacity=0.2,
        threshold=0.2,
        lookback=180,
    )

    assert unavailable.status == "BLOCKED"
    assert "UNAVAILABLE" in unavailable.reason
    assert below.status == "BLOCKED"
    assert kept is result


def test_oracle_can_select_high_capacity_windows_but_remains_same_cell_upper_bound() -> None:
    results = {
        seed: [
            _window_result("w0", -10.0),
            _window_result("w1", 1.0),
            _window_result("w2", 1.0),
            _window_result("w3", 1.0),
        ]
        for seed in asset_audit.DEFAULT_SEEDS
    }
    capacities = {
        "w0": {lookback: 0.0 for lookback in capacity.LOOKBACKS},
        "w1": {lookback: 1.0 for lookback in capacity.LOOKBACKS},
        "w2": {lookback: 1.0 for lookback in capacity.LOOKBACKS},
        "w3": {lookback: 1.0 for lookback in capacity.LOOKBACKS},
    }

    observed = capacity._evaluate_oracle_symbol_cell(
        results_by_seed=results,
        capacities_by_window=capacities,
        capital=500.0,
    )

    assert observed["oracle_passed"] is True
    assert observed["oracle_selected"]["threshold"] == pytest.approx(1.0)
    assert observed["oracle_selected"]["summary"]["minimum_trade_coverage"] == pytest.approx(0.75)
    assert observed["oracle_selected"]["summary"]["worst_seed_total_pnl"] == pytest.approx(3.0)


def test_worker_cache_accepts_exact_base_and_rejects_policy_drift() -> None:
    _parameters, policies, config, maker_policy = capacity._base_components(
        profit_opt._base_research_config()
    )
    research = SimpleNamespace(config=config, _cache=_cache(policies))

    observed = capacity._verify_worker_cache(
        research,
        policies,
        maker_policy,
        allowed_window_ids={"w1", "w2"},
        cost=asset_audit.SCENARIOS["BASE"],
        seed=3,
    )
    assert observed["passed"] is True

    cache = _cache(policies)
    key = next(iter(cache))
    del cache[key]
    changed = list(key)
    changed[10] = 0.75
    cache[tuple(changed)] = object()
    drifted = SimpleNamespace(config=config, _cache=cache)
    with pytest.raises(RuntimeError, match="参数 10 不一致"):
        capacity._verify_worker_cache(
            drifted,
            policies,
            maker_policy,
            allowed_window_ids={"w1", "w2"},
            cost=asset_audit.SCENARIOS["BASE"],
            seed=3,
        )
