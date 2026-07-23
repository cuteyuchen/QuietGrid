from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gc
import json
import os
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, timezone
from math import log, log1p
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_long_horizon_regime as round15
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.cross_era_spot_feasibility as spot_round
import scripts.profit_protection_optimize as profit_opt
from core.models import GridDirectionMode
from scripts.cross_era_oos import _dataset_brief, _write_json
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256
from scripts.robustness import (
    RobustnessResearch,
    SymbolResearchPolicy,
    WindowResult,
    aggregate_results,
    verify_frozen_dataset,
)


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round18-cycle-capacity-upper-bound-protocol.md"
)
PROTOCOL_SHA256 = "8c74ca7ff2ee05d266886d4055ed8f25d961af40afa4d52ee3ee96a0a0e5ff89"
ASSET_AUDIT_SHA256 = "3d4c1df25da45f37e9661ae0797baecf4a9e799b42e397687d6eeeb62ac6ab27"
ROUND14_RESULT_SHA256 = "c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f"
ROUND15_RESULT_SHA256 = "131dc847d60012a1dcdf5fc601d5e9a4918ca18e3ff1000fb5f75776f5443fc2"
ROUND16_RESULT_SHA256 = "990ee916758de7f89cf6b7d6801d3887dbcce43c35764c48dde07994f9714f9d"
ROUND17_RESULT_SHA256 = "9dfff4caaa30cc78e47c350c2f45d12183f5bb223353c86193bc56f5c4faa969"
LOOKBACKS = (180, 720, 1440)
EXPECTED_INVENTORY_CAPS = {"BTCUSDT": 200.0, "ETHUSDT": 120.0}
EXPECTED_UNPAIRED_LOTS = {"BTCUSDT": 1, "ETHUSDT": 0}
EXPECTED_REDUCE_TARGETS = {"BTCUSDT": 0.50, "ETHUSDT": 1.00}


def _base_components(base_config: Any) -> tuple[
    list[Any],
    dict[str, SymbolResearchPolicy],
    Any,
    Any,
]:
    parameters, locked_policies, maker_policy = profit_opt._locked_policy()
    policies = {
        symbol: replace(policy, entry_filter=None)
        for symbol, policy in locked_policies.items()
    }
    config, candidate_maker_policy = round13._variant_config_and_policy(
        base_config,
        maker_policy,
        round13.CANDIDATE_ID,
    )
    _validate_base_definition(config, policies, candidate_maker_policy)
    return parameters, policies, config, candidate_maker_policy


def _validate_base_definition(
    config: Any,
    policies: Mapping[str, SymbolResearchPolicy],
    maker_policy: Any,
) -> None:
    if set(policies) != set(asset_audit.SYMBOLS):
        raise ValueError("振荡容量评估必须且只能定义 BTCUSDT、ETHUSDT 策略。")
    for symbol, policy in policies.items():
        if policy.parameter.direction_mode != GridDirectionMode.NEUTRAL:
            raise ValueError(f"{symbol} direction_mode 必须保持 NEUTRAL。")
        if policy.entry_filter is not None:
            raise ValueError(f"{symbol} 基础策略不允许入口过滤。")
        if policy.max_unpaired_lots_per_side != EXPECTED_UNPAIRED_LOTS[symbol]:
            raise ValueError(f"{symbol} 未配对 lot 上限不一致。")
        if abs(
            float(policy.reduce_target_step_fraction or 0.0)
            - EXPECTED_REDUCE_TARGETS[symbol]
        ) > 1e-12:
            raise ValueError(f"{symbol} 减仓目标比例不一致。")
        if abs(
            float(policy.max_inventory_notional)
            - EXPECTED_INVENTORY_CAPS[symbol]
        ) > 1e-12:
            raise ValueError(f"{symbol} 库存名义上限不一致。")
    if int(config.wind_down_bars) != round13.CANDIDATE_WIND_DOWN_BARS:
        raise ValueError("基础策略 wind-down 必须保持 2160 bars。")
    if str(config.unpaired_lot_cap_enforcement).upper() != "BAR_BOUNDARY":
        raise ValueError("lot 上限必须在 BAR_BOUNDARY 执行。")
    if bool(config.profit_protection_enabled):
        raise ValueError("基础策略不允许启用利润保护。")
    if float(config.volatility_reduce_expansion_ratio) != 0.0:
        raise ValueError("基础策略不允许启用波动减仓。")
    if int(config.volatility_reduce_after_breaches) != 0:
        raise ValueError("基础策略不允许启用波动减仓。")
    if int(maker_policy.reprice_interval_bars) != 5:
        raise ValueError("Maker 重挂间隔必须保持 5 bars。")
    if abs(float(maker_policy.initial_offset_steps) - 1.10) > 1e-12:
        raise ValueError("Maker 初始偏移必须保持 1.10。")
    if abs(float(maker_policy.unwind_fraction) - 1.0) > 1e-12:
        raise ValueError("Maker unwind fraction 必须保持 1.0。")
    if abs(float(maker_policy.urgency_exponent) - 2.0) > 1e-12:
        raise ValueError("Maker 紧迫度指数必须保持 2.0。")


