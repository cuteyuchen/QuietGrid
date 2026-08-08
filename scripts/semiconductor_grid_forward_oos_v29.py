"""Freeze and audit the semiconductor grid v2.9 Forward OOS sequence.

The command consumes only committed v2.8 artifacts and frozen market inputs.
It never contacts an exchange and never changes live configuration.
"""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.semiconductor_grid_backtest import (
    RESEARCH_SYMBOLS,
    _audit_funding,
    _find_csv,
    _funding_path,
    _load_rules,
    _projected_funding_pct,
    _read_funding,
    _read_klines_with_audit,
    _row_close_dt,
    _scenarios,
    _grid,
    _regime,
    _viability,
    build_calendar_closed_windows,
)
from scripts.semiconductor_grid_backtest_v28 import (
    _backtest_config,
    _grid_for,
    _profile,
    _row as _v28_result_row,
)
from core.models import GridDirectionMode
from strategy.adaptive_grid import AdaptiveGridConfig
from strategy.backtest import run_grid_backtest
from strategy.regime import RegimeEngine
from strategy.semiconductor_grid import (
    StrategyAdmissionError,
    build_semiconductor_grid_candidate,
    long_signal_from_mapping,
)
from strategy.semiconductor_grid import symbol_profiles_from_mapping
from strategy.semiconductor_grid_v28 import Combination, factor_snapshot
from strategy.semiconductor_grid_v29 import (
    DIAGNOSTIC_CONTROL_ID,
    EX_MU_CANDIDATE_ID,
    FORWARD_OOS_SCENARIOS,
    FORWARD_OOS_SEEDS,
    LEDGER_FIELDS,
    PRIMARY_CANDIDATE_ID,
    ForwardOOSLedger,
    build_exposure_evidence,
    candidate_registry,
    classify_forward_window,
    evaluate_forward_oos,
    file_sha256,
    first_eligible_forward_window,
    latest_timestamp_in_research_inputs,
    production_safety_snapshot,
)


