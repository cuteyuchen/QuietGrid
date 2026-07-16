from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

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


def test_force_close_has_highest_priority() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=True),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )

    decision = manager.evaluate_symbol(_session(realized_pnl=20), 50.0)

    assert decision.action == RiskAction.FORCE_CLOSE
    assert decision.priority == 1


def test_take_profit_closes_symbol() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )

    decision = manager.evaluate_symbol(_session(realized_pnl=10), 100.0)

    assert decision.action == RiskAction.CLOSE
    assert "止盈" in decision.reason


def test_upper_dynamic_stop_closes_before_cooldown() -> None:
    manager = RiskManager(
        scheduler=FakeScheduler(force_close=False),  # type: ignore[arg-type]
        config=RiskConfig(take_profit_usdt=10, total_capital_limit=1000, max_concurrent=3),
    )
    session = _session()
    assert session.params is not None
    stop_buffer_pct = 1 - session.params.stop_loss_price / session.params.lower

    decision = manager.evaluate_symbol(session, session.params.upper * (1 + stop_buffer_pct))

    assert decision.action == RiskAction.CLOSE
    assert "上方动态止损线" in decision.reason
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


def test_v2_risk_manager_uses_inventory_unrealized_loss() -> None:
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

    assert decision.action == RiskAction.CLOSE


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
