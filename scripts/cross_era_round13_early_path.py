from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.profit_protection_optimize as profit_opt
import scripts.robustness as robustness
from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
)
from scripts.cross_era_oos import _dataset_brief, _registered_candidates, _write_json
from scripts.cross_era_round13_diagnose import (
    ROUND13_RESULT_SHA256,
    _sha256,
    _validate_round13_result,
)
from scripts.profit_protection_optimize import COST_50, DEFAULT_SEEDS, CandidateEvidence
from scripts.robustness import RobustnessResearch, WindowResult
from strategy.backtest import BacktestFill, BacktestResult


UTC = timezone.utc
ROUND13_DIAGNOSTIC_SHA256 = (
    "97f8a3a795cd428c2eb645f98908ddd75efe49ee7bc040ded0520b2529a28150"
)
HORIZONS = (30, 60, 120)


def _inventory_state(
    fills: Sequence[BacktestFill],
    bar_index: int,
) -> dict[str, float | str]:
    long_qty = 0.0
    short_qty = 0.0
    for fill in fills:
        if fill.bar_index > bar_index:
            break
        intent = str(fill.order_intent).upper()
        position_side = str(fill.position_side).upper()
        if position_side not in {"LONG", "SHORT"}:
            position_side = "LONG" if fill.side.upper() == "BUY" else "SHORT"
        if intent in {"OPEN", "SEED"}:
            if position_side == "LONG":
                long_qty += float(fill.qty)
            else:
                short_qty += float(fill.qty)
        elif intent == "REDUCE":
            if position_side == "LONG":
                long_qty = max(0.0, long_qty - float(fill.qty))
            else:
                short_qty = max(0.0, short_qty - float(fill.qty))
    net_qty = long_qty - short_qty
    net_side = "FLAT"
    if net_qty > 1e-12:
        net_side = "LONG"
    elif net_qty < -1e-12:
        net_side = "SHORT"
    return {
        "long_qty": long_qty,
        "short_qty": short_qty,
        "gross_qty": long_qty + short_qty,
        "net_qty": net_qty,
        "net_side": net_side,
    }


def _equity_point_at(result: BacktestResult, bar_index: int) -> Any:
    candidates = [
        point for point in result.equity_curve if int(point.bar_index) <= bar_index
    ]
    if not candidates:
        raise ValueError(f"回测在 bar {bar_index} 前没有权益点。")
    return candidates[-1]


def _checkpoint_summary(
    result: BacktestResult,
    klines: Sequence[Mapping[str, Any]],
    first_fill: BacktestFill,
    horizon: int,
) -> dict[str, Any]:
    if horizon <= 0:
        raise ValueError("诊断 horizon 必须大于 0。")
    start_bar = int(first_fill.bar_index)
    target_bar = start_bar + horizon - 1
    last_bar = int(result.equity_curve[-1].bar_index)
    observed_bar = min(target_bar, last_bar, len(klines) - 1)
    if observed_bar < start_bar:
        raise ValueError("首个开仓成交发生在权益曲线之后。")
    point = _equity_point_at(result, observed_bar)
    segment = klines[start_bar : observed_bar + 1]
    entry_price = float(first_fill.price)
    close = float(point.close)
    raw_return = close / entry_price - 1.0
    position_side = str(first_fill.position_side).upper()
    if position_side not in {"LONG", "SHORT"}:
        position_side = "LONG" if first_fill.side.upper() == "BUY" else "SHORT"
    maximum_high = max(float(row["high"]) for row in segment)
    minimum_low = min(float(row["low"]) for row in segment)
    if position_side == "LONG":
        favorable = max(0.0, maximum_high / entry_price - 1.0)
        adverse = max(0.0, 1.0 - minimum_low / entry_price)
        directional_return = raw_return
    else:
        favorable = max(0.0, 1.0 - minimum_low / entry_price)
        adverse = max(0.0, maximum_high / entry_price - 1.0)
        directional_return = -raw_return
    closes = [entry_price, *(float(row["close"]) for row in segment)]
    path_length = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
    path_efficiency = abs(close - entry_price) / path_length if path_length else 0.0
    observed_fills = [
        fill
        for fill in result.fills
        if start_bar <= int(fill.bar_index) <= observed_bar
    ]
    pair_count = sum(
        fill.grid_pnl is not None and str(fill.order_intent).upper() == "REDUCE"
        for fill in observed_fills
    )
    inventory = _inventory_state(result.fills, observed_bar)
    return {
        "horizon_bars": horizon,
        "target_bar_index": target_bar,
        "observed_bar_index": observed_bar,
        "observed_bars": observed_bar - start_bar + 1,
        "full_horizon_observed": observed_bar >= target_bar,
        "close": close,
        "raw_return_pct": raw_return,
        "directional_return_pct": directional_return,
        "favorable_excursion_pct": favorable,
        "adverse_excursion_pct": adverse,
        "path_efficiency": path_efficiency,
        "fill_count": len(observed_fills),
        "pair_count": pair_count,
        "zero_pair": pair_count == 0,
        "equity": float(point.equity),
        "realized_pnl": float(point.realized_pnl),
        "unrealized_pnl": float(point.unrealized_pnl),
        "drawdown": float(point.drawdown),
        "gross_inventory_notional": float(point.gross_inventory_notional),
        "inventory_utilization": float(point.inventory_utilization),
        **inventory,
        "adverse_unpaired_inventory": (
            directional_return < 0
            and pair_count == 0
            and float(inventory["gross_qty"]) > 0
        ),
        "stopped_before_or_at_horizon": (
            result.stopped_at_index is not None
            and int(result.stopped_at_index) <= target_bar
        ),
    }


