from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timezone

import httpx
import pytest

from data_sources import BinanceArchiveHistoricalDataSource, DataSourceError
from data_sources.archive_checksum import sha256_hexdigest


ONE_MINUTE = 60_000


def _kline_line(open_time: int, close: float = 100.0) -> str:
    return (
        f"{open_time},{close},{close + 1},{close - 1},{close},"
        f"12.5,{open_time + ONE_MINUTE - 1},1250,9,0,0,0"
    )


def _day_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def _zip_for(csv_name: str, start_ms: int, count: int) -> bytes:
    body = "\n".join(_kline_line(start_ms + i * ONE_MINUTE) for i in range(count))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(csv_name, body)
    return buffer.getvalue()


class _ArchiveServer:
    """内存版 data.binance.vision：按 URL 返回 ZIP 与 .CHECKSUM，或 404。"""

    def __init__(self) -> None:
        self.zips: dict[str, bytes] = {}
        self.requested: list[str] = []

    def add_daily(self, symbol: str, interval: str, year: int, month: int, day: int, count: int) -> None:
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}-{day:02d}"
        path = f"/data/futures/um/daily/klines/{symbol}/{interval}/{name}.zip"
        self.zips[path] = _zip_for(f"{name}.csv", _day_ms(year, month, day), count)

    def add_monthly(self, symbol: str, interval: str, year: int, month: int, count: int) -> None:
        name = f"{symbol}-{interval}-{year:04d}-{month:02d}"
        path = f"/data/futures/um/monthly/klines/{symbol}/{interval}/{name}.zip"
        self.zips[path] = _zip_for(f"{name}.csv", _day_ms(year, month, 1), count)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requested.append(path)
        if path.endswith(".CHECKSUM"):
            zip_path = path[: -len(".CHECKSUM")]
            data = self.zips.get(zip_path)
            if data is None:
                return httpx.Response(404)
            return httpx.Response(200, text=f"{sha256_hexdigest(data)}  {zip_path.rsplit('/', 1)[-1]}\n")
        data = self.zips.get(path)
        if data is None:
            return httpx.Response(404)
        if request.method == "HEAD":
            return httpx.Response(200)
        return httpx.Response(200, content=data)


def _source(server: _ArchiveServer, now: datetime) -> BinanceArchiveHistoricalDataSource:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
    return BinanceArchiveHistoricalDataSource(
        client=client,
        pause_seconds=0,
        now_ms=lambda: int(now.timestamp() * 1000),
    )


def test_fetch_uses_monthly_and_daily_and_verifies_checksum() -> None:
    server = _ArchiveServer()
    # 请求 2026-03-30 ~ 2026-04-02：3 月尾用日包，4 月整月用月包（此处只覆盖到 4-02，故也是日包）。
    server.add_daily("BTCUSDT", "1m", 2026, 3, 30, 1440)
    server.add_daily("BTCUSDT", "1m", 2026, 3, 31, 1440)
    server.add_daily("BTCUSDT", "1m", 2026, 4, 1, 1440)

    async def run() -> list[int]:
        source = _source(server, datetime(2026, 5, 1, tzinfo=timezone.utc))
        rows = [
            row.open_time
            async for row in source.fetch_klines(
                "BTCUSDT",
                "1m",
                datetime(2026, 3, 30, tzinfo=timezone.utc),
                datetime(2026, 4, 2, tzinfo=timezone.utc),
            )
        ]
        await source.close()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1440 * 3
    assert rows == sorted(rows)


def test_full_month_prefers_monthly_package() -> None:
    server = _ArchiveServer()
    server.add_monthly("BTCUSDT", "1m", 2026, 4, 43200)

    async def run() -> tuple[int, list[str]]:
        source = _source(server, datetime(2026, 6, 1, tzinfo=timezone.utc))
        count = 0
        async for _ in source.fetch_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 4, 1, tzinfo=timezone.utc),
            datetime(2026, 5, 1, tzinfo=timezone.utc),
        ):
            count += 1
        await source.close()
        return count, server.requested

    count, requested = asyncio.run(run())
    assert count == 43200
    assert any("/monthly/" in path for path in requested)
    assert not any("/daily/" in path for path in requested)


def test_missing_monthly_falls_back_to_daily() -> None:
    server = _ArchiveServer()
    # 不提供月包，只提供整月每一天的日包（用 2026-02，28 天）。
    for day in range(1, 29):
        server.add_daily("BTCUSDT", "1m", 2026, 2, day, 10)

    async def run() -> tuple[int, list[str]]:
        source = _source(server, datetime(2026, 4, 1, tzinfo=timezone.utc))
        count = 0
        async for _ in source.fetch_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 1, tzinfo=timezone.utc),
        ):
            count += 1
        await source.close()
        return count, server.requested

    count, requested = asyncio.run(run())
    assert count == 28 * 10
    assert any("/monthly/" in path for path in requested)  # 尝试过月包
    assert any("/daily/" in path for path in requested)  # 回退到日包


def test_checksum_mismatch_aborts_download() -> None:
    server = _ArchiveServer()
    server.add_daily("BTCUSDT", "1m", 2026, 3, 30, 5)
    # 篡改 CHECKSUM：返回一个错误摘要。
    original = server.handler

    def tampered(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            server.requested.append(request.url.path)
            return httpx.Response(200, text="0" * 64 + "  file.zip\n")
        return original(request)

    async def run() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(tampered))
        source = BinanceArchiveHistoricalDataSource(
            client=client, pause_seconds=0, now_ms=lambda: int(datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp() * 1000)
        )
        async for _ in source.fetch_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 3, 30, tzinfo=timezone.utc),
            datetime(2026, 3, 31, tzinfo=timezone.utc),
        ):
            pass
        await source.close()

    with pytest.raises(DataSourceError, match="checksum 不匹配"):
        asyncio.run(run())


def test_archive_exists_probes_head() -> None:
    server = _ArchiveServer()
    server.add_daily("AAPLUSDT", "1m", 2026, 4, 30, 1)

    async def run() -> bool:
        source = _source(server, datetime(2026, 5, 1, tzinfo=timezone.utc))
        exists = await source.archive_exists("AAPLUSDT", "1m")
        await source.close()
        return exists

    assert asyncio.run(run()) is True
