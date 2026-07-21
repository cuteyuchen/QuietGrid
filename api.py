from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import inspect
import platform
import secrets
import shlex
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

import web as legacy_web
from core.config import (
    AppConfig,
    _web_auth_token,
    load_config,
    select_account,
    validate_startup_config,
)
from data_sources import (
    BacktestWindow,
    BinanceArchiveHistoricalDataSource,
    BinanceHistoricalDataSource,
    DataSourceError,
    DatasetRequest,
    HistoricalDataSource,
    HistoricalDataSourceRegistry,
    HybridBinanceHistoricalDataSource,
    NyseWindowSlicer,
    normalized_klines_from_mappings,
)
from data_sources.models import FundingEvent
from data_sources.csv_source import INTERVAL_MILLISECONDS
from data_sources.dataset_service import BacktestDatasetService
from db.database import init_db
from db.repository import Repository, RoundStartConflict
from exchange.binance import BinanceFuturesClient
from strategy.grid_calculator import SUPPORTED_RANGE_METHODS
from strategy.selector import SelectionConfig, Selector

# 无状态辅助函数拆分包，重新导出以保持 `api._xxx` 访问方式不变。
from api_support.metrics import (
    _numbers_close,
    _numeric_quantile,
    _optional_float,
    _orders_refer_to_same_order,
    _ratio_sum,
    _series_cvar,
    _series_sharpe,
    _series_sortino,
)
from api_support.backtest_analysis import (
    _backtest_row_time,
    _backtest_window_distribution,
    _nyse_window_distribution,
    _window_analysis,
)
from api_support.payloads import (
    _exchange_order_payload,
    _grid_round_payload,
    _order_payload,
    _round_candidate_payload,
    _session_duration_hours,
    _session_performance_payload,
    _system_log_payload,
    _trade_payload,
    _verification_payload,
)


DEFAULT_BOUNDED_RUN_SECONDS = 60.0


@dataclass(frozen=True)
class AccountRequestContext:
    config: AppConfig
    repo: Repository


