"""Run the registered v2.7 semiconductor closed-market grid matrix (v2.7.1)."""
from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import statistics
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.models import GridDirectionMode
from core.scheduler import Scheduler
from data_sources.models import FundingEvent
from strategy.adaptive_grid import AdaptiveGridConfig
from strategy.backtest import BacktestConfig, BacktestResult, run_grid_backtest
from strategy.grid_viability import GridViabilityConfig
from strategy.semiconductor_grid import (
    RESEARCH_SYMBOLS,
    StrategyAdmissionError,
    build_semiconductor_grid_candidate,
    long_signal_from_mapping,
    profiles_from_mapping,
    symbol_profiles_from_mapping,
)
from strategy.window_models import WindowKind

UTC = timezone.utc
SEEDS = (3, 10, 17, 31, 59, 97)
DEFAULT_OUTPUT = Path("reports/semiconductor-grid-backtest-v2.7.1")
STRATEGY_VERSION = "semiconductor-grid-v2.7.1"


@dataclass(frozen=True)
class ClosedWindow:
    window_key: str
    market_group: str
    rows: tuple[dict[str, Any], ...]
    window_kind: str = "WEEKEND"
    previous_market_close: datetime | None = None
    force_close_at: datetime | None = None
    next_reference_open: datetime | None = None
    split: str = ""

    @property
    def start_time(self) -> datetime:
        return _row_dt(self.rows[0])

    @property
    def end_time(self) -> datetime:
        return _row_dt(self.rows[-1])


@dataclass(frozen=True)
class RuleSnapshot:
    tick_size: float = 0.0
    step_size: float = 0.0
    min_qty: float = 0.0
    min_notional: float = 0.0
    status: str = ""
    contract_type: str = ""
    onboard_date: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Scenario:
    name: str
    maker_fee_rate: float
    taker_fee_rate: float
    maker_fill_probability: float
    max_fills_per_bar: int
    stop_slippage_bps: float


def build_closed_windows(
    rows: Iterable[dict[str, Any]],
    scheduler: Any,
    market_group: str,
) -> list[ClosedWindow]:
    """Compatibility helper for unit tests: group allowed bars by window_key."""

    groups: list[ClosedWindow] = []
    key = None
    bucket: list[dict[str, Any]] = []
    kind = "WEEKEND"
    meta: dict[str, Any] = {}
    for row in rows:
        window = scheduler.classify_window(_row_dt(row))
        current = window.window_key if window.allowed else None
        if current != key:
            if key and bucket:
                groups.append(
                    ClosedWindow(
                        window_key=key,
                        market_group=market_group,
                        rows=tuple(bucket),
                        window_kind=kind,
                        previous_market_close=meta.get("previous_market_close"),
                        force_close_at=meta.get("force_close_at"),
                        next_reference_open=meta.get("next_reference_open"),
                    )
                )
            key, bucket = current, []
            meta = {}
        if current:
            bucket.append(row)
            kind = getattr(window.kind, "value", str(window.kind))
            meta = {
                "previous_market_close": window.previous_market_close,
                "force_close_at": window.force_close_at,
                "next_reference_open": window.next_premarket_open,
            }
    if key and bucket:
        groups.append(
            ClosedWindow(
                window_key=key,
                market_group=market_group,
                rows=tuple(bucket),
                window_kind=kind,
                previous_market_close=meta.get("previous_market_close"),
                force_close_at=meta.get("force_close_at"),
                next_reference_open=meta.get("next_reference_open"),
            )
        )
    return groups


def build_calendar_closed_windows(
    rows: Sequence[dict[str, Any]],
    *,
    market_group: str,
    calendar_name: str,
    market_timezone: str,
    reference_open_time: str | None,
    force_close_minutes: int = 120,
    minimum_trade_minutes: int = 120,
    window_key_prefix: str | None = None,
) -> list[ClosedWindow]:
    """Build weekend/holiday closed windows from calendar boundaries.

    Mirrors Scheduler.classify_window semantics without per-bar calendar lookups.
    Allowed bars satisfy:

      previous_close <= open_time < next_reference_open
        - (force_close_minutes + minimum_trade_minutes)
    """

    if not rows:
        return []
    tz = ZoneInfo(market_timezone)
    calendar = mcal.get_calendar(calendar_name)
    start_local = _row_dt(rows[0]).astimezone(tz).date() - timedelta(days=14)
    end_local = _row_dt(rows[-1]).astimezone(tz).date() + timedelta(days=21)
    schedule = calendar.schedule(start_date=start_local, end_date=end_local)
    if schedule.empty:
        return []

    market_opens: list[datetime] = []
    market_closes: list[datetime] = []
    reference_opens: list[datetime] = []
    for _, row in schedule.iterrows():
        market_open = row["market_open"].to_pydatetime().astimezone(UTC)
        market_close = row["market_close"].to_pydatetime().astimezone(UTC)
        if reference_open_time in (None, "", "null"):
            reference_open = market_open
        else:
            hour, minute = [int(part) for part in str(reference_open_time).split(":")[:2]]
            local_open = market_open.astimezone(tz)
            reference_open = datetime.combine(
                local_open.date(),
                time(hour=hour, minute=minute),
                tzinfo=tz,
            ).astimezone(UTC)
        market_opens.append(market_open)
        market_closes.append(market_close)
        reference_opens.append(reference_open)

    open_times = [int(row["open_time"]) for row in rows]
    prefix = window_key_prefix or market_group or calendar_name
    required_minutes = force_close_minutes + minimum_trade_minutes
    windows: list[ClosedWindow] = []

    for index, previous_close in enumerate(market_closes):
        next_market_open = None
        next_reference_open = None
        for cursor in range(index + 1, len(market_opens)):
            if market_opens[cursor] > previous_close:
                next_market_open = market_opens[cursor]
                next_reference_open = reference_opens[cursor]
                break
        if next_market_open is None or next_reference_open is None:
            continue

        kind = _closed_kind(previous_close, next_market_open, tz)
        if kind not in (WindowKind.WEEKEND, WindowKind.HOLIDAY):
            continue

        trade_end = next_reference_open - timedelta(minutes=required_minutes)
        force_close_at = next_reference_open - timedelta(minutes=force_close_minutes)
        if trade_end <= previous_close:
            continue

        start_ms = int(previous_close.timestamp() * 1000)
        end_ms = int(trade_end.timestamp() * 1000)
        left = _lower_bound(open_times, start_ms)
        right = _lower_bound(open_times, end_ms)
        if right <= left:
            continue
        bucket = list(rows[left:right])
        window_key = (
            f"{prefix}:{previous_close.isoformat()}:{next_reference_open.isoformat()}"
        )
        windows.append(
            ClosedWindow(
                window_key=window_key,
                market_group=market_group,
                rows=tuple(bucket),
                window_kind=kind.value,
                previous_market_close=previous_close,
                force_close_at=force_close_at,
                next_reference_open=next_reference_open,
            )
        )
    return windows


