from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Any, Sequence

from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
    _symbol_trade_coverage,
)
from scripts.cross_era_joint_entry_validate import _validation_checks
from scripts.cross_era_oos import (
    _dataset_brief,
    _evidence_payload,
    _load_research_state,
    _market_states,
    _protocol_sha256,
    _registered_candidates,
    _write_json,
)
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    _evaluate_candidate,
    _initialize_worker,
    _locked_policy,
)
from scripts.robustness import EntryFilter, RobustnessResearch


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-extended-development-round4-20260723"
SOURCE_RESULT_SHA256 = {
    "entry-development-results.json": (
        "8a555feab5a54507f8edd96dc6590d6a6a28caa7f72f73b7a9c2ac7a31c5a7f8"
    ),
    "joint-entry-development-results.json": (
        "797b7ecd775e874351bad4db9ca0d03f3065eabaa77db7b24cf466e06f96e5f4"
    ),
    "joint-entry-validation-results.json": (
        "56a171d59d07520b4a295fbe13721acf4f6ab348dca6d81955cc3c9bdac7c65b"
    ),
}


def _registered_btc_filters() -> tuple[EntryFilter, ...]:
    return (
        EntryFilter(0.40, 0.95, 0.35),
        EntryFilter(0.40, 1.05, 0.35),
        EntryFilter(0.55, 0.95, 0.35),
        EntryFilter(0.55, 1.05, 0.35),
    )


def _registered_eth_filters() -> tuple[EntryFilter, ...]:
    return (
        EntryFilter(0.35, 1.00, 0.55),
        EntryFilter(0.35, 1.05, 0.55),
        EntryFilter(0.45, 1.00, 0.55),
    )


def _candidate_pairs() -> tuple[tuple[str, EntryFilter, EntryFilter], ...]:
    return tuple(
        (
            f"BTC_{btc_filter.filter_id}_ETH_{eth_filter.filter_id}",
            btc_filter,
            eth_filter,
        )
        for btc_filter, eth_filter in product(
            _registered_btc_filters(),
            _registered_eth_filters(),
        )
    )


def _cell_specs(split: Any) -> tuple[tuple[str, str, Sequence[str], tuple[float, float, float]], ...]:
    return (
        ("DEV_BASE", "development", split.development, BASE_COST),
        ("DEV_COST50", "development", split.development, COST_50),
        ("VAL_BASE", "validation", split.validation, BASE_COST),
        ("VAL_COST50", "validation", split.validation, COST_50),
    )


def _cell_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    seed_count: int,
    btc_coverage: float,
    eth_coverage: float,
) -> dict[str, bool]:
    return _validation_checks(
        baseline,
        candidate,
        seed_count=seed_count,
        btc_coverage=btc_coverage,
        eth_coverage=eth_coverage,
    )


def _selection_metrics(cell_summaries: dict[str, dict[str, Any]]) -> dict[str, float]:
    summaries = list(cell_summaries.values())
    return {
        "minimum_worst_seed_total_pnl": min(
            float(item["worst_seed_total_pnl"]) for item in summaries
        ),
        "minimum_symbol_pnl": min(
            float(item["symbol_pnl"].get(symbol, 0.0))
            for item in summaries
            for symbol in ("BTCUSDT", "ETHUSDT")
        ),
        "minimum_mean_seed_total_pnl": min(
            float(item["mean_seed_total_pnl"]) for item in summaries
        ),
        "minimum_seed_profit_factor": min(
            float(item["minimum_seed_profit_factor"]) for item in summaries
        ),
        "maximum_best_window_concentration": max(
            float(item["worst_best_window_concentration"]) for item in summaries
        ),
        "maximum_drawdown_pct": max(
            float(item["max_drawdown_pct"]) for item in summaries
        ),
    }