def _initialize_worker(
    manifests: Sequence[str],
    base_config: Any,
    end_time_iso: str | None,
) -> None:
    end_time = datetime.fromisoformat(end_time_iso) if end_time_iso else None
    metadata, windows = round15._load_dataset(
        manifests,
        base_config,
        end_time=end_time,
    )
    parameters, policies, config, maker_policy = _base_components(base_config)
    profit_opt._WORKER_STATE.clear()
    profit_opt._WORKER_STATE.update(
        {
            "metadata": metadata,
            "windows": windows,
            "parameters": parameters,
            "symbol_policies": policies,
            "maker_policy": maker_policy,
            "config": config,
        }
    )


def _verify_worker_cache(
    research: RobustnessResearch,
    policies: Mapping[str, SymbolResearchPolicy],
    maker_policy: Any,
    *,
    allowed_window_ids: set[str],
    cost: tuple[float, float, float],
    seed: int,
) -> dict[str, Any]:
    symbols_by_window: dict[str, set[str]] = {}
    maker_fee, taker_fee, slippage_bps = cost
    expected_entries = len(allowed_window_ids) * len(asset_audit.SYMBOLS)
    for cache_key in research._cache:
        if len(cache_key) != 16:
            raise RuntimeError("振荡容量 worker cache 键长度不一致。")
        parameter_id = str(cache_key[0])
        symbol = str(cache_key[1]).strip().upper()
        window_id = str(cache_key[2])
        if symbol not in policies:
            raise RuntimeError(f"worker 出现未授权标的: {symbol}")
        if window_id not in allowed_window_ids:
            raise RuntimeError("worker 访问了授权窗口之外的数据。")
        policy = policies[symbol]
        if parameter_id != policy.parameter.parameter_id:
            raise RuntimeError(f"{symbol} 参数集不一致。")
        expected = {
            3: float(research.config.maker_fill_probability),
            4: float(round13.CANDIDATE_WIND_DOWN_BARS),
            5: float(EXPECTED_INVENTORY_CAPS[symbol]),
            6: 5.0,
            7: 1.10,
            8: 1.0,
            9: float(EXPECTED_UNPAIRED_LOTS[symbol]),
            10: float(EXPECTED_REDUCE_TARGETS[symbol]),
            11: float(maker_fee),
            12: float(taker_fee),
            13: float(slippage_bps),
            14: 2.0,
            15: float(seed),
        }
        for index, expected_value in expected.items():
            if abs(float(cache_key[index]) - expected_value) > 1e-12:
                raise RuntimeError(
                    f"{symbol} worker cache 参数 {index} 不一致: "
                    f"{cache_key[index]} != {expected_value}"
                )
        symbols_by_window.setdefault(window_id, set()).add(symbol)
    if len(research._cache) != expected_entries:
        raise RuntimeError(
            f"worker cache 数量不一致: {len(research._cache)} != {expected_entries}"
        )
    if set(symbols_by_window) != allowed_window_ids:
        raise RuntimeError("worker 未覆盖全部授权窗口。")
    if any(set(symbols) != set(asset_audit.SYMBOLS) for symbols in symbols_by_window.values()):
        raise RuntimeError("worker 未对每个窗口同时覆盖 BTC/ETH。")
    _validate_base_definition(research.config, policies, maker_policy)
    return {
        "window_count": len(symbols_by_window),
        "symbol_window_count": expected_entries,
        "cache_entry_count": len(research._cache),
        "wind_down_bars": round13.CANDIDATE_WIND_DOWN_BARS,
        "urgency_exponent": 2.0,
        "maker_fee_rate": maker_fee,
        "taker_fee_rate": taker_fee,
        "stop_slippage_bps": slippage_bps,
        "seed": seed,
        "entry_filters_enabled": False,
        "passed": True,
    }


