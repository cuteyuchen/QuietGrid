from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from db.database import init_db
from db.repository import Repository
from exchange.mock import MockExchangeClient
from strategy.adaptive_grid import AdaptiveGridConfig
from strategy.controller import ControllerConfig, TradingController, V2FeatureFlags
from strategy.cooldown import CooldownConfig
from strategy.grid_calculator import GridConfig
from strategy.inventory import InventoryConfig
from strategy.observer import ObserverConfig
from strategy.regime import RegimeConfig
from strategy.selector import SelectionConfig, SelectionScore


class OpenScheduler:
    def is_in_window(self, now=None) -> bool:
        return True

    def should_force_close(self, now=None) -> bool:
        return False


def _controller(tmp_path, *, leverage: int = 1) -> TradingController:
    db_path = tmp_path / "v2-controller.db"
    init_db(db_path)
    return TradingController(
        exchange=MockExchangeClient(),
        scheduler=OpenScheduler(),  # type: ignore[arg-type]
        repository=Repository(db_path),
        selector_config=SelectionConfig(max_concurrent=1),
        observer_config=ObserverConfig(observe_hours=1, min_samples=30),
        grid_config=GridConfig(),
        controller_config=ControllerConfig(
            capital_per_symbol=200,
            leverage=leverage,
            max_concurrent=1,
            take_profit_usdt=10,
            total_capital_limit=1000,
            effective_leverage_cap=1,
            max_session_loss_pct=0.005,
            max_window_loss_pct=0.015,
            max_symbol_inventory_pct=0.10,
            max_window_stop_count=3,
        ),
        cooldown_config=CooldownConfig(),
        feature_flags=V2FeatureFlags(
            regime_v2=True,
            inventory_manager=True,
            adaptive_grid_v2=True,
            risk_manager_v2=True,
        ),
        regime_config=RegimeConfig(
            max_spread_pct=0.003,
            min_depth_usdt=1000,
            enter_threshold=60,
            stay_threshold=50,
        ),
        adaptive_grid_config=AdaptiveGridConfig(),
        inventory_config=InventoryConfig(),
    )


def test_v2_controller_fails_startup_when_leverage_exceeds_cap(tmp_path) -> None:
    async def run() -> None:
        controller = _controller(tmp_path, leverage=2)

        result = await controller.validate_startup(datetime.now(timezone.utc))

        assert result.ok is False
        assert "杠杆" in result.reason

    asyncio.run(run())


def test_v2_candidate_analysis_persists_regime_and_adaptive_grid(tmp_path) -> None:
    async def run() -> None:
        controller = _controller(tmp_path)
        item = SelectionScore(
            symbol="AAPLUSDT",
            score=1,
            volume_score=1,
            depth_score=1,
            volume_24h=1_000_000,
            depth_usdt=2_000,
            bid_price=99.9,
            ask_price=100.1,
            spread_pct=0.002,
        )
        now = datetime.now(timezone.utc)

        current_price, params, _snapshot = await controller._analyze_round_candidate(
            item,
            ObserverConfig(observe_hours=1, min_samples=30),
            GridConfig(),
            now,
        )

        assert current_price == 100
        assert params.grid_mode == "adaptive_v2"
        assert params.regime_score is not None
        regime = controller.repository.latest_regime_decision("AAPLUSDT")
        assert regime is not None
        assert regime["allowed"] == 1
        assert controller.repository.recent_rows("event_store", 1)[0]["event_type"] == "REGIME_CHANGED"

    asyncio.run(run())
