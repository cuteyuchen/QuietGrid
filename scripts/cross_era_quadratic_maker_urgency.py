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
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    CandidateEvidence,
    _locked_policy,
)
from scripts.robustness import (
    EntryFilter,
    RobustnessResearch,
    WindowResult,
    WindDownMakerPolicy,
)


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-quadratic-maker-urgency-round11-20260723"
PROTOCOL_SHA256 = "90eb865aec3b5e4da793e8de22ee224fe28cf3373bf8634b5727b9554b3667bb"
ROUND5_RESULT_SHA256 = (
    "c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084"
)
ROUND10_RESULT_SHA256 = (
    "49d0ba18bb3464aa7e14f32695478d95151bb0b9f71e8690a473939106f49ea8"
)
REFERENCE_VARIANT_ID = "W1440_LINEAR_E1"
CANDIDATE_ID = "W1440_QUADRATIC_E2"
WIND_DOWN_BARS = 1440
REFERENCE_EXPONENT = 1.0
CANDIDATE_EXPONENT = 2.0
FIXED_FILTERS = {
    "BTCUSDT": EntryFilter(0.40, 1.05, 0.35),
    "ETHUSDT": EntryFilter(0.35, 1.05, 0.55),
}


def _maker_policy_for_variant(
    base_policy: WindDownMakerPolicy,
    variant_id: str,
) -> WindDownMakerPolicy:
    if variant_id == REFERENCE_VARIANT_ID:
        exponent = REFERENCE_EXPONENT
    elif variant_id == CANDIDATE_ID:
        exponent = CANDIDATE_EXPONENT
    else:
        raise ValueError(f"未知 Round 11 变体: {variant_id}")
    return replace(base_policy, urgency_exponent=exponent)


def _verify_worker_cache(
    research: RobustnessResearch,
    *,
    allowed_window_ids: set[str],
    expected_exponent: float,
) -> dict[str, Any]:
    symbols_by_window: dict[str, set[str]] = {}
    observed_wind_down: set[int] = set()
    observed_reprice: set[int] = set()
    observed_offset: set[float] = set()
    observed_unwind: set[float] = set()
    observed_exponent: set[float] = set()

    for cache_key in research._cache:
        if len(cache_key) < 16:
            raise RuntimeError("Round 11 worker cache 缺少紧迫度指数。")
        symbol = str(cache_key[1]).strip().upper()
        window_id = str(cache_key[2])
        if window_id not in allowed_window_ids:
            raise RuntimeError("Round 11 worker 访问了 Development/Validation 之外的窗口。")
        symbols_by_window.setdefault(window_id, set()).add(symbol)
        observed_wind_down.add(int(cache_key[4]))
        observed_reprice.add(int(cache_key[6]))
        observed_offset.add(float(cache_key[7]))
        observed_unwind.add(float(cache_key[8]))
        observed_exponent.add(float(cache_key[-2]))

    if set(symbols_by_window) != allowed_window_ids:
        raise RuntimeError("Round 11 worker 未覆盖全部 Development/Validation 窗口。")
    if any(values != {"BTCUSDT", "ETHUSDT"} for values in symbols_by_window.values()):
        raise RuntimeError("Round 11 worker 未对同一窗口同时覆盖 BTC/ETH。")
    if observed_wind_down != {WIND_DOWN_BARS}:
        raise RuntimeError(f"Round 11 wind-down 不一致: {observed_wind_down}")
    if observed_reprice != {5}:
        raise RuntimeError(f"Round 11 Maker 重挂间隔不一致: {observed_reprice}")
    if observed_offset != {1.10}:
        raise RuntimeError(f"Round 11 Maker 初始偏移不一致: {observed_offset}")
    if observed_unwind != {1.0}:
        raise RuntimeError(f"Round 11 Maker unwind fraction 不一致: {observed_unwind}")
    if observed_exponent != {expected_exponent}:
        raise RuntimeError(f"Round 11 紧迫度指数不一致: {observed_exponent}")

    return {
        "window_count": len(symbols_by_window),
        "symbol_window_count": sum(len(values) for values in symbols_by_window.values()),
        "wind_down_bars": WIND_DOWN_BARS,
        "reprice_interval_bars": 5,
        "initial_offset_steps": 1.10,
        "unwind_fraction": 1.0,
        "urgency_exponent": expected_exponent,
        "cache_entry_count": len(research._cache),
        "passed": True,
    }


