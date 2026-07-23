from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    CandidateEvidence,
    ProfitCandidate,
    _base_research_config,
    _candidate,
    _classify_market_state,
    _evaluate_candidate,
    _initialize_worker,
    _load_data,
    _locked_policy,
    _markdown_table,
    _run_pytest,
    _state_breakdown_rows,
    _stress_passed,
    _summary_payload,
    _walk_forward_rows,
    _write_csv,
)
from scripts.robustness import split_window_ids


UTC = timezone.utc


def _volatility_candidate(
    candidate_id: str,
    round_name: str,
    *,
    expansion_ratio: float,
    breaches: int,
    fraction: float,
    mode: str = "BOTH",
    only_when_losing: bool = False,
    wind_down_after_reduce: bool = False,
    resume_after_normal_bars: int = 0,
) -> ProfitCandidate:
    return _candidate(
        candidate_id,
        round_name,
        enabled=False,
        mode="OFF",
        volatility_reduce_expansion_ratio=expansion_ratio,
        volatility_reduce_after_breaches=breaches,
        volatility_reduce_fraction=fraction,
        volatility_reduce_mode=mode,
        volatility_reduce_only_when_losing=only_when_losing,
        volatility_wind_down_after_reduce=wind_down_after_reduce,
        volatility_resume_after_normal_bars=resume_after_normal_bars,
    )


def _relative_improvement(baseline: float, candidate: float) -> float:
    if baseline < 0:
        return (candidate - baseline) / abs(baseline)
    return 1.0 if candidate >= baseline else -1.0


def _checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    tests_passed: bool,
) -> dict[str, bool]:
    base = baseline["combined"]
    item = candidate["combined"]
    base_mean = float(base["mean_seed_total_pnl"])
    mean_retention = (
        float(item["mean_seed_total_pnl"]) / base_mean
        if base_mean > 0
        else (1.0 if float(item["mean_seed_total_pnl"]) >= base_mean else -1.0)
    )
    base_range = float(base["state_pnl"].get("RANGE", 0.0))
    range_retention = (
        float(item["state_pnl"].get("RANGE", 0.0)) / base_range
        if base_range > 0
        else 1.0
    )
    base_volatility = float(base["state_pnl"].get("VOLATILITY_EXPANSION", 0.0))
    candidate_volatility = float(
        item["state_pnl"].get("VOLATILITY_EXPANSION", 0.0)
    )
    base_fee = base["fee_to_gross_profit_ratio"]
    candidate_fee = item["fee_to_gross_profit_ratio"]
    fee_ok = (
        candidate_fee is not None
        and (
            base_fee is None
            or float(candidate_fee)
            <= max(float(base_fee) * 1.15, float(base_fee) + 0.05)
        )
    )
    symbol_ok = True
    for symbol, base_pnl in base["symbol_pnl"].items():
        candidate_pnl = float(item["symbol_pnl"].get(symbol, 0.0))
        if candidate_pnl < float(base_pnl) - max(5.0, abs(float(base_pnl)) * 0.30):
            symbol_ok = False
    target = float(candidate["candidate"]["volatility_reduce_fraction"])
    observed = item["median_volatility_reduce_inventory_reduction_pct"]
    return {
        "volatility_loss_improvement_ge_20pct": _relative_improvement(
            base_volatility,
            candidate_volatility,
        )
        >= 0.20,
        "worst_5pct_loss_improvement_ge_20pct": _relative_improvement(
            float(base["worst_5pct_window_mean_pnl"]),
            float(item["worst_5pct_window_mean_pnl"]),
        )
        >= 0.20,
        "max_drawdown_not_worse_than_5pct": float(item["max_drawdown_pct"])
        <= float(base["max_drawdown_pct"]) * 1.05,
        "mean_pnl_retention_ge_80pct": mean_retention >= 0.80,
        "range_profit_retention_ge_75pct": range_retention >= 0.75,
        "positive_seed_count_ge_4": int(item["positive_seed_count"]) >= 4,
        "both_symbols_no_catastrophic_deterioration": symbol_ok,
        "fee_ratio_not_materially_worse": fee_ok,
        "best_window_concentration_le_35pct": float(
            item["worst_best_window_concentration"]
        )
        <= 0.35,
        "volatility_reduce_observed": int(item["volatility_reduce_count"]) > 0,
        "inventory_reduction_ge_90pct_target": (
            observed is not None and float(observed) >= target * 0.90 - 1e-9
        ),
        "full_pytest_passed": tests_passed,
    }


def _development_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate": payload["candidate"],
        "combined": payload["development"],
    }


