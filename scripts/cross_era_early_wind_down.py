from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
    _symbol_trade_coverage,
)
from scripts.cross_era_extended_development import (
    _cell_checks,
    _select_candidate,
    _selection_metrics,
)
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
    CandidateEvidence,
    _locked_policy,
)
from scripts.robustness import EntryFilter, RobustnessResearch, WindowResult


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-early-wind-down-round5-20260723"
ROUND4_RESULT_SHA256 = (
    "003ef0c486edbbd0bda27b06301ec66de95691924a59559e927cb36a09eb9045"
)
ROUND4_DIAGNOSTIC_SHA256 = (
    "f10bed52850af45c0abeb10a95c1ffe86d97c3dcef40e53793d3a47506c6698d"
)
REFERENCE_WIND_DOWN_BARS = 1440
FIXED_FILTERS = {
    "BTCUSDT": EntryFilter(0.40, 1.05, 0.35),
    "ETHUSDT": EntryFilter(0.35, 1.05, 0.55),
}
ROUND4_REFERENCE_CANDIDATE_ID = (
    "BTC_de0.40_ve1.05_rr0.35_ETH_de0.35_ve1.05_rr0.55"
)


def _registered_wind_downs() -> tuple[int, ...]:
    return (2160, 2880)


def _cell_specs(
    split: Any,
) -> tuple[tuple[str, str, Sequence[str], tuple[float, float, float]], ...]:
    return (
        ("DEV_BASE", "development", split.development, BASE_COST),
        ("DEV_COST50", "development", split.development, COST_50),
        ("VAL_BASE", "validation", split.validation, BASE_COST),
        ("VAL_COST50", "validation", split.validation, COST_50),
    )


def _mechanism_summary(results: Sequence[WindowResult]) -> dict[str, float | int]:
    traded = [item for item in results if item.status == "TRADED"]
    return {
        "traded_result_count": len(traded),
        "paired_grid_pnl": sum(item.paired_grid_pnl for item in traded),
        "stop_exit_pnl": sum(item.stop_exit_pnl for item in traded),
        "stop_exit_cost": sum(item.stop_exit_cost for item in traded),
        "fees_paid": sum(item.fees_paid for item in traded),
        "exit_slippage_cost": sum(item.exit_slippage_cost for item in traded),
        "fill_count": sum(item.fill_count for item in traded),
        "pair_count": sum(item.pair_count for item in traded),
    }


def _mechanism_checks(
    reference: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, bool]:
    reference_stop = float(reference["stop_exit_pnl"])
    candidate_stop = float(candidate["stop_exit_pnl"])
    if reference_stop < 0:
        stop_improvement = (candidate_stop - reference_stop) / abs(reference_stop)
    else:
        stop_improvement = 1.0 if candidate_stop >= reference_stop else -1.0
    reference_paired = float(reference["paired_grid_pnl"])
    candidate_paired = float(candidate["paired_grid_pnl"])
    paired_retention = (
        candidate_paired / reference_paired
        if reference_paired > 0
        else 1.0 if candidate_paired >= 0 else -1.0
    )
    return {
        "stop_exit_loss_improvement_ge_20pct": stop_improvement >= 0.20,
        "paired_grid_pnl_retention_ge_60pct": paired_retention >= 0.60,
    }


def _mechanism_metrics(
    reference: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, float]:
    reference_stop = float(reference["stop_exit_pnl"])
    candidate_stop = float(candidate["stop_exit_pnl"])
    reference_paired = float(reference["paired_grid_pnl"])
    candidate_paired = float(candidate["paired_grid_pnl"])
    return {
        "stop_exit_loss_improvement": (
            (candidate_stop - reference_stop) / abs(reference_stop)
            if reference_stop < 0
            else 1.0 if candidate_stop >= reference_stop else -1.0
        ),
        "paired_grid_pnl_retention": (
            candidate_paired / reference_paired
            if reference_paired > 0
            else 1.0 if candidate_paired >= 0 else -1.0
        ),
    }


