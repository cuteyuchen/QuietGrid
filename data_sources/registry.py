"""受控历史数据源注册表。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from data_sources.base import DataSourceError, HistoricalDataSource


DataSourceFactory = Callable[..., HistoricalDataSource]


class HistoricalDataSourceRegistry:
    """只允许应用显式注册的数据源，避免把用户输入当作远程 URL。"""

    def __init__(self) -> None:
        self._factories: dict[str, DataSourceFactory] = {}

    def register(self, provider_id: str, factory: DataSourceFactory) -> None:
        normalized = provider_id.strip().lower()
        if not normalized:
            raise ValueError("provider_id 不能为空。")
        if normalized in self._factories:
            raise ValueError(f"历史数据源已注册: {normalized}")
        self._factories[normalized] = factory

    def create(self, provider_id: str, **kwargs: Any) -> HistoricalDataSource:
        normalized = provider_id.strip().lower()
        factory = self._factories.get(normalized)
        if factory is None:
            raise DataSourceError(f"不支持的历史数据源: {normalized or provider_id}")
        source = factory(**kwargs)
        if source.provider_id.strip().lower() != normalized:
            raise DataSourceError(f"历史数据源标识不一致: 期望 {normalized}，实际 {source.provider_id}")
        return source

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))
