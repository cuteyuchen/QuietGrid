from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_extreme_reversal as round30
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def test_protocol_and_frozen_dependencies_are_locked() -> None:
    assert _sha256(round30.PROTOCOL_PATH.resolve()) == round30.PROTOCOL_SHA256
    assert _sha256(round30.DATA_AUDIT_PATH.resolve()) == round30.DATA_AUDIT_SHA256
    assert _sha256(round30.Path(round30.round29.__file__).resolve()) == round30.ROUND29_READER_SOURCE_SHA256
    assert _sha256(round30.Path(round30.round22.__file__).resolve()) == round30.ROUND22_FUNDING_READER_SOURCE_SHA256
    for config in round30.ASSET_CONFIG.values():
        assert _sha256(config["price_manifest"].resolve()) == config["price_manifest_sha256"]
        assert _sha256(config["funding_manifest"].resolve()) == config["funding_manifest_sha256"]


def _rows(hours: int = 80) -> list[dict[str, object]]:
    start = datetime(2021, 1, 1, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for index in range(hours):
        timestamp = start + timedelta(hours=index)
        rows.append(
            {
                "segment": "TEST",
                "open_time": int(timestamp.timestamp() * 1000),
                "open": 100.0,
                "close": 100.0,
            }
        )
    return rows


def test_build_signals_uses_only_completed_twelve_hour_return() -> None:
    rows = _rows(20)
    rows[12]["close"] = 104.0
    split = {
        "segment": "TEST",
        "start": datetime(2021, 1, 1, 13, tzinfo=UTC),
        "end": datetime(2021, 1, 1, 14, tzinfo=UTC),
        "signal_rows": 2,
    }

    signals, audit = round30._build_signals(
        rows,
        split,
        direction="CONTRARIAN",
        lookback_hours=12,
        threshold=0.03,
    )

    assert signals[0]["target_position"] == -1
    assert signals[0]["return_pct"] == pytest.approx(0.04)
    assert signals[0]["warmup_row_count"] == 13
    assert signals[0]["latest_source_time"] < signals[0]["execution_time"]
    assert audit["future_data_used"] is False


def test_simulation_holds_once_costs_both_sides_and_finishes_flat() -> None:
    rows = _rows(80)
    start = datetime(2021, 1, 1, 13, tzinfo=UTC)
    end = datetime(2021, 1, 4, 7, tzinfo=UTC)
    split = {
        "segment": "TEST",
        "start": start,
        "end": end,
        "signal_rows": 67,
    }
    entry_time = int(start.timestamp() * 1000)
    ignored_time = int((start + timedelta(hours=8)).timestamp() * 1000)
    signals = [
        {
            "execution_time": entry_time,
            "target_position": 1,
            "lookback_hours": 12,
            "warmup_row_count": 13,
            "latest_source_time": entry_time - round30.HOUR_MS,
        },
        {
            "execution_time": ignored_time,
            "target_position": -1,
            "lookback_hours": 12,
            "warmup_row_count": 13,
            "latest_source_time": ignored_time - round30.HOUR_MS,
        },
    ]

    result = round30._simulate(
        rows,
        [],
        signals,
        split=split,
        initial_capital=100.0,
        execution_cost_rate=0.001,
        hold_hours=48,
    )

    assert result["metrics"]["completed_trade_count"] == 1
    assert result["metrics"]["scheduled_completed_trade_count"] == 1
    assert result["metrics"]["final_truncated_trade_count"] == 0
    assert result["metrics"]["execution_side_count"] == 2
    assert result["metrics"]["ignored_signal_count"] == 1
    assert result["metrics"]["execution_costs"] == pytest.approx(0.2)
    assert result["metrics"]["total_pnl"] == pytest.approx(-0.2)
    assert result["metrics"]["pnl_decomposition_error"] == pytest.approx(0.0)
    assert result["metrics"]["final_position"] == 0
    assert result["checks"]["all_execution_sides_costed_and_flat"] is True
    assert result["checks"]["completed_trade_hours_exact_when_scheduled"] is True


def test_screen_candidate_ids_are_unique_and_selected_id_is_canonical() -> None:
    ids = {
        round30._candidate_id(direction, lookback, hold, threshold)
        for direction in round30.SCREEN_DIRECTIONS
        for lookback in round30.SCREEN_LOOKBACKS
        for hold in round30.SCREEN_HOLDS
        for threshold in round30.SCREEN_THRESHOLDS
    }

    assert len(ids) == 250
    assert (
        round30._candidate_id("CONTRARIAN", 12, 48, 0.03)
        == round30.SELECTED_CANDIDATE_ID
    )
