from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import api as api_module
from api import create_app
from core.config import AppConfig
from core.models import GridOrder, OrderSide, OrderStatus
from db.database import init_db
from db.repository import Repository


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

    sessions = client.get("/api/sessions/active").json()["items"]
    assert len(sessions) == 1
    assert sessions[0]["id"] == session_id
    assert sessions[0]["state_label"] == "网格运行"
    assert sessions[0]["volatility_method_label"] == "Garman-Klass"
    assert sessions[0]["current_volatility"] == 0.0105
    assert sessions[0]["open_order_count"] == 1


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
    repo.create_trade(first_session, "BTCUSDT", "order-1", "BUY", 100, 0.5, 1, None, now)
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
    window_id = repo.create_window(now)
    first_session = repo.create_session(window_id, "BTCUSDT", "RUNNING", 200, 10, now)
    second_session = repo.create_session(window_id, "ETHUSDT", "COOLDOWN", 200, 10, now)
    client = TestClient(create_app(_test_config(db_path)))

    response = client.post(
        "/api/actions/sessions/stop-all",
        json={"confirm": True, "reason": "全部停止", "request_id": "stop-all-1"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "all_sessions_stop"
    assert len(body["result"]["stop_requests"]) == 2
    pending = Repository(db_path).pending_session_stop_requests()
    assert set(pending) == {first_session, second_session}
    assert pending[first_session]["request_id"] == f"stop-all-1:{first_session}"


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
            "max_concurrent": 2,
            "observe_hours": 1.5,
            "min_step_pct": 0.002,
            "max_grid_num": 12,
            "take_profit_usdt": 8,
            "total_capital_limit": 900,
            "max_maker_fee_rate": 0.0001,
        },
    )
    invalid = client.post(
        "/api/strategy-config/draft",
        json={
            "volatility_method": "unknown",
            "max_concurrent": 2,
            "observe_hours": 1.5,
            "min_step_pct": 0.002,
            "max_grid_num": 12,
            "take_profit_usdt": 8,
            "total_capital_limit": 900,
            "max_maker_fee_rate": 0.0001,
        },
    )

    assert initial["current"]["volatility_method"] == "std"
    assert response.status_code == 200
    body = response.json()
    assert body["draft"]["volatility_method"] == "yang_zhang"
    assert body["draft"]["take_profit_usdt"] == 8
    assert {item["key"] for item in body["diff"]} >= {
        "volatility_method",
        "max_concurrent",
        "take_profit_usdt",
        "total_capital_limit",
        "max_maker_fee_rate",
    }
    draft = Repository(db_path).strategy_config_draft()
    assert draft["volatility_method"] == "yang_zhang"
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


def test_console_testnet_run_rejects_non_testnet(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"

    async def fake_test_run(config, seconds):
        return {"test_run_ok": True, "seconds": seconds}

    monkeypatch.setattr(api_module, "_run_testnet_run_action", fake_test_run)
    client = TestClient(create_app(_test_config(db_path, testnet=False)))

    response = client.post(
        "/api/actions/testnet-run",
        json={"confirm": True, "reason": "测试运行", "loop_seconds": 20},
    )

    assert response.status_code == 409


def test_console_symbol_start_grid_runs_single_allowlisted_symbol(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"
    seen = {}

    async def fake_symbol_run(config, symbol, seconds):
        seen["symbol"] = symbol
        seen["seconds"] = seconds
        seen["allowlist"] = list(config.raw["selection"]["symbol_allowlist"])
        return {"test_run_ok": True, "symbol": symbol, "max_seconds": seconds}

    monkeypatch.setattr(api_module, "_run_symbol_testnet_run_action", fake_symbol_run)
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


def test_console_symbol_start_grid_rejects_non_allowlisted_symbol(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "quietgrid.db"

    async def fake_symbol_run(config, symbol, seconds):
        raise AssertionError("non-allowlisted symbol must be rejected before runner")

    monkeypatch.setattr(api_module, "_run_symbol_testnet_run_action", fake_symbol_run)
    client = TestClient(create_app(_test_config(db_path, testnet=True)))

    response = client.post(
        "/api/actions/symbols/notrealusdt/start-grid",
        json={"confirm": True, "reason": "启动非法标的", "loop_seconds": 30},
    )

    assert response.status_code == 422
    assert "allowlist" in response.json()["detail"]


def _test_config(db_path, testnet: bool = True) -> AppConfig:
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
        binance_api_key="",
        binance_api_secret="",
        binance_testnet=testnet,
        binance_testnet_raw="true" if testnet else None,
    )
