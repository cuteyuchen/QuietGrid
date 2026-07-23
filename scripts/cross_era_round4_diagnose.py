from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
)
from scripts.cross_era_oos import (
    _dataset_brief,
    _load_research_state,
    _registered_candidates,
    _write_json,
)
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    CandidateEvidence,
    _evaluate_seed_worker,
    _initialize_worker,
    _locked_policy,
)
from scripts.robustness import EntryFilter, RobustnessResearch, WindowResult


UTC = timezone.utc
ROUND4_RESULT_SHA256 = (
    "003ef0c486edbbd0bda27b06301ec66de95691924a59559e927cb36a09eb9045"
)
DEV_FILTERS = {
    "BTCUSDT": EntryFilter(0.55, 0.95, 0.35),
    "ETHUSDT": EntryFilter(0.35, 1.00, 0.55),
}
VAL_FILTERS = {
    "BTCUSDT": EntryFilter(0.40, 1.05, 0.35),
    "ETHUSDT": EntryFilter(0.35, 1.05, 0.55),
}


def _diagnostic_jobs(
    split: Any,
) -> tuple[
    tuple[
        str,
        str,
        Sequence[str],
        tuple[float, float, float],
        int,
        str,
    ],
    ...,
]:
    return (
        (
            "DEV_COST50_SEED97",
            "development",
            split.development,
            COST_50,
            97,
            "DEV",
        ),
        (
            "VAL_BASE_SEED17",
            "validation",
            split.validation,
            BASE_COST,
            17,
            "VAL",
        ),
        (
            "VAL_COST50_SEED17",
            "validation",
            split.validation,
            COST_50,
            17,
            "VAL",
        ),
    )


def _symbol_breakdown(results: Sequence[WindowResult]) -> dict[str, Any]:
    traded = [item for item in results if item.status == "TRADED"]
    steps = [float(item.step_pct) for item in traded if item.step_pct is not None]
    return {
        "result_count": len(results),
        "status_counts": dict(sorted(Counter(item.status for item in results).items())),
        "pnl": sum(item.pnl for item in traded),
        "gross_grid_pnl": sum(item.gross_grid_pnl for item in traded),
        "paired_grid_pnl": sum(item.paired_grid_pnl for item in traded),
        "stop_exit_pnl": sum(item.stop_exit_pnl for item in traded),
        "stop_exit_cost": sum(item.stop_exit_cost for item in traded),
        "fees_paid": sum(item.fees_paid for item in traded),
        "funding_paid": sum(item.funding_paid for item in traded),
        "exit_slippage_cost": sum(item.exit_slippage_cost for item in traded),
        "fill_count": sum(item.fill_count for item in traded),
        "pair_count": sum(item.pair_count for item in traded),
        "mean_step_pct": sum(steps) / len(steps) if steps else None,
        "minimum_step_pct": min(steps) if steps else None,
        "maximum_step_pct": max(steps) if steps else None,
        "maximum_inventory_utilization": max(
            (item.max_inventory_utilization for item in traded),
            default=0.0,
        ),
        "stopped_window_count": sum(
            item.stopped_at_index is not None for item in traded
        ),
    }


def _concentration_series(
    results: Sequence[WindowResult],
) -> dict[str, Any]:
    grouped: dict[str, float] = defaultdict(float)
    for item in results:
        grouped[item.window_id] += item.pnl
    positive = sorted(
        (
            {"window_id": window_id, "pnl": pnl}
            for window_id, pnl in grouped.items()
            if pnl > 0
        ),
        key=lambda item: (-float(item["pnl"]), str(item["window_id"])),
    )
    positive_total = sum(float(item["pnl"]) for item in positive)
    return {
        "window_count": len(grouped),
        "positive_window_count": len(positive),
        "positive_pnl": positive_total,
        "best_positive_pnl": float(positive[0]["pnl"]) if positive else 0.0,
        "concentration": (
            float(positive[0]["pnl"]) / positive_total
            if positive_total > 0
            else 0.0
        ),
        "top_positive_windows": positive[:8],
        "worst_windows": sorted(
            (
                {"window_id": window_id, "pnl": pnl}
                for window_id, pnl in grouped.items()
            ),
            key=lambda item: (float(item["pnl"]), str(item["window_id"])),
        )[:8],
    }


def _concentration_diagnostics(
    results: Sequence[WindowResult],
) -> dict[str, Any]:
    series = {"PORTFOLIO": _concentration_series(results)}
    for symbol in sorted({item.symbol for item in results}):
        series[symbol] = _concentration_series(
            [item for item in results if item.symbol == symbol]
        )
    driver = max(
        series,
        key=lambda name: (float(series[name]["concentration"]), name),
    )
    return {"driver": driver, "series": series}