def _wind_down_seed_worker(
    wind_down_bars: int,
    seed: int,
    split_ids: dict[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[int, int, dict[str, tuple[Any, list[WindowResult]]]]:
    state = profit_opt._WORKER_STATE
    maker_fee, taker_fee, slippage_bps = cost
    config = replace(state["base_config"], wind_down_bars=wind_down_bars)
    research = RobustnessResearch(
        state["windows"],
        state["parameters"],
        config,
        dataset_metadata=state["metadata"],
    )
    runs = {}
    for split_name, window_ids in split_ids.items():
        runs[split_name] = research.evaluate_joint_policy_windows(
            state["symbol_policies"],
            state["maker_policy"],
            window_ids,
            maker_fee_rate=maker_fee,
            taker_fee_rate=taker_fee,
            stop_slippage_bps=slippage_bps,
            fill_seed_salt=seed,
        )
    return wind_down_bars, seed, runs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scenario_name(cost: tuple[float, float, float]) -> str:
    return "BASE" if cost == BASE_COST else "COST50"


def _flatten_results(
    evidence: CandidateEvidence,
    split_name: str,
) -> list[WindowResult]:
    return [
        item
        for seed_runs in evidence.runs.values()
        for item in seed_runs[split_name][1]
    ]


def _assert_close(name: str, actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        if set(actual) != set(expected):
            raise ValueError(f"W1440 重算字段不一致: {name}")
        for key in expected:
            _assert_close(f"{name}.{key}", actual[key], expected[key])
        return
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not math.isclose(
            float(actual),
            float(expected),
            rel_tol=1e-10,
            abs_tol=1e-10,
        ):
            raise ValueError(
                f"W1440 重算不一致: {name} actual={actual} expected={expected}"
            )
        return
    if actual != expected:
        raise ValueError(f"W1440 重算不一致: {name}")


def _verify_reference_summary(
    actual: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    for key in (
        "mean_seed_total_pnl",
        "worst_seed_total_pnl",
        "positive_seed_count",
        "minimum_seed_profit_factor",
        "max_drawdown_pct",
        "worst_best_window_concentration",
        "symbol_pnl",
    ):
        _assert_close(key, actual[key], expected[key])


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Round 5 提前 Wind-down 扩展开发筛选",
        "",
        f"- 固定入口：BTC `{payload['fixed_filters']['BTCUSDT']['filter_id']}`；"
        f"ETH `{payload['fixed_filters']['ETHUSDT']['filter_id']}`",
        f"- 候选数：{len(payload['candidates'])}",
        f"- 合格候选数：{len(payload['eligible_candidate_ids'])}",
        f"- 选中候选：`{payload['selected_candidate_id'] or 'NONE'}`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "| 候选 | DEV BASE | DEV COST50 | VAL BASE | VAL COST50 | 退出损失改善 | 配对收益保留 | 最弱种子 | 最大集中度 | 全过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for candidate_id, item in sorted(payload["candidates"].items()):
        counts = {
            name: (sum(cell["checks"].values()), len(cell["checks"]))
            for name, cell in item["cells"].items()
        }
        mechanism = item["mechanism_metrics"]
        metrics = item["selection_metrics"]
        lines.append(
            "| {candidate_id} | {db[0]}/{db[1]} | {dc[0]}/{dc[1]} | "
            "{vb[0]}/{vb[1]} | {vc[0]}/{vc[1]} | {stop:.2%} | {paired:.2%} | "
            "{worst:.4f} | {concentration:.2%} | {passed} |".format(
                candidate_id=candidate_id,
                db=counts["DEV_BASE"],
                dc=counts["DEV_COST50"],
                vb=counts["VAL_BASE"],
                vc=counts["VAL_COST50"],
                stop=mechanism["stop_exit_loss_improvement"],
                paired=mechanism["paired_grid_pnl_retention"],
                worst=metrics["minimum_worst_seed_total_pnl"],
                concentration=metrics["maximum_best_window_concentration"],
                passed="PASS" if item["all_checks_passed"] else "FAIL",
            )
        )
    lines.extend(["", f"结论：{payload['conclusion']}", ""])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估最后 36/48 小时提前 wind-down 的跨周期稳健性。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round5-early-wind-down-protocol.md",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    if seeds != DEFAULT_SEEDS:
        raise ValueError("Round 5 必须使用协议冻结的六个种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "round5-early-wind-down-results.json"
    if output.exists():
        raise FileExistsError(f"Round 5 结果已存在，拒绝覆盖: {output}")

    round4_path = report_dir / "round4-extended-development-results.json"
    diagnostic_path = report_dir / "round4-diagnostics.json"
    if _sha256(round4_path) != ROUND4_RESULT_SHA256:
        raise ValueError("Round 4 冻结结果已变化。")
    if _sha256(diagnostic_path) != ROUND4_DIAGNOSTIC_SHA256:
        raise ValueError("Round 4 冻结诊断已变化。")
    round4 = json.loads(round4_path.read_text(encoding="utf-8"))
    if round4.get("eligible_candidate_ids"):
        raise ValueError("Round 4 已有合格候选，不应执行 Round 5。")
    if round4.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Final OOS 已不再封存。")

    base_config, metadata, windows, split = _load_research_state(args.manifests)
    datasets = _dataset_brief(args.manifests, metadata)
    if round4.get("datasets") != datasets:
        raise ValueError("当前冻结数据与 Round 4 不一致。")
    market_states = {
        "development": _market_states(windows, split.development),
        "validation": _market_states(windows, split.validation),
    }
    split_ids = {
        "development": split.development,
        "validation": split.validation,
    }
    wind_down_values = (REFERENCE_WIND_DOWN_BARS,) + _registered_wind_downs()
    futures = {}
    raw_runs: dict[tuple[int, str], dict[int, dict[str, tuple[Any, list[WindowResult]]]]] = {
        (wind_down, scenario): {}
        for wind_down in wind_down_values
        for scenario in ("BASE", "COST50")
    }
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=profit_opt._initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    ) as executor:
        for wind_down in wind_down_values:
            for scenario, cost in (("BASE", BASE_COST), ("COST50", COST_50)):
                print(f"EVALUATING W{wind_down} {scenario}", flush=True)
                for seed in seeds:
                    future = executor.submit(
                        _wind_down_seed_worker,
                        wind_down,
                        seed,
                        split_ids,
                        cost,
                    )
                    futures[future] = (wind_down, scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            expected_wind_down, scenario, expected_seed = futures[future]
            wind_down, seed, runs = future.result()
            if wind_down != expected_wind_down or seed != expected_seed:
                raise RuntimeError("Round 5 worker 返回了错误任务标识。")
            raw_runs[(wind_down, scenario)][seed] = runs

    baseline_candidate = _registered_candidates()[0]
    evidences = {
        key: CandidateEvidence(
            baseline_candidate,
            {seed: runs[seed] for seed in sorted(runs)},
        )
        for key, runs in raw_runs.items()
    }
    locked_parameters, _symbol_policies, _maker_policy = _locked_policy()
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
    cells = _cell_specs(split)
    raw_baseline_payloads = {}
    reference_cells = {}
    reference_filtered_evidence = {}
    round4_reference = round4["candidates"][ROUND4_REFERENCE_CANDIDATE_ID]
    for cell_name, split_name, window_ids, cost in cells:
        scenario = _scenario_name(cost)
        raw_baseline = evidences[(REFERENCE_WIND_DOWN_BARS, scenario)]
        raw_payload = _evidence_payload(
            raw_baseline,
            split_name,
            market_states[split_name],
        )
        raw_baseline_payloads[cell_name] = {
            "split": split_name,
            "window_count": len(window_ids),
            "evidence": raw_payload,
        }
        filtered = _filtered_evidence_for_symbols(
            raw_baseline,
            FIXED_FILTERS,
            contexts[split_name],
            candidate_id=f"W{REFERENCE_WIND_DOWN_BARS}_REFERENCE",
            round_name="round5_reference",
            split_name=split_name,
        )
        filtered_payload = _evidence_payload(
            filtered,
            split_name,
            market_states[split_name],
        )
        _verify_reference_summary(
            filtered_payload["summary"],
            round4_reference["cells"][cell_name]["candidate"]["summary"],
        )
        reference_filtered_evidence[cell_name] = filtered
        reference_cells[cell_name] = {
            "candidate": filtered_payload,
            "trade_coverage": {
                "BTCUSDT": _symbol_trade_coverage(
                    raw_baseline,
                    filtered,
                    "BTCUSDT",
                    split_name,
                ),
                "ETHUSDT": _symbol_trade_coverage(
                    raw_baseline,
                    filtered,
                    "ETHUSDT",
                    split_name,
                ),
            },
        }

    reference_mechanism = _mechanism_summary(
        _flatten_results(reference_filtered_evidence["DEV_COST50"], "development")
    )
    candidate_payloads = {}
    for wind_down in _registered_wind_downs():
        candidate_id = f"W{wind_down}"
        cell_payloads = {}
        cell_summaries = {}
        candidate_evidence_by_cell = {}
        for cell_name, split_name, _window_ids, cost in cells:
            scenario = _scenario_name(cost)
            raw_evidence = evidences[(wind_down, scenario)]
            filtered = _filtered_evidence_for_symbols(
                raw_evidence,
                FIXED_FILTERS,
                contexts[split_name],
                candidate_id=candidate_id,
                round_name="round5_early_wind_down",
                split_name=split_name,
            )
            candidate_evidence_by_cell[cell_name] = filtered
            candidate_payload = _evidence_payload(
                filtered,
                split_name,
                market_states[split_name],
            )
            btc_coverage = _symbol_trade_coverage(
                evidences[(REFERENCE_WIND_DOWN_BARS, scenario)],
                filtered,
                "BTCUSDT",
                split_name,
            )
            eth_coverage = _symbol_trade_coverage(
                evidences[(REFERENCE_WIND_DOWN_BARS, scenario)],
                filtered,
                "ETHUSDT",
                split_name,
            )
            checks = _cell_checks(
                raw_baseline_payloads[cell_name]["evidence"]["summary"],
                candidate_payload["summary"],
                seed_count=len(seeds),
                btc_coverage=btc_coverage,
                eth_coverage=eth_coverage,
            )
            cell_payloads[cell_name] = {
                "candidate": candidate_payload,
                "trade_coverage": {
                    "BTCUSDT": btc_coverage,
                    "ETHUSDT": eth_coverage,
                },
                "checks": checks,
                "passed": all(checks.values()),
            }
            cell_summaries[cell_name] = candidate_payload["summary"]
        candidate_mechanism = _mechanism_summary(
            _flatten_results(
                candidate_evidence_by_cell["DEV_COST50"],
                "development",
            )
        )
        mechanism_checks = _mechanism_checks(
            reference_mechanism,
            candidate_mechanism,
        )
        candidate_payloads[candidate_id] = {
            "wind_down_bars": wind_down,
            "cells": cell_payloads,
            "mechanism": {
                "reference": reference_mechanism,
                "candidate": candidate_mechanism,
            },
            "mechanism_metrics": _mechanism_metrics(
                reference_mechanism,
                candidate_mechanism,
            ),
            "mechanism_checks": mechanism_checks,
            "all_cells_passed": all(
                item["passed"] for item in cell_payloads.values()
            ),
            "all_checks_passed": (
                all(item["passed"] for item in cell_payloads.values())
                and all(mechanism_checks.values())
            ),
            "selection_metrics": _selection_metrics(cell_summaries),
        }

    selection_input = {
        candidate_id: {
            **payload,
            "all_cells_passed": payload["all_checks_passed"],
        }
        for candidate_id, payload in candidate_payloads.items()
    }
    selected = _select_candidate(selection_input)
    eligible = sorted(
        candidate_id
        for candidate_id, payload in candidate_payloads.items()
        if payload["all_checks_passed"]
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": _protocol_sha256(Path(args.protocol).resolve()),
        "source_sha256": {
            "round4_results": ROUND4_RESULT_SHA256,
            "round4_diagnostics": ROUND4_DIAGNOSTIC_SHA256,
        },
        "datasets": datasets,
        "seeds": list(seeds),
        "split": {
            "development_count": len(split.development),
            "validation_count": len(split.validation),
            "final_oos_count": len(split.final_oos),
            "validation_role": "CONSUMED_AS_EXTENDED_DEVELOPMENT",
            "final_oos_status": "SEALED_NOT_EVALUATED",
        },
        "fixed_filters": {
            symbol: asdict(entry_filter) | {"filter_id": entry_filter.filter_id}
            for symbol, entry_filter in FIXED_FILTERS.items()
        },
        "reference_wind_down_bars": REFERENCE_WIND_DOWN_BARS,
        "raw_baselines": raw_baseline_payloads,
        "reference_cells": reference_cells,
        "reference_mechanism": reference_mechanism,
        "candidates": candidate_payloads,
        "eligible_candidate_ids": eligible,
        "selected_candidate_id": selected,
        "final_oos_authorized": selected is not None,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "conclusion": (
            "提前 wind-down 选出唯一候选；只允许先预注册 Final OOS 协议。"
            if selected
            else "NO_ROBUST_CANDIDATE：提前 wind-down 未通过四单元与机制门槛。"
        ),
    }
    _write_json(output, result)
    (report_dir / "round5-early-wind-down-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"ELIGIBLE {len(eligible)}")
    print(f"SELECTED {selected or 'NONE'}")


if __name__ == "__main__":
    main()
