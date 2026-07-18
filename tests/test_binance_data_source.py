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


def _funding(funding_time: int, rate: float, mark: str | None = "100.0") -> dict[str, object]:
    row: dict[str, object] = {
        "symbol": "BTCUSDT",
        "fundingTime": funding_time,
        "fundingRate": str(rate),
    }
    if mark is not None:
        row["markPrice"] = mark
    return row


def test_binance_source_fetch_funding_paginates_and_filters_future() -> None:
    start_ms = 1_800_000_000_000
    eight_hours = 8 * 60 * 60 * 1000
    now_ms = start_ms + 3 * eight_hours
    seen_starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/fundingRate"
        cursor = int(request.url.params["startTime"])
        seen_starts.append(cursor)
        if cursor == start_ms:
            return httpx.Response(
                200,
                json=[
                    _funding(start_ms, 0.0001),
                    _funding(start_ms + eight_hours, 0.0002, mark=None),
                ],
            )
        if cursor == start_ms + eight_hours + 1:
            # 第二页：一个已结算事件 + 一个未来事件（>= now_ms 应被剔除）。
            return httpx.Response(
                200,
                json=[
                    _funding(start_ms + 2 * eight_hours, -0.0003, mark="0"),
                    _funding(now_ms + eight_hours, 0.0005),
                ],
            )
        # 第三页：已无新的已结算事件，返回空以终止分页。
        return httpx.Response(200, json=[])

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        source = BinanceHistoricalDataSource(
            client=client,
            pause_seconds=0,
            funding_page_limit=2,
            now_ms=lambda: now_ms,
        )
        start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        events = [
            event
            async for event in source.fetch_funding(
                "btcusdt",
                start,
                start + timedelta(days=2),
            )
        ]
        await client.aclose()
        return events

    events = asyncio.run(run())

    assert [event.funding_time for event in events] == [
        start_ms,
        start_ms + eight_hours,
        start_ms + 2 * eight_hours,
    ]
    assert [round(event.funding_rate, 4) for event in events] == [0.0001, 0.0002, -0.0003]
    # markPrice 缺失或非正值均归一为 None。
    assert events[0].mark_price == 100.0
    assert events[1].mark_price is None
    assert events[2].mark_price is None
    assert seen_starts == [
        start_ms,
        start_ms + eight_hours + 1,
        start_ms + 2 * eight_hours + 1,
    ]


def test_binance_source_fetch_funding_rejects_naive_datetime() -> None:
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
        source = BinanceHistoricalDataSource(client=client)
        try:
            async for _ in source.fetch_funding("BTCUSDT", datetime(2026, 1, 1), datetime(2026, 1, 2)):
                pass
        finally:
            await client.aclose()

    with pytest.raises(ValueError, match="时区"):
        asyncio.run(run())