def _development_score(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[Any, ...]:
    checks = _checks(
        _development_payload(baseline),
        _development_payload(candidate),
        tests_passed=True,
    )
    checks.pop("full_pytest_passed", None)
    item = candidate["development"]
    return (
        all(checks.values()),
        sum(checks.values()),
        float(item["worst_5pct_window_mean_pnl"]),
        float(item["state_pnl"].get("VOLATILITY_EXPANSION", 0.0)),
        float(item["mean_seed_total_pnl"]),
        -float(item["max_drawdown_pct"]),
    )


def _parameter_rows(
    payloads: dict[str, dict[str, Any]],
    checks: dict[str, dict[str, bool]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate_id, payload in payloads.items():
        candidate = payload["candidate"]
        combined = payload["combined"]
        item_checks = checks.get(candidate_id, {})
        rows.append({
            "candidate_id": candidate_id,
            "round": candidate["round_name"],
            "expansion_ratio": candidate["volatility_reduce_expansion_ratio"],
            "breaches": candidate["volatility_reduce_after_breaches"],
            "fraction": candidate["volatility_reduce_fraction"],
            "mode": candidate["volatility_reduce_mode"],
            "only_when_losing": candidate[
                "volatility_reduce_only_when_losing"
            ],
            "development_mean_seed_total_pnl": payload["development"][
                "mean_seed_total_pnl"
            ],
            "mean_seed_total_pnl": combined["mean_seed_total_pnl"],
            "worst_seed_total_pnl": combined["worst_seed_total_pnl"],
            "worst_5pct_window_mean_pnl": combined["worst_5pct_window_mean_pnl"],
            "max_drawdown_pct": combined["max_drawdown_pct"],
            "range_pnl": combined["state_pnl"].get("RANGE", 0.0),
            "volatility_expansion_pnl": combined["state_pnl"].get(
                "VOLATILITY_EXPANSION",
                0.0,
            ),
            "fee_to_gross_profit_ratio": combined["fee_to_gross_profit_ratio"],
            "volatility_breach_count": combined[
                "volatility_breach_count"
            ],
            "volatility_max_consecutive_breaches": combined[
                "volatility_max_consecutive_breaches"
            ],
            "volatility_reduce_count": combined["volatility_reduce_count"],
            "volatility_reduce_cost": combined["volatility_reduce_cost"],
            "median_inventory_reduction_pct": combined[
                "median_volatility_reduce_inventory_reduction_pct"
            ],
            "passed_gate_count": sum(item_checks.values()),
            "all_gates_passed": bool(item_checks) and all(item_checks.values()),
        })
    return rows


def _write_reports(
    output_dir: Path,
    *,
    metadata: Sequence[dict[str, Any]],
    split: Any,
    windows: Sequence[Any],
    market_states: dict[str, str],
    payloads: dict[str, dict[str, Any]],
    evidences: dict[str, CandidateEvidence],
    selected_id: str,
    checks: dict[str, dict[str, bool]],
    stress_summary: dict[str, Any],
    stress_ok: bool,
    pytest_result: dict[str, Any],
    seeds: Sequence[int],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = payloads[selected_id]
    selected_checks = checks[selected_id]
    robust = all(selected_checks.values()) and stress_ok
    conclusion = (
        "P4 研究门槛全部通过；仅建议进入测试网并等待全新锁定 OOS。"
        if robust
        else "本轮没有稳健候选，保持生产参数不变。"
    )
    _write_csv(output_dir / "parameter-search.csv", _parameter_rows(payloads, checks))
    ordered_ids = [
        item.window_id
        for item in sorted(windows, key=lambda value: value.market_close)
        if item.window_id in set(split.development) | set(split.validation)
    ]
    selected_evidence = evidences[selected_id]
    _write_csv(
        output_dir / "walk-forward.csv",
        _walk_forward_rows([selected_evidence], ordered_ids, seeds),
    )
    _write_csv(
        output_dir / "state-breakdown.csv",
        _state_breakdown_rows([selected_evidence], market_states),
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "protocol": "causal-volatility-defense-research-validation",
        "selection_rule": "development_only_validation_reserved_for_final_acceptance",
        "lookahead_policy": (
            "bar i 的 volatility_expansion 仅由 i 之前 60 根已闭合 K 线计算；"
            "达到连续确认次数后在 bar i 开盘执行一次部分减仓。"
        ),
        "datasets": list(metadata),
        "split": {
            "development": {"count": len(split.development)},
            "validation": {"count": len(split.validation)},
            "final_oos": {
                "count": len(split.final_oos),
                "status": "CONSUMED_RESEARCH_VALIDATION_ONLY",
            },
        },
        "candidates": payloads,
        "selected_candidate_id": selected_id,
        "selected_checks": selected_checks,
        "selected_cost_50": stress_summary,
        "selected_cost_50_passed": stress_ok,
        "pytest": pytest_result,
        "robust_research_candidate": robust,
        "testnet_recommended": robust,
        "production_defaults_changed": False,
        "conclusion": conclusion,
    }
    (output_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    baseline = payloads["P4_BASELINE"]["combined"]
    combined = selected["combined"]
    comparison = [
        [name, baseline[key], combined[key]]
        for name, key in (
            ("六种子平均净收益", "mean_seed_total_pnl"),
            ("六种子最差净收益", "worst_seed_total_pnl"),
            ("最差 5% 窗口均值", "worst_5pct_window_mean_pnl"),
            ("最大回撤", "max_drawdown_pct"),
            ("费用/毛利润", "fee_to_gross_profit_ratio"),
        )
    ]
    comparison.extend([
        ["RANGE PnL", baseline["state_pnl"].get("RANGE", 0.0), combined["state_pnl"].get("RANGE", 0.0)],
        ["VOLATILITY_EXPANSION PnL", baseline["state_pnl"].get("VOLATILITY_EXPANSION", 0.0), combined["state_pnl"].get("VOLATILITY_EXPANSION", 0.0)],
    ])
    report = "\n".join([
        "# P4 因果波动扩张防御报告",
        "",
        f"- 选中：`{selected_id}`（仅按 Development 选择）",
        "- 信号：严格使用当前 Bar 之前 60 根已闭合 K 线",
        "- Validation：仅用于最终验收",
        "- Final OOS：旧区间已消费，本轮仅为 Research Validation",
        "- 生产参数：未修改",
        f"- COST50：{'PASS' if stress_ok else 'FAIL'}",
        f"- 完整测试：{'PASS' if pytest_result.get('passed') else 'FAIL'}",
        "",
        "## 基线对照",
        "",
        _markdown_table(comparison, ["指标", "基线", "P4"]),
        "",
        "## 执行实效",
        "",
        f"- 因果波动扩张 Bar：{combined['volatility_breach_count']}",
        f"- 最大连续扩张：{combined['volatility_max_consecutive_breaches']}",
        f"- 主动部分减仓：{combined['volatility_reduce_count']}",
        f"- 减仓成本：{combined['volatility_reduce_cost']:.6f} USDT",
        "- 库存下降中位数："
        f"{combined['median_volatility_reduce_inventory_reduction_pct']}",
        "",
        "## 门槛",
        "",
        _markdown_table(
            [[name, "PASS" if passed else "FAIL"] for name, passed in selected_checks.items()],
            ["门槛", "结果"],
        ),
        "",
        "## 结论",
        "",
        conclusion,
        "",
    ])
    (output_dir / "final-report.md").write_text(report, encoding="utf-8")
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行严格因果的波动扩张存量仓位减仓研究。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--max-rounds", type=int, choices=(1, 2, 3), default=3)
    parser.add_argument("--report-dir", default="reports/volatility-defense")
    parser.add_argument("--skip-tests", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = [int(value.strip()) for value in args.seeds.split(",") if value.strip()]
    if not seeds:
        raise ValueError("至少需要一个固定种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    repo_root = Path(__file__).resolve().parents[1]
    base_config = _base_research_config()
    metadata, windows = _load_data(args.manifests, base_config)
    split = split_window_ids(
        windows,
        dev_ratio=base_config.dev_ratio,
        validation_ratio=base_config.validation_ratio,
        min_windows_per_split=base_config.min_windows_per_split,
    )
    split_ids = {"development": split.development, "validation": split.validation}
    market_states = {item.window_id: _classify_market_state(item) for item in windows}
    parameters, symbol_policies, maker_policy = _locked_policy()
    evidences: dict[str, CandidateEvidence] = {}
    payloads: dict[str, dict[str, Any]] = {}
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=_initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    )

    def evaluate(candidate: ProfitCandidate) -> None:
        for existing in evidences.values():
            old = asdict(existing.candidate) | {"candidate_id": "", "round_name": ""}
            new = asdict(candidate) | {"candidate_id": "", "round_name": ""}
            if old == new:
                print(f"REUSING {existing.candidate.candidate_id} AS {candidate.candidate_id}", flush=True)
                evidence = CandidateEvidence(candidate, existing.runs)
                evidences[candidate.candidate_id] = evidence
                payloads[candidate.candidate_id] = _summary_payload(evidence, market_states)
                return
        print(f"EVALUATING {candidate.candidate_id}", flush=True)
        evidence = _evaluate_candidate(
            candidate,
            windows=windows,
            metadata=metadata,
            base_config=base_config,
            parameters=parameters,
            symbol_policies=symbol_policies,
            maker_policy=maker_policy,
            split_ids=split_ids,
            seeds=seeds,
            cost=BASE_COST,
            executor=executor,
        )
        evidences[candidate.candidate_id] = evidence
        payloads[candidate.candidate_id] = _summary_payload(evidence, market_states)

    baseline = _volatility_candidate(
        "P4_BASELINE",
        "baseline",
        expansion_ratio=1.50,
        breaches=0,
        fraction=0.20,
    )
    evaluate(baseline)
    round1: list[ProfitCandidate] = []
    for expansion_ratio in (1.50, 1.75, 2.00):
        candidate = _volatility_candidate(
            f"P4_R1_V{int(expansion_ratio * 100)}_N3_F20",
            "round_1_expansion_ratio",
            expansion_ratio=expansion_ratio,
            breaches=3,
            fraction=0.20,
        )
        evaluate(candidate)
        round1.append(candidate)
    best_ratio = max(
        round1,
        key=lambda item: _development_score(
            payloads[baseline.candidate_id],
            payloads[item.candidate_id],
        ),
    )
    confirmation_candidates: list[ProfitCandidate] = []
    if args.max_rounds >= 2:
        for breaches in (3, 5, 10):
            candidate = _volatility_candidate(
                (
                    f"P4_R2_V{int(best_ratio.volatility_reduce_expansion_ratio * 100)}_"
                    f"N{breaches}_F20"
                ),
                "round_2_confirmation",
                expansion_ratio=best_ratio.volatility_reduce_expansion_ratio,
                breaches=breaches,
                fraction=0.20,
            )
            evaluate(candidate)
            confirmation_candidates.append(candidate)
    if not confirmation_candidates:
        confirmation_candidates = [best_ratio]
    best_confirmation = max(
        confirmation_candidates,
        key=lambda item: _development_score(
            payloads[baseline.candidate_id],
            payloads[item.candidate_id],
        ),
    )
    final_candidates = list(confirmation_candidates)
    if args.max_rounds >= 3:
        for fraction in (0.20, 0.35):
            candidate = _volatility_candidate(
                (
                    f"P4_R3_V{int(best_confirmation.volatility_reduce_expansion_ratio * 100)}_"
                    f"N{best_confirmation.volatility_reduce_after_breaches}_"
                    f"F{int(fraction * 100)}"
                ),
                "round_3_fraction",
                expansion_ratio=best_confirmation.volatility_reduce_expansion_ratio,
                breaches=best_confirmation.volatility_reduce_after_breaches,
                fraction=fraction,
            )
            evaluate(candidate)
            final_candidates.append(candidate)
    selected_candidate = max(
        final_candidates,
        key=lambda item: _development_score(
            payloads[baseline.candidate_id],
            payloads[item.candidate_id],
        ),
    )
    selected_id = selected_candidate.candidate_id
    print(f"STRESSING {selected_id} COST_50", flush=True)
    stress_evidence = _evaluate_candidate(
        selected_candidate,
        windows=windows,
        metadata=metadata,
        base_config=base_config,
        parameters=parameters,
        symbol_policies=symbol_policies,
        maker_policy=maker_policy,
        split_ids=split_ids,
        seeds=seeds,
        cost=COST_50,
        executor=executor,
    )
    executor.shutdown(wait=True, cancel_futures=False)
    stress_summary = _summary_payload(stress_evidence, market_states)
    stress_ok = _stress_passed(stress_summary)
    pytest_result = _run_pytest(repo_root, skip=args.skip_tests)
    checks = {
        candidate_id: _checks(
            payloads[baseline.candidate_id],
            payload,
            tests_passed=bool(pytest_result.get("passed")),
        )
        for candidate_id, payload in payloads.items()
        if candidate_id != baseline.candidate_id
    }
    result = _write_reports(
        (repo_root / args.report_dir).resolve(),
        metadata=metadata,
        split=split,
        windows=windows,
        market_states=market_states,
        payloads=payloads,
        evidences=evidences,
        selected_id=selected_id,
        checks=checks,
        stress_summary=stress_summary,
        stress_ok=stress_ok,
        pytest_result=pytest_result,
        seeds=seeds,
    )
    print(f"SELECTED {selected_id}")
    print(f"ROBUST_RESEARCH_CANDIDATE {result['robust_research_candidate']}")
    print(f"CONCLUSION {result['conclusion']}")
    print(f"REPORT_DIR {(repo_root / args.report_dir).resolve()}")


if __name__ == "__main__":
    main()