def assign_time_splits(windows: Sequence[ClosedWindow]) -> list[ClosedWindow]:
    ordered = sorted(windows, key=lambda item: item.start_time)
    total = len(ordered)
    if total == 0:
        return []
    dev_end = max(1, int(math.floor(total * 0.50))) if total >= 2 else total
    val_end = max(dev_end, int(math.floor(total * 0.75))) if total >= 4 else total
    if total >= 4:
        val_end = min(val_end, total - 1)
        dev_end = min(dev_end, val_end)
    assigned: list[ClosedWindow] = []
    for index, window in enumerate(ordered):
        if index < dev_end:
            split = "Development"
        elif index < val_end:
            split = "Validation"
        else:
            split = "Final_OOS"
        assigned.append(
            ClosedWindow(
                window_key=window.window_key,
                market_group=window.market_group,
                rows=window.rows,
                window_kind=window.window_kind,
                previous_market_close=window.previous_market_close,
                force_close_at=window.force_close_at,
                next_reference_open=window.next_reference_open,
                split=split,
            )
        )
    return assigned


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回测 SNDK/MU/SOXL/SKHYNIX 休市窗口密集网格 v2.7.1",
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--data-dir", default="data/backtests/semiconductor-v2.7")
    parser.add_argument("--rules-json")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--symbols", nargs="*", default=list(RESEARCH_SYMBOLS))
    parser.add_argument("--allow-missing-rules", action="store_true")
    parser.add_argument("--allow-missing-funding", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--base-commit", default="")
    return parser


def main() -> None:
    args = _parser().parse_args()
    started_at = datetime.now(UTC)
    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    cfg = raw.get("semiconductor_grid", {}) or {}
    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()) and not args.overwrite:
        raise RuntimeError(f"输出目录非空: {output}；传 --overwrite 才能覆盖")
    output.mkdir(parents=True, exist_ok=True)

    symbols = tuple(str(item).upper() for item in args.symbols)
    unknown = sorted(set(symbols) - set(RESEARCH_SYMBOLS))
    if unknown:
        raise ValueError("未注册标的: " + ", ".join(unknown))

    sprofiles = symbol_profiles_from_mapping(cfg.get("symbol_profiles", {}) or {})
    profiles = profiles_from_mapping(cfg.get("profiles", {}) or {})
    scenarios = _scenarios(cfg.get("execution", {}) or {})
    viability = _viability(cfg.get("viability", {}) or {})
    long_signal = long_signal_from_mapping(cfg.get("long_signal", {}) or {})
    base_grid = _grid(raw)
    observe = int(cfg.get("observation_rows", 180))
    minimum = int(cfg.get("minimum_trade_rows", 120))
    capital = float(cfg.get("capital_per_symbol", 500))
    leverage = float(cfg.get("economic_leverage", 1))
    force_close_minutes = int(cfg.get("force_close_minutes", 120))
    minimum_trade_minutes = int(cfg.get("minimum_trade_minutes", 120))

    data_dir = Path(args.data_dir)
    rules_path = Path(args.rules_json) if args.rules_json else data_dir / "exchange-rules.json"
    if not rules_path.exists() and not args.allow_missing_rules:
        raise FileNotFoundError(f"缺少规则快照: {rules_path}")
    rules = _load_rules(rules_path) if rules_path.exists() else {}

    rows_out: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    viability_rows: list[dict[str, Any]] = []
    window_manifest: list[dict[str, Any]] = []
    input_manifest: list[dict[str, Any]] = []
    data_audit_symbols: list[dict[str, Any]] = []
    all_windows: list[dict[str, Any]] = []
    hash_manifest: dict[str, Any] = {"files": {}}

    total_unique_windows = 0
    data_quality_failed = False
    data_quality_reasons: list[str] = []

    for symbol in symbols:
        if symbol not in sprofiles:
            raise ValueError(f"缺少 {symbol} symbol_profile")
        sp = sprofiles[symbol]
        csv_path = _find_csv(data_dir, symbol)
        try:
            bars, audit = _read_klines_with_audit(csv_path)
        except ValueError as exc:
            data_quality_failed = True
            data_quality_reasons.append(f"{symbol}: {exc}")
            data_audit_symbols.append(
                {"symbol": symbol, "csv": str(csv_path), "ok": False, "error": str(exc)}
            )
            continue

        funding_path = _funding_path(csv_path)
        if funding_path is None and not args.allow_missing_funding:
            raise FileNotFoundError(f"缺少 {symbol} Funding sidecar")
        funding = _read_funding(funding_path) if funding_path else []
        funding_audit = _audit_funding(bars, funding, funding_path)

        rule = rules.get(symbol, RuleSnapshot())
        if symbol not in rules and not args.allow_missing_rules:
            raise ValueError(f"规则快照缺少 {symbol}")

        windows = build_calendar_closed_windows(
            bars,
            market_group=sp.market_group,
            calendar_name=sp.calendar_name,
            market_timezone=sp.market_timezone,
            reference_open_time=sp.reference_open_time,
            force_close_minutes=force_close_minutes,
            minimum_trade_minutes=minimum_trade_minutes,
            window_key_prefix=sp.market_group,
        )
        windows = assign_time_splits(windows)
        total_unique_windows += len(windows)

        csv_sha = _sha(csv_path)
        funding_sha = _sha(funding_path) if funding_path else None
        hash_manifest["files"][str(csv_path)] = csv_sha
        if funding_path:
            hash_manifest["files"][str(funding_path)] = funding_sha

        input_manifest.append(
            {
                "symbol": symbol,
                "csv": str(csv_path),
                "csv_sha256": csv_sha,
                "funding": str(funding_path) if funding_path else None,
                "funding_sha256": funding_sha,
                "rows": len(bars),
                "closed_windows": len(windows),
                "calendar": sp.calendar_name,
                "market_group": sp.market_group,
                "start_open_time": bars[0]["open_time"] if bars else None,
                "end_open_time": bars[-1]["open_time"] if bars else None,
            }
        )
        data_audit_symbols.append(
            {
                "symbol": symbol,
                "csv": str(csv_path),
                "ok": audit["ok"] and funding_audit["ok"],
                "rows": audit["rows"],
                "duplicate_count": audit["duplicate_count"],
                "gap_count": audit["gap_count"],
                "conflict_count": audit["conflict_count"],
                "open_time_start": audit["open_time_start"],
                "open_time_end": audit["open_time_end"],
                "funding_events": funding_audit["events"],
                "funding_coverage_ratio": funding_audit["coverage_ratio"],
                "funding_interval_hours_median": funding_audit["interval_hours_median"],
                "funding_ok": funding_audit["ok"],
                "notes": audit.get("notes", []) + funding_audit.get("notes", []),
            }
        )
        if not (audit["ok"] and funding_audit["ok"]):
            data_quality_failed = True
            data_quality_reasons.append(f"{symbol}: data/funding audit failed")

        allowed_profiles = [
            profile
            for profile in profiles
            if (
                profile.direction_mode == GridDirectionMode.NEUTRAL and sp.allow_neutral
            )
            or (profile.direction_mode == GridDirectionMode.LONG and sp.allow_long)
        ]

        for window in windows:
            window_manifest.append(
                {
                    "symbol": symbol,
                    "market_group": window.market_group,
                    "window_key": window.window_key,
                    "window_kind": window.window_kind,
                    "split": window.split,
                    "start_time": window.start_time.isoformat(),
                    "end_time": window.end_time.isoformat(),
                    "bar_count": len(window.rows),
                    "previous_market_close": (
                        window.previous_market_close.isoformat()
                        if window.previous_market_close
                        else ""
                    ),
                    "force_close_at": (
                        window.force_close_at.isoformat() if window.force_close_at else ""
                    ),
                    "next_reference_open": (
                        window.next_reference_open.isoformat()
                        if window.next_reference_open
                        else ""
                    ),
                }
            )
            all_windows.append(
                {
                    "symbol": symbol,
                    "window_key": window.window_key,
                    "start": window.start_time,
                    "end": window.end_time,
                }
            )

            if len(window.rows) < observe + minimum:
                blocked.append(
                    _blocked(
                        symbol,
                        window,
                        "ALL",
                        "INSUFFICIENT_WINDOW_ROWS",
                        str(len(window.rows)),
                    )
                )
                continue

            obs = list(window.rows[:observe])
            trade = list(window.rows[observe:])
            price = float(obs[-1]["close"])
            events = [
                item
                for item in funding
                if int(trade[0]["open_time"])
                <= item.funding_time
                <= int(trade[-1]["close_time"])
            ]
            prior = [
                item.funding_rate
                for item in funding
                if item.funding_time <= int(obs[-1]["close_time"])
            ]
            funding_rate = prior[-1] if prior else 0.0
            projected = sum(abs(item.funding_rate) for item in events)

            for profile in allowed_profiles:
                for scenario in scenarios:
                    try:
                        candidate = build_semiconductor_grid_candidate(
                            symbol_profile=sp,
                            strategy_profile=profile,
                            klines=obs,
                            current_price=price,
                            funding_rate=funding_rate,
                            projected_funding_pct=projected,
                            maker_fee_rate=scenario.maker_fee_rate,
                            regime_score=100.0,
                            capital=capital,
                            leverage=leverage,
                            tick_size=rule.tick_size,
                            step_size=rule.step_size,
                            min_qty=rule.min_qty,
                            min_notional=rule.min_notional,
                            taker_fee_rate=scenario.taker_fee_rate,
                            base_grid_config=base_grid,
                            viability_config=viability,
                            long_signal_config=long_signal,
                        )
                    except StrategyAdmissionError as exc:
                        code = "ADMISSION_BLOCKED"
                        message = str(exc)
                        if "做多信号未通过" in message:
                            code = "SIGNAL_BLOCKED"
                        elif "Grid Viability Gate" in message:
                            code = "VIABILITY_BLOCKED"
                        blocked.append(
                            _blocked(
                                symbol,
                                window,
                                profile.name,
                                code,
                                message,
                                scenario.name,
                            )
                        )
                        viability_rows.append(
                            _viability_row(
                                symbol,
                                window,
                                profile.name,
                                scenario.name,
                                getattr(exc, "viability", None),
                                getattr(exc, "long_signal", None),
                                code,
                                message,
                            )
                        )
                        continue
                    except ValueError as exc:
                        blocked.append(
                            _blocked(
                                symbol,
                                window,
                                profile.name,
                                "ADMISSION_BLOCKED",
                                str(exc),
                                scenario.name,
                            )
                        )
                        continue

                    viability_rows.append(
                        _viability_row(
                            symbol,
                            window,
                            profile.name,
                            scenario.name,
                            candidate.viability,
                            candidate.long_signal,
                            "PASSED",
                            "",
                        )
                    )
                    symbol_capital = capital * sp.capital_multiplier
                    for seed in SEEDS:
                        result = run_grid_backtest(
                            candidate.params,
                            trade,
                            current_price=price,
                            config=BacktestConfig(
                                capital=symbol_capital,
                                leverage=leverage,
                                maker_fee_rate=scenario.maker_fee_rate,
                                taker_fee_rate=scenario.taker_fee_rate,
                                fill_model="L0_CONSERVATIVE",
                                min_tick_size=rule.tick_size,
                                quantity_step_size=rule.step_size,
                                max_fills_per_bar=scenario.max_fills_per_bar,
                                maker_fill_probability=scenario.maker_fill_probability,
                                fill_probability_seed=seed,
                                stop_slippage_bps=scenario.stop_slippage_bps,
                                seed_slippage_bps=float(cfg.get("seed_slippage_bps", 10)),
                                force_close_at_end=True,
                                direction_mode=profile.direction_mode,
                                max_inventory_notional=symbol_capital
                                * float(cfg.get("max_inventory_multiplier", 1)),
                                inventory_caution_utilization=float(
                                    cfg.get("inventory_caution_utilization", 0.4)
                                ),
                                inventory_critical_utilization=float(
                                    cfg.get("inventory_critical_utilization", 0.8)
                                ),
                                max_unpaired_lots_per_side=int(
                                    cfg.get("max_unpaired_lots_per_side", 8)
                                ),
                            ),
                            funding_events=events,
                        )
                        rows_out.append(
                            _result_row(
                                symbol,
                                window,
                                profile.name,
                                scenario.name,
                                seed,
                                symbol_capital,
                                candidate,
                                result,
                            )
                        )


    finished_at = datetime.now(UTC)
    summary = _aggregate(rows_out, ("market_group", "profile", "scenario"))
    split_summary = _aggregate(
        rows_out, ("market_group", "profile", "scenario", "split")
    )
    seed_summary = _aggregate(
        rows_out, ("market_group", "profile", "scenario", "seed")
    )
    symbol_summary = _aggregate(rows_out, ("symbol", "profile", "scenario"))
    month_summary = _aggregate(rows_out, ("year_month", "profile", "scenario"))
    assessments = assess_profiles(summary, cfg.get("acceptance", {}) or {})
    acceptance_payload = build_acceptance_payload(
        rows_out=rows_out,
        summary=summary,
        split_summary=split_summary,
        seed_summary=seed_summary,
        assessments=assessments,
        acceptance=cfg.get("acceptance", {}) or {},
        total_unique_windows=total_unique_windows,
        data_quality_failed=data_quality_failed,
        data_quality_reasons=data_quality_reasons,
    )
    conclusion = acceptance_payload["conclusion_code"]
    overlap_report = _window_overlap_report(all_windows)
    cost_rows = _cost_breakdown(rows_out)
    inventory_rows = _inventory_breakdown(rows_out)

    dependency_manifest = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "packages": _package_versions(
            ["pandas", "pandas_market_calendars", "PyYAML", "httpx", "numpy"]
        ),
        "generated_at": finished_at.isoformat(),
    }
    run_manifest = {
        "schema_version": 1,
        "strategy_version": STRATEGY_VERSION,
        "base_branch": "master",
        "base_commit_sha": args.base_commit or _git_head("master"),
        "research_branch": _git_branch(),
        "run_started_at_utc": started_at.isoformat(),
        "run_finished_at_utc": finished_at.isoformat(),
        "python_version": platform.python_version(),
        "symbols": list(symbols),
        "seeds": list(SEEDS),
        "profiles": [profile.name for profile in profiles],
        "scenarios": [scenario.name for scenario in scenarios],
        "data_dir": str(data_dir),
        "rules_json": str(rules_path) if rules_path.exists() else None,
        "output_dir": str(output),
        "observation_rows": observe,
        "minimum_trade_rows": minimum,
        "force_close_minutes": force_close_minutes,
        "minimum_trade_minutes": minimum_trade_minutes,
        "total_unique_windows": total_unique_windows,
        "run_count": len(rows_out),
        "blocked_count": len(blocked),
        "conclusion_code": conclusion,
    }
    if rules_path.exists():
        hash_manifest["files"][str(rules_path)] = _sha(rules_path)
    hash_manifest["generated_at"] = finished_at.isoformat()

    data_audit_json = {
        "schema_version": 1,
        "generated_at": finished_at.isoformat(),
        "symbols": data_audit_symbols,
        "ok": not data_quality_failed,
        "reasons": data_quality_reasons,
    }
    payload = {
        "schema_version": 1,
        "strategy_version": STRATEGY_VERSION,
        "generated_at": finished_at.isoformat(),
        "conclusion": conclusion,
        "conclusion_code": conclusion,
        "input_manifest": input_manifest,
        "profile_summary": summary,
        "split_summary": split_summary,
        "seed_summary": seed_summary,
        "symbol_summary": symbol_summary,
        "month_summary": month_summary,
        "assessments": assessments,
        "acceptance": acceptance_payload,
        "blocked_count": len(blocked),
        "run_count": len(rows_out),
        "total_unique_windows": total_unique_windows,
        "best_candidate": acceptance_payload.get("best_candidate"),
    }

    _write_csv(output / "window-results.csv", rows_out)
    _write_csv(output / "window-breakdown.csv", rows_out)
    _write_csv(output / "blocked-windows.csv", blocked)
    _write_csv(output / "profile-summary.csv", summary)
    _write_csv(output / "profile-scenario-summary.csv", summary)
    _write_csv(output / "seed-breakdown.csv", seed_summary)
    _write_csv(output / "symbol-breakdown.csv", symbol_summary)
    _write_csv(output / "month-breakdown.csv", month_summary)
    _write_csv(output / "assessment.csv", assessments)
    _write_csv(output / "window-manifest.csv", window_manifest)
    _write_csv(output / "grid-viability-breakdown.csv", viability_rows)
    _write_csv(output / "inventory-breakdown.csv", inventory_rows)
    _write_csv(output / "cost-breakdown.csv", cost_rows)
    _write_csv(
        output / "parameter-search.csv",
        [
            {
                "phase": "B",
                "status": "fixed_baseline_only",
                "note": "No Phase C search executed; fixed registered matrix only.",
            }
        ],
    )
    _write_json(output / "results.json", payload)
    _write_json(output / "run-manifest.json", run_manifest)
    _write_json(output / "dependency-manifest.json", dependency_manifest)
    _write_json(output / "input-hash-manifest.json", hash_manifest)
    _write_json(output / "data-audit.json", data_audit_json)
    _write_json(output / "window-manifest.json", {"windows": window_manifest})
    _write_json(output / "acceptance-gates.json", acceptance_payload)
    if rules_path.exists():
        report_rules = output / "exchange-rules.json"
        if rules_path.resolve() != report_rules.resolve():
            report_rules.write_text(rules_path.read_text(encoding="utf-8"), encoding="utf-8")
    (output / "data-audit.md").write_text(
        _data_audit_markdown(data_audit_json, input_manifest), encoding="utf-8"
    )
    (output / "window-overlap-audit.md").write_text(overlap_report, encoding="utf-8")
    (output / "final-report.md").write_text(
        _final_report(payload, acceptance_payload, data_audit_json, run_manifest),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(output),
                "conclusion": conclusion,
                "runs": len(rows_out),
                "blocked": len(blocked),
                "windows": total_unique_windows,
            },
            ensure_ascii=False,
        )
    )


