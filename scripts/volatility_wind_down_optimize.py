from __future__ import annotations

import argparse
import concurrent.futures
import os
from pathlib import Path

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
    _run_pytest,
    _stress_passed,
    _summary_payload,
)
from scripts.robustness import split_window_ids
from scripts.volatility_defense_optimize import _checks, _volatility_candidate
from scripts.volatility_worst_side_optimize import _load_p4, _write_reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 P4 参数，仅验证减仓后窗口内只减不增。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--p4-results", default="reports/volatility-defense/results.json")
    parser.add_argument("--report-dir", default="reports/volatility-wind-down")
    parser.add_argument("--candidate-id", default="P7_WIND_DOWN")
    parser.add_argument("--expansion-ratio", type=float)
    parser.add_argument("--breaches", type=int)
    parser.add_argument("--fraction", type=float)
    parser.add_argument("--resume-after-normal-bars", type=int, default=0)
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
    expansion_ratio = (
        float(args.expansion_ratio)
        if args.expansion_ratio is not None
        else float(p4_candidate["volatility_reduce_expansion_ratio"])
    )
    breaches = (
        int(args.breaches)
        if args.breaches is not None
        else int(p4_candidate["volatility_reduce_after_breaches"])
    )
    fraction = (
        float(args.fraction)
        if args.fraction is not None
        else float(p4_candidate["volatility_reduce_fraction"])
    )
    stage_label = args.candidate_id.split("_", 1)[0]
    candidate = _volatility_candidate(
        args.candidate_id,
        f"{stage_label.lower()}_structure",
        expansion_ratio=expansion_ratio,
        breaches=breaches,
        fraction=fraction,
        mode="BOTH",
        wind_down_after_reduce=True,
        resume_after_normal_bars=args.resume_after_normal_bars,
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
    print(f"EVALUATING {args.candidate_id}", flush=True)
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
    print(f"STRESSING {args.candidate_id} COST_50", flush=True)
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
    included_ids = set(split.development) | set(split.validation)
    ordered_window_ids = [
        item.window_id
        for item in sorted(windows, key=lambda value: value.market_close)
        if item.window_id in included_ids
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
        protocol="causal-volatility-wind-down-research-validation",
        title=f"# {stage_label} 波动减仓后只减不增报告",
        structure_summary=(
            f"V{expansion_ratio:.2f}/N{breaches}/F{fraction:.0%}/BOTH；"
            "减仓成功后只允许 REDUCE 单；"
            + (
                f"连续 {args.resume_after_normal_bars} 根正常 Bar 后恢复"
                if args.resume_after_normal_bars > 0
                else "窗口内不恢复"
            )
        ),
        stage_label=stage_label,
        lookahead_policy=(
            "扩张信号仅使用当前 Bar 之前 60 根已闭合 K 线；"
            "减仓成功后立即撤销 OPEN 单，窗口内不再恢复。"
        ),
        candidate_column=args.candidate_id.replace("_", " "),
    )
    print(f"ROBUST_RESEARCH_CANDIDATE {result['robust_research_candidate']}")
    print(f"CONCLUSION {result['conclusion']}")
    print(f"REPORT_DIR {(repo_root / args.report_dir).resolve()}")


if __name__ == "__main__":
    main()