def _capture_backtest(
    result: BacktestResult,
    klines: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    opening_fills = [
        fill
        for fill in result.fills
        if str(fill.order_intent).upper() in {"OPEN", "SEED"}
        and int(fill.bar_index) >= 0
    ]
    if not opening_fills:
        return {
            "entered": False,
            "total_pnl": result.total_pnl,
            "stopped_reason": result.stopped_reason,
            "stopped_at_index": result.stopped_at_index,
            "fill_count": len(result.fills),
            "pair_count": result.pair_completion_count,
        }
    first_fill = opening_fills[0]
    position_side = str(first_fill.position_side).upper()
    if position_side not in {"LONG", "SHORT"}:
        position_side = "LONG" if first_fill.side.upper() == "BUY" else "SHORT"
    paired_fills = [
        fill
        for fill in result.fills
        if fill.grid_pnl is not None
        and str(fill.order_intent).upper() == "REDUCE"
        and int(fill.bar_index) >= int(first_fill.bar_index)
    ]
    return {
        "entered": True,
        "first_entry_bar_index": int(first_fill.bar_index),
        "first_entry_price": float(first_fill.price),
        "first_entry_side": position_side,
        "first_pair_bar_index": (
            int(paired_fills[0].bar_index) if paired_fills else None
        ),
        "bars_to_first_pair": (
            int(paired_fills[0].bar_index) - int(first_fill.bar_index)
            if paired_fills
            else None
        ),
        "total_pnl": float(result.total_pnl),
        "stopped_reason": result.stopped_reason,
        "stopped_at_index": result.stopped_at_index,
        "bars_from_entry_to_stop": (
            int(result.stopped_at_index) - int(first_fill.bar_index)
            if result.stopped_at_index is not None
            else None
        ),
        "fill_count": len(result.fills),
        "pair_count": int(result.pair_completion_count),
        "max_inventory_utilization": float(result.max_inventory_utilization),
        "checkpoints": {
            str(horizon): _checkpoint_summary(result, klines, first_fill, horizon)
            for horizon in HORIZONS
        },
    }


def _early_path_seed_worker(
    seed: int,
    window_ids: Sequence[str],
) -> tuple[
    int,
    dict[str, tuple[Any, list[WindowResult]]],
    dict[tuple[str, str], dict[str, Any]],
    dict[str, Any],
]:
    state = profit_opt._WORKER_STATE
    allowed = set(window_ids)
    lookup = {
        (
            window.symbol,
            int(window.rows[window.observation_rows].open_time),
        ): window.window_id
        for window in state["windows"]
        if window.window_id in allowed
        and window.status == "READY"
        and len(window.rows) > window.observation_rows
    }
    captures: dict[tuple[str, str], dict[str, Any]] = {}
    original = robustness.run_grid_backtest

    def capturing_backtest(*args: Any, **kwargs: Any) -> BacktestResult:
        result = original(*args, **kwargs)
        params = args[0]
        klines = args[1]
        first_open_time = int(klines[0].get("open_time", klines[0]["timestamp"]))
        key = (str(params.symbol).upper(), first_open_time)
        window_id = lookup.get(key)
        if window_id is None:
            raise RuntimeError(f"无法定位诊断回测窗口: {key}")
        capture_key = (key[0], window_id)
        if capture_key in captures:
            raise RuntimeError(f"诊断窗口被重复执行: {capture_key}")
        captures[capture_key] = _capture_backtest(result, klines)
        return result

    robustness.run_grid_backtest = capturing_backtest
    try:
        variant, returned_seed, runs, observation = round13._external_seed_worker(
            round13.CANDIDATE_ID,
            seed,
            window_ids,
            COST_50,
        )
    finally:
        robustness.run_grid_backtest = original
    if variant != round13.CANDIDATE_ID or returned_seed != seed:
        raise RuntimeError("早期路径 worker 返回了错误任务标识。")
    return seed, runs["external"], captures, observation


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _checkpoint_aggregate(records: Sequence[dict[str, Any]], horizon: int) -> dict[str, Any]:
    checkpoints = [record["checkpoints"][str(horizon)] for record in records]
    adverse = [float(item["adverse_excursion_pct"]) for item in checkpoints]
    favorable = [float(item["favorable_excursion_pct"]) for item in checkpoints]
    directional = [float(item["directional_return_pct"]) for item in checkpoints]
    return {
        "record_count": len(checkpoints),
        "full_horizon_count": sum(bool(item["full_horizon_observed"]) for item in checkpoints),
        "mean_adverse_excursion_pct": _mean(adverse),
        "median_adverse_excursion_pct": statistics.median(adverse),
        "maximum_adverse_excursion_pct": max(adverse, default=0.0),
        "mean_favorable_excursion_pct": _mean(favorable),
        "median_favorable_excursion_pct": statistics.median(favorable),
        "mean_directional_return_pct": _mean(directional),
        "median_directional_return_pct": statistics.median(directional),
        "mean_path_efficiency": _mean([
            float(item["path_efficiency"]) for item in checkpoints
        ]),
        "zero_pair_count": sum(bool(item["zero_pair"]) for item in checkpoints),
        "zero_pair_rate": _mean([
            float(bool(item["zero_pair"])) for item in checkpoints
        ]),
        "adverse_unpaired_inventory_count": sum(
            bool(item["adverse_unpaired_inventory"]) for item in checkpoints
        ),
        "adverse_unpaired_inventory_rate": _mean([
            float(bool(item["adverse_unpaired_inventory"])) for item in checkpoints
        ]),
        "mean_inventory_utilization": _mean([
            float(item["inventory_utilization"]) for item in checkpoints
        ]),
        "mean_equity": _mean([float(item["equity"]) for item in checkpoints]),
        "mean_fill_count": _mean([
            float(item["fill_count"]) for item in checkpoints
        ]),
        "mean_pair_count": _mean([
            float(item["pair_count"]) for item in checkpoints
        ]),
    }


def _aggregate_records(
    records: Sequence[dict[str, Any]],
    labels: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["window_id"])].append(record)
    windows = []
    for window_id, items in grouped.items():
        items.sort(key=lambda item: int(item["seed"]))
        if tuple(int(item["seed"]) for item in items) != DEFAULT_SEEDS:
            raise ValueError(f"窗口 {window_id} 的早期路径种子不完整。")
        first_entry = [int(item["first_entry_bar_index"]) for item in items]
        stop_offsets = [
            int(item["bars_from_entry_to_stop"])
            for item in items
            if item["bars_from_entry_to_stop"] is not None
        ]
        windows.append({
            "window_id": window_id,
            "label": labels[window_id],
            "seed_count": len(items),
            "mean_pnl": _mean([float(item["pnl"]) for item in items]),
            "first_entry_side_counts": dict(sorted(Counter(
                str(item["first_entry_side"]) for item in items
            ).items())),
            "median_first_entry_bar_index": statistics.median(first_entry),
            "minimum_first_entry_bar_index": min(first_entry),
            "maximum_first_entry_bar_index": max(first_entry),
            "stopped_seed_count": len(stop_offsets),
            "median_bars_from_entry_to_stop": (
                statistics.median(stop_offsets) if stop_offsets else None
            ),
            "no_pair_final_seed_count": sum(int(item["pair_count"]) == 0 for item in items),
            "checkpoints": {
                str(horizon): _checkpoint_aggregate(items, horizon)
                for horizon in HORIZONS
            },
            "seed_records": items,
        })
    windows.sort(key=lambda item: (float(item["mean_pnl"]), item["window_id"]))

    group_summary = {}
    for label in sorted(set(labels.values())):
        members = [record for record in records if labels[record["window_id"]] == label]
        group_summary[label] = {
            "window_count": len({record["window_id"] for record in members}),
            "record_count": len(members),
            "mean_pnl": _mean([float(record["pnl"]) for record in members]),
            "median_first_entry_bar_index": statistics.median([
                int(record["first_entry_bar_index"]) for record in members
            ]),
            "no_pair_final_rate": _mean([
                float(int(record["pair_count"]) == 0) for record in members
            ]),
            "checkpoints": {
                str(horizon): _checkpoint_aggregate(members, horizon)
                for horizon in HORIZONS
            },
        }
    return windows, group_summary


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 13 BTC 首次开仓后 30/60/120 分钟路径诊断",
        "",
        "本报告只重放 2020H1 外部区间的 Round 13 候选 COST50；Final OOS 未读取。",
        "",
        f"- 固定种子：{', '.join(str(seed) for seed in payload['seeds'])}",
        f"- 已交易 BTC 窗口：{payload['traded_window_count']}",
        f"- Final OOS：`{payload['final_oos_status']}`",
        "",
        "## 亏损窗口与盈利窗口对比",
        "",
        "| 组别 | 窗口 | 记录 | 最终无配对率 | 30m 不利/有利 | 60m 不利/有利 | 120m 不利/有利 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, summary in payload["group_summary"].items():
        checkpoints = summary["checkpoints"]
        lines.append(
            "| {label} | {windows} | {records} | {no_pair:.2%} | "
            "{a30:.2%}/{f30:.2%} | {a60:.2%}/{f60:.2%} | "
            "{a120:.2%}/{f120:.2%} |".format(
                label=label,
                windows=summary["window_count"],
                records=summary["record_count"],
                no_pair=summary["no_pair_final_rate"],
                a30=checkpoints["30"]["mean_adverse_excursion_pct"],
                f30=checkpoints["30"]["mean_favorable_excursion_pct"],
                a60=checkpoints["60"]["mean_adverse_excursion_pct"],
                f60=checkpoints["60"]["mean_favorable_excursion_pct"],
                a120=checkpoints["120"]["mean_adverse_excursion_pct"],
                f120=checkpoints["120"]["mean_favorable_excursion_pct"],
            )
        )
    lines.extend([
        "",
        "## 持续亏损窗口",
        "",
        "| 窗口 | 均值 PnL | 首次开仓 bar | 最终无配对种子 | 30m 不利且未配对 | 60m 不利且未配对 | 120m 不利且未配对 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for row in payload["windows"]:
        if row["label"] != "PERSISTENT_LOSS":
            continue
        lines.append(
            "| `{window}` | {pnl:.4f} | {entry:.0f} | {no_pair}/6 | "
            "{p30}/6 | {p60}/6 | {p120}/6 |".format(
                window=row["window_id"],
                pnl=row["mean_pnl"],
                entry=row["median_first_entry_bar_index"],
                no_pair=row["no_pair_final_seed_count"],
                p30=row["checkpoints"]["30"]["adverse_unpaired_inventory_count"],
                p60=row["checkpoints"]["60"]["adverse_unpaired_inventory_count"],
                p120=row["checkpoints"]["120"]["adverse_unpaired_inventory_count"],
            )
        )
    lines.extend([
        "",
        "## 研究边界",
        "",
        "- 这些路径来自已经消费的 2020H1 外部区间，只能用于形成结构性假设；",
        "- 本报告不自动搜索阈值，也不注册候选；",
        "- Final OOS 保持 `SEALED_NOT_EVALUATED`；",
        "- `direction_mode` 保持 `NEUTRAL`；",
        "- 生产默认值未修改。",
        "",
    ])
    return "\n".join(lines)


def _write_csv(path: Path, records: Sequence[dict[str, Any]]) -> None:
    rows = []
    for record in records:
        for horizon in HORIZONS:
            checkpoint = record["checkpoints"][str(horizon)]
            rows.append({
                "seed": record["seed"],
                "window_id": record["window_id"],
                "label": record["label"],
                "pnl": record["pnl"],
                "first_entry_bar_index": record["first_entry_bar_index"],
                "first_entry_side": record["first_entry_side"],
                "bars_from_entry_to_stop": record["bars_from_entry_to_stop"],
                "final_pair_count": record["pair_count"],
                "horizon_bars": horizon,
                "directional_return_pct": checkpoint["directional_return_pct"],
                "favorable_excursion_pct": checkpoint["favorable_excursion_pct"],
                "adverse_excursion_pct": checkpoint["adverse_excursion_pct"],
                "path_efficiency": checkpoint["path_efficiency"],
                "fill_count": checkpoint["fill_count"],
                "pair_count": checkpoint["pair_count"],
                "zero_pair": checkpoint["zero_pair"],
                "adverse_unpaired_inventory": checkpoint[
                    "adverse_unpaired_inventory"
                ],
                "net_side": checkpoint["net_side"],
                "net_qty": checkpoint["net_qty"],
                "inventory_utilization": checkpoint["inventory_utilization"],
                "equity": checkpoint["equity"],
            })
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="诊断 Round 13 BTC 首次开仓后的 30/60/120 分钟路径。"
    )
    parser.add_argument(
        "--round13-result",
        default="reports/cross-era-oos/round13-prehistory-quadratic-w2160-results.json",
    )
    parser.add_argument(
        "--round13-diagnostic",
        default="reports/cross-era-oos/round13-diagnostics.json",
    )
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    result_path = Path(args.round13_result).resolve()
    diagnostic_path = Path(args.round13_diagnostic).resolve()
    if _sha256(result_path) != ROUND13_RESULT_SHA256:
        raise ValueError("Round 13 冻结结果哈希不一致。")
    if _sha256(diagnostic_path) != ROUND13_DIAGNOSTIC_SHA256:
        raise ValueError("Round 13 全种子诊断哈希不一致。")
    round13_payload = json.loads(result_path.read_text(encoding="utf-8"))
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    _validate_round13_result(round13_payload)
    if diagnostic.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 13 诊断中的 Final OOS 已不再封存。")

    manifests = tuple(str(item["manifest"]) for item in round13_payload["datasets"])
    base_config = profit_opt._base_research_config()
    metadata, windows = profit_opt._load_data(manifests, base_config)
    if _dataset_brief(manifests, metadata) != round13_payload["datasets"]:
        raise ValueError("当前冻结数据与 Round 13 结果不一致。")
    external_ids = round13._paired_ready_window_ids(windows)

    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(DEFAULT_SEEDS)),
        initializer=profit_opt._initialize_worker,
        initargs=(manifests, base_config),
    ) as executor:
        for seed in DEFAULT_SEEDS:
            print(f"CAPTURING COST50 SEED {seed}", flush=True)
            future = executor.submit(_early_path_seed_worker, seed, external_ids)
            futures[future] = seed
        raw_runs = {}
        captures = {}
        observations = []
        for future in concurrent.futures.as_completed(futures):
            expected_seed = futures[future]
            seed, run, seed_captures, observation = future.result()
            if seed != expected_seed:
                raise RuntimeError("早期路径 worker 返回了错误种子。")
            raw_runs[seed] = {"external": run}
            captures[seed] = seed_captures
            observations.append(observation)
    if not observations or any(item != observations[0] for item in observations[1:]):
        raise RuntimeError("早期路径 worker 执行参数不一致。")
    if observations[0] != round13_payload["execution_integrity"][round13.CANDIDATE_ID]:
        raise RuntimeError("早期路径重放与 Round 13 执行完整性不一致。")

    locked_parameters, _symbol_policies, _maker_policy = round13._locked_policy()
    research = RobustnessResearch(
        windows,
        locked_parameters,
        base_config,
        dataset_metadata=metadata,
    )
    contexts = _populate_entry_decisions(research, external_ids)
    raw_evidence = CandidateEvidence(_registered_candidates()[0], raw_runs)
    filtered = _filtered_evidence_for_symbols(
        raw_evidence,
        round13.FIXED_FILTERS,
        contexts,
        candidate_id=round13.CANDIDATE_ID,
        round_name="round13_early_path_diagnostic",
        split_name="external",
    )

    expected_windows = {
        row["window_id"]: row
        for row in diagnostic["scenarios"]["COST50"]["candidate_windows"]
    }
    persistent_ids = {
        row["window_id"] for row in diagnostic["persistent_loss_windows"]
    }
    labels = {
        window_id: (
            "PERSISTENT_LOSS"
            if window_id in persistent_ids
            else "PROFITABLE"
        )
        for window_id, row in expected_windows.items()
        if int(row["traded_seed_count"]) > 0
    }
    records = []
    for seed in DEFAULT_SEEDS:
        results = filtered.runs[seed]["external"][1]
        for outcome in results:
            if outcome.symbol != "BTCUSDT" or outcome.status != "TRADED":
                continue
            capture = captures[seed].get((outcome.symbol, outcome.window_id))
            if capture is None or not capture.get("entered"):
                raise RuntimeError(
                    f"缺少 BTC 已交易窗口的早期路径: seed={seed} {outcome.window_id}"
                )
            expected = next(
                item
                for item in expected_windows[outcome.window_id]["seed_results"]
                if int(item["seed"]) == seed
            )
            if abs(float(expected["pnl"]) - float(outcome.pnl)) > 1e-9:
                raise RuntimeError("早期路径重放 PnL 与全种子诊断不一致。")
            if abs(float(capture["total_pnl"]) - float(outcome.pnl)) > 1e-9:
                raise RuntimeError("捕获的回测 PnL 与 WindowResult 不一致。")
            records.append({
                "seed": seed,
                "window_id": outcome.window_id,
                "label": labels[outcome.window_id],
                "pnl": float(outcome.pnl),
                **capture,
            })
    expected_record_count = len(labels) * len(DEFAULT_SEEDS)
    if len(records) != expected_record_count:
        raise RuntimeError(
            "早期路径记录数量不一致: "
            f"expected={expected_record_count} actual={len(records)}"
        )
    windows_payload, group_summary = _aggregate_records(records, labels)

    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "round13-early-path-results.json"
    markdown_output = output_dir / "round13-early-path-report.md"
    csv_output = output_dir / "round13-early-path.csv"
    for output in (json_output, markdown_output, csv_output):
        if output.exists():
            raise FileExistsError(f"Round 13 早期路径结果已存在，拒绝覆盖: {output}")
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "diagnostic_role": "POST_ENTRY_CAUSAL_PATH_DIAGNOSTIC_ONLY",
        "round13_result_sha256": ROUND13_RESULT_SHA256,
        "round13_diagnostic_sha256": ROUND13_DIAGNOSTIC_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "datasets": round13_payload["datasets"],
        "scenario": "COST50",
        "seeds": list(DEFAULT_SEEDS),
        "horizons": list(HORIZONS),
        "traded_window_count": len(labels),
        "record_count": len(records),
        "windows": windows_payload,
        "group_summary": group_summary,
        "candidate_preregistered": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
    }
    _write_json(json_output, payload)
    markdown_output.write_text(_report_markdown(payload), encoding="utf-8")
    _write_csv(csv_output, records)
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
