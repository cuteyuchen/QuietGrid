from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.freeze_round23_premium_index_archive as freeze
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _kline(open_time: int, close: str = "-0.0004") -> str:
    return (
        f"{open_time},0,0,0,{close},0,{open_time + 59_999},0,0,0,0,0"
    )


def _archive(
    symbol: str,
    month: str,
    rows: list[str],
    *,
    header: bool,
) -> bytes:
    buffer = io.BytesIO()
    name = f"{symbol}-1m-{month}.csv"
    body = "\n".join(rows) + "\n"
    if header:
        body = ",".join(freeze.EXPECTED_HEADER) + "\n" + body
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, body)
    return buffer.getvalue()


def _window(window_id: str, start_ms: int, row_count: int) -> dict[str, object]:
    market_close = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    force_close_at = market_close + timedelta(minutes=row_count)
    return {
        "role": "TEST",
        "split": "external",
        "window_id": window_id,
        "market_close": market_close,
        "force_close_at": force_close_at,
        "start_ms": start_ms,
        "end_ms": start_ms + row_count * 60_000,
        "expected_row_count": row_count,
    }


def test_data_protocol_and_audit_hashes_are_frozen() -> None:
    assert _sha256(freeze.PROTOCOL_PATH.resolve()) == freeze.PROTOCOL_SHA256
    assert _sha256(freeze.AUDIT_PATH.resolve()) == freeze.AUDIT_SHA256


def test_authorized_months_exclude_entire_final_oos_buffer() -> None:
    months = freeze._authorized_months()

    assert len(months) == 65
    assert months[0] == "2020-01"
    assert months[-1] == "2026-06"
    assert "2023-07" not in months
    assert "2024-07" not in months
    assert "2024-08" in months


def test_actual_authorized_windows_remain_disjoint_from_final_oos() -> None:
    windows, _isolation = freeze._load_authorized_windows(
        Path(
            "reports/cross-era-oos/"
            "round12-quadratic-volatility-defense-results.json"
        ),
        Path(
            "reports/cross-era-oos/"
            "round13-prehistory-quadratic-w2160-results.json"
        ),
    )

    assert len(windows) == 293
    counts: dict[str, int] = {}
    for item in windows:
        key = f"{item['role']}_{item['split']}"
        counts[key] = counts.get(key, 0) + 1
    assert counts == freeze.EXPECTED_WINDOW_COUNTS
    excluded_start, _ = freeze._month_bounds(freeze.EXCLUDED_START)
    _, excluded_end = freeze._month_bounds(freeze.EXCLUDED_END)
    assert all(
        int(item["end_ms"]) <= excluded_start
        or int(item["start_ms"]) >= excluded_end
        for item in windows
    )


def test_parse_checksum_requires_matching_filename() -> None:
    checksum = "a" * 64

    assert freeze._parse_checksum(f"{checksum}  file.zip", "file.zip") == checksum
    with pytest.raises(ValueError, match="文件名不一致"):
        freeze._parse_checksum(f"{checksum}  other.zip", "file.zip")


@pytest.mark.parametrize("header", [False, True])
def test_parse_archive_accepts_headerless_and_standard_header(
    monkeypatch: pytest.MonkeyPatch,
    header: bool,
) -> None:
    start_ms = 1_577_836_800_000
    monkeypatch.setattr(
        freeze,
        "_month_bounds",
        lambda _month: (start_ms, start_ms + 3 * 60_000),
    )
    payload = _archive(
        "BTCUSDT",
        "2020-01",
        [_kline(start_ms + index * 60_000) for index in range(3)],
        header=header,
    )
    digest = hashlib.sha256(payload).hexdigest()

    parsed = freeze._parse_archive(
        "BTCUSDT",
        "2020-01",
        payload,
        source_sha256=digest,
        windows=[_window("w1", start_ms, 3)],
    )

    assert parsed["full_row_count"] == 3
    assert parsed["selected_row_count"] == 3
    assert parsed["header_present"] is header
    assert [item[1] for item in parsed["rows"]] == [
        start_ms,
        start_ms + 60_000,
        start_ms + 120_000,
    ]


def test_parse_archive_records_missing_minute_without_interpolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_ms = 1_577_836_800_000
    monkeypatch.setattr(
        freeze,
        "_month_bounds",
        lambda _month: (start_ms, start_ms + 3 * 60_000),
    )
    payload = _archive(
        "ETHUSDT",
        "2020-01",
        [_kline(start_ms), _kline(start_ms + 120_000)],
        header=False,
    )

    parsed = freeze._parse_archive(
        "ETHUSDT",
        "2020-01",
        payload,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        windows=[_window("w1", start_ms, 3)],
    )

    assert parsed["missing_minute_count"] == 1
    assert parsed["gap_ranges"] == [
        {
            "start_ms": start_ms + 60_000,
            "end_ms": start_ms + 120_000,
            "missing_minutes": 1,
        }
    ]


