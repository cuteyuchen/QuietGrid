from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
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
    _summarize,
)
from scripts.robustness import split_window_ids


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-historical-oos-20260722"


def _registered_candidates() -> tuple[ProfitCandidate, ...]:
    baseline = _candidate(
        "X0_BASELINE",
        "control",
        enabled=False,
        mode="OFF",
    )
    p3 = _candidate(
        "X1_P3_ACTIVE_PROFIT",
        "registered_structure",
        activation_usdt=2.0,
        close_drawdown_pct=0.40,
        passive_reduce_after_bars=30,
        active_reduce_after_bars=360,
        passive_reduce_fraction=0.35,
        active_reduce_fraction=0.20,
    )
    p4 = _candidate(
        "X2_P4_VOLATILITY",
        "registered_structure",
        enabled=False,
        mode="OFF",
        volatility_reduce_expansion_ratio=1.50,
        volatility_reduce_after_breaches=10,
        volatility_reduce_fraction=0.20,
    )
    p9 = _candidate(
        "X3_P9_EVENT_FREEZE",
        "registered_structure",
        enabled=False,
        mode="OFF",
        volatility_reduce_expansion_ratio=1.75,
        volatility_reduce_after_breaches=3,
        volatility_reduce_fraction=0.20,
        volatility_wind_down_after_reduce=True,
        volatility_resume_after_normal_bars=10,
    )
    p3_p4 = _candidate(
        "X4_P3_PLUS_P4",
        "registered_structure",
        activation_usdt=2.0,
        close_drawdown_pct=0.40,
        passive_reduce_after_bars=30,
        active_reduce_after_bars=360,
        passive_reduce_fraction=0.35,
        active_reduce_fraction=0.20,
        volatility_reduce_expansion_ratio=1.50,
        volatility_reduce_after_breaches=10,
        volatility_reduce_fraction=0.20,
    )
    p3_p9 = _candidate(
        "X5_P3_PLUS_P9",
        "registered_structure",
        activation_usdt=2.0,
        close_drawdown_pct=0.40,
        passive_reduce_after_bars=30,
        active_reduce_after_bars=360,
        passive_reduce_fraction=0.35,
        active_reduce_fraction=0.20,
        volatility_reduce_expansion_ratio=1.75,
        volatility_reduce_after_breaches=3,
        volatility_reduce_fraction=0.20,
        volatility_wind_down_after_reduce=True,
        volatility_resume_after_normal_bars=10,
    )
    return baseline, p3, p4, p9, p3_p4, p3_p9


def _relative_improvement(baseline: float, candidate: float) -> float:
    if baseline < 0:
        return (candidate - baseline) / abs(baseline)
    return 1.0 if candidate >= baseline else -1.0


def _retention(baseline: float, candidate: float) -> float:
    if baseline > 0:
        return candidate / baseline
    return 1.0 if candidate > 0 else -1.0


def _fee_ratio_ok(baseline: Any, candidate: Any, multiplier: float = 1.25) -> bool:
    if candidate is None:
        return False
    if baseline is None:
        return True
    return float(candidate) <= float(baseline) * multiplier


def _development_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    seed_count: int,
) -> dict[str, bool]:
    base_range = float(baseline["state_pnl"].get("RANGE", 0.0))
    candidate_range = float(candidate["state_pnl"].get("RANGE", 0.0))
    return {
        "all_seeds_positive": int(candidate["positive_seed_count"]) == seed_count,
        "both_symbols_positive": all(
            float(candidate["symbol_pnl"].get(symbol, 0.0)) > 0
            for symbol in ("BTCUSDT", "ETHUSDT")
        ),
        "worst_5pct_improvement_ge_10pct": _relative_improvement(
            float(baseline["worst_5pct_window_mean_pnl"]),
            float(candidate["worst_5pct_window_mean_pnl"]),
        )
        >= 0.10,
        "max_drawdown_not_worse_than_5pct": float(candidate["max_drawdown_pct"])
        <= float(baseline["max_drawdown_pct"]) * 1.05,
        "mean_pnl_retention_ge_75pct": _retention(
            float(baseline["mean_seed_total_pnl"]),
            float(candidate["mean_seed_total_pnl"]),
        )
        >= 0.75,
        "range_profit_retention_ge_70pct": _retention(base_range, candidate_range)
        >= 0.70,
        "fee_ratio_le_125pct_baseline": _fee_ratio_ok(
            baseline["fee_to_gross_profit_ratio"],
            candidate["fee_to_gross_profit_ratio"],
        ),
        "best_window_concentration_le_35pct": float(
            candidate["worst_best_window_concentration"]
        )
        <= 0.35,
    }


