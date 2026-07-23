from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

import scripts.freeze_round22_funding_archive as freeze
from scripts.cross_era_round13_diagnose import _sha256


def _archive(symbol: str, month: str, rows: list[str]) -> bytes:
    buffer = io.BytesIO()
    name = f"{symbol}-fundingRate-{month}.csv"
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            name,
            "calc_time,funding_interval_hours,last_funding_rate\n"
            + "\n".join(rows)
            + "\n",
        )
    return buffer.getvalue()


def test_data_protocol_hash_is_frozen() -> None:
    assert _sha256(freeze.PROTOCOL_PATH.resolve()) == freeze.PROTOCOL_SHA256


def test_authorized_months_exclude_entire_final_oos_buffer() -> None:
    months = freeze._authorized_months()

    assert len(months) == 65
    assert months[0] == "2020-01"
    assert months[-1] == "2026-06"
    assert "2023-07" not in months
    assert "2024-07" not in months
    assert "2024-08" in months


def test_parse_checksum_requires_matching_filename() -> None:
    checksum = "a" * 64

    assert freeze._parse_checksum(f"{checksum}  file.zip", "file.zip") == checksum
    with pytest.raises(ValueError, match="文件名不一致"):
        freeze._parse_checksum(f"{checksum}  other.zip", "file.zip")


def test_parse_archive_validates_schema_and_month() -> None:
    payload = _archive(
        "BTCUSDT",
        "2020-01",
        [
            "1577836800000,8,-0.00012359",
            "1577865600000,8,0.00010000",
        ],
    )
    sha = hashlib.sha256(payload).hexdigest()

    events = freeze._parse_archive(
        "BTCUSDT",
        "2020-01",
        payload,
        source_sha256=sha,
    )

    assert [item["funding_time"] for item in events] == [
        1577836800000,
        1577865600000,
    ]
    assert all(item["source_zip_sha256"] == sha for item in events)


def test_parse_archive_rejects_event_outside_named_month() -> None:
    payload = _archive(
        "ETHUSDT",
        "2020-01",
        ["1580515200000,8,0.00010000"],
    )

    with pytest.raises(ValueError, match="月份外"):
        freeze._parse_archive(
            "ETHUSDT",
            "2020-01",
            payload,
            source_sha256=hashlib.sha256(payload).hexdigest(),
        )
