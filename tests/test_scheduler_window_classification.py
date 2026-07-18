from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core.scheduler import Scheduler
from strategy.window_models import WindowKind


NY = ZoneInfo("America/New_York")


def test_friday_after_close_is_weekend() -> None:
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    # 2026-07-03 is Friday
    now = datetime(2026, 7, 3, 18, 0, tzinfo=NY)
    window = scheduler.classify_window(now)
    assert window.kind == WindowKind.WEEKEND
    assert window.allowed is True
    assert window.window_key.startswith("NYSE:")


def test_saturday_is_weekend() -> None:
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    now = datetime(2026, 7, 4, 12, 0, tzinfo=NY)
    window = scheduler.classify_window(now)
    assert window.kind == WindowKind.WEEKEND
    assert window.allowed is True


def test_monday_before_premarket_still_weekend() -> None:
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    # Sunday evening still belongs to the weekend window and has enough trade time.
    now = datetime(2026, 7, 5, 20, 0, tzinfo=NY)
    window = scheduler.classify_window(now)
    assert window.kind == WindowKind.WEEKEND
    assert window.allowed is True


def test_monday_near_force_close_blocks_new_entries() -> None:
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    # Monday 01:00: still WEEKEND, but remaining time < force_close + minimum_trade.
    now = datetime(2026, 7, 6, 1, 0, tzinfo=NY)
    window = scheduler.classify_window(now)
    assert window.kind == WindowKind.WEEKEND
    assert window.allowed is False


def test_weekday_overnight_not_allowed() -> None:
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    # Tuesday after close -> Wednesday open.
    now = datetime(2026, 6, 30, 18, 0, tzinfo=NY)
    window = scheduler.classify_window(now)
    assert window.kind == WindowKind.WEEKDAY_OVERNIGHT
    assert window.allowed is False
    assert scheduler.is_in_window(now) is False


def test_force_close_buffer_not_allowed() -> None:
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    now = datetime(2026, 7, 6, 2, 0, tzinfo=NY)
    window = scheduler.classify_window(now)
    assert window.kind == WindowKind.FORCE_CLOSE_BUFFER
    assert window.allowed is False


def test_regular_open_not_allowed() -> None:
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    now = datetime(2026, 7, 2, 10, 0, tzinfo=NY)
    window = scheduler.classify_window(now)
    assert window.kind == WindowKind.REGULAR_OPEN
    assert window.allowed is False


def test_naive_datetime_rejected() -> None:
    scheduler = Scheduler()
    try:
        scheduler.classify_window(datetime(2026, 7, 2, 18, 0))
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive datetime should be rejected")
