from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import timedelta

import pytest

import scripts.freeze_round26_quarterly_calendar_spread as freeze
from scripts.cross_era_round13_diagnose import _sha256


def _archive(symbol: str, month: str, rows: list[list[object]]) -> bytes:
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(freeze.EXPECTED_HEADER)
    writer.writerows(rows)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{symbol}-1h-{month}.csv", stream.getvalue())
    return payload.getvalue()


def _row(open_time: int, price: float) -> list[object]:
    return [
        open_time,
        price,
        price + 1,
        price - 1,
        price + 0.5,
        1,
        open_time + 3_599_999,
        1,
        1,
        1,
        1,
        0,
    ]


def test_audit_and_protocol_are_frozen() -> None:
    assert _sha256(freeze.AUDIT_PATH.resolve()) == freeze.AUDIT_SHA256
    assert _sha256(freeze.PROTOCOL_PATH.resolve()) == freeze.PROTOCOL_SHA256


def test_weekly_windows_and_contract_roll_are_fixed() -> None:
    windows = freeze._weekly_windows()

    assert len(windows) == 206
    assert sum(item["role"] == "DEVELOPMENT" for item in windows) == 67
    assert sum(item["role"] == "VALIDATION" for item in windows) == 48
    assert sum(item["role"] == "POSTHISTORY" for item in windows) == 91
    assert windows[0]["contracts"] == {
        "BTC": "BTCUSDT_210326",
        "ETH": "ETHUSDT_210326",
    }
    assert not any(
        item["window_end"].strftime("%Y%m%d") == "20210326" for item in windows
    )
    first_after_expiry = next(
        item for item in windows if item["window_start"].strftime("%Y%m%d") == "20210326"
    )
    assert first_after_expiry["contracts"] == {
        "BTC": "BTCUSDT_210625",
        "ETH": "ETHUSDT_210625",
    }
    assert len(freeze._excluded_roll_windows()) == 18


def test_each_asset_requires_exactly_122_authorized_archives() -> None:
    windows = freeze._weekly_windows()

    for asset in freeze.ASSETS:
        required = freeze._required_times(asset, windows)
        assert len(required) == 120
        assert not any(
            "2023-07" <= month <= "2024-07" for _symbol, month in required
        )


def test_parse_checksum_requires_matching_filename() -> None:
    digest = "a" * 64
    assert freeze._parse_checksum(f"{digest}  sample.zip", "sample.zip") == digest
    with pytest.raises(ValueError, match="文件名不一致"):
        freeze._parse_checksum(f"{digest}  other.zip", "sample.zip")


def test_parse_archive_normalizes_microseconds_and_selects_required_rows() -> None:
    month = "2021-02"
    start_ms, _end_ms = freeze._month_bounds(month)
    symbol = "BTCUSDT"
    rows = [_row((start_ms + hour * 3_600_000) * 1_000, 30_000 + hour) for hour in range(3)]
    payload = _archive(symbol, month, rows)
    source_sha = hashlib.sha256(payload).hexdigest()

    parsed = freeze._parse_archive(
        symbol,
        month,
        payload,
        source_sha256=source_sha,
        required_times={start_ms, start_ms + 2 * 3_600_000},
    )

    assert parsed["row_count"] == 3
    assert parsed["selected_row_count"] == 2
    assert parsed["timestamp_normalized_row_count"] == 3
    assert parsed["rows"][start_ms]["open"] == pytest.approx(30_000)


def test_parse_archive_rejects_missing_required_hour() -> None:
    month = "2021-02"
    start_ms, _end_ms = freeze._month_bounds(month)
    symbol = "ETHUSDT"
    payload = _archive(symbol, month, [_row(start_ms, 1_500)])

    with pytest.raises(RuntimeError, match="缺少 1 个冻结窗口整点"):
        freeze._parse_archive(
            symbol,
            month,
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            required_times={start_ms, start_ms + int(timedelta(hours=1).total_seconds() * 1000)},
        )


def test_parse_archive_records_only_non_required_invalid_ohlc() -> None:
    month = "2021-02"
    start_ms, _end_ms = freeze._month_bounds(month)
    symbol = "BTCUSDT_210326"
    invalid = _row(start_ms, 30_000)
    invalid[2] = 29_999
    invalid[4] = 31_000
    payload = _archive(
        symbol,
        month,
        [invalid, _row(start_ms + 3_600_000, 30_100)],
    )

    parsed = freeze._parse_archive(
        symbol,
        month,
        payload,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        required_times={start_ms + 3_600_000},
    )

    assert parsed["invalid_ohlc_row_count"] == 1
    assert parsed["invalid_ohlc_rows"][0]["open_time"] == start_ms
    with pytest.raises(ValueError, match="授权窗口整点 OHLC 无效"):
        freeze._parse_archive(
            symbol,
            month,
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
            required_times={start_ms},
        )
