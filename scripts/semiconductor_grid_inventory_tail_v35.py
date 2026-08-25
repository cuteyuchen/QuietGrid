"""QuietGrid v3.5 inventory-tail formation and early-warning study.

This module is a read-only research replay over the frozen v3.4 event set.  It
uses the 31111-NEUTRAL CONTROL path as the canonical formation path and never
changes orders, production configuration, the v2.9 candidate, or its ledger.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import semiconductor_grid_breakout_inventory_protection_v32 as v32  # noqa: E402
from scripts import semiconductor_grid_staged_derisking_v34 as v34  # noqa: E402
from scripts.semiconductor_grid_oos_diagnostics_v292 import _path_rows  # noqa: E402


BASE_COMMIT = "32df63f7a843bdf0982d580f427026f416d70118"
BRANCH = "codex/semiconductor-grid-inventory-tail-early-warning-v3.5"
OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-inventory-tail-v3.5"
V34_OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-staged-derisking-v3.4"
PRIMARY_SCENARIO = "PRIMARY_ZERO_MAKER"
PRIMARY_SEED = "3"
CHECKPOINTS = (-60, -30, -20, -15, -10, -5, 0)
CHECKPOINT_LABELS = {value: "T0" if value == 0 else f"T{value}" for value in CHECKPOINTS}
EVENT_PATH_FIELDS = (
    "timestamp",
    "event_relative_minute",
    "event_id",
    "symbol",
    "window_key",
    "canonical_path",
    "path_state_source",
    "price",
    "net_inventory",
    "gross_inventory",
    "gross_inventory_notional",
    "inventory_utilization",
    "inventory_age",
    "unrealized_inventory_pnl",
    "paired_grid_pnl",
    "fills",
    "fill_rate",
    "one_sided_fill_ratio",
    "reversal_ratio",
    "ATR_pct",
    "realized_vol",
    "price_slope",
    "grid_headroom_remaining",
    "minutes_to_force_close",
)
LABEL_FIELDS = {
    "truth_label",
    "future_tail_loss",
    "future_MAE",
    "future_MFE",
    "event_end_time",
    "tail_event_time",
    "force_close_time",
    "time_to_force_close",
    "posthoc_label",
}

TRUTH_DEFINITION = {
    "source": "v3.4 frozen D2 event set; verbatim v3.3/v3.2 posthoc truth definition",
    "causal_signal_use": False,
    "labels": {
        "TRUE_BREAKOUT": "After the D2 signal, all available forward closes in the registered evaluation path remain outside the grid for the required sustained path.",
        "FALSE_BREAKOUT": "After the D2 signal, at least one future close returns inside the grid range.",
        "UNRESOLVED": "No sufficient future evaluation path exists or neither registered label condition is met.",
    },
    "implementation": "scripts.semiconductor_grid_breakout_inventory_protection_v32._posthoc_label",
    "label_fields_are": "LABEL_ONLY",
}

# These are deliberately few, round-number, mechanism-led rules.  They are
# frozen here before local robustness; the study does not search thresholds.
EARLY_WARNING_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "EW1_BUILD_REVERSAL",
        "description": "inventory_build_rate_utilization_20m >= 0.002 AND reversal_ratio_20m <= 0.25",
        "features": ("inventory_build_rate_utilization_20m", "reversal_ratio_20m"),
        "thresholds": (0.002, 0.25),
        "predicate": lambda row: _f(row.get("inventory_build_rate_utilization_20m")) >= 0.002
        and _f(row.get("reversal_ratio_20m"), 1.0) <= 0.25,
    },
    {
        "rule_id": "EW2_FILL_HEADROOM",
        "description": "one_sided_fill_ratio_10m >= 0.80 AND grid_headroom_remaining <= 2",
        "features": ("one_sided_fill_ratio_10m", "grid_headroom_remaining"),
        "thresholds": (0.80, 2.0),
        "predicate": lambda row: _f(row.get("one_sided_fill_ratio_10m")) >= 0.80
        and _f(row.get("grid_headroom_remaining"), 99.0) <= 2.0,
    },
    {
        "rule_id": "EW3_FILL_REVERSAL",
        "description": "one_sided_fill_ratio_10m >= 0.80 AND reversal_ratio_20m <= 0.25",
        "features": ("one_sided_fill_ratio_10m", "reversal_ratio_20m"),
        "thresholds": (0.80, 0.25),
        "predicate": lambda row: _f(row.get("one_sided_fill_ratio_10m")) >= 0.80
        and _f(row.get("reversal_ratio_20m"), 1.0) <= 0.25,
    },
    {
        "rule_id": "EW4_UTILIZATION_HEADROOM",
        "description": "gross_inventory_utilization >= 0.40 AND grid_headroom_remaining <= 2",
        "features": ("gross_inventory_utilization", "grid_headroom_remaining"),
        "thresholds": (0.40, 2.0),
        "predicate": lambda row: _f(row.get("gross_inventory_utilization")) >= 0.40
        and _f(row.get("grid_headroom_remaining"), 99.0) <= 2.0,
    },
)

INTERACTIONS: tuple[tuple[str, str, str, Callable[[Mapping[str, Any]], bool]], ...] = (
    ("inventory_build_rate_utilization_20m", "reversal_ratio_20m", "build >= 0.002 AND reversal <= 0.25", EARLY_WARNING_RULES[0]["predicate"]),
    ("inventory_build_rate_utilization_20m", "directional_efficiency_ratio_20m", "build >= 0.002 AND efficiency >= 0.60", lambda r: _f(r.get("inventory_build_rate_utilization_20m")) >= 0.002 and _f(r.get("directional_efficiency_ratio_20m")) >= 0.60),
    ("gross_inventory_utilization", "grid_headroom_remaining", "utilization >= 0.40 AND headroom <= 2", EARLY_WARNING_RULES[3]["predicate"]),
    ("one_sided_fill_ratio_10m", "volatility_ratio_short_long", "one-sided >= 0.80 AND vol ratio >= 1.25", lambda r: _f(r.get("one_sided_fill_ratio_10m")) >= 0.80 and _f(r.get("volatility_ratio_short_long")) >= 1.25),
    ("one_sided_fill_ratio_10m", "reversal_ratio_20m", "one-sided >= 0.80 AND reversal <= 0.25", EARLY_WARNING_RULES[2]["predicate"]),
    ("inventory_age_max", "minutes_to_force_close", "age >= 360 AND time to close <= 360", lambda r: _f(r.get("inventory_age_max")) >= 360 and _f(r.get("minutes_to_force_close"), 9999) <= 360),
    ("directional_efficiency_ratio_20m", "minutes_to_force_close", "efficiency >= 0.60 AND time to close <= 360", lambda r: _f(r.get("directional_efficiency_ratio_20m")) >= 0.60 and _f(r.get("minutes_to_force_close"), 9999) <= 360),
    ("inventory_pnl_unrealized", "reversal_ratio_20m", "inventory pnl < 0 AND reversal <= 0.25", lambda r: _f(r.get("inventory_pnl_unrealized")) < 0 and _f(r.get("reversal_ratio_20m"), 1.0) <= 0.25),
)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif str(value).strip().isdigit():
        number = int(str(value).strip())
        parsed = datetime.fromtimestamp(number / 1000.0 if number > 10**12 else number, UTC)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str:
    return _dt(value).isoformat()


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(fields or sorted({key for row in rows for key in row}))
    if not names:
        names = ["status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def deduplicate_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate scenario/seed replicas without changing v3.4 event identity."""
    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[v34.unique_market_event_key(event)].append(event)
    unique: list[dict[str, Any]] = []
    for key in sorted(grouped):
        members = grouped[key]
        primary = next(
            (
                row
                for row in members
                if str(row.get("scenario")) == PRIMARY_SCENARIO and str(row.get("seed")) == PRIMARY_SEED
            ),
            members[0],
        )
        item = dict(primary)
        item["event_id"] = v34.event_cluster_id(primary)
        item["run_level_duplicate_count"] = len(members)
        item["scenario_count"] = len({str(row.get("scenario")) for row in members})
        item["seed_count"] = len({str(row.get("seed")) for row in members})
        unique.append(item)
    return unique


def validate_no_future_leakage(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    for index, row in enumerate(rows):
        feature_time = str(row.get("feature_timestamp") or row.get("timestamp") or "")
        observation_time = str(row.get("observation_timestamp") or feature_time)
        if feature_time and observation_time and _dt(feature_time) > _dt(observation_time):
            errors.append(f"row {index}: feature timestamp exceeds observation timestamp")
        predictor_fields = set(str(row.get("predictor_fields") or "").split("|")) - {""}
        leaked = sorted(predictor_fields & LABEL_FIELDS)
        if leaked:
            errors.append(f"row {index}: LABEL_ONLY fields in predictor matrix: {','.join(leaked)}")
    return errors


def lead_time_minutes(warning_time: str, reference_time: str) -> float | None:
    if not warning_time or not reference_time:
        return None
    return (_dt(reference_time) - _dt(warning_time)).total_seconds() / 60.0


def _event_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("window_key", "")),
        str(row.get("symbol", "")),
        str(row.get("scenario", "")),
        str(row.get("seed", "")),
    )


def _source_rows() -> dict[tuple[str, str, str, str], dict[str, Any]]:
    historical, current = v32._control_rows()
    return {_event_key(row): dict(row) for row in [*historical, *current]}


def _fallback_control_replay(
    trade: Sequence[Mapping[str, Any]], source: Mapping[str, Any]
) -> tuple[Any, Any]:
    """Rebuild the frozen v3.2 aggregate CONTROL fallback without future inputs.

    One registered event had no admitted fill-level replay in v3.2.  Its D2
    event was formed on ``_path_for_detector`` using constant end inventory and
    a conservative +/-1% grid.  Reproduce that exact path family explicitly so
    the event is retained without pretending that fill history exists.
    """
    center = _f(trade[0].get("close"))
    lower = center * 0.99
    upper = center * 1.01
    grid_num = max(1, int(_f(source.get("grid_num"), 3)))
    prices = tuple(lower + (upper - lower) * index / grid_num for index in range(grid_num + 1))
    qty = _f(source.get("pre_exit_position_qty"))
    final_price = _f(source.get("pre_exit_mark_price"), _f(trade[-1].get("close")))
    final_unrealized = _f(source.get("pre_exit_unrealized_pnl"))
    entry = final_price - final_unrealized / qty if abs(qty) > 1e-12 else center
    fills = []
    if abs(qty) > 1e-12:
        fills.append(
            SimpleNamespace(
                side="BUY" if qty > 0 else "SELL",
                position_side="LONG" if qty > 0 else "SHORT",
                order_intent="SEED",
                price=entry,
                qty=abs(qty),
                fee=0.0,
                grid_pnl=None,
                bar_index=0,
                grid_index=grid_num // 2,
                timestamp=trade[0]["close_time"],
            )
        )
    equity = []
    for index, bar in enumerate(trade):
        price = _f(bar.get("close"))
        unrealized = (price - entry) * qty
        equity.append(
            SimpleNamespace(
                bar_index=index,
                equity=unrealized,
                realized_pnl=0.0,
                unrealized_pnl=unrealized,
                drawdown=max(0.0, -unrealized),
                close=price,
                timestamp=bar["close_time"],
                gross_inventory_notional=abs(qty) * price,
                inventory_utilization=abs(qty) * price / 500.0,
            )
        )
    result = SimpleNamespace(
        fills=fills,
        equity_curve=equity,
        stopped_at_index=None,
        stopped_reason=None,
        force_close_count=int(_f(source.get("force_close_count"))),
    )
    candidate = SimpleNamespace(
        params=SimpleNamespace(
            center=center,
            lower=lower,
            upper=upper,
            step_pct=0.001,
            grid_num=grid_num,
            prices=prices,
        )
    )
    return result, candidate


