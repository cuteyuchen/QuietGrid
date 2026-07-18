"""Binance 官方归档（data.binance.vision）历史 K 线数据源。

优先使用官方月度 / 每日归档 ZIP，逐个校验官方 SHA-256 后安全解压。相较 REST
分页，归档链路不受当前交易列表、地区限流与 415 影响，适合大范围历史回测。
本数据源只负责已归档区间；尚未归档的最新尾部由 REST 数据源在 Hybrid 中补齐。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from math import ceil
import inspect
import time
from typing import Any

import httpx

from data_sources.archive_checksum import parse_checksum_file, sha256_hexdigest
from data_sources.archive_planner import BinanceArchivePlanner
from data_sources.archive_zip_reader import read_archive_klines
from data_sources.base import DataSourceError, HistoricalDataSource
from data_sources.csv_source import INTERVAL_MILLISECONDS
from data_sources.models import (
    ArchiveSegment,
    ArchiveSegmentType,
    DatasetPreview,
    HistoricalSymbol,
    NormalizedKline,
    SourceSegmentMetadata,
)


BINANCE_ARCHIVE_BASE_URL = "https://data.binance.vision"
SUPPORTED_INTERVALS = frozenset(("1m", "5m", "15m", "1h"))
DEFAULT_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024


class BinanceArchiveHistoricalDataSource(HistoricalDataSource):
    provider_id = "binance_archive"

    def __init__(
        self,
        *,
        base_url: str = BINANCE_ARCHIVE_BASE_URL,
        market_path: str = "futures/um",
        prefer_monthly: bool = True,
        verify_official_checksum: bool = True,
        max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
        archive_lag_days: int = 1,
        proxy_config: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
        pause_seconds: float = 0.02,
        provider_id: str | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if provider_id is not None:
            self.provider_id = provider_id.strip().lower()
        self.base_url = base_url.rstrip("/")
        self.market_path = market_path.strip("/")
        self.prefer_monthly = bool(prefer_monthly)
        self.verify_official_checksum = bool(verify_official_checksum)
        self.max_uncompressed_bytes = max(1, int(max_uncompressed_bytes))
        self.archive_lag_days = max(0, int(archive_lag_days))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.pause_seconds = max(0.0, float(pause_seconds))
        self._planner = BinanceArchivePlanner(prefer_monthly=self.prefer_monthly)
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._client = client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": "QuietGrid/2.1 historical-data"},
            **_httpx_proxy_kwargs(proxy_config),
        )
        self._owns_client = client is None
        self.source_segments: list[SourceSegmentMetadata] = []

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "BinanceArchiveHistoricalDataSource":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def archive_available_until(self) -> date:
        """官方每日归档最迟可用日期，默认取当前 UTC 日期减去 archive_lag_days。"""
        today = datetime.fromtimestamp(self._now_ms() / 1000, tz=timezone.utc).date()
        return today - timedelta(days=self.archive_lag_days)

    async def list_symbols(self, query: str = "") -> list[HistoricalSymbol]:
        # 归档不依赖 exchangeInfo：历史标的可能已下架或在当前环境不可见。
        # 交给上游（REST 源或用户直接输入）确定标的，这里仅回显输入。
        normalized = query.strip().upper()
        if not normalized:
            return []
        return [HistoricalSymbol(symbol=normalized, status="ARCHIVE", market="USDS_M")]

    async def archive_exists(self, symbol: str, interval: str) -> bool:
        """通过 HEAD 探测某标的是否存在官方归档（模式 B 的存在性判断）。"""
        normalized = symbol.strip().upper()
        until = self.archive_available_until()
        monthly_url = self._monthly_url(normalized, interval, _month_first(until))
        if await self._head_exists(monthly_url):
            return True
        daily_url = self._daily_url(normalized, interval, until)
        return await self._head_exists(daily_url)

    async def preview(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> DatasetPreview:
        normalized = _validate_request(symbol, interval, start_time, end_time)
        interval_ms = INTERVAL_MILLISECONDS[interval]
        duration_ms = _utc_ms(end_time) - _utc_ms(start_time)
        estimated_rows = max(0, ceil(duration_ms / interval_ms))
        segments = self._planner.plan(start_time, end_time, self.archive_available_until())
        monthly = sum(1 for s in segments if s.segment_type is ArchiveSegmentType.MONTHLY_ARCHIVE)
        daily = sum(1 for s in segments if s.segment_type is ArchiveSegmentType.DAILY_ARCHIVE)
        rest = sum(1 for s in segments if s.is_rest)
        warnings: list[str] = []
        if rest:
            warnings.append("请求范围包含尚未归档的最新尾部，需由 REST 补齐。")
        return DatasetPreview(
            provider=self.provider_id,
            symbol=normalized,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            estimated_rows=estimated_rows,
            estimated_pages=monthly + daily,
            estimated_size_bytes=estimated_rows * 128,
            warnings=tuple(warnings),
        )

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[NormalizedKline]:
        normalized = _validate_request(symbol, interval, start_time, end_time)
        self.source_segments = []
        segments = self._planner.plan(start_time, end_time, self.archive_available_until())
        for segment in segments:
            if segment.is_rest:
                # 归档源不负责最新尾部，交给 Hybrid 的 REST 源。
                continue
            async for row in self._emit_segment(normalized, interval, segment):
                yield row
            if self.pause_seconds:
                await self._sleep(self.pause_seconds)

    async def _emit_segment(
        self,
        symbol: str,
        interval: str,
        segment: ArchiveSegment,
    ) -> AsyncIterator[NormalizedKline]:
        if segment.segment_type is ArchiveSegmentType.MONTHLY_ARCHIVE:
            url = self._monthly_url(symbol, interval, segment.period_start)
            csv_name = self._csv_name(symbol, interval, segment.period_start, monthly=True)
            payload = await self._download_zip(url)
            if payload is None:
                # 月包缺失时回退为逐日归档。
                for daily in self._planner.expand_to_daily(segment):
                    async for row in self._emit_segment(symbol, interval, daily):
                        yield row
                return
        else:
            url = self._daily_url(symbol, interval, segment.period_start)
            csv_name = self._csv_name(symbol, interval, segment.period_start, monthly=False)
            payload = await self._download_zip(url)
            if payload is None:
                self.source_segments.append(
                    SourceSegmentMetadata(
                        segment_type=segment.segment_type.value,
                        url=url,
                        status="MISSING",
                        start=segment.period_start.isoformat(),
                        end=segment.period_end.isoformat(),
                    )
                )
                return

        data, official_checksum, local_checksum = payload
        rows = read_archive_klines(
            data,
            expected_csv_name=csv_name,
            max_uncompressed_bytes=self.max_uncompressed_bytes,
        )
        emitted = 0
        for row in rows:
            if segment.start_ms <= row.open_time < segment.end_ms:
                emitted += 1
                yield row
        self.source_segments.append(
            SourceSegmentMetadata(
                segment_type=segment.segment_type.value,
                url=url,
                official_checksum=official_checksum,
                local_checksum=local_checksum,
                rows=emitted,
                start=segment.period_start.isoformat(),
                end=segment.period_end.isoformat(),
            )
        )

    async def _download_zip(
        self,
        url: str,
    ) -> tuple[bytes, str | None, str | None] | None:
        """下载 ZIP 并（可选）校验官方 checksum；404 返回 None 表示归档缺失。"""
        response = await self._request(url)
        if response is None:
            return None
        data = response.content
        local_checksum = sha256_hexdigest(data)
        official_checksum: str | None = None
        if self.verify_official_checksum:
            checksum_response = await self._request(f"{url}.CHECKSUM")
            if checksum_response is None:
                raise DataSourceError(f"官方 CHECKSUM 缺失，无法校验归档: {url}")
            official_checksum = parse_checksum_file(checksum_response.text)
            if local_checksum != official_checksum:
                raise DataSourceError(
                    f"官方 checksum 不匹配：{url} 期望 {official_checksum[:12]}…，"
                    f"实际 {local_checksum[:12]}…。"
                )
        return data, official_checksum, local_checksum

    async def _request(self, url: str) -> httpx.Response | None:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                response = await self._client.get(url, timeout=self.timeout_seconds)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt + 1 >= self.retry_attempts:
                    break
                await self._sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                last_error = DataSourceError(
                    f"Binance 归档暂时不可用: HTTP {response.status_code}"
                )
                if attempt + 1 >= self.retry_attempts:
                    break
                await self._sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            if response.status_code >= 400:
                raise DataSourceError(
                    f"Binance 归档请求被拒绝: HTTP {response.status_code} ({url})"
                )
            return response
        raise DataSourceError(f"Binance 归档请求重试耗尽: {last_error}") from last_error

    async def _head_exists(self, url: str) -> bool:
        try:
            response = await self._client.head(url, timeout=self.timeout_seconds)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise DataSourceError(f"Binance 归档探测失败: {exc}") from exc
        return response.status_code < 400

    def _monthly_url(self, symbol: str, interval: str, period: date) -> str:
        name = self._csv_name(symbol, interval, period, monthly=True).removesuffix(".csv")
        return (
            f"{self.base_url}/data/{self.market_path}/monthly/klines/"
            f"{symbol}/{interval}/{name}.zip"
        )

    def _daily_url(self, symbol: str, interval: str, period: date) -> str:
        name = self._csv_name(symbol, interval, period, monthly=False).removesuffix(".csv")
        return (
            f"{self.base_url}/data/{self.market_path}/daily/klines/"
            f"{symbol}/{interval}/{name}.zip"
        )

    def _csv_name(self, symbol: str, interval: str, period: date, *, monthly: bool) -> str:
        suffix = period.strftime("%Y-%m") if monthly else period.strftime("%Y-%m-%d")
        return f"{symbol}-{interval}-{suffix}.csv"


def _validate_request(
    symbol: str,
    interval: str,
    start_time: datetime,
    end_time: datetime,
) -> str:
    if interval not in SUPPORTED_INTERVALS:
        raise DataSourceError(f"Binance 归档暂不支持周期: {interval}")
    if start_time.tzinfo is None or end_time.tzinfo is None:
        raise ValueError("start_time 和 end_time 必须包含时区。")
    if start_time >= end_time:
        raise ValueError("start_time 必须早于 end_time。")
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol 不能为空。")
    return normalized


def _month_first(value: date) -> date:
    return value.replace(day=1)


def _utc_ms(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _httpx_proxy_kwargs(proxy_config: dict[str, Any] | None) -> dict[str, Any]:
    if not proxy_config or not proxy_config.get("enabled"):
        return {}
    proxy = str(proxy_config.get("https") or proxy_config.get("http") or "").strip()
    if not proxy:
        return {}
    parameters = inspect.signature(httpx.AsyncClient.__init__).parameters
    if "proxy" in parameters:
        return {"proxy": proxy}
    if "proxies" in parameters:
        return {"proxies": proxy}
    raise DataSourceError("当前 httpx 版本不支持代理配置。")


def _utc_ms(value: datetime) -> int:
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _proxy_url(proxy_config: dict[str, Any] | None) -> str | None:
    if not proxy_config or not proxy_config.get("enabled"):
        return None
    value = proxy_config.get("https") or proxy_config.get("http")
    return str(value).strip() or None


def _httpx_proxy_kwargs(proxy_config: dict[str, Any] | None) -> dict[str, Any]:
    proxy = _proxy_url(proxy_config)
    if proxy is None:
        return {}
    parameters = inspect.signature(httpx.AsyncClient.__init__).parameters
    if "proxy" in parameters:
        return {"proxy": proxy}
    if "proxies" in parameters:
        return {"proxies": proxy}
    raise DataSourceError("当前 httpx 版本不支持代理配置。")
