from __future__ import annotations

from copy import deepcopy
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
from strategy.grid_calculator import SUPPORTED_RANGE_METHODS


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
            "account_id": app_config.account_id,
            "account_label": app_config.account_label,
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
            "account_id": app_config.account_id,
            "account_label": app_config.account_label,
            "balance": None,
        }

    @app.get("/api/control-state")
    def control_state(repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        return _control_state_payload(app_config, repo)

    @app.get("/api/strategy-config")
    def strategy_config(repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        return _strategy_config_payload(app_config, repo)

    @app.post("/api/strategy-config/draft")
    def save_strategy_config_draft(
        request: StrategyConfigDraftRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        return _save_strategy_config_draft(app_config, repo, request)

    @app.get("/api/sessions/active")
    def active_sessions(
        limit: int = Query(50, ge=1, le=200),
        include_recent: bool = Query(False),
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        rows = repo.console_sessions(active_only=not include_recent, limit=limit)
        disabled_symbols = repo.disabled_symbols()
        stop_requests = repo.pending_session_stop_requests()
        return {"items": [_session_payload(row, disabled_symbols, stop_requests) for row in rows]}

    @app.get("/api/sessions/{session_id}")
    def session_detail(session_id: int, repo: Repository = Depends(get_repository)) -> dict[str, Any]:
        row = repo.get_session(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        disabled_symbols = repo.disabled_symbols()
        stop_requests = repo.pending_session_stop_requests()
        return {
            "session": _session_payload(row, disabled_symbols, stop_requests),
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

    @app.post("/api/actions/symbols/{symbol}/start-grid")
    async def action_start_symbol_grid(
        symbol: str,
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _ensure_testnet_action(app_config)
        _require_confirm(request)
        normalized_symbol = _normalize_startable_symbol(app_config, symbol)
        seconds = float(request.loop_seconds or 600)
        if seconds < 20:
            raise HTTPException(status_code=422, detail="运行秒数不能小于 20。")
        return await _run_console_action(
            repo,
            action="symbol_start_grid",
            label="启动指定标的网格",
            request=request,
            runner=lambda: _run_symbol_testnet_run_action(app_config, normalized_symbol, seconds),
            extra_detail={"symbol": normalized_symbol, "loop_seconds": seconds},
        )

    @app.post("/api/actions/pause-new-entries")
    def action_pause_new_entries(
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_new_entries_paused(app_config, repo, request, paused=True)

    @app.post("/api/actions/resume-new-entries")
    def action_resume_new_entries(
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_new_entries_paused(app_config, repo, request, paused=False)

    @app.post("/api/actions/sessions/{session_id}/stop")
    def action_stop_session(
        session_id: int,
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_session_stop(app_config, repo, session_id, request)

    @app.post("/api/actions/sessions/{session_id}/manual-close")
    def action_manual_close_session(
        session_id: int,
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_session_manual_close(app_config, repo, session_id, request)

    @app.post("/api/actions/sessions/stop-all")
    def action_stop_all_sessions(
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _request_all_sessions_stop(app_config, repo, request)

    @app.post("/api/actions/symbols/{symbol}/disable-next-entry")
    def action_disable_symbol_next_entry(
        symbol: str,
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_symbol_next_entry_disabled(app_config, repo, symbol, request, disabled=True)

    @app.post("/api/actions/symbols/{symbol}/enable-next-entry")
    def action_enable_symbol_next_entry(
        symbol: str,
        request: ConsoleActionRequest,
        repo: Repository = Depends(get_repository),
    ) -> dict[str, Any]:
        _require_confirm(request)
        return _set_symbol_next_entry_disabled(app_config, repo, symbol, request, disabled=False)

    return app


class ConsoleActionRequest(BaseModel):
    confirm: bool = False
    reason: str = Field(default="控制台手动操作", min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=80)
    loop_seconds: float | None = Field(default=None, ge=20, le=86400)


class StrategyConfigDraftRequest(BaseModel):
    volatility_method: str = Field(min_length=1, max_length=40)
    max_concurrent: int = Field(ge=1, le=10)
    observe_hours: float = Field(gt=0, le=24)
    min_step_pct: float = Field(gt=0, le=0.05)
    max_grid_num: int = Field(ge=1, le=200)
    take_profit_usdt: float | None = Field(default=None, gt=0, le=100000)
    total_capital_limit: float | None = Field(default=None, gt=0, le=10000000)
    max_maker_fee_rate: float | None = Field(default=None, ge=0, le=0.01)


app = create_app()


def _mode_label(config: AppConfig) -> str:
    return "测试网" if config.binance_testnet else "未确认"


def _require_confirm(request: ConsoleActionRequest) -> None:
    if not request.confirm:
        raise HTTPException(status_code=400, detail="控制动作需要 confirm=true。")


def _ensure_testnet_action(config: AppConfig) -> None:
    if not config.binance_testnet:
        raise HTTPException(status_code=409, detail="当前不是测试网模式，拒绝执行交易控制动作。")


def _control_state_payload(config: AppConfig, repo: Repository) -> dict[str, Any]:
    state = repo.get_control_state()
    pause_state = state.get("new_entries_paused")
    disabled_state = state.get("disabled_symbols")
    return {
        "new_entries_paused": bool(pause_state.get("value")) if isinstance(pause_state, dict) else False,
        "new_entries_paused_updated_at": pause_state.get("updated_at") if isinstance(pause_state, dict) else "",
        "disabled_symbols": sorted(repo.disabled_symbols()),
        "disabled_symbols_updated_at": disabled_state.get("updated_at") if isinstance(disabled_state, dict) else "",
        "startable_symbols": _configured_startable_symbols(config),
        "session_stop_requests": list(repo.session_stop_requests().values()),
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
        "max_concurrent": int(request.max_concurrent),
        "observe_hours": float(request.observe_hours),
        "min_step_pct": float(request.min_step_pct),
        "max_grid_num": int(request.max_grid_num),
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
    return {
        "volatility_method": str(grid.get("range_method", "std")),
        "max_concurrent": int(trading.get("max_concurrent", 1)),
        "observe_hours": float(timing.get("observe_hours", 3)),
        "min_step_pct": float(grid.get("min_step_pct", 0.0015)),
        "max_grid_num": int(grid.get("max_grid_num", 20)),
        "take_profit_usdt": float(trading.get("take_profit_usdt", 10)),
        "total_capital_limit": float(trading.get("total_capital_limit", 1000)),
        "max_maker_fee_rate": float(trading.get("max_maker_fee_rate", 0)),
    }


def _strategy_config_diff(current: dict[str, Any], draft: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "volatility_method": "波动率算法",
        "max_concurrent": "最大并发标的",
        "observe_hours": "观察窗口小时",
        "min_step_pct": "最小网格步长",
        "max_grid_num": "最大网格数量",
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

    rows = repo.console_sessions(active_only=True, limit=200)
    request_id = request.request_id or str(uuid4())
    now = datetime.now(timezone.utc)
    action = "all_sessions_stop"
    label = "停止全部网格"
    detail = _action_detail(action, label, request, request_id, {"session_count": len(rows)})
    repo.log_system("WARN", "console_action", "Console action requested.", _json_detail(detail), now)
    requests = []
    before = []
    for row in rows:
        session_id = int(row.get("id") or 0)
        before.append(_session_control_snapshot(repo, session_id))
        requests.append(
            repo.request_session_stop(
                session_id=session_id,
                symbol=str(row.get("symbol") or ""),
                reason=request.reason,
                request_id=f"{request_id}:{session_id}",
                requested_at=now,
            )
        )
    after = [_session_control_snapshot(repo, int(row.get("id") or 0)) for row in rows]
    result = {
        "before": before,
        "after": after,
        "stop_requests": requests,
        "position_confirmation": {
            "status": "queued",
            "status_label": "等待交易循环确认",
            "message": "交易循环会逐个处理停止请求，完成后每个会话会写入 close_reason 与审计日志。",
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
        "message": f"已记录 {len(requests)} 个活动网格停止请求，仓位确认等待交易循环处理。",
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


async def _run_safety_sweep_action(config: AppConfig) -> dict[str, Any]:
    from trader import _run_binance_safety_sweep

    return await _run_binance_safety_sweep(config)


async def _run_testnet_run_action(config: AppConfig, seconds: float) -> dict[str, Any]:
    from trader import _run_binance_test_run

    return await _run_binance_test_run(config, max_seconds=seconds)


async def _run_symbol_testnet_run_action(config: AppConfig, symbol: str, seconds: float) -> dict[str, Any]:
    from trader import _run_binance_test_run

    single_symbol_config = _single_symbol_testnet_config(config, symbol)
    return await _run_binance_test_run(single_symbol_config, max_seconds=seconds)


def _single_symbol_testnet_config(config: AppConfig, symbol: str) -> AppConfig:
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
    disabled_symbols: set[str] | None = None,
    stop_requests: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "")
    session_id = int(row.get("id") or 0)
    stop_request = stop_requests.get(session_id) if stop_requests else None
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
    }


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
