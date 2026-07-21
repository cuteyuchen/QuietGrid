from __future__ import annotations

import asyncio
import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.models import GridDirectionMode
from data_sources.models import NormalizedKline
from scripts import robust_backtest as robust_cli
from scripts import robustness as robustness_module
from scripts.robustness import (
    DynamicModeRule,
    EntryFilter,
    FreezeRequest,
    ParameterSet,
    ResearchConfig,
    RobustnessResearch,
    SymbolResearchPolicy,
    SymbolRules,
    WeekendWindow,
    aggregate_results,
    aggregate_joint_results,
    classify_dynamic_mode,
    freeze_binance_archives,
    generate_entry_filters,
    generate_dynamic_mode_rules,
    generate_parameter_sets,
    generate_wind_down_maker_policies,
    load_weekend_windows,
    parameter_neighbors,
    split_window_ids,
    verify_frozen_dataset,
    write_research_report,
    write_seed_sensitivity_diagnostic,
    write_joint_seed_diagnostic,
    write_joint_oos_report,
    _stable_seed,
)


UTC = timezone.utc


class _ConfigCaptured(RuntimeError):
    pass


def test_robust_backtest_cli_does_not_require_parameter_diagnostic_cost_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_config(**kwargs):
        captured.update(kwargs)
        raise _ConfigCaptured

    monkeypatch.setattr(robust_cli, "ResearchConfig", capture_config)
    args = robust_cli._parser().parse_args(["backtest", "fixture.manifest.json"])

    with pytest.raises(_ConfigCaptured):
        robust_cli._backtest(args)

    assert "maker_fee_rate" not in captured
    assert "taker_fee_rate" not in captured
    assert "stop_slippage_bps" not in captured


def test_parameter_diagnostic_cli_passes_cost_stress_to_research_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_config(**kwargs):
        captured.update(kwargs)
        raise _ConfigCaptured

    monkeypatch.setattr(robust_cli, "ResearchConfig", capture_config)
    args = robust_cli._parser().parse_args(
        [
            "diagnose-parameters",
            "fixture.manifest.json",
            "--maker-fee-rate",
            "0.0003",
            "--taker-fee-rate",
            "0.00075",
            "--stop-slippage-bps",
            "20",
        ]
    )

    with pytest.raises(_ConfigCaptured):
        robust_cli._diagnose_parameters(args)

    assert captured["maker_fee_rate"] == pytest.approx(0.0003)
    assert captured["taker_fee_rate"] == pytest.approx(0.00075)
    assert captured["stop_slippage_bps"] == pytest.approx(20.0)


def test_seed_diagnostic_cli_defaults_to_locked_candidate() -> None:
    args = robust_cli._parser().parse_args(
        ["diagnose-seeds", "fixture.manifest.json"]
    )

    assert args.range_multiplier == pytest.approx(0.9)
    assert args.min_step == pytest.approx(0.00255)
    assert args.stop_buffer == pytest.approx(0.02)
    assert args.reprice_interval == 5
    assert args.initial_offset_steps == pytest.approx(1.1)
    assert args.seed_salts == "17,29,43,59,71,89,101,127,149,173"
    assert args.symbol_capitals == ""
    assert args.max_unpaired_lots_per_side == 0
    assert args.max_directional_efficiency is None
    assert args.max_volatility_expansion is None
    assert args.min_reversal_ratio is None


def test_seed_diagnostic_cli_builds_complete_entry_filter() -> None:
    args = robust_cli._parser().parse_args([
        "diagnose-seeds",
        "fixture.manifest.json",
        "--max-directional-efficiency",
        "0.5",
        "--max-volatility-expansion",
        "1.05",
        "--min-reversal-ratio",
        "0.25",
    ])

    entry_filter = EntryFilter(
        args.max_directional_efficiency,
        args.max_volatility_expansion,
        args.min_reversal_ratio,
    )

    assert entry_filter.filter_id == "de0.50_ve1.05_rr0.25"


