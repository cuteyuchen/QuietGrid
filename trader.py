from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import inspect
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite, sqrt
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from loguru import logger

from core.config import load_config, require_testnet, select_account, select_all_accounts
from core.logging_config import setup_logging
from core.notifications import build_system_log_notifier
from core.scheduler import Scheduler
from data_sources.csv_source import read_legacy_backtest_csv
from db.database import init_db
from db.repository import Repository
from exchange.binance import BinanceFuturesClient
from exchange.mock import MockExchangeClient
from strategy.backtest import (
    BacktestResult,
    backtest_config_from_mapping,
    run_grid_backtest,
    slice_funding_events_for_klines,
)
from strategy.cooldown import CooldownConfig
from strategy.adaptive_grid import AdaptiveGridConfig
from core.models import GridDirectionMode
from strategy.controller import ControllerConfig, TradingController, V2FeatureFlags, _position_close_specs
from strategy.grid_calculator import GridConfig, calculate_grid_params
from strategy.inventory import InventoryConfig
from strategy.observer import ObserverConfig
from strategy.regime import RegimeConfig, RegimeWeights
from strategy.selector import SelectionConfig, _is_perpetual_contract


class _SmokeComplete(Exception):
    pass


ORDER_CREATE_RECOVERY_ATTEMPTS = 5
ORDER_CREATE_RECOVERY_DELAY_SECONDS = 0.25
BINANCE_TESTNET_SUBSTITUTE_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BCHUSDT"}
BINANCE_LOOP_TASK_CANCEL_TIMEOUT_SECONDS = 5.0
BINANCE_LOOP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS = 30.0
DEFAULT_BOUNDED_RUN_SECONDS = 60.0
TRADER_RUNTIME_ID = str(uuid4())


class _TestnetAlwaysOpenScheduler:
    def is_in_window(self, now_utc: datetime | None = None) -> bool:
        return True

    def should_force_close(self, now_utc: datetime | None = None) -> bool:
        return False

    def minutes_to_next_open(self, now_utc: datetime | None = None) -> float:
        return float("inf")

    def classify_window(self, now_utc: datetime | None = None, **kwargs):
        from strategy.window_models import TradingWindow, WindowKind

        return TradingWindow(
            kind=WindowKind.WEEKEND,
            allowed=True,
            window_key="TESTNET_FORCE_WINDOW",
            previous_market_close=None,
            next_market_open=None,
            next_premarket_open=None,
            force_close_at=None,
            minutes_to_force_close=float("inf"),
            reason="强制测试窗口（testnet_force_window=true）。",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="QuietGrid trading process")
    parser.add_argument("--mock-once", action="store_true", help="使用 mock 交易所执行一轮编排验证")
    parser.add_argument("--binance-once", action="store_true", help="使用 Binance 测试网执行一轮编排")
    parser.add_argument("--mock-loop", action="store_true", help="使用 mock 交易所运行长期循环")
    parser.add_argument("--binance-loop", action="store_true", help="使用当前连接的 Binance 环境运行长期循环")
    parser.add_argument("--binance-check", action="store_true", help="只检查 Binance 测试网连接和账户配置，不下单")
    parser.add_argument("--binance-order-smoke", action="store_true", help="使用 Binance 测试网创建并清理一组最小订单")
    parser.add_argument("--binance-test-order-smoke", action="store_true", help="使用 Binance 测试网 order/test 校验下单参数，不创建订单")
    parser.add_argument("--binance-market-roundtrip-smoke", action="store_true", help="使用 Binance 测试网执行最小 Market 开平仓烟测")
    parser.add_argument("--binance-direct-order-diagnose", action="store_true", help="绕过 SDK 直接请求 Binance Futures /order 诊断真实下单接口")
    parser.add_argument("--binance-price-stream-smoke", action="store_true", help="接收一条 Binance 测试网价格 WebSocket 事件")
    parser.add_argument("--binance-signed-write-health", action="store_true", help="只执行 Binance 测试网签名写接口预检，不启动网格")
    parser.add_argument("--binance-listen-key-smoke", action="store_true", help="验证 Binance Futures 用户流 listenKey 生命周期")
    parser.add_argument("--binance-algo-stop-smoke", action="store_true", help="创建并撤销一个 Binance Futures Algo STOP_MARKET 条件单")
    parser.add_argument("--binance-position-smoke", action="store_true", help="只读检查当前连接环境的持仓模式、持仓和未成交订单")
    parser.add_argument("--binance-safety-sweep", action="store_true", help="清理当前连接环境 allowlist 标的的挂单和仓位")
    parser.add_argument(
        "--binance-bounded-run",
        "--binance-test-run",
        dest="binance_test_run",
        action="store_true",
        help="执行当前连接环境有界运行流程：前置持仓检查、限时loop、安全清扫、后置持仓检查",
    )
    parser.add_argument("--account-id", help="选择 config.yaml accounts 中的账户；未配置时使用默认 BINANCE_API_KEY/SECRET")
    parser.add_argument("--all-accounts", action="store_true", help="对 config.yaml accounts 中全部账户并发执行同一个 Binance 运行模式")
    parser.add_argument("--loop-iterations", type=int, help="限制 --mock-loop 或 --binance-loop 的主循环轮数，留空则持续运行")
    parser.add_argument("--loop-seconds", type=float, help="限制 --mock-loop、--binance-loop 或有界运行流程的运行秒数；有界运行默认60秒")
    parser.add_argument("--backtest-csv", help="读取本地CSV K线文件执行离线网格回测，不连接交易所")
    parser.add_argument("--backtest-dir", help="读取目录内所有CSV K线文件执行批量离线回测，不连接交易所")
    parser.add_argument("--backtest-observe-rows", type=int, default=60, help="CSV前多少行作为观察期样本，默认60")
    parser.add_argument("--backtest-symbol", default="AAPLUSDT", help="回测标的名，默认AAPLUSDT")
    parser.add_argument("--backtest-funding-rate", type=float, default=0.0, help="观察期网格计算使用的资金费率，默认0")
    parser.add_argument("--backtest-output", help="可选：把单文件完整回测报告或批量汇总报告写入JSON文件")
    args = parser.parse_args()
    if args.loop_iterations is not None and args.loop_iterations <= 0:
        parser.error("--loop-iterations 必须是正整数。")
    if args.loop_seconds is not None and args.loop_seconds <= 0:
        parser.error("--loop-seconds 必须是正数。")

    config = load_config()
    account_configs = None
    if args.account_id and args.all_accounts:
        parser.error("--account-id 和 --all-accounts 不能同时使用。")
    if args.all_accounts:
        account_configs = select_all_accounts(config)
    elif args.account_id:
        config = select_account(config, args.account_id)
    setup_logging(config.raw)
    if account_configs is None:
        init_db(config.database_path)
    else:
        for account_config in account_configs:
            init_db(account_config.database_path)

    selected_modes = [
        args.mock_once,
        args.binance_once,
        args.mock_loop,
        args.binance_loop,
        args.binance_check,
        args.binance_order_smoke,
        args.binance_test_order_smoke,
        args.binance_market_roundtrip_smoke,
        args.binance_direct_order_diagnose,
        args.binance_price_stream_smoke,
        args.binance_signed_write_health,
        args.binance_listen_key_smoke,
        args.binance_algo_stop_smoke,
        args.binance_position_smoke,
        args.binance_safety_sweep,
        args.binance_test_run,
        args.backtest_csv is not None,
        args.backtest_dir is not None,
    ]
    if sum(1 for enabled in selected_modes if enabled) > 1:
        raise SystemExit("一次只能选择一个运行模式。")
    if args.all_accounts and not _is_all_accounts_supported_mode(args):
        raise SystemExit("--all-accounts 只支持 Binance 运行模式，且必须显式选择一个 Binance 模式。")
    if args.mock_once:
        result = asyncio.run(_run_mock_once(config))
        logger.info("Mock run_once result: {}", result)
        return
    if args.binance_once:
        result = _run_binance_mode(config, account_configs, _run_binance_once)
        logger.info("Binance testnet run_once result: {}", result)
        return
    if args.binance_check:
        result = _run_binance_mode(config, account_configs, _run_binance_check)
        logger.info("Binance testnet check result: {}", result)
        return
    if args.binance_order_smoke:
        result = _run_binance_mode(config, account_configs, _run_binance_order_smoke)
        logger.info("Binance testnet order smoke result: {}", result)
        return
    if args.binance_test_order_smoke:
        result = _run_binance_mode(config, account_configs, _run_binance_test_order_smoke)
        logger.info("Binance testnet order/test smoke result: {}", result)
        return
    if args.binance_market_roundtrip_smoke:
        result = _run_binance_mode(config, account_configs, _run_binance_market_roundtrip_smoke)
        logger.info("Binance testnet market roundtrip smoke result: {}", result)
        return
    if args.binance_direct_order_diagnose:
        result = _run_binance_mode(config, account_configs, _run_binance_direct_order_diagnose)
        logger.info("Binance testnet direct order diagnose result: {}", result)
        return
    if args.binance_price_stream_smoke:
        result = _run_binance_mode(config, account_configs, _run_binance_price_stream_smoke)
        logger.info("Binance testnet price stream smoke result: {}", result)
        return
    if args.binance_signed_write_health:
        result = _run_binance_mode(config, account_configs, _run_binance_signed_write_health)
        logger.info("Binance testnet signed write health result: {}", result)
        return
    if args.binance_listen_key_smoke:
        result = _run_binance_mode(config, account_configs, _run_binance_listen_key_smoke)
        logger.info("Binance testnet listenKey smoke result: {}", result)
        return
    if args.binance_algo_stop_smoke:
        result = _run_binance_mode(config, account_configs, _run_binance_algo_stop_smoke)
        logger.info("Binance testnet algo stop smoke result: {}", result)
        return
    if args.binance_position_smoke:
        result = _run_binance_mode(config, account_configs, _run_binance_position_smoke)
        logger.info("Binance current-environment position check result: {}", result)
        return
    if args.binance_safety_sweep:
        result = _run_binance_mode(config, account_configs, _run_binance_safety_sweep)
        logger.info("Binance current-environment safety sweep result: {}", result)
        return
    if args.binance_test_run:
        result = _run_binance_mode(
            config,
            account_configs,
            _run_binance_test_run,
            max_seconds=args.loop_seconds or DEFAULT_BOUNDED_RUN_SECONDS,
        )
        logger.info("Binance current-environment bounded run result: {}", result)
        return
    if args.backtest_csv:
        result = _run_backtest_csv(
            config,
            Path(args.backtest_csv),
            observe_rows=args.backtest_observe_rows,
            symbol=args.backtest_symbol,
            funding_rate=args.backtest_funding_rate,
            output_path=Path(args.backtest_output) if args.backtest_output else None,
        )
        logger.info("CSV backtest result: {}", json.dumps(result, ensure_ascii=False))
        return
    if args.backtest_dir:
        result = _run_backtest_dir(
            config,
            Path(args.backtest_dir),
            observe_rows=args.backtest_observe_rows,
            symbol=args.backtest_symbol,
            funding_rate=args.backtest_funding_rate,
            output_path=Path(args.backtest_output) if args.backtest_output else None,
        )
        logger.info("CSV backtest batch result: {}", json.dumps(result, ensure_ascii=False))
        return
    if args.mock_loop:
        asyncio.run(_run_mock_loop(config, max_iterations=args.loop_iterations, max_seconds=args.loop_seconds))
        return
    if args.binance_loop:
        _run_binance_mode(
            config,
            account_configs,
            _run_binance_loop,
            max_iterations=args.loop_iterations,
            max_seconds=args.loop_seconds,
        )
        return

    logger.info(
        "QuietGrid initialized. Use --mock-once, --binance-check, --binance-order-smoke, --binance-test-order-smoke, --binance-market-roundtrip-smoke, --binance-direct-order-diagnose, --binance-price-stream-smoke, --binance-signed-write-health, --binance-listen-key-smoke, --binance-algo-stop-smoke, --binance-position-smoke, --binance-safety-sweep, --binance-bounded-run, --backtest-csv, --backtest-dir, --binance-once, --mock-loop or --binance-loop. Use --loop-iterations or --loop-seconds with loop commands for bounded runs."
    )


