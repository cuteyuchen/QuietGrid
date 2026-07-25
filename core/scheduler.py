from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from strategy.window_models import TradingWindow, WindowKind


@dataclass(frozen=True)
class Scheduler:
    """Classify exchange-closed trading windows for one reference market.

    Defaults preserve the original NYSE behaviour.  Markets without a distinct
    pre-market session (for example KRX) can set ``premarket_time=None`` so the
    next regular market open becomes the exit reference.
    """

    force_close_minutes: int = 120
    calendar_name: str = "NYSE"
    minimum_trade_minutes: int = 120
    allowed_window_kinds: tuple[WindowKind, ...] = (
        WindowKind.WEEKEND,
        WindowKind.HOLIDAY,
    )
    market_timezone: str = "America/New_York"
    premarket_time: time | None = time(hour=4)
    window_key_prefix: str | None = None

    def __post_init__(self) -> None:
        if self.force_close_minutes < 0:
            raise ValueError("force_close_minutes 不能为负数。")
        if self.minimum_trade_minutes < 0:
            raise ValueError("minimum_trade_minutes 不能为负数。")
        object.__setattr__(self, "_market_tz", ZoneInfo(self.market_timezone))
        object.__setattr__(self, "_calendar", mcal.get_calendar(self.calendar_name))

    def is_in_window(self, now_utc: datetime | None = None) -> bool:
        return self.classify_window(now_utc).allowed

    def minutes_to_next_open(self, now_utc: datetime | None = None) -> float:
        now = self._normalize_utc(now_utc)
        next_reference_open = self._next_reference_open(now)
        return (next_reference_open - now).total_seconds() / 60

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
        raise RuntimeError(
            f"无法在未来 14 天内找到 {self.calendar_name} 收盘时间。"
        )

    def classify_window(
        self,
        now_utc: datetime | None = None,
        *,
        allowed_kinds: set[WindowKind] | None = None,
    ) -> TradingWindow:
        now = self._normalize_utc(now_utc)
        kinds = allowed_kinds or set(self.allowed_window_kinds)
        if self._is_regular_market_open(now):
            next_reference_open = self._next_reference_open(now)
            return TradingWindow(
                kind=WindowKind.REGULAR_OPEN,
                allowed=False,
                window_key="",
                previous_market_close=None,
                next_market_open=self._next_market_open(now),
                next_premarket_open=next_reference_open,
                force_close_at=None,
                minutes_to_force_close=0.0,
                reason="常规交易时段，不允许网格交易。",
            )

        previous_close = self._previous_market_close(now)
        next_market_open = self._next_market_open(now)
        next_reference_open = self._next_reference_open(now)
        minutes_to_reference_open = (
            next_reference_open - now
        ).total_seconds() / 60
        force_close_at = next_reference_open - timedelta(
            minutes=self.force_close_minutes
        )
        minutes_to_force_close = (force_close_at - now).total_seconds() / 60
        window_key = self.current_window_key(previous_close, next_reference_open)

        if minutes_to_reference_open <= self.force_close_minutes:
            return TradingWindow(
                kind=WindowKind.FORCE_CLOSE_BUFFER,
                allowed=False,
                window_key=window_key,
                previous_market_close=previous_close,
                next_market_open=next_market_open,
                next_premarket_open=next_reference_open,
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
        if allowed and minutes_to_reference_open <= required_minutes:
            allowed = False
            reason = "距参考市场开盘时间不足以完成最小交易窗口。"
        return TradingWindow(
            kind=kind,
            allowed=allowed,
            window_key=window_key,
            previous_market_close=previous_close,
            next_market_open=next_market_open,
            next_premarket_open=next_reference_open,
            force_close_at=force_close_at,
            minutes_to_force_close=max(0.0, minutes_to_force_close),
            reason=reason,
        )

    def current_window_key(
        self,
        previous_market_close: datetime | None,
        next_reference_open: datetime | None,
    ) -> str:
        prev = previous_market_close.isoformat() if previous_market_close else "none"
        nxt = next_reference_open.isoformat() if next_reference_open else "none"
        prefix = self.window_key_prefix or self.calendar_name
        return f"{prefix}:{prev}:{nxt}"

    def _classify_closed_kind(
        self,
        previous_close: datetime | None,
        next_market_open: datetime | None,
    ) -> WindowKind:
        if previous_close is None or next_market_open is None:
            return WindowKind.WEEKEND
        prev_day = previous_close.astimezone(self._market_tz).date()
        next_day = next_market_open.astimezone(self._market_tz).date()
        day_gap = (next_day - prev_day).days
        if day_gap <= 1:
            return WindowKind.WEEKDAY_OVERNIGHT
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

    def _next_reference_open(self, now_utc: datetime) -> datetime:
        schedule = self._schedule_around(now_utc, days_back=0, days_forward=14)
        for _, row in schedule.iterrows():
            market_open_utc = row["market_open"].to_pydatetime().astimezone(
                timezone.utc
            )
            if market_open_utc <= now_utc:
                continue
            if self.premarket_time is None:
                return market_open_utc
            market_open_local = market_open_utc.astimezone(self._market_tz)
            reference_local = datetime.combine(
                market_open_local.date(),
                self.premarket_time,
                tzinfo=self._market_tz,
            )
            return reference_local.astimezone(timezone.utc)
        raise RuntimeError(
            f"无法在未来 14 天内找到 {self.calendar_name} 下一次参考开盘时间。"
        )

    # Compatibility alias retained for existing callers and tests.
    def _next_premarket_open(self, now_utc: datetime) -> datetime:
        return self._next_reference_open(now_utc)

    def _schedule_around(
        self,
        now_utc: datetime,
        days_back: int,
        days_forward: int,
    ):
        now_local = now_utc.astimezone(self._market_tz).date()
        start = now_local - timedelta(days=days_back)
        end = now_local + timedelta(days=days_forward)
        return self._calendar.schedule(start_date=start, end_date=end)

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime:
        if value is None:
            return datetime.now(timezone.utc)
        if value.tzinfo is None:
            raise ValueError("时间必须包含 timezone。")
        return value.astimezone(timezone.utc)
