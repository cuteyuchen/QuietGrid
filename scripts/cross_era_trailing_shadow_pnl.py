from __future__ import annotations

import argparse
import gc
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_long_horizon_regime as round15
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.cross_era_spot_feasibility as spot_round
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_oos import _dataset_brief, _write_json
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256
from scripts.robustness import RobustnessResearch, WindowResult, aggregate_results, verify_frozen_dataset


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round16-trailing-shadow-pnl-protocol.md"
)
PROTOCOL_SHA256 = "5c42008b97f7047048b8cb2aabff9e855335ed9b9d473ed52ab11deb37ea1d90"
ASSET_AUDIT_SHA256 = "3d4c1df25da45f37e9661ae0797baecf4a9e799b42e397687d6eeeb62ac6ab27"
ROUND14_RESULT_SHA256 = "c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f"
ROUND15_RESULT_SHA256 = "131dc847d60012a1dcdf5fc601d5e9a4918ca18e3ff1000fb5f75776f5443fc2"
LOOKBACK_WINDOWS = (4, 8, 13)


def _ordered_window_ids(
    windows: Sequence[Any],
    allowed_ids: Sequence[str],
) -> tuple[str, ...]:
    allowed = set(str(value) for value in allowed_ids)
    if not allowed:
        raise ValueError("滚动影子信号至少需要一个授权窗口。")
    by_window: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str]] = set()
    for window in windows:
        window_id = str(window.window_id)
        if window_id not in allowed:
            continue
        symbol = str(window.symbol).strip().upper()
        key = (window_id, symbol)
        if key in seen:
            raise RuntimeError(f"授权窗口重复: {window_id} {symbol}")
        seen.add(key)
        item = by_window.setdefault(
            window_id,
            {"market_close": window.market_close, "symbols": set()},
        )
        if item["market_close"] != window.market_close:
            raise RuntimeError(f"同一 window id 的 market_close 不一致: {window_id}")
        item["symbols"].add(symbol)
    if set(by_window) != allowed:
        missing = sorted(allowed - set(by_window))
        raise RuntimeError(f"授权窗口未完整加载: {missing[:3]}")
    expected_symbols = set(asset_audit.SYMBOLS)
    for window_id, item in by_window.items():
        if item["symbols"] != expected_symbols:
            raise RuntimeError(f"授权窗口未同时覆盖 BTC/ETH: {window_id}")
    return tuple(
        sorted(by_window, key=lambda value: (by_window[value]["market_close"], value))
    )


def _build_shadow_means(
    raw_runs: Mapping[
        str,
        Mapping[int, Mapping[str, tuple[Any, list[WindowResult]]]],
    ],
    ordered_ids: Sequence[str],
) -> tuple[dict[str, dict[str, dict[str, float]]], dict[str, Any]]:
    expected_ids = set(ordered_ids)
    expected_pairs = {
        (symbol, window_id)
        for symbol in asset_audit.SYMBOLS
        for window_id in ordered_ids
    }
    means: dict[str, dict[str, dict[str, float]]] = {}
    audit: dict[str, Any] = {}
    for scenario, runs_by_seed in raw_runs.items():
        if tuple(sorted(runs_by_seed)) != asset_audit.DEFAULT_SEEDS:
            raise ValueError(f"{scenario} 影子信号种子集合不完整。")
        values = {
            symbol: {window_id: [] for window_id in ordered_ids}
            for symbol in asset_audit.SYMBOLS
        }
        for seed in asset_audit.DEFAULT_SEEDS:
            seen: set[tuple[str, str]] = set()
            for _split_name, (_metrics, results) in runs_by_seed[seed].items():
                for result in results:
                    window_id = str(result.window_id)
                    symbol = str(result.symbol).strip().upper()
                    if window_id not in expected_ids or symbol not in values:
                        raise RuntimeError(
                            f"{scenario} seed {seed} 返回未授权影子结果: "
                            f"{symbol} {window_id}"
                        )
                    key = (symbol, window_id)
                    if key in seen:
                        raise RuntimeError(
                            f"{scenario} seed {seed} 影子结果重复: {symbol} {window_id}"
                        )
                    seen.add(key)
                    values[symbol][window_id].append(float(result.pnl))
            if seen != expected_pairs:
                missing = sorted(expected_pairs - seen)
                extra = sorted(seen - expected_pairs)
                raise RuntimeError(
                    f"{scenario} seed {seed} 影子结果覆盖不完整；"
                    f"missing={missing[:3]} extra={extra[:3]}"
                )
        means[scenario] = {symbol: {} for symbol in asset_audit.SYMBOLS}
        for symbol in asset_audit.SYMBOLS:
            for window_id in ordered_ids:
                seed_values = values[symbol][window_id]
                if len(seed_values) != len(asset_audit.DEFAULT_SEEDS):
                    raise RuntimeError(
                        f"{scenario} {symbol} {window_id} 未恰好覆盖六个种子。"
                    )
                means[scenario][symbol][window_id] = statistics.mean(seed_values)
        audit[scenario] = {
            "window_count": len(ordered_ids),
            "symbol_window_count": len(expected_pairs),
            "seed_count_per_window": len(asset_audit.DEFAULT_SEEDS),
            "complete": True,
        }
    return means, audit


