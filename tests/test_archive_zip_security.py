from __future__ import annotations

import io
import zipfile

import pytest

from data_sources.base import DataSourceError
from data_sources.archive_checksum import (
    parse_checksum_file,
    sha256_hexdigest,
    verify_official_checksum,
)
from data_sources.archive_zip_reader import read_archive_klines


CSV_NAME = "BTCUSDT-1m-2026-04-01.csv"


def _kline_line(open_time: int, close: float = 100.0) -> str:
    return (
        f"{open_time},{close},{close + 1},{close - 1},{close},"
        f"12.5,{open_time + 59_999},1250,9,0,0,0"
    )


def _make_zip(
    *,
    csv_name: str = CSV_NAME,
    body: str | None = None,
    extra_files: dict[str, str] | None = None,
) -> bytes:
    if body is None:
        body = "\n".join(_kline_line(1_800_000_000_000 + i * 60_000) for i in range(3))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(csv_name, body)
        for name, content in (extra_files or {}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_reads_valid_headerless_archive() -> None:
    rows = read_archive_klines(
        _make_zip(), expected_csv_name=CSV_NAME, max_uncompressed_bytes=10_000_000
    )
    assert [row.open_time for row in rows] == [
        1_800_000_000_000,
        1_800_000_000_000 + 60_000,
        1_800_000_000_000 + 120_000,
    ]


def test_reads_archive_with_header_row() -> None:
    body = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n"
    body += _kline_line(1_800_000_000_000)
    rows = read_archive_klines(
        _make_zip(body=body), expected_csv_name=CSV_NAME, max_uncompressed_bytes=10_000_000
    )
    assert len(rows) == 1


def test_rejects_multiple_csv() -> None:
    data = _make_zip(extra_files={"BTCUSDT-1m-2026-04-02.csv": _kline_line(1)})
    with pytest.raises(DataSourceError, match="只允许一个"):
        read_archive_klines(data, expected_csv_name=CSV_NAME, max_uncompressed_bytes=10_000_000)


def test_rejects_name_mismatch() -> None:
    data = _make_zip(csv_name="EVIL-1m-2026-04-01.csv")
    with pytest.raises(DataSourceError, match="名不匹配"):
        read_archive_klines(data, expected_csv_name=CSV_NAME, max_uncompressed_bytes=10_000_000)


def test_rejects_zip_slip_path() -> None:
    data = _make_zip(csv_name="../../etc/evil.csv")
    with pytest.raises(DataSourceError):
        read_archive_klines(data, expected_csv_name=CSV_NAME, max_uncompressed_bytes=10_000_000)


def test_rejects_zip_bomb_over_uncompressed_limit() -> None:
    body = "\n".join(_kline_line(1_800_000_000_000 + i * 60_000) for i in range(1000))
    with pytest.raises(DataSourceError, match="解压体积"):
        read_archive_klines(
            _make_zip(body=body), expected_csv_name=CSV_NAME, max_uncompressed_bytes=100
        )


def test_rejects_bad_column_count() -> None:
    data = _make_zip(body="1,2,3\n")
    with pytest.raises(DataSourceError, match="列数不足"):
        read_archive_klines(data, expected_csv_name=CSV_NAME, max_uncompressed_bytes=10_000_000)


def test_rejects_corrupt_zip() -> None:
    with pytest.raises(DataSourceError, match="损坏"):
        read_archive_klines(b"not a zip", expected_csv_name=CSV_NAME, max_uncompressed_bytes=10_000)


def test_checksum_parse_and_verify_roundtrip() -> None:
    data = _make_zip()
    digest = sha256_hexdigest(data)
    content = f"{digest}  {CSV_NAME.replace('.csv', '.zip')}\n"
    assert parse_checksum_file(content) == digest
    assert verify_official_checksum(data, content) == digest


def test_checksum_mismatch_raises() -> None:
    content = "0" * 64 + "  file.zip\n"
    with pytest.raises(DataSourceError, match="不匹配"):
        verify_official_checksum(_make_zip(), content)


def test_checksum_empty_file_raises() -> None:
    with pytest.raises(DataSourceError, match="为空"):
        parse_checksum_file("   \n")


def test_checksum_invalid_digest_raises() -> None:
    with pytest.raises(DataSourceError, match="合法"):
        parse_checksum_file("not-a-sha  file.zip\n")
