from __future__ import annotations

import argparse
import csv
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from scripts.cross_era_oos import (
    _dataset_brief,
    _evidence_payload,
    _evaluate_candidates,
    _fee_ratio_ok,
    _load_research_state,
    _market_states,
    _protocol_sha256,
    _registered_candidates,
    _relative_improvement,
    _write_json,
)
from scripts.profit_protection_optimize import (
    CandidateEvidence,
    DEFAULT_SEEDS,
    _locked_policy,
)
from scripts.robustness import (
    EntryFilter,
    RobustnessResearch,
    aggregate_joint_results,
    generate_entry_filters,
)


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-eth-entry-development-round2-20260723"
CAPITAL_BY_SYMBOL = {"BTCUSDT": 500.0, "ETHUSDT": 300.0}


def _registered_filters() -> tuple[EntryFilter, ...]:
    filters = generate_entry_filters(
        max_directional_efficiencies=(0.25, 0.35, 0.45, 0.50),
        max_volatility_expansions=(0.75, 0.90, 1.00, 1.05),
        min_reversal_ratios=(0.25, 0.35, 0.45, 0.55),
    )
    return tuple(sorted(filters, key=lambda item: item.filter_id))


def _populate_entry_decisions(
    research: RobustnessResearch,
    window_ids: Sequence[str],
) -> dict[tuple[str, str], Any]:
    allowed = set(window_ids)
    contexts: dict[tuple[str, str], Any] = {}
    for context in research.contexts:
        window = context.window
        if window.window_id not in allowed:
            continue
        observation = list(window.rows[: window.observation_rows])
        if len(observation) < research.config.observation_rows:
            continue
        context.entry_decision = research.regime.evaluate(
            window.symbol,
            [item.to_mapping() for item in observation],
            spread_pct=research.config.assumed_spread_pct,
            depth_usdt=research.config.assumed_depth_usdt,
            funding_rate=research.config.funding_rate_per_settlement,
            data_age_seconds=0.0,
            include_cost=False,
            as_of=observation[-1].open_datetime,
        )
        contexts[(window.symbol, window.window_id)] = context
    return contexts


def _filtered_evidence(
    baseline: CandidateEvidence,
    entry_filter: EntryFilter,
    contexts: dict[tuple[str, str], Any],
) -> CandidateEvidence:
    return _filtered_evidence_for_symbols(
        baseline,
        {"ETHUSDT": entry_filter},
        contexts,
        candidate_id=f"ETH_{entry_filter.filter_id}",
        round_name="round2_eth_entry_filter",
    )


def _filtered_evidence_for_symbols(
    baseline: CandidateEvidence,
    filters_by_symbol: dict[str, EntryFilter],
    contexts: dict[tuple[str, str], Any],
    *,
    candidate_id: str,
    round_name: str,
    split_name: str = "development",
) -> CandidateEvidence:
    candidate = replace(
        baseline.candidate,
        candidate_id=candidate_id,
        round_name=round_name,
    )
    runs = {}
    for seed, seed_runs in baseline.runs.items():
        metrics, results = seed_runs[split_name]
        del metrics
        transformed = []
        for result in results:
            entry_filter = filters_by_symbol.get(result.symbol)
            if entry_filter is None:
                transformed.append(result)
                continue
            context = contexts.get((result.symbol, result.window_id))
            if context is None:
                transformed.append(result)
                continue
            transformed.append(
                RobustnessResearch._apply_entry_filter(
                    result,
                    context,
                    entry_filter,
                )
            )
        runs[int(seed)] = {
            split_name: (
                aggregate_joint_results(
                    transformed,
                    capital_by_symbol=CAPITAL_BY_SYMBOL,
                ),
                transformed,
            )
        }
    return CandidateEvidence(candidate, runs)


def _eth_trade_coverage(
    baseline: CandidateEvidence,
    candidate: CandidateEvidence,
) -> float:
    return _symbol_trade_coverage(baseline, candidate, "ETHUSDT")


def _symbol_trade_coverage(
    baseline: CandidateEvidence,
    candidate: CandidateEvidence,
    symbol: str,
    split_name: str = "development",
) -> float:
    baseline_count = sum(
        result.symbol == symbol and result.status == "TRADED"
        for seed_runs in baseline.runs.values()
        for result in seed_runs[split_name][1]
    )
    candidate_count = sum(
        result.symbol == symbol and result.status == "TRADED"
        for seed_runs in candidate.runs.values()
        for result in seed_runs[split_name][1]
    )
    return candidate_count / baseline_count if baseline_count else 0.0


