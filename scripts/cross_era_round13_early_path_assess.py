from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.cross_era_oos import _write_json
from scripts.cross_era_round13_diagnose import _sha256
from scripts.cross_era_round13_early_path import HORIZONS


UTC = timezone.utc
EARLY_PATH_RESULT_SHA256 = (
    "52cbc1ffb2c911387599e802863ac16e76c877f9e2ebc748dadb20a8fcaac974"
)
COST50_EXIT_COST_RATE = 0.00075 + 0.0020


def _trigger(checkpoint: Mapping[str, Any]) -> bool:
    return (
        bool(checkpoint["zero_pair"])
        and float(checkpoint["adverse_excursion_pct"])
        > float(checkpoint["favorable_excursion_pct"])
    )


def _assess_horizon(
    windows: Sequence[Mapping[str, Any]],
    horizon: int,
) -> dict[str, Any]:
    records = []
    for window in windows:
        for record in window["seed_records"]:
            checkpoint = record["checkpoints"][str(horizon)]
            triggered = _trigger(checkpoint)
            baseline = float(record["pnl"])
            zero_cost = float(checkpoint["equity"]) if triggered else baseline
            costed = (
                float(checkpoint["equity"])
                - float(checkpoint["gross_inventory_notional"])
                * COST50_EXIT_COST_RATE
                if triggered
                else baseline
            )
            records.append({
                "window_id": window["window_id"],
                "label": window["label"],
                "seed": int(record["seed"]),
                "triggered": triggered,
                "baseline_pnl": baseline,
                "zero_cost_guard_pnl": zero_cost,
                "costed_guard_pnl": costed,
            })
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        grouped.setdefault(record["window_id"], []).append(record)
    window_rows = []
    for window_id, items in grouped.items():
        window_rows.append({
            "window_id": window_id,
            "label": items[0]["label"],
            "triggered_seed_count": sum(bool(item["triggered"]) for item in items),
            "mean_baseline_pnl": sum(float(item["baseline_pnl"]) for item in items)
            / len(items),
            "mean_zero_cost_guard_pnl": sum(
                float(item["zero_cost_guard_pnl"]) for item in items
            ) / len(items),
            "mean_costed_guard_pnl": sum(
                float(item["costed_guard_pnl"]) for item in items
            ) / len(items),
        })
    window_rows.sort(key=lambda item: (float(item["mean_baseline_pnl"]), item["window_id"]))
    triggered = [record for record in records if record["triggered"]]
    persistent_ids = {
        record["window_id"]
        for record in records
        if record["label"] == "PERSISTENT_LOSS"
    }
    covered_persistent = {
        record["window_id"]
        for record in triggered
        if record["label"] == "PERSISTENT_LOSS"
    }
    profitable_false_positives = {
        record["window_id"]
        for record in triggered
        if record["label"] == "PROFITABLE"
    }
    baseline_total = sum(float(record["baseline_pnl"]) for record in records)
    zero_cost_total = sum(float(record["zero_cost_guard_pnl"]) for record in records)
    costed_total = sum(float(record["costed_guard_pnl"]) for record in records)
    return {
        "horizon_bars": horizon,
        "baseline_btc_total_pnl": baseline_total,
        "zero_cost_guard_btc_total_pnl": zero_cost_total,
        "costed_guard_btc_total_pnl": costed_total,
        "zero_cost_improvement": zero_cost_total - baseline_total,
        "costed_improvement": costed_total - baseline_total,
        "triggered_record_count": len(triggered),
        "triggered_window_count": len({record["window_id"] for record in triggered}),
        "covered_persistent_loss_windows": sorted(covered_persistent),
        "persistent_loss_window_count": len(persistent_ids),
        "all_persistent_loss_windows_covered": covered_persistent == persistent_ids,
        "profitable_false_positive_windows": sorted(profitable_false_positives),
        "zero_cost_upper_bound_btc_positive": zero_cost_total > 0,
        "costed_btc_positive": costed_total > 0,
        "windows": window_rows,
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 13 首次开仓后不利未配对退出：反事实上界评估",
        "",
        "评估规则固定为：首次开仓后指定 horizon 内仍无配对，且累计不利波动大于有利波动，则立即退出。",
        "",
        "`zero-cost` 把退出费用和滑点设为 0，是该规则不可能超过的乐观上界；`costed` 使用 COST50 的 taker 费与 20 bps 滑点近似。",
        "",
        "| Horizon | 原 BTC PnL | Zero-cost 上界 | COST50 退出估计 | 触发窗口 | 覆盖持续亏损 | 盈利误伤 |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for horizon in HORIZONS:
        item = payload["horizons"][str(horizon)]
        lines.append(
            "| {h} | {base:.4f} | {upper:.4f} | {costed:.4f} | {triggered} | "
            "{covered}/{losses} | {false_positive} |".format(
                h=horizon,
                base=item["baseline_btc_total_pnl"],
                upper=item["zero_cost_guard_btc_total_pnl"],
                costed=item["costed_guard_btc_total_pnl"],
                triggered=item["triggered_window_count"],
                covered=len(item["covered_persistent_loss_windows"]),
                losses=item["persistent_loss_window_count"],
                false_positive=len(item["profitable_false_positive_windows"]),
            )
        )
    lines.extend([
        "",
        "## 判定",
        "",
        "- 30 分钟规则改善最大，但在假设退出完全无成本时，BTC 六种子合计仍为负；",
        "- 30 分钟规则只覆盖 5/7 个持续亏损窗口，并误伤 1 个盈利窗口；",
        "- 因此该结构没有资格进入实现或下一轮候选门禁；",
        "- 不继续搜索 30/60/120 的相邻时间或波动阈值。",
        "",
        f"结论：{payload['conclusion']}",
        "",
        "Final OOS 保持 `SEALED_NOT_EVALUATED`，生产默认值未修改。",
        "",
    ])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估 Round 13 不利未配对退出规则的零成本上界。"
    )
    parser.add_argument(
        "--input",
        default="reports/cross-era-oos/round13-early-path-results.json",
    )
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    input_path = Path(args.input).resolve()
    if _sha256(input_path) != EARLY_PATH_RESULT_SHA256:
        raise ValueError("Round 13 早期路径结果哈希不一致。")
    source = json.loads(input_path.read_text(encoding="utf-8"))
    if source.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Final OOS 已不再封存。")
    horizons = {
        str(horizon): _assess_horizon(source["windows"], horizon)
        for horizon in HORIZONS
    }
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "diagnostic_role": "COUNTERFACTUAL_UPPER_BOUND_ONLY",
        "early_path_result_sha256": EARLY_PATH_RESULT_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "trigger": {
            "zero_pair": True,
            "adverse_excursion_gt_favorable_excursion": True,
        },
        "costed_exit_rate": COST50_EXIT_COST_RATE,
        "horizons": horizons,
        "candidate_preregistered": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": (
            "NO_PREREGISTERED_CANDIDATE：不利未配对退出即使在零退出成本上界下"
            "也不能使 BTC 转正。"
        ),
    }
    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "round13-early-path-assessment.json"
    markdown_output = output_dir / "round13-early-path-assessment.md"
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"Round 13 早期路径评估已存在，拒绝覆盖: {output}")
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
