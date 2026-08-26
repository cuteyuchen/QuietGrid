from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from exchange.base import ExchangeClient


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class PublicMarketData(Protocol):
    async def get_symbols(self) -> list[dict[str, Any]]: ...
    async def get_symbol_rules(self, symbol: str) -> dict[str, Any]: ...
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]: ...
    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]: ...
    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]: ...
    async def get_funding_rate(self, symbol: str) -> float: ...


@dataclass(frozen=True)
class ShadowExecutionProfile:
    name: str
    queue_ahead_fraction: float
    participation_cap: float
    placement_latency_ms: int
    cancel_latency_ms: int
    stale_timeout_seconds: float
    require_trade_through: bool = False
    same_event_policy: str = "CONSERVATIVE_ADVERSE_FIRST"
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0005


PAPER_BASELINE = ShadowExecutionProfile("PAPER_BASELINE", 0.50, 0.25, 100, 100, 90.0)
PAPER_CONSERVATIVE = ShadowExecutionProfile("PAPER_CONSERVATIVE", 0.90, 0.10, 250, 500, 30.0, True)


@dataclass
class ShadowOrder:
    symbol: str
    order_id: str
    client_id: str
    side: str
    price: float
    qty: float
    status: str
    created_at: str
    effective_at: str
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    reject_reason: str | None = None


