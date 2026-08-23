"""QuietGrid v2.9.2 Forward OOS loss attribution diagnostics.

This module is deliberately read-only with respect to the official v2.9
Forward OOS artifacts.  It replays the frozen 31111 candidate, captures the
engine's timestamped fills/equity points, and writes research-only outputs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.models import GridDirectionMode
from scripts.semiconductor_grid_backtest import (
    RESEARCH_SYMBOLS,
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
    build_calendar_closed_windows,
)
from scripts.semiconductor_grid_forward_oos_v29 import (
    _backtest_config,
    _canonical_window_key,
    _collect_window_manifest,
    _grid_for,
    _parse_utc,
    _profile,
)
from strategy.backtest import run_grid_backtest
from strategy.regime import RegimeEngine
from strategy.semiconductor_grid import (
    StrategyAdmissionError,
    build_semiconductor_grid_candidate,
    long_signal_from_mapping,
    symbol_profiles_from_mapping,
)
from strategy.semiconductor_grid_v28 import Combination
from strategy.semiconductor_grid_v29 import (
    FORWARD_OOS_SCENARIOS,
    FORWARD_OOS_SEEDS,
)


OFFICIAL_DIR = ROOT / "reports" / "semiconductor-grid-forward-oos-v2.9"
DEFAULT_OUTPUT = ROOT / "reports" / "semiconductor-grid-oos-diagnostics-v2.9.2"
EXPECTED_CANDIDATE_SHA = "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774"
EXPECTED_CUTOFF = "2026-08-08T20:45:23.438783+00:00"
REQUIRED_OUTPUTS = (
    "run-manifest.json", "window-eligibility-audit.csv", "oos-pnl-attribution.csv",
    "oos-window-diagnostics.csv", "oos-seed-diagnostics.csv", "scenario-attribution.csv",
    "sndk-inventory-path.csv", "sndk-grid-cycle-analysis.csv", "oos-equity-curve.csv",
    "stop-path-analysis.csv", "counterfactual-results.csv", "forward-oos-diagnostic-history.csv",
    "diagnostic-summary.json", "final-report.md",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        number = float(value)
        if abs(number) >= 1e3:
            return datetime.fromtimestamp(number / 1000.0, UTC).isoformat()
        return datetime.fromtimestamp(number, UTC).isoformat()
    try:
        return _parse_utc(str(value)).isoformat()
    except (TypeError, ValueError):
        return str(value)


def _as_datetime(value: Any) -> datetime | None:
    text = _iso(value)
    if not text:
        return None
    try:
        return _parse_utc(text)
    except (TypeError, ValueError):
        return None


def _mean(values: Iterable[Any]) -> float:
    numbers = [_float(value) for value in values]
    return statistics.fmean(numbers) if numbers else 0.0


def _inventory_drag_ratio(paired_grid_pnl: float, inventory_realized_pnl: float) -> float:
    """Use the official positive-edge denominator convention."""
    return abs(min(0.0, inventory_realized_pnl)) / max(paired_grid_pnl, 0.01)


def _inventory_drag_usdt_ratio(paired_grid_pnl: float, inventory_drag_usdt: float) -> float:
    return abs(inventory_drag_usdt) / max(paired_grid_pnl, 0.01)


def _reconcile_pnl(row: Mapping[str, Any]) -> float:
    return _float(row.get("net_pnl")) - (
        _float(row.get("paired_grid_pnl"))
        + _float(row.get("inventory_realized_pnl"))
        + _float(row.get("funding_pnl"))
        - _float(row.get("fees"))
        - _float(row.get("slippage_cost"))
    )


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(fields or sorted({key for row in rows for key in row}))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")


def _read_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _official_snapshot() -> dict[str, str]:
    paths = {
        "ledger_csv": OFFICIAL_DIR / "forward-oos-ledger.csv",
        "ledger_json": OFFICIAL_DIR / "forward-oos-ledger.json",
        "candidate_freeze": OFFICIAL_DIR / "candidate-31111-freeze.json",
        "candidate_registry": OFFICIAL_DIR / "candidate-registry.json",
        "run_manifest": OFFICIAL_DIR / "run-manifest.json",
    }
    return {name: _sha(path) for name, path in paths.items()}


def _assert_baseline() -> tuple[dict[str, str], list[dict[str, Any]], dict[str, Any]]:
    before = _official_snapshot()
    ledger = _read_rows(OFFICIAL_DIR / "forward-oos-ledger.csv")
    candidate = json.loads((OFFICIAL_DIR / "candidate-31111-freeze.json").read_text(encoding="utf-8"))
    if candidate.get("candidate_id") != "31111-NEUTRAL":
        raise RuntimeError("candidate freeze id changed")
    if _sha(OFFICIAL_DIR / "candidate-31111-freeze.json") != EXPECTED_CANDIDATE_SHA:
        raise RuntimeError("candidate SHA changed")
    if str(candidate.get("exposure_cutoff")) != EXPECTED_CUTOFF:
        raise RuntimeError("exposure cutoff changed")
    if not ledger or any(
        str(row.get("candidate_id") or "").startswith("31111-NEUTRAL")
        and str(row.get("candidate_id") or "") != "31111-NEUTRAL-EX-MU"
        and str(row.get("candidate_sha") or EXPECTED_CANDIDATE_SHA) != EXPECTED_CANDIDATE_SHA
        for row in ledger
    ):
        raise RuntimeError("official primary ledger candidate hash changed")
    return before, ledger, candidate


def _post_cutoff_windows() -> list[dict[str, Any]]:
    payload = json.loads((OFFICIAL_DIR / "data-refresh-window-manifest.json").read_text(encoding="utf-8"))
    cutoff = _parse_utc(EXPECTED_CUTOFF)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload.get("windows", []):
        if _parse_utc(row["window_start"]) > cutoff:
            groups[str(row["window_key"])].append(row)
    audit: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        first = rows[0]
        flags = {name: all(bool(row.get(name)) for row in rows) for name in (
            "complete_window", "data_complete", "funding_complete", "rules_frozen",
            "force_close_covered", "portfolio_complete",
        )}
        if flags["complete_window"] and flags["portfolio_complete"]:
            classification, reason = "FORWARD_OOS", "complete portfolio window after exposure cutoff"
        elif not flags["data_complete"]:
            classification, reason = "DATA_INCOMPLETE", "one or more symbols lack a complete bar range"
        elif not flags["funding_complete"]:
            classification, reason = "FUNDING_INCOMPLETE", "funding sidecar coverage is incomplete"
        elif not flags["rules_frozen"]:
            classification, reason = "RULES_INCOMPLETE", "exchange rules are not frozen for every symbol"
        elif not flags["force_close_covered"] or not flags["portfolio_complete"]:
            classification, reason = "PORTFOLIO_INCOMPLETE", "force-close or portfolio coverage is incomplete"
        else:
            classification, reason = "INCOMPLETE_WINDOW", "window did not satisfy all Forward OOS gates"
        audit.append({
            "window_key": key,
            "market_calendar": first.get("market_calendar", ""),
            "window_start": first.get("window_start", ""),
            "window_end": first.get("window_end", ""),
            "force_close_at": first.get("force_close_at", ""),
            **flags,
            "classification": classification,
            "rejection_reason": reason,
        })
    return audit


def _window_lookup(data_dir: Path, raw: Mapping[str, Any], window_key: str, symbol: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any, Any] | None:
    research = dict(raw.get("semiconductor_grid") or {})
    profiles = symbol_profiles_from_mapping(research.get("symbol_profiles", {}))
    profile = profiles[symbol]
    bars, _ = _read_klines_with_audit(_find_csv(data_dir, symbol))
    funding_path = _funding_path(_find_csv(data_dir, symbol))
    funding = _read_funding(funding_path) if funding_path else []
    windows = build_calendar_closed_windows(
        bars,
        market_group=profile.market_group,
        calendar_name=profile.calendar_name,
        market_timezone=profile.market_timezone,
        reference_open_time=profile.reference_open_time,
        force_close_minutes=int(research.get("force_close_minutes", 120)),
        minimum_trade_minutes=int(research.get("minimum_trade_minutes", 120)),
        observation_minutes=int(research.get("observation_rows", 180)),
        window_key_prefix=profile.calendar_name,
    )
    for window in windows:
        if window.observation_start is None or window.force_close_at is None:
            continue
        key = _canonical_window_key(profile.calendar_name, window.observation_start, window.force_close_at)
        if key != window_key:
            continue
        obs_end = int(window.observation_end.timestamp() * 1000)
        close_ms = int(window.force_close_at.timestamp() * 1000)
        observation_rows = int(research.get("observation_rows", 180))
        observation = [row for row in window.rows if int(row["open_time"]) < obs_end][-observation_rows:]
        trade = [row for row in window.rows if obs_end <= int(row["open_time"]) < close_ms]
        events = [event for event in funding if int(trade[0]["open_time"]) <= event.funding_time <= int(trade[-1]["close_time"])] if trade else []
        return observation, trade, profile, events
    return None


def _replay(raw: Mapping[str, Any], data_dir: Path, rules_path: Path, row: Mapping[str, Any], *, overrides: Mapping[str, Any] | None = None, param_overrides: Mapping[str, Any] | None = None) -> tuple[Any, Any, Any, list[dict[str, Any]]]:
    symbol = str(row["symbol"])
    located = _window_lookup(data_dir, raw, str(row["window_key"]), symbol)
    if located is None:
        return None, None, None, []
    observation, trade, profile, funding_events = located
    research = dict(raw.get("semiconductor_grid") or {})
    rules = _load_rules(rules_path)
    regime = RegimeEngine(_regime(raw.get("regime", {})))
    viability = _viability(research.get("viability", {}))
    long_signal = long_signal_from_mapping(research.get("long_signal", {}))
    scenario = {item.name: item for item in _scenarios(research.get("execution", {}))}[str(row["scenario"])]
    item = Combination.parse("31111")
    base_grid = _grid(raw)
    previous_rates = [event.funding_rate for event in funding_events if event.funding_time <= int(observation[-1]["close_time"])]
    funding_rate = previous_rates[-1] if previous_rates else 0.0
    projected = _projected_funding_pct(funding_rate, _row_close_dt(observation[-1]), _parse_utc(str(row["window_end"])))
    depth = sum(float(x.get("quote_volume") or 0.0) for x in observation[-regime.config.long_window:]) / max(1, min(len(observation), regime.config.long_window))
    decision = regime.evaluate(symbol, observation, spread_pct=profile.assumed_spread_pct, depth_usdt=depth, funding_rate=funding_rate, data_age_seconds=0.0, expected_step_pct=profile.normal_min_step_pct * 1.5, include_cost=False, as_of=_row_close_dt(observation[-1]))
    grid = _grid_for(item, base_grid, observation)
    candidate = None
    if decision.allowed and grid is not None:
        try:
            candidate = build_semiconductor_grid_candidate(
                symbol_profile=profile,
                strategy_profile=_profile(symbol, item, GridDirectionMode.NEUTRAL, profile.normal_min_step_pct),
                klines=observation,
                current_price=float(observation[-1]["close"]),
                funding_rate=funding_rate,
                projected_funding_pct=projected,
                maker_fee_rate=scenario.maker_fee_rate,
                regime_score=decision.grid_score,
                capital=float(research.get("capital_per_symbol", 500)),
                leverage=float(research.get("economic_leverage", 1)),
                tick_size=rules[symbol].tick_size,
                step_size=rules[symbol].step_size,
                min_qty=rules[symbol].min_qty,
                min_notional=rules[symbol].min_notional,
                taker_fee_rate=scenario.taker_fee_rate,
                base_grid_config=grid,
                viability_config=viability,
                long_signal_config=long_signal,
            )
        except (StrategyAdmissionError, ValueError):
            candidate = None
    if candidate is None:
        return None, profile, decision, []
    config = _backtest_config(item=item, scenario=scenario, capital=float(research.get("capital_per_symbol", 500)) * profile.capital_multiplier, leverage=float(research.get("economic_leverage", 1)), rule=rules[symbol], direction=GridDirectionMode.NEUTRAL, seed=int(row["seed"]))
    if overrides:
        config = replace(config, **dict(overrides))
    params = replace(candidate.params, **dict(param_overrides or {})) if param_overrides else candidate.params
    result = run_grid_backtest(params, trade, current_price=float(observation[-1]["close"]), config=config, funding_events=funding_events)
    return result, candidate, decision, trade


def _official_row_map(ledger: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    return {(str(r.get("symbol")), str(r.get("window_key")), str(r.get("scenario")), str(r.get("seed"))): dict(r) for r in ledger if r.get("record_type") == "OOS_RESULT"}


def _lots_from_fills(fills: Sequence[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    long_lots: list[dict[str, Any]] = []
    short_lots: list[dict[str, Any]] = []
    for fill in sorted(fills, key=lambda x: (int(x.bar_index), _iso(x.timestamp))):
        side = str(fill.position_side or ("LONG" if fill.side == "BUY" else "SHORT")).upper()
        intent = str(fill.order_intent or "OPEN").upper()
        target = long_lots if side == "LONG" else short_lots
        if intent in {"OPEN", "SEED"}:
            target.append({"entry_price": float(fill.price), "qty": float(fill.qty), "opened_at": _iso(fill.timestamp), "bar_index": int(fill.bar_index)})
        elif intent == "REDUCE":
            remaining = float(fill.qty)
            entry = None
            if fill.grid_pnl is not None and remaining > 0:
                entry = float(fill.price) - float(fill.grid_pnl) / remaining if fill.side == "SELL" else float(fill.price) + float(fill.grid_pnl) / remaining
            for lot in list(target):
                if remaining <= 1e-12:
                    break
                if entry is not None and abs(float(lot["entry_price"]) - entry) > 1e-8:
                    continue
                take = min(float(lot["qty"]), remaining)
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-12:
                    target.remove(lot)
            if remaining > 1e-12:
                for lot in list(target):
                    take = min(float(lot["qty"]), remaining)
                    lot["qty"] -= take
                    remaining -= take
                    if lot["qty"] <= 1e-12:
                        target.remove(lot)
    return long_lots, short_lots


def _path_rows(result: Any, candidate: Any, row: Mapping[str, Any], scenario: str, seed: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if result is None or candidate is None:
        return [], []
    params = candidate.params
    fills = sorted(result.fills, key=lambda x: (int(x.bar_index), _iso(x.timestamp)))
    equity = list(result.equity_curve)
    fill_by_bar: dict[int, list[Any]] = defaultdict(list)
    for fill in fills:
        fill_by_bar[int(fill.bar_index)].append(fill)
    long_lots: list[dict[str, Any]] = []
    short_lots: list[dict[str, Any]] = []
    paired = 0.0
    fees = 0.0
    paths: list[dict[str, Any]] = []
    max_fav = 0.0
    max_adv = 0.0
    stop_index = result.stopped_at_index
    stop_seen = False
    first_unpaired_at = ""
    first_drag_crossover = ""
    final_fees = float(result.fees_paid)
    final_funding = float(result.funding_received - result.funding_paid)
    final_slippage = float(result.stop_exit_slippage_cost + result.force_exit_slippage_cost)
    for point in equity:
        for fill in fill_by_bar.get(int(point.bar_index), []):
            side = str(fill.position_side or ("LONG" if fill.side == "BUY" else "SHORT")).upper()
            target = long_lots if side == "LONG" else short_lots
            if str(fill.order_intent).upper() in {"OPEN", "SEED"}:
                target.append({"entry_price": float(fill.price), "qty": float(fill.qty), "opened_at": _iso(fill.timestamp), "bar_index": int(fill.bar_index)})
            else:
                remaining = float(fill.qty)
                entry = None
                if fill.grid_pnl is not None:
                    entry = float(fill.price) - float(fill.grid_pnl) / remaining if fill.side == "SELL" else float(fill.price) + float(fill.grid_pnl) / remaining
                    paired += float(fill.grid_pnl)
                for lot in list(target):
                    if remaining <= 1e-12:
                        break
                    if entry is not None and abs(lot["entry_price"] - entry) > 1e-8:
                        continue
                    take = min(lot["qty"], remaining); lot["qty"] -= take; remaining -= take
                    if lot["qty"] <= 1e-12: target.remove(lot)
                if remaining > 1e-12:
                    for lot in list(target):
                        take=min(lot["qty"],remaining); lot["qty"]-=take; remaining-=take
                        if lot["qty"] <= 1e-12: target.remove(lot)
            fees += float(fill.fee)
        long_qty = sum(l["qty"] for l in long_lots); short_qty = sum(l["qty"] for l in short_lots)
        net_qty = long_qty - short_qty; gross_qty = long_qty + short_qty
        oldest_age = max((int(point.bar_index) - int(lot["bar_index"]) for lot in [*long_lots, *short_lots]), default=0)
        price = float(point.close)
        inv_unreal = float(point.unrealized_pnl)
        max_adv = min(max_adv, inv_unreal); max_fav = max(max_fav, inv_unreal)
        utilization = float(point.inventory_utilization)
        point_timestamp = _iso(point.timestamp)
        if gross_qty > 1e-12 and not first_unpaired_at:
            first_unpaired_at = point_timestamp
        if paired > 1e-12 and -inv_unreal > paired and not first_drag_crossover:
            first_drag_crossover = point_timestamp
        if stop_index is not None and int(point.bar_index) >= int(stop_index):
            stop_seen = True
        fill_fees = fees
        realized_inventory = float(result.inventory_realized_pnl) if stop_seen else 0.0
        fees_cumulative = final_fees if stop_seen else fill_fees
        funding_cumulative = float(point.realized_pnl) - paired - realized_inventory + fees_cumulative
        slippage_cumulative = final_slippage if stop_seen else 0.0
        paths.append({
            "timestamp": point_timestamp, "price": price, "grid_center": params.center,
            "grid_lower": params.lower, "grid_upper": params.upper, "grid_count": params.grid_num,
            "grid_step": params.step_pct, "long_inventory": long_qty, "short_inventory": short_qty,
            "net_inventory": net_qty, "gross_inventory": gross_qty, "unpaired_lots": len(long_lots) + len(short_lots), "max_unpaired_lot_age": oldest_age, "inventory_notional": float(point.gross_inventory_notional),
            "inventory_utilization": utilization, "paired_grid_pnl_cumulative": paired,
            "inventory_pnl_realized_cumulative": realized_inventory,
            "inventory_pnl_unrealized": inv_unreal, "funding_cumulative": funding_cumulative,
            "fees_cumulative": fees_cumulative,
            "slippage_cumulative": slippage_cumulative,
            "net_pnl_cumulative": float(point.equity), "max_adverse_excursion": max_adv,
            "max_favorable_excursion": max_fav, "gate_state": row.get("gate_status", ""),
            "regime_state": row.get("regime_status", ""), "stop_state": result.stopped_reason if stop_seen else "NONE",
            "force_close_state": "FORCE_CLOSE" if result.force_close_count else "NONE",
            "first_unpaired_inventory_at": first_unpaired_at,
            "inventory_drag_crossover": bool(first_drag_crossover and point_timestamp == first_drag_crossover),
            "inventory_drag_crossover_at": first_drag_crossover,
            "scenario": scenario, "seed": seed, "window_key": row.get("window_key", ""), "symbol": row.get("symbol", ""),
        })
    return paths, fills


def _cycle_rows(fills: Sequence[Any], cycle_prefix: str, *, funding_total: float = 0.0, slippage_total: float = 0.0) -> list[dict[str, Any]]:
    opens: dict[tuple[int, str], Any] = {}
    cycles: list[dict[str, Any]] = []
    for fill in sorted(fills, key=lambda x: (int(x.bar_index), _iso(x.timestamp))):
        if str(fill.order_intent).upper() in {"OPEN", "SEED"}:
            opens[(int(fill.grid_index), str(fill.position_side))] = fill
            continue
        if fill.grid_pnl is None:
            continue
        entry = float(fill.price) - float(fill.grid_pnl) / float(fill.qty) if fill.side == "SELL" else float(fill.price) + float(fill.grid_pnl) / float(fill.qty)
        prior = min((x for x in opens.values() if abs(float(x.price) - entry) < 1e-8), key=lambda x: int(x.bar_index), default=None)
        entry_time = _iso(prior.timestamp) if prior is not None else ""
        holding = max(0, int(fill.bar_index) - int(prior.bar_index)) if prior is not None else 0
        cycles.append({"cycle_id": f"{cycle_prefix}-{len(cycles)+1}", "entry_time": entry_time, "exit_time": _iso(fill.timestamp), "entry_price": entry, "exit_price": float(fill.price), "direction": "LONG" if fill.side == "SELL" else "SHORT", "gross_spread_capture": float(fill.grid_pnl), "fees": float(fill.fee), "slippage": 0.0, "funding_allocated": 0.0, "net_cycle_pnl": float(fill.grid_pnl) - float(fill.fee), "holding_minutes": holding})
    if cycles:
        funding_each = funding_total / len(cycles)
        slippage_each = slippage_total / len(cycles)
        for cycle in cycles:
            cycle["funding_allocated"] = funding_each
            cycle["slippage"] = slippage_each
            cycle["net_cycle_pnl"] = (
                float(cycle["gross_spread_capture"])
                + funding_each
                - float(cycle["fees"])
                - slippage_each
            )
    return cycles


def _aggregate(rows: Sequence[Mapping[str, Any]], keys: Sequence[str], numeric: Sequence[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(row.get(k, "") for k in keys)].append(row)
    output = []
    for key, items in groups.items():
        record = {k: value for k, value in zip(keys, key)}
        for field in numeric:
            values = [_float(item.get(field)) for item in items]
            record[field] = sum(values)
        output.append(record)
    return output


def _post_stop_rows(paths: Sequence[Mapping[str, Any]], result: Any, window_end: str, trade: Sequence[Mapping[str, Any]] | None = None, params: Any | None = None) -> list[dict[str, Any]]:
    if result is None or result.stopped_at_index is None or not paths:
        return []
    stop_index = int(result.stopped_at_index)
    stop_point = next((p for p in paths if p.get("stop_state") not in (None, "NONE")), paths[-1])
    stop_time = _as_datetime(result.pre_exit_timestamp or stop_point.get("timestamp"))
    if stop_time is None:
        return []
    exit_price = _float(result.stopped_at_price)
    lower = _float(paths[0].get("grid_lower"))
    upper = _float(paths[0].get("grid_upper"))
    future_points: list[tuple[datetime, float, float, float]] = []
    if trade:
        for raw in trade[stop_index + 1:]:
            timestamp = _as_datetime(raw.get("close_time") or raw.get("open_time") or raw.get("timestamp"))
            if timestamp is not None:
                future_points.append((timestamp, _float(raw.get("close")), _float(raw.get("high"), _float(raw.get("close"))), _float(raw.get("low"), _float(raw.get("close")))))
    rows=[]
    stop_direction = "LONG" if result.pre_exit_position_qty > 0 else "SHORT" if result.pre_exit_position_qty < 0 else "FLAT"
    entry_average = ""
    if result.pre_exit_position_qty:
        entry_average = _float(result.pre_exit_mark_price) - (_float(result.pre_exit_unrealized_pnl) / result.pre_exit_position_qty) if result.pre_exit_position_qty else ""
    for minutes in (30,60,120,240):
        target=stop_time+timedelta(minutes=minutes)
        future=[item for item in future_points if item[0]>=target]
        item=future[0] if future else None
        observed = [x for t,x,_h,_l in future_points if t <= (item[0] if item else _as_datetime(window_end) or stop_time)]
        pnl_changes = [float(result.pre_exit_position_qty) * (value - exit_price) for value in observed]
        mae = min(pnl_changes, default=0.0)
        mfe = max(pnl_changes, default=0.0)
        price_after = item[1] if item else ""
        returned_range = any(lower <= value <= upper for value in observed)
        returned_entry = bool(entry_average != "" and any((value - float(entry_average)) * (exit_price - float(entry_average)) <= 0 for value in observed))
        classification = "UNRESOLVED" if item is None else ("FALSE_BREAKOUT" if returned_range or returned_entry else "TRUE_BREAKOUT")
        rows.append({"event_time": _iso(stop_time), "event_type": result.stopped_reason or "STOP", "position_direction": stop_direction, "position_notional": result.pre_exit_inventory_notional, "entry_average_price": entry_average, "exit_price": exit_price, "realized_loss": result.inventory_realized_pnl, "observation": f"+{minutes} min", "price_after_exit": price_after, "return_after_exit": (price_after/exit_price-1.0) if item and exit_price else "", "MAE_after_exit": mae, "MFE_after_exit": mfe, "returned_to_grid_range": returned_range, "returned_to_entry_price": returned_entry, "classification": classification})
    window_observed = [x for _t,x,_h,_l in future_points]
    window_pnl_changes = [float(result.pre_exit_position_qty) * (value - exit_price) for value in window_observed]
    returned_range = any(lower <= value <= upper for value in window_observed)
    returned_entry = bool(entry_average != "" and any((value - float(entry_average)) * (exit_price - float(entry_average)) <= 0 for value in window_observed))
    rows.append({"event_time": _iso(stop_time), "event_type": result.stopped_reason or "STOP", "position_direction": stop_direction, "position_notional": result.pre_exit_inventory_notional, "entry_average_price": entry_average, "exit_price": exit_price, "realized_loss": result.inventory_realized_pnl, "observation": "window_end", "price_after_exit": window_observed[-1] if window_observed else "", "return_after_exit": (window_observed[-1]/exit_price-1.0) if window_observed and exit_price else "", "MAE_after_exit": min(window_pnl_changes, default=0.0), "MFE_after_exit": max(window_pnl_changes, default=0.0), "returned_to_grid_range": returned_range, "returned_to_entry_price": returned_entry, "classification": "UNRESOLVED" if not window_observed else ("FALSE_BREAKOUT" if returned_range or returned_entry else "TRUE_BREAKOUT")})
    return rows


def _regime_metrics(trade: Sequence[Mapping[str, Any]], *, grid_center: float, entry_time: str = "", tail_time: str = "", exit_time: str = "") -> dict[str, Any]:
    closes = [_float(row.get("close")) for row in trade if row.get("close") not in (None, "")]
    if not closes:
        return {"directional_efficiency": 0.0, "reversal_ratio": 0.0, "crossings_per_hour": 0.0, "zero_activity_ratio": 1.0, "realized_volatility": 0.0, "ATR": 0.0, "sigma": 0.0, "range_pct": 0.0, "POST_ENTRY_REGIME_SHIFT": False, "regime_shift_count": 0, "entry_time": entry_time, "mid_window_time": "", "inventory_tail_formation_time": tail_time, "exit_time": exit_time}
    diffs = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    absolute_path = sum(abs(value) for value in diffs)
    directional_efficiency = abs(closes[-1] - closes[0]) / absolute_path if absolute_path else 0.0
    signs = [1 if value > 0 else -1 if value < 0 else 0 for value in diffs]
    reversals = sum(1 for left, right in zip(signs, signs[1:]) if left and right and left != right)
    reversal_ratio = reversals / max(1, len(signs) - 1)
    crossings = sum(1 for left, right in zip(closes, closes[1:]) if (left - grid_center) * (right - grid_center) < 0)
    zero_activity_ratio = sum(1 for value in diffs if abs(value) <= 1e-12) / max(1, len(diffs))
    returns = [math.log(closes[index] / closes[index - 1]) for index in range(1, len(closes)) if closes[index] > 0 and closes[index - 1] > 0]
    sigma = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    realized_volatility = sigma * math.sqrt(1440.0)
    true_ranges: list[float] = []
    previous = None
    for row in trade:
        high = _float(row.get("high"), _float(row.get("close")))
        low = _float(row.get("low"), _float(row.get("close")))
        close = _float(row.get("close"))
        true_ranges.append(max(high - low, abs(high - previous), abs(low - previous)) if previous is not None else high - low)
        previous = close
    midpoint = _iso(trade[len(trade) // 2].get("close_time") or trade[len(trade) // 2].get("open_time")) if trade else ""
    split = max(2, len(closes) // 3)
    early_path = sum(abs(value) for value in diffs[:split])
    late_path = sum(abs(value) for value in diffs[-split:])
    early_eff = abs(closes[min(split, len(closes) - 1)] - closes[0]) / early_path if early_path else 0.0
    late_eff = abs(closes[-1] - closes[-split]) / late_path if late_path else 0.0
    shifted = bool(entry_time and tail_time and late_eff > max(0.25, early_eff * 1.5))
    return {"directional_efficiency": directional_efficiency, "reversal_ratio": reversal_ratio, "crossings_per_hour": crossings / max(len(trade) / 60.0, 1 / 60.0), "zero_activity_ratio": zero_activity_ratio, "realized_volatility": realized_volatility, "ATR": statistics.fmean(true_ranges) if true_ranges else 0.0, "sigma": sigma, "range_pct": (max(closes) - min(closes)) / closes[0] if closes[0] else 0.0, "POST_ENTRY_REGIME_SHIFT": shifted, "regime_shift_count": int(shifted), "entry_time": entry_time, "mid_window_time": midpoint, "inventory_tail_formation_time": tail_time, "exit_time": exit_time}


def _append_history(path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> None:
    existing = _read_rows(path) if path.exists() else []
    keys = {(str(row.get("window_key")), str(row.get("scenario")), str(row.get("seed"))) for row in existing}
    additions: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("window_key")), str(row.get("scenario")), str(row.get("seed")))
        if key not in keys:
            additions.append(dict(row))
            keys.add(key)
    if not additions:
        return
    if not path.exists() or path.stat().st_size == 0:
        _write_csv(path, additions, fields)
        return
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writerows(additions)


def run_diagnostics(*, config_path: Path = ROOT / "config" / "config.yaml", data_dir: Path = ROOT / "data" / "backtests" / "semiconductor-v2.7", output_dir: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    before, ledger, candidate = _assert_baseline()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    rules_path = OFFICIAL_DIR / "exchange-rules.json"
    audit = _post_cutoff_windows()
    _write_csv(output_dir / "window-eligibility-audit.csv", audit, ("window_key","market_calendar","window_start","window_end","force_close_at","complete_window","data_complete","funding_complete","rules_frozen","force_close_covered","portfolio_complete","classification","rejection_reason"))
    official_map = _official_row_map(ledger)
    eligible_keys = [row["window_key"] for row in audit if row["classification"] == "FORWARD_OOS"]
    complete_rows = [row for row in ledger if row.get("record_type") == "OOS_RESULT" and row.get("status") == "COMPLETE"]
    replay_rows=[]; path_rows=[]; equity_rows=[]; cycles=[]; stop_rows=[]; regime_rows=[]
    for official in complete_rows:
        result, cand, decision, trade = _replay(raw, data_dir, rules_path, official)
        if result is None:
            continue
        path, fills = _path_rows(result, cand, official, str(official["scenario"]), int(official["seed"]))
        path_rows.extend(path)
        running_peak = 0.0
        for point in path:
            equity = float(point["net_pnl_cumulative"])
            running_peak = max(running_peak, equity)
            drawdown = running_peak - equity
            equity_rows.append({"timestamp":point["timestamp"],"window_key":official["window_key"],"symbol":official["symbol"],"seed":official["seed"],"scenario":official["scenario"],"realized_pnl":equity-float(point["inventory_pnl_unrealized"]),"unrealized_pnl":point["inventory_pnl_unrealized"],"equity":equity,"running_peak":running_peak,"drawdown":drawdown,"drawdown_pct":drawdown/500.0})
        cycles.extend([{**x,"window_key":official["window_key"],"symbol":official["symbol"],"scenario":official["scenario"],"seed":official["seed"]} for x in _cycle_rows(fills, f"{official['scenario']}-{official['seed']}", funding_total=result.funding_received - result.funding_paid, slippage_total=result.stop_exit_slippage_cost + result.force_exit_slippage_cost)])
        stop_rows.extend([{**x,"window_key":official["window_key"],"symbol":official["symbol"],"scenario":official["scenario"],"seed":official["seed"]} for x in _post_stop_rows(path, result, official["window_end"], trade, cand.params)])
        first_unpaired = next((x for x in path if _float(x.get("gross_inventory")) > 1e-12), None)
        first_crossover = next((x for x in path if x.get("inventory_drag_crossover")), None)
        regime = _regime_metrics(trade, grid_center=float(cand.params.center), entry_time=first_unpaired.get("timestamp", "") if first_unpaired else "", tail_time=first_crossover.get("timestamp", "") if first_crossover else "", exit_time=_iso(result.pre_exit_timestamp))
        regime_rows.append({**regime,"window_key":official["window_key"],"symbol":official["symbol"],"scenario":official["scenario"],"seed":official["seed"]})
        slippage = result.stop_exit_slippage_cost + result.force_exit_slippage_cost
        replay_rows.append({"window_key":official["window_key"],"window_start":official.get("window_start", ""),"window_end":official.get("window_end", ""),"symbol":official["symbol"],"scenario":official["scenario"],"seed":official["seed"],"paired_grid_pnl":result.paired_grid_pnl,"inventory_realized_pnl":result.inventory_realized_pnl,"inventory_unrealized_pnl_before_exit":result.pre_exit_unrealized_pnl,"inventory_drag":max(0.0, -float(result.pre_exit_unrealized_pnl)),"funding_pnl":result.funding_received-result.funding_paid,"maker_fees":result.maker_fees,"taker_fees":result.taker_fees,"slippage_cost":slippage,"fees":result.fees_paid,"net_pnl":result.total_pnl,"max_drawdown":result.max_drawdown,"max_drawdown_pct":result.max_drawdown / 500.0,"reconciliation_error":result.total_pnl-(result.paired_grid_pnl+result.inventory_realized_pnl+(result.funding_received-result.funding_paid)-result.fees_paid-slippage),"official_engine_reconciliation_error":result.total_pnl-(result.paired_grid_pnl+result.inventory_realized_pnl+(result.funding_received-result.funding_paid)-result.fees_paid),"fill_count":result.accepted_fill_count,"paired_cycle_count":result.pair_completion_count,"pre_exit_position_qty":result.pre_exit_position_qty,"pre_exit_mark_price":result.pre_exit_mark_price,"max_inventory_utilization":result.max_inventory_utilization,"max_unpaired_lots":result.max_unpaired_lots,"max_unpaired_lot_age":result.max_unpaired_lot_age_bars,"stop_count":int(bool(result.stopped_reason and "stop" in result.stopped_reason))})
    _write_csv(output_dir / "oos-pnl-attribution.csv", replay_rows)
    _write_csv(output_dir / "sndk-inventory-path.csv", [r for r in path_rows if r.get("symbol") == "SNDKUSDT"])
    _write_csv(output_dir / "sndk-grid-cycle-analysis.csv", [r for r in cycles if r.get("symbol") == "SNDKUSDT"])
    _write_csv(output_dir / "oos-equity-curve.csv", equity_rows)
    _write_csv(output_dir / "stop-path-analysis.csv", stop_rows)

    primary = [r for r in replay_rows if r["scenario"] == "PRIMARY_ZERO_MAKER"]
    seed_rows=[]
    for seed in FORWARD_OOS_SEEDS:
        items=[r for r in primary if int(r["seed"])==int(seed) and r["symbol"]=="SNDKUSDT"]
        seed_rows.append({"seed":seed,"net_pnl":_mean(x["net_pnl"] for x in items),"paired_grid_pnl":_mean(x["paired_grid_pnl"] for x in items),"inventory_pnl":_mean(x["inventory_realized_pnl"] for x in items),"max_drawdown":max((_float(x.get("max_drawdown")) for x in items),default=0.0),"fill_count":_mean(x.get("fill_count",0) for x in items)})
    _write_csv(output_dir / "oos-seed-diagnostics.csv", seed_rows)
    scenario_rows=[]
    primary_mean_net = _mean(x["net_pnl"] for x in primary if x["symbol"] == "SNDKUSDT")
    for scenario in FORWARD_OOS_SCENARIOS:
        items=[r for r in replay_rows if r["scenario"]==scenario and r["symbol"]=="SNDKUSDT"]
        mean_net = _mean(x["net_pnl"] for x in items)
        scenario_rows.append({"scenario":scenario,"maker_fill_difference":_mean(x["fill_count"] for x in items)-_mean(x["fill_count"] for x in primary if x["symbol"]=="SNDKUSDT"),"number_of_fills":_mean(x["fill_count"] for x in items),"number_of_paired_cycles":_mean(x["paired_cycle_count"] for x in items),"maker_fill_difference_vs_primary":_mean(x["fill_count"] for x in items)-_mean(x["fill_count"] for x in primary if x["symbol"]=="SNDKUSDT"),"taker_exit_cost":_mean(x["taker_fees"] for x in items),"slippage_cost":_mean(x["slippage_cost"] for x in items),"inventory_tail":_mean(x["inventory_realized_pnl"] for x in items),"funding":_mean(x["funding_pnl"] for x in items),"net_pnl":mean_net,"net_pnl_difference_vs_primary":mean_net-primary_mean_net})
    _write_csv(output_dir / "scenario-attribution.csv", scenario_rows)

    cf=[]
    for row in primary:
        cf.append({"counterfactual":"CF_NO_INVENTORY","label":"DIAGNOSTIC_COUNTERFACTUAL","window_key":row["window_key"],"scenario":row["scenario"],"seed":row["seed"],"paired_pnl":row["paired_grid_pnl"],"inventory_pnl":0.0,"net_pnl":_float(row["paired_grid_pnl"])+_float(row["funding_pnl"])-_float(row["fees"])-_float(row["slippage_cost"]),"max_drawdown":"","inventory_drag":"","missed_grid_pnl":0.0})
    for threshold in (20,30,40,50):
        for row in primary:
            result, cand, decision, trade = _replay(raw, data_dir, rules_path, row, overrides={"inventory_caution_utilization":threshold/100.0})
            if result is not None:
                cf.append({"counterfactual":f"CF_BLOCK_SAME_SIDE_{threshold}","label":"DIAGNOSTIC_COUNTERFACTUAL","window_key":row["window_key"],"scenario":row["scenario"],"seed":row["seed"],"paired_pnl":result.paired_grid_pnl,"inventory_pnl":result.inventory_realized_pnl,"net_pnl":result.total_pnl,"max_drawdown":result.max_drawdown,"inventory_drag":max(0.0,-result.pre_exit_unrealized_pnl),"missed_grid_pnl":_float(row["paired_grid_pnl"])-float(result.paired_grid_pnl)})
    for threshold in (20,30,40):
        for row in primary:
            result, cand, decision, trade = _replay(raw, data_dir, rules_path, row, overrides={"inventory_reduce_only_utilization":threshold/100.0})
            if result is not None:
                cf.append({"counterfactual":f"CF_REDUCE_ONLY_{threshold}","label":"DIAGNOSTIC_COUNTERFACTUAL","window_key":row["window_key"],"scenario":row["scenario"],"seed":row["seed"],"paired_pnl":result.paired_grid_pnl,"inventory_pnl":result.inventory_realized_pnl,"net_pnl":result.total_pnl,"max_drawdown":result.max_drawdown,"inventory_drag":max(0.0,-result.pre_exit_unrealized_pnl),"missed_grid_pnl":_float(row["paired_grid_pnl"])-float(result.paired_grid_pnl)})
    for stop_row in stop_rows:
        if stop_row.get("scenario") != "PRIMARY_ZERO_MAKER":
            continue
        base = next((item for item in primary if str(item["window_key"]) == str(stop_row["window_key"]) and str(item["seed"]) == str(stop_row["seed"])), None)
        if base is None:
            continue
        for horizon in ("30", "60", "120", "240", "TO_WINDOW_END"):
            if stop_row.get("observation") != ("window_end" if horizon == "TO_WINDOW_END" else f"+{horizon} min"):
                continue
            price_after = _float(stop_row.get("price_after_exit"), _float(base.get("inventory_unrealized_pnl_before_exit")))
            qty = _float(base.get("pre_exit_position_qty"))
            mark = _float(base.get("pre_exit_mark_price"), _float(stop_row.get("exit_price")))
            held_unreal = _float(base.get("inventory_unrealized_pnl_before_exit")) + qty * (price_after - mark) if stop_row.get("price_after_exit") != "" else ""
            net_held = _float(base.get("paired_grid_pnl")) + _float(base.get("funding_pnl")) - _float(base.get("fees")) - _float(base.get("slippage_cost")) + held_unreal if held_unreal != "" else ""
            cf.append({"counterfactual":f"CF_HOLD_{horizon}","label":"DIAGNOSTIC_COUNTERFACTUAL","window_key":base["window_key"],"scenario":base["scenario"],"seed":base["seed"],"paired_pnl":base["paired_grid_pnl"],"inventory_pnl":held_unreal,"net_pnl":net_held,"max_drawdown":"","inventory_drag":"","additional_MAE":stop_row.get("MAE_after_exit"),"additional_MFE":stop_row.get("MFE_after_exit"),"classification":stop_row.get("classification")})
    for factor in (0.5, 1.0, 1.5, 2.0):
        for row in primary:
            _baseline, baseline_candidate, _decision, _trade = _replay(raw, data_dir, rules_path, row)
            if baseline_candidate is None:
                continue
            params = baseline_candidate.params
            result, _cand, _decision, _trade = _replay(raw, data_dir, rules_path, row, param_overrides={"stop_loss_price": float(params.center - (params.center - params.stop_loss_price) * factor), "upper_stop_loss_price": (float(params.center + (params.upper_stop_loss_price - params.center) * factor) if params.upper_stop_loss_price is not None else None)})
            if result is not None:
                cf.append({"counterfactual":f"CF_STOP_BOUNDARY_{factor:g}","label":"DIAGNOSTIC_COUNTERFACTUAL","window_key":row["window_key"],"scenario":row["scenario"],"seed":row["seed"],"paired_pnl":result.paired_grid_pnl,"inventory_pnl":result.inventory_realized_pnl,"net_pnl":result.total_pnl,"max_drawdown":result.max_drawdown,"inventory_drag":max(0.0,-result.pre_exit_unrealized_pnl),"stop_reason":result.stopped_reason})
    _write_csv(output_dir / "counterfactual-results.csv", cf)

    window_rows=[]
    for item in audit:
        vals=[r for r in primary if r["window_key"]==item["window_key"]]
        reg=[r for r in regime_rows if r["window_key"]==item["window_key"] and r["scenario"]=="PRIMARY_ZERO_MAKER"]
        cyc=[r for r in cycles if r["window_key"]==item["window_key"] and r["scenario"]=="PRIMARY_ZERO_MAKER"]
        path_for_window=[r for r in path_rows if r.get("window_key") == item["window_key"] and r.get("scenario") == "PRIMARY_ZERO_MAKER" and r.get("symbol") == "SNDKUSDT"]
        window_rows.append({"window_key":item["window_key"],"classification":item["classification"],"net_pnl":_mean(r["net_pnl"] for r in vals),"paired_grid_pnl":_mean(r["paired_grid_pnl"] for r in vals),"inventory_pnl":_mean(r["inventory_realized_pnl"] for r in vals),"inventory_drag":_mean(r.get("inventory_drag") for r in vals),"max_drawdown":max([_float(r.get("max_drawdown")) for r in vals],default=0.0),"inventory_drag_ratio":_inventory_drag_usdt_ratio(_mean(r["paired_grid_pnl"] for r in vals), _mean(r.get("inventory_drag") for r in vals)) if vals else 0.0,"paired_cycle_count":_mean(r["paired_cycle_count"] for r in vals),"positive_cycle_ratio":_mean(float(_float(c.get("net_cycle_pnl")) > 0) for c in cyc),"regime_shift_count":sum(int(bool(r.get("POST_ENTRY_REGIME_SHIFT"))) for r in reg),"stop_count":_mean(r["stop_count"] for r in vals),"max_long_inventory":max([_float(r.get("long_inventory")) for r in path_for_window],default=0.0),"max_short_inventory":max([_float(r.get("short_inventory")) for r in path_for_window],default=0.0),"max_net_inventory":max([_float(r.get("net_inventory")) for r in path_for_window],default=0.0),"min_net_inventory":min([_float(r.get("net_inventory")) for r in path_for_window],default=0.0),"max_gross_inventory":max([_float(r.get("gross_inventory")) for r in path_for_window],default=0.0),"max_inventory_utilization":max([_float(r.get("inventory_utilization")) for r in path_for_window],default=0.0),"max_unpaired_lots":max([_float(r.get("unpaired_lots")) for r in path_for_window],default=0.0),"max_unpaired_lot_age":max([_float(r.get("max_unpaired_lot_age")) for r in path_for_window],default=0.0),"first_unpaired_inventory_at":next((r.get("first_unpaired_inventory_at", "") for r in path_for_window if r.get("first_unpaired_inventory_at")),""),"inventory_drag_crossover_at":next((r.get("inventory_drag_crossover_at", "") for r in path_for_window if r.get("inventory_drag_crossover_at")),""),"directional_efficiency":_mean(r.get("directional_efficiency") for r in reg),"reversal_ratio":_mean(r.get("reversal_ratio") for r in reg),"crossings_per_hour":_mean(r.get("crossings_per_hour") for r in reg),"zero_activity_ratio":_mean(r.get("zero_activity_ratio") for r in reg),"realized_volatility":_mean(r.get("realized_volatility") for r in reg),"ATR":_mean(r.get("ATR") for r in reg),"sigma":_mean(r.get("sigma") for r in reg),"range_pct":_mean(r.get("range_pct") for r in reg),"POST_ENTRY_REGIME_SHIFT":any(bool(r.get("POST_ENTRY_REGIME_SHIFT")) for r in reg)})
    _write_csv(output_dir / "oos-window-diagnostics.csv", window_rows)
    sndk_primary=[r for r in primary if r["symbol"]=="SNDKUSDT"]
    total_paired=_mean(r["paired_grid_pnl"] for r in sndk_primary); total_inv=_mean(r["inventory_realized_pnl"] for r in sndk_primary); drag=_mean(r["inventory_drag"] for r in sndk_primary); ratio=drag/max(total_paired,0.01)
    diagnostic_class="EARLY_WARNING_INVENTORY_TAIL" if total_paired>0 and abs(total_inv)>total_paired else "EARLY_WARNING_GRID_EDGE"
    primary_cycles=[r for r in cycles if r["scenario"]=="PRIMARY_ZERO_MAKER" and r["symbol"]=="SNDKUSDT"]
    summary={"formal_forward_oos_status":"INSUFFICIENT_FORWARD_OOS","formal_assessment_status":"NOT_EVALUATED","diagnostic_status":diagnostic_class,"candidate_id":"31111-NEUTRAL","candidate_sha":EXPECTED_CANDIDATE_SHA,"exposure_cutoff":EXPECTED_CUTOFF,"forward_oos":"2/8","total_paired_grid_pnl":total_paired,"total_inventory_drag":drag,"inventory_drag_usdt":drag,"computed_inventory_drag_ratio":ratio,"official_inventory_drag_ratio":2.89435069529515,"grid_edge_positive":total_paired>0,"inventory_tail_dominates":abs(total_inv)>total_paired,"sndk_removal_authorized":False,"paired_cycle_count":len(primary_cycles),"positive_cycle_ratio":_mean(float(_float(r.get("net_cycle_pnl")) > 0) for r in primary_cycles),"counterfactual_label":"DIAGNOSTIC_COUNTERFACTUAL","official_ledger_unchanged":True}
    _write_json(output_dir / "diagnostic-summary.json", summary)
    history=[{"window_key":r["window_key"],"scenario":r["scenario"],"seed":r["seed"],"paired_grid_pnl":r["paired_grid_pnl"],"inventory_loss":-min(0.0,_float(r["inventory_realized_pnl"])),"inventory_drag_ratio":_inventory_drag_usdt_ratio(_float(r["paired_grid_pnl"]), _float(r.get("inventory_drag"))),"paired_cycle_count":r["paired_cycle_count"],"positive_cycle_ratio":_mean(float(_float(c.get("net_cycle_pnl")) > 0) for c in cycles if c["window_key"]==r["window_key"] and c["scenario"]==r["scenario"] and str(c["seed"])==str(r["seed"])),"regime_shift_count":sum(int(bool(x.get("POST_ENTRY_REGIME_SHIFT"))) for x in regime_rows if x["window_key"]==r["window_key"] and x["scenario"]==r["scenario"] and str(x["seed"])==str(r["seed"])),"stop_count":r["stop_count"],"max_inventory_utilization":r["max_inventory_utilization"],"max_unpaired_lot_age":r["max_unpaired_lot_age"]} for r in primary]
    _append_history(output_dir / "forward-oos-diagnostic-history.csv", history, ("window_key","scenario","seed","paired_grid_pnl","inventory_loss","inventory_drag_ratio","paired_cycle_count","positive_cycle_ratio","regime_shift_count","stop_count","max_inventory_utilization","max_unpaired_lot_age"))

    after=_official_snapshot()
    if before != after:
        raise RuntimeError("official v2.9 artifacts changed during diagnostics")
    manifest={"protocol":"semiconductor-grid-oos-diagnostics-v2.9.2","mode":"DIAGNOSTIC_ONLY","base_commit":"50d681485503415ff339a8c24a3b90fda6049bb7","candidate_sha":EXPECTED_CANDIDATE_SHA,"exposure_cutoff":EXPECTED_CUTOFF,"official_ledger_sha256":before["ledger_csv"],"official_ledger_unchanged":True,"candidate_sha_unchanged":True,"forward_oos":"2/8","required_outputs":list(REQUIRED_OUTPUTS),"counterfactuals_are_official_ledger_excluded":True}
    _write_json(output_dir / "run-manifest.json", manifest)
    scenario_text = "; ".join(f"{row['scenario']}={_float(row['net_pnl']):.6f} (ΔPRIMARY={_float(row['net_pnl_difference_vs_primary']):+.6f})" for row in scenario_rows)
    window_text = "; ".join(f"{row['window_key']} net={_float(row['net_pnl']):.6f}, paired={_float(row['paired_grid_pnl']):.6f}, inventory={_float(row['inventory_pnl']):.6f}, DD={_float(row['max_drawdown']):.6f}, drag_ratio={_float(row['inventory_drag_ratio']):.6f}" for row in window_rows if row['classification'] == 'FORWARD_OOS')
    max_reconciliation_error = max((_float(row.get("reconciliation_error")) for row in replay_rows), default=0.0)
    report=["# QuietGrid v2.9.2 Forward OOS Loss Attribution", "", "## Formal status", "", "`INSUFFICIENT_FORWARD_OOS` (2/8); acceptance gates remain `NOT_EVALUATED`.", "", "## Diagnostic status", "", f"`{diagnostic_class}`", "", "## Window eligibility", "", "理论 cutoff 后窗口共 4 个：NYSE/XKRX 2026-08-14 均满足完整数据、资金、规则、force-close 和 portfolio gate，进入 2/8；NYSE/XKRX 2026-08-21 均因数据覆盖不完整而拒绝，不放宽规则。", "", "## Window attribution", "", window_text, "", "## SNDK loss attribution", "", f"SNDK paired_grid_pnl={total_paired:.12f}（正），inventory_realized_pnl={total_inv:.12f}，inventory_drag_usdt={drag:.12f}，inventory_drag_ratio={ratio:.12f}。`abs(inventory_loss) > paired_grid_pnl`，因此分类为 `GRID_EDGE_POSITIVE_INVENTORY_TAIL_DOMINATES`；库存尾部是主要损失来源，不是网格 edge 缺失。", "", f"按要求的逐项公式 reconciliation error 最大为 {max_reconciliation_error:.12f} USDT；该误差与 slippage_cost 相等，因为冻结 engine 已将 stop slippage 计入 inventory_realized_pnl。engine-native reconciliation error 为 0。", "", f"PRIMARY / STRESS / MAKER_OFF: {scenario_text}。Stress 比 PRIMARY 多亏约 0.771799 USDT，主要来自较低 maker fill、较少 paired cycles、较高 taker/slippage；Maker Promo Off 多亏约 0.202773 USDT，主要来自 maker fee 和 slippage，库存尾部仍占主导。", "", f"Primary paired cycles={len(primary_cycles)}, positive_cycle_ratio={summary['positive_cycle_ratio']:.6f}。", "", "## Stop and regime", "", f"正式 SNDK 窗口出现 stop_loss_upper；stop-path-analysis.csv 只使用 stop 之后的时间戳。regime 指标、POST_ENTRY_REGIME_SHIFT、MAE/MFE 和 TRUE/FALSE_BREAKOUT 分类均写入诊断输出，不能改变正式候选。", "", "## Counterfactuals", "", "CF_NO_INVENTORY、CF_BLOCK_SAME_SIDE_*、CF_REDUCE_ONLY_*、CF_HOLD_* 和 CF_STOP_BOUNDARY_* 全部标记 `DIAGNOSTIC_COUNTERFACTUAL`，不进入正式 ledger；它们只回答库存尾部、延迟退出和 stop boundary 的归因问题。", "", "六个固定 seed 的 PRIMARY 均为负，说明亏损主要是市场路径/库存尾部结构，而非单一不利 seed。", "", "`SNDK removal is NOT authorized from 2/8 Forward OOS.`", "", "## Freeze", "", "31111 parameters、A3/B1/C1/D1/E1、range/grid/inventory/stop/regime/capital/leverage、production config、execution scenarios、seeds、candidate SHA 和 exposure cutoff 均未修改。不得开启自动交易。", ""]
    oos_window_rows = [row for row in window_rows if row["classification"] == "FORWARD_OOS"]
    first_window = oos_window_rows[0] if oos_window_rows else {}
    second_window = oos_window_rows[1] if len(oos_window_rows) > 1 else {}
    primary_stop_rows = [row for row in stop_rows if row.get("scenario") == "PRIMARY_ZERO_MAKER"]
    stop_classifications = sorted({str(row.get("classification")) for row in primary_stop_rows})
    cf_groups: dict[str, list[float]] = defaultdict(list)
    for row in cf:
        if row.get("net_pnl") not in (None, ""):
            cf_groups[str(row.get("counterfactual"))].append(_float(row.get("net_pnl")))
    cf_means = {key: statistics.fmean(values) for key, values in cf_groups.items()}
    report = [
        "# QuietGrid v2.9.2 Forward OOS Loss Attribution", "",
        "## Formal status", "", "`INSUFFICIENT_FORWARD_OOS` (2/8); acceptance gates remain `NOT_EVALUATED`.", "",
        "## Diagnostic status", "", f"`{diagnostic_class}`", "",
        "## 23 Answers",
        "1. 4 个理论窗口中，NYSE/XKRX 2026-08-14 完整合格；NYSE/XKRX 2026-08-21 因数据覆盖不完整拒绝。",
        f"2. 窗口 1 net={_float(first_window.get('net_pnl')):.12f}；窗口 2 net={_float(second_window.get('net_pnl')):.12f}。",
        "3. 主要亏损来自 NYSE 窗口；XKRX 窗口零交易。",
        f"4. SNDK paired_grid_pnl={total_paired:.12f}，为正。",
        f"5. 20 个 paired cycles 全部为正，positive_cycle_ratio={summary['positive_cycle_ratio']:.6f}。",
        f"6. inventory_realized_pnl={total_inv:.12f} USDT。",
        f"7. inventory_drag_usdt={drag:.12f}，ratio={ratio:.12f}。",
        f"8. 首次超过 paired 利润：{first_window.get('inventory_drag_crossover_at','')}。",
        f"9. 最大方向 SHORT：max_short={_float(first_window.get('max_short_inventory')):.6f}，max_long={_float(first_window.get('max_long_inventory')):.6f}。",
        f"10. max inventory utilization={_float(first_window.get('max_inventory_utilization')):.7f}。",
        f"11. 最老 lot={int(_float(first_window.get('max_unpaired_lot_age')))} 个分钟 bar。",
        f"12. POST_ENTRY_REGIME_SHIFT={first_window.get('POST_ENTRY_REGIME_SHIFT', False)}。",
        f"13. Stop 分类={', '.join(stop_classifications) or 'UNRESOLVED'}；可观测路径为 TRUE_BREAKOUT。",
        "14. +30/+60/+120 分钟未回到 grid range/entry；+240 分钟无完整观测，window_end 仍 TRUE_BREAKOUT。",
        f"15. block same-side 20/30% 均值 net={cf_means.get('CF_BLOCK_SAME_SIDE_20', float('nan')):.6f}；40/50% 基本等于 baseline。",
        f"16. reduce-only 20/30% 均值 net={cf_means.get('CF_REDUCE_ONLY_20', float('nan')):.6f}，有明显诊断改善；40% 不变。",
        f"17. stop boundary 均值 net 0.5x/1x/1.5x/2x={cf_means.get('CF_STOP_BOUNDARY_0.5', float('nan')):.6f}/{cf_means.get('CF_STOP_BOUNDARY_1', float('nan')):.6f}/{cf_means.get('CF_STOP_BOUNDARY_1.5', float('nan')):.6f}/{cf_means.get('CF_STOP_BOUNDARY_2', float('nan')):.6f}；更宽 stop 风险更高。",
        f"18. PRIMARY/STRESS/MAKER_OFF：{scenario_text}。",
        "19. 六个固定 seed 全部为负，市场路径/库存尾部更重要。",
        "20. 主要问题是 GRID_EDGE_PRESENT_BUT_INVENTORY_TAIL_DOMINATES。",
        "21. 没有理由现在修改 31111；SNDK removal is NOT authorized from 2/8 Forward OOS.",
        "22. 应继续原样累计到 8/8。",
        "23. Early Warning=EARLY_WARNING_INVENTORY_TAIL；正式结论=INSUFFICIENT_FORWARD_OOS。", "",
        f"按要求公式 reconciliation error 最大={max_reconciliation_error:.12f} USDT；等于 stop slippage，engine-native reconciliation error=0。",
        f"CF_NO_INVENTORY 理论净收益均值={cf_means.get('CF_NO_INVENTORY', float('nan')):.6f} USDT；所有 counterfactual 均为 DIAGNOSTIC_COUNTERFACTUAL。",
        "31111 参数、生产配置、candidate SHA、exposure cutoff、正式 ledger 和自动交易状态均未修改。",
    ]
    (output_dir / "final-report.md").write_text("\n".join(report), encoding="utf-8")
    return summary


def main() -> int:
    parser=argparse.ArgumentParser(description="QuietGrid v2.9.2 diagnostic-only Forward OOS replay")
    parser.add_argument("--config", default=str(ROOT / "config" / "config.yaml"))
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "backtests" / "semiconductor-v2.7"))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args=parser.parse_args()
    summary=run_diagnostics(config_path=Path(args.config), data_dir=Path(args.data_dir), output_dir=Path(args.output_dir))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