def _entry_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    seed_count: int,
    eth_trade_coverage: float,
) -> dict[str, bool]:
    return {
        "all_seeds_positive": int(candidate["positive_seed_count"]) == seed_count,
        "worst_seed_positive": float(candidate["worst_seed_total_pnl"]) > 0,
        "both_symbols_positive": all(
            float(candidate["symbol_pnl"].get(symbol, 0.0)) > 0
            for symbol in ("BTCUSDT", "ETHUSDT")
        ),
        "all_seed_profit_factors_gt_1": float(
            candidate["minimum_seed_profit_factor"]
        )
        > 1.0,
        "worst_5pct_improvement_ge_20pct": _relative_improvement(
            float(baseline["worst_5pct_window_mean_pnl"]),
            float(candidate["worst_5pct_window_mean_pnl"]),
        )
        >= 0.20,
        "max_drawdown_le_5pct": float(candidate["max_drawdown_pct"]) <= 0.05,
        "best_window_concentration_le_35pct": float(
            candidate["worst_best_window_concentration"]
        )
        <= 0.35,
        "eth_trade_coverage_ge_25pct": eth_trade_coverage >= 0.25,
        "fee_ratio_le_125pct_baseline": _fee_ratio_ok(
            baseline["fee_to_gross_profit_ratio"],
            candidate["fee_to_gross_profit_ratio"],
        ),
        "mean_seed_total_pnl_positive": float(candidate["mean_seed_total_pnl"]) > 0,
    }


def _selection_key(
    filter_id: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    coverage: float,
) -> tuple[float, float, float, float, str]:
    return (
        -_relative_improvement(
            float(baseline["worst_5pct_window_mean_pnl"]),
            float(candidate["worst_5pct_window_mean_pnl"]),
        ),
        -float(candidate["worst_seed_total_pnl"]),
        -float(candidate["mean_seed_total_pnl"]),
        -coverage,
        filter_id,
    )


def _select_filter(
    baseline: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    checks: dict[str, dict[str, bool]],
    coverages: dict[str, float],
) -> str | None:
    eligible = [
        filter_id
        for filter_id, item_checks in checks.items()
        if item_checks and all(item_checks.values())
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda filter_id: _selection_key(
            filter_id,
            baseline,
            candidates[filter_id],
            coverages[filter_id],
        ),
    )


def _write_feature_diagnostics(
    path: Path,
    baseline: CandidateEvidence,
    contexts: dict[tuple[str, str], Any],
) -> None:
    grouped: dict[tuple[str, str], list[Any]] = {}
    for seed_runs in baseline.runs.values():
        for result in seed_runs["development"][1]:
            grouped.setdefault((result.symbol, result.window_id), []).append(result)
    rows = []
    for key, results in grouped.items():
        context = contexts.get(key)
        if context is None or context.entry_decision is None:
            continue
        features = context.entry_decision.features
        rows.append({
            "symbol": key[0],
            "window_id": key[1],
            "market_close": results[0].market_close,
            "baseline_status": results[0].status,
            "mean_seed_pnl": sum(item.pnl for item in results) / len(results),
            "directional_efficiency": features.directional_efficiency,
            "volatility_expansion": features.volatility_expansion,
            "reversal_ratio": features.reversal_ratio,
            "grid_score": context.entry_decision.grid_score,
        })
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["symbol"])
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda item: (item["symbol"], item["window_id"])))


