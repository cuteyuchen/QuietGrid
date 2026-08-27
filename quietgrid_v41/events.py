from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def _datetime(value: Any, fallback: datetime | None = None) -> datetime:
    if value is None:
        return fallback or datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        parsed = datetime.fromtimestamp(number / 1000 if number > 10_000_000_000 else number, timezone.utc)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class MarketEvent:
    """Canonical public market event shared by websocket, REST recovery, and paper execution."""

    source: str
    symbol: str
    event_type: str
    exchange_timestamp: datetime
    receive_timestamp: datetime
    sequence: int = 0
    event_id: str = ""
    price: float | None = None
    quantity: float | None = None
    bid: float | None = None
    ask: float | None = None
    mark_price: float | None = None
    funding_rate: float | None = None
    raw_event_id: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MarketEvent":
        payload = dict(value)
        symbol = str(payload.get("symbol") or payload.get("s") or "").upper()
        event_id = str(payload.get("event_id") or payload.get("eventId") or payload.get("id") or "")
        raw_event_id = str(payload.get("raw_event_id") or payload.get("rawEventId") or event_id)
        exchange_timestamp = _datetime(
            payload.get("exchange_timestamp", payload.get("timestamp", payload.get("E")))
        )
        receive_timestamp = _datetime(payload.get("receive_timestamp"), fallback=datetime.now(timezone.utc))
        return cls(
            source=str(payload.get("source") or "PUBLIC_WEBSOCKET"),
            symbol=symbol,
            event_type=str(payload.get("event_type") or payload.get("type") or "TRADE").upper(),
            exchange_timestamp=exchange_timestamp,
            receive_timestamp=receive_timestamp,
            sequence=int(payload.get("sequence") or payload.get("u") or 0),
            event_id=event_id,
            price=_float(payload.get("price", payload.get("trade_price", payload.get("p")))),
            quantity=_float(payload.get("quantity", payload.get("trade_qty", payload.get("q")))),
            bid=_float(payload.get("bid", payload.get("b"))),
            ask=_float(payload.get("ask", payload.get("a"))),
            mark_price=_float(payload.get("mark_price")),
            funding_rate=_float(payload.get("funding_rate")),
            raw_event_id=raw_event_id,
            payload=payload,
        )

    def broker_payload(self) -> dict[str, Any]:
        value = dict(self.payload)
        payload_hash = self.payload_hash
        value.update(
            {
                "source": self.source,
                "symbol": self.symbol,
                "event_type": self.event_type,
                "exchange_timestamp": self.exchange_timestamp,
                "receive_timestamp": self.receive_timestamp,
                "sequence": self.sequence,
                "event_id": self.event_id,
                "raw_event_id": self.raw_event_id,
                "price": self.price,
                "quantity": self.quantity,
                "trade_price": self.price,
                "trade_qty": self.quantity,
                "bid": self.bid,
                "ask": self.ask,
                "mark_price": self.mark_price,
                "funding_rate": self.funding_rate,
                "payload_hash": payload_hash,
            }
        )
        return value

    @property
    def payload_hash(self) -> str:
        encoded = json.dumps(dict(self.payload), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def as_record(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "symbol": self.symbol,
            "event_type": self.event_type,
            "exchange_timestamp": self.exchange_timestamp.isoformat(),
            "receive_timestamp": self.receive_timestamp.isoformat(),
            "sequence": self.sequence,
            "event_id": self.event_id,
            "price": self.price,
            "quantity": self.quantity,
            "bid": self.bid,
            "ask": self.ask,
            "mark_price": self.mark_price,
            "funding_rate": self.funding_rate,
            "raw_event_id": self.raw_event_id,
            "payload_hash": self.payload_hash,
            "payload": dict(self.payload),
        }


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
