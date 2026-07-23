from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

import scripts.freeze_round25_bitmex_funding as freeze
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _payload(
    symbol: str = "XBTUSD",
    *,
    daily_multiplier: float = 3.0,
) -> bytes:
    rows = [
        {
            "timestamp": "2020-01-01T04:00:00.000Z",
            "symbol": symbol,
            "fundingInterval": freeze.FUNDING_INTERVAL,
            "fundingRate": 0.0001,
            "fundingRateDaily": 0.0001 * daily_multiplier,
        },
        {
            "timestamp": "2020-01-01T12:00:00.000Z",
            "symbol": symbol,
            "fundingInterval": freeze.FUNDING_INTERVAL,
            "fundingRate": -0.0002,
            "fundingRateDaily": -0.0002 * daily_multiplier,
        },
    ]
    return json.dumps(rows, separators=(",", ":")).encode("utf-8")


def test_protocol_and_audit_hashes_are_frozen() -> None:
    assert _sha256(freeze.PROTOCOL_PATH.resolve()) == freeze.PROTOCOL_SHA256
    assert _sha256(freeze.AUDIT_PATH.resolve()) == freeze.AUDIT_SHA256


def test_authorized_segments_exclude_final_oos_buffer() -> None:
    assert len(freeze.SEGMENTS) == 2
    first = freeze.SEGMENTS[0]
    second = freeze.SEGMENTS[1]

    assert first[2] == freeze.EXCLUDED_START
    assert second[1] == freeze.EXCLUDED_END
    assert first[2] < second[1]
    assert freeze.EXPECTED_EVENT_COUNT == 5_928
    assert freeze.EXPECTED_PAGE_COUNT == 13


def test_parse_page_validates_schema_and_normalizes_timestamp() -> None:
    payload = _payload()
    digest = hashlib.sha256(payload).hexdigest()

    events = freeze._parse_page(
        payload,
        symbol="XBTUSD",
        segment_name="AUTHORIZED_HISTORY",
        segment_start=datetime(2020, 1, 1, tzinfo=UTC),
        segment_end=datetime(2020, 1, 2, tzinfo=UTC),
        source_page_sha256=digest,
    )

    assert [item["funding_time"] for item in events] == [
        1_577_851_200_000,
        1_577_880_000_000,
    ]
    assert all(item["funding_interval_hours"] == 8 for item in events)
    assert all(item["source_page_sha256"] == digest for item in events)


def test_parse_page_rejects_symbol_mismatch() -> None:
    payload = _payload(symbol="ETHUSD")

    with pytest.raises(ValueError, match="标的不一致"):
        freeze._parse_page(
            payload,
            symbol="XBTUSD",
            segment_name="AUTHORIZED_HISTORY",
            segment_start=datetime(2020, 1, 1, tzinfo=UTC),
            segment_end=datetime(2020, 1, 2, tzinfo=UTC),
            source_page_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_parse_page_rejects_inconsistent_daily_rate() -> None:
    payload = _payload(daily_multiplier=2.0)

    with pytest.raises(ValueError, match="三倍"):
        freeze._parse_page(
            payload,
            symbol="XBTUSD",
            segment_name="AUTHORIZED_HISTORY",
            segment_start=datetime(2020, 1, 1, tzinfo=UTC),
            segment_end=datetime(2020, 1, 2, tzinfo=UTC),
            source_page_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_parse_page_rejects_event_outside_segment() -> None:
    payload = _payload()

    with pytest.raises(ValueError, match="授权段外"):
        freeze._parse_page(
            payload,
            symbol="XBTUSD",
            segment_name="AUTHORIZED_HISTORY",
            segment_start=datetime(2020, 1, 2, tzinfo=UTC),
            segment_end=datetime(2020, 1, 3, tzinfo=UTC),
            source_page_sha256=hashlib.sha256(payload).hexdigest(),
        )
