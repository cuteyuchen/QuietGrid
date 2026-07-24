from __future__ import annotations

import csv
import hashlib
import io
import zipfile

import pytest

import scripts.freeze_round29_order_flow as freeze
from scripts.cross_era_round13_diagnose import _sha256


def _row(open_time: int, price: float, imbalance: float = 0.2) -> list[object]:
    quote = 100.0
    taker_quote = quote * (1 + imbalance) / 2
    return [open_time, price, price + 1, price - 1, price + 0.5, 1, open_time + freeze.HOUR_MS - 1, quote, 1, taker_quote, taker_quote, 0]


def _archive(symbol: str, month: str, rows: list[list[object]]) -> bytes:
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(freeze.EXPECTED_HEADER)
    writer.writerows(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{symbol}-1h-{month}.csv", stream.getvalue())
    return out.getvalue()


def test_protocol_hashes_are_frozen() -> None:
    assert _sha256(freeze.AUDIT_PATH.resolve()) == freeze.AUDIT_SHA256
    assert _sha256(freeze.PROTOCOL_PATH.resolve()) == freeze.PROTOCOL_SHA256


def test_authorized_months_are_59_and_exclude_isolation() -> None:
    months = freeze._months()
    assert len(months) == 59
    assert months[0] == "2020-07"
    assert months[-1] == "2026-06"
    assert "2023-07" not in months
    assert "2024-07" not in months


def test_parse_archive_preserves_order_flow_fields() -> None:
    month = "2021-02"
    start, end = freeze._month_bounds(month)
    rows = [_row(timestamp, 1_000 + index) for index, timestamp in enumerate(range(start, end, freeze.HOUR_MS))]
    payload = _archive("BTCUSDT", month, rows)
    parsed = freeze._parse_archive("BTCUSDT", month, payload, source_sha256=hashlib.sha256(payload).hexdigest())
    assert parsed["row_count"] == 28 * 24
    assert parsed["rows"][0]["quote_volume"] == pytest.approx(100.0)
    assert parsed["rows"][0]["taker_buy_quote_volume"] == pytest.approx(60.0)


def test_parse_archive_accepts_registered_flat_zero_volume_hour() -> None:
    month = "2021-02"
    start, end = freeze._month_bounds(month)
    rows = [_row(timestamp, 1_000 + index) for index, timestamp in enumerate(range(start, end, freeze.HOUR_MS))]
    rows[3][1:5] = [1_003, 1_003, 1_003, 1_003]
    rows[3][5] = 0
    rows[3][7] = 0
    rows[3][9] = 0
    rows[3][10] = 0
    payload = _archive("ETHUSDT", month, rows)
    parsed = freeze._parse_archive("ETHUSDT", month, payload, source_sha256=hashlib.sha256(payload).hexdigest())
    assert parsed["zero_volume_neutral_row_count"] == 1


def test_parse_archive_rejects_inconsistent_zero_quote_volume() -> None:
    month = "2021-02"
    start, end = freeze._month_bounds(month)
    rows = [_row(timestamp, 1_000 + index) for index, timestamp in enumerate(range(start, end, freeze.HOUR_MS))]
    rows[3][7] = 0
    payload = _archive("ETHUSDT", month, rows)
    with pytest.raises(ValueError, match="成交量字段无效"):
        freeze._parse_archive("ETHUSDT", month, payload, source_sha256=hashlib.sha256(payload).hexdigest())
