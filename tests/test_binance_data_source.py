from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from data_sources import BinanceHistoricalDataSource, DataSourceError


EXCHANGE_INFO = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
        },
        {
            "symbol": "OLDUSDT",
            "status": "CLOSE",
            "contractType": "PERPETUAL",
            "baseAsset": "OLD",
            "quoteAsset": "USDT",
        },
    ]
}


def _kline(open_time: int, close: float = 100.0) -> list[object]:
    return [
        open_time,
        str(close),
        str(close + 1),
        str(close - 1),
        str(close),
        "12.5",
        open_time + 59_999,
        "1250",
        9,
        "0",
        "0",
        "0",
    ]


def test_binance_source_filters_symbols_and_previews_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/exchangeInfo"
        return httpx.Response(200, json=EXCHANGE_INFO)

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = BinanceHistoricalDataSource(client=client, page_limit=2)
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        symbols = await source.list_symbols("btc")
        preview = await source.preview("btcusdt", "1m", start, start + timedelta(minutes=5))
        await client.aclose()
        return symbols, preview

    symbols, preview = asyncio.run(run())

    assert [item.symbol for item in symbols] == ["BTCUSDT"]
    assert preview.estimated_rows == 5
    assert preview.estimated_pages == 3


def test_binance_source_paginates_with_progress_guard_and_drops_unclosed() -> None:
    start_ms = 1_800_000_000_000
    seen_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("exchangeInfo"):
            return httpx.Response(200, json=EXCHANGE_INFO)
        cursor = int(request.url.params["startTime"])
        seen_starts.append(cursor)
        if cursor == start_ms:
            return httpx.Response(200, json=[_kline(start_ms), _kline(start_ms + 60_000)])
        return httpx.Response(
            200,
            json=[_kline(start_ms + 120_000), _kline(start_ms + 180_000)],
        )

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = BinanceHistoricalDataSource(
            client=client,
            page_limit=2,
            pause_seconds=0,
            now_ms=lambda: start_ms + 210_000,
        )
        start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        rows = [
            row
            async for row in source.fetch_klines(
                "BTCUSDT",
                "1m",
                start,
                start + timedelta(minutes=4),
            )
        ]
        await client.aclose()
        return rows

    rows = asyncio.run(run())

    assert seen_starts == [start_ms, start_ms + 120_000]
    assert [row.open_time for row in rows] == [
        start_ms,
        start_ms + 60_000,
        start_ms + 120_000,
    ]


def test_binance_source_retries_429_but_not_parameter_error() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0.25"})
        return httpx.Response(200, json=EXCHANGE_INFO)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def run_retry():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = BinanceHistoricalDataSource(client=client, sleep=fake_sleep)
        result = await source.list_symbols()
        await client.aclose()
        return result

    assert [item.symbol for item in asyncio.run(run_retry())] == ["BTCUSDT"]
    assert sleeps == [0.25]

    def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    async def run_bad():
        client = httpx.AsyncClient(transport=httpx.MockTransport(bad_handler))
        source = BinanceHistoricalDataSource(client=client, retry_attempts=3)
        try:
            await source.list_symbols()
        finally:
            await client.aclose()

    with pytest.raises(DataSourceError, match="Invalid symbol"):
        asyncio.run(run_bad())


def test_binance_source_stops_immediately_on_418() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(418, json={"msg": "banned"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = BinanceHistoricalDataSource(client=client, retry_attempts=3)
        try:
            await source.list_symbols()
        finally:
            await client.aclose()

    with pytest.raises(DataSourceError, match="418"):
        asyncio.run(run())
    assert calls == 1
