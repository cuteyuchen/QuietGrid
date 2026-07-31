"""Prepare v2.8 local regions and finalize chronological validation reports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategy.semiconductor_grid_v28 import (
    Combination,
    combination_neighborhood_rows,
    exposure_time_splits,
    exposed_early_rows,
    factor_snapshot,
    local_region,
    phase1_profile_summaries,
    select_local_regions,
)
from scripts.semiconductor_grid_backtest_v28 import _write_csv, _write_json
from scripts.semiconductor_grid_backtest import (
    RESEARCH_SYMBOLS,
    _find_csv,
    _read_klines_with_audit,
    build_calendar_closed_windows,
)
from strategy.semiconductor_grid import symbol_profiles_from_mapping


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="分析半导体网格 v2.8 Phase 2–4")
    parser.add_argument("command", choices=("prepare-phase3", "finalize"))
    parser.add_argument("--report-dir", default="reports/semiconductor-grid-backtest-v2.8")
    return parser


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _code_sha256() -> str:
    paths = (
        ROOT / "scripts" / "semiconductor_grid_backtest_v28.py",
        ROOT / "scripts" / "semiconductor_grid_backtest_v28_phase2.py",
        ROOT / "scripts" / "semiconductor_grid_v28_analysis.py",
        ROOT / "strategy" / "backtest.py",
        ROOT / "strategy" / "semiconductor_grid_v28.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_forward_oos_ledger(
    path: Path,
    frozen_rows: list[dict[str, Any]],
) -> None:
    """Initialize a freeze once; later finalization may not rewrite it."""
    if not path.is_file() or path.stat().st_size == 0:
        _write_csv(path, frozen_rows)
        return
    existing = _read_csv(path)
    identity_fields = (
        "record_type",
        "rank",
        "combination_id",
        "direction",
        "code_sha256",
        "config_sha256",
        "trading_rules_sha256",
        "source_data_cutoff_utc",
        "execution_scenarios",
    )

    def identity(rows: list[dict[str, Any]]) -> list[tuple[str, ...]]:
        return [
            tuple(str(row.get(field) or "") for field in identity_fields)
            for row in rows
            if str(row.get("record_type")) in {"LEDGER_METADATA", "CANDIDATE_FREEZE"}
        ]

    if identity(existing) != identity(frozen_rows):
        raise RuntimeError("Forward OOS ledger 已冻结，拒绝改写候选或哈希。")
    # Existing forward-window rows are append-only and must survive repeated
    # report generation byte-for-byte; no rewrite is necessary.


def _prepare_phase3(report_dir: Path) -> None:
    phase2 = _read_csv(report_dir / "phase2-r1-results.csv")
    if not phase2:
        raise RuntimeError("Phase 2 结果为空，不能选择局部区域。")
    early_phase2 = exposed_early_rows(phase2)
    neighborhoods = combination_neighborhood_rows(early_phase2)
    regions = select_local_regions(early_phase2)
    catalog_by_profile: dict[tuple[str, str], dict[str, Any]] = {}
    for region in regions:
        center = Combination.parse(region["center_combination_id"])
        for member in local_region(center):
            key = (member.id, region["direction"])
            if key in catalog_by_profile:
                catalog_by_profile[key]["region_id"] += f";{region['region_id']}"
            else:
                catalog_by_profile[key] = {
                    "region_id": region["region_id"],
                    "combination_id": member.id,
                    "direction": region["direction"],
                    "selection_reason": "LOCAL_COMPLETE_FACTORIAL",
                }
    catalog = [catalog_by_profile[key] for key in sorted(catalog_by_profile)]
    _write_csv(report_dir / "combination-neighborhoods.csv", neighborhoods)
    _write_csv(report_dir / "phase3-regions.csv", regions)
    _write_csv(report_dir / "phase3-candidate-catalog.csv", catalog)
    _write_json(
        report_dir / "phase3-selection.json",
        {
            "region_count": len(regions),
            "candidate_profile_count": len(catalog),
            "selection_split": "EXPOSED_EARLY",
            "selection_run_count": len(early_phase2),
            "regions": regions,
        },
    )
    print(json.dumps({"regions": len(regions), "profiles": len(catalog)}, ensure_ascii=False))


def _time_splits(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    return exposure_time_splits(rows)


def _time_validation(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    split_by_window = _time_splits(rows)
    early_neighborhoods = combination_neighborhood_rows(exposed_early_rows(rows))
    output: list[dict[str, Any]] = []
    for split in ("EXPOSED_EARLY", "EXPOSED_LATE"):
        split_rows = [
            row
            for row in rows
            if split_by_window[(str(row["symbol"]), str(row["window_key"]))] == split
        ]
        for summary in _combination_summary(split_rows, early_neighborhoods):
            output.append({"split": split, **summary})
    return output


def _effects(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries = phase1_profile_summaries(rows)
    main: list[dict[str, Any]] = []
    pairwise: list[dict[str, Any]] = []
    factors = "ABCDE"
    for scenario in sorted({row["scenario"] for row in summaries}):
        scenario_rows = [row for row in summaries if row["scenario"] == scenario]
        for factor_index, factor in enumerate(factors):
            levels = sorted({int(row["combination_id"][factor_index]) for row in scenario_rows})
            for level in levels:
                values = [
                    row["total_pnl"]
                    for row in scenario_rows
                    if int(row["combination_id"][factor_index]) == level
                ]
                main.append(
                    {
                        "scenario": scenario,
                        "factor": factor,
                        "level": level,
                        "mean_total_pnl": statistics.fmean(values),
                        "median_total_pnl": statistics.median(values),
                        "profile_count": len(values),
                    }
                )
        for left, right in ((0, 1), (0, 4), (1, 3), (2, 3), (2, 4), (0, 2)):
            groups: dict[tuple[int, int], list[float]] = {}
            for row in scenario_rows:
                key = (
                    int(row["combination_id"][left]),
                    int(row["combination_id"][right]),
                )
                groups.setdefault(key, []).append(row["total_pnl"])
            for (left_level, right_level), values in sorted(groups.items()):
                pairwise.append(
                    {
                        "scenario": scenario,
                        "interaction": f"{factors[left]}x{factors[right]}",
                        "left_level": left_level,
                        "right_level": right_level,
                        "mean_total_pnl": statistics.fmean(values),
                        "median_total_pnl": statistics.median(values),
                        "profile_count": len(values),
                    }
                )
    return main, pairwise


def _breakdown(rows: list[dict[str, Any]], factor_index: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in rows:
        key = (
            str(row["direction"]),
            str(row["scenario"]),
            int(str(row["combination_id"])[factor_index]),
        )
        groups.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for (direction, scenario, level), values in sorted(groups.items()):
        output.append(
            {
                "direction": direction,
                "scenario": scenario,
                "level": level,
                "run_count": len(values),
                "net_pnl": sum(float(row["net_pnl"]) for row in values),
                "paired_grid_pnl": sum(float(row["paired_grid_pnl"]) for row in values),
                "inventory_drag": sum(float(row["inventory_drag"]) for row in values),
            }
        )
    return output


def _group_breakdown(
    rows: list[dict[str, Any]],
    keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(str(row[key]) for key in keys), []).append(row)
    output: list[dict[str, Any]] = []
    for key, values in sorted(groups.items()):
        output.append(
            {
                **dict(zip(keys, key)),
                "run_count": len(values),
                "net_pnl": sum(float(row["net_pnl"]) for row in values),
                "paired_grid_pnl": sum(float(row["paired_grid_pnl"]) for row in values),
                "inventory_drag": sum(float(row["inventory_drag"]) for row in values),
                "max_drawdown": max(float(row["max_drawdown"]) for row in values),
            }
        )
    return output


def _combination_summary(
    rows: list[dict[str, Any]],
    neighborhoods: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    neighborhood_index = {
        (str(row["combination_id"]), str(row["direction"])): row
        for row in (neighborhoods or [])
    }
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(
            (str(row["combination_id"]), str(row["direction"]), str(row["scenario"])),
            [],
        ).append(row)
    output: list[dict[str, Any]] = []
    for (combination_id, direction, scenario), values in sorted(groups.items()):
        windows: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in values:
            windows.setdefault((str(row["symbol"]), str(row["window_key"])), []).append(row)
        window_pnls = [
            statistics.fmean(float(row["net_pnl"]) for row in seed_rows)
            for seed_rows in windows.values()
        ]
        gains = sum(max(value, 0.0) for value in window_pnls)
        losses = sum(max(-value, 0.0) for value in window_pnls)
        sorted_pnls = sorted(window_pnls)
        tail_count = max(1, int(len(sorted_pnls) * 0.05 + 0.999999))
        seed_totals: dict[str, float] = {}
        symbol_totals: dict[str, float] = {}
        month_totals: dict[str, float] = {}
        for row in values:
            seed = str(row["seed"])
            seed_totals[seed] = seed_totals.get(seed, 0.0) + float(row["net_pnl"])
            symbol = str(row["symbol"])
            symbol_totals[symbol] = symbol_totals.get(symbol, 0.0) + float(row["net_pnl"])
            month = str(row["force_close_at"])[:7]
            month_totals[month] = month_totals.get(month, 0.0) + float(row["net_pnl"])
        positive = sorted((value for value in window_pnls if value > 0), reverse=True)
        positive_total = sum(positive)
        seed_count = max(len(seed_totals), 1)
        neighbor = neighborhood_index.get((combination_id, direction), {})

        def window_total(field: str) -> float:
            return sum(
                statistics.fmean(float(row.get(field) or 0.0) for row in seed_rows)
                for seed_rows in windows.values()
            )

        output.append(
            {
                "combination_id": combination_id,
                "direction": direction,
                "scenario": scenario,
                "window_count": len(window_pnls),
                "paired_grid_pnl": window_total("paired_grid_pnl"),
                "inventory_realized_pnl": window_total("inventory_realized_pnl"),
                "net_pnl": sum(window_pnls),
                "total_pnl": sum(window_pnls),
                "median_window_pnl": statistics.median(window_pnls),
                "mean_window_pnl": statistics.fmean(window_pnls),
                "profit_factor": gains / losses if losses else (gains if gains else 0.0),
                "positive_window_ratio": sum(value > 0 for value in window_pnls) / len(window_pnls),
                "max_drawdown": max(float(row["max_drawdown"]) for row in values),
                "max_drawdown_pct": max(float(row.get("max_drawdown_pct") or 0.0) for row in values),
                "CVaR_95": statistics.fmean(sorted_pnls[:tail_count]),
                "worst_window_pnl": sorted_pnls[0],
                "worst_5pct_mean": statistics.fmean(sorted_pnls[:tail_count]),
                "max_window_loss": max(0.0, -sorted_pnls[0]),
                "inventory_drag": sum(float(row["inventory_drag"]) for row in values) / 6,
                "inventory_drag_ratio": (
                    sum(float(row["inventory_drag"]) for row in values)
                    / max(sum(float(row["paired_grid_pnl"]) for row in values), 0.01)
                ),
                "pre_exit_inventory_notional": max(
                    float(row.get("pre_exit_inventory_notional") or 0.0) for row in values
                ),
                "peak_negative_unrealized_pnl": max(float(row["peak_negative_unrealized_pnl"]) for row in values),
                "max_inventory_utilization": max(float(row["max_inventory_utilization"]) for row in values),
                "mean_inventory_utilization": statistics.fmean(float(row["mean_inventory_utilization"]) for row in values),
                "max_unpaired_lots": max(int(float(row["max_unpaired_lots"])) for row in values),
                "max_unpaired_lot_age": max(int(float(row["max_unpaired_lot_age"])) for row in values),
                "take_profit_count": window_total("take_profit_count"),
                "profit_protection_suppress_count": window_total("profit_protection_suppress_count"),
                "profit_protection_reduce_count": window_total("profit_protection_reduce_count"),
                "profit_protection_close_count": window_total("profit_protection_close_count"),
                "stop_loss_count": window_total("stop_loss_count"),
                "window_force_close_count": window_total("window_force_close_count"),
                "inventory_forced_exit_count": window_total("inventory_forced_exit_count"),
                "grid_count": statistics.fmean(float(row.get("grid_count") or 0.0) for row in values),
                "step_pct": statistics.fmean(float(row.get("step_pct") or 0.0) for row in values),
                "crossings_per_hour": statistics.fmean(
                    float(row.get("crossings_per_hour") or 0.0) for row in values
                ),
                "pair_completion_count": window_total("pair_completion_count"),
                "accepted_fill_count": window_total("accepted_fill_count"),
                "rejected_fill_count": window_total("rejected_fill_count"),
                "net_capacity_per_hour": statistics.fmean(
                    float(row.get("net_capacity_per_hour") or 0.0) for row in values
                ),
                "seed_positive_count": sum(total > 0 for total in seed_totals.values()),
                "best_window_concentration": (positive[0] / positive_total if positive_total else 0.0),
                "top_3_window_concentration": (sum(positive[:3]) / positive_total if positive_total else 0.0),
                "neighbor_positive_ratio": neighbor.get("neighbor_positive_ratio"),
                "neighbor_stress_nonnegative_ratio": neighbor.get(
                    "neighbor_stress_nonnegative_ratio"
                ),
                "neighbor_split": "EXPOSED_EARLY",
                "symbol_contribution": json.dumps(
                    {key: value / seed_count for key, value in symbol_totals.items()},
                    sort_keys=True,
                ),
                "month_contribution": json.dumps(
                    {key: value / seed_count for key, value in month_totals.items()},
                    sort_keys=True,
                ),
            }
        )
    return output


def _post_stop_paths(
    rows: list[dict[str, Any]],
    *,
    config_path: Path = Path("config/config.yaml"),
    data_dir: Path = Path("data/backtests/semiconductor-v2.7"),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    cfg = raw.get("semiconductor_grid", {}) or {}
    profiles = symbol_profiles_from_mapping(cfg.get("symbol_profiles", {}))
    observation_rows = int(cfg.get("observation_rows", 180))
    windows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for symbol in RESEARCH_SYMBOLS:
        profile = profiles[symbol]
        bars, _audit = _read_klines_with_audit(_find_csv(data_dir, symbol))
        for window in build_calendar_closed_windows(
            bars,
            market_group=profile.market_group,
            calendar_name=profile.calendar_name,
            market_timezone=profile.market_timezone,
            reference_open_time=profile.reference_open_time,
            force_close_minutes=int(cfg.get("force_close_minutes", 120)),
            minimum_trade_minutes=int(cfg.get("minimum_trade_minutes", 120)),
            observation_minutes=observation_rows,
            window_key_prefix=profile.market_group,
        ):
            if not window.complete:
                continue
            observation_end = int(window.observation_end.timestamp() * 1000)
            force_close = int(window.force_close_at.timestamp() * 1000)
            windows[(symbol, window.window_key)] = [
                bar
                for bar in window.rows
                if observation_end <= int(bar["open_time"]) < force_close
            ]
    records: list[dict[str, Any]] = []
    horizons = (30, 60, 120, 240)
    for row in rows:
        reason = str(row.get("stopped_reason") or "")
        if "stop" not in reason and reason not in {"inventory_critical", "inventory_drag_close"}:
            continue
        if row.get("stopped_at_index") in (None, "", "None"):
            continue
        trade = windows.get((str(row["symbol"]), str(row["window_key"])))
        if not trade:
            continue
        index = int(float(row["stopped_at_index"]))
        if index < 0 or index >= len(trade):
            continue
        stop_close = float(trade[index]["close"])
        lower = float(row["grid_lower"])
        upper = float(row["grid_upper"])
        atr = float(row["baseline_atr"])
        tail = trade[index + 1 :]
        lower_break = stop_close < lower or "stop_loss" in reason and "upper" not in reason
        reentry_index = next(
            (
                offset
                for offset, bar in enumerate(tail, start=1)
                if lower <= float(bar["close"]) <= upper
            ),
            None,
        )
        if lower_break:
            outward = max((lower - float(bar["low"]) for bar in tail), default=0.0)
        else:
            outward = max((float(bar["high"]) - upper for bar in tail), default=0.0)
        returns = [float(bar["close"]) / stop_close - 1.0 for bar in tail]
        record: dict[str, Any] = {
            "combination_id": row["combination_id"],
            "direction": row["direction"],
            "symbol": row["symbol"],
            "scenario": row["scenario"],
            "seed": row["seed"],
            "window_key": row["window_key"],
            "stop_reason": reason,
            "stop_index": index,
            "stop_price": stop_close,
            "return_at_window_end": returns[-1] if returns else 0.0,
            "max_favorable_excursion_after_stop": max(returns, default=0.0),
            "max_adverse_excursion_after_stop": min(returns, default=0.0),
            "time_to_reenter_original_grid": reentry_index,
        }
        for horizon in horizons:
            record[f"return_after_{horizon}m"] = (
                float(tail[horizon - 1]["close"]) / stop_close - 1.0
                if len(tail) >= horizon
                else None
            )
            record[f"reentered_within_{horizon}m"] = bool(
                reentry_index is not None and reentry_index <= horizon
            )
        if reentry_index is not None and reentry_index <= 120 and outward <= atr:
            classification = "FALSE_BREAK_LIKELY"
        elif outward > atr and (reentry_index is None or reentry_index > 240):
            classification = "TRUE_BREAK_LIKELY"
        else:
            classification = "AMBIGUOUS"
        record["classification"] = classification
        records.append(record)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault((str(record["combination_id"]), str(record["combination_id"])[4]), []).append(record)
    summary: list[dict[str, Any]] = []
    for (combination_id, stop_level), values in sorted(groups.items()):
        reentries = [
            float(row["time_to_reenter_original_grid"])
            for row in values
            if row["time_to_reenter_original_grid"] is not None
        ]
        summary.append(
            {
                "combination_id": combination_id,
                "stop_level": stop_level,
                "stop_count": len(values),
                "false_break_ratio": sum(row["classification"] == "FALSE_BREAK_LIKELY" for row in values) / len(values),
                "true_break_ratio": sum(row["classification"] == "TRUE_BREAK_LIKELY" for row in values) / len(values),
                "median_reentry_time": statistics.median(reentries) if reentries else None,
                "post_stop_MAE": statistics.fmean(float(row["max_adverse_excursion_after_stop"]) for row in values),
                "post_stop_MFE": statistics.fmean(float(row["max_favorable_excursion_after_stop"]) for row in values),
            }
        )
    return records, summary


def _factor_level_rows(
    rows: list[dict[str, Any]],
    factor_index: int,
    *,
    scenario: str | None = None,
) -> dict[int, dict[str, float]]:
    """Aggregate raw runs by one factor for concise, auditable report text."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if scenario is not None and str(row["scenario"]) != scenario:
            continue
        level = int(str(row["combination_id"])[factor_index])
        groups.setdefault(level, []).append(row)
    output: dict[int, dict[str, float]] = {}
    for level, values in groups.items():
        output[level] = {
            "run_count": float(len(values)),
            "net_pnl": sum(float(row["net_pnl"]) for row in values),
            "paired_grid_pnl": sum(float(row["paired_grid_pnl"]) for row in values),
            "inventory_drag": sum(float(row["inventory_drag"]) for row in values),
            "inventory_drag_ratio": sum(float(row["inventory_drag"]) for row in values)
            / max(sum(float(row["paired_grid_pnl"]) for row in values), 0.01),
            "stop_loss_count": sum(float(row.get("stop_loss_count") or 0.0) for row in values),
            "window_force_close_count": sum(
                float(row.get("window_force_close_count") or 0.0) for row in values
            ),
            "inventory_realized_pnl": sum(
                float(row.get("inventory_realized_pnl") or 0.0) for row in values
            ),
            "protection_actions": sum(
                float(row.get("profit_protection_suppress_count") or 0.0)
                + float(row.get("profit_protection_reduce_count") or 0.0)
                + float(row.get("profit_protection_close_count") or 0.0)
                for row in values
            ),
        }
    return output


