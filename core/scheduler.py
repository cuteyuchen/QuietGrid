from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal


NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Scheduler:
    force_close_minutes: int = 120
    calendar_name: str = "NYSE"

    def __post_init__(self) -> None:
        object.__setattr__(self, "_calendar", mcal.get_calendar(self.calendar_name))

    def is_in_window(self, now_utc: datetime | None = None) -> bool:
        now = self._normalize_utc(now_utc)
        if self._is_regular_market_open(now):
            return False
        return self.minutes_to_next_open(now) > self.force_close_minutes

    def minutes_to_next_open(self, now_utc: datetime | None = None) -> float:
        now = self._normalize_utc(now_utc)
        next_premarket = self._next_premarket_open(now)
        return (next_premarket - now).total_seconds() / 60

    def should_force_close(self, now_utc: datetime | None = None) -> bool:
        now = self._normalize_utc(now_utc)
        return not self._is_regular_market_open(now) and self.minutes_to_next_open(now) <= self.force_close_minutes

    def get_next_window_start(self, now_utc: datetime | None = None) -> datetime:
        now = self._normalize_utc(now_utc)
        schedule = self._schedule_around(now, days_back=1, days_forward=14)
        for _, row in schedule.iterrows():
            close_at = row["market_close"].to_pydatetime().astimezone(timezone.utc)
            if close_at > now:
                return close_at
        raise RuntimeError("无法在未来 14 天内找到 NYSE 收盘时间。")

    def _is_regular_market_open(self, now_utc: datetime) -> bool:
        schedule = self._schedule_around(now_utc, days_back=1, days_forward=1)
        for _, row in schedule.iterrows():
            open_at = row["market_open"].to_pydatetime().astimezone(timezone.utc)
            close_at = row["market_close"].to_pydatetime().astimezone(timezone.utc)
            if open_at <= now_utc < close_at:
                return True
        return False

    def _next_premarket_open(self, now_utc: datetime) -> datetime:
        schedule = self._schedule_around(now_utc, days_back=0, days_forward=14)
        for _, row in schedule.iterrows():
            market_open_utc = row["market_open"].to_pydatetime().astimezone(timezone.utc)
            market_open_ny = market_open_utc.astimezone(NY_TZ)
            premarket_ny = datetime.combine(market_open_ny.date(), time(hour=4), tzinfo=NY_TZ)
            premarket_utc = premarket_ny.astimezone(timezone.utc)
            if market_open_utc > now_utc:
                return premarket_utc
        raise RuntimeError("无法在未来 14 天内找到下一次盘前开始时间。")

    def _schedule_around(self, now_utc: datetime, days_back: int, days_forward: int):
        now_ny = now_utc.astimezone(NY_TZ).date()
        start = now_ny - timedelta(days=days_back)
        end = now_ny + timedelta(days=days_forward)
        return self._calendar.schedule(start_date=start, end_date=end)

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("时间必须包含 timezone。")
        return value.astimezone(timezone.utc)
