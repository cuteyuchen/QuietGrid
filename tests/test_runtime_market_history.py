from __future__ import annotations

from datetime import datetime, timezone

import pytest

from strategy.market_history import RecentMarketHistoryService


class FakeExchange:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        return list(self.rows[-limit:])


def _bar(open_ms: int, *, closed: bool = True, bad_ohlc: bool = False) -> dict:
    close_ms = open_ms + 59_999
    high = 101.0
    low = 99.0
    if bad_ohlc:
        high = 98.0
        low = 102.0
    return {
        "open_time": open_ms,
        "close_time": close_ms,
        "open": 100.0,
        "high": high,
        "low": low,
        "close": 100.5,
        "volume": 10.0,
        "closed": closed,
    }


@pytest.mark.asyncio
async def test_load_closed_klines_trims_open_bar_and_keeps_required() -> None:
    as_of = datetime(2026, 7, 18, 12, 0, 30, tzinfo=timezone.utc)
    # Build 182 closed-ish bars + current open minute
    base = int(datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [_bar(base + i * 60_000) for i in range(182)]
    # current open bar (close_time in future relative to as_of? as_of is 12:00:30)
    # last closed should be the bar opening at 11:59
    service = RecentMarketHistoryService(FakeExchange(rows), max_data_age_seconds=120)
    batch = await service.load_closed_klines(
        "BTCUSDT",
        interval="1m",
        required_bars=180,
        as_of=as_of,
        buffer_bars=2,
    )
    assert len(batch.rows) == 180
    assert batch.quality.valid is True
    assert all(int(row["close_time"]) < int(as_of.timestamp() * 1000) for row in batch.rows)


@pytest.mark.asyncio
async def test_insufficient_bars_blocked() -> None:
    as_of = datetime(2026, 7, 18, 12, 0, 30, tzinfo=timezone.utc)
    base = int(datetime(2026, 7, 18, 11, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [_bar(base + i * 60_000) for i in range(50)]
    service = RecentMarketHistoryService(FakeExchange(rows), max_data_age_seconds=120)
    with pytest.raises(RuntimeError) as exc:
        await service.load_closed_klines(
            "BTCUSDT",
            interval="1m",
            required_bars=180,
            as_of=as_of,
            buffer_bars=2,
        )
    assert "DATA_INSUFFICIENT" in str(exc.value)


@pytest.mark.asyncio
async def test_gap_blocked() -> None:
    as_of = datetime(2026, 7, 18, 12, 0, 30, tzinfo=timezone.utc)
    base = int(datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [_bar(base + i * 60_000) for i in range(180)]
    del rows[50]  # create gap
    service = RecentMarketHistoryService(FakeExchange(rows), max_data_age_seconds=120)
    with pytest.raises(RuntimeError) as exc:
        await service.load_closed_klines(
            "BTCUSDT",
            interval="1m",
            required_bars=180,
            as_of=as_of,
            buffer_bars=2,
        )
    assert "DATA_GAP" in str(exc.value) or "DATA_INSUFFICIENT" in str(exc.value)


@pytest.mark.asyncio
async def test_stale_blocked() -> None:
    as_of = datetime(2026, 7, 18, 12, 0, 30, tzinfo=timezone.utc)
    base = int(datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [_bar(base + i * 60_000) for i in range(180)]
    service = RecentMarketHistoryService(FakeExchange(rows), max_data_age_seconds=90)
    with pytest.raises(RuntimeError) as exc:
        await service.load_closed_klines(
            "BTCUSDT",
            interval="1m",
            required_bars=180,
            as_of=as_of,
            buffer_bars=2,
        )
    assert "DATA_STALE" in str(exc.value)


@pytest.mark.asyncio
async def test_duplicate_identical_is_deduped() -> None:
    as_of = datetime(2026, 7, 18, 12, 0, 30, tzinfo=timezone.utc)
    base = int(datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc).timestamp() * 1000)
    rows = [_bar(base + i * 60_000) for i in range(180)]
    rows.append(dict(rows[-1]))
    service = RecentMarketHistoryService(FakeExchange(rows), max_data_age_seconds=120)
    batch = await service.load_closed_klines(
        "BTCUSDT",
        interval="1m",
        required_bars=180,
        as_of=as_of,
        buffer_bars=2,
    )
    assert len(batch.rows) == 180
    assert batch.duplicate_count >= 1
