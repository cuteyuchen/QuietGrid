from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_early_wind_down import (
    _assert_close,
    _cell_specs,
    _flatten_results,
    _mechanism_checks,
    _mechanism_metrics,
    _mechanism_summary,
    _scenario_name,
    _sha256,
    _verify_reference_summary,
)
from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
    _symbol_trade_coverage,
)
from scripts.cross_era_extended_development import _cell_checks, _selection_metrics
from scripts.cross_era_oos import (
    _dataset_brief,
    _evidence_payload,
    _load_research_state,
    _market_states,
    _protocol_sha256,
    _registered_candidates,
    _write_json,
)
from scripts.cross_era_quadratic_maker_urgency import (
    FIXED_FILTERS,
    WIND_DOWN_BARS,
    _verify_worker_cache,
)
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    CandidateEvidence,
    _locked_policy,
)
from scripts.robustness import RobustnessResearch, WindowResult, WindDownMakerPolicy


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-quadratic-volatility-defense-round12-20260723"
PROTOCOL_SHA256 = "6e797b1bbbd06b10fa678564d60a93fbea8207fea59c99c414c959a9590861a1"
ROUND5_RESULT_SHA256 = (
    "c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084"
)
ROUND11_RESULT_SHA256 = (
    "ab769bc64cb4e4b8fd3294bbeb02b8e673be1b3cd1033efc33695bf47c840f0a"
)
P4_RESULT_SHA256 = (
    "d7a6dabfa637a9c24d095f1e153b9c0af6ef0278db554d53d811e113e8eb4bc8"
)
REFERENCE_VARIANT_ID = "W1440_LINEAR_NO_VOL"
CANDIDATE_ID = "Q2_V150_N10_F20"
REFERENCE_EXPONENT = 1.0
CANDIDATE_EXPONENT = 2.0
VOLATILITY_EXPANSION_RATIO = 1.50
VOLATILITY_BREACH_COUNT = 10
VOLATILITY_REDUCE_FRACTION = 0.20


def _variant_config_and_policy(
    base_config: Any,
    base_policy: WindDownMakerPolicy,
    variant_id: str,
) -> tuple[Any, WindDownMakerPolicy]:
    common = {
        "wind_down_bars": WIND_DOWN_BARS,
        "volatility_reduce_mode": "BOTH",
        "volatility_reduce_only_when_losing": False,
        "volatility_wind_down_after_reduce": False,
        "volatility_resume_after_normal_bars": 0,
    }
    if variant_id == REFERENCE_VARIANT_ID:
        config = replace(
            base_config,
            **common,
            volatility_reduce_expansion_ratio=0.0,
            volatility_reduce_after_breaches=0,
            volatility_reduce_fraction=VOLATILITY_REDUCE_FRACTION,
        )
        policy = replace(base_policy, urgency_exponent=REFERENCE_EXPONENT)
    elif variant_id == CANDIDATE_ID:
        config = replace(
            base_config,
            **common,
            volatility_reduce_expansion_ratio=VOLATILITY_EXPANSION_RATIO,
            volatility_reduce_after_breaches=VOLATILITY_BREACH_COUNT,
            volatility_reduce_fraction=VOLATILITY_REDUCE_FRACTION,
        )
        policy = replace(base_policy, urgency_exponent=CANDIDATE_EXPONENT)
    else:
        raise ValueError(f"未知 Round 12 变体: {variant_id}")
    return config, policy


