from __future__ import annotations

import argparse
import concurrent.futures
import csv
import gc
import json
import math
import os
from collections import deque
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.cross_era_spot_feasibility as spot_round
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_oos import _dataset_brief, _load_research_state, _write_json
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256
from scripts.robustness import (
    CSV_FIELDS,
    RobustnessResearch,
    WindowResult,
    _empirical_quantile,
    _weekend_boundaries,
    aggregate_results,
    load_weekend_windows,
    verify_frozen_dataset,
)


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round15-long-horizon-regime-protocol.md"
)
PROTOCOL_SHA256 = "a81b7666ff229c758955f08d3d5a6682228f539eec20340484380d95d8b4420f"
ASSET_AUDIT_SHA256 = "3d4c1df25da45f37e9661ae0797baecf4a9e799b42e397687d6eeeb62ac6ab27"
ROUND14_RESULT_SHA256 = "c927ad9c955a5e38ee03f834da641cf433c7b244dfeceb34389cdc794170e54f"
LOOKBACKS = (1440, 4320, 10080)
QUANTILES = (0.60, 0.70, 0.80, 0.90)
CURRENT_SPLIT_COUNTS = {"development": 108, "validation": 54, "final_oos": 54}


def _load_dataset(
    manifests: Sequence[str],
    base_config: Any,
    *,
    end_time: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    metadata: list[dict[str, Any]] = []
    windows: list[Any] = []
    for manifest in manifests:
        item = verify_frozen_dataset(manifest)
        metadata.append(item)
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=base_config.observation_rows,
                force_close_minutes=base_config.force_close_minutes,
                minimum_tradable_rows=base_config.minimum_tradable_rows,
                end_time=end_time,
                verified_manifest=item,
            )
        )
    symbols = {str(item.get("symbol") or "").strip().upper() for item in metadata}
    if symbols != set(asset_audit.SYMBOLS):
        raise ValueError("长期方向效率研究必须且只能包含 BTCUSDT、ETHUSDT。")
    return metadata, windows


