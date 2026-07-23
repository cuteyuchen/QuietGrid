from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from core.models import GridState, RiskAction, SymbolSession
from core.scheduler import Scheduler
from strategy.grid_calculator import GridConfig, calculate_grid_params
from strategy.inventory import InventoryLevel, InventorySnapshot
from strategy.risk import RiskConfig, RiskManager


NY = ZoneInfo("America/New_York")


class FakeScheduler:
    def __init__(self, force_close: bool) -> None:
        self.force_close = force_close

    def should_force_close(self, now_utc=None) -> bool:
        return self.force_close


def _session(realized_pnl: float = 0.0) -> SymbolSession:
    klines = [
        {
            "high": (close := 100 + ((idx % 10) - 5) * 0.05) + 0.08,
            "low": close - 0.08,
            "close": close,
        }
        for idx in range(60)
    ]
    params = calculate_grid_params("AAPLUSDT", klines, 100.0, 0.0001, GridConfig())
    return SymbolSession(
        session_id=1,
        symbol="AAPLUSDT",
        state=GridState.RUNNING,
        params=params,
        orders=[],
        realized_pnl=realized_pnl,
        capital=200,
        leverage=10,
        open_time=datetime.now(tz=NY),
    )


def _inventory(
    *,
    unrealized_pnl: float = 0.0,
    gross_notional: float = 0.0,
    level: InventoryLevel = InventoryLevel.NORMAL,
    utilization: float = 0.0,
    risk_score: float = 0.0,
) -> InventorySnapshot:
    return InventorySnapshot(
        net_qty=0,
        net_notional=0,
        gross_notional=gross_notional,
        avg_entry_price=None,
        unrealized_pnl=unrealized_pnl,
        utilization=utilization,
        risk_score=risk_score,
        level=level,
        unpaired_lots=(),
    )


def _manager(**kwargs) -> RiskManager:
    return RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(
            take_profit_usdt=kwargs.pop("take_profit_usdt", 4),
            total_capital_limit=1000,
            max_concurrent=3,
            **kwargs,
        ),
    )


def test_force_close_has_highest_priority() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=True),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )

    decision = manager.evaluate_symbol(_session(realized_pnl=20), 50.0)

    assert decision.action == RiskAction.FORCE_CLOSE
    assert decision.priority == 1


def test_profit_protection_uses_net_pnl_instead_of_realized_pnl_only() -> None:
    manager = _manager(take_profit_usdt=4, profit_estimated_exit_cost_rate=0.001)

    decision = manager.evaluate_symbol(
        _session(realized_pnl=4),
        100.0,
        inventory=_inventory(unrealized_pnl=-1, gross_notional=100),
    )

    assert decision.action == RiskAction.NONE
    assert manager.profit_protection.snapshot(1) == pytest.approx(2.9)


def test_profit_protection_arms_without_immediate_close() -> None:
    manager = _manager(take_profit_usdt=4)

    decision = manager.evaluate_symbol(
        _session(realized_pnl=4),
        100.0,
        inventory=_inventory(),
    )

    assert decision.action == RiskAction.NONE
    assert manager.profit_protection.snapshot(1) == pytest.approx(4)


def test_profit_protection_stages_suppress_reduce_and_close() -> None:
    manager = _manager(take_profit_usdt=4, profit_estimated_exit_cost_rate=0)
    session = _session(realized_pnl=4)
    inventory = _inventory()

    assert manager.evaluate_symbol(session, 100.0, inventory=inventory).action == RiskAction.NONE

    session.realized_pnl = 3.0
    suppress = manager.evaluate_symbol(session, 100.0, inventory=inventory)
    assert suppress.action == RiskAction.DEFEND
    assert "25.00%" in suppress.reason

    session.realized_pnl = 2.6
    reduce = manager.evaluate_symbol(session, 100.0, inventory=inventory)
    assert reduce.action == RiskAction.REDUCE
    assert "35.00%" in reduce.reason

    session.realized_pnl = 2.0
    close = manager.evaluate_symbol(session, 100.0, inventory=inventory)
    assert close.action == RiskAction.CLOSE
    assert "50.00%" in close.reason


def test_profit_protection_runtime_config_update_preserves_peak() -> None:
    manager = _manager(take_profit_usdt=4, profit_estimated_exit_cost_rate=0)
    session = _session(realized_pnl=5)
    inventory = _inventory()
    manager.evaluate_symbol(session, 100.0, inventory=inventory)

    manager.update_config(replace(manager.config, take_profit_usdt=6))

    assert manager.profit_protection.config.activation_profit_usdt == 6
    assert manager.profit_protection.snapshot(session.session_id) == pytest.approx(5)


def test_profit_floor_closes_while_profit_is_still_positive() -> None:
    manager = _manager(
        take_profit_usdt=4,
        profit_minimum_locked_ratio=0.50,
        profit_suppress_drawdown_pct=0.70,
        profit_reduce_drawdown_pct=0.80,
        profit_close_drawdown_pct=0.90,
        profit_estimated_exit_cost_rate=0,
    )
    session = _session(realized_pnl=4)
    inventory = _inventory()
    manager.evaluate_symbol(session, 100.0, inventory=inventory)

    session.realized_pnl = 1.9
    decision = manager.evaluate_symbol(session, 100.0, inventory=inventory)

    assert decision.action == RiskAction.CLOSE
    assert "最低保留线" in decision.reason


