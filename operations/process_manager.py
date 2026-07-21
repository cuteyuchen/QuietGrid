from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from operations.process_models import ProcessStartResult


def _windows_process_exists(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        int(pid),
    )
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


class SupportsTraderRuntime(Protocol):
    def trader_runtime(self) -> dict[str, Any] | None: ...

    def mark_runtime_stopped(
        self,
        runtime_id: str,
        stopped_at: datetime,
        *,
        state: str = "STOPPED",
        last_error: str = "",
    ) -> None: ...

    def set_process_operation(self, operation: dict[str, Any], updated_at: datetime) -> None: ...

    def process_operation(self) -> dict[str, Any] | None: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TraderProcessManager(Protocol):
    def status(self, account_id: str) -> dict[str, Any]: ...

    def start(self, account_id: str) -> ProcessStartResult: ...

    def stop(self, account_id: str) -> dict[str, Any]: ...

    def restart(self, account_id: str) -> dict[str, Any]: ...


class LocalTraderProcessManager:
    def __init__(
        self,
        *,
        repository: SupportsTraderRuntime,
        config: dict[str, Any] | None = None,
        runtime_thresholds: tuple[float, float] = (20.0, 60.0),
        startup_timeout_seconds: float = 15.0,
        project_root: str | Path | None = None,
    ) -> None:
        self.repository = repository
        self.config = dict(config or {})
        self.stale_seconds, self.offline_seconds = runtime_thresholds
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self.stop_timeout_seconds = max(
            1.0,
            float(self.config.get("stop_timeout_seconds", 15.0)),
        )
        self.project_root = Path(project_root or self.config.get("working_directory") or ".").resolve()
        self.python_executable = str(
            self.config.get("python_executable")
            or sys.executable
            or "python"
        )
        self.trader_entry = str(self.config.get("trader_entry") or "trader.py")
        raw_args = self.config.get("trader_args") or ["--binance-loop"]
        if isinstance(raw_args, str):
            self.trader_args = [raw_args]
        else:
            self.trader_args = [str(item) for item in raw_args]
        self.pid_directory = Path(self.config.get("pid_directory") or "data/runtime")
        self.log_directory = Path(self.config.get("log_directory") or "logs")
        if not self.pid_directory.is_absolute():
            self.pid_directory = self.project_root / self.pid_directory
        if not self.log_directory.is_absolute():
            self.log_directory = self.project_root / self.log_directory

    def status(self, account_id: str) -> dict[str, Any]:
        runtime = self.repository.trader_runtime()
        alive = self._alive_from_runtime(runtime)
        pid_info = self._read_pid_file(account_id)
        return {
            **alive,
            "pid_file": pid_info,
            "command": self._command(account_id),
            "process_control_mode": "local",
            "process_control_available": True,
        }

    def start(self, account_id: str) -> ProcessStartResult:
        current = self.status(account_id)
        if current.get("process_state") == "ONLINE":
            return ProcessStartResult(
                started=False,
                pid=current.get("pid"),
                state="ONLINE",
                message="交易进程已经运行，不能重复启动。",
            )
        if current.get("process_state") == "STARTING":
            return ProcessStartResult(
                started=False,
                pid=current.get("pid"),
                state="STARTING",
                message="交易进程正在启动，请稍候。",
            )

        now = _utc_now()
        operation_id = str(uuid4())
        self.repository.set_process_operation(
            {
                "operation_id": operation_id,
                "action": "start",
                "status": "running",
                "requested_at": now.isoformat(),
                "completed_at": None,
                "pid": None,
                "error": "",
            },
            now,
        )

        command = self._command(account_id)
        self.pid_directory.mkdir(parents=True, exist_ok=True)
        self.log_directory.mkdir(parents=True, exist_ok=True)
        log_path = self.log_directory / f"trader-{account_id}.log"
        log_handle = open(log_path, "a", encoding="utf-8")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                close_fds=os.name != "nt",
            )
        except Exception as exc:
            log_handle.close()
            self.repository.set_process_operation(
                {
                    "operation_id": operation_id,
                    "action": "start",
                    "status": "failed",
                    "requested_at": now.isoformat(),
                    "completed_at": _utc_now().isoformat(),
                    "pid": None,
                    "error": str(exc),
                },
                _utc_now(),
            )
            return ProcessStartResult(
                started=False,
                pid=None,
                state="FAILED",
                message=f"启动交易进程失败: {exc}",
            )
        finally:
            # Parent keeps log file open only if Popen failed to take ownership;
            # on Windows the child inherits handles, parent can close.
            try:
                log_handle.close()
            except Exception:
                pass

        self._write_pid_file(account_id, proc.pid, command, now)
        self.repository.set_process_operation(
            {
                "operation_id": operation_id,
                "action": "start",
                "status": "started",
                "requested_at": now.isoformat(),
                "completed_at": _utc_now().isoformat(),
                "pid": int(proc.pid),
                "error": "",
            },
            _utc_now(),
        )
        return ProcessStartResult(
            started=True,
            pid=int(proc.pid),
            state="STARTING",
            message="交易进程已启动，正在等待首个心跳。",
        )

    def stop(self, account_id: str) -> dict[str, Any]:
        runtime = self.repository.trader_runtime() or {}
        pid_info = self._read_pid_file(account_id)
        pid_candidates = {
            int(value)
            for value in (
                pid_info.get("pid") if isinstance(pid_info, dict) else None,
                runtime.get("pid"),
            )
            if value is not None and str(value).strip()
        }
        if not pid_candidates:
            return {"ok": False, "state": "OFFLINE", "message": "未找到可停止的交易进程 PID。"}
        try:
            for pid in sorted(pid_candidates):
                self._terminate_process_tree(pid)
        except Exception as exc:
            return {
                "ok": False,
                "state": "FAILED",
                "message": f"停止交易进程失败: {exc}",
                "pids": sorted(pid_candidates),
            }
        if not self._wait_for_processes_to_stop(pid_candidates):
            return {
                "ok": False,
                "state": "FAILED",
                "message": "停止交易进程超时，仍有进程存活。",
                "pids": sorted(pid_candidates),
            }
        runtime_id = str(runtime.get("runtime_id") or "").strip()
        if runtime_id:
            self.repository.mark_runtime_stopped(
                runtime_id,
                _utc_now(),
                state="STOPPED",
            )
        self._remove_pid_file(account_id)
        return {
            "ok": True,
            "state": "STOPPED",
            "message": "交易进程已停止。",
            "pids": sorted(pid_candidates),
        }

    def restart(self, account_id: str) -> dict[str, Any]:
        stop_result = self.stop(account_id)
        if not stop_result.get("ok"):
            return {"ok": False, "stop": stop_result, "start": None}
        start_result = self.start(account_id)
        return {
            "ok": bool(start_result.started),
            "stop": stop_result,
            "start": start_result.to_mapping(),
        }

    def _terminate_process_tree(self, pid: int) -> None:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.stop_timeout_seconds,
            )
            if result.returncode not in {0, 128}:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(detail or f"taskkill 返回 {result.returncode}")
            return
        try:
            os.kill(int(pid), 15)
        except ProcessLookupError:
            return

    def _wait_for_processes_to_stop(self, pids: set[int]) -> bool:
        deadline = time.monotonic() + self.stop_timeout_seconds
        while time.monotonic() < deadline:
            if not any(self._process_exists(pid) for pid in pids):
                return True
            time.sleep(0.1)
        return not any(self._process_exists(pid) for pid in pids)

    @staticmethod
    def _process_exists(pid: int) -> bool:
        if os.name == "nt":
            return _windows_process_exists(pid)
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _command(self, account_id: str) -> list[str]:
        entry = self.trader_entry
        entry_path = Path(entry)
        if not entry_path.is_absolute():
            entry_path = self.project_root / entry_path
        command = [self.python_executable, str(entry_path), *self.trader_args]
        if account_id and account_id != "default":
            command.extend(["--account-id", str(account_id)])
        return command

    def _pid_path(self, account_id: str) -> Path:
        return self.pid_directory / f"trader-{account_id}.pid"

    def _write_pid_file(self, account_id: str, pid: int, command: list[str], started_at: datetime) -> None:
        payload = {
            "pid": int(pid),
            "account_id": account_id,
            "started_at": started_at.isoformat(),
            "command": command,
        }
        self._pid_path(account_id).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_pid_file(self, account_id: str) -> dict[str, Any] | None:
        path = self._pid_path(account_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _remove_pid_file(self, account_id: str) -> None:
        path = self._pid_path(account_id)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    def _alive_from_runtime(self, runtime: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(runtime, dict) or not str(runtime.get("runtime_id") or "").strip():
            return {
                "process_state": "OFFLINE",
                "alive": False,
                "pid": None,
                "runtime_id": "",
                "runtime_state": "",
                "started_at": "",
                "heartbeat_at": "",
                "heartbeat_age_seconds": None,
                "last_status": "",
                "last_error": "",
            }
        explicit = str(runtime.get("state") or "").strip().upper()
        heartbeat_at = _parse_iso(runtime.get("heartbeat_at")) or _parse_iso(runtime.get("started_at"))
        age = (_utc_now() - heartbeat_at).total_seconds() if heartbeat_at is not None else None
        stopped_at = _parse_iso(runtime.get("stopped_at"))
        if explicit == "FAILED":
            process_state = "FAILED"
            alive = False
        elif stopped_at is not None and explicit in {"STOPPED", "STOPPING"}:
            process_state = explicit
            alive = False
        elif age is None:
            process_state = "OFFLINE"
            alive = False
        elif age <= self.stale_seconds:
            process_state = "ONLINE"
            alive = True
        elif age <= self.offline_seconds:
            process_state = "STALE"
            alive = False
        else:
            process_state = "OFFLINE"
            alive = False
        pid_raw = runtime.get("pid")
        try:
            pid = int(pid_raw) if pid_raw is not None and str(pid_raw).strip() != "" else None
        except (TypeError, ValueError):
            pid = None
        return {
            "process_state": process_state,
            "alive": alive,
            "pid": pid,
            "runtime_id": str(runtime.get("runtime_id") or ""),
            "runtime_state": str(runtime.get("state") or ""),
            "started_at": str(runtime.get("started_at") or ""),
            "heartbeat_at": str(runtime.get("heartbeat_at") or ""),
            "heartbeat_age_seconds": age,
            "last_status": str(runtime.get("last_status") or ""),
            "last_error": str(runtime.get("last_error") or ""),
        }