def _result_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cost_payload(cost: tuple[float, float, float]) -> dict[str, float]:
    return {
        "maker_fee_rate": cost[0],
        "taker_fee_rate": cost[1],
        "stop_loss_slippage_bps": cost[2],
    }


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Round 4 窗口级诊断",
        "",
        "本报告只重放已消费的 Development/Validation 代表性种子；Final OOS 未读取。",
        "",
    ]
    for job_name, item in payload["jobs"].items():
        aggregate = item["aggregate"]
        concentration = item["concentration"]
        lines.extend([
            f"## {job_name}",
            "",
            f"- 组合 PnL：{aggregate['total_pnl']:.4f} USDT",
            f"- Profit Factor：{aggregate['profit_factor']}",
            f"- 集中度来源：`{concentration['driver']}` "
            f"({concentration['series'][concentration['driver']]['concentration']:.2%})",
            "",
            "| 标的 | PnL | 毛网格 | 手续费 | 退出滑点 | 成交 | 配对 | 平均格距 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for symbol, breakdown in item["symbol_breakdown"].items():
            mean_step = breakdown["mean_step_pct"]
            lines.append(
                "| {symbol} | {pnl:.4f} | {gross:.4f} | {fees:.4f} | "
                "{slippage:.4f} | {fills} | {pairs} | {step} |".format(
                    symbol=symbol,
                    pnl=breakdown["pnl"],
                    gross=breakdown["gross_grid_pnl"],
                    fees=breakdown["fees_paid"],
                    slippage=breakdown["exit_slippage_cost"],
                    fills=breakdown["fill_count"],
                    pairs=breakdown["pair_count"],
                    step=(f"{mean_step:.3%}" if mean_step is not None else "N/A"),
                )
            )
        driver = concentration["driver"]
        lines.extend([
            "",
            f"`{driver}` 最高正收益窗口：",
            "",
        ])
        for row in concentration["series"][driver]["top_positive_windows"][:5]:
            lines.append(f"- `{row['window_id']}`：{row['pnl']:.4f} USDT")
        lines.append("")
    lines.extend([
        "## 约束",
        "",
        "- 本报告不选择 Round 5 参数；",
        "- Final OOS 保持 `SEALED_NOT_EVALUATED`；",
        "- 生产参数未修改。",
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="重放 Round 4 代表性种子，定位 BTC 成本脆弱性与集中度来源。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--workers", type=int, default=min(3, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "round4-diagnostics.json"
    if output.exists():
        raise FileExistsError(f"Round 4 诊断已存在，拒绝覆盖: {output}")

    round4_path = report_dir / "round4-extended-development-results.json"
    actual_hash = _result_sha256(round4_path)
    if actual_hash != ROUND4_RESULT_SHA256:
        raise ValueError(
            "Round 4 冻结结果已变化: "
            f"expected={ROUND4_RESULT_SHA256} actual={actual_hash}"
        )
    round4 = json.loads(round4_path.read_text(encoding="utf-8"))
    if round4.get("eligible_candidate_ids"):
        raise ValueError("Round 4 已存在合格候选，不应执行失败诊断。")
    if round4.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Final OOS 已不再封存，拒绝执行诊断。")

    base_config, metadata, windows, split = _load_research_state(args.manifests)
    datasets = _dataset_brief(args.manifests, metadata)
    if round4.get("datasets") != datasets:
        raise ValueError("当前冻结数据与 Round 4 结果不一致。")
    jobs = _diagnostic_jobs(split)
    baseline_candidate = _registered_candidates()[0]
    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(jobs)),
        initializer=_initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    ) as executor:
        for job_name, split_name, window_ids, cost, seed, filter_name in jobs:
            print(f"DIAGNOSING {job_name}", flush=True)
            future = executor.submit(
                _evaluate_seed_worker,
                baseline_candidate,
                seed,
                {split_name: window_ids},
                cost,
            )
            futures[future] = (
                job_name,
                split_name,
                cost,
                seed,
                filter_name,
            )
        raw_runs = {}
        for future in concurrent.futures.as_completed(futures):
            job_name, split_name, cost, seed, filter_name = futures[future]
            returned_seed, seed_runs = future.result()
            if returned_seed != seed:
                raise RuntimeError("诊断 worker 返回了错误种子。")
            raw_runs[job_name] = (
                split_name,
                cost,
                seed,
                filter_name,
                seed_runs,
            )

    locked_parameters, _symbol_policies, _maker_policy = _locked_policy()
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
    job_payloads = {}
    for job_name, _split_name, _window_ids, _cost, _seed, _filter_name in jobs:
        split_name, cost, seed, filter_name, seed_runs = raw_runs[job_name]
        baseline = CandidateEvidence(baseline_candidate, {seed: seed_runs})
        filters = DEV_FILTERS if filter_name == "DEV" else VAL_FILTERS
        filtered = _filtered_evidence_for_symbols(
            baseline,
            filters,
            contexts[split_name],
            candidate_id=f"ROUND4_DIAGNOSTIC_{filter_name}",
            round_name="round4_diagnostic",
            split_name=split_name,
        )
        aggregate, results = filtered.runs[seed][split_name]
        job_payloads[job_name] = {
            "split": split_name,
            "seed": seed,
            "cost": _cost_payload(cost),
            "filters": {
                symbol: asdict(entry_filter) | {"filter_id": entry_filter.filter_id}
                for symbol, entry_filter in filters.items()
            },
            "aggregate": asdict(aggregate),
            "symbol_breakdown": {
                symbol: _symbol_breakdown(
                    [item for item in results if item.symbol == symbol]
                )
                for symbol in ("BTCUSDT", "ETHUSDT")
            },
            "concentration": _concentration_diagnostics(results),
        }

    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "round4_result_sha256": ROUND4_RESULT_SHA256,
        "datasets": datasets,
        "jobs": job_payloads,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "conclusion": (
            "窗口级诊断完成；仅用于冻结下一轮结构性假设，不构成候选通过证据。"
        ),
    }
    _write_json(output, result)
    (report_dir / "round4-diagnostics.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")


if __name__ == "__main__":
    main()
