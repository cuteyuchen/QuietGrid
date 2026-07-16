from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import api as api_module
from api import create_app
from core.config import AccountConfig, AppConfig
from core.models import GridOrder, OrderSide, OrderStatus
from db.database import init_db
from db.repository import Repository


class FakeConsoleExchange:
    def __init__(self) -> None:
        self.closed = False

    async def get_account_summary(self) -> dict:
        return {
            "asset": "USDT",
            "balance": 1000.0,
            "available_balance": 820.0,
            "margin_balance": 1015.0,
            "initial_margin": 180.0,
            "maintenance_margin": 18.0,
            "unrealized_pnl": 15.0,
            "current_exposure": 640.0,
            "positions": [],
        }

    async def get_symbols(self) -> list[dict]:
        return [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "ETHUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "BCHUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        ]

    async def get_24h_ticker(self, symbol: str) -> dict:
        volumes = {"BTCUSDT": "1000000", "ETHUSDT": "700000", "BCHUSDT": "300000"}
        return {"symbol": symbol, "quoteVolume": volumes[symbol]}

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict:
        books = {
            "BTCUSDT": ("100", "101", "20"),
            "ETHUSDT": ("50", "50.5", "40"),
            "BCHUSDT": ("10", "10.2", "80"),
        }
        bid, ask, qty = books[symbol]
        return {"bids": [[bid, qty]], "asks": [[ask, qty]]}

    async def close(self) -> None:
        self.closed = True


