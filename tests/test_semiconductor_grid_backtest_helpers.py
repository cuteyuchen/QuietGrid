from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas_market_calendars as mcal

from scripts.semiconductor_grid_backtest import (
    _aggregate,
    assess_profiles,
    assign_time_splits,
    build_calendar_closed_windows,
    build_closed_windows,
)
from strategy.window_models import TradingWindow, WindowKind


class _FakeScheduler:
    def classify_window(self, value: datetime) -> TradingWindow:
        key = "US:one" if value.minute < 3 else "US:two"
        return TradingWindow(kind=WindowKind.WEEKEND,allowed=True,window_key=key,previous_market_close=None,next_market_open=None,next_premarket_open=None,force_close_at=None,minutes_to_force_close=999,reason="test")


def test_closed_window_builder_groups_by_frozen_window_key() -> None:
    start = datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)
    rows = [{"open_time":int((start+timedelta(minutes=index)).timestamp()*1000),"close_time":int((start+timedelta(minutes=index,seconds=59)).timestamp()*1000),"open":100.0,"high":100.1,"low":99.9,"close":100.0} for index in range(6)]
    windows = build_closed_windows(rows, _FakeScheduler(), "US_STOCK")
    assert [item.window_key for item in windows] == ["US:one", "US:two"]
    assert [len(item.rows) for item in windows] == [3, 3]


def test_aggregate_averages_seeds_before_counting_windows() -> None:
    rows = [{"market_group":"US_STOCK","symbol":"SNDKUSDT","profile":"N100","scenario":"PRIMARY_ZERO_MAKER","window_key":"US:one","seed":seed,"total_pnl":pnl,"max_drawdown":1.0,"max_drawdown_pct":0.002,"fees_paid":0.0,"funding_paid":0.0,"inventory_drag_ratio":0.1} for seed,pnl in [(3,1.0),(10,3.0)]]
    summary = _aggregate(rows, ("market_group", "profile", "scenario"))[0]
    assert summary["runs"] == 2
    assert summary["unique_windows"] == 1
    assert summary["total_pnl"] == 2.0
    assert summary["positive_ratio"] == 1.0


def test_assessment_requires_primary_and_stress() -> None:
    summaries = [
        {"market_group":"US_STOCK","profile":"N100","scenario":"PRIMARY_ZERO_MAKER","unique_windows":8,"total_pnl":10.0,"positive_ratio":0.75,"profit_factor":2.0,"max_drawdown_pct":0.02,"mean_inventory_drag_ratio":0.10,"best_window_concentration":0.20},
        {"market_group":"US_STOCK","profile":"N100","scenario":"EXECUTION_STRESS","unique_windows":8,"total_pnl":2.0,"positive_ratio":0.55,"profit_factor":1.1,"max_drawdown_pct":0.03,"mean_inventory_drag_ratio":0.20,"best_window_concentration":0.30},
    ]
    result = assess_profiles(summaries, {})[0]
    assert result["passed"] is True
    assert result["conclusion"] == "RESEARCH_CANDIDATE"


def _weekend_rows(
    calendar_name: str,
    *,
    reference_open_hour_utc_offset: float | None = None,
    complete: bool = True,
) -> tuple[list[dict], datetime, datetime]:
    schedule = mcal.get_calendar(calendar_name).schedule(
        start_date="2026-07-01",
        end_date="2026-07-20",
    )
    closes = [
        value.to_pydatetime().astimezone(timezone.utc)
        for value in schedule["market_close"]
    ]
    opens = [
        value.to_pydatetime().astimezone(timezone.utc)
        for value in schedule["market_open"]
    ]
    pair = next(
        (close, opens[index + 1])
        for index, close in enumerate(closes[:-1])
        if (opens[index + 1].date() - close.date()).days >= 2
    )
    previous_close, next_open = pair
    if reference_open_hour_utc_offset is None:
        next_reference_open = next_open
    else:
        next_reference_open = next_open + timedelta(
            hours=reference_open_hour_utc_offset
        )
    force_close_at = next_reference_open - timedelta(minutes=120)
    end = force_close_at if complete else force_close_at - timedelta(minutes=1)
    rows: list[dict] = []
    cursor = previous_close
    while cursor < end:
        open_ms = int(cursor.timestamp() * 1000)
        rows.append(
            {
                "open_time": open_ms,
                "close_time": open_ms + 59_999,
                "open": 100.0,
                "high": 100.1,
                "low": 99.9,
                "close": 100.0,
                "quote_volume": 20_000.0,
                "trade_count": 100,
            }
        )
        cursor += timedelta(minutes=1)
    return rows, previous_close, force_close_at


def test_nyse_window_ends_at_force_close_and_minimum_is_admission_only() -> None:
    # NYSE regular open is 09:30 ET while the registered reference open is
    # 04:00 ET, i.e. 5.5 hours before regular open.
    rows, previous_close, force_close_at = _weekend_rows(
        "NYSE",
        reference_open_hour_utc_offset=-5.5,
    )
    first = build_calendar_closed_windows(
        rows,
        market_group="US_STOCK",
        calendar_name="NYSE",
        market_timezone="America/New_York",
        reference_open_time="04:00",
        minimum_trade_minutes=120,
    )
    second = build_calendar_closed_windows(
        rows,
        market_group="US_STOCK",
        calendar_name="NYSE",
        market_timezone="America/New_York",
        reference_open_time="04:00",
        minimum_trade_minutes=60,
    )

    assert len(first) == len(second) == 1
    assert first[0].previous_market_close == previous_close
    assert first[0].force_close_at == force_close_at
    assert first[0].end_time == force_close_at - timedelta(minutes=1)
    assert first[0].rows == second[0].rows
    assert first[0].complete is True


def test_xkrx_window_uses_regular_open_and_incomplete_window_is_excluded() -> None:
    rows, _previous_close, force_close_at = _weekend_rows("XKRX", complete=False)
    windows = build_calendar_closed_windows(
        rows,
        market_group="KR_STOCK",
        calendar_name="XKRX",
        market_timezone="Asia/Seoul",
        reference_open_time=None,
    )
    assigned = assign_time_splits(
        windows,
        forward_oos_start=force_close_at - timedelta(days=1),
    )

    assert len(assigned) == 1
    assert assigned[0].force_close_at == force_close_at
    assert assigned[0].complete is False
    assert assigned[0].blocked_reason == "INCOMPLETE_WINDOW"
    assert assigned[0].split == "INCOMPLETE_WINDOW"