def _result_row(
    symbol: str,
    window: ClosedWindow,
    profile: str,
    scenario: str,
    seed: int,
    capital: float,
    candidate: Any,
    result: BacktestResult,
) -> dict[str, Any]:
    paired = max(0.0, float(result.gross_grid_pnl))
    drag = max(0.0, -float(result.unrealized_pnl))
    inventory_drag = drag / max(paired, 1e-12)
    total_cost = (
        float(result.fees_paid)
        + float(result.funding_paid)
        + float(result.stop_exit_cost)
        + float(getattr(result, "seed_fee", 0.0) or 0.0)
    )
    snap = candidate.viability.snapshot
    return {
        "symbol": symbol,
        "market_group": window.market_group,
        "window_key": window.window_key,
        "window_kind": window.window_kind,
        "split": window.split,
        "year_month": window.start_time.strftime("%Y-%m"),
        "window_start": window.start_time.isoformat(),
        "window_end": window.end_time.isoformat(),
        "profile": profile,
        "scenario": scenario,
        "seed": seed,
        "grid_num": candidate.params.grid_num,
        "step_pct": candidate.params.step_pct,
        "crossings_per_hour": snap.crossings_per_hour,
        "reversal_ratio": snap.reversal_ratio,
        "net_capacity_per_hour": snap.net_capacity_per_hour,
        "zero_activity_ratio": snap.zero_activity_ratio,
        "trade_count_per_hour": snap.trade_count_per_hour,
        "quote_volume_per_hour": snap.quote_volume_per_hour,
        "total_pnl": result.total_pnl,
        "net_pnl": result.total_pnl,
        "gross_grid_pnl": result.gross_grid_pnl,
        "fees_paid": result.fees_paid,
        "funding_paid": result.funding_paid,
        "stop_exit_cost": result.stop_exit_cost,
        "seed_fee": float(getattr(result, "seed_fee", 0.0) or 0.0),
        "total_cost": total_cost,
        "unrealized_pnl": result.unrealized_pnl,
        "inventory_drag": drag,
        "inventory_drag_ratio": inventory_drag,
        "max_drawdown": result.max_drawdown,
        "max_drawdown_pct": result.max_drawdown / max(capital, 1e-12),
        "pair_completion_count": result.pair_completion_count,
        "attempted_fill_count": result.attempted_fill_count,
        "rejected_fill_count": result.rejected_fill_count,
        "accepted_fill_count": max(
            0, int(result.attempted_fill_count) - int(result.rejected_fill_count)
        ),
        "fills": len(result.fills),
        "max_inventory_utilization": result.max_inventory_utilization,
        "max_unpaired_lot_age": result.max_unpaired_lot_age_bars,
        "force_close_inventory_notional": abs(result.net_position_qty) * result.last_price,
        "stopped_reason": result.stopped_reason or "",
        "capital": capital,
    }


