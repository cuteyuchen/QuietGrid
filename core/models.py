from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class GridState(Enum):
    IDLE = "IDLE"
    SELECTING = "SELECTING"
    OBSERVING = "OBSERVING"
    READY = "READY"
    RUNNING = "RUNNING"
    DEFENSIVE = "DEFENSIVE"
    REBALANCING = "REBALANCING"
    RECOVERING = "RECOVERING"
    PAUSED = "PAUSED"
    COOLDOWN = "COOLDOWN"
    CLOSING = "CLOSING"
    STOPPED = "STOPPED"


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class GridDirectionMode(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class OrderIntent(str, Enum):
    OPEN = "OPEN"
    REDUCE = "REDUCE"
    SEED = "SEED"
    PROTECTION = "PROTECTION"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RiskAction(Enum):
    NONE = "none"
    SKIP = "skip"
    REDUCE = "reduce"
    DEFEND = "defend"
    BLOCK = "block"
    COOLDOWN = "cooldown"
    CLOSE = "close"
    FORCE_CLOSE = "force_close"
    HALT_WINDOW = "halt_window"


@dataclass(frozen=True)
class GridParams:
    symbol: str
    upper: float
    lower: float
    center: float
    grid_num: int
    step_pct: float
    grid_prices: list[float]
    baseline_atr: float
    stop_loss_price: float
    calculated_at: datetime
    volatility_method: str = "std"
    volatility_value: float = 0.0
    volatility_window: int = 0
    upper_stop_loss_price: float | None = None
    grid_mode: str = "legacy"
    regime_score: float | None = None
    cost_floor_pct: float = 0.0
    qty_weights: tuple[float, ...] = ()
    parameter_version: str = "legacy-v1"
    economics: dict[str, Any] = field(default_factory=dict)
    direction_mode: GridDirectionMode = GridDirectionMode.NEUTRAL


@dataclass
class GridOrder:
    symbol: str
    order_id: str
    client_id: str
    grid_index: int
    side: OrderSide
    price: float
    qty: float
    status: OrderStatus
    created_at: datetime
    filled_at: datetime | None = None
    fill_price: float | None = None
    entry_price: float | None = None
    position_side: str | None = None
    order_intent: OrderIntent = OrderIntent.OPEN


@dataclass
class SymbolSession:
    session_id: int
    symbol: str
    state: GridState
    params: GridParams | None
    orders: list[GridOrder]
    realized_pnl: float
    capital: float
    leverage: int
    open_time: datetime
    kline_buffer: list[dict[str, Any]] = field(default_factory=list)
    state_entered_at: datetime | None = None
    stop_protection_sides: set[str] = field(default_factory=set)
    soft_breach_count: int = 0
    direction_mode: GridDirectionMode = GridDirectionMode.NEUTRAL
    direction_source: str = "global"
    seed_position_side: str | None = None
    seed_qty: float = 0.0
    seed_entry_price: float | None = None
    seed_slippage_pct: float | None = None
    seed_fee: float = 0.0
    last_retention_decision_at: datetime | None = None


@dataclass(frozen=True)
class TickerInfo:
    symbol: str
    last_price: float
    bid_price: float
    ask_price: float
    volume_24h: float
    bid_qty_5: float
    ask_qty_5: float
    funding_rate: float
    timestamp: datetime


@dataclass(frozen=True)
class RiskDecision:
    action: RiskAction
    reason: str
    priority: int
