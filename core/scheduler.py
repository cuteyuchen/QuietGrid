from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from strategy.window_models import TradingWindow, WindowKind


NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Scheduler:
    force_close_minutes: int = 120
    calendar_name: str = "NYSE"
    minimum_trade_minutes: int = 120
    allowed_window_kinds: tuple[WindowKind, ...] = (WindowKind.WEEKEND, WindowKind.HOLIDAY)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_calendar", mcal.get_calendar(self.calendar_name))

    def is_in_window(self, now_utc: datetime | None = None) -> bool:
        return self.classify_window(now_utc).allowed

    def minutes_to_next_open(self, now_utc: datetime | None = None) -> float:
        now = self._normalize_utc(now_utc)
        next_premarket = self._next_premarket_open(now)
        return (next_premarket - now).total_seconds() / 60

    def should_force_close(self, now_utc: datetime | None = None) -> bool:
        window = self.classify_window(now_utc)
        return window.kind == WindowKind.FORCE_CLOSE_BUFFER

    def get_next_window_start(self, now_utc: datetime | None = None) -> datetime:
        now = self._normalize_utc(now_utc)
        schedule = self._schedule_around(now, days_back=1, days_forward=14)
        for _, row in schedule.iterrows():
            close_at = row["market_close"].to_pydatetime().astimezone(timezone.utc)
            if close_at > now:
                return close_at
        raise RuntimeError("无法在未来 14 天内找到 NYSE 收盘时间。")

    def classify_window(
        self,
        now_utc: datetime | None = None,
        *,
        allowed_kinds: set[WindowKind] | None = None,
    ) -> TradingWindow:
        now = self._normalize_utc(now_utc)
        kinds = allowed_kinds or set(self.allowed_window_kinds)
        if self._is_regular_market_open(now):
            next_premarket = self._next_premarket_open(now)
            return TradingWindow(
                kind=WindowKind.REGULAR_OPEN,
                allowed=False,
                window_key="",
                previous_market_close=None,
                next_market_open=self._next_market_open(now),
                next_premarket_open=next_premarket,
                force_close_at=None,
                minutes_to_force_close=0.0,
                reason="常规交易时段，不允许网格交易。",
            )

        previous_close = self._previous_market_close(now)
        next_market_open = self._next_market_open(now)
        next_premarket = self._next_premarket_open(now)
        minutes_to_premarket = (next_premarket - now).total_seconds() / 60
        force_close_at = next_premarket - timedelta(minutes=self.force_close_minutes)
        minutes_to_force_close = (force_close_at - now).total_seconds() / 60
        window_key = self.current_window_key(previous_close, next_premarket)

        if minutes_to_premarket <= self.force_close_minutes:
            return TradingWindow(
                kind=WindowKind.FORCE_CLOSE_BUFFER,
                allowed=False,
                window_key=window_key,
                previous_market_close=previous_close,
                next_market_open=next_market_open,
                next_premarket_open=next_premarket,
                force_close_at=force_close_at,
                minutes_to_force_close=max(0.0, minutes_to_force_close),
                reason="已进入强制离场缓冲，禁止新开仓。",
            )

        kind = self._classify_closed_kind(previous_close, next_market_open)
        reason_map = {
            WindowKind.WEEKEND: "周末长休市窗口。",
            WindowKind.HOLIDAY: "交易所节假日长休市窗口。",
            WindowKind.WEEKDAY_OVERNIGHT: "普通工作日隔夜窗口，正式策略禁止交易。",
        }
        allowed = kind in kinds
        reason = reason_map.get(kind, "未知窗口。")
        required_minutes = self.force_close_minutes + self.minimum_trade_minutes
        if allowed and minutes_to_premarket <= required_minutes:
            allowed = False
            reason = "距盘前时间不足以完成最小交易窗口。"
        return TradingWindow(
            kind=kind,
            allowed=allowed,
            window_key=window_key,
            previous_market_close=previous_close,
            next_market_open=next_market_open,
            next_premarket_open=next_premarket,
            force_close_at=force_close_at,
            minutes_to_force_close=max(0.0, minutes_to_force_close),
            reason=reason,
        )

    def current_window_key(
        self,
        previous_market_close: datetime | None,
        next_premarket_open: datetime | None,
    ) -> str:
        prev = previous_market_close.isoformat() if previous_market_close else "none"
        nxt = next_premarket_open.isoformat() if next_premarket_open else "none"
        return f"NYSE:{prev}:{nxt}"

    def _classify_closed_kind(
        self,
        previous_close: datetime | None,
        next_market_open: datetime | None,
    ) -> WindowKind:
        if previous_close is None or next_market_open is None:
            return WindowKind.WEEKEND
        prev_day = previous_close.astimezone(NY_TZ).date()
        next_day = next_market_open.astimezone(NY_TZ).date()
        day_gap = (next_day - prev_day).days
        if day_gap <= 1:
            return WindowKind.WEEKDAY_OVERNIGHT
        # 周五收盘到周一开盘：通常 3 天；节假日可能更长。
        # 若中间跨越周六/周日则优先 WEEKEND，否则 HOLIDAY。
        cursor = prev_day + timedelta(days=1)
        saw_weekend = False
        while cursor < next_day:
            if cursor.weekday() >= 5:
                saw_weekend = True
                break
            cursor += timedelta(days=1)
        if saw_weekend:
            return WindowKind.WEEKEND
        return WindowKind.HOLIDAY

    def _is_regular_market_open(self, now_utc: datetime) -> bool:
        schedule = self._schedule_around(now_utc, days_back=1, days_forward=1)
        for _, row in schedule.iterrows():
            open_at = row["market_open"].to_pydatetime().astimezone(timezone.utc)
            close_at = row["market_close"].to_pydatetime().astimezone(timezone.utc)
            if open_at <= now_utc < close_at:
                return True
        return False

    def _previous_market_close(self, now_utc: datetime) -> datetime | None:
        schedule = self._schedule_around(now_utc, days_back=14, days_forward=1)
        previous: datetime | None = None
        for _, row in schedule.iterrows():
            close_at = row["market_close"].to_pydatetime().astimezone(timezone.utc)
            if close_at <= now_utc:
                previous = close_at
            else:
                break
        return previous

    def _next_market_open(self, now_utc: datetime) -> datetime | None:
        schedule = self._schedule_around(now_utc, days_back=0, days_forward=14)
        for _, row in schedule.iterrows():
            open_at = row["market_open"].to_pydatetime().astimezone(timezone.utc)
            if open_at > now_utc:
                return open_at
        return None

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
