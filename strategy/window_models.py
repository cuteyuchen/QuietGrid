from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class WindowKind(str, Enum):
    REGULAR_OPEN = "REGULAR_OPEN"
    WEEKDAY_OVERNIGHT = "WEEKDAY_OVERNIGHT"
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    FORCE_CLOSE_BUFFER = "FORCE_CLOSE_BUFFER"


@dataclass(frozen=True)
class TradingWindow:
    kind: WindowKind
    allowed: bool
    window_key: str
    previous_market_close: datetime | None
    next_market_open: datetime | None
    next_premarket_open: datetime | None
    force_close_at: datetime | None
    minutes_to_force_close: float
    reason: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "allowed": self.allowed,
            "window_key": self.window_key,
            "previous_market_close": self.previous_market_close.isoformat() if self.previous_market_close else "",
            "next_market_open": self.next_market_open.isoformat() if self.next_market_open else "",
            "next_premarket_open": self.next_premarket_open.isoformat() if self.next_premarket_open else "",
            "force_close_at": self.force_close_at.isoformat() if self.force_close_at else "",
            "minutes_to_force_close": self.minutes_to_force_close,
            "reason": self.reason,
        }
