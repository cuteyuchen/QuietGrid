"""Two-year archive freezing and robustness research for QuietGrid.

This module deliberately lives outside the trading runtime.  It reuses the
production grid generator and bar-by-bar backtester, but it never reads API
credentials and never sends an exchange order.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import os
import statistics
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from core.models import GridDirectionMode, GridParams
from data_sources.binance_archive_source import BinanceArchiveHistoricalDataSource
from data_sources.models import NormalizedKline
from strategy.adaptive_grid import (
    AdaptiveGridConfig,
    AdaptiveGridGenerator,
    GridEconomicsError,
)
from strategy.backtest import BacktestConfig, run_grid_backtest
from strategy.grid_calculator import GridCalculationError
from strategy.regime import RegimeConfig, RegimeDecision, RegimeEngine


UTC = timezone.utc
NY_TZ = ZoneInfo("America/New_York")
SCHEMA_VERSION = 1
CSV_FIELDS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
)


@dataclass(frozen=True)
class FreezeRequest:
    symbol: str
    start_time: datetime
    end_time: datetime
    output_dir: Path
    interval: str = "1m"
    max_missing_ratio: float = 0.001


@dataclass(frozen=True)
class WeekendWindow:
    symbol: str
    window_id: str
    market_close: datetime
    force_close_at: datetime
    rows: tuple[NormalizedKline, ...]
    observation_rows: int
    status: str
    skip_reason: str | None = None
    history_rows: tuple[NormalizedKline, ...] = ()

    @property
    def tradable_rows(self) -> int:
        return max(0, len(self.rows) - self.observation_rows)


@dataclass(frozen=True)
class SymbolRules:
    tick_size: float
    step_size: float
    min_qty: float
    min_notional: float


DEFAULT_SYMBOL_RULES = {
    "BTCUSDT": SymbolRules(0.1, 0.001, 0.001, 50.0),
    "ETHUSDT": SymbolRules(0.01, 0.001, 0.001, 50.0),
}

JOINT_COST_SCENARIOS = {
    "BASE": (0.0002, 0.0005, 10.0),
    "COST_25": (0.00025, 0.000625, 15.0),
    "COST_50": (0.0003, 0.00075, 20.0),
}


@dataclass(frozen=True)
class ParameterSet:
    range_multiplier: float
    min_step_pct: float
    stop_buffer_pct: float
    direction_mode: GridDirectionMode = GridDirectionMode.NEUTRAL

    @property
    def parameter_id(self) -> str:
        return (
            f"{self.direction_mode.value.lower()}_"
            f"r{self.range_multiplier:.3f}_"
            f"s{self.min_step_pct:.5f}_"
            f"x{self.stop_buffer_pct:.4f}"
        )


@dataclass(frozen=True)
class EntryFilter:
    max_directional_efficiency: float
    max_volatility_expansion: float
    min_reversal_ratio: float

    @property
    def filter_id(self) -> str:
        return (
            f"de{self.max_directional_efficiency:.2f}_"
            f"ve{self.max_volatility_expansion:.2f}_"
            f"rr{self.min_reversal_ratio:.2f}"
        )


@dataclass(frozen=True)
class SymbolResearchPolicy:
    parameter: ParameterSet
    max_inventory_notional: float
    entry_filter: EntryFilter | None = None
    max_unpaired_lots_per_side: int | None = None
    reduce_target_step_fraction: float | None = None


@dataclass(frozen=True)
class DynamicModeRule:
    lookback_rows: int
    directional_threshold: float
    neutral_threshold: float
    min_persistence: float
    segment_rows: int = 360
    trend_alignment: str = "MOMENTUM"

    @property
    def rule_id(self) -> str:
        return (
            f"lb{self.lookback_rows}_"
            f"dt{self.directional_threshold:.2f}_"
            f"nt{self.neutral_threshold:.2f}_"
            f"p{self.min_persistence:.2f}_"
            f"{self.trend_alignment.lower()}"
        )


@dataclass(frozen=True)
class DynamicModeDecision:
    direction_mode: GridDirectionMode | None
    reason: str
    lookback_rows: int
    return_pct: float
    realized_path_volatility: float
    trend_ratio: float
    persistence: float


@dataclass(frozen=True)
class WindDownMakerPolicy:
    reprice_interval_bars: int
    initial_offset_steps: float
    unwind_fraction: float = 1.0

    @property
    def policy_id(self) -> str:
        return (
            f"maker_unwind_i{self.reprice_interval_bars}_"
            f"o{self.initial_offset_steps:.2f}_"
            f"f{self.unwind_fraction:.2f}"
        )


@dataclass(frozen=True)
class ResearchConfig:
    capital_per_symbol: float = 500.0
    capital_by_symbol: dict[str, float] = field(default_factory=dict)
    leverage: float = 1.0
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    funding_rate_per_settlement: float = 0.0001
    seed_slippage_bps: float = 10.0
    stop_slippage_bps: float = 10.0
    maker_fill_probability: float = 0.65
    max_fills_per_bar: int = 2
    risk_budget_pct: float = 0.03
    observation_rows: int = 180
    force_close_minutes: int = 120
    minimum_tradable_rows: int = 30
    enter_threshold: float = 75.0
    retention_threshold: float = 65.0
    retention_soft_breach_limit: int = 3
    assumed_spread_pct: float = 0.0001
    assumed_depth_usdt: float = 1_000_000.0
    dev_ratio: float = 0.50
    validation_ratio: float = 0.25
    selection_top_k: int = 5
    walk_forward_train_windows: int = 26
    walk_forward_test_windows: int = 8
    walk_forward_step_windows: int = 8
    min_windows_per_split: int = 8
    wind_down_bars: int = 0
    max_inventory_notional: float = 0.0
    inventory_caution_utilization: float = 0.40
    inventory_critical_utilization: float = 0.80
    wind_down_reprice_interval_bars: int = 0
    wind_down_initial_offset_steps: float = 0.0
    wind_down_unwind_fraction: float = 1.0
    max_unpaired_lots_per_side: int = 0
    reduce_target_step_fraction: float = 1.0
    unpaired_lot_cap_enforcement: str = "INTRABAR"


@dataclass(frozen=True)
class WindowResult:
    parameter_id: str
    symbol: str
    window_id: str
    market_close: str
    status: str
    reason: str
    pnl: float = 0.0
    max_drawdown: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    fill_count: int = 0
    pair_count: int = 0
    defensive_count: int = 0
    regime_score: float | None = None
    grid_count: int | None = None
    step_pct: float | None = None
    gross_grid_pnl: float = 0.0
    paired_grid_pnl: float = 0.0
    stop_exit_pnl: float = 0.0
    stop_exit_cost: float = 0.0
    max_inventory_utilization: float = 0.0
    stopped_at_index: int | None = None
    wind_down_reprice_count: int = 0
    wind_down_maker_fill_count: int = 0
    wind_down_maker_pnl: float = 0.0
    max_unpaired_lot_age_bars: int = 0
    exit_oldest_lot_age_bars: int = 0
    exit_long_qty: float = 0.0
    exit_short_qty: float = 0.0
    exit_hedged_fraction: float = 0.0


@dataclass(frozen=True)
class AggregateMetrics:
    window_count: int
    symbol_window_count: int
    traded_symbol_windows: int
    blocked_symbol_windows: int
    total_pnl: float
    total_return_pct: float
    annualized_return_pct: float | None
    max_drawdown: float
    max_drawdown_pct: float
    positive_window_ratio: float
    profit_factor: float | None
    sharpe_per_week: float | None
    trade_coverage: float
    pair_count: int
    fill_count: int
    fees_paid: float
    funding_paid: float
    best_window_concentration: float
    objective: float


@dataclass(frozen=True)
class SplitResult:
    development: tuple[str, ...]
    validation: tuple[str, ...]
    final_oos: tuple[str, ...]


@dataclass
class _WindowContext:
    window: WeekendWindow
    entry_decision: RegimeDecision | None = None
    rolling_components: list[dict[str, Any]] | None = None


async def freeze_binance_archives(
    request: FreezeRequest,
    *,
    source_factory: Callable[[], BinanceArchiveHistoricalDataSource] | None = None,
) -> tuple[Path, Path]:
    """Download official monthly/daily archives, verify checksums, and freeze CSV."""

    symbol = request.symbol.strip().upper()
    if request.interval != "1m":
        raise ValueError("稳健性研究当前只接受 1m 数据。")
    if request.start_time.tzinfo is None or request.end_time.tzinfo is None:
        raise ValueError("冻结时间必须包含时区。")
    if request.start_time >= request.end_time:
        raise ValueError("冻结开始时间必须早于结束时间。")
    if not 0 <= request.max_missing_ratio < 1:
        raise ValueError("max_missing_ratio 必须在 [0, 1) 内。")

    output_dir = request.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    staging = output_dir / f".{symbol.lower()}_{uuid4().hex}.csv.tmp"
    source = (source_factory or BinanceArchiveHistoricalDataSource)()
    available_end = datetime.combine(
        source.archive_available_until() + timedelta(days=1),
        time.min,
        tzinfo=UTC,
    )
    effective_end = min(request.end_time.astimezone(UTC), available_end)
    if effective_end <= request.start_time.astimezone(UTC):
        await source.close()
        raise ValueError("所选范围尚无可用的 Binance 官方日归档。")

    row_count = 0
    duplicate_rows = 0
    missing_intervals = 0
    first_open_time: int | None = None
    last_open_time: int | None = None
    last_fingerprint: tuple[Any, ...] | None = None
    try:
        with staging.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            async for row in source.fetch_klines(
                symbol,
                request.interval,
                request.start_time.astimezone(UTC),
                effective_end,
            ):
                fingerprint = (
                    row.open_time,
                    row.open,
                    row.high,
                    row.low,
                    row.close,
                    row.volume,
                    row.close_time,
                    row.quote_volume,
                    row.trade_count,
                )
                if last_open_time is not None:
                    if row.open_time < last_open_time:
                        raise ValueError("官方归档 K 线时间倒序，拒绝冻结。")
                    if row.open_time == last_open_time:
                        if fingerprint != last_fingerprint:
                            raise ValueError("官方归档存在冲突重复 K 线，拒绝冻结。")
                        duplicate_rows += 1
                        continue
                    delta = row.open_time - last_open_time
                    if delta % 60_000 != 0:
                        raise ValueError("官方归档 K 线时间不落在 1 分钟边界。")
                    missing_intervals += max(0, delta // 60_000 - 1)
                writer.writerow(_row_to_csv(row))
                row_count += 1
                first_open_time = first_open_time or row.open_time
                last_open_time = row.open_time
                last_fingerprint = fingerprint
        if row_count == 0 or first_open_time is None or last_open_time is None:
            raise ValueError("官方归档没有返回可冻结的 K 线。")
        expected = row_count + missing_intervals
        missing_ratio = missing_intervals / max(1, expected)
        if missing_ratio > request.max_missing_ratio:
            raise ValueError(
                f"历史数据缺口比例 {missing_ratio:.4%} 超过上限 "
                f"{request.max_missing_ratio:.4%}。"
            )

        checksum = _sha256_file(staging)
        dataset_id = (
            f"binance_um_{symbol.lower()}_1m_"
            f"{first_open_time}_{last_open_time}_{checksum[:12]}"
        )
        data_path = output_dir / f"{dataset_id}.csv"
        manifest_path = output_dir / f"{dataset_id}.manifest.json"
        if data_path.exists() and manifest_path.exists():
            existing = verify_frozen_dataset(manifest_path)
            if (
                existing.get("file_sha256") == checksum
                and existing.get("dataset_id") == dataset_id
            ):
                return data_path, manifest_path
        if data_path.exists() or manifest_path.exists():
            raise FileExistsError(f"冻结数据集已存在: {dataset_id}")
        os.replace(staging, data_path)
        segments = [asdict(item) for item in source.source_segments]
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "dataset_id": dataset_id,
            "provider": "binance_archive",
            "market": "USDS_M",
            "market_path": source.market_path,
            "symbol": symbol,
            "interval": "1m",
            "requested_start": request.start_time.astimezone(UTC).isoformat(),
            "requested_end": request.end_time.astimezone(UTC).isoformat(),
            "actual_start": datetime.fromtimestamp(
                first_open_time / 1000, tz=UTC
            ).isoformat(),
            "actual_end": datetime.fromtimestamp(
                (last_open_time + 60_000) / 1000, tz=UTC
            ).isoformat(),
            "archive_available_end": effective_end.isoformat(),
            "tail_truncated": effective_end < request.end_time.astimezone(UTC),
            "row_count": row_count,
            "duplicate_rows": duplicate_rows,
            "missing_intervals": missing_intervals,
            "missing_ratio": missing_ratio,
            "file_name": data_path.name,
            "file_sha256": checksum,
            "official_checksums_verified": bool(source.verify_official_checksum),
            "source_segments": segments,
            "created_at": datetime.now(UTC).isoformat(),
        }
        _write_json_atomic(manifest_path, manifest)
        return data_path, manifest_path
    finally:
        await source.close()
        if staging.exists():
            staging.unlink()


def verify_frozen_dataset(manifest_path: str | Path) -> dict[str, Any]:
    manifest_file = Path(manifest_path).resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ValueError("冻结数据集 schema_version 不受支持。")
    data_path = (manifest_file.parent / str(manifest["file_name"])).resolve()
    if not data_path.is_relative_to(manifest_file.parent):
        raise ValueError("冻结数据集路径越界。")
    if not data_path.is_file():
        raise FileNotFoundError(data_path)
    actual = _sha256_file(data_path)
    if actual != manifest.get("file_sha256"):
        raise ValueError("冻结数据集 SHA-256 不匹配。")
    with data_path.open("r", encoding="utf-8") as handle:
        counted = sum(1 for _ in handle) - 1
    if counted != int(manifest.get("row_count") or 0):
        raise ValueError("冻结数据集行数与 manifest 不一致。")
    return manifest


def load_weekend_windows(
    manifest_path: str | Path,
    *,
    observation_rows: int = 180,
    force_close_minutes: int = 120,
    minimum_tradable_rows: int = 30,
    history_rows: int = 0,
) -> list[WeekendWindow]:
    if history_rows < 0:
        raise ValueError("history_rows 不能为负。")
    manifest = verify_frozen_dataset(manifest_path)
    data_path = Path(manifest_path).resolve().parent / manifest["file_name"]
    start = datetime.fromisoformat(manifest["actual_start"]).astimezone(UTC)
    end = datetime.fromisoformat(manifest["actual_end"]).astimezone(UTC)
    boundaries = _weekend_boundaries(start, end, force_close_minutes)
    if not boundaries:
        return []

    windows: list[WeekendWindow] = []
    boundary_index = 0
    bucket: list[NormalizedKline] = []
    boundary_history: tuple[NormalizedKline, ...] = ()
    rolling_history: deque[NormalizedKline] = deque(maxlen=history_rows or None)
    with data_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = _csv_to_row(raw)
            while (
                boundary_index < len(boundaries)
                and row.open_datetime >= boundaries[boundary_index][1]
            ):
                windows.append(
                    _finalize_window(
                        manifest["symbol"],
                        boundaries[boundary_index],
                        bucket,
                        observation_rows,
                        minimum_tradable_rows,
                        boundary_history,
                    )
                )
                boundary_index += 1
                bucket = []
                boundary_history = ()
            if boundary_index >= len(boundaries):
                break
            market_close, force_close_at = boundaries[boundary_index]
            if market_close <= row.open_datetime < force_close_at:
                if not bucket:
                    boundary_history = (
                        tuple(rolling_history)
                        if history_rows > 0
                        else ()
                    )
                bucket.append(row)
            if history_rows > 0:
                rolling_history.append(row)
    while boundary_index < len(boundaries):
        windows.append(
            _finalize_window(
                manifest["symbol"],
                boundaries[boundary_index],
                bucket,
                observation_rows,
                minimum_tradable_rows,
                boundary_history,
            )
        )
        boundary_index += 1
        bucket = []
        boundary_history = ()
    return windows


def generate_parameter_sets(
    *,
    range_multipliers: Sequence[float],
    min_step_pcts: Sequence[float],
    stop_buffer_pcts: Sequence[float],
    direction_modes: Sequence[GridDirectionMode],
) -> list[ParameterSet]:
    values = [
        ParameterSet(float(range_value), float(step), float(stop), mode)
        for range_value, step, stop, mode in product(
            range_multipliers,
            min_step_pcts,
            stop_buffer_pcts,
            direction_modes,
        )
    ]
    if not values:
        raise ValueError("参数集合不能为空。")
    if any(item.range_multiplier <= 0 for item in values):
        raise ValueError("range_multiplier 必须为正。")
    if any(item.min_step_pct <= 0 for item in values):
        raise ValueError("min_step_pct 必须为正。")
    if any(not 0 < item.stop_buffer_pct < 1 for item in values):
        raise ValueError("stop_buffer_pct 必须在 (0, 1) 内。")
    return values


def generate_entry_filters(
    *,
    max_directional_efficiencies: Sequence[float],
    max_volatility_expansions: Sequence[float],
    min_reversal_ratios: Sequence[float],
) -> list[EntryFilter]:
    values = [
        EntryFilter(float(efficiency), float(expansion), float(reversal))
        for efficiency, expansion, reversal in product(
            max_directional_efficiencies,
            max_volatility_expansions,
            min_reversal_ratios,
        )
    ]
    if not values:
        raise ValueError("入口过滤器集合不能为空。")
    if any(not 0 <= item.max_directional_efficiency <= 1 for item in values):
        raise ValueError("max_directional_efficiency 必须在 [0, 1] 内。")
    if any(item.max_volatility_expansion <= 0 for item in values):
        raise ValueError("max_volatility_expansion 必须为正。")
    if any(not 0 <= item.min_reversal_ratio <= 1 for item in values):
        raise ValueError("min_reversal_ratio 必须在 [0, 1] 内。")
    return values


def generate_dynamic_mode_rules(
    *,
    lookback_rows: Sequence[int],
    directional_thresholds: Sequence[float],
    neutral_thresholds: Sequence[float],
    min_persistences: Sequence[float],
    segment_rows: int = 360,
    trend_alignments: Sequence[str] = ("MOMENTUM",),
) -> list[DynamicModeRule]:
    values = [
        DynamicModeRule(
            int(lookback),
            float(directional),
            float(neutral),
            float(persistence),
            int(segment_rows),
            str(alignment).upper(),
        )
        for lookback, directional, neutral, persistence, alignment in product(
            lookback_rows,
            directional_thresholds,
            neutral_thresholds,
            min_persistences,
            trend_alignments,
        )
        if float(neutral) < float(directional)
    ]
    if not values:
        raise ValueError("动态方向规则集合不能为空。")
    if any(item.lookback_rows < 60 for item in values):
        raise ValueError("动态方向 lookback_rows 至少为 60。")
    if segment_rows < 1:
        raise ValueError("动态方向 segment_rows 必须为正。")
    if any(item.neutral_threshold < 0 for item in values):
        raise ValueError("neutral_threshold 不能为负。")
    if any(item.directional_threshold <= 0 for item in values):
        raise ValueError("directional_threshold 必须为正。")
    if any(not 0 <= item.min_persistence <= 1 for item in values):
        raise ValueError("min_persistence 必须在 [0, 1] 内。")
    if any(item.trend_alignment not in {"MOMENTUM", "CONTRARIAN"} for item in values):
        raise ValueError("trend_alignment 仅支持 MOMENTUM/CONTRARIAN。")
    return values


def classify_dynamic_mode(
    rows: Sequence[NormalizedKline],
    rule: DynamicModeRule,
) -> DynamicModeDecision:
    """Classify direction using only rows already closed at decision time."""

    if len(rows) < rule.lookback_rows:
        return DynamicModeDecision(
            None,
            "INSUFFICIENT_HISTORY",
            len(rows),
            0.0,
            0.0,
            0.0,
            0.0,
        )
    history = list(rows[-rule.lookback_rows :])
    if any(
        current.open_time - previous.open_time != 60_000
        for previous, current in zip(history, history[1:])
    ):
        return DynamicModeDecision(
            None,
            "HISTORY_GAP",
            len(history),
            0.0,
            0.0,
            0.0,
            0.0,
        )
    closes = [float(item.close) for item in history]
    if any(not math.isfinite(value) or value <= 0 for value in closes):
        return DynamicModeDecision(
            None,
            "INVALID_HISTORY",
            len(history),
            0.0,
            0.0,
            0.0,
            0.0,
        )
    log_returns = [
        math.log(current / previous)
        for previous, current in zip(closes, closes[1:])
    ]
    total_log_return = math.log(closes[-1] / closes[0])
    path_volatility = math.sqrt(sum(value * value for value in log_returns))
    trend_ratio = (
        abs(total_log_return) / path_volatility
        if path_volatility > 1e-15
        else 0.0
    )
    direction_sign = 1 if total_log_return > 0 else -1 if total_log_return < 0 else 0
    segment_signs: list[int] = []
    for start in range(0, len(closes) - 1, rule.segment_rows):
        end = min(len(closes) - 1, start + rule.segment_rows)
        segment_return = math.log(closes[end] / closes[start])
        if abs(segment_return) <= 1e-15:
            continue
        segment_signs.append(1 if segment_return > 0 else -1)
    persistence = (
        sum(item == direction_sign for item in segment_signs) / len(segment_signs)
        if direction_sign and segment_signs
        else 0.0
    )
    common = {
        "lookback_rows": len(history),
        "return_pct": math.exp(total_log_return) - 1.0,
        "realized_path_volatility": path_volatility,
        "trend_ratio": trend_ratio,
        "persistence": persistence,
    }
    if (
        trend_ratio >= rule.directional_threshold
        and persistence >= rule.min_persistence
    ):
        selected_sign = (
            direction_sign
            if rule.trend_alignment == "MOMENTUM"
            else -direction_sign
        )
        return DynamicModeDecision(
            GridDirectionMode.LONG if selected_sign > 0 else GridDirectionMode.SHORT,
            (
                "PERSISTENT_UPTREND"
                if direction_sign > 0
                else "PERSISTENT_DOWNTREND"
            ),
            **common,
        )
    if trend_ratio <= rule.neutral_threshold:
        return DynamicModeDecision(
            GridDirectionMode.NEUTRAL,
            "RANGE_LIKE",
            **common,
        )
    return DynamicModeDecision(None, "AMBIGUOUS_TREND", **common)


def generate_wind_down_maker_policies(
    *,
    reprice_intervals: Sequence[int],
    initial_offset_steps: Sequence[float],
    unwind_fractions: Sequence[float] = (1.0,),
) -> list[WindDownMakerPolicy]:
    values = [
        WindDownMakerPolicy(int(interval), float(offset), float(fraction))
        for interval, offset, fraction in product(
            reprice_intervals,
            initial_offset_steps,
            unwind_fractions,
        )
    ]
    if not values:
        raise ValueError("渐进 Maker 去库存策略集合不能为空。")
    if any(item.reprice_interval_bars < 1 for item in values):
        raise ValueError("reprice_interval_bars 必须为正整数。")
    if any(item.initial_offset_steps < 0 for item in values):
        raise ValueError("initial_offset_steps 不能为负。")
    if any(not 0 < item.unwind_fraction <= 1 for item in values):
        raise ValueError("unwind_fraction 必须在 (0, 1] 内。")
    return values


def parameter_neighbors(
    target: ParameterSet,
    universe: Sequence[ParameterSet],
) -> list[ParameterSet]:
    axes = (
        sorted({item.range_multiplier for item in universe}),
        sorted({item.min_step_pct for item in universe}),
        sorted({item.stop_buffer_pct for item in universe}),
    )
    indexes = tuple(axis.index(value) for axis, value in zip(
        axes,
        (target.range_multiplier, target.min_step_pct, target.stop_buffer_pct),
    ))
    neighbors: list[ParameterSet] = []
    for item in universe:
        if item.direction_mode != target.direction_mode or item == target:
            continue
        candidate_indexes = tuple(axis.index(value) for axis, value in zip(
            axes,
            (item.range_multiplier, item.min_step_pct, item.stop_buffer_pct),
        ))
        manhattan = sum(abs(a - b) for a, b in zip(indexes, candidate_indexes))
        if manhattan == 1:
            neighbors.append(item)
    return neighbors


def split_window_ids(
    windows: Sequence[WeekendWindow],
    *,
    dev_ratio: float,
    validation_ratio: float,
    min_windows_per_split: int,
) -> SplitResult:
    if not 0 < dev_ratio < 1 or not 0 < validation_ratio < 1:
        raise ValueError("开发集和验证集比例必须在 (0, 1) 内。")
    if dev_ratio + validation_ratio >= 1:
        raise ValueError("必须为最终 OOS 保留正的时间比例。")
    ordered = sorted({window.window_id for window in windows})
    minimum = max(1, int(min_windows_per_split))
    if len(ordered) < minimum * 3:
        raise ValueError(
            f"完整周末窗口不足：{len(ordered)} < {minimum * 3}，"
            "无法建立开发/验证/最终 OOS 三段。"
        )
    dev_end = max(minimum, int(len(ordered) * dev_ratio))
    validation_end = max(dev_end + minimum, int(
        len(ordered) * (dev_ratio + validation_ratio)
    ))
    validation_end = min(validation_end, len(ordered) - minimum)
    if dev_end >= validation_end:
        raise ValueError("时间切分后验证集为空。")
    return SplitResult(
        tuple(ordered[:dev_end]),
        tuple(ordered[dev_end:validation_end]),
        tuple(ordered[validation_end:]),
    )


class RobustnessResearch:
    def __init__(
        self,
        windows: Sequence[WeekendWindow],
        parameters: Sequence[ParameterSet],
        config: ResearchConfig | None = None,
        *,
        symbol_rules: dict[str, SymbolRules] | None = None,
        dataset_metadata: Sequence[dict[str, Any]] | None = None,
    ) -> None:
        self.config = config or ResearchConfig()
        _validate_research_config(self.config)
        self.parameters = list(parameters)
        self.contexts = [
            _WindowContext(window)
            for window in windows
        ]
        self.symbol_rules = symbol_rules or DEFAULT_SYMBOL_RULES
        self.dataset_metadata = [
            {
                key: item.get(key)
                for key in (
                    "dataset_id",
                    "provider",
                    "market",
                    "symbol",
                    "interval",
                    "actual_start",
                    "actual_end",
                    "row_count",
                    "missing_ratio",
                    "file_sha256",
                    "official_checksums_verified",
                )
            }
            for item in (dataset_metadata or ())
        ]
        self.regime = RegimeEngine(RegimeConfig(
            enter_threshold=self.config.enter_threshold,
            stay_threshold=self.config.retention_threshold,
            soft_breach_limit=self.config.retention_soft_breach_limit,
        ))
        self._cache: dict[tuple[Any, ...], WindowResult] = {}
        if not any(item.window.status == "READY" for item in self.contexts):
            raise ValueError("没有可用于研究的完整周末窗口。")
        if not self.parameters:
            raise ValueError("没有参数候选。")

    def _capital_for_symbol(self, symbol: str) -> float:
        normalized = str(symbol).strip().upper()
        return float(
            self.config.capital_by_symbol.get(
                normalized,
                self.config.capital_per_symbol,
            )
        )

    def run(self) -> dict[str, Any]:
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        dev = self._contexts(split.development)
        validation = self._contexts(split.validation)
        final_oos = self._contexts(split.final_oos)
        symbols = sorted({item.window.symbol for item in self.contexts})

        dev_metrics = {
            item.parameter_id: self.evaluate(item, dev)
            for item in self.parameters
        }
        ranked = sorted(
            self.parameters,
            key=lambda item: dev_metrics[item.parameter_id].objective,
            reverse=True,
        )
        finalists = ranked[: max(1, min(self.config.selection_top_k, len(ranked)))]
        validation_metrics = {
            item.parameter_id: self.evaluate(item, validation)
            for item in self.parameters
        }
        locked = self._lock_parameter(
            finalists,
            dev_metrics,
            validation_metrics,
        )
        oos_metrics = self.evaluate(locked, final_oos)
        oos_window_results = [
            self._window_result(
                locked,
                context,
                self.config.maker_fill_probability,
            )
            for context in final_oos
        ]
        neighbor_oos = {
            item.parameter_id: self.evaluate(item, final_oos)
            for item in parameter_neighbors(locked, self.parameters)
        }
        fill_sensitivity = {
            f"{probability:.2f}": self.evaluate(
                locked,
                final_oos,
                fill_probability=probability,
            )
            for probability in (0.50, 0.65, 0.80)
        }
        walk_forward = self._walk_forward(
            self._contexts(split.development + split.validation),
        )
        gates = _stability_gates(
            oos_metrics,
            neighbor_oos,
            walk_forward["metrics"],
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "selection": (
                    "开发集全参数排序；仅前 K 名进入验证；依据开发、验证及验证集"
                    "参数邻域最弱项锁定；最终 OOS 不参与选参。"
                ),
                "oos_policy": (
                    "最终 OOS 只在参数锁定后评估一次；邻域和成交概率结果仅作"
                    "敏感性诊断，禁止据此回头改参数。"
                ),
                "window_mode": (
                    "仅 NYSE 相邻交易日间隔大于 1 天的周末/长假窗口；"
                    "普通工作日隔夜排除。"
                ),
                "lookahead_policy": (
                    "每根交易 Bar 的 Regime 分只使用该 Bar 之前的 "
                    f"{self.config.observation_rows} 根已闭合 K 线。"
                ),
            },
            "assumptions": {
                **asdict(self.config),
                "symbols": symbols,
                "funding_model": (
                    "归档 K 线不含资金费 sidecar；按固定每 8 小时费率折算为每分钟，"
                    "属于压力假设。"
                ),
                "liquidity_model": (
                    "历史 1m K 线没有盘口；点差、深度和 Maker 成交概率均为显式假设。"
                ),
                "execution_limit": (
                    "L0 保守撮合不能还原真实队列位置；结果不能视为收益承诺。"
                ),
            },
            "datasets": self.dataset_metadata,
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": _split_summary(split.final_oos),
            },
            "selected_parameter": _parameter_payload(locked),
            "development": {
                item.parameter_id: _metrics_payload(dev_metrics[item.parameter_id])
                for item in ranked
            },
            "validation": {
                item.parameter_id: _metrics_payload(
                    validation_metrics[item.parameter_id]
                )
                for item in ranked
            },
            "final_oos": _metrics_payload(oos_metrics),
            "final_oos_window_results": [
                asdict(item) for item in oos_window_results
            ],
            "final_oos_neighborhood": {
                key: _metrics_payload(value)
                for key, value in neighbor_oos.items()
            },
            "fill_probability_sensitivity": {
                key: _metrics_payload(value)
                for key, value in fill_sensitivity.items()
            },
            "walk_forward": walk_forward,
            "stability": gates,
        }

    def evaluate(
        self,
        parameter: ParameterSet,
        contexts: Sequence[_WindowContext],
        *,
        fill_probability: float | None = None,
        maker_fee_rate: float | None = None,
        taker_fee_rate: float | None = None,
        stop_slippage_bps: float | None = None,
    ) -> AggregateMetrics:
        probability = (
            self.config.maker_fill_probability
            if fill_probability is None
            else float(fill_probability)
        )
        results = [
            self._window_result(
                parameter,
                context,
                probability,
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
                stop_slippage_bps=stop_slippage_bps,
            )
            for context in contexts
        ]
        return aggregate_results(
            results,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )

    def diagnose_entry_filters(
        self,
        parameter: ParameterSet,
        filters: Sequence[EntryFilter],
        *,
        fill_seed_salt: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate stricter entry filters without reading the final OOS split."""

        if parameter not in self.parameters:
            raise ValueError("入口诊断参数不在当前研究参数集中。")
        if not filters:
            raise ValueError("入口诊断至少需要一个过滤器。")
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        candidates: list[dict[str, Any]] = []
        for entry_filter in filters:
            dev_metrics = self._evaluate_entry_filter(
                parameter,
                development,
                entry_filter,
                fill_seed_salt=fill_seed_salt,
            )
            validation_metrics = self._evaluate_entry_filter(
                parameter,
                validation,
                entry_filter,
                fill_seed_salt=fill_seed_salt,
            )
            checks = _entry_filter_checks(dev_metrics, validation_metrics)
            candidates.append({
                "filter": asdict(entry_filter),
                "filter_id": entry_filter.filter_id,
                "development": _metrics_payload(dev_metrics),
                "validation": _metrics_payload(validation_metrics),
                "checks": checks,
                "passed": all(checks.values()),
                "robust_objective": min(
                    dev_metrics.objective,
                    validation_metrics.objective,
                ),
            })
        candidates.sort(
            key=lambda item: (
                bool(item["passed"]),
                float(item["robust_objective"]),
                float(item["validation"]["objective"]),
            ),
            reverse=True,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "只用开发集和验证集诊断横盘入口硬过滤；最终 OOS 不读取、"
                    "不评估，也不参与阈值选择。"
                ),
                "selection": (
                    "候选必须在开发和验证两段同时满足正收益、Profit Factor、"
                    "最大回撤、交易覆盖率和收益集中度门槛。"
                ),
            },
            "parameter": _parameter_payload(parameter),
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "max_inventory_notional": self.config.max_inventory_notional,
                "wind_down_reprice_interval_bars": (
                    self.config.wind_down_reprice_interval_bars
                ),
                "wind_down_initial_offset_steps": (
                    self.config.wind_down_initial_offset_steps
                ),
                "wind_down_unwind_fraction": (
                    self.config.wind_down_unwind_fraction
                ),
                "max_unpaired_lots_per_side": (
                    self.config.max_unpaired_lots_per_side
                ),
                "reduce_target_step_fraction": (
                    self.config.reduce_target_step_fraction
                ),
                "unpaired_lot_cap_enforcement": (
                    self.config.unpaired_lot_cap_enforcement
                ),
                "maker_fee_rate": self.config.maker_fee_rate,
                "taker_fee_rate": self.config.taker_fee_rate,
                "stop_slippage_bps": self.config.stop_slippage_bps,
                "fill_seed_salt": fill_seed_salt,
            },
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "max_inventory_notional": self.config.max_inventory_notional,
                "wind_down_reprice_interval_bars": (
                    self.config.wind_down_reprice_interval_bars
                ),
                "wind_down_initial_offset_steps": (
                    self.config.wind_down_initial_offset_steps
                ),
                "wind_down_unwind_fraction": (
                    self.config.wind_down_unwind_fraction
                ),
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "candidate_count": len(candidates),
            "passed_count": sum(bool(item["passed"]) for item in candidates),
            "candidates": candidates,
        }

    def diagnose_exit_policies(
        self,
        parameter: ParameterSet,
        wind_down_values: Sequence[int],
    ) -> dict[str, Any]:
        """Compare passive wind-down policies without evaluating final OOS."""

        if parameter not in self.parameters:
            raise ValueError("离场诊断参数不在当前研究参数集中。")
        values = sorted({int(value) for value in wind_down_values})
        if not values or any(value < 0 for value in values):
            raise ValueError("wind_down_values 必须是非负整数集合。")
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        candidates: list[dict[str, Any]] = []
        for wind_down_bars in values:
            dev_metrics = self._evaluate_exit_policy(
                parameter,
                development,
                wind_down_bars,
            )
            validation_metrics = self._evaluate_exit_policy(
                parameter,
                validation,
                wind_down_bars,
            )
            checks = _entry_filter_checks(dev_metrics, validation_metrics)
            candidates.append({
                "wind_down_bars": wind_down_bars,
                "development": _metrics_payload(dev_metrics),
                "validation": _metrics_payload(validation_metrics),
                "checks": checks,
                "passed": all(checks.values()),
                "robust_objective": min(
                    dev_metrics.objective,
                    validation_metrics.objective,
                ),
            })
        candidates.sort(
            key=lambda item: (
                bool(item["passed"]),
                float(item["robust_objective"]),
                float(item["validation"]["objective"]),
            ),
            reverse=True,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "只用开发集和验证集比较终场前停止新增库存的时间；"
                    "最终 OOS 不读取、不评估。"
                ),
                "fixed_variables": (
                    "网格参数、准入门槛、费用、成交概率与风险预算保持不变。"
                ),
            },
            "parameter": _parameter_payload(parameter),
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "candidate_count": len(candidates),
            "passed_count": sum(bool(item["passed"]) for item in candidates),
            "candidates": candidates,
        }

    def diagnose_wind_down_maker(
        self,
        parameter: ParameterSet,
        policies: Sequence[WindDownMakerPolicy],
    ) -> dict[str, Any]:
        """Compare progressive maker inventory unwind with final OOS sealed."""

        if parameter not in self.parameters:
            raise ValueError("Maker 去库存诊断参数不在当前研究参数集中。")
        if self.config.wind_down_bars <= 0:
            raise ValueError("Maker 去库存诊断要求 wind_down_bars 大于 0。")
        values = list(dict.fromkeys(policies))
        if not values:
            raise ValueError("Maker 去库存诊断至少需要一条策略。")
        if any(item.reprice_interval_bars < 1 for item in values):
            raise ValueError("reprice_interval_bars 必须为正整数。")
        if any(item.initial_offset_steps < 0 for item in values):
            raise ValueError("initial_offset_steps 不能为负。")
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        baseline_dev, baseline_dev_ops = self._evaluate_wind_down_maker_policy(
            parameter,
            development,
            None,
        )
        baseline_validation, baseline_validation_ops = (
            self._evaluate_wind_down_maker_policy(
                parameter,
                validation,
                None,
            )
        )
        candidates: list[dict[str, Any]] = []
        for policy in values:
            dev_metrics, dev_ops = self._evaluate_wind_down_maker_policy(
                parameter,
                development,
                policy,
            )
            validation_metrics, validation_ops = (
                self._evaluate_wind_down_maker_policy(
                    parameter,
                    validation,
                    policy,
                )
            )
            checks = _entry_filter_checks(dev_metrics, validation_metrics)
            candidates.append({
                "policy": asdict(policy),
                "policy_id": policy.policy_id,
                "development": _metrics_payload(dev_metrics),
                "validation": _metrics_payload(validation_metrics),
                "development_operations": dev_ops,
                "validation_operations": validation_ops,
                "checks": checks,
                "passed": all(checks.values()),
                "robust_objective": min(
                    dev_metrics.objective,
                    validation_metrics.objective,
                ),
            })
        candidates.sort(
            key=lambda item: (
                bool(item["passed"]),
                float(item["robust_objective"]),
                float(item["validation"]["objective"]),
            ),
            reverse=True,
        )
        selected_policy = next(
            item
            for item in values
            if item.policy_id == candidates[0]["policy_id"]
        )
        fill_sensitivity: dict[str, Any] = {}
        for probability in (0.50, 0.65, 0.80):
            dev_metrics, dev_ops = self._evaluate_wind_down_maker_policy(
                parameter,
                development,
                selected_policy,
                fill_probability=probability,
            )
            validation_metrics, validation_ops = (
                self._evaluate_wind_down_maker_policy(
                    parameter,
                    validation,
                    selected_policy,
                    fill_probability=probability,
                )
            )
            checks = _entry_filter_checks(dev_metrics, validation_metrics)
            fill_sensitivity[f"{probability:.2f}"] = {
                "development": _metrics_payload(dev_metrics),
                "validation": _metrics_payload(validation_metrics),
                "development_operations": dev_ops,
                "validation_operations": validation_ops,
                "checks": checks,
                "passed": all(checks.values()),
            }
        cost_sensitivity: dict[str, Any] = {}
        cost_scenarios = {
            "BASE": (0.0002, 0.0005, 10.0),
            "COST_25": (0.00025, 0.000625, 15.0),
            "COST_50": (0.0003, 0.00075, 20.0),
        }
        for name, (maker_fee, taker_fee, slippage_bps) in cost_scenarios.items():
            dev_metrics, dev_ops = self._evaluate_wind_down_maker_policy(
                parameter,
                development,
                selected_policy,
                maker_fee_rate=maker_fee,
                taker_fee_rate=taker_fee,
                stop_slippage_bps=slippage_bps,
            )
            validation_metrics, validation_ops = (
                self._evaluate_wind_down_maker_policy(
                    parameter,
                    validation,
                    selected_policy,
                    maker_fee_rate=maker_fee,
                    taker_fee_rate=taker_fee,
                    stop_slippage_bps=slippage_bps,
                )
            )
            checks = _entry_filter_checks(dev_metrics, validation_metrics)
            cost_sensitivity[name] = {
                "maker_fee_rate": maker_fee,
                "taker_fee_rate": taker_fee,
                "stop_slippage_bps": slippage_bps,
                "development": _metrics_payload(dev_metrics),
                "validation": _metrics_payload(validation_metrics),
                "development_operations": dev_ops,
                "validation_operations": validation_ops,
                "checks": checks,
                "passed": all(checks.values()),
            }
        walk_forward = self._walk_forward_wind_down(
            parameter,
            values,
            development + validation,
        )
        robustness_checks = {
            "policy_platform_majority_pass": (
                sum(bool(item["passed"]) for item in candidates)
                / len(candidates)
                >= 0.50
            ),
            "fill_sensitivity_all_pass": all(
                item["passed"] for item in fill_sensitivity.values()
            ),
            "cost_sensitivity_all_pass": all(
                item["passed"] for item in cost_sensitivity.values()
            ),
            "walk_forward_positive": walk_forward["metrics"]["total_pnl"] > 0,
            "walk_forward_profit_factor": (
                walk_forward["metrics"]["profit_factor"] is not None
                and walk_forward["metrics"]["profit_factor"] >= 1.05
            ),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "终场停止新增库存后，把剩余减仓单按当前已闭合 Bar 重挂到"
                    "市场附近，尽量以 Maker 完成去库存；最终 OOS 不读取、不评估。"
                ),
                "lookahead_policy": (
                    "重挂价格仅使用当前 Bar 收盘价，并从下一根 Bar 起才允许成交。"
                ),
                "execution": (
                    "重挂单继续使用 Maker 成交概率和单 Bar 最大成交笔数假设；"
                    "强制离场只处理最终剩余库存。"
                ),
            },
            "parameter": _parameter_payload(parameter),
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "max_inventory_notional": self.config.max_inventory_notional,
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "baseline": {
                "development": _metrics_payload(baseline_dev),
                "validation": _metrics_payload(baseline_validation),
                "development_operations": baseline_dev_ops,
                "validation_operations": baseline_validation_ops,
            },
            "selected_policy": asdict(selected_policy) | {
                "policy_id": selected_policy.policy_id,
            },
            "fill_probability_sensitivity": fill_sensitivity,
            "cost_sensitivity": cost_sensitivity,
            "walk_forward": walk_forward,
            "robustness": {
                "checks": robustness_checks,
                "passed": all(robustness_checks.values()),
            },
            "candidate_count": len(candidates),
            "passed_count": sum(bool(item["passed"]) for item in candidates),
            "candidates": candidates,
        }

    def diagnose_fill_seeds(
        self,
        parameter: ParameterSet,
        policy: WindDownMakerPolicy,
        seed_salts: Sequence[int],
        *,
        entry_filter: EntryFilter | None = None,
    ) -> dict[str, Any]:
        """Stress the locked policy across deterministic fill seed salts."""

        if parameter not in self.parameters:
            raise ValueError("多 seed 诊断参数不在当前研究参数集中。")
        if self.config.wind_down_bars <= 0:
            raise ValueError("多 seed 诊断要求 wind_down_bars 大于 0。")
        salts = list(dict.fromkeys(int(value) for value in seed_salts))
        if not salts:
            raise ValueError("多 seed 诊断至少需要一个 seed salt。")
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        cost_scenarios = {
            "BASE": (0.0002, 0.0005, 10.0),
            "COST_25": (0.00025, 0.000625, 15.0),
            "COST_50": (0.0003, 0.00075, 20.0),
        }
        seed_results: list[dict[str, Any]] = []
        for salt in salts:
            scenarios: dict[str, Any] = {}
            for name, (maker_fee, taker_fee, slippage_bps) in cost_scenarios.items():
                dev_metrics, dev_ops = self._evaluate_wind_down_maker_policy(
                    parameter,
                    development,
                    policy,
                    maker_fee_rate=maker_fee,
                    taker_fee_rate=taker_fee,
                    stop_slippage_bps=slippage_bps,
                    fill_seed_salt=salt,
                    entry_filter=entry_filter,
                )
                validation_metrics, validation_ops = (
                    self._evaluate_wind_down_maker_policy(
                        parameter,
                        validation,
                        policy,
                        maker_fee_rate=maker_fee,
                        taker_fee_rate=taker_fee,
                        stop_slippage_bps=slippage_bps,
                        fill_seed_salt=salt,
                        entry_filter=entry_filter,
                    )
                )
                checks = _entry_filter_checks(dev_metrics, validation_metrics)
                scenarios[name] = {
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "stop_slippage_bps": slippage_bps,
                    "development": _metrics_payload(dev_metrics),
                    "validation": _metrics_payload(validation_metrics),
                    "development_operations": dev_ops,
                    "validation_operations": validation_ops,
                    "checks": checks,
                    "passed": all(checks.values()),
                }
            all_metrics = [
                scenario[split_name]
                for scenario in scenarios.values()
                for split_name in ("development", "validation")
            ]
            seed_results.append({
                "seed_salt": salt,
                "scenarios": scenarios,
                "base_passed": bool(scenarios["BASE"]["passed"]),
                "all_cost_scenarios_passed": all(
                    bool(item["passed"]) for item in scenarios.values()
                ),
                "worst_split_pnl": min(
                    float(item["total_pnl"]) for item in all_metrics
                ),
                "max_drawdown_pct": max(
                    float(item["max_drawdown_pct"]) for item in all_metrics
                ),
            })

        required_pass_rate = 0.70
        scenario_summary = {
            name: _seed_scenario_summary(seed_results, name)
            for name in cost_scenarios
        }
        base_pass_rate = sum(
            bool(item["base_passed"]) for item in seed_results
        ) / len(seed_results)
        all_cost_pass_rate = sum(
            bool(item["all_cost_scenarios_passed"]) for item in seed_results
        ) / len(seed_results)
        worst_seed = min(
            seed_results,
            key=lambda item: (
                float(item["worst_split_pnl"]),
                -float(item["max_drawdown_pct"]),
            ),
        )
        checks = {
            "base_seed_majority_pass": base_pass_rate >= required_pass_rate,
            "each_cost_scenario_majority_pass": all(
                float(item["pass_rate"]) >= required_pass_rate
                for item in scenario_summary.values()
            ),
            "all_cost_seed_majority_pass": (
                all_cost_pass_rate >= required_pass_rate
            ),
            "worst_seed_no_catastrophic_drawdown": (
                float(worst_seed["max_drawdown_pct"]) <= 0.05
            ),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "固定网格与 Maker 去库存策略，仅改变确定性撮合 seed salt；"
                    "最终 OOS 不读取、不评估。"
                ),
                "seed_identity": (
                    "salt 只参与 fill_probability_seed；订单语义身份仍由方向、"
                    "价格、入场价、position side、intent 和阶段组成。"
                ),
                "required_pass_rate": required_pass_rate,
                "catastrophic_drawdown_limit_pct": 0.05,
            },
            "parameter": _parameter_payload(parameter),
            "entry_filter": (
                asdict(entry_filter) | {"filter_id": entry_filter.filter_id}
                if entry_filter is not None
                else None
            ),
            "policy": asdict(policy) | {"policy_id": policy.policy_id},
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "max_inventory_notional": self.config.max_inventory_notional,
                "maker_fill_probability": self.config.maker_fill_probability,
                "max_unpaired_lots_per_side": (
                    self.config.max_unpaired_lots_per_side
                ),
                "reduce_target_step_fraction": (
                    self.config.reduce_target_step_fraction
                ),
                "unpaired_lot_cap_enforcement": (
                    self.config.unpaired_lot_cap_enforcement
                ),
                "capital_by_symbol": {
                    symbol: self._capital_for_symbol(symbol)
                    for symbol in sorted({item.window.symbol for item in self.contexts})
                },
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "seed_salts": salts,
            "scenario_summary": scenario_summary,
            "summary": {
                "seed_count": len(seed_results),
                "base_pass_rate": base_pass_rate,
                "all_cost_pass_rate": all_cost_pass_rate,
                "worst_seed_salt": worst_seed["seed_salt"],
                "worst_split_pnl": worst_seed["worst_split_pnl"],
                "worst_seed_max_drawdown_pct": worst_seed["max_drawdown_pct"],
                "checks": checks,
                "passed": all(checks.values()),
            },
            "seeds": seed_results,
        }

    def diagnose_joint_fill_seeds(
        self,
        symbol_policies: dict[str, SymbolResearchPolicy],
        policy: WindDownMakerPolicy,
        seed_salts: Sequence[int],
    ) -> dict[str, Any]:
        """Evaluate symbol-specific policies as one conservative portfolio."""

        normalized_policies = {
            str(symbol).strip().upper(): item
            for symbol, item in symbol_policies.items()
        }
        symbols = sorted({item.window.symbol for item in self.contexts})
        if set(normalized_policies) != set(symbols):
            raise ValueError(
                "联合多 seed 诊断必须为全部标的提供且只提供一份策略。"
            )
        if any(item.parameter not in self.parameters for item in normalized_policies.values()):
            raise ValueError("联合多 seed 诊断包含研究参数集之外的参数。")
        if any(item.max_inventory_notional <= 0 for item in normalized_policies.values()):
            raise ValueError("联合多 seed 诊断的单标的库存上限必须为正。")
        if any(
            item.max_unpaired_lots_per_side is not None
            and item.max_unpaired_lots_per_side < 0
            for item in normalized_policies.values()
        ):
            raise ValueError("联合多 seed 诊断的单标的未配对库存上限不能为负。")
        if any(
            item.reduce_target_step_fraction is not None
            and not 0 < item.reduce_target_step_fraction <= 1
            for item in normalized_policies.values()
        ):
            raise ValueError("联合多 seed 诊断的单标的减仓目标比例必须在 (0, 1] 内。")
        if self.config.wind_down_bars <= 0:
            raise ValueError("联合多 seed 诊断要求 wind_down_bars 大于 0。")
        salts = list(dict.fromkeys(int(value) for value in seed_salts))
        if not salts:
            raise ValueError("联合多 seed 诊断至少需要一个 seed salt。")

        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        cost_scenarios = {
            "BASE": (0.0002, 0.0005, 10.0),
            "COST_25": (0.00025, 0.000625, 15.0),
            "COST_50": (0.0003, 0.00075, 20.0),
        }
        seed_results: list[dict[str, Any]] = []
        for salt in salts:
            scenarios: dict[str, Any] = {}
            for name, (maker_fee, taker_fee, slippage_bps) in cost_scenarios.items():
                dev_metrics, dev_ops = self._evaluate_joint_symbol_policy(
                    development,
                    normalized_policies,
                    policy,
                    maker_fee_rate=maker_fee,
                    taker_fee_rate=taker_fee,
                    stop_slippage_bps=slippage_bps,
                    fill_seed_salt=salt,
                )
                validation_metrics, validation_ops = self._evaluate_joint_symbol_policy(
                    validation,
                    normalized_policies,
                    policy,
                    maker_fee_rate=maker_fee,
                    taker_fee_rate=taker_fee,
                    stop_slippage_bps=slippage_bps,
                    fill_seed_salt=salt,
                )
                checks = _entry_filter_checks(dev_metrics, validation_metrics)
                checks.update({
                    f"development_{check_name}": passed
                    for check_name, passed in _symbol_metric_checks(
                        dev_ops["symbol_metrics"]
                    ).items()
                })
                checks.update({
                    f"validation_{check_name}": passed
                    for check_name, passed in _symbol_metric_checks(
                        validation_ops["symbol_metrics"]
                    ).items()
                })
                scenarios[name] = {
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "stop_slippage_bps": slippage_bps,
                    "development": _metrics_payload(dev_metrics),
                    "validation": _metrics_payload(validation_metrics),
                    "development_operations": dev_ops,
                    "validation_operations": validation_ops,
                    "checks": checks,
                    "passed": all(checks.values()),
                }
            all_metrics = [
                scenario[split_name]
                for scenario in scenarios.values()
                for split_name in ("development", "validation")
            ]
            seed_results.append({
                "seed_salt": salt,
                "scenarios": scenarios,
                "base_passed": bool(scenarios["BASE"]["passed"]),
                "all_cost_scenarios_passed": all(
                    bool(item["passed"]) for item in scenarios.values()
                ),
                "worst_split_pnl": min(
                    float(item["total_pnl"]) for item in all_metrics
                ),
                "max_drawdown_pct": max(
                    float(item["max_drawdown_pct"]) for item in all_metrics
                ),
            })

        required_pass_rate = 0.70
        scenario_summary = {
            name: _seed_scenario_summary(seed_results, name)
            for name in cost_scenarios
        }
        base_pass_rate = sum(
            bool(item["base_passed"]) for item in seed_results
        ) / len(seed_results)
        all_cost_pass_rate = sum(
            bool(item["all_cost_scenarios_passed"]) for item in seed_results
        ) / len(seed_results)
        worst_seed = min(
            seed_results,
            key=lambda item: (
                float(item["worst_split_pnl"]),
                -float(item["max_drawdown_pct"]),
            ),
        )
        checks = {
            "base_seed_majority_pass": base_pass_rate >= required_pass_rate,
            "each_cost_scenario_majority_pass": all(
                float(item["pass_rate"]) >= required_pass_rate
                for item in scenario_summary.values()
            ),
            "all_cost_seed_majority_pass": all_cost_pass_rate >= required_pass_rate,
            "worst_seed_no_catastrophic_drawdown": (
                float(worst_seed["max_drawdown_pct"]) <= 0.05
            ),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "按标的锁定独立参数与入口过滤器，按相同 seed 和费用场景"
                    "合并 BTC/ETH；最终 OOS 不读取、不评估。"
                ),
                "aggregation": (
                    "总盈亏按标的相加；PF 合并未抵消的毛利/毛损；"
                    "回撤使用各标的回撤之和；集中度使用组合与单标的最大值。"
                ),
                "required_pass_rate": required_pass_rate,
                "catastrophic_drawdown_limit_pct": 0.05,
            },
            "symbol_policies": {
                symbol: {
                    "parameter": _parameter_payload(item.parameter),
                    "entry_filter": (
                        asdict(item.entry_filter) | {
                            "filter_id": item.entry_filter.filter_id,
                        }
                        if item.entry_filter is not None
                        else None
                    ),
                    "capital": self._capital_for_symbol(symbol),
                    "max_inventory_notional": item.max_inventory_notional,
                    "max_unpaired_lots_per_side": (
                        self.config.max_unpaired_lots_per_side
                        if item.max_unpaired_lots_per_side is None
                        else item.max_unpaired_lots_per_side
                    ),
                    "reduce_target_step_fraction": (
                        self.config.reduce_target_step_fraction
                        if item.reduce_target_step_fraction is None
                        else item.reduce_target_step_fraction
                    ),
                }
                for symbol, item in sorted(normalized_policies.items())
            },
            "policy": asdict(policy) | {"policy_id": policy.policy_id},
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "maker_fill_probability": self.config.maker_fill_probability,
                "capital_by_symbol": {
                    symbol: self._capital_for_symbol(symbol)
                    for symbol in symbols
                },
                "max_unpaired_lots_per_side": self.config.max_unpaired_lots_per_side,
                "reduce_target_step_fraction": self.config.reduce_target_step_fraction,
                "unpaired_lot_cap_enforcement": self.config.unpaired_lot_cap_enforcement,
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "seed_salts": salts,
            "scenario_summary": scenario_summary,
            "summary": {
                "seed_count": len(seed_results),
                "base_pass_rate": base_pass_rate,
                "all_cost_pass_rate": all_cost_pass_rate,
                "worst_seed_salt": worst_seed["seed_salt"],
                "worst_split_pnl": worst_seed["worst_split_pnl"],
                "worst_seed_max_drawdown_pct": worst_seed["max_drawdown_pct"],
                "checks": checks,
                "passed": all(checks.values()),
            },
            "seeds": seed_results,
        }

    def evaluate_locked_joint_oos(
        self,
        symbol_policies: dict[str, SymbolResearchPolicy],
        policy: WindDownMakerPolicy,
        seed_salts: Sequence[int],
        *,
        lock_report_sha256: str,
        lock_report_generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Evaluate one previously locked BTC/ETH policy on final OOS only."""

        normalized_policies = {
            str(symbol).strip().upper(): item
            for symbol, item in symbol_policies.items()
        }
        symbols = sorted({item.window.symbol for item in self.contexts})
        if set(normalized_policies) != set(symbols):
            raise ValueError("最终 OOS 必须为全部标的提供且只提供一份锁定策略。")
        if any(item.parameter not in self.parameters for item in normalized_policies.values()):
            raise ValueError("最终 OOS 包含研究参数集之外的参数。")
        salts = list(dict.fromkeys(int(value) for value in seed_salts))
        if not salts:
            raise ValueError("最终 OOS 至少需要一个 seed salt。")
        lock_hash = str(lock_report_sha256).strip().lower()
        if len(lock_hash) != 64 or any(character not in "0123456789abcdef" for character in lock_hash):
            raise ValueError("锁定报告 SHA-256 格式无效。")

        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        final_oos = self._contexts(split.final_oos)
        seed_results: list[dict[str, Any]] = []
        for salt in salts:
            scenarios: dict[str, Any] = {}
            for name, (maker_fee, taker_fee, slippage_bps) in JOINT_COST_SCENARIOS.items():
                metrics, operations = self._evaluate_joint_symbol_policy(
                    final_oos,
                    normalized_policies,
                    policy,
                    maker_fee_rate=maker_fee,
                    taker_fee_rate=taker_fee,
                    stop_slippage_bps=slippage_bps,
                    fill_seed_salt=salt,
                )
                checks = _joint_oos_checks(metrics, operations["symbol_metrics"])
                scenarios[name] = {
                    "maker_fee_rate": maker_fee,
                    "taker_fee_rate": taker_fee,
                    "stop_slippage_bps": slippage_bps,
                    "final_oos": _metrics_payload(metrics),
                    "operations": operations,
                    "checks": checks,
                    "passed": all(checks.values()),
                }
            seed_results.append({
                "seed_salt": salt,
                "scenarios": scenarios,
                "base_passed": bool(scenarios["BASE"]["passed"]),
                "all_cost_scenarios_passed": all(
                    bool(item["passed"]) for item in scenarios.values()
                ),
                "worst_oos_pnl": min(
                    float(item["final_oos"]["total_pnl"])
                    for item in scenarios.values()
                ),
                "max_drawdown_pct": max(
                    float(item["final_oos"]["max_drawdown_pct"])
                    for item in scenarios.values()
                ),
            })

        required_pass_rate = 0.70
        scenario_summary = {
            name: {
                "pass_rate": sum(
                    bool(item["scenarios"][name]["passed"])
                    for item in seed_results
                ) / len(seed_results),
                "median_pnl": statistics.median(
                    float(item["scenarios"][name]["final_oos"]["total_pnl"])
                    for item in seed_results
                ),
                "worst_pnl": min(
                    float(item["scenarios"][name]["final_oos"]["total_pnl"])
                    for item in seed_results
                ),
            }
            for name in JOINT_COST_SCENARIOS
        }
        base_pass_rate = sum(bool(item["base_passed"]) for item in seed_results) / len(seed_results)
        all_cost_pass_rate = sum(
            bool(item["all_cost_scenarios_passed"]) for item in seed_results
        ) / len(seed_results)
        worst_seed = min(
            seed_results,
            key=lambda item: (
                float(item["worst_oos_pnl"]),
                -float(item["max_drawdown_pct"]),
            ),
        )
        checks = {
            "base_seed_majority_pass": base_pass_rate >= required_pass_rate,
            "each_cost_scenario_majority_pass": all(
                float(item["pass_rate"]) >= required_pass_rate
                for item in scenario_summary.values()
            ),
            "all_cost_seed_majority_pass": all_cost_pass_rate >= required_pass_rate,
            "worst_seed_no_catastrophic_drawdown": (
                float(worst_seed["max_drawdown_pct"]) <= 0.05
            ),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": "锁定开发/验证策略后，对最终 OOS 执行一次组合与逐标的稳健性验证。",
                "required_pass_rate": required_pass_rate,
                "final_oos_reuse_for_tuning_forbidden": True,
            },
            "lock_report": {
                "sha256": lock_hash,
                "generated_at": lock_report_generated_at,
            },
            "datasets": self.dataset_metadata,
            "symbol_policies": {
                symbol: {
                    "parameter": _parameter_payload(item.parameter),
                    "entry_filter": (
                        asdict(item.entry_filter) | {"filter_id": item.entry_filter.filter_id}
                        if item.entry_filter is not None
                        else None
                    ),
                    "capital": self._capital_for_symbol(symbol),
                    "max_inventory_notional": item.max_inventory_notional,
                    "max_unpaired_lots_per_side": (
                        self.config.max_unpaired_lots_per_side
                        if item.max_unpaired_lots_per_side is None
                        else item.max_unpaired_lots_per_side
                    ),
                    "reduce_target_step_fraction": (
                        self.config.reduce_target_step_fraction
                        if item.reduce_target_step_fraction is None
                        else item.reduce_target_step_fraction
                    ),
                }
                for symbol, item in sorted(normalized_policies.items())
            },
            "policy": asdict(policy) | {"policy_id": policy.policy_id},
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": _split_summary(split.final_oos) | {"status": "EVALUATED_ONCE"},
            },
            "seed_salts": salts,
            "scenario_summary": scenario_summary,
            "summary": {
                "seed_count": len(seed_results),
                "base_pass_rate": base_pass_rate,
                "all_cost_pass_rate": all_cost_pass_rate,
                "worst_seed_salt": worst_seed["seed_salt"],
                "worst_oos_pnl": worst_seed["worst_oos_pnl"],
                "worst_seed_max_drawdown_pct": worst_seed["max_drawdown_pct"],
                "checks": checks,
                "passed": all(checks.values()),
            },
            "seeds": seed_results,
        }

    def diagnose_inventory_policies(
        self,
        parameter: ParameterSet,
        max_inventory_values: Sequence[float],
    ) -> dict[str, Any]:
        """Compare inventory caps on development/validation with final OOS sealed."""

        if parameter not in self.parameters:
            raise ValueError("库存诊断参数不在当前研究参数集中。")
        values = sorted({float(value) for value in max_inventory_values})
        if not values or any(value <= 0 for value in values):
            raise ValueError("max_inventory_values 必须是正数集合。")
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        candidates: list[dict[str, Any]] = []
        for inventory_cap in values:
            dev_metrics = self._evaluate_inventory_policy(
                parameter,
                development,
                inventory_cap,
            )
            validation_metrics = self._evaluate_inventory_policy(
                parameter,
                validation,
                inventory_cap,
            )
            checks = _entry_filter_checks(dev_metrics, validation_metrics)
            candidates.append({
                "max_inventory_notional": inventory_cap,
                "development": _metrics_payload(dev_metrics),
                "validation": _metrics_payload(validation_metrics),
                "checks": checks,
                "passed": all(checks.values()),
                "robust_objective": min(
                    dev_metrics.objective,
                    validation_metrics.objective,
                ),
            })
        candidates.sort(
            key=lambda item: (
                bool(item["passed"]),
                float(item["robust_objective"]),
                float(item["validation"]["objective"]),
            ),
            reverse=True,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "固定网格与终场政策，只使用开发/验证比较库存名义上限；"
                    "最终 OOS 不读取、不评估。"
                ),
                "inventory_behavior": (
                    "CAUTION 取消增加同向库存的开仓单；CRITICAL 才安全退出。"
                ),
            },
            "parameter": _parameter_payload(parameter),
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "inventory_caution_utilization": (
                    self.config.inventory_caution_utilization
                ),
                "inventory_critical_utilization": (
                    self.config.inventory_critical_utilization
                ),
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "candidate_count": len(candidates),
            "passed_count": sum(bool(item["passed"]) for item in candidates),
            "candidates": candidates,
        }

    def diagnose_dynamic_modes(
        self,
        parameter: ParameterSet,
        rules: Sequence[DynamicModeRule],
    ) -> dict[str, Any]:
        """Select LONG/SHORT/NEUTRAL from pre-entry history with OOS sealed."""

        mode_parameters = self._dynamic_mode_parameters(parameter)
        values = list(dict.fromkeys(rules))
        if not values:
            raise ValueError("动态方向诊断至少需要一条规则。")
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        candidates: list[dict[str, Any]] = []
        for rule in values:
            dev_metrics, dev_modes = self._evaluate_dynamic_mode_rule(
                mode_parameters,
                development,
                rule,
            )
            validation_metrics, validation_modes = self._evaluate_dynamic_mode_rule(
                mode_parameters,
                validation,
                rule,
            )
            checks = _entry_filter_checks(dev_metrics, validation_metrics)
            candidates.append({
                "rule": asdict(rule),
                "rule_id": rule.rule_id,
                "development": _metrics_payload(dev_metrics),
                "validation": _metrics_payload(validation_metrics),
                "development_modes": dev_modes,
                "validation_modes": validation_modes,
                "checks": checks,
                "passed": all(checks.values()),
                "robust_objective": min(
                    dev_metrics.objective,
                    validation_metrics.objective,
                ),
            })
        candidates.sort(
            key=lambda item: (
                bool(item["passed"]),
                float(item["robust_objective"]),
                float(item["validation"]["objective"]),
            ),
            reverse=True,
        )
        fixed_mode_baselines = {}
        for mode, mode_parameter in mode_parameters.items():
            fixed_mode_baselines[mode.value] = {
                "development": _metrics_payload(
                    self.evaluate(mode_parameter, development)
                ),
                "validation": _metrics_payload(
                    self.evaluate(mode_parameter, validation)
                ),
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "只用决策时已经闭合的长周期 K 线选择 LONG/SHORT/NEUTRAL；"
                    "最终 OOS 不读取、不评估。"
                ),
                "classification": (
                    "长周期累计对数收益除以路径实现波动得到趋势比率；"
                    "趋势强且分段方向一致时选择方向网格，低趋势比率选择中性，"
                    "模糊阶段暂停。"
                ),
                "lookahead_policy": (
                    "分类输入止于网格观察期最后一根已闭合 K 线；"
                    "窗口后续涨跌不参与方向选择。"
                ),
            },
            "parameter_geometry": {
                "range_multiplier": parameter.range_multiplier,
                "min_step_pct": parameter.min_step_pct,
                "stop_buffer_pct": parameter.stop_buffer_pct,
            },
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "max_inventory_notional": self.config.max_inventory_notional,
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "candidate_count": len(candidates),
            "passed_count": sum(bool(item["passed"]) for item in candidates),
            "fixed_mode_baselines": fixed_mode_baselines,
            "candidates": candidates,
        }

    def diagnose_parameters(self) -> dict[str, Any]:
        """Rank grid parameters on development/validation while final OOS stays sealed."""

        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )
        development = self._contexts(split.development)
        validation = self._contexts(split.validation)
        dev_metrics = {
            item.parameter_id: self.evaluate(item, development)
            for item in self.parameters
        }
        validation_metrics = {
            item.parameter_id: self.evaluate(item, validation)
            for item in self.parameters
        }
        candidates: list[dict[str, Any]] = []
        for parameter in self.parameters:
            neighbors = parameter_neighbors(parameter, self.parameters)
            dev_neighbor_metrics = [
                dev_metrics[item.parameter_id]
                for item in neighbors
            ]
            validation_neighbor_metrics = [
                validation_metrics[item.parameter_id]
                for item in neighbors
            ]
            checks = _entry_filter_checks(
                dev_metrics[parameter.parameter_id],
                validation_metrics[parameter.parameter_id],
            )
            checks.update({
                "development_neighbor_majority_positive": (
                    bool(dev_neighbor_metrics)
                    and sum(item.total_pnl > 0 for item in dev_neighbor_metrics)
                    / len(dev_neighbor_metrics)
                    >= 0.50
                ),
                "validation_neighbor_majority_positive": (
                    bool(validation_neighbor_metrics)
                    and sum(item.total_pnl > 0 for item in validation_neighbor_metrics)
                    / len(validation_neighbor_metrics)
                    >= 0.50
                ),
            })
            neighbor_objectives = [
                item.objective
                for item in validation_neighbor_metrics
            ]
            neighborhood_objective = (
                statistics.median(neighbor_objectives)
                if neighbor_objectives
                else validation_metrics[parameter.parameter_id].objective
            )
            candidates.append({
                "parameter": _parameter_payload(parameter),
                "development": _metrics_payload(dev_metrics[parameter.parameter_id]),
                "validation": _metrics_payload(
                    validation_metrics[parameter.parameter_id]
                ),
                "development_neighbor_positive_ratio": (
                    sum(item.total_pnl > 0 for item in dev_neighbor_metrics)
                    / len(dev_neighbor_metrics)
                    if dev_neighbor_metrics else 0.0
                ),
                "validation_neighbor_positive_ratio": (
                    sum(item.total_pnl > 0 for item in validation_neighbor_metrics)
                    / len(validation_neighbor_metrics)
                    if validation_neighbor_metrics else 0.0
                ),
                "validation_neighborhood_median_objective": neighborhood_objective,
                "checks": checks,
                "passed": all(checks.values()),
                "robust_objective": min(
                    dev_metrics[parameter.parameter_id].objective,
                    validation_metrics[parameter.parameter_id].objective,
                    neighborhood_objective,
                ),
            })
        candidates.sort(
            key=lambda item: (
                bool(item["passed"]),
                float(item["robust_objective"]),
                float(item["validation"]["objective"]),
            ),
            reverse=True,
        )
        walk_forward = self._walk_forward(development + validation)
        robustness_checks = {
            "parameter_candidate_passed": any(
                item["passed"] for item in candidates
            ),
            "walk_forward_positive": walk_forward["metrics"]["total_pnl"] > 0,
            "walk_forward_profit_factor": (
                walk_forward["metrics"]["profit_factor"] is not None
                and walk_forward["metrics"]["profit_factor"] >= 1.05
            ),
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "在固定终场库存政策下，只使用开发集和验证集选择网格参数；"
                    "最终 OOS 不读取、不评估。"
                ),
                "selection": (
                    "开发与验证均须通过收益、PF、回撤、覆盖和集中度门槛，"
                    "且两个时间段的相邻参数多数为正。"
                ),
            },
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "max_inventory_notional": self.config.max_inventory_notional,
                "wind_down_reprice_interval_bars": (
                    self.config.wind_down_reprice_interval_bars
                ),
                "wind_down_initial_offset_steps": (
                    self.config.wind_down_initial_offset_steps
                ),
                "wind_down_unwind_fraction": (
                    self.config.wind_down_unwind_fraction
                ),
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "candidate_count": len(candidates),
            "passed_count": sum(bool(item["passed"]) for item in candidates),
            "walk_forward": walk_forward,
            "robustness": {
                "checks": robustness_checks,
                "passed": all(robustness_checks.values()),
            },
            "candidates": candidates,
        }

    def diagnose_window_paths(
        self,
        parameter: ParameterSet,
        *,
        fill_seed_salt: int | None = None,
    ) -> dict[str, Any]:
        """Expose per-window loss mechanics for development/validation only."""

        if parameter not in self.parameters:
            raise ValueError("窗口诊断参数不在当前研究参数集中。")
        split = split_window_ids(
            [item.window for item in self.contexts],
            dev_ratio=self.config.dev_ratio,
            validation_ratio=self.config.validation_ratio,
            min_windows_per_split=self.config.min_windows_per_split,
        )

        def segment_payload(window_ids: Sequence[str]) -> dict[str, Any]:
            contexts = self._contexts(window_ids)
            results = [
                self._window_result(
                    parameter,
                    context,
                    self.config.maker_fill_probability,
                    fill_seed_salt=fill_seed_salt,
                )
                for context in contexts
            ]
            metrics = aggregate_results(
                results,
                capital_per_symbol=self.config.capital_per_symbol,
                symbol_count=len({item.window.symbol for item in contexts}),
            )
            return {
                "metrics": _metrics_payload(metrics),
                "reason_summary": _window_reason_summary(results),
                "windows": [
                    self._window_path_payload(context, result)
                    for context, result in zip(contexts, results)
                ],
            }

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).isoformat(),
            "protocol": {
                "purpose": (
                    "拆解开发集与验证集逐窗口损失来源；最终 OOS 不读取、不评估。"
                ),
            },
            "parameter": _parameter_payload(parameter),
            "backtest_policy": {
                "wind_down_bars": self.config.wind_down_bars,
                "max_inventory_notional": self.config.max_inventory_notional,
                "wind_down_reprice_interval_bars": (
                    self.config.wind_down_reprice_interval_bars
                ),
                "wind_down_initial_offset_steps": (
                    self.config.wind_down_initial_offset_steps
                ),
                "wind_down_unwind_fraction": (
                    self.config.wind_down_unwind_fraction
                ),
                "max_unpaired_lots_per_side": (
                    self.config.max_unpaired_lots_per_side
                ),
                "maker_fee_rate": self.config.maker_fee_rate,
                "taker_fee_rate": self.config.taker_fee_rate,
                "stop_slippage_bps": self.config.stop_slippage_bps,
                "fill_seed_salt": fill_seed_salt,
            },
            "split": {
                "development": _split_summary(split.development),
                "validation": _split_summary(split.validation),
                "final_oos": {
                    "count": len(split.final_oos),
                    "status": "SEALED_NOT_EVALUATED",
                },
            },
            "development": segment_payload(split.development),
            "validation": segment_payload(split.validation),
        }

    def _window_path_payload(
        self,
        context: _WindowContext,
        result: WindowResult,
    ) -> dict[str, Any]:
        window = context.window
        observation = window.rows[: window.observation_rows]
        tradable = window.rows[window.observation_rows :]
        entry_price = observation[-1].close if observation else None
        path_closes = [row.close for row in tradable]
        decision = context.entry_decision
        features = None
        if decision is not None:
            features = {
                **asdict(decision.features),
                "as_of": decision.features.as_of.isoformat(),
            }
        return {
            **asdict(result),
            "entry_price": entry_price,
            "final_price": path_closes[-1] if path_closes else None,
            "path_return_pct": (
                path_closes[-1] / entry_price - 1.0
                if entry_price and path_closes else None
            ),
            "max_up_excursion_pct": (
                max(path_closes) / entry_price - 1.0
                if entry_price and path_closes else None
            ),
            "max_down_excursion_pct": (
                min(path_closes) / entry_price - 1.0
                if entry_price and path_closes else None
            ),
            "entry_features": features,
        }

    def _evaluate_entry_filter(
        self,
        parameter: ParameterSet,
        contexts: Sequence[_WindowContext],
        entry_filter: EntryFilter,
        *,
        fill_seed_salt: int | None = None,
    ) -> AggregateMetrics:
        results = [
            self._entry_filtered_result(
                parameter,
                context,
                entry_filter,
                fill_seed_salt=fill_seed_salt,
            )
            for context in contexts
        ]
        return aggregate_results(
            results,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )

    def _evaluate_exit_policy(
        self,
        parameter: ParameterSet,
        contexts: Sequence[_WindowContext],
        wind_down_bars: int,
    ) -> AggregateMetrics:
        results = [
            self._window_result(
                parameter,
                context,
                self.config.maker_fill_probability,
                wind_down_bars=wind_down_bars,
            )
            for context in contexts
        ]
        return aggregate_results(
            results,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )

    def _evaluate_wind_down_maker_policy(
        self,
        parameter: ParameterSet,
        contexts: Sequence[_WindowContext],
        policy: WindDownMakerPolicy | None,
        *,
        fill_probability: float | None = None,
        maker_fee_rate: float | None = None,
        taker_fee_rate: float | None = None,
        stop_slippage_bps: float | None = None,
        fill_seed_salt: int | None = None,
        entry_filter: EntryFilter | None = None,
    ) -> tuple[AggregateMetrics, dict[str, Any]]:
        probability = (
            self.config.maker_fill_probability
            if fill_probability is None
            else float(fill_probability)
        )
        results = [
            self._window_result(
                parameter,
                context,
                probability,
                wind_down_reprice_interval_bars=(
                    0 if policy is None else policy.reprice_interval_bars
                ),
                wind_down_initial_offset_steps=(
                    0.0 if policy is None else policy.initial_offset_steps
                ),
                wind_down_unwind_fraction=(
                    1.0 if policy is None else policy.unwind_fraction
                ),
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
                stop_slippage_bps=stop_slippage_bps,
                fill_seed_salt=fill_seed_salt,
            )
            for context in contexts
        ]
        if entry_filter is not None:
            results = [
                self._apply_entry_filter(result, context, entry_filter)
                for context, result in zip(contexts, results)
            ]
        metrics = aggregate_results(
            results,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )
        return metrics, {
            "reprice_count": sum(item.wind_down_reprice_count for item in results),
            "maker_fill_count": sum(
                item.wind_down_maker_fill_count for item in results
            ),
            "maker_unwind_pnl": sum(item.wind_down_maker_pnl for item in results),
            "paired_grid_pnl": sum(item.paired_grid_pnl for item in results),
            "stop_exit_pnl": sum(item.stop_exit_pnl for item in results),
            "stop_exit_cost": sum(item.stop_exit_cost for item in results),
            "symbol_metrics": {
                symbol: _metrics_payload(aggregate_results(
                    [item for item in results if item.symbol == symbol],
                    capital_per_symbol=self._capital_for_symbol(symbol),
                    symbol_count=1,
                ))
                for symbol in sorted({item.symbol for item in results})
            },
        }

    def _evaluate_joint_symbol_policy(
        self,
        contexts: Sequence[_WindowContext],
        symbol_policies: dict[str, SymbolResearchPolicy],
        policy: WindDownMakerPolicy,
        *,
        maker_fee_rate: float,
        taker_fee_rate: float,
        stop_slippage_bps: float,
        fill_seed_salt: int,
    ) -> tuple[AggregateMetrics, dict[str, Any]]:
        results: list[WindowResult] = []
        for context in contexts:
            symbol_policy = symbol_policies[context.window.symbol]
            result = self._window_result(
                symbol_policy.parameter,
                context,
                self.config.maker_fill_probability,
                max_inventory_notional=symbol_policy.max_inventory_notional,
                max_unpaired_lots_per_side=(
                    symbol_policy.max_unpaired_lots_per_side
                ),
                reduce_target_step_fraction=(
                    symbol_policy.reduce_target_step_fraction
                ),
                wind_down_reprice_interval_bars=policy.reprice_interval_bars,
                wind_down_initial_offset_steps=policy.initial_offset_steps,
                wind_down_unwind_fraction=policy.unwind_fraction,
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
                stop_slippage_bps=stop_slippage_bps,
                fill_seed_salt=fill_seed_salt,
            )
            if symbol_policy.entry_filter is not None:
                result = self._apply_entry_filter(
                    result,
                    context,
                    symbol_policy.entry_filter,
                )
            results.append(result)

        capital_by_symbol = {
            symbol: self._capital_for_symbol(symbol)
            for symbol in symbol_policies
        }
        metrics = aggregate_joint_results(
            results,
            capital_by_symbol=capital_by_symbol,
        )
        return metrics, {
            "reprice_count": sum(item.wind_down_reprice_count for item in results),
            "maker_fill_count": sum(
                item.wind_down_maker_fill_count for item in results
            ),
            "maker_unwind_pnl": sum(item.wind_down_maker_pnl for item in results),
            "paired_grid_pnl": sum(item.paired_grid_pnl for item in results),
            "stop_exit_pnl": sum(item.stop_exit_pnl for item in results),
            "stop_exit_cost": sum(item.stop_exit_cost for item in results),
            "symbol_metrics": {
                symbol: _metrics_payload(aggregate_results(
                    [item for item in results if item.symbol == symbol],
                    capital_per_symbol=capital_by_symbol[symbol],
                    symbol_count=1,
                ))
                for symbol in sorted(symbol_policies)
            },
        }

    def _evaluate_inventory_policy(
        self,
        parameter: ParameterSet,
        contexts: Sequence[_WindowContext],
        max_inventory_notional: float,
    ) -> AggregateMetrics:
        results = [
            self._window_result(
                parameter,
                context,
                self.config.maker_fill_probability,
                max_inventory_notional=max_inventory_notional,
            )
            for context in contexts
        ]
        return aggregate_results(
            results,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )

    def _dynamic_mode_parameters(
        self,
        parameter: ParameterSet,
    ) -> dict[GridDirectionMode, ParameterSet]:
        result: dict[GridDirectionMode, ParameterSet] = {}
        for mode in GridDirectionMode:
            match = next((
                item
                for item in self.parameters
                if item.direction_mode == mode
                and item.range_multiplier == parameter.range_multiplier
                and item.min_step_pct == parameter.min_step_pct
                and item.stop_buffer_pct == parameter.stop_buffer_pct
            ), None)
            if match is None:
                raise ValueError(f"动态方向诊断缺少 {mode.value} 参数。")
            result[mode] = match
        return result

    def _evaluate_dynamic_mode_rule(
        self,
        parameters: dict[GridDirectionMode, ParameterSet],
        contexts: Sequence[_WindowContext],
        rule: DynamicModeRule,
    ) -> tuple[AggregateMetrics, dict[str, Any]]:
        results: list[WindowResult] = []
        counts: dict[str, int] = defaultdict(int)
        pnl_by_mode: dict[str, float] = defaultdict(float)
        reason_counts: dict[str, int] = defaultdict(int)
        for context in contexts:
            visible_rows = (
                list(context.window.history_rows)
                + list(context.window.rows[: context.window.observation_rows])
            )
            decision = classify_dynamic_mode(visible_rows, rule)
            reason_counts[decision.reason] += 1
            if decision.direction_mode is None:
                counts["PAUSE"] += 1
                results.append(WindowResult(
                    parameter_id=rule.rule_id,
                    symbol=context.window.symbol,
                    window_id=context.window.window_id,
                    market_close=context.window.market_close.isoformat(),
                    status="BLOCKED",
                    reason=f"DYNAMIC_MODE_PAUSE: {decision.reason}",
                ))
                continue
            mode = decision.direction_mode
            counts[mode.value] += 1
            result = self._window_result(
                parameters[mode],
                context,
                self.config.maker_fill_probability,
            )
            pnl_by_mode[mode.value] += result.pnl
            results.append(result)
        metrics = aggregate_results(
            results,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )
        return metrics, {
            "selection_counts": dict(sorted(counts.items())),
            "pnl_by_mode": dict(sorted(pnl_by_mode.items())),
            "reason_counts": dict(sorted(reason_counts.items())),
        }

    def _entry_filtered_result(
        self,
        parameter: ParameterSet,
        context: _WindowContext,
        entry_filter: EntryFilter,
        *,
        fill_seed_salt: int | None = None,
    ) -> WindowResult:
        baseline = self._window_result(
            parameter,
            context,
            self.config.maker_fill_probability,
            fill_seed_salt=fill_seed_salt,
        )
        return self._apply_entry_filter(baseline, context, entry_filter)

    @staticmethod
    def _apply_entry_filter(
        baseline: WindowResult,
        context: _WindowContext,
        entry_filter: EntryFilter,
    ) -> WindowResult:
        if baseline.status != "TRADED":
            return baseline
        decision = context.entry_decision
        if decision is None:
            raise RuntimeError("入口诊断缺少已计算的 Regime 决策。")
        features = decision.features
        reasons: list[str] = []
        if features.directional_efficiency > entry_filter.max_directional_efficiency:
            reasons.append(
                "方向效率 "
                f"{features.directional_efficiency:.4f} > "
                f"{entry_filter.max_directional_efficiency:.4f}"
            )
        if features.volatility_expansion > entry_filter.max_volatility_expansion:
            reasons.append(
                "波动扩张 "
                f"{features.volatility_expansion:.4f} > "
                f"{entry_filter.max_volatility_expansion:.4f}"
            )
        if features.reversal_ratio < entry_filter.min_reversal_ratio:
            reasons.append(
                "反转比例 "
                f"{features.reversal_ratio:.4f} < "
                f"{entry_filter.min_reversal_ratio:.4f}"
            )
        if not reasons:
            return baseline
        return replace(
            baseline,
            status="BLOCKED",
            reason="ENTRY_FILTER: " + "；".join(reasons),
            pnl=0.0,
            max_drawdown=0.0,
            fees_paid=0.0,
            funding_paid=0.0,
            fill_count=0,
            pair_count=0,
            defensive_count=0,
        )

    def _window_result(
        self,
        parameter: ParameterSet,
        context: _WindowContext,
        fill_probability: float,
        *,
        wind_down_bars: int | None = None,
        max_inventory_notional: float | None = None,
        wind_down_reprice_interval_bars: int | None = None,
        wind_down_initial_offset_steps: float | None = None,
        wind_down_unwind_fraction: float | None = None,
        max_unpaired_lots_per_side: int | None = None,
        reduce_target_step_fraction: float | None = None,
        maker_fee_rate: float | None = None,
        taker_fee_rate: float | None = None,
        stop_slippage_bps: float | None = None,
        fill_seed_salt: int | None = None,
    ) -> WindowResult:
        window = context.window
        selected_wind_down = (
            self.config.wind_down_bars
            if wind_down_bars is None
            else int(wind_down_bars)
        )
        selected_inventory_cap = (
            self.config.max_inventory_notional
            if max_inventory_notional is None
            else float(max_inventory_notional)
        )
        selected_reprice_interval = (
            self.config.wind_down_reprice_interval_bars
            if wind_down_reprice_interval_bars is None
            else int(wind_down_reprice_interval_bars)
        )
        selected_offset_steps = (
            self.config.wind_down_initial_offset_steps
            if wind_down_initial_offset_steps is None
            else float(wind_down_initial_offset_steps)
        )
        selected_unwind_fraction = (
            self.config.wind_down_unwind_fraction
            if wind_down_unwind_fraction is None
            else float(wind_down_unwind_fraction)
        )
        selected_unpaired_lots = (
            self.config.max_unpaired_lots_per_side
            if max_unpaired_lots_per_side is None
            else int(max_unpaired_lots_per_side)
        )
        selected_reduce_target_fraction = (
            self.config.reduce_target_step_fraction
            if reduce_target_step_fraction is None
            else float(reduce_target_step_fraction)
        )
        selected_maker_fee = (
            self.config.maker_fee_rate
            if maker_fee_rate is None
            else float(maker_fee_rate)
        )
        selected_taker_fee = (
            self.config.taker_fee_rate
            if taker_fee_rate is None
            else float(taker_fee_rate)
        )
        selected_stop_slippage = (
            self.config.stop_slippage_bps
            if stop_slippage_bps is None
            else float(stop_slippage_bps)
        )
        cache_key = (
            parameter.parameter_id,
            window.symbol,
            window.window_id,
            fill_probability,
            selected_wind_down,
            selected_inventory_cap,
            selected_reprice_interval,
            selected_offset_steps,
            selected_unwind_fraction,
            selected_unpaired_lots,
            selected_reduce_target_fraction,
            selected_maker_fee,
            selected_taker_fee,
            selected_stop_slippage,
            fill_seed_salt,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._run_window(
            parameter,
            context,
            fill_probability,
            selected_wind_down,
            selected_inventory_cap,
            selected_reprice_interval,
            selected_offset_steps,
            selected_unwind_fraction,
            selected_unpaired_lots,
            selected_reduce_target_fraction,
            selected_maker_fee,
            selected_taker_fee,
            selected_stop_slippage,
            fill_seed_salt,
        )
        self._cache[cache_key] = result
        return result

    def _run_window(
        self,
        parameter: ParameterSet,
        context: _WindowContext,
        fill_probability: float,
        wind_down_bars: int,
        max_inventory_notional: float,
        wind_down_reprice_interval_bars: int,
        wind_down_initial_offset_steps: float,
        wind_down_unwind_fraction: float,
        max_unpaired_lots_per_side: int,
        reduce_target_step_fraction: float,
        maker_fee_rate: float,
        taker_fee_rate: float,
        stop_slippage_bps: float,
        fill_seed_salt: int | None,
    ) -> WindowResult:
        window = context.window
        observation = list(window.rows[: window.observation_rows])
        tradable = list(window.rows[window.observation_rows :])
        base = {
            "parameter_id": parameter.parameter_id,
            "symbol": window.symbol,
            "window_id": window.window_id,
            "market_close": window.market_close.isoformat(),
        }
        if window.status != "READY":
            return WindowResult(
                **base,
                status="SKIPPED",
                reason=window.skip_reason or "窗口未通过数据完整性检查",
            )
        if len(observation) < self.config.observation_rows or not tradable:
            return WindowResult(**base, status="SKIPPED", reason="窗口数据不足")
        rules = self.symbol_rules.get(window.symbol)
        if rules is None:
            return WindowResult(**base, status="SKIPPED", reason="缺少交易规则假设")
        obs_maps = [item.to_mapping() for item in observation]
        entry = context.entry_decision
        if entry is None:
            entry = self.regime.evaluate(
                window.symbol,
                obs_maps,
                spread_pct=self.config.assumed_spread_pct,
                depth_usdt=self.config.assumed_depth_usdt,
                funding_rate=self.config.funding_rate_per_settlement,
                data_age_seconds=0.0,
                include_cost=False,
                as_of=observation[-1].open_datetime,
            )
            context.entry_decision = entry
        if entry.hard_blocks:
            return WindowResult(
                **base,
                status="BLOCKED",
                reason="；".join(entry.hard_blocks),
                regime_score=entry.grid_score,
            )
        adaptive = AdaptiveGridGenerator(AdaptiveGridConfig(
            k_atr_range=2.0 * parameter.range_multiplier,
            k_sigma_range=2.0 * parameter.range_multiplier,
            min_step_pct=parameter.min_step_pct,
            stop_buffer_pct=parameter.stop_buffer_pct,
        ))
        try:
            params = adaptive.generate(
                window.symbol,
                obs_maps,
                current_price=observation[-1].close,
                funding_rate=self.config.funding_rate_per_settlement,
                funding_cost_rate=self.config.funding_rate_per_settlement,
                maker_fee_rate=maker_fee_rate,
                regime_score=entry.grid_score,
                capital=self._capital_for_symbol(window.symbol),
                leverage=self.config.leverage,
                tick_size=rules.tick_size,
                step_size=rules.step_size,
                min_qty=rules.min_qty,
                min_notional=rules.min_notional,
                direction_mode=parameter.direction_mode,
                risk_budget=(
                    self._capital_for_symbol(window.symbol)
                    * self.config.risk_budget_pct
                ),
                taker_fee_rate=taker_fee_rate,
                calculated_at=observation[-1].open_datetime,
            )
        except (GridEconomicsError, GridCalculationError) as exc:
            return WindowResult(
                **base,
                status="BLOCKED",
                reason=str(exc),
                regime_score=entry.grid_score,
            )
        final_entry = self.regime.evaluate(
            window.symbol,
            obs_maps,
            spread_pct=self.config.assumed_spread_pct,
            depth_usdt=self.config.assumed_depth_usdt,
            funding_rate=self.config.funding_rate_per_settlement,
            data_age_seconds=0.0,
            expected_step_pct=params.step_pct,
            cost_floor_pct=params.cost_floor_pct,
            cost_breakdown=_numeric_cost_breakdown(params.economics),
            as_of=observation[-1].open_datetime,
        )
        if not final_entry.allowed:
            return WindowResult(
                **base,
                status="BLOCKED",
                reason=f"{final_entry.verdict}: {final_entry.grid_score:.2f}",
                regime_score=final_entry.grid_score,
                grid_count=params.grid_num,
                step_pct=params.step_pct,
            )

        rolling = self._rolling_components(context)
        cost_score = _cost_score(
            params.step_pct,
            params.cost_floor_pct,
            float(params.economics.get("risk_discount_pct") or 0.0),
        )
        trade_rows: list[dict[str, Any]] = []
        for row, components in zip(tradable, rolling):
            payload = row.to_mapping()
            payload["regime_score"] = (
                0.0
                if components["hard_blocks"]
                else _score_with_cost(
                    components["component_scores"],
                    cost_score,
                    self.regime.config,
                )
            )
            trade_rows.append(payload)
        backtest = run_grid_backtest(
            params,
            trade_rows,
            observation[-1].close,
            BacktestConfig(
                capital=self._capital_for_symbol(window.symbol),
                leverage=self.config.leverage,
                maker_fee_rate=maker_fee_rate,
                stop_on_range_break=False,
                stop_on_stop_loss=True,
                fill_model="L0_CONSERVATIVE",
                min_tick_size=rules.tick_size,
                max_fills_per_bar=self.config.max_fills_per_bar,
                maker_fill_probability=fill_probability,
                fill_probability_seed=(
                    _stable_seed(
                        parameter.parameter_id,
                        window.symbol,
                        window.window_id,
                    )
                    if fill_seed_salt is None
                    else _stable_seed(
                        str(fill_seed_salt),
                        parameter.parameter_id,
                        window.symbol,
                        window.window_id,
                    )
                ),
                taker_fee_rate=taker_fee_rate,
                stop_slippage_bps=stop_slippage_bps,
                funding_rate_per_bar=(
                    self.config.funding_rate_per_settlement / 480.0
                ),
                force_close_at_end=True,
                direction_mode=parameter.direction_mode,
                seed_slippage_bps=self.config.seed_slippage_bps,
                retention_score_threshold=self.config.retention_threshold,
                retention_soft_breach_limit=(
                    self.config.retention_soft_breach_limit
                ),
                quantity_step_size=rules.step_size,
                wind_down_bars=wind_down_bars,
                max_inventory_notional=max_inventory_notional,
                inventory_caution_utilization=(
                    self.config.inventory_caution_utilization
                ),
                inventory_critical_utilization=(
                    self.config.inventory_critical_utilization
                ),
                wind_down_reprice_interval_bars=(
                    wind_down_reprice_interval_bars
                ),
                wind_down_initial_offset_steps=(
                    wind_down_initial_offset_steps
                ),
                wind_down_unwind_fraction=wind_down_unwind_fraction,
                max_unpaired_lots_per_side=max_unpaired_lots_per_side,
                reduce_target_step_fraction=reduce_target_step_fraction,
                unpaired_lot_cap_enforcement=self.config.unpaired_lot_cap_enforcement,
            ),
        )
        return WindowResult(
            **base,
            status="TRADED",
            reason=backtest.stopped_reason or "completed",
            pnl=backtest.total_pnl,
            max_drawdown=backtest.max_drawdown,
            fees_paid=backtest.fees_paid,
            funding_paid=backtest.funding_paid,
            fill_count=len(backtest.fills),
            pair_count=backtest.pair_completion_count,
            defensive_count=backtest.defensive_entry_count,
            regime_score=final_entry.grid_score,
            grid_count=params.grid_num,
            step_pct=params.step_pct,
            gross_grid_pnl=backtest.gross_grid_pnl,
            paired_grid_pnl=(
                backtest.gross_grid_pnl - backtest.stop_exit_pnl
            ),
            stop_exit_pnl=backtest.stop_exit_pnl,
            stop_exit_cost=backtest.stop_exit_cost,
            max_inventory_utilization=backtest.max_inventory_utilization,
            stopped_at_index=backtest.stopped_at_index,
            wind_down_reprice_count=backtest.wind_down_reprice_count,
            wind_down_maker_fill_count=backtest.wind_down_maker_fill_count,
            wind_down_maker_pnl=backtest.wind_down_maker_pnl,
            max_unpaired_lot_age_bars=backtest.max_unpaired_lot_age_bars,
            exit_oldest_lot_age_bars=backtest.exit_oldest_lot_age_bars,
            exit_long_qty=backtest.exit_long_qty,
            exit_short_qty=backtest.exit_short_qty,
            exit_hedged_fraction=backtest.exit_hedged_fraction,
        )

    def _rolling_components(
        self,
        context: _WindowContext,
    ) -> list[dict[str, Any]]:
        if context.rolling_components is not None:
            return context.rolling_components
        rows = list(context.window.rows)
        start = context.window.observation_rows
        output: list[dict[str, Any]] = []
        for index in range(start, len(rows)):
            # The score attached to bar[index] uses only bars strictly before it.
            history = rows[index - self.config.observation_rows : index]
            decision = self.regime.evaluate(
                context.window.symbol,
                [item.to_mapping() for item in history],
                spread_pct=self.config.assumed_spread_pct,
                depth_usdt=self.config.assumed_depth_usdt,
                funding_rate=self.config.funding_rate_per_settlement,
                data_age_seconds=0.0,
                include_cost=False,
                running=True,
                as_of=history[-1].open_datetime,
            )
            output.append({
                "component_scores": decision.component_scores,
                "hard_blocks": decision.hard_blocks,
            })
        context.rolling_components = output
        return output

    def _contexts(self, window_ids: Sequence[str]) -> list[_WindowContext]:
        wanted = set(window_ids)
        return [item for item in self.contexts if item.window.window_id in wanted]

    def _lock_parameter(
        self,
        finalists: Sequence[ParameterSet],
        development: dict[str, AggregateMetrics],
        validation: dict[str, AggregateMetrics],
    ) -> ParameterSet:
        def robust_score(item: ParameterSet) -> tuple[float, float]:
            neighbor_metrics = [
                validation[neighbor.parameter_id].objective
                for neighbor in parameter_neighbors(item, self.parameters)
            ]
            neighborhood = (
                statistics.median(neighbor_metrics)
                if neighbor_metrics
                else validation[item.parameter_id].objective
            )
            weakest = min(
                development[item.parameter_id].objective,
                validation[item.parameter_id].objective,
                neighborhood,
            )
            return weakest, validation[item.parameter_id].objective

        return max(finalists, key=robust_score)

    def _walk_forward(
        self,
        contexts: Sequence[_WindowContext],
    ) -> dict[str, Any]:
        ordered_ids = sorted({item.window.window_id for item in contexts})
        train = self.config.walk_forward_train_windows
        test = self.config.walk_forward_test_windows
        step = self.config.walk_forward_step_windows
        folds: list[dict[str, Any]] = []
        stitched: list[WindowResult] = []
        for start in range(0, len(ordered_ids) - train - test + 1, step):
            train_ids = ordered_ids[start : start + train]
            test_ids = ordered_ids[start + train : start + train + test]
            train_contexts = self._contexts(train_ids)
            test_contexts = self._contexts(test_ids)
            ranked = max(
                self.parameters,
                key=lambda item: self.evaluate(item, train_contexts).objective,
            )
            metrics = self.evaluate(ranked, test_contexts)
            stitched.extend(
                self._window_result(
                    ranked,
                    context,
                    self.config.maker_fill_probability,
                )
                for context in test_contexts
            )
            folds.append({
                "train": _split_summary(tuple(train_ids)),
                "test": _split_summary(tuple(test_ids)),
                "selected_parameter": ranked.parameter_id,
                "test_metrics": _metrics_payload(metrics),
            })
        stitched_metrics = aggregate_results(
            stitched,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )
        return {
            "fold_count": len(folds),
            "folds": folds,
            "metrics": _metrics_payload(stitched_metrics),
        }

    def _walk_forward_wind_down(
        self,
        parameter: ParameterSet,
        policies: Sequence[WindDownMakerPolicy],
        contexts: Sequence[_WindowContext],
    ) -> dict[str, Any]:
        ordered_ids = sorted({item.window.window_id for item in contexts})
        train = self.config.walk_forward_train_windows
        test = self.config.walk_forward_test_windows
        step = self.config.walk_forward_step_windows
        purge = 1
        folds: list[dict[str, Any]] = []
        stitched: list[WindowResult] = []
        required = train + purge + test
        for start in range(0, len(ordered_ids) - required + 1, step):
            train_ids = ordered_ids[start : start + train]
            purge_ids = ordered_ids[start + train : start + train + purge]
            test_ids = ordered_ids[
                start + train + purge : start + required
            ]
            train_contexts = self._contexts(train_ids)
            test_contexts = self._contexts(test_ids)
            selected = max(
                policies,
                key=lambda item: self._evaluate_wind_down_maker_policy(
                    parameter,
                    train_contexts,
                    item,
                )[0].objective,
            )
            metrics, operations = self._evaluate_wind_down_maker_policy(
                parameter,
                test_contexts,
                selected,
            )
            stitched.extend(
                self._window_result(
                    parameter,
                    context,
                    self.config.maker_fill_probability,
                    wind_down_reprice_interval_bars=(
                        selected.reprice_interval_bars
                    ),
                    wind_down_initial_offset_steps=(
                        selected.initial_offset_steps
                    ),
                    wind_down_unwind_fraction=selected.unwind_fraction,
                )
                for context in test_contexts
            )
            folds.append({
                "train": _split_summary(tuple(train_ids)),
                "purged": _split_summary(tuple(purge_ids)),
                "test": _split_summary(tuple(test_ids)),
                "selected_policy": selected.policy_id,
                "test_metrics": _metrics_payload(metrics),
                "test_operations": operations,
            })
        stitched_metrics = aggregate_results(
            stitched,
            capital_per_symbol=self.config.capital_per_symbol,
            symbol_count=len({item.window.symbol for item in contexts}),
        )
        return {
            "purge_windows": purge,
            "fold_count": len(folds),
            "folds": folds,
            "metrics": _metrics_payload(stitched_metrics),
        }


def aggregate_results(
    results: Sequence[WindowResult],
    *,
    capital_per_symbol: float,
    symbol_count: int,
) -> AggregateMetrics:
    if not results:
        return AggregateMetrics(
            0, 0, 0, 0, 0.0, 0.0, None, 0.0, 0.0, 0.0, None, None,
            0.0, 0, 0, 0.0, 0.0, 0.0, -1_000_000.0,
        )
    grouped: dict[str, float] = defaultdict(float)
    market_closes: dict[str, datetime] = {}
    for item in results:
        grouped[item.window_id] += item.pnl
        market_closes[item.window_id] = datetime.fromisoformat(item.market_close)
    ordered = sorted(grouped, key=lambda key: market_closes[key])
    base_capital = capital_per_symbol * max(1, symbol_count)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    returns: list[float] = []
    for window_id in ordered:
        pnl = grouped[window_id]
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
        returns.append(pnl / base_capital)
    positive = [value for value in grouped.values() if value > 0]
    negative = [value for value in grouped.values() if value < 0]
    profit_factor = (
        sum(positive) / abs(sum(negative))
        if positive and negative
        else None
    )
    sharpe = None
    if len(returns) >= 2:
        std = statistics.stdev(returns)
        if std > 0:
            sharpe = statistics.mean(returns) / std * math.sqrt(52.0)
    span_years = max(
        0.0,
        (
            max(market_closes.values()) - min(market_closes.values())
        ).total_seconds() / (365.25 * 86_400),
    )
    total_return = cumulative / base_capital
    annualized = None
    if span_years > 0 and 1.0 + total_return > 0:
        annualized = (1.0 + total_return) ** (1.0 / span_years) - 1.0
    concentration = max(positive) / sum(positive) if positive else 0.0
    traded = sum(item.status == "TRADED" for item in results)
    coverage = traded / len(results)
    max_drawdown_pct = max_drawdown / base_capital
    # Deliberately simple and auditable: return minus drawdown, with a small
    # penalty for a strategy that almost never qualifies.
    objective = total_return - max_drawdown_pct - 0.01 * (1.0 - coverage)
    return AggregateMetrics(
        window_count=len(ordered),
        symbol_window_count=len(results),
        traded_symbol_windows=traded,
        blocked_symbol_windows=sum(item.status != "TRADED" for item in results),
        total_pnl=cumulative,
        total_return_pct=total_return,
        annualized_return_pct=annualized,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        positive_window_ratio=len(positive) / len(ordered),
        profit_factor=profit_factor,
        sharpe_per_week=sharpe,
        trade_coverage=coverage,
        pair_count=sum(item.pair_count for item in results),
        fill_count=sum(item.fill_count for item in results),
        fees_paid=sum(item.fees_paid for item in results),
        funding_paid=sum(item.funding_paid for item in results),
        best_window_concentration=concentration,
        objective=objective,
    )


def aggregate_joint_results(
    results: Sequence[WindowResult],
    *,
    capital_by_symbol: dict[str, float],
) -> AggregateMetrics:
    """Aggregate a multi-symbol portfolio with conservative risk statistics."""

    symbols = sorted({item.symbol for item in results})
    if not symbols:
        return aggregate_results(
            results,
            capital_per_symbol=1.0,
            symbol_count=1,
        )
    missing = [symbol for symbol in symbols if symbol not in capital_by_symbol]
    if missing:
        raise ValueError(f"联合聚合缺少标的本金: {', '.join(missing)}")
    total_capital = sum(float(capital_by_symbol[symbol]) for symbol in symbols)
    if total_capital <= 0:
        raise ValueError("联合聚合总本金必须为正。")

    actual = aggregate_results(
        results,
        capital_per_symbol=total_capital / len(symbols),
        symbol_count=len(symbols),
    )
    symbol_metrics = {
        symbol: aggregate_results(
            [item for item in results if item.symbol == symbol],
            capital_per_symbol=float(capital_by_symbol[symbol]),
            symbol_count=1,
        )
        for symbol in symbols
    }
    gross_profit = sum(item.pnl for item in results if item.pnl > 0)
    gross_loss = abs(sum(item.pnl for item in results if item.pnl < 0))
    profit_factor = (
        gross_profit / gross_loss
        if gross_profit > 0 and gross_loss > 0
        else None
    )
    conservative_drawdown = max(
        actual.max_drawdown,
        sum(item.max_drawdown for item in symbol_metrics.values()),
    )
    conservative_drawdown_pct = conservative_drawdown / total_capital
    conservative_concentration = max(
        actual.best_window_concentration,
        max(item.best_window_concentration for item in symbol_metrics.values()),
    )
    objective = (
        actual.total_return_pct
        - conservative_drawdown_pct
        - 0.01 * (1.0 - actual.trade_coverage)
    )
    return replace(
        actual,
        profit_factor=profit_factor,
        max_drawdown=conservative_drawdown,
        max_drawdown_pct=conservative_drawdown_pct,
        best_window_concentration=conservative_concentration,
        objective=objective,
    )


def _window_reason_summary(results: Sequence[WindowResult]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[WindowResult]] = defaultdict(list)
    for item in results:
        reason = item.reason
        if reason.startswith("BLOCKED_SCORE"):
            reason = "BLOCKED_SCORE"
        grouped[(item.status, reason)].append(item)
    return [
        {
            "status": status,
            "reason": reason,
            "count": len(items),
            "total_pnl": sum(item.pnl for item in items),
            "gross_grid_pnl": sum(item.gross_grid_pnl for item in items),
            "paired_grid_pnl": sum(item.paired_grid_pnl for item in items),
            "stop_exit_pnl": sum(item.stop_exit_pnl for item in items),
            "stop_exit_cost": sum(item.stop_exit_cost for item in items),
            "max_exit_oldest_lot_age_bars": max(
                (item.exit_oldest_lot_age_bars for item in items),
                default=0,
            ),
            "mean_exit_oldest_lot_age_bars": (
                sum(item.exit_oldest_lot_age_bars for item in items) / len(items)
                if items else 0.0
            ),
            "max_exit_hedged_fraction": max(
                (item.exit_hedged_fraction for item in items),
                default=0.0,
            ),
        }
        for (status, reason), items in sorted(
            grouped.items(),
            key=lambda pair: (-len(pair[1]), pair[0]),
        )
    ]


def write_research_report(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_robustness_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_report_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_entry_filter_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_entry_filter_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_entry_filter_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_exit_policy_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_exit_policy_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_exit_policy_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_wind_down_maker_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_maker_unwind_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_wind_down_maker_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_seed_sensitivity_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_seed_sensitivity_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_seed_sensitivity_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_joint_seed_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_joint_seed_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_joint_seed_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_joint_oos_report(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stem}.json"
    md_path = directory / f"{stem}.md"
    if json_path.exists() or md_path.exists():
        raise FileExistsError(f"最终 OOS 报告已存在，拒绝重复执行：{stem}")
    _write_json_atomic(json_path, report)
    md_path.write_text(_joint_oos_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_parameter_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_parameter_diagnostic_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_parameter_diagnostic_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_window_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_window_diagnostic_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_window_diagnostic_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_inventory_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_inventory_diagnostic_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_inventory_diagnostic_markdown(report), encoding="utf-8")
    return json_path, md_path


def write_dynamic_mode_diagnostic(
    report: dict[str, Any],
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> tuple[Path, Path]:
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = stem or f"quietgrid_dynamic_mode_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    json_path = directory / f"{name}.json"
    md_path = directory / f"{name}.md"
    _write_json_atomic(json_path, report)
    md_path.write_text(_dynamic_mode_markdown(report), encoding="utf-8")
    return json_path, md_path


def _weekend_boundaries(
    start: datetime,
    end: datetime,
    force_close_minutes: int,
) -> list[tuple[datetime, datetime]]:
    calendar = mcal.get_calendar("NYSE")
    schedule = calendar.schedule(
        start_date=start.astimezone(NY_TZ).date() - timedelta(days=14),
        end_date=end.astimezone(NY_TZ).date() + timedelta(days=14),
    )
    sessions = list(schedule.iterrows())
    boundaries: list[tuple[datetime, datetime]] = []
    for index in range(len(sessions) - 1):
        current_label, current = sessions[index]
        next_label, _following = sessions[index + 1]
        current_date = current_label.date()
        next_date = next_label.date()
        if (next_date - current_date).days <= 1:
            continue
        market_close = current["market_close"].to_pydatetime().astimezone(UTC)
        premarket = datetime.combine(
            next_date,
            time(hour=4),
            tzinfo=NY_TZ,
        ).astimezone(UTC)
        force_close = premarket - timedelta(minutes=force_close_minutes)
        if market_close < end and force_close > start:
            boundaries.append((market_close, force_close))
    return boundaries


def _finalize_window(
    symbol: str,
    boundary: tuple[datetime, datetime],
    rows: Sequence[NormalizedKline],
    observation_rows: int,
    minimum_tradable_rows: int,
    history_rows: Sequence[NormalizedKline] = (),
) -> WeekendWindow:
    market_close, force_close_at = boundary
    expected_rows = int((force_close_at - market_close).total_seconds() // 60)
    skip_reason = None
    if len(rows) != expected_rows:
        skip_reason = f"K线不连续：{len(rows)}/{expected_rows}"
    elif any(
        current.open_time - previous.open_time != 60_000
        for previous, current in zip(rows, rows[1:])
    ):
        skip_reason = "K线存在分钟缺口"
    elif len(rows) < observation_rows + minimum_tradable_rows:
        skip_reason = "观察期后可交易 K 线不足"
    return WeekendWindow(
        symbol=symbol,
        window_id=f"nyse_{market_close:%Y%m%dT%H%M%SZ}",
        market_close=market_close,
        force_close_at=force_close_at,
        rows=tuple(rows),
        observation_rows=observation_rows,
        status="READY" if skip_reason is None else "SKIPPED",
        skip_reason=skip_reason,
        history_rows=tuple(history_rows),
    )


def _score_with_cost(
    components: dict[str, float | None],
    cost_score: float,
    config: RegimeConfig,
) -> float:
    weights = {
        "volatility": config.weights.volatility,
        "trend": config.weights.trend,
        "liquidity": config.weights.liquidity,
        "mean_reversion": config.weights.mean_reversion,
        "cost": config.weights.cost,
        "event": config.weights.event if config.event_source_available else 0.0,
    }
    scores = dict(components)
    scores["cost"] = cost_score
    total = sum(weights.values())
    return sum(weights[key] * float(scores.get(key) or 0.0) for key in weights) / total


def _cost_score(step_pct: float, hard_cost_pct: float, risk_discount_pct: float) -> float:
    if step_pct <= 0:
        return 0.0
    return max(
        0.0,
        min(100.0, 100.0 * (step_pct - hard_cost_pct - risk_discount_pct) / step_pct),
    )


def _numeric_cost_breakdown(values: dict[str, Any]) -> dict[str, float]:
    """Regime cost explanations accept numeric terms, not solver diagnostics."""

    result: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number) and number >= 0:
            result[str(key)] = number
    return result


def _stability_gates(
    oos: AggregateMetrics,
    neighbors: dict[str, AggregateMetrics],
    walk_forward_payload: dict[str, Any],
) -> dict[str, Any]:
    neighbor_values = list(neighbors.values())
    checks = {
        "oos_positive": oos.total_pnl > 0,
        "oos_profit_factor": (
            (oos.profit_factor is not None and oos.profit_factor >= 1.05)
            or (
                oos.profit_factor is None
                and oos.total_pnl > 0
                and oos.max_drawdown == 0
            )
        ),
        "oos_max_drawdown": oos.max_drawdown_pct <= 0.05,
        "oos_trade_coverage": oos.trade_coverage >= 0.25,
        "oos_not_single_window": oos.best_window_concentration <= 0.35,
        "oos_neighbor_majority_positive": (
            bool(neighbor_values)
            and sum(item.total_pnl > 0 for item in neighbor_values)
            / len(neighbor_values)
            >= 0.50
        ),
        "walk_forward_positive": float(
            walk_forward_payload.get("total_pnl") or 0.0
        ) > 0,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "meaning": (
            "通过仅表示当前冻结样本、费用和撮合假设下具有跨窗口稳健性；"
            "不构成未来收益保证。"
        ),
    }


def _entry_filter_checks(
    development: AggregateMetrics,
    validation: AggregateMetrics,
) -> dict[str, bool]:
    def acceptable_profit_factor(metrics: AggregateMetrics) -> bool:
        return (
            metrics.profit_factor is not None
            and metrics.profit_factor >= 1.05
        ) or (
            metrics.profit_factor is None
            and metrics.total_pnl > 0
            and metrics.max_drawdown == 0
        )

    return {
        "development_positive": development.total_pnl > 0,
        "development_profit_factor": acceptable_profit_factor(development),
        "development_max_drawdown": development.max_drawdown_pct <= 0.05,
        "development_trade_coverage": development.trade_coverage >= 0.25,
        "development_not_single_window": (
            development.best_window_concentration <= 0.35
        ),
        "validation_positive": validation.total_pnl > 0,
        "validation_profit_factor": acceptable_profit_factor(validation),
        "validation_max_drawdown": validation.max_drawdown_pct <= 0.05,
        "validation_trade_coverage": validation.trade_coverage >= 0.25,
        "validation_not_single_window": (
            validation.best_window_concentration <= 0.35
        ),
    }


def _joint_oos_checks(
    portfolio: AggregateMetrics,
    symbol_metrics: dict[str, dict[str, Any]],
) -> dict[str, bool]:
    return {
        "portfolio_positive": portfolio.total_pnl > 0,
        "portfolio_profit_factor": (
            portfolio.profit_factor is not None and portfolio.profit_factor >= 1.05
        ) or (
            portfolio.profit_factor is None
            and portfolio.total_pnl > 0
            and portfolio.max_drawdown == 0
        ),
        "portfolio_max_drawdown": portfolio.max_drawdown_pct <= 0.05,
        "portfolio_trade_coverage": portfolio.trade_coverage >= 0.25,
        "portfolio_not_single_window": portfolio.best_window_concentration <= 0.35,
        **_symbol_metric_checks(symbol_metrics),
    }


def _symbol_metric_checks(
    symbol_metrics: dict[str, dict[str, Any]],
) -> dict[str, bool]:
    def acceptable_profit_factor(metrics: dict[str, Any]) -> bool:
        profit_factor = metrics.get("profit_factor")
        return (
            profit_factor is not None and float(profit_factor) >= 1.05
        ) or (
            profit_factor is None
            and float(metrics.get("total_pnl") or 0.0) > 0
            and float(metrics.get("max_drawdown") or 0.0) == 0
        )

    return {
        "each_symbol_positive": all(
            float(item.get("total_pnl") or 0.0) > 0
            for item in symbol_metrics.values()
        ),
        "each_symbol_profit_factor": all(
            acceptable_profit_factor(item) for item in symbol_metrics.values()
        ),
        "each_symbol_max_drawdown": all(
            float(item.get("max_drawdown_pct") or 0.0) <= 0.05
            for item in symbol_metrics.values()
        ),
        "each_symbol_trade_coverage": all(
            float(item.get("trade_coverage") or 0.0) >= 0.25
            for item in symbol_metrics.values()
        ),
    }


def _validate_research_config(config: ResearchConfig) -> None:
    if config.capital_per_symbol <= 0 or config.leverage <= 0:
        raise ValueError("研究本金和杠杆必须为正。")
    if any(float(value) <= 0 for value in config.capital_by_symbol.values()):
        raise ValueError("按标的研究本金必须为正。")
    if not 0 <= config.maker_fill_probability <= 1:
        raise ValueError("maker_fill_probability 必须在 [0, 1] 内。")
    if config.observation_rows < 61:
        raise ValueError("observation_rows 至少为 61。")
    if config.minimum_tradable_rows < 1:
        raise ValueError("minimum_tradable_rows 必须为正。")
    if config.walk_forward_train_windows < 1:
        raise ValueError("walk_forward_train_windows 必须为正。")
    if config.walk_forward_test_windows < 1:
        raise ValueError("walk_forward_test_windows 必须为正。")
    if config.walk_forward_step_windows < 1:
        raise ValueError("walk_forward_step_windows 必须为正。")
    if not 0 < config.risk_budget_pct < 1:
        raise ValueError("risk_budget_pct 必须在 (0, 1) 内。")
    if config.wind_down_bars < 0:
        raise ValueError("wind_down_bars 不能为负。")
    if config.max_inventory_notional < 0:
        raise ValueError("max_inventory_notional 不能为负。")
    if config.wind_down_reprice_interval_bars < 0:
        raise ValueError("wind_down_reprice_interval_bars 不能为负。")
    if config.wind_down_initial_offset_steps < 0:
        raise ValueError("wind_down_initial_offset_steps 不能为负。")
    if not 0 < config.wind_down_unwind_fraction <= 1:
        raise ValueError("wind_down_unwind_fraction 必须在 (0, 1] 内。")
    if config.max_unpaired_lots_per_side < 0:
        raise ValueError("max_unpaired_lots_per_side 不能为负。")
    if config.unpaired_lot_cap_enforcement not in {"INTRABAR", "BAR_BOUNDARY"}:
        raise ValueError(
            "unpaired_lot_cap_enforcement 必须为 INTRABAR 或 BAR_BOUNDARY。"
        )
    if not (
        0 < config.inventory_caution_utilization
        < config.inventory_critical_utilization
        <= 1
    ):
        raise ValueError("库存 CAUTION/CRITICAL 阈值无效。")


def _report_markdown(report: dict[str, Any]) -> str:
    selected = report["selected_parameter"]
    oos = report["final_oos"]
    stability = report["stability"]
    checks = "\n".join(
        f"- {'✅' if passed else '❌'} {name}"
        for name, passed in stability["checks"].items()
    )
    split_rows = "\n".join(
        f"| {name} | {value['count']} | {value['start']} | {value['end']} |"
        for name, value in report["split"].items()
    )
    return f"""# QuietGrid 稳健性回测报告

生成时间：{report["generated_at"]}

## 结论

**稳健性门槛：{'通过' if stability['passed'] else '未通过'}**

{stability["meaning"]}

## 严格时间切分

| 数据段 | 周末窗口数 | 开始 | 结束 |
|---|---:|---|---|
{split_rows}

最终 OOS 不参与参数选择；失败时应修改策略假设并创建新的研究版本，不能回看
同一 OOS 反复调参。

## 锁定参数

| 参数 | 值 |
|---|---:|
| ID | `{selected['parameter_id']}` |
| 方向 | {selected['direction_mode']} |
| 区间倍率 | {selected['range_multiplier']} |
| 最小格距 | {selected['min_step_pct']:.4%} |
| 外部止损缓冲 | {selected['stop_buffer_pct']:.2%} |

## 最终 OOS

| 指标 | 结果 |
|---|---:|
| 总盈亏 | {oos['total_pnl']:.4f} USDT |
| 固定本金收益率 | {oos['total_return_pct']:.2%} |
| 最大回撤 | {oos['max_drawdown_pct']:.2%} |
| Profit Factor | {_format_optional(oos['profit_factor'])} |
| 周频 Sharpe | {_format_optional(oos['sharpe_per_week'])} |
| 正收益窗口比例 | {oos['positive_window_ratio']:.2%} |
| 实际交易覆盖率 | {oos['trade_coverage']:.2%} |
| 单一最佳窗口贡献 | {oos['best_window_concentration']:.2%} |

## 门槛检查

{checks}

## 重要限制

- 1 分钟 OHLC 无法还原 Maker 排队位置，采用确定性保守成交概率模型。
- 历史 K 线不含盘口，点差和深度是显式假设。
- 资金费按固定 8 小时费率压力假设，不是逐笔历史 sidecar。
- “通过”不等于稳定获利承诺；实盘前仍需测试网、影子运行和小资金阶段。
"""


def _entry_filter_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["candidates"][:12]:
        development = item["development"]
        validation = item["validation"]
        rows.append(
            "| {filter_id} | {passed} | {dev_pnl:.2f} | {dev_pf} | "
            "{dev_dd:.2%} | {dev_coverage:.2%} | {val_pnl:.2f} | {val_pf} | "
            "{val_dd:.2%} | {val_coverage:.2%} |".format(
                filter_id=item["filter_id"],
                passed="通过" if item["passed"] else "未通过",
                dev_pnl=development["total_pnl"],
                dev_pf=_format_optional(development["profit_factor"]),
                dev_dd=development["max_drawdown_pct"],
                dev_coverage=development["trade_coverage"],
                val_pnl=validation["total_pnl"],
                val_pf=_format_optional(validation["profit_factor"]),
                val_dd=validation["max_drawdown_pct"],
                val_coverage=validation["trade_coverage"],
            )
        )
    return f"""# QuietGrid 横盘入口过滤诊断

生成时间：{report['generated_at']}

本报告只使用开发集与验证集；最终 OOS 保持封存且未执行。

通过候选：{report['passed_count']} / {report['candidate_count']}

| 过滤器 | 结果 | 开发盈亏 | 开发 PF | 开发回撤 | 开发覆盖 | 验证盈亏 | 验证 PF | 验证回撤 | 验证覆盖 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

只有同时满足两段正收益、PF、回撤、覆盖率和收益集中度门槛的候选，才允许进入下一轮邻域与 Walk-Forward 复验。
"""


def _exit_policy_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["candidates"]:
        development = item["development"]
        validation = item["validation"]
        rows.append(
            "| {bars} | {passed} | {dev_pnl:.2f} | {dev_pf} | {dev_dd:.2%} | "
            "{dev_coverage:.2%} | {val_pnl:.2f} | {val_pf} | {val_dd:.2%} | "
            "{val_coverage:.2%} |".format(
                bars=item["wind_down_bars"],
                passed="通过" if item["passed"] else "未通过",
                dev_pnl=development["total_pnl"],
                dev_pf=_format_optional(development["profit_factor"]),
                dev_dd=development["max_drawdown_pct"],
                dev_coverage=development["trade_coverage"],
                val_pnl=validation["total_pnl"],
                val_pf=_format_optional(validation["profit_factor"]),
                val_dd=validation["max_drawdown_pct"],
                val_coverage=validation["trade_coverage"],
            )
        )
    return f"""# QuietGrid 终场库存控制诊断

生成时间：{report['generated_at']}

本报告只使用开发集与验证集；最终 OOS 保持封存且未执行。

通过候选：{report['passed_count']} / {report['candidate_count']}

| 提前停止开仓（分钟） | 结果 | 开发盈亏 | 开发 PF | 开发回撤 | 开发覆盖 | 验证盈亏 | 验证 PF | 验证回撤 | 验证覆盖 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

该实验只改变终场前停止新增库存的时间；未通过时不应继续围绕该单一变量细调。
"""


def _wind_down_maker_markdown(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    rows = []
    for item in report["candidates"]:
        dev = item["development"]
        val = item["validation"]
        dev_ops = item["development_operations"]
        val_ops = item["validation_operations"]
        rows.append(
            "| {policy} | {passed} | {dev_pnl:.2f} | {dev_pf} | "
            "{val_pnl:.2f} | {val_pf} | {dev_fills} | {val_fills} | "
            "{dev_exit:.2f} | {val_exit:.2f} |".format(
                policy=item["policy_id"],
                passed="通过" if item["passed"] else "未通过",
                dev_pnl=dev["total_pnl"],
                dev_pf=_format_optional(dev["profit_factor"]),
                val_pnl=val["total_pnl"],
                val_pf=_format_optional(val["profit_factor"]),
                dev_fills=dev_ops["maker_fill_count"],
                val_fills=val_ops["maker_fill_count"],
                dev_exit=dev_ops["stop_exit_pnl"],
                val_exit=val_ops["stop_exit_pnl"],
            )
        )
    sensitivity_rows = "\n".join(
        "| {probability} | {passed} | {dev_pnl:.2f} | {dev_pf} | "
        "{val_pnl:.2f} | {val_pf} |".format(
            probability=probability,
            passed="通过" if item["passed"] else "未通过",
            dev_pnl=item["development"]["total_pnl"],
            dev_pf=_format_optional(item["development"]["profit_factor"]),
            val_pnl=item["validation"]["total_pnl"],
            val_pf=_format_optional(item["validation"]["profit_factor"]),
        )
        for probability, item in report["fill_probability_sensitivity"].items()
    )
    walk_forward = report["walk_forward"]
    robustness = report["robustness"]
    cost_rows = "\n".join(
        "| {name} | {passed} | {maker:.3%} | {taker:.3%} | {slippage:.0f} | "
        "{dev_pnl:.2f} | {dev_pf} | {val_pnl:.2f} | {val_pf} |".format(
            name=name,
            passed="通过" if item["passed"] else "未通过",
            maker=item["maker_fee_rate"],
            taker=item["taker_fee_rate"],
            slippage=item["stop_slippage_bps"],
            dev_pnl=item["development"]["total_pnl"],
            dev_pf=_format_optional(item["development"]["profit_factor"]),
            val_pnl=item["validation"]["total_pnl"],
            val_pf=_format_optional(item["validation"]["profit_factor"]),
        )
        for name, item in report["cost_sensitivity"].items()
    )
    return f"""# QuietGrid 终场渐进 Maker 去库存诊断

生成时间：{report['generated_at']}

本报告只使用开发集和验证集；最终 OOS 保持封存且未执行。重挂价格使用当前已闭合
Bar 的收盘价，订单从下一根 Bar 起才允许成交。

基线：开发 {baseline['development']['total_pnl']:.2f} USDT / PF {_format_optional(baseline['development']['profit_factor'])}；
验证 {baseline['validation']['total_pnl']:.2f} USDT / PF {_format_optional(baseline['validation']['profit_factor'])}。

通过候选：{report['passed_count']} / {report['candidate_count']}

| 策略 | 结果 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF | 开发 Maker 去库存成交 | 验证 Maker 去库存成交 | 开发最终退出盈亏 | 验证最终退出盈亏 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## Maker 成交概率敏感性

锁定策略：`{report['selected_policy']['policy_id']}`

| Maker 成交概率 | 结果 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF |
|---:|---|---:|---:|---:|---:|
{sensitivity_rows}

## 费用与终场滑点压力

| 场景 | 结果 | Maker 单边 | Taker 单边 | 终场滑点 bps | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF |
|---|---|---:|---:|---:|---:|---:|---:|---:|
{cost_rows}

## Purged Walk-Forward

- 折数：{walk_forward['fold_count']}
- 每折隔离窗口：{walk_forward['purge_windows']}
- 拼接盈亏：{walk_forward['metrics']['total_pnl']:.2f} USDT
- 拼接 PF：{_format_optional(walk_forward['metrics']['profit_factor'])}
- 综合稳健性：{'通过' if robustness['passed'] else '未通过'}
"""


def _seed_sensitivity_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["seeds"]:
        base = item["scenarios"]["BASE"]
        cost50 = item["scenarios"]["COST_50"]
        rows.append(
            "| {seed} | {passed} | {dev:.2f} | {dev_pf} | {val:.2f} | "
            "{val_pf} | {c50_dev:.2f} | {c50_val:.2f} | {dd:.2%} |".format(
                seed=item["seed_salt"],
                passed=(
                    "通过" if item["all_cost_scenarios_passed"] else "未通过"
                ),
                dev=base["development"]["total_pnl"],
                dev_pf=_format_optional(base["development"]["profit_factor"]),
                val=base["validation"]["total_pnl"],
                val_pf=_format_optional(base["validation"]["profit_factor"]),
                c50_dev=cost50["development"]["total_pnl"],
                c50_val=cost50["validation"]["total_pnl"],
                dd=item["max_drawdown_pct"],
            )
        )
    scenario_rows = []
    for name, item in report["scenario_summary"].items():
        scenario_rows.append(
            "| {name} | {rate:.0%} | {dev_min:.2f} | {dev_med:.2f} | "
            "{val_min:.2f} | {val_med:.2f} | {dd:.2%} |".format(
                name=name,
                rate=item["pass_rate"],
                dev_min=item["development_pnl"]["min"],
                dev_med=item["development_pnl"]["median"],
                val_min=item["validation_pnl"]["min"],
                val_med=item["validation_pnl"]["median"],
                dd=item["max_drawdown_pct"],
            )
        )
    summary = report["summary"]
    backtest_policy = report["backtest_policy"]
    return f"""# QuietGrid 多随机 Seed 撮合敏感性

生成时间：{report['generated_at']}

固定参数：`{report['parameter']['parameter_id']}`
固定去库存策略：`{report['policy']['policy_id']}`
单侧未配对库存上限：{backtest_policy.get('max_unpaired_lots_per_side', 0)} 层
减仓目标：{backtest_policy.get('reduce_target_step_fraction', 1.0):.2f} 个完整网格步长
最终 OOS：`{report['split']['final_oos']['status']}`

| Seed salt | 全费用通过 | BASE 开发盈亏 | BASE 开发 PF | BASE 验证盈亏 | BASE 验证 PF | COST50 开发盈亏 | COST50 验证盈亏 | 最大回撤 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

## 分场景汇总

| 场景 | Seed 通过率 | 开发最差盈亏 | 开发中位盈亏 | 验证最差盈亏 | 验证中位盈亏 | 最差回撤 |
|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(scenario_rows)}