def create_app(config: AppConfig | None = None) -> FastAPI:
    app_config = config or load_config()
    validate_startup_config(app_config)
    for account in app_config.accounts:
        init_db(account.database_path)
        Repository(account.database_path, account_id=account.id).fail_interrupted_backtest_dataset_jobs()
    init_db(app_config.database_path)
    Repository(
        app_config.database_path,
        account_id=app_config.account_id,
    ).fail_interrupted_backtest_dataset_jobs()
    max_dataset_jobs = max(
        1,
        int(
            app_config.raw.get("backtest", {})
            .get("online_data", {})
            .get("max_concurrent_jobs", 1)
        ),
    )
    dataset_executor = ThreadPoolExecutor(
        max_workers=max_dataset_jobs,
        thread_name_prefix="quietgrid-dataset",
    )
    dataset_futures: set[Future[Any]] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            dataset_executor.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(
        title="QuietGrid Console API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
    )

    web_auth_token = _web_auth_token(app_config.raw.get("web", {}))

    @app.middleware("http")
    async def _enforce_auth_token(request: Request, call_next):
        # 配置了访问令牌时，除 CORS 预检外的所有请求都必须携带匹配令牌。
        if web_auth_token and request.method != "OPTIONS":
            provided = request.headers.get("x-auth-token") or ""
            if not provided:
                authorization = request.headers.get("authorization") or ""
                if authorization.lower().startswith("bearer "):
                    provided = authorization[7:].strip()
            if not secrets.compare_digest(provided, web_auth_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "缺少或无效的访问令牌。"},
                )
        return await call_next(request)

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
        return _trader_process_status(ctx.config, ctx.repo)

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

    @app.post("/api/actions/trader-loop/start")
    def action_start_trader_loop(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _run_trader_process_action(ctx.config, ctx.repo, request, "start")

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

    @app.post("/api/actions/auto-trading/start")
    def action_start_auto_trading(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _run_auto_trading_action(ctx.config, ctx.repo, request, enabled=True)

    @app.post("/api/actions/auto-trading/stop")
    def action_stop_auto_trading(
        request: ConsoleActionRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _run_auto_trading_action(ctx.config, ctx.repo, request, enabled=False)

    @app.get("/api/v2/current-round")
    def current_round(ctx: AccountRequestContext = Depends(get_account_context)) -> dict[str, Any]:
        return _current_round_payload(ctx.config, ctx.repo)

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
            "data_health": _regime_data_health(ctx.config, latest_regime),
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
        frozen_items = [_v2_dataset_payload(item) for item in ctx.repo.backtest_datasets()]
        root = _legacy_backtest_dataset_root(ctx.config)
        if not root.exists():
            return {"items": frozen_items, "legacy_csv_items": []}
        dataset_root = _backtest_dataset_root(ctx.config)
        frozen_paths = {
            (dataset_root / str(item.get("file_path") or "")).resolve()
            for item in ctx.repo.backtest_datasets()
            if item.get("file_path")
        }
        legacy_items = []
        for path in sorted(root.rglob("*.csv"), key=lambda item: item.name.lower()):
            relative_path = path.relative_to(root).as_posix()
            if path.resolve() in frozen_paths:
                continue
            stat = path.stat()
            legacy_items.append(
                {
                    "dataset_id": None,
                    "source_type": "LEGACY_CSV",
                    "name": path.name,
                    "relative_path": relative_path,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime,
                        tz=timezone.utc,
                    ).isoformat(),
                }
            )
        # items 暂时保留旧 CSV，保证 v2.0 前端与第三方客户端无缝迁移。
        return {
            "items": [*frozen_items, *legacy_items],
            "legacy_csv_items": legacy_items,
        }

    @app.get("/api/v2/backtest-data/providers")
    def v2_backtest_data_providers() -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "binance_hybrid",
                    "label": "Binance 官方归档（推荐）",
                    "market": "USDS_M",
                    "intervals": ["1m", "5m", "15m", "1h"],
                    "price_types": ["CONTRACT"],
                    "recommended": True,
                    "description": "官方月度/每日归档为主，仅最新尾部使用 REST 补齐。",
                },
                {
                    "id": "binance_archive",
                    "label": "Binance 官方归档（仅归档）",
                    "market": "USDS_M",
                    "intervals": ["1m", "5m", "15m", "1h"],
                    "price_types": ["CONTRACT"],
                    "description": "仅使用官方归档 ZIP，不含尚未归档的最新尾部。",
                },
                {
                    "id": "binance_rest",
                    "label": "Binance REST（仅最新数据）",
                    "market": "USDS_M",
                    "intervals": ["1m", "5m", "15m", "1h"],
                    "price_types": ["CONTRACT"],
                    "description": "REST 分页，易受地区限制与限流影响，建议仅取最新尾部。",
                },
            ]
        }

    @app.get("/api/v2/backtest-data/providers/binance/symbols")
    async def v2_backtest_data_binance_symbols(
        query: str = Query("", max_length=32),
        market: str = Query("usds_m", pattern=r"^usds_m$"),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        source = _build_historical_data_source(ctx.config, "binance")
        try:
            items = await source.list_symbols(query)
            return {"items": [item.__dict__ for item in items], "market": market.upper()}
        except DataSourceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            await _close_historical_data_source(source)

    @app.post("/api/v2/backtest-data/preview")
    async def v2_backtest_data_preview(
        request: V2BacktestDatasetRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        service = _backtest_dataset_service(ctx.config, ctx.repo)
        try:
            preview = await service.preview(request.to_domain())
            return _dataset_preview_payload(preview)
        except (DataSourceError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post(
        "/api/v2/backtest-data/jobs",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def v2_create_backtest_data_job(
        request: V2BacktestDatasetRequest,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        domain_request = request.to_domain()
        service = _backtest_dataset_service(ctx.config, ctx.repo)
        try:
            job = await service.create_job(domain_request)
        except (DataSourceError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if job.get("status") != "READY":
            future = dataset_executor.submit(
                _run_backtest_dataset_job,
                ctx.config,
                str(job["job_id"]),
                domain_request,
            )
            dataset_futures.add(future)
            future.add_done_callback(dataset_futures.discard)
        return _dataset_job_payload(job)

    @app.post("/api/v2/backtest-data/upload", status_code=status.HTTP_201_CREATED)
    async def v2_upload_backtest_data(
        http_request: Request,
        file_name: str = Query(..., min_length=5, max_length=180),
        symbol: str = Query(..., min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$"),
        interval: str = Query("1m", pattern=r"^(1m|5m|15m|1h)$"),
        window_mode: str = Query(
            "NYSE_CLOSED_ONLY",
            pattern=r"^NYSE_CLOSED_ONLY$",
        ),
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        if Path(file_name).suffix.lower() != ".csv":
            raise HTTPException(status_code=422, detail="仅支持 .csv 文件。")
        max_bytes = 25 * 1024 * 1024
        staging_root = _backtest_staging_root(ctx.config)
        staging_root.mkdir(parents=True, exist_ok=True)
        upload_path = staging_root / f"upload_raw_{uuid4().hex}.csv"
        written = 0
        try:
            with upload_path.open("wb") as fh:
                async for chunk in http_request.stream():
                    written += len(chunk)
                    if written > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="上传 CSV 不能超过 25 MB。",
                        )
                    fh.write(chunk)
            if written == 0:
                raise HTTPException(status_code=422, detail="上传 CSV 为空。")
            service = _backtest_dataset_service(ctx.config, ctx.repo)
            dataset = await service.import_csv(
                upload_path,
                symbol=symbol,
                interval=interval,
                window_mode=window_mode,
            )
            ctx.repo.append_audit_log(
                actor="console",
                action="UPLOAD_BACKTEST_DATASET",
                resource_type="BACKTEST_DATASET",
                resource_id=str(dataset.get("dataset_id") or ""),
                detail={"file_name": Path(file_name).name, "size_bytes": written},
                created_at=datetime.now(timezone.utc),
            )
            return _v2_dataset_payload(dataset)
        except HTTPException:
            raise
        except (DataSourceError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            if upload_path.exists():
                upload_path.unlink()

    @app.get("/api/v2/backtest-data/jobs/{job_id}")
    def v2_backtest_data_job(
        job_id: str,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        job = ctx.repo.get_backtest_dataset_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="数据集任务不存在。")
        return _dataset_job_payload(job)

    @app.post("/api/v2/backtest-data/jobs/{job_id}/cancel")
    def v2_cancel_backtest_data_job(
        job_id: str,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        if not ctx.repo.request_backtest_dataset_job_cancel(job_id):
            job = ctx.repo.get_backtest_dataset_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="数据集任务不存在。")
            raise HTTPException(status_code=409, detail="当前任务状态不能取消。")
        ctx.repo.append_audit_log(
            actor="console",
            action="CANCEL_BACKTEST_DATASET_JOB",
            resource_type="BACKTEST_DATASET_JOB",
            resource_id=job_id,
            detail={"cancel_requested": True},
            created_at=datetime.now(timezone.utc),
        )
        return {"job_id": job_id, "cancel_requested": True}

    @app.get("/api/v2/backtests/datasets/{dataset_id}")
    def v2_backtest_dataset_detail(
        dataset_id: str,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        dataset = ctx.repo.get_backtest_dataset(dataset_id)
        if dataset is None:
            raise HTTPException(status_code=404, detail="冻结回测数据集不存在。")
        payload = _v2_dataset_payload(dataset)
        payload["windows"] = ctx.repo.backtest_dataset_windows(dataset_id)
        return payload

    @app.delete("/api/v2/backtests/datasets/{dataset_id}")
    def v2_delete_backtest_dataset(
        dataset_id: str,
        ctx: AccountRequestContext = Depends(get_account_context),
    ) -> dict[str, Any]:
        try:
            deleted = ctx.repo.soft_delete_backtest_dataset(dataset_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="冻结回测数据集不存在。")
        ctx.repo.append_audit_log(
            actor="console",
            action="DELETE_BACKTEST_DATASET",
            resource_type="BACKTEST_DATASET",
            resource_id=dataset_id,
            detail={"soft_delete": True},
            created_at=datetime.now(timezone.utc),
        )
        return {"dataset_id": dataset_id, "deleted": True}

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
        dataset_metadata: dict[str, Any] = {}
        funding_events: list[FundingEvent] | None = None
        if request.dataset_id is not None:
            service = _backtest_dataset_service(ctx.config, ctx.repo)
            try:
                dataset_path, dataset_metadata = service.resolve(request.dataset_id)
            except DataSourceError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if request.include_funding:
                if not dataset_metadata.get("has_funding"):
                    raise HTTPException(
                        status_code=422,
                        detail="该数据集未冻结历史资金费，无法启用事件化资金费回测。",
                    )
                try:
                    funding_events = service.load_funding_events(request.dataset_id)
                except DataSourceError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            dataset_symbol = str(dataset_metadata.get("symbol") or "").strip().upper()
            requested_symbol = request.symbol.strip().upper()
            if dataset_symbol and dataset_symbol != requested_symbol:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"回测标的 {requested_symbol} 与冻结数据集标的 "
                        f"{dataset_symbol} 不一致，请重新选择数据集或标的。"
                    ),
                )
        else:
            if request.include_funding:
                raise HTTPException(
                    status_code=422,
                    detail="事件化资金费仅支持带资金费的冻结数据集（dataset_id）。",
                )
            dataset_path = _resolve_backtest_dataset(ctx.config, str(request.dataset))
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
            dataset_id=str(dataset_metadata.get("dataset_id") or "") or None,
            dataset_checksum=str(dataset_metadata.get("checksum") or "") or None,
            data_provider=str(dataset_metadata.get("provider") or "LEGACY_CSV"),
            window_mode="NYSE_CLOSED_ONLY",
            dataset_schema_version=(
                int(dataset_metadata["schema_version"])
                if dataset_metadata.get("schema_version") is not None
                else None
            ),
            window_count=(
                int(dataset_metadata["window_count"])
                if dataset_metadata.get("window_count") is not None
                else None
            ),
        )
        try:
            summary, rows = await asyncio.to_thread(
                _execute_v2_backtest,
                ctx.config,
                request,
                dataset_path,
                report_path,
                dataset_metadata,
                funding_events,
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


def _regime_data_health(
    config: AppConfig,
    latest_regime: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> str:
    if not latest_regime:
        return "WAITING"
    raw_time = latest_regime.get("as_of_time")
    if not raw_time:
        return "STALE"
    try:
        as_of = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
    except ValueError:
        return "STALE"
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    regime_config = config.raw.get("regime", {}) if isinstance(config.raw, dict) else {}
    max_age_seconds = float(regime_config.get("max_data_age_seconds", 90.0))
    age_seconds = ((now or datetime.now(timezone.utc)) - as_of.astimezone(timezone.utc)).total_seconds()
    return "HEALTHY" if 0 <= age_seconds <= max_age_seconds else "STALE"


class ConsoleActionRequest(BaseModel):
    confirm: bool = False
    reason: str = Field(default="控制台手动操作", min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=80)
    loop_seconds: float | None = Field(default=None, ge=20, le=86400)


class StrategyConfigDraftRequest(BaseModel):
    direction_mode: str = Field(default="NEUTRAL", pattern=r"^(LONG|SHORT|NEUTRAL)$")
    direction_overrides: dict[str, str] = Field(default_factory=dict)
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


class V2BacktestDatasetRequest(BaseModel):
    provider: str = Field(
        default="binance_hybrid",
        pattern=r"^(binance_hybrid|binance_archive|binance_rest|binance)$",
    )
    symbol: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    interval: str = Field(default="1m", pattern=r"^(1m|5m|15m|1h)$")
    price_type: str = Field(default="CONTRACT", pattern=r"^CONTRACT$")
    start_time: datetime
    end_time: datetime
    window_mode: str = Field(
        default="NYSE_CLOSED_ONLY",
        pattern=r"^NYSE_CLOSED_ONLY$",
    )

    def to_domain(self) -> DatasetRequest:
        return DatasetRequest(
            provider=self.provider,
            symbol=self.symbol,
            interval=self.interval,
            start_time=self.start_time,
            end_time=self.end_time,
            window_mode=self.window_mode,
        )


class V2BacktestRunRequest(BaseModel):
    dataset: str | None = Field(default=None, min_length=1, max_length=240)
    dataset_id: str | None = Field(default=None, min_length=8, max_length=240)
    symbol: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    observe_rows: int = Field(default=180, ge=30, le=100000)
    capital: float = Field(default=500.0, gt=0, le=1000000)
    leverage: float = Field(default=1.0, ge=1.0, le=2.0)
    maker_fee_rate: float = Field(default=0.0, ge=0.0, le=0.01)
    fill_model: str = Field(default="L0_CONSERVATIVE", pattern=r"^L0_CONSERVATIVE$")
    maker_fill_probability: float = Field(default=0.65, ge=0.0, le=1.0)
    max_fills_per_bar: int = Field(default=2, ge=1, le=20)
    taker_fee_rate: float = Field(default=0.0005, ge=0.0, le=0.02)
    stop_slippage_bps: float = Field(default=10.0, ge=0.0, le=1000.0)
    direction_mode: str = Field(default="NEUTRAL", pattern=r"^(LONG|SHORT|NEUTRAL)$")
    seed_slippage_bps: float = Field(default=10.0, ge=0.0, le=20.0)
    funding_rate_per_bar: float = Field(default=0.0, ge=-0.01, le=0.01)
    include_funding: bool = False
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

    @model_validator(mode="after")
    def validate_dataset_reference(self) -> "V2BacktestRunRequest":
        if (self.dataset is None) == (self.dataset_id is None):
            raise ValueError("dataset 与 dataset_id 必须且只能提供一个。")
        return self


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


def _legacy_backtest_dataset_root(config: AppConfig) -> Path:
    backtest = config.raw.get("backtest", {})
    raw_path = str(
        backtest.get("legacy_dataset_dir", backtest.get("dataset_dir", "data"))
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


def _backtest_staging_root(config: AppConfig) -> Path:
    raw_path = str(
        config.raw.get("backtest", {}).get(
            "staging_dir",
            "data/backtests/staging",
        )
    ).strip()
    root = Path(raw_path)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent / root
    return root.resolve()


def _build_historical_data_source(
    config: AppConfig,
    provider: str,
) -> HistoricalDataSource:
    backtest = config.raw.get("backtest", {})
    online = backtest.get("online_data", {})
    if not bool(online.get("enabled", True)):
        raise DataSourceError("在线历史数据下载已在配置中关闭。")
    providers_config = backtest.get("providers", {})
    proxy_config = config.raw.get("proxy")

    def _rest_config() -> dict[str, Any]:
        return providers_config.get("binance_rest", {})

    def _archive_config() -> dict[str, Any]:
        return providers_config.get("binance_archive", {})

    def _hybrid_config() -> dict[str, Any]:
        return providers_config.get("binance_hybrid", {})

    def _build_rest(
        *, validate_symbol_listing: bool = True, provider_id: str = "binance_rest"
    ) -> BinanceHistoricalDataSource:
        rest = _rest_config()
        return BinanceHistoricalDataSource(
            proxy_config=proxy_config,
            base_url=str(rest.get("base_url") or "https://fapi.binance.com"),
            provider_id=provider_id,
            validate_symbol_listing=validate_symbol_listing,
            timeout_seconds=float(online.get("request_timeout_seconds", 15.0)),
            retry_attempts=int(online.get("retry_attempts", 3)),
            retry_backoff_seconds=float(online.get("retry_backoff_seconds", 0.5)),
            page_limit=int(online.get("page_limit", 1500)),
            pause_seconds=float(online.get("page_pause_seconds", 0.05)),
        )

    def _build_archive(
        *, provider_id: str = "binance_archive"
    ) -> BinanceArchiveHistoricalDataSource:
        archive = _archive_config()
        return BinanceArchiveHistoricalDataSource(
            base_url=str(archive.get("base_url") or "https://data.binance.vision"),
            market_path=str(archive.get("market_path") or "futures/um"),
            prefer_monthly=bool(archive.get("prefer_monthly", True)),
            verify_official_checksum=bool(archive.get("verify_official_checksum", True)),
            max_uncompressed_bytes=int(
                archive.get("max_uncompressed_bytes", 2 * 1024 * 1024 * 1024)
            ),
            proxy_config=proxy_config,
            timeout_seconds=float(online.get("request_timeout_seconds", 30.0)),
            retry_attempts=int(online.get("retry_attempts", 3)),
            retry_backoff_seconds=float(online.get("retry_backoff_seconds", 0.5)),
            pause_seconds=float(online.get("page_pause_seconds", 0.02)),
            provider_id=provider_id,
        )

    def _build_hybrid() -> HybridBinanceHistoricalDataSource:
        hybrid = _hybrid_config()
        return HybridBinanceHistoricalDataSource(
            archive_source=_build_archive(),
            # Hybrid 中的 REST 只补最新尾部，历史标的不强制当前 TRADING。
            rest_source=_build_rest(validate_symbol_listing=False),
            tolerate_missing_latest_tail=bool(
                hybrid.get("tolerate_missing_latest_tail", True)
            ),
        )

    registry = HistoricalDataSourceRegistry()
    registry.register("binance_rest", _build_rest)
    registry.register("binance_archive", _build_archive)
    registry.register("binance_hybrid", _build_hybrid)
    # 兼容旧 provider 名 "binance"：映射到归档优先的 Hybrid 主链路。
    registry.register(
        "binance",
        lambda: _build_hybrid_as("binance"),
    )

    def _build_hybrid_as(provider_id: str) -> HistoricalDataSource:
        source = _build_hybrid()
        source.provider_id = provider_id
        return source

    return registry.create(provider)


def _backtest_dataset_service(
    config: AppConfig,
    repo: Repository | None = None,
) -> BacktestDatasetService:
    backtest = config.raw.get("backtest", {})
    online = backtest.get("online_data", {})
    return BacktestDatasetService(
        repo=repo or Repository(config.database_path, account_id=config.account_id),
        dataset_root=_backtest_dataset_root(config),
        staging_root=_backtest_staging_root(config),
        source_factory=lambda provider: _build_historical_data_source(config, provider),
        validation_config=backtest.get("validation", {}),
        windowing_config=backtest.get("windowing", {}),
        max_range_days_1m=int(online.get("max_range_days_1m", 180)),
    )


def _run_backtest_dataset_job(
    config: AppConfig,
    job_id: str,
    request: DatasetRequest,
) -> None:
    asyncio.run(_backtest_dataset_service(config).run_job(job_id, request))


async def _close_historical_data_source(source: HistoricalDataSource) -> None:
    close = getattr(source, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _dataset_preview_payload(preview: Any) -> dict[str, Any]:
    return {
        "provider": preview.provider,
        "symbol": preview.symbol,
        "interval": preview.interval,
        "start_time": preview.start_time.isoformat(),
        "end_time": preview.end_time.isoformat(),
        "estimated_rows": int(preview.estimated_rows),
        "estimated_pages": int(preview.estimated_pages),
        "estimated_size_bytes": int(preview.estimated_size_bytes),
        "cache_hit": bool(preview.cache_hit),
        "window_count": preview.window_count,
        "warnings": list(preview.warnings),
    }


def _dataset_job_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(job.get("job_id") or ""),
        "dataset_id": job.get("dataset_id"),
        "provider": str(job.get("provider") or ""),
        "symbol": str(job.get("symbol") or ""),
        "interval": str(job.get("interval") or ""),
        "requested_start": str(job.get("requested_start") or ""),
        "requested_end": str(job.get("requested_end") or ""),
        "window_mode": str(job.get("window_mode") or ""),
        "status": str(job.get("status") or "UNKNOWN"),
        "stage": str(job.get("stage") or ""),
        "progress": float(job.get("progress") or 0.0),
        "current_page": int(job.get("current_page") or 0),
        "total_pages": int(job.get("total_pages") or 0),
        "downloaded_rows": int(job.get("downloaded_rows") or 0),
        "cancel_requested": bool(job.get("cancel_requested")),
        "error": job.get("error"),
        "created_at": str(job.get("created_at") or ""),
        "started_at": str(job.get("started_at") or ""),
        "completed_at": str(job.get("completed_at") or ""),
        "updated_at": str(job.get("updated_at") or ""),
    }


def _v2_dataset_payload(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "dataset_id": str(dataset.get("dataset_id") or ""),
        "source_type": "FROZEN_DATASET",
        "provider": str(dataset.get("provider") or ""),
        "market": str(dataset.get("market") or ""),
        "symbol": str(dataset.get("symbol") or ""),
        "interval": str(dataset.get("interval") or ""),
        "price_type": str(dataset.get("price_type") or "CONTRACT"),
        "requested_start": str(dataset.get("requested_start") or ""),
        "requested_end": str(dataset.get("requested_end") or ""),
        "actual_start": str(dataset.get("actual_start") or ""),
        "actual_end": str(dataset.get("actual_end") or ""),
        "row_count": int(dataset.get("row_count") or 0),
        "file_format": str(dataset.get("file_format") or ""),
        "file_path": str(dataset.get("file_path") or ""),
        "checksum": str(dataset.get("checksum") or ""),
        "schema_version": int(dataset.get("schema_version") or 0),
        "quality_status": str(dataset.get("quality_status") or ""),
        "quality_report": (
            dataset.get("quality_report")
            if isinstance(dataset.get("quality_report"), dict)
            else {}
        ),
        "window_mode": str(dataset.get("window_mode") or ""),
        "window_count": dataset.get("window_count"),
        "raw_window_count": dataset.get("raw_window_count"),
        "eligible_window_count": dataset.get("eligible_window_count"),
        "skipped_window_count": dataset.get("skipped_window_count"),
        "source_segments": _decode_source_segments(dataset.get("source_segments_json")),
        "status": str(dataset.get("status") or ""),
        "error": dataset.get("error"),
        "created_at": str(dataset.get("created_at") or ""),
        "updated_at": str(dataset.get("updated_at") or ""),
    }


def _decode_source_segments(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _resolve_backtest_dataset(config: AppConfig, dataset: str) -> Path:
    root = _legacy_backtest_dataset_root(config)
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
    dataset_metadata: dict[str, Any] | None = None,
    funding_events: list[FundingEvent] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from trader import _read_backtest_csv

    raw = deepcopy(config.raw)
    raw.setdefault("trading", {}).update(
        {
            "capital_per_symbol": request.capital,
            "leverage": request.leverage,
            "max_maker_fee_rate": request.maker_fee_rate,
            "direction_mode": request.direction_mode,
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
            "seed_slippage_bps": request.seed_slippage_bps,
            "funding_rate_per_bar": request.funding_rate_per_bar,
            "walk_forward_test_rows": request.walk_forward_test_rows,
            "monte_carlo_simulations": request.monte_carlo_simulations,
            "monte_carlo_missing_fill_probability": (
                request.monte_carlo_missing_fill_probability
            ),
            "monte_carlo_loss_multiplier": request.monte_carlo_loss_multiplier,
        }
    )
    dataset_metadata = dataset_metadata or {}
    window_mode = "NYSE_CLOSED_ONLY"
    raw["backtest"]["force_close_at_end"] = True
    runtime_config = SimpleNamespace(raw=raw)
    rows = _read_backtest_csv(dataset_path)
    interval = str(dataset_metadata.get("interval") or "1m")
    try:
        interval_ms = INTERVAL_MILLISECONDS[interval]
    except KeyError as exc:
        raise RuntimeError(f"数据集周期不支持休市窗口切分: {interval}") from exc
    windowing = raw.get("backtest", {}).get("windowing", {})
    slicer = NyseWindowSlicer(
        force_close_minutes=int(windowing.get("force_close_minutes", 120)),
        minimum_tradable_rows=int(windowing.get("minimum_tradable_rows", 30)),
    )
    normalized_rows = normalized_klines_from_mappings(
        rows,
        interval_ms=interval_ms,
    )
    windows = slicer.slice(normalized_rows, request.observe_rows)
    summary, report, window_backtests = _run_nyse_window_backtests(
        runtime_config,
        request,
        windows,
        funding_events,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    validation_rows = [
        row.to_mapping()
        for window in windows
        if window.status == "READY"
        for row in window.rows
    ]
    effective_dataset_metadata = {
        **dataset_metadata,
        "window_mode": window_mode,
        "window_count": len(windows),
    }
    summary["funding_mode"] = "EVENT" if funding_events is not None else "PER_BAR"
    _append_v2_backtest_validation(
        runtime_config,
        request,
        validation_rows,
        report_path,
        effective_dataset_metadata,
        window_backtests=window_backtests,
        funding_events=funding_events,
    )
    return summary, validation_rows


def _run_nyse_window_backtests(
    runtime_config: SimpleNamespace,
    request: "V2BacktestRunRequest",
    windows: list[BacktestWindow],
    funding_events: list[FundingEvent] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    from strategy.backtest import (
        backtest_config_from_mapping,
        run_grid_backtest,
        slice_funding_events_for_klines,
    )
    from strategy.grid_calculator import calculate_grid_params
    from trader import _backtest_report, _backtest_summary, _grid_config_from_raw

    if not windows:
        raise RuntimeError("所选数据范围没有覆盖 NYSE 休市窗口。")
    grid_config = _grid_config_from_raw(runtime_config.raw)
    run_config = backtest_config_from_mapping(runtime_config.raw)
    entries: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    for window in windows:
        metadata = window.to_metadata()
        if window.status != "READY":
            entries.append({"window": metadata, "status": "SKIPPED"})
            continue
        mappings = [row.to_mapping() for row in window.rows]
        observe = mappings[: request.observe_rows]
        test = mappings[request.observe_rows :]
        try:
            current_price = float(observe[-1]["close"])
            params = calculate_grid_params(
                symbol=request.symbol.upper(),
                klines=observe,
                current_price=current_price,
                funding_rate=0.0,
                config=grid_config,
            )
            window_funding = (
                slice_funding_events_for_klines(funding_events, test)
                if funding_events is not None
                else None
            )
            result = run_grid_backtest(
                params,
                test,
                current_price=current_price,
                config=run_config,
                funding_events=window_funding,
            )
            summary = _backtest_summary(result, request.observe_rows, len(test))
            report = _backtest_report(result, params, summary)
            entry = {
                "window": metadata,
                "status": "COMPLETED",
                "summary": summary,
                "report": report,
            }
            completed.append(entry)
            entries.append(entry)
        except Exception as exc:
            entries.append(
                {
                    "window": metadata,
                    "status": "FAILED",
                    "error": str(exc),
                }
            )
    if not completed:
        reasons = [
            item.get("window", {}).get("warning") or item.get("error")
            for item in entries
        ]
        message = "；".join(str(reason) for reason in reasons if reason)
        raise RuntimeError(message or "没有可执行的 NYSE 休市回测窗口。")
    summary, report = _aggregate_window_backtests(entries, completed)
    return summary, report, entries


def _aggregate_window_backtests(
    entries: list[dict[str, Any]],
    completed: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    fills: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    window_summaries: list[dict[str, Any]] = []
    cumulative_pnl = 0.0
    max_equity = 0.0
    max_drawdown = 0.0
    global_bar_index = 0
    for entry in completed:
        window = entry["window"]
        summary = entry["summary"]
        report = entry["report"]
        base_pnl = cumulative_pnl
        for fill in report.get("fills", []):
            item = dict(fill)
            item["window_id"] = window["window_id"]
            item["window_bar_index"] = item.get("bar_index")
            item["bar_index"] = global_bar_index + int(item.get("bar_index") or 0)
            item["realized_pnl_after"] = base_pnl + float(
                item.get("realized_pnl_after") or 0.0
            )
            fills.append(item)
        local_curve = report.get("equity_curve", [])
        for point in local_curve:
            item = dict(point)
            equity = base_pnl + float(item.get("equity") or 0.0)
            max_equity = max(max_equity, equity)
            drawdown = max(0.0, max_equity - equity)
            max_drawdown = max(max_drawdown, drawdown)
            item.update(
                {
                    "window_id": window["window_id"],
                    "window_bar_index": item.get("bar_index"),
                    "bar_index": global_bar_index + int(item.get("bar_index") or 0),
                    "equity": equity,
                    "realized_pnl": base_pnl
                    + float(item.get("realized_pnl") or 0.0),
                    "drawdown": drawdown,
                }
            )
            equity_curve.append(item)
        cumulative_pnl += float(summary.get("total_pnl") or 0.0)
        global_bar_index += len(local_curve)
        window_summaries.append(
            {
                **window,
                "status": "COMPLETED",
                "total_pnl": float(summary.get("total_pnl") or 0.0),
                "max_drawdown": float(summary.get("max_drawdown") or 0.0),
                "fills": int(summary.get("fills") or 0),
                "stopped_reason": summary.get("stopped_reason"),
                "max_inventory_utilization": float(
                    summary.get("max_inventory_utilization") or 0.0
                ),
            }
        )
    for entry in entries:
        if entry.get("status") == "COMPLETED":
            continue
        window_summaries.append(
            {
                **entry.get("window", {}),
                "status": entry.get("status"),
                "error": entry.get("error"),
            }
        )
    window_summaries.sort(key=lambda item: str(item.get("market_close") or ""))
    summary = _aggregate_window_summary(completed, fills, equity_curve, max_drawdown)
    report = {
        "summary": summary,
        "grid_params": completed[0]["report"].get("grid_params", {}),
        "window_grid_params": [
            {
                "window_id": entry["window"]["window_id"],
                **entry["report"].get("grid_params", {}),
            }
            for entry in completed
        ],
        "fills": fills,
        "equity_curve": equity_curve,
        "windows": window_summaries,
    }
    return summary, report


def _aggregate_window_summary(
    completed: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    max_drawdown: float,
) -> dict[str, Any]:
    summaries = [entry["summary"] for entry in completed]
    total_pnl = sum(float(item.get("total_pnl") or 0.0) for item in summaries)
    changes = [
        float(equity_curve[index].get("equity") or 0.0)
        - float(equity_curve[index - 1].get("equity") or 0.0)
        for index in range(1, len(equity_curve))
    ]
    closed_pnls = [
        float(fill["grid_pnl"])
        for fill in fills
        if fill.get("grid_pnl") is not None
    ]
    gross_profit = sum(value for value in closed_pnls if value > 0)
    gross_loss = abs(sum(value for value in closed_pnls if value < 0))
    inventory = [
        float(point.get("inventory_utilization") or 0.0)
        for point in equity_curve
    ]
    grid_trade_count = sum(int(item.get("grid_trade_count") or 0) for item in summaries)
    winning = sum(int(item.get("winning_grid_trades") or 0) for item in summaries)
    backtest_rows = sum(int(item.get("backtest_rows") or 0) for item in summaries)
    return {
        "symbol": str(summaries[0].get("symbol") or ""),
        "observe_rows": int(summaries[0].get("observe_rows") or 0),
        "backtest_rows": backtest_rows,
        "window_count": len(completed),
        "fills": len(fills),
        "fills_per_bar": len(fills) / backtest_rows if backtest_rows else 0.0,
        "grid_trade_count": grid_trade_count,
        "winning_grid_trades": winning,
        "losing_grid_trades": sum(
            int(item.get("losing_grid_trades") or 0) for item in summaries
        ),
        "break_even_grid_trades": sum(
            int(item.get("break_even_grid_trades") or 0) for item in summaries
        ),
        "win_rate": winning / grid_trade_count if grid_trade_count else 0.0,
        "avg_grid_pnl": sum(closed_pnls) / len(closed_pnls) if closed_pnls else 0.0,
        "gross_grid_pnl": sum(float(item.get("gross_grid_pnl") or 0.0) for item in summaries),
        "fees_paid": sum(float(item.get("fees_paid") or 0.0) for item in summaries),
        "realized_pnl": total_pnl,
        "unrealized_pnl": 0.0,
        "total_pnl": total_pnl,
        "max_equity": max((float(item.get("equity") or 0.0) for item in equity_curve), default=0.0),
        "max_drawdown": max_drawdown,
        "equity_sharpe": _series_sharpe(changes),
        "sortino": _series_sortino(changes),
        "calmar": total_pnl / max_drawdown if max_drawdown > 0 else 0.0,
        "cvar_95": _series_cvar(changes),
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else (gross_profit if gross_profit > 0 else 0.0)
        ),
        "grid_fill_ratio": _ratio_sum(summaries, "fills", "attempted_fill_count"),
        "pair_completion_ratio": _ratio_sum(summaries, "pair_completion_count", "fills"),
        "inventory_p50": _numeric_quantile(inventory, 0.50),
        "inventory_p95": _numeric_quantile(inventory, 0.95),
        "inventory_p99": _numeric_quantile(inventory, 0.99),
        "max_inventory_utilization": max(inventory, default=0.0),
        "attempted_fill_count": sum(int(item.get("attempted_fill_count") or 0) for item in summaries),
        "rejected_fill_count": sum(int(item.get("rejected_fill_count") or 0) for item in summaries),
        "pair_completion_count": sum(int(item.get("pair_completion_count") or 0) for item in summaries),
        "funding_paid": sum(float(item.get("funding_paid") or 0.0) for item in summaries),
        "stop_exit_cost": sum(float(item.get("stop_exit_cost") or 0.0) for item in summaries),
        "stop_exit_pnl": sum(float(item.get("stop_exit_pnl") or 0.0) for item in summaries),
        "net_position_qty": 0.0,
        "open_order_count": 0,
        "stopped_reason": "window_batch_completed",
        "stopped_at_index": None,
        "stopped_at_price": None,
        "last_price": float(summaries[-1].get("last_price") or 0.0),
    }


def _append_v2_backtest_validation(
    runtime_config: SimpleNamespace,
    request: "V2BacktestRunRequest",
    rows: list[dict[str, Any]],
    report_path: Path,
    dataset_metadata: dict[str, Any] | None = None,
    window_backtests: list[dict[str, Any]] | None = None,
    funding_events: list[FundingEvent] | None = None,
) -> None:
    from strategy.backtest import (
        backtest_config_from_mapping,
        run_grid_backtest,
        slice_funding_events_for_klines,
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
            test_list = list(test)
            fold_funding = (
                slice_funding_events_for_klines(funding_events, test_list)
                if funding_events is not None
                else None
            )
            result = run_grid_backtest(
                params,
                test_list,
                current_price=current_price,
                config=run_config,
                funding_events=fold_funding,
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
        funding_events=funding_events,
    )
    window_distribution = (
        _nyse_window_distribution(window_backtests)
        if window_backtests is not None
        else _backtest_window_distribution(
            report.get("equity_curve", []),
            request.distribution_window_rows,
            0.0,
        )
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
        "window_analysis": _window_analysis(window_backtests),
        "sample_label": request.sample_label,
        "parameters_frozen": request.parameters_frozen,
        "warning": (
            "该报告已标记为冻结参数样本外结果；仍需确认数据此前未参与调参。"
            if request.sample_label == "OOS_FROZEN"
            else "当前结果不是冻结参数样本外证明；只有从未参与调参的数据区间才可标记为样本外。"
        ),
    }
    dataset_metadata = dataset_metadata or {}
    report["metadata"] = {
        "dataset": request.dataset,
        "dataset_id": request.dataset_id,
        "dataset_checksum": dataset_metadata.get("checksum"),
        "data_provider": dataset_metadata.get("provider", "LEGACY_CSV"),
        "window_mode": "NYSE_CLOSED_ONLY",
        "window_count": (
            len(window_backtests)
            if window_backtests is not None
            else dataset_metadata.get("window_count")
        ),
        "dataset_schema_version": dataset_metadata.get("schema_version"),
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
    funding_events: list[FundingEvent] | None = None,
) -> dict[str, Any]:
    from strategy.backtest import (
        run_grid_backtest,
        slice_funding_events_for_klines,
    )
    from strategy.grid_calculator import calculate_grid_params

    if len(rows) <= request.observe_rows:
        return {"status": "FAILED", "error": "成本敏感性分析样本不足。", "scenarios": []}
    train = rows[: request.observe_rows]
    test = rows[request.observe_rows :]
    scenario_funding = (
        slice_funding_events_for_klines(funding_events, test)
        if funding_events is not None
        else None
    )
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
                funding_events=scenario_funding,
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
        "dataset_id": str(row.get("dataset_id") or ""),
        "dataset_checksum": str(row.get("dataset_checksum") or ""),
        "data_provider": str(row.get("data_provider") or ""),
        "window_mode": str(row.get("window_mode") or ""),
        "window_count": row.get("window_count"),
        "dataset_schema_version": row.get("dataset_schema_version"),
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
    if previous.get("source") == "NYSE_WINDOWS":
        return report
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
            "direction_modes": [
                {"value": "NEUTRAL", "label": "中性网格"},
                {"value": "LONG", "label": "做多网格"},
                {"value": "SHORT", "label": "做空网格"},
            ],
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
    direction_overrides: dict[str, str] = {}
    for raw_symbol, raw_mode in request.direction_overrides.items():
        symbol = str(raw_symbol).strip().upper()
        mode = str(raw_mode).strip().upper()
        if not symbol:
            continue
        if mode not in {"LONG", "SHORT", "NEUTRAL"}:
            raise HTTPException(status_code=422, detail=f"{symbol} 的方向模式无效：{raw_mode}")
        direction_overrides[symbol] = mode
    draft = {
        "direction_mode": str(request.direction_mode).strip().upper(),
        "direction_overrides": direction_overrides,
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
        "direction_mode": str(trading.get("direction_mode", "NEUTRAL")).upper(),
        "direction_overrides": {
            str(symbol).upper(): str(mode).upper()
            for symbol, mode in (trading.get("direction_overrides", {}) or {}).items()
        },
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
        "direction_mode": "全局网格方向",
        "direction_overrides": "标的方向覆盖",
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


def _runtime_heartbeat_thresholds(config: AppConfig) -> tuple[float, float]:
    raw = config.raw.get("runtime", {}) if isinstance(config.raw, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        stale = float(raw.get("heartbeat_stale_seconds", 20))
    except (TypeError, ValueError):
        stale = 20.0
    try:
        offline = float(raw.get("heartbeat_offline_seconds", 60))
    except (TypeError, ValueError):
        offline = 60.0
    stale = max(1.0, stale)
    offline = max(stale, offline)
    return stale, offline


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _alive_process_state_from_runtime(
    runtime: dict[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_seconds: float = 20.0,
    offline_seconds: float = 60.0,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if not isinstance(runtime, dict) or not str(runtime.get("runtime_id") or "").strip():
        return {
            "process_state": "OFFLINE",
            "alive": False,
            "pid": None,
            "runtime_id": "",
            "runtime_state": "",
            "started_at": "",
            "heartbeat_at": "",
            "heartbeat_age_seconds": None,
            "uptime_seconds": None,
            "last_status": "",
            "last_error": "",
        }
    explicit = str(runtime.get("state") or "").strip().upper()
    stopped_at = _parse_iso_datetime(runtime.get("stopped_at"))
    heartbeat_at = _parse_iso_datetime(runtime.get("heartbeat_at")) or _parse_iso_datetime(runtime.get("started_at"))
    started_at = _parse_iso_datetime(runtime.get("started_at"))
    age = (current - heartbeat_at).total_seconds() if heartbeat_at is not None else None
    uptime = (current - started_at).total_seconds() if started_at is not None else None
    if explicit == "FAILED" or (stopped_at is not None and explicit == "FAILED"):
        process_state = "FAILED"
        alive = False
    elif stopped_at is not None and explicit in {"STOPPED", "STOPPING"}:
        process_state = explicit
        alive = False
    elif age is None:
        process_state = "OFFLINE"
        alive = False
    elif age <= stale_seconds:
        process_state = "ONLINE"
        alive = True
    elif age <= offline_seconds:
        process_state = "STALE"
        alive = False
    else:
        process_state = "OFFLINE"
        alive = False
    pid_raw = runtime.get("pid")
    try:
        pid = int(pid_raw) if pid_raw is not None and str(pid_raw).strip() != "" else None
    except (TypeError, ValueError):
        pid = None
    return {
        "process_state": process_state,
        "alive": alive,
        "pid": pid,
        "runtime_id": str(runtime.get("runtime_id") or ""),
        "runtime_state": str(runtime.get("state") or ""),
        "started_at": str(runtime.get("started_at") or ""),
        "heartbeat_at": str(runtime.get("heartbeat_at") or ""),
        "heartbeat_age_seconds": age,
        "uptime_seconds": uptime,
        "last_status": str(runtime.get("last_status") or ""),
        "last_error": str(runtime.get("last_error") or ""),
    }


def _trader_process_status(config: AppConfig, repo: Repository | None = None) -> dict[str, Any]:
    control = _process_control_config(config)
    mode = str(control.get("mode") or "auto").strip().lower()
    service = str(control.get("service") or "quietgrid-trader").strip() or "quietgrid-trader"
    if mode == "auto":
        mode = "local" if platform.system().lower() == "windows" else "systemd"
    control_payload: dict[str, Any]
    if mode == "local":
        control_payload = {
            "available": True,
            "mode": "local",
            "service": service,
            "state": "unknown",
            "detail": "本地进程控制已启用；在线状态以 Trader 心跳为准。",
            "process_control_available": True,
            "process_control_mode": "local",
        }
    elif mode == "command":
        status_command = _process_command(control, "status_command")
        if not status_command:
            control_payload = {
                "available": bool(_process_command(control, "stop_command") or _process_command(control, "restart_command")),
                "mode": "command",
                "service": service,
                "state": "unknown",
                "detail": "已配置 command 交易进程控制，但未配置 status_command。",
                "process_control_available": bool(
                    _process_command(control, "stop_command")
                    or _process_command(control, "restart_command")
                    or _process_command(control, "start_command")
                ),
                "process_control_mode": "command",
            }
        else:
            result = _run_process_command(status_command, timeout_seconds=_process_command_timeout(control))
            detail = (result.stdout or result.stderr).strip()
            state = _command_process_state(result.returncode, detail)
            control_payload = {
                "available": True,
                "mode": "command",
                "service": service,
                "state": state,
                "detail": detail,
                "returncode": result.returncode,
                "process_control_available": True,
                "process_control_mode": "command",
            }
    elif mode == "systemd":
        result = _run_systemctl(["is-active", service])
        if result.returncode == 0:
            state = "running"
        elif str(result.stdout).strip() == "inactive":
            state = "stopped"
        else:
            state = "unknown"
        control_payload = {
            "available": True,
            "mode": "systemd",
            "service": service,
            "state": state,
            "detail": (result.stdout or result.stderr).strip(),
            "process_control_available": True,
            "process_control_mode": "systemd",
        }
    else:
        control_payload = {
            "available": False,
            "mode": mode,
            "service": service,
            "state": "unavailable",
            "detail": "当前运行环境未配置 systemd、local 或 command 交易进程控制。",
            "process_control_available": False,
            "process_control_mode": mode,
        }

    stale_seconds, offline_seconds = _runtime_heartbeat_thresholds(config)
    runtime = repo.trader_runtime() if repo is not None else None
    live = _alive_process_state_from_runtime(
        runtime,
        stale_seconds=stale_seconds,
        offline_seconds=offline_seconds,
    )
    # 兼容旧字段：state 优先展示真实在线状态；控制能力单独字段。
    mapped_state = {
        "ONLINE": "running",
        "STARTING": "starting",
        "STALE": "stale",
        "STOPPING": "stopping",
        "STOPPED": "stopped",
        "FAILED": "failed",
        "OFFLINE": "stopped" if control_payload.get("mode") in {"systemd", "command", "local"} else "unavailable",
    }.get(str(live["process_state"]), str(control_payload.get("state") or "unknown"))
    return {
        **control_payload,
        **live,
        "state": mapped_state if live["process_state"] != "OFFLINE" or not control_payload.get("available") else control_payload.get("state"),
        "detail": (
            live["last_error"]
            or control_payload.get("detail")
            or f"process_state={live['process_state']}"
        ),
    }


def _local_process_manager(config: AppConfig, repo: Repository):
    from operations.process_manager import LocalTraderProcessManager

    control = _process_control_config(config)
    stale, offline = _runtime_heartbeat_thresholds(config)
    timeout = float(control.get("timeout_seconds", config.raw.get("runtime", {}).get("startup_timeout_seconds", 15)))
    return LocalTraderProcessManager(
        repository=repo,
        config=control,
        runtime_thresholds=(stale, offline),
        startup_timeout_seconds=timeout,
        project_root=Path.cwd(),
    )


def _wait_for_trader_heartbeat(
    config: AppConfig,
    repo: Repository,
    *,
    timeout_seconds: float = 15.0,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + max(1.0, float(timeout_seconds))
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _trader_process_status(config, repo)
        if last.get("alive") or str(last.get("process_state") or "") == "ONLINE":
            return {**last, "wait_ok": True}
        time.sleep(max(0.1, float(poll_seconds)))
    return {**last, "wait_ok": False}


def _run_trader_process_action(
    config: AppConfig,
    repo: Repository,
    request: ConsoleActionRequest,
    operation: str,
    *,
    wait_for_heartbeat: bool = True,
) -> dict[str, Any]:
    if operation not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=422, detail="不支持的交易进程控制动作。")

    before = _trader_process_status(config, repo)
    if not before.get("process_control_available", before.get("available")):
        raise HTTPException(status_code=409, detail=str(before.get("detail") or "交易进程控制不可用。"))
    labels = {
        "start": "启动交易 loop 进程",
        "stop": "停止交易 loop 进程",
        "restart": "重启交易 loop 进程",
    }
    label = labels[operation]
    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    detail = _action_detail("trader_loop_" + operation, label, request, request_id, {"before": before})
    repo.log_system("WARN", "console_action", "Console action requested.", _json_detail(detail), now)
    service = str(before.get("service") or "quietgrid-trader")
    mode = str(before.get("mode") or before.get("process_control_mode") or "")
    result_payload: dict[str, Any]
    if mode == "local":
        manager = _local_process_manager(config, repo)
        account_id = str(getattr(config, "account_id", "default") or "default")
        if operation == "start":
            if before.get("process_state") == "ONLINE" or before.get("alive"):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "TRADER_ALREADY_RUNNING", "message": "交易进程已经运行，不能重复启动。"},
                )
            start_result = manager.start(account_id)
            if not start_result.started and start_result.state == "ONLINE":
                raise HTTPException(
                    status_code=409,
                    detail={"code": "TRADER_ALREADY_RUNNING", "message": start_result.message},
                )
            if not start_result.started and start_result.state == "FAILED":
                raise HTTPException(
                    status_code=500,
                    detail={"code": "TRADER_START_FAILED", "message": start_result.message},
                )
            heartbeat_info: dict[str, Any] = {"wait_ok": None}
            if wait_for_heartbeat:
                heartbeat_info = _wait_for_trader_heartbeat(
                    config, repo, timeout_seconds=manager.startup_timeout_seconds
                )
                if not heartbeat_info.get("alive"):
                    raise HTTPException(
                        status_code=504,
                        detail={
                            "code": "TRADER_START_TIMEOUT",
                            "message": "交易进程已拉起，但在超时时间内未收到心跳。",
                            "pid": start_result.pid,
                            "wait": heartbeat_info,
                        },
                    )
            result_payload = {
                "returncode": 0,
                "stdout": start_result.message,
                "stderr": "",
                "start": {**start_result.to_mapping(), "heartbeat": heartbeat_info},
            }
        elif operation == "stop":
            active = int(repo.dashboard_summary().get("active_sessions") or 0)
            force = bool(getattr(request, "force", False)) if hasattr(request, "force") else False
            reason_force = "force" in str(request.reason or "").lower()
            if active > 0 and not (force or reason_force):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ACTIVE_SESSIONS_PRESENT",
                        "message": f"仍有 {active} 个活动会话，停止进程不会自动平仓。请先停止本轮/安全清扫，或 reason 含 force。",
                        "active_sessions": active,
                    },
                )
            stop_result = manager.stop(account_id)
            if not stop_result.get("ok"):
                raise HTTPException(status_code=500, detail=str(stop_result.get("message") or "停止失败"))
            result_payload = {
                "returncode": 0,
                "stdout": str(stop_result.get("message") or ""),
                "stderr": "",
                "stop": stop_result,
            }
        else:
            active = int(repo.dashboard_summary().get("active_sessions") or 0)
            reason_force = "force" in str(request.reason or "").lower()
            if active > 0 and not reason_force:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "ACTIVE_SESSIONS_PRESENT",
                        "message": f"仍有 {active} 个活动会话，重启前请先停止本轮或在 reason 中标注 force。",
                        "active_sessions": active,
                    },
                )
            restart_result = manager.restart(account_id)
            result_payload = {
                "returncode": 0 if restart_result.get("ok") else 1,
                "stdout": json.dumps(restart_result, ensure_ascii=False),
                "stderr": "",
                "restart": restart_result,
            }
            if result_payload["returncode"] != 0:
                raise HTTPException(status_code=500, detail="重启交易进程失败。")
    elif mode == "command":
        command = _process_command(_process_control_config(config), f"{operation}_command")
        if not command:
            raise HTTPException(status_code=409, detail=f"未配置 {operation}_command，无法执行交易进程控制。")
        result = _run_process_command(command, timeout_seconds=_process_command_timeout(_process_control_config(config)))
        result_payload = {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        if result.returncode != 0:
            after = _trader_process_status(config, repo)
            payload = {"before": before, "after": after, **result_payload}
            repo.log_system(
                "ERROR",
                "console_action",
                "Console action failed.",
                _json_detail({**detail, "result": payload}),
                datetime.now(timezone.utc),
            )
            raise HTTPException(status_code=500, detail=result.stderr.strip() or result.stdout.strip() or f"{label}失败。")
    else:
        if operation == "start":
            systemctl_op = "start"
        else:
            systemctl_op = operation
        result = _run_systemctl([systemctl_op, service])
        result_payload = {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        if result.returncode != 0:
            after = _trader_process_status(config, repo)
            payload = {"before": before, "after": after, **result_payload}
            repo.log_system(
                "ERROR",
                "console_action",
                "Console action failed.",
                _json_detail({**detail, "result": payload}),
                datetime.now(timezone.utc),
            )
            raise HTTPException(status_code=500, detail=result.stderr.strip() or result.stdout.strip() or f"{label}失败。")

    after = _trader_process_status(config, repo)
    payload = {
        "before": before,
        "after": after,
        **result_payload,
    }
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
        "state": after.get("process_state") or after.get("state"),
        "pid": after.get("pid"),
        "message": f"{label}已提交，当前状态：{after.get('process_state') or after.get('state')}。",
        "result": payload,
    }


def _run_auto_trading_action(
    config: AppConfig,
    repo: Repository,
    request: ConsoleActionRequest,
    *,
    enabled: bool,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    request_id = request.request_id or str(uuid4())
    control = {
        "enabled": bool(enabled),
        "requested_at": now.isoformat(),
        "requested_by": "web",
        "request_id": request_id,
        "mode": "AUTO_WINDOW",
        "account_id": str(getattr(config, "account_id", "default") or "default"),
    }
    control, changed = repo.ensure_auto_trading_control(control, now)
    trader_state = _trader_process_status(config, repo)
    start_payload: dict[str, Any] | None = None
    trader_start_attempted = False
    if enabled and not trader_state.get("alive"):
        trader_start_attempted = True
        try:
            start_payload = _run_trader_process_action(
                config, repo, request, "start", wait_for_heartbeat=True
            )
            trader_state = _trader_process_status(config, repo)
        except HTTPException as exc:
            # 409 已在线；504 超时仍算“已提交启动”，由前端继续轮询心跳。
            if exc.status_code not in {409, 504}:
                raise
    label = "启动自动交易" if enabled else "停止自动交易"
    if changed:
        detail = _action_detail(
            "auto_trading_" + ("start" if enabled else "stop"),
            label,
            request,
            request_id,
            control,
        )
        repo.log_system("INFO", "auto_trading", f"{label} completed.", _json_detail(detail), now)
    transition_state = "ENABLED" if enabled else "DISABLED"
    return {
        "ok": True,
        "auto_trading_enabled": bool(enabled),
        "enabled": bool(enabled),
        "changed": changed,
        "transition_state": transition_state,
        "can_start": not enabled,
        "can_stop": enabled,
        "blocked_reason": "",
        "trader_process_state": trader_state.get("process_state") or trader_state.get("state"),
        "message": (
            (
                "自动交易已启用，系统将在 Trader 在线后检查交易窗口。"
                if changed
                else (
                    "自动交易已经启用，已请求恢复离线 Trader。"
                    if trader_start_attempted
                    else "自动交易已经启用。"
                )
            )
            if enabled
            else (
                "自动交易已关闭；现有会话不会自动平仓。"
                if changed
                else "自动交易已经停止。"
            )
        ),
        "trader_start": start_payload,
        "control": control,
    }


def _current_round_payload(config: AppConfig, repo: Repository) -> dict[str, Any]:
    trader = _trader_process_status(config, repo)
    runtime = repo.runtime_state()
    auto = repo.auto_trading_control() or {"enabled": False}
    auto_enabled = bool(auto.get("enabled"))
    auto = {
        **auto,
        "enabled": auto_enabled,
        "transition_state": "ENABLED" if auto_enabled else "DISABLED",
        "can_start": not auto_enabled,
        "can_stop": auto_enabled,
        "blocked_reason": "",
    }
    timing = config.raw.get("timing", {}) if isinstance(config.raw, dict) else {}
    window_payload: dict[str, Any] = {
        "kind": "",
        "allowed": False,
        "window_key": "",
        "force_close_at": "",
        "minutes_to_force_close": None,
        "testnet_force_window": bool(timing.get("testnet_force_window", False)),
    }
    try:
        from core.scheduler import Scheduler
        from strategy.window_models import WindowKind

        kinds = []
        for item in timing.get("allowed_window_kinds") or ["WEEKEND", "HOLIDAY"]:
            try:
                kinds.append(WindowKind(str(item).strip().upper()))
            except ValueError:
                continue
        scheduler = Scheduler(
            force_close_minutes=int(timing.get("force_close_minutes", 120)),
            minimum_trade_minutes=int(timing.get("minimum_trade_minutes", 120)),
            allowed_window_kinds=tuple(kinds or [WindowKind.WEEKEND, WindowKind.HOLIDAY]),
        )
        if bool(timing.get("testnet_force_window", False)) and bool(getattr(config, "binance_testnet", False)):
            window_payload = {
                "kind": "WEEKEND",
                "allowed": True,
                "window_key": "TESTNET_FORCE_WINDOW",
                "force_close_at": "",
                "minutes_to_force_close": None,
                "testnet_force_window": True,
                "reason": "强制测试窗口",
            }
        else:
            window = scheduler.classify_window(datetime.now(timezone.utc))
            window_payload = window.to_mapping()
            window_payload["testnet_force_window"] = False
    except Exception as exc:
        window_payload["reason"] = str(exc)

    candidates = []
    current_round_id = runtime.get("current_round_id")
    if current_round_id is not None:
        try:
            rows = repo.round_candidates(int(current_round_id)) if hasattr(repo, "round_candidates") else []
            candidates = rows or []
        except Exception:
            candidates = []

    recent_events: list[dict[str, Any]] = []
    try:
        for row in repo.recent_rows("system_logs", limit=20):
            recent_events.append(
                {
                    "time": row.get("log_time") or row.get("created_at") or "",
                    "level": row.get("level") or "",
                    "module": row.get("module") or "",
                    "message": row.get("message") or "",
                }
            )
    except Exception:
        recent_events = []

    request = repo.round_start_request(include_terminal=True)
    stream_health_entry = repo.get_control_state().get("stream_health")
    stream_health = (
        stream_health_entry.get("value")
        if isinstance(stream_health_entry, dict)
        and isinstance(stream_health_entry.get("value"), dict)
        else {"streams": {}, "updated_at": ""}
    )
    session_rows = repo.console_sessions(
        active_only=True,
        limit=200,
        window_id=int(current_round_id) if current_round_id is not None else None,
    )
    disabled_symbols = repo.disabled_symbols()
    stop_requests = repo.pending_session_stop_requests()
    control_requests = repo.pending_session_control_requests()
    sessions = [
        _session_payload(
            row,
            config,
            disabled_symbols,
            stop_requests,
            control_requests,
        )
        for row in session_rows
    ]
    return {
        "trader": trader,
        "auto_trading": auto,
        "window": window_payload,
        "round": {
            "state": runtime.get("round_state") or "IDLE",
            "round_id": current_round_id,
            "last_scan_at": runtime.get("last_scan_at") or "",
            "next_scan_at": runtime.get("next_scan_at") or "",
            "runtime_id": runtime.get("runtime_id") or "",
            "start_request": request,
        },
        "candidates": candidates,
        "sessions": sessions,
        "stream_health": stream_health,
        "risk": {
            "new_entries_paused": bool(repo.new_entries_paused()),
        },
        "recent_events": recent_events,
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
        "soft_breach_count": int(row.get("soft_breach_count") or 0),
        "last_retention_decision_at": row.get("last_retention_decision_at"),
        "cooldown_current_atr": row.get("cooldown_current_atr"),
        "cooldown_amplitude_pct": row.get("cooldown_amplitude_pct"),
        "cooldown_amplitude_limit_pct": row.get("cooldown_amplitude_limit_pct"),
        "cooldown_reason": row.get("cooldown_reason"),
        "cooldown_evaluated_at": row.get("cooldown_evaluated_at"),
        "direction_mode": str(row.get("direction_mode") or "NEUTRAL").upper(),
        "direction_source": (
            str(row.get("direction_source") or "").strip().lower()
            or (
                "symbol_override"
                if symbol.upper()
                in {
                    str(item).upper()
                    for item in config.raw.get("trading", {}).get(
                        "direction_overrides", {}
                    )
                }
                else "global"
            )
        ),
        "seed_position_side": row.get("seed_position_side"),
        "seed_qty": row.get("seed_qty"),
        "seed_entry_price": row.get("seed_entry_price"),
        "seed_slippage_pct": row.get("seed_slippage_pct"),
        "seed_fee": row.get("seed_fee"),
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


def _volatility_stage_payload(row: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    state = str(row.get("state") or "").upper()
    if state == "OBSERVING":
        progress, remaining = _observation_progress(row.get("open_time"), config)
        stage = "observing" if remaining is None or remaining > 0 else "calculating"
        label = "正在观察/波动计算中" if stage == "observing" else "波动计算待完成"
    elif state in {"RUNNING", "DEFENSIVE", "COOLDOWN", "CLOSING", "PAUSED"}:
        stage = "trading"
        if state == "PAUSED":
            label = "网格已暂停，持仓风控仍在运行"
        elif state == "DEFENSIVE":
            label = "防御模式：只保留减仓和保护订单"
        elif state == "COOLDOWN":
            label = "硬止损冷静期"
        else:
            label = "计算结束，自动交易已启动"
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
