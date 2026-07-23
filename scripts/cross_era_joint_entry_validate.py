from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
    _symbol_trade_coverage,
)
from scripts.cross_era_oos import (
    _dataset_brief,
    _evidence_payload,
    _evaluate_candidates,
    _fee_ratio_ok,
    _load_research_state,
    _market_states,
    _protocol_sha256,
    _registered_candidates,
    _scenario_checks,
    _write_json,
)
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    _locked_policy,
)
from scripts.robustness import EntryFilter, RobustnessResearch


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-joint-entry-validation-round3-20260723"


def _entry_filter(payload: dict[str, Any]) -> EntryFilter:
    return EntryFilter(
        float(payload["max_directional_efficiency"]),
        float(payload["max_volatility_expansion"]),
        float(payload["min_reversal_ratio"]),
    )


def _validation_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    seed_count: int,
    btc_coverage: float,
    eth_coverage: float,
) -> dict[str, bool]:
    checks = _scenario_checks(
        baseline,
        candidate,
        seed_count=seed_count,
    )
    checks["worst_seed_positive"] = float(candidate["worst_seed_total_pnl"]) > 0
    checks["btc_trade_coverage_ge_25pct"] = btc_coverage >= 0.25
    checks["eth_trade_coverage_ge_25pct"] = eth_coverage >= 0.25
    checks["fee_ratio_le_125pct_baseline"] = _fee_ratio_ok(
        baseline["fee_to_gross_profit_ratio"],
        candidate["fee_to_gross_profit_ratio"],
    )
    return checks


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Round 3 联合入口 Validation",
        "",
        f"- BTC：`{payload['btc_entry_filter']['filter_id']}`",
        f"- ETH：`{payload['eth_entry_filter']['filter_id']}`",
        f"- Validation 窗口：{payload['validation_window_count']}",
        f"- 本阶段通过：`{payload['stage_passed']}`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "| 场景 | 基线平均 | 候选平均 | 候选最差 | 最小 PF | BTC PnL | ETH PnL | 最大回撤 | 集中度 | 门槛 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario in ("BASE", "COST50"):
        item = payload["scenarios"][scenario]
        baseline = item["baseline"]["summary"]
        candidate = item["candidate"]["summary"]
        checks = payload["checks"][scenario]
        lines.append(
            "| {scenario} | {base:.4f} | {mean:.4f} | {worst:.4f} | {pf:.3f} | "
            "{btc:.4f} | {eth:.4f} | {dd:.2%} | {concentration:.2%} | {passed}/{total} |".format(
                scenario=scenario,
                base=baseline["mean_seed_total_pnl"],
                mean=candidate["mean_seed_total_pnl"],
                worst=candidate["worst_seed_total_pnl"],
                pf=candidate["minimum_seed_profit_factor"],
                btc=candidate["symbol_pnl"].get("BTCUSDT", 0.0),
                eth=candidate["symbol_pnl"].get("ETHUSDT", 0.0),
                dd=candidate["max_drawdown_pct"],
                concentration=candidate["worst_best_window_concentration"],
                passed=sum(checks.values()),
                total=len(checks),
            )
        )
    lines.extend([
        "",
        f"结论：{payload['conclusion']}",
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="一次性评估 Round 3 联合入口候选的 Validation BASE 与 COST50。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--development-protocol",
        default="reports/cross-era-oos/round3-joint-entry-protocol.md",
    )
    parser.add_argument(
        "--validation-protocol",
        default="reports/cross-era-oos/round3-validation-protocol.md",
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
    output = report_dir / "joint-entry-validation-results.json"
    if output.exists():
        raise FileExistsError(f"Validation 已评估，拒绝覆盖: {output}")

    prior_path = report_dir / "joint-entry-development-results.json"
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    if not prior.get("validation_authorized") or not prior.get("selected_btc_filter_id"):
        raise ValueError("Development 未通过，Validation 仍封存。")
    development_protocol = Path(args.development_protocol).resolve()
    if prior.get("protocol_sha256") != _protocol_sha256(development_protocol):
        raise ValueError("Development 协议已变化，拒绝打开 Validation。")
    if list(seeds) != prior.get("seeds"):
        raise ValueError("固定种子与 Development 不一致。")

    base_config, metadata, windows, split = _load_research_state(args.manifests)
    datasets = _dataset_brief(args.manifests, metadata)
    if prior.get("datasets") != datasets:
        raise ValueError("冻结数据与 Development 不一致。")
    market_states = _market_states(windows, split.validation)
    selected_id = str(prior["selected_btc_filter_id"])
    selected_payload = prior["candidates"][selected_id]
    btc_filter = _entry_filter(selected_payload["btc_entry_filter"])
    eth_filter = _entry_filter(selected_payload["eth_entry_filter"])

    locked_parameters, _symbol_policies, _maker_policy = _locked_policy()
    research = RobustnessResearch(
        windows,
        locked_parameters,
        base_config,
        dataset_metadata=metadata,
    )
    contexts = _populate_entry_decisions(research, split.validation)
    baseline_candidate = _registered_candidates()[0]

    scenarios = {}
    checks = {}
    coverages = {}
    for scenario, cost in (("BASE", BASE_COST), ("COST50", COST_50)):
        baseline_evidence = _evaluate_candidates(
            (baseline_candidate,),
            manifests=args.manifests,
            base_config=base_config,
            metadata=metadata,
            windows=windows,
            split_name="validation",
            window_ids=split.validation,
            seeds=seeds,
            cost=cost,
            workers=args.workers,
        )[baseline_candidate.candidate_id]
        candidate_evidence = _filtered_evidence_for_symbols(
            baseline_evidence,
            {"BTCUSDT": btc_filter, "ETHUSDT": eth_filter},
            contexts,
            candidate_id=f"BTC_{btc_filter.filter_id}_ETH_{eth_filter.filter_id}",
            round_name="round3_joint_entry_validation",
            split_name="validation",
        )
        baseline_payload = _evidence_payload(
            baseline_evidence,
            "validation",
            market_states,
        )
        candidate_payload = _evidence_payload(
            candidate_evidence,
            "validation",
            market_states,
        )
        btc_coverage = _symbol_trade_coverage(
            baseline_evidence,
            candidate_evidence,
            "BTCUSDT",
            "validation",
        )
        eth_coverage = _symbol_trade_coverage(
            baseline_evidence,
            candidate_evidence,
            "ETHUSDT",
            "validation",
        )
        scenarios[scenario] = {
            "baseline": baseline_payload,
            "candidate": candidate_payload,
        }
        coverages[scenario] = {
            "BTCUSDT": btc_coverage,
            "ETHUSDT": eth_coverage,
        }
        checks[scenario] = _validation_checks(
            baseline_payload["summary"],
            candidate_payload["summary"],
            seed_count=len(seeds),
            btc_coverage=btc_coverage,
            eth_coverage=eth_coverage,
        )

    passed = all(all(item.values()) for item in checks.values())
    validation_protocol = Path(args.validation_protocol).resolve()
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "development_protocol_sha256": prior["protocol_sha256"],
        "validation_protocol_sha256": _protocol_sha256(validation_protocol),
        "datasets": datasets,
        "seeds": list(seeds),
        "validation_window_count": len(split.validation),
        "btc_entry_filter": asdict(btc_filter) | {"filter_id": btc_filter.filter_id},
        "eth_entry_filter": asdict(eth_filter) | {"filter_id": eth_filter.filter_id},
        "scenarios": scenarios,
        "trade_coverage": coverages,
        "checks": checks,
        "stage_passed": passed,
        "final_oos_authorized": passed,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "conclusion": (
            "Validation BASE 与 COST50 全部通过，允许执行一次 Final OOS。"
            if passed
            else "本轮没有稳健候选，保持生产参数不变。"
        ),
    }
    _write_json(output, result)
    (report_dir / "joint-entry-validation-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"VALIDATION_PASSED {passed}")


if __name__ == "__main__":
    main()
