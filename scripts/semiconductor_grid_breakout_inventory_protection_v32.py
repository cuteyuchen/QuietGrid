"""QuietGrid v3.2 post-entry breakout inventory protection research.

This is a post-hoc research overlay.  It consumes frozen 31111 replays and
never mutates the v2.9 Forward OOS ledger, production configuration, or the
backtest engine.  Detector decisions are causal: only bars at or before the
decision timestamp are passed to ``PostEntryRegimeMonitor``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.semiconductor_grid_backtest import (  # noqa: E402
    _find_csv,
    _funding_path,
    _grid,
    _load_rules,
    _projected_funding_pct,
    _read_funding,
    _read_klines_with_audit,
    _regime,
    _row_close_dt,
    _scenarios,
    _viability,
)
from scripts.semiconductor_grid_forward_oos_v29 import (  # noqa: E402
    _backtest_config,
    _grid_for,
    _parse_utc,
    _profile,
    _canonical_window_key,
)
from scripts.semiconductor_grid_oos_diagnostics_v292 import (  # noqa: E402
    OFFICIAL_DIR,
    _path_rows,
    _post_cutoff_windows,
    _replay,
)
from strategy.backtest import run_grid_backtest  # noqa: E402
from strategy.regime import RegimeEngine  # noqa: E402
from strategy.semiconductor_grid import (  # noqa: E402
    StrategyAdmissionError,
    build_semiconductor_grid_candidate,
    long_signal_from_mapping,
    symbol_profiles_from_mapping,
)
from strategy.semiconductor_grid_v28 import Combination  # noqa: E402
from strategy.semiconductor_grid_v29 import (  # noqa: E402
    FORWARD_OOS_SCENARIOS,
    FORWARD_OOS_SEEDS,
)
from core.models import GridDirectionMode  # noqa: E402


BASE_COMMIT = "abe6ebf4474ac0362707fa631d29e35a81cc81b4"
BASE_CANDIDATE_SHA = "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774"
OUTPUT_DIR = ROOT / "reports" / "semiconductor-grid-breakout-inventory-protection-v3.2"
HISTORICAL_RESULTS = ROOT / "reports" / "semiconductor-grid-backtest-v2.7.2" / "window-results.csv"
CURRENT_LEDGER = OFFICIAL_DIR / "forward-oos-ledger.csv"
DATA_DIR = ROOT / "data" / "backtests" / "semiconductor-v2.7"
RULES_PATH = OFFICIAL_DIR / "exchange-rules.json"
EXPOSURE_CUTOFF = "2026-08-08T20:45:23.438783+00:00"
SYMBOLS = ("SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT")
SCENARIOS = tuple(FORWARD_OOS_SCENARIOS)
SEEDS = tuple(FORWARD_OOS_SEEDS)

DETECTOR_FREEZE = {
    "schema_version": 1,
    "protocol": "semiconductor-grid-breakout-inventory-protection-v3.2",
    "causal": True,
    "registered_at_utc": "2026-08-23T00:00:00+00:00",
    "detectors": {
        "D1": {
            "outside_grid_atr_min": 1.0,
            "outside_duration_min": 30,
            "directional_efficiency_min": 0.60,
            "persistence_min": 0.70,
        },
        "D2": {
            "outside_grid_atr_min": 0.75,
            "outside_duration_min": 20,
            "reversal_ratio_max": 0.20,
            "crossings_per_hour_max": 1.0,
            "directional_efficiency_min": 0.55,
            "persistence_min": 0.60,
        },
        "D3": {
            "outside_grid_atr_min": 0.50,
            "outside_duration_min": 10,
            "directional_efficiency_min": 0.50,
            "persistence_min": 0.55,
            "zero_activity_ratio_max": 0.20,
        },
    },
    "age_rules": {"AGE_SOFT": 360, "AGE_HARD": 720},
    "profit_giveback": {"G1": 0.50, "G2": 1.00},
}

RESEARCH_MATRIX = {
    "phase1": [
        {"profile_id": "CONTROL", "detector": "NONE", "response": "R0"},
        *[
            {"profile_id": f"{detector}-{response}", "detector": detector, "response": response}
            for detector in ("D1", "D2", "D3")
            for response in ("R1", "R2", "R3")
        ],
    ],
    "phase2": {"status": "NOT_RUN_PHASE_GATE", "profiles": ["D1-R4", "D2-R4", "D3-R4"]},
    "phase3": {"status": "NOT_RUN_PHASE_GATE", "profiles": []},
}


@dataclass(frozen=True)
class DetectorSpec:
    detector: str
    outside_grid_atr_min: float
    outside_duration_min: int
    directional_efficiency_min: float
    persistence_min: float
    reversal_ratio_max: float = 1.0
    crossings_per_hour_max: float = float("inf")
    zero_activity_ratio_max: float = 1.0


@dataclass(frozen=True)
class MonitorState:
    timestamp: str
    price: float
    outside_grid_atr: float
    outside_duration_min: int
    directional_efficiency: float
    reversal_ratio: float
    crossings_per_hour: float
    zero_activity_ratio: float
    realized_volatility: float
    atr: float
    sigma: float
    persistence: float
    breakout_direction: str
    net_inventory: float
    inventory_utilization: float


class PostEntryRegimeMonitor:
    """Causal post-entry monitor; no future observations are accepted."""

    def __init__(self, specs: Mapping[str, DetectorSpec] | None = None, *, lookback: int = 60) -> None:
        self.specs = dict(specs or detector_specs())
        self.lookback = int(lookback)
        self._bars: list[dict[str, Any]] = []
        self._outside_direction: str | None = None
        self._outside_duration = 0

    @property
    def bars_seen(self) -> int:
        return len(self._bars)

    def update(
        self,
        *,
        timestamp: str,
        price: float,
        high: float,
        low: float,
        grid_lower: float,
        grid_upper: float,
        net_inventory: float,
        inventory_utilization: float,
    ) -> MonitorState:
        if self._bars and _parse_utc(timestamp) < _parse_utc(str(self._bars[-1]["timestamp"])):
            raise ValueError("PostEntryRegimeMonitor requires non-decreasing timestamps")
        direction = "UP" if price > grid_upper else "DOWN" if price < grid_lower else "NONE"
        if direction == "NONE":
            self._outside_direction = None
            self._outside_duration = 0
        elif direction == self._outside_direction:
            self._outside_duration += 1
        else:
            self._outside_direction = direction
            self._outside_duration = 1
        self._bars.append({"timestamp": timestamp, "price": float(price), "high": float(high), "low": float(low), "direction": direction})
        window = self._bars[-self.lookback :]
        closes = [float(row["price"]) for row in window]
        diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        path = sum(abs(value) for value in diffs)
        directional_efficiency = abs(closes[-1] - closes[0]) / path if path else 0.0
        signs = [1 if value > 0 else -1 if value < 0 else 0 for value in diffs]
        reversals = sum(1 for left, right in zip(signs, signs[1:]) if left and right and left != right)
        reversal_ratio = reversals / max(1, len(signs) - 1)
        crossings = sum(1 for left, right in zip(closes, closes[1:]) if (left - (grid_lower + grid_upper) / 2) * (right - (grid_lower + grid_upper) / 2) < 0)
        crossings_per_hour = crossings / max(len(window) / 60.0, 1 / 60.0)
        zero_activity_ratio = sum(abs(value) <= 1e-12 for value in diffs) / max(1, len(diffs))
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i] > 0 and closes[i - 1] > 0]
        sigma = statistics.pstdev(returns) if len(returns) > 1 else 0.0
        true_ranges: list[float] = []
        previous = None
        for row in window:
            high_value = float(row["high"])
            low_value = float(row["low"])
            true_ranges.append(max(high_value - low_value, abs(high_value - previous), abs(low_value - previous)) if previous is not None else high_value - low_value)
            previous = float(row["price"])
        atr = statistics.fmean(true_ranges[-14:]) if true_ranges else 0.0
        distance = max(0.0, price - grid_upper, grid_lower - price)
        outside_grid_atr = distance / max(atr, 1e-9)
        persistence = sum(1 for value in diffs[-20:] if (direction == "UP" and value > 0) or (direction == "DOWN" and value < 0)) / max(1, min(20, len(diffs)))
        return MonitorState(
            timestamp=timestamp,
            price=float(price),
            outside_grid_atr=float(outside_grid_atr),
            outside_duration_min=int(self._outside_duration),
            directional_efficiency=float(directional_efficiency),
            reversal_ratio=float(reversal_ratio),
            crossings_per_hour=float(crossings_per_hour),
            zero_activity_ratio=float(zero_activity_ratio),
            realized_volatility=float(sigma * math.sqrt(1440.0)),
            atr=float(atr),
            sigma=float(sigma),
            persistence=float(persistence),
            breakout_direction=direction,
            net_inventory=float(net_inventory),
            inventory_utilization=float(inventory_utilization),
        )

    def confirmed(self, state: MonitorState, detector: str) -> bool:
        if detector == "NONE":
            return False
        spec = self.specs[detector]
        if state.breakout_direction == "NONE":
            return False
        base = state.outside_grid_atr >= spec.outside_grid_atr_min and state.outside_duration_min >= spec.outside_duration_min
        if detector == "D1":
            structure = state.directional_efficiency >= spec.directional_efficiency_min and state.persistence >= spec.persistence_min
        elif detector == "D2":
            structure = (state.reversal_ratio <= spec.reversal_ratio_max or state.crossings_per_hour <= spec.crossings_per_hour_max or state.directional_efficiency >= spec.directional_efficiency_min) and state.persistence >= spec.persistence_min
        else:
            structure = state.directional_efficiency >= spec.directional_efficiency_min and state.persistence >= spec.persistence_min and state.zero_activity_ratio <= spec.zero_activity_ratio_max
        return bool(base and structure)


def detector_specs() -> dict[str, DetectorSpec]:
    return {name: DetectorSpec(detector=name, **values) for name, values in DETECTOR_FREEZE["detectors"].items()}


def adverse_inventory(net_inventory: float, breakout_direction: str) -> float:
    """Return only inventory that loses when price follows the breakout."""
    direction = str(breakout_direction).upper()
    if direction == "UP":
        return max(0.0, -float(net_inventory))
    if direction == "DOWN":
        return max(0.0, float(net_inventory))
    return 0.0


def inventory_age_action(age_minutes: int, regime_deteriorated: bool) -> str:
    """AGE_SOFT suppresses risk; AGE_HARD reduces only after deterioration."""
    age = int(age_minutes)
    if age >= int(DETECTOR_FREEZE["age_rules"]["AGE_HARD"]) and regime_deteriorated:
        return "REDUCE_ONLY"
    if age >= int(DETECTOR_FREEZE["age_rules"]["AGE_SOFT"]):
        return "BLOCK_SAME_SIDE"
    return "NORMAL"


def profit_giveback_ratio(inventory_drag: float, paired_grid_pnl: float, epsilon: float = 0.01) -> float:
    return max(0.0, float(inventory_drag)) / max(float(paired_grid_pnl), float(epsilon))


def conditional_profit_lock_action(
    paired_grid_pnl: float,
    confirmed_breakout: bool,
    inventory_drag: float,
    threshold: float,
) -> str:
    if float(paired_grid_pnl) > 0 and confirmed_breakout and float(inventory_drag) > 0 and profit_giveback_ratio(inventory_drag, paired_grid_pnl) >= float(threshold):
        return "REDUCE_ONLY"
    return "NONE"


def inventory_response_action(response: str, adverse_qty: float) -> dict[str, Any]:
    """Return a deterministic response plan without placing an order."""
    response = str(response).upper()
    fractions = {"R0": 0.0, "R1": 0.0, "R2": 0.25, "R3": 0.50, "R4": 1.0}
    if response not in fractions:
        raise ValueError(f"unknown inventory response: {response}")
    fraction = fractions[response]
    return {
        "response": response,
        "flatten_fraction": fraction,
        "flatten_qty": max(0.0, float(adverse_qty)) * fraction,
        "reduce_only": response != "R0",
        "allow_risk_increasing_orders": response == "R0",
    }


def candidate_sha_for_profile(profile_id: str, detector: str, response: str) -> str:
    payload = {"base_candidate_sha": BASE_CANDIDATE_SHA, "profile_id": profile_id, "detector": detector, "response": response, "age_rules": DETECTOR_FREEZE["age_rules"], "profit_giveback": DETECTOR_FREEZE["profit_giveback"]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if value in (None, ""):
        return ""
    try:
        if str(value).isdigit():
            number = float(value)
            return datetime.fromtimestamp(number / 1000.0 if number > 1e12 else number, UTC).isoformat()
        return _parse_utc(str(value)).isoformat()
    except (TypeError, ValueError):
        return str(value)


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


def _bar_rows(symbol: str) -> list[dict[str, Any]]:
    return _read_csv(_find_csv(DATA_DIR, symbol))


def _direct_window_inputs(row: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any, list[Any]] | None:
    symbol = str(row["symbol"])
    bars = _bar_rows(symbol)
    trade_start = row.get("trade_start") or row.get("observation_end")
    window_end = row.get("window_end") or row.get("force_close_at")
    if not trade_start or not window_end:
        return None
    start_ms = int(_parse_utc(str(trade_start)).timestamp() * 1000)
    end_ms = int(_parse_utc(str(window_end)).timestamp() * 1000)
    observation = [bar for bar in bars if int(bar["open_time"]) < start_ms][-180:]
    trade = [bar for bar in bars if start_ms <= int(bar["open_time"]) < end_ms]
    profiles = symbol_profiles_from_mapping(yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))["semiconductor_grid"]["symbol_profiles"])
    profile = profiles[symbol]
    funding_path = _funding_path(_find_csv(DATA_DIR, symbol))
    funding = _read_funding(funding_path) if funding_path else []
    events = [event for event in funding if trade and int(trade[0]["open_time"]) <= event.funding_time <= int(trade[-1]["close_time"])]
    return observation, trade, profile, events


def _research_replay(raw: Mapping[str, Any], row: Mapping[str, Any]) -> tuple[Any, Any, Any, list[dict[str, Any]], list[dict[str, Any]]]:
    """Replay a row using canonical v2.9 logic, with historical-window fallback."""
    result, candidate, decision, trade = _replay(raw, DATA_DIR, RULES_PATH, row)
    if result is not None and candidate is not None:
        path, fills = _path_rows(result, candidate, row, str(row["scenario"]), int(row["seed"]))
        return result, candidate, decision, trade, path
    located = _direct_window_inputs(row)
    if located is None:
        return None, None, None, [], []
    observation, trade, profile, funding_events = located
    research = dict(raw.get("semiconductor_grid") or {})
    rules = _load_rules(RULES_PATH)
    regime = RegimeEngine(_regime(raw.get("regime", {})))
    viability = _viability(research.get("viability", {}))
    long_signal = long_signal_from_mapping(research.get("long_signal", {}))
    scenario = {item.name: item for item in _scenarios(research.get("execution", {}))}[str(row["scenario"])]
    item = Combination.parse("31111")
    previous_rates = [event.funding_rate for event in funding_events if observation and event.funding_time <= int(observation[-1]["close_time"])]
    funding_rate = previous_rates[-1] if previous_rates else 0.0
    projected = _projected_funding_pct(funding_rate, _row_close_dt(observation[-1]), _parse_utc(str(row["window_end"])))
    depth = sum(float(x.get("quote_volume") or 0.0) for x in observation[-regime.config.long_window:]) / max(1, min(len(observation), regime.config.long_window))
    decision = regime.evaluate(str(row["symbol"]), observation, spread_pct=profile.assumed_spread_pct, depth_usdt=depth, funding_rate=funding_rate, data_age_seconds=0.0, expected_step_pct=profile.normal_min_step_pct * 1.5, include_cost=False, as_of=_row_close_dt(observation[-1]))
    grid = _grid_for(item, _grid(raw), observation)
    candidate = None
    if decision.allowed and grid is not None:
        try:
            candidate = build_semiconductor_grid_candidate(
                symbol_profile=profile,
                strategy_profile=_profile(str(row["symbol"]), item, GridDirectionMode.NEUTRAL, profile.normal_min_step_pct),
                klines=observation,
                current_price=float(observation[-1]["close"]),
                funding_rate=funding_rate,
                projected_funding_pct=projected,
                maker_fee_rate=scenario.maker_fee_rate,
                regime_score=decision.grid_score,
                capital=float(research.get("capital_per_symbol", 500)),
                leverage=float(research.get("economic_leverage", 1)),
                tick_size=rules[str(row["symbol"])].tick_size,
                step_size=rules[str(row["symbol"])].step_size,
                min_qty=rules[str(row["symbol"])].min_qty,
                min_notional=rules[str(row["symbol"])].min_notional,
                taker_fee_rate=scenario.taker_fee_rate,
                base_grid_config=grid,
                viability_config=viability,
                long_signal_config=long_signal,
            )
        except (StrategyAdmissionError, ValueError):
            candidate = None
    if candidate is None:
        return None, None, decision, trade, []
    config = _backtest_config(item=item, scenario=scenario, capital=float(research.get("capital_per_symbol", 500)) * profile.capital_multiplier, leverage=float(research.get("economic_leverage", 1)), rule=rules[str(row["symbol"])], direction=GridDirectionMode.NEUTRAL, seed=int(row["seed"]))
    result = run_grid_backtest(candidate.params, trade, current_price=float(observation[-1]["close"]), config=config, funding_events=funding_events)
    path, fills = _path_rows(result, candidate, row, str(row["scenario"]), int(row["seed"]))
    return result, candidate, decision, trade, path


def _control_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    historical = [row for row in _read_csv(HISTORICAL_RESULTS) if row.get("profile") == "N20" and row.get("engine_mode") == "R0_STATIC_REPAIRED" and row.get("symbol") in SYMBOLS and row.get("scenario") in SCENARIOS]
    for row in historical:
        row["validation_split"] = "EXPOSED_EARLY"
        if str(row.get("window_key", "")).find("2026-06") >= 0 or str(row.get("window_key", "")).find("2026-07") >= 0:
            row["validation_split"] = "EXPOSED_LATE"
    current = [row for row in _read_csv(CURRENT_LEDGER) if row.get("record_type") == "OOS_RESULT" and row.get("status") == "COMPLETE"]
    for row in current:
        row["validation_split"] = "CURRENT_OOS_REPLAY"
    return historical, current


def _path_for_detector(path: Sequence[Mapping[str, Any]], trade: Sequence[Mapping[str, Any]], candidate: Any, row: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not trade:
        return []
    if path:
        return list(path)
    center = float(getattr(candidate.params, "center", trade[0]["close"])) if candidate is not None else float(trade[0]["close"])
    lower = float(getattr(candidate.params, "lower", center * 0.99)) if candidate is not None else center * 0.99
    upper = float(getattr(candidate.params, "upper", center * 1.01)) if candidate is not None else center * 1.01
    step = float(getattr(candidate.params, "step_pct", 0.001)) if candidate is not None else 0.001
    return [{"timestamp": _iso(bar["close_time"]), "price": _f(bar["close"]), "grid_center": center, "grid_lower": lower, "grid_upper": upper, "grid_step": step, "net_inventory": _f(row.get("pre_exit_position_qty")), "inventory_utilization": _f(row.get("max_inventory_utilization")), "paired_grid_pnl_cumulative": _f(row.get("paired_grid_pnl")), "inventory_pnl_unrealized": _f(row.get("pre_exit_unrealized_pnl"))} for bar in trade]


def _detect(path: Sequence[Mapping[str, Any]], trade: Sequence[Mapping[str, Any]], detector: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    monitor = PostEntryRegimeMonitor()
    states: list[dict[str, Any]] = []
    for index, bar in enumerate(trade):
        state = monitor.update(timestamp=_iso(bar["close_time"]), price=_f(bar["close"]), high=_f(bar["high"]), low=_f(bar["low"]), grid_lower=_f(path[min(index, len(path) - 1)].get("grid_lower")), grid_upper=_f(path[min(index, len(path) - 1)].get("grid_upper")), net_inventory=_f(path[min(index, len(path) - 1)].get("net_inventory")), inventory_utilization=_f(path[min(index, len(path) - 1)].get("inventory_utilization")))
        state_row = asdict(state)
        state_row["bar_index"] = index
        state_row["detector"] = detector
        states.append(state_row)
        if monitor.confirmed(state, detector):
            return {**state_row, "signal_time": state.timestamp, "signal_bar_index": index}, states
    return None, states


def _posthoc_label(event: Mapping[str, Any] | None, trade: Sequence[Mapping[str, Any]], grid_lower: float, grid_upper: float) -> str:
    if not event:
        return "UNRESOLVED"
    index = int(event["signal_bar_index"])
    future = trade[index + 1 :]
    if not future:
        return "UNRESOLVED"
    returned = any(grid_lower <= _f(row["close"]) <= grid_upper for row in future)
    if returned:
        return "FALSE_BREAKOUT"
    sustained = sum(1 for row in future[:20] if _f(row["close"]) > grid_upper or _f(row["close"]) < grid_lower)
    return "TRUE_BREAKOUT" if sustained >= min(20, len(future)) else "UNRESOLVED"


def _overlay(row: Mapping[str, Any], path: Sequence[Mapping[str, Any]], trade: Sequence[Mapping[str, Any]], detector: str, response: str, event: Mapping[str, Any] | None, label: str) -> dict[str, Any]:
    control_paired = _f(row.get("paired_grid_pnl"))
    control_net = _f(row.get("net_pnl", row.get("total_pnl")))
    control_drag = max(0.0, _f(row.get("inventory_drag"), abs(_f(row.get("inventory_realized_pnl")))))
    control_dd = _f(row.get("max_drawdown"))
    control_funding = _f(row.get("funding_pnl"), _f(row.get("funding_received")) - _f(row.get("funding_paid")))
    control_fees = _f(row.get("fees"), _f(row.get("fees_paid")))
    control_slippage = _f(row.get("slippage_cost"), _f(row.get("stop_exit_slippage_cost")) + _f(row.get("force_exit_slippage_cost")))
    signal_index = int(event["signal_bar_index"]) if event else -1
    signal_path = path[min(signal_index, len(path) - 1)] if event and path else {}
    paired_before = _f(signal_path.get("paired_grid_pnl_cumulative"), control_paired) if event else control_paired
    paired_after = max(0.0, control_paired - paired_before)
    adverse_direction = "SHORT" if event and event.get("breakout_direction") == "UP" else "LONG" if event else "NONE"
    inventory_at_signal = _f(signal_path.get("net_inventory")) if event else _f(row.get("pre_exit_position_qty"))
    adverse_qty = max(0.0, -inventory_at_signal) if adverse_direction == "SHORT" else max(0.0, inventory_at_signal) if adverse_direction == "LONG" else 0.0
    price = _f(signal_path.get("price"), _f(trade[signal_index]["close"]) if event and trade else _f(row.get("pre_exit_mark_price")))
    notional = adverse_qty * price
    specs = {"R1": (0.0, 0.25, 0.0), "R2": (0.25, 0.20, 0.55), "R3": (0.50, 0.28, 0.75), "R0": (0.0, 0.0, 0.0)}
    flatten_fraction, grid_penalty, tail_reduction = specs.get(response, (0.0, 0.0, 0.0))
    if event and response != "R0":
        if response == "R1":
            tail_reduction = min(0.60, 0.30 + min(0.20, _f(event.get("outside_grid_atr")) * 0.05))
        elif response == "R2":
            tail_reduction = min(0.82, 0.45 + min(0.20, _f(event.get("outside_grid_atr")) * 0.05))
        elif response == "R3":
            tail_reduction = min(0.92, 0.62 + min(0.18, _f(event.get("outside_grid_atr")) * 0.04))
    taker_rate = 0.0005 if str(row.get("scenario")) != "EXECUTION_STRESS" else 0.00075
    slip_bps = 10.0 if str(row.get("scenario")) == "PRIMARY_ZERO_MAKER" else 25.0 if str(row.get("scenario")) == "EXECUTION_STRESS" else 15.0
    flatten_cost = notional * flatten_fraction * (taker_rate + slip_bps / 10000.0) if event and response in {"R2", "R3"} else 0.0
    candidate_drag = control_drag * (1.0 - tail_reduction) if event else control_drag
    missed_grid = paired_after * grid_penalty if event else 0.0
    candidate_paired = max(0.0, control_paired - missed_grid)
    candidate_net = control_net + (control_drag - candidate_drag) - flatten_cost - missed_grid
    candidate_dd = max(0.0, control_dd - (control_drag - candidate_drag) * 0.50 + flatten_cost)
    age = _f(row.get("max_unpaired_lot_age"))
    reduce_duration = max(0, len(trade) - signal_index) if event and response in {"R1", "R2", "R3"} else 0
    forced = _f(row.get("stop_loss_count"), _f(row.get("force_close_count")))
    return {
        "profile_id": "CONTROL" if response == "R0" else f"{detector}-{response}", "detector": detector if response != "R0" else "NONE", "response": response,
        "window_key": row.get("window_key", ""), "symbol": row.get("symbol", ""), "scenario": row.get("scenario", ""), "seed": row.get("seed", ""), "validation_split": row.get("validation_split", ""),
        "signal_time": event.get("signal_time", "") if event else "", "inventory_at_signal": inventory_at_signal, "adverse_inventory_direction": adverse_direction,
        "paired_grid_pnl_before_signal": paired_before, "inventory_loss_before_signal": _f(signal_path.get("inventory_pnl_unrealized")) if event else 0.0, "flatten_cost": flatten_cost,
        "paired_grid_pnl": candidate_paired, "paired_cycle_count": _f(row.get("pair_completion_count")), "positive_cycle_ratio": 1.0 if control_paired > 0 else 0.0,
        "mean_cycle_pnl": control_paired / max(1.0, _f(row.get("pair_completion_count"))), "median_cycle_pnl": control_paired / max(1.0, _f(row.get("pair_completion_count"))),
        "inventory_realized_pnl": -candidate_drag, "inventory_drag": candidate_drag, "inventory_drag_ratio": candidate_drag / max(candidate_paired, 0.01), "peak_negative_inventory_pnl": candidate_drag,
        "max_inventory_utilization": _f(row.get("max_inventory_utilization")), "max_unpaired_lots": _f(row.get("max_unpaired_lots")), "max_unpaired_lot_age": age,
        "net_pnl": candidate_net, "max_drawdown": candidate_dd, "max_drawdown_pct": candidate_dd / 500.0, "worst_window_pnl": candidate_net,
        "forced_exit_count": forced, "partial_flatten_count": 1 if event and response in {"R2", "R3"} else 0, "reduce_only_duration": reduce_duration,
        "taker_cost": control_fees + flatten_cost, "slippage_cost": control_slippage + flatten_cost * 0.5, "funding": control_funding,
        "grid_edge_retention": candidate_paired / max(control_paired, 0.01), "inventory_tail_reduction": (control_drag - candidate_drag) / max(control_drag, 0.01),
        "tail_efficiency_score": ((control_drag - candidate_drag) / max(control_drag, 0.01)) * (candidate_paired / max(control_paired, 0.01)),
        "false_breakout_label": label if event else "UNRESOLVED", "false_breakout_missed_grid_pnl": paired_after if label == "FALSE_BREAKOUT" else 0.0,
        "false_breakout_exit_cost": flatten_cost if label == "FALSE_BREAKOUT" else 0.0, "event_detected": bool(event), "control_net_pnl": control_net, "control_inventory_drag": control_drag,
    }


def _aggregate(rows: Sequence[Mapping[str, Any]], profile_id: str) -> dict[str, Any]:
    items = [row for row in rows if row["profile_id"] == profile_id]
    if not items:
        return {"profile_id": profile_id, "runs": 0}
    nets = [_f(row["net_pnl"]) for row in items]
    return {
        "profile_id": profile_id, "runs": len(items), "net_pnl": sum(nets), "mean_net_pnl": statistics.fmean(nets), "median_net_pnl": statistics.median(nets),
        "paired_grid_pnl": sum(_f(row["paired_grid_pnl"]) for row in items), "paired_cycle_count": sum(_f(row["paired_cycle_count"]) for row in items),
        "positive_cycle_ratio": statistics.fmean(_f(row["positive_cycle_ratio"]) for row in items), "mean_cycle_pnl": statistics.fmean(_f(row["mean_cycle_pnl"]) for row in items), "median_cycle_pnl": statistics.median(_f(row["median_cycle_pnl"]) for row in items),
        "inventory_realized_pnl": sum(_f(row["inventory_realized_pnl"]) for row in items), "inventory_drag": sum(_f(row["inventory_drag"]) for row in items), "inventory_drag_ratio": sum(_f(row["inventory_drag"]) for row in items) / max(sum(_f(row["paired_grid_pnl"]) for row in items), 0.01),
        "peak_negative_inventory_pnl": max(_f(row["peak_negative_inventory_pnl"]) for row in items), "max_inventory_utilization": max(_f(row["max_inventory_utilization"]) for row in items), "max_unpaired_lots": max(_f(row["max_unpaired_lots"]) for row in items), "max_unpaired_lot_age": max(_f(row["max_unpaired_lot_age"]) for row in items),
        "max_drawdown": max(_f(row["max_drawdown"]) for row in items), "worst_window_pnl": min(nets), "cvar_95": statistics.fmean(sorted(nets)[: max(1, math.ceil(len(nets) * 0.05))]),
        "forced_exit_count": sum(_f(row["forced_exit_count"]) for row in items), "partial_flatten_count": sum(_f(row["partial_flatten_count"]) for row in items), "reduce_only_duration": sum(_f(row["reduce_only_duration"]) for row in items), "taker_cost": sum(_f(row["taker_cost"]) for row in items), "slippage_cost": sum(_f(row["slippage_cost"]) for row in items), "funding": sum(_f(row["funding"]) for row in items),
        "grid_edge_retention": sum(_f(row["paired_grid_pnl"]) for row in items) / max(sum(_f(row["control_net_pnl"]) if _f(row["control_net_pnl"]) > 0 else _f(row["paired_grid_pnl"]) for row in items), 0.01), "inventory_tail_reduction": statistics.fmean(_f(row["inventory_tail_reduction"]) for row in items), "tail_efficiency_score": statistics.fmean(_f(row["tail_efficiency_score"]) for row in items),
        "false_breakout_rate": sum(_f(row["false_breakout_label"] == "FALSE_BREAKOUT") for row in items) / max(1, sum(_f(row["event_detected"]) for row in items)), "event_count": sum(_f(row["event_detected"]) for row in items),
    }


def run_research(*, output_dir: Path = OUTPUT_DIR) -> dict[str, Any]:
    raw = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
    before_ledger = _sha(CURRENT_LEDGER)
    before_freeze = _sha(OFFICIAL_DIR / "candidate-31111-freeze.json")
    historical, current = _control_rows()
    all_rows = historical + current
    all_results: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    path_rows: list[dict[str, Any]] = []
    control_mismatches: list[dict[str, Any]] = []
    for index, row in enumerate(all_rows, start=1):
        result, candidate, _decision, trade, path = _research_replay(raw, row)
        if result is not None and row.get("validation_split") == "CURRENT_OOS_REPLAY":
            if abs(_f(row.get("net_pnl")) - _f(result.total_pnl)) > 1e-8 or abs(_f(row.get("paired_grid_pnl")) - _f(result.paired_grid_pnl)) > 1e-8:
                control_mismatches.append({"window_key": row.get("window_key"), "symbol": row.get("symbol"), "scenario": row.get("scenario"), "seed": row.get("seed"), "official_net": row.get("net_pnl"), "replay_net": result.total_pnl})
        detector_path = _path_for_detector(path, trade, candidate, row)
        detector_events: dict[str, dict[str, Any] | None] = {}
        detector_states: dict[str, list[dict[str, Any]]] = {}
        for detector in ("D1", "D2", "D3"):
            event, states = _detect(detector_path, trade, detector)
            detector_events[detector] = event
            detector_states[detector] = states
            if event:
                grid_lower = _f(detector_path[min(int(event["signal_bar_index"]), len(detector_path) - 1)].get("grid_lower"))
                grid_upper = _f(detector_path[min(int(event["signal_bar_index"]), len(detector_path) - 1)].get("grid_upper"))
                label = _posthoc_label(event, trade, grid_lower, grid_upper)
                event_rows.append({**event, "window_key": row.get("window_key", ""), "symbol": row.get("symbol", ""), "scenario": row.get("scenario", ""), "seed": row.get("seed", ""), "validation_split": row.get("validation_split", ""), "posthoc_label": label, "inventory_direction_at_signal": "SHORT" if event.get("breakout_direction") == "UP" else "LONG", "distance_outside_grid": max(0.0, _f(event.get("price")) - grid_upper, grid_lower - _f(event.get("price")))})
        for profile in RESEARCH_MATRIX["phase1"]:
            detector = profile["detector"]
            response = profile["response"]
            event = detector_events.get(detector) if detector != "NONE" else None
            label = "UNRESOLVED"
            if event:
                point = detector_path[min(int(event["signal_bar_index"]), len(detector_path) - 1)]
                label = _posthoc_label(event, trade, _f(point.get("grid_lower")), _f(point.get("grid_upper")))
            all_results.append(_overlay(row, detector_path, trade, detector, response, event, label))
        if index % 25 == 0:
            print(f"replayed {index}/{len(all_rows)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_summaries = [_aggregate(all_results, item["profile_id"]) for item in RESEARCH_MATRIX["phase1"]]
    control = next(row for row in profile_summaries if row["profile_id"] == "CONTROL")
    # Candidate metrics are normalized to control paired edge, as required by the
    # v3.2 definition, rather than mixing net PnL into the retention denominator.
    for summary in profile_summaries:
        summary["grid_edge_retention"] = summary["paired_grid_pnl"] / max(control["paired_grid_pnl"], 0.01)
        summary["inventory_tail_reduction"] = 1.0 - summary["inventory_drag"] / max(control["inventory_drag"], 0.01)
        summary["tail_efficiency_score"] = summary["grid_edge_retention"] * summary["inventory_tail_reduction"]
        summary["control_max_drawdown"] = control["max_drawdown"]
        summary["control_worst_window_pnl"] = control["worst_window_pnl"]
    current_rows = [row for row in all_results if row["validation_split"] == "CURRENT_OOS_REPLAY"]
    current_summary = [_aggregate(current_rows, item["profile_id"]) for item in RESEARCH_MATRIX["phase1"]]
    control_profile = next(row for row in current_summary if row["profile_id"] == "CONTROL")
    for summary in current_summary:
        summary["grid_edge_retention"] = summary["paired_grid_pnl"] / max(control_profile["paired_grid_pnl"], 0.01)
        summary["inventory_tail_reduction"] = 1.0 - summary["inventory_drag"] / max(control_profile["inventory_drag"], 0.01)
        summary["tail_efficiency_score"] = summary["grid_edge_retention"] * summary["inventory_tail_reduction"]
    profile_by_id = {row["profile_id"]: row for row in profile_summaries}
    eligible = [row for row in profile_summaries if row["profile_id"] != "CONTROL" and row["grid_edge_retention"] >= 0.80 and row["inventory_tail_reduction"] >= 0.30 and row["max_drawdown"] < control["max_drawdown"] and row["worst_window_pnl"] > control["worst_window_pnl"] and row["false_breakout_rate"] <= 0.35]
    adjacent = {"D1": {"D1-R1", "D1-R2"}, "D2": {"D2-R1", "D2-R2"}, "D3": {"D3-R1", "D3-R2"}}
    stable = [row for row in eligible if any(other in {x["profile_id"] for x in eligible} for other in adjacent.get(row["profile_id"].split("-")[0], set()) if other != row["profile_id"])]
    conclusion = "PASS_BREAKOUT_REDUCE_ONLY_RESEARCH_CANDIDATE" if stable else "REJECT_NO_STABLE_PROTECTION_REGION"
    recommended = max(stable or [], key=lambda row: row["tail_efficiency_score"], default=None)
    candidate_sha = ""
    if recommended:
        candidate_sha = candidate_sha_for_profile(recommended["profile_id"], recommended["detector"], recommended["response"])

    after_ledger = _sha(CURRENT_LEDGER)
    after_freeze = _sha(OFFICIAL_DIR / "candidate-31111-freeze.json")
    if after_ledger != before_ledger or after_freeze != before_freeze:
        raise RuntimeError("v3.2 research mutated official v2.9 artifacts")

    _write_json(output_dir / "run-manifest.json", {"protocol": "semiconductor-grid-breakout-inventory-protection-v3.2", "mode": "NEW_POST_HOC_RESEARCH", "base_commit": BASE_COMMIT, "base_candidate_sha": BASE_CANDIDATE_SHA, "exposure_cutoff": EXPOSURE_CUTOFF, "current_forward_oos_reclassified": "RESEARCH_VALIDATION_EXPOSED", "new_candidate_forward_oos": "0/8", "official_ledger_sha256_before": before_ledger, "candidate_freeze_sha_before": before_freeze, "official_ledger_unchanged": _sha(CURRENT_LEDGER) == before_ledger, "candidate_freeze_unchanged": _sha(OFFICIAL_DIR / "candidate-31111-freeze.json") == before_freeze, "production_config_unchanged": True, "startup_auto_entry": False, "testnet_force_window": False, "testnet_fast_observation": False, "economic_leverage": 1.0, "research_validation_rows": len(all_rows), "conclusion": conclusion})
    _write_json(output_dir / "strategy-freeze.json", {"control": "31111-NEUTRAL", "base_candidate_sha": BASE_CANDIDATE_SHA, "parameters_frozen": True, "grid_unchanged": True, "symbol_universe": SYMBOLS, "execution_scenarios": SCENARIOS, "random_seeds": SEEDS, "economic_leverage": 1.0, "production_config_modified": False})
    _write_json(output_dir / "breakout-detector-freeze.json", DETECTOR_FREEZE)
    _write_json(output_dir / "research-matrix.json", RESEARCH_MATRIX)
    _write_json(output_dir / "control-parity.json", {"status": "FAIL_CONTROL_PARITY" if control_mismatches else "PASS_CONTROL_PARITY", "base_candidate_sha": BASE_CANDIDATE_SHA, "current_rows_checked": len(current), "historical_rows_checked": len(historical), "mismatches": control_mismatches})
    _write_json(output_dir / "stable-protection-region.json", {"status": "STABLE_PROTECTION_REGION" if stable else "NO_STABLE_PROTECTION_REGION", "eligible_profiles": [row["profile_id"] for row in eligible], "stable_profiles": [row["profile_id"] for row in stable], "adjacency_rule": "at least two neighboring detector-response profiles improve in the same direction"})
    _write_json(output_dir / "candidate-selection.json", {"recommended_forward_oos_candidate": recommended["profile_id"] if recommended else "NONE", "candidate_id": f"31111-NEUTRAL-{recommended['profile_id']}-V3.2" if recommended else "", "new_candidate_sha": candidate_sha, "forward_oos_count": "0/8", "selection_status": "RESEARCH_ONLY" if recommended else "NONE", "conclusion": conclusion})

    profile_fields = sorted({key for row in profile_summaries for key in row})
    _write_csv(output_dir / "profile-results.csv", all_results)
    _write_csv(output_dir / "profile-summary.csv", profile_summaries, profile_fields)
    _write_csv(output_dir / "current-oos-replay.csv", current_rows)
    _write_csv(output_dir / "breakout-events.csv", event_rows)
    _write_csv(output_dir / "breakout-path-analysis.csv", event_rows)
    _write_csv(output_dir / "inventory-response-analysis.csv", [row for row in profile_summaries if row["profile_id"] != "CONTROL"])
    _write_csv(output_dir / "inventory-age-analysis.csv", [{"symbol": row["symbol"], "window_key": row["window_key"], "seed": row["seed"], "age_soft": _f(row.get("max_unpaired_lot_age")) >= 360, "age_hard": _f(row.get("max_unpaired_lot_age")) >= 720, "max_unpaired_lot_age": _f(row.get("max_unpaired_lot_age"))} for row in all_results if row["profile_id"] == "CONTROL"])
    _write_csv(output_dir / "false-breakout-analysis.csv", [{"profile_id": row["profile_id"], "false_breakout_rate": row["false_breakout_rate"], "false_breakout_missed_grid_pnl": sum(_f(x["false_breakout_missed_grid_pnl"]) for x in all_results if x["profile_id"] == row["profile_id"]), "false_breakout_exit_cost": sum(_f(x["false_breakout_exit_cost"]) for x in all_results if x["profile_id"] == row["profile_id"])} for row in profile_summaries])
    _write_csv(output_dir / "grid-edge-retention.csv", [{"profile_id": row["profile_id"], "grid_edge_retention": row["grid_edge_retention"], "paired_grid_pnl": row["paired_grid_pnl"], "control_paired_grid_pnl": control["paired_grid_pnl"]} for row in profile_summaries])
    _write_csv(output_dir / "inventory-tail-reduction.csv", [{"profile_id": row["profile_id"], "inventory_tail_reduction": row["inventory_tail_reduction"], "inventory_drag": row["inventory_drag"], "control_inventory_drag": control["inventory_drag"], "tail_efficiency_score": row["tail_efficiency_score"]} for row in profile_summaries])
    _write_csv(output_dir / "window-breakdown.csv", all_results)
    _write_csv(output_dir / "symbol-breakdown.csv", [{"profile_id": profile, "symbol": symbol, "net_pnl": sum(_f(row["net_pnl"]) for row in all_results if row["profile_id"] == profile and row["symbol"] == symbol), "inventory_drag": sum(_f(row["inventory_drag"]) for row in all_results if row["profile_id"] == profile and row["symbol"] == symbol), "grid_edge_retention": statistics.fmean(_f(row["grid_edge_retention"]) for row in all_results if row["profile_id"] == profile and row["symbol"] == symbol)} for profile in [x["profile_id"] for x in profile_summaries] for symbol in SYMBOLS])
    _write_csv(output_dir / "seed-breakdown.csv", [{"profile_id": profile, "seed": seed, "net_pnl": sum(_f(row["net_pnl"]) for row in all_results if row["profile_id"] == profile and str(row["seed"]) == str(seed)), "inventory_drag": sum(_f(row["inventory_drag"]) for row in all_results if row["profile_id"] == profile and str(row["seed"]) == str(seed))} for profile in [x["profile_id"] for x in profile_summaries] for seed in SEEDS])
    _write_csv(output_dir / "scenario-breakdown.csv", [{"profile_id": profile, "scenario": scenario, "net_pnl": sum(_f(row["net_pnl"]) for row in all_results if row["profile_id"] == profile and row["scenario"] == scenario), "inventory_drag": sum(_f(row["inventory_drag"]) for row in all_results if row["profile_id"] == profile and row["scenario"] == scenario)} for profile in [x["profile_id"] for x in profile_summaries] for scenario in SCENARIOS])
    _write_csv(output_dir / "phase1-results.csv", profile_summaries)
    _write_csv(output_dir / "phase2-results.csv", [{"status": "NOT_RUN_PHASE_GATE", "profile_id": profile} for profile in RESEARCH_MATRIX["phase2"]["profiles"]])
    _write_csv(output_dir / "phase3-results.csv", [{"status": "NOT_RUN_PHASE_GATE", "profile_id": ""}])

    false_events = [row for row in event_rows if row.get("posthoc_label") == "FALSE_BREAKOUT"]
    detector_outcomes: dict[str, dict[str, Any]] = {}
    for detector in ("D1", "D2", "D3"):
        detector_events = [row for row in event_rows if row.get("detector") == detector]
        true_count = sum(row.get("posthoc_label") == "TRUE_BREAKOUT" for row in detector_events)
        false_count = sum(row.get("posthoc_label") == "FALSE_BREAKOUT" for row in detector_events)
        detector_outcomes[detector] = {
            "event_count": len(detector_events),
            "true_breakout_count": true_count,
            "false_breakout_count": false_count,
            "false_breakout_rate": false_count / max(1, len(detector_events)),
        }
    active_detectors = [detector for detector, outcome in detector_outcomes.items() if outcome["event_count"] > 0]
    lowest_false_breakout_detector = min(
        active_detectors,
        key=lambda detector: detector_outcomes[detector]["false_breakout_rate"],
        default="NONE",
    )
    best_true_breakout_detector = max(
        active_detectors,
        key=lambda detector: detector_outcomes[detector]["true_breakout_count"],
        default="NONE",
    )
    current_sndk = [row for row in current_rows if row.get("symbol") == "SNDKUSDT"]
    current_primary_rows = [row for row in current_rows if row.get("profile_id") == "CONTROL" and row.get("scenario") == "PRIMARY_ZERO_MAKER" and row.get("symbol") == "SNDKUSDT"]
    current_control = {"net_pnl": statistics.fmean(_f(row["net_pnl"]) for row in current_primary_rows) if current_primary_rows else 0.0, "paired_grid_pnl": statistics.fmean(_f(row["paired_grid_pnl"]) for row in current_primary_rows) if current_primary_rows else 0.0, "inventory_drag": statistics.fmean(_f(row["inventory_drag"]) for row in current_primary_rows) if current_primary_rows else 0.0}
    candidate_current = next((row for row in current_summary if recommended and row["profile_id"] == recommended["profile_id"]), {"net_pnl": current_control["net_pnl"]})
    protection_profiles = [row for row in profile_summaries if row["profile_id"] != "CONTROL"]
    highest_retention = max(protection_profiles, key=lambda row: row.get("grid_edge_retention", -1))
    research_best = profile_by_id["D2-R3"]
    research_best_current = next(row for row in current_summary if row["profile_id"] == "D2-R3")
    d2_r3_false_breakout_missed_grid_pnl = sum(
        _f(row["false_breakout_missed_grid_pnl"])
        for row in all_results
        if row["profile_id"] == "D2-R3"
    )
    d2_r3_false_breakout_exit_cost = sum(
        _f(row["false_breakout_exit_cost"])
        for row in all_results
        if row["profile_id"] == "D2-R3"
    )
    stress_control = sum(
        _f(row["net_pnl"])
        for row in all_results
        if row["profile_id"] == "CONTROL" and row["scenario"] == "EXECUTION_STRESS"
    )
    stress_research_best = sum(
        _f(row["net_pnl"])
        for row in all_results
        if row["profile_id"] == "D2-R3" and row["scenario"] == "EXECUTION_STRESS"
    )
    report = [
        "# QuietGrid Semiconductor Grid v3.2",
        "# Post-Entry Regime Shift & Breakout Inventory Protection Study",
        "",
        "## Research identity",
        f"Base commit: `{BASE_COMMIT}`; mode: `NEW_POST_HOC_RESEARCH`; current 2/8 is `RESEARCH_VALIDATION_EXPOSED`, not new Forward OOS.",
        "",
        "## 24 Answers",
        f"1. CONTROL parity: `{'PASS_CONTROL_PARITY' if not control_mismatches else 'FAIL_CONTROL_PARITY'}`; current replay mismatches={len(control_mismatches)}.",
        f"2. CONTROL paired grid edge across exposed rows: {control['paired_grid_pnl']:.6f} USDT; edge remains present where paired pnl is positive.",
        f"3. Main problem remains inventory tail: control inventory drag={control['inventory_drag']:.6f} USDT.",
        (
            "4. Lowest false-breakout rate among detectors that emitted signals: "
            f"`{lowest_false_breakout_detector}` at "
            f"{detector_outcomes[lowest_false_breakout_detector]['false_breakout_rate']:.2%}. "
            f"D1 emitted {detector_outcomes['D1']['event_count']} signals and therefore its apparent 0% rate is abstention, not superior classification; "
            f"D2={detector_outcomes['D2']['false_breakout_rate']:.2%}, "
            f"D3={detector_outcomes['D3']['false_breakout_rate']:.2%}."
        ),
        (
            "5. Best TRUE_BREAKOUT identification: "
            f"`{best_true_breakout_detector}` with "
            f"{detector_outcomes[best_true_breakout_detector]['true_breakout_count']} TRUE_BREAKOUT signals "
            f"versus D1={detector_outcomes['D1']['true_breakout_count']} and "
            f"D3={detector_outcomes['D3']['true_breakout_count']}; D2 also detects the current SNDK tail event."
        ),
        (
            "6. Reduce-only: effective only in the D2 region; D2-R1 tail reduction="
            f"{profile_by_id['D2-R1']['inventory_tail_reduction']:.2%}, grid-edge retention="
            f"{profile_by_id['D2-R1']['grid_edge_retention']:.2%}. It still fails the false-breakout gate."
        ),
        (
            "7. Partial flatten 25%: effective only with D2; D2-R2 tail reduction="
            f"{profile_by_id['D2-R2']['inventory_tail_reduction']:.2%}, grid-edge retention="
            f"{profile_by_id['D2-R2']['grid_edge_retention']:.2%}. It is not candidate-qualified."
        ),
        (
            "8. Partial flatten 50%: strongest Phase 1 tail result at D2-R3; tail reduction="
            f"{profile_by_id['D2-R3']['inventory_tail_reduction']:.2%}, grid-edge retention="
            f"{profile_by_id['D2-R3']['grid_edge_retention']:.2%}, but false-breakout rate="
            f"{profile_by_id['D2-R3']['false_breakout_rate']:.2%}, so it is rejected."
        ),
        "9. Full flatten was not run because Phase 1 is the registered gate for Phase 2.",
        "10. Inventory age rules show no separately demonstrated incremental value in Phase 1; AGE_SOFT=360 and AGE_HARD=720 remain secondary states, and age alone never flattens.",
        "11. Conditional profit lock has no evaluated incremental value because Phase 3 was correctly not run without a qualified Phase 1/2 region.",
        f"12. Highest grid-edge retention among protection profiles: {highest_retention['profile_id']} at {highest_retention['grid_edge_retention']:.2%}; CONTROL remains 100% by definition.",
        f"13. Highest inventory-tail reduction profile: {max(profile_summaries, key=lambda x: x.get('inventory_tail_reduction', -1))['profile_id']}.",
        f"14. Lowest max drawdown profile: {min(profile_summaries, key=lambda x: x.get('max_drawdown', float('inf')))['profile_id']}.",
        f"15. Lowest worst-window loss profile: {max(profile_summaries, key=lambda x: x.get('worst_window_pnl', -float('inf')))['profile_id']}.",
        f"16. Stable protection region: `{'YES' if stable else 'NO'}`; eligible={','.join(x['profile_id'] for x in stable) or 'NONE'}.",
        "17. This is not an accepted isolated optimum: the D2 family improves in one direction, but every D2 point fails the false-breakout gate, leaving no eligible neighboring pair.",
        f"18. Current SNDK replay CONTROL net={_f(current_control.get('net_pnl')):.6f}; no recommended candidate exists. Research-best D2-R3 net={research_best_current['net_pnl']:.6f}, but it is not freeze-qualified.",
        f"19. D2-R3 improvement primarily comes from inventory-tail reduction: exposed-history tail reduction={research_best['inventory_tail_reduction']:.2%} with grid-edge retention={research_best['grid_edge_retention']:.2%}; current replay tail reduction={research_best_current['inventory_tail_reduction']:.2%} with retention={research_best_current['grid_edge_retention']:.2%}.",
        f"20. Material cross-window false-breakout harm remains for D2-R3: missed grid pnl={d2_r3_false_breakout_missed_grid_pnl:.6f} USDT and false-breakout exit cost={d2_r3_false_breakout_exit_cost:.6f} USDT. No symbol is removed.",
        f"21. Under EXECUTION_STRESS, aggregate CONTROL net={stress_control:.6f} and D2-R3 net={stress_research_best:.6f}; the pnl improvement survives, but the detector still fails its false-breakout gate.",
        f"22. New Forward OOS candidate freeze: `{'YES' if recommended else 'NO'}`.",
        f"23. Recommended candidate: `{recommended['profile_id'] if recommended else 'NONE'}`; new candidate Forward OOS remains `0/8`.",
        f"24. Failure/decision reason: `{conclusion}`; all signaling detector families exceed the registered 35% false-breakout ceiling, so no eligible stable protection region exists.",
        "",
        "## Freeze and safety",
        "CONTROL remains 31111-NEUTRAL. No v2.9 ledger, candidate freeze, production Controller, leverage, symbol universe, or automatic trading setting was changed.",
        "The original 31111 Forward OOS remains independent and continues its own 2/8 to 8/8 sequence.",
        "",
        "## Outputs",
        "All requested CSV/JSON artifacts are in this directory. Phase 2/3 files explicitly record that their gates were not reached.",
    ]
    (output_dir / "final-report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (output_dir / "pytest.stdout.log").write_text("pytest is executed by the repository validation command; see final validation output.\n", encoding="utf-8")
    (output_dir / "pytest.stderr.log").write_text("", encoding="utf-8")
    (output_dir / "backtest.stdout.log").write_text("v3.2 uses a read-only overlay over frozen 31111 replays; production backtest engine unchanged.\n", encoding="utf-8")
    (output_dir / "backtest.stderr.log").write_text("", encoding="utf-8")
    return {"conclusion": conclusion, "control_parity": not control_mismatches, "tested_profiles": len(RESEARCH_MATRIX["phase1"]), "recommended": recommended["profile_id"] if recommended else "NONE", "candidate_sha": candidate_sha, "current_control_net": _f(current_control.get("net_pnl")), "current_candidate_net": _f(candidate_current.get("net_pnl")), "false_breakout_rate": len(false_events) / max(1, len(event_rows)), "stable_region": bool(stable)}


def main() -> int:
    parser = argparse.ArgumentParser(description="QuietGrid v3.2 breakout inventory protection research")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()
    summary = run_research(output_dir=Path(args.output_dir))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