def _aggregate(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in rows:
        try:
            key = tuple(row[k] for k in keys)
        except KeyError:
            continue
        groups.setdefault(key, []).append(row)
    output: list[dict[str, Any]] = []
    for key, runs in sorted(groups.items(), key=lambda item: item[0]):
        by_symbol_window: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for run in runs:
            by_symbol_window.setdefault(
                (str(run["symbol"]), str(run["window_key"])), []
            ).append(run)
        symbol_windows: list[dict[str, Any]] = []
        for (symbol, window_key), seed_rows in by_symbol_window.items():
            symbol_windows.append(
                {
                    "symbol": symbol,
                    "window_key": window_key,
                    "pnl": statistics.fmean(float(x["total_pnl"]) for x in seed_rows),
                    "drawdown": max(float(x["max_drawdown"]) for x in seed_rows),
                    "drawdown_pct": max(float(x["max_drawdown_pct"]) for x in seed_rows),
                    "fees": statistics.fmean(float(x["fees_paid"]) for x in seed_rows),
                    "funding": statistics.fmean(float(x["funding_paid"]) for x in seed_rows),
                    "drag": statistics.fmean(
                        float(x["inventory_drag_ratio"]) for x in seed_rows
                    ),
                    "gross": statistics.fmean(
                        float(x.get("gross_grid_pnl", 0.0)) for x in seed_rows
                    ),
                    "cost": statistics.fmean(
                        float(x.get("total_cost", 0.0)) for x in seed_rows
                    ),
                }
            )
        by_window: dict[str, list[dict[str, Any]]] = {}
        for item in symbol_windows:
            by_window.setdefault(item["window_key"], []).append(item)
        portfolios: list[dict[str, Any]] = []
        for window_key, items in by_window.items():
            portfolios.append(
                {
                    "window_key": window_key,
                    "pnl": sum(item["pnl"] for item in items),
                    "drawdown": sum(item["drawdown"] for item in items),
                    "drawdown_pct": max(item["drawdown_pct"] for item in items),
                    "fees": sum(item["fees"] for item in items),
                    "funding": sum(item["funding"] for item in items),
                    "drag": statistics.fmean(item["drag"] for item in items),
                    "gross": sum(item["gross"] for item in items),
                    "cost": sum(item["cost"] for item in items),
                }
            )
        pnls = [item["pnl"] for item in portfolios]
        positives = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        total = sum(pnls)
        best = max(positives, default=0.0)
        positive_sum = sum(positives)
        concentration = best / positive_sum if positive_sum > 0 else (1.0 if pnls else 0.0)
        top3 = sorted(positives, reverse=True)[:3]
        top3_concentration = (
            sum(top3) / positive_sum if positive_sum > 0 else (1.0 if pnls else 0.0)
        )
        record = {name: value for name, value in zip(keys, key)}
        record.update(
            {
                "runs": len(runs),
                "unique_windows": len(portfolios),
                "total_pnl": total,
                "mean_window_pnl": statistics.fmean(pnls) if pnls else 0.0,
                "median_window_pnl": statistics.median(pnls) if pnls else 0.0,
                "worst_window_pnl": min(pnls) if pnls else 0.0,
                "positive_ratio": len(positives) / len(pnls) if pnls else 0.0,
                "profit_factor": (
                    sum(positives) / abs(sum(losses))
                    if losses
                    else (sum(positives) if positives else 0.0)
                ),
                "max_drawdown": max((item["drawdown"] for item in portfolios), default=0.0),
                "max_drawdown_pct": max(
                    (item["drawdown_pct"] for item in portfolios), default=0.0
                ),
                "fees_paid": sum(item["fees"] for item in portfolios),
                "funding_paid": sum(item["funding"] for item in portfolios),
                "gross_grid_pnl": sum(item["gross"] for item in portfolios),
                "total_cost": sum(item["cost"] for item in portfolios),
                "mean_inventory_drag_ratio": (
                    statistics.fmean(item["drag"] for item in portfolios)
                    if portfolios
                    else 0.0
                ),
                "best_window_concentration": concentration,
                "top_3_window_concentration": top3_concentration,
                "cvar_95": _cvar(pnls, 0.05),
            }
        )
        output.append(record)
    return output


def assess_profiles(
    summary: list[dict[str, Any]], acceptance: Mapping[str, Any]
) -> list[dict[str, Any]]:
    limits = {
        "minimum_unique_windows": int(acceptance.get("minimum_unique_windows", 8)),
        "minimum_positive_ratio": float(acceptance.get("minimum_positive_ratio", 0.55)),
        "minimum_profit_factor": float(acceptance.get("minimum_profit_factor", 1.05)),
        "maximum_drawdown_pct_of_capital": float(
            acceptance.get("maximum_drawdown_pct_of_capital", 0.05)
        ),
        "maximum_mean_inventory_drag_ratio": float(
            acceptance.get("maximum_mean_inventory_drag_ratio", 0.35)
        ),
        "maximum_best_window_concentration": float(
            acceptance.get("maximum_best_window_concentration", 0.35)
        ),
    }
    indexed = {
        (item["market_group"], item["profile"], item["scenario"]): item for item in summary
    }
    combos = sorted({(item["market_group"], item["profile"]) for item in summary})
    out: list[dict[str, Any]] = []
    for market, profile in combos:
        primary = indexed.get((market, profile, "PRIMARY_ZERO_MAKER"))
        stress = indexed.get((market, profile, "EXECUTION_STRESS"))
        reasons: list[str] = []
        if not primary:
            reasons.append("missing_primary")
        if not stress:
            reasons.append("missing_execution_stress")
        if primary:
            if int(primary["unique_windows"]) < limits["minimum_unique_windows"]:
                reasons.append("insufficient_windows")
            if float(primary["total_pnl"]) <= 0:
                reasons.append("primary_not_positive")
            if float(primary["positive_ratio"]) < limits["minimum_positive_ratio"]:
                reasons.append("low_positive_ratio")
            if float(primary["profit_factor"]) < limits["minimum_profit_factor"]:
                reasons.append("low_profit_factor")
            if float(primary["max_drawdown_pct"]) > limits["maximum_drawdown_pct_of_capital"]:
                reasons.append("drawdown_too_high")
            if (
                float(primary["mean_inventory_drag_ratio"])
                > limits["maximum_mean_inventory_drag_ratio"]
            ):
                reasons.append("inventory_drag_too_high")
            if (
                float(primary["best_window_concentration"])
                > limits["maximum_best_window_concentration"]
            ):
                reasons.append("window_concentration_too_high")
        if stress and float(stress["total_pnl"]) <= 0:
            reasons.append("execution_stress_not_positive")
        passed = not reasons
        out.append(
            {
                "market_group": market,
                "profile": profile,
                "passed": passed,
                "conclusion": (
                    "SEMICONDUCTOR_GRID_RESEARCH_CANDIDATE"
                    if passed
                    else "SEMICONDUCTOR_GRID_NOT_VALIDATED"
                ),
                "reasons": ";".join(reasons),
            }
        )
    return out


def build_acceptance_payload(
    *,
    rows_out: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    split_summary: list[dict[str, Any]],
    seed_summary: list[dict[str, Any]],
    assessments: list[dict[str, Any]],
    acceptance: Mapping[str, Any],
    total_unique_windows: int,
    data_quality_failed: bool,
    data_quality_reasons: list[str],
) -> dict[str, Any]:
    limits = {
        "minimum_unique_windows": int(acceptance.get("minimum_unique_windows", 8)),
        "minimum_positive_ratio": float(acceptance.get("minimum_positive_ratio", 0.55)),
        "minimum_profit_factor": float(acceptance.get("minimum_profit_factor", 1.05)),
        "maximum_drawdown_pct_of_capital": float(
            acceptance.get("maximum_drawdown_pct_of_capital", 0.05)
        ),
        "maximum_mean_inventory_drag_ratio": float(
            acceptance.get("maximum_mean_inventory_drag_ratio", 0.35)
        ),
        "maximum_best_window_concentration": float(
            acceptance.get("maximum_best_window_concentration", 0.35)
        ),
    }
    if data_quality_failed:
        return {
            "conclusion_code": "FAIL_DATA_QUALITY",
            "passed": False,
            "limits": limits,
            "gates": [],
            "best_candidate": None,
            "reasons": data_quality_reasons,
            "assessments": assessments,
        }
    if total_unique_windows < limits["minimum_unique_windows"]:
        return {
            "conclusion_code": "FAIL_INSUFFICIENT_DATA",
            "passed": False,
            "limits": limits,
            "gates": [
                {
                    "gate": "minimum_unique_windows",
                    "threshold": limits["minimum_unique_windows"],
                    "actual": total_unique_windows,
                    "status": "FAIL",
                }
            ],
            "best_candidate": None,
            "reasons": [f"total_unique_windows={total_unique_windows}"],
            "assessments": assessments,
        }
    if not rows_out:
        return {
            "conclusion_code": "FAIL_NO_ROBUST_EDGE",
            "passed": False,
            "limits": limits,
            "gates": [],
            "best_candidate": None,
            "reasons": ["no_valid_backtest_runs"],
            "assessments": assessments,
        }

    split_index = {
        (
            item.get("market_group"),
            item.get("profile"),
            item.get("scenario"),
            item.get("split"),
        ): item
        for item in split_summary
    }
    seed_index: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for item in seed_summary:
        seed_index[
            (item.get("market_group"), item.get("profile"), item.get("scenario"))
        ].append(item)

    candidates: list[dict[str, Any]] = []
    for assessment in assessments:
        market = assessment["market_group"]
        profile = assessment["profile"]
        primary = next(
            (
                item
                for item in summary
                if item.get("market_group") == market
                and item.get("profile") == profile
                and item.get("scenario") == "PRIMARY_ZERO_MAKER"
            ),
            None,
        )
        stress = next(
            (
                item
                for item in summary
                if item.get("market_group") == market
                and item.get("profile") == profile
                and item.get("scenario") == "EXECUTION_STRESS"
            ),
            None,
        )
        maker_off = next(
            (
                item
                for item in summary
                if item.get("market_group") == market
                and item.get("profile") == profile
                and item.get("scenario") == "MAKER_PROMO_OFF"
            ),
            None,
        )
        if not primary or not stress:
            continue
        dev = split_index.get((market, profile, "PRIMARY_ZERO_MAKER", "Development"))
        val = split_index.get((market, profile, "PRIMARY_ZERO_MAKER", "Validation"))
        oos = split_index.get((market, profile, "PRIMARY_ZERO_MAKER", "Final_OOS"))
        seed_rows = seed_index.get((market, profile, "PRIMARY_ZERO_MAKER"), [])
        positive_seeds = sum(1 for item in seed_rows if float(item.get("total_pnl", 0)) > 0)
        gates = [
            _gate(
                "minimum_unique_windows",
                limits["minimum_unique_windows"],
                int(primary.get("unique_windows", 0)),
                int(primary.get("unique_windows", 0)) >= limits["minimum_unique_windows"],
            ),
            _gate(
                "positive_window_ratio",
                limits["minimum_positive_ratio"],
                float(primary.get("positive_ratio", 0.0)),
                float(primary.get("positive_ratio", 0.0)) >= limits["minimum_positive_ratio"],
            ),
            _gate(
                "profit_factor",
                limits["minimum_profit_factor"],
                float(primary.get("profit_factor", 0.0)),
                float(primary.get("profit_factor", 0.0)) >= limits["minimum_profit_factor"],
            ),
            _gate(
                "max_drawdown_pct_of_capital",
                limits["maximum_drawdown_pct_of_capital"],
                float(primary.get("max_drawdown_pct", 0.0)),
                float(primary.get("max_drawdown_pct", 0.0))
                <= limits["maximum_drawdown_pct_of_capital"],
            ),
            _gate(
                "mean_inventory_drag_ratio",
                limits["maximum_mean_inventory_drag_ratio"],
                float(primary.get("mean_inventory_drag_ratio", 0.0)),
                float(primary.get("mean_inventory_drag_ratio", 0.0))
                <= limits["maximum_mean_inventory_drag_ratio"],
            ),
            _gate(
                "best_window_concentration",
                limits["maximum_best_window_concentration"],
                float(primary.get("best_window_concentration", 1.0)),
                float(primary.get("best_window_concentration", 1.0))
                <= limits["maximum_best_window_concentration"],
            ),
            _gate(
                "PRIMARY_ZERO_MAKER_net_pnl",
                0.0,
                float(primary.get("total_pnl", 0.0)),
                float(primary.get("total_pnl", 0.0)) > 0,
            ),
            _gate(
                "EXECUTION_STRESS_net_pnl",
                0.0,
                float(stress.get("total_pnl", 0.0)),
                float(stress.get("total_pnl", 0.0)) > 0,
            ),
            _gate(
                "validation_positive",
                0.0,
                float(val.get("total_pnl", 0.0)) if val else None,
                bool(val) and float(val.get("total_pnl", 0.0)) > 0,
            ),
            _gate(
                "final_oos_positive",
                0.0,
                float(oos.get("total_pnl", 0.0)) if oos else None,
                bool(oos) and float(oos.get("total_pnl", 0.0)) > 0,
            ),
            _gate("positive_seed_count", 4, positive_seeds, positive_seeds >= 4),
        ]
        maker_dependent = bool(maker_off) and float(maker_off.get("total_pnl", 0.0)) <= 0
        hard_fail_reasons = [gate["gate"] for gate in gates if gate["status"] == "FAIL"]
        inventory_fail = "mean_inventory_drag_ratio" in hard_fail_reasons
        stress_fail = "EXECUTION_STRESS_net_pnl" in hard_fail_reasons
        passed_hard = not hard_fail_reasons
        candidates.append(
            {
                "market_group": market,
                "profile": profile,
                "passed": passed_hard and not maker_dependent,
                "maker_dependent": maker_dependent,
                "primary_total_pnl": float(primary.get("total_pnl", 0.0)),
                "stress_total_pnl": float(stress.get("total_pnl", 0.0)),
                "maker_off_total_pnl": (
                    float(maker_off.get("total_pnl", 0.0)) if maker_off else None
                ),
                "final_oos_net_pnl": float(oos.get("total_pnl", 0.0)) if oos else None,
                "validation_net_pnl": float(val.get("total_pnl", 0.0)) if val else None,
                "development_net_pnl": float(dev.get("total_pnl", 0.0)) if dev else None,
                "positive_seed_count": positive_seeds,
                "gates": gates,
                "hard_fail_reasons": hard_fail_reasons,
                "inventory_fail": inventory_fail,
                "stress_fail": stress_fail,
                "assessment_passed": bool(assessment.get("passed")),
            }
        )

    if not candidates:
        return {
            "conclusion_code": "FAIL_NO_ROBUST_EDGE",
            "passed": False,
            "limits": limits,
            "gates": [],
            "best_candidate": None,
            "reasons": ["no_assessable_candidates"],
            "assessments": assessments,
            "candidates": [],
        }

    ranked = sorted(
        candidates,
        key=lambda item: (
            1 if item["passed"] else 0,
            1 if item["assessment_passed"] else 0,
            item["primary_total_pnl"],
            item["stress_total_pnl"],
        ),
        reverse=True,
    )
    best = ranked[0]
    if any(item["passed"] for item in ranked):
        conclusion = "PASS_TESTNET_CANDIDATE"
        best = next(item for item in ranked if item["passed"])
    elif any(not item["hard_fail_reasons"] and item["maker_dependent"] for item in ranked):
        conclusion = "PASS_RESEARCH_ONLY_MAKER_DEPENDENT"
        best = next(
            item
            for item in ranked
            if not item["hard_fail_reasons"] and item["maker_dependent"]
        )
    elif all(item["stress_fail"] for item in ranked):
        conclusion = "FAIL_EXECUTION_STRESS"
    elif all(item["inventory_fail"] for item in ranked):
        conclusion = "FAIL_INVENTORY_TAIL"
    else:
        conclusion = "FAIL_NO_ROBUST_EDGE"

    return {
        "conclusion_code": conclusion,
        "passed": conclusion == "PASS_TESTNET_CANDIDATE",
        "limits": limits,
        "gates": best.get("gates", []),
        "best_candidate": best,
        "reasons": best.get("hard_fail_reasons", []),
        "assessments": assessments,
        "candidates": ranked,
    }


def _scenarios(raw: Mapping[str, Any]) -> tuple[Scenario, ...]:
    values = raw.get("scenarios", {}) if isinstance(raw, Mapping) else {}
    result: list[Scenario] = []
    for name, spec0 in values.items():
        spec = dict(spec0 or {})
        result.append(
            Scenario(
                str(name).upper(),
                float(spec.get("maker_fee_rate", 0)),
                float(spec.get("taker_fee_rate", 0.0005)),
                float(spec.get("maker_fill_probability", 0.65)),
                int(spec.get("max_fills_per_bar", 2)),
                float(spec.get("stop_slippage_bps", 10)),
            )
        )
    return tuple(result) or (
        Scenario("PRIMARY_ZERO_MAKER", 0, 0.0005, 0.65, 2, 10),
        Scenario("EXECUTION_STRESS", 0, 0.00075, 0.45, 1, 25),
        Scenario("MAKER_PROMO_OFF", 0.0002, 0.0005, 0.65, 2, 15),
    )


def _grid(raw: Mapping[str, Any]) -> AdaptiveGridConfig:
    grid = raw.get("grid", {}) or {}
    costs = raw.get("costs", {}) or {}
    trading = raw.get("trading", {}) or {}
    return AdaptiveGridConfig(
        center_half_life_minutes=float(grid.get("center_half_life_minutes", 30)),
        k_atr_range=float(grid.get("k_atr_range", 2)),
        k_sigma_range=float(grid.get("k_sigma_range", 2)),
        max_range_pct=float(grid.get("max_range_pct", 0.03)),
        min_step_pct=float(grid.get("min_step_pct", 0.0015)),
        max_step_pct=float(grid.get("max_step_pct", 0.01)),
        k_atr_step=float(grid.get("k_atr_step", 0.5)),
        k_sigma_step=float(grid.get("k_sigma_step", 0.8)),
        min_grid_num=int(grid.get("min_grid_num", 3)),
        max_grid_num=int(grid.get("max_grid_num", 100)),
        expansion_rate=float(grid.get("expansion_rate", 0.08)),
        stop_buffer_pct=float(trading.get("stop_buffer_pct", 0.015)),
        adverse_selection_buffer_pct=float(
            costs.get("adverse_selection_buffer_pct", 0.0002)
        ),
        slippage_buffer_pct=float(costs.get("slippage_buffer_pct", 0.0003)),
        safety_margin_pct=float(costs.get("safety_margin_pct", 0.0002)),
        horizon_bars=int(grid.get("horizon_bars", 60)),
        volatility_estimator=str(grid.get("volatility_estimator", "ewma")),
    )


def _viability(raw: Mapping[str, Any]) -> GridViabilityConfig:
    return GridViabilityConfig(
        lookback_bars=int(raw.get("lookback_bars", 60)),
        bars_per_hour=float(raw.get("bars_per_hour", 60)),
        min_crossings_per_hour=float(raw.get("min_crossings_per_hour", 1)),
        min_reversal_ratio=float(raw.get("min_reversal_ratio", 0.25)),
        max_zero_activity_ratio=float(raw.get("max_zero_activity_ratio", 0.2)),
        min_trade_count_per_hour=float(raw.get("min_trade_count_per_hour", 60)),
        min_quote_volume_per_hour=float(raw.get("min_quote_volume_per_hour", 10000)),
        max_spread_to_step_ratio=float(raw.get("max_spread_to_step_ratio", 0.5)),
        min_net_capacity_per_hour=float(raw.get("min_net_capacity_per_hour", 0.00025)),
    )


def _scheduler(sp: Any, cfg: Mapping[str, Any]) -> Scheduler:
    reference = None
    if sp.reference_open_time not in (None, "", "null"):
        hour, minute = [int(part) for part in str(sp.reference_open_time).split(":")[:2]]
        reference = time(hour=hour, minute=minute)
    return Scheduler(
        force_close_minutes=int(cfg.get("force_close_minutes", 120)),
        minimum_trade_minutes=int(cfg.get("minimum_trade_minutes", 120)),
        calendar_name=sp.calendar_name,
        market_timezone=sp.market_timezone,
        premarket_time=reference,
        window_key_prefix=sp.market_group,
    )


def _read_klines_with_audit(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            open_time = int(float(raw.get("open_time") or raw.get("timestamp") or 0))
            close_time = int(float(raw.get("close_time") or open_time + 59_999))
            rows.append(
                {
                    "open_time": open_time,
                    "close_time": close_time,
                    "timestamp": open_time,
                    "open": float(raw["open"]),
                    "high": float(raw["high"]),
                    "low": float(raw["low"]),
                    "close": float(raw["close"]),
                    "volume": float(raw.get("volume") or 0),
                    "quote_volume": float(raw.get("quote_volume") or 0),
                    "trade_count": int(float(raw.get("trade_count") or 0)),
                }
            )
    rows.sort(key=lambda item: item["open_time"])
    duplicate_count = len(rows) - len({item["open_time"] for item in rows})
    if duplicate_count:
        raise ValueError(f"{path} 存在重复时间")
    gap_count = 0
    conflict_count = 0
    notes: list[str] = []
    for index, item in enumerate(rows):
        if (
            item["close_time"] <= item["open_time"]
            or item["high"] < max(item["open"], item["close"])
            or item["low"] > min(item["open"], item["close"])
            or item["low"] <= 0
        ):
            conflict_count += 1
            raise ValueError(f"{path} 第 {index + 1} 行非法")
        if index and item["open_time"] - rows[index - 1]["open_time"] != 60_000:
            gap_count += 1
            raise ValueError(f"{path} 1m 数据不连续")
    audit = {
        "ok": True,
        "rows": len(rows),
        "duplicate_count": duplicate_count,
        "gap_count": gap_count,
        "conflict_count": conflict_count,
        "open_time_start": rows[0]["open_time"] if rows else None,
        "open_time_end": rows[-1]["open_time"] if rows else None,
        "notes": notes,
    }
    return rows, audit


def _read_funding(path: Path) -> list[FundingEvent]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("events", payload) if isinstance(payload, dict) else payload
    return sorted(
        (
            FundingEvent(
                int(item["funding_time"]),
                float(item["funding_rate"]),
                float(item["mark_price"])
                if item.get("mark_price") not in (None, "")
                else None,
            )
            for item in records or []
        ),
        key=lambda item: item.funding_time,
    )


def _audit_funding(
    bars: Sequence[dict[str, Any]],
    funding: Sequence[FundingEvent],
    path: Path | None,
) -> dict[str, Any]:
    notes: list[str] = []
    if path is None:
        return {
            "ok": False,
            "events": 0,
            "coverage_ratio": 0.0,
            "interval_hours_median": None,
            "notes": ["missing_funding_sidecar"],
        }
    if not funding:
        return {
            "ok": False,
            "events": 0,
            "coverage_ratio": 0.0,
            "interval_hours_median": None,
            "notes": ["empty_funding_events"],
        }
    start = int(bars[0]["open_time"])
    end = int(bars[-1]["close_time"])
    covered = [item for item in funding if start <= item.funding_time <= end]
    intervals = [
        (later.funding_time - earlier.funding_time) / 3_600_000
        for earlier, later in zip(covered, covered[1:])
    ]
    median_interval = statistics.median(intervals) if intervals else None
    expected = max(1.0, (end - start) / 3_600_000 / 8.0)
    coverage_ratio = len(covered) / expected if expected else 0.0
    ok = bool(covered) and coverage_ratio >= 0.8
    if not ok:
        notes.append(
            f"funding_coverage_ratio={coverage_ratio:.3f} events={len(covered)}"
        )
    return {
        "ok": ok,
        "events": len(covered),
        "coverage_ratio": coverage_ratio,
        "interval_hours_median": median_interval,
        "notes": notes,
    }


def _load_rules(path: Path) -> dict[str, RuleSnapshot]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("symbols", payload)
    result: dict[str, RuleSnapshot] = {}
    for key, values in records.items():
        result[str(key).upper()] = RuleSnapshot(
            tick_size=float(values.get("tick_size", 0)),
            step_size=float(values.get("step_size", 0)),
            min_qty=float(values.get("min_qty", 0)),
            min_notional=float(values.get("min_notional", 0)),
            status=str(values.get("status", "")),
            contract_type=str(values.get("contract_type", "")),
            onboard_date=int(values.get("onboard_date", 0) or 0),
            raw=dict(values),
        )
    return result


def _find_csv(data_dir: Path, symbol: str) -> Path:
    for path in (data_dir / f"{symbol}-1m.csv", data_dir / f"{symbol}.csv"):
        if path.exists():
            return path
    matches = sorted(data_dir.glob(f"*{symbol}*1m*.csv"))
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"未找到 {symbol} 1m CSV")


def _funding_path(csv_path: Path) -> Path | None:
    path = csv_path.with_suffix(".funding.json")
    return path if path.exists() else None


def _row_dt(row: Mapping[str, Any]) -> datetime:
    return datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=UTC)