def _trailing_signal_maps(
    shadow_means: Mapping[str, Mapping[str, Mapping[str, float]]],
    ordered_ids: Sequence[str],
    lookback: int,
) -> tuple[dict[str, dict[str, dict[str, float | None]]], dict[str, Any]]:
    if lookback <= 0:
        raise ValueError("滚动影子 lookback 必须大于 0。")
    signals: dict[str, dict[str, dict[str, float | None]]] = {}
    audit: dict[str, Any] = {}
    for scenario, scenario_means in shadow_means.items():
        signals[scenario] = {}
        audit[scenario] = {}
        for symbol in asset_audit.SYMBOLS:
            source = scenario_means[symbol]
            symbol_signals: dict[str, float | None] = {}
            for index, window_id in enumerate(ordered_ids):
                if index < lookback:
                    symbol_signals[window_id] = None
                    continue
                history_ids = ordered_ids[index - lookback : index]
                if window_id in history_ids:
                    raise RuntimeError("滚动影子信号错误包含目标窗口。")
                symbol_signals[window_id] = statistics.mean(
                    float(source[history_id]) for history_id in history_ids
                )
            signals[scenario][symbol] = symbol_signals
            audit[scenario][symbol] = {
                "lookback_windows": lookback,
                "target_count": len(ordered_ids),
                "history_unavailable_count": min(lookback, len(ordered_ids)),
                "first_signal_window_id": (
                    ordered_ids[lookback] if len(ordered_ids) > lookback else None
                ),
                "causal_order_verified": True,
                "self_reference_count": 0,
            }
    return signals, audit


def _apply_trailing_filter(
    result: WindowResult,
    *,
    signal: float | None,
    lookback: int,
) -> WindowResult:
    if result.status != "TRADED":
        return result
    if signal is not None and float(signal) > 0:
        return result
    reason = (
        f"TRAIL_SHADOW_PNL_K{lookback}: HISTORY_UNAVAILABLE"
        if signal is None
        else f"TRAIL_SHADOW_PNL_K{lookback}: {float(signal):.6f} <= 0"
    )
    return RobustnessResearch._blocked_entry_result(result, reason)


def _load_evidence(
    role: str,
    manifests: Sequence[str],
    base_config: Any,
    windows: Sequence[Any],
    split_ids: Mapping[str, Sequence[str]],
    workers: int,
    *,
    end_time: datetime | None = None,
) -> dict[str, Any]:
    allowed_ids = tuple(
        window_id for ids in split_ids.values() for window_id in ids
    )
    ordered_ids = _ordered_window_ids(windows, allowed_ids)
    raw_runs, integrity = round15._run_dataset(
        manifests,
        base_config,
        split_ids,
        workers,
        end_time=end_time,
    )
    if int(integrity["window_count"]) != len(ordered_ids):
        raise RuntimeError(f"{role} worker 窗口覆盖数量不一致。")
    shadow_means, shadow_audit = _build_shadow_means(raw_runs, ordered_ids)
    signals: dict[int, Any] = {}
    signal_audit: dict[int, Any] = {}
    for lookback in LOOKBACK_WINDOWS:
        signals[lookback], signal_audit[lookback] = _trailing_signal_maps(
            shadow_means,
            ordered_ids,
            lookback,
        )
    return {
        "base_config": base_config,
        "split_ids": {name: tuple(ids) for name, ids in split_ids.items()},
        "ordered_ids": ordered_ids,
        "raw_runs": raw_runs,
        "shadow_means": shadow_means,
        "shadow_audit": shadow_audit,
        "signals": signals,
        "signal_audit": signal_audit,
        "execution_integrity": integrity,
    }