def _top_primary_profile_by_symbol(rows: list[dict[str, Any]]) -> dict[str, str]:
    totals: dict[tuple[str, str], float] = {}
    for row in rows:
        if str(row["scenario"]) != "PRIMARY_ZERO_MAKER":
            continue
        key = (str(row["symbol"]), f"{row['combination_id']}-{row['direction']}")
        totals[key] = totals.get(key, 0.0) + float(row["net_pnl"])
    best: dict[str, tuple[str, float]] = {}
    for (symbol, profile), value in sorted(totals.items()):
        if symbol not in best or value > best[symbol][1]:
            best[symbol] = (profile, value)
    return {symbol: f"{profile} ({value:.2f})" for symbol, (profile, value) in best.items()}


def _report_insights(
    phase1: list[dict[str, Any]],
    phase3: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    pairwise: list[dict[str, Any]],
    stop_paths: list[dict[str, Any]],
    stable_late: list[dict[str, Any]],
    stress_survivors: list[dict[str, Any]],
) -> list[str]:
    primary_interactions: list[str] = []
    for interaction in ("AxB", "AxE", "BxD", "CxD"):
        cells = [
            row
            for row in pairwise
            if row["scenario"] == "PRIMARY_ZERO_MAKER" and row["interaction"] == interaction
        ]
        if cells:
            top = max(cells, key=lambda row: abs(float(row["mean_total_pnl"])))
            primary_interactions.append(
                f"{interaction}({top['left_level']},{top['right_level']})"
                f" mean_total_pnl={float(top['mean_total_pnl']):.2f}"
            )

    stop_by_a: dict[int, list[dict[str, Any]]] = {}
    for row in stop_paths:
        stop_by_a.setdefault(int(str(row["combination_id"])[0]), []).append(row)
    stop_sentence = "无止损路径样本"
    if stop_by_a:
        def ratios(values: list[dict[str, Any]]) -> tuple[float, float]:
            count = max(len(values), 1)
            return (
                sum(row["classification"] == "FALSE_BREAK_LIKELY" for row in values) / count,
                sum(row["classification"] == "TRUE_BREAK_LIKELY" for row in values) / count,
            )

        narrow = [row for level, values in stop_by_a.items() if level <= 2 for row in values]
        wide = [row for level, values in stop_by_a.items() if level >= 3 for row in values]
        if narrow and wide:
            nf, nt = ratios(narrow)
            wf, wt = ratios(wide)
            stop_sentence = (
                f"A1/A2 假突破 {nf:.3f}、真实趋势尾部 {nt:.3f}；"
                f"A3/A4 假突破 {wf:.3f}、真实趋势尾部 {wt:.3f}"
            )

    # Use the complete Phase 1 matrix for factor-level questions. Phase 3 is
    # intentionally local and therefore cannot answer questions about B3/B4
    # or all C/D levels.
    c_levels = _factor_level_rows(phase1, 2, scenario="PRIMARY_ZERO_MAKER")
    c_sentence = "无 C 维度数据"
    if c_levels:
        c_parts = []
        for level in sorted(c_levels):
            item = c_levels[level]
            c_parts.append(
                f"C{level}: actions={int(item['protection_actions'])}, "
                f"stop={int(item['stop_loss_count'])}, "
                f"window_close={int(item['window_force_close_count'])}, "
                f"inventory_pnl={item['inventory_realized_pnl']:.2f}"
            )
        c_sentence = "; ".join(c_parts)

    d_levels = _factor_level_rows(phase1, 3, scenario="PRIMARY_ZERO_MAKER")
    d_sentence = "无 D 维度数据"
    if d_levels:
        d_sentence = "; ".join(
            f"D{level}: paired={item['paired_grid_pnl']:.2f}, "
            f"inventory_drag={item['inventory_drag']:.2f}, "
            f"ratio={item['inventory_drag_ratio']:.3f}"
            for level, item in sorted(d_levels.items())
        )

    b_levels = _factor_level_rows(phase1, 1, scenario="PRIMARY_ZERO_MAKER")
    b_sentence = "无 B 维度数据"
    if b_levels:
        b_sentence = "; ".join(
            (
                f"B{level}: net={b_levels[level]['net_pnl']:.2f}, "
                f"inventory_drag={b_levels[level]['inventory_drag']:.2f}, "
                f"ratio={b_levels[level]['inventory_drag_ratio']:.3f}"
                if level in b_levels
                else f"B{level}: covering array 中无有效准入运行"
            )
            for level in (3, 4)
        )

    maker_off = {
        (row["combination_id"], row["direction"]): row
        for row in validation
        if row["split"] == "EXPOSED_LATE" and row["scenario"] == "MAKER_PROMO_OFF"
    }
    maker_nonnegative = sum(
        maker_off.get((row["combination_id"], row["direction"]), {}).get("total_pnl", -1) >= 0
        for row in stable_late
    )
    top_stress = sorted(
        stress_survivors,
        key=lambda row: float(row["total_pnl"]),
        reverse=True,
    )[:5]
    stress_text = ", ".join(
        f"{row['combination_id']}-{row['direction']} ({float(row['total_pnl']):.2f})"
        for row in top_stress
    ) or "无"
    symbol_best = _top_primary_profile_by_symbol(phase1)
    symbol_text = "; ".join(f"{symbol}: {profile}" for symbol, profile in sorted(symbol_best.items()))

    return [
        "1. 稳定盈利区域：" + (
            f"有；{len(stable_late)} 个 EXPOSED_LATE 通过 profile 同时满足邻域稳定性与时间门槛。"
            if stable_late
            else "无；没有 profile 同时通过邻域稳定性与时间门槛。"
        ),
        "2. 关键交互（PRIMARY_ZERO_MAKER 每个交互取绝对均值收益最大单元）："
        + ("、".join(primary_interactions) or "无"),
        "3. 宽区间与止损路径：" + stop_sentence + "；该比较是路径诊断，不等同于因果证明。",
        "4. 利润保护：按 C 等级的保护动作、止损、窗口末退出和库存实现损益为：" + c_sentence + "。",
        "5. 库存控制：按 D 等级的配对网格收益与库存拖累为：" + d_sentence + "。",
        "6. 密集网格零 Maker：" + b_sentence + "。",
        "7. 标的差异：Phase 1 PRIMARY 按标的最高累计净收益 profile 为：" + (symbol_text or "无") + "。",
        f"8. EXECUTION_STRESS 仍有效的 profile 数量：{len(stress_survivors)}；最高五个为：{stress_text}。",
        f"9. Maker 依赖：{maker_nonnegative}/{len(stable_late)} 个稳定 late profile 在 MAKER_PROMO_OFF 下非负；"
        + ("因此当前稳定区域不依赖 Maker 免费。" if maker_nonnegative == len(stable_late) else "因此部分结果依赖 Maker 免费。"),
        "10. Forward OOS：INSUFFICIENT_FORWARD_OOS，当前 0/8 个完整窗口，不能做正式候选判断。",
    ]


