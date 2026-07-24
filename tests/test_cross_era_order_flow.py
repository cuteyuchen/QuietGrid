from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_order_flow as round29
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def test_protocol_and_frozen_inputs_are_locked() -> None:
    assert _sha256(round29.PROTOCOL_PATH.resolve()) == round29.PROTOCOL_SHA256
    assert _sha256(round29.ROUND28_RESULT_PATH.resolve()) == round29.ROUND28_RESULT_SHA256
    for config in round29.ASSET_CONFIG.values():
        assert _sha256(config["price_manifest"].resolve()) == config["price_manifest_sha256"]
        assert _sha256(config["funding_manifest"].resolve()) == config["funding_manifest_sha256"]


def _rows(hours: int = 12) -> list[dict[str, object]]:
    start = datetime(2021, 2, 6, 1, tzinfo=UTC)
    rows = []
    for index in range(hours):
        timestamp = start + timedelta(hours=index)
        rows.append(
            {
                "segment": "TEST",
                "open_time": int(timestamp.timestamp() * 1000),
                "open": 100.0 + index,
                "close": 100.5 + index,
                "quote_volume": 100.0,
                "taker_buy_quote_volume": 60.0,
            }
        )
    return rows


def test_build_signals_uses_previous_eight_hours_and_follow_direction() -> None:
    rows = _rows(14)
    split = {
        "segment": "TEST",
        "start": datetime(2021, 2, 6, 9, tzinfo=UTC),
        "end": datetime(2021, 2, 6, 14, tzinfo=UTC),
        "signal_rows": 6,
    }

    signals, audit = round29._build_signals(rows, split)

    assert len(signals) == 6
    assert all(item["target_position"] == 1 for item in signals)
    assert all(item["warmup_row_count"] == 8 for item in signals)
    assert audit["future_data_used"] is False


def test_build_signals_treats_zero_volume_warmup_as_neutral() -> None:
    rows = _rows(14)
    rows[4]["quote_volume"] = 0.0
    rows[4]["taker_buy_quote_volume"] = 0.0
    split = {
        "segment": "TEST",
        "start": datetime(2021, 2, 6, 9, tzinfo=UTC),
        "end": datetime(2021, 2, 6, 14, tzinfo=UTC),
        "signal_rows": 6,
    }

    signals, _audit = round29._build_signals(rows, split)

    assert signals[0]["zero_volume_warmup_count"] == 1
    assert signals[0]["mean_imbalance"] == pytest.approx(0.175)