def test_freeze_symbol_requires_complete_window_and_writes_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_ms = 1_577_836_800_000
    window = _window("w1", start_ms, 3)
    source_sha = "b" * 64
    daily_sha = "d" * 64
    monthly = [
        {
            "month": "2020-01",
            "url": "https://example.test/file.zip",
            "zip_sha256": source_sha,
            "full_row_count": 2,
            "missing_minute_count": 1,
            "gap_ranges": [
                {
                    "start_ms": start_ms + 60_000,
                    "end_ms": start_ms + 120_000,
                    "missing_minutes": 1,
                }
            ],
            "selected_row_count": 2,
            "first_open_time": start_ms,
            "last_open_time": start_ms + 120_000,
            "header_present": False,
            "timestamp_normalized_row_count": 0,
            "official_checksum_verified": True,
            "rows": [
                (
                    "w1",
                    start_ms + index * 120_000,
                    -0.0004,
                    "2020-01",
                    "monthly",
                    "2020-01",
                    source_sha,
                )
                for index in range(2)
            ],
        }
    ]
    daily = [
        {
            "day": "2020-01-01",
            "url": "https://example.test/day.zip",
            "http_status": 200,
            "zip_sha256": daily_sha,
            "row_count": 1,
            "selected_row_count": 1,
            "first_open_time": start_ms + 60_000,
            "last_open_time": start_ms + 60_000,
            "header_present": False,
            "timestamp_normalized_row_count": 0,
            "official_checksum_verified": True,
            "missing_minute_count": 1_439,
            "gap_ranges": [],
            "open_times": [start_ms + 60_000],
            "rows": [
                (
                    "w1",
                    start_ms + 60_000,
                    -0.0004,
                    "2020-01",
                    "daily",
                    "2020-01-01",
                    daily_sha,
                )
            ],
        }
    ]
    monkeypatch.setattr(freeze, "_authorized_months", lambda: ("2020-01",))
    monkeypatch.setattr(freeze, "EXPECTED_MONTHLY_MISSING_MINUTES", {"BTCUSDT": 1})
    monkeypatch.setattr(freeze, "EXPECTED_DAILY_RECOVERED_MINUTES", {"BTCUSDT": 1})
    monkeypatch.setattr(
        freeze,
        "EXPECTED_SOURCE_REMAINING_MISSING_MINUTES",
        {"BTCUSDT": 0},
    )
    monkeypatch.setattr(freeze, "EXCLUDED_INCOMPLETE_WINDOWS", {})
    monkeypatch.setattr(
        freeze,
        "EXPECTED_FROZEN_WINDOW_COUNTS",
        {"TEST_external": 1},
    )
    monkeypatch.setattr(freeze, "EXPECTED_FROZEN_WINDOW_COUNT", 1)

    result = freeze._freeze_symbol(
        tmp_path,
        "BTCUSDT",
        monthly,
        daily,
        [window],
        current_isolation={"final_oos_status": "SEALED_NOT_EVALUATED"},
    )

    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert result["row_count"] == 3
    assert manifest["authorized_windows_complete"] is True
    assert manifest["official_monthly_checksums_verified"] is True
    assert manifest["available_daily_checksums_verified"] is True
    assert manifest["windows"][0]["complete"] is True


def test_freeze_symbol_rejects_incomplete_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_ms = 1_577_836_800_000
    window = _window("w1", start_ms, 3)
    source_sha = "c" * 64
    monthly = [
        {
            "month": "2020-01",
            "url": "https://example.test/file.zip",
            "zip_sha256": source_sha,
            "full_row_count": 2,
            "missing_minute_count": 1,
            "gap_ranges": [
                {
                    "start_ms": start_ms + 120_000,
                    "end_ms": start_ms + 180_000,
                    "missing_minutes": 1,
                }
            ],
            "selected_row_count": 2,
            "first_open_time": start_ms,
            "last_open_time": start_ms + 60_000,
            "header_present": False,
            "timestamp_normalized_row_count": 0,
            "official_checksum_verified": True,
            "rows": [
                (
                    "w1",
                    start_ms + index * 60_000,
                    -0.0004,
                    "2020-01",
                    "monthly",
                    "2020-01",
                    source_sha,
                )
                for index in range(2)
            ],
        }
    ]
    monkeypatch.setattr(freeze, "_authorized_months", lambda: ("2020-01",))
    monkeypatch.setattr(freeze, "EXPECTED_MONTHLY_MISSING_MINUTES", {"BTCUSDT": 1})
    monkeypatch.setattr(freeze, "EXPECTED_DAILY_RECOVERED_MINUTES", {"BTCUSDT": 0})
    monkeypatch.setattr(
        freeze,
        "EXPECTED_SOURCE_REMAINING_MISSING_MINUTES",
        {"BTCUSDT": 1},
    )
    monkeypatch.setattr(freeze, "EXCLUDED_INCOMPLETE_WINDOWS", {})
    monkeypatch.setattr(
        freeze,
        "EXPECTED_FROZEN_WINDOW_COUNTS",
        {"TEST_external": 1},
    )
    monkeypatch.setattr(freeze, "EXPECTED_FROZEN_WINDOW_COUNT", 1)

    with pytest.raises(RuntimeError, match="仍缺"):
        freeze._freeze_symbol(
            tmp_path,
            "BTCUSDT",
            monthly,
            [],
            [window],
            current_isolation={"final_oos_status": "SEALED_NOT_EVALUATED"},
        )
