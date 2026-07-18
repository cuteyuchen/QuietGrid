from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from db.database import init_db
from db.repository import Repository
from operations.process_manager import LocalTraderProcessManager


class FakeRepo:
    def __init__(self, runtime=None):
        self._runtime = runtime
        self.operations = []

    def trader_runtime(self):
        return self._runtime

    def set_process_operation(self, operation, updated_at):
        self.operations.append(operation)

    def process_operation(self):
        return self.operations[-1] if self.operations else None


def test_status_offline_without_runtime(tmp_path: Path) -> None:
    manager = LocalTraderProcessManager(
        repository=FakeRepo(),
        config={"working_directory": str(tmp_path), "pid_directory": str(tmp_path / "pid"), "log_directory": str(tmp_path / "logs")},
        project_root=tmp_path,
    )
    status = manager.status("default")
    assert status["process_state"] == "OFFLINE"
    assert status["alive"] is False


def test_status_online_from_fresh_heartbeat(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    runtime = {
        "runtime_id": "rt-1",
        "pid": 123,
        "started_at": now.isoformat(),
        "heartbeat_at": now.isoformat(),
        "state": "SCANNING",
        "last_status": "ok",
        "last_error": "",
        "stopped_at": None,
    }
    manager = LocalTraderProcessManager(
        repository=FakeRepo(runtime),
        config={"working_directory": str(tmp_path)},
        project_root=tmp_path,
        runtime_thresholds=(20.0, 60.0),
    )
    status = manager.status("default")
    assert status["process_state"] == "ONLINE"
    assert status["alive"] is True
    assert status["pid"] == 123


def test_status_stale_after_threshold(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    runtime = {
        "runtime_id": "rt-1",
        "pid": 123,
        "started_at": (now - timedelta(seconds=40)).isoformat(),
        "heartbeat_at": (now - timedelta(seconds=30)).isoformat(),
        "state": "SCANNING",
        "stopped_at": None,
    }
    manager = LocalTraderProcessManager(
        repository=FakeRepo(runtime),
        config={"working_directory": str(tmp_path)},
        project_root=tmp_path,
        runtime_thresholds=(20.0, 60.0),
    )
    status = manager.status("default")
    assert status["process_state"] == "STALE"


def test_start_refuses_when_online(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    runtime = {
        "runtime_id": "rt-1",
        "pid": 99,
        "started_at": now.isoformat(),
        "heartbeat_at": now.isoformat(),
        "state": "RUNNING",
        "stopped_at": None,
    }
    manager = LocalTraderProcessManager(
        repository=FakeRepo(runtime),
        config={"working_directory": str(tmp_path)},
        project_root=tmp_path,
    )
    result = manager.start("default")
    assert result.started is False
    assert result.state == "ONLINE"


def test_repository_process_operation(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime.now(timezone.utc)
    repo.set_process_operation({"operation_id": "op1", "action": "start", "status": "running"}, now)
    op = repo.process_operation()
    assert op is not None
    assert op["operation_id"] == "op1"
