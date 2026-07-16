from __future__ import annotations

import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from core.models import GridOrder
from db.database import connect


SystemLogNotifier = Callable[[str, str, str, str | None, datetime], None]


class RoundStartConflict(RuntimeError):
    pass

ORDER_UPSERT_SQL = """
INSERT INTO orders
    (session_id, symbol, order_id, client_id, grid_index, side, price, qty,
     status, entry_price, created_at, filled_at, fill_price, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
ON CONFLICT(session_id, client_id) DO UPDATE SET
    order_id = excluded.order_id,
    grid_index = excluded.grid_index,
    side = excluded.side,
    price = excluded.price,
    qty = excluded.qty,
    status = excluded.status,
    entry_price = excluded.entry_price,
    filled_at = excluded.filled_at,
    fill_price = excluded.fill_price,
    updated_at = CURRENT_TIMESTAMP
"""


def _order_upsert_params(session_id: int, order: GridOrder) -> tuple[Any, ...]:
    return (
        session_id,
        order.symbol,
        order.order_id,
        order.client_id,
        order.grid_index,
        order.side.value,
        order.price,
        order.qty,
        order.status.value,
        order.entry_price,
        order.created_at.isoformat(),
        order.filled_at.isoformat() if order.filled_at else None,
        order.fill_price,
    )