- BASE seed 通过率：{summary['base_pass_rate']:.0%}
- 全费用场景同时通过率：{summary['all_cost_pass_rate']:.0%}
- 最差 seed：{summary['worst_seed_salt']}
- 最差拆分盈亏：{summary['worst_split_pnl']:.2f} USDT
- 最差 seed 最大回撤：{summary['worst_seed_max_drawdown_pct']:.2%}
- 综合结论：{'通过' if summary['passed'] else '未通过'}
"""


def _joint_seed_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["seeds"]:
        base = item["scenarios"]["BASE"]
        cost50 = item["scenarios"]["COST_50"]
        rows.append(
            "| {seed} | {passed} | {dev:.2f} | {dev_pf} | {val:.2f} | "
            "{val_pf} | {c50_dev:.2f} | {c50_val:.2f} | {dd:.2%} |".format(
                seed=item["seed_salt"],
                passed=(
                    "通过" if item["all_cost_scenarios_passed"] else "未通过"
                ),
                dev=base["development"]["total_pnl"],
                dev_pf=_format_optional(base["development"]["profit_factor"]),
                val=base["validation"]["total_pnl"],
                val_pf=_format_optional(base["validation"]["profit_factor"]),
                c50_dev=cost50["development"]["total_pnl"],
                c50_val=cost50["validation"]["total_pnl"],
                dd=item["max_drawdown_pct"],
            )
        )
    policy_rows = "\n".join(
        "| {symbol} | {parameter} | {entry_filter} | {capital:.0f} | {inventory:.0f} | "
        "{unpaired} | {reduce:.2f} |".format(
            symbol=symbol,
            parameter=item["parameter"]["parameter_id"],
            entry_filter=(
                item["entry_filter"]["filter_id"]
                if item["entry_filter"] is not None
                else "无"
            ),
            capital=item["capital"],
            inventory=item["max_inventory_notional"],
            unpaired=item.get("max_unpaired_lots_per_side", 0),
            reduce=item.get("reduce_target_step_fraction", 1.0),
        )
        for symbol, item in report["symbol_policies"].items()
    )
    summary = report["summary"]
    return f"""# QuietGrid BTC + ETH 联合多 Seed 稳健性

