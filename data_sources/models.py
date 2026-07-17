"""历史数据链路使用的稳定领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite
from typing import Any


class DatasetStatus(StrEnum):
    CREATED = "CREATED"
    DOWNLOADING = "DOWNLOADING"
    NORMALIZING = "NORMALIZING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    READY_WITH_WARNINGS = "READY_WITH_WARNINGS"
    FAILED = "FAILED"
    CORRUPTED = "CORRUPTED"
    DELETED = "DELETED"


@dataclass(frozen=True)
class DatasetRequest:
    provider: str
    symbol: str
    interval: str
    start_time: datetime
    end_time: datetime
    window_mode: str = "NYSE_CLOSED_ONLY"

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider 不能为空。")
        if not self.symbol.strip():
            raise ValueError("symbol 不能为空。")
        if not self.interval.strip():
            raise ValueError("interval 不能为空。")
        if not _is_timezone_aware(self.start_time) or not _is_timezone_aware(self.end_time):
            raise ValueError("start_time 和 end_time 必须包含时区。")
        if self.start_time >= self.end_time:
            raise ValueError("start_time 必须早于 end_time。")
        object.__setattr__(self, "provider", self.provider.strip().lower())
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "interval", self.interval.strip())
        object.__setattr__(self, "window_mode", self.window_mode.strip().upper())


@dataclass(frozen=True)
class HistoricalSymbol:
    symbol: str
    status: str = "TRADING"
    market: str = ""
    base_asset: str = ""
    quote_asset: str = ""


@dataclass(frozen=True)
class DatasetPreview:
    provider: str
    symbol: str
    interval: str
    start_time: datetime
    end_time: datetime
    estimated_rows: int
    estimated_pages: int = 0
    estimated_size_bytes: int = 0
    cache_hit: bool = False
    window_count: int | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedKline:
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    quote_volume: float = 0.0
    trade_count: int = 0

    def __post_init__(self) -> None:
        if self.open_time < 0 or self.close_time <= self.open_time:
            raise ValueError("K线时间范围无效。")
        prices = (self.open, self.high, self.low, self.close)
        if not all(isfinite(value) and value > 0 for value in prices):
            raise ValueError("K线 OHLC 必须是有限正数。")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close) or self.high < self.low:
            raise ValueError("K线 OHLC 关系无效。")
        if not isfinite(self.volume) or self.volume < 0:
            raise ValueError("K线成交量必须是有限非负数。")
        if not isfinite(self.quote_volume) or self.quote_volume < 0:
            raise ValueError("K线成交额必须是有限非负数。")
        if self.trade_count < 0:
            raise ValueError("K线成交笔数不能为负数。")

    @property
    def open_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.open_time / 1000, tz=timezone.utc)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "timestamp": self.open_time,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "trade_count": self.trade_count,
        }


@dataclass
class DatasetQualityReport:
    input_rows: int = 0
    output_rows: int = 0
    duplicate_rows: int = 0
    conflicting_duplicates: int = 0
    missing_intervals: int = 0
    max_consecutive_missing: int = 0
    unclosed_rows: int = 0
    first_open_time: int | None = None
    last_open_time: int | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def missing_ratio(self) -> float:
        expected = self.output_rows + self.missing_intervals
        return self.missing_intervals / expected if expected else 0.0

    @property
    def status(self) -> DatasetStatus:
        if self.errors:
            return DatasetStatus.FAILED
        if self.warnings:
            return DatasetStatus.READY_WITH_WARNINGS
        return DatasetStatus.READY

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "duplicate_rows": self.duplicate_rows,
            "conflicting_duplicates": self.conflicting_duplicates,
            "missing_intervals": self.missing_intervals,
            "missing_ratio": self.missing_ratio,
            "max_consecutive_missing": self.max_consecutive_missing,
            "unclosed_rows": self.unclosed_rows,
            "first_open_time": self.first_open_time,
            "last_open_time": self.last_open_time,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def _is_timezone_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None
