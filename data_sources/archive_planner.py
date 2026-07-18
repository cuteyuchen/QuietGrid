"""将历史下载区间规划为 Binance 官方月度 / 每日归档与 REST 尾部片段。"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone

from data_sources.models import ArchiveSegment, ArchiveSegmentType


_ONE_DAY = timedelta(days=1)


class BinanceArchivePlanner:
    """按月度优先、部分月份回退每日、未归档尾部交给 REST 的规则拆分下载区间。"""

    def __init__(self, *, prefer_monthly: bool = True) -> None:
        self.prefer_monthly = bool(prefer_monthly)

    def plan(
        self,
        start_time: datetime,
        end_time: datetime,
        archive_available_until: date,
    ) -> list[ArchiveSegment]:
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError("start_time 和 end_time 必须包含时区。")
        if start_time >= end_time:
            raise ValueError("start_time 必须早于 end_time。")

        request_start_ms = _utc_ms(start_time)
        request_end_ms = _utc_ms(end_time)
        start_day = start_time.astimezone(timezone.utc).date()
        # end_time 为开区间，最后覆盖到的日期是 end_time 前一毫秒所在日。
        last_day = _utc_ms_to_datetime(request_end_ms - 1).date()

        segments: list[ArchiveSegment] = []
        archive_last_day = min(last_day, archive_available_until)
        if archive_last_day >= start_day:
            segments.extend(
                self._archive_segments(
                    start_day,
                    archive_last_day,
                    request_start_ms,
                    request_end_ms,
                )
            )

        rest_first_day = max(start_day, archive_available_until + _ONE_DAY)
        if rest_first_day <= last_day:
            rest_start_ms = max(request_start_ms, _day_start_ms(rest_first_day))
            segments.append(
                ArchiveSegment(
                    segment_type=ArchiveSegmentType.REST_TAIL,
                    period_start=rest_first_day,
                    period_end=last_day,
                    start_ms=rest_start_ms,
                    end_ms=request_end_ms,
                    label=f"rest {rest_first_day.isoformat()}~{last_day.isoformat()}",
                )
            )
        return segments

    def expand_to_daily(self, segment: ArchiveSegment) -> list[ArchiveSegment]:
        """把一个月度片段展开为逐日片段，供上游月包 404 时回退使用。"""
        if segment.segment_type is not ArchiveSegmentType.MONTHLY_ARCHIVE:
            raise ValueError("只能展开月度归档片段。")
        return self._daily_segments(
            segment.period_start,
            segment.period_end,
            segment.start_ms,
            segment.end_ms,
        )

    def _archive_segments(
        self,
        first_day: date,
        last_day: date,
        request_start_ms: int,
        request_end_ms: int,
    ) -> list[ArchiveSegment]:
        segments: list[ArchiveSegment] = []
        cursor = first_day.replace(day=1)
        while cursor <= last_day:
            month_first = cursor
            month_last = _month_last_day(cursor)
            covered_first = max(first_day, month_first)
            covered_last = min(last_day, month_last)
            whole_month = covered_first == month_first and covered_last == month_last
            if self.prefer_monthly and whole_month:
                segments.append(
                    self._monthly_segment(
                        month_first, month_last, request_start_ms, request_end_ms
                    )
                )
            else:
                segments.extend(
                    self._daily_segments(
                        covered_first, covered_last, request_start_ms, request_end_ms
                    )
                )
            cursor = (month_last + _ONE_DAY)
        return segments

    def _monthly_segment(
        self,
        month_first: date,
        month_last: date,
        request_start_ms: int,
        request_end_ms: int,
    ) -> ArchiveSegment:
        return ArchiveSegment(
            segment_type=ArchiveSegmentType.MONTHLY_ARCHIVE,
            period_start=month_first,
            period_end=month_last,
            start_ms=max(request_start_ms, _day_start_ms(month_first)),
            end_ms=min(request_end_ms, _day_end_ms(month_last)),
            label=month_first.strftime("%Y-%m"),
        )

    def _daily_segments(
        self,
        first_day: date,
        last_day: date,
        request_start_ms: int,
        request_end_ms: int,
    ) -> list[ArchiveSegment]:
        segments: list[ArchiveSegment] = []
        day = first_day
        while day <= last_day:
            segments.append(
                ArchiveSegment(
                    segment_type=ArchiveSegmentType.DAILY_ARCHIVE,
                    period_start=day,
                    period_end=day,
                    start_ms=max(request_start_ms, _day_start_ms(day)),
                    end_ms=min(request_end_ms, _day_end_ms(day)),
                    label=day.isoformat(),
                )
            )
            day += _ONE_DAY
        return segments


def _month_last_day(value: date) -> date:
    last = calendar.monthrange(value.year, value.month)[1]
    return value.replace(day=last)


def _day_start_ms(value: date) -> int:
    return _utc_ms(datetime(value.year, value.month, value.day, tzinfo=timezone.utc))


def _day_end_ms(value: date) -> int:
    return _day_start_ms(value + _ONE_DAY)


def _utc_ms(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _utc_ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
