from __future__ import annotations

import json
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.models import GridOrder
from db.database import connect


SystemLogNotifier = Callable[[str, str, str, str | None, datetime], None]


class Repository:
    def __init__(self, db_path: str | Path, notifier: SystemLogNotifier | None = None) -> None:
        self.db_path = db_path
        self.notifier = notifier

    def create_window(self, window_start: datetime) -> int:
        with connect(self.db_path) as conn:
            cur = conn.execute(
                "INSERT INTO windows (window_start) VALUES (?)",
                (window_start.isoformat(),),
            )
            conn.commit()
            return int(cur.lastrowid)

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
                    stop_loss_price = ?
                WHERE id = ?
                """,
                (grid_upper, grid_lower, grid_num, step_pct, baseline_atr, stop_loss_price, session_id),
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
        with connect(self.db_path) as conn:
            conn.execute(
                """
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
                """,
                (
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
                ),
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

    def recent_rows(self, table: str, limit: int = 50) -> list[dict[str, Any]]:
        if table not in {"windows", "sessions", "orders", "trades", "state_logs", "system_logs"}:
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