def _report_markdown(payload: dict[str, Any]) -> str:
    ranked = sorted(
        payload["candidates"].items(),
        key=lambda item: (
            -sum(payload["checks"][item[0]].values()),
            -float(item[1]["summary"]["mean_seed_total_pnl"]),
            item[0],
        ),
    )[:15]
    lines = [
        "# Round 2 ETH 入口过滤 Development 筛选",
        "",
        f"- 候选数：{len(payload['candidates'])}",
        f"- Development 窗口：{payload['split']['development_count']}",
        "- Validation：`SEALED_NOT_EVALUATED`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "| Filter | 平均 PnL | 最差种子 | ETH PnL | 最差 5% | 最大回撤 | ETH 覆盖 | 门槛 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for filter_id, item in ranked:
        summary = item["summary"]
        item_checks = payload["checks"][filter_id]
        lines.append(
            "| {filter_id} | {mean:.4f} | {worst:.4f} | {eth:.4f} | "
            "{tail:.4f} | {dd:.2%} | {coverage:.2%} | {passed}/{total} |".format(
                filter_id=filter_id,
                mean=summary["mean_seed_total_pnl"],
                worst=summary["worst_seed_total_pnl"],
                eth=summary["symbol_pnl"].get("ETHUSDT", 0.0),
                tail=summary["worst_5pct_window_mean_pnl"],
                dd=summary["max_drawdown_pct"],
                coverage=payload["eth_trade_coverage"][filter_id],
                passed=sum(item_checks.values()),
                total=len(item_checks),
            )
        )
    selected = payload["selected_filter_id"]
    lines.extend([
        "",
        (
            f"结论：Development 选中 `{selected}`，允许实现 Validation 门禁。"
            if selected
            else "结论：本轮没有稳健候选，Validation 与 Final OOS 保持封存。"
        ),
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="仅使用跨周期 Development 搜索更严格的 ETH 因果入口过滤器。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round2-entry-protocol.md",
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
    output = report_dir / "entry-development-results.json"
    if output.exists():
        raise FileExistsError(f"入口 Development 结果已存在，拒绝覆盖: {output}")

    base_config, metadata, windows, split = _load_research_state(args.manifests)
    market_states = _market_states(windows, split.development)
    baseline_candidate = _registered_candidates()[0]
    baseline_evidence = _evaluate_candidates(
        (baseline_candidate,),
        manifests=args.manifests,
        base_config=base_config,
        metadata=metadata,
        windows=windows,
        split_name="development",
        window_ids=split.development,
        seeds=seeds,
        cost=(0.0002, 0.0005, 10.0),
        workers=args.workers,
    )[baseline_candidate.candidate_id]

    locked_parameters, _symbol_policies, _maker_policy = _locked_policy()
    research = RobustnessResearch(
        windows,
        locked_parameters,
        base_config,
        dataset_metadata=metadata,
    )
    contexts = _populate_entry_decisions(research, split.development)
    _write_feature_diagnostics(
        report_dir / "entry-development-features.csv",
        baseline_evidence,
        contexts,
    )

    baseline_payload = _evidence_payload(
        baseline_evidence,
        "development",
        market_states,
    )
    baseline_summary = baseline_payload["summary"]
    candidate_payloads = {}
    checks = {}
    coverages = {}
    for entry_filter in _registered_filters():
        evidence = _filtered_evidence(baseline_evidence, entry_filter, contexts)
        payload = _evidence_payload(evidence, "development", market_states)
        payload["entry_filter"] = asdict(entry_filter) | {"filter_id": entry_filter.filter_id}
        coverage = _eth_trade_coverage(baseline_evidence, evidence)
        candidate_payloads[entry_filter.filter_id] = payload
        coverages[entry_filter.filter_id] = coverage
        checks[entry_filter.filter_id] = _entry_checks(
            baseline_summary,
            payload["summary"],
            seed_count=len(seeds),
            eth_trade_coverage=coverage,
        )

    selected = _select_filter(
        baseline_summary,
        {key: value["summary"] for key, value in candidate_payloads.items()},
        checks,
        coverages,
    )
    protocol_path = Path(args.protocol).resolve()
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": _protocol_sha256(protocol_path),
        "datasets": _dataset_brief(args.manifests, metadata),
        "seeds": list(seeds),
        "split": {
            "development_count": len(split.development),
            "validation_count": len(split.validation),
            "final_oos_count": len(split.final_oos),
            "validation_status": "SEALED_NOT_EVALUATED",
            "final_oos_status": "SEALED_NOT_EVALUATED",
        },
        "baseline": baseline_payload,
        "candidates": candidate_payloads,
        "eth_trade_coverage": coverages,
        "checks": checks,
        "selected_filter_id": selected,
        "validation_authorized": selected is not None,
        "production_defaults_changed": False,
        "conclusion": (
            "Development 存在 ETH 入口过滤候选，允许实现 Validation 门禁。"
            if selected
            else "本轮没有稳健候选，保持生产参数不变。"
        ),
    }
    _write_json(output, result)
    (report_dir / "entry-development-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"SELECTED_FILTER {selected or 'NONE'}")


if __name__ == "__main__":
    main()
