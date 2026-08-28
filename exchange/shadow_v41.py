"""v4.1 persistent paper execution engine.

The module deliberately contains execution state only. Strategy selection remains in
the existing controller and frozen 31111 artifact.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from exchange.shadow import PAPER_BASELINE, ShadowExecutionProfile


EPSILON = 1e-12
ACTIVE_STATUSES = ("NEW", "PARTIALLY_FILLED", "CANCEL_PENDING", "STOP_PENDING", "STOP_TRIGGERED")
TERMINAL_STATUSES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED_NO_POSITION")


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
    order_type: str = "LIMIT"
    stop_price: float | None = None
    reduce_only: bool = False
    position_side: str | None = None
    triggered_at: str | None = None
    filled_at: str | None = None
    trigger_source: str | None = None
    working_type: str | None = None
    queue_ahead_initial: float = 0.0
    queue_ahead_remaining: float = 0.0
    requested_qty: float | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value: Any, fallback: datetime | None = None) -> datetime:
    if value is None:
        return fallback or _now()
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        number = float(value)
        parsed = datetime.fromtimestamp(number / 1000 if number > 10_000_000_000 else number, timezone.utc)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _positive(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > EPSILON else 0.0


class _Connection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


class ShadowBroker:
    """SQLite-backed paper broker with explicit stop, queue, and recovery semantics."""

    def __init__(self, db_path: str | Path, profile: ShadowExecutionProfile = PAPER_BASELINE, initial_cash: float = 10_000.0) -> None:
        self.db_path = Path(db_path)
        self.profile = profile
        self.initial_cash = float(initial_cash)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_market: dict[str, dict[str, Any]] = {}
        self._init_db()
        self._load_market_state()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, factory=_Connection)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS shadow_orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    client_id TEXT UNIQUE NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL DEFAULT 0,
                    qty REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    filled_qty REAL NOT NULL DEFAULT 0,
                    avg_fill_price REAL,
                    reject_reason TEXT,
                    cancel_requested_at TEXT,
                    cancel_effective_at TEXT,
                    order_type TEXT NOT NULL DEFAULT 'LIMIT',
                    stop_price REAL,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    position_side TEXT,
                    triggered_at TEXT,
                    filled_at TEXT,
                    trigger_source TEXT,
                    working_type TEXT,
                    queue_ahead_initial REAL NOT NULL DEFAULT 0,
                    queue_ahead_remaining REAL NOT NULL DEFAULT 0,
                    requested_qty REAL
                );
                CREATE TABLE IF NOT EXISTS shadow_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fill_key TEXT UNIQUE,
                    order_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    qty REAL NOT NULL,
                    fee REAL NOT NULL,
                    liquidity TEXT NOT NULL DEFAULT 'MAKER',
                    event_time TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_positions (
                    symbol TEXT PRIMARY KEY,
                    qty REAL NOT NULL DEFAULT 0,
                    avg_price REAL NOT NULL DEFAULT 0,
                    realized_pnl REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS shadow_account_state (
                    account_id INTEGER PRIMARY KEY CHECK (account_id = 1),
                    cash REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_market_events (
                    event_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_market_state (
                    symbol TEXT PRIMARY KEY,
                    last_event_at TEXT NOT NULL,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    payload_json TEXT
                );
                CREATE TABLE IF NOT EXISTS shadow_market_freshness (
                    symbol TEXT NOT NULL,
                    stream_type TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    exchange_at TEXT NOT NULL,
                    receive_at TEXT NOT NULL,
                    PRIMARY KEY(symbol, stream_type)
                );
                CREATE TABLE IF NOT EXISTS shadow_funding_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    settlement_timestamp TEXT NOT NULL,
                    position_qty REAL NOT NULL,
                    funding_rate REAL NOT NULL,
                    funding_pnl REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(symbol, settlement_timestamp)
                );
                CREATE TABLE IF NOT EXISTS shadow_runtime_state (
                    state_key TEXT PRIMARY KEY,
                    state_value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS shadow_force_flat (
                    symbol TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    force_flat_latch INTEGER NOT NULL DEFAULT 1,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    sequence INTEGER NOT NULL DEFAULT 0,
                    requested_at TEXT NOT NULL,
                    completed_at TEXT,
                    failure TEXT,
                    last_result_json TEXT
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO shadow_account_state(account_id,cash,updated_at) VALUES (1,?,?)",
                (self.initial_cash, _iso(_now())),
            )
            order_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(shadow_orders)")}
            for name, definition in {
                "cancel_requested_at": "TEXT",
                "cancel_effective_at": "TEXT",
                "order_type": "TEXT NOT NULL DEFAULT 'LIMIT'",
                "stop_price": "REAL",
                "reduce_only": "INTEGER NOT NULL DEFAULT 0",
                "position_side": "TEXT",
                "triggered_at": "TEXT",
                "filled_at": "TEXT",
                "trigger_source": "TEXT",
                "working_type": "TEXT",
                "queue_ahead_initial": "REAL NOT NULL DEFAULT 0",
                "queue_ahead_remaining": "REAL NOT NULL DEFAULT 0",
                "requested_qty": "REAL",
            }.items():
                if name not in order_columns:
                    conn.execute(f"ALTER TABLE shadow_orders ADD COLUMN {name} {definition}")
            fill_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(shadow_fills)")}
            if "fill_key" not in fill_columns:
                conn.execute("ALTER TABLE shadow_fills ADD COLUMN fill_key TEXT")
            if "liquidity" not in fill_columns:
                conn.execute("ALTER TABLE shadow_fills ADD COLUMN liquidity TEXT NOT NULL DEFAULT 'MAKER'")
            market_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(shadow_market_state)")}
            if "sequence" not in market_columns:
                conn.execute("ALTER TABLE shadow_market_state ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0")
            if "payload_json" not in market_columns:
                conn.execute("ALTER TABLE shadow_market_state ADD COLUMN payload_json TEXT")
            force_flat_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(shadow_force_flat)")}
            if "max_attempts" not in force_flat_columns:
                conn.execute("ALTER TABLE shadow_force_flat ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3")

    def _load_market_state(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT symbol,payload_json FROM shadow_market_state WHERE payload_json IS NOT NULL").fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                self._last_market[str(row["symbol"])] = payload
        with self._connect() as conn:
            rows = conn.execute("SELECT symbol,stream_type,event_key,exchange_at,receive_at FROM shadow_market_freshness").fetchall()
        for row in rows:
            state = self._last_market.setdefault(str(row["symbol"]), {})
            state[f"_{str(row['stream_type']).lower()}_at"] = _dt(row["exchange_at"])
            state[f"_{str(row['stream_type']).lower()}_received_at"] = _dt(row["receive_at"])
            state[f"_{str(row['stream_type']).lower()}_event_key"] = row["event_key"]

    def _record(self, event_type: str, payload: dict[str, Any], at: datetime | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO shadow_events(event_type,event_time,payload_json) VALUES (?,?,?)",
                (event_type, _iso(at or _now()), _json(payload)),
            )

    def _latch(self, symbol: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT force_flat_latch FROM shadow_force_flat WHERE symbol=? AND status IN ('FORCE_FLAT_REQUESTED','WAIT_CANCEL_TERMINAL','WAIT_FLATTEN','FAIL_FORCE_FLAT')",
                (symbol.upper(),),
            ).fetchone()
        return bool(row and row["force_flat_latch"])

    def _market_is_stale(self, symbol: str, *, book: bool = False) -> bool:
        quote = self._last_market.get(symbol.upper(), {})
        if quote.get("_stale"):
            return True
        received = quote.get("_book_received_at" if book else "_trade_received_at", quote.get("receive_timestamp"))
        if received is None:
            return False
        return (_now() - _dt(received)).total_seconds() > self.profile.stale_timeout_seconds

    def _book_is_fresh(self, symbol: str) -> bool:
        quote = self._last_market.get(symbol.upper(), {})
        if not (_positive(quote.get("bid")) and _positive(quote.get("ask"))):
            return False
        received = quote.get("_book_received_at", quote.get("receive_timestamp"))
        if received is not None:
            return (_now() - _dt(received)).total_seconds() <= self.profile.stale_timeout_seconds
        # Legacy fixtures and explicit REST bootstrap quotes carry a valid top of book
        # but no stream timestamp.
        return True

    def _insert_order(self, order: ShadowOrder) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO shadow_orders(
                    order_id,symbol,client_id,side,price,qty,status,created_at,effective_at,
                    filled_qty,avg_fill_price,reject_reason,cancel_requested_at,cancel_effective_at,
                    order_type,stop_price,reduce_only,position_side,triggered_at,filled_at,
                    trigger_source,working_type,queue_ahead_initial,queue_ahead_remaining,requested_qty
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order.order_id, order.symbol, order.client_id, order.side, order.price, order.qty,
                    order.status, order.created_at, order.effective_at, order.filled_qty,
                    order.avg_fill_price, order.reject_reason, None, None, order.order_type,
                    order.stop_price, int(order.reduce_only), order.position_side, order.triggered_at,
                    order.filled_at, order.trigger_source, order.working_type,
                    order.queue_ahead_initial, order.queue_ahead_remaining,
                    order.requested_qty if order.requested_qty is not None else order.qty,
                ),
            )

    @staticmethod
    def _response(row: dict[str, Any]) -> dict[str, Any]:
        requested = row.get("requested_qty") if row.get("requested_qty") is not None else row.get("qty", 0.0)
        return {
            **row,
            "orderId": row.get("order_id", ""),
            "clientOrderId": row.get("client_id", ""),
            "origQty": requested,
            "requestedQty": requested,
            "executedQty": row.get("filled_qty", 0.0),
            "avgPrice": row.get("avg_fill_price") or 0.0,
            "rejectReason": row.get("reject_reason"),
            "orderType": row.get("order_type", "LIMIT"),
            "stopPrice": row.get("stop_price"),
            "reduceOnly": bool(row.get("reduce_only", 0)),
            "queueAheadInitial": row.get("queue_ahead_initial", 0.0),
            "queueAheadRemaining": row.get("queue_ahead_remaining", 0.0),
            "triggeredAt": row.get("triggered_at"),
            "filledAt": row.get("filled_at"),
            "triggerSource": row.get("trigger_source"),
            "workingType": row.get("working_type"),
        }

    async def get_account_balance(self) -> float:
        with self._connect() as conn:
            row = conn.execute("SELECT cash FROM shadow_account_state WHERE account_id=1").fetchone()
        return float(row["cash"] if row else self.initial_cash)

    async def get_account_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            positions = [dict(row) for row in conn.execute("SELECT * FROM shadow_positions WHERE ABS(qty)>?", (EPSILON,))]
            funding = float(conn.execute("SELECT COALESCE(SUM(funding_pnl),0) FROM shadow_funding_settlements").fetchone()[0])
            cash = float(conn.execute("SELECT cash FROM shadow_account_state WHERE account_id=1").fetchone()[0])
        return {"asset": "USDT", "balance": cash, "available_balance": cash, "positions": positions, "funding_pnl": funding, "lane": "TRADFI_SHADOW", "production_private_api": "DISABLED"}

    async def get_position(self, symbol: str) -> dict[str, Any]:
        normalized = symbol.upper()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shadow_positions WHERE symbol=?", (normalized,)).fetchone()
        return dict(row) if row else {"symbol": normalized, "qty": 0.0, "avg_price": 0.0, "realized_pnl": 0.0}

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM shadow_orders WHERE symbol=? AND status IN ({marks}) ORDER BY created_at,order_id",
                (symbol.upper(), *ACTIVE_STATUSES),
            ).fetchall()
        return [self._response(dict(row)) for row in rows]

    async def get_order(self, symbol: str, order_id: str, client_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM shadow_orders WHERE symbol=? AND (order_id=? OR client_id=?) LIMIT 1",
                (symbol.upper(), order_id, client_id),
            ).fetchone()
        return self._response(dict(row)) if row else {"symbol": symbol.upper(), "orderId": order_id, "clientOrderId": client_id, "status": "UNKNOWN"}

    async def get_order_trades(self, symbol: str, order_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM shadow_fills WHERE symbol=? AND order_id=? ORDER BY id", (symbol.upper(), order_id)).fetchall()
        return [dict(row) for row in rows]

    async def place_limit_order_post_only(self, symbol: str, side: str, price: float, qty: float, client_id: str, position_side: str | None = None) -> dict[str, Any]:
        normalized, normalized_side = symbol.upper(), side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid paper order side: {side}")
        if not client_id:
            raise ValueError("paper limit order requires client_id")
        existing = await self.get_order(normalized, "", client_id)
        if existing.get("status") not in {None, "", "UNKNOWN"}:
            return existing
        quantity, limit_price = _positive(qty), _positive(price)
        now = _now()
        reason = None
        if quantity <= EPSILON or limit_price <= EPSILON:
            reason = "INVALID_LIMIT_ORDER"
        elif self._latch(normalized) and not await self._would_reduce(normalized, normalized_side):
            reason = "FORCE_FLAT_LATCHED"
        quote = self._last_market.get(normalized, {})
        if not self._book_is_fresh(normalized) and not await self._would_reduce(normalized, normalized_side):
            reason = "BOOK_DATA_STALE"
        bid, ask = _positive(quote.get("bid")), _positive(quote.get("ask"))
        marketable = (normalized_side == "BUY" and ask and limit_price >= ask) or (normalized_side == "SELL" and bid and limit_price <= bid)
        if marketable:
            reason = "POST_ONLY_MARKETABLE"
        status = "REJECTED" if reason else "NEW"
        order_id = f"paper-{uuid4().hex[:16]}"
        effective = datetime.fromtimestamp(now.timestamp() + self.profile.placement_latency_ms / 1000, timezone.utc)
        queue_key = "bid_qty" if normalized_side == "BUY" else "ask_qty"
        displayed_queue = float(quote.get("queue_ahead", quote.get(queue_key, quote.get("bidQty" if normalized_side == "BUY" else "askQty", 0))) or 0)
        # Older v4 paper fixtures have no displayed size; preserve their conservative
        # order-relative queue while persisting the same cumulative field.
        queue_base = displayed_queue if displayed_queue > EPSILON else quantity
        queue_initial = max(0.0, queue_base * max(0.0, float(self.profile.queue_ahead_fraction)))
        order = ShadowOrder(normalized, order_id, client_id, normalized_side, limit_price, quantity, status, _iso(now), _iso(effective), reject_reason=reason, position_side=position_side, queue_ahead_initial=queue_initial, queue_ahead_remaining=queue_initial)
        self._insert_order(order)
        self._record("ORDER_REJECTED" if reason else "ORDER_NEW", asdict(order), now)
        return self._response(asdict(order)) | {"timeInForce": "GTX"}

    async def place_stop_market_order(self, symbol: str, side: str, stop_price: float, client_id: str, close_position: bool = True, quantity: float | None = None, position_side: str | None = None, working_type: str = "CONTRACT_PRICE") -> dict[str, Any]:
        normalized, normalized_side = symbol.upper(), side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid paper stop side: {side}")
        if not client_id:
            raise ValueError("paper stop order requires client_id")
        existing = await self.get_order(normalized, "", client_id)
        if existing.get("status") not in {None, "", "UNKNOWN"}:
            return existing
        stop = _positive(stop_price)
        requested = _positive(quantity) if quantity is not None else 0.0
        status = "STOP_PENDING" if stop > EPSILON else "REJECTED"
        now = _now()
        order = ShadowOrder(normalized, f"paper-{uuid4().hex[:16]}", client_id, normalized_side, 0.0, requested, status, _iso(now), _iso(now), reject_reason=None if status == "STOP_PENDING" else "INVALID_STOP_PRICE", order_type="STOP_MARKET", stop_price=stop, reduce_only=True, position_side=position_side, working_type=str(working_type or "CONTRACT_PRICE").upper())
        self._insert_order(order)
        self._record("STOP_PENDING" if status == "STOP_PENDING" else "ORDER_REJECTED", asdict(order), now)
        return self._response(asdict(order))

    async def place_market_order(self, symbol: str, side: str, qty: float, reduce_only: bool | None = None, position_side: str | None = None, client_id: str | None = None) -> dict[str, Any]:
        normalized, normalized_side = symbol.upper(), side.upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError(f"invalid paper market side: {side}")
        requested = _positive(qty)
        cid = client_id or f"paper-market-{uuid4().hex[:16]}"
        if client_id:
            existing = await self.get_order(normalized, "", client_id)
            if existing.get("status") not in {None, "", "UNKNOWN"}:
                return existing
        is_reduce_only = bool(reduce_only) if reduce_only is not None else False
        now = _now()
        if self._latch(normalized) and not is_reduce_only:
            order = ShadowOrder(normalized, f"paper-{uuid4().hex[:16]}", cid, normalized_side, 0.0, 0.0, "REJECTED", _iso(now), _iso(now), reject_reason="FORCE_FLAT_LATCHED", order_type="MARKET", requested_qty=requested)
            self._insert_order(order)
            self._record("ORDER_REJECTED", asdict(order), now)
            return self._response(asdict(order))
        quote = self._last_market.get(normalized, {})
        if self._market_is_stale(normalized):
            raise RuntimeError("market data is stale; paper market execution is blocked")
        price = _positive(quote.get("last", quote.get("price", quote.get("trade_price", quote.get("ask", quote.get("bid", 0))))))
        if price <= EPSILON:
            raise RuntimeError("no market price available for paper market order")
        position = await self.get_position(normalized)
        current = float(position.get("qty", 0.0) or 0.0)
        executable = requested
        if is_reduce_only:
            executable = min(requested, current) if normalized_side == "SELL" and current > EPSILON else min(requested, abs(current)) if normalized_side == "BUY" and current < -EPSILON else 0.0
        if is_reduce_only and executable <= EPSILON:
            order = ShadowOrder(normalized, f"paper-{uuid4().hex[:16]}", cid, normalized_side, price, 0.0, "REJECTED", _iso(now), _iso(now), reject_reason="NO_REDUCIBLE_POSITION", order_type="MARKET", reduce_only=True, position_side=position_side, requested_qty=requested)
            self._insert_order(order)
            self._record("ORDER_REJECTED", asdict(order), now)
            return self._response(asdict(order))
        order_id = f"paper-{uuid4().hex[:16]}"
        self._insert_order(ShadowOrder(normalized, order_id, cid, normalized_side, price, executable, "NEW", _iso(now), _iso(now), order_type="MARKET", reduce_only=is_reduce_only, position_side=position_side, requested_qty=requested))
        filled = await self._apply_fill(order_id, normalized, normalized_side, price, executable, now, reduce_only=is_reduce_only, liquidity="TAKER")
        with self._connect() as conn:
            conn.execute("UPDATE shadow_orders SET filled_qty=?,avg_fill_price=?,status='FILLED',filled_at=? WHERE order_id=?", (filled, price if filled > EPSILON else None, _iso(now), order_id))
        self._record("MARKET_FILLED", {"order_id": order_id, "symbol": normalized, "qty": filled, "price": price, "reduce_only": is_reduce_only}, now)
        return await self.get_order(normalized, order_id, cid)

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        normalized, now = symbol.upper(), _now()
        effective = datetime.fromtimestamp(now.timestamp() + self.profile.cancel_latency_ms / 1000, timezone.utc)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shadow_orders WHERE symbol=? AND order_id=?", (normalized, order_id)).fetchone()
            if row is None:
                return {"symbol": normalized, "orderId": order_id, "status": "CANCELED"}
            if row["status"] in TERMINAL_STATUSES:
                return self._response(dict(row))
            if row["status"] == "STOP_TRIGGERED":
                return self._response(dict(row))
            conn.execute("UPDATE shadow_orders SET status='CANCEL_PENDING',cancel_requested_at=?,cancel_effective_at=? WHERE symbol=? AND order_id=?", (_iso(now), _iso(effective), normalized, order_id))
            row = conn.execute("SELECT * FROM shadow_orders WHERE symbol=? AND order_id=?", (normalized, order_id)).fetchone()
        self._record("CANCEL_PENDING", {"symbol": normalized, "order_id": order_id, "effective_at": _iso(effective)}, now)
        return self._response(dict(row))

    async def cancel_all_orders(self, symbol: str) -> None:
        normalized, now = symbol.upper(), _now()
        effective = datetime.fromtimestamp(now.timestamp() + self.profile.cancel_latency_ms / 1000, timezone.utc)
        statuses = tuple(status for status in ACTIVE_STATUSES if status != "STOP_TRIGGERED")
        marks = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            conn.execute(f"UPDATE shadow_orders SET status='CANCEL_PENDING',cancel_requested_at=?,cancel_effective_at=? WHERE symbol=? AND status IN ({marks})", (_iso(now), _iso(effective), normalized, *statuses))
        self._record("CANCEL_ALL", {"symbol": normalized, "effective_at": _iso(effective)}, now)

    async def _would_reduce(self, symbol: str, side: str) -> bool:
        qty = float((await self.get_position(symbol)).get("qty", 0.0) or 0.0)
        return (side == "SELL" and qty > EPSILON) or (side == "BUY" and qty < -EPSILON)

    def _market_value(self, event: dict[str, Any], working_type: str) -> tuple[float, str]:
        keys = (("mark_price", "mark_price"), ("price", "price"), ("trade_price", "trade_price")) if str(working_type or "").upper() == "MARK_PRICE" else (("trade_price", "trade_price"), ("price", "price"), ("last", "last"), ("mark_price", "mark_price"))
        for key, source in keys:
            value = _positive(event.get(key))
            if value > EPSILON:
                return value, source
        return 0.0, ""

    def _settle_cancel(self, order_id: str, at: datetime) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM shadow_orders WHERE order_id=?", (order_id,)).fetchone()
            if row and row["status"] in {"NEW", "PARTIALLY_FILLED", "CANCEL_PENDING", "STOP_PENDING"}:
                conn.execute("UPDATE shadow_orders SET status='CANCELED' WHERE order_id=?", (order_id,))
        self._record("CANCELED", {"order_id": order_id}, at)

    def _settle_all_cancels(self, symbol: str, at: datetime) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT order_id FROM shadow_orders WHERE symbol=? AND status='CANCEL_PENDING'", (symbol.upper(),)).fetchall()
        for row in rows:
            self._settle_cancel(row["order_id"], at)

    def _stop_should_trigger(self, row: sqlite3.Row, event: dict[str, Any]) -> bool:
        value, _source = self._market_value(event, row["working_type"])
        stop = _positive(row["stop_price"])
        return value > EPSILON and stop > EPSILON and ((row["side"] == "SELL" and value <= stop) or (row["side"] == "BUY" and value >= stop))

    async def _trigger_stop(self, row: sqlite3.Row, event: dict[str, Any], at: datetime) -> dict[str, Any] | None:
        value, source = self._market_value(event, row["working_type"])
        if value <= EPSILON:
            return None
        with self._connect() as conn:
            current = conn.execute("SELECT * FROM shadow_orders WHERE order_id=?", (row["order_id"],)).fetchone()
            if current is None or current["status"] not in {"STOP_PENDING", "CANCEL_PENDING"}:
                return None
            conn.execute("UPDATE shadow_orders SET status='STOP_TRIGGERED',triggered_at=?,trigger_source=? WHERE order_id=?", (_iso(at), source, row["order_id"]))
        self._record("STOP_TRIGGERED", {"order_id": row["order_id"], "symbol": row["symbol"], "trigger_price": value, "trigger_source": source}, at)
        position = await self.get_position(row["symbol"])
        current_qty = float(position.get("qty", 0.0) or 0.0)
        if abs(current_qty) <= EPSILON:
            with self._connect() as conn:
                conn.execute("UPDATE shadow_orders SET status='EXPIRED_NO_POSITION',triggered_at=? WHERE order_id=?", (_iso(at), row["order_id"]))
            self._record("STOP_EXPIRED_NO_POSITION", {"order_id": row["order_id"], "symbol": row["symbol"], "trigger_price": value}, at)
            return {"order_id": row["order_id"], "qty": 0.0, "price": value, "status": "EXPIRED_NO_POSITION", "trigger_source": source}
        requested = _positive(row["qty"])
        executable = min(requested or abs(current_qty), current_qty) if row["side"] == "SELL" and current_qty > EPSILON else min(requested or abs(current_qty), abs(current_qty)) if row["side"] == "BUY" and current_qty < -EPSILON else 0.0
        filled = await self._apply_fill(row["order_id"], row["symbol"], row["side"], value, executable, at, reduce_only=True, liquidity="TAKER") if executable > EPSILON else 0.0
        with self._connect() as conn:
            conn.execute("UPDATE shadow_orders SET filled_qty=?,avg_fill_price=?,status='FILLED',filled_at=? WHERE order_id=?", (filled, value if filled > EPSILON else None, _iso(at), row["order_id"]))
        self._record("STOP_FILLED", {"order_id": row["order_id"], "symbol": row["symbol"], "qty": filled, "price": value}, at)
        return {"order_id": row["order_id"], "qty": filled, "price": value, "status": "FILLED", "trigger_source": source}

    async def process_market_event(self, symbol: str, event: dict[str, Any]) -> list[dict[str, Any]]:
        normalized = symbol.upper()
        at = _dt(event.get("exchange_timestamp", event.get("timestamp")))
        received = _dt(event.get("receive_timestamp"), fallback=_now())
        stream_type = str(event.get("event_type") or "TRADE").upper()
        source = str(event.get("source") or "UNKNOWN").upper()
        raw_key = str(event.get("raw_event_id") or event.get("event_id") or "")
        if not raw_key:
            raw_key = f"EVENT_ID_FALLBACK_HASH:{hashlib.sha256(_json({'symbol': normalized, **event}).encode('utf-8')).hexdigest()}"
            event = {**event, "raw_event_id": raw_key, "event_id_mode": "EVENT_ID_FALLBACK_HASH"}
        key = f"{source}:{normalized}:{stream_type}:{raw_key}"
        sequence = event.get("sequence")
        sequence_number = int(sequence) if sequence is not None else 0
        duplicate = False
        out_of_order = False
        gap_detected = False
        with self._connect() as conn:
            try:
                conn.execute("INSERT INTO shadow_market_events(event_key,symbol,event_time,payload_json) VALUES (?,?,?,?)", (key, normalized, _iso(at), _json(event)))
            except sqlite3.IntegrityError:
                duplicate = True
            previous = conn.execute("SELECT last_event_at,sequence FROM shadow_market_state WHERE symbol=?", (normalized,)).fetchone()
            if not duplicate and previous is not None:
                previous_at = _dt(previous["last_event_at"])
                previous_sequence = int(previous["sequence"] or 0)
                if sequence_number and previous_sequence:
                    out_of_order = sequence_number < previous_sequence
                    gap_detected = sequence_number > previous_sequence + 1
                else:
                    out_of_order = at < previous_at
            if not duplicate and not out_of_order:
                conn.execute("INSERT INTO shadow_market_state(symbol,last_event_at,sequence,payload_json) VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET last_event_at=excluded.last_event_at,sequence=excluded.sequence,payload_json=excluded.payload_json", (normalized, _iso(at), sequence_number, _json(event)))
                conn.execute(
                    "INSERT INTO shadow_market_freshness(symbol,stream_type,event_key,exchange_at,receive_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(symbol,stream_type) DO UPDATE SET event_key=excluded.event_key,exchange_at=excluded.exchange_at,receive_at=excluded.receive_at",
                    (normalized, stream_type, key, _iso(at), _iso(received)),
                )
        if duplicate:
            self._record("DUPLICATE_MARKET_EVENT", {"symbol": normalized, "event_key": key}, received)
            return []
        if out_of_order:
            self._record("OUT_OF_ORDER_MARKET_EVENT", {"symbol": normalized, "event_key": key, "timestamp": _iso(at)}, received)
            return []
        if gap_detected:
            self._record("MARKET_EVENT_GAP", {"symbol": normalized, "event_key": key, "previous_sequence": previous_sequence, "sequence": sequence}, received)
        state = dict(self._last_market.get(normalized, {}))
        if stream_type == "TRADE":
            for name in ("price", "quantity", "trade_price", "trade_qty", "last"):
                state.pop(name, None)
            state.update({name: event[name] for name in ("price", "quantity", "trade_price", "trade_qty") if name in event})
            state["last"] = _positive(event.get("trade_price", event.get("price")))
            state["_trade_at"] = at
            state["_trade_received_at"] = received
        elif stream_type == "BOOK_TICKER":
            for name in ("bid", "ask", "bid_qty", "ask_qty", "bidQty", "askQty"):
                state.pop(name, None)
            state.update({name: event[name] for name in ("bid", "ask", "bid_qty", "ask_qty") if name in event})
            state["_book_at"] = at
            state["_book_received_at"] = received
        elif stream_type in {"MARK_PRICE", "FUNDING"}:
            state.update({name: event[name] for name in ("mark_price", "funding_rate") if name in event})
            state[f"_{stream_type.lower()}_at"] = at
            state[f"_{stream_type.lower()}_received_at"] = received
        else:
            state.update(event)
        state["_event_type"] = stream_type
        age = max(0.0, (received - at).total_seconds())
        state["_stale"] = max(0.0, (received - at).total_seconds()) > self.profile.stale_timeout_seconds
        self._last_market[normalized] = state
        if age > self.profile.stale_timeout_seconds:
            await self._advance_force_flat(normalized, at, received)
            self._record("MARKET_DATA_STALE", {"symbol": normalized, "age": age}, received)
            return []
        with self._connect() as conn:
            conn.execute(
                "UPDATE shadow_market_state SET payload_json=? WHERE symbol=?",
                (_json(state), normalized),
            )
        await self._advance_force_flat(normalized, at, received)
        fills: list[dict[str, Any]] = []
        # Equal-time cancellation/trigger events use adverse-first ordering.
        with self._connect() as conn:
            stops = conn.execute("SELECT * FROM shadow_orders WHERE symbol=? AND order_type='STOP_MARKET' AND status IN ('STOP_PENDING','CANCEL_PENDING') ORDER BY created_at,order_id", (normalized,)).fetchall()
        for row in stops:
            cancel_at = _dt(row["cancel_effective_at"]) if row["cancel_effective_at"] else None
            if cancel_at is not None and cancel_at < at:
                self._settle_cancel(row["order_id"], at)
                continue
            if self._stop_should_trigger(row, event):
                result = await self._trigger_stop(row, event, at)
                if result:
                    fills.append(result)
            elif cancel_at is not None and cancel_at <= at:
                self._settle_cancel(row["order_id"], at)
        if stream_type != "TRADE":
            return fills
        trade_price = _positive(event.get("trade_price", event.get("price")))
        trade_qty = _positive(event.get("trade_qty", event.get("quantity")))
        if trade_price <= EPSILON or trade_qty <= EPSILON:
            return fills
        with self._connect() as conn:
            orders = conn.execute("SELECT * FROM shadow_orders WHERE symbol=? AND order_type='LIMIT' AND status IN ('NEW','PARTIALLY_FILLED','CANCEL_PENDING') ORDER BY created_at,order_id", (normalized,)).fetchall()
        for row in orders:
            if _dt(row["effective_at"]) > at:
                continue
            cancel_at = _dt(row["cancel_effective_at"]) if row["cancel_effective_at"] else None
            if cancel_at is not None and cancel_at < at:
                self._settle_cancel(row["order_id"], at)
                continue
            side = str(row["side"]).upper()
            eligible = (side == "BUY" and trade_price <= float(row["price"])) or (side == "SELL" and trade_price >= float(row["price"]))
            if self.profile.require_trade_through:
                eligible = eligible and ((side == "BUY" and trade_price < float(row["price"])) or (side == "SELL" and trade_price > float(row["price"])))
            queue_before = max(0.0, float(row["queue_ahead_remaining"] or 0.0))
            queue_after = max(0.0, queue_before - min(queue_before, trade_qty)) if eligible else queue_before
            eligible_flow = max(0.0, trade_qty - queue_before) if eligible else 0.0
            available = eligible_flow * max(0.0, min(1.0, float(self.profile.participation_cap)))
            remaining = max(0.0, float(row["qty"]) - float(row["filled_qty"]))
            fill_qty = min(remaining, available)
            with self._connect() as conn:
                conn.execute("UPDATE shadow_orders SET queue_ahead_remaining=? WHERE order_id=?", (queue_after, row["order_id"]))
            if fill_qty > EPSILON:
                filled = await self._apply_fill(row["order_id"], normalized, side, float(row["price"]), fill_qty, at, reduce_only=bool(row["reduce_only"]), liquidity="MAKER")
                with self._connect() as conn:
                    current = conn.execute("SELECT filled_qty FROM shadow_orders WHERE order_id=?", (row["order_id"],)).fetchone()
                    total = float(current["filled_qty"] if current else 0.0) + filled
                    status = "FILLED" if total >= float(row["qty"]) - EPSILON else "PARTIALLY_FILLED"
                    conn.execute("UPDATE shadow_orders SET filled_qty=?,avg_fill_price=?,status=?,filled_at=? WHERE order_id=?", (total, float(row["price"]), status, _iso(at) if status == "FILLED" else None, row["order_id"]))
                fills.append({"order_id": row["order_id"], "qty": filled, "price": float(row["price"]), "status": status, "queue_ahead_remaining": queue_after})
            if cancel_at is not None and cancel_at <= at:
                with self._connect() as conn:
                    state = conn.execute("SELECT status FROM shadow_orders WHERE order_id=?", (row["order_id"],)).fetchone()
                if state and state["status"] in {"NEW", "PARTIALLY_FILLED", "CANCEL_PENDING"}:
                    self._settle_cancel(row["order_id"], at)
        return fills

    async def _apply_fill(self, order_id: str, symbol: str, side: str, price: float, qty: float, at: datetime, *, reduce_only: bool = False, liquidity: str = "MAKER") -> float:
        requested = _positive(qty)
        if requested <= EPSILON:
            return 0.0
        normalized, normalized_side = symbol.upper(), side.upper()
        fill_key = f"{order_id}:{_iso(at)}:{normalized_side}:{float(price):.12g}:{requested:.12g}"
        with self._connect() as conn:
            if conn.execute("SELECT id FROM shadow_fills WHERE fill_key=?", (fill_key,)).fetchone() is not None:
                return 0.0
            row = conn.execute("SELECT * FROM shadow_positions WHERE symbol=?", (normalized,)).fetchone()
            old_qty = float(row["qty"] if row else 0.0)
            old_avg = float(row["avg_price"] if row else 0.0)
            actual = requested
            signed = actual if normalized_side == "BUY" else -actual
            if reduce_only:
                if (normalized_side == "SELL" and old_qty <= EPSILON) or (normalized_side == "BUY" and old_qty >= -EPSILON):
                    return 0.0
                actual = min(requested, abs(old_qty))
                signed = actual if normalized_side == "BUY" else -actual
            if actual <= EPSILON:
                return 0.0
            new_qty = old_qty + signed
            opposite = old_qty != 0 and old_qty * signed < 0
            close_qty = min(abs(old_qty), abs(signed)) if opposite else 0.0
            realized = close_qty * (float(price) - old_avg) * (1 if old_qty > 0 else -1) if opposite else 0.0
            if abs(new_qty) <= EPSILON:
                new_qty = 0.0
            if old_qty == 0 or (opposite and abs(signed) >= abs(old_qty)):
                new_avg = float(price) if abs(new_qty) > EPSILON else 0.0
            elif opposite:
                new_avg = old_avg
            else:
                new_avg = (old_avg * abs(old_qty) + float(price) * abs(signed)) / max(abs(old_qty) + abs(signed), EPSILON)
            conn.execute("INSERT INTO shadow_positions(symbol,qty,avg_price,realized_pnl) VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty,avg_price=excluded.avg_price,realized_pnl=shadow_positions.realized_pnl+excluded.realized_pnl", (normalized, new_qty, new_avg, realized))
            fee_rate = self.profile.taker_fee_rate if liquidity == "TAKER" else self.profile.maker_fee_rate
            fee = abs(float(price) * actual) * fee_rate
            conn.execute("INSERT INTO shadow_fills(fill_key,order_id,symbol,side,price,qty,fee,liquidity,event_time) VALUES (?,?,?,?,?,?,?,?,?)", (fill_key, order_id, normalized, normalized_side, float(price), actual, fee, liquidity, _iso(at)))
            signed_cash_flow = actual if normalized_side == "BUY" else -actual
            conn.execute("UPDATE shadow_account_state SET cash=cash-?-?,updated_at=? WHERE account_id=1", (signed_cash_flow * float(price), fee, _iso(at)))
        return actual

    async def apply_funding_settlement(self, symbol: str, settlement_timestamp: datetime | str | int | float, position_qty: float | None = None, funding_rate: float = 0.0, mark_price: float | None = None) -> dict[str, Any]:
        normalized, settlement, now = symbol.upper(), _dt(settlement_timestamp), _now()
        if position_qty is None:
            position_qty = float((await self.get_position(normalized)).get("qty", 0.0) or 0.0)
        price = _positive(mark_price) or _positive(self._last_market.get(normalized, {}).get("mark_price", self._last_market.get(normalized, {}).get("last", 0)))
        pnl = -float(position_qty) * price * float(funding_rate)
        with self._connect() as conn:
            try:
                conn.execute("INSERT INTO shadow_funding_settlements(symbol,settlement_timestamp,position_qty,funding_rate,funding_pnl,created_at) VALUES (?,?,?,?,?,?)", (normalized, _iso(settlement), float(position_qty), float(funding_rate), pnl, _iso(now)))
                conn.execute("UPDATE shadow_account_state SET cash=cash+?,updated_at=? WHERE account_id=1", (pnl, _iso(now)))
                applied = True
            except sqlite3.IntegrityError:
                applied = False
        if applied:
            self._record("FUNDING_SETTLED", {"symbol": normalized, "settlement_timestamp": _iso(settlement), "position_qty": position_qty, "funding_rate": funding_rate, "funding_pnl": pnl}, now)
        return {"symbol": normalized, "settlement_timestamp": _iso(settlement), "position_qty": float(position_qty), "funding_rate": float(funding_rate), "funding_pnl": pnl, "applied": applied}

    async def force_flat(self, symbol: str, episode_id: str | None = None, max_attempts: int = 3) -> dict[str, Any]:
        normalized, now = symbol.upper(), _now()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shadow_force_flat WHERE symbol=?", (normalized,)).fetchone()
            if episode_id and row and row["episode_id"] == episode_id and row["last_result_json"]:
                return json.loads(row["last_result_json"])
            if row is None or row["status"] == "COMPLETE" or (episode_id and row["episode_id"] != episode_id):
                active_episode = episode_id or uuid4().hex[:12]
                conn.execute("INSERT INTO shadow_force_flat(symbol,episode_id,status,force_flat_latch,max_attempts,sequence,requested_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET episode_id=excluded.episode_id,status=excluded.status,force_flat_latch=1,max_attempts=excluded.max_attempts,sequence=0,requested_at=excluded.requested_at,completed_at=NULL,failure=NULL,last_result_json=NULL", (normalized, active_episode, "WAIT_CANCEL_TERMINAL", 1, max(1, int(max_attempts)), 0, _iso(now)))
            else:
                active_episode = str(row["episode_id"])
                conn.execute("UPDATE shadow_force_flat SET status='WAIT_CANCEL_TERMINAL',force_flat_latch=1,max_attempts=?,failure=NULL WHERE symbol=?", (max(1, int(max_attempts)), normalized))
        await self.cancel_all_orders(normalized)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shadow_force_flat WHERE symbol=?", (normalized,)).fetchone()
            cancel_effective_at = None
            if row:
                cancel = conn.execute(
                    "SELECT MAX(cancel_effective_at) AS effective_at FROM shadow_orders WHERE symbol=? AND cancel_requested_at IS NOT NULL AND cancel_requested_at >= ?",
                    (normalized, str(row["requested_at"])),
                ).fetchone()
                cancel_effective_at = cancel["effective_at"] if cancel else None
        if await self._active_risk_count(normalized) == 0 and abs(float((await self.get_position(normalized)).get("qty", 0.0) or 0.0)) <= EPSILON:
            result = {"symbol": normalized, "episode_id": active_episode, "status": "COMPLETE", "flattened": True, "qty": 0.0, "attempts": [], "cancel_effective_at": cancel_effective_at}
            self._complete_flat(normalized, result)
            return result
        result = {
            "symbol": normalized,
            "episode_id": active_episode,
            "status": "WAIT_CANCEL_TERMINAL",
            "flattened": False,
            "cancel_effective_at": cancel_effective_at,
            "max_attempts": max(1, int(max_attempts)),
            "attempts": [],
        }
        with self._connect() as conn:
            conn.execute("UPDATE shadow_force_flat SET last_result_json=? WHERE symbol=?", (_json(result), normalized))
        self._record("FORCE_FLAT_CANCEL_REQUESTED", result, now)
        return result

    async def _advance_force_flat(self, symbol: str, at: datetime, received: datetime) -> None:
        normalized = symbol.upper()
        with self._connect() as conn:
            episode = conn.execute("SELECT * FROM shadow_force_flat WHERE symbol=?", (normalized,)).fetchone()
        if episode is None or episode["status"] not in {"WAIT_CANCEL_TERMINAL", "WAIT_FLATTEN"}:
            return
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT order_id,cancel_effective_at,status,order_type,reduce_only FROM shadow_orders WHERE symbol=? AND status='CANCEL_PENDING'",
                (normalized,),
            ).fetchall()
        for row in pending:
            cancel_at = _dt(row["cancel_effective_at"])
            if at >= cancel_at:
                self._settle_cancel(row["order_id"], at)
        if await self._active_risk_count(normalized) > 0:
            return
        position_qty = float((await self.get_position(normalized)).get("qty", 0.0) or 0.0)
        if abs(position_qty) <= EPSILON:
            result = {"symbol": normalized, "episode_id": str(episode["episode_id"]), "status": "COMPLETE", "flattened": True, "qty": 0.0, "attempts": []}
            self._complete_flat(normalized, result)
            return
        with self._connect() as conn:
            if str(episode["status"]) == "WAIT_FLATTEN" and str(episode["last_result_json"]):
                previous = json.loads(str(episode["last_result_json"]))
                attempts = list(previous.get("attempts", []))
            else:
                attempts = []
        bounded_attempts = max(1, int(episode["max_attempts"] or 3))
        sequence = int(episode["sequence"] or 0) + 1
        with self._connect() as conn:
            conn.execute("UPDATE shadow_force_flat SET status='WAIT_FLATTEN',sequence=? WHERE symbol=?", (sequence, normalized))
        side = "SELL" if position_qty > 0 else "BUY"
        client_id = f"qg-v41-force-flat-{normalized.lower()}-{episode['episode_id']}-{sequence}"
        try:
            order = await self.place_market_order(normalized, side, abs(position_qty), reduce_only=True, client_id=client_id)
            attempts.append({"client_order_id": client_id, "order": order})
        except Exception as exc:
            if len(attempts) >= bounded_attempts:
                failure = {"symbol": normalized, "episode_id": str(episode["episode_id"]), "status": "FAIL_FORCE_FLAT", "flattened": False, "attempts": attempts, "error": str(exc)}
                with self._connect() as conn:
                    conn.execute("UPDATE shadow_force_flat SET status='FAIL_FORCE_FLAT',failure=?,last_result_json=? WHERE symbol=?", ("bounded retries exhausted", _json(failure), normalized))
                self._record("FAIL_FORCE_FLAT", failure, received)
                return
            attempts.append({"client_order_id": client_id, "status": "ERROR", "error": str(exc)})
        position_qty = float((await self.get_position(normalized)).get("qty", 0.0) or 0.0)
        if abs(position_qty) <= EPSILON:
            result = {"symbol": normalized, "episode_id": str(episode["episode_id"]), "status": "COMPLETE", "flattened": True, "qty": 0.0, "attempts": attempts}
            self._complete_flat(normalized, result)
            return
        result = {"symbol": normalized, "episode_id": str(episode["episode_id"]), "status": "WAIT_FLATTEN", "flattened": False, "qty": position_qty, "attempts": attempts}
        with self._connect() as conn:
            conn.execute("UPDATE shadow_force_flat SET last_result_json=? WHERE symbol=?", (_json(result), normalized))

    async def _active_risk_count(self, symbol: str) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM shadow_orders WHERE symbol=? AND order_type='LIMIT' AND reduce_only=0 AND status IN ('NEW','PARTIALLY_FILLED','CANCEL_PENDING')", (symbol.upper(),)).fetchone()[0])

    def _complete_flat(self, symbol: str, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE shadow_force_flat SET status='COMPLETE',force_flat_latch=0,completed_at=?,failure=NULL,last_result_json=? WHERE symbol=?", (_iso(_now()), _json(result), symbol.upper()))
        self._record("FORCE_FLAT_COMPLETE", result)

    async def reconcile(self, symbol: str | None = None) -> dict[str, Any]:
        normalized = symbol.upper() if symbol else None
        violations: list[str] = []
        with self._connect() as conn:
            orders = [dict(row) for row in conn.execute("SELECT * FROM shadow_orders")]
            fills = [dict(row) for row in conn.execute("SELECT * FROM shadow_fills ORDER BY event_time,id")]
            positions = {str(row["symbol"]): float(row["qty"]) for row in conn.execute("SELECT * FROM shadow_positions")}
            cursors = [dict(row) for row in conn.execute("SELECT * FROM shadow_market_state")]
            force_flat = [dict(row) for row in conn.execute("SELECT * FROM shadow_force_flat")]
            active_risk = {
                str(row["symbol"]): int(row["count"])
                for row in conn.execute(
                    "SELECT symbol,COUNT(*) AS count FROM shadow_orders "
                    "WHERE order_type='LIMIT' AND reduce_only=0 "
                    "AND status IN ('NEW','PARTIALLY_FILLED','CANCEL_PENDING') GROUP BY symbol"
                )
            }
        if normalized:
            orders = [row for row in orders if row["symbol"] == normalized]
            fills = [row for row in fills if row["symbol"] == normalized]
            positions = {key: value for key, value in positions.items() if key == normalized}
            cursors = [row for row in cursors if row["symbol"] == normalized]
        computed: dict[str, float] = {}
        fills_by_order: dict[str, list[dict[str, Any]]] = {}
        client_ids: set[str] = set()
        for order in orders:
            filled, quantity = float(order.get("filled_qty") or 0.0), float(order.get("qty") or 0.0)
            client_id = str(order.get("client_id") or "")
            if client_id in client_ids:
                violations.append(f"clientOrderId unique: {client_id}")
            client_ids.add(client_id)
            if filled < -EPSILON or filled > quantity + EPSILON:
                violations.append(f"filled_qty<=quantity: {order['order_id']}")
            if order.get("status") == "FILLED" and quantity > EPSILON and abs(filled - quantity) > EPSILON:
                violations.append(f"FILLED has remaining quantity: {order['order_id']}")
            if order.get("order_type") == "STOP_MARKET" and float(order.get("queue_ahead_remaining") or 0.0) > EPSILON:
                violations.append(f"STOP_MARKET entered maker queue: {order['order_id']}")
            if (
                order.get("order_type") == "STOP_MARKET"
                and order.get("status") == "FILLED"
                and float(order.get("filled_qty") or 0.0) <= EPSILON
            ):
                violations.append(f"zero-position stop cannot be FILLED: {order['order_id']}")
            if float(order.get("queue_ahead_remaining") or 0.0) < -EPSILON or float(order.get("queue_ahead_remaining") or 0.0) > float(order.get("queue_ahead_initial") or 0.0) + EPSILON:
                violations.append(f"negative queue remaining: {order['order_id']}")
        order_map = {row["order_id"]: row for row in orders}
        for fill in fills:
            fills_by_order.setdefault(str(fill["order_id"]), []).append(fill)
            signed = float(fill["qty"]) if fill["side"] == "BUY" else -float(fill["qty"])
            order = order_map.get(fill["order_id"])
            if order is None:
                violations.append(f"fill references unknown order: {fill['order_id']}")
                continue
            if order.get("status") == "CANCELED" and order.get("cancel_effective_at"):
                if _dt(fill["event_time"]) > _dt(order["cancel_effective_at"]):
                    violations.append(f"CANCELED filled after cancel: {order['order_id']}")
            if int(order.get("reduce_only") or 0):
                before = computed.get(str(fill["symbol"]), 0.0)
                if (fill["side"] == "SELL" and (before <= EPSILON or float(fill["qty"]) > before + EPSILON)) or (
                    fill["side"] == "BUY" and (before >= -EPSILON or float(fill["qty"]) > abs(before) + EPSILON)
                ):
                    violations.append(f"reduce_only increased exposure: {order['order_id']}")
            computed[fill["symbol"]] = computed.get(fill["symbol"], 0.0) + signed
        for order_id, order in order_map.items():
            filled_from_trades = sum(float(fill["qty"]) for fill in fills_by_order.get(order_id, []))
            if abs(filled_from_trades - float(order.get("filled_qty") or 0.0)) > EPSILON:
                violations.append(f"order fill total mismatch: {order_id}")
        for key in set(computed) | set(positions):
            if abs(computed.get(key, 0.0) - positions.get(key, 0.0)) > EPSILON:
                violations.append(f"position matches fills: {key}")
        for cursor in cursors:
            if not cursor.get("last_event_at"):
                violations.append(f"market cursor missing: {cursor['symbol']}")
            if active_risk.get(str(cursor["symbol"]), 0) > 0:
                with self._connect() as conn:
                    freshness = conn.execute(
                        "SELECT receive_at FROM shadow_market_freshness WHERE symbol=? AND stream_type='BOOK_TICKER'",
                        (str(cursor["symbol"]),),
                    ).fetchone()
                if freshness is None:
                    violations.append(f"active maker order has no book evidence: {cursor['symbol']}")
        for episode in force_flat:
            episode_symbol = str(episode["symbol"])
            if normalized and episode_symbol != normalized:
                continue
            if episode["status"] == "COMPLETE":
                if int(episode.get("force_flat_latch") or 0) != 0 or abs(positions.get(episode_symbol, 0.0)) > EPSILON or active_risk.get(episode_symbol, 0) > 0:
                    violations.append(f"force-flat terminal invariant: {episode_symbol}")
            elif episode["status"] in {"WAIT_CANCEL_TERMINAL", "WAIT_FLATTEN"}:
                if int(episode.get("force_flat_latch") or 0) != 1:
                    violations.append(f"force-flat active latch missing: {episode_symbol}")
            elif int(episode.get("force_flat_latch") or 0) == 0:
                violations.append(f"force-flat latch missing: {episode_symbol}")
        result = "INCONSISTENT_BLOCKED" if violations else "RECONCILED"
        self._record("RECONCILE", {"result": result, "violations": violations, "symbol": normalized})
        return {"result": result, "status": result, "violations": violations, "orders": len(orders), "fills": len(fills), "positions": positions}

    def status(self) -> dict[str, Any]:
        with self._connect() as conn:
            orders = conn.execute("SELECT status,COUNT(*) AS count FROM shadow_orders GROUP BY status ORDER BY status").fetchall()
            positions = conn.execute("SELECT * FROM shadow_positions").fetchall()
            episodes = conn.execute("SELECT * FROM shadow_force_flat").fetchall()
            cursors = conn.execute("SELECT symbol,last_event_at,sequence FROM shadow_market_state").fetchall()
        return {"profile": self.profile.name, "orders": [dict(row) for row in orders], "positions": [dict(row) for row in positions], "force_flat": [dict(row) for row in episodes], "market_cursors": [dict(row) for row in cursors], "production_private_api": "DISABLED"}