生成时间：{report['generated_at']}

最终 OOS：`{report['split']['final_oos']['status']}`
联合口径：{report['protocol']['aggregation']}

| 标的 | 参数 | 入口过滤器 | 本金 | 最大库存 | 单侧未配对上限 | 减仓目标格数 |
|---|---|---|---:|---:|---:|---:|
{policy_rows}

| Seed salt | 全费用通过 | BASE 开发盈亏 | BASE 开发 PF | BASE 验证盈亏 | BASE 验证 PF | COST50 开发盈亏 | COST50 验证盈亏 | 保守最大回撤 |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

- BASE seed 通过率：{summary['base_pass_rate']:.0%}
- 全费用场景同时通过率：{summary['all_cost_pass_rate']:.0%}
- 最差 seed：{summary['worst_seed_salt']}
- 最差拆分盈亏：{summary['worst_split_pnl']:.2f} USDT
- 最差 seed 保守回撤：{summary['worst_seed_max_drawdown_pct']:.2%}
- 综合结论：{'通过' if summary['passed'] else '未通过'}
"""


def _joint_oos_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["seeds"]:
        base = item["scenarios"]["BASE"]
        cost50 = item["scenarios"]["COST_50"]
        base_symbols = base["operations"]["symbol_metrics"]
        rows.append(
            "| {seed} | {passed} | {base_pnl:.2f} | {base_pf} | {btc:.2f} | "
            "{eth:.2f} | {cost50:.2f} | {dd:.2%} |".format(
                seed=item["seed_salt"],
                passed="通过" if item["all_cost_scenarios_passed"] else "未通过",
                base_pnl=base["final_oos"]["total_pnl"],
                base_pf=_format_optional(base["final_oos"]["profit_factor"]),
                btc=base_symbols["BTCUSDT"]["total_pnl"],
                eth=base_symbols["ETHUSDT"]["total_pnl"],
                cost50=cost50["final_oos"]["total_pnl"],
                dd=item["max_drawdown_pct"],
            )
        )
    summary = report["summary"]
    return f"""# QuietGrid BTC + ETH 最终样本外验证

