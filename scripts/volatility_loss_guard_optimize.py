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
        description="冻结 P4 参数，仅验证亏损状态波动减仓保护。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--p4-results", default="reports/volatility-defense/results.json")
    parser.add_argument("--report-dir", default="reports/volatility-loss-guard")
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
        "P6_LOSS_GUARD",
        "p6_structure",
        expansion_ratio=float(p4_candidate["volatility_reduce_expansion_ratio"]),
        breaches=int(p4_candidate["volatility_reduce_after_breaches"]),
        fraction=float(p4_candidate["volatility_reduce_fraction"]),
        mode="BOTH",
        only_when_losing=True,
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
    print("EVALUATING P6_LOSS_GUARD", flush=True)
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
    print("STRESSING P6_LOSS_GUARD COST_50", flush=True)
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
        protocol="causal-volatility-loss-guard-research-validation",
        title="# P6 仅亏损状态波动减仓报告",
        structure_summary=(
            "冻结 P4 的 V1.50/N10/F20/BOTH，仅在触发开盘时净 PnL 为负才减仓"
        ),
        stage_label="P6",
        lookahead_policy=(
            "扩张信号仅使用当前 Bar 之前 60 根已闭合 K 线；"
            "净 PnL 保护只使用触发开盘价、既有已实现 PnL 与既有 lot 成本。"
        ),
        candidate_column="P6 LOSS_GUARD",
    )
    print(f"ROBUST_RESEARCH_CANDIDATE {result['robust_research_candidate']}")
    print(f"CONCLUSION {result['conclusion']}")
    print(f"REPORT_DIR {(repo_root / args.report_dir).resolve()}")


if __name__ == "__main__":
    main()