def test_joint_seed_cli_defaults_to_locked_symbol_policies() -> None:
    args = robust_cli._parser().parse_args([
        "diagnose-joint-seeds",
        "btc.manifest.json",
        "eth.manifest.json",
    ])

    assert args.btc_capital == pytest.approx(500.0)
    assert args.eth_capital == pytest.approx(300.0)
    assert args.btc_range_multiplier == pytest.approx(1.25)
    assert args.btc_min_step == pytest.approx(0.0018)
    assert args.btc_max_inventory_notional == pytest.approx(200.0)
    assert args.btc_max_unpaired_lots_per_side == 1
    assert args.btc_reduce_target_step_fraction == pytest.approx(0.50)
    assert args.eth_range_multiplier == pytest.approx(1.0)
    assert args.eth_min_step == pytest.approx(0.0018)
    assert args.eth_max_inventory_notional == pytest.approx(120.0)
    assert args.eth_max_unpaired_lots_per_side == 0
    assert args.eth_reduce_target_step_fraction == pytest.approx(1.0)
    assert args.eth_max_directional_efficiency == pytest.approx(0.50)
    assert args.eth_max_volatility_expansion == pytest.approx(1.05)
    assert args.eth_min_reversal_ratio == pytest.approx(0.25)


def test_final_oos_cli_requires_lock_report() -> None:
    parser = robust_cli._parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["finalize-joint-oos", "fixture.manifest.json"])
    args = parser.parse_args([
        "finalize-joint-oos",
        "btc.manifest.json",
        "eth.manifest.json",
        "--lock-report",
        "locked.json",
    ])
    assert args.lock_report == "locked.json"


def test_seed_diagnostic_cli_passes_unpaired_lot_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_config(**kwargs):
        captured.update(kwargs)
        raise _ConfigCaptured

    monkeypatch.setattr(robust_cli, "ResearchConfig", capture_config)
    args = robust_cli._parser().parse_args([
        "diagnose-seeds",
        "fixture.manifest.json",
        "--max-unpaired-lots-per-side",
        "2",
        "--reduce-target-step-fraction",
        "0.75",
    ])

    with pytest.raises(_ConfigCaptured):
        robust_cli._diagnose_seeds(args)

    assert captured["max_unpaired_lots_per_side"] == 2
    assert captured["reduce_target_step_fraction"] == pytest.approx(0.75)


def test_window_diagnostic_cli_accepts_exact_seed_cost_and_unwind_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_config(**kwargs):
        captured.update(kwargs)
        raise _ConfigCaptured

    monkeypatch.setattr(robust_cli, "ResearchConfig", capture_config)
    args = robust_cli._parser().parse_args([
        "diagnose-windows",
        "fixture.manifest.json",
        "--max-inventory-notional",
        "120",
        "--reprice-interval",
        "5",
        "--initial-offset-steps",
        "1.1",
        "--unwind-fraction",
        "1.0",
        "--max-unpaired-lots-per-side",
        "2",
        "--reduce-target-step-fraction",
        "0.5",
        "--maker-fee-rate",
        "0.0003",
        "--taker-fee-rate",
        "0.00075",
        "--stop-slippage-bps",
        "20",
        "--seed-salt",
        "59",
    ])

    with pytest.raises(_ConfigCaptured):
        robust_cli._diagnose_windows(args)

    assert captured["max_inventory_notional"] == pytest.approx(120)
    assert captured["wind_down_reprice_interval_bars"] == 5
    assert captured["wind_down_initial_offset_steps"] == pytest.approx(1.1)
    assert captured["wind_down_unwind_fraction"] == pytest.approx(1.0)
    assert captured["max_unpaired_lots_per_side"] == 2
    assert captured["reduce_target_step_fraction"] == pytest.approx(0.5)
    assert captured["maker_fee_rate"] == pytest.approx(0.0003)
    assert captured["taker_fee_rate"] == pytest.approx(0.00075)
    assert captured["stop_slippage_bps"] == pytest.approx(20)


def test_entry_diagnostic_cli_accepts_exact_seed_cost_and_unwind_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def capture_config(**kwargs):
        captured.update(kwargs)
        raise _ConfigCaptured

    monkeypatch.setattr(robust_cli, "ResearchConfig", capture_config)
    args = robust_cli._parser().parse_args([
        "diagnose-entry",
        "fixture.manifest.json",
        "--wind-down-bars",
        "1440",
        "--max-inventory-notional",
        "120",
        "--reprice-interval",
        "5",
        "--initial-offset-steps",
        "1.1",
        "--unwind-fraction",
        "1.0",
        "--maker-fee-rate",
        "0.0003",
        "--taker-fee-rate",
        "0.00075",
        "--stop-slippage-bps",
        "20",
        "--seed-salt",
        "59",
    ])

    with pytest.raises(_ConfigCaptured):
        robust_cli._diagnose_entry(args)

    assert captured["wind_down_bars"] == 1440
    assert captured["max_inventory_notional"] == pytest.approx(120)
    assert captured["wind_down_reprice_interval_bars"] == 5
    assert captured["wind_down_initial_offset_steps"] == pytest.approx(1.1)
    assert captured["maker_fee_rate"] == pytest.approx(0.0003)
    assert captured["taker_fee_rate"] == pytest.approx(0.00075)
    assert captured["stop_slippage_bps"] == pytest.approx(20)


