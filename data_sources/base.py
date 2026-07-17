"""历史行情数据源统一接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from data_sources.models import DatasetPreview, HistoricalSymbol, NormalizedKline


class DataSourceError(RuntimeError):
    """数据源读取、解析或远程请求失败。"""


class HistoricalDataSource(ABC):
    """所有在线与本地历史行情提供方必须实现的最小接口。"""

    provider_id: str

    @abstractmethod
    async def list_symbols(self, query: str = "") -> list[HistoricalSymbol]:
        """返回当前数据源可用的交易标的。"""

    @abstractmethod
    async def preview(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> DatasetPreview:
        """在不创建冻结数据集的前提下估算数据量与请求成本。"""

    @abstractmethod
    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[NormalizedKline]:
        """按时间升序流式返回已经标准化的 K 线。"""
