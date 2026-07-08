from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
        allow_methods=["GET", "POST"],
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

    @app.get("/api/control-state")
    def control_state(repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        return _control_state_payload(repo)

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

    @app.post("/api/actions/safety-sweep")
    async def action_safety_sweep(
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _ensure_testnet_action(app_config)
        _require_confirm(request)
        return await _run_console_action(
            repo,
            action="safety_sweep",
            label="安全清扫",
            request=request,
            runner=lambda: _run_safety_sweep_action(app_config),
        )

    @app.post("/api/actions/testnet-run")
    async def action_testnet_run(
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _ensure_testnet_action(app_config)
        _require_confirm(request)
        seconds = float(request.loop_seconds or 600)
        if seconds < 20:
            raise HTTPException(status_code=422, detail="运行秒数不能小于 20。")
        return await _run_console_action(
            repo,
            action="testnet_run",
            label="一键测试网流程",
            request=request,
            runner=lambda: _run_testnet_run_action(app_config, seconds),
            extra_detail={"loop_seconds": seconds},
        )

    @app.post("/api/actions/pause-new-entries")
    def action_pause_new_entries(
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_new_entries_paused(repo, request, paused=True)

    @app.post("/api/actions/resume-new-entries")
    def action_resume_new_entries(
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_new_entries_paused(repo, request, paused=False)

    return app


class ConsoleActionRequest(BaseModel):
    confirm: bool = False
    reason: str = Field(default="控制台手动操作", min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=80)
    loop_seconds: float | None = Field(default=None, ge=20, le=86400)


app = create_app()


def _mode_label(config: AppConfig) -> str:
    return "测试网" if config.binance_testnet else "未确认"


def _require_confirm(request: ConsoleActionRequest) -> None:
    if not request.confirm:
        raise HTTPException(status_code=400, detail="控制动作需要 confirm=true。")


def _ensure_testnet_action(config: AppConfig) -> None:
    if not config.binance_testnet:
        raise HTTPException(status_code=409, detail="当前不是测试网模式，拒绝执行交易控制动作。")


def _control_state_payload(repo: Repository) -> dict[str, Any]:
    state = repo.get_control_state()
    pause_state = state.get("new_entries_paused")
    return {
        "new_entries_paused": bool(pause_state.get("value")) if isinstance(pause_state, dict) else False,
        "new_entries_paused_updated_at": pause_state.get("updated_at") if isinstance(pause_state, dict) else "",
    }


async def _run_console_action(
    repo: Repository,
    action: str,
    label: str,
    request: ConsoleActionRequest,
    runner,
    extra_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    request_id = request.request_id or str(uuid4())
    started_at = datetime.now(timezone.utc)
    detail = _action_detail(action, label, request, request_id, extra_detail)
    repo.log_system("INFO", "console_action", "Console action requested.", _json_detail(detail), started_at)
    try:
        result = await runner()
    except Exception as exc:
        repo.log_system(
            "ERROR",
            "console_action",
            "Console action failed.",
            _json_detail({**detail, "error_type": type(exc).__name__, "error": str(exc)}),
            datetime.now(timezone.utc),
        )
        raise HTTPException(status_code=500, detail=f"{label}执行失败：{exc}") from exc
    repo.log_system(
        "INFO",
        "console_action",
        "Console action completed.",
        _json_detail({**detail, "result": result}),
        datetime.now(timezone.utc),
    )
    return {
        "ok": True,
        "action": action,
        "label": label,
        "request_id": request_id,
        "message": f"{label}已完成。",
        "result": result,
    }


def _set_new_entries_paused(repo: Repository, request: ConsoleActionRequest, paused: bool) -> dict[str, Any]:
    from datetime import datetime, timezone

    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    action = "pause_new_entries" if paused else "resume_new_entries"
    label = "暂停新开仓" if paused else "恢复新开仓"
    detail = _action_detail(action, label, request, request_id)
    repo.log_system("INFO", "console_action", "Console action requested.", _json_detail(detail), now)
    repo.set_control_state("new_entries_paused", paused, now)
    repo.log_system(
        "INFO",
        "console_action",
        "Console action completed.",
        _json_detail({**detail, "new_entries_paused": paused}),
        datetime.now(timezone.utc),
    )
    return {
        "ok": True,
        "action": action,
        "label": label,
        "request_id": request_id,
        "message": f"{label}已完成。",
        "control_state": _control_state_payload(repo),
    }


def _action_detail(
    action: str,
    label: str,
    request: ConsoleActionRequest,
    request_id: str,
    extra_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "label": label,
        "request_id": request_id,
        "reason": request.reason,
        **(extra_detail or {}),
    }


def _json_detail(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


async def _run_safety_sweep_action(config: AppConfig) -> dict[str, Any]:
    from trader import _run_binance_safety_sweep

    return await _run_binance_safety_sweep(config)


async def _run_testnet_run_action(config: AppConfig, seconds: float) -> dict[str, Any]:
    from trader import _run_binance_test_run

    return await _run_binance_test_run(config, max_seconds=seconds)


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
