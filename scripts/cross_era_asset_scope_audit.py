from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
)
from scripts.cross_era_oos import (
    _dataset_brief,
    _load_research_state,
    _registered_candidates,
    _write_json,
)
from scripts.cross_era_round13_diagnose import (
    ROUND13_RESULT_SHA256,
    _sha256,
    _validate_round13_result,
)
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    CandidateEvidence,
)
from scripts.robustness import (
    AggregateMetrics,
    RobustnessResearch,
    WindowResult,
    aggregate_results,
)


UTC = timezone.utc
ROUND12_RESULT_SHA256 = (
    "d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d"
)
SCENARIOS = {"BASE": BASE_COST, "COST50": COST_50}
SYMBOLS = ("BTCUSDT", "ETHUSDT")
EXPECTED_CURRENT_SPLIT_COUNTS = {
    "development": 108,
    "validation": 54,
    "final_oos": 54,
}


def _validate_frozen_dataset(
    actual: Sequence[Mapping[str, Any]],
    expected: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> None:
    if list(actual) != list(expected):
        raise ValueError(f"{label} 冻结数据与来源结果不一致。")


def _validated_current_splits(
    split: Any,
    recorded_split: Mapping[str, Any],
) -> dict[str, tuple[str, ...]]:
    partitions = {
        "development": tuple(split.development),
        "validation": tuple(split.validation),
        "final_oos": tuple(split.final_oos),
    }
    actual_counts = {name: len(ids) for name, ids in partitions.items()}
    if actual_counts != EXPECTED_CURRENT_SPLIT_COUNTS:
        raise ValueError(
            "CURRENT 切分数量不一致: "
            f"{actual_counts} != {EXPECTED_CURRENT_SPLIT_COUNTS}"
        )
    recorded_counts = {
        "development": int(recorded_split.get("development_count", -1)),
        "validation": int(recorded_split.get("validation_count", -1)),
        "final_oos": int(recorded_split.get("final_oos_count", -1)),
    }
    if recorded_counts != EXPECTED_CURRENT_SPLIT_COUNTS:
        raise ValueError(
            "Round 12 记录的切分数量不一致: "
            f"{recorded_counts} != {EXPECTED_CURRENT_SPLIT_COUNTS}"
        )
    partition_sets = {name: set(ids) for name, ids in partitions.items()}
    if any(
        len(partition_sets[name]) != len(partitions[name])
        for name in partitions
    ):
        raise RuntimeError("CURRENT 切分包含重复窗口。")
    if (
        partition_sets["development"] & partition_sets["validation"]
        or partition_sets["development"] & partition_sets["final_oos"]
        or partition_sets["validation"] & partition_sets["final_oos"]
    ):
        raise RuntimeError("CURRENT Development/Validation/Final OOS 切分重叠。")
    return {
        "development": partitions["development"],
        "validation": partitions["validation"],
    }


def _validate_external_execution_integrity(
    actual: Mapping[str, Any],
    recorded: Mapping[str, Any],
) -> None:
    expected = recorded.get(round13.CANDIDATE_ID)
    if not isinstance(expected, Mapping):
        raise ValueError(
            f"Round 13 缺少 {round13.CANDIDATE_ID} 执行完整性记录。"
        )
    if dict(actual) != dict(expected):
        raise RuntimeError(
            "2020H1 执行完整性与 Round 13 固定候选不一致。"
        )


def _asset_seed_worker(
    seed: int,
    split_ids: Mapping[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[
    int,
    dict[str, tuple[AggregateMetrics, list[WindowResult]]],
    dict[str, Any],
]:
    state = profit_opt._WORKER_STATE
    config, maker_policy = round13._variant_config_and_policy(
        state["base_config"],
        state["maker_policy"],
        round13.CANDIDATE_ID,
    )
    research = RobustnessResearch(
        state["windows"],
        state["parameters"],
        config,
        dataset_metadata=state["metadata"],
    )
    maker_fee, taker_fee, slippage_bps = cost
    runs = {
        split_name: research.evaluate_joint_policy_windows(
            state["symbol_policies"],
            maker_policy,
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
    observation = round13._verify_worker_cache(
        research,
        allowed_window_ids=allowed,
        expected_wind_down_bars=round13.CANDIDATE_WIND_DOWN_BARS,
        expected_exponent=round13.CANDIDATE_EXPONENT,
    )
    return seed, runs, observation


def _run_dataset(
    manifests: Sequence[str],
    base_config: Any,
    split_ids: Mapping[str, Sequence[str]],
    workers: int,
) -> tuple[
    dict[str, dict[int, dict[str, tuple[AggregateMetrics, list[WindowResult]]]]],
    dict[str, Any],
]:
    raw_runs = {scenario: {} for scenario in SCENARIOS}
    observations = []
    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(workers, len(DEFAULT_SEEDS)),
        initializer=profit_opt._initialize_worker,
        initargs=(tuple(manifests), base_config),
    ) as executor:
        for scenario, cost in SCENARIOS.items():
            print(f"AUDITING {scenario}", flush=True)
            for seed in DEFAULT_SEEDS:
                future = executor.submit(_asset_seed_worker, seed, split_ids, cost)
                futures[future] = (scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            scenario, expected_seed = futures[future]
            seed, runs, observation = future.result()
            if seed != expected_seed:
                raise RuntimeError("资产范围 worker 返回了错误种子。")
            raw_runs[scenario][seed] = runs
            observations.append(observation)
    if not observations or any(item != observations[0] for item in observations[1:]):
        raise RuntimeError("资产范围 worker 执行参数不一致。")
    return raw_runs, observations[0]


def _profit_factor_pass(metrics: AggregateMetrics) -> bool:
    if metrics.profit_factor is not None:
        return float(metrics.profit_factor) > 1.0
    return float(metrics.total_pnl) > 0


def _summarize_symbol(
    metrics_by_seed: Mapping[int, AggregateMetrics],
) -> dict[str, Any]:
    if tuple(sorted(metrics_by_seed)) != DEFAULT_SEEDS:
        raise ValueError("资产汇总的种子集合不完整。")
    ordered = [metrics_by_seed[seed] for seed in DEFAULT_SEEDS]
    seed_total_pnl = {
        str(seed): float(metrics_by_seed[seed].total_pnl) for seed in DEFAULT_SEEDS
    }
    profit_factors = {
        str(seed): metrics_by_seed[seed].profit_factor for seed in DEFAULT_SEEDS
    }
    return {
        "seed_count": len(ordered),
        "seed_total_pnl": seed_total_pnl,
        "mean_seed_total_pnl": statistics.mean(seed_total_pnl.values()),
        "median_seed_total_pnl": statistics.median(seed_total_pnl.values()),
        "worst_seed_total_pnl": min(seed_total_pnl.values()),
        "positive_seed_count": sum(value > 0 for value in seed_total_pnl.values()),
        "seed_profit_factor": profit_factors,
        "all_seed_profit_factors_gt_1": all(
            _profit_factor_pass(metrics) for metrics in ordered
        ),
        "maximum_drawdown_pct": max(float(metrics.max_drawdown_pct) for metrics in ordered),
        "worst_best_window_concentration": max(
            float(metrics.best_window_concentration) for metrics in ordered
        ),
        "minimum_trade_coverage": min(float(metrics.trade_coverage) for metrics in ordered),
        "mean_trade_coverage": statistics.mean(
            float(metrics.trade_coverage) for metrics in ordered
        ),
        "mean_positive_window_ratio": statistics.mean(
            float(metrics.positive_window_ratio) for metrics in ordered
        ),
        "mean_fees_paid": statistics.mean(float(metrics.fees_paid) for metrics in ordered),
        "mean_fill_count": statistics.mean(float(metrics.fill_count) for metrics in ordered),
        "mean_pair_count": statistics.mean(float(metrics.pair_count) for metrics in ordered),
        "runs": {
            str(seed): asdict(metrics_by_seed[seed]) for seed in DEFAULT_SEEDS
        },
    }


def _scope_checks(summary: Mapping[str, Any]) -> dict[str, bool]:
    return {
        "all_seeds_positive": int(summary["positive_seed_count"])
        == int(summary["seed_count"]),
        "worst_seed_positive": float(summary["worst_seed_total_pnl"]) > 0,
        "all_seed_profit_factors_gt_1": bool(
            summary["all_seed_profit_factors_gt_1"]
        ),
        "max_drawdown_le_5pct": float(summary["maximum_drawdown_pct"]) <= 0.05,
        "best_window_concentration_le_35pct": float(
            summary["worst_best_window_concentration"]
        )
        <= 0.35,
        "minimum_trade_coverage_ge_25pct": float(
            summary["minimum_trade_coverage"]
        )
        >= 0.25,
    }


def _filtered_symbol_cells(
    raw_runs: Mapping[
        str,
        Mapping[int, Mapping[str, tuple[AggregateMetrics, list[WindowResult]]]],
    ],
    *,
    windows: Sequence[Any],
    metadata: Sequence[dict[str, Any]],
    base_config: Any,
    split_ids: Mapping[str, Sequence[str]],
    role: str,
) -> dict[str, Any]:
    parameters, _policies, _maker_policy = round13._locked_policy()
    research = RobustnessResearch(
        windows,
        parameters,
        base_config,
        dataset_metadata=metadata,
    )
    all_ids = tuple(
        window_id for ids in split_ids.values() for window_id in ids
    )
    contexts = _populate_entry_decisions(research, all_ids)
    baseline_candidate = _registered_candidates()[0]
    cells = {}
    for scenario, scenario_runs in raw_runs.items():
        for split_name in split_ids:
            evidence = CandidateEvidence(
                baseline_candidate,
                {
                    seed: {split_name: scenario_runs[seed][split_name]}
                    for seed in DEFAULT_SEEDS
                },
            )
            filtered = _filtered_evidence_for_symbols(
                evidence,
                round13.FIXED_FILTERS,
                contexts,
                candidate_id=round13.CANDIDATE_ID,
                round_name="cross_era_asset_scope_audit",
                split_name=split_name,
            )
            cell_name = f"{role}_{split_name.upper()}_{scenario}"
            symbols = {}
            for symbol in SYMBOLS:
                metrics_by_seed = {
                    seed: aggregate_results(
                        [
                            result
                            for result in filtered.runs[seed][split_name][1]
                            if result.symbol == symbol
                        ],
                        capital_per_symbol=float(base_config.capital_by_symbol[symbol]),
                        symbol_count=1,
                    )
                    for seed in DEFAULT_SEEDS
                }
                summary = _summarize_symbol(metrics_by_seed)
                checks = _scope_checks(summary)
                symbols[symbol] = {
                    "summary": summary,
                    "checks": checks,
                    "passed": all(checks.values()),
                }
            cells[cell_name] = {
                "role": role,
                "split": split_name,
                "scenario": scenario,
                "window_count": len(split_ids[split_name]),
                "symbols": symbols,
            }
    return cells


def _asset_verdict(cells: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    failed = [
        name
        for name, cell in cells.items()
        if not bool(cell["symbols"][symbol]["passed"])
    ]
    return {
        "cell_count": len(cells),
        "passed_cell_count": len(cells) - len(failed),
        "failed_cells": failed,
        "all_cells_passed": not failed,
        "minimum_worst_seed_total_pnl": min(
            float(cell["symbols"][symbol]["summary"]["worst_seed_total_pnl"])
            for cell in cells.values()
        ),
        "maximum_drawdown_pct": max(
            float(cell["symbols"][symbol]["summary"]["maximum_drawdown_pct"])
            for cell in cells.values()
        ),
        "maximum_best_window_concentration": max(
            float(
                cell["symbols"][symbol]["summary"][
                    "worst_best_window_concentration"
                ]
            )
            for cell in cells.values()
        ),
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# W2160 + E2 固定策略的 BTC/ETH 资产范围审计",
        "",
        "本审计在固定参数、固定入口过滤和六个固定种子下，比较 CURRENT Development、已消费 Validation 与 2020H1 外部区间。Final OOS 未读取。",
        "",
        "| 单元 | 标的 | 平均种子 PnL | 最差种子 | 正种子 | 最差 PF 通过 | 最大回撤 | 最大集中度 | 门槛 |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for cell_name, cell in payload["cells"].items():
        for symbol in SYMBOLS:
            item = cell["symbols"][symbol]
            summary = item["summary"]
            lines.append(
                "| `{cell}` | {symbol} | {mean:.4f} | {worst:.4f} | {positive}/6 | "
                "{pf} | {drawdown:.2%} | {concentration:.2%} | {passed}/6 |".format(
                    cell=cell_name,
                    symbol=symbol,
                    mean=summary["mean_seed_total_pnl"],
                    worst=summary["worst_seed_total_pnl"],
                    positive=summary["positive_seed_count"],
                    pf="是" if summary["all_seed_profit_factors_gt_1"] else "否",
                    drawdown=summary["maximum_drawdown_pct"],
                    concentration=summary["worst_best_window_concentration"],
                    passed=sum(item["checks"].values()),
                )
            )
    lines.extend(["", "## 资产判定", ""])
    for symbol in SYMBOLS:
        verdict = payload["asset_verdicts"][symbol]
        lines.append(
            f"- {symbol}：通过 {verdict['passed_cell_count']}/{verdict['cell_count']} 个单元；"
            f"失败单元：{', '.join(verdict['failed_cells']) or '无'}。"
        )
    lines.extend([
        "",
        f"结论：{payload['conclusion']}",
        "",
        "本审计不改变资产范围，不注册候选，不读取 Final OOS，也不修改生产默认值。",
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="审计 W2160 + E2 固定策略在 BTC/ETH 单资产上的跨周期稳定性。"
    )
    parser.add_argument(
        "--round12-result",
        default="reports/cross-era-oos/round12-quadratic-volatility-defense-results.json",
    )
    parser.add_argument(
        "--round13-result",
        default="reports/cross-era-oos/round13-prehistory-quadratic-w2160-results.json",
    )
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    if _sha256(round12_path) != ROUND12_RESULT_SHA256:
        raise ValueError("Round 12 冻结结果哈希不一致。")
    if _sha256(round13_path) != ROUND13_RESULT_SHA256:
        raise ValueError("Round 13 冻结结果哈希不一致。")
    round12_payload = json.loads(round12_path.read_text(encoding="utf-8"))
    round13_payload = json.loads(round13_path.read_text(encoding="utf-8"))
    _validate_round13_result(round13_payload)
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 12 Final OOS 已不再封存。")
    if round12_payload.get("eligible_candidate_ids"):
        raise ValueError("Round 12 已存在合格候选。")

    current_manifests = tuple(
        str(item["manifest"]) for item in round12_payload["datasets"]
    )
    current_config, current_metadata, current_windows, current_split = (
        _load_research_state(current_manifests)
    )
    _validate_frozen_dataset(
        _dataset_brief(current_manifests, current_metadata),
        round12_payload["datasets"],
        label="CURRENT",
    )
    current_splits = _validated_current_splits(
        current_split,
        round12_payload["split"],
    )
    accessed_current_ids = {
        window_id for ids in current_splits.values() for window_id in ids
    }
    if accessed_current_ids & set(current_split.final_oos):
        raise RuntimeError("资产范围审计触及 Final OOS。")
    print("DATASET CURRENT", flush=True)
    current_raw, current_integrity = _run_dataset(
        current_manifests,
        current_config,
        current_splits,
        args.workers,
    )
    current_cells = _filtered_symbol_cells(
        current_raw,
        windows=current_windows,
        metadata=current_metadata,
        base_config=current_config,
        split_ids=current_splits,
        role="CURRENT",
    )

    external_manifests = tuple(
        str(item["manifest"]) for item in round13_payload["datasets"]
    )
    external_config = profit_opt._base_research_config()
    external_metadata, external_windows = profit_opt._load_data(
        external_manifests,
        external_config,
    )
    _validate_frozen_dataset(
        _dataset_brief(external_manifests, external_metadata),
        round13_payload["datasets"],
        label="2020H1",
    )
    external_ids = round13._paired_ready_window_ids(external_windows)
    external_splits = {"external": external_ids}
    print("DATASET PREHISTORY", flush=True)
    external_raw, external_integrity = _run_dataset(
        external_manifests,
        external_config,
        external_splits,
        args.workers,
    )
    _validate_external_execution_integrity(
        external_integrity,
        round13_payload["execution_integrity"],
    )
    external_cells = _filtered_symbol_cells(
        external_raw,
        windows=external_windows,
        metadata=external_metadata,
        base_config=external_config,
        split_ids=external_splits,
        role="PREHISTORY",
    )

    cells = current_cells | external_cells
    verdicts = {symbol: _asset_verdict(cells, symbol) for symbol in SYMBOLS}
    eligible = [
        symbol for symbol, verdict in verdicts.items() if verdict["all_cells_passed"]
    ]
    conclusion = (
        "ASSET_SCOPE_ELIGIBLE_FOR_PREREGISTRATION: " + ", ".join(eligible)
        if eligible
        else "NO_ROBUST_ASSET_SCOPE：BTC 与 ETH 均未通过全部跨周期单资产门槛。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "diagnostic_role": "ASSET_SCOPE_AUDIT_ONLY",
        "round12_result_sha256": ROUND12_RESULT_SHA256,
        "round13_result_sha256": ROUND13_RESULT_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "policy": {
            "wind_down_bars": round13.CANDIDATE_WIND_DOWN_BARS,
            "urgency_exponent": round13.CANDIDATE_EXPONENT,
            "direction_mode": "NEUTRAL",
            "fixed_filters": {
                symbol: asdict(entry_filter)
                for symbol, entry_filter in round13.FIXED_FILTERS.items()
            },
        },
        "seeds": list(DEFAULT_SEEDS),
        "execution_integrity": {
            "CURRENT": current_integrity,
            "PREHISTORY": external_integrity,
        },
        "cells": cells,
        "asset_verdicts": verdicts,
        "eligible_asset_scopes": eligible,
        "candidate_preregistered": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "asset-scope-audit-results.json"
    markdown_output = output_dir / "asset-scope-audit-report.md"
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"资产范围审计已存在，拒绝覆盖: {output}")
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
