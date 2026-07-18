from __future__ import annotations

import pytest

from data_sources.base import HistoricalDataSource
from data_sources.runtime_market import assert_backtest_provider, assert_runtime_provider
from exchange.mock import MockExchangeClient


class DummyHistorical(HistoricalDataSource):
    provider_id = "dummy"

    async def list_symbols(self, query: str = ""):
        return []

    async def preview(self, symbol, interval, start_time, end_time):
        raise NotImplementedError

    def fetch_klines(self, symbol, interval, start_time, end_time):
        raise NotImplementedError


def test_runtime_provider_accepts_exchange() -> None:
    provider = assert_runtime_provider(MockExchangeClient())
    assert hasattr(provider, "get_klines")


def test_runtime_provider_rejects_historical() -> None:
    with pytest.raises(TypeError):
        assert_runtime_provider(DummyHistorical())


def test_backtest_provider_accepts_historical() -> None:
    provider = assert_backtest_provider(DummyHistorical())
    assert provider.provider_id == "dummy"


def test_backtest_provider_rejects_exchange() -> None:
    with pytest.raises(TypeError):
        assert_backtest_provider(MockExchangeClient())
