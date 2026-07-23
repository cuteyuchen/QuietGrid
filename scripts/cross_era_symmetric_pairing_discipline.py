from __future__ import annotations

import argparse
import concurrent.futures
import gc
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    "reports/cross-era-oos/round17-symmetric-pairing-discipline-protocol.md"
)
PROTOCOL_SHA256 = "b5f567e7067ebb15a7a4237b947f2aea503f9c936e9b681f8d33b47fa202fdee"
CANDIDATE_ID = "SYMMETRIC_PAIR_CAP1_TARGET075"
ASSET_AUDIT_SHA256 = "3d4c1df25da45f37e9661ae0797baecf4a9e799b42e397687d6eeeb62ac6ab27"
ROUND14_RESULT_SHA256 = "c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f"
ROUND15_RESULT_SHA256 = "131dc847d60012a1dcdf5fc601d5e9a4918ca18e3ff1000fb5f75776f5443fc2"
ROUND16_RESULT_SHA256 = "990ee916758de7f89cf6b7d6801d3887dbcce43c35764c48dde07994f9714f9d"
DIAGNOSTIC_HASHES = {
    "reports/robustness/btc-reduce-target-0p50-10seeds-20260721.json": (
        "96b3fa688eec5fad6fa7cf2a623232235cb86eb6fc9d470e3efaeceeb68e403d"
    ),
    "reports/robustness/btc-reduce-target-0p75-10seeds-20260721.json": (
        "2b5ca2e70353d6e21eeefa7200765a2fae795e532f6d284f2aa5a026206f55b5"
    ),
    "reports/robustness/eth-reduce-target-0p50-10seeds-20260721.json": (
        "87863c7dbd3daf4e85bac23c61898fbba44b360476b0aebb0577c4474732001b"
    ),
    "reports/robustness/eth-reduce-target-0p75-10seeds-20260721.json": (
        "8267a8c16d5e039490daf583d43b9b690e72bdf5576544dfa9d77c0c0a016e8a"
    ),
}
EXPECTED_INVENTORY_CAPS = {"BTCUSDT": 200.0, "ETHUSDT": 120.0}
EXPECTED_UNPAIRED_LOTS = 1
EXPECTED_REDUCE_TARGET = 0.75


def _candidate_components(base_config: Any) -> tuple[
    list[Any],
    dict[str, SymbolResearchPolicy],
    Any,
    Any,
]:
    parameters, locked_policies, maker_policy = profit_opt._locked_policy()
    policies = {
        symbol: replace(
            policy,
            entry_filter=None,
            max_unpaired_lots_per_side=EXPECTED_UNPAIRED_LOTS,
            reduce_target_step_fraction=EXPECTED_REDUCE_TARGET,
        )
        for symbol, policy in locked_policies.items()
    }
    config, candidate_maker_policy = round13._variant_config_and_policy(
        base_config,
        maker_policy,
        round13.CANDIDATE_ID,
    )
    _validate_candidate_definition(config, policies, candidate_maker_policy)
    return parameters, policies, config, candidate_maker_policy


def _validate_candidate_definition(
    config: Any,
    policies: Mapping[str, SymbolResearchPolicy],
    maker_policy: Any,
) -> None:
    if set(policies) != set(asset_audit.SYMBOLS):
        raise ValueError("Round 17 必须且只能定义 BTCUSDT、ETHUSDT 策略。")
    for symbol, policy in policies.items():
        if policy.parameter.direction_mode != GridDirectionMode.NEUTRAL:
            raise ValueError(f"Round 17 {symbol} direction_mode 必须保持 NEUTRAL。")
        if policy.entry_filter is not None:
            raise ValueError(f"Round 17 {symbol} 不允许入口过滤。")
        if policy.max_unpaired_lots_per_side != EXPECTED_UNPAIRED_LOTS:
            raise ValueError(f"Round 17 {symbol} 未配对 lot 上限不一致。")
        if abs(
            float(policy.reduce_target_step_fraction or 0.0)
            - EXPECTED_REDUCE_TARGET
        ) > 1e-12:
            raise ValueError(f"Round 17 {symbol} 减仓目标比例不一致。")
        if abs(
            float(policy.max_inventory_notional)
            - EXPECTED_INVENTORY_CAPS[symbol]
        ) > 1e-12:
            raise ValueError(f"Round 17 {symbol} 库存名义上限不一致。")
    if int(config.wind_down_bars) != round13.CANDIDATE_WIND_DOWN_BARS:
        raise ValueError("Round 17 wind-down 必须保持 2160 bars。")
    if str(config.unpaired_lot_cap_enforcement).upper() != "BAR_BOUNDARY":
        raise ValueError("Round 17 lot 上限必须在 BAR_BOUNDARY 执行。")
    if bool(config.profit_protection_enabled):
        raise ValueError("Round 17 不允许启用利润保护。")
    if float(config.volatility_reduce_expansion_ratio) != 0.0:
        raise ValueError("Round 17 不允许启用波动减仓。")
    if int(config.volatility_reduce_after_breaches) != 0:
        raise ValueError("Round 17 不允许启用波动减仓。")
    if int(maker_policy.reprice_interval_bars) != 5:
        raise ValueError("Round 17 Maker 重挂间隔必须保持 5 bars。")
    if abs(float(maker_policy.initial_offset_steps) - 1.10) > 1e-12:
        raise ValueError("Round 17 Maker 初始偏移必须保持 1.10。")
    if abs(float(maker_policy.unwind_fraction) - 1.0) > 1e-12:
        raise ValueError("Round 17 Maker unwind fraction 必须保持 1.0。")
    if abs(float(maker_policy.urgency_exponent) - 2.0) > 1e-12:
        raise ValueError("Round 17 Maker 紧迫度指数必须保持 2.0。")