def _control_value(conn: sqlite3.Connection, key: str) -> Any:
    row = conn.execute("SELECT value FROM control_state WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(str(row["value"]))
    except json.JSONDecodeError:
        return row["value"]


def _upsert_control_value(conn: sqlite3.Connection, key: str, value: Any, updated_at: datetime) -> None:
    conn.execute(
        """
        INSERT INTO control_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, json.dumps(value, ensure_ascii=False), updated_at.isoformat()),
    )


class Repository:
    def __init__(
        self,
        db_path: str | Path,
        notifier: SystemLogNotifier | None = None,
        account_id: str = "default",
    ) -> None:
        self.db_path = db_path
        self.notifier = notifier
        self.account_id = str(account_id).strip() or "default"

    def create_window(
        self,
        window_start: datetime,
        runtime_id: str | None = None,
        status: str = "open",
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO windows (runtime_id, window_start, status) VALUES (?, ?, ?)",
                (runtime_id, window_start.isoformat(), status),
            )
            conn.commit()
            return int(cur.lastrowid)

    def claim_round_window(self, runtime_id: str, window_start: datetime) -> int:
        normalized_runtime_id = str(runtime_id).strip()
        if not normalized_runtime_id:
            raise RoundStartConflict("交易服务尚未注册本次运行实例。")
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            runtime = _control_value(conn, "trader_runtime")
            if not isinstance(runtime, dict) or str(runtime.get("runtime_id") or "") != normalized_runtime_id:
                raise RoundStartConflict("交易服务运行实例已变化，请刷新页面后重试。")
            round_runtime = _control_value(conn, "round_runtime")
            if (
                isinstance(round_runtime, dict)
                and str(round_runtime.get("runtime_id") or "") == normalized_runtime_id
                and str(round_runtime.get("round_state") or "IDLE").upper() not in {"IDLE", "STOPPED"}
            ):
                raise RoundStartConflict("当前已有正在运行的网格轮次。")
            request = _control_value(conn, "round_start_request")
            if not isinstance(request, dict) or str(request.get("runtime_id") or "") != normalized_runtime_id:
                raise RoundStartConflict("当前运行实例没有待处理的启动请求。")
            cur = conn.execute(
                "INSERT INTO windows (runtime_id, window_start, status) VALUES (?, ?, 'SCANNING')",
                (normalized_runtime_id, window_start.isoformat()),
            )
            window_id = int(cur.lastrowid)
            request.update(
                {
                    "status": "running",
                    "window_id": window_id,
                    "updated_at": window_start.isoformat(),
                }
            )
            _upsert_control_value(conn, "round_start_request", request, window_start)
            _upsert_control_value(
                conn,
                "round_runtime",
                {
                    "runtime_id": normalized_runtime_id,
                    "current_round_id": window_id,
                    "round_state": "SCANNING",
                    "round_started_at": window_start.isoformat(),
                    "last_scan_at": "",
                    "next_scan_at": window_start.isoformat(),
                },
                window_start,
            )
            conn.commit()
            return window_id

    def close_window(self, window_id: int, window_end: datetime, status: str = "closed") -> None:
        with connect(self.db_path) as conn:
            total_pnl = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS value FROM sessions WHERE window_id = ?",
                (window_id,),
            ).fetchone()["value"]
            conn.execute(
                """
                UPDATE windows
                SET window_end = ?, status = ?, total_pnl = ?
                WHERE id = ?
                """,
                (window_end.isoformat(), status, total_pnl, window_id),
            )
            conn.commit()

    def close_unfinished_windows(self, closed_at: datetime, status: str = "STOPPED") -> list[int]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT id FROM windows WHERE window_end IS NULL AND UPPER(status) IN ('OPEN', 'SCANNING', 'RUNNING', 'STOPPING')"
            ).fetchall()
            window_ids = [int(row["id"]) for row in rows]
            for window_id in window_ids:
                total_pnl = conn.execute(
                    "SELECT COALESCE(SUM(realized_pnl), 0) AS value FROM sessions WHERE window_id = ?",
                    (window_id,),
                ).fetchone()["value"]
                conn.execute(
                    "UPDATE windows SET window_end = ?, status = ?, total_pnl = ? WHERE id = ?",
                    (closed_at.isoformat(), status, total_pnl, window_id),
                )
            conn.commit()
            return window_ids

    def create_session(
        self,
        window_id: int,
        symbol: str,
        state: str,
        capital: float,
        leverage: int,
        open_time: datetime,
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (window_id, symbol, state, capital, leverage, open_time)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (window_id, symbol, state, capital, leverage, open_time.isoformat()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def log_state(
        self,
        session_id: int,
        symbol: str,
        from_state: str,
        to_state: str,
        trigger: str,
        detail: str | None,
        log_time: datetime,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO state_logs
                    (session_id, symbol, from_state, to_state, trigger, detail, log_time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, symbol, from_state, to_state, trigger, detail, log_time.isoformat()),
            )
            conn.commit()

    def update_session_grid(
        self,
        session_id: int,
        grid_upper: float,
        grid_lower: float,
        grid_num: int,
        step_pct: float,
        baseline_atr: float,
        stop_loss_price: float,
        volatility_method: str | None = None,
        volatility_value: float | None = None,
        volatility_window: int | None = None,
        regime_score: float | None = None,
        grid_mode: str | None = None,
        cost_floor_pct: float | None = None,
        parameter_version: str | None = None,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET grid_upper = ?,
                    grid_lower = ?,
                    grid_num = ?,
                    step_pct = ?,
                    baseline_atr = ?,
                    stop_loss_price = ?,
                    volatility_method = ?,
                    volatility_value = ?,
                    volatility_window = ?,
                    regime_score = ?,
                    grid_mode = ?,
                    cost_floor_pct = ?,
                    parameter_version = ?
                WHERE id = ?
                """,
                (
                    grid_upper,
                    grid_lower,
                    grid_num,
                    step_pct,
                    baseline_atr,
                    stop_loss_price,
                    volatility_method,
                    volatility_value,
                    volatility_window,
                    regime_score,
                    grid_mode,
                    cost_floor_pct,
                    parameter_version,
                    session_id,
                ),
            )
            conn.commit()

    def update_session_current_volatility(
        self,
        session_id: int,
        volatility_value: float,
        volatility_window: int,
        calculated_at: datetime,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET volatility_current_value = ?,
                    volatility_current_window = ?,
                    volatility_current_at = ?
                WHERE id = ?
                """,
                (volatility_value, volatility_window, calculated_at.isoformat(), session_id),
            )
            conn.commit()

    def update_session_state(self, session_id: int, state: str) -> None:
        with connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET state = ? WHERE id = ?", (state, session_id))
            conn.commit()

    def update_session_pnl(self, session_id: int, realized_pnl: float) -> None:
        with connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET realized_pnl = ? WHERE id = ?", (realized_pnl, session_id))
            conn.commit()

    def close_session(self, session_id: int, close_reason: str, close_time: datetime, state: str = "STOPPED") -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET state = ?, close_time = ?, close_reason = ?
                WHERE id = ?
                """,
                (state, close_time.isoformat(), close_reason, session_id),
            )
            conn.commit()

    def unclosed_sessions(self) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM sessions
                WHERE close_time IS NULL
                  AND state IN ('OBSERVING', 'RUNNING', 'COOLDOWN', 'CLOSING')
                ORDER BY id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def create_trade(
        self,
        session_id: int,
        symbol: str,
        order_id: str,
        side: str,
        price: float,
        qty: float,
        grid_index: int | None,
        grid_pnl: float | None,
        trade_time: datetime,
        fee: float = 0.0,
        funding_fee: float = 0.0,
    ) -> int:
        with connect(self.db_path) as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO trades
                        (session_id, symbol, order_id, side, price, qty, quote_qty,
                         grid_index, grid_pnl, fee, funding_fee, trade_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        symbol,
                        order_id,
                        side,
                        price,
                        qty,
                        price * qty,
                        grid_index,
                        grid_pnl,
                        fee,
                        funding_fee,
                        trade_time.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT id FROM trades WHERE session_id = ? AND order_id = ?",
                    (session_id, order_id),
                ).fetchone()
                if row is None:
                    raise
                return int(row["id"])
            conn.commit()
            return int(cur.lastrowid)

    def trade_exists(self, session_id: int, order_id: str) -> bool:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE session_id = ? AND order_id = ? LIMIT 1",
                (session_id, order_id),
            ).fetchone()
            return row is not None

    def upsert_order(self, session_id: int, order: GridOrder) -> None:
        self.upsert_orders(session_id, [order])

    def upsert_orders(self, session_id: int, orders: list[GridOrder]) -> None:
        if not orders:
            return
        with connect(self.db_path) as conn:
            conn.executemany(
                ORDER_UPSERT_SQL,
                [_order_upsert_params(session_id, order) for order in orders],
            )
            conn.commit()

    def update_order_status(
        self,
        session_id: int,
        client_id: str,
        status: str,
        filled_at: datetime | None = None,
        fill_price: float | None = None,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE orders
                SET status = ?,
                    filled_at = COALESCE(?, filled_at),
                    fill_price = COALESCE(?, fill_price),
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ? AND client_id = ?
                """,
                (
                    status,
                    filled_at.isoformat() if filled_at else None,
                    fill_price,
                    session_id,
                    client_id,
                ),
            )
            conn.commit()

    def log_system(
        self,
        level: str,
        module: str,
        message: str,
        detail: str | None,
        log_time: datetime,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO system_logs (level, module, message, detail, log_time)
                VALUES (?, ?, ?, ?, ?)
                """,
                (level, module, message, detail, log_time.isoformat()),
            )
            conn.commit()
        if self.notifier is None:
            return
        try:
            self.notifier(level, module, message, detail, log_time)
        except Exception:
            logging.getLogger(__name__).warning("system log notification failed", exc_info=True)

    def set_control_state(self, key: str, value: Any, updated_at: datetime) -> None:
        normalized_key = str(key).strip()
        if not normalized_key:
            raise ValueError("control state key must not be empty")
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO control_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (normalized_key, json.dumps(value, ensure_ascii=False), updated_at.isoformat()),
            )
            conn.commit()

    def get_control_state(self) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            rows = conn.execute("SELECT key, value, updated_at FROM control_state ORDER BY key").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                value = json.loads(str(row["value"]))
            except json.JSONDecodeError:
                value = row["value"]
            result[str(row["key"])] = {"value": value, "updated_at": row["updated_at"]}
        return result

    def register_runtime(self, runtime_id: str, started_at: datetime) -> dict[str, Any]:
        normalized_runtime_id = str(runtime_id).strip()
        if not normalized_runtime_id:
            raise ValueError("runtime_id must not be empty")
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = _control_value(conn, "trader_runtime")
            if isinstance(current, dict) and str(current.get("runtime_id") or "") == normalized_runtime_id:
                conn.commit()
                return dict(current)
            runtime = {
                "runtime_id": normalized_runtime_id,
                "started_at": started_at.isoformat(),
            }
            _upsert_control_value(conn, "trader_runtime", runtime, started_at)
            conn.execute("DELETE FROM control_state WHERE key IN ('round_start_request', 'round_stop_request', 'round_runtime')")
            conn.commit()
            return runtime

    def runtime_state(self) -> dict[str, Any]:
        state = self.get_control_state()
        runtime_entry = state.get("trader_runtime")
        runtime = runtime_entry.get("value") if isinstance(runtime_entry, dict) else None
        round_entry = state.get("round_runtime")
        round_runtime = round_entry.get("value") if isinstance(round_entry, dict) else None
        runtime_id = str(runtime.get("runtime_id") or "") if isinstance(runtime, dict) else ""
        current_round_id = None
        round_state = "IDLE"
        round_started_at = ""
        last_scan_at = ""
        next_scan_at = ""
        if isinstance(round_runtime, dict) and str(round_runtime.get("runtime_id") or "") == runtime_id:
            current_round_id = round_runtime.get("current_round_id")
            round_state = str(round_runtime.get("round_state") or "IDLE")
            round_started_at = str(round_runtime.get("round_started_at") or "")
            last_scan_at = str(round_runtime.get("last_scan_at") or "")
            next_scan_at = str(round_runtime.get("next_scan_at") or "")
        request = self.round_start_request(include_terminal=True)
        request_busy = bool(
            request
            and str(request.get("runtime_id") or "") == runtime_id
            and str(request.get("status") or "") in {"requested", "running"}
        )
        if request_busy and current_round_id is None:
            round_state = "REQUESTED"
            round_started_at = str(request.get("requested_at") or "")
        if round_state == "STOPPED":
            request_busy = False
        return {
            "runtime_id": runtime_id,
            "runtime_started_at": str(runtime.get("started_at") or "") if isinstance(runtime, dict) else "",
            "round_start_available": bool(runtime_id)
            and not request_busy
            and (current_round_id is None or round_state in {"IDLE", "STOPPED"}),
            "current_round_id": current_round_id,
            "round_state": round_state,
            "round_started_at": round_started_at,
            "last_scan_at": last_scan_at,
            "next_scan_at": next_scan_at,
        }

    def request_round_start(
        self,
        reason: str,
        request_id: str,
        requested_at: datetime,
    ) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            runtime = _control_value(conn, "trader_runtime")
            runtime_id = str(runtime.get("runtime_id") or "") if isinstance(runtime, dict) else ""
            if not runtime_id:
                raise RoundStartConflict("交易服务尚未启动，当前不能创建网格轮次。")
            round_runtime = _control_value(conn, "round_runtime")
            if (
                isinstance(round_runtime, dict)
                and str(round_runtime.get("runtime_id") or "") == runtime_id
                and str(round_runtime.get("round_state") or "IDLE").upper() not in {"IDLE", "STOPPED"}
            ):
                raise RoundStartConflict("当前已有正在运行的网格轮次，请先暂停操作或停止本轮。")
            current = _control_value(conn, "round_start_request")
            if (
                isinstance(current, dict)
                and str(current.get("runtime_id") or "") == runtime_id
                and str(current.get("status") or "") in {"requested", "running"}
                and not (
                    isinstance(round_runtime, dict)
                    and str(round_runtime.get("round_state") or "").upper() == "STOPPED"
                )
            ):
                raise RoundStartConflict("当前交易服务运行实例的网格轮次已经提交。")
            request = {
                "runtime_id": runtime_id,
                "reason": str(reason),
                "request_id": str(request_id),
                "status": "requested",
                "requested_at": requested_at.isoformat(),
                "updated_at": requested_at.isoformat(),
            }
            _upsert_control_value(conn, "round_start_request", request, requested_at)
            conn.commit()
            return request

    def round_start_request(self, include_terminal: bool = False) -> dict[str, Any] | None:
        state = self.get_control_state().get("round_start_request")
        if not isinstance(state, dict) or not isinstance(state.get("value"), dict):
            return None
        request = dict(state["value"])
        status = str(request.get("status") or "")
        if not include_terminal and status in {"completed", "failed", "cancelled"}:
            return None
        return request

    def update_round_start_request(
        self,
        status: str,
        detail: Any,
        updated_at: datetime,
    ) -> None:
        request = self.round_start_request(include_terminal=True)
        if request is None:
            return
        request["status"] = str(status)
        request["detail"] = detail
        request["updated_at"] = updated_at.isoformat()
        self.set_control_state("round_start_request", request, updated_at)

    def set_round_runtime_state(
        self,
        window_id: int,
        state: str,
        updated_at: datetime,
        *,
        last_scan_at: datetime | None = None,
        next_scan_at: datetime | None = None,
    ) -> None:
        runtime = self.runtime_state()
        payload = {
            "runtime_id": runtime["runtime_id"],
            "current_round_id": int(window_id),
            "round_state": str(state).upper(),
            "round_started_at": runtime.get("round_started_at") or updated_at.isoformat(),
            "last_scan_at": last_scan_at.isoformat() if last_scan_at else runtime.get("last_scan_at", ""),
            "next_scan_at": next_scan_at.isoformat() if next_scan_at else runtime.get("next_scan_at", ""),
        }
        with connect(self.db_path) as conn:
            conn.execute("UPDATE windows SET status = ? WHERE id = ?", (payload["round_state"], int(window_id)))
            _upsert_control_value(conn, "round_runtime", payload, updated_at)
            conn.commit()

    def request_round_stop(self, reason: str, request_id: str, requested_at: datetime) -> dict[str, Any]:
        runtime = self.runtime_state()
        window_id = runtime.get("current_round_id")
        if not window_id or str(runtime.get("round_state") or "") in {"IDLE", "STOPPED"}:
            raise RoundStartConflict("当前没有正在运行的网格轮次。")
        request = {
            "runtime_id": runtime["runtime_id"],
            "window_id": int(window_id),
            "reason": str(reason),
            "request_id": str(request_id),
            "status": "requested",
            "requested_at": requested_at.isoformat(),
            "updated_at": requested_at.isoformat(),
        }
        self.set_control_state("round_stop_request", request, requested_at)
        return request

    def round_stop_request(self) -> dict[str, Any] | None:
        entry = self.get_control_state().get("round_stop_request")
        value = entry.get("value") if isinstance(entry, dict) else None
        if not isinstance(value, dict) or str(value.get("status") or "") in {"completed", "failed", "cancelled"}:
            return None
        return dict(value)

    def update_round_stop_request(self, status: str, detail: Any, updated_at: datetime) -> None:
        request = self.round_stop_request()
        if request is None:
            return
        request.update({"status": str(status), "detail": detail, "updated_at": updated_at.isoformat()})
        self.set_control_state("round_stop_request", request, updated_at)

    def upsert_round_candidate(self, window_id: int, symbol: str, updated_at: datetime, **values: Any) -> None:
        columns = {
            "liquidity_rank", "score", "volume_score", "depth_score", "volume_24h", "depth_usdt",
            "price", "bid_price", "ask_price", "spread_pct", "volatility_method", "volatility_value",
            "volatility_window", "range_lower", "range_upper", "range_width_pct", "threshold_met",
            "session_id", "stage", "error", "last_kline_close_at", "market_updated_at", "calculated_at",
            "data_stale",
        }
        normalized = {key: value for key, value in values.items() if key in columns}
        if "threshold_met" in normalized:
            normalized["threshold_met"] = int(bool(normalized["threshold_met"]))
        if "data_stale" in normalized:
            normalized["data_stale"] = int(bool(normalized["data_stale"]))
        normalized["updated_at"] = updated_at.isoformat()
        names = ["window_id", "symbol", *normalized.keys()]
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name}=excluded.{name}" for name in normalized)
        params = [int(window_id), str(symbol).strip().upper(), *normalized.values()]
        with connect(self.db_path) as conn:
            conn.execute(
                f"INSERT INTO round_candidates ({', '.join(names)}) VALUES ({placeholders}) "
                f"ON CONFLICT(window_id, symbol) DO UPDATE SET {updates}",
                params,
            )
            conn.commit()

    def update_round_candidate_market(
        self,
        window_id: int,
        symbol: str,
        price: float,
        updated_at: datetime,
        *,
        bid_price: float | None = None,
        ask_price: float | None = None,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE round_candidates
                SET price = ?, bid_price = COALESCE(?, bid_price), ask_price = COALESCE(?, ask_price),
                    market_updated_at = ?, data_stale = 0, updated_at = ?
                WHERE window_id = ? AND symbol = ?
                """,
                (
                    price, bid_price, ask_price, updated_at.isoformat(), updated_at.isoformat(),
                    int(window_id), str(symbol).strip().upper(),
                ),
            )
            conn.commit()

    def round_candidates(self, window_id: int) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM round_candidates
                WHERE window_id = ?
                ORDER BY COALESCE(liquidity_rank, 2147483647), symbol
                """,
                (int(window_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_round_candidates_stale(self, window_id: int, stale_before: datetime, updated_at: datetime) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                UPDATE round_candidates
                SET data_stale = 1, updated_at = ?
                WHERE window_id = ?
                  AND (market_updated_at IS NULL OR market_updated_at < ?)
                  AND data_stale = 0
                """,
                (updated_at.isoformat(), int(window_id), stale_before.isoformat()),
            )
            conn.commit()
            return int(cur.rowcount)

    def mark_round_candidate_stage(
        self,
        window_id: int,
        symbol: str,
        stage: str,
        updated_at: datetime,
        session_id: int | None = None,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE round_candidates
                SET stage = ?, session_id = COALESCE(?, session_id), updated_at = ?
                WHERE window_id = ? AND symbol = ?
                """,
                (stage, session_id, updated_at.isoformat(), int(window_id), str(symbol).strip().upper()),
            )
            conn.commit()

    def request_session_control(
        self,
        session_id: int,
        symbol: str,
        action: str,
        reason: str,
        request_id: str,
        requested_at: datetime,
    ) -> dict[str, Any]:
        normalized_action = str(action).strip().lower()
        if normalized_action not in {"pause", "resume"}:
            raise ValueError("session control action must be pause or resume")
        requests = self.session_control_requests(include_terminal=True)
        key = str(int(session_id))
        request = {
            "session_id": int(session_id),
            "symbol": str(symbol).strip().upper(),
            "action": normalized_action,
            "reason": str(reason),
            "request_id": str(request_id),
            "status": "requested",
            "requested_at": requested_at.isoformat(),
            "updated_at": requested_at.isoformat(),
        }
        requests[key] = request
        self.set_control_state("session_control_requests", requests, requested_at)
        return request

    def session_control_requests(self, include_terminal: bool = False) -> dict[str, dict[str, Any]]:
        state = self.get_control_state().get("session_control_requests")
        if not isinstance(state, dict) or not isinstance(state.get("value"), dict):
            return {}
        requests: dict[str, dict[str, Any]] = {}
        for key, raw_request in state["value"].items():
            if not isinstance(raw_request, dict):
                continue
            status = str(raw_request.get("status") or "requested")
            if not include_terminal and status in {"completed", "failed", "cancelled", "not_found"}:
                continue
            try:
                session_id = int(raw_request.get("session_id") or key)
            except (TypeError, ValueError):
                continue
            requests[str(session_id)] = {
                **raw_request,
                "session_id": session_id,
                "symbol": str(raw_request.get("symbol") or "").strip().upper(),
                "action": str(raw_request.get("action") or "").strip().lower(),
                "status": status,
            }
        return requests

    def pending_session_control_requests(self) -> dict[int, dict[str, Any]]:
        return {
            int(request["session_id"]): request
            for request in self.session_control_requests().values()
            if str(request.get("status")) == "requested"
        }

    def update_session_control_request(
        self,
        session_id: int,
        status: str,
        detail: str | None,
        updated_at: datetime,
    ) -> None:
        requests = self.session_control_requests(include_terminal=True)
        key = str(int(session_id))
        request = requests.get(key)
        if request is None:
            return
        request["status"] = str(status)
        request["detail"] = detail
        request["updated_at"] = updated_at.isoformat()
        requests[key] = request
        self.set_control_state("session_control_requests", requests, updated_at)

    def new_entries_paused(self) -> bool:
        state = self.get_control_state().get("new_entries_paused")
        if not isinstance(state, dict):
            return False
        return bool(state.get("value"))

    def disabled_symbols(self) -> set[str]:
        state = self.get_control_state().get("disabled_symbols")
        if not isinstance(state, dict):
            return set()
        value = state.get("value")
        if not isinstance(value, list):
            return set()
        return {str(symbol).strip().upper() for symbol in value if str(symbol).strip()}

    def set_symbol_disabled(self, symbol: str, disabled: bool, updated_at: datetime) -> list[str]:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol must not be empty")
        symbols = self.disabled_symbols()
        if disabled:
            symbols.add(normalized_symbol)
        else:
            symbols.discard(normalized_symbol)
        ordered = sorted(symbols)
        self.set_control_state("disabled_symbols", ordered, updated_at)
        return ordered

    def request_session_stop(
        self,
        session_id: int,
        symbol: str,
        reason: str,
        request_id: str,
        requested_at: datetime,
        request_type: str = "stop",
    ) -> dict[str, Any]:
        requests = self.session_stop_requests(include_terminal=True)
        key = str(int(session_id))
        request = {
            "session_id": int(session_id),
            "symbol": str(symbol).strip().upper(),
            "reason": str(reason),
            "request_id": str(request_id),
            "request_type": str(request_type).strip() or "stop",
            "status": "requested",
            "requested_at": requested_at.isoformat(),
            "updated_at": requested_at.isoformat(),
        }
        requests[key] = request
        self.set_control_state("session_stop_requests", requests, requested_at)
        return request

    def session_stop_requests(self, include_terminal: bool = False) -> dict[str, dict[str, Any]]:
        state = self.get_control_state().get("session_stop_requests")
        if not isinstance(state, dict):
            return {}
        value = state.get("value")
        if not isinstance(value, dict):
            return {}
        requests: dict[str, dict[str, Any]] = {}
        for key, raw_request in value.items():
            if not isinstance(raw_request, dict):
                continue
            status = str(raw_request.get("status") or "")
            if not include_terminal and status in {"completed", "cancelled", "not_found"}:
                continue
            try:
                session_id = int(raw_request.get("session_id") or key)
            except (TypeError, ValueError):
                continue
            requests[str(session_id)] = {
                **raw_request,
                "session_id": session_id,
                "symbol": str(raw_request.get("symbol") or "").strip().upper(),
                "request_type": str(raw_request.get("request_type") or "stop"),
                "status": status or "requested",
            }
        return requests

    def pending_session_stop_requests(self) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        for request in self.session_stop_requests().values():
            if str(request.get("status")) not in {"requested", "closing"}:
                continue
            result[int(request["session_id"])] = request
        return result

    def update_session_stop_request(
        self,
        session_id: int,
        status: str,
        detail: str | None,
        updated_at: datetime,
    ) -> None:
        requests = self.session_stop_requests(include_terminal=True)
        key = str(int(session_id))
        request = requests.get(key)
        if request is None:
            return
        request["status"] = str(status)
        request["detail"] = detail
        request["updated_at"] = updated_at.isoformat()
        requests[key] = request
        self.set_control_state("session_stop_requests", requests, updated_at)

    def strategy_config_draft(self) -> dict[str, Any] | None:
        state = self.get_control_state().get("strategy_config_draft")
        if not isinstance(state, dict):
            return None
        value = state.get("value")
        if not isinstance(value, dict):
            return None
        return dict(value)

    def set_strategy_config_draft(self, draft: dict[str, Any], updated_at: datetime) -> dict[str, Any]:
        normalized = dict(draft)
        self.set_control_state("strategy_config_draft", normalized, updated_at)
        return normalized

    def save_selection_candidates(
        self,
        account_id: str,
        environment: str,
        rows: list[dict[str, Any]],
        snapshot_at: datetime,
    ) -> None:
        normalized_account = str(account_id or "default").strip() or "default"
        normalized_environment = str(environment or "testnet").strip() or "testnet"
        with connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM selection_candidates WHERE account_id = ? AND environment = ?",
                (normalized_account, normalized_environment),
            )
            conn.executemany(
                """
                INSERT INTO selection_candidates
                    (account_id, environment, snapshot_at, rank, symbol, score, volume_score,
                     depth_score, volume_24h, depth_usdt, bid_price, ask_price, spread_pct,
                     selected, disabled, status, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        normalized_account,
                        normalized_environment,
                        snapshot_at.isoformat(),
                        int(row.get("rank") or 0),
                        str(row.get("symbol") or "").strip().upper(),
                        row.get("score"),
                        row.get("volume_score"),
                        row.get("depth_score"),
                        row.get("volume_24h"),
                        row.get("depth_usdt"),
                        row.get("bid_price"),
                        row.get("ask_price"),
                        row.get("spread_pct"),
                        1 if row.get("selected") else 0,
                        1 if row.get("disabled") else 0,
                        str(row.get("status") or "ok"),
                        str(row.get("error") or ""),
                    )
                    for row in rows
                    if str(row.get("symbol") or "").strip()
                ],
            )
            conn.commit()

    def latest_selection_candidates(self, account_id: str, environment: str, limit: int = 50) -> list[dict[str, Any]]:
        normalized_account = str(account_id or "default").strip() or "default"
        normalized_environment = str(environment or "testnet").strip() or "testnet"
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM selection_candidates
                WHERE account_id = ?
                  AND environment = ?
                ORDER BY rank ASC, id ASC
                LIMIT ?
                """,
                (normalized_account, normalized_environment, int(limit)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["selected"] = bool(item.get("selected"))
            item["disabled"] = bool(item.get("disabled"))
            result.append(item)
        return result

    def recent_rows(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
        if table not in {
            "windows",
            "sessions",
            "orders",
            "trades",
            "state_logs",
            "system_logs",
            "selection_candidates",
            "event_store",
            "feature_snapshots",
            "regime_decisions",
            "grid_plans",
            "inventory_lots",
            "inventory_snapshots",
            "risk_snapshots",
            "control_commands",
            "audit_logs",
        }:
            raise ValueError(f"不支持查询表: {table}")
        with connect(self.db_path) as conn:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def order_status_counts(self) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    orders.status AS status,
                    COUNT(*) AS count,
                    COALESCE(SUM(orders.qty), 0) AS qty,
                    COALESCE(SUM(orders.price * orders.qty), 0) AS notional
                FROM orders
                JOIN sessions ON sessions.id = orders.session_id
                WHERE sessions.close_time IS NULL
                  AND sessions.state != 'STOPPED'
                GROUP BY orders.status
                ORDER BY
                    CASE orders.status
                        WHEN 'open' THEN 0
                        WHEN 'pending' THEN 1
                        WHEN 'filled' THEN 2
                        WHEN 'cancelled' THEN 3
                        WHEN 'rejected' THEN 4
                        ELSE 5
                    END,
                    orders.status
                """
            ).fetchall()
            return [
                {
                    "status": row["status"],
                    "count": int(row["count"]),
                    "qty": float(row["qty"]),
                    "notional": float(row["notional"]),
                }
                for row in rows
            ]

    def recent_alert_events(self, limit: int = 20) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, log_time, level, module, message, detail
                FROM system_logs
                WHERE level IN ('WARN', 'ERROR')
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_system_logs_by_modules(self, modules: list[str]) -> list[dict[str, Any]]:
        normalized = [str(module).strip() for module in modules if str(module).strip()]
        if not normalized:
            return []
        placeholders = ", ".join("?" for _ in normalized)
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT log.id, log.log_time, log.level, log.module, log.message, log.detail
                FROM system_logs AS log
                JOIN (
                    SELECT module, MAX(id) AS latest_id
                    FROM system_logs
                    WHERE module IN ({placeholders})
                    GROUP BY module
                ) AS latest
                  ON log.module = latest.module
                 AND log.id = latest.latest_id
                ORDER BY log.id DESC
                """,
                normalized,
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_commission_health(self) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, log_time, level, module, message, detail
                FROM system_logs
                WHERE module = 'commission_health'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        detail = result.get("detail")
        if not detail:
            return result
        try:
            parsed = json.loads(str(detail))
        except json.JSONDecodeError:
            result["parse_error"] = "invalid_json"
            return result
        if isinstance(parsed, dict):
            result.update(parsed)
        return result

    def active_session_volatility_rows(self) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    id AS session_id,
                    symbol,
                    state,
                    volatility_method,
                    volatility_value,
                    volatility_window,
                    volatility_current_value,
                    volatility_current_window,
                    volatility_current_at,
                    grid_upper,
                    grid_lower,
                    grid_num,
                    step_pct,
                    baseline_atr
                FROM sessions
                WHERE close_time IS NULL
                  AND state != 'STOPPED'
                ORDER BY id DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def console_grid_rounds(self) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    windows.id AS window_id,
                    windows.window_start,
                    windows.window_end,
                    windows.status,
                    windows.total_pnl,
                    COUNT(sessions.id) AS session_count,
                    SUM(
                        CASE
                            WHEN sessions.close_time IS NULL AND sessions.state != 'STOPPED' THEN 1
                            ELSE 0
                        END
                    ) AS active_session_count
                FROM windows
                LEFT JOIN sessions ON sessions.window_id = windows.id
                GROUP BY
                    windows.id,
                    windows.window_start,
                    windows.window_end,
                    windows.status,
                    windows.total_pnl
                ORDER BY windows.id DESC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def console_sessions(
        self,
        active_only: bool = True,
        limit: int = 50,
        window_id: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("sessions.close_time IS NULL AND sessions.state != 'STOPPED'")
        if window_id is not None:
            clauses.append("sessions.window_id = ?")
            params.append(int(window_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    sessions.*,
                    COALESCE(order_counts.open_order_count, 0) AS open_order_count,
                    COALESCE(trade_counts.trade_count, 0) AS trade_count
                FROM sessions
                LEFT JOIN (
                    SELECT session_id, COUNT(*) AS open_order_count
                    FROM orders
                    WHERE status = 'open'
                    GROUP BY session_id
                ) AS order_counts ON order_counts.session_id = sessions.id
                LEFT JOIN (
                    SELECT session_id, COUNT(*) AS trade_count
                    FROM trades
                    GROUP BY session_id
                ) AS trade_counts ON trade_counts.session_id = sessions.id
                {where}
                ORDER BY sessions.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row is not None else None

    def console_orders(self, session_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if session_id is not None:
            where = "WHERE session_id = ?"
            params.append(session_id)
        params.append(max(1, min(int(limit), 300)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM orders
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def console_trades(self, session_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if session_id is not None:
            where = "WHERE session_id = ?"
            params.append(session_id)
        params.append(max(1, min(int(limit), 300)))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM trades
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def dashboard_summary(self) -> dict[str, Any]:
        with connect(self.db_path) as conn:
            active_sessions = conn.execute(
                "SELECT COUNT(*) AS value FROM sessions WHERE close_time IS NULL AND state != 'STOPPED'"
            ).fetchone()["value"]
            open_orders = conn.execute(
                """
                SELECT COUNT(*) AS value
                FROM orders
                JOIN sessions ON sessions.id = orders.session_id
                WHERE orders.status = 'open'
                  AND sessions.close_time IS NULL
                  AND sessions.state != 'STOPPED'
                """
            ).fetchone()["value"]
            realized_pnl = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) AS value FROM sessions").fetchone()["value"]
            latest_log = conn.execute("SELECT message FROM system_logs ORDER BY id DESC LIMIT 1").fetchone()
            return {
                "active_sessions": active_sessions,
                "open_orders": open_orders,
                "realized_pnl": realized_pnl,
                "latest_system_message": latest_log["message"] if latest_log else "",
            }

    def append_event(
        self,
        event_type: str,
        event_time: datetime,
        payload: dict[str, Any],
        *,
        session_id: int | None = None,
        symbol: str | None = None,
        available_time: datetime | None = None,
        event_id: str | None = None,
        code_commit: str | None = None,
    ) -> str:
        normalized_event_id = str(event_id or uuid4())
        with connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO event_store
                    (event_id, account_id, session_id, symbol, event_type, event_time,
                     available_time, payload_json, code_commit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_event_id,
                    self.account_id,
                    session_id,
                    str(symbol).strip().upper() if symbol else None,
                    str(event_type).strip().upper(),
                    event_time.isoformat(),
                    (available_time or event_time).isoformat(),
                    _json(payload),
                    code_commit,
                ),
            )
            conn.commit()
        return normalized_event_id

    def create_feature_snapshot(
        self,
        *,
        session_id: int | None,
        symbol: str,
        as_of_time: datetime,
        source_time: datetime,
        features: dict[str, Any],
        feature_version: str,
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO feature_snapshots
                    (session_id, symbol, as_of_time, source_time, features_json, feature_version)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    symbol.strip().upper(),
                    as_of_time.isoformat(),
                    source_time.isoformat(),
                    _json(features),
                    feature_version,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def create_regime_decision(
        self,
        *,
        session_id: int | None,
        symbol: str,
        as_of_time: datetime,
        state: str,
        grid_score: float,
        allowed: bool,
        reasons: list[str] | tuple[str, ...],
        hard_blocks: list[str] | tuple[str, ...],
        component_scores: dict[str, float],
        model_version: str,
        feature_snapshot_id: int | None,
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO regime_decisions
                    (session_id, symbol, as_of_time, state, grid_score, allowed,
                     reasons_json, hard_blocks_json, component_scores_json,
                     model_version, feature_snapshot_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    symbol.strip().upper(),
                    as_of_time.isoformat(),
                    state,
                    grid_score,
                    int(allowed),
                    _json(list(reasons)),
                    _json(list(hard_blocks)),
                    _json(component_scores),
                    model_version,
                    feature_snapshot_id,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def create_grid_plan(self, session_id: int | None, params: Any) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO grid_plans
                    (session_id, symbol, as_of_time, center, lower_price, upper_price,
                     step_pct, grid_num, prices_json, qty_weights_json, cost_floor_pct,
                     regime_score, parameter_version, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    params.symbol,
                    params.calculated_at.isoformat(),
                    params.center,
                    params.lower,
                    params.upper,
                    params.step_pct,
                    params.grid_num,
                    _json(params.grid_prices),
                    _json(params.qty_weights),
                    params.cost_floor_pct,
                    params.regime_score,
                    params.parameter_version,
                    None,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def replace_inventory_lots(
        self,
        session_id: int,
        symbol: str,
        lots: list[Any] | tuple[Any, ...],
        updated_at: datetime,
    ) -> None:
        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM inventory_lots WHERE session_id = ?", (session_id,))
            conn.executemany(
                """
                INSERT INTO inventory_lots
                    (session_id, symbol, side, entry_price, qty, entry_grid_index,
                     target_exit_price, opened_at, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                [
                    (
                        session_id,
                        symbol.strip().upper(),
                        lot.side,
                        lot.entry_price,
                        lot.qty,
                        lot.entry_grid_index,
                        lot.target_exit_price,
                        lot.opened_at.isoformat() if hasattr(lot.opened_at, "isoformat") else lot.opened_at,
                        updated_at.isoformat(),
                    )
                    for lot in lots
                ],
            )
            conn.commit()

    def create_inventory_snapshot(
        self,
        session_id: int,
        symbol: str,
        snapshot: Any,
        as_of_time: datetime,
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO inventory_snapshots
                    (session_id, symbol, as_of_time, net_qty, net_notional,
                     gross_notional, avg_entry_price, unrealized_pnl, utilization,
                     risk_score, risk_level, unpaired_lots)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    symbol.strip().upper(),
                    as_of_time.isoformat(),
                    snapshot.net_qty,
                    snapshot.net_notional,
                    snapshot.gross_notional,
                    snapshot.avg_entry_price,
                    snapshot.unrealized_pnl,
                    snapshot.utilization,
                    snapshot.risk_score,
                    snapshot.level.value,
                    len(snapshot.unpaired_lots),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def create_risk_snapshot(
        self,
        *,
        as_of_time: datetime,
        risk_level: str,
        action: str,
        reason: str,
        session_id: int | None = None,
        window_id: int | None = None,
        symbol: str | None = None,
        session_pnl: float | None = None,
        window_pnl: float | None = None,
        inventory_utilization: float | None = None,
        limits: dict[str, Any] | None = None,
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO risk_snapshots
                    (session_id, window_id, symbol, as_of_time, risk_level, action,
                     reason, session_pnl, window_pnl, inventory_utilization, limits_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    window_id,
                    symbol.strip().upper() if symbol else None,
                    as_of_time.isoformat(),
                    risk_level,
                    action,
                    reason,
                    session_pnl,
                    window_pnl,
                    inventory_utilization,
                    _json(limits or {}),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def latest_regime_decision(self, symbol: str | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM regime_decisions"
        params: tuple[Any, ...] = ()
        if symbol:
            sql += " WHERE symbol = ?"
            params = (symbol.strip().upper(),)
        sql += " ORDER BY id DESC LIMIT 1"
        with connect(self.db_path) as conn:
            row = conn.execute(sql, params).fetchone()
            return _decoded_row(
                row,
                "reasons_json",
                "hard_blocks_json",
                "component_scores_json",
            )

    def regime_decision_history(
        self,
        symbol: str,
        limit: int = 1440,
    ) -> list[dict[str, Any]]:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            return []
        capped_limit = max(1, min(int(limit), 5000))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM (
                    SELECT * FROM regime_decisions
                    WHERE symbol = ?
                    ORDER BY as_of_time DESC, id DESC
                    LIMIT ?
                )
                ORDER BY as_of_time ASC, id ASC
                """,
                (normalized_symbol, capped_limit),
            ).fetchall()
            return [
                _decoded_row(
                    row,
                    "reasons_json",
                    "hard_blocks_json",
                    "component_scores_json",
                )
                or {}
                for row in rows
            ]

    def latest_inventory_snapshot(self, session_id: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM inventory_snapshots"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id DESC LIMIT 1"
        with connect(self.db_path) as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row is not None else None

    def inventory_snapshot_history(
        self,
        session_id: int,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        capped_limit = max(1, min(int(limit), 5000))
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM (
                    SELECT * FROM inventory_snapshots
                    WHERE session_id = ?
                    ORDER BY as_of_time DESC, id DESC
                    LIMIT ?
                )
                ORDER BY as_of_time ASC, id ASC
                """,
                (session_id, capped_limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_grid_plan(self, session_id: int) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT * FROM grid_plans
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return _decoded_row(row, "prices_json", "qty_weights_json")

    def inventory_lots(self, session_id: int) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM inventory_lots
                WHERE session_id = ? AND status = 'OPEN'
                ORDER BY opened_at ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def latest_risk_snapshot(self, session_id: int | None = None) -> dict[str, Any] | None:
        sql = "SELECT * FROM risk_snapshots"
        params: tuple[Any, ...] = ()
        if session_id is not None:
            sql += " WHERE session_id = ?"
            params = (session_id,)
        sql += " ORDER BY id DESC LIMIT 1"
        with connect(self.db_path) as conn:
            row = conn.execute(sql, params).fetchone()
            return _decoded_row(row, "limits_json")

    def session_events(self, session_id: int, limit: int = 500) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_store
                WHERE session_id = ?
                ORDER BY event_time ASC, id ASC
                LIMIT ?
                """,
                (session_id, max(1, min(int(limit), 5000))),
            ).fetchall()
            return [_decoded_row(row, "payload_json") or {} for row in rows]

    def window_realized_pnl(self, window_id: int | None) -> float:
        if window_id is None:
            return 0.0
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) AS value FROM sessions WHERE window_id = ?",
                (window_id,),
            ).fetchone()
            return float(row["value"] or 0.0)

    def window_stop_count(self, window_id: int | None) -> int:
        if window_id is None:
            return 0
        with connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS value
                FROM sessions
                WHERE window_id = ?
                  AND close_reason IS NOT NULL
                  AND (
                    LOWER(close_reason) LIKE '%stop%'
                    OR close_reason LIKE '%止损%'
                    OR close_reason LIKE '%库存风险%'
                  )
                """,
                (window_id,),
            ).fetchone()
            return int(row["value"] or 0)

    def enqueue_control_command(
        self,
        *,
        command_type: str,
        target_type: str,
        target_id: str | None,
        payload: dict[str, Any],
        reason: str,
        idempotency_key: str,
        requested_at: datetime,
        requested_by: str = "console",
    ) -> dict[str, Any]:
        normalized_key = str(idempotency_key).strip()
        if not normalized_key:
            raise ValueError("idempotency_key 不能为空。")
        with connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT * FROM control_commands WHERE idempotency_key = ?",
                (normalized_key,),
            ).fetchone()
            if existing is not None:
                return _decoded_row(existing, "payload_json", "result_json") or {}
            command_id = f"cmd_{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO control_commands
                    (command_id, account_id, command_type, target_type, target_id,
                     payload_json, reason, idempotency_key, status, requested_by,
                     requested_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    command_id,
                    self.account_id,
                    command_type.strip().upper(),
                    target_type.strip().upper(),
                    target_id,
                    _json(payload),
                    reason,
                    normalized_key,
                    requested_by,
                    requested_at.isoformat(),
                    requested_at.isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM control_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            conn.commit()
            return _decoded_row(row, "payload_json", "result_json") or {}

    def pending_control_commands(self, limit: int = 50) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM control_commands
                WHERE account_id = ? AND status = 'PENDING'
                ORDER BY id ASC
                LIMIT ?
                """,
                (self.account_id, max(1, min(int(limit), 200))),
            ).fetchall()
            return [
                _decoded_row(row, "payload_json", "result_json") or {}
                for row in rows
            ]

    def update_control_command(
        self,
        command_id: str,
        status: str,
        result: dict[str, Any] | str | None,
        updated_at: datetime,
    ) -> None:
        normalized_status = status.strip().upper()
        if normalized_status not in {"PENDING", "ACCEPTED", "REJECTED", "EXECUTED", "FAILED"}:
            raise ValueError(f"不支持的控制命令状态: {status}")
        with connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE control_commands
                SET status = ?, result_json = ?, updated_at = ?
                WHERE command_id = ?
                """,
                (
                    normalized_status,
                    _json(result) if result is not None else None,
                    updated_at.isoformat(),
                    command_id,
                ),
            )
            conn.commit()

    def get_control_command(self, command_id: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM control_commands WHERE command_id = ?",
                (command_id,),
            ).fetchone()
            return _decoded_row(row, "payload_json", "result_json")

    def create_backtest_run(
        self,
        *,
        run_id: str,
        symbol: str,
        started_at: datetime,
        fill_model: str,
        config: dict[str, Any],
        parameter_version: str | None = None,
        code_commit: str | None = None,
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO backtest_runs
                    (run_id, symbol, started_at, fill_model, parameter_version,
                     code_commit, status, config_json)
                VALUES (?, ?, ?, ?, ?, ?, 'RUNNING', ?)
                """,
                (
                    run_id,
                    symbol,
                    started_at.isoformat(),
                    fill_model,
                    parameter_version,
                    code_commit,
                    _json(config),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def complete_backtest_run(
        self,
        *,
        run_id: str,
        completed_at: datetime,
        data_start: str | None,
        data_end: str | None,
        report_path: str,
        metrics: dict[str, Any],
    ) -> None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM backtest_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"回测记录不存在: {run_id}")
            run_pk = int(row["id"])
            conn.execute(
                """
                UPDATE backtest_runs
                SET completed_at = ?, data_start = ?, data_end = ?,
                    status = 'COMPLETED', report_path = ?
                WHERE id = ?
                """,
                (
                    completed_at.isoformat(),
                    data_start,
                    data_end,
                    report_path,
                    run_pk,
                ),
            )
            for name, value in metrics.items():
                metric_value = float(value) if isinstance(value, (int, float)) else None
                metric_json = None if metric_value is not None else _json(value)
                conn.execute(
                    """
                    INSERT INTO backtest_metrics
                        (backtest_run_id, metric_name, metric_value, metric_json)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(backtest_run_id, metric_name) DO UPDATE SET
                        metric_value = excluded.metric_value,
                        metric_json = excluded.metric_json
                    """,
                    (run_pk, str(name), metric_value, metric_json),
                )
            conn.commit()

    def fail_backtest_run(
        self,
        run_id: str,
        completed_at: datetime,
        error: str,
    ) -> None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM backtest_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return
            run_pk = int(row["id"])
            conn.execute(
                """
                UPDATE backtest_runs
                SET completed_at = ?, status = 'FAILED'
                WHERE id = ?
                """,
                (completed_at.isoformat(), run_pk),
            )
            conn.execute(
                """
                INSERT INTO backtest_metrics
                    (backtest_run_id, metric_name, metric_json)
                VALUES (?, 'error', ?)
                ON CONFLICT(backtest_run_id, metric_name) DO UPDATE SET
                    metric_value = NULL,
                    metric_json = excluded.metric_json
                """,
                (run_pk, _json(error)),
            )
            conn.commit()

    def backtest_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT * FROM backtest_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [self._backtest_row(conn, row) for row in rows]

    def get_backtest_run(self, run_id: str) -> dict[str, Any] | None:
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM backtest_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            return self._backtest_row(conn, row) if row is not None else None

    def _backtest_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        result = _decoded_row(row, "config_json") or {}
        metric_rows = conn.execute(
            """
            SELECT metric_name, metric_value, metric_json
            FROM backtest_metrics
            WHERE backtest_run_id = ?
            ORDER BY id ASC
            """,
            (int(row["id"]),),
        ).fetchall()
        metrics: dict[str, Any] = {}
        for metric in metric_rows:
            if metric["metric_value"] is not None:
                metrics[str(metric["metric_name"])] = float(metric["metric_value"])
                continue
            raw = metric["metric_json"]
            if raw is None:
                metrics[str(metric["metric_name"])] = None
                continue
            try:
                metrics[str(metric["metric_name"])] = json.loads(str(raw))
            except json.JSONDecodeError:
                metrics[str(metric["metric_name"])] = str(raw)
        result["metrics"] = metrics
        return result

    def append_audit_log(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str | None,
        detail: dict[str, Any],
        created_at: datetime,
        source_ip: str | None = None,
        user_agent: str | None = None,
    ) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                """
                INSERT INTO audit_logs
                    (actor, action, resource_type, resource_id, detail_json,
                     source_ip, user_agent, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor,
                    action,
                    resource_type,
                    resource_id,
                    _json(detail),
                    source_ip,
                    user_agent,
                    created_at.isoformat(),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _decoded_row(row: sqlite3.Row | None, *json_fields: str) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for field in json_fields:
        raw = result.get(field)
        if raw is None:
            continue
        try:
            result[field.removesuffix("_json")] = json.loads(str(raw))
        except json.JSONDecodeError:
            result[field.removesuffix("_json")] = raw
    return result