def _candidate_cells(
    candidate: Mapping[str, Any],
    datasets: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    cells = {}
    lookback = int(candidate["lookback_windows"])
    for role, dataset in datasets.items():
        for scenario, scenario_runs in dataset["raw_runs"].items():
            for split_name in dataset["split_ids"]:
                cell_name = f"{role}_{split_name.upper()}_{scenario}"
                symbols = {}
                for symbol in asset_audit.SYMBOLS:
                    metrics_by_seed = {}
                    for seed in asset_audit.DEFAULT_SEEDS:
                        results = scenario_runs[seed][split_name][1]
                        transformed = [
                            _apply_trailing_filter(
                                result,
                                signal=(
                                    dataset["signals"][lookback][scenario][symbol][
                                        result.window_id
                                    ]
                                ),
                                lookback=lookback,
                            )
                            for result in results
                            if result.symbol == symbol
                        ]
                        metrics_by_seed[seed] = aggregate_results(
                            transformed,
                            capital_per_symbol=float(
                                dataset["base_config"].capital_by_symbol[symbol]
                            ),
                            symbol_count=1,
                        )
                    summary = asset_audit._summarize_symbol(metrics_by_seed)
                    checks = asset_audit._scope_checks(summary)
                    symbols[symbol] = {
                        "summary": summary,
                        "checks": checks,
                        "passed": all(checks.values()),
                    }
                cells[cell_name] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "window_count": len(dataset["split_ids"][split_name]),
                    "symbols": symbols,
                }
    return cells


