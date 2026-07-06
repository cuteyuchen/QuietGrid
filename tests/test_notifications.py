from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from core.notifications import WebhookNotifier, build_system_log_notifier


def test_notifications_are_disabled_by_default() -> None:
    assert build_system_log_notifier({}) is None
    assert build_system_log_notifier({"enabled": False, "webhook_url": "http://example.test/hook"}) is None


def test_enabled_notifications_require_webhook_url() -> None:
    with pytest.raises(ValueError, match="webhook_url"):
        build_system_log_notifier({"enabled": True, "webhook_url": ""})


def test_webhook_notifier_filters_below_min_level() -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []
    notifier = WebhookNotifier(
        "http://example.test/hook",
        min_level="ERROR",
        post_json=lambda url, payload, timeout: calls.append((url, payload, timeout)),
    )

    notifier("WARN", "controller", "warning", None, datetime(2026, 7, 5, tzinfo=timezone.utc))

    assert calls == []


def test_webhook_notifier_posts_generic_payload() -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []
    now = datetime(2026, 7, 5, 12, 30, tzinfo=timezone.utc)
    notifier = WebhookNotifier(
        "http://example.test/hook",
        min_level="WARN",
        timeout_seconds=2,
        post_json=lambda url, payload, timeout: calls.append((url, payload, timeout)),
    )

    notifier("ERROR", "risk", "Position mismatch.", "symbol=AAPLUSDT", now)

    assert calls == [
        (
            "http://example.test/hook",
            {
                "source": "quietgrid",
                "level": "ERROR",
                "module": "risk",
                "message": "Position mismatch.",
                "detail": "symbol=AAPLUSDT",
                "log_time": now.isoformat(),
            },
            2,
        )
    ]


def test_webhook_notifier_posts_dingtalk_payload() -> None:
    calls: list[tuple[str, dict[str, Any], float]] = []
    now = datetime(2026, 7, 5, 12, 30, tzinfo=timezone.utc)
    notifier = WebhookNotifier(
        "http://example.test/dingtalk",
        payload_format="dingtalk",
        post_json=lambda url, payload, timeout: calls.append((url, payload, timeout)),
    )

    notifier("WARN", "commission_health", "Maker fee changed.", "maker=0.0002", now)

    payload = calls[0][1]
    assert calls[0][0] == "http://example.test/dingtalk"
    assert payload["msgtype"] == "text"
    assert "[QuietGrid][WARN] commission_health" in payload["text"]["content"]
    assert "Maker fee changed." in payload["text"]["content"]
    assert "detail=maker=0.0002" in payload["text"]["content"]
