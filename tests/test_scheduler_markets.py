from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from core.scheduler import Scheduler


def test_nyse_defaults_keep_original_premarket_reference() -> None:
    ny = ZoneInfo("America/New_York")
    scheduler = Scheduler(force_close_minutes=120)
    at_force = datetime(2026, 7, 6, 2, 0, tzinfo=ny)
    assert scheduler.should_force_close(at_force) is True
    assert scheduler.minutes_to_next_open(at_force) == 120


def test_krx_uses_regular_open_when_no_premarket_is_configured() -> None:
    seoul = ZoneInfo("Asia/Seoul")
    scheduler = Scheduler(force_close_minutes=120, calendar_name="XKRX", market_timezone="Asia/Seoul", premarket_time=None, window_key_prefix="KR_STOCK")
    saturday = datetime(2026, 7, 4, 12, 0, tzinfo=seoul)
    window = scheduler.classify_window(saturday)
    assert window.allowed is True
    assert window.next_premarket_open == window.next_market_open
    assert window.window_key.startswith("KR_STOCK:")
    assert scheduler.minutes_to_next_open(saturday) > 0
