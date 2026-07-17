from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from data_sources import (
    CsvHistoricalDataSource,
    DataSourceError,
    DatasetRequest,
    DatasetStatus,
    HistoricalDataSourceRegistry,
    NormalizedKline,
    read_legacy_backtest_csv,
)
from data_sources.normalizer import validate_and_normalize_klines


def test_dataset_request_normalizes_identity_and_requires_aware_time() -> None:
    request = DatasetRequest(
        provider=" Binance ",
        symbol=" btcusdt ",
        interval="1m",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    assert request.provider == "binance"
    assert request.symbol == "BTCUSDT"
    assert request.window_mode == "NYSE_CLOSED_ONLY"

    with pytest.raises(ValueError, match="必须包含时区"):
        DatasetRequest(
            provider="binance",
            symbol="BTCUSDT",
            interval="1m",
            start_time=datetime(2026, 1, 1),
            end_time=datetime(2026, 1, 2),
        )


def test_registry_only_creates_explicitly_registered_provider(tmp_path) -> None:
    registry = HistoricalDataSourceRegistry()
    registry.register("csv", CsvHistoricalDataSource)

    source = registry.create("CSV", path=tmp_path / "sample.csv")

    assert isinstance(source, CsvHistoricalDataSource)
    assert registry.providers() == ("csv",)
    with pytest.raises(DataSourceError, match="不支持的历史数据源"):
        registry.create("https://example.com/evil")


def test_legacy_csv_reader_preserves_existing_minimal_contract(tmp_path) -> None:
    path = tmp_path / "legacy.csv"
    path.write_text("timestamp, high , low , close\nobs-1,101,99,100\n", encoding="utf-8")

    rows = read_legacy_backtest_csv(path)

    assert rows == [{"timestamp": "obs-1", "high": 101.0, "low": 99.0, "close": 100.0}]


def test_csv_source_previews_and_streams_standard_rows(tmp_path) -> None:
    path = tmp_path / "btcusdt.csv"
    path.write_text(
        "open_time,open,high,low,close,volume,quote_volume,trade_count\n"
        "2026-01-01T00:00:00Z,100,101,99,100.5,12,1206,8\n"
        "2026-01-01T00:01:00Z,100.5,102,100,101,9,909,5\n",
        encoding="utf-8",
    )
    source = CsvHistoricalDataSource(path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(minutes=2)

    async def run():
        preview = await source.preview("BTCUSDT", "1m", start, end)
        rows = [row async for row in source.fetch_klines("BTCUSDT", "1m", start, end)]
        return preview, rows

    preview, rows = asyncio.run(run())

    assert preview.estimated_rows == 2
    assert preview.cache_hit is True
    assert [row.close for row in rows] == [100.5, 101.0]
    assert rows[0].close_time == rows[0].open_time + 60_000 - 1


def test_normalizer_sorts_deduplicates_and_reports_gaps() -> None:
    def row(minute: int, close: float = 100.0) -> NormalizedKline:
        opened = 1_700_000_000_000 + minute * 60_000
        return NormalizedKline(
            open_time=opened,
            close_time=opened + 59_999,
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
        )

    normalized, report = validate_and_normalize_klines(
        [row(3), row(0), row(0)],
        interval_ms=60_000,
        warning_missing_ratio=0.1,
        reject_missing_ratio=0.8,
    )

    assert [item.open_time for item in normalized] == sorted(item.open_time for item in normalized)
    assert report.duplicate_rows == 1
    assert report.missing_intervals == 2
    assert report.max_consecutive_missing == 2
    assert report.status is DatasetStatus.READY_WITH_WARNINGS


def test_normalizer_rejects_conflicting_duplicate() -> None:
    first = NormalizedKline(1000, 1999, 100, 101, 99, 100)
    conflicting = NormalizedKline(1000, 1999, 100, 102, 99, 101)

    with pytest.raises(DataSourceError, match="冲突重复K线"):
        validate_and_normalize_klines([first, conflicting], interval_ms=1000)


def test_normalizer_removes_unclosed_row() -> None:
    closed = NormalizedKline(1000, 1999, 100, 101, 99, 100)
    unclosed = NormalizedKline(2000, 2999, 100, 101, 99, 100)

    normalized, report = validate_and_normalize_klines(
        [closed, unclosed],
        interval_ms=1000,
        now_ms=2500,
    )

    assert normalized == [closed]
    assert report.unclosed_rows == 1
    assert report.status is DatasetStatus.READY_WITH_WARNINGS
