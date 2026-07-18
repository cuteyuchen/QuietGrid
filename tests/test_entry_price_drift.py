from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from strategy.controller import TradingController
from strategy.grid_calculator import GridCalculationError


class _AlwaysOpen:
    def is_in_window(self, now=None) -> bool:
        return True

    def should_force_close(self, now=None) -> bool:
        return False


class _TickerExchange:
    def __init__(self, last_price: float) -> None:
        self.last_price = last_price

    async def get_24h_ticker(self, symbol: str) -> dict:
        return {"lastPrice": self.last_price}


@pytest.mark.asyncio
async def test_revalidate_entry_price_blocks_drift() -> None:
    controller = object.__new__(TradingController)
    controller.scheduler = _AlwaysOpen()
    controller.exchange = _TickerExchange(101.0)
    controller._entry_config = {"max_price_drift_pct": 0.002, "revalidate_before_place": True}
    with pytest.raises(GridCalculationError) as exc:
        await controller._revalidate_entry_price("BTCUSDT", 100.0, datetime.now(timezone.utc))
    assert "PRICE_DRIFT" in str(exc.value)


@pytest.mark.asyncio
async def test_revalidate_entry_price_allows_small_move() -> None:
    controller = object.__new__(TradingController)
    controller.scheduler = _AlwaysOpen()
    controller.exchange = _TickerExchange(100.1)
    controller._entry_config = {"max_price_drift_pct": 0.002, "revalidate_before_place": True}
    price = await controller._revalidate_entry_price("BTCUSDT", 100.0, datetime.now(timezone.utc))
    assert price == 100.1