def _selection_key(
    candidate_id: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[float, float, float, float, str]:
    return (
        -_relative_improvement(
            float(baseline["worst_5pct_window_mean_pnl"]),
            float(candidate["worst_5pct_window_mean_pnl"]),
        ),
        float(candidate["max_drawdown_pct"]),
        -float(candidate["worst_seed_total_pnl"]),
        -float(candidate["mean_seed_total_pnl"]),
        candidate_id,
    )


def _select_candidate(
    baseline: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    checks: dict[str, dict[str, bool]],
) -> str | None:
    eligible = [
        candidate_id
        for candidate_id, item_checks in checks.items()
        if item_checks and all(item_checks.values())
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate_id: _selection_key(
            candidate_id,
            baseline,
            candidates[candidate_id],
        ),
    )


def _scenario_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    seed_count: int,
) -> dict[str, bool]:
    return {
        "all_seeds_positive": int(candidate["positive_seed_count"]) == seed_count,
        "both_symbols_positive": all(
            float(candidate["symbol_pnl"].get(symbol, 0.0)) > 0
            for symbol in ("BTCUSDT", "ETHUSDT")
        ),
        "all_seed_profit_factors_gt_1": float(
            candidate["minimum_seed_profit_factor"]
        )
        > 1.0,
        "max_drawdown_le_5pct": float(candidate["max_drawdown_pct"]) <= 0.05,
        "max_drawdown_not_worse_than_5pct": float(candidate["max_drawdown_pct"])
        <= float(baseline["max_drawdown_pct"]) * 1.05,
        "best_window_concentration_le_35pct": float(
            candidate["worst_best_window_concentration"]
        )
        <= 0.35,
        "worst_5pct_not_worse_than_baseline": float(
            candidate["worst_5pct_window_mean_pnl"]
        )
        >= float(baseline["worst_5pct_window_mean_pnl"]),
        "mean_pnl_retention_ge_75pct": _retention(
            float(baseline["mean_seed_total_pnl"]),
            float(candidate["mean_seed_total_pnl"]),
        )
        >= 0.75,
    }


def _evidence_payload(
    evidence: CandidateEvidence,
    split_name: str,
    market_states: dict[str, str],
) -> dict[str, Any]:
    summary = _summarize(evidence, {split_name}, market_states)
    seed_profit_factors = {
        str(seed): (
            float(metrics.profit_factor)
            if metrics.profit_factor is not None
            else 0.0
        )
        for seed, seed_runs in evidence.runs.items()
        for name, (metrics, _results) in seed_runs.items()
        if name == split_name
    }
    summary["seed_profit_factor"] = seed_profit_factors
    summary["minimum_seed_profit_factor"] = min(
        seed_profit_factors.values(),
        default=0.0,
    )
    return {
        "candidate": asdict(evidence.candidate),
        "summary": summary,
        "runs": {
            str(seed): asdict(seed_runs[split_name][0])
            for seed, seed_runs in evidence.runs.items()
        },
    }


