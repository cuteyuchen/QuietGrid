from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from data_sources.archive_planner import BinanceArchivePlanner
from data_sources.models import ArchiveSegmentType


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _types(segments) -> list[str]:
    return [segment.segment_type.value for segment in segments]


def _labels(segments) -> list[str]:
    return [segment.label for segment in segments]


def test_single_whole_month_uses_monthly() -> None:
    planner = BinanceArchivePlanner()
    segments = planner.plan(_utc(2026, 4, 1), _utc(2026, 5, 1), date(2026, 7, 16))
    assert _types(segments) == [ArchiveSegmentType.MONTHLY_ARCHIVE.value]
    assert _labels(segments) == ["2026-04"]


def test_multiple_whole_months_use_monthly() -> None:
    planner = BinanceArchivePlanner()
    segments = planner.plan(_utc(2026, 4, 1), _utc(2026, 7, 1), date(2026, 7, 16))
    assert _types(segments) == [ArchiveSegmentType.MONTHLY_ARCHIVE.value] * 3
    assert _labels(segments) == ["2026-04", "2026-05", "2026-06"]


def test_partial_first_month_falls_back_to_daily() -> None:
    planner = BinanceArchivePlanner()
    segments = planner.plan(_utc(2026, 4, 29), _utc(2026, 6, 1), date(2026, 7, 16))
    # 4-29, 4-30 daily; 5 whole month monthly.
    assert _types(segments) == [
        ArchiveSegmentType.DAILY_ARCHIVE.value,
        ArchiveSegmentType.DAILY_ARCHIVE.value,
        ArchiveSegmentType.MONTHLY_ARCHIVE.value,
    ]
    assert _labels(segments) == ["2026-04-29", "2026-04-30", "2026-05"]


def test_partial_last_month_falls_back_to_daily() -> None:
    planner = BinanceArchivePlanner()
    segments = planner.plan(_utc(2026, 4, 1), _utc(2026, 5, 3), date(2026, 7, 16))
    # 4 whole month; 5-01, 5-02 daily (end_time 5-03 is exclusive).
    assert _types(segments) == [
        ArchiveSegmentType.MONTHLY_ARCHIVE.value,
        ArchiveSegmentType.DAILY_ARCHIVE.value,
        ArchiveSegmentType.DAILY_ARCHIVE.value,
    ]
    assert _labels(segments) == ["2026-04", "2026-05-01", "2026-05-02"]


def test_latest_tail_not_yet_archived_uses_rest() -> None:
    planner = BinanceArchivePlanner()
    segments = planner.plan(_utc(2026, 7, 1), _utc(2026, 7, 18), date(2026, 7, 16))
    types = _types(segments)
    assert types[-1] == ArchiveSegmentType.REST_TAIL.value
    rest = segments[-1]
    assert rest.period_start == date(2026, 7, 17)
    assert rest.period_end == date(2026, 7, 17)
    # 7-01..7-16 archived as daily (partial month), then REST tail.
    assert all(t == ArchiveSegmentType.DAILY_ARCHIVE.value for t in types[:-1])
    assert _labels(segments)[:3] == ["2026-07-01", "2026-07-02", "2026-07-03"]


def test_full_example_from_plan() -> None:
    planner = BinanceArchivePlanner()
    segments = planner.plan(_utc(2026, 3, 10), _utc(2026, 7, 17), date(2026, 7, 16))
    types = _types(segments)
    # 3-10..3-31 daily, 4/5/6 monthly, 7-01..7-16 daily. No REST (end 7-17 exclusive → last day 7-16, archived).
    assert ArchiveSegmentType.MONTHLY_ARCHIVE.value in types
    monthly_labels = [
        s.label for s in segments if s.segment_type is ArchiveSegmentType.MONTHLY_ARCHIVE
    ]
    assert monthly_labels == ["2026-04", "2026-05", "2026-06"]
    assert segments[0].label == "2026-03-10"
    assert ArchiveSegmentType.REST_TAIL.value not in types


def test_request_entirely_after_archive_is_all_rest() -> None:
    planner = BinanceArchivePlanner()
    segments = planner.plan(_utc(2026, 7, 17), _utc(2026, 7, 18), date(2026, 7, 16))
    assert _types(segments) == [ArchiveSegmentType.REST_TAIL.value]


def test_expand_monthly_to_daily() -> None:
    planner = BinanceArchivePlanner()
    monthly = planner.plan(_utc(2026, 4, 1), _utc(2026, 5, 1), date(2026, 7, 16))[0]
    daily = planner.expand_to_daily(monthly)
    assert len(daily) == 30
    assert all(s.segment_type is ArchiveSegmentType.DAILY_ARCHIVE for s in daily)
    assert daily[0].label == "2026-04-01"
    assert daily[-1].label == "2026-04-30"


def test_expand_non_monthly_rejected() -> None:
    planner = BinanceArchivePlanner()
    daily = planner.plan(_utc(2026, 7, 17), _utc(2026, 7, 18), date(2026, 7, 16))[0]
    with pytest.raises(ValueError):
        planner.expand_to_daily(daily)


def test_invalid_range_rejected() -> None:
    planner = BinanceArchivePlanner()
    with pytest.raises(ValueError):
        planner.plan(_utc(2026, 5, 1), _utc(2026, 5, 1), date(2026, 7, 16))
