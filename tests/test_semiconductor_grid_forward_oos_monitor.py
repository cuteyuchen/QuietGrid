from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.semiconductor_grid_forward_oos_v29_monitor import (
    FROZEN_V29_RUNNER_SHA256,
    _assert_history_unchanged,
    _exclusive_monitor_lock,
    pending_complete_windows,
)
from scripts.semiconductor_grid_forward_oos_v29 import _source_file_sha256
from strategy.semiconductor_grid_v29 import (
    FORWARD_OOS_SCENARIOS,
    FORWARD_OOS_SEEDS,
    PRIMARY_CANDIDATE_ID,
)


CANDIDATE_SHA = "frozen-31111-sha"


def _manifest(
    *,
    symbol: str = "SNDKUSDT",
    window_key: str = "NYSE:window-1",
    complete: bool = True,
    eligible: bool = True,
    window_end: datetime | None = None,
) -> dict[str, object]:
    end = window_end or datetime(2026, 8, 17, 6, tzinfo=UTC)
    return {
        "symbol": symbol,
        "market_calendar": "NYSE",
        "window_key": window_key,
        "window_start": (end - timedelta(days=2)).isoformat(),
        "window_end": end.isoformat(),
        "complete_window": complete,
        "oos_eligible": eligible,
    }


def _result(
    manifest: dict[str, object], scenario: str, seed: int
) -> dict[str, object]:
    return {
        "record_type": "OOS_RESULT",
        "candidate_id": PRIMARY_CANDIDATE_ID,
        "candidate_sha": CANDIDATE_SHA,
        "symbol": manifest["symbol"],
        "window_key": manifest["window_key"],
        "scenario": scenario,
        "seed": seed,
        "sequence_valid": True,
    }


def test_pending_complete_windows_detects_only_closed_unseen_windows() -> None:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    complete = _manifest()
    future = _manifest(
        window_key="NYSE:future",
        window_end=now + timedelta(days=1),
    )
    incomplete = _manifest(window_key="NYSE:partial", complete=False)

    pending = pending_complete_windows(
        [complete, future, incomplete],
        [],
        candidate_sha=CANDIDATE_SHA,
        checked_at=now,
    )

    assert [item["window_key"] for item in pending] == ["NYSE:window-1"]
    assert pending[0]["existing_result_rows"] == 0
    assert pending[0]["missing_result_rows"] == len(FORWARD_OOS_SCENARIOS) * len(
        FORWARD_OOS_SEEDS
    )


def test_pending_complete_windows_repairs_only_missing_matrix_rows() -> None:
    manifest = _manifest()
    records = [
        _result(manifest, scenario, seed)
        for scenario in FORWARD_OOS_SCENARIOS
        for seed in FORWARD_OOS_SEEDS
    ]
    records.pop()

    pending = pending_complete_windows(
        [manifest],
        records,
        candidate_sha=CANDIDATE_SHA,
        checked_at=datetime(2026, 8, 22, tzinfo=UTC),
    )

    assert len(pending) == 1
    assert pending[0]["existing_result_rows"] == len(records)
    assert pending[0]["missing_result_rows"] == 1


def test_pending_complete_windows_is_idempotent_after_full_matrix() -> None:
    manifest = _manifest()
    records = [
        _result(manifest, scenario, seed)
        for scenario in FORWARD_OOS_SCENARIOS
        for seed in FORWARD_OOS_SEEDS
    ]

    assert pending_complete_windows(
        [manifest],
        records,
        candidate_sha=CANDIDATE_SHA,
        checked_at=datetime(2026, 8, 22, tzinfo=UTC),
    ) == []


def test_append_only_guard_accepts_prefix_and_rejects_history_rewrite() -> None:
    before = [{"record_type": "CANDIDATE_FREEZE", "candidate_id": "31111"}]
    appended = {"record_type": "OOS_RESULT", "candidate_id": "31111"}
    _assert_history_unchanged(before, [*before, appended], b"header\nold\n", b"header\nold\nnew\n")

    with pytest.raises(RuntimeError, match="CSV history"):
        _assert_history_unchanged(before, [*before, appended], b"old", b"changed")
    with pytest.raises(RuntimeError, match="historical records"):
        _assert_history_unchanged(before, [{"record_type": "MUTATED"}, appended], b"old", b"oldnew")


def test_monitor_lock_prevents_concurrent_append(tmp_path: Path) -> None:
    with _exclusive_monitor_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already running"):
            with _exclusive_monitor_lock(tmp_path):
                pass
    assert not (tmp_path / ".forward-oos-monitor.lock").exists()


def test_v29_execution_runner_remains_byte_frozen() -> None:
    runner = Path(__file__).parents[1] / "scripts" / "semiconductor_grid_forward_oos_v29.py"
    assert _source_file_sha256(runner) == FROZEN_V29_RUNNER_SHA256
