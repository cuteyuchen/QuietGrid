from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from math import isfinite
import platform
import shlex
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import web as legacy_web
from core.config import AppConfig, load_config, select_account
from db.database import init_db
from db.repository import Repository, RoundStartConflict
from exchange.binance import BinanceFuturesClient
from strategy.grid_calculator import SUPPORTED_RANGE_METHODS
from strategy.selector import SelectionConfig, Selector


DEFAULT_BOUNDED_RUN_SECONDS = 60.0


@dataclass(frozen=True)
class AccountRequestContext:
    config: AppConfig
    repo: Repository


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or load_config()
    for account in app_config.accounts:
        init_db(account.database_path)
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

    def get_account_context(account_id: str | None = Query(None)) -> AccountRequestContext:
        try:
            selected_config = select_account(app_config, account_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        init_db(selected_config.database_path)
        return AccountRequestContext(
            config=selected_config,
            repo=Repository(selected_config.database_path, account_id=selected_config.account_id),
        )

    @app.get("/api/health")
    def health(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return {
            "ok": True,
            "database": str(ctx.config.database_path),
            "mode": _mode_label(ctx.config),
            "account_id": ctx.config.account_id,
            "account_label": ctx.config.account_label,
        }

    @app.get("/api/accounts")
    def accounts() -> dict[str, Any]:
        return {
            "mode": _mode_label(app_config),
            "current_account_id": app_config.account_id,
            "current_account_label": app_config.account_label,
            "accounts": [_account_payload(account, app_config) for account in app_config.accounts],
        }

    @app.get("/api/events")
    def events(
        interval_seconds: float = Query(3.0, ge=1.0, le=60.0),
        once: bool = Query(False),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> StreamingResponse:
        return StreamingResponse(
            _console_event_stream(ctx.config, ctx.repo, interval_seconds, once),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/summary")
    async def summary(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        config = ctx.config
        repo = ctx.repo
        raw_summary = repo.dashboard_summary()
        latest_logs = repo.recent_rows("system_logs", limit=1)
        latest_log = latest_logs[0] if latest_logs else None
        latest_message = legacy_web._localize_message(str(raw_summary.get("latest_system_message") or ""))
        heartbeat = str(latest_log.get("log_time")) if latest_log else ""
        risk_level = _risk_level(latest_log)
        account_summary = await _load_account_summary(config)
        return {
            "mode": _mode_label(config),
            "loop_state": legacy_web._compact_latest_message(str(raw_summary.get("latest_system_message") or "")) if latest_log else "等待运行数据",
            "heartbeat": heartbeat,
            "active_sessions": int(raw_summary.get("active_sessions") or 0),
            "open_orders": int(raw_summary.get("open_orders") or 0),
            "realized_pnl": float(raw_summary.get("realized_pnl") or 0.0),
            "latest_system_message": latest_message,
            "risk_level": risk_level,
            "database": str(config.database_path),
            "account_id": config.account_id,
            "account_label": config.account_label,
            "balance": account_summary.get("balance"),
            "available_balance": account_summary.get("available_balance"),
            "margin_balance": account_summary.get("margin_balance"),
            "initial_margin": account_summary.get("initial_margin"),
            "maintenance_margin": account_summary.get("maintenance_margin"),
            "unrealized_pnl": account_summary.get("unrealized_pnl"),
            "current_exposure": account_summary.get("current_exposure"),
            "account_summary": account_summary,
        }

    @app.get("/api/control-state")
    def control_state(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return _control_state_payload(ctx.config, ctx.repo)

    @app.get("/api/process/trader")
    def trader_process_status(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return _trader_process_status(ctx.config)

    @app.get("/api/selection/candidates")
    async def selection_candidates(
        limit: int = Query(20, ge=1, le=100),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {"items": await _load_liquidity_candidates(ctx.config, ctx.repo, limit)}

    @app.get("/api/strategy-config")
    def strategy_config(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return _strategy_config_payload(ctx.config, ctx.repo)

    @app.post("/api/strategy-config/draft")
    def save_strategy_config_draft(
        request: StrategyConfigDraftRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return _save_strategy_config_draft(ctx.config, ctx.repo, request)

    @app.get("/api/sessions/active")
    def active_sessions(
        limit: int = Query(50, ge=1, le=200),
        include_recent: bool = Query(False),
        window_id: int | None = Query(None, ge=1),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        config = ctx.config
        repo = ctx.repo
        rows = repo.console_sessions(active_only=not include_recent, limit=limit, window_id=window_id)
        disabled_symbols = repo.disabled_symbols()
        stop_requests = repo.pending_session_stop_requests()
        control_requests = repo.pending_session_control_requests()
        return {"items": [_session_payload(row, config, disabled_symbols, stop_requests, control_requests) for row in rows]}

    @app.get("/api/grid-rounds")
    def grid_rounds(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return {"items": [_grid_round_payload(row) for row in ctx.repo.console_grid_rounds()]}

    @app.get("/api/grid-rounds/{round_id}/candidates")
    def grid_round_candidates(
        round_id: int,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {"items": [_round_candidate_payload(row) for row in ctx.repo.round_candidates(round_id)]}

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: int, ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        config = ctx.config
        repo = ctx.repo
        row = repo.get_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        disabled_symbols = repo.disabled_symbols()
        stop_requests = repo.pending_session_stop_requests()
        control_requests = repo.pending_session_control_requests()
        trades = repo.console_trades(session_id=session_id)
        orders = repo.console_orders(session_id=session_id)
        session_payload = _session_payload(row, config, disabled_symbols, stop_requests, control_requests)
        session_payload["open_order_count"] = sum(
            1 for item in orders if str(item.get("status") or "").lower() == "open"
        )
        session_payload["trade_count"] = len(trades)
        return {
            "session": session_payload,
            "orders": [_order_payload(item) for item in orders],
            "trades": [_trade_payload(item) for item in trades],
            "performance": _session_performance_payload(row, trades),
            "position": await _load_session_position(config, row),
        }

    @app.get("/api/orders")
    def orders(
        session_id: int | None = Query(None),
        limit: int = Query(100, ge=1, le=300),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {"items": [_order_payload(row) for row in ctx.repo.console_orders(session_id=session_id, limit=limit)]}

    @app.get("/api/trades")
    def trades(
        session_id: int | None = Query(None),
        limit: int = Query(100, ge=1, le=300),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {"items": [_trade_payload(row) for row in ctx.repo.console_trades(session_id=session_id, limit=limit)]}

    @app.get("/api/logs/system")
    def system_logs(
        limit: int = Query(50, ge=1, le=200),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {"items": [_system_log_payload(row) for row in ctx.repo.recent_rows("system_logs", limit=limit)]}

    @app.get("/api/verification/testnet")
    def testnet_verification(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        log_rows = ctx.repo.latest_system_logs_by_modules(list(legacy_web._TESTNET_VERIFICATION_MODULES))
        rows = legacy_web._testnet_verification_rows(log_rows)
        return {"items": [_verification_payload(row) for row in rows]}

    @app.get("/api/verification/environment")
    def environment_verification(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return {"items": _readonly_environment_verification_rows(ctx.config, ctx.repo)}

    @app.post("/api/actions/environment/verify-readonly")
    async def action_verify_environment_readonly(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        rows = await _run_readonly_environment_verification(ctx.config, ctx.repo)
        return {
            "ok": True,
            "action": "environment_verify_readonly",
            "label": "只读环境验证",
            "request_id": request.request_id or str(uuid4()),
            "message": "当前连接环境的接口、账户和可用资金只读验证已完成。",
            "result": {"items": rows},
        }

    @app.post("/api/actions/safety-sweep")
    async def action_safety_sweep(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return await _run_console_action(
            ctx.repo,
            action="safety_sweep",
            label="安全清扫",
            request=request,
            runner=lambda: _run_safety_sweep_action(ctx.config),
        )

    @app.post("/api/actions/testnet-run")
    async def action_legacy_testnet_run(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return await _run_bounded_run_console_action(ctx, request)

    @app.post("/api/actions/bounded-run")
    async def action_bounded_run(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return await _run_bounded_run_console_action(ctx, request)

    @app.post("/api/actions/symbols/{symbol}/start-grid")
    async def action_start_symbol_grid(
        symbol: str,
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        normalized_symbol = _normalize_startable_symbol(ctx.config, symbol)
        seconds = float(request.loop_seconds or DEFAULT_BOUNDED_RUN_SECONDS)
        if seconds < 20:
            raise HTTPException(status_code=422, detail="运行秒数不能小于 20。")
        return await _run_console_action(
            ctx.repo,
            action="symbol_start_grid",
            label="启动指定标的网格",
            request=request,
            runner=lambda: _run_symbol_bounded_run_action(ctx.config, normalized_symbol, seconds),
            extra_detail={"symbol": normalized_symbol, "loop_seconds": seconds},
        )

    @app.post("/api/actions/grid-rounds/start")
    def action_start_grid_round(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        now = datetime.now(timezone.utc)
        request_id = request.request_id or str(uuid4())
        try:
            queued = ctx.repo.request_round_start(request.reason, request_id, now)
        except RoundStartConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ctx.repo.log_system(
            "INFO",
            "console_action",
            "Grid round start requested.",
            _json_detail(queued),
            now,
        )
        return {
            "ok": True,
            "action": "grid_round_start",
            "label": "启动一轮网格",
            "request_id": str(queued.get("request_id") or request_id),
            "message": "启动请求已提交；交易服务将扫描流动性候选并按波动阈值启动本轮网格。",
            "control_state": _control_state_payload(ctx.config, ctx.repo),
            "result": queued,
        }

    @app.post("/api/actions/pause-new-entries")
    def action_pause_new_entries(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_new_entries_paused(ctx.config, ctx.repo, request, paused=True)

    @app.post("/api/actions/resume-new-entries")
    def action_resume_new_entries(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_new_entries_paused(ctx.config, ctx.repo, request, paused=False)

    @app.post("/api/actions/sessions/{session_id}/stop")
    def action_stop_session(
        session_id: int,
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_session_stop(ctx.config, ctx.repo, session_id, request)

    @app.post("/api/actions/sessions/{session_id}/manual-close")
    def action_manual_close_session(
        session_id: int,
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_session_manual_close(ctx.config, ctx.repo, session_id, request)

    @app.post("/api/actions/sessions/{session_id}/pause")
    def action_pause_session(
        session_id: int,
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_session_control(ctx.config, ctx.repo, session_id, request, "pause")

    @app.post("/api/actions/sessions/{session_id}/resume")
    def action_resume_session(
        session_id: int,
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_session_control(ctx.config, ctx.repo, session_id, request, "resume")

    @app.post("/api/actions/sessions/stop-all")
    def action_stop_all_sessions(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_all_sessions_stop(ctx.config, ctx.repo, request)

    @app.post("/api/actions/trader-loop/stop")
    def action_stop_trader_loop(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _run_trader_process_action(ctx.config, ctx.repo, request, "stop")

    @app.post("/api/actions/trader-loop/restart")
    def action_restart_trader_loop(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _run_trader_process_action(ctx.config, ctx.repo, request, "restart")

    @app.post("/api/actions/symbols/{symbol}/disable-next-entry")
    def action_disable_symbol_next_entry(
        symbol: str,
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_symbol_next_entry_disabled(ctx.config, ctx.repo, symbol, request, disabled=True)

    @app.post("/api/actions/symbols/{symbol}/enable-next-entry")
    def action_enable_symbol_next_entry(
        symbol: str,
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_symbol_next_entry_disabled(ctx.config, ctx.repo, symbol, request, disabled=False)

    @app.get("/api/v2/health")
    def v2_health(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        latest_event = ctx.repo.recent_rows("event_store", limit=1)
        return {
            "ok": True,
            "api_version": "v2",
            "environment": "testnet" if ctx.config.binance_testnet else "live",
            "account_id": ctx.config.account_id,
            "database": str(ctx.config.database_path),
            "event_store_ready": True,
            "latest_event_time": latest_event[0]["event_time"] if latest_event else None,
        }

    @app.get("/api/v2/dashboard")
    async def v2_dashboard(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        account = await _load_account_summary(ctx.config)
        summary_row = ctx.repo.dashboard_summary()
        runtime = ctx.repo.runtime_state()
        latest_regime = ctx.repo.latest_regime_decision()
        latest_inventory = ctx.repo.latest_inventory_snapshot()
        latest_risk = ctx.repo.latest_risk_snapshot()
        risk_config = ctx.config.raw.get("risk", {})
        equity = float(account.get("balance") or 0.0)
        window_id = runtime.get("current_round_id")
        window_pnl = ctx.repo.window_realized_pnl(int(window_id)) if window_id else 0.0
        window_stop_count = ctx.repo.window_stop_count(int(window_id)) if window_id else 0
        loss_budget = equity * float(risk_config.get("max_weekend_loss_pct", 0.0))
        return {
            "environment": "testnet" if ctx.config.binance_testnet else "live",
            "trader_status": str(runtime.get("round_state") or "IDLE"),
            "account_id": ctx.config.account_id,
            "equity": equity,
            "available_balance": account.get("available_balance"),
            "current_exposure": account.get("current_exposure"),
            "window_id": window_id,
            "window_pnl": window_pnl,
            "window_loss_budget": loss_budget,
            "window_loss_budget_remaining": min(
                loss_budget,
                max(0.0, loss_budget + window_pnl),
            ),
            "window_stop_count": window_stop_count,
            "active_sessions": int(summary_row.get("active_sessions") or 0),
            "open_orders": int(summary_row.get("open_orders") or 0),
            "global_risk_level": (
                str(latest_risk.get("risk_level"))
                if latest_risk
                else "LOW"
            ),
            "data_health": "HEALTHY" if latest_regime else "WAITING",
            "latest_regime": latest_regime,
            "latest_inventory": latest_inventory,
            "latest_risk": latest_risk,
            "risk_policy": {
                "effective_leverage_cap": float(
                    risk_config.get("effective_leverage_cap", 1.0)
                ),
                "max_session_loss_pct": float(
                    risk_config.get("max_session_loss_pct", 0.0)
                ),
                "max_weekend_loss_pct": float(
                    risk_config.get("max_weekend_loss_pct", 0.0)
                ),
                "max_symbol_inventory_pct": float(
                    risk_config.get("max_symbol_inventory_pct", 0.0)
                ),
                "max_group_notional_pct": float(
                    risk_config.get("max_group_notional_pct", 0.0)
                ),
                "max_consecutive_session_losses": int(
                    risk_config.get("max_consecutive_session_losses", 0)
                ),
                "max_window_stop_count": int(
                    risk_config.get("max_window_stop_count", 0)
                ),
                "block_risk_increase_hot_reload": bool(
                    risk_config.get("block_risk_increase_hot_reload", True)
                ),
            },
        }

    @app.get("/api/v2/regime/{symbol}")
    def v2_regime(
        symbol: str,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        decision = ctx.repo.latest_regime_decision(symbol)
        if decision is None:
            raise HTTPException(status_code=404, detail="暂无该标的的 Regime 决策。")
        return decision

    @app.get("/api/v2/regime/{symbol}/history")
    def v2_regime_history(
        symbol: str,
        limit: int = Query(1440, ge=1, le=5000),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {
            "symbol": symbol.strip().upper(),
            "items": ctx.repo.regime_decision_history(symbol, limit),
        }

    @app.get("/api/v2/sessions/{session_id}/grid")
    def v2_session_grid(
        session_id: int,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        plan = ctx.repo.latest_grid_plan(session_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="暂无该会话的 v2 网格计划。")
        return plan

    @app.get("/api/v2/sessions/{session_id}/inventory")
    def v2_session_inventory(
        session_id: int,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        snapshot = ctx.repo.latest_inventory_snapshot(session_id)
        return {
            "snapshot": snapshot,
            "lots": ctx.repo.inventory_lots(session_id),
            "history": ctx.repo.inventory_snapshot_history(session_id),
        }

    @app.get("/api/v2/sessions/{session_id}/risk")
    def v2_session_risk(
        session_id: int,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        snapshot = ctx.repo.latest_risk_snapshot(session_id)
        return {"snapshot": snapshot}

    @app.get("/api/v2/sessions/{session_id}/events")
    def v2_session_events(
        session_id: int,
        limit: int = Query(500, ge=1, le=5000),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {"items": ctx.repo.session_events(session_id, limit=limit)}

    @app.get("/api/v2/sessions/{session_id}/workspace")
    async def v2_session_workspace(
        session_id: int,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        session = ctx.repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        orders = ctx.repo.console_orders(session_id=session_id, limit=500)
        trades = ctx.repo.console_trades(session_id=session_id, limit=500)
        return {
            "session": session,
            "grid_plan": ctx.repo.latest_grid_plan(session_id),
            "inventory": ctx.repo.latest_inventory_snapshot(session_id),
            "inventory_lots": ctx.repo.inventory_lots(session_id),
            "inventory_history": ctx.repo.inventory_snapshot_history(session_id),
            "risk": ctx.repo.latest_risk_snapshot(session_id),
            "events": ctx.repo.session_events(session_id, limit=1000),
            "orders": [_order_payload(item) for item in orders],
            "trades": [_trade_payload(item) for item in trades],
        }

    @app.get("/api/v2/sessions/{session_id}/order-reconciliation")
    async def v2_session_order_reconciliation(
        session_id: int,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        session = ctx.repo.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        local_orders = ctx.repo.console_orders(session_id=session_id, limit=500)
        local_open = [
            item
            for item in local_orders
            if str(item.get("status") or "").upper()
            in {"OPEN", "NEW", "PARTIALLY_FILLED", "PENDING"}
        ]
        exchange_result = await _load_exchange_open_orders(
            ctx.config,
            str(session.get("symbol") or ""),
        )
        exchange_orders = exchange_result["items"]
        differences = _order_reconciliation_differences(local_open, exchange_orders)
        return {
            "status": exchange_result["status"],
            "error": exchange_result["error"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "symbol": str(session.get("symbol") or ""),
            "local_orders": [_order_payload(item) for item in local_open],
            "exchange_orders": exchange_orders,
            "differences": differences,
            "consistent": exchange_result["status"] == "ok" and not differences,
        }

    @app.get("/api/v2/backtests/datasets")
    def v2_backtest_datasets(
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        root = _backtest_dataset_root(ctx.config)
        if not root.exists():
            return {"items": []}
        items = []
        for path in sorted(root.rglob("*.csv"), key=lambda item: item.name.lower()):
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "relative_path": path.relative_to(root).as_posix(),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                }
            )
        return {"items": items}

    @app.get("/api/v2/backtests")
    def v2_backtests(
        limit: int = Query(100, ge=1, le=500),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return {
            "items": [
                _v2_backtest_payload(item)
                for item in ctx.repo.backtest_runs(limit=limit)
            ]
        }

    @app.post("/api/v2/backtests")
    async def v2_start_backtest(
        request: V2BacktestRunRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        if request.sample_label == "OOS_FROZEN" and not request.parameters_frozen:
            raise HTTPException(
                status_code=422,
                detail="标记为 OOS_FROZEN 前必须确认参数已经冻结。",
            )
        dataset_path = _resolve_backtest_dataset(ctx.config, request.dataset)
        started_at = datetime.now(timezone.utc)
        run_id = f"bt_{uuid4().hex}"
        report_root = _backtest_report_root(ctx.config)
        report_path = report_root / f"{run_id}.json"
        run_config = request.model_dump()
        ctx.repo.create_backtest_run(
            run_id=run_id,
            symbol=request.symbol.upper(),
            started_at=started_at,
            fill_model=request.fill_model,
            config=run_config,
            parameter_version="v2-console",
            code_commit=_current_git_commit(),
        )
        try:
            summary, rows = await asyncio.to_thread(
                _execute_v2_backtest,
                ctx.config,
                request,
                dataset_path,
                report_path,
            )
            metrics = _v2_backtest_metrics(summary, report_path)
            ctx.repo.complete_backtest_run(
                run_id=run_id,
                completed_at=datetime.now(timezone.utc),
                data_start=_backtest_row_time(rows[0]) if rows else None,
                data_end=_backtest_row_time(rows[-1]) if rows else None,
                report_path=str(report_path),
                metrics=metrics,
            )
        except Exception as exc:
            ctx.repo.fail_backtest_run(
                run_id,
                datetime.now(timezone.utc),
                str(exc),
            )
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        row = ctx.repo.get_backtest_run(run_id)
        if row is None:
            raise HTTPException(status_code=500, detail="回测已完成但报告记录不存在。")
        return _v2_backtest_payload(row, include_report=True)

    @app.get("/api/v2/backtests/{run_id}")
    def v2_backtest_detail(
        run_id: str,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        row = ctx.repo.get_backtest_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail="回测记录不存在。")
        return _v2_backtest_payload(row, include_report=True)

    @app.get("/api/v2/config/active")
    def v2_active_config(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return _v2_active_config_payload(ctx.config)

    @app.get("/api/v2/commands/{command_id}")
    def v2_command_status(
        command_id: str,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        command = ctx.repo.get_control_command(command_id)
        if command is None:
            raise HTTPException(status_code=404, detail="控制命令不存在。")
        return command

    @app.post("/api/v2/commands/pause")
    def v2_pause(
        request: V2CommandRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return _enqueue_v2_command(ctx, request, "PAUSE_NEW_ENTRIES", "SYSTEM", None, "PAUSE")

    @app.post("/api/v2/commands/resume")
    def v2_resume(
        request: V2CommandRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return _enqueue_v2_command(ctx, request, "RESUME_NEW_ENTRIES", "SYSTEM", None, "RESUME")

    @app.post("/api/v2/commands/close-session")
    def v2_close_session(
        request: V2CommandRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        if request.session_id is None:
            raise HTTPException(status_code=422, detail="session_id 不能为空。")
        session = ctx.repo.get_session(request.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在。")
        expected = f"CLOSE-{str(session.get('symbol') or '').upper()}"
        return _enqueue_v2_command(
            ctx,
            request,
            "CLOSE_SESSION",
            "SESSION",
            str(request.session_id),
            expected,
        )

    @app.post("/api/v2/commands/stop-all")
    def v2_stop_all(
        request: V2CommandRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return _enqueue_v2_command(ctx, request, "STOP_ALL", "SYSTEM", None, "STOP-ALL")

    @app.post("/api/v2/commands/safety-sweep")
    def v2_safety_sweep(
        request: V2CommandRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        return _enqueue_v2_command(ctx, request, "SAFETY_SWEEP", "SYSTEM", None, "SAFETY-SWEEP")

    return app


class ConsoleActionRequest(BaseModel):
    confirm: bool = False
    reason: str = Field(default="控制台手动操作", min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=80)
    loop_seconds: float | None = Field(default=None, ge=20, le=86400)


class StrategyConfigDraftRequest(BaseModel):
    volatility_method: str = Field(min_length=1, max_length=40)
    leverage: int = Field(ge=1, le=125)
    capital_per_symbol: float = Field(gt=0, le=10000000)
    max_concurrent: int = Field(ge=1, le=10)
    scan_candidate_count: int = Field(default=10, ge=1, le=100)
    observe_hours: float = Field(gt=0, le=24)
    observe_kline_interval: str = Field(min_length=1, max_length=16)
    min_step_pct: float = Field(gt=0, le=0.05)
    min_tradable_range_pct: float = Field(default=0.0015, gt=0, le=0.05)
    max_grid_num: int = Field(ge=1, le=200)
    stop_buffer_pct: float = Field(ge=0, lt=1)
    safety_multiplier: float = Field(ge=0, le=100)
    take_profit_usdt: float | None = Field(default=None, gt=0, le=100000)
    total_capital_limit: float | None = Field(default=None, gt=0, le=10000000)
    max_maker_fee_rate: float | None = Field(default=None, ge=0, le=0.01)


class V2CommandRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=200)
    confirmation: str = Field(min_length=3, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=120)
    session_id: int | None = Field(default=None, ge=1)
    requested_by: str = Field(default="console", min_length=1, max_length=80)


class V2BacktestRunRequest(BaseModel):
    dataset: str = Field(min_length=1, max_length=240)
    symbol: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    observe_rows: int = Field(default=180, ge=30, le=100000)
    capital: float = Field(default=200.0, gt=0, le=1000000)
    leverage: float = Field(default=1.0, ge=1.0, le=2.0)
    maker_fee_rate: float = Field(default=0.0, ge=0.0, le=0.01)
    fill_model: str = Field(default="L0_CONSERVATIVE", pattern=r"^L0_CONSERVATIVE$")
    maker_fill_probability: float = Field(default=0.65, ge=0.0, le=1.0)
    max_fills_per_bar: int = Field(default=2, ge=1, le=20)
    taker_fee_rate: float = Field(default=0.0005, ge=0.0, le=0.02)
    stop_slippage_bps: float = Field(default=10.0, ge=0.0, le=1000.0)
    funding_rate_per_bar: float = Field(default=0.0, ge=-0.01, le=0.01)
    walk_forward_test_rows: int = Field(default=12, ge=5, le=100000)
    monte_carlo_simulations: int = Field(default=1000, ge=100, le=10000)
    monte_carlo_missing_fill_probability: float = Field(default=0.10, ge=0.0, le=1.0)
    monte_carlo_loss_multiplier: float = Field(default=1.25, ge=1.0, le=10.0)
    distribution_window_rows: int = Field(default=60, ge=5, le=100000)
    sample_label: str = Field(
        default="DEVELOPMENT",
        pattern=r"^(DEVELOPMENT|VALIDATION|OOS_FROZEN)$",
    )
    parameters_frozen: bool = False


app = create_app()


def _mode_label(config: AppConfig) -> str:
    return "测试网" if config.binance_testnet else "真实盘"


def _v2_active_config_payload(config: AppConfig) -> dict[str, Any]:
    raw = config.raw
    allowed_sections = (
        "features",
        "risk",
        "regime",
        "grid",
        "inventory",
        "cooldown",
        "costs",
        "timing",
        "selection",
    )
    return {
        "environment": "testnet" if config.binance_testnet else "live",
        "account_id": config.account_id,
        "version": "v2-active",
        "sections": {
            section: deepcopy(raw.get(section, {}))
            for section in allowed_sections
        },
    }


def _backtest_dataset_root(config: AppConfig) -> Path:
    raw_path = str(
        config.raw.get("backtest", {}).get("dataset_dir", "data/backtests")
    ).strip()
    root = Path(raw_path)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    return root.resolve()


def _backtest_report_root(config: AppConfig) -> Path:
    raw_path = str(
        config.raw.get("backtest", {}).get(
            "report_dir",
            "data/backtests/reports",
        )
    ).strip()
    root = Path(raw_path)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_backtest_dataset(config: AppConfig, dataset: str) -> Path:
    root = _backtest_dataset_root(config)
    candidate = (root / dataset).resolve()
    if not candidate.is_relative_to(root):
        raise HTTPException(status_code=400, detail="回测数据集路径越界。")
    if not candidate.is_file() or candidate.suffix.lower() != ".csv":
        raise HTTPException(status_code=404, detail="回测 CSV 数据集不存在。")
    return candidate


def _execute_v2_backtest(
    config: AppConfig,
    request: "V2BacktestRunRequest",
    dataset_path: Path,
    report_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from trader import _read_backtest_csv, _run_backtest_csv

    raw = deepcopy(config.raw)
    raw.setdefault("trading", {}).update(
        {
            "capital_per_symbol": request.capital,
            "leverage": request.leverage,
            "max_maker_fee_rate": request.maker_fee_rate,
        }
    )
    raw.setdefault("backtest", {}).update(
        {
            "fill_model": request.fill_model,
            "max_fills_per_bar": request.max_fills_per_bar,
            "maker_fill_probability": request.maker_fill_probability,
            "fill_probability_seed": int(
                raw.get("backtest", {}).get("fill_probability_seed", 17)
            ),
            "taker_fee_rate": request.taker_fee_rate,
            "stop_slippage_bps": request.stop_slippage_bps,
            "funding_rate_per_bar": request.funding_rate_per_bar,
            "walk_forward_test_rows": request.walk_forward_test_rows,
            "monte_carlo_simulations": request.monte_carlo_simulations,
            "monte_carlo_missing_fill_probability": (
                request.monte_carlo_missing_fill_probability
            ),
            "monte_carlo_loss_multiplier": request.monte_carlo_loss_multiplier,
        }
    )
    runtime_config = SimpleNamespace(raw=raw)
    rows = _read_backtest_csv(dataset_path)
    summary = _run_backtest_csv(
        runtime_config,
        dataset_path,
        request.observe_rows,
        request.symbol.upper(),
        0.0,
        report_path,
    )
    _append_v2_backtest_validation(
        runtime_config,
        request,
        rows,
        report_path,
    )
    return summary, rows


def _append_v2_backtest_validation(
    runtime_config: SimpleNamespace,
    request: "V2BacktestRunRequest",
    rows: list[dict[str, Any]],
    report_path: Path,
) -> None:
    from strategy.backtest import (
        backtest_config_from_mapping,
        run_grid_backtest,
    )
    from strategy.grid_calculator import calculate_grid_params
    from strategy.validation import (
        MonteCarloConfig,
        WalkForwardConfig,
        evaluate_walk_forward,
        monte_carlo_resample,
    )
    from trader import _grid_config_from_raw

    raw_backtest = runtime_config.raw.get("backtest", {})
    remaining = len(rows) - request.observe_rows
    requested_test_rows = request.walk_forward_test_rows
    test_rows = min(requested_test_rows, max(1, remaining // 2))
    walk_config = WalkForwardConfig(
        train_rows=request.observe_rows,
        test_rows=test_rows,
        step_rows=test_rows,
    )
    grid_config = _grid_config_from_raw(runtime_config.raw)
    run_config = backtest_config_from_mapping(runtime_config.raw)

    def evaluate_fold(train, test, fold):
        try:
            current_price = float(train[-1]["close"])
            params = calculate_grid_params(
                symbol=request.symbol.upper(),
                klines=list(train),
                current_price=current_price,
                funding_rate=0.0,
                config=grid_config,
            )
            result = run_grid_backtest(
                params,
                list(test),
                current_price=current_price,
                config=run_config,
            )
            return {
                "status": "COMPLETED",
                "total_pnl": result.total_pnl,
                "max_drawdown": result.max_drawdown,
                "fills": len(result.fills),
                "max_inventory_utilization": result.max_inventory_utilization,
                "stopped_reason": result.stopped_reason,
            }
        except Exception as exc:
            return {
                "status": "FAILED",
                "total_pnl": None,
                "max_drawdown": None,
                "error": str(exc),
            }

    walk_forward = evaluate_walk_forward(rows, walk_config, evaluate_fold)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    fills = report.get("fills", []) if isinstance(report, dict) else []
    event_returns = [
        float(item["grid_pnl"])
        for item in fills
        if isinstance(item, dict) and item.get("grid_pnl") is not None
    ]
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    stop_exit_pnl = float(summary.get("stop_exit_pnl") or 0.0)
    if stop_exit_pnl:
        event_returns.append(stop_exit_pnl)
    monte_carlo = monte_carlo_resample(
        event_returns,
        MonteCarloConfig(
            simulations=request.monte_carlo_simulations,
            seed=int(raw_backtest.get("monte_carlo_seed", 17)),
            missing_positive_fill_probability=(
                request.monte_carlo_missing_fill_probability
            ),
            loss_multiplier=request.monte_carlo_loss_multiplier,
            cost_per_event=float(
                raw_backtest.get("monte_carlo_cost_per_event", 0.0)
            ),
        ),
    )
    cost_sensitivity = _backtest_cost_sensitivity(
        rows,
        request,
        grid_config,
        run_config,
    )
    window_distribution = _backtest_window_distribution(
        report.get("equity_curve", []),
        request.distribution_window_rows,
        0.0,
    )
    report["validation"] = {
        "walk_forward": walk_forward,
        "monte_carlo": monte_carlo,
        "cost_sensitivity": cost_sensitivity,
        "window_distribution": window_distribution,
        "regime_diagnostics": {
            "status": "NOT_AVAILABLE",
            "reason": "当前 CSV 未包含历史盘口深度与点差，未伪造 Regime 过滤结果。",
        },
        "sample_label": request.sample_label,
        "parameters_frozen": request.parameters_frozen,
        "warning": (
            "该报告已标记为冻结参数样本外结果；仍需确认数据此前未参与调参。"
            if request.sample_label == "OOS_FROZEN"
            else "当前结果不是冻结参数样本外证明；只有从未参与调参的数据区间才可标记为样本外。"
        ),
    }
    report["metadata"] = {
        "dataset": request.dataset,
        "sample_label": request.sample_label,
        "parameters_frozen": request.parameters_frozen,
        "data_start": _backtest_row_time(rows[0]) if rows else None,
        "data_end": _backtest_row_time(rows[-1]) if rows else None,
        "row_count": len(rows),
        "observe_rows": request.observe_rows,
        "execution_rows": max(0, len(rows) - request.observe_rows),
        "fill_model": request.fill_model,
        "code_commit": _current_git_commit(),
        "run_config": request.model_dump(),
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _backtest_cost_sensitivity(
    rows: list[dict[str, Any]],
    request: "V2BacktestRunRequest",
    grid_config: Any,
    base_config: Any,
) -> dict[str, Any]:
    from strategy.backtest import run_grid_backtest
    from strategy.grid_calculator import calculate_grid_params

    if len(rows) <= request.observe_rows:
        return {"status": "FAILED", "error": "成本敏感性分析样本不足。", "scenarios": []}
    train = rows[: request.observe_rows]
    test = rows[request.observe_rows :]
    try:
        current_price = float(train[-1]["close"])
        params = calculate_grid_params(
            symbol=request.symbol.upper(),
            klines=train,
            current_price=current_price,
            funding_rate=0.0,
            config=grid_config,
        )
        scenarios = (
            ("BASELINE", "基准", base_config),
            (
                "HIGHER_MAKER_FEE",
                "Maker 费率 +2bp",
                replace(
                    base_config,
                    maker_fee_rate=min(0.02, base_config.maker_fee_rate + 0.0002),
                ),
            ),
            (
                "LOWER_FILL_RATE",
                "成交概率下降 20%",
                replace(
                    base_config,
                    maker_fill_probability=max(
                        0.0,
                        base_config.maker_fill_probability * 0.8,
                    ),
                ),
            ),
            (
                "DOUBLE_STOP_SLIPPAGE",
                "止损滑点翻倍",
                replace(
                    base_config,
                    stop_slippage_bps=max(
                        base_config.stop_slippage_bps * 2,
                        base_config.stop_slippage_bps + 5,
                    ),
                ),
            ),
            (
                "COMBINED_ADVERSE",
                "费用、漏单与滑点联合恶化",
                replace(
                    base_config,
                    maker_fee_rate=min(0.02, base_config.maker_fee_rate + 0.0002),
                    maker_fill_probability=max(
                        0.0,
                        base_config.maker_fill_probability * 0.75,
                    ),
                    stop_slippage_bps=max(
                        base_config.stop_slippage_bps * 2,
                        base_config.stop_slippage_bps + 5,
                    ),
                    funding_rate_per_bar=(
                        base_config.funding_rate_per_bar * 2
                        if base_config.funding_rate_per_bar
                        else 0.000001
                    ),
                ),
            ),
        )
        results = []
        for key, label, scenario_config in scenarios:
            result = run_grid_backtest(
                params,
                test,
                current_price=current_price,
                config=scenario_config,
            )
            results.append(
                {
                    "key": key,
                    "label": label,
                    "total_pnl": result.total_pnl,
                    "max_drawdown": result.max_drawdown,
                    "fills": len(result.fills),
                    "max_inventory_utilization": result.max_inventory_utilization,
                    "stopped_reason": result.stopped_reason,
                }
            )
        baseline_pnl = float(results[0]["total_pnl"])
        for item in results:
            item["pnl_delta_vs_baseline"] = float(item["total_pnl"]) - baseline_pnl
        return {
            "status": "COMPLETED",
            "scenario_count": len(results),
            "worst_total_pnl": min(float(item["total_pnl"]) for item in results),
            "scenarios": results,
        }
    except Exception as exc:
        return {"status": "FAILED", "error": str(exc), "scenarios": []}


def _backtest_window_distribution(
    equity_curve: Any,
    window_rows: int,
    initial_equity: float,
) -> dict[str, Any]:
    if not isinstance(equity_curve, list) or not equity_curve:
        return {
            "status": "EMPTY",
            "window_rows": window_rows,
            "window_count": 0,
            "values": [],
        }
    values: list[float] = []
    previous_equity = float(initial_equity)
    for start in range(0, len(equity_curve), window_rows):
        window = equity_curve[start : start + window_rows]
        if not window:
            continue
        end_equity = float(window[-1].get("equity") or previous_equity)
        values.append(end_equity - previous_equity)
        previous_equity = end_equity
    return {
        "status": "COMPLETED",
        "window_rows": window_rows,
        "window_count": len(values),
        "positive_ratio": (
            sum(1 for value in values if value > 0) / len(values)
            if values
            else 0.0
        ),
        "p05": _numeric_quantile(values, 0.05),
        "p50": _numeric_quantile(values, 0.50),
        "p95": _numeric_quantile(values, 0.95),
        "worst": min(values, default=0.0),
        "best": max(values, default=0.0),
        "values": values,
    }


def _numeric_quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _v2_backtest_metrics(
    summary: dict[str, Any],
    report_path: Path,
) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    fills = report.get("fills", []) if isinstance(report, dict) else []
    closed_pnls = [
        float(item["grid_pnl"])
        for item in fills
        if isinstance(item, dict) and item.get("grid_pnl") is not None
    ]
    gross_profit = sum(value for value in closed_pnls if value > 0)
    gross_loss = abs(sum(value for value in closed_pnls if value < 0))
    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else (gross_profit if gross_profit > 0 else 0.0)
    )
    metric_names = (
        "total_pnl",
        "max_drawdown",
        "equity_sharpe",
        "win_rate",
        "fills",
        "fills_per_bar",
        "grid_trade_count",
        "gross_grid_pnl",
        "fees_paid",
        "realized_pnl",
        "unrealized_pnl",
        "net_position_qty",
        "sortino",
        "calmar",
        "cvar_95",
        "profit_factor",
        "grid_fill_ratio",
        "pair_completion_ratio",
        "inventory_p50",
        "inventory_p95",
        "inventory_p99",
        "max_inventory_utilization",
        "attempted_fill_count",
        "rejected_fill_count",
        "funding_paid",
        "stop_exit_cost",
        "stop_exit_pnl",
    )
    metrics = {
        name: summary.get(name)
        for name in metric_names
        if name in summary
    }
    metrics.update(
        {
            "profit_factor": summary.get("profit_factor", profit_factor),
            "inventory_p99": summary.get("inventory_p99"),
            "fill_model_level": 0,
        }
    )
    validation = report.get("validation", {}) if isinstance(report, dict) else {}
    walk_forward = (
        validation.get("walk_forward", {})
        if isinstance(validation, dict)
        else {}
    )
    monte_carlo = (
        validation.get("monte_carlo", {})
        if isinstance(validation, dict)
        else {}
    )
    sensitivity = (
        validation.get("cost_sensitivity", {})
        if isinstance(validation, dict)
        else {}
    )
    window_distribution = (
        validation.get("window_distribution", {})
        if isinstance(validation, dict)
        else {}
    )
    if isinstance(walk_forward, dict):
        metrics.update(
            {
                "walk_forward_fold_count": walk_forward.get("fold_count"),
                "walk_forward_profitable_fold_ratio": walk_forward.get(
                    "profitable_fold_ratio"
                ),
                "walk_forward_worst_fold_pnl": walk_forward.get(
                    "worst_fold_pnl"
                ),
            }
        )
    if isinstance(monte_carlo, dict):
        metrics.update(
            {
                "monte_carlo_p05": monte_carlo.get("total_pnl_p05"),
                "monte_carlo_loss_probability": monte_carlo.get(
                    "loss_probability"
                ),
                "monte_carlo_drawdown_p99": monte_carlo.get(
                    "max_drawdown_p99"
                ),
            }
        )
    if isinstance(sensitivity, dict):
        metrics["sensitivity_worst_total_pnl"] = sensitivity.get(
            "worst_total_pnl"
        )
    if isinstance(window_distribution, dict):
        metrics.update(
            {
                "window_pnl_p05": window_distribution.get("p05"),
                "window_positive_ratio": window_distribution.get(
                    "positive_ratio"
                ),
            }
        )
    return metrics


def _backtest_row_time(row: dict[str, Any]) -> str | None:
    for key in (
        "available_time",
        "event_time",
        "timestamp",
        "open_time",
        "time",
        "close_time",
    ):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _current_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _v2_backtest_payload(
    row: dict[str, Any],
    *,
    include_report: bool = False,
) -> dict[str, Any]:
    payload = {
        "run_id": str(row.get("run_id") or ""),
        "symbol": str(row.get("symbol") or ""),
        "status": str(row.get("status") or "UNKNOWN"),
        "started_at": str(row.get("started_at") or ""),
        "completed_at": str(row.get("completed_at") or ""),
        "data_start": str(row.get("data_start") or ""),
        "data_end": str(row.get("data_end") or ""),
        "fill_model": str(row.get("fill_model") or ""),
        "parameter_version": str(row.get("parameter_version") or ""),
        "code_commit": str(row.get("code_commit") or ""),
        "report_path": str(row.get("report_path") or ""),
        "config": row.get("config") if isinstance(row.get("config"), dict) else {},
        "metrics": row.get("metrics") if isinstance(row.get("metrics"), dict) else {},
    }
    if include_report:
        report_path = Path(payload["report_path"])
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                payload["report"] = _normalize_backtest_report(
                    report,
                    payload["config"],
                )
                validation = (
                    payload["report"].get("validation", {})
                    if isinstance(payload["report"], dict)
                    else {}
                )
                window_distribution = (
                    validation.get("window_distribution", {})
                    if isinstance(validation, dict)
                    else {}
                )
                if isinstance(window_distribution, dict):
                    payload["metrics"] = dict(payload["metrics"])
                    payload["metrics"]["window_pnl_p05"] = window_distribution.get(
                        "p05"
                    )
                    payload["metrics"]["window_positive_ratio"] = (
                        window_distribution.get("positive_ratio")
                    )
            except (OSError, json.JSONDecodeError):
                payload["report"] = None
        else:
            payload["report"] = None
    return payload


def _normalize_backtest_report(
    report: Any,
    run_config: dict[str, Any],
) -> Any:
    """Rebuild derived window PnL for reports created before the PnL-baseline fix."""
    if not isinstance(report, dict):
        return report
    equity_curve = report.get("equity_curve")
    validation = report.get("validation")
    if (
        not isinstance(equity_curve, list)
        or not equity_curve
        or not isinstance(validation, dict)
    ):
        return report
    previous = validation.get("window_distribution")
    previous = previous if isinstance(previous, dict) else {}
    try:
        window_rows = max(
            1,
            int(
                previous.get("window_rows")
                or run_config.get("window_distribution_rows")
                or 60
            ),
        )
        final_equity = float(equity_curve[-1].get("equity") or 0.0)
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        total_pnl = float(summary.get("total_pnl") or 0.0)
        capital = float(run_config.get("capital") or 0.0)
    except (TypeError, ValueError, AttributeError):
        return report
    pnl_distance = abs(final_equity - total_pnl)
    capital_distance = abs(final_equity - (capital + total_pnl))
    initial_equity = 0.0 if pnl_distance <= capital_distance else capital
    normalized = _backtest_window_distribution(
        equity_curve,
        window_rows,
        initial_equity,
    )
    normalized["normalization_basis"] = (
        "PNL_BASELINE_ZERO" if initial_equity == 0 else "ACCOUNT_EQUITY"
    )
    validation["window_distribution"] = normalized
    return report


def _enqueue_v2_command(
    ctx: AccountRequestContext,
    request: V2CommandRequest,
    command_type: str,
    target_type: str,
    target_id: str | None,
    expected_confirmation: str,
) -> dict[str, Any]:
    if request.confirmation.strip().upper() != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail=f"确认文本不匹配，请输入 {expected_confirmation}。",
        )
    now = datetime.now(timezone.utc)
    command = ctx.repo.enqueue_control_command(
        command_type=command_type,
        target_type=target_type,
        target_id=target_id,
        payload={"session_id": request.session_id} if request.session_id else {},
        reason=request.reason,
        idempotency_key=request.idempotency_key,
        requested_at=now,
        requested_by=request.requested_by,
    )
    ctx.repo.append_audit_log(
        actor=request.requested_by,
        action=command_type,
        resource_type=target_type,
        resource_id=target_id,
        detail={
            "command_id": command.get("command_id"),
            "reason": request.reason,
            "status": command.get("status"),
        },
        created_at=now,
    )
    return {
        "command_id": command.get("command_id"),
        "status": command.get("status"),
    }


def _account_payload(account: Any, config: AppConfig) -> dict[str, Any]:
    return {
        "id": account.id,
        "label": account.label,
        "mode": "测试网" if account.binance_testnet else "真实盘",
        "binance_testnet": bool(account.binance_testnet),
        "database": str(account.database_path),
        "selected": account.id == config.account_id,
        "has_api_key": bool(account.binance_api_key and account.binance_api_secret),
    }


async def _console_event_stream(config: AppConfig, repo: Repository, interval_seconds: float, once: bool):
    last_versions: dict[str, str] = {}
    yield "retry: 5000\n\n"
    while True:
        payloads = {
            "runtime": _console_runtime_event_payload(config, repo),
            "market": _console_market_event_payload(config, repo),
            "session": _console_event_state_payload(config, repo),
        }
        for event_name, payload in payloads.items():
            version = str(payload["version"])
            if once or version != last_versions.get(event_name):
                yield _sse_event(event_name, payload)
                last_versions[event_name] = version
                if event_name == "session":
                    yield _sse_event("state", payload)
        if once:
            break
        await asyncio.sleep(interval_seconds)


def _console_runtime_event_payload(config: AppConfig, repo: Repository) -> dict[str, Any]:
    runtime = repo.runtime_state()
    return {
        "account_id": config.account_id,
        "version": json.dumps(runtime, sort_keys=True, ensure_ascii=False),
        "server_time": datetime.now(timezone.utc).isoformat(),
        **runtime,
    }


def _console_market_event_payload(config: AppConfig, repo: Repository) -> dict[str, Any]:
    runtime = repo.runtime_state()
    round_id = runtime.get("current_round_id")
    rows = repo.round_candidates(int(round_id)) if round_id else []
    items = [_round_candidate_payload(row) for row in rows]
    marker = max((str(row.get("updated_at") or "") for row in rows), default="")
    return {
        "account_id": config.account_id,
        "round_id": round_id,
        "version": json.dumps({"round_id": round_id, "updated_at": marker}, sort_keys=True),
        "server_time": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }


def _console_event_state_payload(config: AppConfig, repo: Repository) -> dict[str, Any]:
    summary = repo.dashboard_summary()
    latest_log = _first_row(repo.recent_rows("system_logs", limit=1))
    latest_order = _first_row(repo.console_orders(limit=1))
    latest_trade = _first_row(repo.console_trades(limit=1))
    latest_control = _latest_control_state_marker(repo)
    markers = {
        "active_sessions": int(summary.get("active_sessions") or 0),
        "open_orders": int(summary.get("open_orders") or 0),
        "realized_pnl": float(summary.get("realized_pnl") or 0.0),
        "latest_log_id": latest_log.get("id"),
        "latest_log_time": latest_log.get("log_time"),
        "latest_order_id": latest_order.get("id"),
        "latest_order_updated_at": latest_order.get("updated_at"),
        "latest_trade_id": latest_trade.get("id"),
        "latest_trade_time": latest_trade.get("trade_time"),
        "latest_control_updated_at": latest_control,
    }
    return {
        "account_id": config.account_id,
        "mode": _mode_label(config),
        "version": json.dumps(markers, sort_keys=True, ensure_ascii=False),
        "server_time": datetime.now(timezone.utc).isoformat(),
        **markers,
    }


def _latest_control_state_marker(repo: Repository) -> str:
    state = repo.get_control_state()
    updated_values = [str(item.get("updated_at") or "") for item in state.values() if isinstance(item, dict)]
    return max(updated_values, default="")


def _first_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[0] if rows else {}


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _load_account_summary(config: AppConfig) -> dict[str, Any]:
    if not config.binance_api_key or not config.binance_api_secret:
        return _empty_account_summary("unconfigured", "当前账户未配置 Binance API 密钥。")
    exchange = None
    try:
        exchange = await BinanceFuturesClient.create(
            api_key=config.binance_api_key,
            api_secret=config.binance_api_secret,
            testnet=config.binance_testnet,
            proxy_config=config.raw.get("proxy"),
        )
        summary = await exchange.get_account_summary()
    except Exception as exc:
        return _empty_account_summary("error", str(exc))
    finally:
        if exchange is not None:
            await exchange.close()
    return {
        **_empty_account_summary("ok", ""),
        **summary,
    }


async def _load_session_position(config: AppConfig, row: dict[str, Any]) -> dict[str, Any]:
    if row.get("close_time") or str(row.get("state") or "").upper() == "STOPPED":
        return {"status": "historical", "error": "", "symbol": row.get("symbol"), "qty": 0.0}
    if not config.binance_api_key or not config.binance_api_secret:
        return {"status": "unconfigured", "error": "当前账户未配置 Binance API 密钥。", "symbol": row.get("symbol")}
    exchange = None
    try:
        exchange = await BinanceFuturesClient.create(
            api_key=config.binance_api_key,
            api_secret=config.binance_api_secret,
            testnet=config.binance_testnet,
            proxy_config=config.raw.get("proxy"),
        )
        position = await exchange.get_position(str(row.get("symbol") or ""))
        return {"status": "ok", "error": "", **position}
    except Exception as exc:
        return {"status": "error", "error": str(exc), "symbol": row.get("symbol")}
    finally:
        if exchange is not None:
            await exchange.close()


async def _load_exchange_open_orders(
    config: AppConfig,
    symbol: str,
) -> dict[str, Any]:
    if not config.binance_api_key or not config.binance_api_secret:
        return {
            "status": "unconfigured",
            "error": "当前账户未配置 Binance API 密钥。",
            "items": [],
        }
    exchange = None
    try:
        exchange = await BinanceFuturesClient.create(
            api_key=config.binance_api_key,
            api_secret=config.binance_api_secret,
            testnet=config.binance_testnet,
            proxy_config=config.raw.get("proxy"),
        )
        rows = await exchange.get_open_orders(symbol)
        return {
            "status": "ok",
            "error": "",
            "items": [_exchange_order_payload(item) for item in rows],
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc), "items": []}
    finally:
        if exchange is not None:
            await exchange.close()


def _exchange_order_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": str(
            row.get("orderId")
            or row.get("order_id")
            or ""
        ),
        "client_id": str(
            row.get("clientOrderId")
            or row.get("origClientOrderId")
            or row.get("client_id")
            or ""
        ),
        "symbol": str(row.get("symbol") or ""),
        "side": str(row.get("side") or "").upper(),
        "price": _optional_float(row.get("price")),
        "qty": _optional_float(
            row.get("origQty")
            if row.get("origQty") not in (None, "")
            else row.get("qty")
        ),
        "executed_qty": _optional_float(row.get("executedQty")),
        "status": str(row.get("status") or "OPEN").upper(),
        "type": str(row.get("type") or ""),
        "reduce_only": bool(row.get("reduceOnly", False)),
        "update_time": row.get("updateTime") or row.get("time"),
    }


def _order_reconciliation_differences(
    local_orders: list[dict[str, Any]],
    exchange_orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    unmatched_exchange = set(range(len(exchange_orders)))
    for local in local_orders:
        matched_index = next(
            (
                index
                for index in unmatched_exchange
                if _orders_refer_to_same_order(local, exchange_orders[index])
            ),
            None,
        )
        if matched_index is None:
            differences.append(
                {
                    "type": "LOCAL_ONLY",
                    "severity": "HIGH",
                    "client_id": str(local.get("client_id") or ""),
                    "order_id": str(local.get("order_id") or ""),
                    "message": "本地认为订单仍未结束，但交易所开放订单中不存在。",
                    "local": _order_payload(local),
                    "exchange": None,
                }
            )
            continue
        unmatched_exchange.remove(matched_index)
        exchange = exchange_orders[matched_index]
        mismatches: list[str] = []
        if str(local.get("side") or "").upper() != str(exchange.get("side") or "").upper():
            mismatches.append("方向")
        if not _numbers_close(local.get("price"), exchange.get("price")):
            mismatches.append("价格")
        if not _numbers_close(local.get("qty"), exchange.get("qty")):
            mismatches.append("数量")
        if mismatches:
            differences.append(
                {
                    "type": "FIELD_MISMATCH",
                    "severity": "HIGH",
                    "client_id": str(local.get("client_id") or ""),
                    "order_id": str(local.get("order_id") or ""),
                    "message": f"本地与交易所订单字段不一致：{'、'.join(mismatches)}。",
                    "local": _order_payload(local),
                    "exchange": exchange,
                }
            )
    for index in sorted(unmatched_exchange):
        exchange = exchange_orders[index]
        differences.append(
            {
                "type": "EXCHANGE_ONLY",
                "severity": "CRITICAL",
                "client_id": str(exchange.get("client_id") or ""),
                "order_id": str(exchange.get("order_id") or ""),
                "message": "交易所存在本地未跟踪的开放订单。",
                "local": None,
                "exchange": exchange,
            }
        )
    return differences


def _orders_refer_to_same_order(
    local: dict[str, Any],
    exchange: dict[str, Any],
) -> bool:
    local_client = str(local.get("client_id") or "")
    exchange_client = str(exchange.get("client_id") or "")
    if local_client and exchange_client and local_client == exchange_client:
        return True
    local_order = str(local.get("order_id") or "")
    exchange_order = str(exchange.get("order_id") or "")
    return bool(local_order and exchange_order and local_order == exchange_order)


def _numbers_close(left: Any, right: Any) -> bool:
    left_value = _optional_float(left)
    right_value = _optional_float(right)
    if left_value is None or right_value is None:
        return left_value is right_value
    return abs(left_value - right_value) <= max(
        1e-9,
        1e-8 * max(abs(left_value), abs(right_value)),
    )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _readonly_environment_verification_rows(config: AppConfig, repo: Repository) -> list[dict[str, Any]]:
    state = repo.get_control_state().get("readonly_environment_verification")
    if isinstance(state, dict) and isinstance(state.get("value"), list):
        return [dict(row) for row in state["value"] if isinstance(row, dict)]
    key_status = "passed" if config.binance_api_key and config.binance_api_secret else "not_run"
    key_detail = "当前账户已配置 API 密钥，可执行只读验证。" if key_status == "passed" else "当前账户未配置 API 密钥。"
    return [
        _readonly_verification_row("API 密钥配置", key_status, key_detail, "environment_credentials", "-"),
        _readonly_verification_row("交易所接口连接", "not_run", "尚未执行当前环境只读连接检查。", "environment_connectivity", "-"),
        _readonly_verification_row("账户与可用资金", "not_run", "尚未读取当前环境账户资金。", "environment_funds", "-"),
    ]


async def _run_readonly_environment_verification(config: AppConfig, repo: Repository) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    checked_at = now.isoformat()
    has_credentials = bool(config.binance_api_key and config.binance_api_secret)
    rows = [
        _readonly_verification_row(
            "API 密钥配置",
            "passed" if has_credentials else "failed",
            "当前账户已配置 API 密钥。" if has_credentials else "当前账户未配置 Binance API 密钥。",
            "environment_credentials",
            checked_at,
        )
    ]
    if not has_credentials:
        rows.extend(
            [
                _readonly_verification_row("交易所接口连接", "failed", "缺少 API 密钥，未连接交易所。", "environment_connectivity", checked_at),
                _readonly_verification_row("账户与可用资金", "failed", "缺少 API 密钥，未读取账户资金。", "environment_funds", checked_at),
            ]
        )
        repo.set_control_state("readonly_environment_verification", rows, now)
        return rows
    exchange = None
    try:
        exchange = await BinanceFuturesClient.create(
            api_key=config.binance_api_key,
            api_secret=config.binance_api_secret,
            testnet=config.binance_testnet,
            proxy_config=config.raw.get("proxy"),
        )
        symbols = await exchange.get_symbols()
        rows.append(
            _readonly_verification_row(
                "交易所接口连接",
                "passed",
                f"已连接{_mode_label(config)}接口，可交易永续标的 {len(symbols)} 个。",
                "environment_connectivity",
                checked_at,
            )
        )
        summary = await exchange.get_account_summary()
        rows.append(
            _readonly_verification_row(
                "账户与可用资金",
                "passed",
                (
                    f"余额 {float(summary.get('balance') or 0):.2f} {summary.get('asset') or 'USDT'}，"
                    f"可用 {float(summary.get('available_balance') or 0):.2f}，"
                    f"当前暴露 {float(summary.get('current_exposure') or 0):.2f}。"
                ),
                "environment_funds",
                checked_at,
            )
        )
    except Exception as exc:
        if len(rows) == 1:
            rows.append(_readonly_verification_row("交易所接口连接", "failed", str(exc), "environment_connectivity", checked_at))
        rows.append(_readonly_verification_row("账户与可用资金", "failed", str(exc), "environment_funds", checked_at))
    finally:
        if exchange is not None:
            await exchange.close()
    repo.set_control_state("readonly_environment_verification", rows, now)
    repo.log_system("INFO" if all(row["status"] == "passed" for row in rows) else "ERROR", "environment_readonly", "Readonly environment verification completed.", _json_detail({"rows": rows}), now)
    return rows


def _readonly_verification_row(name: str, status: str, detail: str, module: str, checked_at: str) -> dict[str, Any]:
    labels = {"passed": "通过", "failed": "失败", "not_run": "未运行"}
    return {
        "name": name,
        "status": status,
        "status_code": status,
        "status_label": labels.get(status, status),
        "detail": detail,
        "module": module,
        "latest_message": detail,
        "last_checked": checked_at,
    }


def _empty_account_summary(status: str, error: str) -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "asset": "USDT",
        "balance": None,
        "available_balance": None,
        "margin_balance": None,
        "initial_margin": None,
        "maintenance_margin": None,
        "unrealized_pnl": None,
        "current_exposure": None,
        "positions": [],
    }


async def _load_liquidity_candidates(config: AppConfig, repo: Repository, limit: int) -> list[dict[str, Any]]:
    if not config.binance_api_key or not config.binance_api_secret:
        return _fallback_candidate_rows(config, repo, limit, "unconfigured", "当前账户未配置 Binance API 密钥。")
    exchange = None
    try:
        exchange = await BinanceFuturesClient.create(
            api_key=config.binance_api_key,
            api_secret=config.binance_api_secret,
            testnet=config.binance_testnet,
            proxy_config=config.raw.get("proxy"),
        )
        selector = Selector(exchange, _selection_config(config))
        scored = await selector.score_candidates()
    except Exception as exc:
        return _fallback_candidate_rows(config, repo, limit, "error", str(exc))
    finally:
        if exchange is not None:
            await exchange.close()

    selected_symbols = {item.symbol for item in scored[: _selection_max_concurrent(config)]}
    disabled_symbols = repo.disabled_symbols()
    volatility_by_symbol = _session_volatility_by_symbol(repo)
    rows = [
        _candidate_payload(index + 1, item, item.symbol in selected_symbols, item.symbol in disabled_symbols, volatility_by_symbol)
        for index, item in enumerate(scored[:limit])
    ]
    repo.save_selection_candidates(config.account_id, _selection_environment(config), rows, datetime.now(timezone.utc))
    return rows


def _selection_config(config: AppConfig) -> SelectionConfig:
    raw = config.raw
    selection = raw.get("selection", {})
    trading = raw.get("trading", {})
    return SelectionConfig(
        max_concurrent=_selection_max_concurrent(config),
        scan_candidate_count=int(selection.get("scan_candidate_count", 10)),
        symbol_blacklist=tuple(str(item) for item in selection.get("symbol_blacklist", [])),
        symbol_allowlist=tuple(str(item) for item in selection.get("symbol_allowlist", [])),
        volume_weight=float(selection.get("volume_weight", 0.7)),
        depth_weight=float(selection.get("depth_weight", 0.3)),
        depth_levels=int(selection.get("depth_levels", trading.get("depth_levels", 5))),
    )


def _selection_max_concurrent(config: AppConfig) -> int:
    return int(config.raw.get("trading", {}).get("max_concurrent", 1))


def _selection_environment(config: AppConfig) -> str:
    return "testnet" if config.binance_testnet else "live"


def _fallback_candidate_rows(config: AppConfig, repo: Repository, limit: int, status: str, error: str) -> list[dict[str, Any]]:
    cached = _cached_candidate_rows(config, repo, limit, status, error)
    if cached:
        return cached
    return _configured_candidate_rows(config, repo, "unconfigured" if status == "unconfigured" else status, error)[:limit]


def _cached_candidate_rows(config: AppConfig, repo: Repository, limit: int, status: str, error: str) -> list[dict[str, Any]]:
    volatility_by_symbol = _session_volatility_by_symbol(repo)
    rows = []
    for row in repo.latest_selection_candidates(config.account_id, _selection_environment(config), limit):
        symbol = str(row.get("symbol") or "").upper()
        if not symbol:
            continue
        rows.append(
            {
                "rank": row.get("rank"),
                "symbol": symbol,
                "score": row.get("score"),
                "volume_score": row.get("volume_score"),
                "depth_score": row.get("depth_score"),
                "volume_24h": row.get("volume_24h"),
                "depth_usdt": row.get("depth_usdt"),
                "bid_price": row.get("bid_price"),
                "ask_price": row.get("ask_price"),
                "spread_pct": row.get("spread_pct"),
                "selected": bool(row.get("selected")),
                "disabled": bool(row.get("disabled")),
                "status": "cached" if status == "unconfigured" else "stale",
                "error": error,
                "snapshot_at": row.get("snapshot_at"),
                **volatility_by_symbol.get(symbol, _empty_candidate_volatility()),
            }
        )
    return rows


def _configured_candidate_rows(config: AppConfig, repo: Repository, status: str, error: str) -> list[dict[str, Any]]:
    selected = set(_configured_startable_symbols(config)[: _selection_max_concurrent(config)])
    disabled_symbols = repo.disabled_symbols()
    volatility_by_symbol = _session_volatility_by_symbol(repo)
    rows = []
    for index, symbol in enumerate(_configured_startable_symbols(config), start=1):
        rows.append(
            {
                "rank": index,
                "symbol": symbol,
                "score": None,
                "volume_score": None,
                "depth_score": None,
                "volume_24h": None,
                "depth_usdt": None,
                "bid_price": None,
                "ask_price": None,
                "spread_pct": None,
                "selected": symbol in selected,
                "disabled": symbol in disabled_symbols,
                "status": status,
                "error": error,
                **volatility_by_symbol.get(symbol, _empty_candidate_volatility()),
            }
        )
    return rows


def _candidate_payload(
    rank: int,
    item,
    selected: bool,
    disabled: bool,
    volatility_by_symbol: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "rank": rank,
        "symbol": item.symbol,
        "score": item.score,
        "volume_score": item.volume_score,
        "depth_score": item.depth_score,
        "volume_24h": item.volume_24h,
        "depth_usdt": item.depth_usdt,
        "bid_price": item.bid_price,
        "ask_price": item.ask_price,
        "spread_pct": item.spread_pct,
        "selected": selected,
        "disabled": disabled,
        "status": "ok",
        "error": "",
        **volatility_by_symbol.get(item.symbol, _empty_candidate_volatility()),
    }


def _session_volatility_by_symbol(repo: Repository) -> dict[str, dict[str, Any]]:
    rows = repo.console_sessions(active_only=False, limit=200)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if not symbol or symbol in result:
            continue
        result[symbol] = {
            "volatility_method": row.get("volatility_method"),
            "volatility_method_label": legacy_web._localize_scalar_text(str(row.get("volatility_method") or "")),
            "volatility_value": row.get("volatility_value"),
            "current_volatility": row.get("volatility_current_value"),
            "volatility_window": row.get("volatility_window"),
            "current_volatility_window": row.get("volatility_current_window"),
            "stage": legacy_web._localize_scalar_text(str(row.get("state") or "")),
        }
    return result


def _empty_candidate_volatility() -> dict[str, Any]:
    return {
        "volatility_method": None,
        "volatility_method_label": "",
        "volatility_value": None,
        "current_volatility": None,
        "volatility_window": None,
        "current_volatility_window": None,
        "stage": "等待波动计算",
    }


def _require_confirm(request: ConsoleActionRequest) -> None:
    if not request.confirm:
        raise HTTPException(status_code=400, detail="控制动作需要 confirm=true。")


def _control_state_payload(config: AppConfig, repo: Repository) -> dict[str, Any]:
    state = repo.get_control_state()
    pause_state = state.get("new_entries_paused")
    disabled_state = state.get("disabled_symbols")
    round_request = repo.round_start_request(include_terminal=True)
    runtime = repo.runtime_state()
    return {
        "new_entries_paused": bool(pause_state.get("value")) if isinstance(pause_state, dict) else False,
        "new_entries_paused_updated_at": pause_state.get("updated_at") if isinstance(pause_state, dict) else "",
        "disabled_symbols": sorted(repo.disabled_symbols()),
        "disabled_symbols_updated_at": disabled_state.get("updated_at") if isinstance(disabled_state, dict) else "",
        "startable_symbols": _configured_startable_symbols(config),
        "session_stop_requests": list(repo.session_stop_requests().values()),
        "session_control_requests": list(repo.session_control_requests().values()),
        "round_start_request": round_request,
        **runtime,
    }


def _configured_startable_symbols(config: AppConfig) -> list[str]:
    selection = config.raw.get("selection", {})
    blacklist = {str(symbol).strip().upper() for symbol in selection.get("symbol_blacklist", []) if str(symbol).strip()}
    symbols: list[str] = []
    seen: set[str] = set()
    for raw_symbol in selection.get("symbol_allowlist", []):
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in seen or symbol in blacklist:
            continue
        seen.add(symbol)
        symbols.append(symbol)
    return symbols


def _normalize_startable_symbol(config: AppConfig, symbol: str) -> str:
    normalized = str(symbol).strip().upper()
    if not normalized:
        raise HTTPException(status_code=422, detail="标的不能为空。")
    startable = set(_configured_startable_symbols(config))
    if normalized not in startable:
        raise HTTPException(status_code=422, detail=f"{normalized} 不在配置的可启动标的 allowlist 中。")
    return normalized


def _strategy_config_payload(config: AppConfig, repo: Repository) -> dict[str, Any]:
    state = repo.get_control_state()
    draft_state = state.get("strategy_config_draft")
    current = _current_strategy_config(config)
    draft = {**current, **(repo.strategy_config_draft() or {})}
    return {
        "current": current,
        "draft": draft,
        "diff": _strategy_config_diff(current, draft),
        "draft_updated_at": draft_state.get("updated_at") if isinstance(draft_state, dict) else "",
        "options": {
            "volatility_methods": _volatility_method_options(),
        },
    }


def _save_strategy_config_draft(
    config: AppConfig,
    repo: Repository,
    request: StrategyConfigDraftRequest,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    method = str(request.volatility_method).strip().lower()
    if method not in SUPPORTED_RANGE_METHODS:
        raise HTTPException(status_code=422, detail=f"不支持的波动率算法：{request.volatility_method}")
    current = _current_strategy_config(config)
    draft = {
        "volatility_method": method,
        "leverage": int(request.leverage),
        "capital_per_symbol": float(request.capital_per_symbol),
        "max_concurrent": int(request.max_concurrent),
        "scan_candidate_count": int(request.scan_candidate_count),
        "observe_hours": float(request.observe_hours),
        "observe_kline_interval": str(request.observe_kline_interval).strip(),
        "min_step_pct": float(request.min_step_pct),
        "min_tradable_range_pct": float(request.min_tradable_range_pct),
        "max_grid_num": int(request.max_grid_num),
        "stop_buffer_pct": float(request.stop_buffer_pct),
        "safety_multiplier": float(request.safety_multiplier),
    }
    if request.take_profit_usdt is not None:
        draft["take_profit_usdt"] = float(request.take_profit_usdt)
    else:
        draft["take_profit_usdt"] = float(current["take_profit_usdt"])
    if request.total_capital_limit is not None:
        draft["total_capital_limit"] = float(request.total_capital_limit)
    else:
        draft["total_capital_limit"] = float(current["total_capital_limit"])
    if request.max_maker_fee_rate is not None:
        draft["max_maker_fee_rate"] = float(request.max_maker_fee_rate)
    else:
        draft["max_maker_fee_rate"] = float(current["max_maker_fee_rate"])
    now = datetime.now(timezone.utc)
    before = repo.strategy_config_draft()
    repo.log_system(
        "INFO",
        "console_action",
        "Strategy config draft save requested.",
        _json_detail({"before": before, "draft": draft}),
        now,
    )
    repo.set_strategy_config_draft(draft, now)
    repo.log_system(
        "INFO",
        "console_action",
        "Strategy config draft saved.",
        _json_detail({"draft": draft, "diff": _strategy_config_diff(current, draft)}),
        datetime.now(timezone.utc),
    )
    payload = _strategy_config_payload(config, repo)
    return {
        "ok": True,
        "message": "策略参数草稿已保存，将在下一轮新建网格时生效。",
        **payload,
    }


def _current_strategy_config(config: AppConfig) -> dict[str, Any]:
    raw = config.raw
    trading = raw.get("trading", {})
    timing = raw.get("timing", {})
    grid = raw.get("grid", {})
    selection = raw.get("selection", {})
    return {
        "volatility_method": str(grid.get("range_method", "std")),
        "leverage": int(trading.get("leverage", 10)),
        "capital_per_symbol": float(trading.get("capital_per_symbol", 200)),
        "max_concurrent": int(trading.get("max_concurrent", 1)),
        "scan_candidate_count": int(selection.get("scan_candidate_count", 10)),
        "observe_hours": float(timing.get("observe_hours", 3)),
        "observe_kline_interval": str(timing.get("observe_kline_interval", "1m")),
        "min_step_pct": float(grid.get("min_step_pct", 0.0015)),
        "min_tradable_range_pct": float(grid.get("min_tradable_range_pct", 0.0015)),
        "max_grid_num": int(grid.get("max_grid_num", 20)),
        "stop_buffer_pct": float(trading.get("stop_buffer_pct", grid.get("stop_buffer_pct", 0.015))),
        "safety_multiplier": float(grid.get("safety_multiplier", 3.5)),
        "take_profit_usdt": float(trading.get("take_profit_usdt", 10)),
        "total_capital_limit": float(trading.get("total_capital_limit", 1000)),
        "max_maker_fee_rate": float(trading.get("max_maker_fee_rate", 0)),
    }


def _strategy_config_diff(current: dict[str, Any], draft: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "volatility_method": "波动率算法",
        "leverage": "杠杆倍数",
        "capital_per_symbol": "单标的本金",
        "max_concurrent": "最大并发标的",
        "scan_candidate_count": "流动性扫描标的数",
        "observe_hours": "观察窗口小时",
        "observe_kline_interval": "K线周期",
        "min_step_pct": "最小网格步长",
        "min_tradable_range_pct": "最小可交易波动区间",
        "max_grid_num": "最大网格数量",
        "stop_buffer_pct": "止损缓冲",
        "safety_multiplier": "资金费安全倍数",
        "take_profit_usdt": "单标的止盈",
        "total_capital_limit": "总资金上限",
        "max_maker_fee_rate": "Maker 费率上限",
    }
    diff: list[dict[str, Any]] = []
    for key, label in labels.items():
        current_value = current.get(key)
        draft_value = draft.get(key)
        if current_value == draft_value:
            continue
        diff.append(
            {
                "key": key,
                "label": label,
                "current": current_value,
                "draft": draft_value,
            }
        )
    return diff


def _volatility_method_options() -> list[dict[str, str]]:
    ordered = ["std", "parkinson", "garman_klass", "rogers_satchell", "yang_zhang", "quantile"]
    return [
        {"value": method, "label": legacy_web._localize_scalar_text(method)}
        for method in ordered
        if method in SUPPORTED_RANGE_METHODS
    ]


async def _run_bounded_run_console_action(
    ctx: AccountRequestContext,
    request: ConsoleActionRequest,
) -> dict[str, Any]:
    _require_confirm(request)
    seconds = float(request.loop_seconds or DEFAULT_BOUNDED_RUN_SECONDS)
    if seconds < 20:
        raise HTTPException(status_code=422, detail="运行秒数不能小于 20。")
    return await _run_console_action(
        ctx.repo,
        action="bounded_run",
        label="一键有界运行",
        request=request,
        runner=lambda: _run_bounded_run_action(ctx.config, seconds),
        extra_detail={"loop_seconds": seconds},
    )


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


def _set_new_entries_paused(config: AppConfig, repo: Repository, request: ConsoleActionRequest, paused: bool) -> dict[str, Any]:
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
        "control_state": _control_state_payload(config, repo),
    }


def _set_symbol_next_entry_disabled(
    config: AppConfig,
    repo: Repository,
    symbol: str,
    request: ConsoleActionRequest,
    disabled: bool,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise HTTPException(status_code=422, detail="标的不能为空。")
    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    action = "disable_symbol_next_entry" if disabled else "enable_symbol_next_entry"
    label = "禁用标的下一轮开仓" if disabled else "启用标的下一轮开仓"
    before = sorted(repo.disabled_symbols())
    detail = _action_detail(action, label, request, request_id, {"symbol": normalized_symbol})
    repo.log_system("INFO", "console_action", "Console action requested.", _json_detail(detail), now)
    after = repo.set_symbol_disabled(normalized_symbol, disabled, now)
    repo.log_system(
        "INFO",
        "console_action",
        "Console action completed.",
        _json_detail({**detail, "disabled_symbols_before": before, "disabled_symbols_after": after}),
        datetime.now(timezone.utc),
    )
    state_word = "禁用" if disabled else "启用"
    return {
        "ok": True,
        "action": action,
        "label": label,
        "request_id": request_id,
        "message": f"{normalized_symbol} 下一轮开仓已{state_word}。",
        "control_state": _control_state_payload(config, repo),
        "result": {
            "symbol": normalized_symbol,
            "disabled_symbols_before": before,
            "disabled_symbols_after": after,
        },
    }


def _request_session_stop(config: AppConfig, repo: Repository, session_id: int, request: ConsoleActionRequest) -> dict[str, Any]:
    return _request_session_close_control(
        config=config,
        repo=repo,
        session_id=session_id,
        request=request,
        action="session_stop",
        label="停止单个网格",
        request_type="stop",
        queued_message="交易循环处理该请求时会撤单并尝试同步平仓，完成后会写入会话 close_reason 与审计日志。",
        result_message_suffix="仓位确认等待交易循环处理。",
    )


def _request_session_manual_close(config: AppConfig, repo: Repository, session_id: int, request: ConsoleActionRequest) -> dict[str, Any]:
    return _request_session_close_control(
        config=config,
        repo=repo,
        session_id=session_id,
        request=request,
        action="session_manual_close",
        label="手动平仓",
        request_type="manual_close",
        queued_message="交易循环处理该请求时会立即撤销该会话挂单并同步平仓，完成后会写入手动平仓审计日志。",
        result_message_suffix="手动平仓确认等待交易循环处理。",
    )


def _request_session_control(
    config: AppConfig,
    repo: Repository,
    session_id: int,
    request: ConsoleActionRequest,
    action: str,
) -> dict[str, Any]:
    row = repo.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    state = str(row.get("state") or "").upper()
    if row.get("close_time") or state == "STOPPED":
        raise HTTPException(status_code=409, detail="会话已经停止，无法暂停或恢复。")
    if action == "pause" and state == "PAUSED":
        raise HTTPException(status_code=409, detail="会话已经暂停。")
    if action == "resume" and state != "PAUSED":
        raise HTTPException(status_code=409, detail="只有暂停中的会话可以恢复。")
    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    control_request = repo.request_session_control(
        session_id=session_id,
        symbol=str(row.get("symbol") or ""),
        action=action,
        reason=request.reason,
        request_id=request_id,
        requested_at=now,
    )
    label = "暂停单个网格" if action == "pause" else "恢复单个网格"
    repo.log_system("INFO", "console_action", "Session control requested.", _json_detail(control_request), now)
    return {
        "ok": True,
        "action": f"session_{action}",
        "label": label,
        "request_id": request_id,
        "message": f"{row.get('symbol')} {label}请求已记录，将由交易循环安全执行。",
        "control_state": _control_state_payload(config, repo),
        "result": control_request,
    }


def _request_session_close_control(
    config: AppConfig,
    repo: Repository,
    session_id: int,
    request: ConsoleActionRequest,
    action: str,
    label: str,
    request_type: str,
    queued_message: str,
    result_message_suffix: str,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    row = repo.get_session(session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    if row.get("close_time") or str(row.get("state") or "").upper() == "STOPPED":
        raise HTTPException(status_code=409, detail="会话已经停止，无需重复提交控制动作。")
    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    before = _session_control_snapshot(repo, session_id)
    detail = _action_detail(
        action,
        label,
        request,
        request_id,
        {"session_id": session_id, "symbol": row.get("symbol"), "request_type": request_type},
    )
    repo.log_system("WARN", "console_action", "Console action requested.", _json_detail(detail), now)
    stop_request = repo.request_session_stop(
        session_id=session_id,
        symbol=str(row.get("symbol") or ""),
        reason=request.reason,
        request_id=request_id,
        requested_at=now,
        request_type=request_type,
    )
    after = _session_control_snapshot(repo, session_id)
    result = {
        "before": before,
        "after": after,
        "stop_request": stop_request,
        "position_confirmation": {
            "status": "queued",
            "status_label": "等待交易循环确认",
            "message": queued_message,
        },
    }
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
        "message": (
            f"{row.get('symbol')} {label}请求已记录。"
            f"开放订单 {before['open_orders']} -> {after['open_orders']}，{result_message_suffix}"
        ),
        "control_state": _control_state_payload(config, repo),
        "result": result,
    }


def _request_all_sessions_stop(config: AppConfig, repo: Repository, request: ConsoleActionRequest) -> dict[str, Any]:
    from datetime import datetime, timezone

    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    action = "round_stop"
    label = "停止整轮网格"
    try:
        stop_request = repo.request_round_stop(request.reason, request_id, now)
    except RoundStartConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    rows = repo.console_sessions(active_only=True, limit=200, window_id=int(stop_request["window_id"]))
    detail = _action_detail(
        action,
        label,
        request,
        request_id,
        {"round_id": stop_request["window_id"], "session_count": len(rows)},
    )
    repo.log_system("WARN", "console_action", "Console action requested.", _json_detail(detail), now)
    result = {
        "round_stop_request": stop_request,
        "active_sessions": [
            {"session_id": int(row.get("id") or 0), "symbol": str(row.get("symbol") or "")}
            for row in rows
        ],
        "position_confirmation": {
            "status": "queued",
            "status_label": "等待交易循环确认",
            "message": "交易循环会停止本轮扫描，逐个撤单平仓并关闭轮次。",
        },
    }
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
        "message": f"第 {stop_request['window_id']} 轮停止请求已记录，仓位确认等待交易循环处理。",
        "control_state": _control_state_payload(config, repo),
        "result": result,
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


def _trader_process_status(config: AppConfig) -> dict[str, Any]:
    control = _process_control_config(config)
    mode = str(control.get("mode") or "auto").strip().lower()
    service = str(control.get("service") or "quietgrid-trader").strip() or "quietgrid-trader"
    if mode == "auto":
        mode = "systemd" if platform.system().lower() != "windows" else "unavailable"
    if mode == "command":
        status_command = _process_command(control, "status_command")
        if not status_command:
            return {
                "available": bool(_process_command(control, "stop_command") or _process_command(control, "restart_command")),
                "mode": "command",
                "service": service,
                "state": "unknown",
                "detail": "已配置 command 交易进程控制，但未配置 status_command。",
            }
        result = _run_process_command(status_command, timeout_seconds=_process_command_timeout(control))
        detail = (result.stdout or result.stderr).strip()
        state = _command_process_state(result.returncode, detail)
        return {
            "available": True,
            "mode": "command",
            "service": service,
            "state": state,
            "detail": detail,
            "returncode": result.returncode,
        }
    if mode != "systemd":
        return {
            "available": False,
            "mode": mode,
            "service": service,
            "state": "unavailable",
            "detail": "当前运行环境未配置 systemd 或 command 交易进程控制。",
        }
    result = _run_systemctl(["is-active", service])
    if result.returncode == 0:
        state = "running"
    elif str(result.stdout).strip() == "inactive":
        state = "stopped"
    else:
        state = "unknown"
    return {
        "available": True,
        "mode": "systemd",
        "service": service,
        "state": state,
        "detail": (result.stdout or result.stderr).strip(),
    }


def _run_trader_process_action(
    config: AppConfig,
    repo: Repository,
    request: ConsoleActionRequest,
    operation: str,
) -> dict[str, Any]:
    if operation not in {"stop", "restart"}:
        raise HTTPException(status_code=422, detail="不支持的交易进程控制动作。")
    from datetime import datetime, timezone

    before = _trader_process_status(config)
    if not before.get("available"):
        raise HTTPException(status_code=409, detail=str(before.get("detail") or "交易进程控制不可用。"))
    label = "停止交易 loop 进程" if operation == "stop" else "重启交易 loop 进程"
    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    detail = _action_detail("trader_loop_" + operation, label, request, request_id, {"before": before})
    repo.log_system("WARN", "console_action", "Console action requested.", _json_detail(detail), now)
    service = str(before.get("service") or "quietgrid-trader")
    if before.get("mode") == "command":
        command = _process_command(_process_control_config(config), f"{operation}_command")
        if not command:
            raise HTTPException(status_code=409, detail=f"未配置 {operation}_command，无法执行交易进程控制。")
        result = _run_process_command(command, timeout_seconds=_process_command_timeout(_process_control_config(config)))
    else:
        result = _run_systemctl([operation, service])
    after = _trader_process_status(config)
    payload = {
        "before": before,
        "after": after,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
    if result.returncode != 0:
        repo.log_system(
            "ERROR",
            "console_action",
            "Console action failed.",
            _json_detail({**detail, "result": payload}),
            datetime.now(timezone.utc),
        )
        raise HTTPException(status_code=500, detail=result.stderr.strip() or result.stdout.strip() or f"{label}失败。")
    repo.log_system(
        "INFO",
        "console_action",
        "Console action completed.",
        _json_detail({**detail, "result": payload}),
        datetime.now(timezone.utc),
    )
    return {
        "ok": True,
        "action": "trader_loop_" + operation,
        "label": label,
        "request_id": request_id,
        "message": f"{label}已提交，当前状态：{after.get('state')}。",
        "result": payload,
    }


def _process_control_config(config: AppConfig) -> dict[str, Any]:
    raw = config.raw.get("process_control", {})
    return raw if isinstance(raw, dict) else {}


def _run_systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _process_command(control: dict[str, Any], key: str) -> list[str]:
    raw = control.get(key)
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return shlex.split(raw, posix=platform.system().lower() != "windows")
    raise HTTPException(status_code=500, detail=f"process_control.{key} 必须是字符串或字符串列表。")


def _process_command_timeout(control: dict[str, Any]) -> float:
    try:
        return float(control.get("timeout_seconds", 15))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="process_control.timeout_seconds 必须是数字。") from exc


def _run_process_command(command: list[str], timeout_seconds: float = 15) -> subprocess.CompletedProcess[str]:
    if not command:
        raise HTTPException(status_code=500, detail="交易进程控制命令为空。")
    return subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )


def _command_process_state(returncode: int, detail: str) -> str:
    normalized = str(detail or "").strip().lower()
    if any(token in normalized for token in ("failed", "failure", "error")):
        return "failed"
    if any(token in normalized for token in ("inactive", "stopped", "stop pending", "not running")):
        return "stopped"
    if any(token in normalized for token in ("active", "running", "started", "start pending")):
        return "running"
    return "running" if returncode == 0 else "unknown"


async def _run_safety_sweep_action(config: AppConfig) -> dict[str, Any]:
    from trader import _run_binance_safety_sweep

    return await _run_binance_safety_sweep(config)


async def _run_bounded_run_action(config: AppConfig, seconds: float) -> dict[str, Any]:
    from trader import _run_binance_test_run

    return await _run_binance_test_run(config, max_seconds=seconds)


async def _run_symbol_bounded_run_action(config: AppConfig, symbol: str, seconds: float) -> dict[str, Any]:
    from trader import _run_binance_test_run

    single_symbol_config = _single_symbol_bounded_config(config, symbol)
    return await _run_binance_test_run(single_symbol_config, max_seconds=seconds)


def _single_symbol_bounded_config(config: AppConfig, symbol: str) -> AppConfig:
    raw = deepcopy(config.raw)
    raw.setdefault("selection", {})
    raw.setdefault("trading", {})
    raw["selection"]["symbol_allowlist"] = [symbol]
    raw["selection"]["symbol_blacklist"] = [
        item for item in raw["selection"].get("symbol_blacklist", []) if str(item).strip().upper() != symbol
    ]
    raw["trading"]["max_concurrent"] = 1
    return AppConfig(
        raw=raw,
        binance_api_key=config.binance_api_key,
        binance_api_secret=config.binance_api_secret,
        binance_testnet=config.binance_testnet,
        binance_testnet_raw=config.binance_testnet_raw,
        account_id=config.account_id,
        account_label=config.account_label,
        accounts=config.accounts,
    )


_run_testnet_run_action = _run_bounded_run_action
_run_symbol_testnet_run_action = _run_symbol_bounded_run_action
_single_symbol_testnet_config = _single_symbol_bounded_config


def _risk_level(latest_log: dict[str, Any] | None) -> str:
    if latest_log is None:
        return "无运行记录"
    level = str(latest_log.get("level") or "").upper()
    if level in {"ERROR", "CRITICAL"}:
        return "异常"
    if level in {"WARN", "WARNING"}:
        return "警告"
    return "正常"


def _session_payload(
    row: dict[str, Any],
    config: AppConfig,
    disabled_symbols: set[str] | None = None,
    stop_requests: dict[int, dict[str, Any]] | None = None,
    control_requests: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    session_id = int(row.get("id") or 0)
    stop_request = stop_requests.get(session_id) if stop_requests else None
    control_request = control_requests.get(session_id) if control_requests else None
    return {
        "id": session_id,
        "window_id": row.get("window_id"),
        "symbol": symbol,
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
        "next_entry_disabled": symbol.upper() in (disabled_symbols or set()),
        "stop_requested": stop_request is not None,
        "stop_request_status": stop_request.get("status") if stop_request else "",
        "stop_request_type": stop_request.get("request_type") if stop_request else "",
        "control_requested": control_request is not None,
        "control_request_status": control_request.get("status") if control_request else "",
        "control_request_action": control_request.get("action") if control_request else "",
        **_volatility_stage_payload(row, config),
    }


def _grid_round_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_id": int(row.get("window_id") or 0),
        "window_start": row.get("window_start"),
        "window_end": row.get("window_end"),
        "status": row.get("status"),
        "status_label": legacy_web._localize_scalar_text(str(row.get("status") or "")),
        "total_pnl": row.get("total_pnl"),
        "session_count": int(row.get("session_count") or 0),
        "active_session_count": int(row.get("active_session_count") or 0),
    }


def _round_candidate_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_id": int(row.get("window_id") or 0),
        "symbol": str(row.get("symbol") or ""),
        "rank": row.get("liquidity_rank"),
        "score": row.get("score"),
        "volume_score": row.get("volume_score"),
        "depth_score": row.get("depth_score"),
        "volume_24h": row.get("volume_24h"),
        "depth_usdt": row.get("depth_usdt"),
        "price": row.get("price"),
        "bid_price": row.get("bid_price"),
        "ask_price": row.get("ask_price"),
        "spread_pct": row.get("spread_pct"),
        "volatility_method": row.get("volatility_method"),
        "volatility_method_label": legacy_web._localize_scalar_text(str(row.get("volatility_method") or "")),
        "volatility_value": row.get("volatility_value"),
        "volatility_window": row.get("volatility_window"),
        "range_lower": row.get("range_lower"),
        "range_upper": row.get("range_upper"),
        "range_width_pct": row.get("range_width_pct"),
        "threshold_met": bool(row.get("threshold_met")),
        "selected": bool(row.get("session_id")),
        "disabled": False,
        "status": "stale" if row.get("data_stale") else "ok",
        "current_volatility": row.get("volatility_value"),
        "current_volatility_window": row.get("volatility_window"),
        "snapshot_at": row.get("calculated_at") or row.get("updated_at"),
        "session_id": row.get("session_id"),
        "stage": row.get("stage"),
        "error": row.get("error") or "",
        "last_kline_close_at": row.get("last_kline_close_at"),
        "market_updated_at": row.get("market_updated_at"),
        "calculated_at": row.get("calculated_at"),
        "data_stale": bool(row.get("data_stale")),
        "updated_at": row.get("updated_at"),
    }


def _volatility_stage_payload(row: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    state = str(row.get("state") or "").upper()
    if state == "OBSERVING":
        progress, remaining = _observation_progress(row.get("open_time"), config)
        stage = "observing" if remaining is None or remaining > 0 else "calculating"
        label = "正在观察/波动计算中" if stage == "observing" else "波动计算待完成"
    elif state in {"RUNNING", "COOLDOWN", "CLOSING", "PAUSED"}:
        stage = "trading"
        label = "网格已暂停，持仓风控仍在运行" if state == "PAUSED" else "计算结束，自动交易已启动"
        progress = 1.0
        remaining = 0
    elif state == "STOPPED":
        stage = "stopped"
        label = "已停止"
        progress = 1.0
        remaining = 0
    else:
        stage = "pending"
        label = "等待观察"
        progress = None
        remaining = None
    return {
        "volatility_stage": stage,
        "volatility_stage_label": label,
        "volatility_progress_pct": progress,
        "volatility_remaining_seconds": remaining,
    }


def _observation_progress(open_time: Any, config: AppConfig) -> tuple[float | None, int | None]:
    try:
        total_seconds = float(config.raw.get("timing", {}).get("observe_hours", 3)) * 3600
    except (TypeError, ValueError):
        total_seconds = 3 * 3600
    if total_seconds <= 0:
        return 1.0, 0
    try:
        opened_at = datetime.fromisoformat(str(open_time))
    except (TypeError, ValueError):
        return None, None
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    elapsed = max(0.0, (datetime.now(timezone.utc) - opened_at.astimezone(timezone.utc)).total_seconds())
    remaining = max(0, int(total_seconds - elapsed))
    return min(1.0, elapsed / total_seconds), remaining


def _session_control_snapshot(repo: Repository, session_id: int) -> dict[str, Any]:
    row = repo.get_session(session_id)
    if row is None:
        return {
            "session_id": session_id,
            "state": "NOT_FOUND",
            "state_label": "未找到",
            "open_orders": 0,
            "orders_by_status": {},
        }
    orders = repo.console_orders(session_id=session_id, limit=300)
    orders_by_status: dict[str, int] = {}
    for order in orders:
        status = str(order.get("status") or "")
        orders_by_status[status] = orders_by_status.get(status, 0) + 1
    open_orders = orders_by_status.get("open", 0)
    return {
        "session_id": session_id,
        "symbol": row.get("symbol"),
        "state": row.get("state"),
        "state_label": legacy_web._localize_scalar_text(str(row.get("state") or "")),
        "open_orders": open_orders,
        "orders_by_status": orders_by_status,
        "close_time": row.get("close_time"),
        "close_reason": row.get("close_reason"),
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


def _session_performance_payload(row: dict[str, Any], trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: (str(item.get("trade_time") or ""), int(item.get("id") or 0)))
    gross_grid_pnl = sum(float(item.get("grid_pnl") or 0.0) for item in ordered if item.get("grid_pnl") is not None)
    trading_fees = sum(float(item.get("fee") or 0.0) for item in ordered)
    funding_fee = sum(float(item.get("funding_fee") or 0.0) for item in ordered)
    realized_pnl = float(row.get("realized_pnl") or 0.0)
    unpaired_pnl = realized_pnl - gross_grid_pnl + trading_fees - funding_fee
    capital = float(row.get("capital") or 0.0)
    roi = realized_pnl / capital if capital > 0 else None
    initial_margin = capital
    current_margin = max(0.0, capital + realized_pnl) if capital > 0 else None
    margin_change = (current_margin - initial_margin) if current_margin is not None else None
    duration_hours = _session_duration_hours(row)
    annualized_roi = roi * (24 * 365 / duration_hours) if roi is not None and duration_hours and duration_hours > 0 else None

    cumulative = 0.0
    curve = []
    for item in ordered:
        cumulative += float(item.get("grid_pnl") or 0.0)
        cumulative -= float(item.get("fee") or 0.0)
        cumulative += float(item.get("funding_fee") or 0.0)
        curve.append(
            {
                "time": item.get("trade_time"),
                "value": cumulative,
            }
        )

    return {
        "gross_grid_pnl": gross_grid_pnl,
        "trading_fees": trading_fees,
        "funding_fee": funding_fee,
        "realized_pnl": realized_pnl,
        "unpaired_pnl": unpaired_pnl,
        "initial_margin": initial_margin,
        "current_margin": current_margin,
        "margin_change": margin_change,
        "roi": roi,
        "annualized_roi": annualized_roi,
        "duration_hours": duration_hours,
        "trade_count": len(ordered),
        "unpaired_trade_count": sum(1 for item in ordered if item.get("grid_pnl") is None),
        "pnl_curve": curve[-80:],
    }


def _session_duration_hours(row: dict[str, Any]) -> float | None:
    try:
        opened_at = datetime.fromisoformat(str(row.get("open_time")))
    except (TypeError, ValueError):
        return None
    close_value = row.get("close_time")
    if close_value:
        try:
            closed_at = datetime.fromisoformat(str(close_value))
        except (TypeError, ValueError):
            closed_at = datetime.now(timezone.utc)
    else:
        closed_at = datetime.now(timezone.utc)
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    return max(0.0, (closed_at.astimezone(timezone.utc) - opened_at.astimezone(timezone.utc)).total_seconds() / 3600)


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
        "name": row.get("verification_item") or legacy_web._ENVIRONMENT_VERIFICATION_LABELS.get(module, module),
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
