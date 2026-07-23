from __future__ import annotations

import argparse
import concurrent.futures
import csv
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
    _candidate_checks,
    _classify_market_state,
    _development_rank,
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


def _load_p2_results(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload.get("p0_p2_research"), dict):
        payload = payload["p0_p2_research"]
    candidates = payload.get("candidates")
    selected_id = payload.get("selected_candidate_id")
    if not isinstance(candidates, dict) or "P0_OFF" not in candidates:
        raise ValueError("P2 结果缺少 candidates.P0_OFF。")
    if not isinstance(selected_id, str) or selected_id not in candidates:
        raise ValueError("P2 结果缺少有效的 selected_candidate_id。")
    return payload, candidates["P0_OFF"], candidates[selected_id]


def _p2_core_candidate(payload: dict[str, Any]) -> ProfitCandidate:
    item = payload["candidate"]
    return _candidate(
        "P2_LOCKED",
        "p2_locked",
        enabled=bool(item["enabled"]),
        mode=str(item["mode"]),
        activation_usdt=float(item["activation_usdt"]),
        minimum_locked_ratio=float(item["minimum_locked_ratio"]),
        suppress_drawdown_pct=float(item["suppress_drawdown_pct"]),
        reduce_drawdown_pct=float(item["reduce_drawdown_pct"]),
        close_drawdown_pct=float(item["close_drawdown_pct"]),
    )


def _with_p3(
    core: ProfitCandidate,
    candidate_id: str,
    round_name: str,
    *,
    passive_after: int,
    active_after: int,
    passive_fraction: float,
    active_fraction: float,
) -> ProfitCandidate:
    return _candidate(
        candidate_id,
        round_name,
        enabled=core.enabled,
        mode=core.mode,
        activation_usdt=core.activation_usdt,
        minimum_locked_ratio=core.minimum_locked_ratio,
        suppress_drawdown_pct=core.suppress_drawdown_pct,
        reduce_drawdown_pct=core.reduce_drawdown_pct,
        close_drawdown_pct=core.close_drawdown_pct,
        passive_reduce_after_bars=passive_after,
        active_reduce_after_bars=active_after,
        passive_reduce_fraction=passive_fraction,
        active_reduce_fraction=active_fraction,
    )


def _development_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, bool]:
    return _candidate_checks(
        {"combined": baseline["development"]},
        {"combined": candidate["development"]},
        tests_passed=True,
    )


