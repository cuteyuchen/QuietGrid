from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from loguru import logger

from core.config import load_config, require_testnet
from core.logging_config import setup_logging
from core.notifications import build_system_log_notifier
from core.scheduler import Scheduler
from db.database import init_db
from db.repository import Repository
from exchange.binance import BinanceFuturesClient
from exchange.mock import MockExchangeClient
from strategy.backtest import BacktestConfig, BacktestResult, run_grid_backtest
from strategy.cooldown import CooldownConfig
from strategy.controller import ControllerConfig, TradingController, _position_close_specs
from strategy.grid_calculator import GridConfig, calculate_grid_params
from strategy.observer import ObserverConfig
from strategy.selector import SelectionConfig, _is_perpetual_contract


class _SmokeComplete(Exception):
    pass


ORDER_CREATE_RECOVERY_ATTEMPTS = 5
ORDER_CREATE_RECOVERY_DELAY_SECONDS = 0.25


def main() -> None:
    parser = argparse.ArgumentParser(description="QuietGrid trading process")
    parser.add_argument("--mock-once", action="store_true", help="使用 mock 交易所执行一轮编排验证")
    parser.add_argument("--binance-once", action="store_true", help="使用 Binance 测试网执行一轮编排")
    parser.add_argument("--mock-loop", action="store_true", help="使用 mock 交易所运行长期循环")
    parser.add_argument("--binance-loop", action="store_true", help="使用 Binance 测试网运行长期循环")
    parser.add_argument("--binance-check", action="store_true", help="只检查 Binance 测试网连接和账户配置，不下单")
    parser.add_argument("--binance-order-smoke", action="store_true", help="使用 Binance 测试网创建并清理一组最小订单")
    parser.add_argument("--binance-test-order-smoke", action="store_true", help="使用 Binance 测试网 order/test 校验下单参数，不创建订单")
    parser.add_argument("--binance-market-roundtrip-smoke", action="store_true", help="使用 Binance 测试网执行最小 Market 开平仓烟测")
    parser.add_argument("--binance-direct-order-diagnose", action="store_true", help="绕过 SDK 直接请求 Binance Futures /order 诊断真实下单接口")
    parser.add_argument("--binance-price-stream-smoke", action="store_true", help="接收一条 Binance 测试网价格 WebSocket 事件")
    parser.add_argument("--binance-listen-key-smoke", action="store_true", help="验证 Binance Futures 用户流 listenKey 生命周期")
    parser.add_argument("--binance-algo-stop-smoke", action="store_true", help="创建并撤销一个 Binance Futures Algo STOP_MARKET 条件单")
    parser.add_argument("--binance-position-smoke", action="store_true", help="只读检查 Binance 测试网持仓模式、持仓和未成交订单")
    parser.add_argument("--binance-safety-sweep", action="store_true", help="清理 Binance 测试网 allowlist 标的的挂单和仓位")
    parser.add_argument("--backtest-csv", help="读取本地CSV K线文件执行离线网格回测，不连接交易所")
    parser.add_argument("--backtest-observe-rows", type=int, default=60, help="CSV前多少行作为观察期样本，默认60")
    parser.add_argument("--backtest-symbol", default="AAPLUSDT", help="回测标的名，默认AAPLUSDT")
    parser.add_argument("--backtest-funding-rate", type=float, default=0.0, help="观察期网格计算使用的资金费率，默认0")
    parser.add_argument("--backtest-output", help="可选：把完整回测报告写入JSON文件")
    args = parser.parse_args()

    config = load_config()
    setup_logging(config.raw)
    init_db(config.database_path)

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
        args.binance_listen_key_smoke,
        args.binance_algo_stop_smoke,
        args.binance_position_smoke,
        args.binance_safety_sweep,
        args.backtest_csv is not None,
    ]
    if sum(1 for enabled in selected_modes if enabled) > 1:
        raise SystemExit("一次只能选择一个运行模式。")
    if args.mock_once:
        result = asyncio.run(_run_mock_once(config))
        logger.info("Mock run_once result: {}", result)
        return
    if args.binance_once:
        result = asyncio.run(_run_binance_once(config))
        logger.info("Binance testnet run_once result: {}", result)
        return
    if args.binance_check:
        result = asyncio.run(_run_binance_check(config))
        logger.info("Binance testnet check result: {}", result)
        return
    if args.binance_order_smoke:
        result = asyncio.run(_run_binance_order_smoke(config))
        logger.info("Binance testnet order smoke result: {}", result)
        return
    if args.binance_test_order_smoke:
        result = asyncio.run(_run_binance_test_order_smoke(config))
        logger.info("Binance testnet order/test smoke result: {}", result)
        return
    if args.binance_market_roundtrip_smoke:
        result = asyncio.run(_run_binance_market_roundtrip_smoke(config))
        logger.info("Binance testnet market roundtrip smoke result: {}", result)
        return
    if args.binance_direct_order_diagnose:
        result = asyncio.run(_run_binance_direct_order_diagnose(config))
        logger.info("Binance testnet direct order diagnose result: {}", result)
        return
    if args.binance_price_stream_smoke:
        result = asyncio.run(_run_binance_price_stream_smoke(config))
        logger.info("Binance testnet price stream smoke result: {}", result)
        return
    if args.binance_listen_key_smoke:
        result = asyncio.run(_run_binance_listen_key_smoke(config))
        logger.info("Binance testnet listenKey smoke result: {}", result)
        return
    if args.binance_algo_stop_smoke:
        result = asyncio.run(_run_binance_algo_stop_smoke(config))
        logger.info("Binance testnet algo stop smoke result: {}", result)
        return
    if args.binance_position_smoke:
        result = asyncio.run(_run_binance_position_smoke(config))
        logger.info("Binance testnet position smoke result: {}", result)
        return
    if args.binance_safety_sweep:
        result = asyncio.run(_run_binance_safety_sweep(config))
        logger.info("Binance testnet safety sweep result: {}", result)
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
    if args.mock_loop:
        asyncio.run(_run_mock_loop(config))
        return
    if args.binance_loop:
        asyncio.run(_run_binance_loop(config))
        return

    logger.info(
        "QuietGrid initialized in testnet-safe mode. Use --mock-once, --binance-check, --binance-order-smoke, --binance-test-order-smoke, --binance-market-roundtrip-smoke, --binance-direct-order-diagnose, --binance-price-stream-smoke, --binance-listen-key-smoke, --binance-algo-stop-smoke, --binance-position-smoke, --binance-safety-sweep, --backtest-csv, --binance-once, --mock-loop or --binance-loop."
    )


