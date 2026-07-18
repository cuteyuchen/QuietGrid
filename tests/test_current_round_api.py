from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from api import create_app
from core.config import AppConfig
from db.database import init_db
from db.repository import Repository
from tests.test_api import _test_config


def test_current_round_and_auto_trading_endpoints(tmp_path: Path) -> None:
    db_path = tmp_path / "api.db"
    config = _test_config(db_path)
    config.raw["process_control"] = {"mode": "local", "timeout_seconds": 5}
    config.raw["runtime"] = {
        "heartbeat_interval_seconds": 5,
        "heartbeat_stale_seconds": 20,
        "heartbeat_offline_seconds": 60,
    }
    client = TestClient(create_app(config))

    body = client.get("/api/v2/current-round").json()
    assert "trader" in body
    assert "auto_trading" in body
    assert "window" in body
    assert "round" in body

    stop = client.post(
        "/api/actions/auto-trading/stop",
        json={"confirm": True, "reason": "test stop", "request_id": "auto-stop-1"},
    )
    assert stop.status_code == 200
    assert stop.json()["auto_trading_enabled"] is False

    repo = Repository(db_path)
    now = datetime.now(timezone.utc)
    repo.register_runtime("rt-api", now, pid=42, state="RUNNING")
    repo.update_runtime_heartbeat("rt-api", now, state="SCANNING", last_status="ok")

    process = client.get("/api/process/trader").json()
    assert process["process_state"] == "ONLINE"
    assert process["alive"] is True
    assert process["pid"] == 42


def test_trader_start_rejects_duplicate_when_online(tmp_path: Path) -> None:
    db_path = tmp_path / "api.db"
    init_db(db_path)
    config = _test_config(db_path)
    config.raw["process_control"] = {"mode": "local", "timeout_seconds": 5}
    repo = Repository(db_path)
    now = datetime.now(timezone.utc)
    repo.register_runtime("rt-live", now, pid=7, state="RUNNING")
    repo.update_runtime_heartbeat("rt-live", now, state="RUNNING")
    client = TestClient(create_app(config))

    response = client.post(
        "/api/actions/trader-loop/start",
        json={"confirm": True, "reason": "dup", "request_id": "start-1"},
    )
    assert response.status_code == 409


def test_current_round_includes_active_sessions_for_current_window(tmp_path: Path) -> None:
    db_path = tmp_path / "api.db"
    init_db(db_path)
    config = _test_config(db_path)
    repo = Repository(db_path)
    now = datetime.now(timezone.utc)
    repo.register_runtime("rt-live", now, pid=7, state="RUNNING")
    repo.request_round_start("test", "round-1", now)
    window_id = repo.claim_round_window("rt-live", now)
    session_id = repo.create_session(window_id, "BCHUSDT", "RUNNING", 200, 1, now)
    repo.update_session_soft_breach_count(session_id, 2)
    client = TestClient(create_app(config))

    body = client.get("/api/v2/current-round").json()

    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["symbol"] == "BCHUSDT"
    assert body["sessions"][0]["soft_breach_count"] == 2
