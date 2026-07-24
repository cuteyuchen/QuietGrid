from __future__ import annotations

import csv
import hashlib
import io
import zipfile

import pytest

import scripts.freeze_round28_spot_quarterly_carry as freeze
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


def test_audit_and_protocol_are_frozen() -> None:
    assert _sha256(freeze.AUDIT_PATH.resolve()) == freeze.AUDIT_SHA256
    assert _sha256(freeze.PROTOCOL_PATH.resolve()) == freeze.PROTOCOL_SHA256


def test_windows_and_required_archives_are_fixed() -> None:
    windows = freeze._windows()

    assert len(windows) == 16
    assert sum(item["role"] == "DEVELOPMENT" for item in windows) == 5
    assert sum(item["role"] == "VALIDATION" for item in windows) == 3
    assert sum(item["role"] == "POSTHISTORY" for item in windows) == 8
    assert windows[0]["entry_time"].isoformat() == "2021-05-26T08:00:00+00:00"
    assert windows[0]["end_time"].isoformat() == "2021-06-25T07:00:00+00:00"
    for asset in freeze.ASSETS:
        required = freeze._required_archives(asset, windows)
        assert len(required) == 62
        assert sum(key[0] == "SPOT" for key in required) == 31
        assert sum(key[0] == "QUARTERLY" for key in required) == 31
        assert not any("2023-07" <= key[2] <= "2024-07" for key in required)


def test_parse_spot_archive_requires_full_month_and_normalizes_microseconds() -> None:
    symbol = "BTCUSDT"
    month = "2021-02"
    start_ms, end_ms = freeze._month_bounds(month)
    rows = [
        _row(open_time * 1_000, 1_000 + index)
        for index, open_time in enumerate(range(start_ms, end_ms, freeze.HOUR_MS))
    ]
    payload = _archive(symbol, month, rows)
    required = {start_ms, start_ms + freeze.HOUR_MS}

    parsed = freeze._parse_archive(
        "SPOT",
        symbol,
        month,
        payload,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        required_times=required,
    )

    assert parsed["row_count"] == 28 * 24
    assert parsed["selected_row_count"] == 2
    assert parsed["timestamp_normalized_row_count"] == 28 * 24


def test_parse_quarterly_archive_allows_partial_month() -> None:
    symbol = "BTCUSDT_210326"
    month = "2021-03"
    start_ms, _end_ms = freeze._month_bounds(month)
    times = [start_ms + index * freeze.HOUR_MS for index in range(48)]
    payload = _archive(symbol, month, [_row(value, 1_100 + index) for index, value in enumerate(times)])

    parsed = freeze._parse_archive(
        "QUARTERLY",
        symbol,
        month,
        payload,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        required_times=set(times[-2:]),
    )

    assert parsed["row_count"] == 48
    assert parsed["selected_row_count"] == 2


def test_parse_archive_rejects_missing_required_hour() -> None:
    symbol = "ETHUSDT_210326"
    month = "2021-03"
    start_ms, _end_ms = freeze._month_bounds(month)
    payload = _archive(symbol, month, [_row(start_ms, 1_000)])

    with pytest.raises(RuntimeError, match="缺少 1 个授权窗口整点"):
        freeze._parse_archive(
            "QUARTERLY",
            symbol,
            month,
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            required_times={start_ms, start_ms + freeze.HOUR_MS},
        )


def test_parse_archive_rejects_invalid_ohlc() -> None:
    symbol = "ETHUSDT_210326"
    month = "2021-03"
    start_ms, _end_ms = freeze._month_bounds(month)
    row = _row(start_ms, 1_000)
    row[2] = 999
    row[4] = 1_001
    payload = _archive(symbol, month, [row])

    with pytest.raises(ValueError, match="OHLC 关系无效"):
        freeze._parse_archive(
            "QUARTERLY",
            symbol,
            month,
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            required_times={start_ms},
        )