def _selection_key(
    candidate_id: str,
    metrics: dict[str, float],
) -> tuple[float, float, float, float, float, float, str]:
    return (
        -metrics["minimum_worst_seed_total_pnl"],
        -metrics["minimum_symbol_pnl"],
        -metrics["minimum_mean_seed_total_pnl"],
        -metrics["minimum_seed_profit_factor"],
        metrics["maximum_best_window_concentration"],
        metrics["maximum_drawdown_pct"],
        candidate_id,
    )


def _select_candidate(candidates: dict[str, dict[str, Any]]) -> str | None:
    eligible = [
        candidate_id
        for candidate_id, payload in candidates.items()
        if payload["all_cells_passed"]
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate_id: _selection_key(
            candidate_id,
            candidates[candidate_id]["selection_metrics"],
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_source_results(report_dir: Path) -> dict[str, dict[str, Any]]:
    results = {}
    for filename, expected_hash in SOURCE_RESULT_SHA256.items():
        path = report_dir / filename
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"冻结输入结果已变化: {filename} expected={expected_hash} actual={actual_hash}"
            )
        results[filename] = json.loads(path.read_text(encoding="utf-8"))
    return results


def _verify_source_contract(
    sources: dict[str, dict[str, Any]],
    *,
    datasets: list[dict[str, Any]],
    seeds: Sequence[int],
) -> None:
    entry_development = sources["entry-development-results.json"]
    joint_development = sources["joint-entry-development-results.json"]
    validation = sources["joint-entry-validation-results.json"]
    for filename, payload in sources.items():
        if payload.get("datasets") != datasets:
            raise ValueError(f"冻结数据与 {filename} 不一致。")
        if payload.get("seeds") != list(seeds):
            raise ValueError(f"固定种子与 {filename} 不一致。")
        if payload.get("production_defaults_changed") is not False:
            raise ValueError(f"{filename} 不满足生产参数未修改约束。")
    if validation.get("stage_passed") is not False:
        raise ValueError("Round 4 仅适用于已失败并消费的 Validation。")
    if validation.get("final_oos_authorized") is not False:
        raise ValueError("原 Validation 不得授权 Final OOS。")
    if validation.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Final OOS 已不再封存，拒绝执行 Round 4。")

    for entry_filter in _registered_btc_filters():
        filter_id = entry_filter.filter_id
        checks = joint_development["checks"].get(filter_id)
        if not checks or not all(checks.values()):
            raise ValueError(f"BTC 冻结候选不再满足 Development 来源约束: {filter_id}")
    for entry_filter in _registered_eth_filters():
        filter_id = entry_filter.filter_id
        payload = entry_development["candidates"].get(filter_id)
        coverage = entry_development["eth_trade_coverage"].get(filter_id)
        if payload is None or coverage is None:
            raise ValueError(f"ETH 冻结候选缺少 Development 来源证据: {filter_id}")
        summary = payload["summary"]
        if not (
            int(summary["positive_seed_count"]) == len(seeds)
            and float(summary["worst_seed_total_pnl"]) > 0
            and all(
                float(summary["symbol_pnl"].get(symbol, 0.0)) > 0
                for symbol in ("BTCUSDT", "ETHUSDT")
            )
            and float(summary["minimum_seed_profit_factor"]) > 1.0
            and float(coverage) >= 0.25
        ):
            raise ValueError(f"ETH 冻结候选不再满足 Development 来源约束: {filter_id}")


def _cost_payload(cost: tuple[float, float, float]) -> dict[str, float]:
    return {
        "maker_fee_rate": cost[0],
        "taker_fee_rate": cost[1],
        "stop_loss_slippage_bps": cost[2],
    }


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Round 4 扩展开发集最终候选筛选",
        "",
        f"- 固定候选数：{len(payload['candidates'])}",
        f"- 四单元全部通过数：{len(payload['eligible_candidate_ids'])}",
        f"- 选中候选：`{payload['selected_candidate_id'] or 'NONE'}`",
        "- Validation 角色：`CONSUMED_AS_EXTENDED_DEVELOPMENT`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "| 候选 | DEV BASE | DEV COST50 | VAL BASE | VAL COST50 | 最弱种子 | 最弱标的 | 最低 PF | 最大回撤 | 全过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for candidate_id, item in sorted(payload["candidates"].items()):
        metrics = item["selection_metrics"]
        cell_passes = {
            name: sum(cell["checks"].values())
            for name, cell in item["cells"].items()
        }
        cell_totals = {
            name: len(cell["checks"])
            for name, cell in item["cells"].items()
        }
        lines.append(
            "| {candidate_id} | {dev_base}/{dev_base_total} | {dev_cost}/{dev_cost_total} | "
            "{val_base}/{val_base_total} | {val_cost}/{val_cost_total} | {worst:.4f} | "
            "{symbol:.4f} | {pf:.3f} | {dd:.2%} | {passed} |".format(
                candidate_id=candidate_id,
                dev_base=cell_passes["DEV_BASE"],
                dev_base_total=cell_totals["DEV_BASE"],
                dev_cost=cell_passes["DEV_COST50"],
                dev_cost_total=cell_totals["DEV_COST50"],
                val_base=cell_passes["VAL_BASE"],
                val_base_total=cell_totals["VAL_BASE"],
                val_cost=cell_passes["VAL_COST50"],
                val_cost_total=cell_totals["VAL_COST50"],
                worst=metrics["minimum_worst_seed_total_pnl"],
                symbol=metrics["minimum_symbol_pnl"],
                pf=metrics["minimum_seed_profit_factor"],
                dd=metrics["maximum_drawdown_pct"],
                passed="PASS" if item["all_cells_passed"] else "FAIL",
            )
        )
    lines.extend(["", f"结论：{payload['conclusion']}", ""])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在冻结的扩展开发集上评估 12 个最终入口候选，不读取 Final OOS。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round4-final-candidate-protocol.md",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    if not seeds:
        raise ValueError("至少需要一个固定种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "round4-extended-development-results.json"
    if output.exists():
        raise FileExistsError(f"Round 4 结果已存在，拒绝覆盖: {output}")

    sources = _load_source_results(report_dir)
    base_config, metadata, windows, split = _load_research_state(args.manifests)
    datasets = _dataset_brief(args.manifests, metadata)
    _verify_source_contract(sources, datasets=datasets, seeds=seeds)
    joint_development = sources["joint-entry-development-results.json"]
    if (
        len(split.development) != int(joint_development["split"]["development_count"])
        or len(split.validation) != int(joint_development["split"]["validation_count"])
        or len(split.final_oos) != int(joint_development["split"]["final_oos_count"])
    ):
        raise ValueError("当前时间切分与冻结 Development 结果不一致。")

    market_states = {
        "development": _market_states(windows, split.development),
        "validation": _market_states(windows, split.validation),
    }
    locked_parameters, symbol_policies, maker_policy = _locked_policy()
    research = RobustnessResearch(
        windows,
        locked_parameters,
        base_config,
        dataset_metadata=metadata,
    )
    contexts = {
        "development": _populate_entry_decisions(research, split.development),
        "validation": _populate_entry_decisions(research, split.validation),
    }
    baseline_candidate = _registered_candidates()[0]
    baseline_evidence = {}
    baseline_payloads = {}
    cells = _cell_specs(split)
    cost_evidence = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=_initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    ) as executor:
        for scenario, cost in (("BASE", BASE_COST), ("COST50", COST_50)):
            print(f"EVALUATING development+validation {scenario}", flush=True)
            cost_evidence[scenario] = _evaluate_candidate(
                baseline_candidate,
                windows=windows,
                metadata=metadata,
                base_config=base_config,
                parameters=locked_parameters,
                symbol_policies=symbol_policies,
                maker_policy=maker_policy,
                split_ids={
                    "development": split.development,
                    "validation": split.validation,
                },
                seeds=seeds,
                cost=cost,
                executor=executor,
            )

    for cell_name, split_name, window_ids, cost in cells:
        scenario = "BASE" if cost == BASE_COST else "COST50"
        evidence = cost_evidence[scenario]
        baseline_evidence[cell_name] = evidence
        baseline_payloads[cell_name] = {
            "split": split_name,
            "window_count": len(window_ids),
            "cost": _cost_payload(cost),
            "evidence": _evidence_payload(
                evidence,
                split_name,
                market_states[split_name],
            ),
        }

    candidate_payloads = {}
    for candidate_id, btc_filter, eth_filter in _candidate_pairs():
        cell_payloads = {}
        cell_summaries = {}
        for cell_name, split_name, _window_ids, _cost in cells:
            evidence = _filtered_evidence_for_symbols(
                baseline_evidence[cell_name],
                {"BTCUSDT": btc_filter, "ETHUSDT": eth_filter},
                contexts[split_name],
                candidate_id=candidate_id,
                round_name="round4_extended_development",
                split_name=split_name,
            )
            candidate_evidence = _evidence_payload(
                evidence,
                split_name,
                market_states[split_name],
            )
            btc_coverage = _symbol_trade_coverage(
                baseline_evidence[cell_name],
                evidence,
                "BTCUSDT",
                split_name,
            )
            eth_coverage = _symbol_trade_coverage(
                baseline_evidence[cell_name],
                evidence,
                "ETHUSDT",
                split_name,
            )
            checks = _cell_checks(
                baseline_payloads[cell_name]["evidence"]["summary"],
                candidate_evidence["summary"],
                seed_count=len(seeds),
                btc_coverage=btc_coverage,
                eth_coverage=eth_coverage,
            )
            cell_payloads[cell_name] = {
                "candidate": candidate_evidence,
                "trade_coverage": {
                    "BTCUSDT": btc_coverage,
                    "ETHUSDT": eth_coverage,
                },
                "checks": checks,
                "passed": all(checks.values()),
            }
            cell_summaries[cell_name] = candidate_evidence["summary"]
        candidate_payloads[candidate_id] = {
            "btc_entry_filter": asdict(btc_filter) | {"filter_id": btc_filter.filter_id},
            "eth_entry_filter": asdict(eth_filter) | {"filter_id": eth_filter.filter_id},
            "cells": cell_payloads,
            "all_cells_passed": all(item["passed"] for item in cell_payloads.values()),
            "selection_metrics": _selection_metrics(cell_summaries),
        }

    selected = _select_candidate(candidate_payloads)
    eligible = sorted(
        candidate_id
        for candidate_id, payload in candidate_payloads.items()
        if payload["all_cells_passed"]
    )
    protocol_path = Path(args.protocol).resolve()
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": _protocol_sha256(protocol_path),
        "source_result_sha256": SOURCE_RESULT_SHA256,
        "datasets": datasets,
        "seeds": list(seeds),
        "split": {
            "development_count": len(split.development),
            "validation_count": len(split.validation),
            "final_oos_count": len(split.final_oos),
            "validation_role": "CONSUMED_AS_EXTENDED_DEVELOPMENT",
            "final_oos_status": "SEALED_NOT_EVALUATED",
        },
        "registered_btc_filters": [
            asdict(item) | {"filter_id": item.filter_id}
            for item in _registered_btc_filters()
        ],
        "registered_eth_filters": [
            asdict(item) | {"filter_id": item.filter_id}
            for item in _registered_eth_filters()
        ],
        "baselines": baseline_payloads,
        "candidates": candidate_payloads,
        "eligible_candidate_ids": eligible,
        "selected_candidate_id": selected,
        "final_oos_authorized": selected is not None,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "conclusion": (
            "扩展开发集选择出唯一最终候选；只允许先预注册 Final OOS 协议。"
            if selected
            else "NO_ROBUST_CANDIDATE：没有候选通过四个独立单元，Final OOS 保持封存。"
        ),
    }
    _write_json(output, result)
    (report_dir / "round4-extended-development-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"ELIGIBLE {len(eligible)}")
    print(f"SELECTED {selected or 'NONE'}")


if __name__ == "__main__":
    main()