def _urgency_seed_worker(
    variant_id: str,
    seed: int,
    split_ids: dict[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[str, int, dict[str, tuple[Any, list[WindowResult]]], dict[str, Any]]:
    state = profit_opt._WORKER_STATE
    maker_fee, taker_fee, slippage_bps = cost
    config = replace(state["base_config"], wind_down_bars=WIND_DOWN_BARS)
    maker_policy = _maker_policy_for_variant(state["maker_policy"], variant_id)
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
    observation = _verify_worker_cache(
        research,
        allowed_window_ids=set(split_ids["development"]) | set(split_ids["validation"]),
        expected_exponent=maker_policy.urgency_exponent,
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
        "# Round 11 二次 Maker Wind-down 紧迫度扩展开发筛选",
        "",
        "- 证据角色：Development + 已消费 Validation，仅用于扩展开发",
        "- 稳定收益声明：否",
        f"- 候选数：{len(payload['candidates'])}",
        f"- 合格候选数：{len(payload['eligible_candidate_ids'])}",
        f"- 选中候选：`{payload['selected_candidate_id'] or 'NONE'}`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "公式：`offset_steps = 1.10 × remaining_ratio²`，固定 `W1440`。",
        "",
        "| 候选 | DEV BASE | DEV COST50 | VAL BASE | VAL COST50 | 退出损失改善 | 配对收益保留 | 最弱种子 | 最大集中度 | 全过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        (
            "| {candidate_id} | {db[0]}/{db[1]} | {dc[0]}/{dc[1]} | "
            "{vb[0]}/{vb[1]} | {vc[0]}/{vc[1]} | {stop:.2%} | {paired:.2%} | "
            "{worst:.4f} | {concentration:.2%} | {passed} |"
        ).format(
            candidate_id=CANDIDATE_ID,
            db=counts["DEV_BASE"],
            dc=counts["DEV_COST50"],
            vb=counts["VAL_BASE"],
            vc=counts["VAL_COST50"],
            stop=mechanism["stop_exit_loss_improvement"],
            paired=mechanism["paired_grid_pnl_retention"],
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
        description="评估固定 W1440 的二次 Maker wind-down 紧迫度。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round11-quadratic-maker-urgency-protocol.md",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    if seeds != DEFAULT_SEEDS:
        raise ValueError("Round 11 必须使用协议冻结的六个种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")

    protocol_path = Path(args.protocol).resolve()
    if _sha256(protocol_path) != PROTOCOL_SHA256:
        raise ValueError("Round 11 协议哈希已变化。")
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "round11-quadratic-maker-urgency-results.json"
    if output.exists():
        raise FileExistsError(f"Round 11 结果已存在，拒绝覆盖: {output}")

    round5_path = report_dir / "round5-early-wind-down-results.json"
    round10_path = report_dir / "round10-duration-adaptive-wind-down-results.json"
    if _sha256(round5_path) != ROUND5_RESULT_SHA256:
        raise ValueError("Round 5 冻结结果已变化。")
    if _sha256(round10_path) != ROUND10_RESULT_SHA256:
        raise ValueError("Round 10 冻结结果已变化。")
    round5 = json.loads(round5_path.read_text(encoding="utf-8"))
    round10 = json.loads(round10_path.read_text(encoding="utf-8"))
    for prior, name in ((round5, "Round 5"), (round10, "Round 10")):
        if prior.get("eligible_candidate_ids"):
            raise ValueError(f"{name} 已有合格候选，不应执行 Round 11。")
        if prior.get("final_oos_status") != "SEALED_NOT_EVALUATED":
            raise ValueError(f"{name} 的 Final OOS 已不再封存。")

    base_config, metadata, windows, split = _load_research_state(args.manifests)
    datasets = _dataset_brief(args.manifests, metadata)
    if round5.get("datasets") != datasets or round10.get("datasets") != datasets:
        raise ValueError("当前冻结数据与 Round 5/10 不一致。")

    locked_parameters, locked_policies, maker_policy = _locked_policy()
    if any(
        policy.wind_down_bars is not None
        or policy.wind_down_initial_offset_steps is not None
        or policy.wind_down_reference_tradable_rows is not None
        or policy.wind_down_min_bars is not None
        or policy.wind_down_max_bars is not None
        for policy in locked_policies.values()
    ):
        raise ValueError("Round 11 不允许按标的覆盖 wind-down 参数。")
    if maker_policy.reprice_interval_bars != 5:
        raise ValueError("Round 11 Maker 重挂间隔必须保持 5 bars。")
    if abs(maker_policy.initial_offset_steps - 1.10) > 1e-12:
        raise ValueError("Round 11 Maker 初始偏移必须保持 1.10。")
    if abs(maker_policy.unwind_fraction - 1.0) > 1e-12:
        raise ValueError("Round 11 Maker unwind fraction 必须保持 1.00。")
    if abs(maker_policy.urgency_exponent - REFERENCE_EXPONENT) > 1e-12:
        raise ValueError("Round 11 默认紧迫度指数必须保持 1.0。")

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
                        _urgency_seed_worker,
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
                raise RuntimeError("Round 11 worker 返回了错误任务标识。")
            raw_runs[(variant_id, scenario)][seed] = runs
            observations[variant_id].append(observation)

    execution_integrity = {}
    for variant_id, items in observations.items():
        if not items or any(item != items[0] for item in items[1:]):
            raise RuntimeError(f"Round 11 worker 执行参数不一致: {variant_id}")
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
            round_name="round11_reference",
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
            round_name="round11_quadratic_maker_urgency",
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

    candidate_mechanism = _mechanism_summary(
        _flatten_results(candidate_evidence_by_cell["DEV_COST50"], "development")
    )
    mechanism_checks = _mechanism_checks(reference_mechanism, candidate_mechanism)
    all_cells_passed = all(item["passed"] for item in cell_payloads.values())
    all_checks_passed = all_cells_passed and all(mechanism_checks.values())
    candidate_payloads = {
        CANDIDATE_ID: {
            "wind_down_bars": WIND_DOWN_BARS,
            "urgency": {
                "initial_offset_steps": 1.10,
                "reference_exponent": REFERENCE_EXPONENT,
                "candidate_exponent": CANDIDATE_EXPONENT,
                "formula": "offset_steps = 1.10 * remaining_ratio ** 2",
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
            "round10_results": ROUND10_RESULT_SHA256,
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
            "二次 Maker 紧迫度选出唯一候选；只允许先预注册 Final OOS 协议。"
            if selected
            else "NO_ROBUST_CANDIDATE：二次 Maker 紧迫度未通过四单元与机制门槛。"
        ),
    }
    _write_json(output, result)
    (report_dir / "round11-quadratic-maker-urgency-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"ELIGIBLE {len(eligible)}")
    print(f"SELECTED {selected or 'NONE'}")


if __name__ == "__main__":
    main()
