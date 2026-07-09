from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from core.scheduler import Scheduler
from db.database import init_db
from db.repository import Repository
from exchange.mock import MockExchangeClient
from trader import (
    _build_controller,
    _run_backtest_csv,
    _run_backtest_dir,
    _binance_signed_query,
    _binance_direct_signed_params,
    _run_binance_direct_order_diagnose,
    _run_binance_check,
    _run_binance_loop,
    _run_binance_market_roundtrip_smoke,
    _run_binance_once,
    _run_binance_order_smoke,
    _run_binance_price_stream_smoke,
    _run_binance_signed_write_health,
    _run_binance_test_order_smoke,
    _run_binance_listen_key_smoke,
    _run_binance_algo_stop_smoke,
    _run_binance_position_smoke,
    _run_binance_safety_sweep,
    _run_binance_test_run,
    _create_binance_client_for_module,
    _run_dynamic_price_stream,
    _json_log_detail,
    _sanitize_direct_transport_error,
    main,
)


BINANCE_SAFE_SELECTION = {"symbol_allowlist": ["AAPLUSDT"]}


def _runtime_backtest_config(db_path) -> SimpleNamespace:
    return SimpleNamespace(
        binance_api_key="",
        binance_api_secret="",
        binance_testnet=False,
        database_path=db_path,
        raw={
            "logging": {},
            "database": {"path": str(db_path)},
            "trading": {
                "capital_per_symbol": 202,
                "leverage": 1,
                "max_maker_fee_rate": 0.001,
                "stop_buffer_pct": 0.015,
            },
            "grid": {
                "range_method": "std",
                "std_k": 1.8,
                "quantile_upper": 0.95,
                "quantile_lower": 0.05,
                "min_step_pct": 0.0015,
                "safety_multiplier": 3.5,
                "max_grid_num": 20,
                "max_range_pct": 0.05,
            },
            "cooldown": {"atr_period": 14},
        },
    )


def _write_backtest_csv(path, observe_rows: int = 60) -> None:
    rows = ["timestamp,high,low,close"]
    for index in range(observe_rows):
        close = 100 + ((index % 10) - 5) * 0.05
        rows.append(f"obs-{index},{close + 0.08},{close - 0.08},{close}")
    rows.extend(
        [
            "bt-1,100.2,99.8,100.0",
            "bt-2,100.9,100.4,100.8",
            "bt-3,100.2,99.8,100.0",
        ]
    )
    path.write_text("\n".join(rows), encoding="utf-8")


