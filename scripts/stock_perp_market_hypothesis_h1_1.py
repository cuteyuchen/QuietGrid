"""Evaluate the corrected stock-perpetual H1.1 market hypothesis."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import stock_perp_market_hypothesis as v2_5_h1  # noqa: E402
from scripts.rebuild_stock_perp_windows_h1_1 import (  # noqa: E402
    CORE_SYMBOLS,
    CRYPTO_SENSITIVE_EQUITY,
    DEVELOPMENT_MONTHS,
    EXPOSED_VALIDATION_MONTH,
    OUTPUT_DIR,
    PROTOCOL_PATH,
    SOURCE_REPORT_DIR,
    TRADITIONAL_EQUITY,
)
from scripts.stock_perp_common import (  # noqa: E402
    SEED_VALUES,
    git_branch,
    git_commit,
    immutable_write,
    write_csv,
    write_json,
)


STEP_PCT = 0.0015
MAKER_FEE = 0.0002
GROSS_CYCLE_EDGE = max(STEP_PCT - 2 * MAKER_FEE, 0.0)
BOOTSTRAP_REPS = 5000
BOOTSTRAP_SEED = 31
NOT_RUN_STATUS = "NOT_RUN_H1_1_FAILED"
TECHNICAL_INVALID = "H1_1_METHOD_OR_SAMPLE_INVALID"
SUPPORTED = "H1_1_STOCK_PERP_WEEKEND_EFFECT_SUPPORTED"

FEATURE_FIELDS = (
    "window_id",
    "symbol",
    "asset_group",
    "group",
    "seed",
    "matched_window_id",
    "matched_calendar_key",
    "calendar_key",
    "month",
    "split",
    "listing_stage",
    "window_type",
    "status",
    "row_count",
    "tradable_rows",
    "tradable_hours",
    "window_realized_volatility",
    "hourly_realized_volatility",
    "atr_pct",
    "high_low_range_pct",
    "directional_efficiency",
    "max_single_direction_move_pct",
    "return_sign_flip_rate",
    "reversal_legs",
    "reversal_legs_per_hour",
    "completed_grid_cycles",
    "completed_grid_cycles_per_hour",
    "gross_cycle_edge",
    "fee_adjusted_cycle_capacity_window",
    "fee_adjusted_cycle_capacity_per_hour",
    "zero_trade_ratio",
    "trades_per_hour",
    "base_volume_per_hour",
    "quote_volume_per_hour",
    "median_trade_size",
    "aggtrade_event_count",
    "aggtrade_event_count_per_hour",
    "funding_abs_sum_window",
    "funding_abs_per_hour",
    "premium_abs_mean",
    "mark_deviation_abs_mean",
    "first_half_hourly_realized_volatility",
    "second_half_hourly_realized_volatility",
    "second_to_first_volatility_ratio",
    "volatility_expansion",
)

PER_HOUR_FIELDS = (
    "hourly_realized_volatility",
    "reversal_legs_per_hour",
    "completed_grid_cycles_per_hour",
    "fee_adjusted_cycle_capacity_per_hour",
    "trades_per_hour",
    "base_volume_per_hour",
    "quote_volume_per_hour",
    "aggtrade_event_count_per_hour",
    "funding_abs_per_hour",
)

BLOCK_METRICS = (
    "window_realized_volatility",
    "hourly_realized_volatility",
    "directional_efficiency",
    "completed_grid_cycles_per_hour",
    "fee_adjusted_cycle_capacity_per_hour",
    "zero_trade_ratio",
    "trades_per_hour",
    "base_volume_per_hour",
    "quote_volume_per_hour",
    "median_trade_size",
    "aggtrade_event_count_per_hour",
)

BOOTSTRAP_METRICS = {
    "hourly_realized_volatility": True,
    "directional_efficiency": True,
    "fee_adjusted_cycle_capacity_per_hour": False,
    "zero_trade_ratio": True,
    "trades_per_hour": False,
}

COMPARISON_FIELDS = (
    "comparison",
    "scope",
    "seed",
    "metric",
    "w_median",
    "control_median",
    "delta_w_minus_control",
    "ratio_w_to_control",
    "evaluation_status",
)

BREAKDOWN_FIELDS = (
    "scope",
    "month",
    "group",
    "seed",
    "calendar_key",
    "window_type",
    "listing_stage",
    "block_count",
    "symbol_count",
    *BLOCK_METRICS,
)

BOOTSTRAP_FIELDS = (
    "comparison",
    "scope",
    "seed",
    "metric",
    "observed_delta",
    "ci_2_5",
    "ci_97_5",
    "favorable_support_probability",
    "favorable_direction",
    "w_block_count",
    "control_block_count",
    "effective_month_count",
    "effective_traditional_symbol_count",
    "status",
)

DIAGNOSTIC_FIELDS = (
    "window_id",
    "symbol",
    "asset_group",
    "group",
    "seed",
    "calendar_key",
    "month",
    "split",
    "diagnostic_view",
    "status",
    "skip_reason",
    "tradable_rows",
    "tradable_hours",
    "window_realized_volatility",
    "hourly_realized_volatility",
    "directional_efficiency",
    "completed_grid_cycles_per_hour",
    "fee_adjusted_cycle_capacity_per_hour",
    "zero_trade_ratio",
    "trades_per_hour",
    "aggtrade_event_count_per_hour",
)


class AggTradeSeries:
    """Memory-efficient sorted AggTrade timestamp and quote-size arrays."""

    def __init__(self, timestamps: np.ndarray, trade_sizes: np.ndarray) -> None:
        self.timestamps = timestamps
        self.trade_sizes = trade_sizes

    @classmethod
    def load(cls, path: str | Path, *, expected_rows: int) -> "AggTradeSeries":
        timestamps = np.empty(expected_rows, dtype=np.int64)
        trade_sizes = np.empty(expected_rows, dtype=np.float64)
        cursor = 0
        for chunk in pd.read_csv(
            path,
            usecols=["price", "quantity", "transact_time"],
            dtype={
                "price": "float64",
                "quantity": "float64",
                "transact_time": "int64",
            },
            chunksize=1_000_000,
        ):
            count = len(chunk)
            if cursor + count > expected_rows:
                raise ValueError(f"AggTrades row count exceeds manifest: {path}")
            timestamps[cursor : cursor + count] = chunk["transact_time"].to_numpy(
                copy=False
            )
            trade_sizes[cursor : cursor + count] = (
                chunk["price"].to_numpy(copy=False)
                * chunk["quantity"].to_numpy(copy=False)
            )
            cursor += count
        if cursor != expected_rows:
            raise ValueError(
                f"AggTrades row count mismatch for {path}: {cursor} != {expected_rows}"
            )
        if cursor > 1 and np.any(timestamps[1:] < timestamps[:-1]):
            raise ValueError(f"AggTrades timestamps are not sorted: {path}")
        return cls(timestamps, trade_sizes)

    def metrics(self, start_ms: int, end_ms: int, *, hours: float) -> dict[str, Any]:
        left = int(np.searchsorted(self.timestamps, start_ms, side="left"))
        right = int(np.searchsorted(self.timestamps, end_ms, side="left"))
        count = right - left
        median_size = (
            float(np.median(self.trade_sizes[left:right])) if count > 0 else 0.0
        )
        return {
            "median_trade_size": median_size,
            "aggtrade_event_count": count,
            "aggtrade_event_count_per_hour": count / hours,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run corrected stock-perp H1.1")
    parser.add_argument(
        "--discovery", default=str(SOURCE_REPORT_DIR / "symbol-discovery.json")
    )
    parser.add_argument(
        "--data-manifest",
        default=str(SOURCE_REPORT_DIR / "asset-data-manifest.json"),
    )
    parser.add_argument(
        "--audit-json", default=str(SOURCE_REPORT_DIR / "asset-data-audit.json")
    )
    parser.add_argument(
        "--window-manifest",
        default=str(OUTPUT_DIR / "window-manifest-h1-1.json"),
    )
    parser.add_argument(
        "--input-hash-manifest",
        default=str(OUTPUT_DIR / "input-hash-manifest.json"),
    )
    parser.add_argument("--protocol", default=str(PROTOCOL_PATH))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--bootstrap-reps", type=int, default=BOOTSTRAP_REPS)
    parser.add_argument("--pytest-exit-code", type=int)
    parser.add_argument("--pytest-summary", default="")
    parser.add_argument("--pytest-command", default=".venv python -m pytest -q")
    parser.add_argument("--pytest-stdout-log", default="")
    parser.add_argument("--pytest-stderr-log", default="")
    return parser


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _returns_with_previous(
    closes: Sequence[float],
    previous_close: float,
) -> list[float]:
    if not closes or previous_close <= 0:
        return []
    result: list[float] = []
    left = previous_close
    for right in closes:
        if left > 0 and right > 0:
            result.append(math.log(right / left))
        left = right
    return result


def _hourly_realized_volatility(
    closes: Sequence[float],
    previous_close: float,
) -> tuple[float, float]:
    if not closes:
        return 0.0, 0.0
    returns = _returns_with_previous(closes, previous_close)
    hours = len(closes) / 60.0
    window_value = math.sqrt(sum(value * value for value in returns))
    hourly_value = math.sqrt(sum(value * value for value in returns) / hours)
    return window_value, hourly_value


def _metric_values(
    rows: Sequence[Mapping[str, Any]],
    *,
    previous_close: float,
    funding: Sequence[Mapping[str, Any]],
    mark: Mapping[int, float],
    premium: Mapping[int, float],
    agg_trades: AggTradeSeries | None,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("Feature metrics require at least one tradable row")
    closes = [float(row["close"]) for row in rows]
    returns = _returns_with_previous(closes, previous_close)
    hours = len(rows) / 60.0
    window_rv = math.sqrt(sum(value * value for value in returns))
    hourly_rv = math.sqrt(sum(value * value for value in returns) / hours)

    tr_values: list[float] = []
    prior = previous_close
    for row in rows:
        high = float(row["high"])
        low = float(row["low"])
        tr_values.append(max(high - low, abs(high - prior), abs(low - prior)))
        prior = float(row["close"])
    atr_pct = statistics.fmean(
        value / close for value, close in zip(tr_values, closes) if close > 0
    )
    high_low_range_pct = (
        (max(float(row["high"]) for row in rows) - min(float(row["low"]) for row in rows))
        / closes[0]
        if closes[0] > 0
        else 0.0
    )
    directional_denominator = abs(closes[0] - previous_close) + sum(
        abs(right - left) for left, right in zip(closes, closes[1:])
    )
    directional_efficiency = (
        abs(closes[-1] - previous_close) / directional_denominator
        if directional_denominator > 0
        else 0.0
    )
    cycle_closes = [previous_close, *closes]
    reversal_legs, completed_cycles = v2_5_h1._grid_cycles(
        cycle_closes, STEP_PCT
    )
    start_ms = int(rows[0]["open_time"])
    end_ms = int(rows[-1]["close_time"]) + 1
    funding_values = [
        abs(float(item["funding_rate"]))
        for item in funding
        if start_ms <= int(item["funding_time"]) < end_ms
    ]
    premium_values = [
        abs(float(premium[int(row["open_time"])]))
        for row in rows
        if int(row["open_time"]) in premium
    ]
    mark_values = [
        abs(
            (float(mark[int(row["open_time"])]) - float(row["close"]))
            / float(row["close"])
        )
        for row in rows
        if int(row["open_time"]) in mark and float(row["close"]) > 0
    ]

    midpoint = max(1, len(rows) // 2)
    first_rows = rows[:midpoint]
    second_rows = rows[midpoint:]
    _, first_half_hourly = _hourly_realized_volatility(
        [float(row["close"]) for row in first_rows], previous_close
    )
    second_previous = float(first_rows[-1]["close"])
    _, second_half_hourly = (
        _hourly_realized_volatility(
            [float(row["close"]) for row in second_rows], second_previous
        )
        if second_rows
        else (0.0, 0.0)
    )
    volatility_ratio = (
        second_half_hourly / first_half_hourly
        if first_half_hourly > 0
        else None
    )
    volatility_expansion = float(
        second_half_hourly > 0
        and (first_half_hourly == 0 or second_half_hourly >= first_half_hourly * 1.5)
    )
    total_trades = sum(int(row["trade_count"]) for row in rows)
    total_base_volume = sum(float(row["volume"]) for row in rows)
    total_quote_volume = sum(float(row["quote_volume"]) for row in rows)
    agg_metrics = (
        agg_trades.metrics(start_ms, end_ms, hours=hours)
        if agg_trades is not None
        else {
            "median_trade_size": 0.0,
            "aggtrade_event_count": 0,
            "aggtrade_event_count_per_hour": 0.0,
        }
    )
    return {
        "row_count": len(rows),
        "tradable_rows": len(rows),
        "tradable_hours": hours,
        "window_realized_volatility": window_rv,
        "hourly_realized_volatility": hourly_rv,
        "atr_pct": atr_pct,
        "high_low_range_pct": high_low_range_pct,
        "directional_efficiency": directional_efficiency,
        "max_single_direction_move_pct": v2_5_h1._max_directional_move(
            cycle_closes
        ),
        "return_sign_flip_rate": v2_5_h1._sign_flip_rate(returns),
        "reversal_legs": reversal_legs,
        "reversal_legs_per_hour": reversal_legs / hours,
        "completed_grid_cycles": completed_cycles,
        "completed_grid_cycles_per_hour": completed_cycles / hours,
        "gross_cycle_edge": GROSS_CYCLE_EDGE,
        "fee_adjusted_cycle_capacity_window": completed_cycles
        * GROSS_CYCLE_EDGE,
        "fee_adjusted_cycle_capacity_per_hour": completed_cycles
        / hours
        * GROSS_CYCLE_EDGE,
        "zero_trade_ratio": sum(
            int(row["trade_count"]) == 0 or float(row["volume"]) == 0
            for row in rows
        )
        / len(rows),
        "trades_per_hour": total_trades / hours,
        "base_volume_per_hour": total_base_volume / hours,
        "quote_volume_per_hour": total_quote_volume / hours,
        **agg_metrics,
        "funding_abs_sum_window": sum(funding_values),
        "funding_abs_per_hour": sum(funding_values) / hours,
        "premium_abs_mean": statistics.fmean(premium_values)
        if premium_values
        else 0.0,
        "mark_deviation_abs_mean": statistics.fmean(mark_values)
        if mark_values
        else 0.0,
        "first_half_hourly_realized_volatility": first_half_hourly,
        "second_half_hourly_realized_volatility": second_half_hourly,
        "second_to_first_volatility_ratio": volatility_ratio,
        "volatility_expansion": volatility_expansion,
    }


def _feature_row(
    window: Mapping[str, Any],
    klines: Sequence[Mapping[str, Any]],
    funding: Sequence[Mapping[str, Any]],
    mark: Mapping[int, float],
    premium: Mapping[int, float],
    agg_trades: AggTradeSeries | None = None,
) -> dict[str, Any]:
    start = int(window["row_start_index"]) + int(window["observation_rows"])
    end = int(window["row_end_index"])
    identity = {field: window.get(field, "") for field in FEATURE_FIELDS}
    if end <= start:
        return identity | {
            "status": "SKIPPED",
            "row_count": 0,
            "tradable_rows": 0,
            "tradable_hours": 0.0,
        }
    rows = list(klines[start:end])
    previous_close = (
        float(klines[start - 1]["close"])
        if start > 0
        else float(rows[0]["open"])
    )
    return identity | {
        "status": "READY",
        **_metric_values(
            rows,
            previous_close=previous_close,
            funding=funding,
            mark=mark,
            premium=premium,
            agg_trades=agg_trades,
        ),
    }


def _diagnostic_rows(
    window: Mapping[str, Any],
    klines: Sequence[Mapping[str, Any]],
    funding: Sequence[Mapping[str, Any]],
    mark: Mapping[int, float],
    premium: Mapping[int, float],
    agg_trades: AggTradeSeries | None,
) -> list[dict[str, Any]]:
    start = int(window["row_start_index"]) + int(window["observation_rows"])
    end = int(window["row_end_index"])
    group = str(window["group"])
    specifications: list[tuple[str, int, int]] = []
    if group == "W":
        specifications = [
            ("W_HEAD_10H", start, min(end, start + 600)),
            ("W_TAIL_10H", max(start, end - 600), end),
        ]
    elif group == "O":
        specifications = [("O_FULL", start, end)]
    elif group == "R":
        specifications = [("R_HEAD_10H", start, min(end, start + 600))]
    result: list[dict[str, Any]] = []
    for label, diag_start, diag_end in specifications:
        row_count = diag_end - diag_start
        base = {
            "window_id": window.get("window_id"),
            "symbol": window.get("symbol"),
            "asset_group": window.get("asset_group"),
            "group": group,
            "seed": window.get("seed"),
            "calendar_key": window.get("calendar_key"),
            "month": window.get("month"),
            "split": window.get("split"),
            "diagnostic_view": label,
        }
        if row_count < 600:
            result.append(
                base
                | {
                    "status": "SKIPPED",
                    "skip_reason": "INSUFFICIENT_10H_TRADABLE_ROWS",
                    "tradable_rows": row_count,
                    "tradable_hours": row_count / 60.0,
                }
            )
            continue
        rows = list(klines[diag_start:diag_end])
        previous_close = (
            float(klines[diag_start - 1]["close"])
            if diag_start > 0
            else float(rows[0]["open"])
        )
        metrics = _metric_values(
            rows,
            previous_close=previous_close,
            funding=funding,
            mark=mark,
            premium=premium,
            agg_trades=agg_trades,
        )
        result.append(base | {"status": "READY", "skip_reason": ""} | metrics)
    return result


def _scope_symbols(scope: str) -> tuple[str, ...]:
    if scope == "ALL_CORE":
        return CORE_SYMBOLS
    if scope == "TRADITIONAL_EQUITY":
        return TRADITIONAL_EQUITY
    if scope == "CRYPTO_SENSITIVE_EQUITY":
        return CRYPTO_SENSITIVE_EQUITY
    if scope.startswith("SYMBOL:"):
        symbol = scope.split(":", 1)[1]
        if symbol not in CORE_SYMBOLS:
            raise ValueError(f"Unknown symbol scope: {scope}")
        return (symbol,)
    raise ValueError(f"Unknown scope: {scope}")


def _all_scopes() -> tuple[str, ...]:
    return (
        "ALL_CORE",
        "TRADITIONAL_EQUITY",
        "CRYPTO_SENSITIVE_EQUITY",
        *(f"SYMBOL:{symbol}" for symbol in CORE_SYMBOLS),
    )


def _calendar_blocks(
    features: Sequence[Mapping[str, Any]],
    *,
    group: str,
    scope: str,
    split: str = "RESEARCH_DEVELOPMENT",
    seed: int | None = None,
    month: str | None = None,
    listing_stage: str | None = None,
    window_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    symbols = set(_scope_symbols(scope))
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in features:
        if (
            row.get("status") != "READY"
            or row.get("group") != group
            or row.get("split") != split
            or row.get("symbol") not in symbols
        ):
            continue
        if seed is not None and int(row.get("seed") or -1) != seed:
            continue
        if month is not None and row.get("month") != month:
            continue
        if listing_stage is not None and row.get("listing_stage") != listing_stage:
            continue
        if window_types is not None and row.get("window_type") not in window_types:
            continue
        grouped[str(row["calendar_key"])].append(row)
    blocks: list[dict[str, Any]] = []
    for key, rows in sorted(grouped.items()):
        values: dict[str, Any] = {
            "scope": scope,
            "month": str(rows[0]["month"]),
            "group": group,
            "seed": rows[0].get("seed", ""),
            "calendar_key": key,
            "block_count": 1,
            "symbol_count": len({str(row["symbol"]) for row in rows}),
            "symbols": sorted({str(row["symbol"]) for row in rows}),
            "window_type": str(rows[0].get("window_type") or ""),
            "listing_stage": ",".join(
                sorted({str(row.get("listing_stage") or "") for row in rows})
            ),
        }
        for metric in BLOCK_METRICS:
            numbers = [
                value
                for row in rows
                for value in [_float_or_none(row.get(metric))]
                if value is not None
            ]
            values[metric] = statistics.median(numbers) if numbers else None
        blocks.append(values)
    return blocks


def _block_median(blocks: Sequence[Mapping[str, Any]], metric: str) -> float | None:
    values = [
        value
        for row in blocks
        for value in [_float_or_none(row.get(metric))]
        if value is not None
    ]
    return statistics.median(values) if values else None


def _comparison_rows(
    features: Sequence[Mapping[str, Any]],
    *,
    control_group: str,
    evaluation_status: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    comparison = f"W_VS_{control_group}"
    for scope in _all_scopes():
        w_blocks = _calendar_blocks(features, group="W", scope=scope)
        seeds: Sequence[int | None] = SEED_VALUES if control_group == "R" else (None,)
        seed_medians: dict[str, list[float]] = defaultdict(list)
        for seed in seeds:
            control_blocks = _calendar_blocks(
                features,
                group=control_group,
                scope=scope,
                seed=seed,
            )
            for metric in BLOCK_METRICS:
                w_median = _block_median(w_blocks, metric)
                control_median = _block_median(control_blocks, metric)
                if control_median is not None:
                    seed_medians[metric].append(control_median)
                rows.append(
                    {
                        "comparison": comparison,
                        "scope": scope,
                        "seed": seed if seed is not None else "",
                        "metric": metric,
                        "w_median": w_median,
                        "control_median": control_median,
                        "delta_w_minus_control": (
                            w_median - control_median
                            if w_median is not None and control_median is not None
                            else None
                        ),
                        "ratio_w_to_control": (
                            w_median / control_median
                            if w_median is not None
                            and control_median not in (None, 0.0)
                            else None
                        ),
                        "evaluation_status": evaluation_status,
                    }
                )
        if control_group == "R":
            for metric in BLOCK_METRICS:
                w_median = _block_median(w_blocks, metric)
                controls = seed_medians.get(metric) or []
                control_median = statistics.median(controls) if controls else None
                rows.append(
                    {
                        "comparison": comparison,
                        "scope": scope,
                        "seed": "SEED_MEDIAN",
                        "metric": metric,
                        "w_median": w_median,
                        "control_median": control_median,
                        "delta_w_minus_control": (
                            w_median - control_median
                            if w_median is not None and control_median is not None
                            else None
                        ),
                        "ratio_w_to_control": (
                            w_median / control_median
                            if w_median is not None
                            and control_median not in (None, 0.0)
                            else None
                        ),
                        "evaluation_status": evaluation_status,
                    }
                )
    return rows


def _bootstrap_difference(
    w_values: Sequence[float],
    control_values: Sequence[float],
    *,
    lower_is_better: bool,
    reps: int,
) -> dict[str, Any]:
    w = np.asarray(w_values, dtype=np.float64)
    control = np.asarray(control_values, dtype=np.float64)
    if w.size == 0 or control.size == 0:
        return {
            "observed_delta": None,
            "ci_2_5": None,
            "ci_97_5": None,
            "favorable_support_probability": None,
            "status": "INSUFFICIENT_BLOCKS",
        }
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    w_indexes = rng.integers(0, w.size, size=(reps, w.size))
    c_indexes = rng.integers(0, control.size, size=(reps, control.size))
    deltas = np.median(w[w_indexes], axis=1) - np.median(
        control[c_indexes], axis=1
    )
    observed = float(np.median(w) - np.median(control))
    favorable = deltas <= 0 if lower_is_better else deltas >= 0
    return {
        "observed_delta": observed,
        "ci_2_5": float(np.percentile(deltas, 2.5)),
        "ci_97_5": float(np.percentile(deltas, 97.5)),
        "favorable_support_probability": float(np.mean(favorable)),
        "status": "READY",
    }


def _bootstrap_rows(
    features: Sequence[Mapping[str, Any]],
    *,
    reps: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for comparison, control_group in (("W_VS_O", "O"), ("W_VS_R", "R")):
        for scope in _all_scopes():
            w_blocks = _calendar_blocks(features, group="W", scope=scope)
            seeds: Sequence[int | None] = (
                SEED_VALUES if control_group == "R" else (None,)
            )
            for seed in seeds:
                control_blocks = _calendar_blocks(
                    features,
                    group=control_group,
                    scope=scope,
                    seed=seed,
                )
                effective_months = len(
                    {str(row["month"]) for row in w_blocks}
                    & {str(row["month"]) for row in control_blocks}
                )
                traditional_symbols = len(
                    set(TRADITIONAL_EQUITY)
                    & {
                        str(symbol)
                        for row in [*w_blocks, *control_blocks]
                        for symbol in row.get("symbols") or []
                    }
                )
                for metric, lower_is_better in BOOTSTRAP_METRICS.items():
                    w_values = [
                        float(row[metric])
                        for row in w_blocks
                        if row.get(metric) is not None
                    ]
                    control_values = [
                        float(row[metric])
                        for row in control_blocks
                        if row.get(metric) is not None
                    ]
                    bootstrap = _bootstrap_difference(
                        w_values,
                        control_values,
                        lower_is_better=lower_is_better,
                        reps=reps,
                    )
                    result.append(
                        {
                            "comparison": comparison,
                            "scope": scope,
                            "seed": seed if seed is not None else "",
                            "metric": metric,
                            **bootstrap,
                            "favorable_direction": (
                                "W<=CONTROL" if lower_is_better else "W>=CONTROL"
                            ),
                            "w_block_count": len(w_values),
                            "control_block_count": len(control_values),
                            "effective_month_count": effective_months,
                            "effective_traditional_symbol_count": traditional_symbols,
                        }
                    )
    return result


def _technical_checks(
    *,
    window_payload: Mapping[str, Any],
    input_hash_payload: Mapping[str, Any],
    audit_payload: Mapping[str, Any],
    features: Sequence[Mapping[str, Any]],
    pytest_exit_code: int | None,
) -> dict[str, Any]:
    windows = list(window_payload.get("windows") or [])
    development_ready = [
        row
        for row in windows
        if row.get("status") == "READY"
        and row.get("split") == "RESEARCH_DEVELOPMENT"
    ]
    block_counts = window_payload.get("count_audit", {}).get(
        "calendar_block_counts_by_split", {}
    ).get("RESEARCH_DEVELOPMENT", {})
    r_by_seed = window_payload.get("count_audit", {}).get(
        "development_r_blocks_by_seed", {}
    )
    complete_traditional = []
    for symbol in TRADITIONAL_EQUITY:
        groups = {
            str(row["group"])
            for row in features
            if row.get("status") == "READY"
            and row.get("split") == "RESEARCH_DEVELOPMENT"
            and row.get("symbol") == symbol
        }
        if groups == {"W", "O", "R"}:
            complete_traditional.append(symbol)
    per_hour_complete = bool(features) and all(
        all(_float_or_none(row.get(field)) is not None for field in PER_HOUR_FIELDS)
        for row in features
        if row.get("status") == "READY"
    )
    june_windows = [row for row in windows if row.get("month") == EXPOSED_VALIDATION_MONTH]
    checks = {
        "eight_core_data_audits_pass": bool(audit_payload.get("passed"))
        and len(
            [
                row
                for row in audit_payload.get("assets") or []
                if row.get("status") == "PASS" and row.get("symbol") in CORE_SYMBOLS
            ]
        )
        == 8,
        "input_hashes_match": bool(input_hash_payload.get("passed")),
        "wor_overlap_zero": bool(
            window_payload.get("overlap_audit", {}).get("passed")
        ),
        "o_not_deleted_by_r": bool(
            window_payload.get("count_audit", {}).get("o_unchanged_by_random")
        ),
        "o_count_before_equals_after": window_payload.get("o_count_before_random")
        == window_payload.get("o_count_after_random"),
        "at_least_12_w_development_blocks": int(block_counts.get("W", 0)) >= 12,
        "at_least_30_o_development_blocks": int(block_counts.get("O", 0)) >= 30,
        "at_least_10_r_development_blocks_each_seed": all(
            int(r_by_seed.get(str(seed), 0)) >= 10 for seed in SEED_VALUES
        ),
        "three_complete_development_months": {
            str(row["month"])
            for row in development_ready
            if row.get("group") == "W"
        }
        == set(DEVELOPMENT_MONTHS),
        "three_traditional_symbols_complete": len(complete_traditional) >= 3,
        "all_duration_metrics_have_per_hour_fields": per_hour_complete,
        "june_is_research_validation_exposed": bool(june_windows)
        and all(
            row.get("split") == "RESEARCH_VALIDATION_EXPOSED"
            for row in june_windows
        ),
        "future_forward_oos_not_computed": not any(
            row.get("split") == "FORWARD_OOS_FUTURE" for row in features
        )
        and window_payload.get("forward_oos_read") is False,
        "full_pytest_passed": pytest_exit_code == 0,
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "development_calendar_block_counts": dict(block_counts),
        "development_r_blocks_by_seed": dict(r_by_seed),
        "complete_traditional_symbols": complete_traditional,
        "development_months": list(DEVELOPMENT_MONTHS),
    }


def _scope_medians(
    features: Sequence[Mapping[str, Any]],
    *,
    scope: str,
    listing_stage: str | None = None,
    month: str | None = None,
    window_types: set[str] | None = None,
) -> dict[str, dict[str, float | None]]:
    result: dict[str, dict[str, float | None]] = {}
    for group in ("W", "O"):
        blocks = _calendar_blocks(
            features,
            group=group,
            scope=scope,
            listing_stage=listing_stage,
            month=month,
            window_types=window_types,
        )
        result[group] = {
            metric: _block_median(blocks, metric) for metric in BLOCK_METRICS
        }
    r_seed_values: dict[str, list[float]] = defaultdict(list)
    for seed in SEED_VALUES:
        blocks = _calendar_blocks(
            features,
            group="R",
            scope=scope,
            seed=seed,
            listing_stage=listing_stage,
            month=month,
            window_types=window_types,
        )
        for metric in BLOCK_METRICS:
            value = _block_median(blocks, metric)
            if value is not None:
                r_seed_values[metric].append(value)
    result["R"] = {
        metric: statistics.median(r_seed_values[metric])
        if r_seed_values.get(metric)
        else None
        for metric in BLOCK_METRICS
    }
    return result


def _bootstrap_support(
    rows: Sequence[Mapping[str, Any]],
    *,
    comparison: str,
    scope: str,
    metric: str,
) -> float | None:
    values = [
        float(row["favorable_support_probability"])
        for row in rows
        if row.get("comparison") == comparison
        and row.get("scope") == scope
        and row.get("metric") == metric
        and row.get("status") == "READY"
        and row.get("favorable_support_probability") is not None
    ]
    return statistics.median(values) if values else None


def _comparison_gate(
    medians: Mapping[str, Mapping[str, float | None]],
    *,
    control: str,
    bootstrap_support: float | None,
) -> dict[str, bool]:
    w = medians["W"]
    c = medians[control]
    return {
        "hourly_volatility_le_90pct": bool(
            w["hourly_realized_volatility"] is not None
            and c["hourly_realized_volatility"] is not None
            and w["hourly_realized_volatility"]
            <= c["hourly_realized_volatility"] * 0.9
        ),
        "directional_efficiency_not_higher": bool(
            w["directional_efficiency"] is not None
            and c["directional_efficiency"] is not None
            and w["directional_efficiency"] <= c["directional_efficiency"]
        ),
        "cycles_per_hour_not_lower": bool(
            w["completed_grid_cycles_per_hour"] is not None
            and c["completed_grid_cycles_per_hour"] is not None
            and w["completed_grid_cycles_per_hour"]
            >= c["completed_grid_cycles_per_hour"]
        ),
        "capacity_per_hour_not_lower": bool(
            w["fee_adjusted_cycle_capacity_per_hour"] is not None
            and c["fee_adjusted_cycle_capacity_per_hour"] is not None
            and w["fee_adjusted_cycle_capacity_per_hour"]
            >= c["fee_adjusted_cycle_capacity_per_hour"]
        ),
        "zero_trade_ratio_acceptable": bool(
            w["zero_trade_ratio"] is not None
            and c["zero_trade_ratio"] is not None
            and w["zero_trade_ratio"] <= c["zero_trade_ratio"] * 1.25 + 0.01
        ),
        "trades_per_hour_at_least_75pct": bool(
            w["trades_per_hour"] is not None
            and c["trades_per_hour"] is not None
            and w["trades_per_hour"] >= c["trades_per_hour"] * 0.75
        ),
        "capacity_bootstrap_support_ge_95pct": bool(
            bootstrap_support is not None and bootstrap_support >= 0.95
        ),
    }


def _economic_checks(
    features: Sequence[Mapping[str, Any]],
    bootstrap_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    all_medians = _scope_medians(features, scope="ALL_CORE")
    w_o_support = _bootstrap_support(
        bootstrap_rows,
        comparison="W_VS_O",
        scope="ALL_CORE",
        metric="fee_adjusted_cycle_capacity_per_hour",
    )
    w_r_support = _bootstrap_support(
        bootstrap_rows,
        comparison="W_VS_R",
        scope="ALL_CORE",
        metric="fee_adjusted_cycle_capacity_per_hour",
    )
    w_vs_o = _comparison_gate(
        all_medians, control="O", bootstrap_support=w_o_support
    )
    w_vs_o["w_capacity_strictly_positive"] = bool(
        all_medians["W"]["fee_adjusted_cycle_capacity_per_hour"] is not None
        and all_medians["W"]["fee_adjusted_cycle_capacity_per_hour"] > 0
    )
    w_vs_r = _comparison_gate(
        all_medians, control="R", bootstrap_support=w_r_support
    )

    month_advantage: dict[str, bool] = {}
    for month in DEVELOPMENT_MONTHS:
        medians = _scope_medians(features, scope="ALL_CORE", month=month)
        w_capacity = medians["W"]["fee_adjusted_cycle_capacity_per_hour"]
        o_capacity = medians["O"]["fee_adjusted_cycle_capacity_per_hour"]
        r_capacity = medians["R"]["fee_adjusted_cycle_capacity_per_hour"]
        month_advantage[month] = bool(
            w_capacity is not None
            and o_capacity is not None
            and r_capacity is not None
            and w_capacity >= o_capacity
            and w_capacity >= r_capacity
        )

    traditional_medians = _scope_medians(
        features, scope="TRADITIONAL_EQUITY"
    )
    traditional_w_o = _comparison_gate(
        traditional_medians, control="O", bootstrap_support=1.0
    )
    traditional_w_r = _comparison_gate(
        traditional_medians, control="R", bootstrap_support=1.0
    )
    traditional_direction = all(
        traditional_w_o[name] and traditional_w_r[name]
        for name in (
            "hourly_volatility_le_90pct",
            "directional_efficiency_not_higher",
            "cycles_per_hour_not_lower",
            "capacity_per_hour_not_lower",
        )
    )
    symbol_advantage: dict[str, bool] = {}
    for symbol in TRADITIONAL_EQUITY:
        medians = _scope_medians(features, scope=f"SYMBOL:{symbol}")
        w_capacity = medians["W"]["fee_adjusted_cycle_capacity_per_hour"]
        o_capacity = medians["O"]["fee_adjusted_cycle_capacity_per_hour"]
        r_capacity = medians["R"]["fee_adjusted_cycle_capacity_per_hour"]
        symbol_advantage[symbol] = bool(
            w_capacity is not None
            and o_capacity is not None
            and r_capacity is not None
            and w_capacity >= o_capacity
            and w_capacity >= r_capacity
        )

    mature = _scope_medians(
        features,
        scope="ALL_CORE",
        listing_stage="LISTING_AFTER_30_DAYS",
    )
    mature_same_direction = bool(
        mature["W"]["fee_adjusted_cycle_capacity_per_hour"] is not None
        and mature["O"]["fee_adjusted_cycle_capacity_per_hour"] is not None
        and mature["R"]["fee_adjusted_cycle_capacity_per_hour"] is not None
        and mature["W"]["fee_adjusted_cycle_capacity_per_hour"]
        >= mature["O"]["fee_adjusted_cycle_capacity_per_hour"]
        and mature["W"]["fee_adjusted_cycle_capacity_per_hour"]
        >= mature["R"]["fee_adjusted_cycle_capacity_per_hour"]
    )
    w_blocks = _calendar_blocks(features, group="W", scope="ALL_CORE")
    control_capacity = max(
        float(value)
        for value in (
            all_medians["O"]["fee_adjusted_cycle_capacity_per_hour"],
            all_medians["R"]["fee_adjusted_cycle_capacity_per_hour"],
        )
        if value is not None
    )
    positive_advantages = [
        max(
            0.0,
            float(row["fee_adjusted_cycle_capacity_per_hour"])
            - control_capacity,
        )
        for row in w_blocks
        if row.get("fee_adjusted_cycle_capacity_per_hour") is not None
    ]
    positive_total = sum(positive_advantages)
    best_concentration = (
        max(positive_advantages) / positive_total if positive_total > 0 else 1.0
    )
    seed_direction: dict[str, bool] = {}
    w_capacity = all_medians["W"]["fee_adjusted_cycle_capacity_per_hour"]
    for seed in SEED_VALUES:
        r_capacity = _block_median(
            _calendar_blocks(
                features, group="R", scope="ALL_CORE", seed=seed
            ),
            "fee_adjusted_cycle_capacity_per_hour",
        )
        seed_direction[str(seed)] = bool(
            w_capacity is not None
            and r_capacity is not None
            and w_capacity >= r_capacity
        )

    window_types = {
        str(row.get("window_type"))
        for row in features
        if row.get("group") == "W"
        and row.get("split") == "RESEARCH_DEVELOPMENT"
        and row.get("status") == "READY"
    }
    crypto = _scope_medians(features, scope="CRYPTO_SENSITIVE_EQUITY")
    traditional_capacity_advantage = bool(
        traditional_medians["W"]["fee_adjusted_cycle_capacity_per_hour"]
        is not None
        and traditional_medians["O"]["fee_adjusted_cycle_capacity_per_hour"]
        is not None
        and traditional_medians["R"]["fee_adjusted_cycle_capacity_per_hour"]
        is not None
        and traditional_medians["W"]["fee_adjusted_cycle_capacity_per_hour"]
        >= traditional_medians["O"]["fee_adjusted_cycle_capacity_per_hour"]
        and traditional_medians["W"]["fee_adjusted_cycle_capacity_per_hour"]
        >= traditional_medians["R"]["fee_adjusted_cycle_capacity_per_hour"]
    )
    crypto_capacity_advantage = bool(
        crypto["W"]["fee_adjusted_cycle_capacity_per_hour"] is not None
        and crypto["O"]["fee_adjusted_cycle_capacity_per_hour"] is not None
        and crypto["R"]["fee_adjusted_cycle_capacity_per_hour"] is not None
        and crypto["W"]["fee_adjusted_cycle_capacity_per_hour"]
        >= crypto["O"]["fee_adjusted_cycle_capacity_per_hour"]
        and crypto["W"]["fee_adjusted_cycle_capacity_per_hour"]
        >= crypto["R"]["fee_adjusted_cycle_capacity_per_hour"]
    )
    stability = {
        "month_advantage": month_advantage,
        "at_least_60pct_months": sum(month_advantage.values())
        / len(month_advantage)
        >= 0.6,
        "traditional_group_direction_passed": traditional_direction,
        "traditional_symbol_advantage": symbol_advantage,
        "at_least_two_traditional_symbols": sum(symbol_advantage.values()) >= 2,
        "crypto_sensitive_not_sole_driver": not (
            crypto_capacity_advantage and not traditional_capacity_advantage
        ),
        "mature_stage_direction_not_reversed": mature_same_direction,
        "regular_and_long_weekends_reported": {
            "REGULAR_WEEKEND",
            "THREE_DAY_LONG_WEEKEND",
        }.issubset(window_types),
        "best_window_contribution_le_35pct": best_concentration <= 0.35,
        "best_window_concentration": best_concentration,
        "not_dependent_on_one_symbol": sum(symbol_advantage.values()) >= 2,
        "seed_direction": seed_direction,
        "seed_direction_at_least_5_of_6": sum(seed_direction.values()) >= 5,
        "no_block_duration_liquidity_anomaly_explains_advantage": bool(
            w_vs_o["zero_trade_ratio_acceptable"]
            and w_vs_o["trades_per_hour_at_least_75pct"]
            and w_vs_r["zero_trade_ratio_acceptable"]
            and w_vs_r["trades_per_hour_at_least_75pct"]
        ),
    }
    required = [
        *w_vs_o.values(),
        *w_vs_r.values(),
        stability["at_least_60pct_months"],
        stability["traditional_group_direction_passed"],
        stability["at_least_two_traditional_symbols"],
        stability["crypto_sensitive_not_sole_driver"],
        stability["mature_stage_direction_not_reversed"],
        stability["regular_and_long_weekends_reported"],
        stability["best_window_contribution_le_35pct"],
        stability["not_dependent_on_one_symbol"],
        stability["seed_direction_at_least_5_of_6"],
        stability["no_block_duration_liquidity_anomaly_explains_advantage"],
    ]
    return {
        "medians": all_medians,
        "w_vs_o": w_vs_o,
        "w_vs_r": w_vs_r,
        "stability": stability,
        "passed": all(required),
    }


def _conclusion(economic: Mapping[str, Any]) -> str:
    if economic.get("passed"):
        return SUPPORTED
    w_o = economic.get("w_vs_o") or {}
    w_r = economic.get("w_vs_r") or {}
    stability = economic.get("stability") or {}
    if w_o and all(w_o.values()) and w_r and not all(w_r.values()):
        return "WEEKEND_EFFECT_NOT_UNIQUE_VS_MATCHED_RANDOM"
    liquidity_names = (
        "zero_trade_ratio_acceptable",
        "trades_per_hour_at_least_75pct",
    )
    path_names = (
        "hourly_volatility_le_90pct",
        "directional_efficiency_not_higher",
        "cycles_per_hour_not_lower",
        "capacity_per_hour_not_lower",
    )
    if all(w_o.get(name) and w_r.get(name) for name in path_names) and not all(
        w_o.get(name) and w_r.get(name) for name in liquidity_names
    ):
        return "WEEKEND_STRUCTURE_NOT_EXECUTABLE_AT_OBSERVED_LIQUIDITY"
    if not stability.get("traditional_group_direction_passed"):
        crypto_only = not stability.get("crypto_sensitive_not_sole_driver")
        if crypto_only:
            return "CRYPTO_SENSITIVE_EQUITY_EFFECT_ONLY"
    return "STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_REJECTED_AFTER_METHOD_FIX"


def _summary_breakdown_rows(
    features: Sequence[Mapping[str, Any]],
    scopes: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scope in scopes:
        for group in ("W", "O"):
            blocks = _calendar_blocks(features, group=group, scope=scope)
            row = {
                "scope": scope,
                "month": "ALL_DEVELOPMENT",
                "group": group,
                "seed": "",
                "calendar_key": "",
                "window_type": "",
                "listing_stage": "",
                "block_count": len(blocks),
                "symbol_count": len(_scope_symbols(scope)),
            }
            row.update({metric: _block_median(blocks, metric) for metric in BLOCK_METRICS})
            rows.append(row)
        for seed in SEED_VALUES:
            blocks = _calendar_blocks(
                features, group="R", scope=scope, seed=seed
            )
            row = {
                "scope": scope,
                "month": "ALL_DEVELOPMENT",
                "group": "R",
                "seed": seed,
                "calendar_key": "",
                "window_type": "",
                "listing_stage": "",
                "block_count": len(blocks),
                "symbol_count": len(_scope_symbols(scope)),
            }
            row.update({metric: _block_median(blocks, metric) for metric in BLOCK_METRICS})
            rows.append(row)
    return rows


def _month_breakdown_rows(features: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for month in DEVELOPMENT_MONTHS:
        for scope in ("ALL_CORE", "TRADITIONAL_EQUITY", "CRYPTO_SENSITIVE_EQUITY"):
            for group in ("W", "O"):
                blocks = _calendar_blocks(
                    features, group=group, scope=scope, month=month
                )
                row = {
                    "scope": scope,
                    "month": month,
                    "group": group,
                    "seed": "",
                    "calendar_key": "",
                    "window_type": "",
                    "listing_stage": "",
                    "block_count": len(blocks),
                    "symbol_count": len(_scope_symbols(scope)),
                }
                row.update(
                    {metric: _block_median(blocks, metric) for metric in BLOCK_METRICS}
                )
                rows.append(row)
            for seed in SEED_VALUES:
                blocks = _calendar_blocks(
                    features, group="R", scope=scope, seed=seed, month=month
                )
                row = {
                    "scope": scope,
                    "month": month,
                    "group": "R",
                    "seed": seed,
                    "calendar_key": "",
                    "window_type": "",
                    "listing_stage": "",
                    "block_count": len(blocks),
                    "symbol_count": len(_scope_symbols(scope)),
                }
                row.update(
                    {metric: _block_median(blocks, metric) for metric in BLOCK_METRICS}
                )
                rows.append(row)
    return rows


def _calendar_breakdown_rows(features: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scope in _all_scopes():
        for group in ("W", "O", "R"):
            rows.extend(_calendar_blocks(features, group=group, scope=scope))
    return rows


def _validation_rows(features: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "window_id": row.get("window_id"),
            "symbol": row.get("symbol"),
            "asset_group": row.get("asset_group"),
            "group": row.get("group"),
            "seed": row.get("seed"),
            "calendar_key": row.get("calendar_key"),
            "month": row.get("month"),
            "sample_status": row.get("split"),
            "diagnostic_role": "EXPOSED_DIRECTIONAL_DIAGNOSTIC_ONLY",
            "hourly_realized_volatility": row.get("hourly_realized_volatility"),
            "directional_efficiency": row.get("directional_efficiency"),
            "fee_adjusted_cycle_capacity_per_hour": row.get(
                "fee_adjusted_cycle_capacity_per_hour"
            ),
            "zero_trade_ratio": row.get("zero_trade_ratio"),
            "trades_per_hour": row.get("trades_per_hour"),
        }
        for row in features
        if row.get("split") == "RESEARCH_VALIDATION_EXPOSED"
    ]


def _write_h2_not_run(output_dir: Path, conclusion: str) -> dict[str, str]:
    reason = f"H1_1_CONCLUSION={conclusion}"
    hashes = {
        "baseline-comparison-h2-1.csv": write_csv(
            output_dir / "baseline-comparison-h2-1.csv",
            ("baseline", "status", "reason"),
            [
                {"baseline": f"B{index}", "status": NOT_RUN_STATUS, "reason": reason}
                for index in range(6)
            ],
        ),
        "execution-stress-h2-1.csv": write_csv(
            output_dir / "execution-stress-h2-1.csv",
            ("scenario", "status", "reason"),
            [
                {"scenario": name, "status": NOT_RUN_STATUS, "reason": reason}
                for name in ("BASE", "COST50", "EXECUTION_STRESS")
            ],
        ),
        "portfolio-summary-h2-1.csv": write_csv(
            output_dir / "portfolio-summary-h2-1.csv",
            ("status", "reason"),
            [{"status": NOT_RUN_STATUS, "reason": reason}],
        ),
    }
    hashes["baseline-comparison-h2-1.md"] = immutable_write(
        output_dir / "baseline-comparison-h2-1.md",
        "\n".join(
            [
                "# H2.1 fixed baseline",
                "",
                f"Status: `{NOT_RUN_STATUS}`",
                "",
                f"Reason: `{reason}`",
                "",
                "B0-B5 and all cost/execution scenarios were not run.",
                "",
            ]
        ),
    )
    return hashes


def _report(payload: Mapping[str, Any]) -> str:
    technical = payload["h1_1"]["technical_gate"]
    lines = [
        "# Stock perpetual weekend H1.1",
        "",
        f"Conclusion: `{payload['conclusion']}`",
        "",
        f"- Technical gate: `{'PASS' if technical['passed'] else 'FAIL'}`",
        f"- Development blocks: `{technical['development_calendar_block_counts']}`",
        f"- R blocks by seed: `{technical['development_r_blocks_by_seed']}`",
        f"- O before/after R: `{payload['window_counts']['o_count_before_random']}` / `{payload['window_counts']['o_count_after_random']}`",
        f"- Forward OOS read: `{payload['future_forward_oos_read']}`",
        f"- B0-B5 run: `{payload['b0_b5_run']}`",
        "",
        "## Technical checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, passed in technical["checks"].items():
        lines.append(f"| `{name}` | **{'PASS' if passed else 'FAIL'}** |")
    lines.extend(
        [
            "",
            f"Failed checks: `{', '.join(technical['failed_checks']) or 'none'}`",
            "",
            "All duration-dependent primary metrics use their per-hour fields. The window-level values remain diagnostic only.",
            "",
            "June is labeled `RESEARCH_VALIDATION_EXPOSED`; it is not claimed as sealed OOS.",
            "",
        ]
    )
    if not technical["passed"]:
        lines.append("Economic gates and bootstrap were not evaluated after the technical gate failed.")
        lines.append("")
    return "\n".join(lines)


def _sha256_if_exists(path: str | Path | None) -> str | None:
    if not path:
        return None
    value = Path(path)
    if not value.exists():
        return None
    return hashlib.sha256(value.read_bytes()).hexdigest()


def _git_lines(*arguments: str) -> list[str]:
    result = subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _registered_gates() -> dict[str, Any]:
    return {
        "technical": {
            "tier_a_core_audits": "8 PASS",
            "input_hashes": "all match",
            "w_o_r_overlap": 0,
            "o_count_before_equals_after": True,
            "development_w_blocks_min": 12,
            "development_o_blocks_min": 30,
            "development_r_blocks_per_seed_min": 10,
            "development_months_min": 3,
            "traditional_symbols_complete_min": 3,
            "per_hour_fields_required": list(PER_HOUR_FIELDS),
            "june_status": "RESEARCH_VALIDATION_EXPOSED",
            "future_forward_oos_read": False,
            "full_pytest_exit_code": 0,
        },
        "w_vs_o": {
            "hourly_realized_volatility": "W <= O * 0.90",
            "directional_efficiency": "W <= O",
            "completed_grid_cycles_per_hour": "W >= O",
            "fee_adjusted_cycle_capacity_per_hour": "W >= O and W > 0",
            "zero_trade_ratio": "W <= O * 1.25 + 0.01",
            "trades_per_hour": "W >= O * 0.75",
            "capacity_bootstrap_support_probability": ">= 0.95",
        },
        "w_vs_r": {
            "hourly_realized_volatility": "W <= R * 0.90",
            "directional_efficiency": "W <= R",
            "completed_grid_cycles_per_hour": "W >= R",
            "fee_adjusted_cycle_capacity_per_hour": "W >= R",
            "zero_trade_ratio": "W <= R * 1.25 + 0.01",
            "trades_per_hour": "W >= R * 0.75",
            "capacity_bootstrap_support_probability": ">= 0.95",
        },
        "stability": {
            "development_month_advantage_ratio": ">= 0.60",
            "traditional_group_direction": "pass W vs O and W vs R path gates",
            "traditional_symbols_with_capacity_advantage_min": 2,
            "crypto_sensitive_not_sole_driver": True,
            "after_listing_day_30_direction_not_reversed": True,
            "regular_and_long_weekends_reported": True,
            "best_w_block_positive_advantage_contribution": "<= 0.35",
            "not_dependent_on_one_symbol": True,
            "r_seed_direction_consistency": ">= 5/6",
            "no_block_duration_liquidity_anomaly_explains_advantage": True,
        },
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    if git_branch() != "codex/profit-protection-backtest-v2.3":
        raise ValueError("H1.1 must run on codex/profit-protection-backtest-v2.3")
    discovery_path = Path(args.discovery)
    data_manifest_path = Path(args.data_manifest)
    audit_path = Path(args.audit_json)
    window_path = Path(args.window_manifest)
    input_hash_path = Path(args.input_hash_manifest)
    protocol_path = Path(args.protocol)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    data_manifest = json.loads(data_manifest_path.read_text(encoding="utf-8"))
    audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
    window_payload = json.loads(window_path.read_text(encoding="utf-8"))
    input_hash_payload = json.loads(input_hash_path.read_text(encoding="utf-8"))
    if tuple(window_payload.get("random_seeds") or ()) != SEED_VALUES:
        raise ValueError("H1.1 window seeds do not match the preregistration")
    if window_payload.get("direction_mode") != "NEUTRAL" or window_payload.get(
        "leverage"
    ) != 1:
        raise ValueError("H1.1 windows did not preserve NEUTRAL / 1x")
    if not input_hash_payload.get("passed"):
        raise ValueError("H1.1 input hash audit failed")

    ready_windows = [
        row
        for row in window_payload.get("windows") or []
        if row.get("status") == "READY"
        and row.get("split") != "FORWARD_OOS_FUTURE"
    ]
    windows_by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in ready_windows:
        windows_by_symbol[str(row["symbol"])].append(row)

    features: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for symbol in CORE_SYMBOLS:
        item = data_manifest["assets"][symbol]
        klines = v2_5_h1._read_klines(item["files"]["klines"]["path"])
        funding = v2_5_h1._read_funding(item["files"]["funding"]["path"])
        mark = v2_5_h1._read_side_map(item["files"]["mark_price"]["path"])
        premium = v2_5_h1._read_side_map(
            item["files"]["premium_index"]["path"], signed=True
        )
        agg_meta = item["files"].get("agg_trades") or {}
        agg_trades = (
            AggTradeSeries.load(
                agg_meta["path"], expected_rows=int(agg_meta["row_count"])
            )
            if agg_meta.get("path")
            else None
        )
        for window in sorted(
            windows_by_symbol[symbol],
            key=lambda row: (str(row.get("market_close")), str(row.get("window_id"))),
        ):
            features.append(
                _feature_row(window, klines, funding, mark, premium, agg_trades)
            )
            diagnostics.extend(
                _diagnostic_rows(
                    window, klines, funding, mark, premium, agg_trades
                )
            )
        del agg_trades, klines, funding, mark, premium
        gc.collect()

    technical = _technical_checks(
        window_payload=window_payload,
        input_hash_payload=input_hash_payload,
        audit_payload=audit_payload,
        features=features,
        pytest_exit_code=args.pytest_exit_code,
    )
    evaluation_status = (
        "EVALUATED" if technical["passed"] else "NOT_EVALUATED_TECHNICAL_GATE_FAILED"
    )
    w_vs_o_rows = _comparison_rows(
        features, control_group="O", evaluation_status=evaluation_status
    )
    w_vs_r_rows = _comparison_rows(
        features, control_group="R", evaluation_status=evaluation_status
    )
    if technical["passed"]:
        bootstrap_rows = _bootstrap_rows(
            features, reps=max(BOOTSTRAP_REPS, int(args.bootstrap_reps))
        )
        economic = _economic_checks(features, bootstrap_rows)
        conclusion = _conclusion(economic)
    else:
        bootstrap_rows = [
            {
                "comparison": "NOT_RUN",
                "scope": "ALL_CORE",
                "seed": "",
                "metric": "",
                "status": "NOT_RUN_TECHNICAL_GATE_FAILED",
            }
        ]
        economic = {
            "status": "NOT_EVALUATED_TECHNICAL_GATE_FAILED",
            "passed": False,
        }
        conclusion = TECHNICAL_INVALID

    asset_group_rows = _summary_breakdown_rows(
        features,
        ("ALL_CORE", "TRADITIONAL_EQUITY", "CRYPTO_SENSITIVE_EQUITY"),
    )
    symbol_rows = _summary_breakdown_rows(
        features, tuple(f"SYMBOL:{symbol}" for symbol in CORE_SYMBOLS)
    )
    month_rows = _month_breakdown_rows(features)
    calendar_rows = _calendar_breakdown_rows(features)
    validation_rows = _validation_rows(features)

    artifact_hashes = {
        "market-features-hourly.csv": write_csv(
            output_dir / "market-features-hourly.csv", FEATURE_FIELDS, features
        ),
        "w-vs-o-summary.csv": write_csv(
            output_dir / "w-vs-o-summary.csv", COMPARISON_FIELDS, w_vs_o_rows
        ),
        "w-vs-r-summary.csv": write_csv(
            output_dir / "w-vs-r-summary.csv", COMPARISON_FIELDS, w_vs_r_rows
        ),
        "asset-group-breakdown.csv": write_csv(
            output_dir / "asset-group-breakdown.csv", BREAKDOWN_FIELDS, asset_group_rows
        ),
        "symbol-breakdown.csv": write_csv(
            output_dir / "symbol-breakdown.csv", BREAKDOWN_FIELDS, symbol_rows
        ),
        "month-breakdown.csv": write_csv(
            output_dir / "month-breakdown.csv", BREAKDOWN_FIELDS, month_rows
        ),
        "calendar-block-breakdown.csv": write_csv(
            output_dir / "calendar-block-breakdown.csv", BREAKDOWN_FIELDS, calendar_rows
        ),
        "bootstrap-results.csv": write_csv(
            output_dir / "bootstrap-results.csv", BOOTSTRAP_FIELDS, bootstrap_rows
        ),
        "matched-10h-diagnostics.csv": write_csv(
            output_dir / "matched-10h-diagnostics.csv", DIAGNOSTIC_FIELDS, diagnostics
        ),
        "research-validation-exposed.csv": write_csv(
            output_dir / "research-validation-exposed.csv",
            (
                "window_id",
                "symbol",
                "asset_group",
                "group",
                "seed",
                "calendar_key",
                "month",
                "sample_status",
                "diagnostic_role",
                "hourly_realized_volatility",
                "directional_efficiency",
                "fee_adjusted_cycle_capacity_per_hour",
                "zero_trade_ratio",
                "trades_per_hour",
            ),
            validation_rows,
        ),
    }
    downstream_hashes = (
        _write_h2_not_run(output_dir, conclusion)
        if conclusion != SUPPORTED
        else {}
    )
    implementation_commit = git_commit()
    implementation_paths = _git_lines(
        "show", "--format=", "--name-only", implementation_commit
    )
    production_default_paths = [
        path
        for path in implementation_paths
        if path == "config" or path.startswith("config/")
    ]
    master_commit = _git_lines("rev-parse", "master")[0]
    payload = {
        "schema_version": 2,
        "protocol": str(protocol_path),
        "generated_at": pd.Timestamp.now(tz="UTC").isoformat(),
        "git_branch": git_branch(),
        "git_commit": implementation_commit,
        "master_commit": master_commit,
        "implementation_changed_paths": implementation_paths,
        "input_files_sha256": input_hash_payload.get("data_files"),
        "input_hash_manifest_sha256": hashlib.sha256(
            input_hash_path.read_bytes()
        ).hexdigest(),
        "v2_5_window_manifest_sha256": window_payload.get(
            "source_v2_5_manifest_sha256"
        ),
        "h1_1_window_manifest_sha256": hashlib.sha256(
            window_path.read_bytes()
        ).hexdigest(),
        "data_previously_viewed": True,
        "sample_reclassification": window_payload.get("sample_status"),
        "window_counts": {
            "calendar_blocks_by_split": window_payload.get("count_audit", {}).get(
                "calendar_block_counts_by_split"
            ),
            "o_count_before_random": window_payload.get("o_count_before_random"),
            "o_count_after_random": window_payload.get("o_count_after_random"),
            "r_development_by_seed": window_payload.get("count_audit", {}).get(
                "development_r_blocks_by_seed"
            ),
        },
        "window_overlap_audit": window_payload.get("overlap_audit"),
        "asset_groups": window_payload.get("asset_groups"),
        "formulas": {
            "hours": "tradable_rows / 60",
            "window_realized_volatility": "sqrt(sum(log_return_i^2))",
            "hourly_realized_volatility": "sqrt(sum(log_return_i^2) / hours)",
            "completed_grid_cycles_per_hour": "completed_grid_cycles / hours",
            "gross_cycle_edge": "max(step_pct - 2 * maker_fee, 0)",
            "fee_adjusted_cycle_capacity_window": "completed_grid_cycles * gross_cycle_edge",
            "fee_adjusted_cycle_capacity_per_hour": "completed_grid_cycles_per_hour * gross_cycle_edge",
            "median_trade_size": "median(agg_trade_price * agg_trade_quantity), quote notional",
        },
        "configuration": {
            "step_pct": STEP_PCT,
            "maker_fee": MAKER_FEE,
            "bootstrap_reps": max(BOOTSTRAP_REPS, int(args.bootstrap_reps)),
            "bootstrap_seed": BOOTSTRAP_SEED,
            "direction_mode": "NEUTRAL",
            "leverage": 1,
        },
        "registered_gates": _registered_gates(),
        "feature_count": len(features),
        "exposed_validation_feature_count": len(validation_rows),
        "h1_1": {
            "technical_gate": technical,
            "economic_gate": economic,
            "w_vs_o_summary": w_vs_o_rows,
            "w_vs_r_summary": w_vs_r_rows,
            "bootstrap_results": bootstrap_rows,
        },
        "conclusion": conclusion,
        "h2_1_allowed": conclusion == SUPPORTED,
        "h2_1_status": (
            "AUTHORIZED_NOT_RUN_IN_H1_1_TASK"
            if conclusion == SUPPORTED
            else NOT_RUN_STATUS
        ),
        "b0_b5_run": False,
        "base_cost50_execution_stress_run": False,
        "parameter_search_opened": False,
        "research_validation_exposed_read": bool(validation_rows),
        "future_forward_oos_read": False,
        "production_defaults_changed": bool(production_default_paths),
        "production_default_paths_changed": production_default_paths,
        "master_modified": False,
        "real_money_authorized": False,
        "pytest": {
            "command": args.pytest_command,
            "exit_code": args.pytest_exit_code,
            "summary": args.pytest_summary,
            "stdout_log": args.pytest_stdout_log,
            "stdout_sha256": _sha256_if_exists(args.pytest_stdout_log),
            "stderr_log": args.pytest_stderr_log,
            "stderr_sha256": _sha256_if_exists(args.pytest_stderr_log),
        },
        "artifact_sha256": artifact_hashes | downstream_hashes,
        "breakdown_artifacts": {
            name: artifact_hashes[name]
            for name in (
                "asset-group-breakdown.csv",
                "symbol-breakdown.csv",
                "month-breakdown.csv",
                "calendar-block-breakdown.csv",
                "matched-10h-diagnostics.csv",
            )
        },
    }
    write_json(output_dir / "market-hypothesis-h1-1.json", payload)
    write_json(output_dir / "results.json", payload)
    report = _report(payload)
    immutable_write(output_dir / "market-hypothesis-h1-1.md", report)
    immutable_write(output_dir / "final-report.md", report)
    return payload


def main() -> None:
    args = _parser().parse_args()
    result = run(args)
    print(
        json.dumps(
            {
                "conclusion": result["conclusion"],
                "technical_gate_passed": result["h1_1"]["technical_gate"][
                    "passed"
                ],
                "failed_technical_checks": result["h1_1"]["technical_gate"][
                    "failed_checks"
                ],
                "feature_count": result["feature_count"],
                "b0_b5_run": result["b0_b5_run"],
                "future_forward_oos_read": result["future_forward_oos_read"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
