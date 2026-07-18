from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from db.database import init_db
from db.repository import Repository


def test_register_runtime_and_heartbeat(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)

    runtime = repo.register_runtime("rt-1", now, pid=1234, state="BOOTING")
    assert runtime["runtime_id"] == "rt-1"
    assert runtime["pid"] == 1234
    assert runtime["heartbeat_at"] == now.isoformat()

    later = now + timedelta(seconds=5)
    repo.update_runtime_heartbeat("rt-1", later, state="SCANNING", last_status="no_eligible_symbol")
    stored = repo.trader_runtime()
    assert stored is not None
    assert stored["state"] == "SCANNING"
    assert stored["last_status"] == "no_eligible_symbol"
    assert stored["heartbeat_at"] == later.isoformat()

    repo.mark_runtime_stopped("rt-1", later + timedelta(seconds=1), state="STOPPED")
    stopped = repo.trader_runtime()
    assert stopped is not None
    assert stopped["state"] == "STOPPED"
    assert stopped["stopped_at"] is not None


def test_old_runtime_heartbeat_is_ignored(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    repo.register_runtime("rt-new", now, pid=1, state="RUNNING")
    repo.update_runtime_heartbeat("rt-old", now + timedelta(seconds=1), state="RUNNING")
    stored = repo.trader_runtime()
    assert stored is not None
    assert stored["runtime_id"] == "rt-new"


def test_timestamp_only_heartbeat_never_overwrites_runtime_phase(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    repo.register_runtime("rt-1", now, pid=1, state="RECOVERING")
    repo.update_runtime_heartbeat(
        "rt-1",
        now + timedelta(seconds=1),
        state="RUNNING",
        last_status="recovered",
    )

    repo.update_runtime_heartbeat("rt-1", now + timedelta(seconds=2))

    stored = repo.trader_runtime()
    assert stored is not None
    assert stored["state"] == "RUNNING"
    assert stored["last_status"] == "recovered"
