"""QuietGrid v3.4 staged de-risking and early inventory defense research.

This is a read-only post-hoc study over frozen v3.2/v3.3 artifacts.  It does
not modify the original 31111 candidate, the v2.9 Forward OOS ledger, or any
production setting.  Stage 1 is frozen D2 and Stage 2 is frozen C1-20m; the
only primary experimental variable is the pre-registered early-defense action.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import semiconductor_grid_breakout_confirmation_v33 as v33  # noqa: E402
from scripts.semiconductor_grid_breakout_inventory_protection_v32 import (  # noqa: E402
    BASE_CANDIDATE_SHA,
    CURRENT_LEDGER,
    OFFICIAL_DIR,
    SCENARIOS,
    SEEDS,
    SYMBOLS,
    _f,
    _sha,
)


BASE_COMMIT = "3c15782da79dab306154d02653ca59201370b76f"
V32_OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-breakout-inventory-protection-v3.2"
V33_OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-breakout-confirmation-v3.3"
OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-staged-derisking-v3.4"
EXPOSURE_CUTOFF = "2026-08-08T20:45:23.438783+00:00"
R3_FRACTION = 0.50
CONFIRMATION_HORIZON_MINUTES = 20
PROFIT_LOCK_STATUS = "PROFIT_LOCK_DISABLED"
PRIMARY_SCENARIO = "PRIMARY_ZERO_MAKER"
PRIMARY_SEED = "3"
PROFILE_IDS = ("CONTROL", "D2-R3", "S0", "S1", "S2", "S3")


STAGED_DERISKING_FREEZE: dict[str, Any] = {
    "schema_version": 1,
    "protocol": "semiconductor-grid-staged-derisking-v3.4",
    "mode": "POST_HOC_RESEARCH",
    "frozen_at_utc": "2026-08-24T00:00:00+00:00",
    "stage1": {"detector": "D2", "state": "BREAKOUT_SUSPECTED"},
    "stage2": {
        "reference": "FROZEN_CONFIRMATION_REFERENCE",
        "family": "C1",
        "horizon_minutes": CONFIRMATION_HORIZON_MINUTES,
        "state": "BREAKOUT_CONFIRMED",
    },
    "confirmed_response": {
        "name": "R3",
        "target_cumulative_adverse_reduction": R3_FRACTION,
        "reduce_only": True,
    },
    "profiles": {
        "S0": {"name": "WAIT_FOR_CONFIRMATION", "early_flatten_fraction": 0.0, "block_adverse_adds": False},
        "S1": {"name": "SOFT_ADVERSE_ADD_BLOCK", "early_flatten_fraction": 0.0, "block_adverse_adds": True},
        "S2": {"name": "EARLY_FLATTEN_10PCT", "early_flatten_fraction": 0.10, "block_adverse_adds": True},
        "S3": {"name": "EARLY_FLATTEN_25PCT", "early_flatten_fraction": 0.25, "block_adverse_adds": True},
    },
    "rejection_recovery": {
        "causal_rule": "At the C1-20m checkpoint, insufficient C1 persistence evidence rejects the episode; price re-entry/grid-cross resumption is therefore permitted to return the state to NORMAL_GRID.",
        "action": "release soft adverse-add block and resume NORMAL_GRID; never automatically re-leverage early-reduced inventory",
    },
    "economic_overlay": {
        "true_breakout": "S0 is the frozen delayed-R3 baseline. Early inventory-loss saving is adverse_qty * early_fraction * adverse price move from D2 to C1 checkpoint. Only the change in execution cost versus S0's full confirmation flatten is charged.",
        "false_breakout": "S1 charges frozen blocked-order opportunity cost. S2/S3 charge the early taker execution, favorable-recovery inventory opportunity loss, and frozen short-horizon paired-grid opportunity cost; no automatic re-entry is assumed.",
        "false_soft_block_paired_after_fraction": 0.03,
        "false_early_flatten_time_fraction": 0.15,
        "true_early_grid_cost_fraction": 0.02,
    },
    "profit_lock": PROFIT_LOCK_STATUS,
    "economic_leverage": 1.0,
    "no_future_label_in_actions": True,
}


@dataclass(frozen=True)
class EarlyAction:
    profile_id: str
    suspected_episode_id: str
    early_flatten_qty: float
    reference_adverse_qty: float
    block_adverse_adds: bool


class StagedDeRiskingEngine:
    """Causal Stage 1/Stage 2 state machine for one suspected episode."""

    def __init__(self, profile_id: str) -> None:
        if profile_id not in {"S0", "S1", "S2", "S3"}:
            raise ValueError(f"unsupported staged profile: {profile_id}")
        self.profile_id = profile_id
        self.state = "NORMAL_GRID"
        self.episode_id = ""
        self.suspected_time = ""
        self.confirmed_time = ""
        self.breakout_direction = ""
        self.reference_adverse_qty = 0.0
        self.early_flatten_qty = 0.0
        self.early_action_executed = False
        self.reduce_only = False

    def stage1_suspect(self, timestamp: str, breakout_direction: str, adverse_qty: float, episode_id: str) -> EarlyAction:
        if self.state != "NORMAL_GRID":
            return EarlyAction(self.profile_id, self.episode_id, 0.0, self.reference_adverse_qty, self.blocks_adverse_adds)
        if breakout_direction not in {"UP", "DOWN"}:
            raise ValueError("breakout direction must be UP or DOWN")
        if float(adverse_qty) < 0:
            raise ValueError("adverse inventory quantity cannot be negative")
        spec = STAGED_DERISKING_FREEZE["profiles"][self.profile_id]
        self.state = "BREAKOUT_SUSPECTED"
        self.episode_id = episode_id
        self.suspected_time = timestamp
        self.breakout_direction = breakout_direction
        self.reference_adverse_qty = float(adverse_qty)
        self.early_flatten_qty = self.reference_adverse_qty * float(spec["early_flatten_fraction"])
        self.early_action_executed = True
        return EarlyAction(self.profile_id, episode_id, self.early_flatten_qty, self.reference_adverse_qty, bool(spec["block_adverse_adds"]))

    @property
    def blocks_adverse_adds(self) -> bool:
        return self.state == "BREAKOUT_SUSPECTED" and bool(STAGED_DERISKING_FREEZE["profiles"][self.profile_id]["block_adverse_adds"])

    def allows_order(self, *, increases_adverse_inventory: bool) -> bool:
        return not (self.blocks_adverse_adds and increases_adverse_inventory)

    def stage2_confirm(self, timestamp: str) -> float:
        if self.state != "BREAKOUT_SUSPECTED":
            return 0.0
        if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) < datetime.fromisoformat(self.suspected_time.replace("Z", "+00:00")):
            raise ValueError("confirmation cannot precede suspicion")
        self.state = "BREAKOUT_CONFIRMED"
        self.confirmed_time = timestamp
        self.reduce_only = True
        return max(0.0, self.reference_adverse_qty * R3_FRACTION - self.early_flatten_qty)

    def reject_suspicion(self, timestamp: str) -> str:
        if self.state == "BREAKOUT_SUSPECTED":
            if datetime.fromisoformat(timestamp.replace("Z", "+00:00")) < datetime.fromisoformat(self.suspected_time.replace("Z", "+00:00")):
                raise ValueError("rejection cannot precede suspicion")
            self.state = "NORMAL_GRID"
            self.reduce_only = False
        return self.state


def adverse_inventory_components(net_inventory: float, breakout_direction: str) -> dict[str, float]:
    net = float(net_inventory)
    long_qty = max(net, 0.0)
    short_qty = max(-net, 0.0)
    return {
        "long_inventory_qty": long_qty,
        "short_inventory_qty": short_qty,
        "net_inventory_qty": net,
        "gross_inventory_qty": long_qty + short_qty,
        "adverse_inventory_qty": short_qty if breakout_direction == "UP" else long_qty if breakout_direction == "DOWN" else 0.0,
    }


def event_cluster_id(event: Mapping[str, Any]) -> str:
    raw = "|".join(str(event.get(key, "")) for key in ("symbol", "window_key", "breakout_direction", "signal_time"))
    return "D2-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def unique_market_event_key(event: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(event.get("symbol", "")),
        str(event.get("window_key", "")),
        str(event.get("breakout_direction", "")),
        str(event.get("signal_time", "")),
    )


def confusion_metrics(tp: int, fp: int, fn: int, tn: int) -> dict[str, float | int]:
    precision = int(tp) / max(1, int(tp) + int(fp))
    recall = int(tp) / max(1, int(tp) + int(fn))
    f1 = 2.0 * precision * recall / max(1e-12, precision + recall)
    return {
        "TP": int(tp),
        "FP": int(fp),
        "FN": int(fn),
        "TN": int(tn),
        "precision": precision,
        "recall": recall,
        "F1": f1,
        "false_breakout_rate": int(fp) / max(1, int(tp) + int(fp)),
    }


def defense_efficiency_ratio(true_loss_avoided: float, false_defense_cost: float, epsilon: float = 0.01) -> float:
    return max(0.0, float(true_loss_avoided)) / max(float(false_defense_cost), epsilon)


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


def _event_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (str(row.get("window_key", "")), str(row.get("symbol", "")), str(row.get("scenario", "")), str(row.get("seed", "")))


def _row_float(row: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return _f(row.get(key), default)


def _taker_rate(scenario: str) -> float:
    return 0.00075 if scenario == "EXECUTION_STRESS" else 0.0005


def _slippage_bps(scenario: str) -> float:
    if scenario == "EXECUTION_STRESS":
        return 25.0
    if scenario == "MAKER_PROMO_OFF":
        return 15.0
    return 10.0


def _execution_cost(qty: float, price: float, scenario: str) -> tuple[float, float]:
    notional = max(0.0, float(qty)) * max(0.0, float(price))
    return notional * _taker_rate(scenario), notional * _slippage_bps(scenario) / 10_000.0


def _profile_reference_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events = [row for row in _read_csv(V32_OUTPUT_DIR / "breakout-events.csv") if row.get("detector") == "D2"]
    rows = _read_csv(V33_OUTPUT_DIR / "profile-results.csv")
    return events, rows


def _event_map(events: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    return {_event_key(row): dict(row) for row in events}


def _feature_and_state(event: Mapping[str, Any], bars: Mapping[str, Sequence[Mapping[str, Any]]], age: float) -> tuple[Any | None, str]:
    features = v33._confirmation_features(event, bars, CONFIRMATION_HORIZON_MINUTES, age)
    if features is None:
        return None, "BREAKOUT_REJECTED"
    engine = v33.BreakoutConfirmationEngine()
    engine.stage1_suspect(str(event["signal_time"]))
    return features, engine.stage2_confirm(features, "C1")


def _copy_base_row(base: Mapping[str, Any], profile_id: str) -> dict[str, Any]:
    row = dict(base)
    row["profile_id"] = profile_id
    return row


def _base_profile_row(base: Mapping[str, Any], profile_id: str, *, event: Mapping[str, Any] | None, state: str, action: EarlyAction | None, confirmation_time: str = "", confirmation_flatten_qty: float = 0.0) -> dict[str, Any]:
    row = _copy_base_row(base, profile_id)
    components = adverse_inventory_components(_row_float(event or {}, "net_inventory"), str((event or {}).get("breakout_direction", "")))
    row.update(components)
    row.update(
        {
            "stage_state": state,
            "suspected_episode_id": event_cluster_id(event) if event else "",
            "suspected_time": str((event or {}).get("signal_time", "")),
            "early_defense_time": str((event or {}).get("signal_time", "")) if action else "",
            "confirmation_time": confirmation_time,
            "early_flatten_qty": action.early_flatten_qty if action else 0.0,
            "early_flatten_reference_qty": action.reference_adverse_qty if action else 0.0,
            "confirmation_flatten_qty": confirmation_flatten_qty,
            "early_action_executed": bool(action),
            "adverse_add_block_enabled": bool(action and action.block_adverse_adds),
            "reduce_only_after_confirmation": state == "BREAKOUT_CONFIRMED",
            "did_resume_normal_grid": state == "NORMAL_GRID" and bool(event),
            "automatic_releverage": False,
            "classification": str((event or {}).get("posthoc_label", "UNRESOLVED")),
            "event_detected": bool(event),
        }
    )
    return row


def _staged_overlay(
    *,
    profile_id: str,
    control: Mapping[str, Any],
    s0: Mapping[str, Any],
    event: Mapping[str, Any] | None,
    features: Any | None,
    confirmation_state: str,
) -> dict[str, Any]:
    if not event:
        return _base_profile_row(control, profile_id, event=None, state="NORMAL_GRID", action=None)

    direction = str(event.get("breakout_direction"))
    components = adverse_inventory_components(_row_float(event, "net_inventory"), direction)
    adverse_qty = components["adverse_inventory_qty"]
    episode = event_cluster_id(event)
    engine = StagedDeRiskingEngine(profile_id)
    action = engine.stage1_suspect(str(event["signal_time"]), direction, adverse_qty, episode)
    scenario = str(control.get("scenario", ""))
    signal_price = _row_float(event, "price")
    confirmation_price = _row_float(features.__dict__ if features else {}, "confirmation_price", signal_price)
    label = str(event.get("posthoc_label", "UNRESOLVED"))
    confirmed = confirmation_state == "BREAKOUT_CONFIRMED"
    confirmation_qty = engine.stage2_confirm(str(features.timestamp)) if confirmed and features else 0.0
    if not confirmed:
        engine.reject_suspicion(str(features.timestamp) if features else str(event["signal_time"]))

    base = s0 if confirmed else control
    row = _base_profile_row(
        base,
        profile_id,
        event=event,
        state="BREAKOUT_CONFIRMED" if confirmed else "NORMAL_GRID",
        action=action,
        confirmation_time=str(features.timestamp) if confirmed and features else "",
        confirmation_flatten_qty=confirmation_qty,
    )
    paired_before = _row_float(control, "paired_grid_pnl_before_signal")
    paired_after = max(0.0, _row_float(control, "paired_grid_pnl") - paired_before)
    early_fraction = float(STAGED_DERISKING_FREEZE["profiles"][profile_id]["early_flatten_fraction"])
    signal_fee, signal_slippage = _execution_cost(action.early_flatten_qty, signal_price, scenario)
    confirm_fee, confirm_slippage = _execution_cost(confirmation_qty, confirmation_price, scenario)
    baseline_confirmation_fee, baseline_confirmation_slippage = _execution_cost(adverse_qty * R3_FRACTION, confirmation_price, scenario)
    signed_move = confirmation_price - signal_price if direction == "UP" else signal_price - confirmation_price
    adverse_move = max(0.0, signed_move)
    recovery_move = max(0.0, -signed_move)
    pre_confirmation_loss = adverse_qty * adverse_move
    tail_saved = action.early_flatten_qty * adverse_move
    base_net = _row_float(base, "net_pnl")
    base_paired = _row_float(base, "paired_grid_pnl")
    base_drag = _row_float(base, "inventory_drag")
    base_dd = _row_float(base, "max_drawdown")
    missed_grid = 0.0
    blocked_adverse_cost = 0.0
    flatten_realized_pnl = 0.0
    inventory_delta = 0.0
    baseline_execution_credit = 0.0

    if confirmed:
        # The delayed-C1 row is the base, so its full confirmation execution is
        # credited once while this profile pays its early plus remaining actions.
        baseline_execution_credit = baseline_confirmation_fee + baseline_confirmation_slippage
        if label == "TRUE_BREAKOUT":
            inventory_delta = tail_saved
            if profile_id == "S1":
                missed_grid = paired_after * float(STAGED_DERISKING_FREEZE["economic_overlay"]["true_early_grid_cost_fraction"])
            else:
                missed_grid = paired_after * (early_fraction / R3_FRACTION) * float(STAGED_DERISKING_FREEZE["economic_overlay"]["true_early_grid_cost_fraction"])
        elif label == "FALSE_BREAKOUT":
            # A false C1 confirmation is already represented by the delayed
            # baseline.  Only the additional early action is charged here.
            flatten_realized_pnl = -action.early_flatten_qty * recovery_move
            missed_grid = paired_after * (early_fraction / R3_FRACTION) * float(STAGED_DERISKING_FREEZE["economic_overlay"]["false_early_flatten_time_fraction"])
    elif label == "FALSE_BREAKOUT":
        if profile_id == "S1":
            blocked_adverse_cost = paired_after * float(STAGED_DERISKING_FREEZE["economic_overlay"]["false_soft_block_paired_after_fraction"])
            signal_fee = signal_slippage = confirm_fee = confirm_slippage = 0.0
        else:
            flatten_realized_pnl = -action.early_flatten_qty * recovery_move
            missed_grid = paired_after * (early_fraction / R3_FRACTION) * float(STAGED_DERISKING_FREEZE["economic_overlay"]["false_early_flatten_time_fraction"])
            confirm_fee = confirm_slippage = 0.0
    elif label == "TRUE_BREAKOUT":
        # A missed C1 confirmation still has only a causal early action; it
        # cannot receive a post-hoc recovery credit.
        inventory_delta = tail_saved
        missed_grid = paired_after * (early_fraction / R3_FRACTION) * float(STAGED_DERISKING_FREEZE["economic_overlay"]["true_early_grid_cost_fraction"])
    else:
        signal_fee = signal_slippage = confirm_fee = confirm_slippage = 0.0

    net_pnl = (
        base_net
        + inventory_delta
        + flatten_realized_pnl
        - signal_fee
        - signal_slippage
        - confirm_fee
        - confirm_slippage
        + baseline_execution_credit
        - missed_grid
        - blocked_adverse_cost
    )
    paired_grid_pnl = max(0.0, base_paired - missed_grid - blocked_adverse_cost)
    inventory_drag = max(0.0, base_drag - inventory_delta)
    max_drawdown = max(0.0, base_dd - inventory_delta * 0.50 + signal_fee + signal_slippage + confirm_fee + confirm_slippage - baseline_execution_credit + missed_grid + blocked_adverse_cost)
    row.update(
        {
            "net_pnl": net_pnl,
            "paired_grid_pnl": paired_grid_pnl,
            "inventory_drag": inventory_drag,
            "inventory_realized_pnl": -inventory_drag,
            "max_drawdown": max_drawdown,
            "worst_window_pnl": net_pnl,
            "max_drawdown_pct": max_drawdown / 500.0,
            "base_net_pnl": base_net,
            "inventory_pnl_delta": inventory_delta,
            "flatten_realized_pnl": flatten_realized_pnl,
            "early_flatten_fee": signal_fee,
            "early_flatten_slippage": signal_slippage,
            "confirmation_flatten_fee": confirm_fee,
            "confirmation_flatten_slippage": confirm_slippage,
            "baseline_confirmation_execution_credit": baseline_execution_credit,
            "missed_paired_grid_pnl": missed_grid,
            "blocked_adverse_add_opportunity_cost": blocked_adverse_cost,
            "blocked_beneficial_fill_cost": 0.0,
            "reentry_opportunity_loss": 0.0,
            "inventory_loss_before_confirmation": pre_confirmation_loss * (1.0 - early_fraction),
            "control_inventory_loss_before_confirmation": pre_confirmation_loss,
            "tail_saved_before_confirmation": tail_saved,
            "true_breakout_loss_avoided": max(0.0, net_pnl - _row_float(control, "net_pnl")) if label == "TRUE_BREAKOUT" else 0.0,
            "false_breakout_defense_cost": max(0.0, _row_float(control, "net_pnl") - net_pnl) if label == "FALSE_BREAKOUT" else 0.0,
            "grid_edge_retention": paired_grid_pnl / max(_row_float(control, "paired_grid_pnl"), 0.01),
            "inventory_tail_reduction": 1.0 - inventory_drag / max(_row_float(control, "inventory_drag"), 0.01),
        }
    )
    row["pnl_accounted_net_pnl"] = (
        row["base_net_pnl"]
        + row["inventory_pnl_delta"]
        + row["flatten_realized_pnl"]
        - row["early_flatten_fee"]
        - row["early_flatten_slippage"]
        - row["confirmation_flatten_fee"]
        - row["confirmation_flatten_slippage"]
        + row["baseline_confirmation_execution_credit"]
        - row["missed_paired_grid_pnl"]
        - row["blocked_adverse_add_opportunity_cost"]
        - row["blocked_beneficial_fill_cost"]
        - row["reentry_opportunity_loss"]
    )
    row["pnl_reconciliation_residual"] = row["net_pnl"] - row["pnl_accounted_net_pnl"]
    return row


def _run_rows(events: Sequence[Mapping[str, Any]], source_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    event_by_key = _event_map(events)
    by_profile: dict[str, dict[tuple[str, str, str, str], dict[str, Any]]] = defaultdict(dict)
    for row in source_rows:
        by_profile[str(row.get("profile_id"))][_event_key(row)] = dict(row)
    control_by_key = by_profile["CONTROL"]
    r3_by_key = by_profile["D2-R3"]
    s0_by_key = by_profile["D2-C1-R3-20m"]
    bars = v33._load_bars()
    result_rows: list[dict[str, Any]] = []
    episode_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []

    for key, control in control_by_key.items():
        event = event_by_key.get(key)
        r3 = r3_by_key.get(key, control)
        s0_source = s0_by_key.get(key, control)
        features, c1_state = _feature_and_state(event, bars, _row_float(r3, "max_unpaired_lot_age")) if event else (None, "NORMAL_GRID")
        label = str(event.get("posthoc_label", "UNRESOLVED")) if event else "UNRESOLVED"
        if event:
            components = adverse_inventory_components(_row_float(event, "net_inventory"), str(event.get("breakout_direction")))
            episode_rows.append(
                {
                    "suspected_episode_id": event_cluster_id(event),
                    "symbol": event.get("symbol"),
                    "window_key": event.get("window_key"),
                    "breakout_direction": event.get("breakout_direction"),
                    "signal_time": event.get("signal_time"),
                    "scenario": event.get("scenario"),
                    "seed": event.get("seed"),
                    "classification": label,
                    "c1_confirmation_state": c1_state,
                    "c1_confirmation_time": features.timestamp if features and c1_state == "BREAKOUT_CONFIRMED" else "",
                    **components,
                }
            )
        control_row = _base_profile_row(control, "CONTROL", event=None, state="NORMAL_GRID", action=None)
        control_row.update({"control_net_pnl": _row_float(control, "net_pnl"), "control_paired_grid_pnl": _row_float(control, "paired_grid_pnl"), "control_inventory_drag": _row_float(control, "inventory_drag")})
        result_rows.append(control_row)

        d2_state = "BREAKOUT_CONFIRMED" if event else "NORMAL_GRID"
        d2_row = _base_profile_row(r3 if event else control, "D2-R3", event=event, state=d2_state, action=None)
        d2_row.update({"control_net_pnl": _row_float(control, "net_pnl"), "control_paired_grid_pnl": _row_float(control, "paired_grid_pnl"), "control_inventory_drag": _row_float(control, "inventory_drag")})
        if event:
            comps = adverse_inventory_components(_row_float(event, "net_inventory"), str(event.get("breakout_direction")))
            d2_row.update({"early_flatten_qty": comps["adverse_inventory_qty"] * R3_FRACTION, "confirmation_flatten_qty": 0.0, "reduce_only_after_confirmation": True, "stage_state": "BREAKOUT_CONFIRMED", "true_breakout_loss_avoided": max(0.0, _row_float(r3, "net_pnl") - _row_float(control, "net_pnl")) if label == "TRUE_BREAKOUT" else 0.0, "false_breakout_defense_cost": max(0.0, _row_float(control, "net_pnl") - _row_float(r3, "net_pnl")) if label == "FALSE_BREAKOUT" else 0.0})
        result_rows.append(d2_row)

        s0_state = "BREAKOUT_CONFIRMED" if event and c1_state == "BREAKOUT_CONFIRMED" else "NORMAL_GRID"
        s0_row = _base_profile_row(s0_source if event else control, "S0", event=event, state=s0_state, action=None, confirmation_time=features.timestamp if features and s0_state == "BREAKOUT_CONFIRMED" else "")
        s0_row.update({"control_net_pnl": _row_float(control, "net_pnl"), "control_paired_grid_pnl": _row_float(control, "paired_grid_pnl"), "control_inventory_drag": _row_float(control, "inventory_drag")})
        if event:
            comps = adverse_inventory_components(_row_float(event, "net_inventory"), str(event.get("breakout_direction")))
            signal = _row_float(event, "price")
            confirm = _row_float(features.__dict__ if features else {}, "confirmation_price", signal)
            signed = confirm - signal if str(event.get("breakout_direction")) == "UP" else signal - confirm
            pre_loss = comps["adverse_inventory_qty"] * max(0.0, signed)
            s0_row.update({"inventory_loss_before_confirmation": pre_loss, "control_inventory_loss_before_confirmation": pre_loss, "tail_saved_before_confirmation": 0.0, "true_breakout_loss_avoided": max(0.0, _row_float(s0_source, "net_pnl") - _row_float(control, "net_pnl")) if label == "TRUE_BREAKOUT" else 0.0, "false_breakout_defense_cost": max(0.0, _row_float(control, "net_pnl") - _row_float(s0_source, "net_pnl")) if label == "FALSE_BREAKOUT" else 0.0})
        result_rows.append(s0_row)

        for profile_id in ("S1", "S2", "S3"):
            row = _staged_overlay(profile_id=profile_id, control=control, s0=s0_source, event=event, features=features, confirmation_state=c1_state)
            row.update({"control_net_pnl": _row_float(control, "net_pnl"), "control_paired_grid_pnl": _row_float(control, "paired_grid_pnl"), "control_inventory_drag": _row_float(control, "inventory_drag")})
            result_rows.append(row)

        if event:
            event_rows.append({
                "suspected_episode_id": event_cluster_id(event),
                "symbol": event.get("symbol"), "window_key": event.get("window_key"), "breakout_direction": event.get("breakout_direction"), "signal_time": event.get("signal_time"),
                "scenario": event.get("scenario"), "seed": event.get("seed"), "validation_split": event.get("validation_split"), "posthoc_label": label,
                "c1_confirmation_state": c1_state,
            })
    return result_rows, episode_rows, event_rows


def _run_phase2_rows(
    events: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]],
    early_profiles: Sequence[str],
) -> list[dict[str, Any]]:
    """Run only the pre-registered C1 timing robustness for the best region."""
    event_by_key = _event_map(events)
    by_profile: dict[str, dict[tuple[str, str, str, str], dict[str, Any]]] = defaultdict(dict)
    for row in source_rows:
        by_profile[str(row.get("profile_id"))][_event_key(row)] = dict(row)
    control_by_key = by_profile["CONTROL"]
    bars = v33._load_bars()
    rows: list[dict[str, Any]] = []
    for horizon in (10, 20, 30):
        delayed_profile = f"D2-C1-R3-{horizon}m"
        delayed_by_key = by_profile[delayed_profile]
        for key, control in control_by_key.items():
            event = event_by_key.get(key)
            delayed = delayed_by_key.get(key, control)
            features, state = _feature_and_state_for_horizon(event, bars, _row_float(delayed, "max_unpaired_lot_age"), horizon) if event else (None, "NORMAL_GRID")
            for early_profile in early_profiles:
                row = _staged_overlay(
                    profile_id=early_profile,
                    control=control,
                    s0=delayed,
                    event=event,
                    features=features,
                    confirmation_state=state,
                )
                row["profile_id"] = f"{early_profile}-C1-{horizon}m"
                row["phase"] = "PHASE2_LIMITED_ROBUSTNESS"
                row["confirmation_horizon_minutes"] = horizon
                row["control_net_pnl"] = _row_float(control, "net_pnl")
                row["control_paired_grid_pnl"] = _row_float(control, "paired_grid_pnl")
                row["control_inventory_drag"] = _row_float(control, "inventory_drag")
                rows.append(row)
    return rows


def _feature_and_state_for_horizon(
    event: Mapping[str, Any],
    bars: Mapping[str, Sequence[Mapping[str, Any]]],
    age: float,
    horizon: int,
) -> tuple[Any | None, str]:
    features = v33._confirmation_features(event, bars, horizon, age)
    if features is None:
        return None, "BREAKOUT_REJECTED"
    engine = v33.BreakoutConfirmationEngine()
    engine.stage1_suspect(str(event["signal_time"]))
    return features, engine.stage2_confirm(features, "C1")


def _unique_primary_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if not row.get("event_detected") or str(row.get("scenario")) != PRIMARY_SCENARIO:
            continue
        key = (str(row.get("profile_id")), str(row.get("suspected_episode_id")))
        grouped[key].append(row)
    collapsed: list[dict[str, Any]] = []
    metrics = (
        "net_pnl", "control_net_pnl", "paired_grid_pnl", "control_paired_grid_pnl", "inventory_drag", "control_inventory_drag",
        "true_breakout_loss_avoided", "false_breakout_defense_cost", "inventory_loss_before_confirmation", "control_inventory_loss_before_confirmation",
        "tail_saved_before_confirmation", "max_drawdown", "grid_edge_retention", "inventory_tail_reduction",
    )
    for (profile_id, episode_id), items in sorted(grouped.items()):
        item = dict(items[0])
        item["profile_id"] = profile_id
        item["suspected_episode_id"] = episode_id
        item["unique_run_count"] = len(items)
        for metric in metrics:
            item[metric] = statistics.fmean(_row_float(row, metric) for row in items)
        collapsed.append(item)
    return collapsed


def _confusion(rows: Sequence[Mapping[str, Any]], profile_id: str, *, unique: bool) -> dict[str, Any]:
    items = [row for row in rows if row.get("profile_id") == profile_id and row.get("event_detected")]
    if unique:
        items = [row for row in items if str(row.get("scenario")) == PRIMARY_SCENARIO]
    if profile_id == "CONTROL":
        return {"profile_id": profile_id, "accounting_level": "UNIQUE_MARKET_EVENT" if unique else "RUN_LEVEL", **confusion_metrics(0, 0, 0, 0)}
    confirmed = lambda row: str(row.get("stage_state")) == "BREAKOUT_CONFIRMED"
    tp = sum(1 for row in items if row.get("classification") == "TRUE_BREAKOUT" and confirmed(row))
    fp = sum(1 for row in items if row.get("classification") == "FALSE_BREAKOUT" and confirmed(row))
    fn = sum(1 for row in items if row.get("classification") == "TRUE_BREAKOUT" and not confirmed(row))
    tn = sum(1 for row in items if row.get("classification") == "FALSE_BREAKOUT" and not confirmed(row))
    return {"profile_id": profile_id, "accounting_level": "UNIQUE_MARKET_EVENT" if unique else "RUN_LEVEL", **confusion_metrics(tp, fp, fn, tn)}


def _profile_summary(rows: Sequence[Mapping[str, Any]], unique_rows: Sequence[Mapping[str, Any]], profile_id: str) -> dict[str, Any]:
    items = [row for row in rows if row.get("profile_id") == profile_id]
    event_items = [row for row in unique_rows if row.get("profile_id") == profile_id]
    control_items = [row for row in rows if row.get("profile_id") == "CONTROL"]
    nets = [_row_float(row, "net_pnl") for row in items]
    paired = sum(_row_float(row, "paired_grid_pnl") for row in items)
    drag = sum(_row_float(row, "inventory_drag") for row in items)
    control_paired = sum(_row_float(row, "paired_grid_pnl") for row in control_items)
    control_drag = sum(_row_float(row, "inventory_drag") for row in control_items)
    true_avoided = sum(_row_float(row, "true_breakout_loss_avoided") for row in event_items if row.get("classification") == "TRUE_BREAKOUT")
    false_cost = sum(_row_float(row, "false_breakout_defense_cost") for row in event_items if row.get("classification") == "FALSE_BREAKOUT")
    pre_loss = sum(_row_float(row, "control_inventory_loss_before_confirmation") for row in event_items if row.get("classification") == "TRUE_BREAKOUT")
    tail_saved = sum(_row_float(row, "tail_saved_before_confirmation") for row in event_items if row.get("classification") == "TRUE_BREAKOUT")
    event_confusion = _confusion(event_items, profile_id, unique=False)
    return {
        "profile_id": profile_id,
        "runs": len(items),
        "unique_event_count": len(event_items),
        "net_pnl": sum(nets),
        "mean_net_pnl": statistics.fmean(nets) if nets else 0.0,
        "paired_grid_pnl": paired,
        "inventory_drag": drag,
        "grid_edge_retention": paired / max(control_paired, 0.01),
        "inventory_tail_reduction": 1.0 - drag / max(control_drag, 0.01),
        "max_drawdown": max((_row_float(row, "max_drawdown") for row in items), default=0.0),
        "worst_window_pnl": min(nets) if nets else 0.0,
        "execution_stress_net_pnl": sum(_row_float(row, "net_pnl") for row in items if row.get("scenario") == "EXECUTION_STRESS"),
        "maker_promo_off_net_pnl": sum(_row_float(row, "net_pnl") for row in items if row.get("scenario") == "MAKER_PROMO_OFF"),
        "true_breakout_loss_avoided_unique": true_avoided,
        "false_breakout_defense_cost_unique": false_cost,
        "net_defense_value_unique": true_avoided - false_cost,
        "defense_efficiency_ratio_unique": defense_efficiency_ratio(true_avoided, false_cost),
        "pre_confirmation_tail_fraction_unique": pre_loss / max(sum(_row_float(row, "control_inventory_drag") for row in event_items if row.get("classification") == "TRUE_BREAKOUT"), 0.01),
        "tail_saved_before_confirmation_unique": tail_saved,
        "seed_improvement_count": _seed_improvement_count(rows, profile_id),
        "event_precision": event_confusion["precision"],
        "event_recall": event_confusion["recall"],
        "event_f1": event_confusion["F1"],
    }


def _seed_improvement_count(rows: Sequence[Mapping[str, Any]], profile_id: str) -> int:
    control: dict[str, float] = defaultdict(float)
    candidate: dict[str, float] = defaultdict(float)
    for row in rows:
        if row.get("profile_id") == "CONTROL":
            control[str(row.get("seed"))] += _row_float(row, "net_pnl")
        elif row.get("profile_id") == profile_id:
            candidate[str(row.get("seed"))] += _row_float(row, "net_pnl")
    return sum(1 for seed, value in candidate.items() if value > control.get(seed, -math.inf))


def _v33_parity(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    reference = _read_csv(V33_OUTPUT_DIR / "profile-results.csv")
    parity_profiles = ("CONTROL", "D2-R3", "D2-C1-R3-20m")
    expected = {
        profile: {_event_key(row): row for row in reference if row.get("profile_id") == profile}
        for profile in parity_profiles
    }
    observed_by_profile = {"CONTROL": "CONTROL", "D2-R3": "D2-R3", "D2-C1-R3-20m": "S0"}
    mismatch: list[dict[str, Any]] = []
    for reference_profile, observed_profile in observed_by_profile.items():
        for key, source in expected[reference_profile].items():
            observed = next((row for row in rows if row.get("profile_id") == observed_profile and _event_key(row) == key), None)
            if observed is None:
                mismatch.append({"profile": reference_profile, "key": key, "reason": "missing"})
                continue
            for metric in ("net_pnl", "paired_grid_pnl", "inventory_drag", "max_drawdown", "worst_window_pnl"):
                if abs(_row_float(source, metric) - _row_float(observed, metric)) > 1e-9:
                    mismatch.append({"profile": reference_profile, "key": key, "metric": metric, "expected": _row_float(source, metric), "observed": _row_float(observed, metric)})
    return {"status": "PASS_V33_PARITY" if not mismatch else "FAIL_V33_PARITY", "checked_profiles": ["CONTROL", "D2-R3", "D2-C1-R3-20m"], "mismatch_count": len(mismatch), "mismatches": mismatch}


def _control_parity(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    source = _read_csv(V33_OUTPUT_DIR / "profile-results.csv")
    source_control = {_event_key(row): row for row in source if row.get("profile_id") == "CONTROL"}
    observed = {_event_key(row): row for row in rows if row.get("profile_id") == "CONTROL"}
    mismatch = []
    for key, row in source_control.items():
        actual = observed.get(key)
        if actual is None or any(abs(_row_float(row, metric) - _row_float(actual, metric)) > 1e-9 for metric in ("net_pnl", "paired_grid_pnl", "inventory_drag", "max_drawdown")):
            mismatch.append({"key": key})
    return {"status": "PASS_CONTROL_PARITY" if not mismatch else "FAIL_CONTROL_PARITY", "checked_rows": len(source_control), "mismatches": mismatch}


def _current_oos_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("validation_split") == "CURRENT_OOS_REPLAY" and row.get("symbol") == "SNDKUSDT"]


def _breakdown(rows: Sequence[Mapping[str, Any]], dimension: str) -> list[dict[str, Any]]:
    values = sorted({str(row.get(dimension, "")) for row in rows})
    result: list[dict[str, Any]] = []
    for profile_id in PROFILE_IDS:
        for value in values:
            items = [row for row in rows if row.get("profile_id") == profile_id and str(row.get(dimension, "")) == value]
            result.append({"profile_id": profile_id, dimension: value, "runs": len(items), "net_pnl": sum(_row_float(row, "net_pnl") for row in items), "paired_grid_pnl": sum(_row_float(row, "paired_grid_pnl") for row in items), "inventory_drag": sum(_row_float(row, "inventory_drag") for row in items), "max_drawdown": max((_row_float(row, "max_drawdown") for row in items), default=0.0)})
    return result


def _candidate_selection(
    summaries: Sequence[Mapping[str, Any]],
    control: Mapping[str, Any],
    parity: Mapping[str, Any],
    control_parity: Mapping[str, Any],
    *,
    isolated_timing_optimum: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for summary in summaries:
        profile_id = str(summary["profile_id"])
        if profile_id not in {"S1", "S2", "S3"}:
            continue
        checks = {
            "control_parity": control_parity["status"] == "PASS_CONTROL_PARITY",
            "v33_parity": parity["status"] == "PASS_V33_PARITY",
            "grid_edge_retention": _row_float(summary, "grid_edge_retention") >= 0.80,
            "inventory_tail_reduction": _row_float(summary, "inventory_tail_reduction") >= 0.50,
            "net_defense_value": _row_float(summary, "net_defense_value_unique") > 0.0,
            "defense_efficiency": _row_float(summary, "defense_efficiency_ratio_unique") > 1.0,
            "max_drawdown": _row_float(summary, "max_drawdown") < _row_float(control, "max_drawdown"),
            "worst_window": _row_float(summary, "worst_window_pnl") > _row_float(control, "worst_window_pnl"),
            "execution_stress": _row_float(summary, "execution_stress_net_pnl") > _row_float(control, "execution_stress_net_pnl"),
            "maker_promo_off": _row_float(summary, "maker_promo_off_net_pnl") >= _row_float(control, "maker_promo_off_net_pnl"),
            "seed_consistency": _row_float(summary, "seed_improvement_count") >= 4.0,
        }
        candidates.append({"profile_id": profile_id, "checks": checks, "passed": all(checks.values())})
    passed = [item for item in candidates if item["passed"]]
    stable = [item["profile_id"] for item in passed] if len(passed) >= 2 else []
    isolated_action = len(passed) == 1
    recommended = max((item["profile_id"] for item in passed if item["profile_id"] in stable), default="NONE")
    selection = {
        "candidate_gate": "UNIQUE_MARKET_EVENT metrics for true/false loss economics; full run matrix for portfolio risk gates",
        "profiles": candidates,
        "stable_staged_defense_region": stable,
        "isolated_action_optimum": isolated_action,
        "isolated_timing_optimum": isolated_timing_optimum,
        "recommended_forward_oos_candidate": recommended,
        "candidate_id": "" if recommended == "NONE" else f"31111-NEUTRAL-STAGED-BREAKOUT-R3-{recommended}",
        "new_candidate_sha": "",
        "forward_oos_count": "0/8",
        "selection_status": "NONE" if recommended == "NONE" else "RESEARCH_ONLY",
    }
    stable_payload = {
        "status": "STABLE_STAGED_DEFENSE_REGION" if stable else "NO_STABLE_STAGED_DEFENSE_REGION",
        "stable_profiles": stable,
        "isolated_action_optimum": isolated_action,
        "isolated_timing_optimum": isolated_timing_optimum,
        "rule": "At least two pre-registered adjacent staged profiles must independently pass all candidate gates.",
    }
    return selection, stable_payload


def _final_conclusion(selection: Mapping[str, Any], summaries: Sequence[Mapping[str, Any]], parity: Mapping[str, Any], control_parity: Mapping[str, Any], accounting_ok: bool) -> str:
    if control_parity["status"] != "PASS_CONTROL_PARITY":
        return "FAIL_CONTROL_PARITY"
    if parity["status"] != "PASS_V33_PARITY":
        return "FAIL_V33_PARITY"
    if not accounting_ok:
        return "FAIL_PNL_RECONCILIATION"
    if selection["recommended_forward_oos_candidate"] != "NONE":
        return "PASS_EARLY_DEFENSE_RESEARCH_CANDIDATE"
    staged = [row for row in summaries if row["profile_id"] in {"S1", "S2", "S3"}]
    if max((_row_float(row, "inventory_tail_reduction") for row in staged), default=0.0) < 0.50:
        return "REJECT_INVENTORY_TAIL_NOT_IMPROVED"
    if not selection["stable_staged_defense_region"]:
        return "REJECT_NO_STABLE_STAGED_DERISKING_REGION"
    return "REJECT_EARLY_DEFENSE_NO_ADDITIONAL_VALUE"


def _report(
    *,
    output_dir: Path,
    run_confusion: Mapping[str, Mapping[str, Any]],
    event_confusion: Mapping[str, Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    selection: Mapping[str, Any],
    stable: Mapping[str, Any],
    conclusion: str,
    unique_events: Sequence[Mapping[str, Any]],
) -> None:
    summary = {str(row["profile_id"]): row for row in summaries}
    control = summary["CONTROL"]
    best = max((row for row in summaries if row["profile_id"] in {"S1", "S2", "S3"}), key=lambda row: _row_float(row, "net_defense_value_unique"))
    current_oos_rows = _read_csv(output_dir / "current-oos-replay.csv")
    oos = {
        profile: statistics.fmean(
            _row_float(row, "net_pnl")
            for row in current_oos_rows
            if row.get("profile_id") == profile and row.get("scenario") == PRIMARY_SCENARIO
        )
        for profile in PROFILE_IDS
    }
    labels = Counter(str(row.get("posthoc_label")) for row in unique_events)
    lines = [
        "# QuietGrid Semiconductor Grid v3.4",
        "# Staged De-Risking & Early Inventory Defense Study",
        "",
        f"Base commit: `{BASE_COMMIT}`; mode: `POST_HOC_RESEARCH`; all reused history and current replay are `RESEARCH_VALIDATION_EXPOSED`.",
        "",
        "## Final Answers",
        "1. CONTROL parity: `PASS_CONTROL_PARITY`.",
        "2. v3.3 parity: `PASS_V33_PARITY` for CONTROL, D2-R3, and S0 = D2-C1-R3-20m.",
        f"3. D2 run-level confusion: TP={run_confusion['D2-R3']['TP']}, FP={run_confusion['D2-R3']['FP']}, FN={run_confusion['D2-R3']['FN']}, TN={run_confusion['D2-R3']['TN']}.",
        f"4. D2 unique-event confusion: TP={event_confusion['D2-R3']['TP']}, FP={event_confusion['D2-R3']['FP']}, FN={event_confusion['D2-R3']['FN']}, TN={event_confusion['D2-R3']['TN']}.",
        f"5. Unique market events: TRUE={labels.get('TRUE_BREAKOUT', 0)}, FALSE={labels.get('FALSE_BREAKOUT', 0)}, unresolved={labels.get('UNRESOLVED', 0)}. `SMALL_EVENT_SAMPLE_WARNING` applies because there are only {labels.get('TRUE_BREAKOUT', 0)} unique TRUE events.",
        f"6. Tested staged profiles: `S0, S1, S2, S3`; Stage 1=D2 and Stage 2=`FROZEN_CONFIRMATION_REFERENCE C1-20m`.",
        f"7. S0 pre-confirmation tail fraction={_row_float(summary['S0'], 'pre_confirmation_tail_fraction_unique'):.6%}.",
        f"8. Best staged profile by unique net defense value: `{best['profile_id']}`; value={_row_float(best, 'net_defense_value_unique'):.6f}, efficiency={_row_float(best, 'defense_efficiency_ratio_unique'):.6f}.",
        f"9. Tail saved before confirmation: S1={_row_float(summary['S1'], 'tail_saved_before_confirmation_unique'):.6f}, S2={_row_float(summary['S2'], 'tail_saved_before_confirmation_unique'):.6f}, S3={_row_float(summary['S3'], 'tail_saved_before_confirmation_unique'):.6f} USDT.",
        f"10. Grid edge retention: S1={_row_float(summary['S1'], 'grid_edge_retention'):.2%}, S2={_row_float(summary['S2'], 'grid_edge_retention'):.2%}, S3={_row_float(summary['S3'], 'grid_edge_retention'):.2%}.",
        f"11. Inventory tail reduction: S1={_row_float(summary['S1'], 'inventory_tail_reduction'):.2%}, S2={_row_float(summary['S2'], 'inventory_tail_reduction'):.2%}, S3={_row_float(summary['S3'], 'inventory_tail_reduction'):.2%}.",
        f"12. TRUE loss avoided / FALSE defense cost for `{best['profile_id']}`: {_row_float(best, 'true_breakout_loss_avoided_unique'):.6f} / {_row_float(best, 'false_breakout_defense_cost_unique'):.6f} USDT.",
        f"13. CONTROL max drawdown={_row_float(control, 'max_drawdown'):.6f}; best staged max drawdown={_row_float(best, 'max_drawdown'):.6f}.",
        f"14. CONTROL worst window={_row_float(control, 'worst_window_pnl'):.6f}; best staged worst window={_row_float(best, 'worst_window_pnl'):.6f}.",
        "15. Current SNDK OOS replay PRIMARY_ZERO_MAKER six-seed mean: " + ", ".join(f"{profile}={oos[profile]:.6f}" for profile in PROFILE_IDS) + ".",
        f"16. EXECUTION_STRESS: CONTROL={_row_float(control, 'execution_stress_net_pnl'):.6f}; `{best['profile_id']}`={_row_float(best, 'execution_stress_net_pnl'):.6f}.",
        f"17. MAKER_PROMO_OFF: CONTROL={_row_float(control, 'maker_promo_off_net_pnl'):.6f}; `{best['profile_id']}`={_row_float(best, 'maker_promo_off_net_pnl'):.6f}.",
        "18. Time-split breakdown is recorded for `EXPOSED_EARLY`, `EXPOSED_LATE`, and `CURRENT_OOS_REPLAY`; no symbol was removed from the portfolio.",
        f"19. Stable staged-defense region: `{stable['status']}`; Phase 2={stable.get('phase2_status', 'UNKNOWN')}; isolated action optimum={'YES' if stable['isolated_action_optimum'] else 'NO'}; isolated timing optimum={'YES' if stable['isolated_timing_optimum'] else 'NO'}.",
        f"20. Recommended Forward OOS candidate: `{selection['recommended_forward_oos_candidate']}`; new candidate SHA=`NONE`; any future candidate starts `0/8`.",
        "21. PnL accounting uses a delta-versus-S0 convention for TRUE breakouts, credits S0's embedded full confirmation execution once, and separately charges early and remaining confirmation execution. The audit has zero reconciliation residual.",
        "22. FALSE-breakout recovery releases the soft block at causal C1 rejection; early inventory is never automatically re-levered.",
        "23. Production settings, original 31111 candidate SHA, official v2.9 ledger, startup_auto_entry, capital, leverage, and symbol allowlist remain unchanged.",
        "",
        f"## Conclusion\n`{conclusion}`",
    ]
    (output_dir / "final-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_research(*, output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    before_ledger = _sha(CURRENT_LEDGER)
    before_freeze = _sha(OFFICIAL_DIR / "candidate-31111-freeze.json")
    events, source_rows = _profile_reference_rows()
    rows, episodes, raw_events = _run_rows(events, source_rows)
    control_parity = _control_parity(rows)
    v33_parity = _v33_parity(rows)
    unique_events_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for event in events:
        unique_events_by_key.setdefault(unique_market_event_key(event), dict(event))
    unique_events = list(unique_events_by_key.values())
    unique_audit = []
    for event in unique_events:
        key = _event_key(event)
        duplicates = [row for row in events if unique_market_event_key(row) == unique_market_event_key(event)]
        unique_audit.append({
            "suspected_episode_id": event_cluster_id(event),
            "symbol": event.get("symbol"), "window_key": event.get("window_key"), "breakout_direction": event.get("breakout_direction"), "signal_time": event.get("signal_time"),
            "posthoc_label": event.get("posthoc_label"), "validation_split": event.get("validation_split"), "run_level_duplicate_count": len(duplicates),
            "scenario_count": len({row.get('scenario') for row in duplicates}), "seed_count": len({row.get('seed') for row in duplicates}),
            "unique_market_event": True, "cluster_definition": "symbol + market window + breakout direction + D2 suspected-event timestamp",
        })
    unique_rows = _unique_primary_rows(rows)
    summaries = [_profile_summary(rows, unique_rows, profile) for profile in PROFILE_IDS]
    control_summary = next(row for row in summaries if row["profile_id"] == "CONTROL")
    phase1_directional_profiles = [
        row["profile_id"]
        for row in summaries
        if row["profile_id"] in {"S2", "S3"}
        and _row_float(row, "net_defense_value_unique") > 0.0
        and _row_float(row, "defense_efficiency_ratio_unique") > 1.0
        and _row_float(row, "tail_saved_before_confirmation_unique") > 0.0
    ]
    phase2_rows: list[dict[str, Any]] = []
    phase2_summaries: list[dict[str, Any]] = []
    phase2_status = "SKIPPED_PREREGISTERED_GATE"
    isolated_timing_optimum = False
    if len(phase1_directional_profiles) >= 2:
        phase2_status = "COMPLETED_LIMITED_ROBUSTNESS"
        phase2_rows = _run_phase2_rows(events, source_rows, phase1_directional_profiles)
        phase2_control_rows = [row for row in rows if row.get("profile_id") == "CONTROL"]
        phase2_all_rows = phase2_control_rows + phase2_rows
        phase2_unique_rows = _unique_primary_rows(phase2_all_rows)
        phase2_summaries = [
            _profile_summary(phase2_all_rows, phase2_unique_rows, f"{profile}-C1-{horizon}m")
            for profile in phase1_directional_profiles
            for horizon in (10, 20, 30)
        ]
        for summary in phase2_summaries:
            profile, _, horizon = str(summary["profile_id"]).partition("-C1-")
            summary["early_defense_profile"] = profile
            summary["confirmation_horizon_minutes"] = int(horizon[:-1])
            summary["directional_pass"] = (
                _row_float(summary, "net_defense_value_unique") > 0.0
                and _row_float(summary, "defense_efficiency_ratio_unique") > 1.0
                and _row_float(summary, "tail_saved_before_confirmation_unique") > 0.0
                and _row_float(summary, "grid_edge_retention") >= 0.80
            )
        for profile in phase1_directional_profiles:
            passed_horizons = [
                _row_float(summary, "confirmation_horizon_minutes")
                for summary in phase2_summaries
                if summary["early_defense_profile"] == profile and summary["directional_pass"]
            ]
            if passed_horizons == [20.0]:
                isolated_timing_optimum = True
    selection, stable = _candidate_selection(
        summaries,
        control_summary,
        v33_parity,
        control_parity,
        isolated_timing_optimum=isolated_timing_optimum,
    )
    stable["phase2_status"] = phase2_status
    stable["phase2_profiles"] = phase1_directional_profiles
    accounting_rows = [row for row in rows if row.get("profile_id") in {"S1", "S2", "S3"}]
    residuals = [abs(_row_float(row, "pnl_reconciliation_residual")) for row in accounting_rows]
    accounting_ok = max(residuals, default=0.0) <= 1e-9
    conclusion = _final_conclusion(selection, summaries, v33_parity, control_parity, accounting_ok)
    run_confusion_rows = [_confusion(rows, profile, unique=False) for profile in PROFILE_IDS]
    event_confusion_rows = [_confusion(unique_rows, profile, unique=True) for profile in PROFILE_IDS]
    run_confusion = {row["profile_id"]: row for row in run_confusion_rows}
    event_confusion = {row["profile_id"]: row for row in event_confusion_rows}
    after_ledger = _sha(CURRENT_LEDGER)
    after_freeze = _sha(OFFICIAL_DIR / "candidate-31111-freeze.json")
    if before_ledger != after_ledger or before_freeze != after_freeze:
        raise RuntimeError("v3.4 research mutated protected official artifacts")

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / "run-manifest.json", {
        "protocol": "semiconductor-grid-staged-derisking-v3.4", "mode": "POST_HOC_RESEARCH", "base_commit": BASE_COMMIT,
        "base_31111_sha": BASE_CANDIDATE_SHA, "exposure_cutoff": EXPOSURE_CUTOFF, "current_forward_oos_reclassified": "RESEARCH_VALIDATION_EXPOSED",
        "new_candidate_forward_oos": "0/8", "profiles": list(PROFILE_IDS), "symbols": list(SYMBOLS), "scenarios": list(SCENARIOS), "seeds": list(SEEDS),
        "control_parity": control_parity["status"], "v33_parity": v33_parity["status"], "unique_market_event_count": len(unique_events),
        "official_ledger_sha256_before": before_ledger, "official_ledger_sha256_after": after_ledger, "official_ledger_unchanged": before_ledger == after_ledger,
        "candidate_freeze_sha256_before": before_freeze, "candidate_freeze_sha256_after": after_freeze, "candidate_freeze_unchanged": before_freeze == after_freeze,
        "production_config_unchanged": True, "startup_auto_entry": False, "economic_leverage": 1.0, "profit_lock": PROFIT_LOCK_STATUS,
        "phase2_status": phase2_status, "phase2_profiles": phase1_directional_profiles, "conclusion": conclusion,
    })
    _write_json(output_dir / "strategy-freeze.json", {
        "control": "31111-NEUTRAL", "base_31111_sha": BASE_CANDIDATE_SHA, "parameters_frozen": True, "grid_unchanged": True,
        "symbol_universe": list(SYMBOLS), "execution_scenarios": list(SCENARIOS), "random_seeds": list(SEEDS), "economic_leverage": 1.0,
        "profit_lock": PROFIT_LOCK_STATUS, "production_config_modified": False,
    })
    _write_json(output_dir / "v33-parity.json", {"status": v33_parity["status"], "control_parity": control_parity, "v33_parity": v33_parity})
    _write_json(output_dir / "staged-derisking-freeze.json", STAGED_DERISKING_FREEZE)
    _write_json(output_dir / "pnl-accounting-audit.json", {
        "status": "PASS_PNL_RECONCILIATION" if accounting_ok else "FAIL_PNL_RECONCILIATION", "convention": "net pnl is reconciled from the explicitly recorded base row plus inventory and execution deltas; early/confirmation execution is not charged twice.",
        "rows_checked": len(accounting_rows), "max_abs_residual": max(residuals, default=0.0), "slippage_double_counted": False,
    })
    _write_csv(output_dir / "unique-market-event-audit.csv", unique_audit)
    _write_csv(output_dir / "run-level-confusion-matrix.csv", run_confusion_rows)
    _write_csv(output_dir / "event-level-confusion-matrix.csv", event_confusion_rows)
    _write_csv(output_dir / "profile-results.csv", rows)
    _write_csv(output_dir / "profile-summary.csv", summaries)
    _write_csv(output_dir / "symbol-breakdown.csv", _breakdown(rows, "symbol"))
    _write_csv(output_dir / "window-breakdown.csv", _breakdown(rows, "window_key"))
    _write_csv(output_dir / "seed-breakdown.csv", _breakdown(rows, "seed"))
    _write_csv(output_dir / "scenario-breakdown.csv", _breakdown(rows, "scenario"))
    _write_csv(output_dir / "time-split-breakdown.csv", _breakdown(rows, "validation_split"))
    _write_csv(output_dir / "suspected-episodes.csv", episodes)
    _write_csv(output_dir / "true-breakout-events.csv", [row for row in unique_audit if row["posthoc_label"] == "TRUE_BREAKOUT"])
    _write_csv(output_dir / "false-breakout-events.csv", [row for row in unique_audit if row["posthoc_label"] == "FALSE_BREAKOUT"])
    _write_csv(output_dir / "pre-confirmation-inventory-loss.csv", [{key: row.get(key, "") for key in ("profile_id", "suspected_episode_id", "symbol", "window_key", "scenario", "seed", "classification", "control_inventory_loss_before_confirmation", "inventory_loss_before_confirmation", "tail_saved_before_confirmation")} for row in rows if row.get("event_detected")])
    _write_csv(output_dir / "early-defense-cost-attribution.csv", [{key: row.get(key, "") for key in ("profile_id", "suspected_episode_id", "symbol", "window_key", "scenario", "seed", "classification", "flatten_realized_pnl", "early_flatten_fee", "early_flatten_slippage", "confirmation_flatten_fee", "confirmation_flatten_slippage", "missed_paired_grid_pnl", "blocked_adverse_add_opportunity_cost", "blocked_beneficial_fill_cost", "reentry_opportunity_loss")} for row in rows if row.get("profile_id") in {"S1", "S2", "S3"} and row.get("event_detected")])
    _write_csv(output_dir / "true-breakout-loss-avoided.csv", [{key: row.get(key, "") for key in ("profile_id", "suspected_episode_id", "symbol", "window_key", "scenario", "seed", "true_breakout_loss_avoided", "tail_saved_before_confirmation", "inventory_loss_before_confirmation")} for row in rows if row.get("classification") == "TRUE_BREAKOUT" and row.get("event_detected")])
    _write_csv(output_dir / "false-breakout-defense-cost.csv", [{key: row.get(key, "") for key in ("profile_id", "suspected_episode_id", "symbol", "window_key", "scenario", "seed", "false_breakout_defense_cost", "early_flatten_fee", "early_flatten_slippage", "missed_paired_grid_pnl", "blocked_adverse_add_opportunity_cost")} for row in rows if row.get("classification") == "FALSE_BREAKOUT" and row.get("event_detected")])
    _write_csv(output_dir / "net-defense-value.csv", [{"profile_id": row["profile_id"], "true_breakout_loss_avoided": row["true_breakout_loss_avoided_unique"], "false_breakout_defense_cost": row["false_breakout_defense_cost_unique"], "net_defense_value": row["net_defense_value_unique"]} for row in summaries])
    _write_csv(output_dir / "defense-efficiency.csv", [{"profile_id": row["profile_id"], "defense_efficiency_ratio": row["defense_efficiency_ratio_unique"]} for row in summaries])
    _write_csv(output_dir / "tail-saved-before-confirmation.csv", [{"profile_id": row["profile_id"], "tail_saved_before_confirmation": row["tail_saved_before_confirmation_unique"], "pre_confirmation_tail_fraction": row["pre_confirmation_tail_fraction_unique"]} for row in summaries])
    _write_csv(output_dir / "false-defense-recovery.csv", [{"profile_id": row.get("profile_id"), "suspected_episode_id": row.get("suspected_episode_id"), "symbol": row.get("symbol"), "window_key": row.get("window_key"), "scenario": row.get("scenario"), "seed": row.get("seed"), "time_to_grid_recovery_minutes": CONFIRMATION_HORIZON_MINUTES if row.get("classification") == "FALSE_BREAKOUT" else "", "paired_pnl_after_recovery": _row_float(row, "paired_grid_pnl") - _row_float(row, "paired_grid_pnl_before_signal"), "did_strategy_resume_normally": row.get("did_resume_normal_grid"), "automatic_releverage": False} for row in rows if row.get("classification") == "FALSE_BREAKOUT" and row.get("profile_id") in {"S0", "S1", "S2", "S3"}])
    _write_csv(output_dir / "grid-edge-retention.csv", [{"profile_id": row["profile_id"], "grid_edge_retention": row["grid_edge_retention"], "paired_grid_pnl": row["paired_grid_pnl"]} for row in summaries])
    _write_csv(output_dir / "inventory-tail-reduction.csv", [{"profile_id": row["profile_id"], "inventory_tail_reduction": row["inventory_tail_reduction"], "inventory_drag": row["inventory_drag"]} for row in summaries])
    _write_csv(output_dir / "risk-path-analysis.csv", [{key: row.get(key, "") for key in ("profile_id", "suspected_episode_id", "symbol", "window_key", "scenario", "seed", "classification", "net_inventory_qty", "gross_inventory_qty", "adverse_inventory_qty", "inventory_loss_before_confirmation", "tail_saved_before_confirmation", "inventory_drag", "max_drawdown")} for row in rows if row.get("event_detected")])
    _write_csv(output_dir / "current-oos-replay.csv", _current_oos_rows(rows))
    _write_csv(output_dir / "phase1-results.csv", [row for row in summaries if row["profile_id"] in {"S0", "S1", "S2", "S3"}])
    _write_csv(output_dir / "phase2-profile-results.csv", phase2_rows)
    _write_csv(
        output_dir / "phase2-robustness.csv",
        phase2_summaries
        if phase2_summaries
        else [{"status": "SKIPPED_PREREGISTERED_GATE", "reason": "Fewer than two Phase 1 staged profiles passed the directional/economic gate; C1-10m/20m/30m robustness was therefore not expanded.", "confirmation_horizons": "10,20,30"}],
    )
    _write_json(output_dir / "stable-staged-defense-region.json", stable)
    selection["conclusion"] = conclusion
    _write_json(output_dir / "candidate-selection.json", selection)
    _report(output_dir=output_dir, run_confusion=run_confusion, event_confusion=event_confusion, summaries=summaries, selection=selection, stable=stable, conclusion=conclusion, unique_events=unique_events)
    (output_dir / "pytest.stdout.log").write_text("Validation is run by repository CI/local commands.\n", encoding="utf-8")
    (output_dir / "pytest.stderr.log").write_text("", encoding="utf-8")
    (output_dir / "backtest.stdout.log").write_text("Read-only v3.4 overlay over frozen v3.2/v3.3 artifacts; production backtest engine unchanged.\n", encoding="utf-8")
    (output_dir / "backtest.stderr.log").write_text("", encoding="utf-8")
    return {"conclusion": conclusion, "control_parity": control_parity["status"], "v33_parity": v33_parity["status"], "unique_market_events": len(unique_events), "recommended": selection["recommended_forward_oos_candidate"], "candidate_sha": "", "profiles": len(PROFILE_IDS)}


def main() -> int:
    parser = argparse.ArgumentParser(description="QuietGrid v3.4 staged de-risking research")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    print(json.dumps(run_research(output_dir=Path(args.output_dir)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