def _finalize(report_dir: Path) -> None:
    phase1 = _read_csv(report_dir / "phase1-r0-results.csv")
    phase3 = _read_csv(report_dir / "phase3-local-factorial-results.csv")
    regions = _read_csv(report_dir / "phase3-regions.csv")
    validation = _time_validation(phase3) if phase3 else []
    main, pairwise = _effects(phase1)
    _write_csv(report_dir / "phase4-time-validation-results.csv", validation)
    _write_csv(report_dir / "main-effects.csv", main)
    _write_csv(report_dir / "pairwise-interactions.csv", pairwise)
    _write_csv(report_dir / "interaction-heatmaps.csv", pairwise)
    source = phase3 or phase1
    _write_csv(report_dir / "profit-protection-breakdown.csv", _breakdown(source, 2))
    _write_csv(report_dir / "inventory-control-breakdown.csv", _breakdown(source, 3))
    _write_csv(report_dir / "stop-mechanism-breakdown.csv", _breakdown(source, 4))
    phase1_neighborhoods = combination_neighborhood_rows(exposed_early_rows(phase1))
    _write_csv(
        report_dir / "combination-summary.csv",
        _combination_summary(phase1, phase1_neighborhoods),
    )
    _write_csv(report_dir / "symbol-breakdown.csv", _group_breakdown(source, ("symbol", "direction", "scenario")))
    _write_csv(report_dir / "scenario-breakdown.csv", _group_breakdown(source, ("scenario",)))
    _write_csv(report_dir / "seed-breakdown.csv", _group_breakdown(source, ("seed", "scenario")))
    _write_csv(report_dir / "window-breakdown.csv", _group_breakdown(source, ("symbol", "window_key", "scenario")))
    stop_paths, stop_summary = _post_stop_paths(phase1)
    _write_csv(report_dir / "post-stop-path-analysis.csv", stop_paths)
    _write_csv(report_dir / "post-stop-summary.csv", stop_summary)
    early_phase3 = exposed_early_rows(phase3) if phase3 else []
    local_neighborhoods = combination_neighborhood_rows(early_phase3) if early_phase3 else []
    if local_neighborhoods:
        _write_csv(report_dir / "combination-neighborhoods.csv", local_neighborhoods)
    late = [row for row in validation if row["split"] == "EXPOSED_LATE"]
    primary = {
        (row["combination_id"], row["direction"]): row
        for row in late
        if row["scenario"] == "PRIMARY_ZERO_MAKER"
    }
    stress = {
        (row["combination_id"], row["direction"]): row
        for row in late
        if row["scenario"] == "EXECUTION_STRESS"
    }
    passing = [
        {"combination_id": combination_id, "direction": direction, **row}
        for (combination_id, direction), row in primary.items()
        if row["total_pnl"] > 0
        and row["median_window_pnl"] >= 0
        and row["profit_factor"] >= 1.05
        and row["inventory_drag_ratio"] <= 0.75
        and stress.get((combination_id, direction), {}).get("total_pnl", -1) >= 0
    ]
    stable_profiles = {
        (row["combination_id"], row["direction"])
        for row in local_neighborhoods
        if row["stable_region_candidate"]
    }
    early_summaries = _combination_summary(early_phase3, local_neighborhoods)
    early_primary = {
        (row["combination_id"], row["direction"]): row
        for row in early_summaries
        if row["scenario"] == "PRIMARY_ZERO_MAKER"
    }
    early_stress = {
        (row["combination_id"], row["direction"]): row
        for row in early_summaries
        if row["scenario"] == "EXECUTION_STRESS"
    }

    def positive_symbol_count(row: dict[str, Any]) -> int:
        contributions = json.loads(str(row.get("symbol_contribution") or "{}"))
        return sum(float(value) > 0 for value in contributions.values())

    stable_late = []
    for row in passing:
        key = (row["combination_id"], row["direction"])
        early = early_primary.get(key)
        early_stress_row = early_stress.get(key)
        if not early or key not in stable_profiles:
            continue
        if not (
            early["window_count"] >= 4
            and early["total_pnl"] > 0
            and early["median_window_pnl"] >= 0
            and early_stress_row
            and early_stress_row["total_pnl"] >= 0
            and early["profit_factor"] >= 1.05
            and early["positive_window_ratio"] >= 0.50
            and early["inventory_drag_ratio"] <= 0.75
            and early["best_window_concentration"] <= 0.50
            and early["seed_positive_count"] >= 4
            and positive_symbol_count(early) >= 2
        ):
            continue
        stable_late.append(row)

    neighborhood_index = {
        (row["combination_id"], row["direction"]): row
        for row in local_neighborhoods
    }
    stable_late.sort(
        key=lambda row: (
            float(row["median_window_pnl"]),
            float(stress[(row["combination_id"], row["direction"])]["total_pnl"]),
            float(row["worst_5pct_mean"]),
            -float(row["inventory_drag_ratio"]),
            float(
                neighborhood_index[(row["combination_id"], row["direction"])][
                    "neighbor_positive_ratio"
                ]
            ),
            -float(row["best_window_concentration"]),
        ),
        reverse=True,
    )
    frozen_candidates = (
        [row for row in stable_late if row["direction"] == "NEUTRAL"][:2]
        + [row for row in stable_late if row["direction"] == "LONG"][:1]
    )
    phase3_primary = [
        row
        for row in phase1_profile_summaries(phase3)
        if row["scenario"] == "PRIMARY_ZERO_MAKER"
    ] if phase3 else []
    phase3_stress = {
        (row["combination_id"], row["direction"]): row
        for row in phase1_profile_summaries(phase3)
        if row["scenario"] == "EXECUTION_STRESS"
    } if phase3 else {}
    positive_primary = [row for row in phase3_primary if row["total_pnl"] > 0]
    stress_survivors = [
        row
        for row in positive_primary
        if phase3_stress.get((row["combination_id"], row["direction"]), {}).get("total_pnl", -1) >= 0
    ]
    if stable_late:
        maker_off = {
            (row["combination_id"], row["direction"]): row
            for row in validation
            if row["split"] == "EXPOSED_LATE" and row["scenario"] == "MAKER_PROMO_OFF"
        }
        conclusion = (
            "PASS_STABLE_PARAMETER_REGION_RESEARCH_ONLY"
            if all(
                maker_off.get((row["combination_id"], row["direction"]), {}).get("total_pnl", -1) >= 0
                for row in stable_late
            )
            else "PASS_MAKER_DEPENDENT_REGION_RESEARCH_ONLY"
        )
    elif positive_primary and not stable_profiles:
        conclusion = "REJECT_ISOLATED_OPTIMA_ONLY"
    elif positive_primary and not stress_survivors:
        conclusion = "REJECT_EXECUTION_STRESS_ACROSS_REGIONS"
    elif positive_primary and all(row["inventory_drag_ratio"] > 0.75 for row in positive_primary):
        conclusion = "REJECT_INVENTORY_TAIL_ACROSS_REGIONS"
    else:
        conclusion = "REJECT_NO_STABLE_PARAMETER_REGION"
    _write_json(
        report_dir / "phase4-acceptance.json",
        {
            "passing_profile_count": len(passing),
            "stable_hard_filter_pass_count": len(stable_late),
            "frozen_candidate_count": len(frozen_candidates),
            "profiles": passing,
            "frozen_candidates": frozen_candidates,
        },
    )
    code_sha = _code_sha256()
    config_sha = _sha256(ROOT / "config" / "config.yaml")
    rules_path = report_dir / "exchange-rules.json"
    rules_sha = _sha256(rules_path)
    data_cutoff = max((str(row["force_close_at"]) for row in phase3), default="")
    scenarios = ";".join(sorted({str(row["scenario"]) for row in phase3}))
    ledger_rows: list[dict[str, Any]] = [
        {
            "record_type": "LEDGER_METADATA",
            "status": "INSUFFICIENT_FORWARD_OOS",
            "complete_window_count": 0,
            "required_window_count": 8,
            "immutable_append_only": True,
            "code_sha256": code_sha,
            "config_sha256": config_sha,
            "trading_rules_sha256": rules_sha,
            "source_data_cutoff_utc": data_cutoff,
            "execution_scenarios": scenarios,
        }
    ]
    for rank, row in enumerate(frozen_candidates, start=1):
        key = (row["combination_id"], row["direction"])
        ledger_rows.append(
            {
                "record_type": "CANDIDATE_FREEZE",
                "status": "INSUFFICIENT_FORWARD_OOS",
                "rank": rank,
                "combination_id": row["combination_id"],
                "direction": row["direction"],
                "complete_window_count": 0,
                "required_window_count": 8,
                "immutable_append_only": True,
                "code_sha256": code_sha,
                "config_sha256": config_sha,
                "trading_rules_sha256": rules_sha,
                "source_data_cutoff_utc": data_cutoff,
                "execution_scenarios": scenarios,
                "late_median_window_pnl": row["median_window_pnl"],
                "late_execution_stress_pnl": stress[key]["total_pnl"],
                "late_worst_5pct_mean": row["worst_5pct_mean"],
                "late_inventory_drag_ratio": row["inventory_drag_ratio"],
                "early_neighbor_positive_ratio": neighborhood_index[key][
                    "neighbor_positive_ratio"
                ],
                "late_best_window_concentration": row["best_window_concentration"],
            }
        )
    _write_forward_oos_ledger(report_dir / "forward-oos-ledger.csv", ledger_rows)
    acceptance = {
        "conclusion_code": conclusion,
        "phase3_region_count": len(regions),
        "phase3_profile_count": len({(row["combination_id"], row["direction"]) for row in phase3}),
        "stable_profile_count": len(stable_profiles),
        "exposed_late_pass_count": len(passing),
        "stable_exposed_late_pass_count": len(stable_late),
        "frozen_candidate_count": len(frozen_candidates),
        "selection_split": "EXPOSED_EARLY",
        "forward_oos_status": "INSUFFICIENT_FORWARD_OOS",
    }
    _write_json(report_dir / "acceptance-gates.json", acceptance)
    false_break_ratio = (
        sum(row["classification"] == "FALSE_BREAK_LIKELY" for row in stop_paths)
        / len(stop_paths)
        if stop_paths
        else 0.0
    )
    true_break_ratio = (
        sum(row["classification"] == "TRUE_BREAK_LIKELY" for row in stop_paths)
        / len(stop_paths)
        if stop_paths
        else 0.0
    )
    required_answers = _report_insights(
        phase1,
        phase3,
        validation,
        pairwise,
        stop_paths,
        stable_late,
        stress_survivors,
    )
    report_lines = [
        "# Semiconductor Grid v2.8 Final Report",
        "",
        "## Conclusion",
        "",
        f"`{conclusion}`",
        "",
        f"- Phase 1 runs: {len(phase1)}",
        f"- Phase 2 R1 runs: {len(_read_csv(report_dir / 'phase2-r1-results.csv'))}",
        f"- Phase 3 local profiles: {acceptance['phase3_profile_count']}",
        f"- Stable neighbor profiles: {len(stable_profiles)}",
        f"- EXPOSED_LATE passing profiles: {len(passing)}",
        "- Forward OOS: INSUFFICIENT_FORWARD_OOS (0/8 complete windows)",
        "",
        "## Required Questions",
        "",
        *required_answers,
        "",
        "## Aggregate Stop Classification",
        "",
        f"- false-break ratio: {false_break_ratio:.3f}",
        f"- true-break ratio: {true_break_ratio:.3f}",
    ]
    (report_dir / "final-report.md").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "phase4_profiles": len(validation),
                "late_pass": len(passing),
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    args = _parser().parse_args()
    report_dir = Path(args.report_dir)
    if args.command == "prepare-phase3":
        _prepare_phase3(report_dir)
    else:
        _finalize(report_dir)


if __name__ == "__main__":
    main()