class ShadowBroker:
    """Persistent paper broker with deterministic, conservative maker fills."""

    def __init__(self, db_path: str | Path, profile: ShadowExecutionProfile = PAPER_BASELINE) -> None:
        self.db_path = Path(db_path)
        self.profile = profile
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_market: dict[str, dict[str, Any]] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS shadow_orders (
                order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, client_id TEXT UNIQUE NOT NULL,
                side TEXT NOT NULL, price REAL NOT NULL, qty REAL NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, effective_at TEXT NOT NULL, filled_qty REAL NOT NULL DEFAULT 0,
                avg_fill_price REAL, reject_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS shadow_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, price REAL NOT NULL, qty REAL NOT NULL, fee REAL NOT NULL,
                event_time TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shadow_positions (
                symbol TEXT PRIMARY KEY, qty REAL NOT NULL DEFAULT 0, avg_price REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS shadow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
                event_time TEXT NOT NULL, payload_json TEXT NOT NULL
            );
            """)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def _record_event(self, event_type: str, payload: dict[str, Any], at: datetime) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO shadow_events(event_type,event_time,payload_json) VALUES (?,?,?)",
                         (event_type, self._iso(at), json.dumps(payload, default=str, separators=(",", ":"))))

    async def get_account_balance(self) -> float:
        return 0.0

    async def get_account_summary(self) -> dict[str, Any]:
        positions = []
        with self._connect() as conn:
            for row in conn.execute("SELECT * FROM shadow_positions WHERE ABS(qty) > 1e-12"):
                positions.append(dict(row))
        return {"asset": "USDT", "balance": 0.0, "available_balance": 0.0, "positions": positions,
                "lane": "TRADFI_SHADOW", "production_private_api": "DISABLED"}

    async def get_position(self, symbol: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shadow_positions WHERE symbol = ?", (symbol,)).fetchone()
        return dict(row) if row else {"symbol": symbol, "qty": 0.0, "avg_price": 0.0, "realized_pnl": 0.0}

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM shadow_orders WHERE symbol = ? AND status IN ('NEW','PARTIALLY_FILLED')", (symbol,)).fetchall()
        return [self._order_response(dict(row)) for row in rows]

    async def get_order(self, symbol: str, order_id: str, client_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shadow_orders WHERE (order_id = ? OR client_id = ?) AND symbol = ?", (order_id, client_id, symbol)).fetchone()
        return self._order_response(dict(row)) if row else {"symbol": symbol, "orderId": order_id, "clientOrderId": client_id, "status": "UNKNOWN"}

    async def get_order_trades(self, symbol: str, order_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM shadow_fills WHERE symbol = ? AND order_id = ?", (symbol, order_id)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _order_response(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "orderId": row.get("order_id", ""),
            "clientOrderId": row.get("client_id", ""),
            "origQty": row.get("qty", 0.0),
            "executedQty": row.get("filled_qty", 0.0),
            "avgPrice": row.get("avg_fill_price") or 0.0,
        }

    async def place_limit_order_post_only(self, symbol: str, side: str, price: float, qty: float, client_id: str, position_side: str | None = None) -> dict[str, Any]:
        now = self._now()
        existing = await self.get_order(symbol, "", client_id)
        if str(existing.get("status", "")).upper() not in {"UNKNOWN", ""} and str(existing.get("client_id") or existing.get("clientOrderId") or "") == client_id:
            return existing
        quote = self._last_market.get(symbol, {})
        bid = float(quote.get("bid", 0) or 0)
        ask = float(quote.get("ask", 0) or 0)
        marketable = (side.upper() == "BUY" and ask > 0 and price >= ask) or (side.upper() == "SELL" and bid > 0 and price <= bid)
        order_id = f"paper-{uuid4().hex[:16]}"
        status = "REJECTED" if marketable else "NEW"
        reason = "POST_ONLY_MARKETABLE" if marketable else None
        effective = now.timestamp() + self.profile.placement_latency_ms / 1000
        order = ShadowOrder(symbol, order_id, client_id, side.upper(), float(price), float(qty), status,
                            self._iso(now), self._iso(datetime.fromtimestamp(effective, timezone.utc)), reject_reason=reason)
        with self._connect() as conn:
            conn.execute("INSERT INTO shadow_orders(order_id,symbol,client_id,side,price,qty,status,created_at,effective_at,filled_qty,avg_fill_price,reject_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (order.order_id, order.symbol, order.client_id, order.side, order.price, order.qty, order.status, order.created_at, order.effective_at, 0.0, None, reason))
        self._record_event("ORDER_REJECTED" if marketable else "ORDER_NEW", asdict(order), now)
        return {"symbol": symbol, "orderId": order_id, "clientOrderId": client_id, "status": status, "price": price, "origQty": qty, "timeInForce": "GTX", "rejectReason": reason}

    async def place_market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = True, position_side: str | None = None, client_id: str | None = None) -> dict[str, Any]:
        quote = self._last_market.get(symbol, {})
        price = float(quote.get("last", quote.get("ask", quote.get("bid", 0))) or 0)
        if price <= 0:
            raise RuntimeError("no market price available for paper market order")
        order_id = f"paper-{uuid4().hex[:16]}"
        cid = client_id or order_id
        now = self._now()
        if client_id:
            existing = await self.get_order(symbol, "", client_id)
            if str(existing.get("status", "")).upper() not in {"UNKNOWN", ""} and str(existing.get("client_id") or existing.get("clientOrderId") or "") == client_id:
                return existing
        with self._connect() as conn:
            conn.execute("INSERT INTO shadow_orders(order_id,symbol,client_id,side,price,qty,status,created_at,effective_at,filled_qty,avg_fill_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (order_id, symbol, cid, side.upper(), price, float(qty), "FILLED", self._iso(now), self._iso(now), float(qty), price))
        await self._apply_fill(order_id, symbol, side.upper(), price, float(qty), now)
        return {"symbol": symbol, "orderId": order_id, "clientOrderId": cid, "status": "FILLED", "executedQty": qty, "avgPrice": price}

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        now = self._now()
        with self._connect() as conn:
            conn.execute("UPDATE shadow_orders SET status = 'CANCELED' WHERE symbol = ? AND order_id = ? AND status IN ('NEW','PARTIALLY_FILLED')", (symbol, order_id))
            row = conn.execute("SELECT * FROM shadow_orders WHERE symbol = ? AND order_id = ?", (symbol, order_id)).fetchone()
        self._record_event("ORDER_CANCELED", {"symbol": symbol, "order_id": order_id}, now)
        return self._order_response(dict(row)) if row else {"symbol": symbol, "orderId": order_id, "status": "CANCELED"}

    async def cancel_all_orders(self, symbol: str) -> None:
        now = self._now()
        with self._connect() as conn:
            conn.execute("UPDATE shadow_orders SET status = 'CANCELED' WHERE symbol = ? AND status IN ('NEW','PARTIALLY_FILLED')", (symbol,))
        self._record_event("CANCEL_ALL", {"symbol": symbol}, now)

    async def process_market_event(self, symbol: str, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply one public book/trade event; touching a quote never fills by itself."""
        at = event.get("timestamp") or self._now()
        if isinstance(at, str):
            at = datetime.fromisoformat(at)
        self._last_market[symbol] = {**self._last_market.get(symbol, {}), **event}
        age = (self._now() - at.astimezone(timezone.utc)).total_seconds()
        if age > self.profile.stale_timeout_seconds:
            self._record_event("MARKET_DATA_STALE", {"symbol": symbol, "age": age}, self._now())
            return []
        trade_price = event.get("trade_price")
        trade_qty = float(event.get("trade_qty") or 0)
        if trade_price is None or trade_qty <= 0:
            return []
        fills: list[dict[str, Any]] = []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM shadow_orders WHERE symbol = ? AND status IN ('NEW','PARTIALLY_FILLED')", (symbol,)).fetchall()
        for row in rows:
            if datetime.fromisoformat(row["effective_at"]) > at:
                continue
            eligible = (row["side"] == "BUY" and float(trade_price) <= row["price"]) or (row["side"] == "SELL" and float(trade_price) >= row["price"])
            if self.profile.require_trade_through:
                eligible = eligible and ((row["side"] == "BUY" and float(trade_price) < row["price"]) or (row["side"] == "SELL" and float(trade_price) > row["price"]))
            if not eligible:
                continue
            available = max(0.0, trade_qty * self.profile.participation_cap - row["qty"] * self.profile.queue_ahead_fraction)
            fill_qty = min(float(row["qty"]) - float(row["filled_qty"]), available)
            if fill_qty <= 0:
                continue
            await self._apply_fill(row["order_id"], symbol, row["side"], float(trade_price), fill_qty, at)
            status = "FILLED" if float(row["filled_qty"]) + fill_qty >= float(row["qty"]) - 1e-12 else "PARTIALLY_FILLED"
            with self._connect() as conn:
                conn.execute("UPDATE shadow_orders SET filled_qty = filled_qty + ?, avg_fill_price = ?, status = ? WHERE order_id = ?", (fill_qty, float(trade_price), status, row["order_id"]))
            fills.append({"order_id": row["order_id"], "qty": fill_qty, "price": float(trade_price), "status": status})
        return fills

    async def _apply_fill(self, order_id: str, symbol: str, side: str, price: float, qty: float, at: datetime) -> None:
        signed = qty if side == "BUY" else -qty
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shadow_positions WHERE symbol = ?", (symbol,)).fetchone()
            old_qty = float(row["qty"]) if row else 0.0
            old_avg = float(row["avg_price"]) if row else 0.0
            new_qty = old_qty + signed
            realized = 0.0
            if old_qty and old_qty * signed < 0:
                realized = min(abs(old_qty), abs(signed)) * (price - old_avg) * (1 if old_qty > 0 else -1)
            new_avg = price if old_qty == 0 or old_qty * signed < 0 and abs(signed) >= abs(old_qty) else (old_avg * abs(old_qty) + price * abs(signed)) / max(abs(old_qty) + abs(signed), 1e-12)
            conn.execute("INSERT INTO shadow_positions(symbol,qty,avg_price,realized_pnl) VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,avg_price=excluded.avg_price,realized_pnl=shadow_positions.realized_pnl+excluded.realized_pnl", (symbol, new_qty, new_avg, realized))
            conn.execute("INSERT INTO shadow_fills(order_id,symbol,side,price,qty,fee,event_time) VALUES (?,?,?,?,?,?,?)", (order_id, symbol, side, price, qty, abs(price * qty) * self.profile.maker_fee_rate, self._iso(at)))

    async def force_flat(self, symbol: str) -> dict[str, Any]:
        await self.cancel_all_orders(symbol)
        pos = await self.get_position(symbol)
        qty = abs(float(pos.get("qty", 0)))
        if qty <= 1e-12:
            return {"symbol": symbol, "flattened": True, "qty": 0.0}
        side = "SELL" if float(pos["qty"]) > 0 else "BUY"
        result = await self.place_market_order(symbol, side, qty, reduce_only=True, client_id=f"qg-v40-force-flat-{symbol.lower()}")
        return {"symbol": symbol, "flattened": True, "qty": qty, "order": result}

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            orders = conn.execute("SELECT status, COUNT(*) AS count FROM shadow_orders GROUP BY status").fetchall()
            positions = conn.execute("SELECT * FROM shadow_positions").fetchall()
        return {"profile": self.profile.name, "orders": [dict(row) for row in orders], "positions": [dict(row) for row in positions], "production_private_api": "DISABLED"}


class ShadowExchangeClient(ExchangeClient):
    """Composition adapter: public market data plus the independent paper broker."""

    def __init__(self, market_data: PublicMarketData, broker: ShadowBroker) -> None:
        self.market_data = market_data
        self.broker = broker

    async def get_symbols(self): return await self.market_data.get_symbols()
    async def get_symbol_rules(self, symbol): return await self.market_data.get_symbol_rules(symbol)
    async def get_klines(self, symbol, interval, limit): return await self.market_data.get_klines(symbol, interval, limit)
    async def get_24h_ticker(self, symbol): return await self.market_data.get_24h_ticker(symbol)
    async def get_orderbook_depth(self, symbol, limit): return await self.market_data.get_orderbook_depth(symbol, limit)
    async def get_funding_rate(self, symbol): return await self.market_data.get_funding_rate(symbol)
    async def set_leverage(self, symbol, leverage):
        if int(leverage) != 1:
            raise ValueError("v4 economic leverage is frozen at 1x")
    async def set_margin_type(self, symbol, margin_type):
        if str(margin_type).upper() != "ISOLATED":
            raise ValueError("v4 shadow margin type is ISOLATED only")
    async def get_account_balance(self): return await self.broker.get_account_balance()
    async def get_account_summary(self): return await self.broker.get_account_summary()
    async def get_position(self, symbol): return await self.broker.get_position(symbol)
    async def get_open_orders(self, symbol): return await self.broker.get_open_orders(symbol)
    async def get_order(self, symbol, order_id, client_id): return await self.broker.get_order(symbol, order_id, client_id)
    async def get_order_trades(self, symbol, order_id): return await self.broker.get_order_trades(symbol, order_id)
    async def place_limit_order_post_only(self, *args, **kwargs): return await self.broker.place_limit_order_post_only(*args, **kwargs)
    async def place_market_order(self, *args, **kwargs): return await self.broker.place_market_order(*args, **kwargs)
    async def place_stop_market_order(self, symbol, side, stop_price, client_id, close_position=True):
        return await self.broker.place_limit_order_post_only(symbol, side, stop_price, 0.0, client_id)
    async def cancel_order(self, *args, **kwargs): return await self.broker.cancel_order(*args, **kwargs)
    async def cancel_all_orders(self, *args, **kwargs): return await self.broker.cancel_all_orders(*args, **kwargs)
    async def get_commission_rate(self, symbol): return {"maker": self.broker.profile.maker_fee_rate, "taker": self.broker.profile.taker_fee_rate}
