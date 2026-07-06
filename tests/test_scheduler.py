from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core.scheduler import Scheduler


NY = ZoneInfo("America/New_York")


def test_regular_session_is_not_trading_window() -> None:
    scheduler = Scheduler(force_close_minutes=120)
    now = datetime(2026, 7, 2, 10, 0, tzinfo=NY)

    assert scheduler.is_in_window(now) is False
    assert scheduler.should_force_close(now) is False


def test_after_close_before_weekend_window_is_tradable() -> None:
    scheduler = Scheduler(force_close_minutes=120)
    now = datetime(2026, 7, 2, 18, 0, tzinfo=NY)

    assert scheduler.is_in_window(now) is True
    assert scheduler.should_force_close(now) is False


def test_force_close_starts_two_hours_before_premarket() -> None:
    scheduler = Scheduler(force_close_minutes=120)

    before_force = datetime(2026, 7, 6, 1, 59, tzinfo=NY)
    at_force = datetime(2026, 7, 6, 2, 0, tzinfo=NY)
    after_premarket_started = datetime(2026, 7, 6, 5, 0, tzinfo=NY)

    assert scheduler.should_force_close(before_force) is False
    assert scheduler.should_force_close(at_force) is True
    assert scheduler.should_force_close(after_premarket_started) is True


def test_force_close_uses_new_york_time_during_standard_time() -> None:
    scheduler = Scheduler(force_close_minutes=120)

    before_force = datetime(2026, 1, 5, 1, 59, tzinfo=NY)
    at_force = datetime(2026, 1, 5, 2, 0, tzinfo=NY)

    assert scheduler.should_force_close(before_force) is False
    assert scheduler.should_force_close(at_force) is True
    assert scheduler.minutes_to_next_open(at_force) == 120


def test_half_day_close_starts_window_after_early_close() -> None:
    scheduler = Scheduler(force_close_minutes=120)

    before_early_close = datetime(2026, 11, 27, 12, 30, tzinfo=NY)
    after_early_close = datetime(2026, 11, 27, 13, 30, tzinfo=NY)

    assert scheduler.is_in_window(before_early_close) is False
    assert scheduler.is_in_window(after_early_close) is True


def test_naive_datetime_is_rejected() -> None:
    scheduler = Scheduler()

    try:
        scheduler.is_in_window(datetime(2026, 7, 2, 18, 0))
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive datetime should be rejected")
