from __future__ import annotations

import csv
import hashlib
import io
import zipfile

import pytest

import scripts.freeze_round27_absolute_trend as freeze
from scripts.cross_era_round13_diagnose import _sha256


def _row(open_time: int, price: float) -> list[object]:
    return [
        open_time,
        price,
        price + 1,
        price - 1,
        price + 0.5,
        1,
        open_time + freeze.HOUR_MS - 1,
        1,
        1,
        1,
        1,
        0,
    ]


def _archive(symbol: str, month: str, rows: list[list[object]]) -> bytes:
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(freeze.EXPECTED_HEADER)
    writer.writerows(rows)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{symbol}-1h-{month}.csv", stream.getvalue())
    return payload.getvalue()


def _complete_month(symbol: str, month: str, *, micros: bool = False) -> bytes:
    start_ms, end_ms = freeze._month_bounds(month)
    rows = []
    for index, open_time in enumerate(range(start_ms, end_ms, freeze.HOUR_MS)):
        timestamp = open_time * 1_000 if micros else open_time
        rows.append(_row(timestamp, 1_000 + index))
    return _archive(symbol, month, rows)


def test_audit_and_protocol_are_frozen() -> None:
    assert _sha256(freeze.AUDIT_PATH.resolve()) == freeze.AUDIT_SHA256
    assert _sha256(freeze.PROTOCOL_PATH.resolve()) == freeze.PROTOCOL_SHA256


def test_authorized_months_are_exact_and_exclude_final_oos() -> None:
    months = freeze._authorized_months()

    assert len(months) == 59
    assert months[:2] == ("2020-07", "2020-08")
    assert months[-2:] == ("2026-05", "2026-06")
    assert "2023-07" not in months
    assert "2024-07" not in months
    assert "2024-08" in months


def test_parse_checksum_requires_matching_filename() -> None:
    digest = "a" * 64

    assert freeze._parse_checksum(f"{digest}  file.zip", "file.zip") == digest
    with pytest.raises(ValueError, match="文件名不一致"):
        freeze._parse_checksum(f"{digest}  other.zip", "file.zip")


def test_parse_archive_accepts_complete_month_and_normalizes_microseconds() -> None:
    symbol = "BTCUSDT"
    month = "2021-02"
    payload = _complete_month(symbol, month, micros=True)
    source_sha = hashlib.sha256(payload).hexdigest()

    parsed = freeze._parse_archive(
        symbol, month, payload, source_sha256=source_sha
    )

    assert parsed["row_count"] == 28 * 24
    assert parsed["timestamp_normalized_row_count"] == 28 * 24
    assert parsed["rows"][0]["segment"] == "HISTORY"
    assert parsed["rows"][0]["source_zip_sha256"] == source_sha


def test_parse_archive_rejects_missing_hour() -> None:
    symbol = "ETHUSDT"
    month = "2021-02"
    start_ms, end_ms = freeze._month_bounds(month)
    rows = [
        _row(open_time, 1_000 + index)
        for index, open_time in enumerate(range(start_ms, end_ms, freeze.HOUR_MS))
        if index != 10
    ]
    payload = _archive(symbol, month, rows)

    with pytest.raises(ValueError, match="不连续"):
        freeze._parse_archive(
            symbol,
            month,
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
        )


def test_parse_archive_rejects_invalid_ohlc() -> None:
    symbol = "BTCUSDT"
    month = "2021-02"
    start_ms, end_ms = freeze._month_bounds(month)
    rows = [
        _row(open_time, 1_000 + index)
        for index, open_time in enumerate(range(start_ms, end_ms, freeze.HOUR_MS))
    ]
    rows[15][2] = rows[15][1] - 1
    payload = _archive(symbol, month, rows)

    with pytest.raises(ValueError, match="OHLC 关系无效"):
        freeze._parse_archive(
            symbol,
            month,
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
        )