def test_backtest_csv_runs_offline_from_local_file(tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    csv_path = tmp_path / "klines.csv"
    _write_backtest_csv(csv_path)

    result = _run_backtest_csv(
        _runtime_backtest_config(db_path),
        csv_path,
        observe_rows=60,
        symbol="BTCUSDT",
        funding_rate=0.0001,
    )

    assert result["symbol"] == "BTCUSDT"
    assert result["observe_rows"] == 60
    assert result["backtest_rows"] == 3
    assert result["fills"] >= 1
    assert "total_pnl" in result


def test_backtest_csv_can_write_full_json_report(tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    csv_path = tmp_path / "klines.csv"
    output_path = tmp_path / "reports" / "backtest.json"
    _write_backtest_csv(csv_path)

    result = _run_backtest_csv(
        _runtime_backtest_config(db_path),
        csv_path,
        observe_rows=60,
        symbol="BTCUSDT",
        funding_rate=0.0001,
        output_path=output_path,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["output_path"] == str(output_path)
    assert report["summary"]["symbol"] == "BTCUSDT"
    assert "max_drawdown" in report["summary"]
    assert "win_rate" in report["summary"]
    assert 0.0 <= report["summary"]["win_rate"] <= 1.0
    assert "avg_grid_pnl" in report["summary"]
    assert "fills_per_bar" in report["summary"]
    assert "equity_sharpe" in report["summary"]
    assert report["grid_params"]["symbol"] == "BTCUSDT"
    assert isinstance(report["grid_params"]["grid_prices"], list)
    assert report["fills"]
    assert {"side", "grid_index", "price", "qty", "bar_index"}.issubset(report["fills"][0])
    assert report["equity_curve"]
    assert {"bar_index", "equity", "drawdown", "close"}.issubset(report["equity_curve"][0])


def test_backtest_dir_aggregates_csv_files_and_errors(tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()
    _write_backtest_csv(csv_dir / "window-a.csv")
    _write_backtest_csv(csv_dir / "window-b.csv")
    (csv_dir / "bad.csv").write_text("timestamp,high,close\n1,100,100\n", encoding="utf-8")
    output_path = tmp_path / "reports" / "batch.json"

    result = _run_backtest_dir(
        _runtime_backtest_config(db_path),
        csv_dir,
        observe_rows=60,
        symbol="BTCUSDT",
        funding_rate=0.0001,
        output_path=output_path,
    )

    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["files"] == 3
    assert result["succeeded"] == 2
    assert result["failed"] == 1
    assert result["total_fills"] >= 2
    assert result["total_grid_trades"] == sum(item["grid_trade_count"] for item in report["reports"])
    assert 0.0 <= result["win_rate"] <= 1.0
    assert "avg_grid_pnl" in result
    assert "avg_equity_sharpe" in result
    assert result["best_file"] in {"window-a.csv", "window-b.csv"}
    assert result["output_path"] == str(output_path)
    assert report["summary"]["succeeded"] == 2
    assert [item["source_file"] for item in report["reports"]] == ["window-a.csv", "window-b.csv"]
    assert report["errors"][0]["source_file"] == "bad.csv"


def test_backtest_dir_rejects_all_failed_files(tmp_path) -> None:
    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()
    (csv_dir / "bad.csv").write_text("timestamp,high,close\n1,100,100\n", encoding="utf-8")

    try:
        _run_backtest_dir(
            _runtime_backtest_config(tmp_path / "trader.db"),
            csv_dir,
            observe_rows=60,
            symbol="BTCUSDT",
            funding_rate=0.0,
        )
    except RuntimeError as exc:
        assert "批量回测没有成功样本" in str(exc)
    else:
        raise AssertionError("batch backtest with only invalid files should fail")


def test_backtest_csv_rejects_missing_required_columns(tmp_path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("timestamp,high,close\n1,100,100\n", encoding="utf-8")

    try:
        _run_backtest_csv(
            _runtime_backtest_config(tmp_path / "trader.db"),
            csv_path,
            observe_rows=1,
            symbol="BTCUSDT",
            funding_rate=0.0,
        )
    except RuntimeError as exc:
        assert "缺少必要列" in str(exc)
    else:
        raise AssertionError("missing low column should fail")


def test_main_backtest_csv_mode_does_not_require_testnet(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    csv_path = tmp_path / "klines.csv"
    _write_backtest_csv(csv_path)
    config = _runtime_backtest_config(db_path)
    captured = {}

    monkeypatch.setattr(
        sys,
        "argv",
        ["trader.py", "--backtest-csv", str(csv_path), "--backtest-observe-rows", "60", "--backtest-symbol", "BTCUSDT"],
    )
    monkeypatch.setattr("trader.load_config", lambda: config)
    monkeypatch.setattr("trader.setup_logging", lambda raw: None)
    monkeypatch.setattr("trader.init_db", lambda path: None)
    monkeypatch.setattr("trader._run_backtest_csv", lambda *args, **kwargs: captured.setdefault("called", True) or {"ok": True})

    main()

    assert captured["called"] is True


def test_main_backtest_dir_mode_does_not_require_testnet(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()
    _write_backtest_csv(csv_dir / "window-a.csv")
    config = _runtime_backtest_config(db_path)
    captured = {}

    monkeypatch.setattr(
        sys,
        "argv",
        ["trader.py", "--backtest-dir", str(csv_dir), "--backtest-observe-rows", "60", "--backtest-symbol", "BTCUSDT"],
    )
    monkeypatch.setattr("trader.load_config", lambda: config)
    monkeypatch.setattr("trader.setup_logging", lambda raw: None)
    monkeypatch.setattr("trader.init_db", lambda path: None)
    monkeypatch.setattr("trader._run_backtest_dir", lambda *args, **kwargs: captured.setdefault("called", True) or {"ok": True})

    main()

    assert captured["called"] is True


def test_main_default_mode_does_not_require_testnet(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    config = SimpleNamespace(
        binance_api_key="",
        binance_api_secret="",
        binance_testnet=False,
        database_path=db_path,
        raw={"logging": {}, "database": {"path": str(db_path)}},
    )

    monkeypatch.setattr(sys, "argv", ["trader.py"])
    monkeypatch.setattr("trader.load_config", lambda: config)
    monkeypatch.setattr("trader.setup_logging", lambda raw: None)
    monkeypatch.setattr("trader.init_db", lambda path: None)

    main()


def test_main_selects_account_before_initializing_database(monkeypatch, tmp_path) -> None:
    base_db = tmp_path / "base.db"
    selected_db = tmp_path / "selected.db"
    base_config = SimpleNamespace(
        binance_api_key="base-key",
        binance_api_secret="base-secret",
        binance_testnet=True,
        database_path=base_db,
        raw={"logging": {}, "database": {"path": str(base_db)}},
    )
    selected_config = SimpleNamespace(
        account_id="hedge",
        binance_api_key="selected-key",
        binance_api_secret="selected-secret",
        binance_testnet=True,
        database_path=selected_db,
        raw={"logging": {}, "database": {"path": str(selected_db)}},
    )
    captured = {}

    def fake_select_account(config_arg, account_id):
        captured["selected_from"] = config_arg
        captured["account_id"] = account_id
        return selected_config

    async def fake_run_binance_test_run(config_arg, max_seconds=600.0):
        captured["runner_config"] = config_arg
        captured["max_seconds"] = max_seconds
        return {"test_run_ok": True}

    monkeypatch.setattr(sys, "argv", ["trader.py", "--account-id", "hedge", "--binance-test-run", "--loop-seconds", "30"])
    monkeypatch.setattr("trader.load_config", lambda: base_config)
    monkeypatch.setattr("trader.select_account", fake_select_account)
    monkeypatch.setattr("trader.setup_logging", lambda raw: captured.setdefault("logging_raw", raw))
    monkeypatch.setattr("trader.init_db", lambda path: captured.setdefault("db_path", path))
    monkeypatch.setattr("trader._run_binance_test_run", fake_run_binance_test_run)

    main()

    assert captured["selected_from"] is base_config
    assert captured["account_id"] == "hedge"
    assert captured["db_path"] == selected_db
    assert captured["runner_config"] is selected_config
    assert captured["max_seconds"] == 30.0


def test_main_forwards_loop_bounds_to_binance_loop(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    config = SimpleNamespace(
        binance_api_key="key",
        binance_api_secret="secret",
        binance_testnet=True,
        database_path=db_path,
        raw={"logging": {}, "database": {"path": str(db_path)}},
    )
    captured = {}

    async def fake_run_binance_loop(config_arg, max_iterations=None, max_seconds=None):
        captured["config"] = config_arg
        captured["max_iterations"] = max_iterations
        captured["max_seconds"] = max_seconds
        return ["bounded"]

    monkeypatch.setattr(sys, "argv", ["trader.py", "--binance-loop", "--loop-iterations", "7", "--loop-seconds", "12.5"])
    monkeypatch.setattr("trader.load_config", lambda: config)
    monkeypatch.setattr("trader.setup_logging", lambda raw: None)
    monkeypatch.setattr("trader.init_db", lambda path: None)
    monkeypatch.setattr("trader._run_binance_loop", fake_run_binance_loop)

    main()

    assert captured == {"config": config, "max_iterations": 7, "max_seconds": 12.5}


def test_main_forwards_loop_seconds_to_binance_test_run(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    config = SimpleNamespace(
        binance_api_key="key",
        binance_api_secret="secret",
        binance_testnet=True,
        database_path=db_path,
        raw={"logging": {}, "database": {"path": str(db_path)}},
    )
    captured = {}

    async def fake_run_binance_test_run(config_arg, max_seconds=600.0):
        captured["config"] = config_arg
        captured["max_seconds"] = max_seconds
        return {"test_run_ok": True}

    monkeypatch.setattr(sys, "argv", ["trader.py", "--binance-test-run", "--loop-seconds", "30"])
    monkeypatch.setattr("trader.load_config", lambda: config)
    monkeypatch.setattr("trader.setup_logging", lambda raw: None)
    monkeypatch.setattr("trader.init_db", lambda path: None)
    monkeypatch.setattr("trader._run_binance_test_run", fake_run_binance_test_run)

    main()

    assert captured == {"config": config, "max_seconds": 30.0}


def test_main_binance_test_run_uses_safe_default_seconds(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    config = SimpleNamespace(
        binance_api_key="key",
        binance_api_secret="secret",
        binance_testnet=True,
        database_path=db_path,
        raw={"logging": {}, "database": {"path": str(db_path)}},
    )
    captured = {}

    async def fake_run_binance_test_run(config_arg, max_seconds=600.0):
        captured["max_seconds"] = max_seconds
        return {"test_run_ok": True}

    monkeypatch.setattr(sys, "argv", ["trader.py", "--binance-test-run"])
    monkeypatch.setattr("trader.load_config", lambda: config)
    monkeypatch.setattr("trader.setup_logging", lambda raw: None)
    monkeypatch.setattr("trader.init_db", lambda path: None)
    monkeypatch.setattr("trader._run_binance_test_run", fake_run_binance_test_run)

    main()

    assert captured == {"max_seconds": 600.0}


class FakeController:
    def __init__(self) -> None:
        self.active_sessions: dict[str, object] = {}
        self.price_events: list[dict[str, Any]] = []

    async def handle_price_update_event(self, event: dict[str, Any]) -> None:
        self.price_events.append(event)


class FakeExchange:
    def __init__(self) -> None:
        self.subscriptions: list[list[str]] = []
        self.started = asyncio.Event()
        self.cancelled = 0

    async def run_price_stream(self, symbols, handler):
        self.subscriptions.append(list(symbols))
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class FailingPriceStreamExchange(FakeExchange):
    async def run_price_stream(self, symbols, handler):
        self.subscriptions.append(list(symbols))
        self.started.set()
        raise RuntimeError("price stream failed")


class FinishedPriceStreamExchange(FakeExchange):
    async def run_price_stream(self, symbols, handler):
        self.subscriptions.append(list(symbols))
        self.started.set()
        return None


def test_dynamic_price_stream_restarts_when_active_symbols_change() -> None:
    async def run() -> None:
        exchange = FakeExchange()
        controller = FakeController()
        task = asyncio.create_task(_run_dynamic_price_stream(exchange, controller, poll_seconds=0.01))

        controller.active_sessions["AAPLUSDT"] = object()
        await asyncio.wait_for(exchange.started.wait(), timeout=1)
        assert exchange.subscriptions == [["AAPLUSDT"]]

        exchange.started.clear()
        controller.active_sessions["MSFTUSDT"] = object()
        await asyncio.wait_for(exchange.started.wait(), timeout=1)
        assert exchange.subscriptions[-1] == ["AAPLUSDT", "MSFTUSDT"]
        assert exchange.cancelled >= 1

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())


def test_dynamic_price_stream_fails_when_inner_stream_finishes() -> None:
    async def run() -> None:
        exchange = FinishedPriceStreamExchange()
        controller = FakeController()
        controller.active_sessions["AAPLUSDT"] = object()

        try:
            await _run_dynamic_price_stream(exchange, controller, poll_seconds=0.01)
        except RuntimeError as exc:
            assert "price stream stopped unexpectedly" in str(exc)
        else:
            raise AssertionError("inner price stream completion should stop dynamic price stream")

        assert exchange.subscriptions == [["AAPLUSDT"]]

    asyncio.run(run())


def test_dynamic_price_stream_propagates_inner_stream_failure() -> None:
    async def run() -> None:
        exchange = FailingPriceStreamExchange()
        controller = FakeController()
        controller.active_sessions["AAPLUSDT"] = object()

        try:
            await _run_dynamic_price_stream(exchange, controller, poll_seconds=0.01)
        except RuntimeError as exc:
            assert "price stream failed" in str(exc)
        else:
            raise AssertionError("inner price stream failure should propagate")

        assert exchange.subscriptions == [["AAPLUSDT"]]

    asyncio.run(run())


class CloseableMockExchange(MockExchangeClient):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        return None


class OrderSmokeExchange(CloseableMockExchange):
    def __init__(self) -> None:
        super().__init__()
        self.symbols = [{"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL"}]
        self.leverage_calls: list[tuple[str, int]] = []
        self.margin_calls: list[tuple[str, str]] = []
        self.cancelled_orders: list[tuple[str, str]] = []
        self.created_limit_orders: list[dict[str, Any]] = []
        self.test_orders: list[dict[str, Any]] = []
        self.listen_key_closed = False
        self.algo_orders: list[dict[str, Any]] = []
        self.cancelled_algo_ids: list[int] = []

    async def get_symbol_rules(self, symbol: str) -> dict[str, Any]:
        return {"tick_size": 0.1, "step_size": 0.001, "min_qty": 0.001, "min_notional": 50.0}

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "lastPrice": "100.0", "quoteVolume": "1000000"}

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage_calls.append((symbol, leverage))

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        self.margin_calls.append((symbol, margin_type))

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        self.orders[symbol][-1]["orderId"] = "limit-1"
        self.created_limit_orders.append({**self.orders[symbol][-1]})
        return {**self.orders[symbol][-1]}

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ) -> dict[str, Any]:
        await super().place_stop_market_order(symbol, side, stop_price, client_id, close_position)
        self.stop_orders[symbol][-1]["orderId"] = "stop-1"
        return {**self.stop_orders[symbol][-1]}

    async def get_order(self, symbol: str, order_id: str, client_id: str) -> dict[str, Any]:
        for order in self.orders.get(symbol, []) + self.stop_orders.get(symbol, []):
            if str(order.get("orderId", "")) == str(order_id) or str(order.get("client_id", "")) == str(client_id):
                return {
                    "symbol": symbol,
                    "orderId": order.get("orderId", order_id),
                    "clientOrderId": order.get("client_id", client_id),
                    "status": "NEW",
                }
        return {"symbol": symbol, "orderId": order_id, "clientOrderId": client_id, "status": "NEW"}

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        self.cancelled_orders.append((symbol, order_id))
        await super().cancel_order(symbol, order_id)

    async def test_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ):
        order = {"symbol": symbol, "side": side, "price": price, "qty": qty, "client_id": client_id}
        if position_side is not None:
            order["position_side"] = position_side
        self.test_orders.append(order)
        return {"orderId": 0, **order}

    async def test_market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = True):
        order = {"symbol": symbol, "side": side, "qty": qty, "reduce_only": reduce_only}
        self.test_orders.append(order)
        return {"orderId": 0, **order}

    async def test_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ):
        order = {
            "symbol": symbol,
            "side": side,
            "stop_price": stop_price,
            "client_id": client_id,
            "close_position": close_position,
        }
        self.test_orders.append(order)
        return {"orderId": 0, **order}

    async def create_futures_listen_key(self) -> str:
        return "listen-key"

    async def keepalive_futures_listen_key(self, listen_key: str):
        return {"listenKey": listen_key}

    async def close_futures_listen_key(self, listen_key: str):
        self.listen_key_closed = True
        return {}

    async def get_position_mode(self):
        return {"dualSidePosition": True}

    async def place_algo_stop_market_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        trigger_price: float,
        qty: float,
        client_algo_id: str,
    ):
        order = {
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "triggerPrice": trigger_price,
            "quantity": qty,
            "clientAlgoId": client_algo_id,
            "algoId": 456,
            "algoStatus": "NEW",
        }
        self.algo_orders.append(order)
        return order

    async def get_open_algo_orders(self, symbol: str):
        return [order for order in self.algo_orders if order["symbol"] == symbol]

    async def cancel_algo_order(self, symbol: str, algo_id: int | str):
        self.cancelled_algo_ids.append(int(algo_id))
        self.algo_orders = [order for order in self.algo_orders if int(order["algoId"]) != int(algo_id)]
        return {"algoId": int(algo_id), "code": "200", "msg": "success"}


class FailedCreateNoOpenOrdersExchange(OrderSmokeExchange):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_all_called = False

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("create status unknown")

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return []

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_all_called = True
        raise AssertionError("cancel all should not run when reconciliation sees no open orders")


class TimeoutButCreatedOrderSmokeExchange(OrderSmokeExchange):
    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        raise RuntimeError("create status unknown")


class DelayedLookupAfterTimeoutOrderSmokeExchange(OrderSmokeExchange):
    def __init__(self) -> None:
        super().__init__()
        self.limit_lookup_calls = 0
        self.limit_recovered = False

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)
        raise RuntimeError(
            "APIError(code=-1007): Timeout waiting for response from backend server. "
            "Send status unknown; execution status unknown."
        )

    async def get_order(self, symbol: str, order_id: str, client_id: str) -> dict[str, Any]:
        if client_id.startswith("qgsm-l-") and not self.limit_recovered:
            self.limit_lookup_calls += 1
            if self.limit_lookup_calls < 3:
                return {"symbol": symbol, "orderId": "", "clientOrderId": client_id, "status": "UNKNOWN"}
            self.limit_recovered = True
        return await super().get_order(symbol, order_id, client_id)


class FirstSymbolFailsOrderSmokeExchange(OrderSmokeExchange):
    def __init__(self) -> None:
        super().__init__()
        self.symbols = [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        ]
        self.failed_symbols: list[str] = []

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        if symbol == "BTCUSDT":
            self.failed_symbols.append(symbol)
            raise RuntimeError("BTC create status unknown")
        return await super().place_limit_order_post_only(symbol, side, price, qty, client_id, position_side)


class PriceStreamSmokeExchange(OrderSmokeExchange):
    def __init__(self) -> None:
        super().__init__()
        self.price_stream_symbols: list[str] | None = None

    async def run_price_stream(self, symbols, handler, reconnect_delay_seconds=5, max_reconnects=None):
        self.price_stream_symbols = list(symbols)
        await handler({"symbol": symbols[0], "price": 101.5, "event_time": "now"})


class NoExchangeInfoPriceStreamExchange(PriceStreamSmokeExchange):
    async def get_symbols(self):
        raise RuntimeError("exchangeInfo unavailable")


class MarketRoundtripSmokeExchange(OrderSmokeExchange):
    def __init__(self) -> None:
        super().__init__()
        self.market_order_requests: list[dict[str, Any]] = []

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        request = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "reduce_only": reduce_only,
            "position_side": position_side,
            "client_id": client_id,
        }
        self.market_order_requests.append(request)
        if reduce_only:
            if side == "SELL":
                self.positions[symbol] = max(0.0, self.positions.get(symbol, 0.0) - qty)
            else:
                self.positions[symbol] = min(0.0, self.positions.get(symbol, 0.0) + qty)
        else:
            self.positions[symbol] = self.positions.get(symbol, 0.0) + (qty if side == "BUY" else -qty)
        return {
            "orderId": f"market-{len(self.market_order_requests)}",
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": reduce_only,
            "positionSide": position_side,
            "clientOrderId": client_id,
            "status": "FILLED",
        }


class PositionSmokeExchange(OrderSmokeExchange):
    async def get_position(self, symbol: str):
        return {
            "symbol": symbol,
            "qty": 0.1,
            "long_qty": 0.3,
            "short_qty": 0.2,
            "positions": [
                {"symbol": symbol, "positionSide": "LONG", "qty": 0.3},
                {"symbol": symbol, "positionSide": "SHORT", "qty": -0.2},
            ],
        }


class NoExchangeInfoPositionSmokeExchange(PositionSmokeExchange):
    async def get_symbols(self):
        raise RuntimeError("exchangeInfo unavailable")


class SafetySweepExchange(OrderSmokeExchange):
    def __init__(self) -> None:
        super().__init__()
        self.orders["BTCUSDT"] = [
            {
                "symbol": "BTCUSDT",
                "orderId": "ordinary-1",
                "client_id": "qgsm-l-old",
                "status": "NEW",
            }
        ]
        self.algo_orders = [
            {
                "symbol": "BTCUSDT",
                "algoId": 456,
                "clientAlgoId": "qgalgo-old",
                "algoStatus": "NEW",
            }
        ]
        self.positions["BTCUSDT"] = -0.25
        self.cancel_all_symbols: list[str] = []

    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_all_symbols.append(symbol)
        await super().cancel_all_orders(symbol)
        self.algo_orders = [order for order in self.algo_orders if order["symbol"] != symbol]


class CancelAllFailsSafetySweepExchange(SafetySweepExchange):
    async def cancel_all_orders(self, symbol: str) -> None:
        self.cancel_all_symbols.append(symbol)
        raise RuntimeError("cancel all failed")


class EmptySafetySweepExchange(OrderSmokeExchange):
    async def cancel_all_orders(self, symbol: str) -> None:
        raise AssertionError("cancel all should not run when there are no open orders")


class NonZeroMakerFeeExchange(CloseableMockExchange):
    async def get_commission_rate(self, symbol: str) -> dict[str, float]:
        if symbol == "MSFTUSDT":
            return {"maker": 0.0002, "taker": 0.0005}
        return {"maker": 0.0, "taker": 0.0005}


class FakeStartupCheck:
    ok = True
    reason = "ok"


class FakeBinanceOnceController:
    def __init__(self) -> None:
        self.active_sessions = {"AAPLUSDT": object()}
        self.recovered = False
        self.recoverable_symbols: set[str] | None = None
        self.cleaned_reason: str | None = None

    async def validate_startup(self):
        return FakeStartupCheck()

    async def recover_unclosed_sessions(self, at=None, recoverable_symbols=None):
        self.recovered = True
        self.recoverable_symbols = recoverable_symbols

    async def run_once(self):
        return {"status": "started"}

    async def close_all_active_sessions(self, reason, at=None):
        self.cleaned_reason = reason
        self.active_sessions = {}
        return ["AAPLUSDT"]


class FailingCleanupOnceController(FakeBinanceOnceController):
    async def close_all_active_sessions(self, reason, at=None):
        self.cleaned_reason = reason
        return []


class LoopExchange(CloseableMockExchange):
    def __init__(self) -> None:
        super().__init__()
        self.user_stream_cancelled = False

    async def run_user_stream(self, handler):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.user_stream_cancelled = True
            raise


class FailingUserStreamExchange(CloseableMockExchange):
    async def run_user_stream(self, handler):
        raise RuntimeError("user stream failed")


class FinishedUserStreamExchange(CloseableMockExchange):
    async def run_user_stream(self, handler):
        return None


class SignedWriteFailingExchange(CloseableMockExchange):
    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        raise RuntimeError("signed write timeout")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        raise RuntimeError("signed write timeout")


class FakeBinanceLoopController:
    def __init__(self) -> None:
        self.active_sessions = {"AAPLUSDT": object()}
        self.recovered = False
        self.recoverable_symbols: set[str] | None = None
        self.cleaned_reason: str | None = None
        self.loop_max_iterations: int | None = None

    async def validate_startup(self):
        return FakeStartupCheck()

    async def recover_unclosed_sessions(self, at=None, recoverable_symbols=None):
        self.recovered = True
        self.recoverable_symbols = recoverable_symbols

    async def handle_order_filled_event(self, event):
        return None

    async def handle_price_update_event(self, event):
        return None

    async def run_loop(self, max_iterations=None):
        self.loop_max_iterations = max_iterations
        await asyncio.sleep(0)
        raise RuntimeError("loop failed")

    async def close_all_active_sessions(self, reason, at=None):
        self.cleaned_reason = reason
        self.active_sessions = {}
        return ["AAPLUSDT"]


class WaitingBinanceLoopController(FakeBinanceLoopController):
    def __init__(self) -> None:
        super().__init__()
        self.loop_cancelled = False

    async def run_loop(self, max_iterations=None):
        self.loop_max_iterations = max_iterations
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.loop_cancelled = True
            raise


def test_binance_once_cleans_up_active_sessions_before_exit(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = CloseableMockExchange()
        controller = FakeBinanceOnceController()

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": BINANCE_SAFE_SELECTION},
        )

        result = await _run_binance_once(config)

        assert result == {"status": "started"}
        assert controller.recovered is True
        assert controller.recoverable_symbols == {"AAPLUSDT"}
        assert controller.cleaned_reason == "binance_once_cleanup"
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_once_raises_when_cleanup_leaves_active_sessions(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = CloseableMockExchange()
        controller = FailingCleanupOnceController()

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": BINANCE_SAFE_SELECTION},
        )

        try:
            await _run_binance_once(config)
        except RuntimeError as exc:
            assert "binance_once_cleanup" in str(exc)
            assert "AAPLUSDT" in str(exc)
        else:
            raise AssertionError("cleanup residual active sessions should fail the Binance once entrypoint")

        assert controller.recovered is True
        assert controller.recoverable_symbols == {"AAPLUSDT"}
        assert controller.cleaned_reason == "binance_once_cleanup"
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_live_entrypoints_require_signed_write_health(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        calls = [
            (_run_binance_once, "binance_once"),
            (_run_binance_loop, "binance_loop"),
            (_run_binance_signed_write_health, "binance_signed_write_health"),
        ]

        def fail_build_controller(*args, **kwargs):
            raise AssertionError("controller should not be built when signed write health fails")

        monkeypatch.setattr("trader._build_controller", fail_build_controller)
        for runner, caller in calls:
            exchange = SignedWriteFailingExchange()

            async def fake_create(**kwargs):
                return exchange

            monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
            config = SimpleNamespace(
                binance_api_key="key",
                binance_api_secret="secret",
                binance_testnet=True,
                database_path=db_path,
                raw={
                    "trading": {"leverage": 10},
                    "proxy": {"enabled": False},
                    "selection": BINANCE_SAFE_SELECTION,
                    "timing": {"loop_interval_seconds": 10},
                },
            )

            try:
                await runner(config)
            except RuntimeError as exc:
                assert "signed write health check failed" in str(exc)
                assert caller in str(exc)
                assert "signed write timeout" in str(exc)
            else:
                raise AssertionError("live Binance entrypoint should stop when signed writes fail")

            assert exchange.closed is True
            log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
            assert log["level"] == "ERROR"
            assert log["module"] == "binance_signed_write_health"
            assert caller in log["message"]
            assert "signed write timeout" in log["detail"]

    asyncio.run(run())


def test_binance_entrypoints_require_testnet(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)

        async def fail_create(**kwargs):
            raise AssertionError("Binance client should not be created when testnet is disabled")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fail_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=False,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        for runner in (
            _run_binance_once,
            _run_binance_check,
            _run_binance_loop,
            _run_binance_signed_write_health,
            _run_binance_position_smoke,
            _run_binance_safety_sweep,
            _run_binance_market_roundtrip_smoke,
        ):
            try:
                await runner(config)
            except RuntimeError as exc:
                assert "BINANCE_TESTNET=true" in str(exc)
            else:
                raise AssertionError("Binance entrypoint should require testnet mode")

    asyncio.run(run())


def test_binance_entrypoints_require_api_credentials(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)

        async def fail_create(**kwargs):
            raise AssertionError("Binance client should not be created without API credentials")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fail_create)
        config = SimpleNamespace(
            binance_api_key="",
            binance_api_secret="",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        for runner in (
            _run_binance_once,
            _run_binance_check,
            _run_binance_loop,
            _run_binance_signed_write_health,
            _run_binance_position_smoke,
            _run_binance_safety_sweep,
            _run_binance_market_roundtrip_smoke,
        ):
            try:
                await runner(config)
            except RuntimeError as exc:
                assert "BINANCE_API_KEY" in str(exc)
                assert "BINANCE_API_SECRET" in str(exc)
            else:
                raise AssertionError("Binance entrypoint should require API credentials")

    asyncio.run(run())


def test_binance_loop_exits_and_cleans_up_when_user_stream_fails(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = FailingUserStreamExchange()
        controller = WaitingBinanceLoopController()
        price_stream_cancelled = False

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def fake_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            nonlocal price_stream_cancelled
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                price_stream_cancelled = True
                raise

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", fake_dynamic_price_stream)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        try:
            await _run_binance_loop(config)
        except RuntimeError as exc:
            assert "user stream failed" in str(exc)
        else:
            raise AssertionError("user stream failure should stop Binance loop")

        assert controller.loop_cancelled is True
        assert controller.cleaned_reason == "binance_loop_shutdown_cleanup"
        assert controller.active_sessions == {}
        assert price_stream_cancelled is True
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_loop_logs_when_user_stream_stops_without_error(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = FinishedUserStreamExchange()
        controller = WaitingBinanceLoopController()
        controller.repository = Repository(db_path)
        price_stream_cancelled = False

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def fake_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            nonlocal price_stream_cancelled
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                price_stream_cancelled = True
                raise

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", fake_dynamic_price_stream)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        try:
            await _run_binance_loop(config)
        except RuntimeError as exc:
            assert "user stream stopped unexpectedly" in str(exc)
        else:
            raise AssertionError("user stream completion should stop Binance loop")

        logs = Repository(db_path).recent_rows("system_logs", limit=1)
        assert logs[0]["level"] == "ERROR"
        assert logs[0]["module"] == "binance_loop"
        assert "user stream stopped unexpectedly" in logs[0]["message"]
        assert controller.loop_cancelled is True
        assert price_stream_cancelled is True
        assert controller.cleaned_reason == "binance_loop_shutdown_cleanup"
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_loop_exits_and_cleans_up_when_price_stream_fails(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = LoopExchange()
        controller = WaitingBinanceLoopController()

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def failing_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            raise RuntimeError("price stream failed")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", failing_dynamic_price_stream)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        try:
            await _run_binance_loop(config)
        except RuntimeError as exc:
            assert "price stream failed" in str(exc)
        else:
            raise AssertionError("price stream failure should stop Binance loop")

        assert controller.loop_cancelled is True
        assert controller.cleaned_reason == "binance_loop_shutdown_cleanup"
        assert controller.active_sessions == {}
        assert exchange.user_stream_cancelled is True
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_loop_cleans_up_active_sessions_on_exit(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = LoopExchange()
        controller = FakeBinanceLoopController()
        price_stream_cancelled = False

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def fake_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            nonlocal price_stream_cancelled
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                price_stream_cancelled = True
                raise

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", fake_dynamic_price_stream)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        try:
            await _run_binance_loop(config)
        except RuntimeError as exc:
            assert "loop failed" in str(exc)
        else:
            raise AssertionError("loop failure should propagate after cleanup")

        assert controller.recovered is True
        assert controller.recoverable_symbols == {"AAPLUSDT"}
        assert controller.cleaned_reason == "binance_loop_shutdown_cleanup"
        assert exchange.user_stream_cancelled is True
        assert price_stream_cancelled is True
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_loop_passes_max_iterations_to_controller(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = LoopExchange()
        controller = FakeBinanceLoopController()

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def fake_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            await asyncio.Future()

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", fake_dynamic_price_stream)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        try:
            await _run_binance_loop(config, max_iterations=3)
        except RuntimeError as exc:
            assert "loop failed" in str(exc)
        else:
            raise AssertionError("loop failure should propagate after cleanup")

        assert controller.loop_max_iterations == 3
        assert controller.cleaned_reason == "binance_loop_shutdown_cleanup"
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_loop_bounded_seconds_cleans_up_active_sessions(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = LoopExchange()
        controller = WaitingBinanceLoopController()
        controller.repository = Repository(db_path)
        price_stream_cancelled = False
        sweep_calls = []

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def fake_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            nonlocal price_stream_cancelled
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                price_stream_cancelled = True
                raise

        async def fake_sweep(exchange_arg, repository_arg, eligible):
            sweep_calls.append((exchange_arg, repository_arg, tuple(eligible)))
            return {"safety_sweep_ok": True, "symbols": [], "closed_sessions": []}

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", fake_dynamic_price_stream)
        monkeypatch.setattr("trader._sweep_binance_symbols", fake_sweep)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        result = await _run_binance_loop(config, max_seconds=0.01)

        assert result == ["loop_timeout"]
        assert controller.cleaned_reason == "binance_loop_shutdown_cleanup"
        assert controller.active_sessions == {}
        assert controller.loop_cancelled is True
        assert exchange.user_stream_cancelled is True
        assert price_stream_cancelled is True
        assert sweep_calls == [(exchange, controller.repository, ("AAPLUSDT",))]
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_loop_shutdown_cleanup_timeout_uses_safety_sweep_fallback(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = LoopExchange()
        controller = WaitingBinanceLoopController()
        controller.repository = Repository(db_path)
        fallback_calls = []

        async def hanging_close_all(reason, at=None):
            controller.cleaned_reason = reason
            await asyncio.Future()

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def fake_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            await asyncio.Future()

        async def fake_sweep(exchange_arg, repository_arg, eligible):
            fallback_calls.append((exchange_arg, repository_arg, tuple(eligible)))
            controller.active_sessions = {}
            return {"safety_sweep_ok": True, "symbols": [], "closed_sessions": []}

        controller.close_all_active_sessions = hanging_close_all  # type: ignore[method-assign]
        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", fake_dynamic_price_stream)
        monkeypatch.setattr("trader._sweep_binance_symbols", fake_sweep)
        monkeypatch.setattr("trader.BINANCE_LOOP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS", 0.01)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        result = await _run_binance_loop(config, max_seconds=0.01)

        logs = Repository(db_path).recent_rows("system_logs", limit=5)
        messages = [row["message"] for row in logs]

        assert result == ["loop_timeout"]
        assert fallback_calls == [(exchange, controller.repository, ("AAPLUSDT",))]
        assert "Binance loop shutdown cleanup failed; running safety sweep fallback." in messages
        assert "Binance testnet safety sweep completed." in messages
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_loop_cancel_drain_timeout_still_runs_shutdown_cleanup(monkeypatch, tmp_path) -> None:
    class StubbornCancelController(FakeBinanceLoopController):
        async def run_loop(self, max_iterations=None):
            self.loop_max_iterations = max_iterations
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await asyncio.Future()

    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = LoopExchange()
        controller = StubbornCancelController()

        async def fake_create(**kwargs):
            return exchange

        def fake_build_controller(exchange_arg, config_arg, live_observation=None):
            assert exchange_arg is exchange
            assert live_observation is True
            return controller

        async def fake_dynamic_price_stream(exchange_arg, controller_arg, poll_seconds=10):
            await asyncio.Future()

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fake_build_controller)
        monkeypatch.setattr("trader._run_dynamic_price_stream", fake_dynamic_price_stream)
        monkeypatch.setattr("trader.BINANCE_LOOP_TASK_CANCEL_TIMEOUT_SECONDS", 0.01)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": BINANCE_SAFE_SELECTION,
                "timing": {"loop_interval_seconds": 10},
            },
        )

        result = await _run_binance_loop(config, max_seconds=0.01)

        assert result == ["loop_timeout"]
        assert controller.cleaned_reason == "binance_loop_shutdown_cleanup"
        assert controller.active_sessions == {}
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_entrypoints_require_symbol_allowlist(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)

        async def fail_create(**kwargs):
            raise AssertionError("Binance client should not be created without an allowlist")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fail_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": []},
                "timing": {"loop_interval_seconds": 10},
            },
        )

        for runner in (_run_binance_once, _run_binance_check, _run_binance_loop, _run_binance_position_smoke, _run_binance_safety_sweep, _run_binance_market_roundtrip_smoke):
            try:
                await runner(config)
            except RuntimeError as exc:
                assert "selection.symbol_allowlist" in str(exc)
            else:
                raise AssertionError("Binance entrypoint should require an explicit symbol allowlist")

    asyncio.run(run())


def test_binance_entrypoints_require_tradable_allowlist_symbol(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        created_exchanges: list[CloseableMockExchange] = []

        async def fake_create(**kwargs):
            exchange = CloseableMockExchange()
            created_exchanges.append(exchange)
            return exchange

        def fail_build_controller(*args, **kwargs):
            raise AssertionError("controller should not be built when allowlist has no tradable symbols")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fail_build_controller)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["NOTREALUSDT"], "symbol_blacklist": []},
                "timing": {"loop_interval_seconds": 10},
            },
        )

        for runner in (_run_binance_once, _run_binance_check, _run_binance_loop, _run_binance_position_smoke, _run_binance_safety_sweep, _run_binance_market_roundtrip_smoke):
            try:
                await runner(config)
            except RuntimeError as exc:
                assert "未匹配到任何可交易 USDT 合约" in str(exc)
            else:
                raise AssertionError("Binance entrypoint should require at least one tradable allowlist symbol")

        assert len(created_exchanges) == 6
        assert all(exchange.closed for exchange in created_exchanges)

    asyncio.run(run())


def test_binance_check_normalizes_allowlist_and_blacklist(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = CloseableMockExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {
                    "capital_per_symbol": 200,
                    "leverage": 10,
                    "max_concurrent": 1,
                    "take_profit_usdt": 10,
                    "total_capital_limit": 1000,
                    "stop_buffer_pct": 0.015,
                },
                "timing": {
                    "observe_hours": 1,
                    "observe_kline_interval": "1m",
                    "observe_check_seconds": 60,
                    "force_close_minutes": 120,
                    "loop_interval_seconds": 10,
                    "scheduler_check_minutes": 5,
                },
                "grid": {
                    "range_method": "std",
                    "std_k": 1.8,
                    "quantile_upper": 0.95,
                    "quantile_lower": 0.05,
                    "min_step_pct": 0.0015,
                    "safety_multiplier": 3.5,
                    "max_grid_num": 20,
                    "max_range_pct": 0.05,
                },
                "cooldown": {
                    "atr_period": 14,
                    "calm_window_minutes": 30,
                    "atr_recovery_ratio": 0.8,
                    "amplitude_multiplier": 2.0,
                },
                "selection": {
                    "volume_weight": 0.7,
                    "depth_weight": 0.3,
                    "depth_levels": 5,
                    "symbol_allowlist": [" aaplusdt ", " msftusdt "],
                    "symbol_blacklist": [" msftusdt "],
                },
                "proxy": {"enabled": False},
            },
        )

        result = await _run_binance_check(config)

        assert result["eligible_symbols"] == 1
        assert result["sample_symbol"] == "AAPLUSDT"
        assert result["commission_health"]["status"] == "ok"
        assert result["commission_health"]["ok_count"] == 1
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_check_warns_when_candidate_maker_fee_exceeds_limit(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = NonZeroMakerFeeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {
                    "capital_per_symbol": 200,
                    "leverage": 10,
                    "max_concurrent": 2,
                    "take_profit_usdt": 10,
                    "total_capital_limit": 1000,
                    "stop_buffer_pct": 0.015,
                    "max_maker_fee_rate": 0.0,
                },
                "timing": {
                    "observe_hours": 1,
                    "observe_kline_interval": "1m",
                    "observe_check_seconds": 60,
                    "force_close_minutes": 120,
                    "loop_interval_seconds": 10,
                    "scheduler_check_minutes": 5,
                },
                "grid": {
                    "range_method": "std",
                    "std_k": 1.8,
                    "quantile_upper": 0.95,
                    "quantile_lower": 0.05,
                    "min_step_pct": 0.0015,
                    "safety_multiplier": 3.5,
                    "max_grid_num": 20,
                    "max_range_pct": 0.05,
                },
                "cooldown": {
                    "atr_period": 14,
                    "calm_window_minutes": 30,
                    "atr_recovery_ratio": 0.8,
                    "amplitude_multiplier": 2.0,
                },
                "selection": {
                    "volume_weight": 0.7,
                    "depth_weight": 0.3,
                    "depth_levels": 5,
                    "symbol_allowlist": ["AAPLUSDT", "MSFTUSDT"],
                    "symbol_blacklist": [],
                },
                "proxy": {"enabled": False},
            },
        )

        result = await _run_binance_check(config)

        assert result["commission_health"]["status"] == "warn"
        assert result["commission_health"]["ok_count"] == 1
        assert result["commission_health"]["warn_count"] == 1
        assert result["commission_health"]["symbols"][1]["symbol"] == "MSFTUSDT"
        assert result["commission_health"]["symbols"][1]["maker"] == 0.0002
        log = Repository(db_path).recent_rows("system_logs", limit=2)[1]
        assert log["level"] == "WARN"
        assert log["module"] == "commission_health"
        assert "MSFTUSDT" in log["detail"]
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_order_smoke_places_and_cleans_testnet_orders(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = OrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_order_smoke(config)

        assert result["smoke_ok"] is True
        assert result["symbol"] == "BTCUSDT"
        assert result["limit_order_id"] == "limit-1"
        assert result["stop_order_id"] == "stop-1"
        assert result["limit_status"] == "NEW"
        assert result["stop_status"] == "NEW"
        assert result["margin_type_ok"] is True
        assert result["leverage_ok"] is True
        assert result["setup_warnings"] == []
        assert result["limit_price"] == 95.0
        assert result["stop_price"] == 90.0
        assert result["qty"] >= 0.5
        assert exchange.leverage_calls == [("BTCUSDT", 10)]
        assert exchange.margin_calls == [("BTCUSDT", "ISOLATED")]
        assert exchange.created_limit_orders[0]["position_side"] == "LONG"
        assert ("BTCUSDT", "limit-1") in exchange.cancelled_orders
        assert ("BTCUSDT", "stop-1") in exchange.cancelled_orders
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_order_smoke_reconciles_failed_create_without_blind_cancel_all(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        monkeypatch.setattr("trader.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = FailedCreateNoOpenOrdersExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        try:
            await _run_binance_order_smoke(config)
        except RuntimeError as exc:
            assert "create status unknown" in str(exc)
        else:
            raise AssertionError("failed create should keep failing after reconciliation")

        assert exchange.cancel_all_called is False
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_order_smoke_recovers_limit_order_created_after_timeout(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        monkeypatch.setattr("trader.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = TimeoutButCreatedOrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_order_smoke(config)

        assert result["smoke_ok"] is True
        assert result["limit_order_id"] == "limit-1"
        assert ("BTCUSDT", "limit-1") in exchange.cancelled_orders
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_order_smoke_polls_for_delayed_order_after_unknown_create(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        monkeypatch.setattr("trader.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = DelayedLookupAfterTimeoutOrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_order_smoke(config)

        assert result["smoke_ok"] is True
        assert result["limit_order_id"] == "limit-1"
        assert exchange.limit_lookup_calls == 3
        assert ("BTCUSDT", "limit-1") in exchange.cancelled_orders
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_order_smoke_tries_next_symbol_after_unrecovered_create_failure(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        monkeypatch.setattr("trader.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = FirstSymbolFailsOrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT", "ETHUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_order_smoke(config)

        assert result["smoke_ok"] is True
        assert result["symbol"] == "ETHUSDT"
        assert result["attempted_symbols"] == ["BTCUSDT", "ETHUSDT"]
        assert exchange.failed_symbols == ["BTCUSDT"]
        assert ("ETHUSDT", "limit-1") in exchange.cancelled_orders
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_price_stream_smoke_returns_first_price_event(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = PriceStreamSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_price_stream_smoke(config, timeout_seconds=1)

        assert result == {
            "stream_ok": True,
            "symbol": "BTCUSDT",
            "event": {"symbol": "BTCUSDT", "price": 101.5, "event_time": "now"},
        }
        assert exchange.price_stream_symbols == ["BTCUSDT"]
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "binance_price_stream_smoke"
        assert log["message"] == "Binance testnet price stream smoke completed."

    asyncio.run(run())


def test_binance_price_stream_smoke_uses_configured_symbol_without_exchange_info(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = NoExchangeInfoPriceStreamExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": [" btcusdt ", "AAPLUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_price_stream_smoke(config, timeout_seconds=1)

        assert result["stream_ok"] is True
        assert result["symbol"] == "BTCUSDT"
        assert exchange.price_stream_symbols == ["BTCUSDT"]

    asyncio.run(run())


def test_binance_market_roundtrip_smoke_opens_and_closes_market_position(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = MarketRoundtripSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_market_roundtrip_smoke(config)

        assert result["market_roundtrip_ok"] is True
        assert result["symbol"] == "BTCUSDT"
        assert result["open_order_id"] == "market-1"
        assert result["close_order_id"] == "market-2"
        assert result["position_side"] == "LONG"
        assert result["position_after_close"] == {"qty": 0.0, "long_qty": 0.0, "short_qty": 0.0}
        assert exchange.market_order_requests == [
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "qty": result["qty"],
                "reduce_only": False,
                "position_side": "LONG",
                "client_id": result["open_client_id"],
            },
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "qty": result["qty"],
                "reduce_only": True,
                "position_side": "LONG",
                "client_id": result["close_client_id"],
            },
        ]
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_direct_order_diagnose_places_raw_limit_order(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = OrderSmokeExchange()
        raw_requests: list[dict[str, Any]] = []

        async def fake_create(**kwargs):
            return exchange

        async def fake_direct_request(config, method, path, params):
            raw_requests.append({"method": method, "path": path, "params": params})
            return {"ok": True, "status_code": 200, "json": {"orderId": "raw-1", **params}, "text": ""}

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._binance_direct_signed_request", fake_direct_request)
        monkeypatch.setattr("trader.ORDER_CREATE_RECOVERY_DELAY_SECONDS", 0)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_direct_order_diagnose(config)

        assert result["direct_order_diagnose_ok"] is True
        assert result["endpoint_order_ok"] is True
        assert result["http_status"] == 200
        assert result["symbol"] == "BTCUSDT"
        assert result["proxy_enabled"] is False
        assert result["position_side"] == "LONG"
        assert raw_requests == [
            {
                "method": "POST",
                "path": "/fapi/v1/order",
                "params": {
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "GTX",
                    "quantity": "0.527",
                    "price": "95",
                    "newClientOrderId": result["client_id"],
                    "positionSide": "LONG",
                },
            }
        ]
        assert ("BTCUSDT", "raw-1") in exchange.cancelled_orders
        assert result["cleanup_errors"] == []
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["level"] == "INFO"
        assert log["module"] == "binance_direct_order_diagnose"

    asyncio.run(run())


def test_binance_direct_order_diagnose_reports_endpoint_failure_without_residual(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = OrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        async def fake_direct_request(config, method, path, params):
            return {
                "ok": False,
                "status_code": 503,
                "json": {
                    "code": -1007,
                    "msg": "Timeout waiting for response from backend server. Send status unknown; execution status unknown.",
                },
                "text": "",
            }

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._binance_direct_signed_request", fake_direct_request)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 10},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_direct_order_diagnose(config)

        assert result["direct_order_diagnose_ok"] is True
        assert result["endpoint_order_ok"] is False
        assert result["http_status"] == 503
        assert result["proxy_enabled"] is False
        assert result["response"]["json"]["code"] == -1007
        assert result["recovered_order_status"] is None
        assert result["cleanup_errors"] == []
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["level"] == "ERROR"
        assert log["module"] == "binance_direct_order_diagnose"

    asyncio.run(run())


def test_binance_signed_query_and_direct_error_redaction() -> None:
    query = _binance_signed_query({"symbol": "BTCUSDT", "timestamp": 123}, "secret")

    assert "symbol=BTCUSDT" in query
    assert "timestamp=123" in query
    assert "signature=" in query
    assert "secret" not in query
    assert (
        _sanitize_direct_transport_error("request failed https://example.test/order?signature=abc123&x=1")
        == "request failed https://example.test/order?signature=<redacted>"
    )


def test_binance_direct_signed_params_use_server_time_and_wide_recv_window() -> None:
    params = _binance_direct_signed_params({"symbol": "BTCUSDT"}, 123456789)

    assert params == {"symbol": "BTCUSDT", "recvWindow": 60000, "timestamp": 123456789}


def test_json_log_detail_serializes_datetimes() -> None:
    detail = _json_log_detail({"event": {"event_time": datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)}})

    assert json.loads(detail)["event"]["event_time"] == "2026-07-07 12:00:00+00:00"


def test_binance_client_create_failure_is_logged(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)

        async def fail_create(**kwargs):
            raise RuntimeError("Cannot connect to host testnet.binance.vision:443 ssl:default [None]")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fail_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": True, "https": "http://127.0.0.1:7897"},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        try:
            await _create_binance_client_for_module(config, "binance_test_order_smoke")
        except RuntimeError as exc:
            assert "Cannot connect to host" in str(exc)
        else:
            raise AssertionError("client creation failure should propagate")

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        detail = json.loads(log["detail"])
        assert log["level"] == "ERROR"
        assert log["module"] == "binance_test_order_smoke"
        assert log["message"] == "Binance testnet client creation failed."
        assert detail["ok"] is False
        assert detail["stage"] == "client_create"
        assert detail["proxy_enabled"] is True
        assert "testnet.binance.vision" in detail["error"]

    asyncio.run(run())


def test_binance_test_order_smoke_validates_signed_order_params(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = OrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"], "symbol_blacklist": []},
            },
        )

        result = await _run_binance_test_order_smoke(config)

        assert result["test_order_ok"] is True
        assert result["symbol"] == "BTCUSDT"
        assert result["limit_response"]["orderId"] == 0
        assert result["market_response"]["orderId"] == 0
        assert result["stop_response"]["orderId"] == 0
        assert result["stop_supported"] is True
        assert result["stop_error"] is None
        assert exchange.test_orders[0]["price"] == 95.0
        assert exchange.test_orders[0]["position_side"] == "LONG"
        assert exchange.test_orders[1]["reduce_only"] is True
        assert exchange.test_orders[2]["stop_price"] == 90.0
        assert exchange.test_orders[2]["close_position"] is True
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "binance_test_order_smoke"
        assert log["message"] == "Binance testnet order/test smoke completed."

    asyncio.run(run())


def test_binance_listen_key_smoke_validates_lifecycle(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = OrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": {"symbol_allowlist": ["BTCUSDT"]}},
        )

        result = await _run_binance_listen_key_smoke(config)

        assert result == {"listen_key_ok": True, "listen_key_length": len("listen-key")}
        assert exchange.listen_key_closed is True
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "binance_listen_key_smoke"
        assert log["message"] == "Binance testnet listenKey smoke completed."

    asyncio.run(run())


def test_binance_signed_write_health_can_run_without_controller(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = OrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {"leverage": 7},
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["BTCUSDT"]},
            },
        )

        result = await _run_binance_signed_write_health(config)

        assert result["signed_write_ok"] is True
        assert result["caller"] == "binance_signed_write_health"
        assert result["symbol"] == "BTCUSDT"
        assert result["leverage"] == 7
        assert exchange.margin_calls == [("BTCUSDT", "ISOLATED")]
        assert exchange.leverage_calls == [("BTCUSDT", 7)]
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "binance_signed_write_health"
        assert log["level"] == "INFO"
        assert "binance_signed_write_health" in log["message"]

    asyncio.run(run())


def test_binance_algo_stop_smoke_places_and_cancels_algo_order(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = OrderSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": {"symbol_allowlist": ["BTCUSDT"]}},
        )

        result = await _run_binance_algo_stop_smoke(config)

        assert result["algo_stop_ok"] is True
        assert result["symbol"] == "BTCUSDT"
        assert result["position_side"] == "SHORT"
        assert result["algo_id"] == 456
        assert result["open_seen"] is True
        assert result["remaining_open"] == 0
        assert exchange.cancelled_algo_ids == [456]
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "binance_algo_stop_smoke"
        assert log["message"] == "Binance testnet algo stop smoke completed."

    asyncio.run(run())


def test_binance_position_smoke_reports_positions_and_open_orders(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = PositionSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": {"symbol_allowlist": ["BTCUSDT"]}},
        )

        result = await _run_binance_position_smoke(config)

        assert result == {
            "position_smoke_ok": True,
            "dual_side_position": True,
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "qty": 0.1,
                    "long_qty": 0.3,
                    "short_qty": 0.2,
                    "position_rows": 2,
                    "ordinary_open": 0,
                    "algo_open": 0,
                }
            ],
        }
        assert exchange.closed is True
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        assert log["module"] == "binance_position_smoke"
        assert log["message"] == "Binance testnet position smoke completed."

    asyncio.run(run())


def test_binance_position_smoke_falls_back_to_configured_testnet_symbols(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = NoExchangeInfoPositionSmokeExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": {"symbol_allowlist": ["BTCUSDT", "AAPLUSDT"]}},
        )

        result = await _run_binance_position_smoke(config)

        assert result["position_smoke_ok"] is True
        assert [item["symbol"] for item in result["symbols"]] == ["BTCUSDT"]

    asyncio.run(run())


def test_binance_safety_sweep_cancels_orders_and_closes_positions(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = SafetySweepExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": {"symbol_allowlist": ["BTCUSDT"]}},
        )
        repo = Repository(db_path)
        window_id = repo.create_window(datetime(2026, 7, 4, 10, 0, tzinfo=timezone.utc))
        session_id = repo.create_session(
            window_id,
            "BTCUSDT",
            "RUNNING",
            200,
            10,
            datetime(2026, 7, 4, 10, 1, tzinfo=timezone.utc),
        )

        result = await _run_binance_safety_sweep(config)

        assert result == {
            "safety_sweep_ok": True,
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "ordinary_before": 1,
                    "algo_before": 1,
                    "ordinary_after": 0,
                    "algo_after": 0,
                    "position_before": {"qty": -0.25, "long_qty": 0.0, "short_qty": 0.0},
                    "position_after": {"qty": 0.0, "long_qty": 0.0, "short_qty": 0.0},
                    "closed_positions": [{"side": "BUY", "qty": 0.25, "position_side": None}],
                }
            ],
            "closed_sessions": [{"session_id": session_id, "symbol": "BTCUSDT", "from_state": "RUNNING"}],
        }
        assert exchange.cancel_all_symbols == ["BTCUSDT"]
        assert exchange.market_orders == [
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "qty": 0.25,
                "reduce_only": True,
                "client_id": "qgsweep-btcusdt-buy",
                "status": "filled",
            }
        ]
        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        detail = json.loads(log["detail"])
        assert log["level"] == "INFO"
        assert log["module"] == "binance_safety_sweep"
        assert log["message"] == "Binance testnet safety sweep completed."
        assert detail["symbols"][0]["symbol"] == "BTCUSDT"
        assert detail["symbols"][0]["closed_positions"] == [{"side": "BUY", "qty": 0.25, "position_side": None}]
        assert detail["closed_sessions"] == [{"session_id": session_id, "symbol": "BTCUSDT", "from_state": "RUNNING"}]
        session = repo.recent_rows("sessions", limit=1)[0]
        state_log = repo.recent_rows("state_logs", limit=1)[0]
        assert session["state"] == "STOPPED"
        assert session["close_reason"] == "binance_safety_sweep"
        assert state_log["trigger"] == "binance_safety_sweep"
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_safety_sweep_falls_back_to_individual_cancels(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = CancelAllFailsSafetySweepExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": {"symbol_allowlist": ["BTCUSDT"]}},
        )

        result = await _run_binance_safety_sweep(config)

        assert result["safety_sweep_ok"] is True
        assert result["symbols"][0]["ordinary_after"] == 0
        assert result["symbols"][0]["algo_after"] == 0
        assert exchange.cancel_all_symbols == ["BTCUSDT"]
        assert exchange.cancelled_orders == [("BTCUSDT", "ordinary-1")]
        assert exchange.cancelled_algo_ids == [456]
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_safety_sweep_skips_cancel_all_when_no_open_orders(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = EmptySafetySweepExchange()

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={"proxy": {"enabled": False}, "selection": {"symbol_allowlist": ["BTCUSDT"]}},
        )

        result = await _run_binance_safety_sweep(config)

        assert result["safety_sweep_ok"] is True
        assert result["symbols"][0]["ordinary_before"] == 0
        assert result["symbols"][0]["algo_before"] == 0
        assert exchange.market_orders == []
        assert exchange.closed is True

    asyncio.run(run())


def test_binance_test_run_sequences_position_loop_sweep_and_post_check(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        config = SimpleNamespace(database_path=db_path, raw={})
        calls = []

        async def fake_position_smoke(config_arg):
            calls.append("position")
            return {"position_smoke_ok": True, "call": len(calls)}

        async def fake_loop(config_arg, max_seconds=None):
            calls.append(("loop", max_seconds))
            return ["loop_timeout"]

        async def fake_safety_sweep(config_arg):
            calls.append("sweep")
            return {"safety_sweep_ok": True}

        monkeypatch.setattr("trader._run_binance_position_smoke", fake_position_smoke)
        monkeypatch.setattr("trader._run_binance_loop", fake_loop)
        monkeypatch.setattr("trader._run_binance_safety_sweep", fake_safety_sweep)

        result = await _run_binance_test_run(config, max_seconds=30)

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        detail = json.loads(log["detail"])

        assert calls == ["position", ("loop", 30), "sweep", "position"]
        assert result["test_run_ok"] is True
        assert result["max_seconds"] == 30
        assert result["loop_result"] == ["loop_timeout"]
        assert result["safety_sweep"] == {"safety_sweep_ok": True}
        assert result["post_position"] == {"position_smoke_ok": True, "call": 4}
        assert log["level"] == "INFO"
        assert log["module"] == "binance_test_run"
        assert detail["test_run_ok"] is True

    asyncio.run(run())


def test_binance_test_run_cleans_up_and_logs_when_loop_fails(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        config = SimpleNamespace(database_path=db_path, raw={})
        calls = []

        async def fake_position_smoke(config_arg):
            calls.append("position")
            return {"position_smoke_ok": True}

        async def fake_loop(config_arg, max_seconds=None):
            calls.append(("loop", max_seconds))
            raise RuntimeError("loop failed")

        async def fake_safety_sweep(config_arg):
            calls.append("sweep")
            return {"safety_sweep_ok": True}

        monkeypatch.setattr("trader._run_binance_position_smoke", fake_position_smoke)
        monkeypatch.setattr("trader._run_binance_loop", fake_loop)
        monkeypatch.setattr("trader._run_binance_safety_sweep", fake_safety_sweep)

        try:
            await _run_binance_test_run(config, max_seconds=15)
        except RuntimeError as exc:
            assert "loop failed" in str(exc)
        else:
            raise AssertionError("loop failure should propagate after cleanup")

        log = Repository(db_path).recent_rows("system_logs", limit=1)[0]
        detail = json.loads(log["detail"])

        assert calls == ["position", ("loop", 15), "sweep", "position"]
        assert log["level"] == "ERROR"
        assert log["module"] == "binance_test_run"
        assert detail["test_run_ok"] is False
        assert detail["loop_error"] == "loop failed"
        assert detail["safety_sweep"] == {"safety_sweep_ok": True}

    asyncio.run(run())


def test_binance_entrypoints_reject_non_perpetual_allowlist_symbol(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        created_exchanges: list[CloseableMockExchange] = []

        async def fake_create(**kwargs):
            exchange = CloseableMockExchange()
            exchange.symbols = [
                {"symbol": "AAPLUSDT", "status": "TRADING", "contractType": "CURRENT_QUARTER"}
            ]
            created_exchanges.append(exchange)
            return exchange

        def fail_build_controller(*args, **kwargs):
            raise AssertionError("controller should not be built for non-perpetual allowlist symbols")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fail_build_controller)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["AAPLUSDT"], "symbol_blacklist": []},
                "timing": {"loop_interval_seconds": 10},
            },
        )

        for runner in (_run_binance_once, _run_binance_check, _run_binance_loop, _run_binance_position_smoke, _run_binance_safety_sweep, _run_binance_market_roundtrip_smoke):
            try:
                await runner(config)
            except RuntimeError as exc:
                assert "未匹配到任何可交易 USDT 合约" in str(exc)
            else:
                raise AssertionError("Binance entrypoint should reject non-perpetual allowlist symbols")

        assert len(created_exchanges) == 6
        assert all(exchange.closed for exchange in created_exchanges)

    asyncio.run(run())


def test_binance_entrypoints_reject_allowlist_symbol_missing_required_exchange_fields(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        created_exchanges: list[CloseableMockExchange] = []

        async def fake_create(**kwargs):
            exchange = CloseableMockExchange()
            exchange.symbols = [
                {"symbol": "AAPLUSDT", "status": "TRADING"},
                {"symbol": "MSFTUSDT", "contractType": "PERPETUAL"},
            ]
            created_exchanges.append(exchange)
            return exchange

        def fail_build_controller(*args, **kwargs):
            raise AssertionError("controller should not be built when allowlist symbols miss required exchange fields")

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        monkeypatch.setattr("trader._build_controller", fail_build_controller)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "proxy": {"enabled": False},
                "selection": {"symbol_allowlist": ["AAPLUSDT", "MSFTUSDT"], "symbol_blacklist": []},
                "timing": {"loop_interval_seconds": 10},
            },
        )

        for runner in (_run_binance_once, _run_binance_check, _run_binance_loop, _run_binance_position_smoke, _run_binance_safety_sweep, _run_binance_market_roundtrip_smoke):
            try:
                await runner(config)
            except RuntimeError as exc:
                assert "未匹配到任何可交易 USDT 合约" in str(exc)
            else:
                raise AssertionError("Binance entrypoint should reject symbols missing required exchange fields")

        assert len(created_exchanges) == 6
        assert all(exchange.closed for exchange in created_exchanges)

    asyncio.run(run())


def test_binance_check_reports_startup_balance_failure(monkeypatch, tmp_path) -> None:
    async def run() -> None:
        db_path = tmp_path / "trader.db"
        init_db(db_path)
        exchange = CloseableMockExchange()
        exchange.balance = 500

        async def fake_create(**kwargs):
            return exchange

        monkeypatch.setattr("trader.BinanceFuturesClient.create", fake_create)
        config = SimpleNamespace(
            binance_api_key="key",
            binance_api_secret="secret",
            binance_testnet=True,
            database_path=db_path,
            raw={
                "trading": {
                    "capital_per_symbol": 200,
                    "leverage": 10,
                    "max_concurrent": 3,
                    "take_profit_usdt": 10,
                    "total_capital_limit": 1000,
                    "stop_buffer_pct": 0.015,
                },
                "timing": {
                    "observe_hours": 1,
                    "observe_kline_interval": "1m",
                    "observe_check_seconds": 60,
                    "force_close_minutes": 120,
                    "loop_interval_seconds": 10,
                    "scheduler_check_minutes": 5,
                },
                "grid": {
                    "range_method": "std",
                    "std_k": 1.8,
                    "quantile_upper": 0.95,
                    "quantile_lower": 0.05,
                    "min_step_pct": 0.0015,
                    "safety_multiplier": 3.5,
                    "max_grid_num": 20,
                    "max_range_pct": 0.05,
                },
                "cooldown": {
                    "atr_period": 14,
                    "calm_window_minutes": 30,
                    "atr_recovery_ratio": 0.8,
                    "amplitude_multiplier": 2.0,
                },
                "selection": {
                    "volume_weight": 0.7,
                    "depth_weight": 0.3,
                    "depth_levels": 5,
                    "symbol_allowlist": ["AAPLUSDT", "MSFTUSDT"],
                    "symbol_blacklist": [],
                },
                "proxy": {"enabled": False},
            },
        )

        result = await _run_binance_check(config)

        assert result["startup_ok"] is False
        assert "并发" in result["reason"]
        assert result["balance"] == 500
        assert result["tradable_symbols"] == 3
        assert result["eligible_symbols"] == 2
        assert result["sample_symbol"] == "AAPLUSDT"

    asyncio.run(run())


def test_build_controller_reads_max_maker_fee_rate(tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    init_db(db_path)
    config = SimpleNamespace(
        database_path=db_path,
        raw={
            "trading": {
                "capital_per_symbol": 200,
                "leverage": 10,
                "max_concurrent": 1,
                "take_profit_usdt": 10,
                "total_capital_limit": 1000,
                "stop_buffer_pct": 0.015,
                "max_maker_fee_rate": 0.0001,
                "maker_fee_check_interval_seconds": 123,
            },
            "timing": {
                "observe_hours": 1,
                "observe_kline_interval": "1m",
                "observe_check_seconds": 60,
                "force_close_minutes": 120,
                "loop_interval_seconds": 10,
                "scheduler_check_minutes": 5,
            },
            "grid": {
                "range_method": "std",
                "std_k": 1.8,
                "quantile_upper": 0.95,
                "quantile_lower": 0.05,
                "min_step_pct": 0.0015,
                "safety_multiplier": 3.5,
                "max_grid_num": 20,
                "max_range_pct": 0.05,
                "rolling_regrid_enabled": True,
                "rolling_regrid_seconds": 3600,
            },
            "cooldown": {
                "atr_period": 14,
                "calm_window_minutes": 17,
                "atr_recovery_ratio": 0.65,
                "amplitude_multiplier": 1.5,
                "min_calm_minutes": 9,
            },
            "selection": {
                "volume_weight": 0.7,
                "depth_weight": 0.3,
                "depth_levels": 5,
                "symbol_blacklist": [],
            },
        },
    )

    controller = _build_controller(MockExchangeClient(), config)

    assert controller.config.max_maker_fee_rate == 0.0001
    assert controller.config.maker_fee_check_interval_seconds == 123
    assert controller.cooldown.config.calm_window_minutes == 17
    assert controller.cooldown.config.atr_recovery_ratio == 0.65
    assert controller.cooldown.config.amplitude_multiplier == 1.5
    assert controller.cooldown.config.min_calm_minutes == 9
    assert controller.grid_config.rolling_regrid_enabled is True
    assert controller.grid_config.rolling_regrid_seconds == 3600


def test_build_controller_applies_testnet_run_now_switches_only_in_testnet(tmp_path) -> None:
    db_path = tmp_path / "trader.db"
    init_db(db_path)
    raw = {
        "trading": {
            "capital_per_symbol": 200,
            "leverage": 10,
            "max_concurrent": 1,
            "take_profit_usdt": 10,
            "total_capital_limit": 1000,
            "stop_buffer_pct": 0.015,
            "max_maker_fee_rate": 0.0002,
        },
        "timing": {
            "observe_hours": 1,
            "observe_kline_interval": "1m",
            "live_observation": True,
            "testnet_force_window": True,
            "testnet_fast_observation": True,
            "observe_check_seconds": 60,
            "force_close_minutes": 120,
            "loop_interval_seconds": 10,
            "scheduler_check_minutes": 5,
        },
        "grid": {
            "range_method": "std",
            "std_k": 1.8,
            "quantile_upper": 0.95,
            "quantile_lower": 0.05,
            "min_step_pct": 0.0015,
            "safety_multiplier": 3.5,
            "max_grid_num": 20,
            "max_range_pct": 0.05,
        },
        "cooldown": {
            "atr_period": 14,
            "calm_window_minutes": 17,
            "atr_recovery_ratio": 0.65,
            "amplitude_multiplier": 1.5,
            "min_calm_minutes": 9,
        },
        "selection": {
            "volume_weight": 0.7,
            "depth_weight": 0.3,
            "depth_levels": 5,
            "symbol_blacklist": [],
        },
    }

    testnet_controller = _build_controller(
        MockExchangeClient(),
        SimpleNamespace(database_path=db_path, binance_testnet=True, raw=raw),
        live_observation=True,
    )
    live_controller = _build_controller(
        MockExchangeClient(),
        SimpleNamespace(database_path=db_path, binance_testnet=False, raw=raw),
        live_observation=True,
    )

    assert testnet_controller.scheduler.is_in_window() is True
    assert testnet_controller.scheduler.should_force_close() is False
    assert testnet_controller.observer.observer_config.live_observation is False
    assert isinstance(live_controller.scheduler, Scheduler)
    assert live_controller.observer.observer_config.live_observation is True