def test_console_api_exposes_summary_and_active_sessions(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    repo.update_session_grid(
        session_id,
        grid_upper=101.0,
        grid_lower=99.0,
        grid_num=4,
        step_pct=0.005,
        baseline_atr=0.2,
        stop_loss_price=98.0,
        volatility_method="garman_klass",
        volatility_value=0.0125,
        volatility_window=60,
    )
    repo.update_session_current_volatility(session_id, 0.0105, 30, now)
    repo.upsert_order(
        session_id,
        GridOrder(
            symbol="BTCUSDT",
            order_id="order-1",
            client_id="cid-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=0.5,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    repo.log_system("INFO", "binance_position_smoke", "Binance testnet position smoke completed.", None, now)

    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    summary = client.get("/api/summary").json()
    assert summary["mode"] == "测试网"
    assert summary["account_id"] == "default"
    assert summary["account_label"] == "默认账户"
    assert summary["active_sessions"] == 1
    assert summary["open_orders"] == 1
    assert summary["latest_system_message"] == "Binance 测试网持仓只读烟测完成。"
    assert summary["risk_level"] == "正常"
    assert summary["balance"] is None
    assert summary["available_balance"] is None
    assert summary["margin_balance"] is None
    assert summary["current_exposure"] is None
    assert summary["account_summary"]["status"] == "unconfigured"

    sessions = client.get("/api/sessions/active").json()["items"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["state_label"] == "网格运行"
    assert sessions[0]["volatility_method_label"] == "Garman-Klass"
    assert sessions[0]["current_volatility"] == 0.0105
    assert sessions[0]["open_order_count"] == 1
    assert sessions[0]["volatility_stage"] == "trading"
    assert sessions[0]["volatility_progress_pct"] == 1.0


def test_session_detail_counts_open_orders_and_trades(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    repo.upsert_order(
        session_id,
        GridOrder(
            symbol="BTCUSDT",
            order_id="open-order",
            client_id="open-client",
            grid_index=1,
            side=OrderSide.BUY,
            price=100.0,
            qty=0.5,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    repo.create_trade(
        session_id=session_id,
        symbol="BTCUSDT",
        order_id="filled-order",
        side="SELL",
        price=101.0,
        qty=0.5,
        grid_index=2,
        grid_pnl=0.5,
        trade_time=now,
        fee=0.01,
    )
    client = TestClient(create_app(_test_config(db_path)))

    response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json()["session"]["open_order_count"] == 1
    assert response.json()["session"]["trade_count"] == 1


def test_grid_rounds_list_every_start_round_and_filter_sessions(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    first_start = datetime(2026, 7, 8, 10, 0, tzinfo=timezone.utc)
    second_start = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    first_window_id = repo.create_window(first_start)
    first_session_id = repo.create_session(first_window_id, "BTCUSDT", "STOPPED", 200, 10, first_start)
    repo.close_session(first_session_id, "window_closed", second_start)
    repo.close_window(first_window_id, second_start)
    second_window_id = repo.create_window(second_start)
    repo.create_session(second_window_id, "ETHUSDT", "RUNNING", 200, 10, second_start)
    repo.create_session(second_window_id, "BCHUSDT", "OBSERVING", 200, 10, second_start)

    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    rounds = client.get("/api/grid-rounds").json()["items"]
    assert [item["window_id"] for item in rounds] == [second_window_id, first_window_id]
    assert rounds[0]["session_count"] == 2
    assert rounds[0]["active_session_count"] == 2
    assert rounds[1]["session_count"] == 1
    assert rounds[1]["active_session_count"] == 0

    first_round_sessions = client.get(
        "/api/sessions/active",
        params={"include_recent": True, "window_id": first_window_id},
    ).json()["items"]
    assert [item["symbol"] for item in first_round_sessions] == ["BTCUSDT"]
    assert first_round_sessions[0]["window_id"] == first_window_id


def test_volatility_stage_payload_covers_observing_calculating_and_trading(tmp_path) -> None:
    config = _test_config(tmp_path / "quietgrid.db")
    observing_row = {"state": "OBSERVING", "open_time": "2099-07-11T12:00:00+00:00"}
    trading_row = {"state": "RUNNING", "open_time": "2026-07-08T12:00:00+00:00"}
    calculating_config = AppConfig(
        raw={**config.raw, "timing": {**config.raw.get("timing", {}), "observe_hours": 0}},
        binance_api_key=config.binance_api_key,
        binance_api_secret=config.binance_api_secret,
        binance_testnet=config.binance_testnet,
        binance_testnet_raw=config.binance_testnet_raw,
        account_id=config.account_id,
        account_label=config.account_label,
        accounts=config.accounts,
    )

    observing = api_module._volatility_stage_payload(observing_row, config)
    calculating = api_module._volatility_stage_payload(observing_row, calculating_config)
    trading = api_module._volatility_stage_payload(trading_row, config)

    assert observing["volatility_stage"] == "observing"
    assert observing["volatility_stage_label"] == "正在观察/波动计算中"
    assert observing["volatility_progress_pct"] == 0.0
    assert observing["volatility_remaining_seconds"] > 0
    assert calculating["volatility_stage"] == "calculating"
    assert calculating["volatility_stage_label"] == "波动计算待完成"
    assert calculating["volatility_progress_pct"] == 1.0
    assert calculating["volatility_remaining_seconds"] == 0
    assert trading["volatility_stage"] == "trading"
    assert trading["volatility_stage_label"] == "计算结束，自动交易已启动"
    assert trading["volatility_progress_pct"] == 1.0
    assert trading["volatility_remaining_seconds"] == 0


def test_console_summary_loads_exchange_account_summary(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    created: list[FakeConsoleExchange] = []

    async def fake_create(**kwargs):
        created.append(FakeConsoleExchange())
        return created[-1]

    monkeypatch.setattr(api_module.BinanceFuturesClient, "create", fake_create)
    client = TestClient(create_app(_test_config(db_path, api_key="key", api_secret="secret")))

    summary = client.get("/api/summary").json()

    assert summary["balance"] == 1000.0
    assert summary["available_balance"] == 820.0
    assert summary["margin_balance"] == 1015.0
    assert summary["initial_margin"] == 180.0
    assert summary["current_exposure"] == 640.0
    assert summary["account_summary"]["status"] == "ok"
    assert summary["account_summary"]["balance"] == 1000.0
    assert summary["account_summary"]["current_exposure"] == 640.0
    assert created and created[0].closed is True


def test_console_api_supports_request_account_switching(tmp_path) -> None:
    main_db = tmp_path / "main.db"
    hedge_db = tmp_path / "hedge.db"
    init_db(main_db)
    init_db(hedge_db)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    Repository(main_db).log_system("INFO", "main", "Binance testnet position smoke completed.", None, now)
    Repository(hedge_db).log_system("INFO", "hedge", "Binance testnet bounded run completed.", None, now)

    config = _test_config(main_db)
    config = AppConfig(
        raw=config.raw,
        binance_api_key="",
        binance_api_secret="",
        binance_testnet=True,
        binance_testnet_raw="true",
        account_id="main",
        account_label="主账户",
        accounts=(
            AccountConfig("main", "主账户", "", "", main_db, binance_testnet=True, binance_testnet_raw="true"),
            AccountConfig("hedge", "对冲账户", "", "", hedge_db, binance_testnet=False, binance_testnet_raw="false"),
        ),
    )
    client = TestClient(create_app(config))

    accounts = client.get("/api/accounts").json()
    default_summary = client.get("/api/summary").json()
    hedge_summary = client.get("/api/summary?account_id=hedge").json()
    missing = client.get("/api/summary?account_id=missing")

    assert accounts["mode"] == "测试网"
    assert accounts["current_account_id"] == "main"
    assert [account["id"] for account in accounts["accounts"]] == ["main", "hedge"]
    assert accounts["accounts"][1]["label"] == "对冲账户"
    assert accounts["accounts"][0]["mode"] == "测试网"
    assert accounts["accounts"][1]["mode"] == "真实盘"
    assert accounts["accounts"][1]["selected"] is False
    assert default_summary["account_id"] == "main"
    assert default_summary["account_label"] == "主账户"
    assert default_summary["database"] == str(main_db)
    assert default_summary["loop_state"] == "持仓只读通过"
    assert hedge_summary["account_id"] == "hedge"
    assert hedge_summary["account_label"] == "对冲账户"
    assert hedge_summary["database"] == str(hedge_db)
    assert hedge_summary["loop_state"] == "有界运行完成"
    assert missing.status_code == 404


def test_console_events_emit_sse_state_for_selected_account(tmp_path) -> None:
    main_db = tmp_path / "main.db"
    hedge_db = tmp_path / "hedge.db"
    init_db(main_db)
    init_db(hedge_db)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    Repository(hedge_db).log_system("INFO", "hedge", "Binance testnet bounded run completed.", None, now)
    config = _test_config(main_db)
    config = AppConfig(
        raw=config.raw,
        binance_api_key="",
        binance_api_secret="",
        binance_testnet=True,
        binance_testnet_raw="true",
        account_id="main",
        account_label="主账户",
        accounts=(
            AccountConfig("main", "主账户", "", "", main_db, binance_testnet=True, binance_testnet_raw="true"),
            AccountConfig("hedge", "对冲账户", "", "", hedge_db, binance_testnet=True, binance_testnet_raw="true"),
        ),
    )
    client = TestClient(create_app(config))

    response = client.get("/api/events?account_id=hedge&once=true")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: runtime" in response.text
    assert "event: market" in response.text
    assert "event: session" in response.text
    assert "event: state" in response.text
    state_block = response.text.split("event: state", 1)[1]
    data_line = next(line for line in state_block.splitlines() if line.startswith("data: "))
    payload = json.loads(data_line.removeprefix("data: "))
    assert payload["account_id"] == "hedge"
    assert payload["mode"] == "测试网"
    assert payload["latest_log_id"] is not None
    assert payload["version"]


def test_console_selection_candidates_fall_back_to_configured_allowlist(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    client = TestClient(create_app(_test_config(db_path)))

    rows = client.get("/api/selection/candidates?limit=2").json()["items"]

    assert [row["symbol"] for row in rows] == ["BTCUSDT", "ETHUSDT"]
    assert rows[0]["rank"] == 1
    assert rows[0]["status"] == "unconfigured"
    assert rows[0]["score"] is None
    assert rows[0]["selected"] is True


def test_console_selection_candidates_use_persisted_snapshot_when_unconfigured(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    Repository(db_path).save_selection_candidates(
        "default",
        "testnet",
        [
            {
                "rank": 1,
                "symbol": "BCHUSDT",
                "score": 0.98,
                "volume_score": 0.9,
                "depth_score": 1.0,
                "volume_24h": 1000.0,
                "depth_usdt": 500.0,
                "bid_price": 237.8,
                "ask_price": 237.9,
                "spread_pct": 0.0004,
                "selected": True,
                "disabled": False,
                "status": "ok",
                "error": "",
            }
        ],
        now,
    )
    client = TestClient(create_app(_test_config(db_path)))

    rows = client.get("/api/selection/candidates?limit=3").json()["items"]

    assert [row["symbol"] for row in rows] == ["BCHUSDT"]
    assert rows[0]["status"] == "cached"
    assert rows[0]["score"] == 0.98
    assert rows[0]["snapshot_at"] == now.isoformat()


def test_console_selection_candidates_use_live_selector(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    created: list[FakeConsoleExchange] = []

    async def fake_create(**kwargs):
        created.append(FakeConsoleExchange())
        return created[-1]

    monkeypatch.setattr(api_module.BinanceFuturesClient, "create", fake_create)
    client = TestClient(create_app(_test_config(db_path, api_key="key", api_secret="secret")))

    rows = client.get("/api/selection/candidates?limit=3").json()["items"]

    assert [row["symbol"] for row in rows] == ["BTCUSDT", "ETHUSDT", "BCHUSDT"]
    assert rows[0]["rank"] == 1
    assert rows[0]["score"] is not None
    assert rows[0]["volume_24h"] == 1000000
    assert rows[0]["bid_price"] == 100
    assert rows[0]["ask_price"] == 101
    assert rows[0]["spread_pct"] > 0
    assert rows[0]["selected"] is True
    assert rows[1]["selected"] is False
    assert rows[0]["status"] == "ok"
    assert created and created[0].closed is True
    persisted = Repository(db_path).latest_selection_candidates("default", "testnet", limit=3)
    assert [row["symbol"] for row in persisted] == ["BTCUSDT", "ETHUSDT", "BCHUSDT"]
    assert persisted[0]["score"] == rows[0]["score"]


def test_console_api_filters_orders_and_trades_by_session(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    first_session = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    second_session = repo.create_session(window_id, "ETHUSDT", "RUNNING", 200, 10, now)
    repo.upsert_order(
        first_session,
        GridOrder(
            symbol="BTCUSDT",
            order_id="order-1",
            client_id="cid-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=0.5,
            status=OrderStatus.FILLED,
            created_at=now,
            filled_at=now,
            fill_price=100,
        ),
    )
    repo.upsert_order(
        second_session,
        GridOrder(
            symbol="ETHUSDT",
            order_id="order-2",
            client_id="cid-2",
            grid_index=1,
            side=OrderSide.SELL,
            price=200,
            qty=0.25,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    repo.update_session_pnl(first_session, 1.25)
    repo.create_trade(first_session, "BTCUSDT", "order-1", "BUY", 100, 0.5, 1, 1.5, now, fee=0.1, funding_fee=-0.05)
    repo.create_trade(second_session, "ETHUSDT", "order-2", "SELL", 200, 0.25, 1, 1.5, now)

    client = TestClient(create_app(_test_config(db_path)))

    orders = client.get(f"/api/orders?session_id={first_session}").json()["items"]
    trades = client.get(f"/api/trades?session_id={first_session}").json()["items"]
    detail = client.get(f"/api/sessions/{first_session}").json()

    assert [row["symbol"] for row in orders] == ["BTCUSDT"]
    assert orders[0]["status_label"] == "已成交"
    assert [row["symbol"] for row in trades] == ["BTCUSDT"]
    assert detail["session"]["symbol"] == "BTCUSDT"
    assert len(detail["orders"]) == 1
    assert len(detail["trades"]) == 1
    assert detail["trades"][0]["funding_fee"] == -0.05
    assert detail["performance"]["gross_grid_pnl"] == 1.5
    assert detail["performance"]["trading_fees"] == 0.1
    assert detail["performance"]["funding_fee"] == -0.05
    assert detail["performance"]["realized_pnl"] == 1.25
    assert detail["performance"]["initial_margin"] == 200
    assert detail["performance"]["current_margin"] == 201.25
    assert detail["performance"]["margin_change"] == 1.25
    assert len(detail["performance"]["pnl_curve"]) == 1
    assert client.get("/api/sessions/999").status_code == 404


def test_console_api_exposes_testnet_verification_rows(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    repo.log_system(
        "INFO",
        "binance_safety_sweep",
        "Binance testnet safety sweep completed.",
        json.dumps(
            {
                "safety_sweep_ok": True,
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "ordinary_after": 0,
                        "algo_after": 0,
                        "position_after": {"qty": 0.0, "long_qty": 0.0, "short_qty": 0.0},
                    }
                ],
            }
        ),
        now,
    )

    client = TestClient(create_app(_test_config(db_path)))

    rows = client.get("/api/verification/testnet").json()["items"]
    safety_sweep = next(row for row in rows if row["module"] == "binance_safety_sweep")

    assert safety_sweep["name"] == "安全清扫"
    assert safety_sweep["status"] == "passed"
    assert safety_sweep["status_label"] == "通过"
    assert "清扫标的: 1" in safety_sweep["detail"]


def test_console_api_exposes_environment_verification_rows(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    client = TestClient(create_app(_test_config(db_path)))

    rows = client.get("/api/verification/environment").json()["items"]
    assert [row["module"] for row in rows] == [
        "environment_credentials",
        "environment_connectivity",
        "environment_funds",
    ]
    assert rows[0]["status"] == "not_run"
    assert "未配置" in rows[0]["detail"]


def test_console_readonly_environment_verification_uses_only_read_interfaces(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    exchange = FakeConsoleExchange()

    async def fake_create(**kwargs):
        return exchange

    monkeypatch.setattr(api_module.BinanceFuturesClient, "create", fake_create)
    client = TestClient(create_app(_test_config(db_path, api_key="key", api_secret="secret")))

    response = client.post(
        "/api/actions/environment/verify-readonly",
        json={"confirm": True, "reason": "只读验证", "request_id": "verify-1"},
    )

    assert response.status_code == 200
    rows = response.json()["result"]["items"]
    assert [row["status"] for row in rows] == ["passed", "passed", "passed"]
    assert "可用 820.00" in rows[2]["detail"]
    assert exchange.closed is True
    persisted = client.get("/api/verification/environment").json()["items"]
    assert persisted == rows


def test_console_action_requires_confirmation(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    client = TestClient(create_app(_test_config(db_path)))

    response = client.post("/api/actions/pause-new-entries", json={"confirm": False, "reason": "test"})

    assert response.status_code == 400
    assert "confirm=true" in response.json()["detail"]


def test_console_pause_and_resume_new_entries_persist_state(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    client = TestClient(create_app(_test_config(db_path)))

    pause_response = client.post(
        "/api/actions/pause-new-entries",
        json={"confirm": True, "reason": "测试暂停", "request_id": "pause-1"},
    )
    resume_response = client.post(
        "/api/actions/resume-new-entries",
        json={"confirm": True, "reason": "测试恢复", "request_id": "resume-1"},
    )

    assert pause_response.status_code == 200
    assert pause_response.json()["control_state"]["new_entries_paused"] is True
    assert resume_response.status_code == 200
    assert resume_response.json()["control_state"]["new_entries_paused"] is False
    control_state = client.get("/api/control-state").json()
    assert control_state["new_entries_paused"] is False
    logs = Repository(db_path).recent_rows("system_logs", limit=4)
    assert {row["module"] for row in logs} == {"console_action"}
    assert any("resume_new_entries" in row["detail"] for row in logs)
    assert any("pause_new_entries" in row["detail"] for row in logs)


def test_console_grid_round_start_queues_one_round_request(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    Repository(db_path).register_runtime("runtime-1", datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc))
    client = TestClient(create_app(_test_config(db_path)))

    first = client.post(
        "/api/actions/grid-rounds/start",
        json={"confirm": True, "reason": "启动本轮", "request_id": "round-1"},
    )
    second = client.post(
        "/api/actions/grid-rounds/start",
        json={"confirm": True, "reason": "重复启动", "request_id": "round-2"},
    )

    assert first.status_code == 200
    assert first.json()["action"] == "grid_round_start"
    assert first.json()["result"]["status"] == "requested"
    assert second.status_code == 409
    state = client.get("/api/control-state").json()
    assert state["round_start_request"]["request_id"] == "round-1"
    assert state["round_start_available"] is False


def test_grid_round_can_restart_after_previous_round_stops_in_same_runtime(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    client = TestClient(create_app(_test_config(db_path)))

    assert client.post(
        "/api/actions/grid-rounds/start",
        json={"confirm": True, "reason": "无 trader", "request_id": "missing-runtime"},
    ).status_code == 409

    repo.register_runtime("runtime-a", now)
    first = client.post(
        "/api/actions/grid-rounds/start",
        json={"confirm": True, "reason": "启动", "request_id": "runtime-a-start"},
    )
    assert first.status_code == 200
    window_id = repo.claim_round_window("runtime-a", now)
    assert repo.runtime_state()["round_start_available"] is False
    assert client.post(
        "/api/actions/grid-rounds/start",
        json={"confirm": True, "reason": "重复", "request_id": "runtime-a-repeat"},
    ).status_code == 409

    repo.set_round_runtime_state(window_id, "STOPPED", now + timedelta(seconds=30))
    assert repo.runtime_state()["round_start_available"] is True
    next_round = client.post(
        "/api/actions/grid-rounds/start",
        json={"confirm": True, "reason": "启动新一轮", "request_id": "runtime-a-next"},
    )
    assert next_round.status_code == 200
    next_window_id = repo.claim_round_window("runtime-a", now + timedelta(minutes=1))
    assert next_window_id > window_id

    repo.register_runtime("runtime-b", now + timedelta(minutes=2))
    state = client.get("/api/control-state").json()
    assert state["runtime_id"] == "runtime-b"
    assert state["round_start_available"] is True
    assert window_id > 0


def test_grid_round_candidates_endpoint_returns_scanning_symbols_without_sessions(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now, runtime_id="runtime-candidate", status="SCANNING")
    repo.upsert_round_candidate(
        window_id,
        "BTCUSDT",
        now,
        liquidity_rank=1,
        price=62000.0,
        volatility_method="std",
        volatility_value=0.002,
        volatility_window=60,
        threshold_met=False,
        stage="below_threshold",
        calculated_at=now.isoformat(),
    )
    client = TestClient(create_app(_test_config(db_path)))

    response = client.get(f"/api/grid-rounds/{window_id}/candidates")

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["symbol"] == "BTCUSDT"
    assert item["session_id"] is None
    assert item["stage"] == "below_threshold"


def test_console_session_pause_and_resume_requests_follow_session_state(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    repo.register_runtime("runtime-1", now)
    repo.request_round_start("启动", "round-1", now)
    window_id = repo.claim_round_window("runtime-1", now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    client = TestClient(create_app(_test_config(db_path)))

    pause = client.post(
        f"/api/actions/sessions/{session_id}/pause",
        json={"confirm": True, "reason": "暂时观察", "request_id": "pause-session-1"},
    )

    assert pause.status_code == 200
    assert pause.json()["result"]["action"] == "pause"
    assert pause.json()["control_state"]["session_control_requests"][0]["status"] == "requested"
    resume = client.post(
        f"/api/actions/sessions/{session_id}/resume",
        json={"confirm": True, "reason": "恢复", "request_id": "resume-session-1"},
    )
    assert resume.status_code == 409


def test_console_symbol_disable_and_enable_next_entry(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    client = TestClient(create_app(_test_config(db_path)))

    disable_response = client.post(
        "/api/actions/symbols/btcusdt/disable-next-entry",
        json={"confirm": True, "reason": "临时屏蔽", "request_id": "disable-1"},
    )
    disabled_session = client.get("/api/sessions/active").json()["items"][0]
    enable_response = client.post(
        "/api/actions/symbols/BTCUSDT/enable-next-entry",
        json={"confirm": True, "reason": "恢复", "request_id": "enable-1"},
    )

    assert disable_response.status_code == 200
    assert disable_response.json()["control_state"]["disabled_symbols"] == ["BTCUSDT"]
    assert disabled_session["id"] == session_id
    assert disabled_session["next_entry_disabled"] is True
    assert enable_response.status_code == 200
    assert enable_response.json()["control_state"]["disabled_symbols"] == []


def test_console_session_stop_records_request_and_snapshot(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    repo.upsert_order(
        session_id,
        GridOrder(
            symbol="BTCUSDT",
            order_id="order-1",
            client_id="cid-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=0.5,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    client = TestClient(create_app(_test_config(db_path)))

    response = client.post(
        f"/api/actions/sessions/{session_id}/stop",
        json={"confirm": True, "reason": "网页手动停止", "request_id": "stop-1"},
    )
    session = client.get("/api/sessions/active").json()["items"][0]

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "session_stop"
    assert body["result"]["before"]["open_orders"] == 1
    assert body["result"]["after"]["open_orders"] == 1
    assert body["result"]["position_confirmation"]["status"] == "queued"
    assert Repository(db_path).pending_session_stop_requests()[session_id]["request_id"] == "stop-1"
    assert session["stop_requested"] is True
    assert session["stop_request_status"] == "requested"


def test_console_session_manual_close_records_request_and_snapshot(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    repo.upsert_order(
        session_id,
        GridOrder(
            symbol="BTCUSDT",
            order_id="order-1",
            client_id="cid-1",
            grid_index=1,
            side=OrderSide.BUY,
            price=100,
            qty=0.5,
            status=OrderStatus.OPEN,
            created_at=now,
        ),
    )
    client = TestClient(create_app(_test_config(db_path)))

    response = client.post(
        f"/api/actions/sessions/{session_id}/manual-close",
        json={"confirm": True, "reason": "网页手动平仓", "request_id": "close-1"},
    )
    session = client.get("/api/sessions/active").json()["items"][0]

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "session_manual_close"
    assert body["label"] == "手动平仓"
    assert body["result"]["before"]["open_orders"] == 1
    assert body["result"]["position_confirmation"]["status"] == "queued"
    pending = Repository(db_path).pending_session_stop_requests()[session_id]
    assert pending["request_id"] == "close-1"
    assert pending["request_type"] == "manual_close"
    assert session["stop_requested"] is True
    assert session["stop_request_status"] == "requested"
    assert session["stop_request_type"] == "manual_close"


def test_console_stop_all_sessions_records_requests(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    repo.register_runtime("runtime-stop", now)
    repo.request_round_start("启动", "round-stop", now)
    window_id = repo.claim_round_window("runtime-stop", now)
    first_session = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    second_session = repo.create_session(window_id, "ETHUSDT", "COOLDOWN", 200, 10, now)
    client = TestClient(create_app(_test_config(db_path)))

    response = client.post(
        "/api/actions/sessions/stop-all",
        json={"confirm": True, "reason": "全部停止", "request_id": "stop-all-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "round_stop"
    assert body["result"]["round_stop_request"]["window_id"] == window_id
    assert {item["session_id"] for item in body["result"]["active_sessions"]} == {first_session, second_session}
    assert Repository(db_path).round_stop_request()["request_id"] == "stop-all-1"


def test_console_session_stop_rejects_stopped_session(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "STOPPED", 200, 10, now)
    repo.close_session(session_id, "done", now)
    client = TestClient(create_app(_test_config(db_path)))

    response = client.post(
        f"/api/actions/sessions/{session_id}/stop",
        json={"confirm": True, "reason": "重复停止"},
    )

    assert response.status_code == 409


def test_console_strategy_config_draft_api_persists_diff(tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    client = TestClient(create_app(_test_config(db_path)))

    initial = client.get("/api/strategy-config").json()
    response = client.post(
        "/api/strategy-config/draft",
        json={
            "volatility_method": "yang_zhang",
            "leverage": 7,
            "capital_per_symbol": 150,
            "max_concurrent": 2,
            "observe_hours": 1.5,
            "observe_kline_interval": "5m",
            "min_step_pct": 0.002,
            "max_grid_num": 12,
            "stop_buffer_pct": 0.02,
            "safety_multiplier": 4.2,
            "take_profit_usdt": 8,
            "total_capital_limit": 900,
            "max_maker_fee_rate": 0.0001,
        },
    )
    invalid = client.post(
        "/api/strategy-config/draft",
        json={
            "volatility_method": "unknown",
            "leverage": 7,
            "capital_per_symbol": 150,
            "max_concurrent": 2,
            "observe_hours": 1.5,
            "observe_kline_interval": "5m",
            "min_step_pct": 0.002,
            "max_grid_num": 12,
            "stop_buffer_pct": 0.02,
            "safety_multiplier": 4.2,
            "take_profit_usdt": 8,
            "total_capital_limit": 900,
            "max_maker_fee_rate": 0.0001,
        },
    )

    assert initial["current"]["volatility_method"] == "std"
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["volatility_method"] == "yang_zhang"
    assert body["draft"]["leverage"] == 7
    assert body["draft"]["capital_per_symbol"] == 150
    assert body["draft"]["observe_kline_interval"] == "5m"
    assert body["draft"]["stop_buffer_pct"] == 0.02
    assert body["draft"]["safety_multiplier"] == 4.2
    assert body["draft"]["take_profit_usdt"] == 8
    assert {item["key"] for item in body["diff"]} >= {
        "volatility_method",
        "leverage",
        "capital_per_symbol",
        "max_concurrent",
        "observe_kline_interval",
        "stop_buffer_pct",
        "safety_multiplier",
        "take_profit_usdt",
        "total_capital_limit",
        "max_maker_fee_rate",
    }
    draft = Repository(db_path).strategy_config_draft()
    assert draft["volatility_method"] == "yang_zhang"
    assert draft["leverage"] == 7
    assert draft["capital_per_symbol"] == 150
    assert draft["observe_kline_interval"] == "5m"
    assert draft["take_profit_usdt"] == 8
    assert invalid.status_code == 422


def test_console_safety_sweep_action_runs_with_audit_log(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"

    async def fake_safety_sweep(config):
        return {"safety_sweep_ok": True, "symbols": []}

    monkeypatch.setattr(api_module, "_run_safety_sweep_action", fake_safety_sweep)
    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    response = client.post(
        "/api/actions/safety-sweep",
        json={"confirm": True, "reason": "测试清扫", "request_id": "sweep-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["action"] == "safety_sweep"
    assert body["result"]["safety_sweep_ok"] is True
    logs = Repository(db_path).recent_rows("system_logs", limit=2)
    assert [row["message"] for row in logs] == ["Console action completed.", "Console action requested."]
    assert all(row["module"] == "console_action" for row in logs)


def test_trader_process_status_uses_systemd_when_available(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"

    def fake_system() -> str:
        return "Linux"

    def fake_systemctl(args):
        assert args == ["is-active", "quietgrid-trader"]
        return subprocess.CompletedProcess(["systemctl", *args], 0, "active\n", "")

    monkeypatch.setattr(api_module.platform, "system", fake_system)
    monkeypatch.setattr(api_module, "_run_systemctl", fake_systemctl)
    client = TestClient(create_app(_test_config(db_path)))

    body = client.get("/api/process/trader").json()

    assert body == {
        "available": True,
        "mode": "systemd",
        "service": "quietgrid-trader",
        "state": "running",
        "detail": "active",
    }


def test_trader_process_stop_action_runs_systemctl_with_audit(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    calls = []
    state = {"active": True}

    def fake_system() -> str:
        return "Linux"

    def fake_systemctl(args):
        calls.append(args)
        if args[0] == "is-active":
            stdout = "active\n" if state["active"] else "inactive\n"
            code = 0 if state["active"] else 3
            return subprocess.CompletedProcess(["systemctl", *args], code, stdout, "")
        if args == ["stop", "quietgrid-trader"]:
            state["active"] = False
            return subprocess.CompletedProcess(["systemctl", *args], 0, "", "")
        raise AssertionError(f"unexpected systemctl args: {args}")

    monkeypatch.setattr(api_module.platform, "system", fake_system)
    monkeypatch.setattr(api_module, "_run_systemctl", fake_systemctl)
    client = TestClient(create_app(_test_config(db_path)))

    response = client.post(
        "/api/actions/trader-loop/stop",
        json={"confirm": True, "reason": "运维停止", "request_id": "loop-stop-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "trader_loop_stop"
    assert body["result"]["after"]["state"] == "stopped"
    assert calls == [
        ["is-active", "quietgrid-trader"],
        ["stop", "quietgrid-trader"],
        ["is-active", "quietgrid-trader"],
    ]
    logs = Repository(db_path).recent_rows("system_logs", limit=2)
    assert [row["message"] for row in logs] == ["Console action completed.", "Console action requested."]


def test_trader_process_command_mode_runs_configured_stop_command_with_audit(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    config = _test_config(db_path)
    config.raw["process_control"] = {
        "mode": "command",
        "service": "quietgrid-trader",
        "status_command": ["qgctl", "status"],
        "stop_command": ["qgctl", "stop"],
        "restart_command": ["qgctl", "restart"],
    }
    calls = []
    state = {"running": True}

    def fake_process_command(command, timeout_seconds=15):
        calls.append(command)
        if command == ["qgctl", "status"]:
            stdout = "running" if state["running"] else "stopped"
            return subprocess.CompletedProcess(command, 0 if state["running"] else 3, stdout, "")
        if command == ["qgctl", "stop"]:
            state["running"] = False
            return subprocess.CompletedProcess(command, 0, "stopped", "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(api_module, "_run_process_command", fake_process_command)
    client = TestClient(create_app(config))

    status = client.get("/api/process/trader").json()
    response = client.post(
        "/api/actions/trader-loop/stop",
        json={"confirm": True, "reason": "命令模式停止", "request_id": "cmd-stop-1"},
    )

    assert status["available"] is True
    assert status["mode"] == "command"
    assert status["state"] == "running"
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "trader_loop_stop"
    assert body["result"]["before"]["mode"] == "command"
    assert body["result"]["after"]["state"] == "stopped"
    assert calls == [["qgctl", "status"], ["qgctl", "status"], ["qgctl", "stop"], ["qgctl", "status"]]
    logs = Repository(db_path).recent_rows("system_logs", limit=2)
    assert [row["message"] for row in logs] == ["Console action completed.", "Console action requested."]


def test_console_bounded_run_uses_current_environment(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    seen = {}

    async def fake_test_run(config, seconds):
        seen["testnet"] = config.binance_testnet
        seen["seconds"] = seconds
        return {"test_run_ok": True, "seconds": seconds}

    monkeypatch.setattr(api_module, "_run_bounded_run_action", fake_test_run)
    client = TestClient(create_app(_test_config(db_path, testnet=False)))

    response = client.post(
        "/api/actions/bounded-run",
        json={"confirm": True, "reason": "测试运行", "loop_seconds": 20},
    )

    assert response.status_code == 200
    assert response.json()["label"] == "一键有界运行"
    assert response.json()["action"] == "bounded_run"
    assert response.json()["result"] == {"test_run_ok": True, "seconds": 20.0}
    assert seen == {"testnet": False, "seconds": 20.0}


def test_console_bounded_run_defaults_to_short_runtime(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    seen = {}

    async def fake_test_run(config, seconds):
        seen["seconds"] = seconds
        return {"test_run_ok": True, "seconds": seconds}

    monkeypatch.setattr(api_module, "_run_bounded_run_action", fake_test_run)
    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    response = client.post(
        "/api/actions/bounded-run",
        json={"confirm": True, "reason": "默认时长测试"},
    )

    assert response.status_code == 200
    assert response.json()["result"] == {"test_run_ok": True, "seconds": 60.0}
    assert seen == {"seconds": 60.0}


def test_console_symbol_start_grid_runs_single_allowlisted_symbol(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    seen = {}

    async def fake_symbol_run(config, symbol, seconds):
        seen["symbol"] = symbol
        seen["seconds"] = seconds
        seen["allowlist"] = list(config.raw["selection"]["symbol_allowlist"])
        return {"test_run_ok": True, "symbol": symbol, "max_seconds": seconds}

    monkeypatch.setattr(api_module, "_run_symbol_bounded_run_action", fake_symbol_run)
    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    response = client.post(
        "/api/actions/symbols/btcusdt/start-grid",
        json={"confirm": True, "reason": "启动 BTC", "request_id": "start-btc", "loop_seconds": 30},
    )
    control_state = client.get("/api/control-state").json()

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "symbol_start_grid"
    assert body["result"]["symbol"] == "BTCUSDT"
    assert seen == {"symbol": "BTCUSDT", "seconds": 30.0, "allowlist": ["BTCUSDT", "ETHUSDT", "BCHUSDT"]}
    assert control_state["startable_symbols"] == ["BTCUSDT", "ETHUSDT", "BCHUSDT"]


def test_console_symbol_start_grid_defaults_to_short_runtime(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    seen = {}

    async def fake_symbol_run(config, symbol, seconds):
        seen["symbol"] = symbol
        seen["seconds"] = seconds
        return {"test_run_ok": True, "symbol": symbol, "max_seconds": seconds}

    monkeypatch.setattr(api_module, "_run_symbol_bounded_run_action", fake_symbol_run)
    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    response = client.post(
        "/api/actions/symbols/btcusdt/start-grid",
        json={"confirm": True, "reason": "默认时长启动 BTC"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["max_seconds"] == 60.0
    assert seen == {"symbol": "BTCUSDT", "seconds": 60.0}


def test_console_symbol_start_grid_rejects_non_allowlisted_symbol(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"

    async def fake_symbol_run(config, symbol, seconds):
        raise AssertionError("non-allowlisted symbol must be rejected before runner")

    monkeypatch.setattr(api_module, "_run_symbol_bounded_run_action", fake_symbol_run)
    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    response = client.post(
        "/api/actions/symbols/notrealusdt/start-grid",
        json={"confirm": True, "reason": "启动非法标的", "loop_seconds": 30},
    )

    assert response.status_code == 422
    assert "allowlist" in response.json()["detail"]


def _test_config(db_path, testnet: bool = True, api_key: str = "", api_secret: str = "") -> AppConfig:
    return AppConfig(
        raw={
            "database": {"path": str(db_path)},
            "web": {"address": "127.0.0.1", "port": 8080},
            "api": {"address": "127.0.0.1", "port": 8000},
            "selection": {
                "symbol_allowlist": ["BTCUSDT", "ETHUSDT", "BCHUSDT"],
                "symbol_blacklist": [],
            },
        },
        binance_api_key=api_key,
        binance_api_secret=api_secret,
        binance_testnet=testnet,
        binance_testnet_raw="true" if testnet else None,
    )


def test_v2_health_dashboard_and_active_config(tmp_path) -> None:
    db_path = tmp_path / "quietgrid-v2.db"
    config = _test_config(db_path)
    config.raw.update(
        {
            "features": {"regime_v2": True, "inventory_manager": True},
            "risk": {"max_weekend_loss_pct": 0.015},
            "regime": {"enter_threshold": 75},
            "grid": {"min_grid_num": 6},
        }
    )
    client = TestClient(create_app(config))

    health = client.get("/api/v2/health")
    dashboard = client.get("/api/v2/dashboard")
    active_config = client.get("/api/v2/config/active")

    assert health.status_code == 200
    assert health.json()["api_version"] == "v2"
    assert dashboard.status_code == 200
    assert dashboard.json()["global_risk_level"] == "LOW"
    assert dashboard.json()["data_health"] == "WAITING"
    assert active_config.json()["sections"]["features"]["regime_v2"] is True
    assert "database" not in active_config.json()["sections"]


def test_v2_control_command_is_idempotent_and_audited(tmp_path) -> None:
    db_path = tmp_path / "quietgrid-v2-command.db"
    client = TestClient(create_app(_test_config(db_path)))
    payload = {
        "reason": "暂停新开仓以检查风险",
        "confirmation": "PAUSE",
        "idempotency_key": "pause-command-0001",
        "requested_by": "tester",
    }

    first = client.post("/api/v2/commands/pause", json=payload)
    duplicate = client.post("/api/v2/commands/pause", json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "PENDING"
    assert duplicate.json()["command_id"] == first.json()["command_id"]
    repo = Repository(db_path)
    assert len(repo.recent_rows("control_commands")) == 1
    assert len(repo.recent_rows("audit_logs")) == 2


def test_v2_close_session_requires_symbol_confirmation(tmp_path) -> None:
    db_path = tmp_path / "quietgrid-v2-close.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime.now(timezone.utc)
    window_id = repo.create_window(now)
    session_id = repo.create_session(window_id, "BTCUSDT", "RUNNING", 100, 1, now)
    client = TestClient(create_app(_test_config(db_path)))

    rejected = client.post(
        "/api/v2/commands/close-session",
        json={
            "reason": "操作员主动降低风险",
            "confirmation": "CLOSE-WRONG",
            "idempotency_key": "close-command-0001",
            "session_id": session_id,
        },
    )
    accepted = client.post(
        "/api/v2/commands/close-session",
        json={
            "reason": "操作员主动降低风险",
            "confirmation": "CLOSE-BTCUSDT",
            "idempotency_key": "close-command-0002",
            "session_id": session_id,
        },
    )

    assert rejected.status_code == 422
    assert accepted.status_code == 200
    command = client.get(f"/api/v2/commands/{accepted.json()['command_id']}")
    assert command.json()["target_id"] == str(session_id)


def test_v2_backtest_center_lists_datasets_runs_and_reports(tmp_path) -> None:
    db_path = tmp_path / "quietgrid-v2-backtest.db"
    dataset_root = tmp_path / "datasets"
    report_root = tmp_path / "reports"
    dataset_root.mkdir()
    rows = ["timestamp,open,high,low,close"]
    for index in range(90):
        close = 100.0 + ((index % 12) - 6) * 0.12
        rows.append(
            f"2026-01-01T00:{index:02d}:00Z,{close:.4f},{close + 0.6:.4f},"
            f"{close - 0.6:.4f},{close:.4f}"
        )
    (dataset_root / "btc-1m.csv").write_text("\n".join(rows), encoding="utf-8")
    config = _test_config(db_path)
    config.raw["backtest"] = {
        "dataset_dir": str(dataset_root),
        "report_dir": str(report_root),
    }
    config.raw["trading"] = {
        "capital_per_symbol": 200,
        "leverage": 1,
        "max_maker_fee_rate": 0,
        "stop_buffer_pct": 0.015,
    }
    client = TestClient(create_app(config))

    datasets = client.get("/api/v2/backtests/datasets")
    created = client.post(
        "/api/v2/backtests",
        json={
            "dataset": "btc-1m.csv",
            "symbol": "BTCUSDT",
            "observe_rows": 30,
            "capital": 200,
            "leverage": 1,
            "maker_fee_rate": 0,
            "fill_model": "L0_CONSERVATIVE",
        },
    )

    assert datasets.status_code == 200
    assert datasets.json()["items"][0]["relative_path"] == "btc-1m.csv"
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "COMPLETED"
    assert body["symbol"] == "BTCUSDT"
    assert body["metrics"]["profit_factor"] >= 0
    assert body["report"]["equity_curve"]
    run_id = body["run_id"]
    listed = client.get("/api/v2/backtests").json()["items"]
    detail = client.get(f"/api/v2/backtests/{run_id}")
    assert listed[0]["run_id"] == run_id
    assert detail.status_code == 200
    assert detail.json()["report"]["summary"]["symbol"] == "BTCUSDT"


def test_v2_backtest_rejects_dataset_path_traversal(tmp_path) -> None:
    db_path = tmp_path / "quietgrid-v2-backtest-path.db"
    dataset_root = tmp_path / "datasets"
    dataset_root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("high,low,close\n1,1,1\n", encoding="utf-8")
    config = _test_config(db_path)
    config.raw["backtest"] = {"dataset_dir": str(dataset_root)}
    client = TestClient(create_app(config))

    response = client.post(
        "/api/v2/backtests",
        json={
            "dataset": "../outside.csv",
            "symbol": "BTCUSDT",
            "observe_rows": 30,
            "capital": 200,
            "leverage": 1,
            "maker_fee_rate": 0,
            "fill_model": "L0_CONSERVATIVE",
        },
    )

    assert response.status_code == 400