async def _run_mock_once(config):
    controller = _build_controller(MockExchangeClient(), config, live_observation=False)
    return await controller.run_once()


async def _run_mock_loop(config):
    controller = _build_controller(MockExchangeClient(), config, live_observation=False)
    await controller.run_loop()


def _run_backtest_csv(
    config,
    csv_path: Path,
    observe_rows: int,
    symbol: str,
    funding_rate: float,
    output_path: Path | None = None,
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
    trading = config.raw.get("trading", {})
    result = run_grid_backtest(
        params,
        backtest_klines,
        current_price=current_price,
        config=BacktestConfig(
            capital=float(trading.get("capital_per_symbol", 200)),
            leverage=float(trading.get("leverage", 10)),
            maker_fee_rate=float(trading.get("max_maker_fee_rate", 0.0)),
        ),
    )
    summary = _backtest_summary(result, observe_rows, len(backtest_klines))
    if output_path is not None:
        _write_backtest_report(output_path, _backtest_report(result, params, summary))
        summary["output_path"] = str(output_path)
    return summary


def _read_backtest_csv(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        raise RuntimeError(f"回测CSV不存在: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise RuntimeError("回测CSV缺少表头。")
        missing = {"high", "low", "close"} - {name.strip() for name in reader.fieldnames}
        if missing:
            raise RuntimeError(f"回测CSV缺少必要列: {', '.join(sorted(missing))}")
        rows = []
        for line_number, row in enumerate(reader, start=2):
            rows.append(_normalize_backtest_csv_row(row, line_number))
    if not rows:
        raise RuntimeError("回测CSV没有数据行。")
    return rows


def _normalize_backtest_csv_row(row: dict[str, Any], line_number: int) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if key is not None:
            normalized[key.strip()] = value
    for key in ("high", "low", "close"):
        try:
            normalized[key] = float(normalized[key])
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError(f"回测CSV第{line_number}行 {key} 无效。") from exc
    return normalized


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
    )


def _backtest_summary(result: BacktestResult, observe_rows: int, backtest_rows: int) -> dict[str, Any]:
    return {
        "symbol": result.symbol,
        "observe_rows": observe_rows,
        "backtest_rows": backtest_rows,
        "fills": len(result.fills),
        "gross_grid_pnl": result.gross_grid_pnl,
        "fees_paid": result.fees_paid,
        "realized_pnl": result.realized_pnl,
        "unrealized_pnl": result.unrealized_pnl,
        "total_pnl": result.total_pnl,
        "net_position_qty": result.net_position_qty,
        "open_order_count": result.open_order_count,
        "stopped_reason": result.stopped_reason,
        "stopped_at_index": result.stopped_at_index,
        "stopped_at_price": result.stopped_at_price,
        "last_price": result.last_price,
    }


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
    }


def _write_backtest_report(output_path: Path, report: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


async def _run_binance_once(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-once 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        await _require_binance_signed_write_health(exchange, config, eligible, "binance_once")
        controller = _build_controller(exchange, config, live_observation=True)
        try:
            check = await controller.validate_startup()
            if not check.ok:
                raise RuntimeError(check.reason)
            await controller.recover_unclosed_sessions()
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
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
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
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
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
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
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
        return {
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
    finally:
        await exchange.close()


async def _run_binance_market_roundtrip_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-market-roundtrip-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
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
            json.dumps(result, ensure_ascii=False),
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
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
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
            json.dumps(result, ensure_ascii=False),
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
        json.dumps(result, ensure_ascii=False),
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
        json.dumps(result, ensure_ascii=False),
        datetime.now(timezone.utc),
    )
    if errors:
        raise RuntimeError(f"Binance signed write health check failed before {caller}: {'; '.join(errors)}")
    return result


async def _run_binance_listen_key_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-listen-key-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
    listen_key: str | None = None
    try:
        listen_key = await exchange.create_futures_listen_key()
        await exchange.keepalive_futures_listen_key(listen_key)
        return {"listen_key_ok": True, "listen_key_length": len(listen_key)}
    finally:
        if listen_key:
            await exchange.close_futures_listen_key(listen_key)
        await exchange.close()


async def _run_binance_algo_stop_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-algo-stop-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
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
        return {
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
    finally:
        if symbol is not None and algo_id is not None:
            try:
                await exchange.cancel_algo_order(symbol, algo_id)
            except Exception:
                pass
        await exchange.close()


async def _run_binance_position_smoke(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-position-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
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
        return {
            "position_smoke_ok": True,
            "dual_side_position": bool(mode.get("dualSidePosition")),
            "symbols": symbols,
        }
    finally:
        await exchange.close()


async def _run_binance_safety_sweep(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-safety-sweep 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    repository = _build_repository(config)
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
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
                                f"测试网安全清扫全撤失败且逐单撤单未完全成功: symbol={symbol}, "
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
                "Binance testnet safety sweep left residual exposure.",
                json.dumps({"symbols": results, "residuals": residuals}, ensure_ascii=False),
                datetime.now(timezone.utc),
            )
            raise RuntimeError(f"测试网安全清扫后仍有残留: {'; '.join(residuals)}")
        result = {
            "safety_sweep_ok": True,
            "symbols": results,
        }
        repository.log_system(
            "INFO",
            "binance_safety_sweep",
            "Binance testnet safety sweep completed.",
            json.dumps(result, ensure_ascii=False),
            datetime.now(timezone.utc),
        )
        return result
    finally:
        await exchange.close()


async def _run_binance_price_stream_smoke(config, timeout_seconds: float = 20):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-price-stream-smoke 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
    event_holder: list[dict] = []
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        symbol = eligible[0]

        async def handler(event):
            event_holder.append(event)
            raise _SmokeComplete

        try:
            await asyncio.wait_for(
                exchange.run_price_stream([symbol], handler, reconnect_delay_seconds=1, max_reconnects=0),
                timeout=timeout_seconds,
            )
        except _SmokeComplete:
            pass
        if not event_holder:
            raise RuntimeError("Binance price stream smoke timed out without receiving an event.")
        return {
            "stream_ok": True,
            "symbol": symbol,
            "event": event_holder[0],
        }
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


async def _run_binance_loop(config):
    require_testnet(config)
    _require_binance_symbol_allowlist(config)
    if not config.binance_api_key or not config.binance_api_secret:
        raise RuntimeError("执行 --binance-loop 需要在 .env 中配置 BINANCE_API_KEY 和 BINANCE_API_SECRET。")
    exchange = await BinanceFuturesClient.create(
        api_key=config.binance_api_key,
        api_secret=config.binance_api_secret,
        testnet=config.binance_testnet,
        proxy_config=config.raw.get("proxy"),
    )
    try:
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        await _require_binance_signed_write_health(exchange, config, eligible, "binance_loop")
        controller = _build_controller(exchange, config, live_observation=True)
        check = await controller.validate_startup()
        if not check.ok:
            raise RuntimeError(check.reason)
        await controller.recover_unclosed_sessions()
        user_stream_task = asyncio.create_task(exchange.run_user_stream(controller.handle_order_filled_event))
        price_stream_task = asyncio.create_task(
            _run_dynamic_price_stream(exchange, controller, poll_seconds=float(config.raw["timing"]["loop_interval_seconds"]))
        )
        controller_task = asyncio.create_task(controller.run_loop())
        task_names = {
            controller_task: "controller loop",
            user_stream_task: "user stream",
            price_stream_task: "price stream",
        }
        try:
            done, _pending = await asyncio.wait(
                {controller_task, user_stream_task, price_stream_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
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
            closed = await _close_all_active_sessions_or_raise(controller, "binance_loop_shutdown_cleanup")
            if closed:
                logger.info("Binance loop shutdown cleanup closed active sessions: {}", closed)
    finally:
        await exchange.close()


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


async def _run_dynamic_price_stream(exchange, controller: TradingController, poll_seconds: float = 10) -> None:
    subscribed_symbols: tuple[str, ...] = ()
    price_stream_task: asyncio.Task | None = None
    try:
        while True:
            active_symbols = tuple(sorted(controller.active_sessions))
            if price_stream_task is not None and price_stream_task.done():
                await price_stream_task
                raise RuntimeError("Binance price stream stopped unexpectedly.")
            if active_symbols != subscribed_symbols:
                if price_stream_task is not None:
                    price_stream_task.cancel()
                    await _await_cancelled(price_stream_task)
                    price_stream_task = None
                subscribed_symbols = active_symbols
                if active_symbols:
                    price_stream_task = asyncio.create_task(
                        exchange.run_price_stream(list(active_symbols), controller.handle_price_update_event)
                    )
            await asyncio.sleep(poll_seconds)
    finally:
        if price_stream_task is not None:
            price_stream_task.cancel()
            await _await_cancelled(price_stream_task)


async def _await_cancelled(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        return


async def _cancel_and_drain_task(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def _close_all_active_sessions_or_raise(controller: TradingController, reason: str) -> list[str]:
    closed = await controller.close_all_active_sessions(reason, datetime.now(timezone.utc))
    remaining = tuple(sorted(getattr(controller, "active_sessions", {})))
    if remaining:
        raise RuntimeError(f"{reason} 未能清理所有活跃会话: {', '.join(remaining)}")
    return closed


def _require_binance_symbol_allowlist(config) -> None:
    allowlist = config.raw.get("selection", {}).get("symbol_allowlist", [])
    if not any(str(symbol).strip() for symbol in allowlist):
        raise RuntimeError("真实 Binance 入口要求配置 selection.symbol_allowlist，避免误选非目标合约。")


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

    signed_params = {
        **params,
        "recvWindow": 5000,
        "timestamp": int(time.time() * 1000),
    }
    payload = _binance_signed_query(signed_params, config.binance_api_secret)
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
            raise RuntimeError(f"测试网安全清扫平仓响应缺少订单ID: symbol={symbol}, client_id={client_id}")
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
    return Repository(config.database_path, notifier=notifier)


def _build_controller(exchange, config, live_observation: bool | None = None) -> TradingController:
    raw = config.raw
    trading = raw["trading"]
    timing = raw["timing"]
    grid = raw["grid"]
    cooldown = raw["cooldown"]
    selection = raw["selection"]
    return TradingController(
        exchange=exchange,
        scheduler=Scheduler(force_close_minutes=int(timing["force_close_minutes"])),
        repository=_build_repository(config),
        selector_config=SelectionConfig(
            max_concurrent=int(trading["max_concurrent"]),
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
            live_observation=bool(live_observation) if live_observation is not None else bool(timing.get("live_observation", False)),
            observe_check_seconds=float(timing.get("observe_check_seconds", 60)),
        ),
        grid_config=GridConfig(
            range_method=str(grid["range_method"]),
            std_k=float(grid["std_k"]),
            quantile_upper=float(grid["quantile_upper"]),
            quantile_lower=float(grid["quantile_lower"]),
            min_step_pct=float(grid["min_step_pct"]),
            safety_multiplier=float(grid["safety_multiplier"]),
            max_grid_num=int(grid["max_grid_num"]),
            max_range_pct=float(grid["max_range_pct"]),
            atr_period=int(cooldown["atr_period"]),
            stop_buffer_pct=float(trading["stop_buffer_pct"]),
        ),
        controller_config=ControllerConfig(
            capital_per_symbol=float(trading["capital_per_symbol"]),
            leverage=int(trading["leverage"]),
            max_concurrent=int(trading["max_concurrent"]),
            take_profit_usdt=float(trading["take_profit_usdt"]),
            total_capital_limit=float(trading["total_capital_limit"]),
            max_maker_fee_rate=float(trading.get("max_maker_fee_rate", 0.0)),
            loop_interval_seconds=float(timing["loop_interval_seconds"]),
            scheduler_check_minutes=float(timing["scheduler_check_minutes"]),
        ),
        cooldown_config=CooldownConfig(
            atr_period=int(cooldown["atr_period"]),
            calm_window_minutes=int(cooldown["calm_window_minutes"]),
            atr_recovery_ratio=float(cooldown["atr_recovery_ratio"]),
            amplitude_multiplier=float(cooldown["amplitude_multiplier"]),
            min_calm_minutes=int(timing.get("min_calm_minutes", cooldown.get("min_calm_minutes", 15))),
        ),
    )


if __name__ == "__main__":
    main()