def _dataset_brief(
    manifests: Sequence[str],
    metadata: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for manifest, item in zip(manifests, metadata):
        path = Path(manifest).resolve()
        result.append({
            "manifest": str(path),
            "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "dataset_id": item.get("dataset_id"),
            "symbol": item.get("symbol"),
            "interval": item.get("interval"),
            "actual_start": item.get("actual_start"),
            "actual_end": item.get("actual_end"),
            "row_count": item.get("row_count"),
            "duplicate_rows": item.get("duplicate_rows"),
            "missing_intervals": item.get("missing_intervals"),
            "official_checksums_verified": item.get("official_checksums_verified"),
            "file_sha256": item.get("file_sha256"),
        })
    return sorted(result, key=lambda item: str(item["symbol"]))


def _protocol_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _screen_markdown(payload: dict[str, Any]) -> str:
    rows = []
    for candidate_id, item in payload["candidates"].items():
        summary = item["summary"]
        item_checks = payload["checks"].get(candidate_id, {})
        rows.append((
            candidate_id,
            f"{summary['mean_seed_total_pnl']:.4f}",
            f"{summary['worst_seed_total_pnl']:.4f}",
            f"{summary['worst_5pct_window_mean_pnl']:.4f}",
            f"{summary['max_drawdown_pct']:.2%}",
            f"{summary['worst_best_window_concentration']:.2%}",
            "CONTROL" if not item_checks else f"{sum(item_checks.values())}/{len(item_checks)}",
        ))
    conclusion = (
        f"Development 选中 `{payload['selected_candidate_id']}`；允许进入 Validation。"
        if payload["selected_candidate_id"]
        else "本轮没有 Development 合格候选；Validation 与 Final OOS 保持封存。"
    )
    return "\n".join([
        "# 跨周期历史 OOS Development 筛选",
        "",
        f"- 协议：`{payload['protocol']}`",
        f"- Development 窗口：{payload['split']['development_count']}",
        f"- Validation：`{payload['split']['validation_status']}`",
        f"- Final OOS：`{payload['split']['final_oos_status']}`",
        "",
        _markdown_table(
            ("候选", "平均 PnL", "最差种子", "最差 5%", "最大回撤", "集中度", "门槛"),
            rows,
        ),
        "",
        f"结论：{conclusion}",
        "",
    ])


def _stage_markdown(payload: dict[str, Any], title: str) -> str:
    rows = []
    for scenario in ("BASE", "COST50"):
        baseline = payload["scenarios"][scenario]["baseline"]["summary"]
        candidate = payload["scenarios"][scenario]["candidate"]["summary"]
        checks = payload["checks"][scenario]
        rows.append((
            scenario,
            f"{baseline['mean_seed_total_pnl']:.4f}",
            f"{candidate['mean_seed_total_pnl']:.4f}",
            f"{candidate['worst_seed_total_pnl']:.4f}",
            f"{candidate['minimum_seed_profit_factor']:.3f}",
            f"{candidate['max_drawdown_pct']:.2%}",
            f"{candidate['worst_best_window_concentration']:.2%}",
            f"{sum(checks.values())}/{len(checks)}",
        ))
    return "\n".join([
        f"# {title}",
        "",
        f"- 候选：`{payload['selected_candidate_id']}`",
        f"- 本阶段通过：`{payload['stage_passed']}`",
        f"- Final OOS：`{payload['final_oos_status']}`",
        "",
        _markdown_table(
            ("场景", "基线平均", "候选平均", "候选最差", "最小 PF", "最大回撤", "集中度", "门槛"),
            rows,
        ),
        "",
        f"结论：{payload['conclusion']}",
        "",
    ])


def _load_research_state(
    manifests: Sequence[str],
) -> tuple[Any, list[dict[str, Any]], list[Any], Any]:
    base_config = _base_research_config()
    metadata, windows = _load_data(manifests, base_config)
    split = split_window_ids(
        windows,
        dev_ratio=base_config.dev_ratio,
        validation_ratio=base_config.validation_ratio,
        min_windows_per_split=base_config.min_windows_per_split,
    )
    return base_config, metadata, windows, split


def _market_states(
    windows: Sequence[Any],
    window_ids: Sequence[str],
) -> dict[str, str]:
    allowed = set(window_ids)
    return {
        item.window_id: _classify_market_state(item)
        for item in windows
        if item.window_id in allowed
    }


def _evaluate_candidates(
    candidates: Sequence[ProfitCandidate],
    *,
    manifests: Sequence[str],
    base_config: Any,
    metadata: Sequence[dict[str, Any]],
    windows: Sequence[Any],
    split_name: str,
    window_ids: Sequence[str],
    seeds: Sequence[int],
    cost: tuple[float, float, float],
    workers: int,
) -> dict[str, CandidateEvidence]:
    parameters, symbol_policies, maker_policy = _locked_policy()
    evidences: dict[str, CandidateEvidence] = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(workers, len(seeds)),
        initializer=_initialize_worker,
        initargs=(tuple(manifests), base_config),
    ) as executor:
        for candidate in candidates:
            print(f"EVALUATING {split_name} {candidate.candidate_id}", flush=True)
            evidences[candidate.candidate_id] = _evaluate_candidate(
                candidate,
                windows=windows,
                metadata=metadata,
                base_config=base_config,
                parameters=parameters,
                symbol_policies=symbol_policies,
                maker_policy=maker_policy,
                split_ids={split_name: window_ids},
                seeds=seeds,
                cost=cost,
                executor=executor,
            )
    return evidences


def _verify_prior(
    prior: dict[str, Any],
    *,
    protocol_sha256: str,
    datasets: list[dict[str, Any]],
) -> None:
    if prior.get("protocol_sha256") != protocol_sha256:
        raise ValueError("协议文件已变化，拒绝继续打开下一阶段。")
    if prior.get("datasets") != datasets:
        raise ValueError("冻结数据或清单已变化，拒绝继续打开下一阶段。")


