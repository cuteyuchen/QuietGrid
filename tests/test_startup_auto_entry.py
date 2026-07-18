from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from db.database import init_db
from db.repository import Repository
from strategy.controller import TradingController
from strategy.window_models import TradingWindow, WindowKind


class _WindowScheduler:
    def __init__(self, window: TradingWindow) -> None:
        self.window = window

    def is_in_window(self, now=None) -> bool:
        return self.window.allowed

    def should_force_close(self, now=None) -> bool:
        return self.window.kind == WindowKind.FORCE_CLOSE_BUFFER

    def classify_window(self, now=None, **kwargs):
        return self.window


def test_bootstrap_auto_entry_creates_request_once(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    repo.register_runtime("rt-1", now, pid=1, state="RUNNING")
    repo.set_auto_trading_control({"enabled": True, "mode": "AUTO_WINDOW"}, now)

    controller = object.__new__(TradingController)
    controller.repository = repo
    controller.scheduler = _WindowScheduler(
        TradingWindow(
            kind=WindowKind.WEEKEND,
            allowed=True,
            window_key="NYSE:test",
            previous_market_close=None,
            next_market_open=None,
            next_premarket_open=None,
            force_close_at=None,
            minutes_to_force_close=1000,
            reason="weekend",
        )
    )
    first = controller.bootstrap_auto_entry(now)
    second = controller.bootstrap_auto_entry(now)
    assert any(item.startswith("auto_round_requested:") for item in first)
    assert any(item.startswith("auto_round_exists:") for item in second)


def test_bootstrap_waits_outside_window(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    init_db(db_path)
    repo = Repository(db_path)
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    repo.register_runtime("rt-1", now, pid=1, state="RUNNING")
    repo.set_auto_trading_control({"enabled": True}, now)
    controller = object.__new__(TradingController)
    controller.repository = repo
    controller.scheduler = _WindowScheduler(
        TradingWindow(
            kind=WindowKind.WEEKDAY_OVERNIGHT,
            allowed=False,
            window_key="NYSE:overnight",
            previous_market_close=None,
            next_market_open=None,
            next_premarket_open=None,
            force_close_at=None,
            minutes_to_force_close=100,
            reason="overnight",
        )
    )
    actions = controller.bootstrap_auto_entry(now)
    assert actions[0].startswith("auto_waiting_window:")
