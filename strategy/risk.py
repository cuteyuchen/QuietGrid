from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from core.models import GridState, RiskAction, RiskDecision, SymbolSession
from core.scheduler import Scheduler


@dataclass(frozen=True)
class RiskConfig:
    take_profit_usdt: float
    total_capital_limit: float
    max_concurrent: int


class RiskManager:
    def __init__(self, scheduler: Scheduler, config: RiskConfig) -> None:
        self.scheduler = scheduler
        self.config = config

    def evaluate_symbol(self, session: SymbolSession, last_price: float, now: datetime | None = None) -> RiskDecision:
        if self.scheduler.should_force_close(now):
            return RiskDecision(RiskAction.FORCE_CLOSE, "临近盘前，触发全局强制离场。", 1)

        if not _is_positive_finite(last_price):
            return RiskDecision(RiskAction.CLOSE, "行情价格异常，强制关闭标的。", 2)

        if not _is_finite(session.realized_pnl):
            return RiskDecision(RiskAction.CLOSE, "已实现盈亏异常，强制关闭标的。", 2)

        if session.realized_pnl >= self.config.take_profit_usdt:
            return RiskDecision(RiskAction.CLOSE, "单标的止盈达标。", 2)

        if session.params is not None:
            if last_price <= session.params.stop_loss_price:
                return RiskDecision(RiskAction.CLOSE, "价格跌破动态止损线。", 3)
            upper_stop_loss_price = session.params.upper * (1 + _stop_buffer_pct(session.params.lower, session.params.stop_loss_price))
            if last_price >= upper_stop_loss_price:
                return RiskDecision(RiskAction.CLOSE, "价格突破上方动态止损线。", 3)

        if session.state == GridState.RUNNING and session.params is not None:
            if last_price < session.params.lower or last_price > session.params.upper:
                return RiskDecision(RiskAction.COOLDOWN, "价格击穿网格区间。", 4)

        return RiskDecision(RiskAction.NONE, "未触发风控。", 99)

    def can_open_new_symbol(self, active_sessions: list[SymbolSession], new_capital: float) -> RiskDecision:
        if not _is_positive_finite(new_capital):
            return RiskDecision(RiskAction.SKIP, "新标的本金配置异常。", 5)
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
