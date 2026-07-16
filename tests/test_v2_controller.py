from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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
from core.models import GridParams, GridState, SymbolSession


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


def test_v2_control_command_is_executed_by_trader_process(tmp_path) -> None:
    async def run() -> None:
        controller = _controller(tmp_path)
        now = datetime.now(timezone.utc)
        command = controller.repository.enqueue_control_command(
            command_type="PAUSE_NEW_ENTRIES",
            target_type="SYSTEM",
            target_id=None,
            payload={},
            reason="测试暂停",
            idempotency_key="controller-pause-0001",
            requested_at=now,
        )

        result = await controller.process_control_commands_once(now)

        assert result == [(command["command_id"], "executed")]
        assert controller.repository.new_entries_paused() is True
        stored = controller.repository.get_control_command(command["command_id"])
        assert stored is not None
        assert stored["status"] == "EXECUTED"

    asyncio.run(run())


def test_v2_control_command_rejects_unknown_action(tmp_path) -> None:
    async def run() -> None:
        controller = _controller(tmp_path)
        now = datetime.now(timezone.utc)
        command = controller.repository.enqueue_control_command(
            command_type="RAISE_LEVERAGE",
            target_type="SYSTEM",
            target_id=None,
            payload={"leverage": 5},
            reason="不允许的风险升级",
            idempotency_key="controller-risk-0001",
            requested_at=now,
        )

        result = await controller.process_control_commands_once(now)

        assert result == [(command["command_id"], "rejected")]
        stored = controller.repository.get_control_command(command["command_id"])
        assert stored is not None
        assert stored["status"] == "REJECTED"

    asyncio.run(run())


def test_v2_cooldown_recovery_requires_flat_exchange_position(tmp_path) -> None:
    async def run() -> None:
        controller = _controller(tmp_path)
        now = datetime.now(timezone.utc)
        controller.grid_config = GridConfig(min_step_pct=0.01)
        session = SymbolSession(
            session_id=controller.repository.create_session(
                window_id=controller.repository.create_window(now),
                symbol="AAPLUSDT",
                state=GridState.COOLDOWN.value,
                capital=200,
                leverage=1,
                open_time=now,
            ),
            symbol="AAPLUSDT",
            state=GridState.COOLDOWN,
            params=GridParams(
                symbol="AAPLUSDT",
                upper=101,
                lower=99,
                center=100,
                grid_num=8,
                step_pct=0.0025,
                grid_prices=[99 + index * 0.25 for index in range(9)],
                baseline_atr=1,
                stop_loss_price=98,
                calculated_at=now,
            ),
            orders=[],
            realized_pnl=0,
            capital=200,
            leverage=1,
            open_time=now,
            state_entered_at=now - timedelta(minutes=20),
        )
        controller.active_sessions[session.symbol] = session
        controller.exchange.positions[session.symbol] = 0.5  # type: ignore[attr-defined]

        recovered = await controller._try_recover_from_cooldown(session, now)

        assert recovered is False
        assert session.state == GridState.COOLDOWN
        log = controller.repository.recent_rows("system_logs", limit=1)[0]
        assert "not flat" in log["message"]

    asyncio.run(run())