def reproduce_v34_event_parity() -> dict[str, Any]:
    events, source_rows = v34._profile_reference_rows()
    rows, _, _ = v34._run_rows(events, source_rows)
    control = v34._control_parity(rows)
    v33 = v34._v33_parity(rows)
    unique = deduplicate_events(events)
    labels = Counter(str(row.get("posthoc_label")) for row in unique)
    observed = {
        "TP": labels.get("TRUE_BREAKOUT", 0),
        "FP": labels.get("FALSE_BREAKOUT", 0),
        "FN": 0,
        "TN": 0,
    }
    expected = {"TP": 2, "FP": 10, "FN": 0, "TN": 0}
    run_labels = Counter(str(row.get("posthoc_label")) for row in events)
    observed_run = {
        "TP": run_labels.get("TRUE_BREAKOUT", 0),
        "FP": run_labels.get("FALSE_BREAKOUT", 0),
        "FN": 0,
        "TN": 0,
    }
    expected_run = {"TP": 36, "FP": 180, "FN": 0, "TN": 0}
    status = (
        "PASS_V34_EVENT_PARITY"
        if control.get("status") == "PASS_CONTROL_PARITY"
        and v33.get("status") == "PASS_V33_PARITY"
        and observed == expected
        and observed_run == expected_run
        else "FAIL_V34_EVENT_PARITY"
    )
    return {
        "status": status,
        "control_parity": control,
        "v33_parity": v33,
        "expected_unique_event_confusion": expected,
        "observed_unique_event_confusion": observed,
        "expected_run_level_confusion": expected_run,
        "observed_run_level_confusion": observed_run,
        "unique_event_count": len(unique),
        "truth_definition_hash": _json_hash(TRUTH_DEFINITION),
    }


def _apply_fill(lots: list[dict[str, Any]], fill: Any) -> tuple[float, bool]:
    intent = str(fill.order_intent or "OPEN").upper()
    if intent in {"OPEN", "SEED"}:
        lots.append(
            {
                "entry_price": float(fill.price),
                "qty": float(fill.qty),
                "opened_bar": int(fill.bar_index),
                "grid_index": int(fill.grid_index),
            }
        )
        return 0.0, False
    remaining = float(fill.qty)
    entry = None
    paired = fill.grid_pnl is not None
    if paired and remaining > 0:
        entry = (
            float(fill.price) - float(fill.grid_pnl) / remaining
            if str(fill.side).upper() == "SELL"
            else float(fill.price) + float(fill.grid_pnl) / remaining
        )
    for lot in list(lots):
        if remaining <= 1e-12:
            break
        if entry is not None and abs(float(lot["entry_price"]) - entry) > 1e-8:
            continue
        take = min(float(lot["qty"]), remaining)
        lot["qty"] -= take
        remaining -= take
        if lot["qty"] <= 1e-12:
            lots.remove(lot)
    if remaining > 1e-12:
        for lot in list(lots):
            take = min(float(lot["qty"]), remaining)
            lot["qty"] -= take
            remaining -= take
            if lot["qty"] <= 1e-12:
                lots.remove(lot)
            if remaining <= 1e-12:
                break
    return float(fill.grid_pnl or 0.0), paired


def _weighted_vwap(lots: Sequence[Mapping[str, Any]]) -> float:
    qty = sum(_f(row.get("qty")) for row in lots)
    return sum(_f(row.get("entry_price")) * _f(row.get("qty")) for row in lots) / qty if qty else 0.0


def _window(rows: Sequence[Mapping[str, Any]], index: int, minutes: int) -> list[Mapping[str, Any]]:
    return list(rows[max(0, index - minutes + 1) : index + 1])


def _returns(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    prices = [_f(row.get("price")) for row in rows]
    return [prices[i] / prices[i - 1] - 1.0 for i in range(1, len(prices)) if prices[i - 1] > 0]


def _slope(rows: Sequence[Mapping[str, Any]]) -> float:
    values = [_f(row.get("price")) for row in rows]
    if len(values) < 2:
        return 0.0
    x_mean = (len(values) - 1) / 2.0
    y_mean = statistics.fmean(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    return sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values)) / denominator if denominator else 0.0


def _reversal_metrics(rows: Sequence[Mapping[str, Any]]) -> tuple[int, float, int, int]:
    prices = [_f(row.get("price")) for row in rows]
    signs = [1 if right > left else -1 if right < left else 0 for left, right in zip(prices, prices[1:])]
    nonzero = [value for value in signs if value]
    reversals = sum(1 for left, right in zip(nonzero, nonzero[1:]) if left != right)
    ratio = reversals / max(1, len(nonzero) - 1)
    streak = 0
    maximum = 0
    previous = 0
    for sign in nonzero:
        streak = streak + 1 if sign == previous else 1
        maximum = max(maximum, streak)
        previous = sign
    return reversals, ratio, streak, maximum


def _atr(rows: Sequence[Mapping[str, Any]]) -> float:
    if not rows:
        return 0.0
    values: list[float] = []
    previous = _f(rows[0].get("price"))
    for row in rows:
        high = _f(row.get("high"), _f(row.get("price")))
        low = _f(row.get("low"), _f(row.get("price")))
        values.append(max(high - low, abs(high - previous), abs(low - previous)))
        previous = _f(row.get("price"))
    return statistics.fmean(values)


def _funding_schedule(now: datetime, end: datetime) -> tuple[int, float | None]:
    cursor = now.replace(minute=0, second=0, microsecond=0)
    while cursor <= now or cursor.hour not in {0, 8, 16}:
        cursor += timedelta(hours=1)
    events = 0
    probe = cursor
    while probe <= end:
        events += 1
        probe += timedelta(hours=8)
    return events, (cursor - now).total_seconds() / 60.0 if cursor <= end else None


