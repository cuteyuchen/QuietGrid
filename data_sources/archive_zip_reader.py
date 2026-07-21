"""安全读取 Binance 官方归档 ZIP，并将其中的 K 线 CSV 标准化。

归档 ZIP 是不受信任的远程输入，这里针对 Zip Slip、Zip Bomb、多余文件、
文件名伪造与 CRC 错误进行硬校验，只允许唯一一个预期文件名的 CSV。
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator

from data_sources.base import DataSourceError
from data_sources.models import NormalizedKline


# Binance kline CSV 的固定列顺序（无表头版本）。
_OPEN_TIME = 0
_OPEN = 1
_HIGH = 2
_LOW = 3
_CLOSE = 4
_VOLUME = 5
_CLOSE_TIME = 6
_QUOTE_VOLUME = 7
_TRADE_COUNT = 8
_MIN_COLUMNS = 9
_HEADER_FIRST_FIELD = "open_time"


def read_archive_klines(
    data: bytes,
    *,
    expected_csv_name: str,
    max_uncompressed_bytes: int,
) -> list[NormalizedKline]:
    """校验并解压归档 ZIP，返回按 open_time 升序的标准 K 线。"""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DataSourceError("归档 ZIP 损坏或不是合法的 ZIP。") from exc

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if len(entries) != 1:
            raise DataSourceError(
                f"归档 ZIP 只允许一个 CSV，实际包含 {len(entries)} 个文件。"
            )
        info = entries[0]
        entry_name = info.filename
        if "/" in entry_name or "\\" in entry_name or entry_name.startswith(".."):
            raise DataSourceError(f"归档 ZIP 存在非法路径条目: {entry_name}")
        if entry_name != expected_csv_name:
            raise DataSourceError(
                f"归档 CSV 名不匹配：期望 {expected_csv_name}，实际 {entry_name}。"
            )
        if info.file_size > max_uncompressed_bytes:
            raise DataSourceError(
                f"归档解压体积 {info.file_size} 超过上限 {max_uncompressed_bytes}。"
            )
        try:
            raw = _read_entry_bounded(archive, info, max_uncompressed_bytes)
        except zipfile.BadZipFile as exc:
            raise DataSourceError("归档 CSV CRC 校验失败。") from exc

    return _parse_csv_rows(raw)


def _read_entry_bounded(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    max_uncompressed_bytes: int,
) -> bytes:
    with archive.open(info, "r") as handle:
        # 读到上限 + 1 字节即可判断是否超限，避免声明大小造假导致 Zip Bomb。
        raw = handle.read(max_uncompressed_bytes + 1)
    if len(raw) > max_uncompressed_bytes:
        raise DataSourceError(
            f"归档解压体积超过上限 {max_uncompressed_bytes}。"
        )
    return raw


def _parse_csv_rows(raw: bytes) -> list[NormalizedKline]:
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    rows: list[NormalizedKline] = []
    for line_number, fields in _skip_optional_header(reader):
        if not fields:
            continue
        if len(fields) < _MIN_COLUMNS:
            raise DataSourceError(
                f"归档 CSV 第 {line_number} 行列数不足: {len(fields)}"
            )
        rows.append(_row_to_kline(fields, line_number))
    if not rows:
        raise DataSourceError("归档 CSV 没有数据行。")
    return rows


def _skip_optional_header(
    reader: Iterator[list[str]],
) -> Iterator[tuple[int, list[str]]]:
    for line_number, fields in enumerate(reader, start=1):
        if line_number == 1 and fields and fields[0].strip().lower() == _HEADER_FIRST_FIELD:
            continue
        yield line_number, fields


def _row_to_kline(fields: list[str], line_number: int) -> NormalizedKline:
    try:
        return NormalizedKline(
            open_time=_timestamp_ms(fields[_OPEN_TIME]),
            open=float(fields[_OPEN]),
            high=float(fields[_HIGH]),
            low=float(fields[_LOW]),
            close=float(fields[_CLOSE]),
            volume=float(fields[_VOLUME]),
            close_time=_timestamp_ms(fields[_CLOSE_TIME]),
            quote_volume=float(fields[_QUOTE_VOLUME]),
            trade_count=int(float(fields[_TRADE_COUNT])),
        )
    except (TypeError, ValueError) as exc:
        raise DataSourceError(f"归档 CSV 第 {line_number} 行字段无效: {exc}") from exc


def _timestamp_ms(value: str) -> int:
    try:
        timestamp = int(value.strip())
    except ValueError:
        timestamp = int(float(value))
    if timestamp >= 100_000_000_000_000:
        timestamp //= 1_000
    if not 100_000_000_000 <= timestamp < 100_000_000_000_000:
        raise ValueError("时间戳必须为 Unix 毫秒或微秒")
    return timestamp
