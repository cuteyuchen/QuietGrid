from __future__ import annotations

import argparse
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.cross_era_entry_screen import (
    _entry_checks,
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
    _symbol_trade_coverage,
)
from scripts.cross_era_oos import (
    _dataset_brief,
    _evidence_payload,
    _evaluate_candidates,
    _load_research_state,
    _market_states,
    _protocol_sha256,
    _registered_candidates,
    _relative_improvement,
    _write_json,
)
from scripts.profit_protection_optimize import DEFAULT_SEEDS, _locked_policy
from scripts.robustness import EntryFilter, RobustnessResearch, generate_entry_filters


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-joint-entry-development-round3-20260723"
FIXED_ETH_FILTER = EntryFilter(0.35, 1.00, 0.55)


def _registered_btc_filters() -> tuple[EntryFilter, ...]:
    filters = generate_entry_filters(
        max_directional_efficiencies=(0.40, 0.45, 0.50, 0.55),
        max_volatility_expansions=(0.90, 0.95, 1.00, 1.05),
        min_reversal_ratios=(0.25, 0.30, 0.35),
    )
    return tuple(sorted(filters, key=lambda item: item.filter_id))


def _joint_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    seed_count: int,
    eth_coverage: float,
    btc_coverage: float,
) -> dict[str, bool]:
    checks = _entry_checks(
        baseline,
        candidate,
        seed_count=seed_count,
        eth_trade_coverage=eth_coverage,
    )
    checks["btc_trade_coverage_ge_25pct"] = btc_coverage >= 0.25
    return checks


def _select_btc_filter(
    baseline: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    checks: dict[str, dict[str, bool]],
    btc_coverages: dict[str, float],
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
        key=lambda filter_id: (
            -_relative_improvement(
                float(baseline["worst_5pct_window_mean_pnl"]),
                float(candidates[filter_id]["worst_5pct_window_mean_pnl"]),
            ),
            -float(candidates[filter_id]["worst_seed_total_pnl"]),
            -float(candidates[filter_id]["mean_seed_total_pnl"]),
            -btc_coverages[filter_id],
            filter_id,
        ),
    )


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
        "# Round 3 BTC + ETH 联合入口 Development 筛选",
        "",
        f"- 固定 ETH：`{payload['fixed_eth_filter']['filter_id']}`",
        f"- BTC 候选数：{len(payload['candidates'])}",
        f"- Development 窗口：{payload['split']['development_count']}",
        "- Validation：`SEALED_NOT_EVALUATED`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "| BTC Filter | 平均 PnL | 最差种子 | BTC PnL | ETH PnL | 最差 5% | 最大回撤 | BTC 覆盖 | 门槛 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for filter_id, item in ranked:
        summary = item["summary"]
        item_checks = payload["checks"][filter_id]
        lines.append(
            "| {filter_id} | {mean:.4f} | {worst:.4f} | {btc:.4f} | {eth:.4f} | "
            "{tail:.4f} | {dd:.2%} | {coverage:.2%} | {passed}/{total} |".format(
                filter_id=filter_id,
                mean=summary["mean_seed_total_pnl"],
                worst=summary["worst_seed_total_pnl"],
                btc=summary["symbol_pnl"].get("BTCUSDT", 0.0),
                eth=summary["symbol_pnl"].get("ETHUSDT", 0.0),
                tail=summary["worst_5pct_window_mean_pnl"],
                dd=summary["max_drawdown_pct"],
                coverage=payload["btc_trade_coverage"][filter_id],
                passed=sum(item_checks.values()),
                total=len(item_checks),
            )
        )
    selected = payload["selected_btc_filter_id"]
    lines.extend([
        "",
        (
            f"结论：Development 选中 BTC `{selected}` + ETH `{FIXED_ETH_FILTER.filter_id}`。"
            if selected
            else "结论：本轮没有稳健候选，Validation 与 Final OOS 保持封存。"
        ),
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="固定 Round 2 ETH 过滤器，仅在 Development 搜索 BTC 因果入口过滤器。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round3-joint-entry-protocol.md",
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
    output = report_dir / "joint-entry-development-results.json"
    if output.exists():
        raise FileExistsError(f"联合入口 Development 结果已存在，拒绝覆盖: {output}")

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
    baseline_payload = _evidence_payload(
        baseline_evidence,
        "development",
        market_states,
    )
    baseline_summary = baseline_payload["summary"]

    candidate_payloads = {}
    checks = {}
    btc_coverages = {}
    eth_coverages = {}
    for btc_filter in _registered_btc_filters():
        evidence = _filtered_evidence_for_symbols(
            baseline_evidence,
            {
                "BTCUSDT": btc_filter,
                "ETHUSDT": FIXED_ETH_FILTER,
            },
            contexts,
            candidate_id=f"BTC_{btc_filter.filter_id}_ETH_{FIXED_ETH_FILTER.filter_id}",
            round_name="round3_joint_entry_filter",
        )
        payload = _evidence_payload(evidence, "development", market_states)
        payload["btc_entry_filter"] = asdict(btc_filter) | {"filter_id": btc_filter.filter_id}
        payload["eth_entry_filter"] = asdict(FIXED_ETH_FILTER) | {
            "filter_id": FIXED_ETH_FILTER.filter_id
        }
        btc_coverage = _symbol_trade_coverage(baseline_evidence, evidence, "BTCUSDT")
        eth_coverage = _symbol_trade_coverage(baseline_evidence, evidence, "ETHUSDT")
        candidate_payloads[btc_filter.filter_id] = payload
        btc_coverages[btc_filter.filter_id] = btc_coverage
        eth_coverages[btc_filter.filter_id] = eth_coverage
        checks[btc_filter.filter_id] = _joint_checks(
            baseline_summary,
            payload["summary"],
            seed_count=len(seeds),
            eth_coverage=eth_coverage,
            btc_coverage=btc_coverage,
        )

    selected = _select_btc_filter(
        baseline_summary,
        {key: value["summary"] for key, value in candidate_payloads.items()},
        checks,
        btc_coverages,
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
        "fixed_eth_filter": asdict(FIXED_ETH_FILTER) | {
            "filter_id": FIXED_ETH_FILTER.filter_id
        },
        "baseline": baseline_payload,
        "candidates": candidate_payloads,
        "btc_trade_coverage": btc_coverages,
        "eth_trade_coverage": eth_coverages,
        "checks": checks,
        "selected_btc_filter_id": selected,
        "validation_authorized": selected is not None,
        "production_defaults_changed": False,
        "conclusion": (
            "Development 存在联合入口候选，允许实现 Validation 门禁。"
            if selected
            else "本轮没有稳健候选，保持生产参数不变。"
        ),
    }
    _write_json(output, result)
    (report_dir / "joint-entry-development-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"SELECTED_BTC_FILTER {selected or 'NONE'}")


if __name__ == "__main__":
    main()
