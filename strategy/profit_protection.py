from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite


class ProfitProtectionAction(str, Enum):
    NONE = "NONE"
    SUPPRESS = "SUPPRESS"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class ProfitProtectionConfig:
    activation_profit_usdt: float
    enabled: bool = True
    minimum_locked_profit_ratio: float = 0.25
    suppress_drawdown_pct: float = 0.25
    reduce_drawdown_pct: float = 0.35
    close_drawdown_pct: float = 0.50
    estimated_exit_cost_rate: float = 0.0007

    def __post_init__(self) -> None:
        if not _is_non_negative_finite(self.activation_profit_usdt):
            raise ValueError("activation_profit_usdt 必须为非负有限数。")
        if not _is_ratio(self.minimum_locked_profit_ratio):
            raise ValueError("minimum_locked_profit_ratio 必须在 [0, 1] 内。")
        thresholds = (
            self.suppress_drawdown_pct,
            self.reduce_drawdown_pct,
            self.close_drawdown_pct,
        )
        if not all(_is_ratio(value) for value in thresholds):
            raise ValueError("利润回撤阈值必须在 [0, 1] 内。")
        if not (
            self.suppress_drawdown_pct
            < self.reduce_drawdown_pct
            < self.close_drawdown_pct
        ):
            raise ValueError("利润回撤阈值必须满足 suppress < reduce < close。")
        if not _is_non_negative_finite(self.estimated_exit_cost_rate):
            raise ValueError("estimated_exit_cost_rate 必须为非负有限数。")


@dataclass(frozen=True)
class ProfitProtectionSnapshot:
    session_id: int
    realized_pnl: float
    unrealized_pnl: float
    estimated_exit_cost: float
    current_net_pnl: float
    peak_net_pnl: float
    drawdown_pct: float
    activation_profit_usdt: float
    minimum_locked_profit_usdt: float
    activated: bool


@dataclass(frozen=True)
class ProfitProtectionDecision:
    action: ProfitProtectionAction
    reason: str
    snapshot: ProfitProtectionSnapshot


class ProfitProtectionTracker:
    """Track per-session net-PnL peaks and convert giveback into staged actions.

    The tracker intentionally requires an inventory snapshot from the caller. A
    realized-PnL-only peak can be materially overstated while the grid carries
    losing inventory, which was the failure mode this component is designed to
    avoid.
    """

    def __init__(self, config: ProfitProtectionConfig) -> None:
        self.config = config
        self._peak_net_pnl_by_session: dict[int, float] = {}

    def evaluate(
        self,
        session_id: int,
        *,
        realized_pnl: float,
        unrealized_pnl: float,
        gross_inventory_notional: float,
    ) -> ProfitProtectionDecision:
        realized = _finite(realized_pnl, "realized_pnl")
        unrealized = _finite(unrealized_pnl, "unrealized_pnl")
        gross_notional = _non_negative_finite(
            gross_inventory_notional,
            "gross_inventory_notional",
        )
        estimated_exit_cost = gross_notional * self.config.estimated_exit_cost_rate
        current_net_pnl = realized + unrealized - estimated_exit_cost
        previous_peak = self._peak_net_pnl_by_session.get(session_id, current_net_pnl)
        peak_net_pnl = max(previous_peak, current_net_pnl)
        self._peak_net_pnl_by_session[session_id] = peak_net_pnl

        activation = self.config.activation_profit_usdt
        minimum_locked = activation * self.config.minimum_locked_profit_ratio
        activated = bool(self.config.enabled and activation > 0 and peak_net_pnl >= activation)
        drawdown_pct = (
            max(0.0, (peak_net_pnl - current_net_pnl) / peak_net_pnl)
            if peak_net_pnl > 0
            else 0.0
        )
        snapshot = ProfitProtectionSnapshot(
            session_id=session_id,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            estimated_exit_cost=estimated_exit_cost,
            current_net_pnl=current_net_pnl,
            peak_net_pnl=peak_net_pnl,
            drawdown_pct=drawdown_pct,
            activation_profit_usdt=activation,
            minimum_locked_profit_usdt=minimum_locked,
            activated=activated,
        )
        if not activated:
            return ProfitProtectionDecision(
                ProfitProtectionAction.NONE,
                "净利润尚未达到利润保护启动线。",
                snapshot,
            )
        if current_net_pnl <= 0:
            return ProfitProtectionDecision(
                ProfitProtectionAction.NONE,
                "当前已无可锁定利润，交由止损与库存风控处理。",
                snapshot,
            )
        if current_net_pnl <= minimum_locked:
            return ProfitProtectionDecision(
                ProfitProtectionAction.CLOSE,
                "当前可锁定净利润已回落至最低保留线。",
                snapshot,
            )
        if drawdown_pct >= self.config.close_drawdown_pct:
            return ProfitProtectionDecision(
                ProfitProtectionAction.CLOSE,
                "峰值净利润回撤达到平仓止盈线。",
                snapshot,
            )
        if drawdown_pct >= self.config.reduce_drawdown_pct:
            return ProfitProtectionDecision(
                ProfitProtectionAction.REDUCE,
                "峰值净利润回撤达到减仓保护线。",
                snapshot,
            )
        if drawdown_pct >= self.config.suppress_drawdown_pct:
            return ProfitProtectionDecision(
                ProfitProtectionAction.SUPPRESS,
                "峰值净利润回撤达到停止新增库存线。",
                snapshot,
            )
        return ProfitProtectionDecision(
            ProfitProtectionAction.NONE,
            "利润保护已启动，当前回撤仍在允许范围内。",
            snapshot,
        )

    def snapshot(self, session_id: int) -> float | None:
        return self._peak_net_pnl_by_session.get(session_id)

    def forget(self, session_id: int) -> None:
        self._peak_net_pnl_by_session.pop(session_id, None)


def _finite(value: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须为有限数。") from exc
    if not isfinite(number):
        raise ValueError(f"{name} 必须为有限数。")
    return number


def _non_negative_finite(value: float, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise ValueError(f"{name} 必须为非负有限数。")
    return number


def _is_non_negative_finite(value: float) -> bool:
    try:
        return isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False


def _is_ratio(value: float) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and 0 <= number <= 1
