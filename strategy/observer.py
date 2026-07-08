from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.models import GridParams
from exchange.base import ExchangeClient
from strategy.grid_calculator import GridConfig, calculate_grid_params


@dataclass(frozen=True)
class ObserverConfig:
    observe_hours: float = 3
    kline_interval: str = "1m"
    min_samples: int = 30
    live_observation: bool = False
    observe_check_seconds: float = 60


class ObservationAborted(RuntimeError):
    pass


class Observer:
    def __init__(self, exchange: ExchangeClient, observer_config: ObserverConfig, grid_config: GridConfig) -> None:
        self.exchange = exchange
        self.observer_config = observer_config
        self.grid_config = grid_config

    async def collect_and_calculate(self, symbol: str, current_price: float) -> GridParams:
        return await self.calculate_from_recent_klines(symbol, current_price)

    async def observe_then_calculate(
        self,
        symbol: str,
        current_price: float,
        should_abort: Callable[[], bool] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> GridParams:
        if self.observer_config.live_observation:
            remaining_seconds = max(0.0, self.observer_config.observe_hours * 3600)
            while remaining_seconds > 0:
                if should_abort is not None and should_abort():
                    raise ObservationAborted("观察期内触发强制离场，中止建仓。")
                step = min(self.observer_config.observe_check_seconds, remaining_seconds)
                await sleep_fn(step)
                remaining_seconds -= step
            if should_abort is not None and should_abort():
                raise ObservationAborted("观察期内触发强制离场，中止建仓。")
        return await self.calculate_from_recent_klines(symbol, current_price)

    async def calculate_from_recent_klines(self, symbol: str, current_price: float) -> GridParams:
        limit = max(int(self.observer_config.observe_hours * 60), self.observer_config.min_samples)
        klines = await self.exchange.get_klines(symbol, self.observer_config.kline_interval, limit)
        funding_rate = await self.exchange.get_funding_rate(symbol)
        effective_grid_config = GridConfig(
            range_method=self.grid_config.range_method,
            std_k=self.grid_config.std_k,
            quantile_upper=self.grid_config.quantile_upper,
            quantile_lower=self.grid_config.quantile_lower,
            min_step_pct=self.grid_config.min_step_pct,
            safety_multiplier=self.grid_config.safety_multiplier,
            max_grid_num=self.grid_config.max_grid_num,
            max_range_pct=self.grid_config.max_range_pct,
            atr_period=self.grid_config.atr_period,
            stop_buffer_pct=self.grid_config.stop_buffer_pct,
            min_samples=self.observer_config.min_samples,
            volatility_refresh_seconds=self.grid_config.volatility_refresh_seconds,
        )
        return calculate_grid_params(symbol, klines, current_price, funding_rate, effective_grid_config)