def _is_all_accounts_supported_mode(args) -> bool:
    return any(
        (
            args.binance_once,
            args.binance_loop,
            args.binance_check,
            args.binance_order_smoke,
            args.binance_test_order_smoke,
            args.binance_market_roundtrip_smoke,
            args.binance_direct_order_diagnose,
            args.binance_price_stream_smoke,
            args.binance_signed_write_health,
            args.binance_listen_key_smoke,
            args.binance_algo_stop_smoke,
            args.binance_position_smoke,
            args.binance_safety_sweep,
            args.binance_test_run,
        )
    )


def _run_binance_mode(config, account_configs, runner, **kwargs):
    if account_configs is None:
        return asyncio.run(runner(config, **kwargs))
    return asyncio.run(_run_for_account_configs(account_configs, runner, **kwargs))


async def _run_for_account_configs(account_configs, runner, **kwargs) -> dict[str, Any]:
    if not account_configs:
        raise RuntimeError("没有可运行账户配置。")
    tasks = [asyncio.create_task(runner(account_config, **kwargs)) for account_config in account_configs]
    try:
        results = await asyncio.gather(*tasks)
    except Exception:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return {str(account_config.account_id): result for account_config, result in zip(account_configs, results)}


async def _run_mock_once(config):
    controller = _build_controller(MockExchangeClient(), config, live_observation=False)
    return await controller.run_once()


async def _run_mock_loop(config, max_iterations: int | None = None, max_seconds: float | None = None):
    controller = _build_controller(MockExchangeClient(), config, live_observation=False)
    await _run_with_runtime_heartbeat(
        config,
        controller.repository,
        lambda: _drive_controller_loop(controller, max_iterations=max_iterations, max_seconds=max_seconds),
        initial_state="BOOTING",
    )


async def _drive_controller_loop(
    controller: TradingController,
    *,
    max_iterations: int | None = None,
    max_seconds: float | None = None,
) -> Any:
    loop_task = controller.run_loop(max_iterations=max_iterations)
    if max_seconds is None:
        return await loop_task
    return await asyncio.wait_for(loop_task, timeout=max_seconds)


