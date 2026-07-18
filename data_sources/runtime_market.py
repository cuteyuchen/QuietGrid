"""运行时行情数据与回测历史数据的类型隔离。

实盘/测试网决策必须走 RuntimeMarketDataProvider（交易所 REST/WS）。
回测必须走 HistoricalDataSource（归档/冻结数据集）。
禁止把 HistoricalDataSource 直接注入 TradingController 下单路径。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from data_sources.base import HistoricalDataSource


@runtime_checkable
class RuntimeMarketDataProvider(Protocol):
    """实时运行决策用 K 线/行情接口。实现方必须是交易所适配器。"""

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]: ...

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]: ...

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]: ...

    async def get_funding_rate(self, symbol: str) -> float: ...


# 回测历史数据源类型别名：仅用于 dataset/backtest 管线。
HistoricalBacktestProvider = HistoricalDataSource


def assert_runtime_provider(provider: object) -> RuntimeMarketDataProvider:
    if isinstance(provider, HistoricalDataSource):
        raise TypeError(
            "禁止将 HistoricalBacktestProvider/HistoricalDataSource 用于实盘运行决策；"
            "请使用交易所 RuntimeMarketDataProvider。"
        )
    if not hasattr(provider, "get_klines"):
        raise TypeError("RuntimeMarketDataProvider 必须提供 get_klines。")
    return provider  # type: ignore[return-value]


def assert_backtest_provider(provider: object) -> HistoricalBacktestProvider:
    if not isinstance(provider, HistoricalDataSource):
        raise TypeError("回测数据源必须实现 HistoricalDataSource。")
    return provider