def test_symbol_capitals_parser_normalizes_and_validates() -> None:
    assert robust_cli._symbol_capitals("btcusdt=800,ETHUSDT=200") == {
        "BTCUSDT": 800.0,
        "ETHUSDT": 200.0,
    }
    with pytest.raises(Exception, match="SYMBOL=CAPITAL"):
        robust_cli._symbol_capitals("BTCUSDT")
    with pytest.raises(Exception, match="必须为正"):
        robust_cli._symbol_capitals("BTCUSDT=0")


def test_fill_seed_salt_is_reproducible_and_changes_sampling_seed() -> None:
    first = _stable_seed("17", "parameter", "BTCUSDT", "window")
    repeated = _stable_seed("17", "parameter", "BTCUSDT", "window")
    different = _stable_seed("29", "parameter", "BTCUSDT", "window")

    assert first == repeated
    assert first != different


def test_research_supports_symbol_specific_capital() -> None:
    start = datetime(2026, 1, 2, 20, 0, tzinfo=UTC)
    rows = tuple(
        _row(start + timedelta(minutes=minute), 100.0)
        for minute in range(100)
    )
    research = RobustnessResearch(
        [WeekendWindow(
            symbol="BTCUSDT",
            window_id="w1",
            market_close=start,
            force_close_at=start + timedelta(minutes=100),
            rows=rows,
            observation_rows=61,
            status="READY",
        )],
        [ParameterSet(1.0, 0.003, 0.01, GridDirectionMode.NEUTRAL)],
        ResearchConfig(
            observation_rows=61,
            minimum_tradable_rows=30,
            min_windows_per_split=1,
            capital_by_symbol={"BTCUSDT": 800.0},
        ),
        symbol_rules={"BTCUSDT": SymbolRules(0.01, 0.001, 0.0, 0.0)},
    )

    assert research._capital_for_symbol("btcusdt") == pytest.approx(800.0)
    assert research._capital_for_symbol("ETHUSDT") == pytest.approx(500.0)


class FakeArchiveSource:
    market_path = "futures/um"
    verify_official_checksum = True
    source_segments: list = []

    def __init__(self, rows: list[NormalizedKline], available: datetime) -> None:
        self.rows = rows
        self.available = available
        self.closed = False

    def archive_available_until(self):
        return self.available.date()

    async def fetch_klines(self, symbol, interval, start_time, end_time):
        del symbol, interval
        for row in self.rows:
            if start_time.timestamp() * 1000 <= row.open_time < end_time.timestamp() * 1000:
                yield row

    async def close(self):
        self.closed = True


def test_freeze_archive_is_immutable_and_verifiable(tmp_path: Path) -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    rows = [_row(start + timedelta(minutes=index), 100 + index) for index in range(5)]
    source = FakeArchiveSource(rows, start + timedelta(days=2))

    data_path, manifest_path = asyncio.run(
        freeze_binance_archives(
            FreezeRequest(
                symbol="BTCUSDT",
                start_time=start,
                end_time=start + timedelta(hours=1),
                output_dir=tmp_path,
            ),
            source_factory=lambda: source,
        )
    )

    manifest = verify_frozen_dataset(manifest_path)
    assert source.closed is True
    assert manifest["symbol"] == "BTCUSDT"
    assert manifest["row_count"] == 5
    assert manifest["missing_intervals"] == 0
    assert manifest["official_checksums_verified"] is True
    assert data_path.name == manifest["file_name"]

    with data_path.open("a", encoding="utf-8") as handle:
        handle.write("tampered\n")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_frozen_dataset(manifest_path)


