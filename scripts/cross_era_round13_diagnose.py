from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
)
from scripts.cross_era_oos import _dataset_brief, _registered_candidates, _write_json
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    CandidateEvidence,
)
from scripts.robustness import RobustnessResearch, WindowResult


UTC = timezone.utc
ROUND13_RESULT_SHA256 = (
    "1f8387048a67d8399d6bb0edb75dd504f5e6a1357f848eafb46c1524fe6903c3"
)
SCENARIOS = {
    "BASE": BASE_COST,
    "COST50": COST_50,
}
VARIANTS = (
    round13.REFERENCE_VARIANT_ID,
    round13.CANDIDATE_ID,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _entry_features(context: Any | None) -> dict[str, float] | None:
    if context is None or context.entry_decision is None:
        return None
    decision = context.entry_decision
    features = decision.features
    return {
        "directional_efficiency": float(features.directional_efficiency),
        "volatility_expansion": float(features.volatility_expansion),
        "reversal_ratio": float(features.reversal_ratio),
        "grid_score": float(decision.grid_score),
    }


def _aggregate_windows(
    results_by_seed: Mapping[int, Sequence[WindowResult]],
    *,
    symbol: str,
    contexts: Mapping[tuple[str, str], Any] | None = None,
) -> list[dict[str, Any]]:
    if not results_by_seed:
        raise ValueError("窗口诊断至少需要一个种子。")
    expected_seeds = tuple(sorted(int(seed) for seed in results_by_seed))
    grouped: dict[str, list[tuple[int, WindowResult]]] = defaultdict(list)
    for seed in expected_seeds:
        seen: set[str] = set()
        for result in results_by_seed[seed]:
            if result.symbol != symbol:
                continue
            if result.window_id in seen:
                raise ValueError(f"种子 {seed} 的窗口 {result.window_id} 重复。")
            seen.add(result.window_id)
            grouped[result.window_id].append((seed, result))

    rows = []
    for window_id, seeded_results in grouped.items():
        seeded_results.sort(key=lambda item: item[0])
        actual_seeds = tuple(seed for seed, _result in seeded_results)
        if actual_seeds != expected_seeds:
            raise ValueError(
                f"窗口 {window_id} 种子覆盖不完整: "
                f"expected={expected_seeds} actual={actual_seeds}"
            )
        all_results = [result for _seed, result in seeded_results]
        traded = [result for result in all_results if result.status == "TRADED"]
        pnl_values = [float(result.pnl) for result in traded]
        stopped = [
            int(result.stopped_at_index)
            for result in traded
            if result.stopped_at_index is not None
        ]
        seed_payload = [
            {
                "seed": seed,
                "status": result.status,
                "reason": result.reason,
                "pnl": result.pnl,
                "gross_grid_pnl": result.gross_grid_pnl,
                "paired_grid_pnl": result.paired_grid_pnl,
                "stop_exit_pnl": result.stop_exit_pnl,
                "stop_exit_cost": result.stop_exit_cost,
                "fees_paid": result.fees_paid,
                "funding_paid": result.funding_paid,
                "exit_slippage_cost": result.exit_slippage_cost,
                "fill_count": result.fill_count,
                "pair_count": result.pair_count,
                "max_inventory_utilization": result.max_inventory_utilization,
                "stopped_at_index": result.stopped_at_index,
            }
            for seed, result in seeded_results
        ]
        row = {
            "symbol": symbol,
            "window_id": window_id,
            "market_close": all_results[0].market_close,
            "seed_count": len(expected_seeds),
            "status_counts": dict(sorted(Counter(
                result.status for result in all_results
            ).items())),
            "reason_counts": dict(sorted(Counter(
                result.reason for result in all_results
            ).items())),
            "traded_seed_count": len(traded),
            "positive_seed_count": sum(value > 0 for value in pnl_values),
            "negative_seed_count": sum(value < 0 for value in pnl_values),
            "zero_seed_count": sum(value == 0 for value in pnl_values),
            "all_traded_seeds_negative": (
                len(traded) == len(expected_seeds)
                and all(value < 0 for value in pnl_values)
            ),
            "total_pnl": sum(pnl_values),
            "mean_pnl": _mean(pnl_values),
            "median_pnl": statistics.median(pnl_values) if pnl_values else 0.0,
            "minimum_pnl": min(pnl_values, default=0.0),
            "maximum_pnl": max(pnl_values, default=0.0),
            "mean_gross_grid_pnl": _mean([
                float(result.gross_grid_pnl) for result in traded
            ]),
            "mean_paired_grid_pnl": _mean([
                float(result.paired_grid_pnl) for result in traded
            ]),
            "mean_stop_exit_pnl": _mean([
                float(result.stop_exit_pnl) for result in traded
            ]),
            "mean_stop_exit_cost": _mean([
                float(result.stop_exit_cost) for result in traded
            ]),
            "mean_fees_paid": _mean([
                float(result.fees_paid) for result in traded
            ]),
            "mean_funding_paid": _mean([
                float(result.funding_paid) for result in traded
            ]),
            "mean_exit_slippage_cost": _mean([
                float(result.exit_slippage_cost) for result in traded
            ]),
            "mean_fill_count": _mean([
                float(result.fill_count) for result in traded
            ]),
            "mean_pair_count": _mean([
                float(result.pair_count) for result in traded
            ]),
            "maximum_inventory_utilization": max(
                (float(result.max_inventory_utilization) for result in traded),
                default=0.0,
            ),
            "stopped_seed_count": len(stopped),
            "stopped_within_30_bars": sum(value <= 30 for value in stopped),
            "stopped_within_60_bars": sum(value <= 60 for value in stopped),
            "stopped_within_120_bars": sum(value <= 120 for value in stopped),
            "minimum_stopped_at_index": min(stopped, default=None),
            "median_stopped_at_index": (
                statistics.median(stopped) if stopped else None
            ),
            "maximum_stopped_at_index": max(stopped, default=None),
            "zero_pair_seed_count": sum(result.pair_count == 0 for result in traded),
            "single_fill_seed_count": sum(result.fill_count <= 1 for result in traded),
            "single_fill_stop_seed_count": sum(
                result.fill_count <= 1 and result.stopped_at_index is not None
                for result in traded
            ),
            "entry_features": _entry_features(
                (contexts or {}).get((symbol, window_id))
            ),
            "seed_results": seed_payload,
        }
        rows.append(row)
    return sorted(rows, key=lambda item: (item["market_close"], item["window_id"]))


def _comparison_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    traded = [row for row in rows if int(row["candidate_traded_seed_count"]) > 0]
    negative = [row for row in traded if float(row["candidate_mean_pnl"]) < 0]
    loss_total = sum(abs(float(row["candidate_total_pnl"])) for row in negative)
    ranked_losses = sorted(
        negative,
        key=lambda row: (float(row["candidate_total_pnl"]), row["window_id"]),
    )
    return {
        "window_count": len(rows),
        "traded_window_count": len(traded),
        "candidate_total_pnl": sum(float(row["candidate_total_pnl"]) for row in traded),
        "reference_total_pnl": sum(float(row["reference_total_pnl"]) for row in traded),
        "candidate_minus_reference_total_pnl": sum(
            float(row["candidate_minus_reference_total_pnl"]) for row in traded
        ),
        "negative_window_count": len(negative),
        "all_seed_negative_window_count": sum(
            bool(row["candidate_all_traded_seeds_negative"]) for row in traded
        ),
        "improved_window_count": sum(
            float(row["candidate_minus_reference_total_pnl"]) > 0 for row in traded
        ),
        "worsened_window_count": sum(
            float(row["candidate_minus_reference_total_pnl"]) < 0 for row in traded
        ),
        "stopped_seed_count": sum(int(row["candidate_stopped_seed_count"]) for row in traded),
        "stopped_within_120_bars": sum(
            int(row["candidate_stopped_within_120_bars"]) for row in traded
        ),
        "zero_pair_seed_count": sum(
            int(row["candidate_zero_pair_seed_count"]) for row in traded
        ),
        "single_fill_stop_seed_count": sum(
            int(row["candidate_single_fill_stop_seed_count"]) for row in traded
        ),
        "worst_window_loss_share": (
            abs(float(ranked_losses[0]["candidate_total_pnl"])) / loss_total
            if loss_total > 0 and ranked_losses
            else 0.0
        ),
        "worst_three_window_loss_share": (
            sum(abs(float(row["candidate_total_pnl"])) for row in ranked_losses[:3])
            / loss_total
            if loss_total > 0
            else 0.0
        ),
        "worst_window_ids": [row["window_id"] for row in ranked_losses[:8]],
    }


def _compare_windows(
    reference: Sequence[dict[str, Any]],
    candidate: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    reference_by_id = {row["window_id"]: row for row in reference}
    candidate_by_id = {row["window_id"]: row for row in candidate}
    if set(reference_by_id) != set(candidate_by_id):
        raise ValueError("参考与候选的 BTC 窗口集合不一致。")
    rows = []
    for window_id in sorted(candidate_by_id, key=lambda value: (
        candidate_by_id[value]["market_close"],
        value,
    )):
        ref = reference_by_id[window_id]
        cand = candidate_by_id[window_id]
        rows.append({
            "window_id": window_id,
            "market_close": cand["market_close"],
            "entry_features": cand["entry_features"],
            "reference_traded_seed_count": ref["traded_seed_count"],
            "candidate_traded_seed_count": cand["traded_seed_count"],
            "reference_total_pnl": ref["total_pnl"],
            "candidate_total_pnl": cand["total_pnl"],
            "candidate_minus_reference_total_pnl": (
                float(cand["total_pnl"]) - float(ref["total_pnl"])
            ),
            "reference_mean_pnl": ref["mean_pnl"],
            "candidate_mean_pnl": cand["mean_pnl"],
            "candidate_negative_seed_count": cand["negative_seed_count"],
            "candidate_all_traded_seeds_negative": cand[
                "all_traded_seeds_negative"
            ],
            "reference_mean_paired_grid_pnl": ref["mean_paired_grid_pnl"],
            "candidate_mean_paired_grid_pnl": cand["mean_paired_grid_pnl"],
            "reference_mean_stop_exit_pnl": ref["mean_stop_exit_pnl"],
            "candidate_mean_stop_exit_pnl": cand["mean_stop_exit_pnl"],
            "reference_mean_fill_count": ref["mean_fill_count"],
            "candidate_mean_fill_count": cand["mean_fill_count"],
            "reference_mean_pair_count": ref["mean_pair_count"],
            "candidate_mean_pair_count": cand["mean_pair_count"],
            "candidate_stopped_seed_count": cand["stopped_seed_count"],
            "candidate_stopped_within_120_bars": cand["stopped_within_120_bars"],
            "candidate_zero_pair_seed_count": cand["zero_pair_seed_count"],
            "candidate_single_fill_stop_seed_count": cand[
                "single_fill_stop_seed_count"
            ],
            "candidate_median_stopped_at_index": cand[
                "median_stopped_at_index"
            ],
            "candidate_mean_fees_paid": cand["mean_fees_paid"],
            "candidate_mean_exit_slippage_cost": cand[
                "mean_exit_slippage_cost"
            ],
        })
    return {"summary": _comparison_summary(rows), "windows": rows}


def _persistent_loss_windows(scenarios: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    base = {
        row["window_id"]: row
        for row in scenarios["BASE"]["comparison"]["windows"]
    }
    cost = {
        row["window_id"]: row
        for row in scenarios["COST50"]["comparison"]["windows"]
    }
    rows = []
    for window_id in sorted(set(base) & set(cost)):
        base_row = base[window_id]
        cost_row = cost[window_id]
        if (
            float(base_row["candidate_mean_pnl"]) >= 0
            or float(cost_row["candidate_mean_pnl"]) >= 0
        ):
            continue
        rows.append({
            "window_id": window_id,
            "market_close": cost_row["market_close"],
            "base_mean_pnl": base_row["candidate_mean_pnl"],
            "cost50_mean_pnl": cost_row["candidate_mean_pnl"],
            "base_negative_seed_count": base_row["candidate_negative_seed_count"],
            "cost50_negative_seed_count": cost_row["candidate_negative_seed_count"],
            "all_seeds_negative_both_scenarios": (
                bool(base_row["candidate_all_traded_seeds_negative"])
                and bool(cost_row["candidate_all_traded_seeds_negative"])
            ),
            "cost50_stopped_within_120_bars": cost_row[
                "candidate_stopped_within_120_bars"
            ],
            "cost50_zero_pair_seed_count": cost_row[
                "candidate_zero_pair_seed_count"
            ],
            "entry_features": cost_row["entry_features"],
        })
    return sorted(rows, key=lambda row: (float(row["cost50_mean_pnl"]), row["window_id"]))


def _validate_round13_result(payload: Mapping[str, Any]) -> None:
    if payload.get("eligible_candidate_ids"):
        raise ValueError("Round 13 已存在合格候选，不应执行失败诊断。")
    if payload.get("phase_b_authorized") is not False:
        raise ValueError("Round 13 Phase B 状态不是明确的 False。")
    if payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Final OOS 已不再封存，拒绝执行诊断。")
    if payload.get("final_oos_authorized") is not False:
        raise ValueError("Round 13 Final OOS 授权状态异常。")
    if payload.get("production_defaults_changed") is not False:
        raise ValueError("Round 13 已修改生产默认值，拒绝按失败状态诊断。")
    if payload.get("stable_profit_claimed") is not False:
        raise ValueError("Round 13 稳定收益声明状态异常。")
    if tuple(int(seed) for seed in payload.get("seeds") or ()) != DEFAULT_SEEDS:
        raise ValueError("Round 13 种子集合与冻结协议不一致。")


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 13 BTC 全种子窗口级失败诊断",
        "",
        "本报告只重放 2020H1 外部诊断区间；不读取当前 Development、Validation 或 Final OOS。",
        "",
        f"- Round 13 结果哈希：`{payload['round13_result_sha256']}`",
        f"- 固定种子：{', '.join(str(seed) for seed in payload['seeds'])}",
        f"- Final OOS：`{payload['final_oos_status']}`",
        "",
    ]
    for scenario_name in ("BASE", "COST50"):
        scenario = payload["scenarios"][scenario_name]
        summary = scenario["comparison"]["summary"]
        lines.extend([
            f"## {scenario_name}",
            "",
            f"- 候选 BTC 六种子合计 PnL：{summary['candidate_total_pnl']:.4f} USDT",
            f"- 参考 BTC 六种子合计 PnL：{summary['reference_total_pnl']:.4f} USDT",
            f"- 负收益窗口：{summary['negative_window_count']} / {summary['traded_window_count']}",
            f"- 六种子全负窗口：{summary['all_seed_negative_window_count']}",
            f"- 120 bars 内止损种子次数：{summary['stopped_within_120_bars']}",
            f"- 最差三个窗口占全部窗口损失：{summary['worst_three_window_loss_share']:.2%}",
            "",
            "| BTC 窗口 | 候选均值 | 参考均值 | 候选-参考 | 负种子 | 120 bars 内止损 | 平均成交 | 平均配对 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        ranked = sorted(
            scenario["comparison"]["windows"],
            key=lambda row: (float(row["candidate_mean_pnl"]), row["window_id"]),
        )
        for row in ranked[:8]:
            if int(row["candidate_traded_seed_count"]) == 0:
                continue
            lines.append(
                "| `{window}` | {candidate:.4f} | {reference:.4f} | {delta:.4f} | "
                "{negative} | {early} | {fills:.2f} | {pairs:.2f} |".format(
                    window=row["window_id"],
                    candidate=float(row["candidate_mean_pnl"]),
                    reference=float(row["reference_mean_pnl"]),
                    delta=float(row["candidate_minus_reference_total_pnl"])
                    / len(payload["seeds"]),
                    negative=int(row["candidate_negative_seed_count"]),
                    early=int(row["candidate_stopped_within_120_bars"]),
                    fills=float(row["candidate_mean_fill_count"]),
                    pairs=float(row["candidate_mean_pair_count"]),
                )
            )
        lines.append("")

    persistent = payload["persistent_loss_windows"]
    lines.extend([
        "## 跨成本场景持续亏损窗口",
        "",
        f"BASE 与 COST50 均为负的 BTC 窗口：{len(persistent)}。",
        "",
    ])
    for row in persistent[:10]:
        lines.append(
            f"- `{row['window_id']}`：BASE {row['base_mean_pnl']:.4f}，"
            f"COST50 {row['cost50_mean_pnl']:.4f}，"
            f"COST50 负种子 {row['cost50_negative_seed_count']}/6。"
        )
    lines.extend([
        "",
        "## 约束与结论",
        "",
        "- 本诊断不选择或预注册新参数；",
        "- Final OOS 保持 `SEALED_NOT_EVALUATED`；",
        "- `direction_mode` 保持 `NEUTRAL`；",
        "- 生产默认值未修改；",
        f"- 结论：{payload['conclusion']}",
        "",
    ])
    return "\n".join(lines)


def _write_csv(path: Path, scenarios: Mapping[str, dict[str, Any]]) -> None:
    rows = []
    for scenario_name in ("BASE", "COST50"):
        for row in scenarios[scenario_name]["comparison"]["windows"]:
            features = row.get("entry_features") or {}
            rows.append({
                "scenario": scenario_name,
                "window_id": row["window_id"],
                "market_close": row["market_close"],
                "candidate_traded_seed_count": row["candidate_traded_seed_count"],
                "candidate_mean_pnl": row["candidate_mean_pnl"],
                "reference_mean_pnl": row["reference_mean_pnl"],
                "candidate_minus_reference_total_pnl": row[
                    "candidate_minus_reference_total_pnl"
                ],
                "candidate_negative_seed_count": row["candidate_negative_seed_count"],
                "candidate_all_traded_seeds_negative": row[
                    "candidate_all_traded_seeds_negative"
                ],
                "candidate_stopped_seed_count": row["candidate_stopped_seed_count"],
                "candidate_stopped_within_120_bars": row[
                    "candidate_stopped_within_120_bars"
                ],
                "candidate_zero_pair_seed_count": row[
                    "candidate_zero_pair_seed_count"
                ],
                "candidate_single_fill_stop_seed_count": row[
                    "candidate_single_fill_stop_seed_count"
                ],
                "candidate_mean_fill_count": row["candidate_mean_fill_count"],
                "candidate_mean_pair_count": row["candidate_mean_pair_count"],
                "candidate_mean_paired_grid_pnl": row[
                    "candidate_mean_paired_grid_pnl"
                ],
                "candidate_mean_stop_exit_pnl": row[
                    "candidate_mean_stop_exit_pnl"
                ],
                "directional_efficiency": features.get("directional_efficiency"),
                "volatility_expansion": features.get("volatility_expansion"),
                "reversal_ratio": features.get("reversal_ratio"),
                "grid_score": features.get("grid_score"),
            })
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="重放 Round 13 六种子并聚合每个 BTC 窗口的失败结构。"
    )
    parser.add_argument("manifests", nargs="*")
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
    if len(args.manifests) not in {0, 2}:
        raise ValueError("必须不传 manifest，或同时传入 BTC/ETH 两个 manifest。")

    result_path = Path(args.round13_result).resolve()
    actual_hash = _sha256(result_path)
    if actual_hash != ROUND13_RESULT_SHA256:
        raise ValueError(
            "Round 13 冻结结果已变化: "
            f"expected={ROUND13_RESULT_SHA256} actual={actual_hash}"
        )
    round13_payload = json.loads(result_path.read_text(encoding="utf-8"))
    _validate_round13_result(round13_payload)

    manifests = tuple(args.manifests) or tuple(
        str(item["manifest"]) for item in round13_payload["datasets"]
    )
    base_config = profit_opt._base_research_config()
    metadata, windows = profit_opt._load_data(manifests, base_config)
    datasets = _dataset_brief(manifests, metadata)
    if datasets != round13_payload["datasets"]:
        raise ValueError("当前冻结数据与 Round 13 结果不一致。")
    external_ids = round13._paired_ready_window_ids(windows)
    if list(external_ids) != round13_payload["external_evidence"]["window_ids"]:
        raise ValueError("当前外部窗口与 Round 13 结果不一致。")

    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "round13-diagnostics.json"
    markdown_output = output_dir / "round13-diagnostics.md"
    csv_output = output_dir / "round13-btc-window-diagnostics.csv"
    for output in (json_output, markdown_output, csv_output):
        if output.exists():
            raise FileExistsError(f"Round 13 诊断已存在，拒绝覆盖: {output}")

    raw_runs: dict[
        tuple[str, str],
        dict[int, dict[str, tuple[Any, list[WindowResult]]]],
    ] = {(variant, scenario): {} for variant in VARIANTS for scenario in SCENARIOS}
    observations: dict[str, list[dict[str, Any]]] = {
        variant: [] for variant in VARIANTS
    }
    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(DEFAULT_SEEDS)),
        initializer=profit_opt._initialize_worker,
        initargs=(manifests, base_config),
    ) as executor:
        for variant in VARIANTS:
            for scenario, cost in SCENARIOS.items():
                print(f"DIAGNOSING {variant} {scenario}", flush=True)
                for seed in DEFAULT_SEEDS:
                    future = executor.submit(
                        round13._external_seed_worker,
                        variant,
                        seed,
                        external_ids,
                        cost,
                    )
                    futures[future] = (variant, scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            expected_variant, scenario, expected_seed = futures[future]
            variant, seed, runs, observation = future.result()
            if variant != expected_variant or seed != expected_seed:
                raise RuntimeError("Round 13 诊断 worker 返回了错误任务标识。")
            raw_runs[(variant, scenario)][seed] = runs
            observations[variant].append(observation)

    execution_integrity = {}
    for variant, items in observations.items():
        if not items or any(item != items[0] for item in items[1:]):
            raise RuntimeError(f"Round 13 诊断 worker 执行参数不一致: {variant}")
        execution_integrity[variant] = items[0]
    if execution_integrity != round13_payload["execution_integrity"]:
        raise RuntimeError("Round 13 诊断重放的执行完整性与冻结结果不一致。")

    locked_parameters, _symbol_policies, _maker_policy = round13._locked_policy()
    research = RobustnessResearch(
        windows,
        locked_parameters,
        base_config,
        dataset_metadata=metadata,
    )
    contexts = _populate_entry_decisions(research, external_ids)
    baseline_candidate = _registered_candidates()[0]
    aggregated = {}
    for key, seed_runs in raw_runs.items():
        variant, _scenario = key
        raw_evidence = CandidateEvidence(
            baseline_candidate,
            {seed: seed_runs[seed] for seed in sorted(seed_runs)},
        )
        filtered = _filtered_evidence_for_symbols(
            raw_evidence,
            round13.FIXED_FILTERS,
            contexts,
            candidate_id=variant,
            round_name="round13_failure_diagnostic",
            split_name="external",
        )
        aggregated[key] = _aggregate_windows(
            {
                seed: filtered.runs[seed]["external"][1]
                for seed in sorted(filtered.runs)
            },
            symbol="BTCUSDT",
            contexts=contexts,
        )
        if {row["window_id"] for row in aggregated[key]} != set(external_ids):
            raise RuntimeError(
                f"Round 13 诊断未覆盖全部 BTC 外部窗口: {key}"
            )

    scenarios = {}
    for scenario_name, cost in SCENARIOS.items():
        reference = aggregated[(round13.REFERENCE_VARIANT_ID, scenario_name)]
        candidate = aggregated[(round13.CANDIDATE_ID, scenario_name)]
        scenarios[scenario_name] = {
            "cost": {
                "maker_fee_rate": cost[0],
                "taker_fee_rate": cost[1],
                "stop_loss_slippage_bps": cost[2],
            },
            "reference_variant": round13.REFERENCE_VARIANT_ID,
            "candidate_variant": round13.CANDIDATE_ID,
            "reference_windows": reference,
            "candidate_windows": candidate,
            "comparison": _compare_windows(reference, candidate),
        }

    persistent = _persistent_loss_windows(scenarios)
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "diagnostic_role": "POST_FAILURE_EXTERNAL_DIAGNOSTIC_ONLY",
        "round13_result": str(result_path),
        "round13_result_sha256": ROUND13_RESULT_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "datasets": datasets,
        "seeds": list(DEFAULT_SEEDS),
        "direction_mode": "NEUTRAL",
        "execution_integrity": execution_integrity,
        "scenarios": scenarios,
        "persistent_loss_windows": persistent,
        "candidate_preregistered": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": (
            "Round 13 的 BTC 亏损已按全部六个种子复现并聚合；"
            "该诊断只用于判断下一步结构性假设，不构成候选通过证据。"
        ),
    }
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    _write_csv(csv_output, scenarios)
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