def _seed_worker(
    seed: int,
    split_ids: Mapping[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[int, dict[str, tuple[Any, list[WindowResult]]], dict[str, Any]]:
    state = profit_opt._WORKER_STATE
    research = RobustnessResearch(
        state["windows"],
        state["parameters"],
        state["config"],
        dataset_metadata=state["metadata"],
    )
    maker_fee, taker_fee, slippage_bps = cost
    runs = {
        split_name: research.evaluate_joint_policy_windows(
            state["symbol_policies"],
            state["maker_policy"],
            window_ids,
            maker_fee_rate=maker_fee,
            taker_fee_rate=taker_fee,
            stop_slippage_bps=slippage_bps,
            fill_seed_salt=seed,
        )
        for split_name, window_ids in split_ids.items()
    }
    allowed = {
        window_id for window_ids in split_ids.values() for window_id in window_ids
    }
    integrity = _verify_worker_cache(
        research,
        state["symbol_policies"],
        state["maker_policy"],
        allowed_window_ids=allowed,
        cost=cost,
        seed=seed,
    )
    return seed, runs, integrity


def _run_dataset(
    manifests: Sequence[str],
    base_config: Any,
    split_ids: Mapping[str, Sequence[str]],
    workers: int,
    *,
    end_time: datetime | None = None,
) -> tuple[
    dict[str, dict[int, dict[str, tuple[Any, list[WindowResult]]]]],
    dict[str, Any],
]:
    raw_runs = {scenario: {} for scenario in asset_audit.SCENARIOS}
    futures: dict[Any, tuple[str, int]] = {}
    task_integrity: list[dict[str, Any]] = []
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(workers, len(asset_audit.DEFAULT_SEEDS)),
        initializer=_initialize_worker,
        initargs=(
            tuple(manifests),
            base_config,
            end_time.isoformat() if end_time is not None else None,
        ),
    ) as executor:
        for scenario, cost in asset_audit.SCENARIOS.items():
            print(f"EVALUATING {scenario}", flush=True)
            for seed in asset_audit.DEFAULT_SEEDS:
                future = executor.submit(_seed_worker, seed, split_ids, cost)
                futures[future] = (scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            scenario, expected_seed = futures[future]
            seed, runs, integrity = future.result()
            if seed != expected_seed:
                raise RuntimeError("振荡容量 worker 返回了错误种子。")
            raw_runs[scenario][seed] = runs
            task_integrity.append({"scenario": scenario, **integrity})
    for scenario in asset_audit.SCENARIOS:
        if tuple(sorted(raw_runs[scenario])) != asset_audit.DEFAULT_SEEDS:
            raise RuntimeError(f"{scenario} 种子覆盖不完整。")
    expected_task_count = len(asset_audit.SCENARIOS) * len(asset_audit.DEFAULT_SEEDS)
    expected_window_count = len(
        {window_id for ids in split_ids.values() for window_id in ids}
    )
    if len(task_integrity) != expected_task_count:
        raise RuntimeError("worker 完整性任务数量不一致。")
    if any(
        not item["passed"] or int(item["window_count"]) != expected_window_count
        for item in task_integrity
    ):
        raise RuntimeError("worker 完整性审计未全部通过。")
    return raw_runs, {
        "task_count": expected_task_count,
        "window_count": expected_window_count,
        "all_tasks_passed": True,
        "tasks": sorted(task_integrity, key=lambda item: (item["scenario"], item["seed"])),
    }


def _extract_target_histories(
    rows: Iterable[tuple[int, float]],
    targets: Mapping[int, str],
    lookbacks: Sequence[int] = LOOKBACKS,
) -> tuple[dict[str, dict[int, tuple[float, ...] | None]], dict[str, Any]]:
    normalized_lookbacks = tuple(sorted({int(value) for value in lookbacks}))
    if not normalized_lookbacks or normalized_lookbacks[0] <= 0:
        raise ValueError("lookback 必须为正整数。")
    histories = {
        window_id: {lookback: None for lookback in normalized_lookbacks}
        for window_id in targets.values()
    }
    target_audit = {
        window_id: {
            "entry_open_time": int(open_time),
            "max_used_open_time": None,
            "available_lookbacks": [],
        }
        for open_time, window_id in targets.items()
    }
    maximum = max(normalized_lookbacks)
    buffer: deque[tuple[int, float]] = deque(maxlen=maximum + 1)
    previous_time: int | None = None
    last_read_time: int | None = None
    maximum_target = max(targets) if targets else None
    gap_reset_count = 0
    found_targets: set[str] = set()
    self_reference_count = 0
    for open_time, close in rows:
        open_time = int(open_time)
        close = float(close)
        if maximum_target is not None and open_time > maximum_target:
            break
        if close <= 0:
            raise ValueError("振荡容量遇到非正收盘价。")
        if previous_time is None or open_time - previous_time != 60_000:
            if previous_time is not None:
                gap_reset_count += 1
            buffer.clear()
        buffer.append((open_time, close))
        window_id = targets.get(open_time)
        if window_id is not None:
            found_targets.add(window_id)
            values = list(buffer)
            target_audit[window_id]["max_used_open_time"] = open_time
            if values and values[-1][0] > open_time:
                self_reference_count += 1
            for lookback in normalized_lookbacks:
                if len(values) < lookback + 1:
                    continue
                selected = values[-(lookback + 1) :]
                if selected[0][0] != open_time - lookback * 60_000:
                    continue
                histories[window_id][lookback] = tuple(item[1] for item in selected)
                target_audit[window_id]["available_lookbacks"].append(lookback)
        previous_time = open_time
        last_read_time = open_time
    available_counts = {
        str(lookback): sum(
            values[lookback] is not None for values in histories.values()
        )
        for lookback in normalized_lookbacks
    }
    return histories, {
        "target_count": len(histories),
        "found_target_count": len(found_targets),
        "available_counts": available_counts,
        "gap_reset_count": gap_reset_count,
        "self_reference_count": self_reference_count,
        "maximum_target_open_time": maximum_target,
        "last_read_open_time": last_read_time,
        "targets": target_audit,
    }


def _extract_manifest_histories(
    manifest_path: str,
    targets: Mapping[int, str],
) -> tuple[dict[str, dict[int, tuple[float, ...] | None]], dict[str, Any]]:
    manifest = verify_frozen_dataset(manifest_path)
    data_path = Path(manifest_path).resolve().parent / str(manifest["file_name"])

    def rows() -> Iterable[tuple[int, float]]:
        with data_path.open("r", newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            open_time_index = header.index("open_time")
            close_index = header.index("close")
            for raw in reader:
                yield int(raw[open_time_index]), float(raw[close_index])

    histories, audit = _extract_target_histories(rows(), targets)
    audit["symbol"] = str(manifest["symbol"])
    audit["manifest"] = str(Path(manifest_path).resolve())
    return histories, audit


def _extract_histories(
    manifests: Sequence[str],
    windows: Sequence[Any],
    allowed_ids: Sequence[str],
) -> tuple[
    dict[str, dict[str, dict[int, tuple[float, ...] | None]]],
    dict[str, Any],
]:
    targets = round15._target_entry_times(windows, allowed_ids)
    histories: dict[str, dict[str, dict[int, tuple[float, ...] | None]]] = {}
    audit: dict[str, Any] = {}
    for manifest in manifests:
        item = verify_frozen_dataset(manifest)
        symbol = str(item["symbol"]).strip().upper()
        histories[symbol], audit[symbol] = _extract_manifest_histories(
            manifest,
            targets[symbol],
        )
        if audit[symbol]["self_reference_count"] != 0:
            raise RuntimeError(f"{symbol} 振荡容量特征发生未来自引用。")
    if set(histories) != set(asset_audit.SYMBOLS):
        raise RuntimeError("振荡容量历史未同时覆盖 BTC/ETH。")
    return histories, audit


def _completed_step_cycles(closes: Sequence[float], step_pct: float) -> int:
    if step_pct <= 0:
        raise ValueError("step_pct 必须为正。")
    if len(closes) < 2:
        return 0
    values = [log(float(value)) for value in closes]
    threshold = log1p(float(step_pct))
    high = values[0]
    low = values[0]
    direction = 0
    reversal_legs = 0
    for value in values[1:]:
        if direction == 0:
            high = max(high, value)
            low = min(low, value)
            if high - value >= threshold:
                reversal_legs += 1
                direction = -1
                low = value
            elif value - low >= threshold:
                reversal_legs += 1
                direction = 1
                high = value
        elif direction > 0:
            if value > high:
                high = value
            elif high - value >= threshold:
                reversal_legs += 1
                direction = -1
                low = value
        else:
            if value < low:
                low = value
            elif value - low >= threshold:
                reversal_legs += 1
                direction = 1
                high = value
    return reversal_legs // 2


def _step_maps(
    raw_runs: Mapping[str, Mapping[int, Mapping[str, tuple[Any, list[WindowResult]]]]],
) -> tuple[dict[str, dict[str, dict[str, float | None]]], dict[str, Any]]:
    collected: dict[str, dict[str, dict[str, set[float]]]] = {
        scenario: {symbol: {} for symbol in asset_audit.SYMBOLS}
        for scenario in raw_runs
    }
    for scenario, scenario_runs in raw_runs.items():
        for seed in asset_audit.DEFAULT_SEEDS:
            for _split_name, (_metrics, results) in scenario_runs[seed].items():
                for result in results:
                    values = collected[scenario][result.symbol].setdefault(
                        result.window_id,
                        set(),
                    )
                    if result.step_pct is not None:
                        values.add(round(float(result.step_pct), 14))
    step_maps: dict[str, dict[str, dict[str, float | None]]] = {}
    unavailable_count = 0
    for scenario, symbols in collected.items():
        step_maps[scenario] = {}
        for symbol, windows in symbols.items():
            step_maps[scenario][symbol] = {}
            for window_id, values in windows.items():
                if len(values) > 1:
                    raise RuntimeError(
                        f"{scenario} {symbol} {window_id} 六种子 step_pct 不一致: {values}"
                    )
                step = next(iter(values)) if values else None
                if step is None:
                    unavailable_count += 1
                step_maps[scenario][symbol][window_id] = step
    return step_maps, {
        "step_consistent_across_seeds": True,
        "unavailable_symbol_window_count": unavailable_count,
    }


def _build_capacities(
    raw_runs: Mapping[str, Mapping[int, Mapping[str, tuple[Any, list[WindowResult]]]]],
    histories: Mapping[str, Mapping[str, Mapping[int, tuple[float, ...] | None]]],
) -> tuple[dict[str, dict[str, dict[str, dict[int, float | None]]]], dict[str, Any]]:
    step_maps, step_audit = _step_maps(raw_runs)
    capacities: dict[str, dict[str, dict[str, dict[int, float | None]]]] = {}
    available_counts: dict[str, dict[str, dict[str, int]]] = {}
    for scenario, cost in asset_audit.SCENARIOS.items():
        maker_fee = float(cost[0])
        capacities[scenario] = {}
        available_counts[scenario] = {}
        for symbol in asset_audit.SYMBOLS:
            capacities[scenario][symbol] = {}
            available_counts[scenario][symbol] = {str(value): 0 for value in LOOKBACKS}
            window_ids = set(histories[symbol]) | set(step_maps[scenario][symbol])
            for window_id in window_ids:
                step = step_maps[scenario][symbol].get(window_id)
                capacities[scenario][symbol][window_id] = {}
                for lookback in LOOKBACKS:
                    history = histories[symbol].get(window_id, {}).get(lookback)
                    capacity: float | None = None
                    if step is not None and history is not None and step > 0:
                        cycles = _completed_step_cycles(history, step)
                        capacity = cycles * max(step - 2.0 * maker_fee, 0.0)
                        available_counts[scenario][symbol][str(lookback)] += 1
                    capacities[scenario][symbol][window_id][lookback] = capacity
    return capacities, {
        **step_audit,
        "available_counts": available_counts,
    }


def _apply_capacity_filter(
    result: WindowResult,
    *,
    capacity: float | None,
    threshold: float,
    lookback: int,
) -> WindowResult:
    if result.status != "TRADED":
        return result
    if capacity is not None and float(capacity) >= float(threshold):
        return result
    reason = (
        f"CYCLE_CAPACITY_L{lookback}: HISTORY_OR_STEP_UNAVAILABLE"
        if capacity is None
        else f"CYCLE_CAPACITY_L{lookback}: {float(capacity):.8f} < {float(threshold):.8f}"
    )
    return RobustnessResearch._blocked_entry_result(result, reason)


def _trial_key(trial: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        bool(trial["passed"]),
        float(trial["summary"]["worst_seed_total_pnl"]),
        float(trial["summary"]["minimum_trade_coverage"]),
        int(trial["lookback"]),
        float(trial["threshold"]),
    )


def _evaluate_oracle_symbol_cell(
    *,
    results_by_seed: Mapping[int, Sequence[WindowResult]],
    capacities_by_window: Mapping[str, Mapping[int, float | None]],
    capital: float,
) -> dict[str, Any]:
    lookback_results = []
    for lookback in LOOKBACKS:
        available = [
            float(values[lookback])
            for values in capacities_by_window.values()
            if values.get(lookback) is not None
        ]
        thresholds = sorted({0.0, *available})
        trials = []
        for threshold in thresholds:
            metrics_by_seed = {}
            for seed in asset_audit.DEFAULT_SEEDS:
                transformed = [
                    _apply_capacity_filter(
                        result,
                        capacity=capacities_by_window.get(result.window_id, {}).get(
                            lookback
                        ),
                        threshold=threshold,
                        lookback=lookback,
                    )
                    for result in results_by_seed[seed]
                ]
                metrics_by_seed[seed] = aggregate_results(
                    transformed,
                    capital_per_symbol=capital,
                    symbol_count=1,
                )
            summary = asset_audit._summarize_symbol(metrics_by_seed)
            checks = asset_audit._scope_checks(summary)
            trials.append(
                {
                    "lookback": lookback,
                    "threshold": threshold,
                    "summary": summary,
                    "checks": checks,
                    "passed": all(checks.values()),
                }
            )
        best = max(trials, key=_trial_key)
        lookback_results.append(
            {
                "lookback": lookback,
                "threshold_count": len(trials),
                "passing_threshold_count": sum(item["passed"] for item in trials),
                "best_trial": best,
            }
        )
    selected = max(
        (item["best_trial"] for item in lookback_results),
        key=_trial_key,
    )
    return {
        "lookbacks": lookback_results,
        "oracle_selected": selected,
        "oracle_passed": bool(selected["passed"]),
    }


def _oracle_cells(datasets: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    for role, dataset in datasets.items():
        for scenario, scenario_runs in dataset["raw_runs"].items():
            for split_name in dataset["split_ids"]:
                cell_name = f"{role}_{split_name.upper()}_{scenario}"
                symbols: dict[str, Any] = {}
                for symbol in asset_audit.SYMBOLS:
                    results_by_seed = {
                        seed: [
                            result
                            for result in scenario_runs[seed][split_name][1]
                            if result.symbol == symbol
                        ]
                        for seed in asset_audit.DEFAULT_SEEDS
                    }
                    symbols[symbol] = _evaluate_oracle_symbol_cell(
                        results_by_seed=results_by_seed,
                        capacities_by_window=dataset["capacities"][scenario][symbol],
                        capital=float(dataset["base_config"].capital_by_symbol[symbol]),
                    )
                cells[cell_name] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "window_count": len(dataset["split_ids"][split_name]),
                    "symbols": symbols,
                }
    return cells


def _upper_bound_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [
        cell["symbols"][symbol]["oracle_selected"]
        for cell in cells.values()
        for symbol in asset_audit.SYMBOLS
    ]
    return {
        "cell_symbol_count": len(selected),
        "passed_cell_symbol_count": sum(item["passed"] for item in selected),
        "all_cells_have_oracle_solution": all(item["passed"] for item in selected),
        "minimum_oracle_worst_seed_total_pnl": min(
            float(item["summary"]["worst_seed_total_pnl"]) for item in selected
        ),
        "minimum_oracle_trade_coverage": min(
            float(item["summary"]["minimum_trade_coverage"]) for item in selected
        ),
    }


def _load_evidence(
    role: str,
    manifests: Sequence[str],
    base_config: Any,
    windows: Sequence[Any],
    split_ids: Mapping[str, Sequence[str]],
    workers: int,
    *,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    allowed_ids = tuple(window_id for ids in split_ids.values() for window_id in ids)
    histories, feature_audit = _extract_histories(manifests, windows, allowed_ids)
    raw_runs, execution_integrity = _run_dataset(
        manifests,
        base_config,
        split_ids,
        workers,
        end_time=end_time,
    )
    capacities, capacity_audit = _build_capacities(raw_runs, histories)
    expected_count = len(set(allowed_ids))
    if int(execution_integrity["window_count"]) != expected_count:
        raise RuntimeError(f"{role} worker 窗口覆盖数量不一致。")
    return {
        "base_config": base_config,
        "split_ids": {name: tuple(ids) for name, ids in split_ids.items()},
        "raw_runs": raw_runs,
        "capacities": capacities,
        "feature_audit": feature_audit,
        "capacity_audit": capacity_audit,
        "execution_integrity": execution_integrity,
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    summary = payload["upper_bound_summary"]
    lines = [
        "# 网格步长振荡容量乐观上界评估",
        "",
        "每个单元允许使用事后最有利 lookback 和阈值；该结果不可部署，只用于排除特征家族。",
        "",
        "| 单元 | 标的 | Lookback | 阈值 | 最差种子 PnL | 最低覆盖 | Oracle 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for symbol, item in cell["symbols"].items():
            selected = item["oracle_selected"]
            failed = [name for name, passed in selected["checks"].items() if not passed]
            lines.append(
                "| {cell} | {symbol} | {lookback} | {threshold:.8f} | "
                "{pnl:.4f} | {coverage:.2%} | {passed} | {failed} |".format(
                    cell=cell_name,
                    symbol=symbol,
                    lookback=selected["lookback"],
                    threshold=selected["threshold"],
                    pnl=selected["summary"]["worst_seed_total_pnl"],
                    coverage=selected["summary"]["minimum_trade_coverage"],
                    passed="是" if selected["passed"] else "否",
                    failed=", ".join(failed),
                )
            )
    lines.extend(
        [
            "",
            "通过单元：{passed}/{total}。".format(
                passed=summary["passed_cell_symbol_count"],
                total=summary["cell_symbol_count"],
            ),
            "",
            f"结论：{payload['conclusion']}",
            "",
            "CURRENT Final OOS 未读取；生产默认值未修改。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估入场前网格步长振荡容量的不可部署乐观上界。"
    )
    parser.add_argument(
        "--round12-result",
        default="reports/cross-era-oos/round12-quadratic-volatility-defense-results.json",
    )
    parser.add_argument(
        "--round13-result",
        default="reports/cross-era-oos/round13-prehistory-quadratic-w2160-results.json",
    )
    parser.add_argument(
        "--asset-audit-result",
        default="reports/cross-era-oos/asset-scope-audit-results.json",
    )
    parser.add_argument(
        "--round14-result",
        default="reports/cross-era-oos/round14-spot-feasibility-results.json",
    )
    parser.add_argument(
        "--round15-result",
        default="reports/cross-era-oos/round15-long-horizon-results.json",
    )
    parser.add_argument(
        "--round16-result",
        default="reports/cross-era-oos/round16-trailing-shadow-pnl-results.json",
    )
    parser.add_argument(
        "--round17-result",
        default="reports/cross-era-oos/round17-symmetric-pairing-results.json",
    )
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("振荡容量上界协议哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    asset_path = Path(args.asset_audit_result).resolve()
    round14_path = Path(args.round14_result).resolve()
    round15_path = Path(args.round15_result).resolve()
    round16_path = Path(args.round16_result).resolve()
    round17_path = Path(args.round17_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        asset_path: ASSET_AUDIT_SHA256,
        round14_path: ROUND14_RESULT_SHA256,
        round15_path: ROUND15_RESULT_SHA256,
        round16_path: ROUND16_RESULT_SHA256,
        round17_path: ROUND17_RESULT_SHA256,
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")

    round12_payload = json.loads(round12_path.read_text(encoding="utf-8"))
    round13_payload = json.loads(round13_path.read_text(encoding="utf-8"))
    round14_payload = json.loads(round14_path.read_text(encoding="utf-8"))
    round16_payload = json.loads(round16_path.read_text(encoding="utf-8"))
    round17_payload = json.loads(round17_path.read_text(encoding="utf-8"))
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")
    if bool(round12_payload.get("final_oos_authorized")):
        raise ValueError("CURRENT Final OOS 被错误授权。")
    if round16_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 16 已存在候选，不能启动本上界评估。")
    if round17_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 17 已存在候选，不能启动本上界评估。")
    if round17_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 17 之后 CURRENT Final OOS 已不再封存。")
    if not str(round17_payload.get("conclusion") or "").startswith(
        "NO_ROBUST_SYMMETRIC_PAIRING_CANDIDATE"
    ):
        raise ValueError("Round 17 失败结论不匹配。")

    datasets: dict[str, dict[str, Any]] = {}
    current_manifests = tuple(str(item["manifest"]) for item in round12_payload["datasets"])
    current_config = profit_opt._base_research_config()
    current_metadata = [verify_frozen_dataset(path) for path in current_manifests]
    current_end, development_ids, validation_ids, current_isolation = (
        round15._current_authorized_end(current_metadata, current_config)
    )
    current_metadata, current_windows = round15._load_dataset(
        current_manifests,
        current_config,
        end_time=current_end,
    )
    asset_audit._validate_frozen_dataset(
        _dataset_brief(current_manifests, current_metadata),
        round12_payload["datasets"],
        label="CURRENT",
    )
    if {window.window_id for window in current_windows} != set(
        development_ids + validation_ids
    ):
        raise RuntimeError("CURRENT 授权窗口与日历切分不一致。")
    print("DATASET CURRENT", flush=True)
    datasets["CURRENT"] = _load_evidence(
        "CURRENT",
        current_manifests,
        current_config,
        current_windows,
        {"development": development_ids, "validation": validation_ids},
        args.workers,
        end_time=current_end,
    )
    del current_windows
    gc.collect()

    prehistory_manifests = tuple(
        str(item["manifest"]) for item in round13_payload["datasets"]
    )
    prehistory_config = profit_opt._base_research_config()
    prehistory_metadata, prehistory_windows = round15._load_dataset(
        prehistory_manifests,
        prehistory_config,
    )
    asset_audit._validate_frozen_dataset(
        _dataset_brief(prehistory_manifests, prehistory_metadata),
        round13_payload["datasets"],
        label="PREHISTORY",
    )
    prehistory_ids = round13._paired_ready_window_ids(prehistory_windows)
    print("DATASET PREHISTORY", flush=True)
    datasets["PREHISTORY"] = _load_evidence(
        "PREHISTORY",
        prehistory_manifests,
        prehistory_config,
        prehistory_windows,
        {"external": prehistory_ids},
        args.workers,
    )
    del prehistory_windows
    gc.collect()

    spot_manifests = tuple(str(item["manifest"]) for item in round14_payload["datasets"])
    spot_config = profit_opt._base_research_config()
    spot_metadata, spot_windows = round15._load_dataset(spot_manifests, spot_config)
    asset_audit._validate_frozen_dataset(
        _dataset_brief(spot_manifests, spot_metadata),
        round14_payload["datasets"],
        label="SPOT",
    )
    spot_ids, spot_quality = spot_round._paired_contiguous_window_ids(spot_windows)
    if spot_quality != round14_payload["data_quality"]:
        raise ValueError("Round 14 Spot 连续窗口质量记录不一致。")
    print("DATASET SPOT", flush=True)
    datasets["SPOT"] = _load_evidence(
        "SPOT",
        spot_manifests,
        spot_config,
        spot_windows,
        {"external": spot_ids},
        args.workers,
    )
    del spot_windows
    gc.collect()

    cells = _oracle_cells(datasets)
    upper_bound = _upper_bound_summary(cells)
    family_ready = bool(upper_bound["all_cells_have_oracle_solution"])
    conclusion = (
        "CYCLE_CAPACITY_FAMILY_WORTH_PREREGISTRATION：全部 16 个单元均存在不可部署的 oracle 通过解；仅允许另写正式协议。"
        if family_ready
        else "NO_PREREGISTERED_CYCLE_CAPACITY_CANDIDATE：至少一个单元在全部 lookback 和 oracle 阈值下仍无法通过，排除本特征家族。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "NON_DEPLOYABLE_OPTIMISTIC_UPPER_BOUND",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "direction_mode": "NEUTRAL",
        "lookbacks": list(LOOKBACKS),
        "seeds": list(asset_audit.DEFAULT_SEEDS),
        "oracle_uses_same_cell_outcomes": True,
        "oracle_is_deployable": False,
        "current_isolation": current_isolation,
        "feature_audit": {
            role: dataset["feature_audit"] for role, dataset in datasets.items()
        },
        "capacity_audit": {
            role: dataset["capacity_audit"] for role, dataset in datasets.items()
        },
        "execution_integrity": {
            role: dataset["execution_integrity"] for role, dataset in datasets.items()
        },
        "cells": cells,
        "upper_bound_summary": upper_bound,
        "formal_round18_preregistration_ready": family_ready,
        "selected_candidate_id": None,
        "final_oos_authorization_ready": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "round18-cycle-capacity-upper-bound-results.json"
    markdown_output = output_dir / "round18-cycle-capacity-upper-bound-report.md"
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"振荡容量上界结果已存在，拒绝覆盖: {output}")
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
