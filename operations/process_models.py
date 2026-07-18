from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProcessStartResult:
    started: bool
    pid: int | None
    state: str
    message: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "pid": self.pid,
            "state": self.state,
            "message": self.message,
        }


@dataclass
class ProcessOperation:
    operation_id: str
    action: str
    status: str
    requested_at: str
    completed_at: str | None = None
    pid: int | None = None
    error: str = ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "action": self.action,
            "status": self.status,
            "requested_at": self.requested_at,
            "completed_at": self.completed_at,
            "pid": self.pid,
            "error": self.error,
        }
