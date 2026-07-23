from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    _base_research_config,
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
from scripts.volatility_defense_optimize import _checks, _volatility_candidate


UTC = timezone.utc


def _load_p4(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    selected_id = payload.get("selected_candidate_id")
    candidates = payload.get("candidates")
    if not isinstance(candidates, dict) or "P4_BASELINE" not in candidates:
        raise ValueError("P4 结果缺少基线。")
    if not isinstance(selected_id, str) or selected_id not in candidates:
        raise ValueError("P4 结果缺少有效选中候选。")
    return payload


def _write_reports(
    output_dir: Path,
    *,
    p4: dict[str, Any],
    candidate_payload: dict[str, Any],
    evidence: Any,
    stress_summary: dict[str, Any],
    stress_ok: bool,
    checks: dict[str, bool],
    pytest_result: dict[str, Any],
    market_states: dict[str, str],
    ordered_window_ids: Sequence[str],
    seeds: Sequence[int],
    protocol: str,
    title: str,
    structure_summary: str,
    stage_label: str,
    lookahead_policy: str,
    candidate_column: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    robust = all(checks.values()) and stress_ok
    conclusion = (
        f"{stage_label} 研究门槛全部通过；建议进入测试网并等待全新锁定 OOS。"
        if robust
        else "本轮没有稳健候选，保持生产参数不变。"
    )
    candidate = candidate_payload["candidate"]
    combined = candidate_payload["combined"]
    parameter_row = {
        "candidate_id": candidate["candidate_id"],
        "expansion_ratio": candidate["volatility_reduce_expansion_ratio"],
        "breaches": candidate["volatility_reduce_after_breaches"],
        "fraction": candidate["volatility_reduce_fraction"],
        "mode": candidate["volatility_reduce_mode"],
        "mean_seed_total_pnl": combined["mean_seed_total_pnl"],
        "worst_seed_total_pnl": combined["worst_seed_total_pnl"],
        "worst_5pct_window_mean_pnl": combined["worst_5pct_window_mean_pnl"],
        "max_drawdown_pct": combined["max_drawdown_pct"],
        "worst_best_window_concentration": combined[
            "worst_best_window_concentration"
        ],
        "volatility_expansion_pnl": combined["state_pnl"].get(
            "VOLATILITY_EXPANSION",
            0.0,
        ),
        "range_pnl": combined["state_pnl"].get("RANGE", 0.0),
        "volatility_reduce_count": combined["volatility_reduce_count"],
        "volatility_reduce_pnl": combined["volatility_reduce_pnl"],
        "volatility_reduce_cost": combined["volatility_reduce_cost"],
        "all_gates_passed": all(checks.values()),
    }
    _write_csv(output_dir / "parameter-search.csv", [parameter_row])
    _write_csv(
        output_dir / "walk-forward.csv",
        _walk_forward_rows([evidence], ordered_window_ids, seeds),
    )
    _write_csv(
        output_dir / "state-breakdown.csv",
        _state_breakdown_rows([evidence], market_states),
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "protocol": protocol,
        "selection_rule": "single_pre_registered_structure_no_validation_selection",
        "lookahead_policy": lookahead_policy,
        "p4_research": p4,
        "candidate": candidate_payload,
        "checks": checks,
        "cost_50": stress_summary,
        "cost_50_passed": stress_ok,
        "pytest": pytest_result,
        "final_oos_status": "CONSUMED_RESEARCH_VALIDATION_ONLY",
        "robust_research_candidate": robust,
        "testnet_recommended": robust,
        "production_defaults_changed": False,
        "conclusion": conclusion,
    }
    (output_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    baseline = p4["candidates"]["P4_BASELINE"]["combined"]
    p4_selected = p4["candidates"][p4["selected_candidate_id"]]["combined"]
    comparison = [
        [name, baseline[key], p4_selected[key], combined[key]]
        for name, key in (
            ("六种子平均净收益", "mean_seed_total_pnl"),
            ("六种子最差净收益", "worst_seed_total_pnl"),
            ("最差 5% 窗口均值", "worst_5pct_window_mean_pnl"),
            ("最大回撤", "max_drawdown_pct"),
            ("最坏窗口集中度", "worst_best_window_concentration"),
            ("费用/毛利润", "fee_to_gross_profit_ratio"),
        )
    ]
    comparison.extend([
        ["RANGE PnL", baseline["state_pnl"].get("RANGE", 0.0), p4_selected["state_pnl"].get("RANGE", 0.0), combined["state_pnl"].get("RANGE", 0.0)],
        ["VOLATILITY_EXPANSION PnL", baseline["state_pnl"].get("VOLATILITY_EXPANSION", 0.0), p4_selected["state_pnl"].get("VOLATILITY_EXPANSION", 0.0), combined["state_pnl"].get("VOLATILITY_EXPANSION", 0.0)],
        ["波动减仓 PnL", 0.0, p4_selected["volatility_reduce_pnl"], combined["volatility_reduce_pnl"]],
        ["波动减仓成本", 0.0, p4_selected["volatility_reduce_cost"], combined["volatility_reduce_cost"]],
    ])
    report = "\n".join([
        title,
        "",
        f"- 结构：{structure_summary}",
        "- 选择：单一预注册结构；Validation 不参与选择",
        "- Final OOS：旧区间已消费，仅为 Research Validation",
        f"- COST50：{'PASS' if stress_ok else 'FAIL'}",
        f"- 完整测试：{'PASS' if pytest_result.get('passed') else 'FAIL'}",
        "- 生产参数：未修改",
        "",
        "## 对照",
        "",
        _markdown_table(comparison, ["指标", "基线", "P4 BOTH", candidate_column]),
        "",
        "## 门槛",
        "",
        _markdown_table(
            [[name, "PASS" if passed else "FAIL"] for name, passed in checks.items()],
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
        description="冻结 P4 参数，仅验证最差持仓侧减仓结构。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--p4-results", default="reports/volatility-defense/results.json")
    parser.add_argument("--report-dir", default="reports/volatility-worst-side")
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
    p4 = _load_p4((repo_root / args.p4_results).resolve())
    p4_candidate = p4["candidates"][p4["selected_candidate_id"]]["candidate"]
    candidate = _volatility_candidate(
        "P5_WORST_SIDE",
        "p5_structure",
        expansion_ratio=float(p4_candidate["volatility_reduce_expansion_ratio"]),
        breaches=int(p4_candidate["volatility_reduce_after_breaches"]),
        fraction=float(p4_candidate["volatility_reduce_fraction"]),
        mode="WORST_SIDE",
    )
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
    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=_initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    )
    print("EVALUATING P5_WORST_SIDE", flush=True)
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
    payload = _summary_payload(evidence, market_states)
    print("STRESSING P5_WORST_SIDE COST_50", flush=True)
    stress_evidence = _evaluate_candidate(
        candidate,
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
    checks = _checks(
        p4["candidates"]["P4_BASELINE"],
        payload,
        tests_passed=bool(pytest_result.get("passed")),
    )
    ordered_window_ids = [
        item.window_id
        for item in sorted(windows, key=lambda value: value.market_close)
        if item.window_id in set(split.development) | set(split.validation)
    ]
    result = _write_reports(
        (repo_root / args.report_dir).resolve(),
        p4=p4,
        candidate_payload=payload,
        evidence=evidence,
        stress_summary=stress_summary,
        stress_ok=stress_ok,
        checks=checks,
        pytest_result=pytest_result,
        market_states=market_states,
        ordered_window_ids=ordered_window_ids,
        seeds=seeds,
        protocol="causal-volatility-worst-side-research-validation",
        title="# P5 波动扩张最差持仓侧减仓报告",
        structure_summary="冻结 P4 的 V1.50/N10/F20，仅把 BOTH 改为 WORST_SIDE",
        stage_label="P5",
        lookahead_policy=(
            "扩张信号仅使用当前 Bar 之前 60 根已闭合 K 线；"
            "触发时仅根据当前开盘价与既有 lot 成本选择未实现亏损更差的一侧。"
        ),
        candidate_column="P5 WORST_SIDE",
    )
    print(f"ROBUST_RESEARCH_CANDIDATE {result['robust_research_candidate']}")
    print(f"CONCLUSION {result['conclusion']}")
    print(f"REPORT_DIR {(repo_root / args.report_dir).resolve()}")


if __name__ == "__main__":
    main()