def _run_screen(args: argparse.Namespace) -> None:
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "development-results.json"
    if output.exists():
        raise FileExistsError(f"Development 结果已存在，拒绝覆盖: {output}")
    protocol_path = Path(args.protocol).resolve()
    base_config, metadata, windows, split = _load_research_state(args.manifests)
    market_states = _market_states(windows, split.development)
    candidates = _registered_candidates()
    evidences = _evaluate_candidates(
        candidates,
        manifests=args.manifests,
        base_config=base_config,
        metadata=metadata,
        windows=windows,
        split_name="development",
        window_ids=split.development,
        seeds=args.seed_values,
        cost=BASE_COST,
        workers=args.workers,
    )
    candidate_payloads = {
        candidate_id: _evidence_payload(evidence, "development", market_states)
        for candidate_id, evidence in evidences.items()
    }
    baseline = candidate_payloads["X0_BASELINE"]["summary"]
    checks = {
        candidate_id: _development_checks(
            baseline,
            item["summary"],
            seed_count=len(args.seed_values),
        )
        for candidate_id, item in candidate_payloads.items()
        if candidate_id != "X0_BASELINE"
    }
    selected_id = _select_candidate(
        baseline,
        {
            candidate_id: item["summary"]
            for candidate_id, item in candidate_payloads.items()
            if candidate_id != "X0_BASELINE"
        },
        checks,
    )
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": _protocol_sha256(protocol_path),
        "datasets": _dataset_brief(args.manifests, metadata),
        "seeds": list(args.seed_values),
        "split": {
            "development_count": len(split.development),
            "validation_count": len(split.validation),
            "final_oos_count": len(split.final_oos),
            "validation_status": "SEALED_NOT_EVALUATED",
            "final_oos_status": "SEALED_NOT_EVALUATED",
        },
        "candidates": candidate_payloads,
        "checks": checks,
        "selected_candidate_id": selected_id,
        "validation_authorized": selected_id is not None,
        "production_defaults_changed": False,
        "conclusion": (
            "Development 存在预注册合格候选，允许进入 Validation。"
            if selected_id
            else "本轮没有稳健候选，保持生产参数不变。"
        ),
    }
    _write_json(output, payload)
    (report_dir / "development-report.md").write_text(
        _screen_markdown(payload),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"SELECTED {selected_id or 'NONE'}")


def _stage_payloads(
    *,
    candidates: Sequence[ProfitCandidate],
    manifests: Sequence[str],
    base_config: Any,
    metadata: Sequence[dict[str, Any]],
    windows: Sequence[Any],
    market_states: dict[str, str],
    split_name: str,
    window_ids: Sequence[str],
    seeds: Sequence[int],
    workers: int,
) -> dict[str, dict[str, dict[str, Any]]]:
    scenarios: dict[str, dict[str, dict[str, Any]]] = {}
    for scenario, cost in (("BASE", BASE_COST), ("COST50", COST_50)):
        evidences = _evaluate_candidates(
            candidates,
            manifests=manifests,
            base_config=base_config,
            metadata=metadata,
            windows=windows,
            split_name=split_name,
            window_ids=window_ids,
            seeds=seeds,
            cost=cost,
            workers=workers,
        )
        scenarios[scenario] = {
            "baseline": _evidence_payload(
                evidences["X0_BASELINE"],
                split_name,
                market_states,
            ),
            "candidate": _evidence_payload(
                evidences[candidates[1].candidate_id],
                split_name,
                market_states,
            ),
        }
    return scenarios


def _selected_candidate(candidate_id: str) -> ProfitCandidate:
    candidates = {item.candidate_id: item for item in _registered_candidates()}
    if candidate_id not in candidates or candidate_id == "X0_BASELINE":
        raise ValueError(f"Development 选中候选无效: {candidate_id}")
    return candidates[candidate_id]


