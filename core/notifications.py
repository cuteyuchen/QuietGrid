from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable


PostJson = Callable[[str, dict[str, Any], float], None]

_LEVEL_PRIORITY = {
    "DEBUG": 10,
    "INFO": 20,
    "WARN": 30,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


@dataclass(frozen=True)
class WebhookNotifier:
    webhook_url: str
    min_level: str = "WARN"
    timeout_seconds: float = 5.0
    payload_format: str = "generic"
    telegram_chat_id: str = ""
    post_json: PostJson | None = None

    def __post_init__(self) -> None:
        if not self.webhook_url.strip():
            raise ValueError("notification webhook_url is required when notifications are enabled")
        if self.timeout_seconds <= 0:
            raise ValueError("notification timeout_seconds must be positive")
        if _level_priority(self.min_level) is None:
            raise ValueError(f"unsupported notification min_level: {self.min_level}")
        if self.payload_format not in {"generic", "dingtalk", "telegram"}:
            raise ValueError(f"unsupported notification format: {self.payload_format}")
        if self.payload_format == "telegram" and not self.telegram_chat_id.strip():
            raise ValueError("notification telegram_chat_id is required when format is telegram")

    def __call__(
        self,
        level: str,
        module: str,
        message: str,
        detail: str | None,
        log_time: datetime,
    ) -> None:
        event_priority = _level_priority(level)
        min_priority = _level_priority(self.min_level)
        if event_priority is None or min_priority is None or event_priority < min_priority:
            return
        payload = self._payload(level, module, message, detail, log_time)
        post_json = self.post_json or _post_json_with_httpx
        post_json(self.webhook_url, payload, self.timeout_seconds)

    def _payload(
        self,
        level: str,
        module: str,
        message: str,
        detail: str | None,
        log_time: datetime,
    ) -> dict[str, Any]:
        if self.payload_format == "dingtalk":
            lines = [
                f"[QuietGrid][{level.upper()}] {module}",
                message,
                f"time={log_time.isoformat()}",
            ]
            if detail:
                lines.append(f"detail={detail}")
            return {"msgtype": "text", "text": {"content": "\n".join(lines)}}
        if self.payload_format == "telegram":
            lines = [
                f"[QuietGrid][{level.upper()}] {module}",
                message,
                f"time={log_time.isoformat()}",
            ]
            if detail:
                lines.append(f"detail={detail}")
            return {
                "chat_id": self.telegram_chat_id,
                "text": _telegram_text("\n".join(lines)),
                "disable_web_page_preview": True,
            }
        return {
            "source": "quietgrid",
            "level": level.upper(),
            "module": module,
            "message": message,
            "detail": detail,
            "log_time": log_time.isoformat(),
        }


def build_system_log_notifier(raw_config: dict[str, Any] | None) -> WebhookNotifier | None:
    if not raw_config or not raw_config.get("enabled"):
        return None
    return WebhookNotifier(
        webhook_url=str(raw_config.get("webhook_url", "")),
        min_level=str(raw_config.get("min_level", "WARN")),
        timeout_seconds=float(raw_config.get("timeout_seconds", 5)),
        payload_format=str(raw_config.get("format", "generic")),
        telegram_chat_id=str(raw_config.get("telegram_chat_id", "")),
    )


def _level_priority(level: str) -> int | None:
    return _LEVEL_PRIORITY.get(str(level).strip().upper())


def _post_json_with_httpx(webhook_url: str, payload: dict[str, Any], timeout_seconds: float) -> None:
    import httpx

    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(webhook_url, json=payload)
        response.raise_for_status()


def _telegram_text(text: str) -> str:
    if len(text) <= 4096:
        return text
    return text[:4093] + "..."