def _candidate_payload(base_config: Any) -> dict[str, Any]:
    _parameters, policies, config, maker_policy = _candidate_components(base_config)
    return {
        "candidate_id": CANDIDATE_ID,
        "direction_mode": "NEUTRAL",
        "symbol_policies": {
            symbol: {
                "parameter": {
                    "range_multiplier": policy.parameter.range_multiplier,
                    "min_step_pct": policy.parameter.min_step_pct,
                    "stop_buffer_pct": policy.parameter.stop_buffer_pct,
                    "direction_mode": policy.parameter.direction_mode.value,
                },
                "parameter_id": policy.parameter.parameter_id,
                "capital": float(config.capital_by_symbol[symbol]),
                "max_inventory_notional": policy.max_inventory_notional,
                "entry_filter": None,
                "max_unpaired_lots_per_side": policy.max_unpaired_lots_per_side,
                "reduce_target_step_fraction": policy.reduce_target_step_fraction,
            }
            for symbol, policy in sorted(policies.items())
        },
        "backtest_policy": {
            "wind_down_bars": config.wind_down_bars,
            "unpaired_lot_cap_enforcement": config.unpaired_lot_cap_enforcement,
            "profit_protection_enabled": config.profit_protection_enabled,
            "volatility_reduce_enabled": False,
        },
        "maker_policy": asdict(maker_policy) | {"policy_id": maker_policy.policy_id},
    }


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
    parameters, policies, config, maker_policy = _candidate_components(base_config)
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
            raise RuntimeError("Round 17 worker cache 键长度不一致。")
        parameter_id = str(cache_key[0])
        symbol = str(cache_key[1]).strip().upper()
        window_id = str(cache_key[2])
        if symbol not in policies:
            raise RuntimeError(f"Round 17 worker 出现未授权标的: {symbol}")
        if window_id not in allowed_window_ids:
            raise RuntimeError("Round 17 worker 访问了授权窗口之外的数据。")
        policy = policies[symbol]
        if parameter_id != policy.parameter.parameter_id:
            raise RuntimeError(f"Round 17 {symbol} 参数集不一致。")
        expected = {
            3: float(research.config.maker_fill_probability),
            4: float(round13.CANDIDATE_WIND_DOWN_BARS),
            5: float(EXPECTED_INVENTORY_CAPS[symbol]),
            6: 5.0,
            7: 1.10,
            8: 1.0,
            9: float(EXPECTED_UNPAIRED_LOTS),
            10: EXPECTED_REDUCE_TARGET,
            11: float(maker_fee),
            12: float(taker_fee),
            13: float(slippage_bps),
            14: 2.0,
            15: float(seed),
        }
        for index, expected_value in expected.items():
            if abs(float(cache_key[index]) - expected_value) > 1e-12:
                raise RuntimeError(
                    f"Round 17 {symbol} worker cache 参数 {index} 不一致: "
                    f"{cache_key[index]} != {expected_value}"
                )
        symbols_by_window.setdefault(window_id, set()).add(symbol)
    if len(research._cache) != expected_entries:
        raise RuntimeError(
            f"Round 17 worker cache 数量不一致: {len(research._cache)} != {expected_entries}"
        )
    if set(symbols_by_window) != allowed_window_ids:
        raise RuntimeError("Round 17 worker 未覆盖全部授权窗口。")
    if any(set(symbols) != set(asset_audit.SYMBOLS) for symbols in symbols_by_window.values()):
        raise RuntimeError("Round 17 worker 未对每个窗口同时覆盖 BTC/ETH。")
    _validate_candidate_definition(research.config, policies, maker_policy)
    return {
        "window_count": len(symbols_by_window),
        "symbol_window_count": expected_entries,
        "cache_entry_count": len(research._cache),
        "wind_down_bars": round13.CANDIDATE_WIND_DOWN_BARS,
        "reprice_interval_bars": 5,
        "initial_offset_steps": 1.10,
        "unwind_fraction": 1.0,
        "urgency_exponent": 2.0,
        "max_unpaired_lots_per_side": EXPECTED_UNPAIRED_LOTS,
        "reduce_target_step_fraction": EXPECTED_REDUCE_TARGET,
        "maker_fee_rate": maker_fee,
        "taker_fee_rate": taker_fee,
        "stop_slippage_bps": slippage_bps,
        "seed": seed,
        "profit_protection_enabled": False,
        "volatility_reduce_enabled": False,
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
                raise RuntimeError("Round 17 worker 返回了错误种子。")
            raw_runs[scenario][seed] = runs
            task_integrity.append({"scenario": scenario, **integrity})
    for scenario in asset_audit.SCENARIOS:
        if tuple(sorted(raw_runs[scenario])) != asset_audit.DEFAULT_SEEDS:
            raise RuntimeError(f"Round 17 {scenario} 种子覆盖不完整。")
    expected_task_count = len(asset_audit.SCENARIOS) * len(asset_audit.DEFAULT_SEEDS)
    if len(task_integrity) != expected_task_count:
        raise RuntimeError("Round 17 worker 完整性任务数量不一致。")
    expected_window_count = len(
        {window_id for ids in split_ids.values() for window_id in ids}
    )
    if any(
        not item["passed"] or int(item["window_count"]) != expected_window_count
        for item in task_integrity
    ):
        raise RuntimeError("Round 17 worker 完整性审计未全部通过。")
    return raw_runs, {
        "task_count": expected_task_count,
        "window_count": expected_window_count,
        "all_tasks_passed": True,
        "tasks": sorted(task_integrity, key=lambda item: (item["scenario"], item["seed"])),
    }


def _load_evidence(
    role: str,
    manifests: Sequence[str],
    base_config: Any,
    split_ids: Mapping[str, Sequence[str]],
    workers: int,
    *,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    raw_runs, integrity = _run_dataset(
        manifests,
        base_config,
        split_ids,
        workers,
        end_time=end_time,
    )
    expected_count = len({window_id for ids in split_ids.values() for window_id in ids})
    if int(integrity["window_count"]) != expected_count:
        raise RuntimeError(f"{role} worker 窗口覆盖数量不一致。")
    return {
        "base_config": base_config,
        "split_ids": {name: tuple(ids) for name, ids in split_ids.items()},
        "raw_runs": raw_runs,
        "execution_integrity": integrity,
    }


def _candidate_cells(datasets: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    for role, dataset in datasets.items():
        for scenario, scenario_runs in dataset["raw_runs"].items():
            for split_name in dataset["split_ids"]:
                cell_name = f"{role}_{split_name.upper()}_{scenario}"
                symbols: dict[str, Any] = {}
                for symbol in asset_audit.SYMBOLS:
                    metrics_by_seed = {}
                    for seed in asset_audit.DEFAULT_SEEDS:
                        results = [
                            result
                            for result in scenario_runs[seed][split_name][1]
                            if result.symbol == symbol
                        ]
                        metrics_by_seed[seed] = aggregate_results(
                            results,
                            capital_per_symbol=float(
                                dataset["base_config"].capital_by_symbol[symbol]
                            ),
                            symbol_count=1,
                        )
                    summary = asset_audit._summarize_symbol(metrics_by_seed)
                    checks = asset_audit._scope_checks(summary)
                    symbols[symbol] = {
                        "summary": summary,
                        "checks": checks,
                        "passed": all(checks.values()),
                    }
                cells[cell_name] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "window_count": len(dataset["split_ids"][split_name]),
                    "symbols": symbols,
                }
    return cells


def _report_markdown(payload: Mapping[str, Any]) -> str:
    selection = payload["candidate"]["selection"]
    lines = [
        "# Round 17：对称配对纪律 Phase A 结果",
        "",
        "BTC/ETH 均限制每侧最多 1 个未配对 lot，并以 0.75 个完整网格步长作为减仓目标；CURRENT Final OOS 未评估。",
        "",
        "| 候选 | 通过单元 | 最差种子 PnL | 最低覆盖 | 全通过 |",
        "| --- | ---: | ---: | ---: | --- |",
        (
            "| `{candidate}` | {passed}/{total} | {worst:.4f} | {coverage:.2%} | {eligible} |"
        ).format(
            candidate=CANDIDATE_ID,
            passed=selection["passed_cell_symbol_count"],
            total=selection["cell_symbol_count"],
            worst=selection["minimum_worst_seed_total_pnl"],
            coverage=selection["minimum_trade_coverage"],
            eligible="是" if selection["all_cells_passed"] else "否",
        ),
        "",
    ]
    failed = []
    for cell_name, cell in payload["candidate"]["cells"].items():
        for symbol, item in cell["symbols"].items():
            if item["passed"]:
                continue
            summary = item["summary"]
            failed_checks = [name for name, passed in item["checks"].items() if not passed]
            failed.append(
                (
                    cell_name,
                    symbol,
                    summary["worst_seed_total_pnl"],
                    summary["minimum_trade_coverage"],
                    ", ".join(failed_checks),
                )
            )
    if failed:
        lines.extend(
            [
                "## 未通过单元",
                "",
                "| 单元 | 标的 | 最差种子 PnL | 最低覆盖 | 失败检查 |",
                "| --- | --- | ---: | ---: | --- |",
            ]
        )
        for cell_name, symbol, pnl, coverage, checks in failed:
            lines.append(
                f"| {cell_name} | {symbol} | {pnl:.4f} | {coverage:.2%} | {checks} |"
            )
        lines.append("")
    lines.extend(
        [
            f"选中候选：{payload['selected_candidate_id'] or '无'}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "生产默认值未修改；没有独立授权文件时，CURRENT Final OOS 继续封存。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="测试 BTC/ETH 对称配对纪律的跨周期 Phase A。"
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
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 17 协议哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    asset_path = Path(args.asset_audit_result).resolve()
    round14_path = Path(args.round14_result).resolve()
    round15_path = Path(args.round15_result).resolve()
    round16_path = Path(args.round16_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        asset_path: ASSET_AUDIT_SHA256,
        round14_path: ROUND14_RESULT_SHA256,
        round15_path: ROUND15_RESULT_SHA256,
        round16_path: ROUND16_RESULT_SHA256,
        **{
            Path(path).resolve(): expected
            for path, expected in DIAGNOSTIC_HASHES.items()
        },
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")

    round12_payload = json.loads(round12_path.read_text(encoding="utf-8"))
    round13_payload = json.loads(round13_path.read_text(encoding="utf-8"))
    round14_payload = json.loads(round14_path.read_text(encoding="utf-8"))
    round15_payload = json.loads(round15_path.read_text(encoding="utf-8"))
    round16_payload = json.loads(round16_path.read_text(encoding="utf-8"))
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")
    if bool(round12_payload.get("final_oos_authorized")):
        raise ValueError("CURRENT Final OOS 被错误授权。")
    if round15_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 15 已存在候选，不能启动 Round 17。")
    if round16_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 16 已存在候选，不能启动失败后 Round 17。")
    if round16_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 16 之后 CURRENT Final OOS 已不再封存。")
    if not str(round16_payload.get("conclusion") or "").startswith(
        "NO_ROBUST_TRAILING_SHADOW_CANDIDATE"
    ):
        raise ValueError("Round 16 失败结论不匹配。")

    datasets: dict[str, dict[str, Any]] = {}
    current_manifests = tuple(str(item["manifest"]) for item in round12_payload["datasets"])
    current_config = profit_opt._base_research_config()
    candidate_definition = _candidate_payload(current_config)
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
        {"external": spot_ids},
        args.workers,
    )
    del spot_windows
    gc.collect()

    cells = _candidate_cells(datasets)
    selection = round15._selection_metrics(cells)
    selected_candidate_id = CANDIDATE_ID if selection["all_cells_passed"] else None
    conclusion = (
        "SYMMETRIC_PAIRING_CANDIDATE_READY_FOR_AUTHORIZATION："
        f"{CANDIDATE_ID} 通过全部 16 个 Phase A 单元；尚未授权 Final OOS。"
        if selected_candidate_id
        else "NO_ROBUST_SYMMETRIC_PAIRING_CANDIDATE：唯一注册候选未通过全部 16 个 Phase A 单元。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "direction_mode": "NEUTRAL",
        "seeds": list(asset_audit.DEFAULT_SEEDS),
        "scenarios": {
            name: {
                "maker_fee_rate": cost[0],
                "taker_fee_rate": cost[1],
                "stop_slippage_bps": cost[2],
            }
            for name, cost in asset_audit.SCENARIOS.items()
        },
        "current_isolation": current_isolation,
        "execution_integrity": {
            role: dataset["execution_integrity"] for role, dataset in datasets.items()
        },
        "candidate": {
            **candidate_definition,
            "cells": cells,
            "selection": selection,
        },
        "eligible_candidate_ids": [CANDIDATE_ID] if selected_candidate_id else [],
        "selected_candidate_id": selected_candidate_id,
        "final_oos_authorization_ready": selected_candidate_id is not None,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "round17-symmetric-pairing-results.json"
    markdown_output = output_dir / "round17-symmetric-pairing-report.md"
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"Round 17 结果已存在，拒绝覆盖: {output}")
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
