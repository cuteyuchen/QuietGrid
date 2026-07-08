from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import web as legacy_web
from core.config import AppConfig, load_config
from db.database import init_db
from db.repository import Repository


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or load_config()
    init_db(app_config.database_path)
    app = FastAPI(title="QuietGrid Console API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    def get_repository() -> Repository:
        return Repository(app_config.database_path)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "database": str(app_config.database_path),
            "mode": _mode_label(app_config),
        }

    @app.get("/api/summary")
    def summary(repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        raw_summary = repo.dashboard_summary()
        latest_logs = repo.recent_rows("system_logs", limit=1)
        latest_log = latest_logs[0] if latest_logs else None
        latest_message = legacy_web._localize_message(str(raw_summary.get("latest_system_message") or ""))
        heartbeat = str(latest_log.get("log_time")) if latest_log else ""
        risk_level = _risk_level(latest_log)
        return {
            "mode": _mode_label(app_config),
            "loop_state": legacy_web._compact_latest_message(str(raw_summary.get("latest_system_message") or "")) if latest_log else "等待运行数据",
            "heartbeat": heartbeat,
            "active_sessions": int(raw_summary.get("active_sessions") or 0),
            "open_orders": int(raw_summary.get("open_orders") or 0),
            "realized_pnl": float(raw_summary.get("realized_pnl") or 0.0),
            "latest_system_message": latest_message,
            "risk_level": risk_level,
            "database": str(app_config.database_path),
            "balance": None,
        }

    @app.get("/api/sessions/active")
    def active_sessions(
        limit: int = Query(50, ge=1, le=200),
        include_recent: bool = Query(False),
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        rows = repo.console_sessions(active_only=not include_recent, limit=limit)
        return {"items": [_session_payload(row) for row in rows]}

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: int, repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        row = repo.get_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {
            "session": _session_payload(row),
            "orders": [_order_payload(item) for item in repo.console_orders(session_id=session_id)],
            "trades": [_trade_payload(item) for item in repo.console_trades(session_id=session_id)],
        }

    @app.get("/api/orders")
    def orders(
        session_id: int | None = Query(None),
        limit: int = Query(100, ge=1, le=300),
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        return {"items": [_order_payload(row) for row in repo.console_orders(session_id=session_id, limit=limit)]}

    @app.get("/api/trades")
    def trades(
        session_id: int | None = Query(None),
        limit: int = Query(100, ge=1, le=300),
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        return {"items": [_trade_payload(row) for row in repo.console_trades(session_id=session_id, limit=limit)]}

    @app.get("/api/logs/system")
    def system_logs(limit: int = Query(50, ge=1, le=200), repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        return {"items": [_system_log_payload(row) for row in repo.recent_rows("system_logs", limit=limit)]}

    @app.get("/api/verification/testnet")
    def testnet_verification(repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        log_rows = repo.latest_system_logs_by_modules(list(legacy_web._TESTNET_VERIFICATION_MODULES))
        rows = legacy_web._testnet_verification_rows(log_rows)
        return {"items": [_verification_payload(row) for row in rows]}

    return app


app = create_app()


def _mode_label(config: AppConfig) -> str:
    return "测试网" if config.binance_testnet else "未确认"


def _risk_level(latest_log: dict[str, Any] | None) -> str:
    if latest_log is None:
        return "无运行记录"
    level = str(latest_log.get("level") or "").upper()
    if level in {"ERROR", "CRITICAL"}:
        return "异常"
    if level in {"WARN", "WARNING"}:
        return "警告"
    return "正常"


def _session_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "window_id": row.get("window_id"),
        "symbol": row.get("symbol"),
        "state": row.get("state"),
        "state_label": legacy_web._localize_scalar_text(str(row.get("state") or "")),
        "upper": row.get("grid_upper"),
        "lower": row.get("grid_lower"),
        "grid_num": row.get("grid_num"),
        "step_pct": row.get("step_pct"),
        "baseline_atr": row.get("baseline_atr"),
        "stop_loss_price": row.get("stop_loss_price"),
        "volatility_method": row.get("volatility_method"),
        "volatility_method_label": legacy_web._localize_scalar_text(str(row.get("volatility_method") or "")),
        "volatility_value": row.get("volatility_value"),
        "volatility_window": row.get("volatility_window"),
        "current_volatility": row.get("volatility_current_value"),
        "current_volatility_window": row.get("volatility_current_window"),
        "current_volatility_at": row.get("volatility_current_at"),
        "capital": row.get("capital"),
        "leverage": row.get("leverage"),
        "realized_pnl": row.get("realized_pnl"),
        "open_time": row.get("open_time"),
        "close_time": row.get("close_time"),
        "close_reason": row.get("close_reason"),
        "open_order_count": int(row.get("open_order_count") or 0),
        "trade_count": int(row.get("trade_count") or 0),
    }


def _order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "symbol": row.get("symbol"),
        "order_id": row.get("order_id"),
        "client_id": row.get("client_id"),
        "grid_index": row.get("grid_index"),
        "side": row.get("side"),
        "side_label": legacy_web._localize_scalar_text(str(row.get("side") or "")),
        "price": row.get("price"),
        "qty": row.get("qty"),
        "status": row.get("status"),
        "status_label": legacy_web._order_status_label(str(row.get("status") or "")),
        "entry_price": row.get("entry_price"),
        "created_at": row.get("created_at"),
        "filled_at": row.get("filled_at"),
        "fill_price": row.get("fill_price"),
        "updated_at": row.get("updated_at"),
    }


def _trade_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "session_id": row.get("session_id"),
        "symbol": row.get("symbol"),
        "order_id": row.get("order_id"),
        "side": row.get("side"),
        "side_label": legacy_web._localize_scalar_text(str(row.get("side") or "")),
        "price": row.get("price"),
        "qty": row.get("qty"),
        "quote_qty": row.get("quote_qty"),
        "grid_index": row.get("grid_index"),
        "grid_pnl": row.get("grid_pnl"),
        "fee": row.get("fee"),
        "funding_fee": row.get("funding_fee"),
        "trade_time": row.get("trade_time"),
        "created_at": row.get("created_at"),
    }


def _system_log_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "time": row.get("log_time"),
        "level": row.get("level"),
        "level_label": legacy_web._localize_scalar_text(str(row.get("level") or "")),
        "module": row.get("module"),
        "module_label": legacy_web._localize_value("module", row.get("module")),
        "message": legacy_web._localize_message(str(row.get("message") or "")),
        "detail": legacy_web._localize_detail(str(row.get("detail") or "")) if row.get("detail") else "",
    }


def _verification_payload(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("verification_status") or "unknown")
    module = str(row.get("module") or "")
    return {
        "module": module,
        "name": row.get("verification_item") or legacy_web._TESTNET_VERIFICATION_LABELS.get(module, module),
        "status": status,
        "status_label": legacy_web._VERIFICATION_STATUS_LABELS.get(status, status),
        "last_checked": row.get("last_checked") or "",
        "latest_message": legacy_web._localize_message(str(row.get("latest_message") or "")),
        "detail": row.get("detail_summary") or "",
    }


def run() -> None:
    import uvicorn

    config = load_config()
    web_config = config.raw.get("web", {})
    api_config = config.raw.get("api", {})
    host = str(api_config.get("address", web_config.get("address", "0.0.0.0")))
    port = int(api_config.get("port", 8000))
    uvicorn.run("api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
