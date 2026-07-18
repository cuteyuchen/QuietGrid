"""归档优先的 Binance 混合历史数据源。

按官方归档（月度 / 每日）覆盖大部分历史区间，仅对尚未归档的最新尾部调用 REST。
REST 若返回 415 等不可用错误，归档区间仍然成功，尾部标记为 unavailable 并产生
READY_WITH_WARNINGS，而不是让整个历史下载失败。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from math import ceil

from data_sources.base import (
    DataSourceError,
    HistoricalDataSource,
    RestUnavailableError,
)
from data_sources.binance_archive_source import BinanceArchiveHistoricalDataSource
from data_sources.binance_source import BinanceHistoricalDataSource
from data_sources.csv_source import INTERVAL_MILLISECONDS
from data_sources.models import (
    DatasetPreview,
    FundingEvent,
    HistoricalSymbol,
    NormalizedKline,
    SourceSegmentMetadata,
)


_ONE_DAY = timedelta(days=1)


class HybridBinanceHistoricalDataSource(HistoricalDataSource):
    provider_id = "binance_hybrid"
    supports_funding = True

    def __init__(
        self,
        archive_source: BinanceArchiveHistoricalDataSource,
        rest_source: BinanceHistoricalDataSource,
        *,
        tolerate_missing_latest_tail: bool = True,
    ) -> None:
        self.archive_source = archive_source
        self.rest_source = rest_source
        self.tolerate_missing_latest_tail = bool(tolerate_missing_latest_tail)
        self.source_segments: list[SourceSegmentMetadata] = []
        self.warnings: list[str] = []

    async def close(self) -> None:
        await self.archive_source.close()
        await self.rest_source.close()

    async def __aenter__(self) -> "HybridBinanceHistoricalDataSource":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def list_symbols(self, query: str = "") -> list[HistoricalSymbol]:
        # 标的搜索仍走 REST 的 exchangeInfo；历史存在性由归档 HEAD 探测补充。
        return await self.rest_source.list_symbols(query)

    async def preview(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> DatasetPreview:
        _validate_interval(interval)
        preview = await self.archive_source.preview(symbol, interval, start_time, end_time)
        warnings = list(preview.warnings)
        tail_start = self._rest_tail_start(end_time)
        if tail_start is not None and tail_start < end_time:
            warnings.append(
                "最新尾部尚未归档，将在下载时尝试用 REST 补齐；若 REST 不可用会保留"
                "归档范围并产生告警。"
            )
        return DatasetPreview(
            provider=self.provider_id,
            symbol=preview.symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            estimated_rows=preview.estimated_rows,
            estimated_pages=preview.estimated_pages,
            estimated_size_bytes=preview.estimated_size_bytes,
            warnings=tuple(warnings),
        )

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[NormalizedKline]:
        _validate_interval(interval)
        self.source_segments = []
        self.warnings = []
        last_open_time = -1

        async for row in self.archive_source.fetch_klines(
            symbol, interval, start_time, end_time
        ):
            last_open_time = max(last_open_time, row.open_time)
            yield row
        self.source_segments.extend(self.archive_source.source_segments)

        tail_start = self._rest_tail_start(end_time)
        if tail_start is None or tail_start >= end_time:
            return
        effective_start = max(tail_start, start_time)
        async for row in self._emit_rest_tail(
            symbol, interval, effective_start, end_time, last_open_time
        ):
            yield row

    async def fetch_funding(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[FundingEvent]:
        # 资金费历史只有 REST 端点提供，归档不覆盖，全程走 REST 源。
        async for event in self.rest_source.fetch_funding(symbol, start_time, end_time):
            yield event

    async def _emit_rest_tail(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
        archive_last_open_time: int,
    ) -> AsyncIterator[NormalizedKline]:
        emitted = 0
        try:
            async for row in self.rest_source.fetch_klines(
                symbol, interval, start_time, end_time
            ):
                # 归档与 REST 在边界可能重叠，按 open_time 去重。
                if row.open_time <= archive_last_open_time:
                    continue
                emitted += 1
                yield row
        except RestUnavailableError as exc:
            if not self.tolerate_missing_latest_tail:
                raise
            message = (
                f"REST 补最新尾部不可用（{exc}）：已保留官方归档范围，"
                "尾部标记为 unavailable。"
            )
            self.warnings.append(message)
            self.source_segments.append(
                SourceSegmentMetadata(
                    segment_type="rest_tail",
                    status="UNAVAILABLE",
                    start=start_time.isoformat(),
                    end=end_time.isoformat(),
                )
            )
            return
        self.source_segments.append(
            SourceSegmentMetadata(
                segment_type="rest_tail",
                rows=emitted,
                start=start_time.isoformat(),
                end=end_time.isoformat(),
            )
        )

    def _rest_tail_start(self, end_time: datetime) -> datetime | None:
        """归档可用日期之后的第一毫秒；若请求未触及未归档区间则返回 None。"""
        available_until = self.archive_source.archive_available_until()
        first_rest_day = available_until + _ONE_DAY
        tail_start = datetime(
            first_rest_day.year,
            first_rest_day.month,
            first_rest_day.day,
            tzinfo=timezone.utc,
        )
        if tail_start >= end_time:
            return None
        return tail_start


def _validate_interval(interval: str) -> None:
    if interval not in INTERVAL_MILLISECONDS:
        raise DataSourceError(f"Binance 历史数据暂不支持周期: {interval}")