def _blocked(
    symbol: str,
    window: ClosedWindow,
    profile: str,
    code: str,
    reason: str,
    scenario: str = "",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market_group": window.market_group,
        "window_key": window.window_key,
        "window_start": window.start_time.isoformat(),
        "window_end": window.end_time.isoformat(),
        "split": window.split,
        "profile": profile,
        "scenario": scenario,
        "code": code,
        "reason": reason,
    }


def _viability_row(
    symbol: str,
    window: ClosedWindow,
    profile: str,
    scenario: str,
    viability: Any,
    long_signal: Any,
    code: str,
    reason: str,
) -> dict[str, Any]:
    snapshot = getattr(viability, "snapshot", None)
    return {
        "symbol": symbol,
        "market_group": window.market_group,
        "window_key": window.window_key,
        "split": window.split,
        "profile": profile,
        "scenario": scenario,
        "code": code,
        "reason": reason,
        "allowed": bool(getattr(viability, "allowed", False)),
        "crossings_per_hour": getattr(snapshot, "crossings_per_hour", None),
        "reversal_ratio": getattr(snapshot, "reversal_ratio", None),
        "zero_activity_ratio": getattr(snapshot, "zero_activity_ratio", None),
        "trade_count_per_hour": getattr(snapshot, "trade_count_per_hour", None),
        "quote_volume_per_hour": getattr(snapshot, "quote_volume_per_hour", None),
        "spread_to_step_ratio": getattr(snapshot, "spread_to_step_ratio", None),
        "net_capacity_per_hour": getattr(snapshot, "net_capacity_per_hour", None),
        "long_signal_allowed": (
            None if long_signal is None else bool(getattr(long_signal, "allowed", False))
        ),
        "long_signal_reasons": (
            ""
            if long_signal is None
            else ";".join(getattr(long_signal, "reasons", ()) or ())
        ),
    }