生成时间：{report['generated_at']}

状态：`{report['split']['final_oos']['status']}`
锁定报告 SHA-256：`{report['lock_report']['sha256']}`

本报告是锁定开发/验证策略后的最终 OOS 评估。该区间不得再用于选参或修改过滤器。
通过判定同时约束组合和 BTC、ETH 各标的，避免单一标的收益掩盖另一标的亏损。

| Seed salt | 全费用通过 | BASE 组合盈亏 | BASE PF | BTC BASE 盈亏 | ETH BASE 盈亏 | COST50 组合盈亏 | 最大回撤 |
|---:|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

- BASE seed 通过率：{summary['base_pass_rate']:.0%}
- 全费用场景同时通过率：{summary['all_cost_pass_rate']:.0%}
- 最差 seed：{summary['worst_seed_salt']}
- 最差 OOS 盈亏：{summary['worst_oos_pnl']:.2f} USDT
- 最差 seed 最大回撤：{summary['worst_seed_max_drawdown_pct']:.2%}
- 最终结论：{'通过' if summary['passed'] else '未通过'}

“通过”仅表示这份冻结历史和显式执行成本下具有稳健正期望证据，不保证未来收益。
"""


def _parameter_diagnostic_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["candidates"][:12]:
        parameter = item["parameter"]
        development = item["development"]
        validation = item["validation"]
        rows.append(
            "| {parameter_id} | {passed} | {dev_pnl:.2f} | {dev_pf} | "
            "{val_pnl:.2f} | {val_pf} | {dev_neighbors:.0%} | "
            "{val_neighbors:.0%} |".format(
                parameter_id=parameter["parameter_id"],
                passed="通过" if item["passed"] else "未通过",
                dev_pnl=development["total_pnl"],
                dev_pf=_format_optional(development["profit_factor"]),
                val_pnl=validation["total_pnl"],
                val_pf=_format_optional(validation["profit_factor"]),
                dev_neighbors=item["development_neighbor_positive_ratio"],
                val_neighbors=item["validation_neighbor_positive_ratio"],
            )
        )
    return f"""# QuietGrid 网格参数开发/验证诊断

