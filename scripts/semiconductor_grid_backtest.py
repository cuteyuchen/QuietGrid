"""Run the pre-registered v2.7.2 semiconductor closed-market grid retest."""
from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
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
from strategy.backtest import (
    BacktestConfig,
    BacktestResult,
    run_grid_backtest,
    slice_funding_events_for_klines,
)
from strategy.cooldown import CooldownConfig, CooldownEvaluator
from strategy.grid_viability import GridViabilityConfig
from strategy.regime import RegimeConfig, RegimeDecision, RegimeEngine, RegimeWeights
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
DEFAULT_OUTPUT = Path("reports/semiconductor-grid-backtest-v2.7.2")
STRATEGY_VERSION = "semiconductor-grid-v2.7.2"
MINIMUM_DRAG_DENOMINATOR = 0.01


@dataclass(frozen=True)
class ClosedWindow:
    window_key: str
    market_group: str
    rows: tuple[dict[str, Any], ...]
    window_kind: str = "WEEKEND"
    calendar: str = ""
    previous_market_close: datetime | None = None
    observation_start: datetime | None = None
    observation_end: datetime | None = None
    trade_start: datetime | None = None
    last_trade_bar: datetime | None = None
    force_close_at: datetime | None = None
    next_reference_open: datetime | None = None
    complete: bool = True
    blocked_reason: str = ""
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