def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        if not fields:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _cost_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _aggregate(rows, ("market_group", "profile", "scenario"))


def _inventory_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (row.get("market_group"), row.get("profile"), row.get("scenario"))
        groups[key].append(row)
    output: list[dict[str, Any]] = []
    for key, items in sorted(groups.items()):
        output.append(
            {
                "market_group": key[0],
                "profile": key[1],
                "scenario": key[2],
                "runs": len(items),
                "mean_inventory_drag_ratio": statistics.fmean(
                    float(item.get("inventory_drag_ratio", 0.0)) for item in items
                ),
                "mean_max_inventory_utilization": statistics.fmean(
                    float(item.get("max_inventory_utilization", 0.0)) for item in items
                ),
                "mean_max_unpaired_lot_age": statistics.fmean(
                    float(item.get("max_unpaired_lot_age", 0.0)) for item in items
                ),
                "mean_force_close_inventory_notional": statistics.fmean(
                    float(item.get("force_close_inventory_notional", 0.0)) for item in items
                ),
            }
        )
    return output


def _window_overlap_report(windows: list[dict[str, Any]]) -> str:
    lines = [
        "# Window Overlap Audit",
        "",
        f"- total_window_records: {len(windows)}",
    ]
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in windows:
        by_symbol[str(item["symbol"])].append(item)
    overlaps = 0
    for symbol, items in sorted(by_symbol.items()):
        ordered = sorted(items, key=lambda row: row["start"])
        symbol_overlaps = 0
        for earlier, later in zip(ordered, ordered[1:]):
            if later["start"] <= earlier["end"]:
                symbol_overlaps += 1
                overlaps += 1
                lines.append(
                    f"- OVERLAP {symbol}: {earlier['window_key']} vs {later['window_key']}"
                )
        lines.append(f"- {symbol}: windows={len(ordered)} overlaps={symbol_overlaps}")
    lines.append("")
    lines.append(f"- total_overlaps: {overlaps}")
    lines.append("")
    return "\n".join(lines)