生成时间：{report['generated_at']}

终场前停止新增库存：{report['backtest_policy']['wind_down_bars']} 分钟。
最大库存名义金额：{report['backtest_policy'].get('max_inventory_notional', 0):.0f} USDT。
最终 OOS 保持封存且未执行。

通过候选：{report['passed_count']} / {report['candidate_count']}

| 参数 | 结果 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF | 开发邻域为正 | 验证邻域为正 |
|---|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}

只有双时间段自身与相邻参数平台同时通过，才允许进入 Walk-Forward。
"""


def _window_diagnostic_markdown(report: dict[str, Any]) -> str:
    def reason_rows(segment: dict[str, Any]) -> str:
        return "\n".join(
            "| {status} | {reason} | {count} | {pnl:.2f} | {paired:.2f} | "
            "{exit_pnl:.2f} | {exit_cost:.2f} |".format(
                status=item["status"],
                reason=item["reason"],
                count=item["count"],
                pnl=item["total_pnl"],
                paired=item["paired_grid_pnl"],
                exit_pnl=item["stop_exit_pnl"],
                exit_cost=item["stop_exit_cost"],
            )
            for item in segment["reason_summary"]
        )

    worst = sorted(
        report["development"]["windows"],
        key=lambda item: float(item["pnl"]),
    )[:12]
    worst_rows = "\n".join(
        "| {symbol} | {window_id} | {reason} | {pnl:.2f} | {path:.2%} | "
        "{up:.2%} | {down:.2%} | {direction} | {efficiency:.3f} |".format(
            symbol=item["symbol"],
            window_id=item["window_id"],
            reason=item["reason"],
            pnl=item["pnl"],
            path=float(item["path_return_pct"] or 0.0),
            up=float(item["max_up_excursion_pct"] or 0.0),
            down=float(item["max_down_excursion_pct"] or 0.0),
            direction=(item["entry_features"] or {}).get("trend_direction", "—"),
            efficiency=float(
                (item["entry_features"] or {}).get("directional_efficiency", 0.0)
            ),
        )
        for item in worst
    )
    return f"""# QuietGrid 逐窗口损失机制诊断

