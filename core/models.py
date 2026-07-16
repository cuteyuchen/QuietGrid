from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class GridState(Enum):
    IDLE = "IDLE"
    OBSERVING = "OBSERVING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COOLDOWN = "COOLDOWN"
    CLOSING = "CLOSING"
    STOPPED = "STOPPED"


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RiskAction(Enum):
    NONE = "none"
    SKIP = "skip"
    COOLDOWN = "cooldown"
    CLOSE = "close"
    FORCE_CLOSE = "force_close"


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
