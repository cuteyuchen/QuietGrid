from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


class SupportsKlines(Protocol):
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class RuntimeKlineQuality:
    valid: bool
    required_bars: int
    actual_bars: int
    missing_count: int
    duplicate_count: int
    conflicting_duplicate_count: int
    stale_seconds: float
    reason_codes: tuple[str, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "required_bars": self.required_bars,
            "actual_bars": self.actual_bars,
            "missing_count": self.missing_count,
            "duplicate_count": self.duplicate_count,
            "conflicting_duplicate_count": self.conflicting_duplicate_count,
            "stale_seconds": self.stale_seconds,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class RecentKlineBatch:
    symbol: str
    interval: str
    requested_limit: int
    rows: tuple[dict[str, Any], ...]
    first_open_time: datetime
    last_close_time: datetime
    age_seconds: float
    missing_count: int
    duplicate_count: int
    quality: RuntimeKlineQuality


class RecentMarketHistoryService:
    def __init__(
        self,
        exchange: SupportsKlines,
        *,
        max_data_age_seconds: float = 90.0,
        interval_ms: int = 60_000,
        allow_gaps: bool = False,
    ) -> None:
        self.exchange = exchange
        self.max_data_age_seconds = float(max_data_age_seconds)
        self.interval_ms = int(interval_ms)
        self.allow_gaps = bool(allow_gaps)

    async def load_closed_klines(
        self,
        symbol: str,
        *,
        interval: str,
        required_bars: int,
        as_of: datetime,
        buffer_bars: int = 2,
    ) -> RecentKlineBatch:
        if required_bars < 1:
            raise ValueError("required_bars must be >= 1")
        if as_of.tzinfo is None:
            raise ValueError("as_of must include timezone")
        as_of_utc = as_of.astimezone(timezone.utc)
        as_of_ms = int(as_of_utc.timestamp() * 1000)
        request_limit = int(required_bars) + max(0, int(buffer_bars))
        raw_rows = await self.exchange.get_klines(symbol, interval, request_limit)
        normalized = [_normalize_kline_row(row, self.interval_ms) for row in raw_rows]
        normalized = [row for row in normalized if row is not None]
        if not normalized and raw_rows:
            # 兼容 mock / 旧适配器：仅有 OHLC、无时间戳时，按 as_of 向前合成已闭合 1m 轴。
            normalized = _synthesize_closed_timeline(raw_rows, as_of_ms, self.interval_ms)
        normalized.sort(key=lambda row: int(row["open_time"]))

        deduped: list[dict[str, Any]] = []
        duplicate_count = 0
        conflicting_duplicate_count = 0
        seen: dict[int, dict[str, Any]] = {}
        for row in normalized:
            open_time = int(row["open_time"])
            existing = seen.get(open_time)
            if existing is None:
                seen[open_time] = row
                deduped.append(row)
                continue
            duplicate_count += 1
            if not _rows_equal(existing, row):
                conflicting_duplicate_count += 1

        closed_rows = [row for row in deduped if int(row["close_time"]) < as_of_ms]
        reasons: list[str] = []
        if conflicting_duplicate_count:
            reasons.append("DATA_CONFLICTING_DUPLICATE")
        for row in closed_rows:
            if not _ohlc_valid(row):
                reasons.append("DATA_OHLC_INVALID")
                break

        missing_count = 0
        if closed_rows:
            for left, right in zip(closed_rows, closed_rows[1:]):
                gap = int(right["open_time"]) - int(left["open_time"])
                if gap != self.interval_ms:
                    expected = max(0, (gap // self.interval_ms) - 1)
                    missing_count += expected
            if missing_count and not self.allow_gaps:
                reasons.append("DATA_GAP")

        if len(closed_rows) < required_bars:
            reasons.append("DATA_INSUFFICIENT")
            selected = tuple(closed_rows)
        else:
            selected = tuple(closed_rows[-required_bars:])

        last_close_ms = int(selected[-1]["close_time"]) if selected else 0
        first_open_ms = int(selected[0]["open_time"]) if selected else 0
        age_seconds = max(0.0, (as_of_ms - last_close_ms) / 1000.0) if selected else float("inf")
        if selected and age_seconds > self.max_data_age_seconds:
            reasons.append("DATA_STALE")

        quality = RuntimeKlineQuality(
            valid=not reasons,
            required_bars=int(required_bars),
            actual_bars=len(selected),
            missing_count=int(missing_count),
            duplicate_count=int(duplicate_count),
            conflicting_duplicate_count=int(conflicting_duplicate_count),
            stale_seconds=float(age_seconds if selected else 0.0),
            reason_codes=tuple(reasons),
        )
        if not selected:
            raise RuntimeError("DATA_INSUFFICIENT: no closed klines")
        if reasons:
            raise RuntimeError(";".join(reasons))
        return RecentKlineBatch(
            symbol=symbol,
            interval=interval,
            requested_limit=request_limit,
            rows=selected,
            first_open_time=datetime.fromtimestamp(first_open_ms / 1000, tz=timezone.utc),
            last_close_time=datetime.fromtimestamp(last_close_ms / 1000, tz=timezone.utc),
            age_seconds=float(age_seconds),
            missing_count=int(missing_count),
            duplicate_count=int(duplicate_count),
            quality=quality,
        )


def _normalize_kline_row(row: dict[str, Any], interval_ms: int) -> dict[str, Any] | None:
    try:
        open_raw = row.get("open_time") if row.get("open_time") is not None else row.get("openTime")
        if open_raw is None:
            return None
        open_time = int(open_raw)
        close_time_raw = row.get("close_time") if row.get("close_time") is not None else row.get("closeTime")
        close_time = int(close_time_raw) if close_time_raw is not None else open_time + interval_ms - 1
        open_price = float(row["open"])
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        volume = float(row.get("volume") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "open_time": open_time,
        "close_time": close_time,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _synthesize_closed_timeline(
    raw_rows: list[dict[str, Any]],
    as_of_ms: int,
    interval_ms: int,
) -> list[dict[str, Any]]:
    last_open = ((as_of_ms // interval_ms) - 1) * interval_ms
    rows: list[dict[str, Any]] = []
    count = len(raw_rows)
    for idx, raw in enumerate(raw_rows):
        try:
            open_price = float(raw["open"])
            high = float(raw["high"])
            low = float(raw["low"])
            close = float(raw["close"])
            volume = float(raw.get("volume") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        open_time = last_open - (count - 1 - idx) * interval_ms
        rows.append(
            {
                "open_time": open_time,
                "close_time": open_time + interval_ms - 1,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return rows


def _rows_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("open", "high", "low", "close", "volume", "close_time")
    return all(left.get(key) == right.get(key) for key in keys)


def _ohlc_valid(row: dict[str, Any]) -> bool:
    open_price = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    if min(open_price, high, low, close) <= 0:
        return False
    if high < low:
        return False
    if high < max(open_price, close):
        return False
    if low > min(open_price, close):
        return False
    return True