def _verify_variant_execution(
    research: RobustnessResearch,
    *,
    allowed_window_ids: set[str],
    expected_exponent: float,
    volatility_enabled: bool,
) -> dict[str, Any]:
    observation = _verify_worker_cache(
        research,
        allowed_window_ids=allowed_window_ids,
        expected_exponent=expected_exponent,
    )
    config = research.config
    expected_ratio = VOLATILITY_EXPANSION_RATIO if volatility_enabled else 0.0
    expected_breaches = VOLATILITY_BREACH_COUNT if volatility_enabled else 0
    if abs(config.volatility_reduce_expansion_ratio - expected_ratio) > 1e-12:
        raise RuntimeError("Round 12 波动扩张阈值不一致。")
    if config.volatility_reduce_after_breaches != expected_breaches:
        raise RuntimeError("Round 12 波动连续次数不一致。")
    if abs(config.volatility_reduce_fraction - VOLATILITY_REDUCE_FRACTION) > 1e-12:
        raise RuntimeError("Round 12 波动减仓比例不一致。")
    if config.volatility_reduce_mode != "BOTH":
        raise RuntimeError("Round 12 波动减仓方向必须为 BOTH。")
    if config.volatility_reduce_only_when_losing:
        raise RuntimeError("Round 12 不允许仅亏损时减仓。")
    if config.volatility_wind_down_after_reduce:
        raise RuntimeError("Round 12 不允许减仓后冻结 OPEN。")
    if config.volatility_resume_after_normal_bars != 0:
        raise RuntimeError("Round 12 不允许恢复冷却参数。")
    return observation | {
        "volatility_reduce_expansion_ratio": expected_ratio,
        "volatility_reduce_after_breaches": expected_breaches,
        "volatility_reduce_fraction": VOLATILITY_REDUCE_FRACTION,
        "volatility_reduce_mode": "BOTH",
        "volatility_enabled": volatility_enabled,
    }


