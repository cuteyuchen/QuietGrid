"""Command-line entry point for QuietGrid archive and robustness research."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.models import GridDirectionMode
from scripts.robustness import (
    EntryFilter,
    FreezeRequest,
    ParameterSet,
    ResearchConfig,
    RobustnessResearch,
    SymbolResearchPolicy,
    freeze_binance_archives,
    generate_dynamic_mode_rules,
    generate_entry_filters,
    generate_parameter_sets,
    generate_wind_down_maker_policies,
    load_weekend_windows,
    verify_frozen_dataset,
    write_entry_filter_diagnostic,
    write_dynamic_mode_diagnostic,
    write_exit_policy_diagnostic,
    write_inventory_diagnostic,
    write_parameter_diagnostic,
    write_research_report,
    write_seed_sensitivity_diagnostic,
    write_joint_seed_diagnostic,
    write_joint_oos_report,
    write_window_diagnostic,
    write_wind_down_maker_diagnostic,
)
from data_sources.binance_archive_source import BinanceArchiveHistoricalDataSource


UTC = timezone.utc


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "download":
        asyncio.run(_download(args))
        return 0
    if args.command == "verify":
        for item in args.manifests:
            manifest = verify_frozen_dataset(item)
            print(f"OK {manifest['dataset_id']} rows={manifest['row_count']}")
        return 0
    if args.command == "backtest":
        _backtest(args)
        return 0
    if args.command == "diagnose-entry":
        _diagnose_entry(args)
        return 0
    if args.command == "diagnose-exit":
        _diagnose_exit(args)
        return 0
    if args.command == "diagnose-parameters":
        _diagnose_parameters(args)
        return 0
    if args.command == "diagnose-windows":
        _diagnose_windows(args)
        return 0
    if args.command == "diagnose-inventory":
        _diagnose_inventory(args)
        return 0
    if args.command == "diagnose-dynamic-mode":
        _diagnose_dynamic_mode(args)
        return 0
    if args.command == "diagnose-unwind":
        _diagnose_unwind(args)
        return 0
    if args.command == "diagnose-seeds":
        _diagnose_seeds(args)
        return 0
    if args.command == "diagnose-joint-seeds":
        _diagnose_joint_seeds(args)
        return 0
    if args.command == "finalize-joint-oos":
        _finalize_joint_oos(args)
        return 0
    parser.error(f"未知命令: {args.command}")
    return 2


async def _download(args: argparse.Namespace) -> None:
    end = _parse_datetime(args.end) if args.end else datetime.now(UTC)
    start = _parse_datetime(args.start) if args.start else end - timedelta(days=730)
    for symbol in _csv_values(args.symbols):
        source_factory = None
        if args.proxy_url:
            source_factory = lambda: BinanceArchiveHistoricalDataSource(
                proxy_config={
                    "enabled": True,
                    "https": args.proxy_url,
                }
            )
        data_path, manifest_path = await freeze_binance_archives(
            FreezeRequest(
                symbol=symbol,
                start_time=start,
                end_time=end,
                output_dir=Path(args.output_dir),
                max_missing_ratio=args.max_missing_ratio,
            ),
            source_factory=source_factory,
        )
        print(f"FROZEN {symbol}: {data_path}")
        print(f"MANIFEST {manifest_path}")


def _backtest(args: argparse.Namespace) -> None:
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        walk_forward_train_windows=args.walk_forward_train,
        walk_forward_test_windows=args.walk_forward_test,
        walk_forward_step_windows=args.walk_forward_step,
        wind_down_bars=args.wind_down_bars,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameters = generate_parameter_sets(
        range_multipliers=_floats(args.range_multipliers),
        min_step_pcts=_floats(args.min_steps),
        stop_buffer_pcts=_floats(args.stop_buffers),
        direction_modes=[
            GridDirectionMode(value.upper())
            for value in _csv_values(args.direction_modes)
        ],
    )
    report = RobustnessResearch(
        windows,
        parameters,
        config,
        dataset_metadata=manifests,
    ).run()
    json_path, md_path = write_research_report(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(
        "STABILITY "
        + ("PASSED" if report["stability"]["passed"] else "NOT_PASSED")
    )


def _diagnose_entry(args: argparse.Namespace) -> None:
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
        max_inventory_notional=args.max_inventory_notional,
        wind_down_reprice_interval_bars=args.reprice_interval,
        wind_down_initial_offset_steps=args.initial_offset_steps,
        wind_down_unwind_fraction=args.unwind_fraction,
        max_unpaired_lots_per_side=args.max_unpaired_lots_per_side,
        maker_fee_rate=args.maker_fee_rate,
        taker_fee_rate=args.taker_fee_rate,
        stop_slippage_bps=args.stop_slippage_bps,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameter = ParameterSet(
        range_multiplier=args.range_multiplier,
        min_step_pct=args.min_step,
        stop_buffer_pct=args.stop_buffer,
        direction_mode=GridDirectionMode(args.direction_mode.upper()),
    )
    filters = generate_entry_filters(
        max_directional_efficiencies=_floats(args.max_directional_efficiencies),
        max_volatility_expansions=_floats(args.max_volatility_expansions),
        min_reversal_ratios=_floats(args.min_reversal_ratios),
    )
    report = RobustnessResearch(
        windows,
        [parameter],
        config,
        dataset_metadata=manifests,
    ).diagnose_entry_filters(
        parameter,
        filters,
        fill_seed_salt=args.seed_salt,
    )
    json_path, md_path = write_entry_filter_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(f"PASSED_CANDIDATES {report['passed_count']}/{report['candidate_count']}")


def _diagnose_exit(args: argparse.Namespace) -> None:
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameter = ParameterSet(
        range_multiplier=args.range_multiplier,
        min_step_pct=args.min_step,
        stop_buffer_pct=args.stop_buffer,
        direction_mode=GridDirectionMode(args.direction_mode.upper()),
    )
    report = RobustnessResearch(
        windows,
        [parameter],
        config,
        dataset_metadata=manifests,
    ).diagnose_exit_policies(
        parameter,
        [int(value) for value in _csv_values(args.wind_down_values)],
    )
    json_path, md_path = write_exit_policy_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(f"PASSED_CANDIDATES {report['passed_count']}/{report['candidate_count']}")


def _diagnose_parameters(args: argparse.Namespace) -> None:
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        maker_fee_rate=args.maker_fee_rate,
        taker_fee_rate=args.taker_fee_rate,
        stop_slippage_bps=args.stop_slippage_bps,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
        max_inventory_notional=args.max_inventory_notional,
        wind_down_reprice_interval_bars=(
            args.wind_down_reprice_interval_bars
        ),
        wind_down_initial_offset_steps=(
            args.wind_down_initial_offset_steps
        ),
        wind_down_unwind_fraction=args.wind_down_unwind_fraction,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameters = generate_parameter_sets(
        range_multipliers=_floats(args.range_multipliers),
        min_step_pcts=_floats(args.min_steps),
        stop_buffer_pcts=_floats(args.stop_buffers),
        direction_modes=[
            GridDirectionMode(value.upper())
            for value in _csv_values(args.direction_modes)
        ],
    )
    report = RobustnessResearch(
        windows,
        parameters,
        config,
        dataset_metadata=manifests,
    ).diagnose_parameters()
    json_path, md_path = write_parameter_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(f"PASSED_CANDIDATES {report['passed_count']}/{report['candidate_count']}")


def _diagnose_windows(args: argparse.Namespace) -> None:
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
        max_inventory_notional=args.max_inventory_notional,
        wind_down_reprice_interval_bars=args.reprice_interval,
        wind_down_initial_offset_steps=args.initial_offset_steps,
        wind_down_unwind_fraction=args.unwind_fraction,
        max_unpaired_lots_per_side=args.max_unpaired_lots_per_side,
        maker_fee_rate=args.maker_fee_rate,
        taker_fee_rate=args.taker_fee_rate,
        stop_slippage_bps=args.stop_slippage_bps,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameter = ParameterSet(
        range_multiplier=args.range_multiplier,
        min_step_pct=args.min_step,
        stop_buffer_pct=args.stop_buffer,
        direction_mode=GridDirectionMode(args.direction_mode.upper()),
    )
    report = RobustnessResearch(
        windows,
        [parameter],
        config,
        dataset_metadata=manifests,
    ).diagnose_window_paths(parameter, fill_seed_salt=args.seed_salt)
    json_path, md_path = write_window_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")


def _diagnose_inventory(args: argparse.Namespace) -> None:
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameter = ParameterSet(
        range_multiplier=args.range_multiplier,
        min_step_pct=args.min_step,
        stop_buffer_pct=args.stop_buffer,
        direction_mode=GridDirectionMode(args.direction_mode.upper()),
    )
    report = RobustnessResearch(
        windows,
        [parameter],
        config,
        dataset_metadata=manifests,
    ).diagnose_inventory_policies(
        parameter,
        _floats(args.max_inventory_values),
    )
    json_path, md_path = write_inventory_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(f"PASSED_CANDIDATES {report['passed_count']}/{report['candidate_count']}")


def _diagnose_dynamic_mode(args: argparse.Namespace) -> None:
    rules = generate_dynamic_mode_rules(
        lookback_rows=[int(value) for value in _csv_values(args.lookbacks)],
        directional_thresholds=_floats(args.directional_thresholds),
        neutral_thresholds=_floats(args.neutral_thresholds),
        min_persistences=_floats(args.min_persistences),
        segment_rows=args.segment_rows,
        trend_alignments=_csv_values(args.trend_alignments),
    )
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
        max_inventory_notional=args.max_inventory_notional,
    )
    windows = []
    manifests = []
    required_history = max(item.lookback_rows for item in rules)
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
                history_rows=required_history,
            )
        )
    parameters = generate_parameter_sets(
        range_multipliers=[args.range_multiplier],
        min_step_pcts=[args.min_step],
        stop_buffer_pcts=[args.stop_buffer],
        direction_modes=list(GridDirectionMode),
    )
    base_parameter = next(
        item
        for item in parameters
        if item.direction_mode == GridDirectionMode.NEUTRAL
    )
    report = RobustnessResearch(
        windows,
        parameters,
        config,
        dataset_metadata=manifests,
    ).diagnose_dynamic_modes(base_parameter, rules)
    json_path, md_path = write_dynamic_mode_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(f"PASSED_CANDIDATES {report['passed_count']}/{report['candidate_count']}")


def _diagnose_unwind(args: argparse.Namespace) -> None:
    config = ResearchConfig(
        capital_per_symbol=args.capital,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
        max_inventory_notional=args.max_inventory_notional,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameter = ParameterSet(
        range_multiplier=args.range_multiplier,
        min_step_pct=args.min_step,
        stop_buffer_pct=args.stop_buffer,
        direction_mode=GridDirectionMode(args.direction_mode.upper()),
    )
    policies = generate_wind_down_maker_policies(
        reprice_intervals=[int(value) for value in _csv_values(args.reprice_intervals)],
        initial_offset_steps=_floats(args.initial_offset_steps),
        unwind_fractions=_floats(args.unwind_fractions),
    )
    report = RobustnessResearch(
        windows,
        [parameter],
        config,
        dataset_metadata=manifests,
    ).diagnose_wind_down_maker(parameter, policies)
    json_path, md_path = write_wind_down_maker_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(f"PASSED_CANDIDATES {report['passed_count']}/{report['candidate_count']}")


def _diagnose_seeds(args: argparse.Namespace) -> None:
    capital_by_symbol = _symbol_capitals(args.symbol_capitals)
    config = ResearchConfig(
        capital_per_symbol=(
            sum(capital_by_symbol.values()) / len(capital_by_symbol)
            if capital_by_symbol
            else args.capital
        ),
        capital_by_symbol=capital_by_symbol,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
        max_inventory_notional=args.max_inventory_notional,
        max_unpaired_lots_per_side=args.max_unpaired_lots_per_side,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        manifests.append(verify_frozen_dataset(manifest))
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    parameter = ParameterSet(
        range_multiplier=args.range_multiplier,
        min_step_pct=args.min_step,
        stop_buffer_pct=args.stop_buffer,
        direction_mode=GridDirectionMode(args.direction_mode.upper()),
    )
    policy = generate_wind_down_maker_policies(
        reprice_intervals=[args.reprice_interval],
        initial_offset_steps=[args.initial_offset_steps],
        unwind_fractions=[args.unwind_fraction],
    )[0]
    filter_values = (
        args.max_directional_efficiency,
        args.max_volatility_expansion,
        args.min_reversal_ratio,
    )
    if any(value is not None for value in filter_values) and not all(
        value is not None for value in filter_values
    ):
        raise ValueError("多 seed 入口过滤的三个阈值必须同时提供。")
    entry_filter = (
        EntryFilter(*[float(value) for value in filter_values])
        if all(value is not None for value in filter_values)
        else None
    )
    report = RobustnessResearch(
        windows,
        [parameter],
        config,
        dataset_metadata=manifests,
    ).diagnose_fill_seeds(
        parameter,
        policy,
        [int(value) for value in _csv_values(args.seed_salts)],
        entry_filter=entry_filter,
    )
    json_path, md_path = write_seed_sensitivity_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(
        "SEED_PASS_RATE "
        f"{report['summary']['all_cost_pass_rate']:.1%} "
        f"PASSED={report['summary']['passed']}"
    )


def _diagnose_joint_seeds(args: argparse.Namespace) -> None:
    capital_by_symbol = {
        "BTCUSDT": float(args.btc_capital),
        "ETHUSDT": float(args.eth_capital),
    }
    config = ResearchConfig(
        capital_per_symbol=sum(capital_by_symbol.values()) / 2,
        capital_by_symbol=capital_by_symbol,
        maker_fill_probability=args.fill_probability,
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=args.wind_down_bars,
    )
    windows = []
    manifests = []
    for manifest in args.manifests:
        metadata = verify_frozen_dataset(manifest)
        manifests.append(metadata)
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    if {str(item.get("symbol") or "").upper() for item in manifests} != {
        "BTCUSDT",
        "ETHUSDT",
    }:
        raise ValueError("联合多 seed 诊断必须且只能提供 BTCUSDT、ETHUSDT 数据集。")

    btc_parameter = ParameterSet(
        range_multiplier=args.btc_range_multiplier,
        min_step_pct=args.btc_min_step,
        stop_buffer_pct=args.btc_stop_buffer,
        direction_mode=GridDirectionMode.NEUTRAL,
    )
    eth_parameter = ParameterSet(
        range_multiplier=args.eth_range_multiplier,
        min_step_pct=args.eth_min_step,
        stop_buffer_pct=args.eth_stop_buffer,
        direction_mode=GridDirectionMode.NEUTRAL,
    )
    eth_filter = EntryFilter(
        args.eth_max_directional_efficiency,
        args.eth_max_volatility_expansion,
        args.eth_min_reversal_ratio,
    )
    maker_policy = generate_wind_down_maker_policies(
        reprice_intervals=[args.reprice_interval],
        initial_offset_steps=[args.initial_offset_steps],
        unwind_fractions=[args.unwind_fraction],
    )[0]
    report = RobustnessResearch(
        windows,
        [btc_parameter, eth_parameter],
        config,
        dataset_metadata=manifests,
    ).diagnose_joint_fill_seeds(
        {
            "BTCUSDT": SymbolResearchPolicy(
                parameter=btc_parameter,
                max_inventory_notional=args.btc_max_inventory_notional,
            ),
            "ETHUSDT": SymbolResearchPolicy(
                parameter=eth_parameter,
                max_inventory_notional=args.eth_max_inventory_notional,
                entry_filter=eth_filter,
            ),
        },
        maker_policy,
        [int(value) for value in _csv_values(args.seed_salts)],
    )
    json_path, md_path = write_joint_seed_diagnostic(
        report,
        args.report_dir,
        stem=args.report_name,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(
        "JOINT_SEED_PASS_RATE "
        f"{report['summary']['all_cost_pass_rate']:.1%} "
        f"PASSED={report['summary']['passed']}"
    )


def _finalize_joint_oos(args: argparse.Namespace) -> None:
    lock_path = Path(args.lock_report).resolve()
    lock_bytes = lock_path.read_bytes()
    lock_report = json.loads(lock_bytes.decode("utf-8"))
    if not bool(lock_report.get("summary", {}).get("passed")):
        raise ValueError("锁定报告尚未通过开发/验证稳健性门槛，拒绝执行最终 OOS。")
    if lock_report.get("split", {}).get("final_oos", {}).get("status") != "SEALED_NOT_EVALUATED":
        raise ValueError("锁定报告的最终 OOS 状态不是 SEALED_NOT_EVALUATED。")

    capital_by_symbol: dict[str, float] = {}
    parameters: list[ParameterSet] = []
    symbol_policies: dict[str, SymbolResearchPolicy] = {}
    for symbol, payload in lock_report["symbol_policies"].items():
        normalized_symbol = str(symbol).strip().upper()
        parameter_payload = payload["parameter"]
        parameter = ParameterSet(
            range_multiplier=float(parameter_payload["range_multiplier"]),
            min_step_pct=float(parameter_payload["min_step_pct"]),
            stop_buffer_pct=float(parameter_payload["stop_buffer_pct"]),
            direction_mode=GridDirectionMode(str(parameter_payload["direction_mode"]).upper()),
        )
        filter_payload = payload.get("entry_filter")
        entry_filter = (
            EntryFilter(
                max_directional_efficiency=float(filter_payload["max_directional_efficiency"]),
                max_volatility_expansion=float(filter_payload["max_volatility_expansion"]),
                min_reversal_ratio=float(filter_payload["min_reversal_ratio"]),
            )
            if filter_payload is not None
            else None
        )
        capital_by_symbol[normalized_symbol] = float(payload["capital"])
        parameters.append(parameter)
        symbol_policies[normalized_symbol] = SymbolResearchPolicy(
            parameter=parameter,
            max_inventory_notional=float(payload["max_inventory_notional"]),
            entry_filter=entry_filter,
        )

    if set(symbol_policies) != {"BTCUSDT", "ETHUSDT"}:
        raise ValueError("最终 OOS 锁定报告必须且只能包含 BTCUSDT、ETHUSDT。")
    backtest_policy = lock_report["backtest_policy"]
    config = ResearchConfig(
        capital_per_symbol=sum(capital_by_symbol.values()) / len(capital_by_symbol),
        capital_by_symbol=capital_by_symbol,
        maker_fill_probability=float(backtest_policy["maker_fill_probability"]),
        min_windows_per_split=args.min_windows_per_split,
        wind_down_bars=int(backtest_policy["wind_down_bars"]),
    )
    policy_payload = lock_report["policy"]
    maker_policy = generate_wind_down_maker_policies(
        reprice_intervals=[int(policy_payload["reprice_interval_bars"])],
        initial_offset_steps=[float(policy_payload["initial_offset_steps"])],
        unwind_fractions=[float(policy_payload["unwind_fraction"])],
    )[0]

    windows = []
    manifests = []
    for manifest in args.manifests:
        metadata = verify_frozen_dataset(manifest)
        manifests.append(metadata)
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
            )
        )
    if {str(item.get("symbol") or "").upper() for item in manifests} != set(symbol_policies):
        raise ValueError("最终 OOS 数据集与锁定报告标的不一致。")

    lock_sha256 = hashlib.sha256(lock_bytes).hexdigest()
    report = RobustnessResearch(
        windows,
        parameters,
        config,
        dataset_metadata=manifests,
    ).evaluate_locked_joint_oos(
        symbol_policies,
        maker_policy,
        [int(value) for value in lock_report["seed_salts"]],
        lock_report_sha256=lock_sha256,
        lock_report_generated_at=lock_report.get("generated_at"),
    )
    stem = args.report_name or f"quietgrid-joint-final-oos-{lock_sha256[:12]}"
    json_path, md_path = write_joint_oos_report(
        report,
        args.report_dir,
        stem=stem,
    )
    print(f"REPORT_JSON {json_path}")
    print(f"REPORT_MD {md_path}")
    print(
        "FINAL_OOS_PASS_RATE "
        f"{report['summary']['all_cost_pass_rate']:.1%} "
        f"PASSED={report['summary']['passed']}"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "冻结 Binance 官方 USD-M 归档，并以严格开发/验证/OOS 协议"
            "执行 QuietGrid 周末窗口稳健性回测。"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="下载并冻结官方归档")
    download.add_argument("--symbols", default="BTCUSDT,ETHUSDT")
    download.add_argument("--start", help="ISO-8601，默认结束时间前 730 天")
    download.add_argument("--end", help="ISO-8601，默认当前 UTC 时间")
    download.add_argument("--output-dir", default="data/backtests/robustness")
    download.add_argument("--max-missing-ratio", type=float, default=0.001)
    download.add_argument(
        "--proxy-url",
        help="可选 HTTP/SOCKS 代理，例如 http://127.0.0.1:7897",
    )

    verify = subparsers.add_parser("verify", help="校验冻结文件 SHA-256 和行数")
    verify.add_argument("manifests", nargs="+")

    backtest = subparsers.add_parser("backtest", help="执行严格稳健性研究")
    backtest.add_argument("manifests", nargs="+")
    backtest.add_argument("--capital", type=float, default=500.0)
    backtest.add_argument("--fill-probability", type=float, default=0.65)
    backtest.add_argument("--direction-modes", default="NEUTRAL")
    backtest.add_argument("--range-multipliers", default="0.75,1.0,1.25")
    backtest.add_argument("--min-steps", default="0.0012,0.0015,0.0018")
    backtest.add_argument("--stop-buffers", default="0.01,0.015,0.02")
    backtest.add_argument("--min-windows-per-split", type=int, default=8)
    backtest.add_argument("--walk-forward-train", type=int, default=26)
    backtest.add_argument("--walk-forward-test", type=int, default=8)
    backtest.add_argument("--walk-forward-step", type=int, default=8)
    backtest.add_argument("--wind-down-bars", type=int, default=0)
    backtest.add_argument("--report-dir", default="reports/robustness")
    backtest.add_argument("--report-name")

    diagnose = subparsers.add_parser(
        "diagnose-entry",
        help="只使用开发/验证集诊断横盘入口过滤，保持最终 OOS 封存",
    )
    diagnose.add_argument("manifests", nargs="+")
    diagnose.add_argument("--capital", type=float, default=500.0)
    diagnose.add_argument("--fill-probability", type=float, default=0.65)
    diagnose.add_argument("--direction-mode", default="NEUTRAL")
    diagnose.add_argument("--range-multiplier", type=float, default=1.0)
    diagnose.add_argument("--min-step", type=float, default=0.0012)
    diagnose.add_argument("--stop-buffer", type=float, default=0.02)
    diagnose.add_argument(
        "--max-directional-efficiencies",
        default="0.2,0.3,0.4,0.5",
    )
    diagnose.add_argument(
        "--max-volatility-expansions",
        default="0.8,1.0,1.2",
    )
    diagnose.add_argument("--min-reversal-ratios", default="0.2,0.3,0.4")
    diagnose.add_argument("--min-windows-per-split", type=int, default=8)
    diagnose.add_argument("--wind-down-bars", type=int, default=0)
    diagnose.add_argument(
        "--max-inventory-notional",
        type=float,
        default=0.0,
    )
    diagnose.add_argument("--reprice-interval", type=int, default=0)
    diagnose.add_argument(
        "--initial-offset-steps",
        type=float,
        default=0.0,
    )
    diagnose.add_argument("--unwind-fraction", type=float, default=1.0)
    diagnose.add_argument(
        "--max-unpaired-lots-per-side",
        type=int,
        default=0,
    )
    diagnose.add_argument("--maker-fee-rate", type=float, default=0.0002)
    diagnose.add_argument("--taker-fee-rate", type=float, default=0.0005)
    diagnose.add_argument("--stop-slippage-bps", type=float, default=10.0)
    diagnose.add_argument("--seed-salt", type=int)
    diagnose.add_argument("--report-dir", default="reports/robustness")
    diagnose.add_argument("--report-name")

    exit_diagnostic = subparsers.add_parser(
        "diagnose-exit",
        help="只使用开发/验证集比较终场前停止新增库存的时间",
    )
    exit_diagnostic.add_argument("manifests", nargs="+")
    exit_diagnostic.add_argument("--capital", type=float, default=500.0)
    exit_diagnostic.add_argument("--fill-probability", type=float, default=0.65)
    exit_diagnostic.add_argument("--direction-mode", default="NEUTRAL")
    exit_diagnostic.add_argument("--range-multiplier", type=float, default=1.0)
    exit_diagnostic.add_argument("--min-step", type=float, default=0.0012)
    exit_diagnostic.add_argument("--stop-buffer", type=float, default=0.02)
    exit_diagnostic.add_argument("--wind-down-values", default="0,60,120,240,480")
    exit_diagnostic.add_argument("--min-windows-per-split", type=int, default=8)
    exit_diagnostic.add_argument("--report-dir", default="reports/robustness")
    exit_diagnostic.add_argument("--report-name")

    parameter_diagnostic = subparsers.add_parser(
        "diagnose-parameters",
        help="固定离场政策，仅使用开发/验证集寻找稳健参数平台",
    )
    parameter_diagnostic.add_argument("manifests", nargs="+")
    parameter_diagnostic.add_argument("--capital", type=float, default=500.0)
    parameter_diagnostic.add_argument("--fill-probability", type=float, default=0.65)
    parameter_diagnostic.add_argument("--maker-fee-rate", type=float, default=0.0002)
    parameter_diagnostic.add_argument("--taker-fee-rate", type=float, default=0.0005)
    parameter_diagnostic.add_argument("--stop-slippage-bps", type=float, default=10.0)
    parameter_diagnostic.add_argument("--direction-modes", default="NEUTRAL")
    parameter_diagnostic.add_argument("--range-multipliers", default="0.75,1.0,1.25")
    parameter_diagnostic.add_argument("--min-steps", default="0.0012,0.0015,0.0018")
    parameter_diagnostic.add_argument("--stop-buffers", default="0.01,0.015,0.02")
    parameter_diagnostic.add_argument("--wind-down-bars", type=int, default=1440)
    parameter_diagnostic.add_argument(
        "--max-inventory-notional",
        type=float,
        default=0.0,
    )
    parameter_diagnostic.add_argument(
        "--wind-down-reprice-interval-bars",
        type=int,
        default=0,
    )
    parameter_diagnostic.add_argument(
        "--wind-down-initial-offset-steps",
        type=float,
        default=0.0,
    )
    parameter_diagnostic.add_argument(
        "--wind-down-unwind-fraction",
        type=float,
        default=1.0,
    )
    parameter_diagnostic.add_argument("--min-windows-per-split", type=int, default=8)
    parameter_diagnostic.add_argument("--report-dir", default="reports/robustness")
    parameter_diagnostic.add_argument("--report-name")

    window_diagnostic = subparsers.add_parser(
        "diagnose-windows",
        help="输出开发/验证逐窗口损失来源，保持最终 OOS 封存",
    )
    window_diagnostic.add_argument("manifests", nargs="+")
    window_diagnostic.add_argument("--capital", type=float, default=500.0)
    window_diagnostic.add_argument("--fill-probability", type=float, default=0.65)
    window_diagnostic.add_argument("--direction-mode", default="NEUTRAL")
    window_diagnostic.add_argument("--range-multiplier", type=float, default=1.0)
    window_diagnostic.add_argument("--min-step", type=float, default=0.0012)
    window_diagnostic.add_argument("--stop-buffer", type=float, default=0.02)
    window_diagnostic.add_argument("--wind-down-bars", type=int, default=1440)
    window_diagnostic.add_argument(
        "--max-inventory-notional",
        type=float,
        default=200.0,
    )
    window_diagnostic.add_argument("--reprice-interval", type=int, default=5)
    window_diagnostic.add_argument(
        "--initial-offset-steps",
        type=float,
        default=1.1,
    )
    window_diagnostic.add_argument("--unwind-fraction", type=float, default=1.0)
    window_diagnostic.add_argument(
        "--max-unpaired-lots-per-side",
        type=int,
        default=0,
    )
    window_diagnostic.add_argument("--maker-fee-rate", type=float, default=0.0002)
    window_diagnostic.add_argument("--taker-fee-rate", type=float, default=0.0005)
    window_diagnostic.add_argument("--stop-slippage-bps", type=float, default=10.0)
    window_diagnostic.add_argument("--seed-salt", type=int)
    window_diagnostic.add_argument("--min-windows-per-split", type=int, default=8)
    window_diagnostic.add_argument("--report-dir", default="reports/robustness")
    window_diagnostic.add_argument("--report-name")

    inventory_diagnostic = subparsers.add_parser(
        "diagnose-inventory",
        help="固定网格与终场政策，只用开发/验证比较库存名义上限",
    )
    inventory_diagnostic.add_argument("manifests", nargs="+")
    inventory_diagnostic.add_argument("--capital", type=float, default=500.0)
    inventory_diagnostic.add_argument("--fill-probability", type=float, default=0.65)
    inventory_diagnostic.add_argument("--direction-mode", default="NEUTRAL")
    inventory_diagnostic.add_argument("--range-multiplier", type=float, default=1.0)
    inventory_diagnostic.add_argument("--min-step", type=float, default=0.0012)
    inventory_diagnostic.add_argument("--stop-buffer", type=float, default=0.02)
    inventory_diagnostic.add_argument("--wind-down-bars", type=int, default=1440)
    inventory_diagnostic.add_argument(
        "--max-inventory-values",
        default="100,150,200,250,300",
    )
    inventory_diagnostic.add_argument("--min-windows-per-split", type=int, default=8)
    inventory_diagnostic.add_argument("--report-dir", default="reports/robustness")
    inventory_diagnostic.add_argument("--report-name")

    dynamic_diagnostic = subparsers.add_parser(
        "diagnose-dynamic-mode",
        help="只用决策时已闭合的长周期历史选择多空中性模式",
    )
    dynamic_diagnostic.add_argument("manifests", nargs="+")
    dynamic_diagnostic.add_argument("--capital", type=float, default=500.0)
    dynamic_diagnostic.add_argument("--fill-probability", type=float, default=0.65)
    dynamic_diagnostic.add_argument("--range-multiplier", type=float, default=1.0)
    dynamic_diagnostic.add_argument("--min-step", type=float, default=0.0012)
    dynamic_diagnostic.add_argument("--stop-buffer", type=float, default=0.02)
    dynamic_diagnostic.add_argument("--lookbacks", default="1440,4320")
    dynamic_diagnostic.add_argument(
        "--directional-thresholds",
        default="0.8,1.2,1.6",
    )
    dynamic_diagnostic.add_argument("--neutral-thresholds", default="0.35,0.5")
    dynamic_diagnostic.add_argument("--min-persistences", default="0.5,0.67")
    dynamic_diagnostic.add_argument(
        "--trend-alignments",
        default="MOMENTUM,CONTRARIAN",
    )
    dynamic_diagnostic.add_argument("--segment-rows", type=int, default=360)
    dynamic_diagnostic.add_argument("--wind-down-bars", type=int, default=1440)
    dynamic_diagnostic.add_argument(
        "--max-inventory-notional",
        type=float,
        default=200.0,
    )
    dynamic_diagnostic.add_argument("--min-windows-per-split", type=int, default=8)
    dynamic_diagnostic.add_argument("--report-dir", default="reports/robustness")
    dynamic_diagnostic.add_argument("--report-name")

    unwind_diagnostic = subparsers.add_parser(
        "diagnose-unwind",
        help="只用开发/验证诊断终场渐进 Maker 去库存",
    )
    unwind_diagnostic.add_argument("manifests", nargs="+")
    unwind_diagnostic.add_argument("--capital", type=float, default=500.0)
    unwind_diagnostic.add_argument("--fill-probability", type=float, default=0.65)
    unwind_diagnostic.add_argument("--direction-mode", default="NEUTRAL")
    unwind_diagnostic.add_argument("--range-multiplier", type=float, default=1.0)
    unwind_diagnostic.add_argument("--min-step", type=float, default=0.0012)
    unwind_diagnostic.add_argument("--stop-buffer", type=float, default=0.02)
    unwind_diagnostic.add_argument("--wind-down-bars", type=int, default=1440)
    unwind_diagnostic.add_argument(
        "--max-inventory-notional",
        type=float,
        default=200.0,
    )
    unwind_diagnostic.add_argument("--reprice-intervals", default="15,60,240")
    unwind_diagnostic.add_argument("--initial-offset-steps", default="0.25,0.5,1.0")
    unwind_diagnostic.add_argument("--unwind-fractions", default="1.0")
    unwind_diagnostic.add_argument("--min-windows-per-split", type=int, default=8)
    unwind_diagnostic.add_argument("--report-dir", default="reports/robustness")
    unwind_diagnostic.add_argument("--report-name")
    seed_diagnostic = subparsers.add_parser(
        "diagnose-seeds",
        help="固定参数与去库存策略，验证多确定性撮合 seed 敏感性",
    )
    seed_diagnostic.add_argument("manifests", nargs="+")
    seed_diagnostic.add_argument("--capital", type=float, default=500.0)
    seed_diagnostic.add_argument(
        "--symbol-capitals",
        default="",
        help="可选按标的本金，例如 BTCUSDT=800,ETHUSDT=200",
    )
    seed_diagnostic.add_argument("--fill-probability", type=float, default=0.65)
    seed_diagnostic.add_argument("--direction-mode", default="NEUTRAL")
    seed_diagnostic.add_argument("--range-multiplier", type=float, default=0.9)
    seed_diagnostic.add_argument("--min-step", type=float, default=0.00255)
    seed_diagnostic.add_argument("--stop-buffer", type=float, default=0.02)
    seed_diagnostic.add_argument("--wind-down-bars", type=int, default=1440)
    seed_diagnostic.add_argument(
        "--max-inventory-notional",
        type=float,
        default=200.0,
    )
    seed_diagnostic.add_argument("--reprice-interval", type=int, default=5)
    seed_diagnostic.add_argument(
        "--initial-offset-steps",
        type=float,
        default=1.1,
    )
    seed_diagnostic.add_argument("--unwind-fraction", type=float, default=1.0)
    seed_diagnostic.add_argument(
        "--max-unpaired-lots-per-side",
        type=int,
        default=0,
        help="每个方向最多保留的未配对库存层数；0 表示不限制。",
    )
    seed_diagnostic.add_argument("--max-directional-efficiency", type=float)
    seed_diagnostic.add_argument("--max-volatility-expansion", type=float)
    seed_diagnostic.add_argument("--min-reversal-ratio", type=float)
    seed_diagnostic.add_argument(
        "--seed-salts",
        default="17,29,43,59,71,89,101,127,149,173",
    )
    seed_diagnostic.add_argument("--min-windows-per-split", type=int, default=8)
    seed_diagnostic.add_argument("--report-dir", default="reports/robustness")
    seed_diagnostic.add_argument("--report-name")
    joint_seed = subparsers.add_parser(
        "diagnose-joint-seeds",
        help="按标的锁定参数，联合验证 BTC/ETH 多 seed 与费用敏感性",
    )
    joint_seed.add_argument("manifests", nargs="+")
    joint_seed.add_argument("--btc-capital", type=float, default=500.0)
    joint_seed.add_argument("--eth-capital", type=float, default=300.0)
    joint_seed.add_argument("--fill-probability", type=float, default=0.65)
    joint_seed.add_argument("--wind-down-bars", type=int, default=1440)
    joint_seed.add_argument("--reprice-interval", type=int, default=5)
    joint_seed.add_argument("--initial-offset-steps", type=float, default=1.1)
    joint_seed.add_argument("--unwind-fraction", type=float, default=1.0)
    joint_seed.add_argument("--btc-range-multiplier", type=float, default=1.25)
    joint_seed.add_argument("--btc-min-step", type=float, default=0.0015)
    joint_seed.add_argument("--btc-stop-buffer", type=float, default=0.02)
    joint_seed.add_argument(
        "--btc-max-inventory-notional",
        type=float,
        default=200.0,
    )
    joint_seed.add_argument("--eth-range-multiplier", type=float, default=1.0)
    joint_seed.add_argument("--eth-min-step", type=float, default=0.0018)
    joint_seed.add_argument("--eth-stop-buffer", type=float, default=0.02)
    joint_seed.add_argument(
        "--eth-max-inventory-notional",
        type=float,
        default=120.0,
    )
    joint_seed.add_argument(
        "--eth-max-directional-efficiency",
        type=float,
        default=0.50,
    )
    joint_seed.add_argument(
        "--eth-max-volatility-expansion",
        type=float,
        default=1.05,
    )
    joint_seed.add_argument(
        "--eth-min-reversal-ratio",
        type=float,
        default=0.25,
    )
    joint_seed.add_argument(
        "--seed-salts",
        default="17,29,43,59,71,89,101,127,149,173",
    )
    joint_seed.add_argument("--min-windows-per-split", type=int, default=8)
    joint_seed.add_argument("--report-dir", default="reports/robustness")
    joint_seed.add_argument("--report-name")
    final_oos = subparsers.add_parser(
        "finalize-joint-oos",
        help="从已通过且仍封存的联合锁定报告执行一次最终 OOS",
    )
    final_oos.add_argument("manifests", nargs="+")
    final_oos.add_argument("--lock-report", required=True)
    final_oos.add_argument("--min-windows-per-split", type=int, default=8)
    final_oos.add_argument("--report-dir", default="reports/robustness")
    final_oos.add_argument("--report-name")
    return parser


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("时间必须包含时区。")
    return parsed.astimezone(UTC)


def _csv_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _floats(value: str) -> list[float]:
    return [float(item) for item in _csv_values(value)]


def _symbol_capitals(value: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in _csv_values(value):
        if "=" not in item:
            raise argparse.ArgumentTypeError(
                "--symbol-capitals 必须使用 SYMBOL=CAPITAL 格式。"
            )
        symbol, raw_capital = item.split("=", 1)
        normalized = symbol.strip().upper()
        capital = float(raw_capital)
        if not normalized or capital <= 0:
            raise argparse.ArgumentTypeError("按标的本金必须为正。")
        result[normalized] = capital
    return result


if __name__ == "__main__":
    raise SystemExit(main())
