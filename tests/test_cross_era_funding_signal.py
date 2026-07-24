from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_funding_signal as round31
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def test_protocol_and_frozen_dependencies_are_locked() -> None:
    assert _sha256(round31.PROTOCOL_PATH.resolve()) == round31.PROTOCOL_SHA256
    assert _sha256(round31.DATA_AUDIT_PATH.resolve()) == round31.DATA_AUDIT_SHA256
    assert _sha256(round31.Path(round31.round29.__file__).resolve()) == round31.ROUND29_READER_SOURCE_SHA256
    assert _sha256(round31.Path(round31.round22.__file__).resolve()) == round31.ROUND22_READER_SOURCE_SHA256


def _rows(hours: int = 100) -> list[dict[str, object]]:
    start = datetime(2021, 1, 1, tzinfo=UTC)
    return [
        {
            "segment": "TEST",
            "open_time": int((start + timedelta(hours=index)).timestamp() * 1000),
            "open": 100.0,
            "close": 100.0,
        }
        for index in range(hours)
    ]


def test_funding_event_is_observed_before_next_hour_execution() -> None:
    rows = _rows(20)
    event_time = int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000)
    split = {
        "segment": "TEST",
        "start": datetime(2021, 1, 1, 1, tzinfo=UTC),
        "end": datetime(2021, 1, 1, 12, tzinfo=UTC),
        "signal_rows": 12,
    }
    signals, audit = round31._build_signals(
        [{"funding_time": event_time, "funding_rate": 0.0002}],
        rows,
        split,
        direction="CONTRARIAN",
        threshold=0.0001,
    )

    assert len(signals) == 1
    assert signals[0]["execution_time"] == event_time + round31.HOUR_MS
    assert signals[0]["target_position"] == -1
    assert audit["all_events_precede_execution"] is True
    assert audit["future_data_used"] is False


def test_simulation_costs_both_sides_and_finishes_flat() -> None:
    rows = _rows(100)
    start = datetime(2021, 1, 1, 1, tzinfo=UTC)
    end = datetime(2021, 1, 4, 4, tzinfo=UTC)
    split = {
        "segment": "TEST",
        "start": start,
        "end": end,
        "signal_rows": 76,
    }
    event_time = int(datetime(2021, 1, 1, tzinfo=UTC).timestamp() * 1000)
    execution_time = event_time + round31.HOUR_MS
    result = round31._simulate(
        rows,
        [{"funding_time": event_time, "funding_rate": 0.0002}],
        [
            {
                "event_time": event_time,
                "execution_time": execution_time,
                "funding_rate": 0.0002,
                "target_position": -1,
            }
        ],
        split=split,
        initial_capital=100.0,
        execution_cost_rate=0.001,
        hold_hours=72,
    )

    assert result["metrics"]["completed_trade_count"] == 1
    assert result["metrics"]["scheduled_trade_count"] == 1
    assert result["metrics"]["execution_side_count"] == 2
    assert result["metrics"]["execution_costs"] == pytest.approx(0.2)
    assert result["metrics"]["total_pnl"] == pytest.approx(-0.2)
    assert result["metrics"]["pnl_decomposition_error"] == pytest.approx(0.0)
    assert result["metrics"]["final_position"] == 0
    assert result["checks"]["all_execution_sides_costed_and_flat"] is True


def test_screen_candidate_ids_are_unique() -> None:
    ids = {
        round31._candidate_id(direction, threshold, hold)
        for direction in round31.SCREEN_DIRECTIONS
        for threshold in round31.SCREEN_THRESHOLDS
        for hold in round31.SCREEN_HOLDS
    }
    assert len(ids) == 40
    assert round31._candidate_id("CONTRARIAN", 0.0001, 72) == round31.SELECTED_CANDIDATE_ID
