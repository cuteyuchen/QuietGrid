from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from core.models import GridState, RiskAction, RiskDecision, SymbolSession
from core.scheduler import Scheduler
from strategy.inventory import InventoryLevel, InventorySnapshot


@dataclass(frozen=True)
class RiskConfig:
    take_profit_usdt: float
    total_capital_limit: float
    max_concurrent: int
    effective_leverage_cap: float = float("inf")
    max_session_loss_pct: float = 0.0
    max_window_loss_pct: float = 0.0
    max_consecutive_session_losses: int = 0
    max_window_stop_count: int = 0


class RiskManager:
    def __init__(self, scheduler: Scheduler, config: RiskConfig) -> None:
        self.scheduler = scheduler
        self.config = config

    def evaluate_symbol(
        self,
        session: SymbolSession,
        last_price: float,
        now: datetime | None = None,
        *,
        inventory: InventorySnapshot | None = None,
        account_equity: float | None = None,
        window_pnl: float = 0.0,
        window_stop_count: int = 0,
    ) -> RiskDecision:
        if self.scheduler.should_force_close(now):
            return RiskDecision(RiskAction.FORCE_CLOSE, "临近盘前，触发全局强制离场。", 1)

        if not _is_positive_finite(last_price):
            return RiskDecision(RiskAction.CLOSE, "行情价格异常，强制关闭标的。", 2)

        if not _is_finite(session.realized_pnl):
            return RiskDecision(RiskAction.CLOSE, "已实现盈亏异常，强制关闭标的。", 2)

        if session.leverage > self.config.effective_leverage_cap:
            return RiskDecision(RiskAction.CLOSE, "会话有效杠杆超过 v2 风险上限。", 2)

        equity = account_equity if account_equity is not None else self.config.total_capital_limit
        if not _is_positive_finite(equity):
            return RiskDecision(RiskAction.CLOSE, "账户权益基准异常，强制关闭标的。", 2)
        if (
            self.config.max_window_loss_pct > 0
            and _is_finite(window_pnl)
            and window_pnl <= -float(equity) * self.config.max_window_loss_pct
        ):
            return RiskDecision(RiskAction.HALT_WINDOW, "本窗口累计损失达到全局熔断线。", 2)
        if self.config.max_window_stop_count > 0 and window_stop_count >= self.config.max_window_stop_count:
            return RiskDecision(RiskAction.HALT_WINDOW, "本窗口止损次数达到全局熔断上限。", 2)

        if inventory is not None:
            if inventory.level == InventoryLevel.CRITICAL:
                return RiskDecision(RiskAction.CLOSE, "库存风险达到 CRITICAL。", 2)
            if inventory.level == InventoryLevel.HIGH:
                return RiskDecision(RiskAction.REDUCE, "库存风险达到 HIGH，只允许减仓。", 3)

        if session.realized_pnl >= self.config.take_profit_usdt:
            return RiskDecision(RiskAction.CLOSE, "单标的止盈达标。", 2)

        if session.params is not None:
            if last_price <= session.params.stop_loss_price:
                return RiskDecision(RiskAction.COOLDOWN, "价格跌破区间外硬止损线。", 3)
            upper_stop_loss_price = (
                session.params.upper_stop_loss_price
                or session.params.upper * (1 + _stop_buffer_pct(session.params.lower, session.params.stop_loss_price))
            )
            if last_price >= upper_stop_loss_price:
                return RiskDecision(RiskAction.COOLDOWN, "价格突破区间外硬止损线。", 3)

        if session.state in {GridState.RUNNING, GridState.DEFENSIVE} and session.params is not None:
            if last_price < session.params.lower or last_price > session.params.upper:
                return RiskDecision(RiskAction.DEFEND, "价格离开普通网格区间，进入防御模式。", 4)

        return RiskDecision(RiskAction.NONE, "未触发风控。", 99)

    def can_open_new_symbol(
        self,
        active_sessions: list[SymbolSession],
        new_capital: float,
        *,
        regime_allowed: bool = True,
        account_equity: float | None = None,
        window_pnl: float = 0.0,
        window_stop_count: int = 0,
        consecutive_session_losses: int = 0,
    ) -> RiskDecision:
        if not regime_allowed:
            return RiskDecision(RiskAction.BLOCK, "Regime Engine 未批准网格启动。", 1)
        if (
            self.config.max_consecutive_session_losses > 0
            and consecutive_session_losses >= self.config.max_consecutive_session_losses
        ):
            return RiskDecision(RiskAction.HALT_WINDOW, "连续亏损会话达到全局熔断上限。", 2)
        if not _is_positive_finite(new_capital):
            return RiskDecision(RiskAction.SKIP, "新标的本金配置异常。", 5)
        equity = account_equity if account_equity is not None else self.config.total_capital_limit
        if not _is_positive_finite(equity):
            return RiskDecision(RiskAction.SKIP, "账户权益基准异常。", 5)
        if (
            self.config.max_window_loss_pct > 0
            and _is_finite(window_pnl)
            and window_pnl <= -float(equity) * self.config.max_window_loss_pct
        ):
            return RiskDecision(RiskAction.HALT_WINDOW, "本窗口累计损失达到全局熔断线。", 2)
        if self.config.max_window_stop_count > 0 and window_stop_count >= self.config.max_window_stop_count:
            return RiskDecision(RiskAction.HALT_WINDOW, "本窗口止损次数达到全局熔断上限。", 2)
        if any(not _is_non_negative_finite(session.capital) for session in active_sessions):
            return RiskDecision(RiskAction.SKIP, "活跃标的资金占用异常。", 5)
        used_capital = sum(session.capital for session in active_sessions)
        if used_capital + new_capital > self.config.total_capital_limit:
            return RiskDecision(RiskAction.SKIP, "总资金占用超过上限。", 5)
        if len(active_sessions) >= self.config.max_concurrent:
            return RiskDecision(RiskAction.SKIP, "活跃标的数达到并发上限。", 6)
        return RiskDecision(RiskAction.NONE, "允许开启新标的。", 99)


def _stop_buffer_pct(lower: float, stop_loss_price: float) -> float:
    if lower <= 0:
        return 0.0
    return max(0.0, 1 - stop_loss_price / lower)


def _is_finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number)


def _is_positive_finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0


def _is_non_negative_finite(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number >= 0