def _current_authorized_end(
    metadata: Sequence[Mapping[str, Any]],
    base_config: Any,
) -> tuple[datetime, tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    boundary_sets = []
    for item in metadata:
        start = datetime.fromisoformat(str(item["actual_start"])).astimezone(UTC)
        end = datetime.fromisoformat(str(item["actual_end"])).astimezone(UTC)
        boundary_sets.append(
            _weekend_boundaries(start, end, base_config.force_close_minutes)
        )
    if not boundary_sets or any(value != boundary_sets[0] for value in boundary_sets[1:]):
        raise RuntimeError("CURRENT BTC/ETH 日历窗口不一致。")
    boundaries = boundary_sets[0]
    expected_total = sum(CURRENT_SPLIT_COUNTS.values())
    if len(boundaries) != expected_total:
        raise RuntimeError(
            f"CURRENT 日历窗口数量不一致: {len(boundaries)} != {expected_total}"
        )
    authorized_count = (
        CURRENT_SPLIT_COUNTS["development"] + CURRENT_SPLIT_COUNTS["validation"]
    )
    authorized = boundaries[:authorized_count]
    ids = tuple(
        f"nyse_{market_close.strftime('%Y%m%dT%H%M%SZ')}"
        for market_close, _force_close_at in authorized
    )
    development = ids[: CURRENT_SPLIT_COUNTS["development"]]
    validation = ids[CURRENT_SPLIT_COUNTS["development"] :]
    return authorized[-1][1], development, validation, {
        "development_count": len(development),
        "validation_count": len(validation),
        "sealed_final_oos_count": CURRENT_SPLIT_COUNTS["final_oos"],
        "authorized_data_end": authorized[-1][1].isoformat(),
    }


def _initialize_worker(
    manifests: Sequence[str],
    base_config: Any,
    end_time_iso: str | None,
) -> None:
    end_time = datetime.fromisoformat(end_time_iso) if end_time_iso else None
    metadata, windows = _load_dataset(manifests, base_config, end_time=end_time)
    parameters, symbol_policies, maker_policy = profit_opt._locked_policy()
    policies_without_short_filter = {
        symbol: replace(policy, entry_filter=None)
        for symbol, policy in symbol_policies.items()
    }
    profit_opt._WORKER_STATE.clear()
    profit_opt._WORKER_STATE.update(
        {
            "metadata": metadata,
            "windows": windows,
            "parameters": parameters,
            "symbol_policies": policies_without_short_filter,
            "maker_policy": maker_policy,
            "base_config": base_config,
        }
    )


def _seed_worker(
    seed: int,
    split_ids: Mapping[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[int, dict[str, tuple[Any, list[WindowResult]]], dict[str, Any]]:
    state = profit_opt._WORKER_STATE
    config, maker_policy = round13._variant_config_and_policy(
        state["base_config"],
        state["maker_policy"],
        round13.CANDIDATE_ID,
    )
    research = RobustnessResearch(
        state["windows"],
        state["parameters"],
        config,
        dataset_metadata=state["metadata"],
    )
    maker_fee, taker_fee, slippage_bps = cost
    runs = {
        split_name: research.evaluate_joint_policy_windows(
            state["symbol_policies"],
            maker_policy,
            window_ids,
            maker_fee_rate=maker_fee,
            taker_fee_rate=taker_fee,
            stop_slippage_bps=slippage_bps,
            fill_seed_salt=seed,
        )
        for split_name, window_ids in split_ids.items()
    }
    allowed = {
        window_id for window_ids in split_ids.values() for window_id in window_ids
    }
    integrity = round13._verify_worker_cache(
        research,
        allowed_window_ids=allowed,
        expected_wind_down_bars=round13.CANDIDATE_WIND_DOWN_BARS,
        expected_exponent=round13.CANDIDATE_EXPONENT,
    )
    return seed, runs, integrity


def _run_dataset(
    manifests: Sequence[str],
    base_config: Any,
    split_ids: Mapping[str, Sequence[str]],
    workers: int,
    *,
    end_time: datetime | None = None,
) -> tuple[
    dict[str, dict[int, dict[str, tuple[Any, list[WindowResult]]]]],
    dict[str, Any],
]:
    raw_runs = {scenario: {} for scenario in asset_audit.SCENARIOS}
    observations = []
    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(workers, len(asset_audit.DEFAULT_SEEDS)),
        initializer=_initialize_worker,
        initargs=(
            tuple(manifests),
            base_config,
            end_time.isoformat() if end_time is not None else None,
        ),
    ) as executor:
        for scenario, cost in asset_audit.SCENARIOS.items():
            print(f"EVALUATING {scenario}", flush=True)
            for seed in asset_audit.DEFAULT_SEEDS:
                future = executor.submit(_seed_worker, seed, split_ids, cost)
                futures[future] = (scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            scenario, expected_seed = futures[future]
            seed, runs, integrity = future.result()
            if seed != expected_seed:
                raise RuntimeError("长期方向效率 worker 返回了错误种子。")
            raw_runs[scenario][seed] = runs
            observations.append(integrity)
    if not observations or any(item != observations[0] for item in observations[1:]):
        raise RuntimeError("长期方向效率 worker 执行参数不一致。")
    return raw_runs, observations[0]


def _target_entry_times(
    windows: Sequence[Any],
    allowed_ids: Sequence[str],
) -> dict[str, dict[int, str]]:
    allowed = set(allowed_ids)
    result = {symbol: {} for symbol in asset_audit.SYMBOLS}
    for window in windows:
        if window.window_id not in allowed:
            continue
        if len(window.rows) < int(window.observation_rows):
            continue
        symbol = str(window.symbol).strip().upper()
        entry_row = window.rows[int(window.observation_rows) - 1]
        entry_time = int(entry_row.open_time)
        if entry_time in result[symbol]:
            raise RuntimeError(f"{symbol} 长期特征入口时间重复。")
        result[symbol][entry_time] = str(window.window_id)
    return result


def _directional_efficiency(returns: Sequence[float]) -> float:
    path = sum(abs(value) for value in returns)
    return abs(sum(returns)) / max(path, 1e-12)


def _extract_manifest_features(
    manifest_path: str,
    targets: Mapping[int, str],
    lookbacks: Sequence[int] = LOOKBACKS,
) -> tuple[dict[str, dict[int, float | None]], dict[str, Any]]:
    manifest = verify_frozen_dataset(manifest_path)
    data_path = Path(manifest_path).resolve().parent / str(manifest["file_name"])
    maximum = max(int(value) for value in lookbacks)
    returns: deque[float] = deque(maxlen=maximum)
    features = {
        window_id: {int(lookback): None for lookback in lookbacks}
        for window_id in targets.values()
    }
    previous_time: int | None = None
    previous_close: float | None = None
    last_read_time: int | None = None
    maximum_target = max(targets) if targets else None
    with data_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        open_time_index = header.index("open_time")
        close_index = header.index("close")
        for raw in reader:
            open_time = int(raw[open_time_index])
            if maximum_target is not None and open_time > maximum_target:
                break
            close = float(raw[close_index])
            if previous_time is None or open_time - previous_time != 60_000:
                returns.clear()
            else:
                if previous_close is None or previous_close <= 0 or close <= 0:
                    raise ValueError("长期方向效率遇到非正收盘价。")
                returns.append(math.log(close / previous_close))
            window_id = targets.get(open_time)
            if window_id is not None:
                values = list(returns)
                for lookback in lookbacks:
                    if len(values) >= int(lookback):
                        features[window_id][int(lookback)] = _directional_efficiency(
                            values[-int(lookback) :]
                        )
            previous_time = open_time
            previous_close = close
            last_read_time = open_time
    missing_targets = [
        window_id
        for window_id, values in features.items()
        if all(value is None for value in values.values())
    ]
    return features, {
        "symbol": str(manifest["symbol"]),
        "target_count": len(features),
        "all_lookbacks_unavailable_count": len(missing_targets),
        "last_read_open_time": last_read_time,
        "maximum_target_open_time": maximum_target,
    }


def _extract_features(
    manifests: Sequence[str],
    windows: Sequence[Any],
    allowed_ids: Sequence[str],
) -> tuple[dict[str, dict[str, dict[int, float | None]]], dict[str, Any]]:
    targets = _target_entry_times(windows, allowed_ids)
    result: dict[str, dict[str, dict[int, float | None]]] = {}
    audit = {}
    for manifest in manifests:
        item = verify_frozen_dataset(manifest)
        symbol = str(item["symbol"]).strip().upper()
        result[symbol], audit[symbol] = _extract_manifest_features(
            manifest,
            targets[symbol],
        )
    return result, audit


def _calibrate_candidates(
    current_features: Mapping[str, Mapping[str, Mapping[int, float | None]]],
    development_ids: Sequence[str],
) -> list[dict[str, Any]]:
    candidates = []
    for lookback in LOOKBACKS:
        values_by_symbol = {}
        for symbol in asset_audit.SYMBOLS:
            values = [
                current_features[symbol].get(window_id, {}).get(lookback)
                for window_id in development_ids
            ]
            available = [float(value) for value in values if value is not None]
            if len(available) < int(0.8 * len(development_ids)):
                raise RuntimeError(
                    f"{symbol} L{lookback} Development 长期特征可用率不足 80%。"
                )
            values_by_symbol[symbol] = available
        for quantile in QUANTILES:
            candidates.append(
                {
                    "candidate_id": (
                        f"LONG_DE_L{lookback}_Q{int(round(quantile * 100)):02d}"
                    ),
                    "lookback": lookback,
                    "quantile": quantile,
                    "thresholds": {
                        symbol: _empirical_quantile(values_by_symbol[symbol], quantile)
                        for symbol in asset_audit.SYMBOLS
                    },
                }
            )
    return candidates


def _apply_long_filter(
    result: WindowResult,
    *,
    feature: float | None,
    threshold: float,
    lookback: int,
) -> WindowResult:
    if result.status != "TRADED":
        return result
    if feature is not None and float(feature) <= float(threshold):
        return result
    reason = (
        f"LONG_DE_L{lookback}: HISTORY_UNAVAILABLE"
        if feature is None
        else f"LONG_DE_L{lookback}: {float(feature):.6f} > {float(threshold):.6f}"
    )
    return RobustnessResearch._blocked_entry_result(result, reason)


def _candidate_cells(
    candidate: Mapping[str, Any],
    datasets: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    cells = {}
    lookback = int(candidate["lookback"])
    thresholds = candidate["thresholds"]
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
                            _apply_long_filter(
                                result,
                                feature=dataset["features"][symbol]
                                .get(result.window_id, {})
                                .get(lookback),
                                threshold=float(thresholds[symbol]),
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


def _selection_metrics(cells: Mapping[str, Any]) -> dict[str, Any]:
    symbol_items = [
        cell["symbols"][symbol]
        for cell in cells.values()
        for symbol in asset_audit.SYMBOLS
    ]
    return {
        "cell_symbol_count": len(symbol_items),
        "passed_cell_symbol_count": sum(item["passed"] for item in symbol_items),
        "all_cells_passed": all(item["passed"] for item in symbol_items),
        "minimum_worst_seed_total_pnl": min(
            float(item["summary"]["worst_seed_total_pnl"])
            for item in symbol_items
        ),
        "minimum_trade_coverage": min(
            float(item["summary"]["minimum_trade_coverage"])
            for item in symbol_items
        ),
    }


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
            int(item["lookback"]),
            float(item["quantile"]),
        ),
    )
    return str(selected["candidate_id"])


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
    features, feature_audit = _extract_features(manifests, windows, allowed_ids)
    raw_runs, integrity = _run_dataset(
        manifests,
        base_config,
        split_ids,
        workers,
        end_time=end_time,
    )
    expected_window_count = len(set(allowed_ids))
    if int(integrity["window_count"]) != expected_window_count:
        raise RuntimeError(f"{role} worker 窗口覆盖数量不一致。")
    return {
        "base_config": base_config,
        "split_ids": {name: tuple(ids) for name, ids in split_ids.items()},
        "features": features,
        "feature_audit": feature_audit,
        "raw_runs": raw_runs,
        "execution_integrity": integrity,
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 15：长期方向效率 Phase A 结果",
        "",
        "长期 1/3/7 天方向效率替代原三小时入口过滤；CURRENT Final OOS 未评估。",
        "",
        "| 候选 | BTC 阈值 | ETH 阈值 | 通过单元 | 最差种子 PnL | 最低覆盖 | 全通过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for candidate in payload["candidates"]:
        selection = candidate["selection"]
        lines.append(
            "| `{candidate_id}` | {btc:.6f} | {eth:.6f} | {passed}/{total} | "
            "{worst:.4f} | {coverage:.2%} | {eligible} |".format(
                candidate_id=candidate["candidate_id"],
                btc=candidate["thresholds"]["BTCUSDT"],
                eth=candidate["thresholds"]["ETHUSDT"],
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
        description="以 1/3/7 天长期方向效率替代短窗入口过滤的跨周期 Phase A。"
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
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 15 协议哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    asset_path = Path(args.asset_audit_result).resolve()
    round14_path = Path(args.round14_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        asset_path: ASSET_AUDIT_SHA256,
        round14_path: ROUND14_RESULT_SHA256,
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")
    round12_payload = json.loads(round12_path.read_text(encoding="utf-8"))
    round13_payload = json.loads(round13_path.read_text(encoding="utf-8"))
    round14_payload = json.loads(round14_path.read_text(encoding="utf-8"))
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")
    if bool(round12_payload.get("final_oos_authorized")):
        raise ValueError("CURRENT Final OOS 被错误授权。")

    datasets: dict[str, dict[str, Any]] = {}

    current_manifests = tuple(str(item["manifest"]) for item in round12_payload["datasets"])
    current_config = profit_opt._base_research_config()
    current_metadata = [verify_frozen_dataset(path) for path in current_manifests]
    current_end, development_ids, validation_ids, current_isolation = (
        _current_authorized_end(current_metadata, current_config)
    )
    current_metadata, current_windows = _load_dataset(
        current_manifests,
        current_config,
        end_time=current_end,
    )
    asset_audit._validate_frozen_dataset(
        _dataset_brief(current_manifests, current_metadata),
        round12_payload["datasets"],
        label="CURRENT",
    )
    current_window_ids = {window.window_id for window in current_windows}
    if current_window_ids != set(development_ids + validation_ids):
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
    prehistory_metadata, prehistory_windows = _load_dataset(
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

    spot_manifests = tuple(
        str(item["manifest"]) for item in round14_payload["datasets"]
    )
    spot_config = profit_opt._base_research_config()
    spot_metadata, spot_windows = _load_dataset(spot_manifests, spot_config)
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

    registered = _calibrate_candidates(
        datasets["CURRENT"]["features"],
        development_ids,
    )
    evaluated = []
    for candidate in registered:
        cells = _candidate_cells(candidate, datasets)
        evaluated.append(
            {
                **candidate,
                "cells": cells,
                "selection": _selection_metrics(cells),
            }
        )
    selected_candidate_id = _select_candidate(evaluated)
    eligible_ids = [
        item["candidate_id"]
        for item in evaluated
        if item["selection"]["all_cells_passed"]
    ]
    conclusion = (
        "LONG_HORIZON_CANDIDATE_READY_FOR_AUTHORIZATION："
        f"{selected_candidate_id} 通过全部 16 个 Phase A 单元；尚未授权 Final OOS。"
        if selected_candidate_id
        else "NO_ROBUST_LONG_HORIZON_CANDIDATE：12 个长期方向效率候选均未通过全部 16 个 Phase A 单元。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "direction_mode": "NEUTRAL",
        "seeds": list(asset_audit.DEFAULT_SEEDS),
        "lookbacks": list(LOOKBACKS),
        "quantiles": list(QUANTILES),
        "current_isolation": current_isolation,
        "feature_audit": {
            role: dataset["feature_audit"] for role, dataset in datasets.items()
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
    json_output = output_dir / "round15-long-horizon-results.json"
    markdown_output = output_dir / "round15-long-horizon-report.md"
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"Round 15 结果已存在，拒绝覆盖: {output}")
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
