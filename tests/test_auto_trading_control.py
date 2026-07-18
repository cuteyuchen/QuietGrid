from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from api import ConsoleActionRequest, _run_auto_trading_action
from db.database import init_db
from db.repository import Repository


def test_auto_trading_control_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    repo.set_auto_trading_control(
        {
            "enabled": True,
            "requested_at": now.isoformat(),
            "requested_by": "web",
            "request_id": "req-1",
            "mode": "AUTO_WINDOW",
            "account_id": "default",
        },
        now,
    )
    control = repo.auto_trading_control()
    assert control is not None
    assert control["enabled"] is True
    assert control["request_id"] == "req-1"


def test_ensure_round_start_request_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    repo.register_runtime("rt-1", now, pid=1, state="RUNNING")
    first, created1 = repo.ensure_round_start_request(
        runtime_id="rt-1",
        reason="auto",
        request_id="auto-round:default:win1",
        window_key="win1",
        requested_at=now,
    )
    second, created2 = repo.ensure_round_start_request(
        runtime_id="rt-1",
        reason="auto",
        request_id="auto-round:default:win1",
        window_key="win1",
        requested_at=now,
    )
    assert created1 is True
    assert created2 is False
    assert first["request_id"] == second["request_id"]


def test_auto_trading_start_is_idempotent_and_does_not_duplicate_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime.now(timezone.utc)
    repo.register_runtime("rt-1", now, pid=1, state="RUNNING")
    config = SimpleNamespace(
        account_id="default",
        raw={
            "process_control": {"mode": "unavailable"},
            "runtime": {"heartbeat_stale_seconds": 30, "heartbeat_offline_seconds": 90},
        },
    )
    request = ConsoleActionRequest(confirm=True, reason="test", request_id="req-1")

    first = _run_auto_trading_action(config, repo, request, enabled=True)  # type: ignore[arg-type]
    second = _run_auto_trading_action(config, repo, request, enabled=True)  # type: ignore[arg-type]

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["transition_state"] == "ENABLED"
    assert second["message"] == "自动交易已经启用。"
    events = [
        row
        for row in repo.recent_rows("system_logs", limit=20)
        if row.get("module") == "auto_trading"
    ]
    assert len(events) == 1