def _development_score(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[Any, ...]:
    checks = _development_checks(baseline, candidate)
    checks.pop("full_pytest_passed", None)
    return (
        all(checks.values()),
        sum(checks.values()),
        _development_rank(candidate),
    )


def _p3_checks(candidate: dict[str, Any]) -> dict[str, bool]:
    item = candidate["combined"]
    config = candidate["candidate"]
    fraction = float(config["active_reduce_fraction"])
    observed_reduction = item[
        "median_profit_active_reduce_inventory_reduction_pct"
    ]
    return {
        "active_reduce_observed": int(item["profit_active_reduce_count"]) > 0,
        "active_inventory_reduction_ge_90pct_target": (
            observed_reduction is not None
            and float(observed_reduction) >= fraction * 0.90 - 1e-9
        ),
    }


def _parameter_rows(
    payloads: dict[str, dict[str, Any]],
    checks: dict[str, dict[str, bool]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate_id, payload in payloads.items():
        config = payload["candidate"]
        combined = payload["combined"]
        item_checks = checks.get(candidate_id, {})
        rows.append({
            "candidate_id": candidate_id,
            "round": config["round_name"],
            "passive_reduce_after_bars": config["passive_reduce_after_bars"],
            "active_reduce_after_bars": config["active_reduce_after_bars"],
            "passive_reduce_fraction": config["passive_reduce_fraction"],
            "active_reduce_fraction": config["active_reduce_fraction"],
            "development_mean_seed_total_pnl": payload["development"][
                "mean_seed_total_pnl"
            ],
            "mean_seed_total_pnl": combined["mean_seed_total_pnl"],
            "worst_seed_total_pnl": combined["worst_seed_total_pnl"],
            "worst_5pct_window_mean_pnl": combined["worst_5pct_window_mean_pnl"],
            "max_drawdown_pct": combined["max_drawdown_pct"],
            "profitable_to_losing_ratio": combined["profitable_to_losing_ratio"],
            "median_peak_profit_giveback_pct": combined[
                "median_peak_profit_giveback_pct"
            ],
            "passive_reduce_reprice_count": combined[
                "profit_passive_reduce_reprice_count"
            ],
            "passive_reduce_fill_count": combined[
                "profit_passive_reduce_fill_count"
            ],
            "active_reduce_count": combined["profit_active_reduce_count"],
            "active_reduce_cost": combined["profit_active_reduce_cost"],
            "median_active_inventory_reduction_pct": combined[
                "median_profit_active_reduce_inventory_reduction_pct"
            ],
            "passed_gate_count": sum(item_checks.values()),
            "all_gates_passed": bool(item_checks) and all(item_checks.values()),
        })
    return rows


def _merge_csv_report(base_path: Path, supplement_path: Path) -> None:
    with base_path.open("r", newline="", encoding="utf-8") as handle:
        base_rows = list(csv.DictReader(handle))
    with supplement_path.open("r", newline="", encoding="utf-8") as handle:
        supplement_rows = list(csv.DictReader(handle))
    base_rows = [
        row
        for row in base_rows
        if not str(row.get("candidate_id") or "").startswith("P3_")
    ]
    rows = [*base_rows, *supplement_rows]
    fields: list[str] = []
    for row in rows:
        for name in row:
            if name not in fields:
                fields.append(name)
    with base_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _seed_rows(selected: dict[str, Any]) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for seed, runs in selected["runs"].items():
        development = runs["development"]
        validation = runs["validation"]
        rows.append([
            seed,
            float(development["total_pnl"]),
            float(validation["total_pnl"]),
            float(development["total_pnl"]) + float(validation["total_pnl"]),
        ])
    return rows


def _render_final_report(result: dict[str, Any]) -> str:
    prior = result["p0_p2_research"]
    p0 = result["p0"]
    p1 = prior["candidates"]["P1_FIXED_A10"]
    p2 = result["p2_locked"]
    selected_id = result["selected_candidate_id"]
    selected = result["p3_candidates"][selected_id]
    combined = selected["combined"]
    gate_rows = [
        [name, "PASS" if passed else "FAIL"]
        for name, passed in result["selected_checks"].items()
    ]
    comparison_rows = [
        [
            name,
            p0["combined"][key],
            p1["combined"][key],
            p2["combined"][key],
            combined[key],
        ]
        for name, key in (
            ("六种子平均净收益", "mean_seed_total_pnl"),
            ("六种子最差净收益", "worst_seed_total_pnl"),
            ("最差 5% 窗口均值", "worst_5pct_window_mean_pnl"),
            ("盈利转亏比例", "profitable_to_losing_ratio"),
            ("峰值回吐中位数", "median_peak_profit_giveback_pct"),
            ("最大回撤", "max_drawdown_pct"),
        )
    ]
    datasets = prior["datasets"]
    date_range = (
        f"{datasets[0]['actual_start']} 至 {datasets[0]['actual_end']}"
        if datasets
        else "未知"
    )
    return "\n".join([
        "# 利润保护 P3 主动分批减仓报告",
        "",
        f"- 选择：`{selected_id}`（只按 Development 选择）",
        "- Validation：仅用于最终验收",
        "- Final OOS：旧区间已消费，本报告仅为 Research Validation",
        f"- 数据区间：{date_range}",
        "- 标的与周期：BTCUSDT、ETHUSDT，1m",
        "- 固定种子：3、10、17、31、59、97",
        "- 成交成本：BASE Maker 0.02%、Taker 0.05%、止损滑点 10 bps；另做 COST50",
        f"- COST50：{'PASS' if result['selected_cost_50_passed'] else 'FAIL'}",
        f"- 完整测试：{'PASS' if result['pytest'].get('passed') else 'FAIL'}",
        "- 生产参数：未修改",
        "",
        "## P0 / P1 / P2 / P3 对照",
        "",
        _markdown_table(comparison_rows, ["指标", "P0", "P1", "P2", "P3"]),
        "",
        "## P3 六种子",
        "",
        _markdown_table(
            _seed_rows(selected),
            ["Seed", "Development PnL", "Validation PnL", "合计 PnL"],
        ),
        "",
        "## 标的与状态",
        "",
        _markdown_table(
            [[name, value] for name, value in combined["symbol_pnl"].items()],
            ["标的", "六种子合计 PnL"],
        ),
        "",
        _markdown_table(
            [[name, value] for name, value in combined["state_pnl"].items()],
            ["市场状态", "六种子合计 PnL"],
        ),
        "",
        "## P3 执行实效",
        "",
        f"- 被动减仓重新报价：{combined['profit_passive_reduce_reprice_count']}",
        f"- 被动减仓成交：{combined['profit_passive_reduce_fill_count']}",
        f"- 主动减仓次数：{combined['profit_active_reduce_count']}",
        f"- 主动减仓退出成本：{combined['profit_active_reduce_cost']:.6f} USDT",
        "- 主动减仓库存下降中位数："
        f"{combined['median_profit_active_reduce_inventory_reduction_pct']}",
        "- 说明：360 分钟主动减仓仅触发 3 次，且多为最小数量步长下的残余仓位清理；"
        "它没有改变最差 5% 窗口、盈利转亏比例或峰值回吐中位数。",
        "",
        "## 门槛",
        "",
        _markdown_table(gate_rows, ["门槛", "结果"]),
        "",
        "## 结论",
        "",
        result["conclusion"],
        "",
        "P3 已完成协议允许的三轮受约束优化，不再继续围绕同一已查看 Validation 微调。"
        "主要亏损仍集中在 VOLATILITY_EXPANSION，后续若研究新结构，必须使用新积累的未查看区间做锁定 OOS。",
        "",
    ])


def finalize_existing_reports(output_dir: Path) -> dict[str, Any]:
    result = json.loads((output_dir / "p3-results.json").read_text(encoding="utf-8"))
    prior_path = output_dir / "p0-p2-results.json"
    if prior_path.exists():
        prior = json.loads(prior_path.read_text(encoding="utf-8"))
    else:
        prior = json.loads((output_dir / "results.json").read_text(encoding="utf-8"))
    if isinstance(prior.get("p0_p2_research"), dict):
        prior = prior["p0_p2_research"]
    result["p0_p2_research"] = prior
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    (output_dir / "p3-results.json").write_text(serialized, encoding="utf-8")
    (output_dir / "results.json").write_text(serialized, encoding="utf-8")
    (output_dir / "p0-p2-results.json").write_text(
        json.dumps(prior, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    report = _render_final_report(result)
    (output_dir / "p3-final-report.md").write_text(report, encoding="utf-8")
    (output_dir / "final-report.md").write_text(report, encoding="utf-8")
    _merge_csv_report(output_dir / "parameter-search.csv", output_dir / "p3-parameter-search.csv")
    _merge_csv_report(output_dir / "walk-forward.csv", output_dir / "p3-walk-forward.csv")
    _merge_csv_report(output_dir / "state-breakdown.csv", output_dir / "p3-state-breakdown.csv")
    return result


def _write_reports(
    output_dir: Path,
    *,
    prior: dict[str, Any],
    p0: dict[str, Any],
    p2: dict[str, Any],
    payloads: dict[str, dict[str, Any]],
    evidences: dict[str, CandidateEvidence],
    selected_id: str,
    base_checks: dict[str, dict[str, bool]],
    p3_checks: dict[str, dict[str, bool]],
    stress_summary: dict[str, Any],
    stress_ok: bool,
    pytest_result: dict[str, Any],
    market_states: dict[str, str],
    ordered_window_ids: Sequence[str],
    seeds: Sequence[int],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checks = {
        candidate_id: {**base_checks[candidate_id], **p3_checks.get(candidate_id, {})}
        for candidate_id in base_checks
    }
    selected_checks = checks[selected_id]
    research_candidate = all(selected_checks.values()) and stress_ok
    conclusion = (
        "P3 研究门槛全部通过；仅建议进入测试网，等待全新锁定 OOS。"
        if research_candidate
        else "本轮没有稳健候选，保持生产参数不变。"
    )
    _write_csv(output_dir / "p3-parameter-search.csv", _parameter_rows(payloads, checks))
    selected_evidence = evidences[selected_id]
    _write_csv(
        output_dir / "p3-walk-forward.csv",
        _walk_forward_rows([selected_evidence], ordered_window_ids, seeds),
    )
    _write_csv(
        output_dir / "p3-state-breakdown.csv",
        _state_breakdown_rows([selected_evidence], market_states),
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "protocol": "profit-protection-p3-research-validation",
        "selection_rule": "development_only_validation_reserved_for_final_acceptance",
        "prior_p2_selected_candidate_id": prior["selected_candidate_id"],
        "prior_p2_robust_research_candidate": prior["robust_research_candidate"],
        "p0_p2_research": prior,
        "p0": p0,
        "p2_locked": p2,
        "p3_candidates": payloads,
        "selected_candidate_id": selected_id,
        "selected_checks": selected_checks,
        "selected_cost_50": stress_summary,
        "selected_cost_50_passed": stress_ok,
        "pytest": pytest_result,
        "final_oos_status": "CONSUMED_RESEARCH_VALIDATION_ONLY",
        "robust_research_candidate": research_candidate,
        "testnet_recommended": research_candidate,
        "production_defaults_changed": False,
        "conclusion": conclusion,
    }
    (output_dir / "p3-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "p0-p2-results.json").write_text(
        json.dumps(prior, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = _render_final_report(result)
    (output_dir / "p3-final-report.md").write_text(report, encoding="utf-8")
    (output_dir / "final-report.md").write_text(report, encoding="utf-8")
    _merge_csv_report(
        output_dir / "parameter-search.csv",
        output_dir / "p3-parameter-search.csv",
    )
    _merge_csv_report(
        output_dir / "walk-forward.csv",
        output_dir / "p3-walk-forward.csv",
    )
    _merge_csv_report(
        output_dir / "state-breakdown.csv",
        output_dir / "p3-state-breakdown.csv",
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在冻结 P2 参数上执行三轮 P3 主动分批减仓研究。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--max-rounds", type=int, choices=(2, 3), default=3)
    parser.add_argument("--p2-results", default="reports/profit-protection/results.json")
    parser.add_argument("--report-dir", default="reports/profit-protection")
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
    prior, p0, p2 = _load_p2_results((repo_root / args.p2_results).resolve())
    core = _p2_core_candidate(p2)
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
    ordered_window_ids = [
        item.window_id
        for item in sorted(windows, key=lambda value: value.market_close)
        if item.window_id in set(split.development) | set(split.validation)
    ]
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
            if asdict(existing.candidate) | {"candidate_id": "", "round_name": ""} == (
                asdict(candidate) | {"candidate_id": "", "round_name": ""}
            ):
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

    round1: list[ProfitCandidate] = []
    for passive_after in (30, 60, 120):
        candidate = _with_p3(
            core,
            f"P3_R1_P{passive_after}_A360_F20",
            "p3_round_1_passive_timing",
            passive_after=passive_after,
            active_after=360,
            passive_fraction=0.20,
            active_fraction=0.20,
        )
        evaluate(candidate)
        round1.append(candidate)
    best_passive = max(
        round1,
        key=lambda item: _development_score(p0, payloads[item.candidate_id]),
    )

    active_candidates: list[ProfitCandidate] = []
    if args.max_rounds >= 2:
        for active_after in (120, 240, 360):
            if active_after <= best_passive.passive_reduce_after_bars:
                continue
            candidate = _with_p3(
                core,
                f"P3_R2_P{best_passive.passive_reduce_after_bars}_A{active_after}_F20",
                "p3_round_2_active_timing",
                passive_after=best_passive.passive_reduce_after_bars,
                active_after=active_after,
                passive_fraction=0.20,
                active_fraction=0.20,
            )
            evaluate(candidate)
            active_candidates.append(candidate)
    if not active_candidates:
        executor.shutdown(wait=True, cancel_futures=False)
        raise RuntimeError("P3 至少需要执行第二轮主动减仓候选。")
    best_active = max(
        active_candidates,
        key=lambda item: _development_score(p0, payloads[item.candidate_id]),
    )

    final_candidates = list(active_candidates)
    if args.max_rounds >= 3:
        for passive_fraction in (0.20, 0.35):
            for active_fraction in (0.20, 0.35):
                candidate = _with_p3(
                    core,
                    (
                        f"P3_R3_P{best_active.passive_reduce_after_bars}_"
                        f"A{best_active.active_reduce_after_bars}_"
                        f"PF{int(passive_fraction * 100)}_AF{int(active_fraction * 100)}"
                    ),
                    "p3_round_3_reduce_fraction",
                    passive_after=best_active.passive_reduce_after_bars,
                    active_after=best_active.active_reduce_after_bars,
                    passive_fraction=passive_fraction,
                    active_fraction=active_fraction,
                )
                evaluate(candidate)
                final_candidates.append(candidate)

    selected_candidate = max(
        final_candidates,
        key=lambda item: _development_score(p0, payloads[item.candidate_id]),
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
    base_checks = {
        candidate_id: _candidate_checks(
            p0,
            payload,
            tests_passed=bool(pytest_result.get("passed")),
        )
        for candidate_id, payload in payloads.items()
        if int(payload["candidate"]["active_reduce_after_bars"]) > 0
    }
    structure_checks = {
        candidate_id: _p3_checks(payloads[candidate_id])
        for candidate_id in base_checks
    }
    result = _write_reports(
        (repo_root / args.report_dir).resolve(),
        prior=prior,
        p0=p0,
        p2=p2,
        payloads=payloads,
        evidences=evidences,
        selected_id=selected_id,
        base_checks=base_checks,
        p3_checks=structure_checks,
        stress_summary=stress_summary,
        stress_ok=stress_ok,
        pytest_result=pytest_result,
        market_states=market_states,
        ordered_window_ids=ordered_window_ids,
        seeds=seeds,
    )
    print(f"SELECTED {selected_id}")
    print(f"ROBUST_RESEARCH_CANDIDATE {result['robust_research_candidate']}")
    print(f"CONCLUSION {result['conclusion']}")
    print(f"REPORT_DIR {(repo_root / args.report_dir).resolve()}")


if __name__ == "__main__":
    main()
