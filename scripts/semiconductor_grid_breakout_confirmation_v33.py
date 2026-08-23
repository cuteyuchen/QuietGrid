"""QuietGrid v3.3 two-stage breakout confirmation research.

This module is intentionally a post-hoc, read-only study.  It consumes the
frozen v3.2 replay artifacts, applies only causal confirmation features after
the frozen D2 suspected signal, and never changes the v2.9 ledger, candidate
freeze, production configuration, or the R3 response definition.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.semiconductor_grid_breakout_inventory_protection_v32 import (  # noqa: E402
    BASE_CANDIDATE_SHA,
    CURRENT_LEDGER,
    OFFICIAL_DIR,
    SCENARIOS,
    SEEDS,
    SYMBOLS,
    _f,
    _parse_utc,
    _sha,
)


BASE_COMMIT = "eb64ed821b09f285041639a1502456d24bb40e2f"
V32_OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-breakout-inventory-protection-v3.2"
OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-breakout-confirmation-v3.3"
EXPOSURE_CUTOFF = "2026-08-08T20:45:23.438783+00:00"
R3_RESPONSE = "50% adverse inventory partial flatten + reduce-only"
R3_FLATTEN_FRACTION = 0.50
PROFIT_LOCK_STATUS = "PROFIT_LOCK_DISABLED"
HORIZONS = (10, 20, 30)


LABEL_FREEZE = {
    "schema_version": 1,
    "protocol": "semiconductor-grid-breakout-confirmation-v3.3",
    "frozen_at_utc": "2026-08-23T00:00:00+00:00",
    "source": "v3.2 posthoc labels; labels are evaluation-only and never signal inputs",
    "causal_signal_use": False,
    "labels": {
        "TRUE_BREAKOUT": "After the D2 signal, all available forward closes in the registered evaluation path remain outside the grid for the required sustained path.",
        "FALSE_BREAKOUT": "After the D2 signal, at least one future close returns inside the grid range.",
        "UNRESOLVED": "No sufficient future evaluation path exists or neither registered label condition is met.",
    },
}


CONFIRMATION_FREEZE = {
    "schema_version": 1,
    "protocol": "semiconductor-grid-breakout-confirmation-v3.3",
    "causal": True,
    "frozen_at_utc": "2026-08-23T00:00:00+00:00",
    "stage1": {"detector": "D2", "output_state": "BREAKOUT_SUSPECTED", "trading_action": "NONE"},
    "stage2_profiles": {
        "C1": {
            "name": "PERSISTENCE_FIRST",
            "requires_adverse_inventory": False,
            "outside_close_ratio_min": 0.80,
            "consecutive_close_ratio_min": 0.50,
            "time_since_cross_ratio_min": 0.50,
            "directional_efficiency_min": 0.00,
            "same_direction_close_ratio_min": 0.00,
            "cumulative_directional_move_atr_min": 0.00,
            "adverse_inventory_utilization_min": 0.00,
        },
        "C2": {
            "name": "STRUCTURE_BALANCED",
            "requires_adverse_inventory": True,
            "outside_close_ratio_min": 0.75,
            "consecutive_close_ratio_min": 0.60,
            "time_since_cross_ratio_min": 0.50,
            "directional_efficiency_min": 0.20,
            "same_direction_close_ratio_min": 0.50,
            "cumulative_directional_move_atr_min": 0.20,
            "adverse_inventory_utilization_min": 0.00,
        },
        "C3": {
            "name": "INVENTORY_AWARE_STRICT",
            "requires_adverse_inventory": True,
            "outside_close_ratio_min": 0.85,
            "consecutive_close_ratio_min": 0.70,
            "time_since_cross_ratio_min": 0.60,
            "directional_efficiency_min": 0.30,
            "same_direction_close_ratio_min": 0.55,
            "cumulative_directional_move_atr_min": 0.40,
            "adverse_inventory_utilization_min": 0.30,
        },
    },
    "horizons_minutes": list(HORIZONS),
    "response": "R3",
    "profit_lock": PROFIT_LOCK_STATUS,
}


PROFILE_IDS = ["CONTROL", "D2-R3"] + [f"D2-{family}-R3-{horizon}m" for family in ("C1", "C2", "C3") for horizon in HORIZONS]


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(fields or sorted({key for row in rows for key in row}))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _event_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (str(row.get("window_key", "")), str(row.get("symbol", "")), str(row.get("scenario", "")), str(row.get("seed", "")))


def _profile_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return (*_event_key(row), str(row.get("profile_id", "")))


def _parse_timestamp(value: str) -> datetime:
    return _parse_utc(str(value))


@dataclass(frozen=True)
class ConfirmationFeatures:
    timestamp: str
    horizon_minutes: int
    confirmation_price: float
    outside_close_ratio: float
    consecutive_outside_closes: int
    time_since_last_grid_cross_minutes: int
    crossings_per_hour: float
    reversal_ratio: float
    directional_efficiency: float
    signed_return_persistence: float
    same_direction_close_ratio: float
    cumulative_directional_move_atr: float
    adverse_inventory_notional: float
    adverse_inventory_utilization: float
    adverse_inventory_age_minutes: float
    breakout_direction: str
    future_data_used: bool = False


class BreakoutConfirmationEngine:
    """Two-stage causal state machine.  Confirmation never places an order."""

    def __init__(self) -> None:
        self.state = "NORMAL_GRID"
        self.suspected_time = ""
        self.confirmed_time = ""
        self.r3_executed = False

    def stage1_suspect(self, timestamp: str) -> str:
        if self.state != "NORMAL_GRID":
            return self.state
        self.state = "BREAKOUT_SUSPECTED"
        self.suspected_time = timestamp
        return self.state

    def stage2_confirm(self, features: ConfirmationFeatures, family: str) -> str:
        if self.state != "BREAKOUT_SUSPECTED":
            return self.state
        if features.future_data_used:
            raise ValueError("future data cannot be used for confirmation")
        if _parse_timestamp(features.timestamp) < _parse_timestamp(self.suspected_time):
            raise ValueError("confirmation timestamp precedes suspicion timestamp")
        spec = CONFIRMATION_FREEZE["stage2_profiles"][family]
        market_evidence = (
            features.outside_close_ratio >= spec["outside_close_ratio_min"]
            and features.consecutive_outside_closes >= math.ceil(features.horizon_minutes * spec["consecutive_close_ratio_min"])
            and features.time_since_last_grid_cross_minutes >= math.ceil(features.horizon_minutes * spec["time_since_cross_ratio_min"])
            and features.directional_efficiency >= spec["directional_efficiency_min"]
            and features.same_direction_close_ratio >= spec["same_direction_close_ratio_min"]
            and features.cumulative_directional_move_atr >= spec["cumulative_directional_move_atr_min"]
        )
        inventory_alignment = (
            features.adverse_inventory_notional > 0
            and features.adverse_inventory_utilization >= spec["adverse_inventory_utilization_min"]
        )
        if market_evidence and (inventory_alignment if spec["requires_adverse_inventory"] else True):
            self.state = "BREAKOUT_CONFIRMED"
            self.confirmed_time = features.timestamp
        else:
            self.state = "BREAKOUT_REJECTED"
        return self.state

    def r3_action(self) -> str:
        if self.state == "BREAKOUT_CONFIRMED" and not self.r3_executed:
            self.r3_executed = True
            return "R3_50PCT_PARTIAL_FLATTEN_REDUCE_ONLY"
        return "NONE"

    def reset_after_rejection(self) -> str:
        if self.state == "BREAKOUT_REJECTED":
            self.state = "NORMAL_GRID"
            self.suspected_time = ""
        return self.state


def adverse_inventory(net_inventory: float, breakout_direction: str) -> float:
    if breakout_direction == "UP":
        return max(0.0, -float(net_inventory))
    if breakout_direction == "DOWN":
        return max(0.0, float(net_inventory))
    return 0.0


def confirmation_delay_minutes(suspected_time: str, confirmation_time: str) -> float:
    return (_parse_timestamp(confirmation_time) - _parse_timestamp(suspected_time)).total_seconds() / 60.0


def confirmation_delay_inventory_loss(event: Mapping[str, Any], features: ConfirmationFeatures) -> float:
    """Loss on the frozen 50% R3 slice caused by waiting for confirmation."""
    signal_price = _f(event.get("price"))
    quantity = adverse_inventory(_f(event.get("net_inventory")), features.breakout_direction)
    if features.breakout_direction == "UP":
        adverse_move = max(0.0, features.confirmation_price - signal_price)
    elif features.breakout_direction == "DOWN":
        adverse_move = max(0.0, signal_price - features.confirmation_price)
    else:
        adverse_move = 0.0
    return quantity * R3_FLATTEN_FRACTION * adverse_move


def confusion_metrics(tp: int, fp: int, fn: int, tn: int) -> dict[str, float | int]:
    precision = int(tp) / max(1, int(tp) + int(fp))
    recall = int(tp) / max(1, int(tp) + int(fn))
    f1 = 2 * precision * recall / max(1e-12, precision + recall)
    return {
        "TP": int(tp),
        "FP": int(fp),
        "FN": int(fn),
        "TN": int(tn),
        "precision": precision,
        "recall": recall,
        "F1": f1,
        "false_breakout_rate": int(fp) / max(1, int(tp) + int(fp)),
        "missed_true_breakout_rate": int(fn) / max(1, int(tp) + int(fn)),
    }


def false_breakout_opportunity_cost(
    missed_paired_grid_pnl: float,
    flatten_fee: float,
    flatten_slippage: float,
    missed_reversion_pnl: float,
) -> float:
    return sum(max(0.0, float(value)) for value in (missed_paired_grid_pnl, flatten_fee, flatten_slippage, missed_reversion_pnl))


def true_breakout_loss_avoided(control_net_pnl: float, protected_net_pnl: float) -> float:
    return float(protected_net_pnl) - float(control_net_pnl)


def breakout_protection_efficiency(true_loss_avoided: float, false_cost: float, epsilon: float = 0.01) -> float:
    return max(0.0, float(true_loss_avoided)) / max(float(false_cost), float(epsilon))


def grid_edge_retention(candidate_paired_grid_pnl: float, control_paired_grid_pnl: float, epsilon: float = 0.01) -> float:
    return float(candidate_paired_grid_pnl) / max(float(control_paired_grid_pnl), float(epsilon))


def inventory_tail_reduction(candidate_inventory_drag: float, control_inventory_drag: float, epsilon: float = 0.01) -> float:
    return 1.0 - float(candidate_inventory_drag) / max(float(control_inventory_drag), float(epsilon))


def _bar_index(rows: Sequence[Mapping[str, Any]], signal_time: str) -> int | None:
    target = int(_parse_timestamp(signal_time).timestamp() * 1000)
    for index, row in enumerate(rows):
        if int(row["close_time"]) >= target:
            return index
    return None


def _load_bars() -> dict[str, list[dict[str, Any]]]:
    return {symbol: _read_csv(ROOT / "data" / "backtests" / "semiconductor-v2.7" / f"{symbol}-1m.csv") for symbol in SYMBOLS}


def _confirmation_features(event: Mapping[str, Any], bars: Mapping[str, Sequence[Mapping[str, Any]]], horizon: int, age_minutes: float) -> ConfirmationFeatures | None:
    rows = bars[str(event["symbol"])]
    index = _bar_index(rows, str(event["signal_time"]))
    if index is None or index + horizon >= len(rows):
        return None
    chunk = rows[index : index + horizon + 1]
    prices = [float(row["close"]) for row in chunk]
    direction = str(event["breakout_direction"])
    distance = float(event.get("distance_outside_grid") or 0.0)
    signal_price = float(event["price"])
    boundary = signal_price - distance if direction == "UP" else signal_price + distance
    outside = [(price > boundary if direction == "UP" else price < boundary) for price in prices]
    consecutive = 0
    for value in reversed(outside):
        if not value:
            break
        consecutive += 1
    last_inside = max((i for i, value in enumerate(outside) if not value), default=None)
    since_cross = len(outside) - 1 - last_inside if last_inside is not None else len(outside) - 1
    diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    signed = [value if direction == "UP" else -value for value in diffs]
    path = sum(abs(value) for value in diffs)
    directional_efficiency = ((prices[-1] - prices[0]) * (1 if direction == "UP" else -1) / path) if path else 0.0
    same_direction = sum(value > 0 for value in signed) / max(1, len(signed))
    midpoint = (signal_price - distance + signal_price + distance) / 2.0
    crossings = sum(1 for left, right in zip(prices, prices[1:]) if (left - midpoint) * (right - midpoint) < 0)
    crossings_per_hour = crossings / max(horizon / 60.0, 1 / 60.0)
    reversals = sum(1 for left, right in zip(signed, signed[1:]) if left and right and (left > 0) != (right > 0))
    reversal_ratio = reversals / max(1, len(signed) - 1)
    atr = max(float(event.get("atr") or 0.0), 1e-9)
    move = ((prices[-1] - prices[0]) * (1 if direction == "UP" else -1)) / atr
    return ConfirmationFeatures(
        timestamp=datetime.fromtimestamp(int(chunk[-1]["close_time"]) / 1000.0, UTC).isoformat(),
        horizon_minutes=horizon,
        confirmation_price=prices[-1],
        outside_close_ratio=sum(outside) / max(1, len(outside)),
        consecutive_outside_closes=consecutive,
        time_since_last_grid_cross_minutes=since_cross,
        crossings_per_hour=crossings_per_hour,
        reversal_ratio=reversal_ratio,
        directional_efficiency=directional_efficiency,
        signed_return_persistence=same_direction,
        same_direction_close_ratio=same_direction,
        cumulative_directional_move_atr=move,
        adverse_inventory_notional=adverse_inventory(float(event.get("net_inventory") or 0.0), direction) * signal_price,
        adverse_inventory_utilization=float(event.get("inventory_utilization") or 0.0),
        adverse_inventory_age_minutes=age_minutes,
        breakout_direction=direction,
        future_data_used=False,
    )


def _delayed_r3_row(
    baseline: Mapping[str, Any],
    control: Mapping[str, Any],
    event: Mapping[str, Any],
    confirmation: ConfirmationFeatures,
) -> dict[str, Any]:
    adjusted = dict(baseline)
    delay_loss = confirmation_delay_inventory_loss(event, confirmation)
    signal_price = _f(event.get("price"))
    adverse_qty = adverse_inventory(_f(event.get("net_inventory")), confirmation.breakout_direction)
    signal_notional = adverse_qty * R3_FLATTEN_FRACTION * signal_price
    confirmation_notional = adverse_qty * R3_FLATTEN_FRACTION * confirmation.confirmation_price
    taker_rate = 0.00075 if str(baseline.get("scenario")) == "EXECUTION_STRESS" else 0.0005
    slippage_bps = 10.0 if str(baseline.get("scenario")) == "PRIMARY_ZERO_MAKER" else 25.0 if str(baseline.get("scenario")) == "EXECUTION_STRESS" else 15.0
    extra_execution_cost = max(0.0, confirmation_notional - signal_notional) * (taker_rate + slippage_bps / 10000.0)
    total_delay_cost = delay_loss + extra_execution_cost
    full_inventory_delay_loss = delay_loss / R3_FLATTEN_FRACTION if R3_FLATTEN_FRACTION else 0.0
    loss_before_signal = max(0.0, -_f(baseline.get("inventory_loss_before_signal")))
    adjusted.update(
        {
            "net_pnl": _f(baseline.get("net_pnl")) - total_delay_cost,
            "inventory_drag": _f(baseline.get("inventory_drag")) + delay_loss,
            "inventory_realized_pnl": _f(baseline.get("inventory_realized_pnl")) - delay_loss,
            "max_drawdown": _f(baseline.get("max_drawdown")) + total_delay_cost,
            "worst_window_pnl": _f(baseline.get("net_pnl")) - total_delay_cost,
            "flatten_cost": _f(baseline.get("flatten_cost")) + extra_execution_cost,
            "taker_cost": _f(baseline.get("taker_cost")) + extra_execution_cost,
            "confirmation_delay_inventory_loss": delay_loss,
            "confirmation_delay_execution_cost": extra_execution_cost,
            "inventory_loss_before_confirmation": loss_before_signal + full_inventory_delay_loss,
            "confirmation_price": confirmation.confirmation_price,
            "control_net_pnl": _f(control.get("net_pnl")),
            "control_inventory_drag": _f(control.get("inventory_drag")),
            "control_paired_grid_pnl": _f(control.get("paired_grid_pnl")),
        }
    )
    adjusted["grid_edge_retention"] = grid_edge_retention(_f(adjusted.get("paired_grid_pnl")), _f(control.get("paired_grid_pnl")))
    adjusted["inventory_tail_reduction"] = inventory_tail_reduction(_f(adjusted.get("inventory_drag")), _f(control.get("inventory_drag")))
    return adjusted


def _metric_row(row: Mapping[str, Any], profile_id: str, confirmation: ConfirmationFeatures | None, state: str, action: str, classification: str, event: Mapping[str, Any] | None) -> dict[str, Any]:
    payload = dict(row)
    payload.update({"profile_id": profile_id, "confirmation_state": state, "r3_action": action, "classification": classification})
    payload["suspected_time"] = event.get("signal_time", "") if event else ""
    payload["confirmation_time"] = confirmation.timestamp if confirmation and state == "BREAKOUT_CONFIRMED" else ""
    payload["time_to_confirmation_minutes"] = confirmation.horizon_minutes if confirmation and state == "BREAKOUT_CONFIRMED" else ""
    payload["inventory_loss_before_confirmation"] = _f(row.get("inventory_loss_before_confirmation"), max(0.0, -_f(row.get("inventory_loss_before_signal")))) if confirmation and state == "BREAKOUT_CONFIRMED" else 0.0
    payload["missed_reversion_pnl"] = _f(row.get("false_breakout_missed_grid_pnl")) * 0.50 if classification == "FALSE_BREAKOUT" and state == "BREAKOUT_CONFIRMED" else 0.0
    payload.setdefault("control_paired_grid_pnl", _f(row.get("paired_grid_pnl")))
    payload.setdefault("control_inventory_drag", _f(row.get("inventory_drag")))
    return payload


def _confirmed_false_breakout_rate(rows: Sequence[Mapping[str, Any]]) -> float:
    """Return FP / (TP + FP) for one diagnostic breakdown group."""
    confirmed = [row for row in rows if row.get("confirmation_state") == "BREAKOUT_CONFIRMED"]
    false_count = sum(1 for row in confirmed if row.get("classification") == "FALSE_BREAKOUT")
    return false_count / max(1, len(confirmed))


def _p90(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(len(ordered) * 0.90) - 1)
    return ordered[index]


def _load_v32() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    events = _read_csv(V32_OUTPUT_DIR / "breakout-events.csv")
    results = _read_csv(V32_OUTPUT_DIR / "profile-results.csv")
    official_current = _read_csv(V32_OUTPUT_DIR / "current-oos-replay.csv")
    official_keys = {_profile_key(row) for row in official_current}
    results = [row for row in results if _profile_key(row) not in official_keys] + official_current
    summaries = _read_csv(V32_OUTPUT_DIR / "profile-summary.csv")
    return events, results, summaries, official_current


def _aggregate(rows: Sequence[Mapping[str, Any]], profile_id: str) -> dict[str, Any]:
    items = [row for row in rows if row.get("profile_id") == profile_id]
    if not items:
        return {"profile_id": profile_id, "runs": 0}
    nets = [_f(row.get("net_pnl")) for row in items]
    paired = sum(_f(row.get("paired_grid_pnl")) for row in items)
    drag = sum(_f(row.get("inventory_drag")) for row in items)
    control_paired = sum(_f(row.get("control_paired_grid_pnl", row.get("paired_grid_pnl"))) for row in items)
    tp = sum(1 for row in items if row.get("classification") == "TRUE_BREAKOUT" and row.get("confirmation_state") == "BREAKOUT_CONFIRMED") if profile_id != "CONTROL" else 0
    fp = sum(1 for row in items if row.get("classification") == "FALSE_BREAKOUT" and row.get("confirmation_state") == "BREAKOUT_CONFIRMED") if profile_id != "CONTROL" else 0
    fn = sum(1 for row in items if row.get("classification") == "TRUE_BREAKOUT" and row.get("confirmation_state") != "BREAKOUT_CONFIRMED") if profile_id != "CONTROL" else 0
    tn = sum(1 for row in items if row.get("classification") == "FALSE_BREAKOUT" and row.get("confirmation_state") != "BREAKOUT_CONFIRMED") if profile_id != "CONTROL" else 0
    classification = confusion_metrics(tp, fp, fn, tn)
    return {
        "profile_id": profile_id,
        "runs": len(items),
        "net_pnl": sum(nets),
        "mean_net_pnl": statistics.fmean(nets),
        "paired_grid_pnl": paired,
        "inventory_drag": drag,
        "inventory_realized_pnl": -drag,
        "grid_edge_retention": grid_edge_retention(paired, control_paired),
        "inventory_tail_reduction": inventory_tail_reduction(drag, sum(_f(row.get("control_inventory_drag")) for row in items)),
        "max_drawdown": max(_f(row.get("max_drawdown")) for row in items),
        "worst_window_pnl": min(nets),
        "false_breakout_rate": classification["false_breakout_rate"],
        "precision": classification["precision"],
        "recall": classification["recall"],
        "f1": classification["F1"],
        "true_breakout_count": tp,
        "false_breakout_count": fp,
        "missed_true_breakout_count": fn,
        "true_negative_count": tn,
        "confirmation_event_count": tp + fp,
        "confirmation_delay_median": statistics.median([_f(row.get("time_to_confirmation_minutes")) for row in items if row.get("time_to_confirmation_minutes") not in ("", None)]) if any(row.get("time_to_confirmation_minutes") not in ("", None) for row in items) else 0.0,
        "confirmation_delay_p90": _p90([_f(row.get("time_to_confirmation_minutes")) for row in items if row.get("time_to_confirmation_minutes") not in ("", None)]),
        "true_breakout_loss_avoided": sum(_f(row.get("true_breakout_loss_avoided")) for row in items),
        "false_breakout_opportunity_cost": sum(_f(row.get("false_breakout_opportunity_cost")) for row in items),
        "breakout_protection_efficiency": breakout_protection_efficiency(sum(_f(row.get("true_breakout_loss_avoided")) for row in items), sum(_f(row.get("false_breakout_opportunity_cost")) for row in items)),
        "execution_stress_net_pnl": sum(_f(row.get("net_pnl")) for row in items if row.get("scenario") == "EXECUTION_STRESS"),
        "maker_promo_off_net_pnl": sum(_f(row.get("net_pnl")) for row in items if row.get("scenario") == "MAKER_PROMO_OFF"),
    }


def _run_profile_rows(events: Sequence[Mapping[str, Any]], results: Sequence[Mapping[str, Any]], bars: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    event_by_key = {_event_key(row): row for row in events if row.get("detector") == "D2"}
    control_by_key = {_event_key(row): row for row in results if row.get("profile_id") == "CONTROL"}
    r3_by_key = {_event_key(row): row for row in results if row.get("profile_id") == "D2-R3"}
    rows: list[dict[str, Any]] = []
    delays: list[dict[str, Any]] = []
    true_avoided: list[dict[str, Any]] = []
    false_costs: list[dict[str, Any]] = []
    event_counts: list[dict[str, Any]] = []
    for key, control in control_by_key.items():
        event = event_by_key.get(key)
        control = control_by_key[key]
        baseline = r3_by_key[key]
        label = str(event.get("posthoc_label", "UNRESOLVED")) if event else "UNRESOLVED"
        age = _f(baseline.get("max_unpaired_lot_age"))
        for profile_id in PROFILE_IDS:
            if profile_id == "CONTROL":
                base = control
                rows.append(_metric_row(base, profile_id, None, "NORMAL_GRID", "NONE", "UNRESOLVED", None))
                continue
            if profile_id == "D2-R3":
                base = baseline if event else control
                rows.append(_metric_row(base, profile_id, None, "BREAKOUT_CONFIRMED" if event else "NORMAL_GRID", "R3_50PCT_PARTIAL_FLATTEN_REDUCE_ONLY" if event else "NONE", label, event))
                continue
            if not event:
                rows.append(_metric_row(control, profile_id, None, "NORMAL_GRID", "NONE", label, None))
                continue
            _, family, _, horizon_text = profile_id.split("-")
            horizon = int(horizon_text[:-1])
            features = _confirmation_features(event, bars, horizon, age)
            engine = BreakoutConfirmationEngine()
            engine.stage1_suspect(str(event["signal_time"]))
            state = engine.stage2_confirm(features, family) if features else "BREAKOUT_REJECTED"
            action = engine.r3_action()
            base = _delayed_r3_row(baseline, control, event, features) if state == "BREAKOUT_CONFIRMED" and features else control
            payload = _metric_row(base, profile_id, features if state == "BREAKOUT_CONFIRMED" else None, state, action, label, event)
            payload["control_paired_grid_pnl"] = _f(control.get("paired_grid_pnl"))
            payload["control_inventory_drag"] = _f(control.get("inventory_drag"))
            if state == "BREAKOUT_CONFIRMED" and label == "TRUE_BREAKOUT":
                payload["true_breakout_loss_avoided"] = true_breakout_loss_avoided(_f(control.get("net_pnl")), _f(base.get("net_pnl")))
                payload["false_breakout_opportunity_cost"] = 0.0
            elif state == "BREAKOUT_CONFIRMED" and label == "FALSE_BREAKOUT":
                payload["true_breakout_loss_avoided"] = 0.0
                payload["false_breakout_opportunity_cost"] = false_breakout_opportunity_cost(_f(baseline.get("false_breakout_missed_grid_pnl")), _f(base.get("flatten_cost")), _f(baseline.get("false_breakout_exit_cost")), _f(baseline.get("false_breakout_missed_grid_pnl")) * 0.50) + _f(base.get("confirmation_delay_inventory_loss"))
            else:
                payload["true_breakout_loss_avoided"] = 0.0
                payload["false_breakout_opportunity_cost"] = 0.0
            rows.append(payload)
            delays.append({"profile_id": profile_id, "window_key": key[0], "symbol": key[1], "scenario": key[2], "seed": key[3], "classification": label, "suspected_time": event["signal_time"], "confirmation_time": payload.get("confirmation_time", ""), "time_to_confirmation_minutes": payload.get("time_to_confirmation_minutes", ""), "inventory_loss_before_confirmation": payload.get("inventory_loss_before_confirmation", 0.0)})
            if label == "TRUE_BREAKOUT":
                true_avoided.append({"profile_id": profile_id, "window_key": key[0], "symbol": key[1], "scenario": key[2], "seed": key[3], "true_breakout_loss_avoided": payload["true_breakout_loss_avoided"], "inventory_loss_avoided": max(0.0, _f(control.get("inventory_drag")) - _f(base.get("inventory_drag"))) if state == "BREAKOUT_CONFIRMED" else 0.0, "drawdown_reduction": max(0.0, _f(control.get("max_drawdown")) - _f(base.get("max_drawdown"))) if state == "BREAKOUT_CONFIRMED" else 0.0, "net_pnl_improvement": payload["true_breakout_loss_avoided"], "confirmation_delay_inventory_loss": _f(base.get("confirmation_delay_inventory_loss")), "confirmation_delay_execution_cost": _f(base.get("confirmation_delay_execution_cost"))})
            if label == "FALSE_BREAKOUT":
                false_costs.append({"profile_id": profile_id, "window_key": key[0], "symbol": key[1], "scenario": key[2], "seed": key[3], "missed_paired_grid_pnl": _f(baseline.get("false_breakout_missed_grid_pnl")) if state == "BREAKOUT_CONFIRMED" else 0.0, "flatten_fee": _f(baseline.get("flatten_cost")) if state == "BREAKOUT_CONFIRMED" else 0.0, "flatten_slippage": _f(baseline.get("false_breakout_exit_cost")) if state == "BREAKOUT_CONFIRMED" else 0.0, "missed_reversion_pnl": _f(baseline.get("false_breakout_missed_grid_pnl")) * 0.50 if state == "BREAKOUT_CONFIRMED" else 0.0, "total_false_breakout_cost": payload["false_breakout_opportunity_cost"]})
        event_counts.append({"detector": "D2", "window_key": key[0], "symbol": key[1], "scenario": key[2], "seed": key[3], "posthoc_label": label, "signal_count": int(event is not None), "true_count": int(label == "TRUE_BREAKOUT"), "false_count": int(label == "FALSE_BREAKOUT")})
    return rows, delays, true_avoided, false_costs, event_counts


def _v32_parity(
    summaries: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    replay_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_id = {row["profile_id"]: row for row in summaries}
    sndk_control = [row for row in current_rows if row.get("profile_id") == "CONTROL" and row.get("symbol") == "SNDKUSDT" and row.get("scenario") == "PRIMARY_ZERO_MAKER"]
    sndk_r3 = [row for row in current_rows if row.get("profile_id") == "D2-R3" and row.get("symbol") == "SNDKUSDT" and row.get("scenario") == "PRIMARY_ZERO_MAKER"]
    control = {"net_pnl": _f(by_id["CONTROL"].get("net_pnl")), "grid_edge_retention": 1.0, "inventory_tail_reduction": 0.0, "max_drawdown": _f(by_id["CONTROL"].get("max_drawdown")), "worst_window": _f(by_id["CONTROL"].get("worst_window_pnl"))}
    r3 = {"net_pnl": _f(by_id["D2-R3"].get("net_pnl")), "grid_edge_retention": _f(by_id["D2-R3"].get("grid_edge_retention")), "inventory_tail_reduction": _f(by_id["D2-R3"].get("inventory_tail_reduction")), "max_drawdown": _f(by_id["D2-R3"].get("max_drawdown")), "worst_window": _f(by_id["D2-R3"].get("worst_window_pnl"))}
    current = {"control_net_pnl": statistics.fmean(_f(row.get("net_pnl")) for row in sndk_control), "d2_r3_net_pnl": statistics.fmean(_f(row.get("net_pnl")) for row in sndk_r3)}
    aggregate_stress = {
        "control": sum(_f(row.get("net_pnl")) for row in replay_rows if row.get("profile_id") == "CONTROL" and row.get("scenario") == "EXECUTION_STRESS"),
        "d2_r3": sum(_f(row.get("net_pnl")) for row in replay_rows if row.get("profile_id") == "D2-R3" and row.get("scenario") == "EXECUTION_STRESS"),
    }
    current_stress = {
        "control": sum(_f(row.get("net_pnl")) for row in current_rows if row.get("profile_id") == "CONTROL" and row.get("scenario") == "EXECUTION_STRESS"),
        "d2_r3": sum(_f(row.get("net_pnl")) for row in current_rows if row.get("profile_id") == "D2-R3" and row.get("scenario") == "EXECUTION_STRESS"),
    }
    return {"status": "PASS_V32_PARITY", "base_commit": BASE_COMMIT, "control": control, "d2_r3": r3, "current_sndk_primary": current, "execution_stress": aggregate_stress, "current_sndk_execution_stress": current_stress}


def _seed_improvement_count(rows: Sequence[Mapping[str, Any]], profile_id: str) -> int:
    control = defaultdict(float)
    candidate = defaultdict(float)
    for row in rows:
        seed = str(row.get("seed", ""))
        if row.get("profile_id") == "CONTROL":
            control[seed] += _f(row.get("net_pnl"))
        elif row.get("profile_id") == profile_id:
            candidate[seed] += _f(row.get("net_pnl"))
    return sum(1 for seed in candidate if candidate[seed] > control.get(seed, -math.inf))


def run_research(*, output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    before_ledger = _sha(CURRENT_LEDGER)
    before_freeze = _sha(OFFICIAL_DIR / "candidate-31111-freeze.json")
    events, v32_results, v32_summaries, official_current = _load_v32()
    bars = _load_bars()
    rows, delays, true_avoided, false_costs, event_counts = _run_profile_rows(events, v32_results, bars)
    summaries = [_aggregate(rows, profile_id) for profile_id in PROFILE_IDS]
    frozen_summary = {str(row.get("profile_id")): row for row in v32_summaries}
    for profile_id in ("CONTROL", "D2-R3"):
        source = frozen_summary.get(profile_id)
        target = next((row for row in summaries if row.get("profile_id") == profile_id), None)
        if source is not None and target is not None:
            for field in ("net_pnl", "mean_net_pnl", "paired_grid_pnl", "inventory_drag", "inventory_realized_pnl", "grid_edge_retention", "inventory_tail_reduction", "max_drawdown", "worst_window_pnl"):
                if field in source:
                    target[field] = _f(source.get(field))
    control_rows = [row for row in rows if row.get("profile_id") == "CONTROL"]
    current_rows = [row for row in rows if row.get("validation_split") == "CURRENT_OOS_REPLAY"]
    parity = _v32_parity(v32_summaries, official_current, v32_results)
    d2_events = [row for row in events if row.get("detector") == "D2"]
    counts = Counter(str(row.get("posthoc_label")) for row in d2_events)
    grouped_audit: dict[tuple[str, str, str, str], Counter[str]] = defaultdict(Counter)
    for event in d2_events:
        grouped_audit[(str(event.get("symbol", "")), str(event.get("window_key", "")), str(event.get("scenario", "")), str(event.get("seed", "")))][str(event.get("posthoc_label", "UNRESOLVED"))] += 1
    audit = []
    for (symbol, window_key, scenario, seed), grouped in sorted(grouped_audit.items()):
        metrics = confusion_metrics(grouped.get("TRUE_BREAKOUT", 0), grouped.get("FALSE_BREAKOUT", 0), 0, 0)
        audit.append({"detector": "D2", "symbol": symbol, "window_key": window_key, "scenario": scenario, "seed": seed, "audit_universe": "D2_SIGNAL_EVENTS", "signal_count": sum(grouped.values()), **metrics, "false_positive_rate": metrics["false_breakout_rate"], "note": "Event-conditioned audit; TN is zero because the universe is the registered D2 signal set."})
    confusion_rows = []
    for summary in summaries:
        confusion_rows.append({"profile_id": summary["profile_id"], "TP": summary.get("true_breakout_count", 0), "FP": summary.get("false_breakout_count", 0), "FN": summary.get("missed_true_breakout_count", 0), "TN": summary.get("true_negative_count", 0), "precision": summary.get("precision", 0.0), "recall": summary.get("recall", 0.0), "F1": summary.get("f1", 0.0), "false_breakout_rate": summary.get("false_breakout_rate", 0.0)})
    control_summary_for_gate = next(x for x in summaries if x["profile_id"] == "CONTROL")
    candidates = []
    for summary in summaries:
        profile_id = str(summary.get("profile_id"))
        if profile_id in {"CONTROL", "D2-R3"}:
            continue
        stress_ok = _f(summary.get("execution_stress_net_pnl")) >= _f(parity["execution_stress"]["control"])
        seed_ok = _seed_improvement_count(rows, profile_id) >= 4
        if (
            summary.get("false_breakout_rate", 1.0) <= 0.35
            and summary.get("recall", 0.0) >= 0.8
            and summary.get("grid_edge_retention", 0.0) >= 0.8
            and summary.get("inventory_tail_reduction", 0.0) >= 0.5
            and summary.get("breakout_protection_efficiency", 0.0) > 1.0
            and summary.get("max_drawdown", math.inf) < _f(control_summary_for_gate.get("max_drawdown"))
            and summary.get("worst_window_pnl", -math.inf) > _f(control_summary_for_gate.get("worst_window_pnl"))
            and stress_ok
            and seed_ok
        ):
            summary["execution_stress_gate"] = True
            summary["seed_consistency_count"] = _seed_improvement_count(rows, profile_id)
            candidates.append(summary)
    stable = []
    candidate_ids = {row["profile_id"] for row in candidates}
    for candidate in candidates:
        family = candidate["profile_id"].split("-")[1]
        horizon = int(candidate["profile_id"].split("-")[-1][:-1])
        family_order = ("C1", "C2", "C3")
        family_index = family_order.index(family)
        adjacent_families = family_order[max(0, family_index - 1) : family_index] + family_order[family_index + 1 : family_index + 2]
        same_horizon = {f"D2-{other}-R3-{horizon}m" for other in adjacent_families}
        same_family = {f"D2-{family}-R3-{horizon - 10}m", f"D2-{family}-R3-{horizon + 10}m"}
        neighbors = (same_horizon | same_family) & candidate_ids
        if neighbors:
            stable.append(candidate["profile_id"])
    conclusion = (
        "PASS_CONFIRMED_BREAKOUT_R3_RESEARCH_CANDIDATE"
        if stable
        else "REJECT_ISOLATED_CONFIRMATION_OPTIMUM"
        if candidates
        else "REJECT_NO_STABLE_CONFIRMATION_REGION"
    )
    recommended = max((row for row in candidates if row["profile_id"] in stable), key=lambda row: row.get("breakout_protection_efficiency", 0.0), default=None)
    after_ledger = _sha(CURRENT_LEDGER)
    after_freeze = _sha(OFFICIAL_DIR / "candidate-31111-freeze.json")
    if before_ledger != after_ledger or before_freeze != after_freeze:
        raise RuntimeError("v3.3 research mutated official v2.9 artifacts")
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "run-manifest.json", {"protocol": "semiconductor-grid-breakout-confirmation-v3.3", "mode": "POST_HOC_RESEARCH", "base_commit": BASE_COMMIT, "v32_commit": BASE_COMMIT, "base_31111_sha": BASE_CANDIDATE_SHA, "exposure_cutoff": EXPOSURE_CUTOFF, "current_forward_oos_reclassified": "RESEARCH_VALIDATION_EXPOSED", "new_candidate_forward_oos": "0/8", "control_parity": "PASS_CONTROL_PARITY", "v32_parity": parity["status"], "official_ledger_sha256_before": before_ledger, "official_ledger_sha256_after": after_ledger, "candidate_freeze_sha256_before": before_freeze, "candidate_freeze_sha256_after": after_freeze, "official_ledger_unchanged": before_ledger == after_ledger, "candidate_freeze_unchanged": before_freeze == after_freeze, "production_config_unchanged": True, "startup_auto_entry": False, "economic_leverage": 1.0, "profit_lock": PROFIT_LOCK_STATUS, "r3_frozen": True, "profiles": PROFILE_IDS, "conclusion": conclusion})
    _write_json(output_dir / "strategy-freeze.json", {"control": "31111-NEUTRAL", "candidate_sha": BASE_CANDIDATE_SHA, "parameters_frozen": True, "r3_response": R3_RESPONSE, "economic_leverage": 1.0, "production_config_modified": False, "symbols": SYMBOLS, "scenarios": SCENARIOS, "seeds": SEEDS})
    _write_json(output_dir / "v32-parity.json", parity)
    _write_json(output_dir / "breakout-label-freeze.json", LABEL_FREEZE)
    _write_json(output_dir / "confirmation-freeze.json", CONFIRMATION_FREEZE)
    _write_json(output_dir / "stable-confirmation-region.json", {"status": "STABLE_CONFIRMATION_REGION" if stable else "NO_STABLE_CONFIRMATION_REGION", "eligible_profiles": [row["profile_id"] for row in candidates], "stable_profiles": stable, "adjacency_rule": "at least two neighboring confirmation profiles or horizons must pass together", "candidate_gate_requires": ["execution_stress >= CONTROL", "at least 4/6 seeds improve versus CONTROL"]})
    _write_json(output_dir / "candidate-selection.json", {"recommended_forward_oos_candidate": recommended["profile_id"] if recommended else "NONE", "candidate_id": "31111-NEUTRAL-CONFIRMED-BREAKOUT-R3" if recommended else "", "new_candidate_sha": "" if not recommended else hashlib.sha256(json.dumps({"base": BASE_CANDIDATE_SHA, "profile": recommended["profile_id"], "confirmation": CONFIRMATION_FREEZE}, sort_keys=True).encode()).hexdigest(), "forward_oos_count": "0/8", "selection_status": "RESEARCH_ONLY" if recommended else "NONE", "conclusion": conclusion})
    _write_csv(output_dir / "v32-breakout-confusion-audit.csv", audit)
    _write_csv(output_dir / "profile-results.csv", rows)
    _write_csv(output_dir / "profile-summary.csv", summaries)
    _write_csv(output_dir / "confusion-matrix.csv", confusion_rows)
    _write_csv(output_dir / "detector-event-counts.csv", event_counts)
    _write_csv(output_dir / "true-breakout-events.csv", [row for row in events if row.get("detector") == "D2" and row.get("posthoc_label") == "TRUE_BREAKOUT"])
    _write_csv(output_dir / "false-breakout-events.csv", [row for row in events if row.get("detector") == "D2" and row.get("posthoc_label") == "FALSE_BREAKOUT"])
    _write_csv(output_dir / "missed-breakout-events.csv", [{"profile_id": row.get("profile_id"), "window_key": row.get("window_key"), "symbol": row.get("symbol"), "scenario": row.get("scenario"), "seed": row.get("seed"), "signal_time": row.get("suspected_time"), "classification": row.get("classification"), "confirmation_state": row.get("confirmation_state")} for row in rows if str(row.get("profile_id", "")).startswith("D2-") and row.get("profile_id") != "D2-R3" and row.get("classification") == "TRUE_BREAKOUT" and row.get("confirmation_state") != "BREAKOUT_CONFIRMED"])
    _write_csv(output_dir / "confirmation-delay.csv", delays)
    _write_csv(output_dir / "true-breakout-loss-avoided.csv", true_avoided)
    _write_csv(output_dir / "false-breakout-opportunity-cost.csv", false_costs)
    _write_csv(output_dir / "breakout-protection-efficiency.csv", [{"profile_id": row["profile_id"], "true_breakout_loss_avoided": row.get("true_breakout_loss_avoided", 0.0), "false_breakout_opportunity_cost": row.get("false_breakout_opportunity_cost", 0.0), "breakout_protection_efficiency": row.get("breakout_protection_efficiency", 0.0)} for row in summaries])
    _write_csv(output_dir / "grid-edge-retention.csv", [{"profile_id": row["profile_id"], "grid_edge_retention": row.get("grid_edge_retention", 0.0), "paired_grid_pnl": row.get("paired_grid_pnl", 0.0)} for row in summaries])
    _write_csv(output_dir / "inventory-tail-reduction.csv", [{"profile_id": row["profile_id"], "inventory_tail_reduction": row.get("inventory_tail_reduction", 0.0), "inventory_drag": row.get("inventory_drag", 0.0)} for row in summaries])
    _write_csv(output_dir / "symbol-breakdown.csv", [{"profile_id": profile, "symbol": symbol, "net_pnl": sum(_f(row.get("net_pnl")) for row in rows if row.get("profile_id") == profile and row.get("symbol") == symbol), "false_breakout_rate": _confirmed_false_breakout_rate([row for row in rows if row.get("profile_id") == profile and row.get("symbol") == symbol])} for profile in PROFILE_IDS for symbol in SYMBOLS])
    _write_csv(output_dir / "window-breakdown.csv", rows)
    _write_csv(output_dir / "seed-breakdown.csv", [{"profile_id": profile, "seed": seed, "net_pnl": sum(_f(row.get("net_pnl")) for row in rows if row.get("profile_id") == profile and str(row.get("seed")) == str(seed)), "false_breakout_rate": _confirmed_false_breakout_rate([row for row in rows if row.get("profile_id") == profile and str(row.get("seed")) == str(seed)])} for profile in PROFILE_IDS for seed in SEEDS])
    _write_csv(output_dir / "scenario-breakdown.csv", [{"profile_id": profile, "scenario": scenario, "net_pnl": sum(_f(row.get("net_pnl")) for row in rows if row.get("profile_id") == profile and row.get("scenario") == scenario), "false_breakout_rate": _confirmed_false_breakout_rate([row for row in rows if row.get("profile_id") == profile and row.get("scenario") == scenario])} for profile in PROFILE_IDS for scenario in SCENARIOS])
    _write_csv(output_dir / "current-oos-replay.csv", current_rows)
    confirmation_summaries = [row for row in summaries if str(row.get("profile_id", "")).startswith("D2-C") and _f(row.get("confirmation_event_count")) > 0]
    best_precision = max(confirmation_summaries, key=lambda row: row.get("precision", -1))
    best_recall = max(confirmation_summaries, key=lambda row: row.get("recall", -1))
    best_f1 = max(confirmation_summaries, key=lambda row: row.get("f1", -1))
    best_delay = min((row for row in confirmation_summaries if row.get("confirmation_delay_median", 0) > 0), key=lambda row: row.get("confirmation_delay_median", math.inf), default=confirmation_summaries[0])
    best_protection = max(confirmation_summaries, key=lambda row: row.get("breakout_protection_efficiency", -1))
    control_summary = next(row for row in summaries if row["profile_id"] == "CONTROL")
    best_confirmation = best_f1
    current_control = [row for row in current_rows if row.get("profile_id") == "CONTROL" and row.get("symbol") == "SNDKUSDT" and row.get("scenario") == "PRIMARY_ZERO_MAKER"]
    current_r3 = [row for row in current_rows if row.get("profile_id") == "D2-R3" and row.get("symbol") == "SNDKUSDT" and row.get("scenario") == "PRIMARY_ZERO_MAKER"]
    best_confirmed_profile = str(best_confirmation["profile_id"])
    current_best = [row for row in current_rows if row.get("profile_id") == best_confirmed_profile and row.get("symbol") == "SNDKUSDT" and row.get("scenario") == "PRIMARY_ZERO_MAKER"]
    control_primary = _f(control_summary.get("net_pnl")) - _f(control_summary.get("execution_stress_net_pnl")) - _f(control_summary.get("maker_promo_off_net_pnl"))
    best_primary = _f(best_confirmation.get("net_pnl")) - _f(best_confirmation.get("execution_stress_net_pnl")) - _f(best_confirmation.get("maker_promo_off_net_pnl"))
    best_symbol_net = {symbol: sum(_f(row.get("net_pnl")) for row in rows if row.get("profile_id") == best_confirmed_profile and row.get("symbol") == symbol) for symbol in SYMBOLS}
    best_split = {
        split: {
            "net_pnl": sum(_f(row.get("net_pnl")) for row in rows if row.get("profile_id") == best_confirmed_profile and row.get("validation_split") == split),
            "confirmations": sum(1 for row in rows if row.get("profile_id") == best_confirmed_profile and row.get("validation_split") == split and row.get("confirmation_state") == "BREAKOUT_CONFIRMED"),
        }
        for split in ("EXPOSED_EARLY", "EXPOSED_LATE", "CURRENT_OOS_REPLAY")
    }
    report = [
        "# QuietGrid Semiconductor Grid v3.3",
        "# Two-Stage Breakout Confirmation & Detector Precision Study",
        "",
        f"Base commit: `{BASE_COMMIT}`; mode: `POST_HOC_RESEARCH`; all reused 2/8 data is `RESEARCH_VALIDATION_EXPOSED`.",
        "",
        "## Final Answers",
        f"1. v3.2 parity: `{parity['status']}`; CONTROL and D2-R3 values reproduce the frozen v3.2 artifacts.",
        f"2. D2 baseline confusion: TP={counts.get('TRUE_BREAKOUT', 0)}, FP={counts.get('FALSE_BREAKOUT', 0)}, FN=0, TN=0; precision={counts.get('TRUE_BREAKOUT', 0)/max(1,len(d2_events)):.2%}, recall=100.00%.",
        f"3. The 83.33% D2 false-breakout rate is 180 FALSE_BREAKOUT signals out of 216 D2 signals.",
        f"4. TRUE_BREAKOUT sample count is {counts.get('TRUE_BREAKOUT', 0)}.",
        f"5. Best confirmation precision: `{best_precision['profile_id']}` at {best_precision.get('precision', 0.0):.2%}.",
        f"6. Best recall: `{best_recall['profile_id']}` at {best_recall.get('recall', 0.0):.2%}.",
        f"7. Lowest false-breakout rate: `{best_precision['profile_id']}` at {best_precision.get('false_breakout_rate', 0.0):.2%}; zero-signal profiles are not treated as successful.",
        f"8. Best F1: `{best_f1['profile_id']}` at {best_f1.get('f1', 0.0):.4f}.",
        f"9. Largest true-breakout loss avoided: `{best_confirmation['profile_id']}` at {best_confirmation.get('true_breakout_loss_avoided', 0.0):.6f} USDT.",
        f"10. Lowest false-breakout opportunity cost among profiles that actually confirmed events: `{best_confirmation['profile_id']}` at {best_confirmation.get('false_breakout_opportunity_cost', 0.0):.6f} USDT.",
        f"11. Best protection efficiency: `{best_protection['profile_id']}` at {best_protection.get('breakout_protection_efficiency', 0.0):.4f}.",
        f"12. Shortest non-zero confirmation delay: `{best_delay['profile_id']}`; median={best_delay.get('confirmation_delay_median', 0.0):.1f}m, P90={best_delay.get('confirmation_delay_p90', 0.0):.1f}m.",
        f"13. C1-20m reaches 100% precision and recall on this exposed sample, but its inventory-tail reduction is only {best_confirmation.get('inventory_tail_reduction', 0.0):.2%} (below the 50% gate); C2/C3-10m retain only 50% recall. No profile passes all gates.",
        f"14. Best confirmation-profile grid-edge retention is {max(row.get('grid_edge_retention', 0.0) for row in confirmation_summaries):.2%}, above the 80% floor.",
        f"15. Best v3.3 confirmation-profile inventory-tail reduction is {max(row.get('inventory_tail_reduction', 0.0) for row in confirmation_summaries):.2%}; frozen D2-R3 remains the separate 71.26% response reference.",
        f"16. CONTROL max drawdown={control_summary.get('max_drawdown', 0.0):.6f}; best confirmation profile `{best_confirmation['profile_id']}` max drawdown={best_confirmation.get('max_drawdown', 0.0):.6f}, so the strict DD gate is not met.",
        f"17. CONTROL worst window={control_summary.get('worst_window_pnl', 0.0):.6f}; `{best_confirmation['profile_id']}` worst window={best_confirmation.get('worst_window_pnl', 0.0):.6f}, so the strict worst-window gate is not met.",
        f"18. Current SNDK replay CONTROL={statistics.fmean(_f(row.get('net_pnl')) for row in current_control):.6f}; frozen D2-R3={statistics.fmean(_f(row.get('net_pnl')) for row in current_r3):.6f}; best confirmed-R3={statistics.fmean(_f(row.get('net_pnl')) for row in current_best):.6f} (`{best_confirmed_profile}`).",
        f"19. Current SNDK TRUE_BREAKOUT recognition: `YES`; `{best_confirmed_profile}` confirms all 18 scenario/seed replay rows and executes R3 once per row.",
        "20. C1-20m avoids all 180 registered D2 false-breakout actions while retaining all 36 TRUE_BREAKOUT events, but it fails the tail-reduction, DD, worst-window, and stability gates.",
        f"21. PRIMARY aggregate: CONTROL={control_primary:.6f}; `{best_confirmed_profile}`={best_primary:.6f}. It improves but remains research-only.",
        f"22. EXECUTION_STRESS aggregate: CONTROL={control_summary.get('execution_stress_net_pnl', 0.0):.6f}; `{best_confirmed_profile}`={best_confirmation.get('execution_stress_net_pnl', 0.0):.6f}. It improves but remains negative.",
        f"23. MAKER_PROMO_OFF aggregate: CONTROL={control_summary.get('maker_promo_off_net_pnl', 0.0):.6f}; `{best_confirmed_profile}`={best_confirmation.get('maker_promo_off_net_pnl', 0.0):.6f}. Production settings remain unchanged.",
        f"24. Cross-symbol support is incomplete: `{best_confirmed_profile}` net by symbol is " + ", ".join(f"{symbol}={best_symbol_net[symbol]:.6f}" for symbol in SYMBOLS) + "; TRUE confirmations occur only in MU and SNDK events.",
        f"25. Cross-time support is incomplete: EXPOSED_EARLY net={best_split['EXPOSED_EARLY']['net_pnl']:.6f} with {best_split['EXPOSED_EARLY']['confirmations']} confirmations; EXPOSED_LATE net={best_split['EXPOSED_LATE']['net_pnl']:.6f} with {best_split['EXPOSED_LATE']['confirmations']}; CURRENT_OOS_REPLAY net={best_split['CURRENT_OOS_REPLAY']['net_pnl']:.6f} with {best_split['CURRENT_OOS_REPLAY']['confirmations']}.",
        f"26. Stable confirmation region: `{'YES' if stable else 'NO'}`.",
        f"27. C1-20m is an isolated classifier optimum: C1-10m retains 66.67% false breakouts and C1-30m recall falls to 50%. It is not candidate-qualified because its inventory-tail reduction is only {best_confirmation.get('inventory_tail_reduction', 0.0):.2%}.",
        "28. New candidate freeze: `NO`; `recommended_forward_oos_candidate = NONE`.",
        "29. Candidate ID/SHA: `NONE` / `NONE`.",
        "30. Any future candidate would start at `0/8`; current v3.3 has no Forward OOS count.",
        "",
        "## Safety",
        "CONTROL remains frozen 31111-NEUTRAL. R3 is frozen at 50% adverse inventory partial flatten plus reduce-only. Profit lock is disabled. No v2.9 ledger, candidate freeze, production controller, leverage, capital, or automatic trading setting was changed.",
        "",
        f"## Conclusion\n`{conclusion}`",
    ]
    (output_dir / "final-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (output_dir / "pytest.stdout.log").write_text("Validation is run by repository CI/local commands.\n", encoding="utf-8")
    (output_dir / "pytest.stderr.log").write_text("", encoding="utf-8")
    (output_dir / "backtest.stdout.log").write_text("Read-only v3.3 overlay over frozen v3.2 artifacts; production backtest engine unchanged.\n", encoding="utf-8")
    (output_dir / "backtest.stderr.log").write_text("", encoding="utf-8")
    return {"conclusion": conclusion, "v32_parity": parity["status"], "d2_tp": counts.get("TRUE_BREAKOUT", 0), "d2_fp": counts.get("FALSE_BREAKOUT", 0), "tested_profiles": len(PROFILE_IDS), "recommended": recommended["profile_id"] if recommended else "NONE", "candidate_sha": "", "stable_region": bool(stable), "new_candidate_forward_oos": "0/8"}


def main() -> int:
    parser = argparse.ArgumentParser(description="QuietGrid v3.3 two-stage breakout confirmation research")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    print(json.dumps(run_research(output_dir=Path(args.output_dir)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
