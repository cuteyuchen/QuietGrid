from __future__ import annotations

import asyncio

from exchange.mock import MockExchangeClient
from strategy.grid_calculator import GridConfig
from strategy.observer import ObservationAborted, Observer, ObserverConfig


def test_observer_collects_klines_and_calculates_grid_params() -> None:
    async def run() -> None:
        observer = Observer(
            MockExchangeClient(),
            ObserverConfig(observe_hours=1, kline_interval="1m", min_samples=30),
            GridConfig(),
        )

        params = await observer.collect_and_calculate("AAPLUSDT", current_price=100.0)

        assert params.symbol == "AAPLUSDT"
        assert params.grid_num >= 1
        assert params.lower < 100 < params.upper

    asyncio.run(run())


def test_observer_live_observation_waits_before_calculation() -> None:
    async def run() -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        observer = Observer(
            MockExchangeClient(),
            ObserverConfig(observe_hours=0.01, kline_interval="1m", min_samples=30, live_observation=True, observe_check_seconds=20),
            GridConfig(),
        )

        params = await observer.observe_then_calculate("AAPLUSDT", 100.0, sleep_fn=fake_sleep)

        assert params.symbol == "AAPLUSDT"
        assert sleeps == [20, 16]

    asyncio.run(run())


def test_observer_live_observation_aborts_when_force_close_triggers() -> None:
    async def run() -> None:
        observer = Observer(
            MockExchangeClient(),
            ObserverConfig(observe_hours=0.01, kline_interval="1m", min_samples=30, live_observation=True, observe_check_seconds=20),
            GridConfig(),
        )

        try:
            await observer.observe_then_calculate("AAPLUSDT", 100.0, should_abort=lambda: True)
        except ObservationAborted as exc:
            assert "强制离场" in str(exc)
        else:
            raise AssertionError("live observation should abort")

    asyncio.run(run())


def test_observer_live_observation_aborts_after_final_wait() -> None:
    async def run() -> None:
        should_abort = False

        async def fake_sleep(seconds: float) -> None:
            nonlocal should_abort
            should_abort = True

        observer = Observer(
            MockExchangeClient(),
            ObserverConfig(observe_hours=0.01, kline_interval="1m", min_samples=30, live_observation=True, observe_check_seconds=60),
            GridConfig(),
        )

        try:
            await observer.observe_then_calculate("AAPLUSDT", 100.0, should_abort=lambda: should_abort, sleep_fn=fake_sleep)
        except ObservationAborted as exc:
            assert "强制离场" in str(exc)
        else:
            raise AssertionError("live observation should abort after the final wait before calculation")

    asyncio.run(run())