def _data_audit_markdown(
    audit: Mapping[str, Any], input_manifest: Sequence[Mapping[str, Any]]
) -> str:
    lines = [
        "# Data Audit",
        "",
        f"- ok: `{audit.get('ok')}`",
        f"- generated_at: `{audit.get('generated_at')}`",
        "",
        "## Symbols",
        "",
    ]
    for item in audit.get("symbols", []):
        lines.extend(
            [
                f"### {item.get('symbol')}",
                f"- csv: `{item.get('csv')}`",
                f"- ok: `{item.get('ok')}`",
                f"- rows: `{item.get('rows')}`",
                f"- gaps: `{item.get('gap_count')}`",
                f"- duplicates: `{item.get('duplicate_count')}`",
                f"- funding_events: `{item.get('funding_events')}`",
                f"- funding_coverage_ratio: `{item.get('funding_coverage_ratio')}`",
                f"- notes: `{'; '.join(item.get('notes') or [])}`",
                "",
            ]
        )
    lines.append("## Input Manifest")
    lines.append("")
    for item in input_manifest:
        lines.append(
            f"- {item.get('symbol')}: windows={item.get('closed_windows')} "
            f"sha256={item.get('csv_sha256')}"
        )
    lines.append("")
    return "\n".join(lines)


def _final_report(
    payload: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    data_audit: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
) -> str:
    best = acceptance.get("best_candidate") or {}
    lines = [
        "# Semiconductor Grid v2.7.1 Backtest",
        "",
        "## 1. 执行摘要",
        "",
        f"- 结论代码: `{payload.get('conclusion_code')}`",
        f"- 是否存在可验证优势: `{'是' if acceptance.get('passed') else '否'}`",
        f"- 最佳 Profile: `{best.get('market_group')}/{best.get('profile')}`",
        f"- 是否依赖零 Maker: `{bool(best.get('maker_dependent'))}`",
        f"- EXECUTION_STRESS 净收益: `{best.get('stress_total_pnl')}`",
        f"- Final OOS 净收益: `{best.get('final_oos_net_pnl')}`",
        f"- 是否允许进入测试网候选: `{'是' if payload.get('conclusion_code') == 'PASS_TESTNET_CANDIDATE' else '否'}`",
        "",
        "## 2. 数据可信度",
        "",
        f"- research_branch: `{run_manifest.get('research_branch')}`",
        f"- base_commit_sha: `{run_manifest.get('base_commit_sha')}`",
        f"- data_ok: `{data_audit.get('ok')}`",
        f"- total_unique_windows: `{payload.get('total_unique_windows')}`",
        f"- runs: `{payload.get('run_count')}`",
        f"- blocked: `{payload.get('blocked_count')}`",
        "",
        "## 3. 固定基准结果",
        "",
    ]
    for item in payload.get("profile_summary", []):
        lines.append(
            f"- {item.get('market_group')}/{item.get('profile')}/{item.get('scenario')}: "
            f"pnl={float(item.get('total_pnl', 0.0)):.6f} windows={item.get('unique_windows')} "
            f"pos_ratio={float(item.get('positive_ratio', 0.0)):.3f} "
            f"pf={float(item.get('profit_factor', 0.0)):.3f}"
        )
    lines.extend(["", "## 4. Profile 对比", ""])
    for assessment in payload.get("assessments", []):
        lines.append(
            f"- {assessment.get('market_group')}/{assessment.get('profile')}: "
            f"`{assessment.get('conclusion')}` ({assessment.get('reasons') or 'passed'})"
        )
    lines.extend(
        [
            "",
            "## 5. 成本与库存诊断",
            "",
            f"- best primary pnl: `{best.get('primary_total_pnl')}`",
            f"- hard fail reasons: `{', '.join(best.get('hard_fail_reasons') or []) or 'none'}`",
            "",
            "## 6. 执行压力结果",
            "",
            f"- PRIMARY_ZERO_MAKER: `{best.get('primary_total_pnl')}`",
            f"- EXECUTION_STRESS: `{best.get('stress_total_pnl')}`",
            f"- MAKER_PROMO_OFF: `{best.get('maker_off_total_pnl')}`",
            "",
            "## 7. Final OOS",
            "",
            f"- Development: `{best.get('development_net_pnl')}`",
            f"- Validation: `{best.get('validation_net_pnl')}`",
            f"- Final OOS: `{best.get('final_oos_net_pnl')}`",
            "",
            "## 8. 验收门槛",
            "",
        ]
    )
    for gate in acceptance.get("gates", []):
        lines.append(
            f"- {gate.get('gate')}: threshold={gate.get('threshold')} "
            f"actual={gate.get('actual')} `{gate.get('status')}`"
        )
    lines.extend(
        [
            "",
            "## 9. 最终结论代码",
            "",
            f"`{payload.get('conclusion_code')}`",
            "",
            "本轮没有稳健候选时，应停止参数搜索并保持自动开仓关闭。",
            "",
        ]
    )
    return "\n".join(lines)


