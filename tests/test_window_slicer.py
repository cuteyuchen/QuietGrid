from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data_sources.models import NormalizedKline
from data_sources.window_slicer import NyseWindowSlicer


def _hourly_rows(start: datetime, end: datetime) -> list[NormalizedKline]:
    rows: list[NormalizedKline] = []
    current = start
    while current < end:
        open_time = int(current.timestamp() * 1000)
        rows.append(
            NormalizedKline(
                open_time=open_time,
                close_time=open_time + 3_599_999,
                open=100,
                high=101,
                low=99,
                close=100,
            )
        )
        current += timedelta(hours=1)
    return rows


def test_nyse_window_uses_half_day_holiday_and_force_close_buffer() -> None:
    slicer = NyseWindowSlicer(force_close_minutes=120, minimum_tradable_rows=2)
    rows = _hourly_rows(
        datetime(2025, 7, 3, 16, tzinfo=timezone.utc),
        datetime(2025, 7, 7, 8, tzinfo=timezone.utc),
    )

    windows = slicer.slice(rows, observation_rows=2)
    target = next(
        window
        for window in windows
        if window.market_close == datetime(2025, 7, 3, 17, tzinfo=timezone.utc)
    )

    assert target.force_close_at == datetime(2025, 7, 7, 6, tzinfo=timezone.utc)
    assert target.status == "READY"
    assert target.rows[0].open_datetime == target.market_close
    assert target.rows[-1].close_time < int(target.force_close_at.timestamp() * 1000)
    assert target.tradable_rows == target.row_count - 2


def test_nyse_window_handles_dst_and_reports_short_windows() -> None:
    slicer = NyseWindowSlicer(force_close_minutes=120, minimum_tradable_rows=2)
    dst_rows = _hourly_rows(
        datetime(2026, 3, 6, 21, tzinfo=timezone.utc),
        datetime(2026, 3, 9, 7, tzinfo=timezone.utc),
    )
    dst_window = next(
        window
        for window in slicer.slice(dst_rows, observation_rows=2)
        if window.market_close == datetime(2026, 3, 6, 21, tzinfo=timezone.utc)
    )

    assert dst_window.force_close_at == datetime(2026, 3, 9, 6, tzinfo=timezone.utc)
    assert dst_window.status == "READY"

    short_rows = _hourly_rows(
        datetime(2026, 3, 6, 21, tzinfo=timezone.utc),
        datetime(2026, 3, 7, 0, tzinfo=timezone.utc),
    )
    short_window = slicer.slice(short_rows, observation_rows=2)[0]
    assert short_window.status == "SKIPPED"
    assert short_window.skip_reason == "INSUFFICIENT_TRADABLE_ROWS"

