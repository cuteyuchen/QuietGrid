from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timezone

import httpx
import pytest

from data_sources import (
    BinanceArchiveHistoricalDataSource,
    BinanceHistoricalDataSource,
    HybridBinanceHistoricalDataSource,
)
from data_sources.archive_checksum import sha256_hexdigest


ONE_MINUTE = 60_000


def _day_ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


def _archive_kline(open_time: int) -> str:
    c = 100.0
    return f"{open_time},{c},{c + 1},{c - 1},{c},1,{open_time + ONE_MINUTE - 1},1,1,0,0,0"


def _rest_kline(open_time: int) -> list[object]:
    c = 100.0
    return [open_time, str(c), str(c + 1), str(c - 1), str(c), "1", open_time + ONE_MINUTE - 1, "1", 1, "0", "0", "0"]


def _daily_zip(symbol: str, interval: str, year: int, month: int, day: int, count: int) -> bytes:
    name = f"{symbol}-{interval}-{year:04d}-{month:02d}-{day:02d}.csv"
    start = _day_ms(year, month, day)
    body = "\n".join(_archive_kline(start + i * ONE_MINUTE) for i in range(count))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, body)
    return buffer.getvalue()


def _build_hybrid(
    handler: httpx.MockTransport,
    now: datetime,
    *,
    rest_status: int | None = None,
    tolerate: bool = True,
) -> HybridBinanceHistoricalDataSource:
    now_ms = int(now.timestamp() * 1000)
    archive = BinanceArchiveHistoricalDataSource(
        client=httpx.AsyncClient(transport=handler),
        pause_seconds=0,
        now_ms=lambda: now_ms,
    )

    def rest_handler(request: httpx.Request) -> httpx.Response:
        if rest_status is not None:
            return httpx.Response(rest_status, json={"code": -1, "msg": "blocked"})
        return handler.handler(request)  # type: ignore[attr-defined]

    rest = BinanceHistoricalDataSource(
        client=httpx.AsyncClient(transport=httpx.MockTransport(rest_handler)),
        page_limit=1500,
        pause_seconds=0,
        validate_symbol_listing=False,
        now_ms=lambda: now_ms,
    )
    return HybridBinanceHistoricalDataSource(archive, rest, tolerate_missing_latest_tail=tolerate)


def test_hybrid_merges_archive_and_rest_tail_without_duplicates() -> None:
    # 归档可用到 07-15（now=07-17, lag=1 → until=07-16... 用 now=07-16 使 until=07-15）。
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    # 归档提供 07-14 与 07-15 两天；REST 提供 07-16 尾部。
    zips = {
        "/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-07-14.zip": _daily_zip(
            "BTCUSDT", "1m", 2026, 7, 14, 1440
        ),
        "/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-07-15.zip": _daily_zip(
            "BTCUSDT", "1m", 2026, 7, 15, 1440
        ),
    }
    rest_start = _day_ms(2026, 7, 16)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/fapi/v1/klines":
            cursor = int(request.url.params["startTime"])
            end = int(request.url.params["endTime"])
            rows = []
            t = max(cursor, rest_start)
            while t <= end and len(rows) < 1500:
                rows.append(_rest_kline(t))
                t += ONE_MINUTE
            return httpx.Response(200, json=rows)
        if path.endswith(".CHECKSUM"):
            data = zips.get(path[: -len(".CHECKSUM")])
            return httpx.Response(404) if data is None else httpx.Response(
                200, text=f"{sha256_hexdigest(data)}  f.csv\n"
            )
        data = zips.get(path)
        return httpx.Response(404) if data is None else httpx.Response(200, content=data)

    async def run() -> tuple[list[int], list[str]]:
        hybrid = _build_hybrid(httpx.MockTransport(handler), now)
        rows = [
            row.open_time
            async for row in hybrid.fetch_klines(
                "BTCUSDT",
                "1m",
                datetime(2026, 7, 14, tzinfo=timezone.utc),
                datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
            )
        ]
        segments = [s.segment_type for s in hybrid.source_segments]
        await hybrid.close()
        return rows, segments

    rows, segments = asyncio.run(run())
    assert rows == sorted(rows)
    assert len(rows) == len(set(rows))  # 无重复
    assert any(t >= rest_start for t in rows)  # 含 REST 尾部
    assert "rest_tail" in segments


def test_hybrid_tolerates_rest_415_and_keeps_archive() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    zips = {
        "/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-07-15.zip": _daily_zip(
            "BTCUSDT", "1m", 2026, 7, 15, 1440
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".CHECKSUM"):
            data = zips.get(path[: -len(".CHECKSUM")])
            return httpx.Response(404) if data is None else httpx.Response(
                200, text=f"{sha256_hexdigest(data)}  f.csv\n"
            )
        data = zips.get(path)
        return httpx.Response(404) if data is None else httpx.Response(200, content=data)

    async def run() -> tuple[int, list[str], list[str]]:
        hybrid = _build_hybrid(httpx.MockTransport(handler), now, rest_status=415)
        rows = []
        async for row in hybrid.fetch_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 7, 15, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
        ):
            rows.append(row.open_time)
        warnings = list(hybrid.warnings)
        statuses = [s.status for s in hybrid.source_segments if s.segment_type == "rest_tail"]
        await hybrid.close()
        return len(rows), warnings, statuses

    count, warnings, statuses = asyncio.run(run())
    assert count == 1440  # 归档全部成功
    assert warnings  # 产生告警
    assert "UNAVAILABLE" in statuses


def test_hybrid_reraises_rest_415_when_not_tolerated() -> None:
    from data_sources.base import RestUnavailableError

    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    zips = {
        "/data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2026-07-15.zip": _daily_zip(
            "BTCUSDT", "1m", 2026, 7, 15, 10
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(".CHECKSUM"):
            data = zips.get(path[: -len(".CHECKSUM")])
            return httpx.Response(404) if data is None else httpx.Response(
                200, text=f"{sha256_hexdigest(data)}  f.csv\n"
            )
        data = zips.get(path)
        return httpx.Response(404) if data is None else httpx.Response(200, content=data)

    async def run() -> None:
        hybrid = _build_hybrid(httpx.MockTransport(handler), now, rest_status=415, tolerate=False)
        async for _ in hybrid.fetch_klines(
            "BTCUSDT",
            "1m",
            datetime(2026, 7, 15, tzinfo=timezone.utc),
            datetime(2026, 7, 16, 12, tzinfo=timezone.utc),
        ):
            pass
        await hybrid.close()

    with pytest.raises(RestUnavailableError):
        asyncio.run(run())