def _runtime_heartbeat_settings(config) -> tuple[float, str]:
    raw = config.raw.get("runtime", {}) if isinstance(getattr(config, "raw", None), dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        interval = float(raw.get("heartbeat_interval_seconds", 5))
    except (TypeError, ValueError):
        interval = 5.0
    return max(1.0, interval), str(getattr(config, "account_id", "default") or "default")


async def _run_with_runtime_heartbeat(
    config,
    repository: Repository,
    body,
    *,
    initial_state: str = "BOOTING",
) -> Any:
    now = datetime.now(timezone.utc)
    repository.register_runtime(
        TRADER_RUNTIME_ID,
        now,
        pid=os.getpid(),
        state=initial_state,
    )
    repository.close_unfinished_windows(now)
    interval, _account_id = _runtime_heartbeat_settings(config)
    stop_event = asyncio.Event()

    async def heartbeat_loop() -> None:
        while not stop_event.is_set():
            try:
                # 仅刷新存活时间。运行阶段和最近状态由交易循环写入，心跳不得
                # 用启动时捕获的 RECOVERING 覆盖 RUNNING/SCANNING。
                repository.update_runtime_heartbeat(
                    TRADER_RUNTIME_ID,
                    datetime.now(timezone.utc),
                )
            except Exception as exc:
                logger.warning("Failed to update trader runtime heartbeat: {}", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    try:
        repository.update_runtime_heartbeat(
            TRADER_RUNTIME_ID,
            datetime.now(timezone.utc),
            state="RECOVERING",
        )
        result = await body()
        repository.mark_runtime_stopped(
            TRADER_RUNTIME_ID,
            datetime.now(timezone.utc),
            state="STOPPED",
        )
        return result
    except Exception as exc:
        repository.mark_runtime_stopped(
            TRADER_RUNTIME_ID,
            datetime.now(timezone.utc),
            state="FAILED",
            last_error=str(exc),
        )
        raise
    finally:
        stop_event.set()
        await _cancel_and_drain_task(heartbeat_task)


class RuntimeStreamHealthReporter:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self._streams: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._last_persisted_message_at: dict[str, datetime] = {}

    async def handle(self, event: dict[str, Any]) -> None:
        stream = str(event.get("stream") or "unknown")
        raw_state = str(event.get("state") or "UNKNOWN").upper()
        at = event.get("at")
        if not isinstance(at, datetime):
            at = datetime.now(timezone.utc)
        async with self._lock:
            previous = dict(self._streams.get(stream, {}))
            state = "ONLINE" if raw_state == "MESSAGE" else raw_state
            current = {
                **previous,
                "stream": stream,
                "state": state,
                "reconnect_count": int(event.get("reconnect_count") or 0),
                "last_error": str(event.get("error") or ""),
                "updated_at": at.isoformat(),
            }
            if raw_state == "MESSAGE":
                current["last_message_at"] = at.isoformat()
            self._streams[stream] = current
            if raw_state == "MESSAGE":
                last_persisted = self._last_persisted_message_at.get(stream)
                if last_persisted is not None and (at - last_persisted).total_seconds() < 5:
                    return
                self._last_persisted_message_at[stream] = at
            self.repository.set_control_state(
                "stream_health",
                {
                    "streams": dict(self._streams),
                    "updated_at": at.isoformat(),
                },
                at,
            )
            if state in {"DEGRADED", "FAILED"}:
                self.repository.update_runtime_heartbeat(
                    TRADER_RUNTIME_ID,
                    at,
                    state="DEGRADED",
                    last_status=f"{stream}_reconnecting",
                    last_error=current["last_error"],
                )
            elif self.allows_new_entries():
                self.repository.update_runtime_heartbeat(
                    TRADER_RUNTIME_ID,
                    at,
                    state="RUNNING",
                    last_status="streams_healthy",
                    last_error="",
                )

    def allows_new_entries(self) -> bool:
        return not any(
            str(item.get("state") or "").upper() in {"DEGRADED", "FAILED"}
            for item in self._streams.values()
        )


async def _create_binance_client_for_module(config, module: str) -> BinanceFuturesClient:
    try:
        return await BinanceFuturesClient.create(
            api_key=config.binance_api_key,
            api_secret=config.binance_api_secret,
            testnet=config.binance_testnet,
            proxy_config=config.raw.get("proxy"),
        )
    except Exception as exc:
        _log_binance_client_create_failure(config, module, exc)
        raise


def _log_binance_client_create_failure(config, module: str, exc: Exception) -> None:
    try:
        _build_repository(config).log_system(
            "ERROR",
            module,
            "Binance testnet client creation failed.",
            _json_log_detail(
                {
                    "ok": False,
                    "stage": "client_create",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "proxy_enabled": _proxy_enabled(config.raw.get("proxy")),
                    "testnet": bool(config.binance_testnet),
                }
            ),
            datetime.now(timezone.utc),
        )
    except Exception as log_exc:
        logger.warning("Failed to persist Binance client creation failure: {}", log_exc)


def _run_backtest_csv(
    config,
    csv_path: Path,
    observe_rows: int,
    symbol: str,
    funding_rate: float,
    output_path: Path | None = None,
    funding_events: list[Any] | None = None,
) -> dict[str, Any]:
    rows = _read_backtest_csv(csv_path)
    if observe_rows < 1:
        raise RuntimeError("--backtest-observe-rows 必须为正整数。")
    if len(rows) <= observe_rows:
        raise RuntimeError("CSV K线数量必须大于观察期行数，才能留下回测区间。")
    observe_klines = rows[:observe_rows]
    backtest_klines = rows[observe_rows:]
    current_price = float(observe_klines[-1]["close"])
    params = calculate_grid_params(
        symbol=symbol,
        klines=observe_klines,
        current_price=current_price,
        funding_rate=funding_rate,
        config=_grid_config_from_raw(config.raw),
    )
    # 只把落在回测区间（观察期之后）的资金费事件传入；传 None 表示不启用事件模式。
    sliced_funding = (
        slice_funding_events_for_klines(funding_events, backtest_klines)
        if funding_events is not None
        else None
    )
    result = run_grid_backtest(
        params,
        backtest_klines,
        current_price=current_price,
        config=backtest_config_from_mapping(config.raw),
        funding_events=sliced_funding,
    )
    summary = _backtest_summary(result, observe_rows, len(backtest_klines))
    if output_path is not None:
        _write_backtest_report(output_path, _backtest_report(result, params, summary))
        summary["output_path"] = str(output_path)
    return summary


def _run_backtest_dir(
    config,
    csv_dir: Path,
    observe_rows: int,
    symbol: str,
    funding_rate: float,
    output_path: Path | None = None,
) -> dict[str, Any]:
    if not csv_dir.exists() or not csv_dir.is_dir():
        raise RuntimeError(f"回测CSV目录不存在: {csv_dir}")
    csv_paths = sorted(path for path in csv_dir.glob("*.csv") if path.is_file())
    if not csv_paths:
        raise RuntimeError(f"回测CSV目录没有CSV文件: {csv_dir}")

    reports: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for csv_path in csv_paths:
        try:
            summary = _run_backtest_csv(
                config,
                csv_path,
                observe_rows=observe_rows,
                symbol=symbol,
                funding_rate=funding_rate,
            )
        except Exception as exc:
            errors.append({"source_file": csv_path.name, "error": str(exc)})
            continue
        reports.append({"source_file": csv_path.name, **summary})

    if not reports:
        reasons = "; ".join(f"{item['source_file']}: {item['error']}" for item in errors)
        raise RuntimeError(f"批量回测没有成功样本: {reasons}")

    aggregate = _backtest_batch_summary(reports, errors)
    if output_path is not None:
        _write_backtest_report(output_path, {"summary": aggregate, "reports": reports, "errors": errors})
        aggregate["output_path"] = str(output_path)
    return aggregate


def _backtest_batch_summary(reports: list[dict[str, Any]], errors: list[dict[str, str]]) -> dict[str, Any]:
    total_pnl = sum(float(item.get("total_pnl", 0.0) or 0.0) for item in reports)
    max_drawdown = max(float(item.get("max_drawdown", 0.0) or 0.0) for item in reports)
    total_fills = sum(int(item.get("fills", 0) or 0) for item in reports)
    total_backtest_rows = sum(int(item.get("backtest_rows", 0) or 0) for item in reports)
    total_grid_trades = sum(int(item.get("grid_trade_count", 0) or 0) for item in reports)
    winning_grid_trades = sum(int(item.get("winning_grid_trades", 0) or 0) for item in reports)
    losing_grid_trades = sum(int(item.get("losing_grid_trades", 0) or 0) for item in reports)
    break_even_grid_trades = sum(int(item.get("break_even_grid_trades", 0) or 0) for item in reports)
    gross_grid_pnl = sum(float(item.get("gross_grid_pnl", 0.0) or 0.0) for item in reports)
    stopped_count = sum(1 for item in reports if item.get("stopped_reason"))
    return {
        "files": len(reports) + len(errors),
        "succeeded": len(reports),
        "failed": len(errors),
        "symbol": reports[0].get("symbol", ""),
        "total_pnl": total_pnl,
        "avg_total_pnl": total_pnl / len(reports),
        "max_drawdown": max_drawdown,
        "total_fills": total_fills,
        "total_grid_trades": total_grid_trades,
        "winning_grid_trades": winning_grid_trades,
        "losing_grid_trades": losing_grid_trades,
        "break_even_grid_trades": break_even_grid_trades,
        "win_rate": winning_grid_trades / total_grid_trades if total_grid_trades else 0.0,
        "avg_grid_pnl": gross_grid_pnl / total_grid_trades if total_grid_trades else 0.0,
        "fills_per_bar": total_fills / total_backtest_rows if total_backtest_rows else 0.0,
        "avg_equity_sharpe": sum(float(item.get("equity_sharpe", 0.0) or 0.0) for item in reports) / len(reports),
        "stopped_count": stopped_count,
        "best_file": max(reports, key=lambda item: float(item.get("total_pnl", 0.0) or 0.0))["source_file"],
        "worst_file": min(reports, key=lambda item: float(item.get("total_pnl", 0.0) or 0.0))["source_file"],
    }


def _read_backtest_csv(csv_path: Path) -> list[dict[str, Any]]:
    return read_legacy_backtest_csv(csv_path)


def _grid_config_from_raw(raw: dict[str, Any]) -> GridConfig:
    trading = raw.get("trading", {})
    grid = raw.get("grid", {})
    cooldown = raw.get("cooldown", {})
    return GridConfig(
        range_method=str(grid.get("range_method", "std")),
        std_k=float(grid.get("std_k", 1.8)),
        quantile_upper=float(grid.get("quantile_upper", 0.95)),
        quantile_lower=float(grid.get("quantile_lower", 0.05)),
        min_step_pct=float(grid.get("min_step_pct", 0.0015)),
        safety_multiplier=float(grid.get("safety_multiplier", 3.5)),
        max_grid_num=int(grid.get("max_grid_num", 20)),
        max_range_pct=float(grid.get("max_range_pct", 0.05)),
        atr_period=int(cooldown.get("atr_period", 14)),
        stop_buffer_pct=float(trading.get("stop_buffer_pct", 0.015)),
        volatility_refresh_seconds=float(grid.get("volatility_refresh_seconds", 60.0)),
        rolling_regrid_enabled=bool(grid.get("rolling_regrid_enabled", False)),
        rolling_regrid_seconds=float(grid.get("rolling_regrid_seconds", 7200.0)),
    )


def _backtest_summary(result: BacktestResult, observe_rows: int, backtest_rows: int) -> dict[str, Any]:
    grid_stats = _backtest_grid_trade_stats(result)
    inventory_values = [
        point.inventory_utilization
        for point in result.equity_curve
    ]
    return {
        "symbol": result.symbol,
        "observe_rows": observe_rows,
        "backtest_rows": backtest_rows,
        "fills": len(result.fills),
        "fills_per_bar": len(result.fills) / backtest_rows if backtest_rows else 0.0,
        **grid_stats,
        "gross_grid_pnl": result.gross_grid_pnl,
        "fees_paid": result.fees_paid,
        "realized_pnl": result.realized_pnl,
        "unrealized_pnl": result.unrealized_pnl,
        "total_pnl": result.total_pnl,
        "max_equity": result.max_equity,
        "max_drawdown": result.max_drawdown,
        "equity_sharpe": _simple_equity_sharpe(result),
        "sortino": _simple_equity_sortino(result),
        "calmar": (
            result.total_pnl / result.max_drawdown
            if result.max_drawdown > 0
            else 0.0
        ),
        "cvar_95": _equity_change_cvar(result, 0.95),
        "profit_factor": _profit_factor(result),
        "grid_fill_ratio": (
            len(result.fills) / result.attempted_fill_count
            if result.attempted_fill_count
            else 0.0
        ),
        "pair_completion_ratio": (
            result.pair_completion_count / len(result.fills)
            if result.fills
            else 0.0
        ),
        "inventory_p50": _quantile(inventory_values, 0.50),
        "inventory_p95": _quantile(inventory_values, 0.95),
        "inventory_p99": _quantile(inventory_values, 0.99),
        "max_inventory_utilization": result.max_inventory_utilization,
        "attempted_fill_count": result.attempted_fill_count,
        "rejected_fill_count": result.rejected_fill_count,
        "funding_paid": result.funding_paid,
        "stop_exit_cost": result.stop_exit_cost,
        "stop_exit_pnl": result.stop_exit_pnl,
        "net_position_qty": result.net_position_qty,
        "open_order_count": result.open_order_count,
        "stopped_reason": result.stopped_reason,
        "stopped_at_index": result.stopped_at_index,
        "stopped_at_price": result.stopped_at_price,
        "last_price": result.last_price,
    }


def _backtest_grid_trade_stats(result: BacktestResult) -> dict[str, Any]:
    closed_grid_pnls = [float(fill.grid_pnl) for fill in result.fills if fill.grid_pnl is not None]
    trade_count = len(closed_grid_pnls)
    winning = sum(1 for pnl in closed_grid_pnls if pnl > 0)
    losing = sum(1 for pnl in closed_grid_pnls if pnl < 0)
    break_even = trade_count - winning - losing
    return {
        "grid_trade_count": trade_count,
        "winning_grid_trades": winning,
        "losing_grid_trades": losing,
        "break_even_grid_trades": break_even,
        "win_rate": winning / trade_count if trade_count else 0.0,
        "avg_grid_pnl": sum(closed_grid_pnls) / trade_count if trade_count else 0.0,
    }


def _simple_equity_sharpe(result: BacktestResult) -> float:
    equity_changes = [
        result.equity_curve[index].equity - result.equity_curve[index - 1].equity
        for index in range(1, len(result.equity_curve))
    ]
    if len(equity_changes) < 2:
        return 0.0
    mean_change = sum(equity_changes) / len(equity_changes)
    variance = sum((change - mean_change) ** 2 for change in equity_changes) / (len(equity_changes) - 1)
    if variance <= 0:
        return 0.0
    return mean_change / sqrt(variance) * sqrt(len(equity_changes))


def _simple_equity_sortino(result: BacktestResult) -> float:
    changes = _equity_changes(result)
    if len(changes) < 2:
        return 0.0
    downside = [min(0.0, value) for value in changes]
    downside_variance = sum(value * value for value in downside) / len(downside)
    if downside_variance <= 0:
        return 0.0
    return (sum(changes) / len(changes)) / sqrt(downside_variance) * sqrt(len(changes))


def _equity_change_cvar(result: BacktestResult, confidence: float) -> float:
    losses = sorted(-value for value in _equity_changes(result) if value < 0)
    if not losses:
        return 0.0
    tail_start = max(0, int(len(losses) * confidence) - 1)
    tail = losses[tail_start:]
    return sum(tail) / len(tail)


def _profit_factor(result: BacktestResult) -> float:
    closed = [
        float(fill.grid_pnl)
        for fill in result.fills
        if fill.grid_pnl is not None
    ]
    profit = sum(value for value in closed if value > 0)
    loss = abs(sum(value for value in closed if value < 0))
    if loss > 0:
        return profit / loss
    return profit if profit > 0 else 0.0


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _equity_changes(result: BacktestResult) -> list[float]:
    return [
        result.equity_curve[index].equity
        - result.equity_curve[index - 1].equity
        for index in range(1, len(result.equity_curve))
    ]


def _backtest_report(result: BacktestResult, params, summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": dict(summary),
        "grid_params": {
            "symbol": params.symbol,
            "upper": params.upper,
            "lower": params.lower,
            "center": params.center,
            "grid_num": params.grid_num,
            "step_pct": params.step_pct,
            "grid_prices": list(params.grid_prices),
            "baseline_atr": params.baseline_atr,
            "stop_loss_price": params.stop_loss_price,
            "volatility_method": params.volatility_method,
            "volatility_value": params.volatility_value,
            "volatility_window": params.volatility_window,
            "calculated_at": params.calculated_at.isoformat(),
        },
        "fills": [
            {
                "symbol": fill.symbol,
                "side": fill.side,
                "grid_index": fill.grid_index,
                "price": fill.price,
                "qty": fill.qty,
                "fee": fill.fee,
                "grid_pnl": fill.grid_pnl,
                "realized_pnl_after": fill.realized_pnl_after,
                "bar_index": fill.bar_index,
                "timestamp": fill.timestamp,
            }
            for fill in result.fills
        ],
        "equity_curve": [
            {
                "bar_index": point.bar_index,
                "equity": point.equity,
                "realized_pnl": point.realized_pnl,
                "unrealized_pnl": point.unrealized_pnl,
                "drawdown": point.drawdown,
                "close": point.close,
                "timestamp": point.timestamp,
                "gross_inventory_notional": point.gross_inventory_notional,
                "inventory_utilization": point.inventory_utilization,
            }
            for point in result.equity_curve
        ],
    }


def _write_backtest_report(output_path: Path, report: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def _run_binance_once(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-once 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_once")
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        await _require_binance_signed_write_health(exchange, config, eligible, "binance_once")
        controller = _build_controller(exchange, config, live_observation=True)
        try:
            check = await controller.validate_startup()
            if not check.ok:
                raise RuntimeError(check.reason)
            await controller.recover_unclosed_sessions(recoverable_symbols=set(eligible))
            return await controller.run_once()
        finally:
            closed = await _close_all_active_sessions_or_raise(controller, "binance_once_cleanup")
            if closed:
                logger.info("Binance once cleanup closed active sessions: {}", closed)
    finally:
        await exchange.close()


async def _run_binance_check(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-check 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_check")
    try:
        await _require_binance_tradable_allowlist_symbols(exchange, config)
        controller = _build_controller(exchange, config, live_observation=False)
        check = await controller.validate_startup()
        symbols = await exchange.get_symbols()
        tradable = [item["symbol"] for item in symbols if item.get("status") == "TRADING"]
        eligible = await controller.selector.candidate_symbols()
        sample_symbol = eligible[0] if eligible else None
        sample_rules = await exchange.get_symbol_rules(sample_symbol) if sample_symbol else {}
        commission_health = await _binance_commission_health(exchange, config, eligible, datetime.now(timezone.utc))
        sample_commission = commission_health["symbols"][0]["commission"] if commission_health["symbols"] else {}
        controller.repository.log_system(
            "INFO",
            "binance_check",
            "Binance testnet check completed.",
            (
                f"symbols={len(tradable)}, eligible={len(eligible)}, sample={sample_symbol}, "
                f"commission={sample_commission}, commission_health={commission_health['status']}"
            ),
            datetime.now(timezone.utc),
        )
        return {
            "startup_ok": check.ok,
            "reason": check.reason,
            "balance": check.balance,
            "tradable_symbols": len(tradable),
            "eligible_symbols": len(eligible),
            "sample_symbol": sample_symbol,
            "sample_rules": sample_rules,
            "sample_commission": sample_commission,
            "commission_health": commission_health,
        }
    finally:
        await exchange.close()


async def _run_binance_order_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-order-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_order_smoke")
    attempted_symbols: list[str] = []
    failures: list[str] = []
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        leverage = int(config.raw.get("trading", {}).get("leverage", 1))
        for symbol in eligible:
            attempted_symbols.append(symbol)
            try:
                result = await _run_binance_order_smoke_for_symbol(exchange, symbol, leverage)
            except Exception as exc:
                failures.append(f"{symbol}: {exc}")
                continue
            result["attempted_symbols"] = list(attempted_symbols)
            if failures:
                result["attempt_failures"] = list(failures)
            return result
        raise RuntimeError(f"所有测试网订单烟测标的均失败: {'; '.join(failures)}")
    finally:
        await exchange.close()


async def _run_binance_test_order_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-test-order-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_test_order_smoke")
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        symbol = eligible[0]
        params = await _smoke_order_params(exchange, symbol)
        suffix = uuid4().hex[:12]
        limit_response = await exchange.test_limit_order_post_only(
            symbol,
            "BUY",
            params["limit_price"],
            params["qty"],
            f"qgtest-l-{suffix}",
            position_side="LONG",
        )
        market_response = await exchange.test_market_order(
            symbol,
            "SELL",
            params["qty"],
            reduce_only=True,
        )
        stop_response = None
        stop_error = None
        try:
            stop_response = await exchange.test_stop_market_order(
                symbol,
                "SELL",
                params["stop_price"],
                f"qgtest-s-{suffix}",
                close_position=True,
            )
        except Exception as exc:
            stop_error = str(exc)
        result = {
            "test_order_ok": True,
            "symbol": symbol,
            "last_price": params["last_price"],
            "limit_price": params["limit_price"],
            "stop_price": params["stop_price"],
            "qty": params["qty"],
            "limit_response": limit_response,
            "market_response": market_response,
            "stop_response": stop_response,
            "stop_supported": stop_response is not None,
            "stop_error": stop_error,
        }
        _build_repository(config).log_system(
            "INFO" if result["test_order_ok"] and result["stop_supported"] else "WARN",
            "binance_test_order_smoke",
            "Binance testnet order/test smoke completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        await exchange.close()


async def _run_binance_market_roundtrip_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-market-roundtrip-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_market_roundtrip_smoke")
    symbol: str | None = None
    cleanup_errors: list[str] = []
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        symbol = eligible[0]
        leverage = int(config.raw.get("trading", {}).get("leverage", 1))
        setup_warnings = []
        try:
            await exchange.set_margin_type(symbol, "ISOLATED")
        except Exception as exc:
            setup_warnings.append(f"set margin type failed: {exc}")
        try:
            await exchange.set_leverage(symbol, leverage)
        except Exception as exc:
            setup_warnings.append(f"set leverage failed: {exc}")

        params = await _smoke_order_params(exchange, symbol)
        qty = params["qty"]
        mode = await exchange.get_position_mode()
        position_side = "LONG" if bool(mode.get("dualSidePosition")) else None
        suffix = uuid4().hex[:12]
        open_client_id = f"qgmkt-o-{suffix}"
        close_client_id = f"qgmkt-c-{suffix}"

        open_order = await _place_market_order_reconciled(
            exchange,
            symbol,
            "BUY",
            qty,
            reduce_only=False,
            position_side=position_side,
            client_id=open_client_id,
        )
        open_order_id = _required_order_id(open_order, "market open order")

        close_order = await _place_market_order_reconciled(
            exchange,
            symbol,
            "SELL",
            qty,
            reduce_only=True,
            position_side=position_side,
            client_id=close_client_id,
        )
        close_order_id = _required_order_id(close_order, "market close order")

        position_after_close = await exchange.get_position(symbol)
        after_summary = _position_sweep_summary(position_after_close)
        if _position_sweep_exposure(after_summary) > 1e-12:
            raise RuntimeError(f"Market roundtrip smoke close left residual position: {after_summary}")
        result = {
            "market_roundtrip_ok": True,
            "symbol": symbol,
            "leverage": leverage,
            "setup_warnings": setup_warnings,
            "last_price": params["last_price"],
            "qty": qty,
            "position_side": position_side,
            "open_client_id": open_client_id,
            "open_order_id": open_order_id,
            "close_client_id": close_client_id,
            "close_order_id": close_order_id,
            "position_after_close": after_summary,
        }
        _build_repository(config).log_system(
            "INFO",
            "binance_market_roundtrip_smoke",
            "Binance testnet market roundtrip smoke completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        if symbol is not None:
            cleanup_errors = await _sweep_symbol_orders_and_positions(exchange, symbol)
            if cleanup_errors:
                logger.warning("Market roundtrip smoke cleanup errors: {}", cleanup_errors)
        await exchange.close()


async def _run_binance_direct_order_diagnose(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-direct-order-diagnose 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_direct_order_diagnose")
    symbol: str | None = None
    order_id: str | None = None
    cleanup_errors: list[str] = []
    result: dict[str, Any] | None = None
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        symbol = eligible[0]
        leverage = int(config.raw.get("trading", {}).get("leverage", 1))
        setup_warnings = []
        try:
            await exchange.set_margin_type(symbol, "ISOLATED")
        except Exception as exc:
            setup_warnings.append(f"set margin type failed: {exc}")
        try:
            await exchange.set_leverage(symbol, leverage)
        except Exception as exc:
            setup_warnings.append(f"set leverage failed: {exc}")

        params = await _smoke_order_params(exchange, symbol)
        mode = await exchange.get_position_mode()
        position_side = "LONG" if bool(mode.get("dualSidePosition")) else None
        client_id = f"qgraw-l-{uuid4().hex[:12]}"
        order_params: dict[str, Any] = {
            "symbol": symbol,
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTX",
            "quantity": _binance_number_string(params["qty"]),
            "price": _binance_number_string(params["limit_price"]),
            "newClientOrderId": client_id,
        }
        if position_side is not None:
            order_params["positionSide"] = position_side

        direct_response = await _binance_direct_signed_request(config, "POST", "/fapi/v1/order", order_params)
        response_body = direct_response.get("json")
        endpoint_ok = bool(direct_response.get("ok"))
        recovered_order = None
        if endpoint_ok and isinstance(response_body, dict):
            order_id = _order_id_or_none(response_body)
        if not endpoint_ok:
            error_text = _binance_direct_error_text(direct_response)
            recovered_order = await _recover_order_by_client_id_after_create_exception(
                exchange,
                symbol,
                client_id,
                RuntimeError(error_text),
            )
            if recovered_order is not None:
                order_id = _order_id_or_none(recovered_order)
                endpoint_ok = order_id is not None

        if order_id is not None:
            cleanup_errors.extend(await _cleanup_smoke_orders(exchange, symbol, [order_id]))
        cleanup_errors.extend(await _sweep_symbol_orders_and_positions(exchange, symbol))

        result = {
            "direct_order_diagnose_ok": not cleanup_errors,
            "endpoint_order_ok": endpoint_ok,
            "symbol": symbol,
            "proxy_enabled": _proxy_enabled(config.raw.get("proxy")),
            "leverage": leverage,
            "setup_warnings": setup_warnings,
            "last_price": params["last_price"],
            "limit_price": params["limit_price"],
            "qty": params["qty"],
            "position_side": position_side,
            "client_id": client_id,
            "http_status": direct_response.get("status_code"),
            "response": _binance_direct_public_response(direct_response),
            "recovered_order_status": recovered_order.get("status") if recovered_order is not None else None,
            "recovered_order_id": order_id if recovered_order is not None else None,
            "cleanup_errors": cleanup_errors,
        }
        _build_repository(config).log_system(
            "INFO" if result["direct_order_diagnose_ok"] and result["endpoint_order_ok"] else "ERROR",
            "binance_direct_order_diagnose",
            "Binance direct REST order endpoint diagnose completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        if cleanup_errors:
            raise RuntimeError(f"直接 REST 下单诊断清理失败: {'; '.join(cleanup_errors)}")
        return result
    finally:
        if symbol is not None and result is None:
            cleanup_errors = await _sweep_symbol_orders_and_positions(exchange, symbol)
            if cleanup_errors:
                logger.warning("Direct order diagnose cleanup errors: {}", cleanup_errors)
        await exchange.close()


async def _binance_commission_health(exchange, config, symbols: list[str], at: datetime) -> dict[str, Any]:
    max_maker_fee_rate = float(config.raw.get("trading", {}).get("max_maker_fee_rate", 0.0))
    details: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            commission = await exchange.get_commission_rate(symbol)
            maker_fee = _commission_maker_fee(commission)
        except Exception as exc:
            details.append(
                {
                    "symbol": symbol,
                    "status": "error",
                    "commission": {},
                    "error": str(exc),
                }
            )
            continue
        status = "ok" if maker_fee <= max_maker_fee_rate else "warn"
        details.append(
            {
                "symbol": symbol,
                "status": status,
                "maker": maker_fee,
                "max_maker_fee_rate": max_maker_fee_rate,
                "commission": commission,
            }
        )

    error_count = sum(1 for item in details if item["status"] == "error")
    warn_count = sum(1 for item in details if item["status"] == "warn")
    status = "error" if error_count else "warn" if warn_count else "ok"
    result = {
        "status": status,
        "max_maker_fee_rate": max_maker_fee_rate,
        "checked_symbols": len(details),
        "ok_count": sum(1 for item in details if item["status"] == "ok"),
        "warn_count": warn_count,
        "error_count": error_count,
        "symbols": details,
    }
    _build_repository(config).log_system(
        "ERROR" if status == "error" else "WARN" if status == "warn" else "INFO",
        "commission_health",
        "Binance maker fee health check completed.",
        _json_log_detail(result),
        at,
    )
    return result


def _commission_maker_fee(commission: dict[str, Any]) -> float:
    if "maker" not in commission:
        raise ValueError("commission response missing maker")
    try:
        maker = float(commission["maker"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid maker commission: {commission.get('maker')}") from exc
    if not isfinite(maker) or maker < 0:
        raise ValueError(f"invalid maker commission: {commission.get('maker')}")
    return maker


async def _require_binance_signed_write_health(
    exchange,
    config,
    eligible_symbols: list[str],
    caller: str,
) -> dict[str, Any]:
    symbol = eligible_symbols[0]
    leverage = int(config.raw.get("trading", {}).get("leverage", 1))
    errors: list[str] = []
    try:
        await exchange.set_margin_type(symbol, "ISOLATED")
    except Exception as exc:
        errors.append(f"set margin type failed: {exc}")
    try:
        await exchange.set_leverage(symbol, leverage)
    except Exception as exc:
        errors.append(f"set leverage failed: {exc}")

    result = {
        "signed_write_ok": not errors,
        "caller": caller,
        "symbol": symbol,
        "leverage": leverage,
        "proxy_enabled": _proxy_enabled(config.raw.get("proxy")),
        "errors": errors,
    }
    _build_repository(config).log_system(
        "INFO" if result["signed_write_ok"] else "ERROR",
        "binance_signed_write_health",
        f"Binance signed write health check {'passed' if result['signed_write_ok'] else 'failed'} before {caller}.",
        _json_log_detail(result),
        datetime.now(timezone.utc),
    )
    if errors:
        raise RuntimeError(f"Binance signed write health check failed before {caller}: {'; '.join(errors)}")
    return result


async def _run_binance_signed_write_health(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-signed-write-health 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_signed_write_health")
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        return await _require_binance_signed_write_health(exchange, config, eligible, "binance_signed_write_health")
    finally:
        await exchange.close()


async def _run_binance_listen_key_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-listen-key-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_listen_key_smoke")
    listen_key: str | None = None
    try:
        listen_key = await exchange.create_futures_listen_key()
        await exchange.keepalive_futures_listen_key(listen_key)
        result = {"listen_key_ok": True, "listen_key_length": len(listen_key)}
        _build_repository(config).log_system(
            "INFO",
            "binance_listen_key_smoke",
            "Binance testnet listenKey smoke completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        if listen_key:
            await exchange.close_futures_listen_key(listen_key)
        await exchange.close()


async def _run_binance_algo_stop_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-algo-stop-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_algo_stop_smoke")
    symbol: str | None = None
    algo_id: int | str | None = None
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        symbol = eligible[0]
        params = await _smoke_order_params(exchange, symbol)
        mode = await exchange.get_position_mode()
        position_side = "SHORT" if bool(mode.get("dualSidePosition")) else "BOTH"
        client_algo_id = f"qgalgo-{uuid4().hex[:12]}"
        response = await exchange.place_algo_stop_market_order(
            symbol,
            "SELL",
            position_side,
            params["stop_price"],
            params["qty"],
            client_algo_id,
        )
        algo_id = response["algoId"]
        open_orders = await exchange.get_open_algo_orders(symbol)
        open_seen = any(str(order.get("algoId")) == str(algo_id) for order in open_orders)
        cancelled_algo_id = algo_id
        cancel_response = await exchange.cancel_algo_order(symbol, algo_id)
        algo_id = None
        remaining = [
            order
            for order in await exchange.get_open_algo_orders(symbol)
            if str(order.get("algoId")) == str(algo_id)
            or str(order.get("clientAlgoId", "")).startswith("qgalgo-")
        ]
        result = {
            "algo_stop_ok": True,
            "symbol": symbol,
            "position_side": position_side,
            "trigger_price": params["stop_price"],
            "qty": params["qty"],
            "algo_id": cancelled_algo_id,
            "open_seen": open_seen,
            "cancel_response": cancel_response,
            "remaining_open": len(remaining),
        }
        _build_repository(config).log_system(
            "INFO" if result["remaining_open"] == 0 else "WARN",
            "binance_algo_stop_smoke",
            "Binance testnet algo stop smoke completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        if symbol is not None and algo_id is not None:
            try:
                await exchange.cancel_algo_order(symbol, algo_id)
            except Exception:
                pass
        await exchange.close()


async def _run_binance_position_smoke(config):
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行持仓只读检查需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_position_smoke")
    try:
        eligible = await _require_binance_smoke_symbols(exchange, config)
        mode = await exchange.get_position_mode()
        symbols = []
        for symbol in eligible:
            position = await exchange.get_position(symbol)
            open_orders = await exchange.get_open_orders(symbol)
            open_algo_orders = await exchange.get_open_algo_orders(symbol)
            symbols.append(
                {
                    "symbol": symbol,
                    "qty": position.get("qty", 0.0),
                    "long_qty": position.get("long_qty", 0.0),
                    "short_qty": position.get("short_qty", 0.0),
                    "position_rows": len(position.get("positions", [])),
                    "ordinary_open": len(open_orders),
                    "algo_open": len(open_algo_orders),
                }
            )
        result = {
            "position_smoke_ok": True,
            "dual_side_position": bool(mode.get("dualSidePosition")),
            "symbols": symbols,
        }
        _build_repository(config).log_system(
            "INFO",
            "binance_position_smoke",
            "Binance current-environment position check completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        await exchange.close()


async def _run_binance_safety_sweep(config):
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行安全清扫需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    repository = _build_repository(config)
    exchange = await _create_binance_client_for_module(config, "binance_safety_sweep")
    try:
        eligible = await _require_binance_smoke_symbols(exchange, config)
        result = await _sweep_binance_symbols(exchange, repository, eligible)
        repository.log_system(
            "INFO",
            "binance_safety_sweep",
            "Binance current-environment safety sweep completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        await exchange.close()


async def _run_binance_test_run(config, max_seconds: float = DEFAULT_BOUNDED_RUN_SECONDS) -> dict[str, Any]:
    if max_seconds <= 0:
        raise RuntimeError("有界运行秒数必须是正数。")
    pre_position = await _run_binance_position_smoke(config)
    repository = _build_repository(config)
    repository.register_runtime(
        TRADER_RUNTIME_ID,
        datetime.now(timezone.utc),
        pid=os.getpid(),
        state="BOOTING",
    )
    repository.request_round_start(
        "bounded_run_diagnostic",
        f"bounded-{uuid4()}",
        datetime.now(timezone.utc),
    )
    loop_result: Any = None
    loop_error: str | None = None
    try:
        loop_result = await _run_binance_loop(config, max_seconds=max_seconds)
    except Exception as exc:
        loop_error = str(exc)
        raise
    finally:
        safety_sweep = await _run_binance_safety_sweep(config)
        post_position = await _run_binance_position_smoke(config)
        result = {
            "test_run_ok": loop_error is None,
            "max_seconds": max_seconds,
            "pre_position": pre_position,
            "loop_result": loop_result,
            "loop_error": loop_error,
            "safety_sweep": safety_sweep,
            "post_position": post_position,
        }
        repository.log_system(
            "INFO" if loop_error is None else "ERROR",
            "binance_test_run",
            (
                "Binance current-environment bounded run completed."
                if loop_error is None
                else "Binance current-environment bounded run failed after cleanup."
            ),
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
    return result


async def _sweep_binance_symbols(exchange, repository: Repository, eligible: list[str]) -> dict[str, Any]:
    results = []
    residuals: list[str] = []
    for symbol in eligible:
        ordinary_orders = await exchange.get_open_orders(symbol)
        algo_orders = await _open_algo_orders(exchange, symbol)
        ordinary_before = len(ordinary_orders)
        algo_before = len(algo_orders)
        position_before = await exchange.get_position(symbol)
        close_specs = _position_close_specs(position_before)

        if ordinary_before or algo_before:
            try:
                await exchange.cancel_all_orders(symbol)
            except Exception as exc:
                fallback_errors = await _cancel_sweep_orders(exchange, symbol, ordinary_orders, algo_orders)
                if fallback_errors:
                    raise RuntimeError(
                        (
                            f"当前环境安全清扫全撤失败且逐单撤单未完全成功: symbol={symbol}, "
                            f"cancel_all_error={exc}, fallback_errors={fallback_errors}"
                        )
                    ) from None
        closed_positions = await _close_sweep_position_specs(exchange, symbol, close_specs)

        ordinary_after = len(await exchange.get_open_orders(symbol))
        algo_after = len(await _open_algo_orders(exchange, symbol))
        position_after = await exchange.get_position(symbol)
        after_summary = _position_sweep_summary(position_after)
        if ordinary_after or algo_after or _position_sweep_exposure(after_summary) > 1e-12:
            residuals.append(
                (
                    f"{symbol}: ordinary_after={ordinary_after}, algo_after={algo_after}, "
                    f"position_after={after_summary}"
                )
            )
        results.append(
            {
                "symbol": symbol,
                "ordinary_before": ordinary_before,
                "algo_before": algo_before,
                "ordinary_after": ordinary_after,
                "algo_after": algo_after,
                "position_before": _position_sweep_summary(position_before),
                "position_after": after_summary,
                "closed_positions": closed_positions,
            }
        )
    if residuals:
        repository.log_system(
            "ERROR",
            "binance_safety_sweep",
            "Binance current-environment safety sweep left residual exposure.",
            _json_log_detail({"symbols": results, "residuals": residuals}),
            datetime.now(timezone.utc),
        )
        raise RuntimeError(f"当前环境安全清扫后仍有残留: {'; '.join(residuals)}")
    closed_sessions = _close_unclosed_sessions_after_safety_sweep(repository, eligible, datetime.now(timezone.utc))
    return {
        "safety_sweep_ok": True,
        "symbols": results,
        "closed_sessions": closed_sessions,
    }


def _close_unclosed_sessions_after_safety_sweep(
    repository: Repository,
    swept_symbols: list[str],
    at: datetime,
) -> list[dict[str, Any]]:
    swept = {symbol.upper() for symbol in swept_symbols}
    closed: list[dict[str, Any]] = []
    for row in repository.unclosed_sessions():
        symbol = str(row["symbol"])
        if symbol.upper() not in swept:
            continue
        session_id = int(row["id"])
        from_state = str(row["state"])
        repository.close_session(session_id, "binance_safety_sweep", at)
        repository.log_state(
            session_id,
            symbol,
            from_state,
            "STOPPED",
            "binance_safety_sweep",
            "当前环境安全清扫完成后同步关闭数据库未结束会话。",
            at,
        )
        closed.append({"session_id": session_id, "symbol": symbol, "from_state": from_state})
    return closed


async def _run_binance_price_stream_smoke(config, timeout_seconds: float = 45):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-price-stream-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await _create_binance_client_for_module(config, "binance_price_stream_smoke")
    event_holder: list[dict] = []
    try:
        symbol = _first_configured_binance_symbol(config)

        async def handler(event):
            event_holder.append(event)
            raise _SmokeComplete

        try:
            await asyncio.wait_for(
                exchange.run_price_stream([symbol], handler, reconnect_delay_seconds=1, max_reconnects=2),
                timeout=timeout_seconds,
            )
        except _SmokeComplete:
            pass
        if not event_holder:
            raise RuntimeError("Binance price stream smoke timed out without receiving an event.")
        result = {
            "stream_ok": True,
            "symbol": symbol,
            "event": event_holder[0],
        }
        _build_repository(config).log_system(
            "INFO",
            "binance_price_stream_smoke",
            "Binance testnet price stream smoke completed.",
            _json_log_detail(result),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        await exchange.close()


async def _run_binance_order_smoke_for_symbol(exchange, symbol: str, leverage: int):
    limit_order_id: str | None = None
    stop_order_id: str | None = None
    order_cleanup_required = False
    result = None
    operation_error: Exception | None = None
    cleanup_errors: list[str] = []
    try:
        setup_warnings = []
        margin_type_ok = True
        leverage_ok = True
        try:
            await exchange.set_margin_type(symbol, "ISOLATED")
        except Exception as exc:
            margin_type_ok = False
            setup_warnings.append(f"set margin type failed: {exc}")
        try:
            await exchange.set_leverage(symbol, leverage)
        except Exception as exc:
            leverage_ok = False
            setup_warnings.append(f"set leverage failed: {exc}")

        params = await _smoke_order_params(exchange, symbol)
        last_price = params["last_price"]
        limit_price = params["limit_price"]
        stop_price = params["stop_price"]
        qty = params["qty"]
        if limit_price <= 0 or stop_price <= 0 or qty <= 0:
            raise RuntimeError("测试网订单烟测计算出无效价格或数量。")

        suffix = uuid4().hex[:12]
        limit_client_id = f"qgsm-l-{suffix}"
        stop_client_id = f"qgsm-s-{suffix}"
        order_cleanup_required = True
        limit_order = await _place_limit_order_post_only_reconciled(
            exchange,
            symbol,
            "BUY",
            limit_price,
            qty,
            limit_client_id,
            position_side="LONG",
        )
        limit_order_id = _required_order_id(limit_order, "limit order")
        limit_lookup = await exchange.get_order(symbol, limit_order_id, limit_client_id)

        stop_order = await _place_stop_market_order_reconciled(
            exchange,
            symbol,
            "SELL",
            stop_price,
            stop_client_id,
            close_position=True,
        )
        stop_order_id = _required_order_id(stop_order, "stop market order")
        stop_lookup = await exchange.get_order(symbol, stop_order_id, stop_client_id)

        result = {
            "smoke_ok": True,
            "symbol": symbol,
            "leverage": leverage,
            "margin_type_ok": margin_type_ok,
            "leverage_ok": leverage_ok,
            "setup_warnings": setup_warnings,
            "last_price": last_price,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "qty": qty,
            "limit_order_id": limit_order_id,
            "limit_status": limit_lookup.get("status"),
            "stop_order_id": stop_order_id,
            "stop_status": stop_lookup.get("status"),
        }
    except Exception as exc:
        operation_error = exc
    finally:
        if order_cleanup_required:
            cleanup_errors.extend(
                await _cleanup_smoke_orders(
                    exchange,
                    symbol,
                    [order_id for order_id in (limit_order_id, stop_order_id) if order_id],
                )
            )
    if operation_error is not None:
        if cleanup_errors:
            logger.warning("Binance order smoke cleanup errors after operation failure: {}", cleanup_errors)
        raise operation_error
    if cleanup_errors:
        raise RuntimeError(f"测试网订单烟测清理失败: {'; '.join(cleanup_errors)}")
    return result


async def _run_binance_loop(config, max_iterations: int | None = None, max_seconds: float | None = None):
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 Binance loop 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    repository = _build_repository(config)

    async def _body() -> Any:
        exchange = await _create_binance_client_for_module(config, "binance_loop")
        bounded_timeout_reached = False
        try:
            eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
            await _require_binance_signed_write_health(exchange, config, eligible, "binance_loop")
            controller = _build_controller(exchange, config, live_observation=True)
            controller_repository = getattr(controller, "repository", None) or repository
            if not hasattr(controller, "repository"):
                controller.repository = controller_repository
            check = await controller.validate_startup()
            if not check.ok:
                raise RuntimeError(check.reason)
            await controller.recover_unclosed_sessions(recoverable_symbols=set(eligible))
            controller_repository.close_unfinished_windows(datetime.now(timezone.utc))
            controller_repository.update_runtime_heartbeat(
                TRADER_RUNTIME_ID,
                datetime.now(timezone.utc),
                state="RUNNING",
                last_status="recovered",
            )
            stream_health = RuntimeStreamHealthReporter(controller_repository)
            controller.stream_health_reporter = stream_health
            user_stream_task = asyncio.create_task(
                _run_supervised_user_stream(exchange, controller, stream_health)
            )
            dynamic_stream_kwargs: dict[str, Any] = {}
            if _supports_named_parameter(_run_dynamic_price_stream, "stream_health"):
                dynamic_stream_kwargs["stream_health"] = stream_health
            price_stream_task = asyncio.create_task(
                _run_dynamic_price_stream(
                    exchange,
                    controller,
                    poll_seconds=float(config.raw["timing"]["loop_interval_seconds"]),
                    **dynamic_stream_kwargs,
                )
            )
            controller_task = asyncio.create_task(controller.run_loop(max_iterations=max_iterations))
            task_names = {
                controller_task: "controller loop",
                user_stream_task: "user stream",
                price_stream_task: "price stream",
            }
            try:
                done, _pending = await asyncio.wait(
                    {controller_task, user_stream_task, price_stream_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=max_seconds,
                )
                if not done:
                    bounded_timeout_reached = True
                    _log_binance_loop_bounded_timeout(controller, max_seconds)
                    return ["loop_timeout"]
                completed = next(iter(done))
                completed_name = task_names[completed]
                try:
                    result = await completed
                except Exception as exc:
                    _log_binance_loop_task_error(controller, completed_name, exc)
                    raise
                if completed is not controller_task:
                    message = f"Binance {completed_name} stopped unexpectedly."
                    _log_binance_loop_task_error(controller, completed_name, RuntimeError(message))
                    raise RuntimeError(message)
                return result
            finally:
                await _cancel_and_drain_task(controller_task)
                await _cancel_and_drain_task(user_stream_task)
                await _cancel_and_drain_task(price_stream_task)
                fallback_swept = False
                try:
                    closed = await asyncio.wait_for(
                        _close_all_active_sessions_or_raise(controller, "binance_loop_shutdown_cleanup"),
                        timeout=BINANCE_LOOP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS,
                    )
                    if closed:
                        logger.info("Binance loop shutdown cleanup closed active sessions: {}", closed)
                except Exception as exc:
                    _log_binance_loop_shutdown_cleanup_fallback(controller, exc)
                    fallback = await _run_binance_loop_safety_sweep_fallback(exchange, controller, eligible)
                    fallback_swept = fallback is not None
                    if fallback is not None:
                        logger.warning("Binance loop shutdown cleanup fallback safety sweep result: {}", fallback)
                if bounded_timeout_reached and not fallback_swept:
                    fallback = await _run_binance_loop_safety_sweep_fallback(exchange, controller, eligible)
                    if fallback is not None:
                        logger.info("Binance bounded loop final safety sweep result: {}", fallback)
        finally:
            await exchange.close()

    return await _run_with_runtime_heartbeat(config, repository, _body, initial_state="BOOTING")


def _log_binance_loop_task_error(controller: TradingController, task_name: str, exc: Exception) -> None:
    repository = getattr(controller, "repository", None)
    log_system = getattr(repository, "log_system", None)
    if log_system is None:
        return
    try:
        log_system(
            "ERROR",
            "binance_loop",
            f"Binance {task_name} stopped unexpectedly.",
            str(exc),
            datetime.now(timezone.utc),
        )
    except Exception as log_exc:
        logger.warning("Failed to persist Binance loop task error: {}", log_exc)


def _log_binance_loop_bounded_timeout(controller: TradingController, max_seconds: float | None) -> None:
    repository = getattr(controller, "repository", None)
    log_system = getattr(repository, "log_system", None)
    if log_system is None:
        return
    try:
        log_system(
            "INFO",
            "binance_loop",
            "Binance loop bounded runtime reached; shutting down.",
            f"max_seconds={max_seconds}",
            datetime.now(timezone.utc),
        )
    except Exception as log_exc:
        logger.warning("Failed to persist Binance loop bounded timeout: {}", log_exc)


def _log_binance_loop_shutdown_cleanup_fallback(controller: TradingController, exc: Exception) -> None:
    repository = getattr(controller, "repository", None)
    log_system = getattr(repository, "log_system", None)
    if log_system is None:
        return
    try:
        log_system(
            "WARN",
            "binance_loop",
            "Binance loop shutdown cleanup failed; running safety sweep fallback.",
            str(exc),
            datetime.now(timezone.utc),
        )
    except Exception as log_exc:
        logger.warning("Failed to persist Binance loop shutdown cleanup fallback: {}", log_exc)


async def _run_binance_loop_safety_sweep_fallback(exchange, controller: TradingController, eligible: list[str]) -> dict[str, Any] | None:
    repository = getattr(controller, "repository", None)
    if repository is None:
        return None
    fallback = await _sweep_binance_symbols(exchange, repository, eligible)
    repository.log_system(
        "INFO",
        "binance_safety_sweep",
        "Binance current-environment safety sweep completed.",
        _json_log_detail(fallback),
        datetime.now(timezone.utc),
    )
    return fallback


async def _run_supervised_user_stream(
    exchange,
    controller: TradingController,
    stream_health: RuntimeStreamHealthReporter,
) -> None:
    reconnect_delay = 1.0
    while True:
        try:
            kwargs: dict[str, Any] = {}
            if _supports_lifecycle_handler(exchange.run_user_stream):
                kwargs["lifecycle_handler"] = stream_health.handle
            await exchange.run_user_stream(
                controller.handle_order_filled_event,
                **kwargs,
            )
            await stream_health.handle(
                {
                    "stream": "user",
                    "state": "DEGRADED",
                    "error": "User Stream 已结束，正在重新连接。",
                    "at": datetime.now(timezone.utc),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await stream_health.handle(
                {
                    "stream": "user",
                    "state": "DEGRADED",
                    "error": str(exc),
                    "at": datetime.now(timezone.utc),
                }
            )
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(30.0, reconnect_delay * 2)


async def _run_dynamic_price_stream(
    exchange,
    controller: TradingController,
    poll_seconds: float = 10,
    *,
    stream_health: RuntimeStreamHealthReporter | None = None,
) -> None:
    poll_seconds = min(max(float(poll_seconds), 0.1), 1.0)
    subscribed_symbols: tuple[str, ...] = ()
    price_stream_task: asyncio.Task | None = None
    kline_stream_task: asyncio.Task | None = None
    try:
        while True:
            market_symbols = getattr(controller, "market_stream_symbols", None)
            active_symbols = tuple(
                market_symbols() if callable(market_symbols) else sorted(controller.active_sessions)
            )
            if price_stream_task is not None and price_stream_task.done():
                try:
                    await price_stream_task
                    detail = "Binance price stream 已结束，正在重新连接。"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    detail = str(exc)
                if stream_health is not None:
                    await stream_health.handle(
                        {
                            "stream": "price",
                            "state": "DEGRADED",
                            "error": detail,
                            "at": datetime.now(timezone.utc),
                        }
                    )
                price_stream_task = None
            if kline_stream_task is not None and kline_stream_task.done():
                try:
                    await kline_stream_task
                    detail = "Binance 1m kline stream 已结束，正在重新连接。"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    detail = str(exc)
                if stream_health is not None:
                    await stream_health.handle(
                        {
                            "stream": "kline",
                            "state": "DEGRADED",
                            "error": detail,
                            "at": datetime.now(timezone.utc),
                        }
                    )
                kline_stream_task = None
            if active_symbols != subscribed_symbols:
                if price_stream_task is not None:
                    price_stream_task.cancel()
                    await _await_cancelled(price_stream_task)
                    price_stream_task = None
                if kline_stream_task is not None:
                    kline_stream_task.cancel()
                    await _await_cancelled(kline_stream_task)
                    kline_stream_task = None
                subscribed_symbols = active_symbols
            if active_symbols:
                if price_stream_task is None:
                    price_kwargs: dict[str, Any] = {}
                    if stream_health is not None and _supports_lifecycle_handler(exchange.run_price_stream):
                        price_kwargs["lifecycle_handler"] = stream_health.handle
                    price_stream_task = asyncio.create_task(
                        exchange.run_price_stream(
                            list(active_symbols),
                            controller.handle_price_update_event,
                            **price_kwargs,
                        )
                    )
                if kline_stream_task is None:
                    run_kline_stream = getattr(exchange, "run_kline_stream", None)
                    kline_handler = getattr(controller, "handle_kline_closed_event", None)
                    if callable(run_kline_stream) and callable(kline_handler):
                        kline_kwargs: dict[str, Any] = {"interval": "1m"}
                        if stream_health is not None and _supports_lifecycle_handler(run_kline_stream):
                            kline_kwargs["lifecycle_handler"] = stream_health.handle
                        kline_stream_task = asyncio.create_task(
                            run_kline_stream(
                                list(active_symbols),
                                kline_handler,
                                **kline_kwargs,
                            )
                        )
            await asyncio.sleep(poll_seconds)
    finally:
        if price_stream_task is not None:
            price_stream_task.cancel()
            await _await_cancelled(price_stream_task)
        if kline_stream_task is not None:
            kline_stream_task.cancel()
            await _await_cancelled(kline_stream_task)


def _supports_lifecycle_handler(callable_object: Any) -> bool:
    return _supports_named_parameter(callable_object, "lifecycle_handler")


def _supports_named_parameter(callable_object: Any, parameter: str) -> bool:
    try:
        return parameter in inspect.signature(callable_object).parameters
    except (TypeError, ValueError):
        return False


async def _await_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        # The stream supervisor has already converted child failures into
        # DEGRADED state. Cleanup must not re-raise a completed child error
        # while the outer supervisor itself is being cancelled.
        return


async def _cancel_and_drain_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=BINANCE_LOOP_TASK_CANCEL_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        return
    except asyncio.TimeoutError:
        return
    except Exception:
        return


async def _close_all_active_sessions_or_raise(controller: TradingController, reason: str) -> list[str]:
    if bool(getattr(controller, "round_active", False)) and callable(getattr(controller, "stop_round", None)):
        closed = await controller.stop_round(reason, datetime.now(timezone.utc))
    else:
        closed = await controller.close_all_active_sessions(reason, datetime.now(timezone.utc))
    remaining = tuple(sorted(getattr(controller, "active_sessions", {})))
    if remaining:
        raise RuntimeError(f"{reason} 未能清理所有活跃会话: {', '.join(remaining)}")
    return closed


def _require_binance_symbol_allowlist(config) -> None:
    allowlist = config.raw.get("selection", {}).get("symbol_allowlist", [])
    if not any(str(symbol).strip() for symbol in allowlist):
        raise RuntimeError("真实 Binance 入口要求配置 selection.symbol_allowlist，避免误选非目标合约。")


def _first_configured_binance_symbol(config) -> str:
    symbols = _configured_binance_symbols(config)
    if not symbols:
        raise RuntimeError("selection.symbol_allowlist 未配置可用于测试网价格流的 USDT 合约。")
    return symbols[0]


def _configured_binance_symbols(config) -> list[str]:
    selection = config.raw.get("selection", {})
    blacklist = _normalized_symbols(selection.get("symbol_blacklist", []))
    symbols = []
    seen = set()
    for raw_symbol in selection.get("symbol_allowlist", []):
        symbol = _normalized_symbol(raw_symbol)
        if not symbol or symbol in seen or symbol in blacklist:
            continue
        seen.add(symbol)
        if symbol.endswith("USDT"):
            symbols.append(symbol)
    return symbols


async def _require_binance_tradable_allowlist_symbols(exchange, config) -> list[str]:
    selection = config.raw.get("selection", {})
    blacklist = _normalized_symbols(selection.get("symbol_blacklist", []))
    symbols = await exchange.get_symbols()
    tradable = {
        _normalized_symbol(item.get("symbol", ""))
        for item in symbols
        if str(item.get("status", "")) == "TRADING"
        and _normalized_symbol(item.get("symbol", "")).endswith("USDT")
        and _is_perpetual_contract(item)
    }
    eligible = []
    seen = set()
    for raw_symbol in selection.get("symbol_allowlist", []):
        symbol = _normalized_symbol(raw_symbol)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if symbol in tradable and symbol not in blacklist:
            eligible.append(symbol)
    if not eligible:
        raise RuntimeError("selection.symbol_allowlist 未匹配到任何可交易 USDT 合约，请检查测试网合约列表和黑名单配置。")
    return eligible


async def _require_binance_smoke_symbols(exchange, config) -> list[str]:
    try:
        return await _require_binance_tradable_allowlist_symbols(exchange, config)
    except Exception as exc:
        if bool(config.binance_testnet):
            fallback = [symbol for symbol in _configured_binance_symbols(config) if symbol in BINANCE_TESTNET_SUBSTITUTE_SYMBOLS]
            if fallback:
                logger.warning("Falling back to configured Binance testnet substitute symbols after exchangeInfo failure: {}", exc)
                return fallback
        raise


def _normalized_symbols(symbols) -> set[str]:
    return {_normalized_symbol(symbol) for symbol in symbols if str(symbol).strip()}


def _normalized_symbol(symbol) -> str:
    return str(symbol).strip().upper()


def _required_order_id(order: dict, label: str) -> str:
    order_id = str(order.get("orderId", "")).strip()
    if not order_id:
        raise RuntimeError(f"Binance {label} response missing orderId")
    return order_id


async def _smoke_order_params(exchange, symbol: str) -> dict[str, float]:
    ticker = await exchange.get_24h_ticker(symbol)
    last_price = float(ticker["lastPrice"])
    rules = await exchange.get_symbol_rules(symbol)
    limit_price = _round_down(last_price * 0.95, float(rules["tick_size"]))
    stop_price = _round_down(last_price * 0.90, float(rules["tick_size"]))
    qty = _round_up(
        max(
            float(rules.get("min_qty", rules["step_size"])),
            float(rules.get("min_notional", 5.0)) / limit_price,
        ),
        float(rules["step_size"]),
    )
    if limit_price <= 0 or stop_price <= 0 or qty <= 0:
        raise RuntimeError("测试网订单烟测计算出无效价格或数量。")
    return {
        "last_price": last_price,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "qty": qty,
    }


async def _place_limit_order_post_only_reconciled(
    exchange,
    symbol: str,
    side: str,
    price: float,
    qty: float,
    client_id: str,
    position_side: str | None = None,
) -> dict:
    try:
        return await exchange.place_limit_order_post_only(
            symbol,
            side,
            price,
            qty,
            client_id,
            position_side=position_side,
        )
    except Exception as exc:
        recovered = await _recover_order_by_client_id_after_create_exception(exchange, symbol, client_id, exc)
        if recovered is not None:
            logger.warning("Recovered limit order after create exception: symbol={}, client_id={}", symbol, client_id)
            return recovered
        raise exc


async def _place_stop_market_order_reconciled(
    exchange,
    symbol: str,
    side: str,
    stop_price: float,
    client_id: str,
    close_position: bool,
) -> dict:
    try:
        return await exchange.place_stop_market_order(symbol, side, stop_price, client_id, close_position=close_position)
    except Exception as exc:
        recovered = await _recover_order_by_client_id_after_create_exception(exchange, symbol, client_id, exc)
        if recovered is not None:
            logger.warning("Recovered stop order after create exception: symbol={}, client_id={}", symbol, client_id)
            return recovered
        raise exc


async def _place_market_order_reconciled(
    exchange,
    symbol: str,
    side: str,
    qty: float,
    reduce_only: bool,
    position_side: str | None,
    client_id: str,
) -> dict:
    try:
        return await exchange.place_market_order(
            symbol,
            side,
            qty,
            reduce_only=reduce_only,
            position_side=position_side,
            client_id=client_id,
        )
    except Exception as exc:
        recovered = await _recover_order_by_client_id_after_create_exception(exchange, symbol, client_id, exc)
        if recovered is not None:
            logger.warning("Recovered market order after create exception: symbol={}, client_id={}", symbol, client_id)
            return recovered
        raise exc


async def _recover_order_by_client_id_after_create_exception(
    exchange,
    symbol: str,
    client_id: str,
    exc: Exception,
) -> dict | None:
    attempts = ORDER_CREATE_RECOVERY_ATTEMPTS if _is_order_create_status_unknown(exc) else 1
    delay_seconds = ORDER_CREATE_RECOVERY_DELAY_SECONDS if attempts > 1 else 0
    return await _recover_order_by_client_id(exchange, symbol, client_id, attempts, delay_seconds)


async def _recover_order_by_client_id(
    exchange,
    symbol: str,
    client_id: str,
    attempts: int = 1,
    delay_seconds: float = 0,
) -> dict | None:
    for attempt in range(max(1, attempts)):
        try:
            order = await exchange.get_order(symbol, "", client_id)
        except Exception:
            order = None
        if order is not None and _order_id_or_none(order) is not None:
            return order
        if attempt < attempts - 1 and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
    return None


def _is_order_create_status_unknown(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "status unknown",
        "timeout waiting for response from backend server",
        "send status unknown",
        "execution status unknown",
        "bad gateway",
        "non-json or raw transport error",
    )
    return any(marker in text for marker in markers)


async def _binance_direct_signed_request(
    config,
    method: str,
    path: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("缺少 httpx 依赖，请先安装 requirements.txt。") from exc

    base_url = "https://testnet.binancefuture.com" if config.binance_testnet else "https://fapi.binance.com"
    url = f"{base_url}{path}"
    headers = {
        "X-MBX-APIKEY": config.binance_api_key,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    proxy = _httpx_proxy_url(config.raw.get("proxy"))
    client = _build_httpx_async_client(httpx, proxy, timeout=20.0)
    async with client:
        try:
            timestamp_ms = await _binance_direct_server_time_ms(client, base_url)
            payload = _binance_signed_query(
                _binance_direct_signed_params(params, timestamp_ms),
                config.binance_api_secret,
            )
            if method.upper() == "GET":
                response = await client.request(method.upper(), f"{url}?{payload}", headers=headers)
            else:
                response = await client.request(method.upper(), url, headers=headers, content=payload)
        except Exception as exc:
            return {
                "ok": False,
                "status_code": None,
                "error": _sanitize_direct_transport_error(str(exc)),
            }
    response_text = response.text
    try:
        response_json: Any = response.json()
    except ValueError:
        response_json = None
    return {
        "ok": 200 <= response.status_code < 300,
        "status_code": response.status_code,
        "json": response_json,
        "text": _truncate_text(response_text),
    }


async def _binance_direct_server_time_ms(client: Any, base_url: str) -> int:
    response = await client.request("GET", f"{base_url}/fapi/v1/time")
    response.raise_for_status()
    body = response.json()
    return int(body["serverTime"])


def _binance_direct_signed_params(params: dict[str, Any], timestamp_ms: int) -> dict[str, Any]:
    return {
        **params,
        "recvWindow": 60000,
        "timestamp": timestamp_ms,
    }


def _binance_signed_query(params: dict[str, Any], api_secret: str) -> str:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    signature = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def _httpx_proxy_url(proxy_config: dict[str, Any] | None) -> str | None:
    if not _proxy_enabled(proxy_config):
        return None
    proxy = proxy_config.get("https") or proxy_config.get("http")
    return str(proxy) if proxy else None


def _proxy_enabled(proxy_config: dict[str, Any] | None) -> bool:
    return bool(proxy_config and proxy_config.get("enabled"))


def _build_httpx_async_client(httpx_module, proxy: str | None, timeout: float):
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if proxy is None:
        return httpx_module.AsyncClient(**client_kwargs)
    try:
        return httpx_module.AsyncClient(**client_kwargs, proxy=proxy)
    except TypeError as exc:
        if "proxy" not in str(exc):
            raise
    return httpx_module.AsyncClient(**client_kwargs, proxies=proxy)


def _binance_number_string(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _binance_direct_error_text(response: dict[str, Any]) -> str:
    body = response.get("json")
    if isinstance(body, dict):
        code = body.get("code")
        msg = body.get("msg")
        return f"Binance direct order response status={response.get('status_code')} code={code} msg={msg}"
    error = response.get("error")
    if error:
        return f"Binance direct order transport error: {error}"
    return f"Binance direct order response status={response.get('status_code')} body={response.get('text')}"


def _binance_direct_public_response(response: dict[str, Any]) -> dict[str, Any]:
    public = {"ok": response.get("ok"), "status_code": response.get("status_code")}
    if response.get("json") is not None:
        public["json"] = response.get("json")
    if response.get("text") and response.get("json") is None:
        public["text"] = response.get("text")
    if response.get("error"):
        public["error"] = response.get("error")
    return public


def _truncate_text(text: str, limit: int = 500) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _sanitize_direct_transport_error(text: str) -> str:
    if "signature=" not in text:
        return text
    return text.split("signature=", 1)[0] + "signature=<redacted>"


def _order_id_or_none(order: dict) -> str | None:
    order_id = str(order.get("orderId", "")).strip()
    return order_id or None


async def _cleanup_smoke_orders(exchange, symbol: str, known_order_ids: list[str]) -> list[str]:
    errors: list[str] = []
    cancelled: set[str] = set()
    for order_id in known_order_ids:
        try:
            await exchange.cancel_order(symbol, order_id)
            cancelled.add(order_id)
        except Exception as exc:
            errors.append(f"cancel known order {order_id} failed: {exc}")
    try:
        open_orders = await exchange.get_open_orders(symbol)
    except Exception as exc:
        try:
            await exchange.cancel_all_orders(symbol)
        except Exception as cancel_exc:
            errors.append(f"query open orders failed: {exc}; cancel all orders failed: {cancel_exc}")
        return errors

    unknown_open_order = False
    for order in open_orders:
        order_id = _order_id_or_none(order)
        if order_id is None:
            unknown_open_order = True
            continue
        if order_id in cancelled:
            continue
        try:
            await exchange.cancel_order(symbol, order_id)
        except Exception as exc:
            errors.append(f"cancel reconciled order {order_id} failed: {exc}")
    if unknown_open_order:
        try:
            await exchange.cancel_all_orders(symbol)
        except Exception as exc:
            errors.append(f"cancel all orders failed: {exc}")
    return errors


async def _open_algo_orders(exchange, symbol: str) -> list[dict]:
    get_open_algo_orders = getattr(exchange, "get_open_algo_orders", None)
    if get_open_algo_orders is None:
        return []
    return list(await get_open_algo_orders(symbol))


async def _sweep_symbol_orders_and_positions(exchange, symbol: str) -> list[str]:
    errors: list[str] = []
    try:
        ordinary_orders = await exchange.get_open_orders(symbol)
    except Exception as exc:
        ordinary_orders = []
        errors.append(f"query open orders failed: {exc}")
    try:
        algo_orders = await _open_algo_orders(exchange, symbol)
    except Exception as exc:
        algo_orders = []
        errors.append(f"query open algo orders failed: {exc}")

    if ordinary_orders or algo_orders:
        try:
            await exchange.cancel_all_orders(symbol)
        except Exception as exc:
            fallback_errors = await _cancel_sweep_orders(exchange, symbol, ordinary_orders, algo_orders)
            if fallback_errors:
                errors.append(f"cancel all failed: {exc}; fallback errors: {fallback_errors}")

    try:
        close_specs = _position_close_specs(await exchange.get_position(symbol))
    except Exception as exc:
        errors.append(f"query position failed: {exc}")
        return errors
    try:
        await _close_sweep_position_specs(exchange, symbol, close_specs)
    except Exception as exc:
        errors.append(f"close residual position failed: {exc}")
    return errors


async def _cancel_sweep_orders(
    exchange,
    symbol: str,
    ordinary_orders: list[dict],
    algo_orders: list[dict],
) -> list[str]:
    errors: list[str] = []
    for order in ordinary_orders:
        order_id = _order_id_or_none(order)
        if order_id is None:
            errors.append(f"ordinary order missing orderId: {order}")
            continue
        try:
            await exchange.cancel_order(symbol, order_id)
        except Exception as exc:
            errors.append(f"cancel ordinary order {order_id} failed: {exc}")

    cancel_algo_order = getattr(exchange, "cancel_algo_order", None)
    for order in algo_orders:
        algo_id = order.get("algoId")
        if algo_id in (None, ""):
            errors.append(f"algo order missing algoId: {order}")
            continue
        if cancel_algo_order is None:
            errors.append(f"exchange does not support cancel_algo_order for algoId={algo_id}")
            continue
        try:
            await cancel_algo_order(symbol, algo_id)
        except Exception as exc:
            errors.append(f"cancel algo order {algo_id} failed: {exc}")
    return errors


async def _close_sweep_position_specs(exchange, symbol: str, close_specs: list[tuple[str, float, str | None]]) -> list[dict]:
    closed = []
    for side, qty, position_side in close_specs:
        client_id = _sweep_close_client_id(symbol, side, position_side)
        try:
            response = await exchange.place_market_order(
                symbol,
                side,
                qty,
                reduce_only=True,
                position_side=position_side,
                client_id=client_id,
            )
        except Exception as exc:
            recovered = await _recover_order_by_client_id_after_create_exception(exchange, symbol, client_id, exc)
            if recovered is None:
                raise
            response = recovered
        order_id = _order_id_or_none(response)
        if order_id is None:
            raise RuntimeError(f"当前环境安全清扫平仓响应缺少订单ID: symbol={symbol}, client_id={client_id}")
        closed.append({"side": side, "qty": qty, "position_side": position_side})
    return closed


def _sweep_close_client_id(symbol: str, side: str, position_side: str | None) -> str:
    close_side = (position_side or side).lower()
    return f"qgsweep-{symbol.lower()}-{close_side}"


def _position_sweep_summary(position: dict) -> dict[str, float]:
    return {
        "qty": float(position.get("qty") or position.get("positionAmt") or 0.0),
        "long_qty": float(position.get("long_qty") or 0.0),
        "short_qty": float(position.get("short_qty") or 0.0),
    }


def _position_sweep_exposure(summary: dict[str, float]) -> float:
    return max(abs(summary["qty"]), abs(summary["long_qty"]), abs(summary["short_qty"]))


def _json_log_detail(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _round_down(value: float, step: float) -> float:
    return _round_to_step(value, step, ROUND_FLOOR)


def _round_up(value: float, step: float) -> float:
    return _round_to_step(value, step, ROUND_CEILING)


def _round_to_step(value: float, step: float, rounding) -> float:
    step_decimal = Decimal(str(step))
    if step_decimal <= 0:
        raise RuntimeError("交易规则 step 必须为正数。")
    value_decimal = Decimal(str(value))
    return float((value_decimal / step_decimal).to_integral_value(rounding=rounding) * step_decimal)


def _build_repository(config) -> Repository:
    notifier = build_system_log_notifier(config.raw.get("notifications", {}))
    return Repository(
        config.database_path,
        notifier=notifier,
        account_id=getattr(config, "account_id", "default"),
    )


def _parse_window_kinds(raw: Any) -> list:
    from strategy.window_models import WindowKind

    if not raw:
        return [WindowKind.WEEKEND, WindowKind.HOLIDAY]
    result = []
    for item in raw:
        text = str(item).strip().upper()
        if not text:
            continue
        result.append(WindowKind(text))
    return result or [WindowKind.WEEKEND, WindowKind.HOLIDAY]


def _build_controller(exchange, config, live_observation: bool | None = None) -> TradingController:
    raw = config.raw
    trading = raw["trading"]
    timing = raw["timing"]
    grid = raw["grid"]
    cooldown = raw["cooldown"]
    selection = raw["selection"]
    features = raw.get("features", {})
    regime = raw.get("regime", {})
    risk = raw.get("risk", {})
    inventory = raw.get("inventory", {})
    costs = raw.get("costs", {})
    entry = raw.get("entry", {})
    regime_weights = regime.get("weights", {})
    testnet_force_window = bool(getattr(config, "binance_testnet", False)) and bool(timing.get("testnet_force_window", False))
    testnet_fast_observation = bool(getattr(config, "binance_testnet", False)) and bool(
        timing.get("testnet_fast_observation", False)
    )
    effective_live_observation = bool(live_observation) if live_observation is not None else bool(timing.get("live_observation", False))
    if testnet_fast_observation:
        effective_live_observation = False
    controller = TradingController(
        exchange=exchange,
        scheduler=(
            _TestnetAlwaysOpenScheduler()
            if testnet_force_window
            else Scheduler(
                force_close_minutes=int(timing["force_close_minutes"]),
                minimum_trade_minutes=int(timing.get("minimum_trade_minutes", 120)),
                allowed_window_kinds=tuple(
                    _parse_window_kinds(timing.get("allowed_window_kinds"))
                ),
            )
        ),
        repository=_build_repository(config),
        selector_config=SelectionConfig(
            max_concurrent=int(trading["max_concurrent"]),
            scan_candidate_count=int(selection.get("scan_candidate_count", 10)),
            symbol_blacklist=tuple(selection.get("symbol_blacklist", [])),
            symbol_allowlist=tuple(selection.get("symbol_allowlist", [])),
            volume_weight=float(selection["volume_weight"]),
            depth_weight=float(selection["depth_weight"]),
            depth_levels=int(selection["depth_levels"]),
        ),
        observer_config=ObserverConfig(
            observe_hours=float(timing["observe_hours"]),
            kline_interval=str(timing["observe_kline_interval"]),
            min_samples=30,
            live_observation=effective_live_observation,
            observe_check_seconds=float(timing.get("observe_check_seconds", 60)),
        ),
        grid_config=GridConfig(
            range_method=str(grid["range_method"]),
            std_k=float(grid["std_k"]),
            quantile_upper=float(grid["quantile_upper"]),
            quantile_lower=float(grid["quantile_lower"]),
            min_step_pct=float(grid["min_step_pct"]),
            min_tradable_range_pct=float(grid.get("min_tradable_range_pct", 0.0015)),
            safety_multiplier=float(grid["safety_multiplier"]),
            max_grid_num=int(grid["max_grid_num"]),
            max_range_pct=float(grid["max_range_pct"]),
            atr_period=int(cooldown["atr_period"]),
            stop_buffer_pct=float(trading["stop_buffer_pct"]),
            volatility_refresh_seconds=float(grid.get("volatility_refresh_seconds", 60.0)),
            rolling_regrid_enabled=bool(grid.get("rolling_regrid_enabled", False)),
            rolling_regrid_seconds=float(grid.get("rolling_regrid_seconds", 7200.0)),
        ),
        controller_config=ControllerConfig(
            capital_per_symbol=float(trading["capital_per_symbol"]),
            leverage=int(trading["leverage"]),
            max_concurrent=int(trading["max_concurrent"]),
            take_profit_usdt=float(trading["take_profit_usdt"]),
            total_capital_limit=float(trading["total_capital_limit"]),
            max_maker_fee_rate=float(trading.get("max_maker_fee_rate", 0.0)),
            maker_fee_check_interval_seconds=float(trading.get("maker_fee_check_interval_seconds", 300.0)),
            loop_interval_seconds=float(timing["loop_interval_seconds"]),
            scheduler_check_minutes=float(timing["scheduler_check_minutes"]),
            effective_leverage_cap=float(risk.get("effective_leverage_cap", float("inf"))),
            max_session_loss_pct=float(risk.get("max_session_loss_pct", 0.0)),
            max_window_loss_pct=float(risk.get("max_weekend_loss_pct", 0.0)),
            max_symbol_inventory_pct=float(risk.get("max_symbol_inventory_pct", 0.10)),
            max_consecutive_session_losses=int(risk.get("max_consecutive_session_losses", 0)),
            max_window_stop_count=int(risk.get("max_window_stop_count", 0)),
            block_risk_increase_hot_reload=bool(
                risk.get("block_risk_increase_hot_reload", True)
            ),
            direction_mode=GridDirectionMode(
                str(trading.get("direction_mode", "NEUTRAL")).strip().upper()
            ),
            direction_overrides={
                str(symbol).strip().upper(): GridDirectionMode(
                    str(mode).strip().upper()
                )
                for symbol, mode in (trading.get("direction_overrides", {}) or {}).items()
            },
            max_unpaired_lots_per_side_by_symbol={
                str(symbol).strip().upper(): int(value)
                for symbol, value in (
                    inventory.get("max_unpaired_lots_per_side_by_symbol", {}) or {}
                ).items()
            },
            reduce_target_step_fraction_by_symbol={
                str(symbol).strip().upper(): float(value)
                for symbol, value in (
                    grid.get("reduce_target_step_fraction_by_symbol", {}) or {}
                ).items()
            },
            seed_execution=str(entry.get("seed_execution", "MARKET")).strip().upper(),
            seed_max_slippage_pct=float(entry.get("seed_max_slippage_pct", 0.002)),
        ),
        cooldown_config=CooldownConfig(
            atr_period=int(cooldown["atr_period"]),
            calm_window_minutes=int(cooldown["calm_window_minutes"]),
            atr_recovery_ratio=float(cooldown["atr_recovery_ratio"]),
            amplitude_multiplier=float(cooldown["amplitude_multiplier"]),
            min_calm_minutes=int(timing.get("min_calm_minutes", cooldown.get("min_calm_minutes", 15))),
        ),
        feature_flags=V2FeatureFlags(
            regime_v2=bool(features.get("regime_v2", False)),
            inventory_manager=bool(features.get("inventory_manager", False)),
            adaptive_grid_v2=bool(features.get("adaptive_grid_v2", False)),
            risk_manager_v2=bool(features.get("risk_manager_v2", False)),
        ),
        regime_config=RegimeConfig(
            short_window=int(regime.get("short_window", 15)),
            long_window=int(regime.get("long_window", 60)),
            enter_threshold=float(regime.get("enter_threshold", 75)),
            stay_threshold=float(regime.get("stay_threshold", 65)),
            soft_breach_limit=int(regime.get("soft_breach_limit", 3)),
            max_data_age_seconds=float(regime.get("max_data_age_seconds", 90)),
            max_spread_pct=float(regime.get("hard_limits", {}).get("max_spread_pct", 0.001)),
            max_vol_expansion_ratio=float(
                regime.get("hard_limits", {}).get("max_vol_expansion_ratio", 2.5)
            ),
            min_depth_usdt=float(regime.get("hard_limits", {}).get("min_depth_usdt", 10_000)),
            weights=RegimeWeights(
                volatility=float(regime_weights.get("volatility", 0.25)),
                trend=float(regime_weights.get("trend", 0.20)),
                liquidity=float(regime_weights.get("liquidity", 0.25)),
                mean_reversion=float(regime_weights.get("mean_reversion", 0.15)),
                cost=float(regime_weights.get("cost", 0.15)),
                event=float(regime_weights.get("event", 0.0)),
            ),
            # 事件权重只有在事件 Provider 接入后才启用；否则 event 分量会被归一化剔除。
            event_source_available=bool(features.get("ai_regime_feature", False)),
        ),
        adaptive_grid_config=AdaptiveGridConfig(
            center_half_life_minutes=float(grid.get("center_half_life_minutes", 30)),
            k_atr_range=float(grid.get("k_atr_range", 2.0)),
            k_sigma_range=float(grid.get("k_sigma_range", 2.0)),
            max_range_pct=float(grid.get("max_range_pct", 0.03)),
            min_step_pct=float(grid.get("min_step_pct", 0.0015)),
            max_step_pct=float(grid.get("max_step_pct", 0.01)),
            k_atr_step=float(grid.get("k_atr_step", 0.50)),
            k_sigma_step=float(grid.get("k_sigma_step", 0.80)),
            min_grid_num=int(grid.get("min_grid_num", 3)),
            max_grid_num=int(grid.get("max_grid_num", 20)),
            expansion_rate=float(grid.get("expansion_rate", 0.08)),
            stop_buffer_pct=float(trading.get("stop_buffer_pct", 0.015)),
            adverse_selection_buffer_pct=float(costs.get("adverse_selection_buffer_pct", 0.0005)),
            slippage_buffer_pct=float(costs.get("slippage_buffer_pct", 0.0005)),
            safety_margin_pct=float(costs.get("safety_margin_pct", 0.0005)),
            horizon_bars=int(grid.get("horizon_bars", 60)),
            volatility_estimator=str(grid.get("volatility_estimator", "ewma")),
        ),
        inventory_config=InventoryConfig(
            caution_utilization=float(inventory.get("caution_utilization", 0.40)),
            high_utilization=float(inventory.get("high_utilization", 0.60)),
            critical_utilization=float(inventory.get("critical_utilization", 0.80)),
            suppress_same_side_orders=bool(inventory.get("suppress_same_side_orders", True)),
            passive_reduce_first=bool(inventory.get("passive_reduce_first", True)),
        ),
    )
    entry = raw.get("entry", {}) if isinstance(raw.get("entry"), dict) else {}
    controller._entry_config = {
        "max_price_drift_pct": float(entry.get("max_price_drift_pct", 0.002)),
        "revalidate_before_place": bool(entry.get("revalidate_before_place", True)),
    }
    return controller


if __name__ == "__main__":
    main()
