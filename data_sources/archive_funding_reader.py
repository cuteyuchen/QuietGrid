"""安全读取 Binance USD-M 月度 fundingRate 归档。"""

from __future__ import annotations

import csv
import io
import zipfile

from data_sources.base import DataSourceError
from data_sources.models import FundingEvent


def read_archive_funding(
    data: bytes,
    *,
    expected_csv_name: str,
    max_uncompressed_bytes: int,
) -> list[FundingEvent]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DataSourceError("Binance 资金费归档不是有效 ZIP。") from exc

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if len(entries) != 1:
            raise DataSourceError("Binance 资金费归档必须且只能包含一个 CSV。")
        info = entries[0]
        if info.filename != expected_csv_name:
            raise DataSourceError(
                f"Binance 资金费归档文件名不匹配: {info.filename}"
            )
        if info.file_size > max_uncompressed_bytes:
            raise DataSourceError("Binance 资金费归档解压后超过安全上限。")
        with archive.open(info, "r") as handle:
            raw = handle.read(max_uncompressed_bytes + 1)
        if len(raw) > max_uncompressed_bytes:
            raise DataSourceError("Binance 资金费归档解压后超过安全上限。")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataSourceError("Binance 资金费归档不是 UTF-8 CSV。") from exc
    reader = csv.DictReader(io.StringIO(text))
    expected = {"calc_time", "last_funding_rate"}
    if not reader.fieldnames or not expected.issubset(reader.fieldnames):
        raise DataSourceError("Binance 资金费归档缺少必要字段。")

    events: list[FundingEvent] = []
    for line_no, row in enumerate(reader, start=2):
        try:
            events.append(
                FundingEvent(
                    funding_time=int(row["calc_time"]),
                    funding_rate=float(row["last_funding_rate"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DataSourceError(
                f"Binance 资金费归档第 {line_no} 行字段无效。"
            ) from exc
    events.sort(key=lambda item: item.funding_time)
    return events
