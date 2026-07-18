"""历史行情数据源统一接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime

from data_sources.models import DatasetPreview, FundingEvent, HistoricalSymbol, NormalizedKline


class DataSourceError(RuntimeError):
    """数据源读取、解析或远程请求失败。"""


class RestUnavailableError(DataSourceError):
    """REST 补尾链路暂时不可用（如 415/403/网关限制），归档范围仍可保留。"""


class FundingUnavailableError(DataSourceError):
    """该数据源不提供历史资金费事件。"""


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

    # 资金费历史与 K 线分开获取（见设计 §6.2），默认数据源不提供。
    supports_funding: bool = False

    def fetch_funding(
        self,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[FundingEvent]:
        """按 funding_time 升序流式返回历史资金费结算事件。

        默认抛出 FundingUnavailableError；支持的数据源需重写并将
        supports_funding 置为 True。返回值为异步迭代器。
        """
        raise FundingUnavailableError(
            f"数据源 {getattr(self, 'provider_id', type(self).__name__)} 不提供历史资金费事件。"
        )