def test_profit_floor_closes_after_gap_below_zero() -> None:
    manager = _manager(
        take_profit_usdt=4,
        profit_minimum_locked_ratio=0.50,
        profit_estimated_exit_cost_rate=0,
    )
    session = _session(realized_pnl=4)
    inventory = _inventory()
    manager.evaluate_symbol(session, 100.0, inventory=inventory)

    session.realized_pnl = -0.1
    decision = manager.evaluate_symbol(session, 100.0, inventory=inventory)

    assert decision.action == RiskAction.CLOSE
    assert "最低保留线" in decision.reason


def test_missing_inventory_snapshot_never_arms_realized_only_take_profit() -> None:
    manager = _manager(take_profit_usdt=4)

    decision = manager.evaluate_symbol(_session(realized_pnl=20), 100.0)

    assert decision.action == RiskAction.NONE
    assert manager.profit_protection.snapshot(1) is None


def test_invalid_profit_threshold_order_fails_fast() -> None:
    with pytest.raises(ValueError, match="suppress < reduce < close"):
        _manager(
            profit_suppress_drawdown_pct=0.50,
            profit_reduce_drawdown_pct=0.35,
            profit_close_drawdown_pct=0.40,
        )


def test_upper_dynamic_stop_enters_hard_stop_cooldown() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )
    session = _session()
    assert session.params is not None
    stop_buffer_pct = 1 - session.params.stop_loss_price / session.params.lower

    decision = manager.evaluate_symbol(session, session.params.upper * (1 + stop_buffer_pct))

    assert decision.action == RiskAction.COOLDOWN
    assert "区间外硬止损线" in decision.reason
    assert decision.priority == 3


def test_invalid_price_or_pnl_closes_symbol() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )

    for bad_price in ("nan", "inf", "-inf", 0, -1):
        decision = manager.evaluate_symbol(_session(), bad_price)  # type: ignore[arg-type]

        assert decision.action == RiskAction.CLOSE
        assert "价格异常" in decision.reason

    decision = manager.evaluate_symbol(_session(realized_pnl=float("nan")), 100.0)

    assert decision.action == RiskAction.CLOSE
    assert "盈亏异常" in decision.reason


def test_capital_and_concurrency_limits_skip_new_symbol() -> None:
    manager = RiskManager(
        scheduler=Scheduler(),
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=300, max_concurrent=1),
    )

    decision = manager.can_open_new_symbol([_session()], new_capital=200)

    assert decision.action == RiskAction.SKIP


def test_capital_limit_has_priority_over_concurrency_limit() -> None:
    manager = RiskManager(
        scheduler=Scheduler(),
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=300, max_concurrent=1),
    )

    decision = manager.can_open_new_symbol([_session()], new_capital=200)

    assert decision.action == RiskAction.SKIP
    assert "总资金" in decision.reason
    assert decision.priority == 5


def test_invalid_capital_values_skip_new_symbol() -> None:
    manager = RiskManager(
        scheduler=Scheduler(),
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )

    for bad_capital in ("nan", "inf", 0, -1):
        decision = manager.can_open_new_symbol([], bad_capital)  # type: ignore[arg-type]

        assert decision.action == RiskAction.SKIP
        assert "本金" in decision.reason

    active = _session()
    active.capital = float("nan")
    decision = manager.can_open_new_symbol([active], 200)

    assert decision.action == RiskAction.SKIP
    assert "资金占用异常" in decision.reason


def test_v2_risk_manager_halts_window_at_loss_budget() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(
            take_profit_usdt=10,
            total_capital_limit=1000,
            max_concurrent=3,
            max_window_loss_pct=0.015,
        ),
    )

    decision = manager.evaluate_symbol(_session(), 100, window_pnl=-15)

    assert decision.action == RiskAction.HALT_WINDOW


def test_v2_risk_manager_does_not_force_close_normal_grid_on_floating_loss() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(
            take_profit_usdt=10,
            total_capital_limit=1000,
            max_concurrent=3,
            max_session_loss_pct=0.005,
        ),
    )
    inventory = InventorySnapshot(
        net_qty=1,
        net_notional=100,
        gross_notional=100,
        avg_entry_price=106,
        unrealized_pnl=-6,
        utilization=0.3,
        risk_score=20,
        level=InventoryLevel.NORMAL,
        unpaired_lots=(),
    )

    decision = manager.evaluate_symbol(_session(), 100, inventory=inventory)

    assert decision.action == RiskAction.NONE


def test_v2_risk_manager_reduces_high_inventory() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )
    inventory = InventorySnapshot(
        net_qty=1,
        net_notional=100,
        gross_notional=100,
        avg_entry_price=100,
        unrealized_pnl=0,
        utilization=0.7,
        risk_score=60,
        level=InventoryLevel.HIGH,
        unpaired_lots=(),
    )

    decision = manager.evaluate_symbol(_session(), 100, inventory=inventory)

    assert decision.action == RiskAction.REDUCE


def test_v2_entry_requires_regime_approval() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )

    decision = manager.can_open_new_symbol([], 200, regime_allowed=False)

    assert decision.action == RiskAction.BLOCK


def test_v2_entry_halts_after_consecutive_session_losses() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),
        config=RiskConfig(
            take_profit_usdt=10,
            total_capital_limit=1000,
            max_concurrent=3,
            max_consecutive_session_losses=2,
        ),
    )

    decision = manager.can_open_new_symbol([], 200, consecutive_session_losses=2)

    assert decision.action == RiskAction.HALT_WINDOW
    assert "连续亏损" in decision.reason
