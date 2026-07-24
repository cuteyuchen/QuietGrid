from __future__ import annotations

import csv
import json

import pytest

from scripts.discover_stock_perpetuals import _classify
from scripts.stock_perp_common import PublicDataError, immutable_write
from scripts.stock_perp_data_audit import audit_funding, audit_klines


KLINE_FIELDS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
)


def _write_klines(path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=KLINE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _kline(open_time: int, *, close: float = 100.0) -> dict[str, object]:
    return {
        "open_time": open_time,
        "close_time": open_time + 59_999,
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 2,
        "quote_volume": 200,
        "trade_count": 3,
    }


def test_immutable_write_accepts_identical_bytes_and_rejects_changes(tmp_path) -> None:
    path = tmp_path / "manifest.json"

    first = immutable_write(path, '{"frozen":true}\n')
    second = immutable_write(path, '{"frozen":true}\n')

    assert first == second
    with pytest.raises(PublicDataError, match="不可变文件"):
        immutable_write(path, '{"frozen":false}\n')


def test_discovery_classification_preserves_core_short_and_hard_exclusion() -> None:
    info = {
        "status": "TRADING",
        "contractType": "TRADIFI_PERPETUAL",
        "quoteAsset": "USDT",
        "underlyingType": "EQUITY",
    }
    listing = {"ETF": "N", "Security Name": "Example, Inc. - Common Stock"}
    probe = {
        "first_valid_1m": {"open_time": 1},
        "last_complete_1m": {"open_time": 2},
        "weekend_nonzero_rows": 10,
        "funding_event_count": 2,
    }

    assert _classify(
        info, listing, probe, complete_months=4, weekend_estimate=14
    ) == ("TIER_A_CORE", [])
    assert _classify(
        info, listing, probe, complete_months=3, weekend_estimate=14
    ) == ("TIER_A_SHORT", ["insufficient_complete_months_or_weekends"])
    tier, reasons = _classify(
        info, None, probe, complete_months=4, weekend_estimate=14
    )
    assert tier == "EXCLUDED"
    assert "no_nasdaq_or_nyse_listing_match" in reasons


def test_kline_audit_detects_duplicates_and_gaps_without_interpolation(tmp_path) -> None:
    start = 1_700_000_000_000
    path = tmp_path / "klines.csv"
    _write_klines(
        path,
        [
            _kline(start),
            _kline(start),
            _kline(start + 180_000),
        ],
    )

    audit = audit_klines(path, start_ms=start, end_ms=start + 240_000)

    assert audit["status"] == "DATA_INVALID"
    assert audit["duplicate_timestamps"] == 1
    assert audit["interval_gaps"] == 1
    assert audit["missing_minutes"] == 2


def test_funding_audit_keeps_real_settlement_intervals(tmp_path) -> None:
    start = 1_700_000_000_000
    path = tmp_path / "funding.json"
    path.write_text(
        json.dumps(
            {
                "events": [
                    {"funding_time": start, "funding_rate": "0.0001"},
                    {"funding_time": start + 8 * 3_600_000, "funding_rate": "-0.0002"},
                ]
            }
        ),
        encoding="utf-8",
    )

    audit = audit_funding(
        path,
        start_ms=start,
        end_ms=start + 16 * 3_600_000,
    )

    assert audit["status"] == "PASS"
    assert audit["event_count"] == 2
    assert audit["interval_hours"] == 8.0