生成时间：{report['generated_at']}

参数：`{report['parameter']['parameter_id']}`
终场前停止新增库存：{report['backtest_policy']['wind_down_bars']} 分钟。
最终 OOS 保持封存且未执行。

## 开发集损失分类

| 状态 | 原因 | 数量 | 总盈亏 | 配对网格盈亏 | 退出库存盈亏 | 退出费用 |
|---|---|---:|---:|---:|---:|---:|
{reason_rows(report['development'])}

## 验证集损失分类

| 状态 | 原因 | 数量 | 总盈亏 | 配对网格盈亏 | 退出库存盈亏 | 退出费用 |
|---|---|---:|---:|---:|---:|---:|
{reason_rows(report['validation'])}

## 开发集最差窗口

| 标的 | 窗口 | 原因 | 盈亏 | 全程收益 | 最大上行 | 最大下行 | 入场方向 | 方向效率 |
|---|---|---|---:|---:|---:|---:|---:|---:|
{worst_rows}
"""


def _inventory_diagnostic_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["candidates"]:
        development = item["development"]
        validation = item["validation"]
        rows.append(
            "| {cap:.0f} | {passed} | {dev_pnl:.2f} | {dev_pf} | {dev_dd:.2%} | "
            "{val_pnl:.2f} | {val_pf} | {val_dd:.2%} |".format(
                cap=item["max_inventory_notional"],
                passed="通过" if item["passed"] else "未通过",
                dev_pnl=development["total_pnl"],
                dev_pf=_format_optional(development["profit_factor"]),
                dev_dd=development["max_drawdown_pct"],
                val_pnl=validation["total_pnl"],
                val_pf=_format_optional(validation["profit_factor"]),
                val_dd=validation["max_drawdown_pct"],
            )
        )
    policy = report["backtest_policy"]
    return f"""# QuietGrid 库存上限开发/验证诊断