@dataclass(frozen=True)
class ControllerFaithfulResult:
    result: BacktestResult
    session_rows: tuple[dict[str, Any], ...]
    regrid_rows: tuple[dict[str, Any], ...]
    cooldown_count: int
    reentry_count: int


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
    observation_minutes: int = 180,
    window_key_prefix: str | None = None,
) -> list[ClosedWindow]:
    """Build weekend/holiday closed windows from calendar boundaries.

    Uses the registered boundary exactly:
      observation_end = previous_close + observation_minutes
      force_close_at = next_reference_open - force_close_minutes
      trading bars satisfy observation_end <= open_time < force_close_at

    ``minimum_trade_minutes`` is only an admission condition.  It never moves the
    terminal boundary.  Windows crossing the frozen data cutoff are returned as
    incomplete so callers can audit and exclude them.
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
    windows: list[ClosedWindow] = []
    data_cutoff_ms = max(int(row["close_time"]) for row in rows)
    data_cutoff = datetime.fromtimestamp(data_cutoff_ms / 1000, tz=UTC)

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

        force_close_at = next_reference_open - timedelta(minutes=force_close_minutes)
        observation_end = previous_close + timedelta(minutes=observation_minutes)
        remaining_trade_minutes = (
            force_close_at - observation_end
        ).total_seconds() / 60
        if remaining_trade_minutes < minimum_trade_minutes:
            continue

        start_ms = int(previous_close.timestamp() * 1000)
        end_ms = int(force_close_at.timestamp() * 1000)
        left = _lower_bound(open_times, start_ms)
        right = _lower_bound(open_times, end_ms)
        if right <= left:
            continue
        bucket = list(rows[left:right])
        complete = data_cutoff_ms >= end_ms - 1
        blocked_reason = "" if complete else "INCOMPLETE_WINDOW"
        last_trade_bar = (
            _row_dt(bucket[-1]) if bucket and _row_dt(bucket[-1]) >= observation_end else None
        )
        window_key = (
            f"{prefix}:{previous_close.isoformat()}:{next_reference_open.isoformat()}"
        )
        windows.append(
            ClosedWindow(
                window_key=window_key,
                market_group=market_group,
                rows=tuple(bucket),
                window_kind=kind.value,
                calendar=calendar_name,
                previous_market_close=previous_close,
                observation_start=previous_close,
                observation_end=observation_end,
                trade_start=observation_end,
                last_trade_bar=last_trade_bar,
                force_close_at=force_close_at,
                next_reference_open=next_reference_open,
                complete=complete,
                blocked_reason=blocked_reason,
            )
        )
    return windows


def assign_time_splits(
    windows: Sequence[ClosedWindow],
    *,
    forward_oos_start: datetime | None = None,
) -> list[ClosedWindow]:
    """Assign only wholly unseen windows to Forward OOS.

    ``forward_oos_start`` is the complete exposure cutoff, not merely a code
    commit time or the last successfully traded window.  A window that began
    on or before the cutoff was at least partially observable and remains
    exposed history even when its force-close boundary is later.
    """
    cutoff = None
    if forward_oos_start is not None:
        cutoff = (
            forward_oos_start.replace(tzinfo=UTC)
            if forward_oos_start.tzinfo is None
            else forward_oos_start.astimezone(UTC)
        )
    ordered = sorted(windows, key=lambda item: item.start_time)
    assigned: list[ClosedWindow] = []
    for window in ordered:
        split = "RESEARCH_VALIDATION_EXPOSED"
        if (
            cutoff is not None
            and window.complete
            and window.observation_start is not None
            and (
                window.observation_start.replace(tzinfo=UTC)
                if window.observation_start.tzinfo is None
                else window.observation_start.astimezone(UTC)
            )
            > cutoff
        ):
            split = "FORWARD_OOS"
        if not window.complete:
            split = "INCOMPLETE_WINDOW"
        assigned.append(
            ClosedWindow(
                window_key=window.window_key,
                market_group=window.market_group,
                rows=window.rows,
                window_kind=window.window_kind,
                calendar=window.calendar,
                previous_market_close=window.previous_market_close,
                observation_start=window.observation_start,
                observation_end=window.observation_end,
                trade_start=window.trade_start,
                last_trade_bar=window.last_trade_bar,
                force_close_at=window.force_close_at,
                next_reference_open=window.next_reference_open,
                complete=window.complete,
                blocked_reason=window.blocked_reason,
                split=split,
            )
        )
    return assigned


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="回测 SNDK/MU/SOXL/SKHYNIX 休市窗口密集网格 v2.7.2",
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
    parser.add_argument(
        "--v271-report-dir",
        default="reports/semiconductor-grid-backtest-v2.7.1",
    )
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
    regime_engine = RegimeEngine(_regime(raw.get("regime", {}) or {}))
    cooldown_raw = raw.get("cooldown", {}) or {}
    timing_raw = raw.get("timing", {}) or {}
    cooldown_evaluator = CooldownEvaluator(
        CooldownConfig(
            atr_period=int(cooldown_raw.get("atr_period", 14)),
            calm_window_minutes=int(
                cooldown_raw.get("calm_window_minutes", 30)
            ),
            atr_recovery_ratio=float(
                cooldown_raw.get("atr_recovery_ratio", 0.80)
            ),
            amplitude_multiplier=float(
                cooldown_raw.get("amplitude_multiplier", 2.0)
            ),
            min_calm_minutes=int(timing_raw.get("min_calm_minutes", 15)),
        )
    )
    rolling_regrid_bars = max(
        1,
        int((raw.get("grid", {}) or {}).get("rolling_regrid_seconds", 7200))
        // 60,
    )
    risk_raw = raw.get("risk", {}) or {}
    observe = int(cfg.get("observation_rows", 180))
    minimum = int(cfg.get("minimum_trade_rows", 120))
    capital = float(cfg.get("capital_per_symbol", 500))
    leverage = float(cfg.get("economic_leverage", 1))
    force_close_minutes = int(cfg.get("force_close_minutes", 120))
    minimum_trade_minutes = int(cfg.get("minimum_trade_minutes", 120))
    forward_oos_start = _git_commit_time("HEAD")

    data_dir = Path(args.data_dir)
    rules_path = Path(args.rules_json) if args.rules_json else data_dir / "exchange-rules.json"
    if not rules_path.exists() and not args.allow_missing_rules:
        raise FileNotFoundError(f"缺少规则快照: {rules_path}")
    rules = _load_rules(rules_path) if rules_path.exists() else {}

    rows_out: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    viability_rows: list[dict[str, Any]] = []
    regime_rows: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    regrid_rows: list[dict[str, Any]] = []
    window_manifest: list[dict[str, Any]] = []
    completed_window_manifest: list[dict[str, Any]] = []
    incomplete_window_manifest: list[dict[str, Any]] = []
    input_manifest: list[dict[str, Any]] = []
    data_audit_symbols: list[dict[str, Any]] = []
    all_windows: list[dict[str, Any]] = []
    hash_manifest: dict[str, Any] = {"files": {}}

    total_unique_windows = 0
    data_quality_failed = False
    data_quality_reasons: list[str] = []
    data_cutoffs: list[datetime] = []

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
        if bars:
            data_cutoffs.append(
                datetime.fromtimestamp(int(bars[-1]["close_time"]) / 1000, tz=UTC)
            )

        rule = rules.get(symbol, RuleSnapshot())
        if symbol not in rules and not args.allow_missing_rules:
            raise ValueError(f"规则快照缺少 {symbol}")
        rule_reasons = _rule_audit_reasons(rule)
        if rule_reasons:
            data_quality_failed = True
            data_quality_reasons.extend(f"{symbol}: {reason}" for reason in rule_reasons)

        windows = build_calendar_closed_windows(
            bars,
            market_group=sp.market_group,
            calendar_name=sp.calendar_name,
            market_timezone=sp.market_timezone,
            reference_open_time=sp.reference_open_time,
            force_close_minutes=force_close_minutes,
            minimum_trade_minutes=minimum_trade_minutes,
            observation_minutes=observe,
            window_key_prefix=sp.market_group,
        )
        windows = assign_time_splits(windows, forward_oos_start=forward_oos_start)
        total_unique_windows += sum(window.complete for window in windows)

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
                "closed_windows": sum(window.complete for window in windows),
                "incomplete_windows": sum(not window.complete for window in windows),
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
                "ok": audit["ok"] and funding_audit["ok"] and not rule_reasons,
                "rows": audit["rows"],
                "duplicate_count": audit["duplicate_count"],
                "gap_count": audit["gap_count"],
                "conflict_count": audit["conflict_count"],
                "open_time_start": audit["open_time_start"],
                "open_time_end": audit["open_time_end"],
                "funding_events": funding_audit["events"],
                "funding_coverage_ratio": funding_audit["coverage_ratio"],
                "funding_extra_event_count": funding_audit.get(
                    "extra_event_count",
                    0,
                ),
                "funding_strictly_increasing": funding_audit.get(
                    "strictly_increasing",
                    False,
                ),
                "funding_interval_hours_median": funding_audit["interval_hours_median"],
                "funding_ok": funding_audit["ok"],
                "notes": (
                    audit.get("notes", [])
                    + funding_audit.get("notes", [])
                    + rule_reasons
                ),
            }
        )
        if not (audit["ok"] and funding_audit["ok"]) or rule_reasons:
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
            manifest_row = _window_manifest_row(symbol, window)
            window_manifest.append(manifest_row)
            if window.complete:
                completed_window_manifest.append(manifest_row)
            else:
                incomplete_window_manifest.append(manifest_row)
            all_windows.append(
                {
                    "symbol": symbol,
                    "window_key": window.window_key,
                    "start": window.start_time,
                    "end": window.end_time,
                }
            )

            if not window.complete:
                blocked.append(
                    _blocked(
                        symbol,
                        window,
                        "ALL",
                        "INCOMPLETE_WINDOW",
                        (
                            f"data cutoff precedes force_close_at="
                            f"{window.force_close_at.isoformat() if window.force_close_at else ''}"
                        ),
                    )
                )
                continue

            observation_end_ms = int(window.observation_end.timestamp() * 1000)
            force_close_ms = int(window.force_close_at.timestamp() * 1000)
            obs = [
                row
                for row in window.rows
                if int(row["open_time"]) < observation_end_ms
            ]
            trade = [
                row
                for row in window.rows
                if observation_end_ms <= int(row["open_time"]) < force_close_ms
            ]
            if len(obs) < observe or len(trade) < minimum:
                blocked.append(
                    _blocked(
                        symbol,
                        window,
                        "ALL",
                        "INSUFFICIENT_WINDOW_ROWS",
                        f"observation={len(obs)}, trade={len(trade)}",
                    )
                )
                continue

            obs = list(obs[-observe:])
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
            projected = _projected_funding_pct(
                funding_rate,
                _row_close_dt(obs[-1]),
                window.force_close_at,
            )

            for profile in allowed_profiles:
                for scenario in scenarios:
                    expected_step = max(
                        profile.min_step_pct,
                        (
                            sp.dense_min_step_pct
                            if profile.max_grid_num > 20
                            else sp.normal_min_step_pct
                        ),
                    )
                    depth_proxy = statistics.fmean(
                        float(row.get("quote_volume") or 0.0)
                        for row in obs[-regime_engine.config.long_window :]
                    )
                    preliminary_regime = regime_engine.evaluate(
                        symbol,
                        obs,
                        spread_pct=sp.assumed_spread_pct,
                        depth_usdt=depth_proxy,
                        funding_rate=funding_rate,
                        data_age_seconds=0.0,
                        expected_step_pct=expected_step,
                        include_cost=False,
                        as_of=_row_close_dt(obs[-1]),
                    )
                    if not preliminary_regime.allowed:
                        regime_rows.append(
                            _regime_row(
                                symbol,
                                window,
                                profile.name,
                                scenario.name,
                                preliminary_regime,
                                "ENTRY_BLOCKED",
                                depth_proxy,
                            )
                        )
                        blocked.append(
                            _blocked(
                                symbol,
                                window,
                                profile.name,
                                "REGIME_BLOCKED",
                                preliminary_regime.verdict,
                                scenario.name,
                            )
                        )
                        continue
                    try:
                        candidate = build_semiconductor_grid_candidate(
                            symbol_profile=sp,
                            strategy_profile=profile,
                            klines=obs,
                            current_price=price,
                            funding_rate=funding_rate,
                            projected_funding_pct=projected,
                            maker_fee_rate=scenario.maker_fee_rate,
                            regime_score=preliminary_regime.grid_score,
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

                    final_regime = regime_engine.evaluate(
                        symbol,
                        obs,
                        spread_pct=sp.assumed_spread_pct,
                        depth_usdt=depth_proxy,
                        funding_rate=funding_rate,
                        data_age_seconds=0.0,
                        expected_step_pct=candidate.params.step_pct,
                        cost_floor_pct=candidate.params.cost_floor_pct,
                        cost_breakdown={
                            "risk_discount_pct": float(
                                candidate.params.economics.get(
                                    "risk_discount_pct",
                                    0.0,
                                )
                            )
                        },
                        as_of=_row_close_dt(obs[-1]),
                    )
                    regime_rows.append(
                        _regime_row(
                            symbol,
                            window,
                            profile.name,
                            scenario.name,
                            final_regime,
                            "PASSED" if final_regime.allowed else "ENTRY_BLOCKED",
                            depth_proxy,
                        )
                    )
                    if not final_regime.allowed:
                        blocked.append(
                            _blocked(
                                symbol,
                                window,
                                profile.name,
                                "REGIME_BLOCKED",
                                final_regime.verdict,
                                scenario.name,
                            )
                        )
                        continue
                    candidate = replace(
                        candidate,
                        params=replace(
                            candidate.params,
                            regime_score=final_regime.grid_score,
                        ),
                    )
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

                    def rebuild_candidate(
                        visible_rows: list[dict[str, Any]],
                    ) -> tuple[Any, RegimeDecision] | None:
                        if len(visible_rows) < observe:
                            return None
                        visible_obs = list(visible_rows[-observe:])
                        visible_price = float(visible_obs[-1]["close"])
                        visible_prior = [
                            item.funding_rate
                            for item in funding
                            if item.funding_time
                            <= int(visible_obs[-1]["close_time"])
                        ]
                        visible_funding_rate = (
                            visible_prior[-1] if visible_prior else 0.0
                        )
                        visible_projected = _projected_funding_pct(
                            visible_funding_rate,
                            _row_close_dt(visible_obs[-1]),
                            window.force_close_at,
                        )
                        visible_depth = statistics.fmean(
                            float(row.get("quote_volume") or 0.0)
                            for row in visible_obs[
                                -regime_engine.config.long_window :
                            ]
                        )
                        preliminary = regime_engine.evaluate(
                            symbol,
                            visible_obs,
                            spread_pct=sp.assumed_spread_pct,
                            depth_usdt=visible_depth,
                            funding_rate=visible_funding_rate,
                            data_age_seconds=0.0,
                            expected_step_pct=expected_step,
                            include_cost=False,
                            as_of=_row_close_dt(visible_obs[-1]),
                        )
                        if not preliminary.allowed:
                            return None
                        try:
                            rebuilt = build_semiconductor_grid_candidate(
                                symbol_profile=sp,
                                strategy_profile=profile,
                                klines=visible_obs,
                                current_price=visible_price,
                                funding_rate=visible_funding_rate,
                                projected_funding_pct=visible_projected,
                                maker_fee_rate=scenario.maker_fee_rate,
                                regime_score=preliminary.grid_score,
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
                        except (StrategyAdmissionError, ValueError):
                            return None
                        final = regime_engine.evaluate(
                            symbol,
                            visible_obs,
                            spread_pct=sp.assumed_spread_pct,
                            depth_usdt=visible_depth,
                            funding_rate=visible_funding_rate,
                            data_age_seconds=0.0,
                            expected_step_pct=rebuilt.params.step_pct,
                            cost_floor_pct=rebuilt.params.cost_floor_pct,
                            cost_breakdown={
                                "risk_discount_pct": float(
                                    rebuilt.params.economics.get(
                                        "risk_discount_pct",
                                        0.0,
                                    )
                                )
                            },
                            as_of=_row_close_dt(visible_obs[-1]),
                        )
                        if not final.allowed:
                            return None
                        return (
                            replace(
                                rebuilt,
                                params=replace(
                                    rebuilt.params,
                                    regime_score=final.grid_score,
                                ),
                            ),
                            final,
                        )

                    for seed in SEEDS:
                        backtest_config = BacktestConfig(
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
                            seed_slippage_bps=float(
                                cfg.get("seed_slippage_bps", 10)
                            ),
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
                        )
                        result = run_grid_backtest(
                            candidate.params,
                            trade,
                            current_price=price,
                            config=backtest_config,
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
                                final_regime,
                                "R0_STATIC_REPAIRED",
                            )
                        )
                        r1 = _run_controller_faithful(
                            symbol=symbol,
                            window=window,
                            profile=profile.name,
                            scenario=scenario.name,
                            seed=seed,
                            observation_rows=obs,
                            trade_rows=trade,
                            funding_events=events,
                            initial_candidate=candidate,
                            base_config=backtest_config,
                            rebuild_candidate=rebuild_candidate,
                            rolling_regrid_bars=rolling_regrid_bars,
                            cooldown=cooldown_evaluator,
                            max_window_loss=symbol_capital
                            * float(risk_raw.get("max_weekend_loss_pct", 0.015)),
                            max_stop_count=int(
                                risk_raw.get("max_window_stop_count", 3)
                            ),
                            max_consecutive_session_losses=int(
                                risk_raw.get(
                                    "max_consecutive_session_losses",
                                    2,
                                )
                            ),
                        )
                        r1_row = _result_row(
                            symbol,
                            window,
                            profile.name,
                            scenario.name,
                            seed,
                            symbol_capital,
                            candidate,
                            r1.result,
                            final_regime,
                            "R1_CONTROLLER_FAITHFUL",
                        )
                        r1_row["session_count"] = len(r1.session_rows)
                        r1_row["regrid_count"] = sum(
                            row["status"] == "COMPLETED"
                            for row in r1.regrid_rows
                        )
                        r1_row["cooldown_count"] = r1.cooldown_count
                        r1_row["reentry_count"] = r1.reentry_count
                        r1_row["regrid_cost"] = sum(
                            float(row["regrid_cost"]) for row in r1.regrid_rows
                        )
                        r1_row["window_active_ratio"] = min(
                            1.0,
                            sum(
                                int(row["duration_bars"])
                                for row in r1.session_rows
                            )
                            / max(1, len(trade)),
                        )
                        rows_out.append(r1_row)
                        session_rows.extend(r1.session_rows)
                        regrid_rows.extend(r1.regrid_rows)


    finished_at = datetime.now(UTC)
    summary = _aggregate(
        rows_out,
        ("engine_mode", "market_group", "profile", "scenario"),
    )
    split_summary = _aggregate(
        rows_out,
        ("engine_mode", "market_group", "profile", "scenario", "split"),
    )
    seed_summary = _aggregate(
        rows_out,
        ("engine_mode", "market_group", "profile", "scenario", "seed"),
    )
    symbol_summary = _aggregate(
        rows_out,
        ("engine_mode", "symbol", "profile", "scenario"),
    )
    month_summary = _aggregate(
        rows_out,
        ("engine_mode", "year_month", "profile", "scenario"),
    )
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
    r0_rows = [
        row for row in rows_out if row["engine_mode"] == "R0_STATIC_REPAIRED"
    ]
    r1_rows = [
        row for row in rows_out if row["engine_mode"] == "R1_CONTROLLER_FAITHFUL"
    ]
    exposed_rows = [
        row
        for row in rows_out
        if row["split"] == "RESEARCH_VALIDATION_EXPOSED"
    ]
    forward_rows = [row for row in rows_out if row["split"] == "FORWARD_OOS"]
    static_vs_controller = _static_vs_controller_summary(rows_out)
    v271_vs_r0 = _v271_vs_r0_summary(
        Path(args.v271_report_dir) / "symbol-breakdown.csv",
        symbol_summary,
    )
    exposed_summary = _aggregate(
        exposed_rows,
        ("engine_mode", "symbol", "profile", "scenario"),
    )
    forward_oos_ledger = (
        forward_rows
        if forward_rows
        else [
            {
                "record_type": "LEDGER_METADATA",
                "status": "INSUFFICIENT_FORWARD_OOS",
                "forward_oos_start": forward_oos_start.isoformat(),
                "complete_window_count": 0,
                "immutable_append_only": True,
            }
        ]
    )
    data_cutoff_utc = min(data_cutoffs) if data_cutoffs else None

    dependency_manifest = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "packages": _package_versions(
            ["pandas", "pandas_market_calendars", "PyYAML", "httpx", "numpy"]
        ),
        "generated_at": finished_at.isoformat(),
        "data_cutoff_utc": (
            data_cutoff_utc.isoformat() if data_cutoff_utc else None
        ),
    }
    run_manifest = {
        "schema_version": 1,
        "strategy_version": STRATEGY_VERSION,
        "base_branch": "master",
        "base_commit_sha": args.base_commit or _git_head("master"),
        "research_branch": _git_branch(),
        "run_started_at_utc": started_at.isoformat(),
        "run_finished_at_utc": finished_at.isoformat(),
        "data_cutoff_utc": (
            data_cutoff_utc.isoformat() if data_cutoff_utc else None
        ),
        "forward_oos_start": forward_oos_start.isoformat(),
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
        "incomplete_window_count": len(incomplete_window_manifest),
        "exposed_history_end": max(
            (
                str(row.get("force_close_at"))
                for row in completed_window_manifest
                if row.get("force_close_at")
            ),
            default=None,
        ),
        "run_count": len(rows_out),
        "blocked_count": len(blocked),
        "conclusion_code": conclusion,
        "worktree_dirty": bool(_git_status_short()),
    }
    if rules_path.exists():
        hash_manifest["files"][str(rules_path)] = _sha(rules_path)
    for required_path in (
        Path(args.config),
        Path(__file__),
        ROOT / "strategy" / "backtest.py",
        ROOT / "strategy" / "regime.py",
        ROOT / "core" / "scheduler.py",
        ROOT / "strategy" / "semiconductor_grid.py",
    ):
        hash_manifest["files"][str(required_path)] = _sha(required_path)
    hash_manifest["generated_at"] = finished_at.isoformat()
    hash_manifest["data_cutoff_utc"] = (
        data_cutoff_utc.isoformat() if data_cutoff_utc else None
    )

    data_audit_json = {
        "schema_version": 1,
        "generated_at": finished_at.isoformat(),
        "data_cutoff_utc": (
            data_cutoff_utc.isoformat() if data_cutoff_utc else None
        ),
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
        "v271_vs_r0": v271_vs_r0,
        "report_diagnostics": _report_diagnostics(
            rows_out,
            session_rows,
            regrid_rows,
        ),
    }

    _write_csv(output / "window-results.csv", rows_out)
    _write_csv(output / "window-results-r0.csv", r0_rows)
    _write_csv(output / "window-results-r1.csv", r1_rows)
    _write_csv(output / "window-breakdown.csv", rows_out)
    _write_csv(output / "blocked-windows.csv", blocked)
    _write_csv(output / "profile-summary.csv", summary)
    _write_csv(output / "profile-scenario-summary.csv", summary)
    _write_csv(output / "seed-breakdown.csv", seed_summary)
    _write_csv(output / "symbol-breakdown.csv", symbol_summary)
    _write_csv(output / "month-breakdown.csv", month_summary)
    _write_csv(output / "assessment.csv", assessments)
    _write_csv(output / "window-manifest.csv", window_manifest)
    _write_csv(output / "completed-window-manifest.csv", completed_window_manifest)
    _write_csv(output / "incomplete-window-manifest.csv", incomplete_window_manifest)
    _write_csv(output / "regime-breakdown.csv", regime_rows)
    _write_csv(output / "grid-viability-breakdown.csv", viability_rows)
    _write_csv(output / "session-breakdown.csv", session_rows)
    _write_csv(output / "regrid-breakdown.csv", regrid_rows)
    _write_csv(
        output / "pre-exit-inventory-breakdown.csv",
        _pre_exit_inventory_rows(rows_out),
    )
    _write_csv(output / "exit-attribution.csv", _exit_attribution_rows(rows_out))
    _write_csv(output / "inventory-breakdown.csv", inventory_rows)
    _write_csv(output / "cost-breakdown.csv", cost_rows)
    _write_csv(output / "seed-distribution.csv", seed_summary)
    _write_csv(
        output / "static-vs-controller-summary.csv",
        static_vs_controller,
    )
    _write_csv(output / "v2.7.1-vs-r0-summary.csv", v271_vs_r0)
    _write_csv(output / "exposed-validation-summary.csv", exposed_summary)
    _write_csv(output / "forward-oos-ledger.csv", forward_oos_ledger)
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
    _write_json(
        output / "repair-manifest.json",
        {
            "base_commit_sha": args.base_commit or _git_head("master"),
            "repair_commit_sha": _git_head(),
            "worktree_dirty": bool(_git_status_short()),
            "strategy_config_sha": _sha(Path(args.config)),
            "backtest_script_sha": _sha(Path(__file__)),
            "window_builder_sha": _sha(Path(__file__)),
            "backtest_engine_sha": _sha(ROOT / "strategy" / "backtest.py"),
            "regime_engine_sha": _sha(ROOT / "strategy" / "regime.py"),
            "scheduler_sha": _sha(ROOT / "core" / "scheduler.py"),
            "strategy_registry_sha": _sha(
                ROOT / "strategy" / "semiconductor_grid.py"
            ),
            "run_started_at_utc": started_at.isoformat(),
            "run_finished_at_utc": finished_at.isoformat(),
            "data_cutoff_utc": (
                data_cutoff_utc.isoformat() if data_cutoff_utc else None
            ),
        },
    )
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
    (output / "window-boundary-audit.md").write_text(
        _window_boundary_audit(completed_window_manifest),
        encoding="utf-8",
    )
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
    regime: RegimeDecision,
    engine_mode: str,
) -> dict[str, Any]:
    paired = float(result.paired_grid_pnl)
    drag = max(0.0, -float(result.pre_exit_unrealized_pnl))
    inventory_drag = drag / max(paired, MINIMUM_DRAG_DENOMINATOR)
    total_cost = float(result.fees_paid) + float(result.funding_paid)
    snap = candidate.viability.snapshot
    return {
        "symbol": symbol,
        "market_group": window.market_group,
        "calendar": window.calendar,
        "window_key": window.window_key,
        "window_kind": window.window_kind,
        "split": window.split,
        "engine_mode": engine_mode,
        "year_month": window.start_time.strftime("%Y-%m"),
        "window_start": window.start_time.isoformat(),
        "window_end": window.end_time.isoformat(),
        "previous_market_close": (
            window.previous_market_close.isoformat()
            if window.previous_market_close
            else ""
        ),
        "observation_end": (
            window.observation_end.isoformat() if window.observation_end else ""
        ),
        "trade_start": window.trade_start.isoformat() if window.trade_start else "",
        "last_trade_bar": (
            window.last_trade_bar.isoformat() if window.last_trade_bar else ""
        ),
        "force_close_at": (
            window.force_close_at.isoformat() if window.force_close_at else ""
        ),
        "next_reference_open": (
            window.next_reference_open.isoformat()
            if window.next_reference_open
            else ""
        ),
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
        "paired_grid_pnl": result.paired_grid_pnl,
        "inventory_realized_pnl": result.inventory_realized_pnl,
        "fees_paid": result.fees_paid,
        "maker_fees": result.maker_fees,
        "taker_fees": result.taker_fees,
        "funding_paid": result.funding_paid,
        "funding_received": result.funding_received,
        "seed_cost": result.seed_cost,
        "stop_exit_cost": result.stop_exit_cost,
        "stop_exit_slippage_cost": result.stop_exit_slippage_cost,
        "force_exit_fees": result.force_exit_fees,
        "force_exit_slippage_cost": result.force_exit_slippage_cost,
        "force_exit_cost": result.force_exit_cost,
        "regrid_cost": 0.0,
        "seed_fee": float(getattr(result, "seed_fee", 0.0) or 0.0),
        "total_cost": total_cost,
        "unrealized_pnl": result.unrealized_pnl,
        "pre_exit_position_qty": result.pre_exit_position_qty,
        "pre_exit_inventory_notional": result.pre_exit_inventory_notional,
        "pre_exit_unrealized_pnl": result.pre_exit_unrealized_pnl,
        "pre_exit_mark_price": result.pre_exit_mark_price,
        "pre_exit_timestamp": result.pre_exit_timestamp,
        "peak_negative_unrealized_pnl": result.peak_negative_unrealized_pnl,
        "inventory_drag": drag,
        "inventory_drag_ratio": inventory_drag,
        "peak_inventory_drag_ratio": result.peak_inventory_drag_ratio,
        "max_drawdown": result.max_drawdown,
        "max_drawdown_pct": result.max_drawdown / max(capital, 1e-12),
        "pair_completion_count": result.pair_completion_count,
        "attempted_fill_count": result.attempted_fill_count,
        "rejected_fill_count": result.rejected_fill_count,
        "accepted_fill_count": result.accepted_fill_count,
        "fills": len(result.fills),
        "max_inventory_utilization": result.max_inventory_utilization,
        "mean_inventory_utilization": result.mean_inventory_utilization,
        "max_unpaired_lots": result.max_unpaired_lots,
        "max_unpaired_lot_age": result.max_unpaired_lot_age_bars,
        "force_close_inventory_notional": result.pre_exit_inventory_notional,
        "regime_score": regime.grid_score,
        "regime_state": regime.state,
        "regime_component_scores": json.dumps(
            regime.component_scores,
            ensure_ascii=False,
            sort_keys=True,
        ),
        "hard_limit_reasons": "|".join(regime.hard_blocks),
        "gate_pass": candidate.viability.allowed,
        "gate_block_reasons": "|".join(candidate.viability.reasons),
        "session_count": 1,
        "regrid_count": 0,
        "cooldown_count": 0,
        "reentry_count": 0,
        "session_duration": len(result.equity_curve),
        "window_active_ratio": 1.0,
        "force_close_count": result.force_close_count,
        "stopped_reason": result.stopped_reason or "",
        "capital": capital,
    }


def _run_controller_faithful(
    *,
    symbol: str,
    window: ClosedWindow,
    profile: str,
    scenario: str,
    seed: int,
    observation_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    funding_events: list[FundingEvent],
    initial_candidate: Any,
    base_config: BacktestConfig,
    rebuild_candidate: Callable[
        [list[dict[str, Any]]],
        tuple[Any, RegimeDecision] | None,
    ],
    rolling_regrid_bars: int,
    cooldown: CooldownEvaluator,
    max_window_loss: float,
    max_stop_count: int,
    max_consecutive_session_losses: int,
) -> ControllerFaithfulResult:
    cursor = 0
    candidate = initial_candidate
    session_results: list[BacktestResult] = []
    session_rows: list[dict[str, Any]] = []
    regrid_rows: list[dict[str, Any]] = []
    cooldown_count = 0
    reentry_count = 0
    window_pnl = 0.0
    stop_count = 0
    consecutive_losses = 0

    def run_segment(
        params: Any,
        rows: list[dict[str, Any]],
        *,
        force_close: bool,
    ) -> BacktestResult:
        return run_grid_backtest(
            params,
            rows,
            current_price=float(rows[0]["open"]),
            config=replace(base_config, force_close_at_end=force_close),
            funding_events=slice_funding_events_for_klines(funding_events, rows),
        )

    while cursor < len(trade_rows):
        segment_start = cursor
        active_candidate = candidate

        while (
            rolling_regrid_bars > 0
            and segment_start + rolling_regrid_bars < len(trade_rows)
        ):
            evaluation_end = segment_start + rolling_regrid_bars
            evaluation_rows = trade_rows[segment_start:evaluation_end]
            probe = run_segment(
                active_candidate.params,
                evaluation_rows,
                force_close=False,
            )
            has_exposure = (
                probe.accepted_fill_count > 0
                or probe.seed_qty > 0
                or abs(probe.net_position_qty) > 1e-12
            )
            regrid_at = _row_dt(trade_rows[evaluation_end])
            if has_exposure:
                regrid_rows.append(
                    {
                        "symbol": symbol,
                        "window_key": window.window_key,
                        "profile": profile,
                        "scenario": scenario,
                        "seed": seed,
                        "regrid_timestamp": regrid_at.isoformat(),
                        "old_center": active_candidate.params.center,
                        "new_center": active_candidate.params.center,
                        "old_step": active_candidate.params.step_pct,
                        "new_step": active_candidate.params.step_pct,
                        "old_grid_num": active_candidate.params.grid_num,
                        "new_grid_num": active_candidate.params.grid_num,
                        "reason": "session_has_fills_or_exposure",
                        "status": "SKIPPED",
                        "canceled_order_count": 0,
                        "inventory_before": probe.net_position_qty,
                        "inventory_after": probe.net_position_qty,
                        "regrid_cost": 0.0,
                    }
                )
                break

            visible = observation_rows + trade_rows[:evaluation_end]
            rebuilt = rebuild_candidate(visible[-len(observation_rows) :])
            if rebuilt is None:
                regrid_rows.append(
                    {
                        "symbol": symbol,
                        "window_key": window.window_key,
                        "profile": profile,
                        "scenario": scenario,
                        "seed": seed,
                        "regrid_timestamp": regrid_at.isoformat(),
                        "old_center": active_candidate.params.center,
                        "new_center": active_candidate.params.center,
                        "old_step": active_candidate.params.step_pct,
                        "new_step": active_candidate.params.step_pct,
                        "old_grid_num": active_candidate.params.grid_num,
                        "new_grid_num": active_candidate.params.grid_num,
                        "reason": "recalculation_admission_blocked",
                        "status": "FAILED_KEEP_OLD_GRID",
                        "canceled_order_count": 0,
                        "inventory_before": 0.0,
                        "inventory_after": 0.0,
                        "regrid_cost": 0.0,
                    }
                )
                break

            rebuilt_candidate, _rebuilt_regime = rebuilt
            regrid_rows.append(
                {
                    "symbol": symbol,
                    "window_key": window.window_key,
                    "profile": profile,
                    "scenario": scenario,
                    "seed": seed,
                    "regrid_timestamp": regrid_at.isoformat(),
                    "old_center": active_candidate.params.center,
                    "new_center": rebuilt_candidate.params.center,
                    "old_step": active_candidate.params.step_pct,
                    "new_step": rebuilt_candidate.params.step_pct,
                    "old_grid_num": active_candidate.params.grid_num,
                    "new_grid_num": rebuilt_candidate.params.grid_num,
                    "reason": "rolling_regrid_due_no_fills_no_exposure",
                    "status": "COMPLETED",
                    "canceled_order_count": probe.open_order_count,
                    "inventory_before": 0.0,
                    "inventory_after": 0.0,
                    "regrid_cost": 0.0,
                }
            )
            active_candidate = rebuilt_candidate
            segment_start = evaluation_end

        remaining = trade_rows[segment_start:]
        result = run_segment(active_candidate.params, remaining, force_close=True)
        session_results.append(result)
        session_number = len(session_results)
        consumed = len(remaining)
        if result.stopped_at_index is not None:
            consumed = min(
                len(remaining),
                max(1, int(result.stopped_at_index) + 1),
            )
        session_end_index = min(
            len(trade_rows) - 1,
            segment_start + consumed - 1,
        )
        session_rows.append(
            {
                "symbol": symbol,
                "window_key": window.window_key,
                "profile": profile,
                "scenario": scenario,
                "seed": seed,
                "session_index": session_number,
                "session_start": _row_dt(trade_rows[segment_start]).isoformat(),
                "session_end": _row_dt(trade_rows[session_end_index]).isoformat(),
                "state_path": (
                    "OBSERVING>RUNNING>FORCE_CLOSING>CLOSED"
                    if result.stopped_reason == "window_force_close"
                    else "REOBSERVING>RUNNING>DEFENSIVE>COOLDOWN"
                    if session_number > 1
                    else "OBSERVING>RUNNING>DEFENSIVE>COOLDOWN"
                ),
                "stopped_reason": result.stopped_reason or "",
                "stopped_at_price": result.stopped_at_price,
                "grid_lower": active_candidate.params.lower,
                "grid_upper": active_candidate.params.upper,
                "baseline_atr": active_candidate.params.baseline_atr,
                "session_pnl": result.total_pnl,
                "paired_grid_pnl": result.paired_grid_pnl,
                "inventory_realized_pnl": result.inventory_realized_pnl,
                "pre_exit_position_qty": result.pre_exit_position_qty,
                "pre_exit_inventory_notional": result.pre_exit_inventory_notional,
                "pre_exit_unrealized_pnl": result.pre_exit_unrealized_pnl,
                "force_exit_cost": result.force_exit_cost,
                "stop_exit_cost": result.stop_exit_cost,
                "accepted_fill_count": result.accepted_fill_count,
                "duration_bars": consumed,
            }
        )
        cursor = segment_start + consumed
        window_pnl += result.total_pnl
        if result.total_pnl < 0:
            consecutive_losses += 1
        else:
            consecutive_losses = 0
        if result.stopped_reason not in {None, "window_force_close"}:
            stop_count += 1

        if result.stopped_reason == "window_force_close" or cursor >= len(trade_rows):
            break
        if max_window_loss > 0 and window_pnl <= -max_window_loss:
            break
        if max_stop_count > 0 and stop_count >= max_stop_count:
            break
        if (
            max_consecutive_session_losses > 0
            and consecutive_losses >= max_consecutive_session_losses
        ):
            break

        cooldown_count += 1
        cooldown_started_at = _row_dt(trade_rows[min(cursor, len(trade_rows) - 1)])
        reentered = False
        for probe_index in range(cursor, len(trade_rows)):
            visible = observation_rows + trade_rows[: probe_index + 1]
            decision = cooldown.evaluate(
                visible,
                baseline_atr=active_candidate.params.baseline_atr,
                min_step_pct=active_candidate.params.step_pct,
                cooldown_started_at=cooldown_started_at,
                now=_row_close_dt(trade_rows[probe_index]),
            )
            remaining_bars = len(trade_rows) - (probe_index + 1)
            if not decision.can_reobserve:
                continue
            if remaining_bars < 120:
                cursor = len(trade_rows)
                break
            rebuilt = rebuild_candidate(visible[-len(observation_rows) :])
            if rebuilt is None:
                cursor = len(trade_rows)
                break
            candidate, _reentry_regime = rebuilt
            cursor = probe_index + 1
            reentry_count += 1
            reentered = True
            break
        if not reentered:
            break

    return ControllerFaithfulResult(
        result=_combine_backtest_results(symbol, session_results),
        session_rows=tuple(session_rows),
        regrid_rows=tuple(regrid_rows),
        cooldown_count=cooldown_count,
        reentry_count=reentry_count,
    )


def _combine_backtest_results(
    symbol: str,
    results: Sequence[BacktestResult],
) -> BacktestResult:
    if not results:
        raise ValueError("R1 至少需要一个会话结果")
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for result in results:
        session_base = cumulative
        for point in result.equity_curve:
            window_equity = session_base + point.equity
            peak = max(peak, window_equity)
            max_drawdown = max(max_drawdown, peak - window_equity)
        cumulative = session_base + result.total_pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    drag = sum(max(0.0, -item.pre_exit_unrealized_pnl) for item in results)
    worst_exit = min(results, key=lambda item: item.pre_exit_unrealized_pnl)
    duration_weight = sum(max(1, len(item.equity_curve)) for item in results)
    return BacktestResult(
        symbol=symbol,
        fills=[fill for item in results for fill in item.fills],
        equity_curve=[point for item in results for point in item.equity_curve],
        gross_grid_pnl=sum(item.gross_grid_pnl for item in results),
        fees_paid=sum(item.fees_paid for item in results),
        realized_pnl=sum(item.total_pnl for item in results),
        unrealized_pnl=0.0,
        total_pnl=sum(item.total_pnl for item in results),
        max_equity=peak,
        max_drawdown=max_drawdown,
        open_order_count=0,
        net_position_qty=0.0,
        stopped_reason=results[-1].stopped_reason,
        stopped_at_index=None,
        stopped_at_price=results[-1].stopped_at_price,
        last_price=results[-1].last_price,
        funding_paid=sum(item.funding_paid for item in results),
        stop_exit_cost=sum(item.stop_exit_cost for item in results),
        stop_exit_pnl=sum(item.stop_exit_pnl for item in results),
        attempted_fill_count=sum(item.attempted_fill_count for item in results),
        rejected_fill_count=sum(item.rejected_fill_count for item in results),
        pair_completion_count=sum(item.pair_completion_count for item in results),
        max_inventory_utilization=max(
            item.max_inventory_utilization for item in results
        ),
        direction_mode=results[0].direction_mode,
        seed_qty=sum(item.seed_qty for item in results),
        seed_fee=sum(item.seed_fee for item in results),
        defensive_entry_count=sum(item.defensive_entry_count for item in results),
        inventory_critical_exit_count=sum(
            item.inventory_critical_exit_count for item in results
        ),
        max_unpaired_lot_age_bars=max(
            item.max_unpaired_lot_age_bars for item in results
        ),
        paired_grid_pnl=sum(item.paired_grid_pnl for item in results),
        inventory_realized_pnl=sum(
            item.inventory_realized_pnl for item in results
        ),
        maker_fees=sum(item.maker_fees for item in results),
        taker_fees=sum(item.taker_fees for item in results),
        funding_received=sum(item.funding_received for item in results),
        seed_cost=sum(item.seed_cost for item in results),
        stop_exit_slippage_cost=sum(
            item.stop_exit_slippage_cost for item in results
        ),
        force_exit_fees=sum(item.force_exit_fees for item in results),
        force_exit_slippage_cost=sum(
            item.force_exit_slippage_cost for item in results
        ),
        force_exit_cost=sum(item.force_exit_cost for item in results),
        pre_exit_position_qty=worst_exit.pre_exit_position_qty,
        pre_exit_inventory_notional=max(
            item.pre_exit_inventory_notional for item in results
        ),
        pre_exit_unrealized_pnl=-drag,
        pre_exit_mark_price=worst_exit.pre_exit_mark_price,
        pre_exit_timestamp=worst_exit.pre_exit_timestamp,
        peak_negative_unrealized_pnl=max(
            item.peak_negative_unrealized_pnl for item in results
        ),
        peak_inventory_drag_ratio=max(
            item.peak_inventory_drag_ratio for item in results
        ),
        mean_inventory_utilization=sum(
            item.mean_inventory_utilization * max(1, len(item.equity_curve))
            for item in results
        )
        / duration_weight,
        max_unpaired_lots=max(item.max_unpaired_lots for item in results),
        accepted_fill_count=sum(item.accepted_fill_count for item in results),
        force_close_count=sum(item.force_close_count for item in results),
        take_profit_count=sum(item.take_profit_count for item in results),
        profit_protection_suppress_count=sum(
            item.profit_protection_suppress_count for item in results
        ),
        profit_protection_reduce_count=sum(
            item.profit_protection_reduce_count for item in results
        ),
        profit_protection_close_count=sum(
            item.profit_protection_close_count for item in results
        ),
    )


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


def _legacy_assess_profiles_v271(
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


def _legacy_build_acceptance_payload_v271(
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


def assess_profiles(
    summary: list[dict[str, Any]],
    acceptance: Mapping[str, Any],
) -> list[dict[str, Any]]:
    limits = {
        "minimum_unique_windows": int(acceptance.get("minimum_unique_windows", 8)),
        "minimum_positive_ratio": float(
            acceptance.get("minimum_positive_ratio", 0.55)
        ),
        "minimum_profit_factor": float(
            acceptance.get("minimum_profit_factor", 1.05)
        ),
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
        (
            item.get("engine_mode", "R0_STATIC_REPAIRED"),
            item["market_group"],
            item["profile"],
            item["scenario"],
        ): item
        for item in summary
    }
    combos = sorted(
        {
            (
                item.get("engine_mode", "R0_STATIC_REPAIRED"),
                item["market_group"],
                item["profile"],
            )
            for item in summary
        }
    )
    output: list[dict[str, Any]] = []
    for engine_mode, market, profile in combos:
        primary = indexed.get(
            (engine_mode, market, profile, "PRIMARY_ZERO_MAKER")
        )
        stress = indexed.get(
            (engine_mode, market, profile, "EXECUTION_STRESS")
        )
        reasons: list[str] = []
        if primary is None:
            reasons.append("missing_primary")
        if stress is None:
            reasons.append("missing_execution_stress")
        if primary is not None:
            checks = (
                (
                    int(primary["unique_windows"])
                    >= limits["minimum_unique_windows"],
                    "insufficient_windows",
                ),
                (
                    float(primary["total_pnl"]) > 0,
                    "primary_not_positive",
                ),
                (
                    float(primary["positive_ratio"])
                    >= limits["minimum_positive_ratio"],
                    "low_positive_ratio",
                ),
                (
                    float(primary["profit_factor"])
                    >= limits["minimum_profit_factor"],
                    "low_profit_factor",
                ),
                (
                    float(primary["max_drawdown_pct"])
                    <= limits["maximum_drawdown_pct_of_capital"],
                    "drawdown_too_high",
                ),
                (
                    float(primary["mean_inventory_drag_ratio"])
                    <= limits["maximum_mean_inventory_drag_ratio"],
                    "inventory_drag_too_high",
                ),
                (
                    float(primary["best_window_concentration"])
                    <= limits["maximum_best_window_concentration"],
                    "window_concentration_too_high",
                ),
            )
            reasons.extend(reason for passed, reason in checks if not passed)
        if stress is not None and float(stress["total_pnl"]) <= 0:
            reasons.append("execution_stress_not_positive")
        output.append(
            {
                "engine_mode": engine_mode,
                "market_group": market,
                "profile": profile,
                "passed": not reasons,
                "conclusion": (
                    "RESEARCH_CANDIDATE"
                    if not reasons
                    else "REJECT"
                ),
                "reasons": ";".join(reasons),
            }
        )
    return output


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
    del split_summary
    limits = {
        "minimum_unique_windows": int(acceptance.get("minimum_unique_windows", 8)),
        "minimum_positive_ratio": float(
            acceptance.get("minimum_positive_ratio", 0.55)
        ),
        "minimum_profit_factor": float(
            acceptance.get("minimum_profit_factor", 1.05)
        ),
        "maximum_drawdown_pct_of_capital": float(
            acceptance.get("maximum_drawdown_pct_of_capital", 0.05)
        ),
        "maximum_mean_inventory_drag_ratio": float(
            acceptance.get("maximum_mean_inventory_drag_ratio", 0.35)
        ),
        "maximum_best_window_concentration": float(
            acceptance.get("maximum_best_window_concentration", 0.35)
        ),
        "minimum_positive_seed_count": 4,
        "minimum_forward_oos_windows": 8,
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
            "candidates": [],
        }
    if total_unique_windows < limits["minimum_unique_windows"]:
        return {
            "conclusion_code": "FAIL_INSUFFICIENT_DATA",
            "passed": False,
            "limits": limits,
            "gates": [
                _gate(
                    "minimum_unique_windows",
                    limits["minimum_unique_windows"],
                    total_unique_windows,
                    False,
                )
            ],
            "best_candidate": None,
            "reasons": [f"total_unique_windows={total_unique_windows}"],
            "assessments": assessments,
            "candidates": [],
        }

    summary_index = {
        (
            item.get("engine_mode"),
            item.get("market_group"),
            item.get("profile"),
            item.get("scenario"),
        ): item
        for item in summary
    }
    seed_index: dict[tuple[Any, Any, Any, Any], list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for item in seed_summary:
        seed_index[
            (
                item.get("engine_mode"),
                item.get("market_group"),
                item.get("profile"),
                item.get("scenario"),
            )
        ].append(item)
    forward_rows = [
        row for row in rows_out if row.get("split") == "FORWARD_OOS"
    ]
    forward_summary = _aggregate(
        forward_rows,
        ("engine_mode", "market_group", "profile", "scenario"),
    )
    forward_index = {
        (
            item.get("engine_mode"),
            item.get("market_group"),
            item.get("profile"),
            item.get("scenario"),
        ): item
        for item in forward_summary
    }

    candidates: list[dict[str, Any]] = []
    for assessment in assessments:
        engine_mode = assessment["engine_mode"]
        market = assessment["market_group"]
        profile = assessment["profile"]
        primary = summary_index.get(
            (engine_mode, market, profile, "PRIMARY_ZERO_MAKER")
        )
        stress = summary_index.get(
            (engine_mode, market, profile, "EXECUTION_STRESS")
        )
        maker_off = summary_index.get(
            (engine_mode, market, profile, "MAKER_PROMO_OFF")
        )
        if primary is None or stress is None:
            continue
        positive_seeds = sum(
            float(item.get("total_pnl", 0.0)) > 0
            for item in seed_index.get(
                (engine_mode, market, profile, "PRIMARY_ZERO_MAKER"),
                [],
            )
        )
        gates = [
            _gate(
                "minimum_unique_windows",
                limits["minimum_unique_windows"],
                int(primary["unique_windows"]),
                int(primary["unique_windows"])
                >= limits["minimum_unique_windows"],
            ),
            _gate(
                "positive_window_ratio",
                limits["minimum_positive_ratio"],
                float(primary["positive_ratio"]),
                float(primary["positive_ratio"])
                >= limits["minimum_positive_ratio"],
            ),
            _gate(
                "profit_factor",
                limits["minimum_profit_factor"],
                float(primary["profit_factor"]),
                float(primary["profit_factor"])
                >= limits["minimum_profit_factor"],
            ),
            _gate(
                "max_drawdown_pct",
                limits["maximum_drawdown_pct_of_capital"],
                float(primary["max_drawdown_pct"]),
                float(primary["max_drawdown_pct"])
                <= limits["maximum_drawdown_pct_of_capital"],
            ),
            _gate(
                "mean_inventory_drag_ratio",
                limits["maximum_mean_inventory_drag_ratio"],
                float(primary["mean_inventory_drag_ratio"]),
                float(primary["mean_inventory_drag_ratio"])
                <= limits["maximum_mean_inventory_drag_ratio"],
            ),
            _gate(
                "best_market_window_concentration",
                limits["maximum_best_window_concentration"],
                float(primary["best_window_concentration"]),
                float(primary["best_window_concentration"])
                <= limits["maximum_best_window_concentration"],
            ),
            _gate(
                "PRIMARY_ZERO_MAKER_net_pnl",
                0.0,
                float(primary["total_pnl"]),
                float(primary["total_pnl"]) > 0,
            ),
            _gate(
                "EXECUTION_STRESS_net_pnl",
                0.0,
                float(stress["total_pnl"]),
                float(stress["total_pnl"]) > 0,
            ),
            _gate(
                "positive_seed_count",
                limits["minimum_positive_seed_count"],
                positive_seeds,
                positive_seeds >= limits["minimum_positive_seed_count"],
            ),
        ]
        exposed_passed = all(gate["status"] == "PASS" for gate in gates)
        maker_dependent = (
            maker_off is not None and float(maker_off["total_pnl"]) <= 0
        )
        forward_primary = forward_index.get(
            (engine_mode, market, profile, "PRIMARY_ZERO_MAKER")
        )
        forward_stress = forward_index.get(
            (engine_mode, market, profile, "EXECUTION_STRESS")
        )
        forward_windows = (
            int(forward_primary["unique_windows"])
            if forward_primary is not None
            else 0
        )
        forward_status = (
            "INSUFFICIENT_FORWARD_OOS"
            if forward_windows < limits["minimum_forward_oos_windows"]
            else "READY_FOR_GATE"
        )
        candidates.append(
            {
                "engine_mode": engine_mode,
                "market_group": market,
                "profile": profile,
                "exposed_passed": exposed_passed,
                "maker_dependent": maker_dependent,
                "gates": gates,
                "hard_fail_reasons": [
                    gate["gate"]
                    for gate in gates
                    if gate["status"] == "FAIL"
                ],
                "primary_total_pnl": float(primary["total_pnl"]),
                "stress_total_pnl": float(stress["total_pnl"]),
                "maker_off_total_pnl": (
                    float(maker_off["total_pnl"])
                    if maker_off is not None
                    else None
                ),
                "forward_oos_status": forward_status,
                "forward_oos_windows": forward_windows,
                "forward_oos_net_pnl": (
                    float(forward_primary["total_pnl"])
                    if forward_primary is not None
                    else None
                ),
                "forward_oos_stress_net_pnl": (
                    float(forward_stress["total_pnl"])
                    if forward_stress is not None
                    else None
                ),
            }
        )

    r1_candidates = [
        item
        for item in candidates
        if item["engine_mode"] == "R1_CONTROLLER_FAITHFUL"
    ]
    passed = [item for item in r1_candidates if item["exposed_passed"]]
    ranked = sorted(
        passed or r1_candidates,
        key=lambda item: item["primary_total_pnl"],
        reverse=True,
    )
    best = ranked[0] if ranked else None
    if best is None:
        conclusion = "FAIL_NO_ROBUST_EDGE"
    elif not best["exposed_passed"]:
        if "EXECUTION_STRESS_net_pnl" in best["hard_fail_reasons"]:
            conclusion = "FAIL_EXECUTION_STRESS"
        elif "mean_inventory_drag_ratio" in best["hard_fail_reasons"]:
            conclusion = "FAIL_INVENTORY_TAIL"
        else:
            conclusion = "FAIL_NO_ROBUST_EDGE"
    elif best["maker_dependent"]:
        conclusion = "PASS_RESEARCH_ONLY_MAKER_DEPENDENT"
    elif best["forward_oos_status"] == "INSUFFICIENT_FORWARD_OOS":
        conclusion = "RESEARCH_CANDIDATE_AWAITING_FORWARD_OOS"
    else:
        forward_passed = (
            float(best.get("forward_oos_net_pnl") or 0.0) > 0
            and float(best.get("forward_oos_stress_net_pnl") or 0.0) > 0
        )
        conclusion = (
            "PASS_TESTNET_CANDIDATE"
            if forward_passed
            else "FAIL_NO_ROBUST_EDGE"
        )
    return {
        "conclusion_code": conclusion,
        "passed": conclusion == "PASS_TESTNET_CANDIDATE",
        "limits": limits,
        "gates": best["gates"] if best else [],
        "best_candidate": best,
        "reasons": best["hard_fail_reasons"] if best else ["no_r1_candidate"],
        "assessments": assessments,
        "candidates": candidates,
        "forward_oos_status": (
            best["forward_oos_status"]
            if best
            else "INSUFFICIENT_FORWARD_OOS"
        ),
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


def _regime(raw: Mapping[str, Any]) -> RegimeConfig:
    hard_limits = raw.get("hard_limits", {}) or {}
    weights = raw.get("weights", {}) or {}
    return RegimeConfig(
        short_window=int(raw.get("short_window", 15)),
        long_window=int(raw.get("long_window", 60)),
        enter_threshold=float(raw.get("enter_threshold", 75)),
        stay_threshold=float(raw.get("stay_threshold", 65)),
        max_data_age_seconds=float(raw.get("max_data_age_seconds", 90)),
        max_spread_pct=float(hard_limits.get("max_spread_pct", 0.001)),
        max_vol_expansion_ratio=float(
            hard_limits.get("max_vol_expansion_ratio", 2.5)
        ),
        min_depth_usdt=float(hard_limits.get("min_depth_usdt", 10_000)),
        soft_breach_limit=int(raw.get("soft_breach_limit", 3)),
        trend_filter_enabled=bool(raw.get("trend_filter_enabled", True)),
        entry_max_directional_efficiency=float(
            raw.get("entry_max_directional_efficiency", 0.55)
        ),
        running_max_directional_efficiency=float(
            raw.get("running_max_directional_efficiency", 0.70)
        ),
        event_source_available=bool(raw.get("event_source_available", False)),
        weights=RegimeWeights(
            volatility=float(weights.get("volatility", 0.25)),
            trend=float(weights.get("trend", 0.25)),
            liquidity=float(weights.get("liquidity", 0.20)),
            mean_reversion=float(weights.get("mean_reversion", 0.15)),
            cost=float(weights.get("cost", 0.10)),
            event=float(weights.get("event", 0.05)),
        ),
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
    if any(
        later["open_time"] <= earlier["open_time"]
        for earlier, later in zip(rows, rows[1:])
    ):
        raise ValueError(f"{path} 时间不是严格递增")
    duplicate_count = len(rows) - len({item["open_time"] for item in rows})
    if duplicate_count:
        raise ValueError(f"{path} 存在重复时间")
    gap_count = 0
    conflict_count = 0
    notes: list[str] = []
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    for index, item in enumerate(rows):
        if (
            item["close_time"] <= item["open_time"]
            or item["high"] < max(item["open"], item["close"])
            or item["low"] > min(item["open"], item["close"])
            or item["low"] <= 0
        ):
            conflict_count += 1
            raise ValueError(f"{path} 第 {index + 1} 行非法")
        if item["close_time"] > now_ms:
            raise ValueError(f"{path} 包含未来时间")
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
    events = list(
        (
            FundingEvent(
                int(item["funding_time"]),
                float(item["funding_rate"]),
                float(item["mark_price"])
                if item.get("mark_price") not in (None, "")
                else None,
            )
            for item in records or []
        )
    )
    if any(
        later.funding_time <= earlier.funding_time
        for earlier, later in zip(events, events[1:])
    ):
        raise ValueError(f"{path} Funding 时间不是严格递增")
    return events


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
    raw_coverage_ratio = len(covered) / expected if expected else 0.0
    coverage_ratio = min(1.0, raw_coverage_ratio)
    extra_event_count = max(0, len(covered) - math.ceil(expected))
    strictly_increasing = all(
        later.funding_time > earlier.funding_time
        for earlier, later in zip(covered, covered[1:])
    )
    ok = bool(covered) and coverage_ratio >= 0.8
    if not strictly_increasing:
        ok = False
        notes.append("funding_time_not_strictly_increasing")
    if not ok:
        notes.append(
            f"funding_coverage_ratio={coverage_ratio:.3f} events={len(covered)}"
        )
    return {
        "ok": ok,
        "events": len(covered),
        "coverage_ratio": coverage_ratio,
        "raw_coverage_ratio": raw_coverage_ratio,
        "extra_event_count": extra_event_count,
        "interval_hours_median": median_interval,
        "strictly_increasing": strictly_increasing,
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


def _rule_audit_reasons(rule: RuleSnapshot) -> list[str]:
    reasons: list[str] = []
    for name, value in (
        ("tick_size", rule.tick_size),
        ("step_size", rule.step_size),
        ("min_qty", rule.min_qty),
        ("min_notional", rule.min_notional),
    ):
        if not math.isfinite(value) or value <= 0:
            reasons.append(f"invalid_{name}")
    if rule.status.upper() != "TRADING":
        reasons.append(f"invalid_status={rule.status or 'missing'}")
    if rule.contract_type.upper() != "TRADIFI_PERPETUAL":
        reasons.append(
            f"invalid_contract_type={rule.contract_type or 'missing'}"
        )
    return reasons


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


def _row_close_dt(row: Mapping[str, Any]) -> datetime:
    return datetime.fromtimestamp(int(row["close_time"]) / 1000, tz=UTC)


def _projected_funding_pct(
    visible_funding_rate: float,
    as_of: datetime,
    force_close_at: datetime,
) -> float:
    remaining_hours = max(
        0.0,
        (force_close_at - as_of).total_seconds() / 3600,
    )
    return abs(float(visible_funding_rate)) * math.ceil(remaining_hours / 8)


def _window_manifest_row(symbol: str, window: ClosedWindow) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "market_group": window.market_group,
        "calendar": window.calendar,
        "window_key": window.window_key,
        "window_kind": window.window_kind,
        "split": window.split,
        "previous_market_close": (
            window.previous_market_close.isoformat()
            if window.previous_market_close
            else ""
        ),
        "observation_start": (
            window.observation_start.isoformat()
            if window.observation_start
            else ""
        ),
        "observation_end": (
            window.observation_end.isoformat() if window.observation_end else ""
        ),
        "trade_start": window.trade_start.isoformat() if window.trade_start else "",
        "last_trade_bar": (
            window.last_trade_bar.isoformat() if window.last_trade_bar else ""
        ),
        "force_close_at": (
            window.force_close_at.isoformat() if window.force_close_at else ""
        ),
        "next_reference_open": (
            window.next_reference_open.isoformat()
            if window.next_reference_open
            else ""
        ),
        "complete": window.complete,
        "blocked_reason": window.blocked_reason,
        "start_time": window.start_time.isoformat(),
        "end_time": window.end_time.isoformat(),
        "bar_count": len(window.rows),
    }


def _regime_row(
    symbol: str,
    window: ClosedWindow,
    profile: str,
    scenario: str,
    decision: RegimeDecision,
    admission: str,
    depth_proxy: float,
) -> dict[str, Any]:
    features = decision.features
    return {
        "symbol": symbol,
        "market_group": window.market_group,
        "window_key": window.window_key,
        "split": window.split,
        "profile": profile,
        "scenario": scenario,
        "admission": admission,
        "regime_score": decision.grid_score,
        "regime_state": decision.state,
        "allowed": decision.allowed,
        "verdict": decision.verdict,
        "threshold_used": decision.threshold_used,
        "hard_limit_reasons": "|".join(decision.hard_blocks),
        "volatility_component": decision.component_scores.get("volatility"),
        "trend_component": decision.component_scores.get("trend"),
        "liquidity_component": decision.component_scores.get("liquidity"),
        "mean_reversion_component": decision.component_scores.get("mean_reversion"),
        "cost_component": decision.component_scores.get("cost"),
        "directional_efficiency": features.directional_efficiency,
        "volatility_expansion": features.volatility_expansion,
        "reversal_ratio": features.reversal_ratio,
        "spread_pct": features.spread_pct,
        "depth_usdt": features.depth_usdt,
        "depth_source": "OBSERVATION_QUOTE_VOLUME_PER_BAR_PROXY",
        "depth_proxy": depth_proxy,
        "as_of": decision.as_of.isoformat(),
        "model_version": decision.model_version,
        "feature_version": decision.feature_version,
    }


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
    return _aggregate(
        rows,
        ("engine_mode", "market_group", "profile", "scenario"),
    )


def _pre_exit_inventory_rows(
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    fields = (
        "engine_mode",
        "symbol",
        "market_group",
        "window_key",
        "profile",
        "scenario",
        "seed",
        "pre_exit_position_qty",
        "pre_exit_inventory_notional",
        "pre_exit_unrealized_pnl",
        "pre_exit_mark_price",
        "pre_exit_timestamp",
        "peak_negative_unrealized_pnl",
        "inventory_drag",
        "inventory_drag_ratio",
        "peak_inventory_drag_ratio",
        "inventory_realized_pnl",
        "max_unpaired_lots",
        "max_unpaired_lot_age",
    )
    return [{field: row.get(field) for field in fields} for row in rows]


def _exit_attribution_rows(
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    fields = (
        "engine_mode",
        "symbol",
        "window_key",
        "profile",
        "scenario",
        "seed",
        "paired_grid_pnl",
        "inventory_realized_pnl",
        "funding_paid",
        "funding_received",
        "maker_fees",
        "taker_fees",
        "seed_cost",
        "stop_exit_cost",
        "stop_exit_slippage_cost",
        "force_exit_fees",
        "force_exit_slippage_cost",
        "force_exit_cost",
        "regrid_cost",
        "net_pnl",
        "stopped_reason",
    )
    return [{field: row.get(field) for field in fields} for row in rows]


def _static_vs_controller_summary(
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    summary = _aggregate(
        list(rows),
        ("engine_mode", "symbol", "profile", "scenario"),
    )
    indexed = {
        (
            item["engine_mode"],
            item["symbol"],
            item["profile"],
            item["scenario"],
        ): item
        for item in summary
    }
    combos = sorted(
        {
            (item["symbol"], item["profile"], item["scenario"])
            for item in summary
        }
    )
    output: list[dict[str, Any]] = []
    for symbol, profile, scenario in combos:
        r0 = indexed.get(("R0_STATIC_REPAIRED", symbol, profile, scenario))
        r1 = indexed.get(("R1_CONTROLLER_FAITHFUL", symbol, profile, scenario))
        if r0 is None or r1 is None:
            continue
        output.append(
            {
                "symbol": symbol,
                "profile": profile,
                "scenario": scenario,
                "r0_net_pnl": r0["total_pnl"],
                "r1_net_pnl": r1["total_pnl"],
                "r1_minus_r0_net_pnl": (
                    float(r1["total_pnl"]) - float(r0["total_pnl"])
                ),
                "r0_max_drawdown": r0["max_drawdown"],
                "r1_max_drawdown": r1["max_drawdown"],
                "r1_minus_r0_drawdown": (
                    float(r1["max_drawdown"]) - float(r0["max_drawdown"])
                ),
                "r0_mean_inventory_drag_ratio": r0[
                    "mean_inventory_drag_ratio"
                ],
                "r1_mean_inventory_drag_ratio": r1[
                    "mean_inventory_drag_ratio"
                ],
                "r1_minus_r0_inventory_drag": (
                    float(r1["mean_inventory_drag_ratio"])
                    - float(r0["mean_inventory_drag_ratio"])
                ),
                "r0_best_window_concentration": r0[
                    "best_window_concentration"
                ],
                "r1_best_window_concentration": r1[
                    "best_window_concentration"
                ],
                "r1_minus_r0_concentration": (
                    float(r1["best_window_concentration"])
                    - float(r0["best_window_concentration"])
                ),
            }
        )
    return output


def _v271_vs_r0_summary(
    old_summary_path: Path,
    current_symbol_summary: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not old_summary_path.exists():
        return []
    with old_summary_path.open("r", encoding="utf-8-sig", newline="") as handle:
        old_rows = list(csv.DictReader(handle))
    old_index = {
        (row.get("symbol"), row.get("profile"), row.get("scenario")): row
        for row in old_rows
    }
    output: list[dict[str, Any]] = []
    for current in current_symbol_summary:
        if current.get("engine_mode") != "R0_STATIC_REPAIRED":
            continue
        key = (
            current.get("symbol"),
            current.get("profile"),
            current.get("scenario"),
        )
        old = old_index.get(key)
        if old is None:
            continue
        old_pnl = float(old.get("total_pnl") or 0.0)
        current_pnl = float(current.get("total_pnl") or 0.0)
        output.append(
            {
                "symbol": key[0],
                "profile": key[1],
                "scenario": key[2],
                "v271_net_pnl": old_pnl,
                "v272_r0_net_pnl": current_pnl,
                "net_pnl_delta": current_pnl - old_pnl,
                "v271_unique_windows": int(
                    float(old.get("unique_windows") or 0)
                ),
                "v272_r0_unique_windows": int(
                    current.get("unique_windows") or 0
                ),
                "incomplete_window_exclusion": "APPLIED",
                "corrected_window_end": "APPLIED",
                "inventory_accounting": "APPLIED",
                "regime_admission": "APPLIED",
                "other": (
                    "funding_sign_split;exposed_reclassification;"
                    "no_future_funding_projection"
                ),
                "attribution_note": (
                    "总差值是耦合修复后的联合效应；未通过事后消融把收益"
                    "强行分摊到单一修复项。"
                ),
            }
        )
    return output


def _report_diagnostics(
    rows: Sequence[dict[str, Any]],
    session_rows: Sequence[dict[str, Any]],
    regrid_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    primary_rows = [
        row
        for row in rows
        if row.get("engine_mode") == "R1_CONTROLLER_FAITHFUL"
        and row.get("scenario") == "PRIMARY_ZERO_MAKER"
    ]
    primary_sessions = [
        row
        for row in session_rows
        if row.get("scenario") == "PRIMARY_ZERO_MAKER"
    ]
    primary_regrids = [
        row
        for row in regrid_rows
        if row.get("scenario") == "PRIMARY_ZERO_MAKER"
    ]
    paired_grid_pnl = sum(
        float(row.get("paired_grid_pnl") or 0.0) for row in primary_rows
    )
    inventory_realized_pnl = sum(
        float(row.get("inventory_realized_pnl") or 0.0)
        for row in primary_rows
    )
    absorbed = max(0.0, -inventory_realized_pnl)
    nonzero_pre_exit = [
        row
        for row in primary_rows
        if abs(float(row.get("pre_exit_position_qty") or 0.0)) > 0.0
    ]
    worst_tail = (
        min(
            primary_rows,
            key=lambda row: float(row.get("pre_exit_unrealized_pnl") or 0.0),
        )
        if primary_rows
        else {}
    )
    stopped_reason_counts = Counter(
        str(row.get("stopped_reason") or "UNKNOWN") for row in primary_sessions
    )
    regrid_status_counts = Counter(
        str(row.get("status") or "UNKNOWN") for row in primary_regrids
    )
    return {
        "scope": "R1_CONTROLLER_FAITHFUL/PRIMARY_ZERO_MAKER",
        "run_count": len(primary_rows),
        "session_count": len(primary_sessions),
        "cooldown_count": sum(
            int(float(row.get("cooldown_count") or 0)) for row in primary_rows
        ),
        "reentry_count": sum(
            int(float(row.get("reentry_count") or 0)) for row in primary_rows
        ),
        "stopped_reason_counts": dict(sorted(stopped_reason_counts.items())),
        "regrid_status_counts": dict(sorted(regrid_status_counts.items())),
        "regrid_cost": sum(
            float(row.get("regrid_cost") or 0.0) for row in primary_regrids
        ),
        "execution_cost": sum(
            float(row.get("total_cost") or 0.0) for row in primary_rows
        ),
        "paired_grid_pnl": paired_grid_pnl,
        "inventory_realized_pnl": inventory_realized_pnl,
        "paired_profit_absorbed": absorbed,
        "paired_profit_absorbed_ratio": (
            absorbed / max(paired_grid_pnl, MINIMUM_DRAG_DENOMINATOR)
        ),
        "mean_pre_exit_inventory_notional": (
            statistics.fmean(
                abs(float(row.get("pre_exit_inventory_notional") or 0.0))
                for row in primary_rows
            )
            if primary_rows
            else 0.0
        ),
        "nonzero_pre_exit_runs": len(nonzero_pre_exit),
        "worst_tail": {
            "symbol": worst_tail.get("symbol"),
            "profile": worst_tail.get("profile"),
            "seed": worst_tail.get("seed"),
            "window_key": worst_tail.get("window_key"),
            "pre_exit_unrealized_pnl": float(
                worst_tail.get("pre_exit_unrealized_pnl") or 0.0
            ),
            "pre_exit_inventory_notional": float(
                worst_tail.get("pre_exit_inventory_notional") or 0.0
            ),
        },
    }


def _inventory_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("engine_mode"),
            row.get("market_group"),
            row.get("profile"),
            row.get("scenario"),
        )
        groups[key].append(row)
    output: list[dict[str, Any]] = []
    for key, items in sorted(groups.items()):
        output.append(
            {
                "engine_mode": key[0],
                "market_group": key[1],
                "profile": key[2],
                "scenario": key[3],
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


def _window_boundary_audit(
    rows: Sequence[dict[str, Any]],
) -> str:
    lines = [
        "# Window Boundary Audit",
        "",
        "公式：`observation_end = previous_market_close + 180 minutes`；"
        "`force_close_at = next_reference_open - 120 minutes`；"
        "`minimum_trade_minutes` 仅用于准入。",
        "",
    ]
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        selected.setdefault(str(row.get("market_group") or ""), row)
    for market_group, row in sorted(selected.items()):
        observation_end = datetime.fromisoformat(str(row["observation_end"]))
        force_close_at = datetime.fromisoformat(str(row["force_close_at"]))
        remaining = (force_close_at - observation_end).total_seconds() / 60
        lines.extend(
            [
                f"## {market_group}",
                "",
                f"- previous_market_close: `{row['previous_market_close']}`",
                f"- observation_end: `{row['observation_end']}`",
                f"- force_close_at: `{row['force_close_at']}`",
                f"- next_reference_open: `{row['next_reference_open']}`",
                f"- last_included_trade_bar: `{row['last_trade_bar']}`",
                f"- remaining_trade_minutes: `{remaining:.0f}`",
                f"- window_complete: `{row['complete']}`",
                "",
            ]
        )
    return "\n".join(lines)


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


def _legacy_final_report_v271(
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


def _final_report(
    payload: Mapping[str, Any],
    acceptance: Mapping[str, Any],
    data_audit: Mapping[str, Any],
    run_manifest: Mapping[str, Any],
) -> str:
    best = acceptance.get("best_candidate") or {}
    diagnostics = payload.get("report_diagnostics") or {}
    profile_summary = payload.get("profile_summary") or []
    seed_summary = payload.get("seed_summary") or []
    symbol_summary = payload.get("symbol_summary") or []
    v271_vs_r0 = payload.get("v271_vs_r0") or []
    r0 = [
        row
        for row in profile_summary
        if row.get("engine_mode") == "R0_STATIC_REPAIRED"
    ]
    r1 = [
        row
        for row in profile_summary
        if row.get("engine_mode") == "R1_CONTROLLER_FAITHFUL"
    ]
    lines = [
        "# Semiconductor Grid v2.7.2 Backtest",
        "",
        "## 1. 实现状态",
        "",
        "- P0 修复：窗口完整性、正确尾部、退出前库存、真实 Regime 已接入。",
        "- 完整测试：以 `pytest.stdout.log` 为准；正式矩阵仅在 0 failed 后运行。",
        f"- 未完成窗口污染：`否`（总完整窗口 {run_manifest.get('total_unique_windows')}）。",
        "- 库存指标：使用强平前快照，不再从平仓后净仓位推断。",
        "- Regime：生产 `RegimeEngine`，观察期闭合 Bar；历史深度使用明确标记的"
        " `OBSERVATION_QUOTE_VOLUME_PER_BAR_PROXY`。",
        "",
        "## 2. 数据与样本暴露",
        "",
        f"- data_cutoff_utc: `{run_manifest.get('data_cutoff_utc')}`",
        f"- 完整窗口数（symbol-window records）: `{run_manifest.get('total_unique_windows')}`",
        f"- 未完成窗口数（已排除）: `{run_manifest.get('incomplete_window_count')}`",
        f"- 已暴露历史边界: `{run_manifest.get('exposed_history_end')}`",
        f"- 已暴露历史标签: `RESEARCH_VALIDATION_EXPOSED`",
        f"- Forward OOS 起点: `{run_manifest.get('forward_oos_start')}`",
        f"- Forward OOS 状态: `{acceptance.get('forward_oos_status', 'INSUFFICIENT_FORWARD_OOS')}`",
        "",
        "## 3. v2.7.1 与 R0 差异",
        "",
        "- incomplete-window exclusion：跨越数据截止的窗口不运行、不聚合。",
        "- corrected window end：交易持续到 `force_close_at`，不再额外扣除最短交易时长。",
        "- inventory accounting：保存 pre-exit 快照并拆分库存退出损益。",
        "- regime admission：不再使用固定 100 分。",
        "- other：Funding 收付分离，旧 OOS 全部重分类为 exposed。",
    ]
    for item in v271_vs_r0:
        lines.append(
            f"- {item.get('symbol')}/{item.get('profile')}/"
            f"{item.get('scenario')}: v2.7.1={float(item.get('v271_net_pnl', 0)):.6f}, "
            f"R0={float(item.get('v272_r0_net_pnl', 0)):.6f}, "
            f"delta={float(item.get('net_pnl_delta', 0)):.6f}。"
        )
    lines.extend(["", "## 4. R0 固定网格结果", ""])
    for item in r0:
        lines.append(
            f"- {item.get('market_group')}/{item.get('profile')}/"
            f"{item.get('scenario')}: pnl={float(item.get('total_pnl', 0)):.6f}, "
            f"windows={item.get('unique_windows')}, "
            f"pf={float(item.get('profit_factor', 0)):.3f}"
        )
    lines.extend(["", "## 5. R1 Controller-faithful 结果", ""])
    for item in r1:
        lines.append(
            f"- {item.get('market_group')}/{item.get('profile')}/"
            f"{item.get('scenario')}: pnl={float(item.get('total_pnl', 0)):.6f}, "
            f"windows={item.get('unique_windows')}, "
            f"pf={float(item.get('profit_factor', 0)):.3f}"
        )
    lines.extend(
        [
            (
                f"- Controller 诊断范围：`{diagnostics.get('scope')}`，"
                f"sessions={diagnostics.get('session_count', 0)}，"
                f"cooldowns={diagnostics.get('cooldown_count', 0)}，"
                f"reentries={diagnostics.get('reentry_count', 0)}。"
            ),
            (
                "- 滚动重建状态："
                f"`{json.dumps(diagnostics.get('regrid_status_counts', {}), ensure_ascii=False)}`；"
                f"regrid_cost={float(diagnostics.get('regrid_cost', 0.0)):.6f} USDT。"
            ),
            (
                "- 会话停止原因："
                f"`{json.dumps(diagnostics.get('stopped_reason_counts', {}), ensure_ascii=False)}`；"
                f"执行成本={float(diagnostics.get('execution_cost', 0.0)):.6f} USDT。"
            ),
        ]
    )
    worst_tail = diagnostics.get("worst_tail") or {}
    lines.extend(
        [
            "",
            "## 6. 库存与退出归因",
            "",
            "- 以下为 R1/PRIMARY 逐运行等权诊断合计（候选与种子并列，"
            "不解释为可同时部署的组合收益）。",
            (
                f"- 配对网格利润={float(diagnostics.get('paired_grid_pnl', 0.0)):.6f} USDT；"
                f"库存已实现损益={float(diagnostics.get('inventory_realized_pnl', 0.0)):.6f} USDT；"
                f"被库存吞噬={float(diagnostics.get('paired_profit_absorbed', 0.0)):.6f} USDT"
                f"（{float(diagnostics.get('paired_profit_absorbed_ratio', 0.0)):.3f}× 配对利润）。"
            ),
            (
                f"- 退出前平均库存名义价值="
                f"{float(diagnostics.get('mean_pre_exit_inventory_notional', 0.0)):.6f} USDT；"
                f"非零退出前库存运行={diagnostics.get('nonzero_pre_exit_runs', 0)}/"
                f"{diagnostics.get('run_count', 0)}。"
            ),
            (
                f"- 最差库存尾部：{worst_tail.get('symbol')}/{worst_tail.get('profile')}/"
                f"seed={worst_tail.get('seed')}，window=`{worst_tail.get('window_key')}`，"
                f"退出前未实现损益="
                f"{float(worst_tail.get('pre_exit_unrealized_pnl', 0.0)):.6f} USDT，"
                f"名义价值={float(worst_tail.get('pre_exit_inventory_notional', 0.0)):.6f} USDT。"
            ),
            f"- 拖累比率最小分母：`{MINIMUM_DRAG_DENOMINATOR}` USDT；"
            "绝对拖累同时保留，未将异常比率截断为零。",
            "- 逐运行明细见 `pre-exit-inventory-breakdown.csv` 与"
            " `exit-attribution.csv`。",
            "",
            "## 7. 稳健性",
            "",
            "- 三执行场景和六随机种子均按预注册矩阵运行；种子先在同一市场窗口内聚合。",
        ]
    )
    if best:
        best_market = best.get("market_group")
        best_profile = best.get("profile")
        for item in r1:
            if (
                item.get("market_group") == best_market
                and item.get("profile") == best_profile
            ):
                lines.append(
                    f"- 最佳受检候选 {best_market}/{best_profile}/"
                    f"{item.get('scenario')}: pnl="
                    f"{float(item.get('total_pnl', 0.0)):.6f} USDT。"
                )
        for item in seed_summary:
            if (
                item.get("engine_mode") == "R1_CONTROLLER_FAITHFUL"
                and item.get("market_group") == best_market
                and item.get("profile") == best_profile
                and item.get("scenario") == "PRIMARY_ZERO_MAKER"
            ):
                lines.append(
                    f"- PRIMARY seed={item.get('seed')}: pnl="
                    f"{float(item.get('total_pnl', 0.0)):.6f} USDT，"
                    f"windows={item.get('unique_windows')}。"
                )
    lines.extend(["", "## 8. 标的级结论", ""])
    for item in symbol_summary:
        if (
            item.get("engine_mode") == "R1_CONTROLLER_FAITHFUL"
            and item.get("scenario") == "PRIMARY_ZERO_MAKER"
        ):
            if int(item.get("unique_windows") or 0) < 8:
                status = "INSUFFICIENT_DATA"
            elif float(item.get("total_pnl", 0)) > 0:
                status = "RESEARCH_CANDIDATE"
            else:
                status = "REJECT"
            lines.append(
                f"- {item.get('symbol')}/{item.get('profile')}: `{status}`，"
                f"net_pnl={float(item.get('total_pnl', 0)):.6f}。"
            )
    lines.extend(
        [
            "",
            "## 9. Forward OOS",
            "",
            f"- 状态：`{acceptance.get('forward_oos_status', 'INSUFFICIENT_FORWARD_OOS')}`",
            "- 历史已暴露窗口未计入 Forward OOS。",
            "",
            "## 10. 验收门槛",
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
            "## 11. 最终结论代码",
            "",
            f"`{acceptance.get('conclusion_code')}`",
            "",
        ]
    )
    if acceptance.get("conclusion_code") not in {
        "PASS_TESTNET_CANDIDATE",
        "RESEARCH_CANDIDATE_AWAITING_FORWARD_OOS",
        "PASS_RESEARCH_ONLY_MAKER_DEPENDENT",
    }:
        lines.append(
            "本轮未形成稳健候选，保持自动开仓关闭，不进行参数搜索。"
        )
    elif best:
        lines.append(
            "历史已暴露样本仅形成研究候选；自动开仓保持关闭，等待 Forward OOS。"
        )
    lines.extend(
        [
            "",
            "## 安全开关",
            "",
            "- startup_auto_entry = false",
            "- testnet_force_window = false",
            "- testnet_fast_observation = false",
            f"- data_quality_ok = {data_audit.get('ok')}",
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


def _git_status_short() -> str:
    completed = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return (completed.stdout or "").strip()


def _git_commit_time(ref: str = "HEAD") -> datetime:
    completed = subprocess.run(
        ["git", "show", "-s", "--format=%cI", ref],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    value = (completed.stdout or "").strip()
    if completed.returncode != 0 or not value:
        raise RuntimeError(f"无法读取 {ref} 的提交时间")
    return datetime.fromisoformat(value).astimezone(UTC)


if __name__ == "__main__":
    main()
