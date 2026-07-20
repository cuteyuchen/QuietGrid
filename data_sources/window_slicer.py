"""按 NYSE 真实交易日历切分 QuietGrid 休市回测窗口。"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from data_sources.models import NormalizedKline


NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class BacktestWindow:
    window_id: str
    market_close: datetime
    force_close_at: datetime
    rows: tuple[NormalizedKline, ...]
    row_start_index: int
    row_end_index: int
    observation_rows: int
    tradable_rows: int
    status: str
    skip_reason: str | None = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "market_close": self.market_close.isoformat(),
            "force_close_at": self.force_close_at.isoformat(),
            "row_start_index": self.row_start_index,
            "row_end_index": self.row_end_index,
            "row_count": self.row_count,
            "observation_rows": self.observation_rows,
            "tradable_rows": self.tradable_rows,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "warning": _skip_reason_label(self.skip_reason),
        }


class NyseWindowSlicer:
    def __init__(
        self,
        *,
        force_close_minutes: int = 120,
        minimum_tradable_rows: int = 30,
        calendar_name: str = "NYSE",
    ) -> None:
        if force_close_minutes < 0:
            raise ValueError("force_close_minutes 不能为负数。")
        if minimum_tradable_rows < 1:
            raise ValueError("minimum_tradable_rows 必须为正整数。")
        self.force_close_minutes = int(force_close_minutes)
        self.minimum_tradable_rows = int(minimum_tradable_rows)
        self.calendar_name = calendar_name
        self._calendar = mcal.get_calendar(calendar_name)

    def estimate_window_count(self, start_time: datetime, end_time: datetime) -> int:
        start = _utc(start_time)
        end = _utc(end_time)
        if start >= end:
            raise ValueError("start_time 必须早于 end_time。")
        return sum(
            1
            for market_close, force_close_at in self._calendar_windows(start, end)
            if market_close < end and force_close_at > start
        )

    def slice(
        self,
        rows: Iterable[NormalizedKline],
        observation_rows: int,
    ) -> list[BacktestWindow]:
        ordered = tuple(sorted(rows, key=lambda item: item.open_time))
        if not ordered:
            return []
        if observation_rows < 0:
            raise ValueError("observation_rows 不能为负数。")
        open_times = [row.open_time for row in ordered]
        data_start = ordered[0].open_datetime
        data_end = datetime.fromtimestamp(
            ordered[-1].close_time / 1000,
            tz=timezone.utc,
        )
        windows: list[BacktestWindow] = []
        for market_close, force_close_at in self._calendar_windows(data_start, data_end):
            if market_close >= data_end or force_close_at <= data_start:
                continue
            start_ms = int(market_close.timestamp() * 1000)
            force_close_ms = int(force_close_at.timestamp() * 1000)
            start_index = bisect_left(open_times, start_ms)
            end_index = bisect_left(open_times, force_close_ms)
            while (
                end_index > start_index
                and ordered[end_index - 1].close_time >= force_close_ms
            ):
                end_index -= 1
            window_rows = ordered[start_index:end_index]
            tradable_rows = max(0, len(window_rows) - observation_rows)
            skip_reason = _window_skip_reason(
                len(window_rows),
                observation_rows,
                tradable_rows,
                self.minimum_tradable_rows,
            )
            windows.append(
                BacktestWindow(
                    window_id=f"nyse_{market_close.strftime('%Y%m%dT%H%M%SZ')}",
                    market_close=market_close,
                    force_close_at=force_close_at,
                    rows=window_rows,
                    row_start_index=start_index,
                    row_end_index=end_index,
                    observation_rows=observation_rows,
                    tradable_rows=tradable_rows,
                    status="SKIPPED" if skip_reason else "READY",
                    skip_reason=skip_reason,
                )
            )
        return windows

    def _calendar_windows(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> list[tuple[datetime, datetime]]:
        start_ny = start_time.astimezone(NY_TZ).date() - timedelta(days=14)
        end_ny = end_time.astimezone(NY_TZ).date() + timedelta(days=14)
        schedule = self._calendar.schedule(start_date=start_ny, end_date=end_ny)
        sessions = list(schedule.iterrows())
        result: list[tuple[datetime, datetime]] = []
        for index in range(len(sessions) - 1):
            current_label, current = sessions[index]
            following_label, following = sessions[index + 1]
            current_date = current_label.date()
            following_date = following_label.date()
            if (following_date - current_date).days <= 1:
                continue
            market_close = current["market_close"].to_pydatetime().astimezone(timezone.utc)
            next_open = following["market_open"].to_pydatetime().astimezone(timezone.utc)
            next_open_ny = next_open.astimezone(NY_TZ)
            premarket = datetime.combine(
                next_open_ny.date(),
                time(hour=4),
                tzinfo=NY_TZ,
            ).astimezone(timezone.utc)
            force_close_at = premarket - timedelta(minutes=self.force_close_minutes)
            if force_close_at > market_close:
                result.append((market_close, force_close_at))
        return result


def normalized_klines_from_mappings(
    rows: Iterable[Mapping[str, Any]],
    *,
    interval_ms: int,
) -> list[NormalizedKline]:
    normalized: list[NormalizedKline] = []
    for index, row in enumerate(rows, start=1):
        open_time = _timestamp_ms(row.get("open_time", row.get("timestamp")), index)
        close_raw = row.get("close_time")
        close_time = (
            _timestamp_ms(close_raw, index)
            if close_raw not in (None, "")
            else open_time + interval_ms - 1
        )
        try:
            normalized.append(
                NormalizedKline(
                    open_time=open_time,
                    close_time=close_time,
                    open=float(row.get("open", row["close"])),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                    quote_volume=float(row.get("quote_volume") or 0.0),
                    trade_count=int(float(row.get("trade_count") or 0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"第 {index} 根 K 线无法用于休市窗口切分: {exc}") from exc
    return normalized


def _timestamp_ms(value: Any, row_index: int) -> int:
    if value in (None, ""):
        raise ValueError(f"第 {row_index} 根 K 线缺少时间戳。")
    text = str(value).strip()
    try:
        numeric = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"第 {row_index} 根 K 线时间戳无效。") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    return int(numeric if abs(numeric) >= 100_000_000_000 else numeric * 1000)


def _window_skip_reason(
    row_count: int,
    observation_rows: int,
    tradable_rows: int,
    minimum_tradable_rows: int,
) -> str | None:
    if row_count == 0:
        return "NO_DATA"
    if row_count <= observation_rows:
        return "INSUFFICIENT_OBSERVATION_ROWS"
    if tradable_rows < minimum_tradable_rows:
        return "INSUFFICIENT_TRADABLE_ROWS"
    return None


def _skip_reason_label(reason: str | None) -> str | None:
    labels = {
        "NO_DATA": "该休市窗口没有可用 K 线。",
        "INSUFFICIENT_OBSERVATION_ROWS": "该休市窗口不足以完成观察期。",
        "INSUFFICIENT_TRADABLE_ROWS": "观察期后剩余 K 线不足，已跳过。",
    }
    return labels.get(reason) if reason else None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("时间必须包含 timezone。")
    return value.astimezone(timezone.utc)