def _gate(name: str, threshold: Any, actual: Any, passed: bool) -> dict[str, Any]:
    return {
        "gate": name,
        "threshold": threshold,
        "actual": actual,
        "status": "PASS" if passed else "FAIL",
    }


def _cvar(values: Sequence[float], tail: float = 0.05) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    count = max(1, int(math.ceil(len(ordered) * tail)))
    return statistics.fmean(ordered[:count])


def _closed_kind(
    previous_close: datetime,
    next_market_open: datetime,
    market_tz: ZoneInfo,
) -> WindowKind:
    prev_day = previous_close.astimezone(market_tz).date()
    next_day = next_market_open.astimezone(market_tz).date()
    day_gap = (next_day - prev_day).days
    if day_gap <= 1:
        return WindowKind.WEEKDAY_OVERNIGHT
    cursor = prev_day + timedelta(days=1)
    while cursor < next_day:
        if cursor.weekday() >= 5:
            return WindowKind.WEEKEND
        cursor += timedelta(days=1)
    return WindowKind.HOLIDAY


def _lower_bound(values: Sequence[int], target: int) -> int:
    low = 0
    high = len(values)
    while low < high:
        mid = (low + high) // 2
        if values[mid] < target:
            low = mid + 1
        else:
            high = mid
    return low


def _package_versions(names: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        import importlib.metadata as metadata
    except ImportError:  # pragma: no cover
        return result
    for name in names:
        try:
            result[name] = metadata.version(name)
        except Exception:
            result[name] = "unknown"
    return result


def _git_branch() -> str:
    try:
        completed = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        return (completed.stdout or "").strip()
    except Exception:
        return ""


def _git_head(ref: str = "HEAD") -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        return (completed.stdout or "").strip()
    except Exception:
        return ""


if __name__ == "__main__":
    main()
