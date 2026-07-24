"""Evaluate H1 before any grid/backtest parameter work."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stock_perp_common import (  # noqa: E402
    SEED_VALUES,
    git_branch,
    git_commit,
    immutable_write,
    write_csv,
    write_json,
)


UTC = timezone.utc
REPORT_DIR = Path("reports/stock-perp-weekend-grid-v1")
STEP_PCT = 0.0015
BASE_MAKER_FEE = 0.0002
BOOTSTRAP_REPS = 2000
NOT_RUN_STATUS = "NOT_RUN_H1_FAILED"

DOWNSTREAM_CSV_FIELDS: dict[str, tuple[str, ...]] = {
    "baseline-comparison.csv": ("baseline", "scenario", "status", "reason"),
    "symbol-breakdown.csv": ("symbol", "status", "reason"),
    "month-breakdown.csv": ("month", "status", "reason"),
    "window-breakdown.csv": ("window_id", "status", "reason"),
    "execution-stress.csv": ("scenario", "status", "reason"),
    "portfolio-summary.csv": ("split", "scenario", "status", "reason"),
    "short-oos-summary.csv": ("opened", "status", "reason"),
    "sensitivity-matrix.csv": ("parameter", "status", "reason"),
}

FEATURE_FIELDS = (
    "window_id",
    "symbol",
    "group",
    "seed",
    "matched_window_id",
    "calendar_key",
    "month",
    "split",
    "listing_stage",
    "status",
    "row_count",
    "tradable_rows",
    "realized_volatility",
    "atr_pct",
    "high_low_range_pct",
    "directional_efficiency",
    "max_single_direction_move_pct",
    "return_sign_flip_rate",
    "reversal_legs",
    "completed_grid_cycles",
    "fee_adjusted_cycle_capacity",
    "zero_trade_ratio",
    "hourly_volume",
    "hourly_trade_count",
    "funding_abs_sum",
    "funding_abs_mean",
    "premium_abs_mean",
    "mark_deviation_abs_mean",
    "volatility_expansion",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行股票永续周末效应 H1 市场假设检验")
    parser.add_argument("--discovery", default=str(REPORT_DIR / "symbol-discovery.json"))
    parser.add_argument("--data-manifest", default=str(REPORT_DIR / "asset-data-manifest.json"))
    parser.add_argument("--window-manifest", default=str(REPORT_DIR / "window-manifest.json"))
    parser.add_argument("--audit-json", default=str(REPORT_DIR / "asset-data-audit.json"))
    parser.add_argument("--data-dir", default="data/backtests/stock-perp-weekend-grid-v1")
    parser.add_argument("--output-dir", default=str(REPORT_DIR))
    parser.add_argument("--bootstrap-reps", type=int, default=BOOTSTRAP_REPS)
    return parser


def _read_rows(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_klines(path: str | Path) -> list[dict[str, Any]]:
    rows = _read_rows(path)
    for row in rows:
        for key in ("open_time", "close_time", "trade_count"):
            row[key] = int(row[key])
        for key in ("open", "high", "low", "close", "volume", "quote_volume"):
            row[key] = float(row[key])
    return rows


def _read_side_map(path: str | Path, *, signed: bool = False) -> dict[int, float]:
    result: dict[int, float] = {}
    for row in _read_rows(path):
        try:
            timestamp = int(row["open_time"])
            value = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and (signed or value > 0):
            result[timestamp] = value
    return result


def _read_funding(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    events = []
    for row in payload.get("events") or []:
        try:
            events.append(
                {
                    "funding_time": int(row["funding_time"]),
                    "funding_rate": float(row["funding_rate"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return events


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _returns(closes: Sequence[float]) -> list[float]:
    values: list[float] = []
    for left, right in zip(closes, closes[1:]):
        if left > 0 and right > 0:
            values.append(math.log(right / left))
    return values


def _sign_flip_rate(values: Sequence[float]) -> float:
    signs = [1 if value > 0 else -1 if value < 0 else 0 for value in values]
    signs = [value for value in signs if value]
    if len(signs) < 2:
        return 0.0
    flips = sum(left != right for left, right in zip(signs, signs[1:]))
    return flips / (len(signs) - 1)


def _max_directional_move(closes: Sequence[float]) -> float:
    if not closes:
        return 0.0
    best = 0.0
    pivot = closes[0]
    direction = 0
    for price in closes[1:]:
        if pivot <= 0:
            pivot = price
            continue
        move = (price - pivot) / pivot
        if direction == 0:
            direction = 1 if move >= 0 else -1
        if direction > 0 and move < 0:
            pivot = price
            direction = -1
        elif direction < 0 and move > 0:
            pivot = price
            direction = 1
        else:
            best = max(best, abs(move))
    return best


def _grid_cycles(closes: Sequence[float], step_pct: float) -> tuple[int, int]:
    """Count threshold reversals without selecting a future direction."""
    if len(closes) < 2:
        return 0, 0
    pivot = closes[0]
    direction = 0
    legs = 0
    cycles = 0
    for price in closes[1:]:
        if pivot <= 0 or price <= 0:
            continue
        move = (price - pivot) / pivot
        if direction == 0:
            if abs(move) >= step_pct:
                direction = 1 if move > 0 else -1
                pivot = price
                legs += 1
            continue
        if direction > 0:
            if price >= pivot:
                pivot = price
            elif (pivot - price) / pivot >= step_pct:
                legs += 1
                cycles += 1
                direction = -1
                pivot = price
        else:
            if price <= pivot:
                pivot = price
            elif (price - pivot) / pivot >= step_pct:
                legs += 1
                cycles += 1
                direction = 1
                pivot = price
    return legs, cycles


def _feature_row(
    window: Mapping[str, Any],
    klines: Sequence[Mapping[str, Any]],
    funding: Sequence[Mapping[str, Any]],
    mark: Mapping[int, float],
    premium: Mapping[int, float],
) -> dict[str, Any]:
    start = int(window["row_start_index"]) + int(window["observation_rows"])
    end = int(window["row_end_index"])
    rows = list(klines[start:end])
    closes = [float(row["close"]) for row in rows]
    returns = _returns(closes)
    if not rows:
        return {field: window.get(field, "") for field in FEATURE_FIELDS} | {
            "status": "SKIPPED",
            "row_count": 0,
            "tradable_rows": 0,
        }
    tr_values: list[float] = []
    for index, row in enumerate(rows):
        previous_close = closes[index - 1] if index else float(row["open"])
        tr_values.append(
            max(
                float(row["high"]) - float(row["low"]),
                abs(float(row["high"]) - previous_close),
                abs(float(row["low"]) - previous_close),
            )
        )
    atr_pct = statistics.fmean(
        value / close for value, close in zip(tr_values, closes) if close > 0
    ) if closes else 0.0
    range_pct = (
        (max(float(row["high"]) for row in rows) - min(float(row["low"]) for row in rows))
        / closes[0]
        if closes and closes[0] > 0
        else 0.0
    )
    directional_denominator = sum(abs(right - left) for left, right in zip(closes, closes[1:]))
    directional_efficiency = (
        abs(closes[-1] - closes[0]) / directional_denominator
        if directional_denominator > 0
        else 0.0
    )
    legs, cycles = _grid_cycles(closes, STEP_PCT)
    start_time = int(rows[0]["open_time"])
    end_time = int(rows[-1]["close_time"])
    funding_values = [
        abs(float(event["funding_rate"]))
        for event in funding
        if start_time <= int(event["funding_time"]) <= end_time
    ]
    premium_values = [
        abs(float(premium[timestamp]))
        for timestamp in range(start_time, end_time + 1, 60_000)
        if timestamp in premium
    ]
    mark_values = [
        abs((float(mark[timestamp]) - float(row["close"])) / float(row["close"]))
        for row in rows
        for timestamp in [int(row["open_time"])]
        if timestamp in mark and float(row["close"]) > 0
    ]
    half = max(1, len(returns) // 2)
    first_vol = math.sqrt(sum(value * value for value in returns[:half]))
    second_vol = math.sqrt(sum(value * value for value in returns[half:]))
    expansion = 1.0 if second_vol >= first_vol * 1.5 and second_vol > 0 else 0.0
    total_trades = sum(int(row["trade_count"]) for row in rows)
    total_volume = sum(float(row["volume"]) for row in rows)
    hours = max(1 / 60, len(rows) / 60)
    feature: dict[str, Any] = {
        **{field: window.get(field, "") for field in FEATURE_FIELDS},
        "status": "READY",
        "row_count": len(rows),
        "tradable_rows": len(rows),
        "realized_volatility": math.sqrt(sum(value * value for value in returns)),
        "atr_pct": atr_pct,
        "high_low_range_pct": range_pct,
        "directional_efficiency": directional_efficiency,
        "max_single_direction_move_pct": _max_directional_move(closes),
        "return_sign_flip_rate": _sign_flip_rate(returns),
        "reversal_legs": legs,
        "completed_grid_cycles": cycles,
        "fee_adjusted_cycle_capacity": cycles * max(STEP_PCT - 2 * BASE_MAKER_FEE, 0.0),
        "zero_trade_ratio": sum(int(row["trade_count"]) == 0 or float(row["volume"]) == 0 for row in rows) / len(rows),
        "hourly_volume": total_volume / hours,
        "hourly_trade_count": total_trades / hours,
        "funding_abs_sum": sum(funding_values),
        "funding_abs_mean": statistics.fmean(funding_values) if funding_values else 0.0,
        "premium_abs_mean": statistics.fmean(premium_values) if premium_values else 0.0,
        "mark_deviation_abs_mean": statistics.fmean(mark_values) if mark_values else 0.0,
        "volatility_expansion": expansion,
    }
    return feature


def _numeric(values: Iterable[Mapping[str, Any]], field: str) -> list[float]:
    result: list[float] = []
    for row in values:
        value = _float_or_none(row.get(field))
        if value is not None:
            result.append(value)
    return result


def _median(values: Iterable[Mapping[str, Any]], field: str) -> float | None:
    numbers = _numeric(values, field)
    return statistics.median(numbers) if numbers else None


def _group_calendar(features: Sequence[Mapping[str, Any]], group: str) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in features:
        if row.get("group") != group or row.get("status") != "READY":
            continue
        key = str(row.get("calendar_key") or row.get("window_id"))
        for field in ("realized_volatility", "directional_efficiency", "fee_adjusted_cycle_capacity", "zero_trade_ratio", "hourly_volume", "hourly_trade_count"):
            value = _float_or_none(row.get(field))
            if value is not None:
                grouped[key][field].append(value)
    return {
        key: {field: statistics.median(values) for field, values in fields.items() if values}
        for key, fields in grouped.items()
    }


def _calendar_values(
    features: Sequence[Mapping[str, Any]],
    group: str,
    field: str,
) -> list[float]:
    return [
        item[field]
        for item in _group_calendar(features, group).values()
        if field in item
    ]


def _bootstrap_w_vs_control(
    w_by_calendar: Mapping[str, Mapping[str, float]],
    control_values: Sequence[float],
    *,
    field: str,
    reps: int,
    lower_is_better: bool,
) -> dict[str, Any]:
    w_values = [float(item[field]) for item in w_by_calendar.values() if field in item]
    controls = list(control_values)
    if not w_values or not controls:
        return {"support_probability": None, "p_value": None, "sample_w": len(w_values), "sample_control": len(controls)}
    observed = statistics.median(w_values) - statistics.median(controls)
    rng = random.Random(31)
    favorable = 0
    for _ in range(max(1, reps)):
        sample_w = [w_values[rng.randrange(len(w_values))] for _ in w_values]
        sample_c = [controls[rng.randrange(len(controls))] for _ in controls]
        delta = statistics.median(sample_w) - statistics.median(sample_c)
        favorable += int(delta <= 0 if lower_is_better else delta >= 0)
    support_probability = favorable / max(1, reps)
    return {
        "observed_delta": observed,
        "support_probability": support_probability,
        "p_value": 1.0 - support_probability,
        "sample_w": len(w_values),
        "sample_control": len(controls),
        "favorable_direction": "W<=CONTROL" if lower_is_better else "W>=CONTROL",
    }


def _h1_checks(features: Sequence[Mapping[str, Any]], *, bootstrap_reps: int) -> dict[str, Any]:
    w = [row for row in features if row.get("group") == "W" and row.get("status") == "READY" and row.get("split") == "RESEARCH_DEVELOPMENT"]
    o = [row for row in features if row.get("group") == "O" and row.get("status") == "READY" and row.get("split") == "RESEARCH_DEVELOPMENT"]
    r = [row for row in features if row.get("group") == "R" and row.get("status") == "READY" and row.get("split") == "RESEARCH_DEVELOPMENT"]
    w_calendar = _group_calendar(w, "W")
    o_calendar = _group_calendar(o, "O")
    r_calendar = _group_calendar(r, "R")
    w_vol = statistics.median(_calendar_values(w, "W", "realized_volatility")) if w_calendar else None
    o_vol = statistics.median(_calendar_values(o, "O", "realized_volatility")) if o_calendar else None
    r_vol = statistics.median(_calendar_values(r, "R", "realized_volatility")) if r_calendar else None
    w_eff = statistics.median(_calendar_values(w, "W", "directional_efficiency")) if w_calendar else None
    o_eff = statistics.median(_calendar_values(o, "O", "directional_efficiency")) if o_calendar else None
    r_eff = statistics.median(_calendar_values(r, "R", "directional_efficiency")) if r_calendar else None
    w_cap = statistics.median(_calendar_values(w, "W", "fee_adjusted_cycle_capacity")) if w_calendar else None
    o_cap = statistics.median(_calendar_values(o, "O", "fee_adjusted_cycle_capacity")) if o_calendar else None
    r_cap = statistics.median(_calendar_values(r, "R", "fee_adjusted_cycle_capacity")) if r_calendar else None
    checks: dict[str, Any] = {
        "tier_a_core_count": len({row["symbol"] for row in features if row.get("group") == "W"}),
        "development_window_counts": {"W": len(w), "O": len(o), "R": len(r)},
        "development_calendar_window_counts": {
            "W": len(w_calendar),
            "O": len(o_calendar),
            "R": len(r_calendar),
        },
        "medians": {
            "realized_volatility": {"W": w_vol, "O": o_vol, "R": r_vol},
            "directional_efficiency": {"W": w_eff, "O": o_eff, "R": r_eff},
            "fee_adjusted_cycle_capacity": {"W": w_cap, "O": o_cap, "R": r_cap},
        },
    }
    checks["volatility_not_higher_than_90pct"] = bool(
        w_vol is not None and o_vol is not None and r_vol is not None and w_vol <= o_vol * 0.9 and w_vol <= r_vol * 0.9
    )
    checks["directional_efficiency_not_higher"] = bool(
        w_eff is not None and o_eff is not None and r_eff is not None and w_eff <= o_eff and w_eff <= r_eff
    )
    checks["cycle_capacity_not_lower"] = bool(
        w_cap is not None and o_cap is not None and r_cap is not None and w_cap >= o_cap and w_cap >= r_cap
    )
    checks["cycle_capacity_strictly_positive"] = bool(w_cap is not None and w_cap > 0)

    months = sorted({row["month"] for row in w if row.get("month")})
    month_advantage: dict[str, bool] = {}
    for month in months:
        wm = [row for row in w if row.get("month") == month]
        om = [row for row in o if row.get("month") == month]
        rm = [row for row in r if row.get("month") == month]
        wc_values = _calendar_values(wm, "W", "fee_adjusted_cycle_capacity")
        oc_values = _calendar_values(om, "O", "fee_adjusted_cycle_capacity")
        rc_values = _calendar_values(rm, "R", "fee_adjusted_cycle_capacity")
        wc = statistics.median(wc_values) if wc_values else None
        oc = statistics.median(oc_values) if oc_values else None
        rc = statistics.median(rc_values) if rc_values else None
        month_advantage[month] = bool(wc is not None and oc is not None and rc is not None and wc >= oc and wc >= rc)
    checks["month_advantage"] = month_advantage
    checks["months_same_direction_ratio"] = (
        sum(month_advantage.values()) / len(month_advantage) if month_advantage else 0.0
    )
    checks["sixty_percent_month_advantage"] = checks["months_same_direction_ratio"] >= 0.6
    symbol_advantage: dict[str, bool] = {}
    for symbol in sorted({row["symbol"] for row in w}):
        ws = [row for row in w if row["symbol"] == symbol]
        os = [row for row in o if row["symbol"] == symbol]
        rs = [row for row in r if row["symbol"] == symbol]
        wc = _median(ws, "fee_adjusted_cycle_capacity")
        oc = _median(os, "fee_adjusted_cycle_capacity")
        rc = _median(rs, "fee_adjusted_cycle_capacity")
        symbol_advantage[symbol] = bool(wc is not None and oc is not None and rc is not None and wc >= oc and wc >= rc)
    checks["symbol_advantage"] = symbol_advantage
    checks["advantage_not_one_symbol"] = sum(symbol_advantage.values()) >= 2
    control_capacity = max(value for value in (o_cap, r_cap) if value is not None) if o_cap is not None and r_cap is not None else None
    capacities = [
        max(0.0, value - control_capacity)
        for value in _calendar_values(w, "W", "fee_adjusted_cycle_capacity")
    ] if control_capacity is not None else []
    total_capacity = sum(capacities)
    checks["positive_cycle_capacity_advantage"] = total_capacity
    checks["best_window_concentration"] = max(capacities) / total_capacity if total_capacity > 0 else 1.0
    checks["best_window_contribution_le_35pct"] = checks["best_window_concentration"] <= 0.35

    bootstrap = {
        "volatility": _bootstrap_w_vs_control(w_calendar, [item["realized_volatility"] for item in o_calendar.values() if "realized_volatility" in item] + [item["realized_volatility"] for item in r_calendar.values() if "realized_volatility" in item], field="realized_volatility", reps=bootstrap_reps, lower_is_better=True),
        "cycle_capacity": _bootstrap_w_vs_control(w_calendar, [item["fee_adjusted_cycle_capacity"] for item in o_calendar.values() if "fee_adjusted_cycle_capacity" in item] + [item["fee_adjusted_cycle_capacity"] for item in r_calendar.values() if "fee_adjusted_cycle_capacity" in item], field="fee_adjusted_cycle_capacity", reps=bootstrap_reps, lower_is_better=False),
    }
    checks["block_bootstrap"] = bootstrap
    checks["bootstrap_supports_non_noise"] = bool(
        bootstrap["cycle_capacity"].get("support_probability") is not None
        and bootstrap["cycle_capacity"]["support_probability"] >= 0.95
    )
    mature_w = [row for row in w if row.get("listing_stage") == "LISTING_AFTER_30_DAYS"]
    mature_o = [row for row in o if row.get("listing_stage") == "LISTING_AFTER_30_DAYS"]
    mature_r = [row for row in r if row.get("listing_stage") == "LISTING_AFTER_30_DAYS"]
    mature_w_values = _calendar_values(mature_w, "W", "fee_adjusted_cycle_capacity")
    mature_o_values = _calendar_values(mature_o, "O", "fee_adjusted_cycle_capacity")
    mature_r_values = _calendar_values(mature_r, "R", "fee_adjusted_cycle_capacity")
    mature_w_cap = statistics.median(mature_w_values) if mature_w_values else None
    mature_o_cap = statistics.median(mature_o_values) if mature_o_values else None
    mature_r_cap = statistics.median(mature_r_values) if mature_r_values else None
    checks["mature_stage_direction"] = {
        "W": mature_w_cap,
        "O": mature_o_cap,
        "R": mature_r_cap,
        "same_direction": bool(
            mature_w_cap is not None and mature_o_cap is not None and mature_r_cap is not None and mature_w_cap >= mature_o_cap and mature_w_cap >= mature_r_cap
        ),
    }
    w_zero_values = _calendar_values(w, "W", "zero_trade_ratio")
    o_zero_values = _calendar_values(o, "O", "zero_trade_ratio")
    r_zero_values = _calendar_values(r, "R", "zero_trade_ratio")
    w_zero = statistics.median(w_zero_values) if w_zero_values else None
    o_zero = statistics.median(o_zero_values) if o_zero_values else None
    r_zero = statistics.median(r_zero_values) if r_zero_values else None
    checks["execution_quality"] = {
        "zero_trade_ratio": {"W": w_zero, "O": o_zero, "R": r_zero},
        "not_significantly_worse": bool(
            w_zero is not None and o_zero is not None and r_zero is not None and w_zero <= max(o_zero, r_zero) * 1.25 + 0.01
        ),
    }
    checks["sample_sufficiency"] = {
        "three_core_symbols": checks["tier_a_core_count"] >= 3,
        "w_controls_present": bool(w_calendar),
        "o_controls_present": bool(o_calendar),
        "r_controls_present": bool(r_calendar),
    }
    required = [
        checks["sample_sufficiency"]["three_core_symbols"],
        checks["volatility_not_higher_than_90pct"],
        checks["directional_efficiency_not_higher"],
        checks["cycle_capacity_not_lower"],
        checks["cycle_capacity_strictly_positive"],
        checks["sixty_percent_month_advantage"],
        checks["advantage_not_one_symbol"],
        checks["best_window_contribution_le_35pct"],
        checks["bootstrap_supports_non_noise"],
        checks["mature_stage_direction"]["same_direction"],
        checks["execution_quality"]["not_significantly_worse"],
        checks["sample_sufficiency"]["w_controls_present"],
        checks["sample_sufficiency"]["o_controls_present"],
        checks["sample_sufficiency"]["r_controls_present"],
    ]
    checks["passed"] = all(required)
    checks["failed_checks"] = [
        name
        for name, value in (
            ("three_core_symbols", checks["sample_sufficiency"]["three_core_symbols"]),
            ("volatility_not_higher_than_90pct", checks["volatility_not_higher_than_90pct"]),
            ("directional_efficiency_not_higher", checks["directional_efficiency_not_higher"]),
            ("cycle_capacity_not_lower", checks["cycle_capacity_not_lower"]),
            ("cycle_capacity_strictly_positive", checks["cycle_capacity_strictly_positive"]),
            ("sixty_percent_month_advantage", checks["sixty_percent_month_advantage"]),
            ("advantage_not_one_symbol", checks["advantage_not_one_symbol"]),
            ("best_window_contribution_le_35pct", checks["best_window_contribution_le_35pct"]),
            ("bootstrap_supports_non_noise", checks["bootstrap_supports_non_noise"]),
            ("mature_stage_direction", checks["mature_stage_direction"]["same_direction"]),
            ("execution_quality", checks["execution_quality"]["not_significantly_worse"]),
            ("w_controls_present", checks["sample_sufficiency"]["w_controls_present"]),
            ("o_controls_present", checks["sample_sufficiency"]["o_controls_present"]),
            ("r_controls_present", checks["sample_sufficiency"]["r_controls_present"]),
        )
        if not value
    ]
    return checks


def _validate_frozen_inputs(
    discovery: Mapping[str, Any],
    data_manifest: Mapping[str, Any],
    windows_payload: Mapping[str, Any],
    audit_payload: Mapping[str, Any],
) -> None:
    if not audit_payload.get("passed"):
        raise ValueError("冻结数据审计未通过，拒绝运行 H1。")
    if not windows_payload.get("overlap_audit", {}).get("passed"):
        raise ValueError("W/O/R 重叠审计未通过，拒绝运行 H1。")
    if tuple(windows_payload.get("random_seeds") or ()) != SEED_VALUES:
        raise ValueError("窗口 manifest 的随机种子不符合冻结协议。")
    if data_manifest.get("direction_mode") != "NEUTRAL" or data_manifest.get("leverage") != 1:
        raise ValueError("冻结数据未保持 NEUTRAL / 1x。")
    if data_manifest.get("production_defaults_changed") is not False:
        raise ValueError("冻结 manifest 未确认生产默认配置保持不变。")
    if not discovery.get("data_previously_viewed") or not data_manifest.get("data_previously_viewed"):
        raise ValueError("已查看数据必须在发现和数据 manifest 中明确标记。")


def _tier_classification(discovery: Mapping[str, Any]) -> dict[str, Any]:
    symbols = list(discovery.get("symbols") or [])
    return {
        "counts": dict(discovery.get("tier_counts") or {}),
        "symbols": {
            tier: sorted(str(item.get("symbol")) for item in symbols if item.get("tier") == tier)
            for tier in ("TIER_A_CORE", "TIER_A_SHORT", "EXCLUDED")
        },
        "reasons": {
            str(item.get("symbol")): list(item.get("exclusion_reasons") or [])
            for item in symbols
            if item.get("tier") != "TIER_A_CORE"
        },
    }


def _data_inventory(data_manifest: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    files: list[dict[str, Any]] = []
    rules: dict[str, Any] = {}
    for symbol, item in sorted((data_manifest.get("assets") or {}).items()):
        rules[str(symbol)] = item.get("rules")
        for kind, meta in sorted((item.get("files") or {}).items()):
            if not isinstance(meta, Mapping) or not meta.get("path"):
                continue
            files.append(
                {
                    "symbol": symbol,
                    "kind": kind,
                    "path": meta.get("path"),
                    "sha256": meta.get("sha256"),
                    "size_bytes": meta.get("size_bytes"),
                    "row_count": meta.get("row_count"),
                }
            )
    return files, rules


def _not_run_matrix() -> dict[str, Any]:
    reason = "H1_FAILED_STOP_RULE"
    return {
        "h2": {"status": NOT_RUN_STATUS, "reason": reason, "checks": {}},
        "baselines": {
            f"B{index}": {"status": NOT_RUN_STATUS, "reason": reason}
            for index in range(6)
        },
        "scenarios": {
            name: {"status": NOT_RUN_STATUS, "reason": reason}
            for name in ("BASE", "COST50", "EXECUTION_STRESS")
        },
        "sensitivity": {"status": NOT_RUN_STATUS, "reason": reason},
        "validation": {"status": NOT_RUN_STATUS, "opened": False, "reason": reason},
        "short_oos": {"status": NOT_RUN_STATUS, "opened": False, "reason": reason},
    }


def _write_not_run_artifacts(output_dir: Path, conclusion: str) -> dict[str, str]:
    reason = "H1_FAILED_STOP_RULE"
    hashes: dict[str, str] = {}
    for filename, fields in DOWNSTREAM_CSV_FIELDS.items():
        row = {field: "" for field in fields}
        row.update({"status": NOT_RUN_STATUS, "reason": reason})
        if "opened" in row:
            row["opened"] = False
        hashes[filename] = write_csv(output_dir / filename, fields, [row])
    markdown = "\n".join(
        [
            "# 固定策略基线",
            "",
            f"状态：`{NOT_RUN_STATUS}`",
            "",
            f"原因：H1 结论为 `{conclusion}`，协议要求立即停止。",
            "",
            "B0–B5、BASE、COST50、EXECUTION_STRESS、Validation、Short OOS 和参数敏感性均未运行。",
            "",
        ]
    )
    hashes["baseline-comparison.md"] = immutable_write(
        output_dir / "baseline-comparison.md", markdown
    )
    return hashes


def _report(payload: Mapping[str, Any]) -> str:
    h1 = payload["h1"]
    lines = [
        "# H1：股票永续周末/节假日低波动假设",
        "",
        "本报告严格在任何网格参数搜索、B0–B5、Validation 或 Short OOS 之前生成。",
        "",
        f"- Tier A-Core：`{h1['tier_a_core_count']}`",
        f"- Development W/O/R：`{h1['development_window_counts']}`",
        f"- Development 日历窗口 W/O/R：`{h1['development_calendar_window_counts']}`",
        f"- W realized volatility 中位数：`{h1['medians']['realized_volatility']['W']}`",
        f"- O realized volatility 中位数：`{h1['medians']['realized_volatility']['O']}`",
        f"- R realized volatility 中位数：`{h1['medians']['realized_volatility']['R']}`",
        "",
        "| H1 门槛 | 结果 |",
        "| --- | --- |",
    ]
    for name, value in (
        ("至少 3 个 Tier A-Core", h1["sample_sufficiency"]["three_core_symbols"]),
        ("W 波动率不高于 O/R 的 90%", h1["volatility_not_higher_than_90pct"]),
        ("W directional efficiency 不高于 O/R", h1["directional_efficiency_not_higher"]),
        ("W fee-adjusted cycle capacity 不低于 O/R", h1["cycle_capacity_not_lower"]),
        ("W fee-adjusted capacity 中位数严格为正", h1["cycle_capacity_strictly_positive"]),
        ("至少 60% 完整月份同方向优势", h1["sixty_percent_month_advantage"]),
        ("优势不只来自一个标的", h1["advantage_not_one_symbol"]),
        ("最佳窗口贡献不超过 35%", h1["best_window_contribution_le_35pct"]),
        ("日历窗口 block bootstrap 支持非随机优势", h1["bootstrap_supports_non_noise"]),
        ("排除上市前 14 天后方向不反转", h1["mature_stage_direction"]["same_direction"]),
        ("W 成交质量不显著差于 O/R", h1["execution_quality"]["not_significantly_worse"]),
    ):
        lines.append(f"| {name} | **{'PASS' if value else 'FAIL'}** |")
    lines.extend(
        [
            "",
            f"失败门槛：`{', '.join(h1['failed_checks']) or '无'}`",
            "",
            f"## 结论：`{payload['conclusion']}`",
            "",
            "H1 失败后已停止参数优化和策略回测；没有读取 Validation/Short OOS，也没有修改生产默认配置。",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    discovery_path = Path(args.discovery)
    data_manifest_path = Path(args.data_manifest)
    window_path = Path(args.window_manifest)
    audit_path = Path(args.audit_json)
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    data_manifest = json.loads(data_manifest_path.read_text(encoding="utf-8"))
    windows_payload = json.loads(window_path.read_text(encoding="utf-8"))
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    _validate_frozen_inputs(discovery, data_manifest, windows_payload, audit_payload)
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    funding_by_symbol: dict[str, list[dict[str, Any]]] = {}
    mark_by_symbol: dict[str, dict[int, float]] = {}
    premium_by_symbol: dict[str, dict[int, float]] = {}
    for symbol, item in data_manifest["assets"].items():
        rows_by_symbol[symbol] = _read_klines(item["files"]["klines"]["path"])
        funding_by_symbol[symbol] = _read_funding(item["files"]["funding"]["path"])
        mark_by_symbol[symbol] = _read_side_map(item["files"]["mark_price"]["path"])
        premium_by_symbol[symbol] = _read_side_map(item["files"]["premium_index"]["path"], signed=True)
    features: list[dict[str, Any]] = []
    for window in windows_payload["windows"]:
        if window.get("status") != "READY":
            continue
        symbol = str(window["symbol"])
        features.append(
            _feature_row(
                window,
                rows_by_symbol[symbol],
                funding_by_symbol[symbol],
                mark_by_symbol[symbol],
                premium_by_symbol[symbol],
            )
        )
    h1 = _h1_checks(features, bootstrap_reps=max(100, args.bootstrap_reps))
    conclusion = (
        "SHORT_HISTORY_STOCK_PERP_WEEKEND_GRID_CANDIDATE"
        if h1["passed"]
        else "STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED"
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    market_features_sha256 = write_csv(
        output_dir / "market-features.csv", FEATURE_FIELDS, features
    )
    data_files, contract_rules = _data_inventory(data_manifest)
    not_run = _not_run_matrix() if not h1["passed"] else {}
    downstream_hashes = (
        _write_not_run_artifacts(output_dir, conclusion)
        if not h1["passed"]
        else {}
    )
    payload = {
        "schema_version": 1,
        "protocol": "docs/codex-stock-perp-weekend-grid-backtest-v2.5.md",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_branch": git_branch(),
        "git_commit": git_commit(),
        "discovery_sha256": hashlib.sha256(discovery_path.read_bytes()).hexdigest(),
        "asset_manifest_sha256": hashlib.sha256(data_manifest_path.read_bytes()).hexdigest(),
        "window_manifest_sha256": hashlib.sha256(window_path.read_bytes()).hexdigest(),
        "data_audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
        "data_audit_path": str(audit_path.resolve()),
        "data_audit_passed": bool(audit_payload.get("passed")),
        "window_overlap_audit": windows_payload.get("overlap_audit"),
        "data_previously_viewed": bool(data_manifest.get("data_previously_viewed")),
        "tier_classification": _tier_classification(discovery),
        "research_splits": windows_payload.get("split_summary"),
        "data_files": data_files,
        "contract_rules": contract_rules,
        "direction_mode": "NEUTRAL",
        "leverage": 1,
        "configuration": {
            "observation_rows": windows_payload.get("observation_rows"),
            "minimum_tradable_rows": windows_payload.get("minimum_tradable_rows"),
            "force_close_minutes": windows_payload.get("force_close_minutes"),
            "step_pct": STEP_PCT,
            "maker_fee_rate": BASE_MAKER_FEE,
            "fee_adjusted_cycle_capacity": (
                "completed_grid_cycles * max(step_pct - 2 * maker_fee_rate, 0)"
            ),
            "production_snapshot_status": NOT_RUN_STATUS if not h1["passed"] else "PENDING_H1_PASS",
        },
        "cost_assumptions": {
            "h1_maker_fee_rate": BASE_MAKER_FEE,
            "base": NOT_RUN_STATUS if not h1["passed"] else "PENDING_H1_PASS",
            "cost50": NOT_RUN_STATUS if not h1["passed"] else "PENDING_H1_PASS",
            "execution_stress": NOT_RUN_STATUS if not h1["passed"] else "PENDING_H1_PASS",
        },
        "random_seeds": list(SEED_VALUES),
        "feature_count": len(features),
        "market_features_path": str((output_dir / "market-features.csv").resolve()),
        "market_features_sha256": market_features_sha256,
        "feature_index": [
            {
                key: row.get(key)
                for key in (
                    "window_id",
                    "symbol",
                    "group",
                    "seed",
                    "matched_window_id",
                    "calendar_key",
                    "month",
                    "split",
                    "listing_stage",
                    "status",
                )
            }
            for row in features
        ],
        "h1": h1,
        **not_run,
        "downstream_artifact_sha256": downstream_hashes,
        "conclusion": conclusion,
        "short_oos_opened": False,
        "validation_opened": False,
        "parameter_search_opened": False,
        "production_defaults_changed": False,
        "master_modified": False,
        "stable_profit_claimed": False,
        "real_money_authorized": False,
        "notes": [
            "统计抽样单位按 calendar_key 聚合，避免同一周末的多个股票被当作完全独立样本。",
            "R 的不同固定种子是重复匹配样本；W/O 不共享实际时间区间。",
            "H1 失败时不得继续运行任何策略回测或参数优化。",
            "所有下游 CSV/Markdown 均为 NOT_RUN_H1_FAILED 占位审计，不包含收益或参数搜索结果。",
        ],
    }
    write_json(output_dir / "market-hypothesis.json", payload)
    immutable_write(output_dir / "market-hypothesis-report.md", _report(payload))
    # This is the protocol-level result.  Downstream scripts must refuse to
    # run unless this conclusion is a passing candidate.
    write_json(output_dir / "results.json", payload)
    immutable_write(
        output_dir / "final-report.md",
        _report(payload)
        + "\n\n`baseline-comparison.csv`, B0–B5、Validation 和 Short OOS 均未运行（H1 gate failed or not opened）。\n",
    )
    return payload


def main() -> None:
    args = _parser().parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "conclusion": result["conclusion"],
                "feature_count": result["feature_count"],
                "failed_checks": result["h1"]["failed_checks"],
                "parameter_search_opened": result["parameter_search_opened"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result["h1"]["passed"]:
        # A failed H1 is an expected research conclusion, not a process error.
        return


if __name__ == "__main__":
    main()
