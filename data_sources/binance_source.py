"""Binance USDⓈ-M Futures 在线历史 K 线数据源。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from math import ceil
import inspect
import time
from typing import Any

import httpx

from data_sources.base import DataSourceError, HistoricalDataSource, RestUnavailableError
from data_sources.csv_source import INTERVAL_MILLISECONDS
from data_sources.models import DatasetPreview, FundingEvent, HistoricalSymbol, NormalizedKline


BINANCE_USDS_M_BASE_URL = "https://fapi.binance.com"
SUPPORTED_INTERVALS = frozenset(("1m", "5m", "15m", "1h"))
MAX_PAGE_LIMIT = 1500
FUNDING_PAGE_LIMIT = 1000


class BinanceHistoricalDataSource(HistoricalDataSource):
    provider_id = "binance_rest"
    supports_funding = True

    def __init__(
        self,
        *,
        proxy_config: dict[str, Any] | None = None,
        timeout_seconds: float = 15.0,
        retry_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
        page_limit: int = MAX_PAGE_LIMIT,
        funding_page_limit: int = FUNDING_PAGE_LIMIT,
        pause_seconds: float = 0.05,
        base_url: str = BINANCE_USDS_M_BASE_URL,
        provider_id: str | None = None,
        validate_symbol_listing: bool = True,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        if not 1 <= page_limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"page_limit 必须在 1..{MAX_PAGE_LIMIT} 范围内。")
        if not 1 <= funding_page_limit <= FUNDING_PAGE_LIMIT:
            raise ValueError(f"funding_page_limit 必须在 1..{FUNDING_PAGE_LIMIT} 范围内。")
        if provider_id is not None:
            self.provider_id = provider_id.strip().lower()
        self.base_url = base_url.rstrip("/")
        self.validate_symbol_listing = bool(validate_symbol_listing)
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.page_limit = int(page_limit)
        self.funding_page_limit = int(funding_page_limit)
        self.pause_seconds = max(0.0, float(pause_seconds))
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._client = client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={"User-Agent": "QuietGrid/2.1 historical-data"},
            **_httpx_proxy_kwargs(proxy_config),
        )
        self._owns_client = client is None
        self._symbols: dict[str, HistoricalSymbol] | None = None
        self.pages_fetched = 0

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "BinanceHistoricalDataSource":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def list_symbols(self, query: str = "") -> list[HistoricalSymbol]:
        if self._symbols is None:
            payload = await self._request_json("/fapi/v1/exchangeInfo")
            if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
                raise DataSourceError("Binance exchangeInfo 响应缺少 symbols。")
            symbols: dict[str, HistoricalSymbol] = {}
            for item in payload["symbols"]:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol") or "").strip().upper()
                status = str(item.get("status") or "").strip().upper()
                contract_type = str(item.get("contractType") or "").strip().upper()
                if not symbol or status != "TRADING" or contract_type != "PERPETUAL":
                    continue
                symbols[symbol] = HistoricalSymbol(
                    symbol=symbol,
                    status=status,
                    market="USDS_M",
                    base_asset=str(item.get("baseAsset") or ""),
                    quote_asset=str(item.get("quoteAsset") or ""),
                )
            self._symbols = symbols
        normalized_query = query.strip().upper()
        return [
            item
            for symbol, item in sorted(self._symbols.items())
            if not normalized_query or normalized_query in symbol
        ]

    async def preview(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> DatasetPreview:
        normalized_symbol = await self._validate_request(symbol, interval, start_time, end_time)
        interval_ms = INTERVAL_MILLISECONDS[interval]
        duration_ms = _utc_ms(end_time) - _utc_ms(start_time)
        estimated_rows = max(0, ceil(duration_ms / interval_ms))
        return DatasetPreview(
            provider=self.provider_id,
            symbol=normalized_symbol,
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            estimated_rows=estimated_rows,
            estimated_pages=ceil(estimated_rows / self.page_limit) if estimated_rows else 0,
            estimated_size_bytes=estimated_rows * 128,
        )

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[NormalizedKline]:
        normalized_symbol = await self._validate_request(symbol, interval, start_time, end_time)
        interval_ms = INTERVAL_MILLISECONDS[interval]
        cursor = _utc_ms(start_time)
        end_ms = _utc_ms(end_time)
        self.pages_fetched = 0
        while cursor < end_ms:
            payload = await self._request_json(
                "/fapi/v1/klines",
                params={
                    "symbol": normalized_symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": self.page_limit,
                },
            )
            if not isinstance(payload, list):
                raise DataSourceError("Binance K线响应不是数组。")
            if not payload:
                break
            self.pages_fetched += 1
            last_open_time: int | None = None
            for raw in payload:
                row = _parse_binance_kline(raw)
                if row.open_time < cursor:
                    continue
                if row.open_time >= end_ms:
                    break
                last_open_time = row.open_time
                if row.close_time >= self._now_ms():
                    continue
                yield row
            if last_open_time is None:
                candidate = _raw_open_time(payload[-1])
                if candidate is None:
                    raise DataSourceError("Binance K线分页响应没有有效 open_time。")
                last_open_time = candidate
            next_cursor = last_open_time + interval_ms
            if next_cursor <= cursor:
                raise DataSourceError("Binance K线分页游标未前进，已中止下载。")
            cursor = next_cursor
            if len(payload) < self.page_limit:
                break
            if self.pause_seconds:
                await self._sleep(self.pause_seconds)

    async def fetch_funding(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[FundingEvent]:
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError("start_time 和 end_time 必须包含时区。")
        if start_time >= end_time:
            raise ValueError("start_time 必须早于 end_time。")
        normalized_symbol = symbol.strip().upper()
        cursor = _utc_ms(start_time)
        end_ms = _utc_ms(end_time)
        now_ms = self._now_ms()
        last_funding_time: int | None = None
        while cursor < end_ms:
            payload = await self._request_json(
                "/fapi/v1/fundingRate",
                params={
                    "symbol": normalized_symbol,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": self.funding_page_limit,
                },
            )
            if not isinstance(payload, list):
                raise DataSourceError("Binance 资金费响应不是数组。")
            if not payload:
                break
            page_last_time: int | None = None
            for raw in payload:
                event = _parse_binance_funding(raw)
                if event.funding_time < cursor or event.funding_time >= end_ms:
                    continue
                # 仅纳入已结算的事件，避免把未来时间点当成历史。
                if event.funding_time >= now_ms:
                    continue
                page_last_time = event.funding_time
                if last_funding_time is not None and event.funding_time <= last_funding_time:
                    continue
                last_funding_time = event.funding_time
                yield event
            if page_last_time is None:
                break
            next_cursor = page_last_time + 1
            if next_cursor <= cursor:
                raise DataSourceError("Binance 资金费分页游标未前进，已中止下载。")
            cursor = next_cursor
            if len(payload) < self.funding_page_limit:
                break
            if self.pause_seconds:
                await self._sleep(self.pause_seconds)

    async def _validate_request(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> str:
        if interval not in SUPPORTED_INTERVALS:
            raise DataSourceError(f"Binance 历史数据暂不支持周期: {interval}")
        if start_time.tzinfo is None or end_time.tzinfo is None:
            raise ValueError("start_time 和 end_time 必须包含时区。")
        if start_time >= end_time:
            raise ValueError("start_time 必须早于 end_time。")
        normalized_symbol = symbol.strip().upper()
        # 历史归档补尾场景不要求标的当前处于 TRADING：合约可能已下架，或该产品
        # 在当前网络出口的 exchangeInfo 中不可见，但历史 K 线仍然存在。
        if self.validate_symbol_listing:
            symbols = {item.symbol for item in await self.list_symbols()}
            if normalized_symbol not in symbols:
                raise DataSourceError(f"Binance USDⓈ-M 不存在可交易永续合约: {normalized_symbol}")
        return normalized_symbol

    async def _request_json(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts):
            try:
                response = await self._client.get(
                    f"{self.base_url}{path}",
                    params=params,
                    timeout=self.timeout_seconds,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt + 1 >= self.retry_attempts:
                    break
                await self._sleep(self.retry_backoff_seconds * (2**attempt))
                continue
            if response.status_code == 418:
                raise DataSourceError("Binance 返回 418，当前 IP 已被临时限制，任务已停止。")
            if response.status_code == 429 or response.status_code >= 500:
                last_error = DataSourceError(
                    f"Binance 暂时不可用: HTTP {response.status_code}"
                )
                if attempt + 1 >= self.retry_attempts:
                    break
                retry_after = _retry_after_seconds(response)
                await self._sleep(
                    retry_after
                    if retry_after is not None
                    else self.retry_backoff_seconds * (2**attempt)
                )
                continue
            if response.status_code in (403, 415):
                # 通常来自区域限制、代理转发或网关请求头重写，而非请求本身有误；
                # 归档范围仍应保留，交由 Hybrid 决定是否降级为 READY_WITH_WARNINGS。
                raise RestUnavailableError(
                    f"Binance REST 当前不可用: HTTP {response.status_code} "
                    f"{_safe_error_message(response)}"
                )
            if response.status_code >= 400:
                raise DataSourceError(
                    f"Binance 请求被拒绝: HTTP {response.status_code} {_safe_error_message(response)}"
                )
            try:
                return response.json()
            except ValueError as exc:
                raise DataSourceError("Binance 返回了无效 JSON。") from exc
        if last_error is None:
            raise DataSourceError("Binance 请求失败。")
        raise DataSourceError(f"Binance 请求重试耗尽: {last_error}") from last_error


def _parse_binance_kline(raw: Any) -> NormalizedKline:
    if not isinstance(raw, (list, tuple)) or len(raw) < 9:
        raise DataSourceError("Binance K线字段数量不足。")
    try:
        return NormalizedKline(
            open_time=int(raw[0]),
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            volume=float(raw[5]),
            close_time=int(raw[6]),
            quote_volume=float(raw[7]),
            trade_count=int(raw[8]),
        )
    except (TypeError, ValueError) as exc:
        raise DataSourceError(f"Binance K线字段无效: {exc}") from exc


def _parse_binance_funding(raw: Any) -> FundingEvent:
    if not isinstance(raw, dict):
        raise DataSourceError("Binance 资金费记录不是对象。")
    try:
        funding_time = int(raw["fundingTime"])
        funding_rate = float(raw["fundingRate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DataSourceError(f"Binance 资金费字段无效: {exc}") from exc
    mark_price: float | None = None
    raw_mark = raw.get("markPrice")
    if raw_mark not in (None, ""):
        try:
            candidate = float(raw_mark)
        except (TypeError, ValueError):
            candidate = 0.0
        # Binance 偶尔返回 0/异常标记价，交给 FundingEvent 前先剔除非正值。
        if candidate > 0:
            mark_price = candidate
    return FundingEvent(
        funding_time=funding_time,
        funding_rate=funding_rate,
        mark_price=mark_price,
    )


def _raw_open_time(raw: Any) -> int | None:
    try:
        return int(raw[0])
    except (IndexError, TypeError, ValueError):
        return None


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


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _safe_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    message = str(payload.get("msg") or "").strip()
    return message[:200]