def _run_validate(args: argparse.Namespace) -> None:
    report_dir = Path(args.report_dir).resolve()
    output = report_dir / "validation-results.json"
    if output.exists():
        raise FileExistsError(f"Validation 结果已存在，拒绝覆盖: {output}")
    prior_path = report_dir / "development-results.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    protocol_path = Path(args.protocol).resolve()
    base_config, metadata, windows, split = _load_research_state(args.manifests)
    market_states = _market_states(windows, split.validation)
    datasets = _dataset_brief(args.manifests, metadata)
    protocol_sha = _protocol_sha256(protocol_path)
    _verify_prior(prior, protocol_sha256=protocol_sha, datasets=datasets)
    selected_id = prior.get("selected_candidate_id")
    if not prior.get("validation_authorized") or not selected_id:
        raise ValueError("Development 未通过，Validation 仍封存。")
    selected = _selected_candidate(str(selected_id))
    baseline = _registered_candidates()[0]
    scenarios = _stage_payloads(
        candidates=(baseline, selected),
        manifests=args.manifests,
        base_config=base_config,
        metadata=metadata,
        windows=windows,
        market_states=market_states,
        split_name="validation",
        window_ids=split.validation,
        seeds=args.seed_values,
        workers=args.workers,
    )
    checks = {
        scenario: _scenario_checks(
            item["baseline"]["summary"],
            item["candidate"]["summary"],
            seed_count=len(args.seed_values),
        )
        for scenario, item in scenarios.items()
    }
    passed = all(all(item.values()) for item in checks.values())
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": protocol_sha,
        "datasets": datasets,
        "seeds": list(args.seed_values),
        "selected_candidate_id": selected.candidate_id,
        "scenarios": scenarios,
        "checks": checks,
        "stage_passed": passed,
        "final_oos_authorized": passed,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "conclusion": (
            "Validation 与 COST50 通过，允许执行一次 Final OOS。"
            if passed
            else "本轮没有稳健候选，保持生产参数不变。"
        ),
    }
    _write_json(output, payload)
    (report_dir / "validation-report.md").write_text(
        _stage_markdown(payload, "跨周期历史 OOS Validation"),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"VALIDATION_PASSED {passed}")


def _run_pytest(repo_root: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [str(repo_root / ".venv" / "Scripts" / "python.exe"), "-m", "pytest", "-q"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _run_finalize(args: argparse.Namespace) -> None:
    report_dir = Path(args.report_dir).resolve()
    output = report_dir / "final-oos-results.json"
    if output.exists():
        raise FileExistsError(f"Final OOS 已评估，拒绝再次运行: {output}")
    prior_path = report_dir / "validation-results.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    protocol_path = Path(args.protocol).resolve()
    base_config, metadata, windows, split = _load_research_state(args.manifests)
    market_states = _market_states(windows, split.final_oos)
    datasets = _dataset_brief(args.manifests, metadata)
    protocol_sha = _protocol_sha256(protocol_path)
    _verify_prior(prior, protocol_sha256=protocol_sha, datasets=datasets)
    if not prior.get("final_oos_authorized") or not prior.get("stage_passed"):
        raise ValueError("Validation 未通过，Final OOS 仍封存。")
    selected = _selected_candidate(str(prior["selected_candidate_id"]))
    baseline = _registered_candidates()[0]
    scenarios = _stage_payloads(
        candidates=(baseline, selected),
        manifests=args.manifests,
        base_config=base_config,
        metadata=metadata,
        windows=windows,
        market_states=market_states,
        split_name="final_oos",
        window_ids=split.final_oos,
        seeds=args.seed_values,
        workers=args.workers,
    )
    checks = {
        scenario: _scenario_checks(
            item["baseline"]["summary"],
            item["candidate"]["summary"],
            seed_count=len(args.seed_values),
        )
        for scenario, item in scenarios.items()
    }
    repo_root = Path(__file__).resolve().parents[1]
    pytest_result = _run_pytest(repo_root)
    passed = all(all(item.values()) for item in checks.values()) and pytest_result["passed"]
    conclusion = (
        "存在跨周期历史研究候选；仍需未来新增、未查看的正向 Final OOS 才能判断是否达到实盘稳定收益标准。"
        if passed
        else "本轮没有稳健候选，保持生产参数不变。"
    )
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": protocol_sha,
        "datasets": datasets,
        "seeds": list(args.seed_values),
        "selected_candidate_id": selected.candidate_id,
        "scenarios": scenarios,
        "checks": checks,
        "pytest": pytest_result,
        "stage_passed": passed,
        "final_oos_status": "EVALUATED_ONCE",
        "testnet_recommended": False,
        "production_defaults_changed": False,
        "conclusion": conclusion,
    }
    _write_json(output, payload)
    (report_dir / "final-oos-report.md").write_text(
        _stage_markdown(payload, "跨周期历史 Final OOS"),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"FINAL_OOS_PASSED {passed}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按预注册协议分阶段执行跨周期历史 OOS，禁止越级打开后续样本。"
    )
    parser.add_argument("stage", choices=("screen", "validate", "finalize"))
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(2, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument("--protocol", default="reports/cross-era-oos/protocol.md")
    return parser


def main() -> None:
    args = _parser().parse_args()
    args.seed_values = tuple(
        int(value.strip())
        for value in args.seeds.split(",")
        if value.strip()
    )
    if not args.seed_values:
        raise ValueError("至少需要一个固定种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    if args.stage == "screen":
        _run_screen(args)
    elif args.stage == "validate":
        _run_validate(args)
    else:
        _run_finalize(args)


if __name__ == "__main__":
    main()