生成时间：{report['generated_at']}

终场前停止新增库存：{policy['wind_down_bars']} 分钟。
CAUTION / CRITICAL：{policy['inventory_caution_utilization']:.0%} / {policy['inventory_critical_utilization']:.0%}。
最终 OOS 保持封存且未执行。

通过候选：{report['passed_count']} / {report['candidate_count']}

| 最大库存名义金额 | 结果 | 开发盈亏 | 开发 PF | 开发回撤 | 验证盈亏 | 验证 PF | 验证回撤 |
|---:|---|---:|---:|---:|---:|---:|---:|
{chr(10).join(rows)}
"""


def _dynamic_mode_markdown(report: dict[str, Any]) -> str:
    rows = []
    for item in report["candidates"][:16]:
        development = item["development"]
        validation = item["validation"]
        dev_counts = item["development_modes"]["selection_counts"]
        val_counts = item["validation_modes"]["selection_counts"]
        rows.append(
            "| {rule_id} | {passed} | {dev_pnl:.2f} | {dev_pf} | "
            "{val_pnl:.2f} | {val_pf} | {dev_modes} | {val_modes} |".format(
                rule_id=item["rule_id"],
                passed="通过" if item["passed"] else "未通过",
                dev_pnl=development["total_pnl"],
                dev_pf=_format_optional(development["profit_factor"]),
                val_pnl=validation["total_pnl"],
                val_pf=_format_optional(validation["profit_factor"]),
                dev_modes=_format_mode_counts(dev_counts),
                val_modes=_format_mode_counts(val_counts),
            )
        )
    policy = report["backtest_policy"]
    baseline_rows = "\n".join(
        "| {mode} | {dev_pnl:.2f} | {dev_pf} | {val_pnl:.2f} | {val_pf} |".format(
            mode=mode,
            dev_pnl=values["development"]["total_pnl"],
            dev_pf=_format_optional(values["development"]["profit_factor"]),
            val_pnl=values["validation"]["total_pnl"],
            val_pf=_format_optional(values["validation"]["profit_factor"]),
        )
        for mode, values in report["fixed_mode_baselines"].items()
    )
    return f"""# QuietGrid 动态方向开发/验证诊断

