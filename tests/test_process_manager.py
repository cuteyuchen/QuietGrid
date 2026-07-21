from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from db.database import init_db
from db.repository import Repository
from operations import process_manager
from operations.process_manager import LocalTraderProcessManager
from operations.process_models import ProcessStartResult


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

    def mark_runtime_stopped(self, runtime_id, stopped_at, *, state="STOPPED", last_error=""):
        if self._runtime is None or self._runtime.get("runtime_id") != runtime_id:
            return
        self._runtime = {
            **self._runtime,
            "state": state,
            "heartbeat_at": stopped_at.isoformat(),
            "stopped_at": stopped_at.isoformat(),
            "last_error": last_error or self._runtime.get("last_error", ""),
        }


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


def test_stop_terminates_launcher_and_runtime_pid_and_marks_runtime_stopped(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(timezone.utc)
    repo = FakeRepo(
        {
            "runtime_id": "rt-live",
            "pid": 202,
            "started_at": now.isoformat(),
            "heartbeat_at": now.isoformat(),
            "state": "RUNNING",
            "stopped_at": None,
        }
    )
    manager = LocalTraderProcessManager(
        repository=repo,
        config={
            "working_directory": str(tmp_path),
            "pid_directory": str(tmp_path / "pid"),
            "log_directory": str(tmp_path / "logs"),
        },
        project_root=tmp_path,
    )
    manager.pid_directory.mkdir(parents=True, exist_ok=True)
    manager._write_pid_file("default", 101, ["python", "trader.py"], now)
    terminated: list[int] = []
    monkeypatch.setattr(manager, "_terminate_process_tree", terminated.append)
    monkeypatch.setattr(manager, "_wait_for_processes_to_stop", lambda pids: True)

    result = manager.stop("default")

    assert result["ok"] is True
    assert result["state"] == "STOPPED"
    assert terminated == [101, 202]
    assert repo.trader_runtime()["state"] == "STOPPED"
    assert manager._read_pid_file("default") is None


def test_restart_does_not_report_success_when_start_did_not_create_a_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = LocalTraderProcessManager(
        repository=FakeRepo(),
        config={"working_directory": str(tmp_path)},
        project_root=tmp_path,
    )
    monkeypatch.setattr(
        manager,
        "stop",
        lambda _account_id: {"ok": True, "state": "STOPPED"},
    )
    monkeypatch.setattr(
        manager,
        "start",
        lambda _account_id: ProcessStartResult(
            started=False,
            pid=202,
            state="ONLINE",
            message="旧进程仍在线。",
        ),
    )

    result = manager.restart("default")

    assert result["ok"] is False


def test_process_exists_uses_windows_process_query(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(process_manager.os, "name", "nt")
    monkeypatch.setattr(
        process_manager,
        "_windows_process_exists",
        lambda pid: calls.append(pid) or False,
    )

    assert LocalTraderProcessManager._process_exists(64344) is False
    assert calls == [64344]