def _select_candidate(candidates: Sequence[Mapping[str, Any]]) -> str | None:
    eligible = [
        item for item in candidates if item["selection"]["all_cells_passed"]
    ]
    if not eligible:
        return None
    selected = max(
        eligible,
        key=lambda item: (
            float(item["selection"]["minimum_worst_seed_total_pnl"]),
            float(item["selection"]["minimum_trade_coverage"]),
            int(item["lookback_windows"]),
        ),
    )
    return str(selected["candidate_id"])


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 16：滚动影子 PnL Phase A 结果",
        "",
        "固定策略过去 K 个已完成窗口的六种子平均影子 PnL 必须大于 0；CURRENT Final OOS 未评估。",
        "",
        "| 候选 | Lookback | 通过单元 | 最差种子 PnL | 最低覆盖 | 全通过 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for candidate in payload["candidates"]:
        selection = candidate["selection"]
        lines.append(
            "| `{candidate_id}` | {lookback} | {passed}/{total} | "
            "{worst:.4f} | {coverage:.2%} | {eligible} |".format(
                candidate_id=candidate["candidate_id"],
                lookback=candidate["lookback_windows"],
                passed=selection["passed_cell_symbol_count"],
                total=selection["cell_symbol_count"],
                worst=selection["minimum_worst_seed_total_pnl"],
                coverage=selection["minimum_trade_coverage"],
                eligible="是" if selection["all_cells_passed"] else "否",
            )
        )
    lines.extend(
        [
            "",
            f"选中候选：{payload['selected_candidate_id'] or '无'}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "生产默认值未修改；没有独立授权文件时，CURRENT Final OOS 继续封存。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用过去已完成周末窗口的固定策略影子 PnL 做跨周期 Phase A。"
    )
    parser.add_argument(
        "--round12-result",
        default="reports/cross-era-oos/round12-quadratic-volatility-defense-results.json",
    )
    parser.add_argument(
        "--round13-result",
        default="reports/cross-era-oos/round13-prehistory-quadratic-w2160-results.json",
    )
    parser.add_argument(
        "--asset-audit-result",
        default="reports/cross-era-oos/asset-scope-audit-results.json",
    )
    parser.add_argument(
        "--round14-result",
        default="reports/cross-era-oos/round14-spot-feasibility-results.json",
    )
    parser.add_argument(
        "--round15-result",
        default="reports/cross-era-oos/round15-long-horizon-results.json",
    )
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 16 协议哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    asset_path = Path(args.asset_audit_result).resolve()
    round14_path = Path(args.round14_result).resolve()
    round15_path = Path(args.round15_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        asset_path: ASSET_AUDIT_SHA256,
        round14_path: ROUND14_RESULT_SHA256,
        round15_path: ROUND15_RESULT_SHA256,
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")

    round12_payload = json.loads(round12_path.read_text(encoding="utf-8"))
    round13_payload = json.loads(round13_path.read_text(encoding="utf-8"))
    round14_payload = json.loads(round14_path.read_text(encoding="utf-8"))
    round15_payload = json.loads(round15_path.read_text(encoding="utf-8"))
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")
    if bool(round12_payload.get("final_oos_authorized")):
        raise ValueError("CURRENT Final OOS 被错误授权。")
    if round15_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 15 已存在候选，不能启动失败后 Round 16。")
    if round15_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 15 之后 CURRENT Final OOS 已不再封存。")
    if not str(round15_payload.get("conclusion") or "").startswith(
        "NO_ROBUST_LONG_HORIZON_CANDIDATE"
    ):
        raise ValueError("Round 15 失败结论不匹配。")

    datasets: dict[str, dict[str, Any]] = {}

    current_manifests = tuple(str(item["manifest"]) for item in round12_payload["datasets"])
    current_config = profit_opt._base_research_config()
    current_metadata = [verify_frozen_dataset(path) for path in current_manifests]
    current_end, development_ids, validation_ids, current_isolation = (
        round15._current_authorized_end(current_metadata, current_config)
    )
    current_metadata, current_windows = round15._load_dataset(
        current_manifests,
        current_config,
        end_time=current_end,
    )
    asset_audit._validate_frozen_dataset(
        _dataset_brief(current_manifests, current_metadata),
        round12_payload["datasets"],
        label="CURRENT",
    )
    if {window.window_id for window in current_windows} != set(
        development_ids + validation_ids
    ):
        raise RuntimeError("CURRENT 授权窗口与日历切分不一致。")
    print("DATASET CURRENT", flush=True)
    datasets["CURRENT"] = _load_evidence(
        "CURRENT",
        current_manifests,
        current_config,
        current_windows,
        {"development": development_ids, "validation": validation_ids},
        args.workers,
        end_time=current_end,
    )
    del current_windows
    gc.collect()

    prehistory_manifests = tuple(
        str(item["manifest"]) for item in round13_payload["datasets"]
    )
    prehistory_config = profit_opt._base_research_config()
    prehistory_metadata, prehistory_windows = round15._load_dataset(
        prehistory_manifests,
        prehistory_config,
    )
    asset_audit._validate_frozen_dataset(
        _dataset_brief(prehistory_manifests, prehistory_metadata),
        round13_payload["datasets"],
        label="PREHISTORY",
    )
    prehistory_ids = round13._paired_ready_window_ids(prehistory_windows)
    print("DATASET PREHISTORY", flush=True)
    datasets["PREHISTORY"] = _load_evidence(
        "PREHISTORY",
        prehistory_manifests,
        prehistory_config,
        prehistory_windows,
        {"external": prehistory_ids},
        args.workers,
    )
    del prehistory_windows
    gc.collect()

    spot_manifests = tuple(str(item["manifest"]) for item in round14_payload["datasets"])
    spot_config = profit_opt._base_research_config()
    spot_metadata, spot_windows = round15._load_dataset(spot_manifests, spot_config)
    asset_audit._validate_frozen_dataset(
        _dataset_brief(spot_manifests, spot_metadata),
        round14_payload["datasets"],
        label="SPOT",
    )
    spot_ids, spot_quality = spot_round._paired_contiguous_window_ids(spot_windows)
    if spot_quality != round14_payload["data_quality"]:
        raise ValueError("Round 14 Spot 连续窗口质量记录不一致。")
    print("DATASET SPOT", flush=True)
    datasets["SPOT"] = _load_evidence(
        "SPOT",
        spot_manifests,
        spot_config,
        spot_windows,
        {"external": spot_ids},
        args.workers,
    )
    del spot_windows
    gc.collect()

    evaluated = []
    for lookback in LOOKBACK_WINDOWS:
        candidate = {
            "candidate_id": f"TRAIL_SHADOW_PNL_K{lookback}",
            "lookback_windows": lookback,
            "threshold": 0.0,
        }
        cells = _candidate_cells(candidate, datasets)
        evaluated.append(
            {
                **candidate,
                "cells": cells,
                "selection": round15._selection_metrics(cells),
            }
        )
    selected_candidate_id = _select_candidate(evaluated)
    eligible_ids = [
        item["candidate_id"]
        for item in evaluated
        if item["selection"]["all_cells_passed"]
    ]
    conclusion = (
        "TRAILING_SHADOW_CANDIDATE_READY_FOR_AUTHORIZATION："
        f"{selected_candidate_id} 通过全部 16 个 Phase A 单元；尚未授权 Final OOS。"
        if selected_candidate_id
        else "NO_ROBUST_TRAILING_SHADOW_CANDIDATE：3 个滚动影子 PnL 候选均未通过全部 16 个 Phase A 单元。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "direction_mode": "NEUTRAL",
        "seeds": list(asset_audit.DEFAULT_SEEDS),
        "lookback_windows": list(LOOKBACK_WINDOWS),
        "signal_threshold": 0.0,
        "signal_uses_unfiltered_shadow_policy": True,
        "signal_is_recursive": False,
        "current_isolation": current_isolation,
        "shadow_audit": {
            role: dataset["shadow_audit"] for role, dataset in datasets.items()
        },
        "signal_audit": {
            role: dataset["signal_audit"] for role, dataset in datasets.items()
        },
        "execution_integrity": {
            role: dataset["execution_integrity"] for role, dataset in datasets.items()
        },
        "candidates": evaluated,
        "eligible_candidate_ids": eligible_ids,
        "selected_candidate_id": selected_candidate_id,
        "final_oos_authorization_ready": selected_candidate_id is not None,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "round16-trailing-shadow-pnl-results.json"
    markdown_output = output_dir / "round16-trailing-shadow-pnl-report.md"
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"Round 16 结果已存在，拒绝覆盖: {output}")
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