生成时间：{report['generated_at']}

本报告只使用决策时已经闭合的历史 K 线；窗口后续涨跌不参与方向选择。
终场前停止新增库存：{policy['wind_down_bars']} 分钟。
最大库存名义金额：{policy['max_inventory_notional']:.0f} USDT。
最终 OOS 保持封存且未执行。

通过候选：{report['passed_count']} / {report['candidate_count']}

## 固定方向基线

| 模式 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF |
|---|---:|---:|---:|---:|
{baseline_rows}

## 动态规则

| 规则 | 结果 | 开发盈亏 | 开发 PF | 验证盈亏 | 验证 PF | 开发模式分布 | 验证模式分布 |
|---|---|---:|---:|---:|---:|---|---|
{chr(10).join(rows)}

只有开发集和验证集同时通过收益、PF、回撤、覆盖率和集中度门槛，才允许进入参数邻域与 Walk-Forward 复验。
"""


def _format_mode_counts(values: dict[str, Any]) -> str:
    return ", ".join(
        f"{key}:{int(value)}"
        for key, value in sorted(values.items())
    ) or "—"


def _split_summary(ids: Sequence[str]) -> dict[str, Any]:
    return {
        "count": len(ids),
        "start": ids[0] if ids else None,
        "end": ids[-1] if ids else None,
    }


def _parameter_payload(item: ParameterSet) -> dict[str, Any]:
    return {
        "parameter_id": item.parameter_id,
        "range_multiplier": item.range_multiplier,
        "min_step_pct": item.min_step_pct,
        "stop_buffer_pct": item.stop_buffer_pct,
        "direction_mode": item.direction_mode.value,
    }


def _metrics_payload(item: AggregateMetrics) -> dict[str, Any]:
    return asdict(item)


def _seed_scenario_summary(
    seed_results: Sequence[dict[str, Any]],
    scenario_name: str,
) -> dict[str, Any]:
    scenarios = [item["scenarios"][scenario_name] for item in seed_results]

    def summary(split_name: str, metric_name: str) -> dict[str, float | None]:
        values = [
            float(item[split_name][metric_name])
            for item in scenarios
            if item[split_name][metric_name] is not None
        ]
        if not values:
            return {"min": None, "median": None, "max": None}
        return {
            "min": min(values),
            "median": statistics.median(values),
            "max": max(values),
        }

    passed = sum(bool(item["passed"]) for item in scenarios)
    return {
        "pass_count": passed,
        "pass_rate": passed / len(scenarios),
        "development_pnl": summary("development", "total_pnl"),
        "development_profit_factor": summary("development", "profit_factor"),
        "validation_pnl": summary("validation", "total_pnl"),
        "validation_profit_factor": summary("validation", "profit_factor"),
        "max_drawdown_pct": max(
            max(
                float(item["development"]["max_drawdown_pct"]),
                float(item["validation"]["max_drawdown_pct"]),
            )
            for item in scenarios
        ),
    }


def _format_optional(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.3f}"


def _stable_seed(*parts: str) -> int:
    digest = hashlib.blake2b("|".join(parts).encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big")


def _row_to_csv(row: NormalizedKline) -> dict[str, Any]:
    return {
        "open_time": row.open_time,
        "open": f"{row.open:.16g}",
        "high": f"{row.high:.16g}",
        "low": f"{row.low:.16g}",
        "close": f"{row.close:.16g}",
        "volume": f"{row.volume:.16g}",
        "close_time": row.close_time,
        "quote_volume": f"{row.quote_volume:.16g}",
        "trade_count": row.trade_count,
    }


def _csv_to_row(raw: dict[str, str]) -> NormalizedKline:
    return NormalizedKline(
        open_time=int(raw["open_time"]),
        close_time=int(raw["close_time"]),
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=float(raw.get("volume") or 0.0),
        quote_volume=float(raw.get("quote_volume") or 0.0),
        trade_count=int(raw.get("trade_count") or 0),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    staging = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        staging.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(staging, path)
    finally:
        if staging.exists():
            staging.unlink()