def test_weekend_window_excludes_normal_weekday_overnight(tmp_path: Path) -> None:
    friday_close = datetime(2026, 7, 17, 20, 0, tzinfo=UTC)
    monday_force_close = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    rows = [
        _row(friday_close + timedelta(minutes=index), 100 + (index % 7) * 0.01)
        for index in range(int((monday_force_close - friday_close).total_seconds() // 60))
    ]
    manifest_path = _frozen_fixture(tmp_path, "BTCUSDT", rows)

    windows = load_weekend_windows(
        manifest_path,
        observation_rows=180,
        minimum_tradable_rows=30,
    )

    assert len(windows) == 1
    assert windows[0].status == "READY"
    assert windows[0].market_close == friday_close
    assert windows[0].force_close_at == monday_force_close
    assert windows[0].tradable_rows == len(rows) - 180

    thursday_close = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)
    friday_force_close = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    weekday_rows = [
        _row(thursday_close + timedelta(minutes=index), 100.0)
        for index in range(int((friday_force_close - thursday_close).total_seconds() // 60))
    ]
    weekday_manifest = _frozen_fixture(
        tmp_path / "weekday",
        "ETHUSDT",
        weekday_rows,
    )
    assert load_weekend_windows(weekday_manifest) == []


def test_weekend_window_preserves_only_pre_close_history(tmp_path: Path) -> None:
    friday_close = datetime(2026, 7, 17, 20, 0, tzinfo=UTC)
    monday_force_close = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
    start = friday_close - timedelta(minutes=90)
    row_count = int((monday_force_close - start).total_seconds() // 60)
    rows = [
        _row(start + timedelta(minutes=index), 100 + index * 0.001)
        for index in range(row_count)
    ]
    manifest_path = _frozen_fixture(tmp_path, "BTCUSDT", rows)

    windows = load_weekend_windows(
        manifest_path,
        observation_rows=180,
        minimum_tradable_rows=30,
        history_rows=60,
    )

    assert len(windows) == 1
    assert len(windows[0].history_rows) == 60
    assert windows[0].history_rows[-1].open_datetime < friday_close
    assert windows[0].rows[0].open_datetime == friday_close


def test_split_is_strictly_chronological_and_never_overlaps() -> None:
    windows = [
        _empty_window(f"nyse_20260{month:02d}01T200000Z")
        for month in range(1, 10)
    ]
    split = split_window_ids(
        windows,
        dev_ratio=0.5,
        validation_ratio=0.25,
        min_windows_per_split=2,
    )

    assert not (set(split.development) & set(split.validation))
    assert not (set(split.validation) & set(split.final_oos))
    assert max(split.development) < min(split.validation)
    assert max(split.validation) < min(split.final_oos)
    assert len(split.development) + len(split.validation) + len(split.final_oos) == 9


def test_parameter_neighborhood_uses_one_axis_step_only() -> None:
    universe = generate_parameter_sets(
        range_multipliers=[0.75, 1.0, 1.25],
        min_step_pcts=[0.0012, 0.0015, 0.0018],
        stop_buffer_pcts=[0.01, 0.015, 0.02],
        direction_modes=[GridDirectionMode.NEUTRAL],
    )
    target = next(
        item
        for item in universe
        if item.range_multiplier == 1.0
        and item.min_step_pct == 0.0015
        and item.stop_buffer_pct == 0.015
    )

    neighbors = parameter_neighbors(target, universe)

    assert len(neighbors) == 6
    assert all(item.direction_mode == GridDirectionMode.NEUTRAL for item in neighbors)


def test_entry_filter_grid_validates_ranges() -> None:
    filters = generate_entry_filters(
        max_directional_efficiencies=[0.2, 0.4],
        max_volatility_expansions=[0.8, 1.2],
        min_reversal_ratios=[0.3],
    )

    assert len(filters) == 4
    assert len({item.filter_id for item in filters}) == 4
    with pytest.raises(ValueError, match="directional_efficiency"):
        generate_entry_filters(
            max_directional_efficiencies=[1.1],
            max_volatility_expansions=[1.0],
            min_reversal_ratios=[0.3],
        )


def test_dynamic_mode_classifier_uses_closed_long_history() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        _row(start + timedelta(minutes=index), 100 + index * 0.05)
        for index in range(120)
    ]
    rule = DynamicModeRule(
        lookback_rows=60,
        directional_threshold=0.8,
        neutral_threshold=0.3,
        min_persistence=0.5,
        segment_rows=15,
    )

    decision = classify_dynamic_mode(rows, rule)

    assert decision.direction_mode == GridDirectionMode.LONG
    assert decision.reason == "PERSISTENT_UPTREND"
    assert decision.lookback_rows == 60
    assert decision.persistence == 1.0

    contrarian = classify_dynamic_mode(
        rows,
        DynamicModeRule(60, 0.8, 0.3, 0.5, 15, "CONTRARIAN"),
    )
    assert contrarian.direction_mode == GridDirectionMode.SHORT


def test_dynamic_mode_rule_grid_rejects_invalid_threshold_order() -> None:
    rules = generate_dynamic_mode_rules(
        lookback_rows=[1440],
        directional_thresholds=[0.8],
        neutral_thresholds=[0.3, 1.0],
        min_persistences=[0.5],
        trend_alignments=["MOMENTUM", "CONTRARIAN"],
    )

    assert len(rules) == 2
    assert rules[0].neutral_threshold == 0.3


def test_wind_down_maker_policy_grid_validates_inputs() -> None:
    policies = generate_wind_down_maker_policies(
        reprice_intervals=[15, 60],
        initial_offset_steps=[0.25, 0.5],
        unwind_fractions=[0.5, 1.0],
    )

    assert len(policies) == 8
    with pytest.raises(ValueError, match="正整数"):
        generate_wind_down_maker_policies(
            reprice_intervals=[0],
            initial_offset_steps=[0.5],
        )


def test_aggregate_results_handles_no_losing_window_without_infinity() -> None:
    from scripts.robustness import WindowResult

    rows = [
        WindowResult(
            parameter_id="p",
            symbol="BTCUSDT",
            window_id="w1",
            market_close="2026-01-01T00:00:00+00:00",
            status="TRADED",
            reason="completed",
            pnl=2.0,
        ),
        WindowResult(
            parameter_id="p",
            symbol="ETHUSDT",
            window_id="w1",
            market_close="2026-01-01T00:00:00+00:00",
            status="BLOCKED",
            reason="score",
        ),
    ]
    metrics = aggregate_results(rows, capital_per_symbol=500, symbol_count=2)

    assert metrics.total_pnl == 2.0
    assert metrics.profit_factor is None
    assert metrics.trade_coverage == 0.5
    assert json.dumps(metrics.__dict__, allow_nan=False)


def test_joint_aggregate_uses_gross_profit_loss_and_conservative_risk() -> None:
    from scripts.robustness import WindowResult

    rows = [
        WindowResult(
            parameter_id="btc",
            symbol="BTCUSDT",
            window_id="w1",
            market_close="2026-01-01T00:00:00+00:00",
            status="TRADED",
            reason="completed",
            pnl=10.0,
        ),
        WindowResult(
            parameter_id="eth",
            symbol="ETHUSDT",
            window_id="w1",
            market_close="2026-01-01T00:00:00+00:00",
            status="TRADED",
            reason="completed",
            pnl=-8.0,
        ),
        WindowResult(
            parameter_id="btc",
            symbol="BTCUSDT",
            window_id="w2",
            market_close="2026-01-08T00:00:00+00:00",
            status="TRADED",
            reason="completed",
            pnl=-4.0,
        ),
        WindowResult(
            parameter_id="eth",
            symbol="ETHUSDT",
            window_id="w2",
            market_close="2026-01-08T00:00:00+00:00",
            status="TRADED",
            reason="completed",
            pnl=3.0,
        ),
    ]

    metrics = aggregate_joint_results(
        rows,
        capital_by_symbol={"BTCUSDT": 500.0, "ETHUSDT": 300.0},
    )

    assert metrics.total_pnl == pytest.approx(1.0)
    assert metrics.profit_factor == pytest.approx(13.0 / 12.0)
    # Portfolio netting would only show a 1 USDT drawdown.  The conservative
    # bound adds BTC and ETH standalone drawdowns (4 + 8).
    assert metrics.max_drawdown == pytest.approx(12.0)
    assert metrics.max_drawdown_pct == pytest.approx(12.0 / 800.0)
    assert metrics.best_window_concentration == pytest.approx(1.0)


def test_small_synthetic_research_writes_json_and_markdown(tmp_path: Path) -> None:
    windows = []
    for index in range(3):
        start = datetime(2026, 1, 2, 20, 0, tzinfo=UTC) + timedelta(days=7 * index)
        rows = tuple(
            _row(
                start + timedelta(minutes=minute),
                100.0 + (0.35 if minute % 2 else -0.35),
            )
            for minute in range(130)
        )
        windows.append(
            WeekendWindow(
                symbol="BTCUSDT",
                window_id=f"nyse_{start:%Y%m%dT%H%M%SZ}",
                market_close=start,
                force_close_at=start + timedelta(minutes=130),
                rows=rows,
                observation_rows=61,
                status="READY",
            )
        )
    parameter = ParameterSet(1.0, 0.003, 0.01, GridDirectionMode.NEUTRAL)
    research = RobustnessResearch(
        windows,
        [parameter],
        ResearchConfig(
            observation_rows=61,
            minimum_tradable_rows=30,
            min_windows_per_split=1,
            walk_forward_train_windows=1,
            walk_forward_test_windows=1,
            walk_forward_step_windows=1,
            wind_down_bars=30,
            max_unpaired_lots_per_side=2,
        ),
        symbol_rules={"BTCUSDT": SymbolRules(0.01, 0.001, 0.0, 0.0)},
    )

    diagnostic = research.diagnose_entry_filters(
        parameter,
        [EntryFilter(1.0, 10.0, 0.0)],
    )
    final_window_id = windows[-1].window_id
    assert diagnostic["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)

    exit_diagnostic = research.diagnose_exit_policies(parameter, [0, 30])
    assert exit_diagnostic["candidate_count"] == 2
    assert exit_diagnostic["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)

    parameter_diagnostic = research.diagnose_parameters()
    assert parameter_diagnostic["candidate_count"] == 1
    assert parameter_diagnostic["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)

    window_diagnostic = research.diagnose_window_paths(parameter)
    assert window_diagnostic["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert window_diagnostic["development"]["windows"]
    for item in window_diagnostic["development"]["windows"]:
        assert item["paired_grid_pnl"] == pytest.approx(
            item["gross_grid_pnl"] - item["stop_exit_pnl"]
        )
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)

    inventory_diagnostic = research.diagnose_inventory_policies(
        parameter,
        [100, 200],
    )
    assert inventory_diagnostic["candidate_count"] == 2
    assert inventory_diagnostic["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)

    unwind_diagnostic = research.diagnose_wind_down_maker(
        parameter,
        generate_wind_down_maker_policies(
            reprice_intervals=[5],
            initial_offset_steps=[0.5],
        ),
    )
    assert unwind_diagnostic["candidate_count"] == 1
    assert unwind_diagnostic["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)

    seed_diagnostic = research.diagnose_fill_seeds(
        parameter,
        generate_wind_down_maker_policies(
            reprice_intervals=[5],
            initial_offset_steps=[0.5],
        )[0],
        [17, 29],
    )
    repeated_seed_diagnostic = research.diagnose_fill_seeds(
        parameter,
        generate_wind_down_maker_policies(
            reprice_intervals=[5],
            initial_offset_steps=[0.5],
        )[0],
        [17, 29],
    )
    filtered_seed_diagnostic = research.diagnose_fill_seeds(
        parameter,
        generate_wind_down_maker_policies(
            reprice_intervals=[5],
            initial_offset_steps=[0.5],
        )[0],
        [17],
        entry_filter=EntryFilter(0.5, 1.05, 0.25),
    )
    assert seed_diagnostic["seed_salts"] == [17, 29]
    assert seed_diagnostic["backtest_policy"]["max_unpaired_lots_per_side"] == 2
    assert seed_diagnostic["seeds"] == repeated_seed_diagnostic["seeds"]
    assert filtered_seed_diagnostic["entry_filter"]["filter_id"] == "de0.50_ve1.05_rr0.25"
    assert seed_diagnostic["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)
    assert {cache_key[-1] for cache_key in research._cache} >= {None, 17, 29}

    seed_json, seed_md = write_seed_sensitivity_diagnostic(
        seed_diagnostic,
        tmp_path,
        stem="seeds",
    )
    assert json.loads(seed_json.read_text(encoding="utf-8"))["seed_salts"] == [17, 29]
    assert "SEALED_NOT_EVALUATED" in seed_md.read_text(encoding="utf-8")

    report = research.run()
    json_path, md_path = write_research_report(report, tmp_path, stem="small")

    assert report["selected_parameter"]["parameter_id"] == parameter.parameter_id
    assert report["split"]["final_oos"]["count"] == 1
    assert json.loads(json_path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert "最终 OOS" in md_path.read_text(encoding="utf-8")


def test_joint_seed_diagnostic_keeps_final_oos_sealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_symbol_policies: set[tuple[int, float]] = set()
    original_run_grid_backtest = robustness_module.run_grid_backtest

    def capture_backtest_policy(*args, **kwargs):
        config = args[3]
        captured_symbol_policies.add((
            config.max_unpaired_lots_per_side,
            config.reduce_target_step_fraction,
        ))
        return original_run_grid_backtest(*args, **kwargs)

    monkeypatch.setattr(
        robustness_module,
        "run_grid_backtest",
        capture_backtest_policy,
    )
    windows = []
    for index in range(3):
        start = datetime(2026, 1, 2, 20, 0, tzinfo=UTC) + timedelta(days=7 * index)
        window_id = f"nyse_{start:%Y%m%dT%H%M%SZ}"
        for symbol, offset in (("BTCUSDT", 0.0), ("ETHUSDT", 0.2)):
            rows = tuple(
                _row(
                    start + timedelta(minutes=minute),
                    100.0 + offset + (0.35 if minute % 2 else -0.35),
                )
                for minute in range(130)
            )
            windows.append(WeekendWindow(
                symbol=symbol,
                window_id=window_id,
                market_close=start,
                force_close_at=start + timedelta(minutes=130),
                rows=rows,
                observation_rows=61,
                status="READY",
            ))
    btc = ParameterSet(1.0, 0.0030, 0.01, GridDirectionMode.NEUTRAL)
    eth = ParameterSet(1.1, 0.0035, 0.01, GridDirectionMode.NEUTRAL)
    research = RobustnessResearch(
        windows,
        [btc, eth],
        ResearchConfig(
            capital_per_symbol=400,
            capital_by_symbol={"BTCUSDT": 500, "ETHUSDT": 300},
            observation_rows=61,
            minimum_tradable_rows=30,
            min_windows_per_split=1,
            wind_down_bars=30,
        ),
        symbol_rules={
            "BTCUSDT": SymbolRules(0.01, 0.001, 0.0, 0.0),
            "ETHUSDT": SymbolRules(0.01, 0.001, 0.0, 0.0),
        },
    )
    report = research.diagnose_joint_fill_seeds(
        {
            "BTCUSDT": SymbolResearchPolicy(
                btc,
                100.0,
                max_unpaired_lots_per_side=1,
                reduce_target_step_fraction=0.5,
            ),
            "ETHUSDT": SymbolResearchPolicy(
                eth,
                80.0,
                EntryFilter(1.0, 10.0, 0.0),
                max_unpaired_lots_per_side=0,
                reduce_target_step_fraction=1.0,
            ),
        },
        generate_wind_down_maker_policies(
            reprice_intervals=[5],
            initial_offset_steps=[0.5],
        )[0],
        [17],
    )
    final_window_id = sorted({item.window_id for item in windows})[-1]

    assert report["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert report["symbol_policies"]["BTCUSDT"]["capital"] == 500
    assert report["symbol_policies"]["BTCUSDT"]["max_unpaired_lots_per_side"] == 1
    assert report["symbol_policies"]["BTCUSDT"]["reduce_target_step_fraction"] == pytest.approx(0.5)
    assert report["symbol_policies"]["ETHUSDT"]["max_unpaired_lots_per_side"] == 0
    assert report["symbol_policies"]["ETHUSDT"]["reduce_target_step_fraction"] == pytest.approx(1.0)
    assert captured_symbol_policies >= {(1, 0.5), (0, 1.0)}
    assert report["symbol_policies"]["ETHUSDT"]["entry_filter"]["filter_id"] == "de1.00_ve10.00_rr0.00"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)
    json_path, md_path = write_joint_seed_diagnostic(report, tmp_path, stem="joint")
    assert json.loads(json_path.read_text(encoding="utf-8"))["seed_salts"] == [17]
    assert "SEALED_NOT_EVALUATED" in md_path.read_text(encoding="utf-8")

    final_report = research.evaluate_locked_joint_oos(
        {
            "BTCUSDT": SymbolResearchPolicy(
                btc,
                100.0,
                max_unpaired_lots_per_side=1,
                reduce_target_step_fraction=0.5,
            ),
            "ETHUSDT": SymbolResearchPolicy(
                eth,
                80.0,
                EntryFilter(1.0, 10.0, 0.0),
                max_unpaired_lots_per_side=0,
                reduce_target_step_fraction=1.0,
            ),
        },
        generate_wind_down_maker_policies(
            reprice_intervals=[5],
            initial_offset_steps=[0.5],
        )[0],
        [17],
        lock_report_sha256="a" * 64,
        lock_report_generated_at=report["generated_at"],
    )
    assert final_report["split"]["final_oos"]["status"] == "EVALUATED_ONCE"
    assert final_report["lock_report"]["sha256"] == "a" * 64
    assert any(cache_key[2] == final_window_id for cache_key in research._cache)
    final_json, final_md = write_joint_oos_report(
        final_report,
        tmp_path,
        stem="joint-final",
    )
    assert json.loads(final_json.read_text(encoding="utf-8"))["seed_salts"] == [17]
    assert "EVALUATED_ONCE" in final_md.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_joint_oos_report(final_report, tmp_path, stem="joint-final")


def test_dynamic_mode_diagnostic_keeps_final_oos_sealed() -> None:
    windows = []
    for index in range(3):
        start = datetime(2026, 1, 2, 20, 0, tzinfo=UTC) + timedelta(days=7 * index)
        history = tuple(
            _row(
                start - timedelta(minutes=60 - minute),
                95.0 + minute * 0.05,
            )
            for minute in range(60)
        )
        rows = tuple(
            _row(start + timedelta(minutes=minute), 100.0 + (minute % 4) * 0.1)
            for minute in range(130)
        )
        windows.append(WeekendWindow(
            symbol="BTCUSDT",
            window_id=f"nyse_{start:%Y%m%dT%H%M%SZ}",
            market_close=start,
            force_close_at=start + timedelta(minutes=130),
            rows=rows,
            observation_rows=61,
            status="READY",
            history_rows=history,
        ))
    parameters = generate_parameter_sets(
        range_multipliers=[1.0],
        min_step_pcts=[0.003],
        stop_buffer_pcts=[0.01],
        direction_modes=list(GridDirectionMode),
    )
    research = RobustnessResearch(
        windows,
        parameters,
        ResearchConfig(
            observation_rows=61,
            minimum_tradable_rows=30,
            min_windows_per_split=1,
            walk_forward_train_windows=1,
            walk_forward_test_windows=1,
            walk_forward_step_windows=1,
        ),
        symbol_rules={"BTCUSDT": SymbolRules(0.01, 0.001, 0.0, 0.0)},
    )
    base = next(
        item for item in parameters
        if item.direction_mode == GridDirectionMode.NEUTRAL
    )

    report = research.diagnose_dynamic_modes(
        base,
        [DynamicModeRule(60, 0.8, 0.3, 0.5, 15)],
    )
    final_window_id = windows[-1].window_id

    assert report["candidate_count"] == 1
    assert report["split"]["final_oos"]["status"] == "SEALED_NOT_EVALUATED"
    assert all(cache_key[2] != final_window_id for cache_key in research._cache)


def _row(moment: datetime, close: float) -> NormalizedKline:
    opened = int(moment.timestamp() * 1000)
    return NormalizedKline(
        open_time=opened,
        close_time=opened + 59_999,
        open=close,
        high=close + 0.1,
        low=max(0.0001, close - 0.1),
        close=close,
        volume=1.0,
        quote_volume=close,
        trade_count=1,
    )


def _frozen_fixture(
    directory: Path,
    symbol: str,
    rows: list[NormalizedKline],
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    data_path = directory / f"{symbol.lower()}.csv"
    with data_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_volume", "trade_count",
            ),
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "open_time": row.open_time,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "close_time": row.close_time,
                "quote_volume": row.quote_volume,
                "trade_count": row.trade_count,
            })
    checksum = hashlib.sha256(data_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "dataset_id": symbol.lower(),
        "symbol": symbol,
        "file_name": data_path.name,
        "file_sha256": checksum,
        "row_count": len(rows),
        "actual_start": rows[0].open_datetime.isoformat(),
        "actual_end": datetime.fromtimestamp(
            (rows[-1].open_time + 60_000) / 1000,
            tz=UTC,
        ).isoformat(),
    }
    manifest_path = directory / f"{symbol.lower()}.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def _empty_window(window_id: str) -> WeekendWindow:
    return WeekendWindow(
        symbol="BTCUSDT",
        window_id=window_id,
        market_close=datetime(2026, 1, 1, tzinfo=UTC),
        force_close_at=datetime(2026, 1, 2, tzinfo=UTC),
        rows=(),
        observation_rows=180,
        status="READY",
    )