def _combined_seed_worker(
    variant_id: str,
    seed: int,
    split_ids: dict[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[str, int, dict[str, tuple[Any, list[WindowResult]]], dict[str, Any]]:
    state = profit_opt._WORKER_STATE
    maker_fee, taker_fee, slippage_bps = cost
    config, maker_policy = _variant_config_and_policy(
        state["base_config"],
        state["maker_policy"],
        variant_id,
    )
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
            maker_policy,
            window_ids,
            maker_fee_rate=maker_fee,
            taker_fee_rate=taker_fee,
            stop_slippage_bps=slippage_bps,
            fill_seed_salt=seed,
        )
    observation = _verify_variant_execution(
        research,
        allowed_window_ids=set(split_ids["development"]) | set(split_ids["validation"]),
        expected_exponent=maker_policy.urgency_exponent,
        volatility_enabled=variant_id == CANDIDATE_ID,
    )
    return variant_id, seed, runs, observation


def _report_markdown(payload: dict[str, Any]) -> str:
    candidate = payload["candidates"][CANDIDATE_ID]
    counts = {
        name: (sum(cell["checks"].values()), len(cell["checks"]))
        for name, cell in candidate["cells"].items()
    }
    mechanism = candidate["mechanism_metrics"]
    metrics = candidate["selection_metrics"]
    return "\n".join([
        "# Round 12 二次 Maker 紧迫度与因果波动减仓扩展开发筛选",
        "",
        "- 证据角色：Development + 已消费 Validation，仅用于扩展开发",
        "- 稳定收益声明：否",
        f"- 候选数：{len(payload['candidates'])}",
        f"- 合格候选数：{len(payload['eligible_candidate_ids'])}",
        f"- 选中候选：`{payload['selected_candidate_id'] or 'NONE'}`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "组合：固定 `W1440`、二次 Maker 紧迫度、因果波动减仓 `V1.50/N10/F20`。",
        "",
        "| 候选 | DEV BASE | DEV COST50 | VAL BASE | VAL COST50 | 退出损失改善 | 配对收益保留 | 波动减仓 | 最弱种子 | 最大集中度 | 全过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        (
            "| {candidate_id} | {db[0]}/{db[1]} | {dc[0]}/{dc[1]} | "
            "{vb[0]}/{vb[1]} | {vc[0]}/{vc[1]} | {stop:.2%} | {paired:.2%} | "
            "{reduces} | {worst:.4f} | {concentration:.2%} | {passed} |"
        ).format(
            candidate_id=CANDIDATE_ID,
            db=counts["DEV_BASE"],
            dc=counts["DEV_COST50"],
            vb=counts["VAL_BASE"],
            vc=counts["VAL_COST50"],
            stop=mechanism["stop_exit_loss_improvement"],
            paired=mechanism["paired_grid_pnl_retention"],
            reduces=candidate["mechanism"]["candidate"]["volatility_reduce_count"],
            worst=metrics["minimum_worst_seed_total_pnl"],
            concentration=metrics["maximum_best_window_concentration"],
            passed="PASS" if candidate["all_checks_passed"] else "FAIL",
        ),
        "",
        f"结论：{payload['conclusion']}",
        "",
    ])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估二次 Maker 紧迫度与因果波动减仓的唯一组合。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round12-quadratic-volatility-defense-protocol.md",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    if seeds != DEFAULT_SEEDS:
        raise ValueError("Round 12 必须使用协议冻结的六个种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")

    protocol_path = Path(args.protocol).resolve()
    if _sha256(protocol_path) != PROTOCOL_SHA256:
        raise ValueError("Round 12 协议哈希已变化。")
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "round12-quadratic-volatility-defense-results.json"
    if output.exists():
        raise FileExistsError(f"Round 12 结果已存在，拒绝覆盖: {output}")

    round5_path = report_dir / "round5-early-wind-down-results.json"
    round11_path = report_dir / "round11-quadratic-maker-urgency-results.json"
    p4_path = report_dir.parent / "volatility-defense" / "results.json"
    if _sha256(round5_path) != ROUND5_RESULT_SHA256:
        raise ValueError("Round 5 冻结结果已变化。")
    if _sha256(round11_path) != ROUND11_RESULT_SHA256:
        raise ValueError("Round 11 冻结结果已变化。")
    if _sha256(p4_path) != P4_RESULT_SHA256:
        raise ValueError("P4 波动防御冻结结果已变化。")
    round5 = json.loads(round5_path.read_text(encoding="utf-8"))
    round11 = json.loads(round11_path.read_text(encoding="utf-8"))
    p4 = json.loads(p4_path.read_text(encoding="utf-8"))
    for prior, name in ((round5, "Round 5"), (round11, "Round 11")):
        if prior.get("eligible_candidate_ids"):
            raise ValueError(f"{name} 已有合格候选，不应执行 Round 12。")
        if prior.get("final_oos_status") != "SEALED_NOT_EVALUATED":
            raise ValueError(f"{name} 的 Final OOS 已不再封存。")
    p4_candidate = p4["candidates"].get("P4_R2_V150_N10_F20", {}).get("candidate", {})
    expected_p4 = {
        "volatility_reduce_expansion_ratio": VOLATILITY_EXPANSION_RATIO,
        "volatility_reduce_after_breaches": VOLATILITY_BREACH_COUNT,
        "volatility_reduce_fraction": VOLATILITY_REDUCE_FRACTION,
    }
    if any(abs(float(p4_candidate.get(key, -1)) - value) > 1e-12 for key, value in expected_p4.items()):
        raise ValueError("P4 冻结波动参数与 Round 12 协议不一致。")

    base_config, metadata, windows, split = _load_research_state(args.manifests)
    datasets = _dataset_brief(args.manifests, metadata)
    if round5.get("datasets") != datasets or round11.get("datasets") != datasets:
        raise ValueError("当前冻结数据与 Round 5/11 不一致。")

    locked_parameters, locked_policies, maker_policy = _locked_policy()
    if any(
        policy.wind_down_bars is not None
        or policy.wind_down_initial_offset_steps is not None
        or policy.wind_down_reference_tradable_rows is not None
        or policy.wind_down_min_bars is not None
        or policy.wind_down_max_bars is not None
        for policy in locked_policies.values()
    ):
        raise ValueError("Round 12 不允许按标的覆盖 wind-down 参数。")
    if maker_policy.reprice_interval_bars != 5:
        raise ValueError("Round 12 Maker 重挂间隔必须保持 5 bars。")
    if abs(maker_policy.initial_offset_steps - 1.10) > 1e-12:
        raise ValueError("Round 12 Maker 初始偏移必须保持 1.10。")
    if abs(maker_policy.unwind_fraction - 1.0) > 1e-12:
        raise ValueError("Round 12 Maker unwind fraction 必须保持 1.00。")
    if abs(maker_policy.urgency_exponent - REFERENCE_EXPONENT) > 1e-12:
        raise ValueError("Round 12 默认紧迫度指数必须保持 1.0。")

    market_states = {
        "development": _market_states(windows, split.development),
        "validation": _market_states(windows, split.validation),
    }
    split_ids = {
        "development": split.development,
        "validation": split.validation,
    }
    variants = (REFERENCE_VARIANT_ID, CANDIDATE_ID)
    raw_runs: dict[
        tuple[str, str],
        dict[int, dict[str, tuple[Any, list[WindowResult]]]],
    ] = {
        (variant_id, scenario): {}
        for variant_id in variants
        for scenario in ("BASE", "COST50")
    }
    observations: dict[str, list[dict[str, Any]]] = {variant_id: [] for variant_id in variants}
    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=profit_opt._initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    ) as executor:
        for variant_id in variants:
            for scenario, cost in (("BASE", BASE_COST), ("COST50", COST_50)):
                print(f"EVALUATING {variant_id} {scenario}", flush=True)
                for seed in seeds:
                    future = executor.submit(
                        _combined_seed_worker,
                        variant_id,
                        seed,
                        split_ids,
                        cost,
                    )
                    futures[future] = (variant_id, scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            expected_variant, scenario, expected_seed = futures[future]
            variant_id, seed, runs, observation = future.result()
            if variant_id != expected_variant or seed != expected_seed:
                raise RuntimeError("Round 12 worker 返回了错误任务标识。")
            raw_runs[(variant_id, scenario)][seed] = runs
            observations[variant_id].append(observation)

    execution_integrity = {}
    for variant_id, items in observations.items():
        if not items or any(item != items[0] for item in items[1:]):
            raise RuntimeError(f"Round 12 worker 执行参数不一致: {variant_id}")
        execution_integrity[variant_id] = items[0]

    baseline_candidate = _registered_candidates()[0]
    evidences = {
        key: CandidateEvidence(
            baseline_candidate,
            {seed: runs[seed] for seed in sorted(runs)},
        )
        for key, runs in raw_runs.items()
    }
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
    for cell_name, split_name, window_ids, cost in cells:
        scenario = _scenario_name(cost)
        raw_baseline = evidences[(REFERENCE_VARIANT_ID, scenario)]
        raw_payload = _evidence_payload(
            raw_baseline,
            split_name,
            market_states[split_name],
        )
        _verify_reference_summary(
            raw_payload["summary"],
            round5["raw_baselines"][cell_name]["evidence"]["summary"],
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
            candidate_id=REFERENCE_VARIANT_ID,
            round_name="round12_reference",
            split_name=split_name,
        )
        filtered_payload = _evidence_payload(
            filtered,
            split_name,
            market_states[split_name],
        )
        _verify_reference_summary(
            filtered_payload["summary"],
            round5["reference_cells"][cell_name]["candidate"]["summary"],
        )
        trade_coverage = {
            symbol: _symbol_trade_coverage(
                raw_baseline,
                filtered,
                symbol,
                split_name,
            )
            for symbol in FIXED_FILTERS
        }
        _assert_close(
            f"reference_cells.{cell_name}.trade_coverage",
            trade_coverage,
            round5["reference_cells"][cell_name]["trade_coverage"],
        )
        reference_filtered_evidence[cell_name] = filtered
        reference_cells[cell_name] = {
            "candidate": filtered_payload,
            "trade_coverage": trade_coverage,
        }

    reference_mechanism = _mechanism_summary(
        _flatten_results(reference_filtered_evidence["DEV_COST50"], "development")
    )
    _assert_close(
        "reference_mechanism",
        reference_mechanism,
        round5["reference_mechanism"],
    )

    cell_payloads = {}
    cell_summaries = {}
    candidate_evidence_by_cell = {}
    for cell_name, split_name, _window_ids, cost in cells:
        scenario = _scenario_name(cost)
        raw_evidence = evidences[(CANDIDATE_ID, scenario)]
        filtered = _filtered_evidence_for_symbols(
            raw_evidence,
            FIXED_FILTERS,
            contexts[split_name],
            candidate_id=CANDIDATE_ID,
            round_name="round12_quadratic_volatility_defense",
            split_name=split_name,
        )
        candidate_evidence_by_cell[cell_name] = filtered
        candidate_payload = _evidence_payload(
            filtered,
            split_name,
            market_states[split_name],
        )
        btc_coverage = _symbol_trade_coverage(
            evidences[(REFERENCE_VARIANT_ID, scenario)],
            filtered,
            "BTCUSDT",
            split_name,
        )
        eth_coverage = _symbol_trade_coverage(
            evidences[(REFERENCE_VARIANT_ID, scenario)],
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

    candidate_results = _flatten_results(
        candidate_evidence_by_cell["DEV_COST50"],
        "development",
    )
    candidate_mechanism = _mechanism_summary(candidate_results) | {
        "volatility_reduce_count": sum(item.volatility_reduce_count for item in candidate_results),
        "volatility_reduce_pnl": sum(item.volatility_reduce_pnl for item in candidate_results),
        "volatility_reduce_cost": sum(item.volatility_reduce_cost for item in candidate_results),
    }
    mechanism_checks = _mechanism_checks(reference_mechanism, candidate_mechanism) | {
        "volatility_reduce_observed": candidate_mechanism["volatility_reduce_count"] > 0,
    }
    all_cells_passed = all(item["passed"] for item in cell_payloads.values())
    all_checks_passed = all_cells_passed and all(mechanism_checks.values())
    candidate_payloads = {
        CANDIDATE_ID: {
            "wind_down_bars": WIND_DOWN_BARS,
            "urgency_exponent": CANDIDATE_EXPONENT,
            "volatility_policy": {
                "expansion_ratio": VOLATILITY_EXPANSION_RATIO,
                "after_breaches": VOLATILITY_BREACH_COUNT,
                "fraction": VOLATILITY_REDUCE_FRACTION,
                "mode": "BOTH",
                "only_when_losing": False,
                "wind_down_after_reduce": False,
                "resume_after_normal_bars": 0,
            },
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
            "all_cells_passed": all_cells_passed,
            "all_checks_passed": all_checks_passed,
            "selection_metrics": _selection_metrics(cell_summaries),
        }
    }
    eligible = [CANDIDATE_ID] if all_checks_passed else []
    selected = CANDIDATE_ID if all_checks_passed else None
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": _protocol_sha256(protocol_path),
        "source_sha256": {
            "round5_results": ROUND5_RESULT_SHA256,
            "round11_results": ROUND11_RESULT_SHA256,
            "p4_volatility_results": P4_RESULT_SHA256,
        },
        "datasets": datasets,
        "seeds": list(seeds),
        "direction_mode": "NEUTRAL",
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
        "execution_integrity": execution_integrity,
        "raw_baselines": raw_baseline_payloads,
        "reference_cells": reference_cells,
        "reference_mechanism": reference_mechanism,
        "candidates": candidate_payloads,
        "eligible_candidate_ids": eligible,
        "selected_candidate_id": selected,
        "final_oos_authorized": selected is not None,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": (
            "二次紧迫度与因果波动减仓组合选出唯一候选；只允许先预注册 Final OOS 协议。"
            if selected
            else "NO_ROBUST_CANDIDATE：二次紧迫度与因果波动减仓组合未通过四单元与机制门槛。"
        ),
    }
    _write_json(output, result)
    (report_dir / "round12-quadratic-volatility-defense-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"ELIGIBLE {len(eligible)}")
    print(f"SELECTED {selected or 'NONE'}")


if __name__ == "__main__":
    main()