DEFAULT_OUTPUT = Path("reports/semiconductor-grid-forward-oos-v2.9")
DEFAULT_V28_REPORT = Path("reports/semiconductor-grid-backtest-v2.8")
DEFAULT_DATA = Path("data/backtests/semiconductor-v2.7")
PHASE_FILES = (
    "phase1-r0-results.csv",
    "phase2-r1-results.csv",
    "phase3-local-factorial-results.csv",
    "phase4-time-validation-results.csv",
)
GATE_FILES = (
    "blocked-windows.csv",
    "phase2-blocked.csv",
    "phase3-blocked.csv",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze semiconductor grid 31111 for v2.9 Forward OOS"
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--v28-report-dir", default=str(DEFAULT_V28_REPORT))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--rules-json", default="")
    parser.add_argument("--freeze-time-utc", default="")
    parser.add_argument("--base-commit-sha", default="")
    parser.add_argument("--candidate-freeze-commit", default="")
    parser.add_argument("--no-ex-mu", action="store_true")
    parser.add_argument("--compileall-result", default="NOT_RUN")
    parser.add_argument("--pytest-result", default="NOT_RUN")
    return parser


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_json(path: Path, payload: Any) -> bytes:
    data = (
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    path.write_bytes(data)
    return data


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _composite_sha(paths: Iterable[Path]) -> str:
    digest = sha256()
    for path in sorted(paths, key=lambda item: str(item).lower()):
        digest.update(str(path.relative_to(ROOT)).replace("\\", "/").encode())
        digest.update(b"\0")
        digest.update(_normalized_source_bytes(path))
        digest.update(b"\0")
    return digest.hexdigest()


def _normalized_source_bytes(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _source_file_sha256(path: Path) -> str:
    return sha256(_normalized_source_bytes(path)).hexdigest()


def _frozen_code_hashes() -> dict[str, str]:
    strategy_paths = (
        ROOT / "strategy" / "semiconductor_grid.py",
        ROOT / "strategy" / "semiconductor_grid_v28.py",
    )
    backtest_path = ROOT / "strategy" / "backtest.py"
    calendar_path = ROOT / "scripts" / "semiconductor_grid_backtest.py"
    v29_path = ROOT / "strategy" / "semiconductor_grid_v29.py"
    runner_path = Path(__file__).resolve()
    return {
        "strategy_sha": _composite_sha(strategy_paths),
        "backtest_engine_sha": _source_file_sha256(backtest_path),
        "calendar_engine_sha": _source_file_sha256(calendar_path),
        "code_sha": _composite_sha(
            (*strategy_paths, backtest_path, calendar_path, v29_path, runner_path)
        ),
    }


def _registered_paths(data_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for symbol in RESEARCH_SYMBOLS:
        csv_path = _find_csv(data_dir, symbol)
        paths.append(csv_path)
        funding_path = _funding_path(csv_path)
        if funding_path is not None:
            paths.append(funding_path)
    return paths


def _funding_complete_for_window(
    funding: Sequence[Any], start: datetime, end: datetime, global_ok: bool
) -> bool:
    if not global_ok:
        return False
    duration_hours = max(0.0, (end - start).total_seconds() / 3600)
    expected = max(1.0, duration_hours / 8.0)
    left = int(start.timestamp() * 1000)
    right = int(end.timestamp() * 1000)
    observed = sum(left <= item.funding_time <= right for item in funding)
    return observed / expected >= 0.8


def _canonical_window_key(calendar: str, start: datetime, end: datetime) -> str:
    return f"{calendar}:{_iso(start)}:{_iso(end)}"


def _collect_window_manifest(
    *,
    raw_config: Mapping[str, Any],
    data_dir: Path,
    rules_path: Path,
    exposure_cutoff: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    research = dict(raw_config.get("semiconductor_grid") or {})
    profiles = symbol_profiles_from_mapping(research.get("symbol_profiles", {}))
    rules = _load_rules(rules_path)
    observation_rows = int(research.get("observation_rows", 180))
    force_close_minutes = int(research.get("force_close_minutes", 120))
    minimum_trade_minutes = int(research.get("minimum_trade_minutes", 120))
    manifest: list[dict[str, Any]] = []
    input_audit: list[dict[str, Any]] = []
    for symbol in RESEARCH_SYMBOLS:
        csv_path = _find_csv(data_dir, symbol)
        funding_path = _funding_path(csv_path)
        bars, bars_audit = _read_klines_with_audit(csv_path)
        funding = _read_funding(funding_path) if funding_path is not None else []
        funding_audit = _audit_funding(bars, funding, funding_path)
        profile = profiles[symbol]
        windows = build_calendar_closed_windows(
            bars,
            market_group=profile.market_group,
            calendar_name=profile.calendar_name,
            market_timezone=profile.market_timezone,
            reference_open_time=profile.reference_open_time,
            force_close_minutes=force_close_minutes,
            minimum_trade_minutes=minimum_trade_minutes,
            observation_minutes=observation_rows,
            window_key_prefix=profile.calendar_name,
        )
        data_sha = file_sha256(csv_path)
        funding_sha = file_sha256(funding_path) if funding_path else None
        input_audit.append(
            {
                "symbol": symbol,
                "data_path": str(csv_path),
                "data_sha256": data_sha,
                "funding_path": str(funding_path) if funding_path else None,
                "funding_sha256": funding_sha,
                "bars": bars_audit,
                "funding": funding_audit,
            }
        )
        for window in windows:
            start = window.observation_start
            end = window.force_close_at
            if start is None or end is None:
                continue
            funding_complete = _funding_complete_for_window(
                funding, start, end, bool(funding_audit.get("ok"))
            )
            complete = bool(window.complete and bars_audit.get("ok"))
            record = {
                "candidate_id": PRIMARY_CANDIDATE_ID,
                "symbol": symbol,
                "market_calendar": profile.calendar_name,
                "window_key": _canonical_window_key(profile.calendar_name, start, end),
                "source_window_key": window.window_key,
                "window_start": _iso(start),
                "window_end": _iso(end),
                "force_close_at": _iso(end),
                "first_seen_at": _iso(start),
                "complete_window": complete and funding_complete,
                "data_complete": complete,
                "funding_complete": funding_complete,
                "rules_frozen": symbol in rules,
                "force_close_covered": bool(window.complete),
                "data_sha": data_sha,
                "funding_sha": funding_sha,
                "rules_sha": file_sha256(rules_path),
            }
            manifest.append(record)
    expected_by_calendar: dict[str, set[str]] = {}
    for symbol in RESEARCH_SYMBOLS:
        expected_by_calendar.setdefault(profiles[symbol].calendar_name, set()).add(
            symbol
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in manifest:
        grouped.setdefault(str(record["window_key"]), []).append(record)
    for records in grouped.values():
        calendar = str(records[0]["market_calendar"])
        expected = expected_by_calendar.get(calendar, set())
        observed = {str(record["symbol"]) for record in records}
        complete_by_symbol = {
            str(record["symbol"]): bool(
                record.get("complete_window")
                and record.get("data_complete")
                and record.get("funding_complete")
                and record.get("rules_frozen")
                and record.get("force_close_covered")
            )
            for record in records
        }
        portfolio_complete = observed >= expected and all(
            complete_by_symbol.get(symbol, False) for symbol in expected
        )
        expected_text = ";".join(sorted(expected))
        observed_text = ";".join(sorted(observed))
        for record in records:
            record["expected_symbols"] = expected_text
            record["observed_symbols"] = observed_text
            record["portfolio_complete"] = portfolio_complete
            record["complete_window"] = bool(record["complete_window"] and portfolio_complete)
            record["exposure_status"] = classify_forward_window(
                record, exposure_cutoff
            )
            record["oos_eligible"] = record["exposure_status"] == "FORWARD_OOS"
    return manifest, input_audit


def _manifest_for_candidate(
    manifest: Sequence[Mapping[str, Any]],
    *,
    symbols: Sequence[str],
    exposure_cutoff: Any,
    raw_config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Recompute portfolio completeness for an independent symbol universe."""

    allowed = {str(symbol) for symbol in symbols}
    selected = [dict(row) for row in manifest if str(row.get("symbol")) in allowed]
    expected_by_calendar: dict[str, set[str]] = {}
    if raw_config is not None:
        profiles = symbol_profiles_from_mapping(
            dict(raw_config.get("semiconductor_grid") or {}).get(
                "symbol_profiles", {}
            )
        )
        for symbol in allowed:
            profile = profiles.get(symbol)
            if profile is not None:
                expected_by_calendar.setdefault(profile.calendar_name, set()).add(
                    symbol
                )
    if not expected_by_calendar:
        for row in selected:
            expected_by_calendar.setdefault(str(row.get("market_calendar")), set()).add(
                str(row.get("symbol"))
            )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in selected:
        grouped.setdefault(str(row.get("window_key") or ""), []).append(row)
    for rows in grouped.values():
        expected = expected_by_calendar.get(str(rows[0].get("market_calendar")), set())
        observed = {str(row.get("symbol")) for row in rows}
        portfolio_complete = observed >= expected and all(
            bool(
                row.get("data_complete")
                and row.get("funding_complete")
                and row.get("rules_frozen")
                and row.get("force_close_covered")
            )
            for row in rows
            if str(row.get("symbol")) in expected
        )
        expected_text = ";".join(sorted(expected))
        observed_text = ";".join(sorted(observed))
        for row in rows:
            symbol_complete = bool(
                row.get("data_complete")
                and row.get("funding_complete")
                and row.get("rules_frozen")
                and row.get("force_close_covered")
            )
            row["expected_symbols"] = expected_text
            row["observed_symbols"] = observed_text
            row["portfolio_complete"] = portfolio_complete
            row["complete_window"] = symbol_complete and portfolio_complete
            row["exposure_status"] = classify_forward_window(row, exposure_cutoff)
            row["oos_eligible"] = row["exposure_status"] == "FORWARD_OOS"
    return selected


def _zero_result_record(
    *,
    window: Mapping[str, Any],
    scenario: Any,
    seed: int,
    candidate_id: str,
    candidate_sha: str,
    config_sha: str,
    rules_sha: str,
    code_sha: str,
    gate_status: str,
    regime_status: str,
    status: str = "NO_TRADE",
) -> dict[str, Any]:
    return {
        "record_type": "OOS_RESULT",
        "status": status,
        "candidate_id": candidate_id,
        "candidate_sha": candidate_sha,
        "symbol": window["symbol"],
        "market_calendar": window["market_calendar"],
        "window_key": window["window_key"],
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "force_close_at": window["force_close_at"],
        "first_seen_at": window["first_seen_at"],
        "completed_at": _iso(datetime.now(UTC)),
        "data_sha": window.get("data_sha", ""),
        "rules_sha": rules_sha,
        "config_sha": config_sha,
        "code_sha": code_sha,
        "scenario": scenario.name,
        "seed": seed,
        "gate_status": gate_status,
        "regime_status": regime_status,
        "complete_window": True,
        "data_complete": True,
        "funding_complete": True,
        "rules_frozen": True,
        "force_close_covered": True,
        "expected_symbols": window.get("expected_symbols", ""),
        "observed_symbols": window.get("observed_symbols", ""),
        "portfolio_complete": bool(window.get("portfolio_complete", True)),
        "oos_eligible": True,
        "sequence_valid": True,
    }


def _run_eligible_windows(
    *,
    raw_config: Mapping[str, Any],
    data_dir: Path,
    rules_path: Path,
    eligible_manifest: Sequence[Mapping[str, Any]],
    candidate_id: str,
    candidate_sha: str,
    config_sha: str,
    rules_sha: str,
    code_sha: str,
    ledger: ForwardOOSLedger,
) -> list[dict[str, Any]]:
    """Run every eligible window through all registered scenarios and seeds."""

    if not eligible_manifest:
        return []
    research = dict(raw_config.get("semiconductor_grid") or {})
    profiles = symbol_profiles_from_mapping(research.get("symbol_profiles", {}))
    rules = _load_rules(rules_path)
    scenarios = {
        item.name: item
        for item in _scenarios(research.get("execution", {}))
        if item.name in FORWARD_OOS_SCENARIOS
    }
    base_grid = _grid(raw_config)
    regime = RegimeEngine(_regime(raw_config.get("regime", {})))
    viability = _viability(research.get("viability", {}))
    long_signal = long_signal_from_mapping(research.get("long_signal", {}))
    item = Combination.parse("31111")
    capital_base = float(research.get("capital_per_symbol", 500))
    leverage = float(research.get("economic_leverage", 1))
    observation_rows = int(research.get("observation_rows", 180))
    minimum_trade_rows = int(research.get("minimum_trade_rows", 120))
    force_close_minutes = int(research.get("force_close_minutes", 120))
    seen = {
        (
            str(row.get("candidate_id") or ""),
            str(row.get("symbol") or ""),
            str(row.get("window_key") or ""),
            str(row.get("scenario") or ""),
            str(row.get("seed") or ""),
        )
        for row in ledger.records()
        if row.get("record_type") == "OOS_RESULT"
        and str(row.get("candidate_id") or "") == candidate_id
        and str(row.get("candidate_sha") or "") == candidate_sha
        and bool(row.get("sequence_valid", True))
    }
    output: list[dict[str, Any]] = []
    contexts = {
        (str(row["symbol"]), str(row["window_key"])): row
        for row in eligible_manifest
    }
    for symbol in sorted({str(row["symbol"]) for row in eligible_manifest}):
        profile = profiles[symbol]
        csv_path = _find_csv(data_dir, symbol)
        bars, _audit = _read_klines_with_audit(csv_path)
        funding_path = _funding_path(csv_path)
        funding = _read_funding(funding_path) if funding_path else []
        windows = build_calendar_closed_windows(
            bars,
            market_group=profile.market_group,
            calendar_name=profile.calendar_name,
            market_timezone=profile.market_timezone,
            reference_open_time=profile.reference_open_time,
            force_close_minutes=force_close_minutes,
            minimum_trade_minutes=int(research.get("minimum_trade_minutes", 120)),
            observation_minutes=observation_rows,
            window_key_prefix=profile.calendar_name,
        )
        by_key = {}
        for window in windows:
            start = window.observation_start
            end = window.force_close_at
            if start is not None and end is not None:
                by_key[_canonical_window_key(profile.calendar_name, start, end)] = window
        for canonical_key in sorted(
            key for (candidate_symbol, key) in contexts if candidate_symbol == symbol
        ):
            context = contexts[(symbol, canonical_key)]
            window = by_key.get(canonical_key)
            if window is None or window.observation_end is None or window.force_close_at is None:
                continue
            observation_end_ms = int(window.observation_end.timestamp() * 1000)
            force_close_ms = int(window.force_close_at.timestamp() * 1000)
            observation = [
                row for row in window.rows if int(row["open_time"]) < observation_end_ms
            ][-observation_rows:]
            trade = [
                row
                for row in window.rows
                if observation_end_ms <= int(row["open_time"]) < force_close_ms
            ]
            if len(observation) < observation_rows or len(trade) < minimum_trade_rows:
                continue
            funding_events = [
                event
                for event in funding
                if int(trade[0]["open_time"]) <= event.funding_time
                <= int(trade[-1]["close_time"])
            ]
            previous_rates = [
                event.funding_rate
                for event in funding
                if event.funding_time <= int(observation[-1]["close_time"])
            ]
            funding_rate = previous_rates[-1] if previous_rates else 0.0
            projected = _projected_funding_pct(
                funding_rate, _row_close_dt(observation[-1]), window.force_close_at
            )
            depth = sum(float(row.get("quote_volume") or 0.0) for row in observation[-regime.config.long_window :]) / max(1, min(len(observation), regime.config.long_window))
            decision = regime.evaluate(
                symbol,
                observation,
                spread_pct=profile.assumed_spread_pct,
                depth_usdt=depth,
                funding_rate=funding_rate,
                data_age_seconds=0.0,
                expected_step_pct=profile.normal_min_step_pct * 1.5,
                include_cost=False,
                as_of=_row_close_dt(observation[-1]),
            )
            for scenario_name in FORWARD_OOS_SCENARIOS:
                scenario = scenarios.get(scenario_name)
                if scenario is None:
                    continue
                grid = _grid_for(item, base_grid, observation)
                candidate = None
                admission_reason = ""
                if decision.allowed and grid is not None:
                    try:
                        candidate = build_semiconductor_grid_candidate(
                            symbol_profile=profile,
                            strategy_profile=_profile(
                                symbol, item, GridDirectionMode.NEUTRAL, profile.normal_min_step_pct
                            ),
                            klines=observation,
                            current_price=float(observation[-1]["close"]),
                            funding_rate=funding_rate,
                            projected_funding_pct=projected,
                            maker_fee_rate=scenario.maker_fee_rate,
                            regime_score=decision.grid_score,
                            capital=capital_base,
                            leverage=leverage,
                            tick_size=rules[symbol].tick_size,
                            step_size=rules[symbol].step_size,
                            min_qty=rules[symbol].min_qty,
                            min_notional=rules[symbol].min_notional,
                            taker_fee_rate=scenario.taker_fee_rate,
                            base_grid_config=grid,
                            viability_config=viability,
                            long_signal_config=long_signal,
                        )
                    except (StrategyAdmissionError, ValueError) as exc:
                        admission_reason = str(exc)
                for seed in FORWARD_OOS_SEEDS:
                    identity = (
                        candidate_id,
                        symbol,
                        canonical_key,
                        scenario_name,
                        str(seed),
                    )
                    if identity in seen:
                        continue
                    if not decision.allowed:
                        row = _zero_result_record(
                            window=context,
                            scenario=scenario,
                            seed=seed,
                            candidate_id=candidate_id,
                            candidate_sha=candidate_sha,
                            config_sha=config_sha,
                            rules_sha=rules_sha,
                            code_sha=code_sha,
                            gate_status="NOT_EVALUATED",
                            regime_status="BLOCKED",
                        )
                    elif candidate is None:
                        row = _zero_result_record(
                            window=context,
                            scenario=scenario,
                            seed=seed,
                            candidate_id=candidate_id,
                            candidate_sha=candidate_sha,
                            config_sha=config_sha,
                            rules_sha=rules_sha,
                            code_sha=code_sha,
                            gate_status="BLOCKED",
                            regime_status="ALLOWED",
                            status=admission_reason or "GRID_VIABILITY_BLOCKED",
                        )
                    else:
                        capital = capital_base * profile.capital_multiplier
                        result = run_grid_backtest(
                            candidate.params,
                            trade,
                            current_price=float(observation[-1]["close"]),
                            config=_backtest_config(
                                item=item,
                                scenario=scenario,
                                capital=capital,
                                leverage=leverage,
                                rule=rules[symbol],
                                direction=GridDirectionMode.NEUTRAL,
                                seed=seed,
                            ),
                            funding_events=funding_events,
                        )
                        row = _v28_result_row(
                            item=item,
                            symbol=symbol,
                            direction=GridDirectionMode.NEUTRAL,
                            scenario=scenario_name,
                            seed=seed,
                            window=window,
                            result=result,
                            candidate=candidate,
                            capital=capital,
                        )
                        row.update(
                            {
                                "record_type": "OOS_RESULT",
                                "status": "COMPLETE",
                                "candidate_id": candidate_id,
                                "candidate_sha": candidate_sha,
                                "window_key": canonical_key,
                                "window_start": context["window_start"],
                                "window_end": context["window_end"],
                                "first_seen_at": context["first_seen_at"],
                                "completed_at": _iso(datetime.now(UTC)),
                                "market_calendar": context["market_calendar"],
                                "data_sha": context.get("data_sha", ""),
                                "rules_sha": rules_sha,
                                "config_sha": config_sha,
                                "code_sha": code_sha,
                                "gate_status": "ALLOWED",
                                "regime_status": "ALLOWED",
                                "range_pct": (
                                    (candidate.params.upper - candidate.params.lower)
                                    / max(candidate.params.center, 1e-12)
                                ),
                                "funding_pnl": result.funding_received - result.funding_paid,
                                "fees": result.fees_paid,
                                "slippage_cost": result.stop_exit_slippage_cost + result.force_exit_slippage_cost,
                                "complete_window": True,
                                "data_complete": True,
                                 "funding_complete": True,
                                 "rules_frozen": True,
                                 "force_close_covered": True,
                                 "expected_symbols": context.get("expected_symbols", ""),
                                 "observed_symbols": context.get("observed_symbols", ""),
                                 "portfolio_complete": bool(
                                     context.get("portfolio_complete", True)
                                 ),
                                 "oos_eligible": True,
                                "sequence_valid": True,
                            }
                        )
                    output.append(row)
                    seen.add(identity)
    if output:
        ledger.append(
            output,
            candidate_sha=candidate_sha,
            candidate_id=candidate_id,
        )
    return output


def _config_freeze(raw: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "trading",
        "entry",
        "timing",
        "grid",
        "cooldown",
        "regime",
        "risk",
        "inventory",
        "costs",
        "semiconductor_grid",
    )
    return {
        "schema_version": 1,
        "protocol": "semiconductor-grid-forward-oos-v2.9",
        "frozen_sections": {key: raw.get(key) for key in keys},
    }


def _candidate_freeze(
    *,
    raw: Mapping[str, Any],
    evidence: Mapping[str, Any],
    first_window: Mapping[str, Any] | None,
    base_commit_sha: str,
    candidate_freeze_commit: str,
    branch: str,
    config_sha: str,
    rules_sha: str,
    strategy_sha: str,
    backtest_sha: str,
    calendar_sha: str,
    code_sha: str,
    freeze_time: datetime,
) -> dict[str, Any]:
    research = dict(raw.get("semiconductor_grid") or {})
    grid = dict(raw.get("grid") or {})
    regime = dict(raw.get("regime") or {})
    risk = dict(raw.get("risk") or {})
    inventory = dict(raw.get("inventory") or {})
    costs = dict(raw.get("costs") or {})
    factor = factor_snapshot(Combination.parse("31111"))
    symbol_profiles = dict(research.get("symbol_profiles") or {})
    return {
        "schema_version": 1,
        "protocol": "semiconductor-grid-forward-oos-v2.9",
        "candidate_id": PRIMARY_CANDIDATE_ID,
        "status": "PRIMARY_FORWARD_OOS_CANDIDATE",
        "source_base_commit_sha": base_commit_sha,
        "freeze_commit_sha": candidate_freeze_commit,
        "freeze_commit_semantics": (
            "v2.9 code/config commit attested by the subsequent artifact commit"
        ),
        "freeze_time_utc": _iso(freeze_time),
        "v2_9_branch": branch,
        "config_sha": config_sha,
        "strategy_sha": strategy_sha,
        "backtest_engine_sha": backtest_sha,
        "calendar_engine_sha": calendar_sha,
        "code_sha": code_sha,
        "exchange_rules_sha": rules_sha,
        "combination_definition": factor,
        "direction": "NEUTRAL",
        "symbol_universe": {
            symbol: {
                "market_calendar": values.get("calendar_name"),
                "market_timezone": values.get("market_timezone"),
                "reference_open_time": values.get("reference_open_time"),
                "capital_multiplier": values.get("capital_multiplier", 1.0),
            }
            for symbol, values in symbol_profiles.items()
        },
        "parameters": {
            "range_and_volatility": {
                **factor["range"],
                "base_k_atr_range": grid.get("k_atr_range"),
                "base_k_sigma_range": grid.get("k_sigma_range"),
                "base_max_range_pct": grid.get("max_range_pct"),
                "effective_multiplier": 2.0,
                "entry_filters_by_symbol": regime.get("entry_filters_by_symbol", {}),
                "registered_a3_max_volatility_expansion_multiplier": 1.5,
                "registered_a3_regime_threshold_delta": -10,
                "v2_8_replay_uses_frozen_code_sha": code_sha,
            },
            "regime_and_reversal": {
                "regime": regime,
                "viability": research.get("viability", {}),
                "a3_minimum_reversal_ratio": 0.35,
                "a3_minimum_crossings_per_hour": 1.5,
                "a3_maximum_zero_activity_ratio": research.get(
                    "viability", {}
                ).get("max_zero_activity_ratio"),
            },
            "grid": {
                **factor["grid"],
                "target_grid_count": "ADAPTIVE_WITHIN_5_TO_10",
                "min_step_pct": "max(1.5 * symbol_normal_min_step_pct, cost_floor)",
                "base_grid": grid,
                "cost_floor_logic": {
                    "adverse_selection_buffer_pct": costs.get(
                        "adverse_selection_buffer_pct"
                    ),
                    "slippage_buffer_pct": costs.get("slippage_buffer_pct"),
                    "safety_margin_pct": costs.get("safety_margin_pct"),
                    "taker_and_funding_included": True,
                },
            },
            "profit_protection": {
                **factor["profit"],
                "active": False,
                "fixed_take_profit_usdt": 0.0,
                "peak_protection": None,
                "inventory_drag_protection": None,
            },
            "inventory": {
                **factor["inventory"],
                "production_inventory": inventory,
                "max_inventory_notional": research.get("capital_per_symbol"),
                "max_unpaired_lots_per_side": research.get(
                    "max_unpaired_lots_per_side"
                ),
            },
            "stop_and_window_loss": {
                **factor["stop"],
                "max_session_loss_pct": risk.get("max_session_loss_pct"),
                "max_weekend_loss_pct": risk.get("max_weekend_loss_pct"),
                "stop_slippage_by_scenario": {
                    name: values.get("stop_slippage_bps")
                    for name, values in research.get("execution", {})
                    .get("scenarios", {})
                    .items()
                },
            },
            "window": {
                "observation_rows": research.get("observation_rows"),
                "minimum_trade_rows": research.get("minimum_trade_rows"),
                "force_close_minutes": research.get("force_close_minutes"),
                "minimum_trade_minutes": research.get("minimum_trade_minutes"),
            },
            "capital_and_leverage": {
                "capital_per_symbol": research.get("capital_per_symbol"),
                "economic_leverage": research.get("economic_leverage"),
                "effective_leverage_cap": risk.get("effective_leverage_cap"),
            },
            "funding_treatment": "REALIZED_FROZEN_SIDECAR_EVENTS",
            "fill_model": "L0_CONSERVATIVE",
            "execution_scenarios": research.get("execution", {}).get(
                "scenarios", {}
            ),
        },
        "execution_scenarios": list(FORWARD_OOS_SCENARIOS),
        "random_seeds": list(FORWARD_OOS_SEEDS),
        "exposure_cutoff": evidence["exposure_cutoff"],
        "exposure_evidence": evidence,
        "first_eligible_forward_window": (
            first_window.get("window_key") if first_window else None
        ),
        "selection_lock": {
            "parameter_search_allowed": False,
            "symbol_reselection_allowed": False,
            "oos_sequence_resets_on_candidate_hash_change": True,
        },
    }


def _dependency_manifest(freeze_time: datetime) -> dict[str, Any]:
    packages: dict[str, str] = {}
    for name in (
        "pandas",
        "pandas-market-calendars",
        "PyYAML",
        "numpy",
        "pytest",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "NOT_INSTALLED"
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "dependency_versions": packages,
        "generated_at_utc": _iso(freeze_time),
    }


def _ledger_initial_records(
    *,
    candidate_sha: str,
    config_sha: str,
    rules_sha: str,
    code_sha: str,
    exposure_cutoff: str,
    freeze_time: datetime,
    include_ex_mu: bool,
    ex_mu_candidate_sha: str | None = None,
) -> list[dict[str, Any]]:
    common = {
        "candidate_sha": candidate_sha,
        "config_sha": config_sha,
        "rules_sha": rules_sha,
        "code_sha": code_sha,
        "completed_at": _iso(freeze_time),
        "exposure_cutoff": exposure_cutoff,
        "complete_window": False,
        "oos_eligible": False,
        "sequence_valid": True,
    }
    rows = [
        {
            **common,
            "record_type": "LEDGER_METADATA",
            "status": "INSUFFICIENT_FORWARD_OOS",
            "candidate_id": PRIMARY_CANDIDATE_ID,
        },
        {
            **common,
            "record_type": "CANDIDATE_FREEZE",
            "status": "PRIMARY_FORWARD_OOS_CANDIDATE",
            "candidate_id": PRIMARY_CANDIDATE_ID,
        },
        {
            **common,
            "record_type": "DIAGNOSTIC_CONTROL",
            "status": "DIAGNOSTIC_CONTROL_ONLY",
            "candidate_id": DIAGNOSTIC_CONTROL_ID,
            "reason": "Not counted as an independent primary candidate.",
        },
    ]
    if include_ex_mu:
        rows.append(
            {
                **common,
                "record_type": "POST_HOC_CANDIDATE",
                "status": "NEW_POST_HOC_RESEARCH_CANDIDATE",
                "candidate_id": EX_MU_CANDIDATE_ID,
                "candidate_sha": ex_mu_candidate_sha or candidate_sha,
                "reason": "Historical validation NOT_CLAIMED; independent 0-window sequence.",
            }
        )
    return rows


def _freeze_report(
    *,
    branch: str,
    base_commit: str,
    candidate_freeze_commit: str,
    candidate_sha: str,
    evidence: Mapping[str, Any],
    first_window: Mapping[str, Any] | None,
    include_ex_mu: bool,
    safety: Mapping[str, Any],
    assessment: Mapping[str, Any],
    compileall_result: str,
    pytest_result: str,
) -> str:
    first = first_window.get("window_key") if first_window else "NONE_YET"
    return f"""# Semiconductor Grid Forward OOS v2.9 Freeze Report

## Current conclusion

`{assessment['conclusion_code']}`

## Freeze answers

- 31111 frozen: YES (`{candidate_sha}`)
- v2.8 source commit: `{base_commit}`
- Candidate freeze commit: `{candidate_freeze_commit}`
- v2.9 branch: `{branch}`
- Exposure cutoff: `{evidence['exposure_cutoff']}`
- Latest research-input timestamp: `{evidence['latest_timestamp_present_in_any_research_input']}`
- Latest phase window seen: `{evidence['latest_window_seen_by_any_phase']}`
- Latest Regime/Gate window seen: `{evidence['latest_window_seen_by_regime_or_gate']}`
- First eligible Forward OOS window: `{first}`
- 31121 status: `DIAGNOSTIC_CONTROL_ONLY`
- EX-MU status: `{'NEW_POST_HOC_RESEARCH_CANDIDATE' if include_ex_mu else 'NOT_REGISTERED'}`
- Automatic trading remains disabled: `{'YES' if safety['safe'] else 'NO'}`
- Complete Forward OOS windows: `{assessment['complete_forward_oos_windows']}/8`
- compileall: `{compileall_result}`
- pytest: `{pytest_result}`

`NONE_YET` means the frozen input contains no complete window whose start is
strictly after the exposure cutoff. It must not be replaced by an already
started, partially present, Regime-blocked, or previously researched window.
"""


def _read_json_mapping(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError(f"Expected a JSON object: {path}")
    return dict(payload)


def _verify_frozen_hash(label: str, expected: Any, actual: str) -> None:
    if not expected or str(expected) != actual:
        raise RuntimeError(
            f"Frozen {label} hash mismatch: expected={expected!s}, actual={actual}"
        )


def _ledger_candidate_hash(
    records: Sequence[Mapping[str, Any]], candidate_id: str
) -> str:
    allowed_types = {"CANDIDATE_FREEZE", "POST_HOC_CANDIDATE"}
    hashes = [
        str(row.get("candidate_sha") or "")
        for row in records
        if str(row.get("candidate_id") or "") == candidate_id
        and str(row.get("record_type") or "") in allowed_types
        and str(row.get("candidate_sha") or "")
    ]
    return hashes[-1] if hashes else ""


def append_frozen_forward_oos(
    *,
    output_dir: str | Path = DEFAULT_OUTPUT,
    data_dir: str | Path = DEFAULT_DATA,
    run_time_utc: datetime | str | None = None,
) -> dict[str, Any]:
    """Append newly completed windows to an existing frozen v2.9 ledger.

    This path never reads live strategy configuration.  It reconstructs the
    runner from ``config-freeze.json`` and ``exchange-rules.json``, verifies
    their hashes plus every frozen code hash, and then appends only unseen
    candidate/window/scenario/seed identities.
    """

    output = Path(output_dir)
    data = Path(data_dir)
    run_time = _parse_utc(run_time_utc or datetime.now(UTC))
    append_dir = output / "append-runs"
    run_name = run_time.strftime("%Y%m%dT%H%M%S.%fZ") + ".json"
    run_path = append_dir / run_name
    if run_path.exists():
        raise FileExistsError(f"Append run manifest already exists: {run_path}")
    candidate_path = output / "candidate-31111-freeze.json"
    candidate_alias = output / "candidate-freeze.json"
    config_path = output / "config-freeze.json"
    rules_path = output / "exchange-rules.json"
    required_paths = (
        candidate_path,
        candidate_alias,
        config_path,
        rules_path,
        output / "forward-oos-ledger.csv",
        output / "forward-oos-ledger.json",
    )
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing frozen v2.9 artifacts: " + ", ".join(missing))

    candidate = _read_json_mapping(candidate_path)
    if candidate.get("candidate_id") != PRIMARY_CANDIDATE_ID:
        raise RuntimeError("Frozen primary candidate id is not 31111-NEUTRAL.")
    candidate_sha = file_sha256(candidate_path)
    _verify_frozen_hash(
        "candidate alias", candidate_sha, file_sha256(candidate_alias)
    )
    config_payload = _read_json_mapping(config_path)
    frozen_sections = config_payload.get("frozen_sections")
    if not isinstance(frozen_sections, Mapping):
        raise ValueError("config-freeze.json is missing frozen_sections.")
    raw_config = dict(frozen_sections)
    config_sha = file_sha256(config_path)
    rules_sha = file_sha256(rules_path)
    _verify_frozen_hash("config", candidate.get("config_sha"), config_sha)
    _verify_frozen_hash("exchange rules", candidate.get("exchange_rules_sha"), rules_sha)
    code_hashes = _frozen_code_hashes()
    for label, actual in code_hashes.items():
        _verify_frozen_hash(label, candidate.get(label), actual)
    safety = production_safety_snapshot(raw_config)
    if not safety["safe"]:
        raise RuntimeError("Frozen production safety flags or 1x leverage are invalid.")

    cutoff = _parse_utc(str(candidate["exposure_cutoff"]))
    manifest, input_audit = _collect_window_manifest(
        raw_config=raw_config,
        data_dir=data,
        rules_path=rules_path,
        exposure_cutoff=cutoff,
    )
    eligible_manifest = [
        row
        for row in manifest
        if bool(row.get("oos_eligible")) and bool(row.get("complete_window"))
    ]
    ledger = ForwardOOSLedger(
        output / "forward-oos-ledger.csv",
        output / "forward-oos-ledger.json",
    )
    records = ledger.records()
    _verify_frozen_hash(
        "primary ledger candidate",
        candidate_sha,
        _ledger_candidate_hash(records, PRIMARY_CANDIDATE_ID),
    )
    appended_primary = _run_eligible_windows(
        raw_config=raw_config,
        data_dir=data,
        rules_path=rules_path,
        eligible_manifest=eligible_manifest,
        candidate_id=PRIMARY_CANDIDATE_ID,
        candidate_sha=candidate_sha,
        config_sha=config_sha,
        rules_sha=rules_sha,
        code_sha=code_hashes["code_sha"],
        ledger=ledger,
    )

    ex_mu_path = output / "candidate-31111-ex-mu-freeze.json"
    ex_mu_sha: str | None = None
    appended_ex_mu: list[dict[str, Any]] = []
    ex_mu_manifest: list[dict[str, Any]] = []
    if ex_mu_path.is_file():
        ex_mu = _read_json_mapping(ex_mu_path)
        if (
            ex_mu.get("candidate_id") != EX_MU_CANDIDATE_ID
            or ex_mu.get("historical_validation") != "NOT_CLAIMED"
            or "MUUSDT" in dict(ex_mu.get("symbol_universe") or {})
        ):
            raise RuntimeError("Frozen EX-MU candidate contract is invalid.")
        ex_mu_sha = file_sha256(ex_mu_path)
        _verify_frozen_hash(
            "EX-MU ledger candidate",
            ex_mu_sha,
            _ledger_candidate_hash(ledger.records(), EX_MU_CANDIDATE_ID),
        )
        ex_mu_manifest = _manifest_for_candidate(
            manifest,
            symbols=("SNDKUSDT", "SOXLUSDT", "SKHYNIXUSDT"),
            exposure_cutoff=cutoff,
            raw_config=raw_config,
        )
        appended_ex_mu = _run_eligible_windows(
            raw_config=raw_config,
            data_dir=data,
            rules_path=rules_path,
            eligible_manifest=[
                row
                for row in ex_mu_manifest
                if bool(row.get("oos_eligible")) and bool(row.get("complete_window"))
            ],
            candidate_id=EX_MU_CANDIDATE_ID,
            candidate_sha=ex_mu_sha,
            config_sha=config_sha,
            rules_sha=rules_sha,
            code_sha=code_hashes["code_sha"],
            ledger=ledger,
        )

    records = ledger.records()
    assessment = evaluate_forward_oos(records)
    ex_mu_assessment = (
        evaluate_forward_oos(records, candidate_id=EX_MU_CANDIDATE_ID)
        if ex_mu_sha
        else None
    )
    _write_json(output / "forward-oos-summary.json", assessment)
    _write_json(output / "acceptance-gates.json", assessment)
    if ex_mu_assessment:
        _write_json(output / "forward-oos-summary-ex-mu.json", ex_mu_assessment)
    _write_csv(
        output / "symbol-breakdown.csv",
        assessment["symbol_breakdown"],
        (
            "symbol",
            "net_pnl",
            "profit_factor",
            "positive_window_ratio",
            "inventory_drag",
            "max_drawdown",
            "complete_forward_oos_windows",
        ),
    )
    _write_csv(
        output / "window-manifest-latest.csv",
        manifest,
        tuple(sorted({key for row in manifest for key in row})) or ("window_key",),
    )
    _write_json(output / "window-manifest-latest.json", {"windows": manifest})
    if ex_mu_manifest:
        _write_json(
            output / "window-manifest-ex-mu.json", {"windows": ex_mu_manifest}
        )

    registry_path = output / "candidate-registry.json"
    registry = (
        _read_json_mapping(registry_path)
        if registry_path.is_file()
        else candidate_registry(
            include_ex_mu=bool(ex_mu_sha),
            primary_candidate_sha=candidate_sha,
            ex_mu_candidate_sha=ex_mu_sha,
        )
    )
    primary_registry = list(registry.get("primary_forward_oos_candidates") or [])
    if primary_registry:
        primary_registry[0]["forward_oos_count"] = assessment[
            "complete_forward_oos_windows"
        ]
    ex_mu_registry = list(registry.get("post_hoc_research_candidates") or [])
    if ex_mu_registry and ex_mu_assessment:
        ex_mu_registry[0]["forward_oos_count"] = ex_mu_assessment[
            "complete_forward_oos_windows"
        ]
    _write_json(registry_path, registry)

    run_record = {
        "run_time_utc": _iso(run_time),
        "mode": "APPEND_FROZEN_FORWARD_OOS",
        "candidate_sha": candidate_sha,
        "ex_mu_candidate_sha": ex_mu_sha,
        "exposure_cutoff": _iso(cutoff),
        "eligible_manifest_rows": len(eligible_manifest),
        "appended_primary_rows": len(appended_primary),
        "appended_ex_mu_rows": len(appended_ex_mu),
        "complete_forward_oos_windows": assessment["complete_forward_oos_windows"],
        "conclusion_code": assessment["conclusion_code"],
        "production_safety": safety,
        "input_audit": input_audit,
    }
    append_dir.mkdir(parents=True, exist_ok=True)
    _write_json(run_path, run_record)

    run_manifest_path = output / "run-manifest.json"
    if run_manifest_path.is_file():
        run_manifest = _read_json_mapping(run_manifest_path)
        append_runs = list(run_manifest.get("append_runs") or [])
        append_runs.append(str(run_path))
        run_manifest["append_runs"] = append_runs
        run_manifest["latest_append_run_utc"] = _iso(run_time)
        run_manifest["complete_forward_oos_windows"] = assessment[
            "complete_forward_oos_windows"
        ]
        run_manifest["conclusion_code"] = assessment["conclusion_code"]
        _write_json(run_manifest_path, run_manifest)
    return {**run_record, "run_manifest": str(run_path)}


def main() -> None:
    args = _parser().parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config)
    data_dir = Path(args.data_dir)
    v28_report = Path(args.v28_report_dir)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    freeze_time = _parse_utc(args.freeze_time_utc or datetime.now(UTC))
    include_ex_mu = not args.no_ex_mu
    source_branch = "codex/semiconductor-grid-combinatorial-backtest-v2.8"
    base_commit = (
        args.base_commit_sha
        or _git("merge-base", "HEAD", source_branch)
        or _git("rev-parse", "HEAD")
    )
    candidate_freeze_commit = (
        args.candidate_freeze_commit or _git("rev-parse", "HEAD")
    )
    branch = _git("branch", "--show-current")
    rules_path = (
        Path(args.rules_json)
        if args.rules_json
        else v28_report / "exchange-rules.json"
    )
    if not rules_path.is_file():
        raise FileNotFoundError("Frozen exchange-rules.json is required.")

    input_paths = _registered_paths(data_dir)
    latest_input = latest_timestamp_in_research_inputs(input_paths)
    phase_records = [
        row for name in PHASE_FILES for row in _read_csv(v28_report / name)
    ]
    gate_records = [
        row for name in GATE_FILES for row in _read_csv(v28_report / name)
    ]
    evidence = build_exposure_evidence(
        research_input_timestamps=[latest_input] if latest_input else [],
        phase_records=phase_records,
        gate_records=gate_records,
        candidate_freeze_time_utc=freeze_time,
    )
    evidence_mapping = evidence.to_mapping()

    manifest, input_audit = _collect_window_manifest(
        raw_config=raw,
        data_dir=data_dir,
        rules_path=rules_path,
        exposure_cutoff=evidence.exposure_cutoff,
    )
    first_window = first_eligible_forward_window(
        manifest, evidence.exposure_cutoff
    )
    safety = production_safety_snapshot(raw)
    if not safety["safe"]:
        raise RuntimeError("Production safety flags or 1x leverage changed; freeze refused.")

    config_payload = _config_freeze(raw)
    config_bytes = _write_json(output / "config-freeze.json", config_payload)
    config_sha = sha256(config_bytes).hexdigest()
    rules_bytes = _normalized_source_bytes(rules_path)
    (output / "exchange-rules.json").write_bytes(rules_bytes)
    rules_sha = sha256(rules_bytes).hexdigest()

    code_hashes = _frozen_code_hashes()
    strategy_sha = code_hashes["strategy_sha"]
    backtest_sha = code_hashes["backtest_engine_sha"]
    calendar_sha = code_hashes["calendar_engine_sha"]
    code_sha = code_hashes["code_sha"]
    candidate_payload = _candidate_freeze(
        raw=raw,
        evidence=evidence_mapping,
        first_window=first_window,
        base_commit_sha=base_commit,
        candidate_freeze_commit=candidate_freeze_commit,
        branch=branch,
        config_sha=config_sha,
        rules_sha=rules_sha,
        strategy_sha=strategy_sha,
        backtest_sha=backtest_sha,
        calendar_sha=calendar_sha,
        code_sha=code_sha,
        freeze_time=freeze_time,
    )
    candidate_bytes = (
        json.dumps(candidate_payload, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    candidate_sha = sha256(candidate_bytes).hexdigest()
    (output / "candidate-31111-freeze.json").write_bytes(candidate_bytes)
    (output / "candidate-freeze.json").write_bytes(candidate_bytes)
    ex_mu_sha: str | None = None
    if include_ex_mu:
        ex_mu_payload = json.loads(candidate_bytes.decode("utf-8"))
        ex_mu_payload["candidate_id"] = EX_MU_CANDIDATE_ID
        ex_mu_payload["status"] = "NEW_POST_HOC_RESEARCH_CANDIDATE"
        ex_mu_payload["historical_validation"] = "NOT_CLAIMED"
        ex_mu_payload["forward_oos_count"] = 0
        ex_mu_payload["symbol_universe"] = {
            symbol: value
            for symbol, value in ex_mu_payload["symbol_universe"].items()
            if symbol != "MUUSDT"
        }
        ex_mu_payload["excluded_symbols"] = ["MUUSDT"]
        ex_mu_payload["independent_sequence"] = True
        ex_mu_bytes = (
            json.dumps(ex_mu_payload, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n"
        ).encode("utf-8")
        ex_mu_sha = sha256(ex_mu_bytes).hexdigest()
        (output / "candidate-31111-ex-mu-freeze.json").write_bytes(ex_mu_bytes)
    registry = candidate_registry(
        include_ex_mu=include_ex_mu,
        primary_candidate_sha=candidate_sha,
        ex_mu_candidate_sha=ex_mu_sha,
    )

    ledger = ForwardOOSLedger(
        output / "forward-oos-ledger.csv",
        output / "forward-oos-ledger.json",
    )
    ledger.initialize(
        _ledger_initial_records(
            candidate_sha=candidate_sha,
            config_sha=config_sha,
            rules_sha=rules_sha,
            code_sha=code_sha,
            exposure_cutoff=evidence_mapping["exposure_cutoff"],
            freeze_time=freeze_time,
            include_ex_mu=include_ex_mu,
            ex_mu_candidate_sha=ex_mu_sha,
        )
    )
    eligible_manifest = [
        row
        for row in manifest
        if bool(row.get("oos_eligible"))
        and bool(row.get("complete_window"))
    ]
    appended_oos_rows = _run_eligible_windows(
        raw_config=raw,
        data_dir=data_dir,
        rules_path=rules_path,
        eligible_manifest=eligible_manifest,
        candidate_id=PRIMARY_CANDIDATE_ID,
        candidate_sha=candidate_sha,
        config_sha=config_sha,
        rules_sha=rules_sha,
        code_sha=code_sha,
        ledger=ledger,
    )
    appended_ex_mu_rows: list[dict[str, Any]] = []
    ex_mu_manifest: list[dict[str, Any]] = []
    if include_ex_mu and ex_mu_sha:
        ex_mu_manifest = _manifest_for_candidate(
            manifest,
            symbols=("SNDKUSDT", "SOXLUSDT", "SKHYNIXUSDT"),
            exposure_cutoff=evidence.exposure_cutoff,
            raw_config=raw,
        )
        appended_ex_mu_rows = _run_eligible_windows(
            raw_config=raw,
            data_dir=data_dir,
            rules_path=rules_path,
            eligible_manifest=[
                row
                for row in ex_mu_manifest
                if bool(row.get("oos_eligible")) and bool(row.get("complete_window"))
            ],
            candidate_id=EX_MU_CANDIDATE_ID,
            candidate_sha=ex_mu_sha,
            config_sha=config_sha,
            rules_sha=rules_sha,
            code_sha=code_sha,
            ledger=ledger,
        )
    assessment = evaluate_forward_oos(ledger.records())
    ex_mu_assessment = (
        evaluate_forward_oos(ledger.records(), candidate_id=EX_MU_CANDIDATE_ID)
        if include_ex_mu
        else None
    )
    registry["primary_forward_oos_candidates"][0]["forward_oos_count"] = assessment[
        "complete_forward_oos_windows"
    ]
    if ex_mu_assessment:
        registry["post_hoc_research_candidates"][0]["forward_oos_count"] = (
            ex_mu_assessment["complete_forward_oos_windows"]
        )
    _write_json(output / "candidate-registry.json", registry)

    window_fields = tuple(
        sorted({key for row in manifest for key in row})
    ) or ("window_key",)
    _write_csv(output / "window-manifest.csv", manifest, window_fields)
    _write_json(output / "window-manifest.json", {"windows": manifest})
    if ex_mu_manifest:
        _write_json(
            output / "window-manifest-ex-mu.json", {"windows": ex_mu_manifest}
        )
    _write_csv(
        output / "symbol-breakdown.csv",
        assessment["symbol_breakdown"],
        (
            "symbol",
            "net_pnl",
            "profit_factor",
            "positive_window_ratio",
            "inventory_drag",
            "max_drawdown",
            "complete_forward_oos_windows",
        ),
    )
    _write_json(output / "forward-oos-summary.json", assessment)
    if ex_mu_assessment:
        _write_json(output / "forward-oos-summary-ex-mu.json", ex_mu_assessment)
    _write_json(output / "acceptance-gates.json", assessment)
    _write_json(output / "dependency-manifest.json", _dependency_manifest(freeze_time))

    hash_files = [
        config_path,
        rules_path,
        *input_paths,
        *(v28_report / name for name in (*PHASE_FILES, *GATE_FILES)),
        v28_report / "final-report.md",
        v28_report / "phase4-acceptance.json",
    ]
    input_hashes = {
        str(path): file_sha256(path)
        for path in hash_files
        if path.is_file()
    }
    input_hashes[str(output / "candidate-31111-freeze.json")] = candidate_sha
    if ex_mu_sha:
        input_hashes[str(output / "candidate-31111-ex-mu-freeze.json")] = ex_mu_sha
    input_hashes[str(output / "config-freeze.json")] = config_sha
    input_hashes[str(output / "exchange-rules.json")] = rules_sha
    _write_json(
        output / "input-hash-manifest.json",
        {
            "schema_version": 1,
            "generated_at_utc": _iso(freeze_time),
            "latest_research_input_timestamp": (
                _iso(latest_input) if latest_input else None
            ),
            "files": input_hashes,
            "input_audit": input_audit,
        },
    )
    _write_json(
        output / "run-manifest.json",
        {
            "schema_version": 1,
            "protocol": "semiconductor-grid-forward-oos-v2.9",
            "base_branch": source_branch,
            "base_commit_sha": base_commit,
            "v2_9_branch": branch,
            "candidate_freeze_commit": candidate_freeze_commit,
            "candidate_freeze_time_utc": _iso(freeze_time),
            "candidate_id": PRIMARY_CANDIDATE_ID,
            "candidate_sha": candidate_sha,
            "appended_oos_rows": len(appended_oos_rows),
            "appended_ex_mu_oos_rows": len(appended_ex_mu_rows),
            "ex_mu_candidate_sha": ex_mu_sha,
            "ex_mu_complete_forward_oos_windows": (
                ex_mu_assessment["complete_forward_oos_windows"]
                if ex_mu_assessment
                else None
            ),
            "ex_mu_conclusion_code": (
                ex_mu_assessment["conclusion_code"] if ex_mu_assessment else None
            ),
            "config_sha": config_sha,
            "code_sha": code_sha,
            "strategy_sha": strategy_sha,
            "backtest_engine_sha": backtest_sha,
            "calendar_engine_sha": calendar_sha,
            "rules_sha": rules_sha,
            "python_version": platform.python_version(),
            "dependency_versions": _dependency_manifest(freeze_time)[
                "dependency_versions"
            ],
            "symbol_universe": list(RESEARCH_SYMBOLS),
            "execution_scenarios": list(FORWARD_OOS_SCENARIOS),
            "random_seeds": list(FORWARD_OOS_SEEDS),
            "exposure": evidence_mapping,
            "first_eligible_forward_window": (
                first_window.get("window_key") if first_window else None
            ),
            "complete_forward_oos_windows": assessment[
                "complete_forward_oos_windows"
            ],
            "conclusion_code": assessment["conclusion_code"],
            "candidate_registry": registry,
            "production_safety": safety,
            "compileall_result": args.compileall_result,
            "pytest_result": args.pytest_result,
        },
    )
    (output / "freeze-report.md").write_text(
        _freeze_report(
            branch=branch,
            base_commit=base_commit,
            candidate_freeze_commit=candidate_freeze_commit,
            candidate_sha=candidate_sha,
            evidence=evidence_mapping,
            first_window=first_window,
            include_ex_mu=include_ex_mu,
            safety=safety,
            assessment=assessment,
            compileall_result=args.compileall_result,
            pytest_result=args.pytest_result,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "candidate_sha": candidate_sha,
                "exposure_cutoff": evidence_mapping["exposure_cutoff"],
                "first_eligible_forward_window": (
                    first_window.get("window_key") if first_window else None
                ),
                "complete_forward_oos_windows": assessment[
                    "complete_forward_oos_windows"
                ],
                "conclusion_code": assessment["conclusion_code"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