def reconstruct_control_path(
    result: Any,
    candidate: Any,
    trade: Sequence[Mapping[str, Any]],
    event: Mapping[str, Any],
    source: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Reconstruct causal per-minute CONTROL inventory and market state."""
    fills_by_bar: dict[int, list[Any]] = defaultdict(list)
    for fill in sorted(result.fills, key=lambda item: (int(item.bar_index), _iso(item.timestamp))):
        fills_by_bar[int(fill.bar_index)].append(fill)
    long_lots: list[dict[str, Any]] = []
    short_lots: list[dict[str, Any]] = []
    paired_grid_pnl = 0.0
    cumulative_new = 0
    cumulative_paired = 0
    same_side_streak = 0
    same_side_streak_max = 0
    last_fill_side = ""
    rows: list[dict[str, Any]] = []
    params = candidate.params
    prices = list(getattr(params, "prices", ()) or ())
    step_abs = abs(float(params.center) * float(params.step_pct))
    force_close = _dt(source.get("force_close_at") or source.get("window_end") or trade[-1]["close_time"])
    trade_start = _dt(source.get("trade_start") or trade[0]["open_time"])
    direction = str(event.get("breakout_direction"))

    for point in sorted(result.equity_curve, key=lambda item: int(item.bar_index)):
        bar_index = int(point.bar_index)
        if bar_index < 0 or not trade:
            continue
        bar = trade[min(bar_index, len(trade) - 1)]
        minute_fills = fills_by_bar.get(bar_index, [])
        buy_fills = 0
        sell_fills = 0
        new_fills = 0
        paired_fills = 0
        for fill in minute_fills:
            side = str(fill.side).upper()
            buy_fills += side == "BUY"
            sell_fills += side == "SELL"
            if side == last_fill_side:
                same_side_streak += 1
            else:
                same_side_streak = 1
                last_fill_side = side
            same_side_streak_max = max(same_side_streak_max, same_side_streak)
            position_side = str(fill.position_side or ("LONG" if side == "BUY" else "SHORT")).upper()
            target = long_lots if position_side == "LONG" else short_lots
            pnl, paired = _apply_fill(target, fill)
            paired_grid_pnl += pnl
            if str(fill.order_intent).upper() in {"OPEN", "SEED"}:
                new_fills += 1
                cumulative_new += 1
            if paired:
                paired_fills += 1
                cumulative_paired += 1

        price = float(point.close)
        long_qty = sum(_f(lot.get("qty")) for lot in long_lots)
        short_qty = sum(_f(lot.get("qty")) for lot in short_lots)
        gross_qty = long_qty + short_qty
        net_qty = long_qty - short_qty
        lots = [*long_lots, *short_lots]
        ages = [max(0, bar_index - int(lot["opened_bar"])) for lot in lots]
        inventory_vwap = (
            _weighted_vwap(long_lots)
            if net_qty > 0
            else _weighted_vwap(short_lots)
            if net_qty < 0
            else 0.0
        )
        timestamp = _dt(point.timestamp)
        funding_remaining, next_funding = _funding_schedule(timestamp, force_close)
        if direction == "UP":
            adverse_distance = max(0.0, float(params.upper) - price)
        else:
            adverse_distance = max(0.0, price - float(params.lower))
        headroom = adverse_distance / step_abs if step_abs else 0.0
        nearest = min((abs(price - value) for value in prices), default=min(abs(price - float(params.lower)), abs(price - float(params.upper))))
        elapsed = max(0.0, (timestamp - trade_start).total_seconds() / 60.0)
        duration = max(1.0, (force_close - trade_start).total_seconds() / 60.0)
        row = {
            "timestamp": timestamp.isoformat(),
            "feature_timestamp": timestamp.isoformat(),
            "observation_timestamp": timestamp.isoformat(),
            "event_id": event["event_id"],
            "symbol": event["symbol"],
            "window_key": event["window_key"],
            "calendar": str(event.get("window_key", "")).split(":", 1)[0],
            "canonical_path": "31111-NEUTRAL CONTROL",
            "scenario": PRIMARY_SCENARIO,
            "seed": PRIMARY_SEED,
            "bar_index": bar_index,
            "price": price,
            "open": _f(bar.get("open"), price),
            "high": _f(bar.get("high"), price),
            "low": _f(bar.get("low"), price),
            "base_volume": _f(bar.get("volume")),
            "quote_volume": _f(bar.get("quote_volume")),
            "trade_count": _f(bar.get("trade_count")),
            "net_inventory": net_qty,
            "gross_inventory": gross_qty,
            "gross_long_notional": long_qty * price,
            "gross_short_notional": short_qty * price,
            "net_inventory_notional": net_qty * price,
            "gross_inventory_notional": gross_qty * price,
            "net_inventory_utilization": abs(net_qty) * price / 500.0,
            "gross_inventory_utilization": float(point.inventory_utilization),
            "inventory_utilization": float(point.inventory_utilization),
            "inventory_age_mean": statistics.fmean(ages) if ages else 0.0,
            "inventory_age_max": max(ages, default=0),
            "oldest_unpaired_lot_age": max(ages, default=0),
            "unpaired_lot_count": len(lots),
            "max_unpaired_lots": max(len(lots), max((int(old.get("unpaired_lot_count", 0)) for old in rows), default=0)),
            "inventory_entry_vwap": inventory_vwap,
            "distance_price_to_inventory_vwap": price - inventory_vwap if inventory_vwap else 0.0,
            "inventory_pnl_unrealized": float(point.unrealized_pnl),
            "paired_grid_pnl": paired_grid_pnl,
            "fills": len(minute_fills),
            "buy_fills": buy_fills,
            "sell_fills": sell_fills,
            "new_inventory_fills": new_fills,
            "paired_reversal_fills": paired_fills,
            "consecutive_same_side_fills": same_side_streak if minute_fills else 0,
            "same_side_fill_streak_max": same_side_streak_max,
            "cumulative_new_inventory_fills": cumulative_new,
            "cumulative_paired_reversal_fills": cumulative_paired,
            "current_grid_step": float(params.step_pct),
            "current_grid_count": int(params.grid_num),
            "distance_to_nearest_grid": nearest,
            "distance_to_upper_boundary": float(params.upper) - price,
            "distance_to_lower_boundary": price - float(params.lower),
            "occupied_grid_levels": len({int(lot["grid_index"]) for lot in lots}),
            "unpaired_grid_levels": len({int(lot["grid_index"]) for lot in lots}),
            "inventory_side_grid_depth": max(ages, default=0),
            "remaining_levels_in_adverse_direction": headroom,
            "grid_headroom_remaining": headroom,
            "minutes_since_window_start": elapsed,
            "minutes_to_force_close": max(0.0, (force_close - timestamp).total_seconds() / 60.0),
            "fraction_of_window_elapsed": min(1.0, elapsed / duration),
            "is_late_session": elapsed / duration >= 0.75,
            "funding_events_remaining": funding_remaining,
            "time_to_next_funding": next_funding if next_funding is not None else "",
            "predictor_fields": "",
            "path_state_source": "FILL_LEVEL_CONTROL_REPLAY",
        }
        rows.append(row)

    if rows and int(rows[-1]["bar_index"]) < len(trade) - 1:
        # v3.2's frozen detector continued over market bars after an early
        # CONTROL equity endpoint by clamping inventory/grid state to the last
        # replay point.  Preserve that causal monitor behavior explicitly.
        last_bar_index = int(rows[-1]["bar_index"])
        long_vwap = _weighted_vwap(long_lots)
        short_vwap = _weighted_vwap(short_lots)
        for bar_index in range(last_bar_index + 1, len(trade)):
            bar = trade[bar_index]
            price = _f(bar.get("close"))
            timestamp = _dt(bar["close_time"])
            long_qty = sum(_f(lot.get("qty")) for lot in long_lots)
            short_qty = sum(_f(lot.get("qty")) for lot in short_lots)
            gross_qty = long_qty + short_qty
            net_qty = long_qty - short_qty
            inventory_vwap = long_vwap if net_qty > 0 else short_vwap if net_qty < 0 else 0.0
            unrealized = (price - long_vwap) * long_qty + (short_vwap - price) * short_qty
            funding_remaining, next_funding = _funding_schedule(timestamp, force_close)
            adverse_distance = (
                max(0.0, float(params.upper) - price)
                if direction == "UP"
                else max(0.0, price - float(params.lower))
            )
            headroom = adverse_distance / step_abs if step_abs else 0.0
            nearest = min(
                (abs(price - value) for value in prices),
                default=min(abs(price - float(params.lower)), abs(price - float(params.upper))),
            )
            elapsed = max(0.0, (timestamp - trade_start).total_seconds() / 60.0)
            duration = max(1.0, (force_close - trade_start).total_seconds() / 60.0)
            ages = [max(0, bar_index - int(lot["opened_bar"])) for lot in [*long_lots, *short_lots]]
            rows.append(
                {
                    **rows[-1],
                    "timestamp": timestamp.isoformat(),
                    "feature_timestamp": timestamp.isoformat(),
                    "observation_timestamp": timestamp.isoformat(),
                    "bar_index": bar_index,
                    "price": price,
                    "open": _f(bar.get("open"), price),
                    "high": _f(bar.get("high"), price),
                    "low": _f(bar.get("low"), price),
                    "base_volume": _f(bar.get("volume")),
                    "quote_volume": _f(bar.get("quote_volume")),
                    "trade_count": _f(bar.get("trade_count")),
                    "net_inventory": net_qty,
                    "gross_inventory": gross_qty,
                    "gross_long_notional": long_qty * price,
                    "gross_short_notional": short_qty * price,
                    "net_inventory_notional": net_qty * price,
                    "gross_inventory_notional": gross_qty * price,
                    "net_inventory_utilization": abs(net_qty) * price / 500.0,
                    "gross_inventory_utilization": gross_qty * price / 500.0,
                    "inventory_utilization": gross_qty * price / 500.0,
                    "inventory_age_mean": statistics.fmean(ages) if ages else 0.0,
                    "inventory_age_max": max(ages, default=0),
                    "oldest_unpaired_lot_age": max(ages, default=0),
                    "inventory_entry_vwap": inventory_vwap,
                    "distance_price_to_inventory_vwap": price - inventory_vwap if inventory_vwap else 0.0,
                    "inventory_pnl_unrealized": unrealized,
                    "fills": 0,
                    "buy_fills": 0,
                    "sell_fills": 0,
                    "new_inventory_fills": 0,
                    "paired_reversal_fills": 0,
                    "consecutive_same_side_fills": 0,
                    "distance_to_nearest_grid": nearest,
                    "distance_to_upper_boundary": float(params.upper) - price,
                    "distance_to_lower_boundary": price - float(params.lower),
                    "remaining_levels_in_adverse_direction": headroom,
                    "grid_headroom_remaining": headroom,
                    "minutes_since_window_start": elapsed,
                    "minutes_to_force_close": max(0.0, (force_close - timestamp).total_seconds() / 60.0),
                    "fraction_of_window_elapsed": min(1.0, elapsed / duration),
                    "is_late_session": elapsed / duration >= 0.75,
                    "funding_events_remaining": funding_remaining,
                    "time_to_next_funding": next_funding if next_funding is not None else "",
                    "predictor_fields": "",
                    "path_state_source": "CLAMPED_POST_EQUITY_CONTROL_STATE",
                }
            )

    for index, row in enumerate(rows):
        for minutes in (1, 5, 10, 20, 30, 60):
            chunk = _window(rows, index, minutes + 1)
            returns = _returns(chunk)
            start_price = _f(chunk[0].get("price"), _f(row.get("price")))
            row[f"return_{minutes}m"] = _f(row.get("price")) / start_price - 1.0 if start_price else 0.0
            if minutes in {5, 10, 20, 30}:
                row[f"linear_price_slope_{minutes}m"] = _slope(chunk)
            if minutes in {5, 10, 20, 60}:
                row[f"realized_vol_{minutes}m"] = statistics.pstdev(returns) if len(returns) > 1 else 0.0
            if minutes in {5, 10, 20, 60}:
                prices_chunk = [_f(item.get("price")) for item in chunk]
                row[f"range_{minutes}m"] = max(prices_chunk) - min(prices_chunk) if prices_chunk else 0.0
            if minutes in {5, 10, 20, 30}:
                initial = _f(chunk[0].get("gross_inventory_notional"))
                row[f"inventory_build_rate_{minutes}m"] = (_f(row.get("gross_inventory_notional")) - initial) / max(1, minutes)
                initial_util = _f(chunk[0].get("gross_inventory_utilization"))
                row[f"inventory_build_rate_utilization_{minutes}m"] = (_f(row.get("gross_inventory_utilization")) - initial_util) / max(1, minutes)
            if minutes in {1, 5, 10, 20}:
                fill_count = sum(int(item.get("fills", 0)) for item in chunk)
                buys = sum(int(item.get("buy_fills", 0)) for item in chunk)
                sells = sum(int(item.get("sell_fills", 0)) for item in chunk)
                row[f"fills_{minutes}m"] = fill_count
                row[f"one_sided_fill_ratio_{minutes}m"] = max(buys, sells) / fill_count if fill_count else 0.0
            if minutes in {5, 10, 20}:
                reversals, ratio, _, _ = _reversal_metrics(chunk)
                row[f"reversal_count_{minutes}m"] = reversals
                row[f"reversal_ratio_{minutes}m"] = ratio

        chunk20 = _window(rows, index, 21)
        returns20 = _returns(chunk20)
        net_move = abs(_f(row.get("price")) - _f(chunk20[0].get("price")))
        total_move = sum(abs(value) for value in returns20) * max(_f(row.get("price")), 1.0)
        row["directional_efficiency_ratio"] = net_move / total_move if total_move else 0.0
        row["directional_efficiency_ratio_20m"] = row["directional_efficiency_ratio"]
        direction_sign = 1 if str(event.get("breakout_direction")) == "UP" else -1
        price_changes = [_f(chunk20[i].get("price")) - _f(chunk20[i - 1].get("price")) for i in range(1, len(chunk20))]
        signed = [change * direction_sign for change in price_changes]
        row["same_direction_bar_ratio"] = sum(value > 0 for value in signed) / max(1, len(signed))
        _, reversal_ratio, current_streak, max_streak = _reversal_metrics(chunk20)
        row["reversal_rate"] = reversal_ratio
        row["reversal_ratio"] = reversal_ratio
        row["crossing_rate"] = sum(
            1
            for left, right in zip(chunk20, chunk20[1:])
            if (_f(left.get("price")) - float(params.center)) * (_f(right.get("price")) - float(params.center)) < 0
        ) / max(1, len(chunk20) - 1)
        row["consecutive_directional_bars"] = current_streak
        row["same_side_move_duration"] = max_streak
        row["failed_reversal_count"] = max(0, max_streak - 3)
        row["time_since_last_reversal"] = max_streak
        row["paired_fill_conversion_rate"] = _f(row.get("cumulative_paired_reversal_fills")) / max(1.0, _f(row.get("cumulative_new_inventory_fills")))
        peak_inventory = max((_f(item.get("gross_inventory_notional")) for item in rows[: index + 1]), default=0.0)
        row["inventory_recovery_rate"] = max(0.0, peak_inventory - _f(row.get("gross_inventory_notional"))) / peak_inventory if peak_inventory else 0.0
        onset_price = next((_f(item.get("price")) for item in rows[: index + 1] if _f(item.get("gross_inventory")) > 0), _f(row.get("price")))
        row["signed_return_from_inventory_onset"] = direction_sign * (_f(row.get("price")) / onset_price - 1.0) if onset_price else 0.0
        row["ATR"] = _atr(_window(rows, index, 14))
        row["ATR_pct"] = row["ATR"] / _f(row.get("price")) if _f(row.get("price")) else 0.0
        row["volatility_ratio_short_long"] = _f(row.get("realized_vol_5m")) / max(_f(row.get("realized_vol_60m")), 1e-12)
        row["range_vs_grid_width"] = _f(row.get("range_20m")) / max(step_abs, 1e-12)
        row["range_vs_initial_grid_band"] = _f(row.get("range_20m")) / max(float(params.upper) - float(params.lower), 1e-12)
        row["distance_to_grid_boundary"] = min(abs(_f(row.get("distance_to_upper_boundary"))), abs(_f(row.get("distance_to_lower_boundary"))))
        q5 = _window(rows, index, 5)
        q20 = _window(rows, index, 20)
        row["quote_volume_rate_5m"] = sum(_f(item.get("quote_volume")) for item in q5) / max(1, len(q5))
        row["quote_volume_rate_20m"] = sum(_f(item.get("quote_volume")) for item in q20) / max(1, len(q20))
        row["trade_rate_5m"] = sum(_f(item.get("trade_count")) for item in q5) / max(1, len(q5))
        row["trade_rate_20m"] = sum(_f(item.get("trade_count")) for item in q20) / max(1, len(q20))
        row["volume_acceleration"] = row["quote_volume_rate_5m"] / max(row["quote_volume_rate_20m"], 1e-12)
        row["trade_count_acceleration"] = row["trade_rate_5m"] / max(row["trade_rate_20m"], 1e-12)
        row["fill_acceleration"] = _f(row.get("fills_5m")) / 5.0 - _f(row.get("fills_20m")) / 20.0
        row["fill_rate"] = _f(row.get("fills_5m")) / 5.0
        row["fills_per_grid_level"] = _f(row.get("fills_20m")) / max(1.0, _f(row.get("current_grid_count")))
        row["one_sided_fill_ratio"] = _f(row.get("one_sided_fill_ratio_10m"))
        row["inventory_age"] = _f(row.get("inventory_age_max"))
        row["unrealized_inventory_pnl"] = _f(row.get("inventory_pnl_unrealized"))
        row["realized_vol"] = _f(row.get("realized_vol_20m"))
        row["price_slope"] = _f(row.get("linear_price_slope_20m"))
        row["return_1m"] = _f(row.get("return_1m"))
        row["predictor_fields"] = "|".join(
            sorted(key for key in row if key not in LABEL_FIELDS and key not in {"predictor_fields"})
        )
    return rows


def _point_at_or_before(path: Sequence[Mapping[str, Any]], timestamp: datetime) -> dict[str, Any] | None:
    eligible = [row for row in path if _dt(row["timestamp"]) <= timestamp]
    return dict(eligible[-1]) if eligible else None


def _event_markers(path: Sequence[Mapping[str, Any]], event: Mapping[str, Any]) -> dict[str, str]:
    signal = _dt(event["signal_time"])
    pre = [row for row in path if _dt(row["timestamp"]) <= signal]
    stress = next((row for row in pre if _f(row.get("gross_inventory_utilization")) >= 0.25 or int(row.get("unpaired_lot_count", 0)) >= 2), None)
    fill_cluster = next((row for row in pre if _f(row.get("fills_5m")) >= 2 and _f(row.get("one_sided_fill_ratio_5m")) >= 0.80), None)
    range_break = next(
        (
            row
            for row in pre
            if (_f(row.get("distance_to_upper_boundary")) < 0 if event["breakout_direction"] == "UP" else _f(row.get("distance_to_lower_boundary")) < 0)
        ),
        None,
    )
    precursor = next(
        (
            row
            for row in pre
            if range_break is not None
            and _dt(row["timestamp"]) >= _dt(range_break["timestamp"])
            and _f(row.get("same_direction_bar_ratio")) >= 0.55
        ),
        range_break,
    )
    anchor = stress or fill_cluster or range_break or (pre[0] if pre else None)
    return {
        "event_anchor_time": str(anchor.get("timestamp", "")) if anchor else "",
        "D2_first_warning_time": str(precursor.get("timestamp", "")) if precursor else "",
        "D2_confirmation_time": _iso(signal),
        "first_inventory_stress": str(stress.get("timestamp", "")) if stress else "",
        "first_one_sided_fill_cluster": str(fill_cluster.get("timestamp", "")) if fill_cluster else "",
        "first_range_break": str(range_break.get("timestamp", "")) if range_break else "",
        "first_D2_precursor": str(precursor.get("timestamp", "")) if precursor else "",
    }


def _event_outcome(path: Sequence[Mapping[str, Any]], event: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    signal = _dt(event["signal_time"])
    at_signal = _point_at_or_before(path, signal)
    after = [row for row in path if _dt(row["timestamp"]) >= signal]
    if not at_signal or not after:
        return {
            "future_tail_loss": 0.0,
            "future_MAE": 0.0,
            "future_MFE": 0.0,
            "event_end_time": "",
            "tail_event_time": "",
            "force_close_time": str(source.get("force_close_at") or source.get("window_end") or ""),
            "time_to_force_close": "",
        }
    signal_pnl = _f(at_signal.get("inventory_pnl_unrealized"))
    worst = min(after, key=lambda row: _f(row.get("inventory_pnl_unrealized")))
    signal_price = _f(at_signal.get("price"))
    direction = 1 if event["breakout_direction"] == "UP" else -1
    signed_moves = [direction * (_f(row.get("price")) - signal_price) for row in after]
    lower = _f(at_signal.get("price")) - _f(at_signal.get("distance_to_lower_boundary"))
    upper = _f(at_signal.get("price")) + _f(at_signal.get("distance_to_upper_boundary"))
    recovery = next((row for row in after[1:] if lower <= _f(row.get("price")) <= upper), None)
    force_close = str(source.get("force_close_at") or source.get("window_end") or after[-1]["timestamp"])
    end = recovery["timestamp"] if recovery and event["posthoc_label"] == "FALSE_BREAKOUT" else after[-1]["timestamp"]
    return {
        "future_tail_loss": max(0.0, signal_pnl - _f(worst.get("inventory_pnl_unrealized"))),
        "future_MAE": max(signed_moves, default=0.0),
        "future_MFE": max((-value for value in signed_moves), default=0.0),
        "event_end_time": end,
        "tail_event_time": worst["timestamp"],
        "force_close_time": force_close,
        "time_to_force_close": lead_time_minutes(event["signal_time"], force_close),
    }


def _snapshots(path: Sequence[Mapping[str, Any]], event: Mapping[str, Any], markers: Mapping[str, str]) -> list[dict[str, Any]]:
    signal = _dt(event["signal_time"])
    rows: list[dict[str, Any]] = []
    for offset in CHECKPOINTS:
        target = signal + timedelta(minutes=offset)
        point = _point_at_or_before(path, target)
        if point is None or (target - _dt(point["timestamp"])).total_seconds() > 90:
            rows.append(
                {
                    "event_id": event["event_id"],
                    "symbol": event["symbol"],
                    "window_key": event["window_key"],
                    "truth_label": event["posthoc_label"],
                    "checkpoint": CHECKPOINT_LABELS[offset],
                    "checkpoint_offset_minutes": offset,
                    "snapshot_status": "INSUFFICIENT_LOOKBACK",
                    "observation_timestamp": target.isoformat(),
                    "feature_timestamp": "",
                    "canonical_path": "31111-NEUTRAL CONTROL",
                }
            )
            continue
        point.update(
            {
                "truth_label": event["posthoc_label"],
                "checkpoint": CHECKPOINT_LABELS[offset],
                "checkpoint_offset_minutes": offset,
                "snapshot_status": "OK",
                "observation_timestamp": target.isoformat(),
                "event_relative_minute": offset,
            }
        )
        rows.append(point)
    relative = {
        "first_inventory_stress": markers.get("first_inventory_stress", ""),
        "first_one_sided_fill_cluster": markers.get("first_one_sided_fill_cluster", ""),
        "first_range_break": markers.get("first_range_break", ""),
        "first_D2_precursor": markers.get("first_D2_precursor", ""),
        "D2_confirmation": markers.get("D2_confirmation_time", ""),
    }
    for name, timestamp in relative.items():
        if not timestamp:
            rows.append(
                {
                    "event_id": event["event_id"],
                    "symbol": event["symbol"],
                    "window_key": event["window_key"],
                    "truth_label": event["posthoc_label"],
                    "checkpoint": name,
                    "snapshot_status": "NULL_EVENT_RELATIVE_MARKER",
                    "canonical_path": "31111-NEUTRAL CONTROL",
                }
            )
            continue
        point = _point_at_or_before(path, _dt(timestamp))
        if point:
            point.update(
                {
                    "truth_label": event["posthoc_label"],
                    "checkpoint": name,
                    "snapshot_status": "OK",
                    "observation_timestamp": timestamp,
                    "event_relative_minute": lead_time_minutes(event["signal_time"], timestamp),
                }
            )
            rows.append(point)
    return rows


def _numeric_features(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    excluded = {
        "bar_index",
        "checkpoint_offset_minutes",
        "event_relative_minute",
        "seed",
    } | LABEL_FIELDS
    keys = sorted({key for row in rows for key in row})
    result: list[str] = []
    for key in keys:
        if key in excluded or key.endswith("_time") or key.endswith("timestamp"):
            continue
        values = [row.get(key) for row in rows if row.get(key) not in (None, "")]
        if values and all(_is_number(value) for value in values):
            result.append(key)
    return result


def _is_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _cliffs_delta(true_values: Sequence[float], false_values: Sequence[float]) -> float:
    if not true_values or not false_values:
        return 0.0
    wins = sum(left > right for left in true_values for right in false_values)
    losses = sum(left < right for left in true_values for right in false_values)
    return (wins - losses) / (len(true_values) * len(false_values))


def _auroc(true_values: Sequence[float], false_values: Sequence[float]) -> float:
    return (_cliffs_delta(true_values, false_values) + 1.0) / 2.0 if true_values and false_values else 0.5


def _auprc(values: Sequence[tuple[float, bool]]) -> float:
    positives = sum(label for _, label in values)
    if not positives:
        return 0.0
    ordered = sorted(values, key=lambda item: item[0], reverse=True)
    hits = 0
    previous_recall = 0.0
    area = 0.0
    for rank, (_, label) in enumerate(ordered, 1):
        hits += int(label)
        recall = hits / positives
        precision = hits / rank
        area += (recall - previous_recall) * precision
        previous_recall = recall
    return area


def _exact_permutation(true_values: Sequence[float], false_values: Sequence[float]) -> float:
    combined = list(true_values) + list(false_values)
    n_true = len(true_values)
    if not combined or not n_true or n_true == len(combined):
        return 1.0
    observed = abs(statistics.fmean(true_values) - statistics.fmean(false_values))
    extreme = 0
    total = 0
    indexes = range(len(combined))
    for chosen in itertools.combinations(indexes, n_true):
        selected = set(chosen)
        left = [combined[index] for index in indexes if index in selected]
        right = [combined[index] for index in indexes if index not in selected]
        statistic = abs(statistics.fmean(left) - statistics.fmean(right))
        extreme += statistic >= observed - 1e-12
        total += 1
    return extreme / total if total else 1.0


def _describe(values: Sequence[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values) if values else 0.0,
        "median": statistics.median(values) if values else 0.0,
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": min(values, default=0.0),
        "max": max(values, default=0.0),
    }


def feature_main_effects(snapshots: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    fixed = [row for row in snapshots if str(row.get("checkpoint", "")).startswith("T") and row.get("snapshot_status") == "OK"]
    features = _numeric_features(fixed)
    rows: list[dict[str, Any]] = []
    for checkpoint in [CHECKPOINT_LABELS[value] for value in CHECKPOINTS]:
        members = [row for row in fixed if row.get("checkpoint") == checkpoint]
        for feature in features:
            true_values = [_f(row[feature]) for row in members if row.get("truth_label") == "TRUE_BREAKOUT" and row.get(feature) not in (None, "")]
            false_values = [_f(row[feature]) for row in members if row.get("truth_label") == "FALSE_BREAKOUT" and row.get(feature) not in (None, "")]
            if not true_values or not false_values:
                continue
            true_stats = _describe(true_values)
            false_stats = _describe(false_values)
            delta = _cliffs_delta(true_values, false_values)
            auc_high = _auroc(true_values, false_values)
            auc = max(auc_high, 1.0 - auc_high)
            signed_values = [(value if auc_high >= 0.5 else -value, True) for value in true_values] + [(value if auc_high >= 0.5 else -value, False) for value in false_values]
            ranked = sorted(
                [(value if auc_high >= 0.5 else -value, row.get("truth_label") == "TRUE_BREAKOUT", row.get("event_id")) for row, value in [(row, _f(row[feature])) for row in members if row.get(feature) not in (None, "")]],
                reverse=True,
            )
            true_ranks = [index for index, (_, label, _) in enumerate(ranked, 1) if label]
            absolute = _f(true_stats["median"]) - _f(false_stats["median"])
            rows.append(
                {
                    "feature": feature,
                    "checkpoint": checkpoint,
                    "true_count": len(true_values),
                    "false_count": len(false_values),
                    "true_mean": true_stats["mean"],
                    "false_mean": false_stats["mean"],
                    "true_median": true_stats["median"],
                    "false_median": false_stats["median"],
                    "true_std": true_stats["std"],
                    "false_std": false_stats["std"],
                    "true_min": true_stats["min"],
                    "true_max": true_stats["max"],
                    "false_min": false_stats["min"],
                    "false_max": false_stats["max"],
                    "absolute_difference": absolute,
                    "relative_difference": absolute / max(abs(_f(false_stats["median"])), 1e-12),
                    "effect_size": delta,
                    "rank_biserial": delta,
                    "cliffs_delta": delta,
                    "direction": "TRUE_HIGH" if delta > 0 else "TRUE_LOW" if delta < 0 else "TIE",
                    "AUROC": auc,
                    "AUPRC": _auprc(signed_values),
                    "true_rank_positions": "|".join(str(value) for value in true_ranks),
                    "top_2_recall": sum(rank <= 2 for rank in true_ranks) / len(true_ranks),
                    "top_5_recall": sum(rank <= 5 for rank in true_ranks) / len(true_ranks),
                    "exact_permutation_result": _exact_permutation(true_values, false_values),
                    "sample_warning": "EXPLORATORY_SMALL_SAMPLE;N_TRUE=2",
                }
            )
    return rows


def _confusion(predictions: Mapping[str, bool], events: Sequence[Mapping[str, Any]]) -> dict[str, int | float]:
    tp = fp = fn = tn = 0
    for event in events:
        truth = event.get("posthoc_label") == "TRUE_BREAKOUT"
        predicted = bool(predictions.get(str(event["event_id"]), False))
        tp += predicted and truth
        fp += predicted and not truth
        fn += not predicted and truth
        tn += not predicted and not truth
    return {**v34.confusion_metrics(tp, fp, fn, tn), "specificity": tn / max(1, tn + fp)}


def _fisher_exact(tp: int, fp: int, fn: int, tn: int) -> float:
    total = tp + fp + fn + tn
    positives = tp + fn
    alerts = tp + fp
    if total == 0:
        return 1.0
    denominator = math.comb(total, alerts)
    observed = math.comb(positives, tp) * math.comb(total - positives, fp) / denominator
    probability = 0.0
    lower = max(0, alerts - (total - positives))
    upper = min(alerts, positives)
    for value in range(lower, upper + 1):
        chance = math.comb(positives, value) * math.comb(total - positives, alerts - value) / denominator
        if chance <= observed + 1e-12:
            probability += chance
    return min(1.0, probability)


def feature_interactions(snapshots: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for checkpoint in [CHECKPOINT_LABELS[value] for value in CHECKPOINTS]:
        members = [row for row in snapshots if row.get("checkpoint") == checkpoint and row.get("snapshot_status") == "OK"]
        for feature_a, feature_b, description, predicate in INTERACTIONS:
            predictions = {str(row["event_id"]): bool(predicate(row)) for row in members}
            metrics = _confusion(predictions, events)
            rows.append(
                {
                    "feature_a": feature_a,
                    "feature_b": feature_b,
                    "checkpoint": checkpoint,
                    "rule_description": description,
                    **metrics,
                    "lead_time_vs_D2": abs(int(checkpoint[1:])) if checkpoint != "T0" else 0,
                    "fisher_exact_test": _fisher_exact(int(metrics["TP"]), int(metrics["FP"]), int(metrics["FN"]), int(metrics["TN"])),
                    "robustness_status": "EXPLORATORY_SMALL_SAMPLE",
                }
            )
    return rows


def _warning_for_rule(path: Sequence[Mapping[str, Any]], event: Mapping[str, Any], predicate: Callable[[Mapping[str, Any]], bool]) -> dict[str, Any] | None:
    signal = _dt(event["signal_time"])
    pre_signal = [row for row in path if _dt(row["timestamp"]) < signal]
    stress = next(
        (
            row
            for row in pre_signal
            if _f(row.get("gross_inventory_utilization")) >= 0.25
            or int(row.get("unpaired_lot_count", 0)) >= 2
        ),
        None,
    )
    start = _dt(stress["timestamp"]) if stress else signal - timedelta(minutes=60)
    return next((dict(row) for row in path if start <= _dt(row["timestamp"]) < signal and predicate(row)), None)


def evaluate_early_warning_rules(
    paths: Mapping[str, Sequence[Mapping[str, Any]]], events: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any] | None]]:
    candidates: list[dict[str, Any]] = []
    warnings_by_rule: dict[str, dict[str, Any] | None] = {}
    for rule in EARLY_WARNING_RULES:
        warnings: dict[str, dict[str, Any] | None] = {
            str(event["event_id"]): _warning_for_rule(paths[str(event["event_id"])], event, rule["predicate"])
            for event in events
        }
        predictions = {event_id: warning is not None for event_id, warning in warnings.items()}
        metrics = _confusion(predictions, events)
        true_leads = [
            lead_time_minutes(str(warnings[str(event["event_id"])]["timestamp"]), str(event["signal_time"]))
            for event in events
            if event.get("posthoc_label") == "TRUE_BREAKOUT" and warnings[str(event["event_id"])] is not None
        ]
        true_warning_rows = [
            (event, warnings[str(event["event_id"])])
            for event in events
            if event.get("posthoc_label") == "TRUE_BREAKOUT"
            and warnings[str(event["event_id"])] is not None
        ]
        tail_counterfactuals = [
            _tail_remaining(paths[str(event["event_id"])], warning, event)
            for event, warning in true_warning_rows
        ]
        row = {
            "rule_id": rule["rule_id"],
            "rule_description": rule["description"],
            "feature_count": len(rule["features"]),
            "features": "|".join(rule["features"]),
            "pre_registered_thresholds": "|".join(str(value) for value in rule["thresholds"]),
            **metrics,
            "mean_lead_time_vs_D2": statistics.fmean(value for value in true_leads if value is not None) if true_leads else 0.0,
            "minimum_lead_time_vs_D2": min((value for value in true_leads if value is not None), default=0.0),
            "mean_inventory_present_at_warning": statistics.fmean(_f(warning.get("gross_inventory_notional")) for _, warning in true_warning_rows) if true_warning_rows else 0.0,
            "mean_future_tail_loss_after_warning": statistics.fmean(item[1] for item in tail_counterfactuals) if tail_counterfactuals else 0.0,
            "mean_fraction_tail_remaining_at_warning": statistics.fmean(item[2] for item in tail_counterfactuals) if tail_counterfactuals else 0.0,
            "LOEO_status": "INSUFFICIENT_EVENT_SAMPLE_FOR_LOEO",
            "candidate_status": "PROMISING_EARLY_WARNING_HYPOTHESIS" if metrics["TP"] == 2 and metrics["FP"] <= 5 and metrics["FN"] == 0 else "NOT_PROMISING",
            "sample_warning": "SMALL_EVENT_SAMPLE;EXPLORATORY_SMALL_SAMPLE;N_TRUE=2",
        }
        candidates.append(row)
        warnings_by_rule[str(rule["rule_id"])] = warnings
    return candidates, warnings_by_rule


def robustness_rows(
    best_rule: Mapping[str, Any] | None,
    paths: Mapping[str, Sequence[Mapping[str, Any]]],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not best_rule:
        return [{"status": "NO_PROMISING_RULE_FOR_LOCAL_ROBUSTNESS"}]
    rule = next(item for item in EARLY_WARNING_RULES if item["rule_id"] == best_rule["rule_id"])
    feature_a, feature_b = rule["features"]
    threshold_a, threshold_b = rule["thresholds"]
    rows: list[dict[str, Any]] = []
    for scale_a in (0.8, 1.0, 1.2):
        for scale_b in (0.8, 1.0, 1.2):
            a = threshold_a * scale_a
            b = threshold_b * scale_b
            high_a = feature_a not in {"grid_headroom_remaining", "reversal_ratio_20m"}
            low_b = feature_b in {"grid_headroom_remaining", "reversal_ratio_20m"}
            predicate = lambda row, a=a, b=b: (_f(row.get(feature_a)) >= a if high_a else _f(row.get(feature_a)) <= a) and (_f(row.get(feature_b)) <= b if low_b else _f(row.get(feature_b)) >= b)
            predictions = {
                str(event["event_id"]): _warning_for_rule(paths[str(event["event_id"])], event, predicate) is not None
                for event in events
            }
            metrics = _confusion(predictions, events)
            rows.append(
                {
                    "rule_id": rule["rule_id"],
                    "feature_a": feature_a,
                    "feature_b": feature_b,
                    "threshold_a_scale": scale_a,
                    "threshold_b_scale": scale_b,
                    "threshold_a": a,
                    "threshold_b": b,
                    **metrics,
                    "purpose": "LOCAL_ROBUSTNESS_NOT_THRESHOLD_SEARCH",
                }
            )
    stable = sum(row["TP"] == 2 and row["FN"] == 0 and row["FP"] <= 5 for row in rows)
    status = "ROBUST_LOCAL_REGION" if stable >= 5 else "REJECT_ISOLATED_EARLY_WARNING_THRESHOLD" if stable == 1 else "UNSTABLE_LOCAL_REGION"
    for row in rows:
        row["robustness_status"] = status
    return rows


def _false_reasons(event: Mapping[str, Any], path: Sequence[Mapping[str, Any]], timeline: Mapping[str, Any]) -> list[str]:
    signal = _dt(event["signal_time"])
    t0 = _point_at_or_before(path, signal) or {}
    end = _dt(timeline["event_end_time"]) if timeline.get("event_end_time") else signal
    recovery_minutes = (end - signal).total_seconds() / 60.0
    reasons: list[str] = []
    if recovery_minutes <= 30:
        reasons.append("FAST_REVERSAL")
    if _f(t0.get("grid_headroom_remaining")) >= 2:
        reasons.append("SUFFICIENT_GRID_HEADROOM")
    if _f(t0.get("directional_efficiency_ratio_20m")) < 0.50:
        reasons.append("LOW_DIRECTIONAL_PERSISTENCE")
    if _f(t0.get("fraction_of_window_elapsed")) < 0.50:
        reasons.append("EARLY_SESSION_RECOVERY")
    if _f(t0.get("inventory_age_max")) < 60:
        reasons.append("LOW_INVENTORY_AGE")
    if _f(t0.get("volatility_ratio_short_long")) <= 1.0:
        reasons.append("VOLATILITY_MEAN_REVERSION")
    if _f(t0.get("gross_inventory_utilization")) < 0.40:
        reasons.append("SMALL_GROSS_EXPOSURE")
    return reasons or ["OTHER"]


def _true_mechanisms(path: Sequence[Mapping[str, Any]], event: Mapping[str, Any]) -> list[str]:
    t0 = _point_at_or_before(path, _dt(event["signal_time"])) or {}
    mechanisms: list[str] = []
    if _f(t0.get("inventory_build_rate_utilization_20m")) > 0:
        mechanisms.append("INVENTORY_BUILD")
    if _f(t0.get("one_sided_fill_ratio_10m")) >= 0.80:
        mechanisms.append("ONE_SIDED_FILL_CASCADE")
    if _f(t0.get("reversal_ratio_20m"), 1.0) <= 0.25:
        mechanisms.append("REVERSAL_FAILURE")
    if _f(t0.get("directional_efficiency_ratio_20m")) >= 0.60:
        mechanisms.append("DIRECTIONAL_PERSISTENCE")
    if _f(t0.get("volatility_ratio_short_long")) >= 1.25:
        mechanisms.append("VOLATILITY_EXPANSION")
    if _f(t0.get("grid_headroom_remaining")) <= 2:
        mechanisms.append("GRID_HEADROOM_COMPRESSION")
    if _f(t0.get("is_late_session")):
        mechanisms.append("LATE_SESSION_TIME_PRESSURE")
    return mechanisms or ["CUMULATIVE_CONTROL_PATH_TAIL"]


def _tail_remaining(path: Sequence[Mapping[str, Any]], warning: Mapping[str, Any] | None, event: Mapping[str, Any]) -> tuple[float, float, float]:
    if warning is None:
        return 0.0, 0.0, 0.0
    start = next(
        (
            row
            for row in path
            if _f(row.get("gross_inventory_utilization")) >= 0.25
            or int(row.get("unpaired_lot_count", 0)) >= 2
        ),
        path[0],
    )
    after_start = [row for row in path if _dt(row["timestamp"]) >= _dt(start["timestamp"])]
    after_warning = [row for row in path if _dt(row["timestamp"]) >= _dt(warning["timestamp"])]
    worst = min((_f(row.get("inventory_pnl_unrealized")) for row in after_start), default=0.0)
    total = max(0.0, _f(start.get("inventory_pnl_unrealized")) - worst)
    remaining = max(0.0, _f(warning.get("inventory_pnl_unrealized")) - min((_f(row.get("inventory_pnl_unrealized")) for row in after_warning), default=_f(warning.get("inventory_pnl_unrealized"))))
    before = max(0.0, total - remaining)
    return before, remaining, min(1.0, remaining / total) if total else 0.0


def _top_features(main_effects: Sequence[Mapping[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    excluded = {
        "price",
        "open",
        "high",
        "low",
        "base_volume",
        "quote_volume",
        "trade_count",
        "bar_index",
        "current_grid_count",
        "current_grid_step",
        "minutes_since_window_start",
        "fraction_of_window_elapsed",
    }
    eligible = [
        row
        for row in main_effects
        if row.get("checkpoint") != "T0" and row.get("feature") not in excluded
    ]
    return sorted(eligible, key=lambda row: (_f(row.get("AUROC")), abs(_f(row.get("effect_size")))), reverse=True)[:limit]


def _earliest_descriptive_checkpoint(main_effects: Sequence[Mapping[str, Any]]) -> str:
    for checkpoint in ("T-60", "T-30", "T-20", "T-15", "T-10", "T-5"):
        if any(
            row.get("checkpoint") == checkpoint
            and int(row.get("true_count", 0)) == 2
            and int(row.get("false_count", 0)) >= 8
            and _f(row.get("AUROC")) >= 0.80
            and abs(_f(row.get("effect_size"))) >= 0.60
            for row in main_effects
        ):
            return checkpoint
    return "NO_DESCRIPTIVE_PRE_D2_SEPARATION"


def _symbol_breakdown(events: Sequence[Mapping[str, Any]], classifications: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_event = {str(row["event_id"]): row for row in classifications}
    rows: list[dict[str, Any]] = []
    for symbol in v32.SYMBOLS:
        members = [event for event in events if event.get("symbol") == symbol]
        true_count = sum(event.get("posthoc_label") == "TRUE_BREAKOUT" for event in members)
        false_count = sum(event.get("posthoc_label") == "FALSE_BREAKOUT" for event in members)
        predicted = [by_event[str(event["event_id"])] for event in members]
        rows.append(
            {
                "symbol": symbol,
                "instrument_class": "LEVERAGED_ETF_LINKED" if symbol == "SOXLUSDT" else "EQUITY_LINKED_PERP",
                "unique_events": len(members),
                "true_events": true_count,
                "false_events": false_count,
                "early_warning_alerts": sum(str(row.get("early_warning_prediction")) == "TRUE" for row in predicted),
                "cross_symbol_status": "NO_TRUE_EVENT_SUPPORT" if true_count == 0 else "DESCRIPTIVE_ONLY",
                "note": "SOXL kept separate; volatility and persistence are not pooled." if symbol == "SOXLUSDT" else "EXPLORATORY_SMALL_SAMPLE",
            }
        )
    return rows


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def _final_report(
    parity: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    timelines: Sequence[Mapping[str, Any]],
    main_effects: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    classifications: Sequence[Mapping[str, Any]],
    robustness: Sequence[Mapping[str, Any]],
    conclusion: str,
) -> str:
    labels = Counter(str(event.get("posthoc_label")) for event in events)
    top = _top_features(main_effects)
    promising = next((row for row in candidates if row.get("candidate_status") == "PROMISING_EARLY_WARNING_HYPOTHESIS"), None)
    best = promising or (min(candidates, key=lambda row: (int(row.get("FN", 0)), int(row.get("FP", 99)))) if candidates else None)
    true_rows = [row for row in classifications if row.get("truth_label") == "TRUE_BREAKOUT"]
    false_rows = [row for row in classifications if row.get("truth_label") == "FALSE_BREAKOUT"]
    false_reason_counts = Counter(reason for row in false_rows for reason in str(row.get("false_positive_reason", "")).split("|") if reason)
    mechanisms = Counter(reason for row in true_rows for reason in str(row.get("true_tail_mechanism", "")).split("|") if reason)
    earliest_checkpoint = _earliest_descriptive_checkpoint(main_effects)
    alerted_true = [row for row in true_rows if row.get("early_warning_prediction") == "TRUE"]
    tail_remaining = statistics.fmean(_f(row.get("fraction_tail_remaining_at_warning")) for row in alerted_true) if alerted_true else 0.0
    checkpoint_features: list[str] = []
    for checkpoint in ("T-60", "T-30", "T-20", "T-10", "T-5"):
        strongest = next(
            (
                row
                for row in _top_features(main_effects, limit=len(main_effects))
                if row.get("checkpoint") == checkpoint
                and int(row.get("true_count", 0)) == 2
            ),
            None,
        )
        checkpoint_features.append(
            f"{checkpoint}={strongest['feature']} (AUROC={_f(strongest['AUROC']):.3f})"
            if strongest
            else f"{checkpoint}=INSUFFICIENT_COMPLETE_EVENT_SUPPORT"
        )
    soxl = [event for event in events if event.get("symbol") == "SOXLUSDT"]
    isolated = next((row.get("robustness_status") for row in robustness if row.get("robustness_status")), "NO_PROMISING_RULE_FOR_LOCAL_ROBUSTNESS")
    lines = [
        "# QuietGrid Semiconductor Grid v3.5",
        "",
        "## Inventory Tail Formation & Early Warning Study",
        "",
        "All observations are `RESEARCH_VALIDATION_EXPOSED`. The canonical formation source is `31111-NEUTRAL CONTROL`; S1/S2/S3 paths are not used. `SMALL_EVENT_SAMPLE` and `EXPLORATORY_SMALL_SAMPLE` apply throughout.",
        "",
        "## Direct Answers",
        "",
        f"1. v3.4 parity: `{parity['status']}`; CONTROL=`{parity['control_parity']['status']}`, v3.3=`{parity['v33_parity']['status']}`.",
        f"2. Unique events: {len(events)}.",
        f"3. TRUE: {labels.get('TRUE_BREAKOUT', 0)}.",
        f"4. FALSE: {labels.get('FALSE_BREAKOUT', 0)}.",
        f"5. Typical TRUE path: {', '.join(name for name, _ in mechanisms.most_common()) or 'insufficient evidence'}; this is descriptive, not validated.",
        f"6. FALSE self-healing mechanisms: {', '.join(f'{name}={count}' for name, count in false_reason_counts.most_common())}.",
        f"7. Earliest descriptive TRUE/FALSE group separation: `{earliest_checkpoint}_DESCRIPTIVE_ONLY`; no tested simple rule both preserves 2/2 TRUE and materially reduces FALSE alerts.",
        "8. Strongest descriptive feature by requested checkpoint: " + "; ".join(checkpoint_features) + ".",
        "9. Inventory build: measured in notional and utilization rates; see feature-main-effects.csv.",
        "10. One-sided fill cascade: measured, but its independent support is limited by sparse fills and two TRUE events.",
        "11. Reversal failure: measured by reversal ratios/counts and paired-fill conversion.",
        "12. Directional persistence: measured by returns, slopes, efficiency, same-direction ratio, and streaks.",
        "13. Volatility expansion: measured by ATR, realized volatility, short/long ratio, and range expansion.",
        "14. Grid headroom: measured in remaining adverse grid levels and boundary distance.",
        "15. Time-to-force-close: measured for every minute and checkpoint.",
        "16. TAIL_IRREVERSIBILITY_POINT: `NO_RELIABLE_IRREVERSIBILITY_POINT` because N_TRUE=2 cannot estimate recovery probability reliably.",
        f"17. Best tested simple rule (status `{best['candidate_status']}`, not an accepted hypothesis): `{best['rule_id']}: {best['rule_description']}`." if best else "17. Best simple hypothesis: NONE.",
        f"18. TRUE recall: {int(best['TP'])}/2; FN={int(best['FN'])}." if best else "18. TRUE recall: 0/2.",
        f"19. FALSE alerts: 10 -> {int(best['FP'])}." if best else "19. FALSE alerts: unchanged at 10.",
        f"20. Mean lead vs D2: {_f(best['mean_lead_time_vs_D2']):.1f} minutes." if best else "20. Mean lead vs D2: 0 minutes.",
        f"21. Fraction of tail remaining at warning among alerted TRUE events (n={len(alerted_true)}): {tail_remaining:.2%}." + (" Unalerted TRUE events have no warning-time value." if len(alerted_true) < len(true_rows) else " Both TRUE events were alerted by the best tested rule."),
        "22. Cross-symbol stability: `INSUFFICIENT_CROSS_SYMBOL_TRUE_SUPPORT`; TRUE events occur only in MU and SNDK.",
        f"23. SOXL: {len(soxl)} FALSE and 0 TRUE events; it is reported separately as leveraged ETF-linked and cannot validate a shared rule.",
        f"24. Local threshold status: `{isolated}`.",
        "25. Sample gate: `SMALL_EVENT_SAMPLE` because TRUE < 5 and total events < 30; it is insufficient for validation or reliable LOEO.",
        "26. Recommended next stage: `NONE` unless a future protocol expands independent TRUE events; no automatic v3.6 action design is authorized.",
        "27. New Forward OOS candidate: `NONE`.",
        "28. Production config: unchanged; auto-entry remains OFF; economic leverage remains 1x.",
        "29. Pytest: see tests.txt; final requirement is 0 failed.",
        f"30. Conclusion code: `{conclusion}`.",
        "",
        "## Interpretation",
        "",
        "Because there are only two independent TRUE market events, rankings, AUROC/AUPRC, exact permutations, and Fisher results are descriptive. Scenario x seed replicas remain execution diagnostics and never enter sample counts. No detector action or trading-path counterfactual is implemented.",
    ]
    return "\n".join(lines) + "\n"


def run_research(*, output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    parity = reproduce_v34_event_parity()
    _write_json(output_dir / "event-parity.json", parity)
    if parity["status"] != "PASS_V34_EVENT_PARITY":
        _write_json(output_dir / "data-quality.json", {"status": "FAIL_EVENT_LABEL_PARITY", "stop_gate": "FAIL_V34_EVENT_PARITY"})
        return {"conclusion": "FAIL_EVENT_LABEL_PARITY", "parity": parity}

    all_events, _ = v34._profile_reference_rows()
    events = deduplicate_events(all_events)
    sources = _source_rows()
    raw = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
    paths: dict[str, list[dict[str, Any]]] = {}
    snapshots: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    unique_rows: list[dict[str, Any]] = []
    replay_errors: list[str] = []
    fallback_event_ids: list[str] = []

    for event in events:
        key = _event_key(event)
        source = sources.get(key)
        if source is None:
            replay_errors.append(f"missing CONTROL source row: {event['event_id']}")
            continue
        result, candidate, _, trade, _ = v32._research_replay(raw, source)
        used_fallback = False
        if result is None or candidate is None:
            located = v32._direct_window_inputs(source)
            if located is None or not located[1]:
                replay_errors.append(f"CONTROL replay failed: {event['event_id']}")
                continue
            trade = located[1]
            result, candidate = _fallback_control_replay(trade, source)
            fallback_event_ids.append(str(event["event_id"]))
            used_fallback = True
        if not trade:
            replay_errors.append(f"empty CONTROL trade path: {event['event_id']}")
            continue
        control_path = reconstruct_control_path(result, candidate, trade, event, source)
        if not control_path:
            replay_errors.append(f"empty CONTROL path: {event['event_id']}")
            continue
        # Preserve an independent parity check against the established path helper.
        reference_path = (
            v32._path_for_detector([], trade, None, source)
            if used_fallback
            else _path_rows(result, candidate, source, PRIMARY_SCENARIO, int(PRIMARY_SEED))[0]
        )
        replayed_path_count = sum(
            row.get("path_state_source") != "CLAMPED_POST_EQUITY_CONTROL_STATE"
            for row in control_path
        )
        if len(reference_path) != replayed_path_count:
            replay_errors.append(f"CONTROL path length parity failed: {event['event_id']}")
            continue
        if len(control_path) < len(trade):
            replay_errors.append(f"CONTROL monitor path coverage failed: {event['event_id']}")
            continue
        markers = _event_markers(control_path, event)
        outcome = _event_outcome(control_path, event, source)
        timeline = {
            "event_id": event["event_id"],
            "symbol": event["symbol"],
            "window_key": event["window_key"],
            "calendar": str(event["window_key"]).split(":", 1)[0],
            "breakout_direction": event["breakout_direction"],
            **markers,
            "truth_label": event["posthoc_label"],
            **outcome,
            "canonical_path": "31111-NEUTRAL CONTROL",
            "validation_split": "RESEARCH_VALIDATION_EXPOSED",
            "label_usage": "LABEL_ONLY",
        }
        timelines.append(timeline)
        unique_rows.append(
            {
                "event_id": event["event_id"],
                "symbol": event["symbol"],
                "window_key": event["window_key"],
                "calendar": timeline["calendar"],
                "breakout_direction": event["breakout_direction"],
                "D2_confirmation_time": markers["D2_confirmation_time"],
                "truth_label": event["posthoc_label"],
                "run_level_duplicate_count": event["run_level_duplicate_count"],
                "scenario_count": event["scenario_count"],
                "seed_count": event["seed_count"],
                "statistical_unit": "UNIQUE_MARKET_EVENT",
                "canonical_path": "31111-NEUTRAL CONTROL",
                "validation_split": "RESEARCH_VALIDATION_EXPOSED",
            }
        )
        paths[str(event["event_id"])] = control_path
        snapshots.extend(_snapshots(control_path, event, markers))
        anchor = _dt(markers["event_anchor_time"]) if markers["event_anchor_time"] else _dt(event["signal_time"]) - timedelta(minutes=60)
        end = _dt(outcome["event_end_time"]) if outcome["event_end_time"] else _dt(control_path[-1]["timestamp"])
        plotted = [row for row in control_path if anchor <= _dt(row["timestamp"]) <= end]
        for row in plotted:
            row["event_relative_minute"] = (_dt(row["timestamp"]) - _dt(event["signal_time"])).total_seconds() / 60.0
        _write_csv(
            output_dir / "event-paths" / f"{event['event_id']}.csv",
            plotted,
            EVENT_PATH_FIELDS,
        )

    leakage_errors = validate_no_future_leakage(snapshots)
    data_quality_status = "PASS_DATA_QUALITY" if not replay_errors and not leakage_errors and len(paths) == 12 else "FAIL_DATA_QUALITY"
    _write_json(
        output_dir / "data-quality.json",
        {
            "status": data_quality_status,
            "replay_errors": replay_errors,
            "leakage_errors": leakage_errors,
            "unique_event_paths": len(paths),
            "NO_L2_ORDERBOOK": True,
            "market_activity_source": "OHLCV quote_volume/base_volume/trade_count proxies; not order-book depth",
            "canonical_path": "31111-NEUTRAL CONTROL",
            "defense_paths_excluded": ["S0", "S1", "S2", "S3"],
            "control_aggregate_fallback_event_ids": fallback_event_ids,
            "control_aggregate_fallback_note": "One frozen v3.2 event had no admitted fill-level replay; constant-inventory CONTROL fallback is reproduced and fill-cascade fields are explicitly sparse.",
        },
    )
    if data_quality_status != "PASS_DATA_QUALITY":
        return {"conclusion": "FAIL_DATA_QUALITY", "parity": parity, "errors": [*replay_errors, *leakage_errors]}

    main_effects = feature_main_effects(snapshots)
    interactions = feature_interactions(snapshots, events)
    candidates, warnings_by_rule = evaluate_early_warning_rules(paths, events)
    promising = next((row for row in candidates if row["candidate_status"] == "PROMISING_EARLY_WARNING_HYPOTHESIS"), None)
    best = promising or min(candidates, key=lambda row: (int(row["FN"]), int(row["FP"]), -_f(row["mean_lead_time_vs_D2"])))
    best_warnings = warnings_by_rule[str(best["rule_id"])]
    robustness = robustness_rows(promising, paths, events)
    timeline_by_event = {str(row["event_id"]): row for row in timelines}
    classifications: list[dict[str, Any]] = []
    false_analysis: list[dict[str, Any]] = []
    true_analysis: list[dict[str, Any]] = []
    tail_summary: list[dict[str, Any]] = []

    for event in events:
        event_id = str(event["event_id"])
        path = paths[event_id]
        timeline = timeline_by_event[event_id]
        warning = best_warnings[event_id]
        lead = lead_time_minutes(str(warning["timestamp"]), str(event["signal_time"])) if warning else None
        before, remaining, fraction = _tail_remaining(path, warning, event)
        reasons = _false_reasons(event, path, timeline) if event["posthoc_label"] == "FALSE_BREAKOUT" else []
        mechanisms = _true_mechanisms(path, event) if event["posthoc_label"] == "TRUE_BREAKOUT" else []
        classification = {
            "event_id": event_id,
            "symbol": event["symbol"],
            "window_key": event["window_key"],
            "truth_label": event["posthoc_label"],
            "D2_prediction": "TRUE",
            "early_warning_prediction": str(warning is not None).upper(),
            "false_positive_reason": "|".join(reasons),
            "true_tail_mechanism": "|".join(mechanisms),
            "earliest_discriminative_point": warning["timestamp"] if warning else "NO_PRE_D2_RULE_SIGNAL",
            "tail_irreversibility_point": "NO_RELIABLE_IRREVERSIBILITY_POINT",
            "lead_time_minutes": lead if lead is not None else 0.0,
            "warning_time": warning["timestamp"] if warning else "",
            "D2_confirmation_time": event["signal_time"],
            "tail_event_time": timeline["tail_event_time"],
            "lead_vs_D2": lead if lead is not None else 0.0,
            "lead_vs_tail": lead_time_minutes(str(warning["timestamp"]), str(timeline["tail_event_time"])) if warning and timeline["tail_event_time"] else 0.0,
            "inventory_present_at_warning": warning.get("gross_inventory_notional", 0.0) if warning else 0.0,
            "future_tail_loss_after_warning": remaining,
            "tail_loss_before_warning": before,
            "tail_loss_after_warning": remaining,
            "fraction_tail_remaining_at_warning": fraction,
        }
        classifications.append(classification)
        if reasons:
            false_analysis.append(
                {
                    "event_id": event_id,
                    "symbol": event["symbol"],
                    "window_key": event["window_key"],
                    "recovery_time": timeline["event_end_time"],
                    "recovery_minutes_after_D2": lead_time_minutes(str(event["signal_time"]), str(timeline["event_end_time"])) if timeline["event_end_time"] else "",
                    "classification_tags": "|".join(reasons),
                    "self_healing_mechanism": "|".join(reasons),
                }
            )
        if mechanisms:
            marker_names = ["first_inventory_stress", "first_one_sided_fill_cluster", "first_range_break", "first_D2_precursor", "D2_confirmation_time"]
            true_analysis.append(
                {
                    "event_id": event_id,
                    "symbol": event["symbol"],
                    "window_key": event["window_key"],
                    **{name: timeline.get(name, "") for name in marker_names},
                    "first_abnormal_inventory_build": timeline.get("first_inventory_stress", ""),
                    "first_reversal_failure": next((row["timestamp"] for row in path if _dt(row["timestamp"]) <= _dt(event["signal_time"]) and _f(row.get("reversal_ratio_20m"), 1.0) <= 0.25), ""),
                    "first_directional_persistence": next((row["timestamp"] for row in path if _dt(row["timestamp"]) <= _dt(event["signal_time"]) and _f(row.get("directional_efficiency_ratio_20m")) >= 0.60), ""),
                    "first_volatility_expansion": next((row["timestamp"] for row in path if _dt(row["timestamp"]) <= _dt(event["signal_time"]) and _f(row.get("volatility_ratio_short_long")) >= 1.25), ""),
                    "first_grid_headroom_compression": next((row["timestamp"] for row in path if _dt(row["timestamp"]) <= _dt(event["signal_time"]) and _f(row.get("grid_headroom_remaining")) <= 2), ""),
                    "first_unrealized_loss_acceleration": next((row["timestamp"] for row in path if _dt(row["timestamp"]) <= _dt(event["signal_time"]) and _f(row.get("inventory_pnl_unrealized")) < 0), ""),
                    "tail_event_time": timeline["tail_event_time"],
                    "earliest_discriminative_point": classification["earliest_discriminative_point"],
                    "true_tail_mechanism": "|".join(mechanisms),
                }
            )
        t0 = _point_at_or_before(path, _dt(event["signal_time"])) or {}
        tail_summary.append(
            {
                "event_id": event_id,
                "symbol": event["symbol"],
                "truth_label": event["posthoc_label"],
                "formation_start": timeline["event_anchor_time"],
                "D2_confirmation_time": timeline["D2_confirmation_time"],
                "resolution_or_force_close": timeline["event_end_time"],
                "future_tail_loss": timeline["future_tail_loss"],
                "gross_inventory_at_D2": t0.get("gross_inventory_notional", 0.0),
                "inventory_utilization_at_D2": t0.get("gross_inventory_utilization", 0.0),
                "one_sided_fill_ratio_at_D2": t0.get("one_sided_fill_ratio_10m", 0.0),
                "reversal_ratio_at_D2": t0.get("reversal_ratio_20m", 0.0),
                "directional_efficiency_at_D2": t0.get("directional_efficiency_ratio_20m", 0.0),
                "volatility_ratio_at_D2": t0.get("volatility_ratio_short_long", 0.0),
                "grid_headroom_at_D2": t0.get("grid_headroom_remaining", 0.0),
                "minutes_to_force_close_at_D2": t0.get("minutes_to_force_close", 0.0),
                "tail_irreversibility_point": "NO_RELIABLE_IRREVERSIBILITY_POINT",
            }
        )

    symbol_rows = _symbol_breakdown(events, classifications)
    false_count = int(best["FP"])
    if promising:
        conclusion = "PASS_PROMISING_EARLY_WARNING_HYPOTHESIS_RESEARCH_ONLY"
    elif int(best["TP"]) < 2:
        conclusion = "REJECT_NO_PRE_CONFIRMATION_SEPARATION"
    elif false_count > 5:
        conclusion = "REJECT_FALSE_POSITIVE_NOT_REDUCED"
    else:
        conclusion = "FAIL_INSUFFICIENT_EVENT_SAMPLE"

    _write_json(output_dir / "event-definition.json", TRUTH_DEFINITION)
    _write_csv(output_dir / "unique-events.csv", unique_rows)
    _write_csv(output_dir / "event-timeline.csv", timelines)
    _write_csv(output_dir / "feature-snapshots.csv", snapshots)
    _write_csv(output_dir / "feature-main-effects.csv", main_effects)
    _write_csv(output_dir / "feature-interactions.csv", interactions)
    _write_csv(output_dir / "event-classification.csv", classifications)
    _write_csv(output_dir / "false-positive-analysis.csv", false_analysis)
    _write_csv(output_dir / "true-event-analysis.csv", true_analysis)
    _write_csv(output_dir / "tail-formation-summary.csv", tail_summary)
    _write_csv(output_dir / "early-warning-candidates.csv", candidates)
    _write_csv(output_dir / "robustness-check.csv", robustness)
    _write_csv(output_dir / "symbol-breakdown.csv", symbol_rows)
    feature_definition = {
        "feature_families": ["inventory_formation", "fill_cascade", "reversal_failure", "directional_persistence", "volatility_range", "market_activity", "grid_geometry", "session_calendar"],
        "checkpoints": [CHECKPOINT_LABELS[value] for value in CHECKPOINTS],
        "event_relative_checkpoints": ["first_inventory_stress", "first_one_sided_fill_cluster", "first_range_break", "first_D2_precursor", "D2_confirmation"],
        "early_warning_rules": [{key: value for key, value in rule.items() if key != "predicate"} for rule in EARLY_WARNING_RULES],
        "future_fields": sorted(LABEL_FIELDS),
    }
    source_paths = [
        ROOT / "data" / "backtests" / "semiconductor-v2.7" / f"{symbol}-1m.csv"
        for symbol in v32.SYMBOLS
    ] + [
        v32.OUTPUT_DIR / "breakout-events.csv",
        v34.V33_OUTPUT_DIR / "profile-results.csv",
        V34_OUTPUT_DIR / "run-manifest.json",
    ]
    manifest = {
        "protocol": "semiconductor-grid-inventory-tail-early-warning-v3.5",
        "branch": BRANCH,
        "commit": _git("rev-parse", "HEAD"),
        "base_commit": BASE_COMMIT,
        "source_data_hashes": {str(path.relative_to(ROOT)): _sha(path) for path in source_paths},
        "control_candidate_sha": v32.BASE_CANDIDATE_SHA,
        "v34_source_commit": BASE_COMMIT,
        "truth_definition_hash": _json_hash(TRUTH_DEFINITION),
        "feature_definition_hash": _json_hash(feature_definition),
        "unique_event_count": len(events),
        "true_event_count": sum(event["posthoc_label"] == "TRUE_BREAKOUT" for event in events),
        "false_event_count": sum(event["posthoc_label"] == "FALSE_BREAKOUT" for event in events),
        "symbols": list(v32.SYMBOLS),
        "scenarios": list(v32.SCENARIOS),
        "seeds": list(v32.SEEDS),
        "statistical_unit": "UNIQUE_MARKET_EVENT",
        "research_status": conclusion,
        "data_classification": "RESEARCH_VALIDATION_EXPOSED",
        "production_config_changed": False,
        "auto_entry_enabled": False,
        "economic_leverage": 1.0,
        "new_forward_oos_candidate": "NONE",
        "small_event_sample": True,
        "LOEO_status": "INSUFFICIENT_EVENT_SAMPLE_FOR_LOEO",
    }
    _write_json(output_dir / "run-manifest.json", manifest)
    (output_dir / "final-report.md").write_text(
        _final_report(parity, events, timelines, main_effects, candidates, classifications, robustness, conclusion),
        encoding="utf-8",
    )
    (output_dir / "tests.txt").write_text("PENDING_FINAL_VERIFICATION\n", encoding="utf-8")
    return {
        "conclusion": conclusion,
        "parity": parity,
        "unique_events": len(events),
        "true_events": sum(event["posthoc_label"] == "TRUE_BREAKOUT" for event in events),
        "false_events": sum(event["posthoc_label"] == "FALSE_BREAKOUT" for event in events),
        "best_rule": best,
        "top_features": _top_features(main_effects),
        "output_dir": str(output_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="QuietGrid v3.5 inventory tail formation research")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    result = run_research(output_dir=Path(args.output_dir))
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if result.get("conclusion") not in {"FAIL_DATA_QUALITY", "FAIL_EVENT_LABEL_PARITY", "FAIL_IMPLEMENTATION"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
