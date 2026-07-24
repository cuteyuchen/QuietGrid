from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from core.config import load_config
from data_sources.binance_source import _httpx_proxy_kwargs
from scripts.cross_era_round13_diagnose import _sha256
from scripts.freeze_round27_absolute_trend import (
    EXPECTED_HEADER,
    HOUR_MS,
    MAX_UNCOMPRESSED_BYTES,
    _month_bounds,
    _parse_checksum,
    _timestamp_ms,
)


UTC = timezone.utc
AUDIT_PATH = Path("reports/cross-era-oos/round29-order-flow-data-audit.md")
AUDIT_SHA256 = "a63320dc95d9f7a98c05b762731be049691beaa0cca2dc85a9586df7b737fee8"
PROTOCOL_PATH = Path("reports/cross-era-oos/round29-order-flow-data-protocol.md")
PROTOCOL_SHA256 = "46f28027acbafbe687dc14de7fd57203f61e06872a29d71a02766f82c637784d"
BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
INTERVAL = "1h"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
SEGMENTS = (
    ("HISTORY", "2020-07", "2023-06", 26_280),
    ("POSTHISTORY", "2024-08", "2026-06", 16_776),
)
EXCLUDED_START = "2023-07"
EXCLUDED_END = "2024-07"
EXPECTED_ARCHIVES_PER_SYMBOL = 59
EXPECTED_ROWS_PER_SYMBOL = 43_056


def _month_index(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m")
    return parsed.year * 12 + parsed.month - 1


def _month_value(index: int) -> str:
    year, month_zero = divmod(index, 12)
    return f"{year:04d}-{month_zero + 1:02d}"


def _months() -> tuple[str, ...]:
    values = tuple(
        month
        for _segment, start, end, _count in SEGMENTS
        for month in (
            _month_value(index)
            for index in range(_month_index(start), _month_index(end) + 1)
        )
    )
    if len(values) != EXPECTED_ARCHIVES_PER_SYMBOL or len(values) != len(set(values)):
        raise RuntimeError("Round 29 授权月份数量不一致。")
    if any(EXCLUDED_START <= month <= EXCLUDED_END for month in values):
        raise RuntimeError("Round 29 授权月份触碰隔离区间。")
    return values


def _segment_for_month(month: str) -> str:
    matching = [name for name, start, end, _count in SEGMENTS if start <= month <= end]
    if len(matching) != 1:
        raise ValueError(f"月份 {month} 不属于唯一授权段。")
    return matching[0]


def _archive_url(symbol: str, month: str) -> str:
    filename = f"{symbol}-{INTERVAL}-{month}.zip"
    return f"{BASE_URL}/{symbol}/{INTERVAL}/{filename}"


def _parse_archive(
    symbol: str,
    month: str,
    payload: bytes,
    *,
    source_sha256: str,
) -> dict[str, Any]:
    expected_csv = f"{symbol}-{INTERVAL}-{month}.csv"
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{symbol} {month} ZIP 无效。") from exc
    with archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) != 1:
            raise ValueError(f"{symbol} {month} ZIP 必须只包含一个 CSV。")
        info = entries[0]
        if (
            info.filename != expected_csv
            or "/" in info.filename
            or "\\" in info.filename
            or info.filename.startswith("..")
        ):
            raise ValueError(f"{symbol} {month} ZIP 内容不一致。")
        if info.file_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(f"{symbol} {month} CSV 解压体积超过上限。")
        try:
            raw = archive.read(info)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{symbol} {month} CSV CRC 校验失败。") from exc
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"{symbol} {month} CSV 实际解压体积超过上限。")

    start_ms, end_ms = _month_bounds(month)
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
    rows: list[dict[str, Any]] = []
    previous_time: int | None = None
    gap_count = 0
    normalized_count = 0
    header_present = False
    zero_volume_neutral_count = 0
    for line_number, fields in enumerate(reader, start=1):
        if not fields:
            continue
        if not rows and fields[0].strip().lower() == "open_time":
            if tuple(value.strip().lower() for value in fields) != EXPECTED_HEADER:
                raise ValueError(f"{symbol} {month} Kline 表头不一致。")
            header_present = True
            continue
        if len(fields) != len(EXPECTED_HEADER):
            raise ValueError(f"{symbol} {month} 第 {line_number} 行不是标准 12 列 Kline。")
        try:
            open_time, normalized = _timestamp_ms(fields[0])
            open_price, high, low, close = (float(fields[index]) for index in (1, 2, 3, 4))
            volume, quote_volume, taker_volume, taker_quote_volume = (
                float(fields[index]) for index in (5, 7, 9, 10)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{symbol} {month} 第 {line_number} 行字段无效。") from exc
        if open_time % HOUR_MS or not start_ms <= open_time < end_ms:
            raise ValueError(f"{symbol} {month} open_time 范围或整点对齐无效。")
        if previous_time is not None:
            if open_time <= previous_time:
                raise ValueError(f"{symbol} {month} open_time 未严格递增。")
            if open_time != previous_time + HOUR_MS:
                gap_count += (open_time - previous_time) // HOUR_MS - 1
        ohlc = (open_price, high, low, close)
        volume_fields = (volume, quote_volume, taker_volume, taker_quote_volume)
        invalid = (
            any(not math.isfinite(value) or value <= 0 for value in ohlc)
            or high < max(open_price, close)
            or low > min(open_price, close)
            or high < low
            or any(not math.isfinite(value) or value < 0 for value in volume_fields)
        )
        if quote_volume == 0:
            neutral_zero = (
                volume == 0
                and taker_volume == 0
                and taker_quote_volume == 0
                and open_price == high == low == close
            )
            if not neutral_zero:
                invalid = True
            else:
                zero_volume_neutral_count += 1
        elif taker_quote_volume > quote_volume + 1e-9:
            invalid = True
        if invalid:
            raise ValueError(f"{symbol} {month} 第 {line_number} 行 OHLC/成交量字段无效。")
        rows.append(
            {
                "segment": _segment_for_month(month),
                "open_time": open_time,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "quote_volume": quote_volume,
                "taker_buy_volume": taker_volume,
                "taker_buy_quote_volume": taker_quote_volume,
                "source_month": month,
                "source_zip_sha256": source_sha256,
            }
        )
        previous_time = open_time
        normalized_count += int(normalized)
    if not rows:
        raise ValueError(f"{symbol} {month} Kline 归档为空。")
    expected_rows = (end_ms - start_ms) // HOUR_MS
    if (
        len(rows) != expected_rows
        or rows[0]["open_time"] != start_ms
        or rows[-1]["open_time"] != end_ms - HOUR_MS
        or gap_count != 0
    ):
        raise ValueError(f"{symbol} {month} 授权 Kline 月档不完整。")
    return {
        "symbol": symbol,
        "month": month,
        "row_count": len(rows),
        "first_open_time": rows[0]["open_time"],
        "last_open_time": rows[-1]["open_time"],
        "header_present": header_present,
        "timestamp_normalized_row_count": normalized_count,
        "gap_count": gap_count,
        "invalid_row_count": 0,
        "zero_volume_neutral_row_count": zero_volume_neutral_count,
        "rows": rows,
    }


async def _download(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    symbol: str,
    month: str,
) -> dict[str, Any]:
    url = _archive_url(symbol, month)
    filename = url.rsplit("/", 1)[-1]

    async def get(target: str) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(3):
            try:
                return await client.get(target)
            except httpx.HTTPError as exc:
                error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"{target} 网络读取失败。") from error

    async with semaphore:
        response, checksum_response = await asyncio.gather(get(url), get(f"{url}.CHECKSUM"))
    if response.status_code != 200 or checksum_response.status_code != 200:
        raise RuntimeError(f"{symbol} {month} ZIP/CHECKSUM 下载失败。")
    payload = bytes(response.content)
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != _parse_checksum(checksum_response.text, filename):
        raise RuntimeError(f"{symbol} {month} 官方 checksum 校验失败。")
    parsed = _parse_archive(symbol, month, payload, source_sha256=actual_sha)
    parsed.update({"url": url, "zip_sha256": actual_sha, "official_checksum_verified": True})
    print(f"VERIFIED {symbol} {month} rows={parsed['row_count']}", flush=True)
    return parsed


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "segment",
                "open_time",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "quote_volume",
                "taker_buy_volume",
                "taker_buy_quote_volume",
                "source_month",
                "source_zip_sha256",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _freeze_symbol(output_dir: Path, symbol: str, archives: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted(archives, key=lambda item: str(item["month"]))
    if len(ordered) != EXPECTED_ARCHIVES_PER_SYMBOL or [str(item["month"]) for item in ordered] != list(_months()):
        raise RuntimeError(f"{symbol} 源月档集合不一致。")
    rows = [dict(row) for archive in ordered for row in archive["rows"]]
    if len(rows) != EXPECTED_ROWS_PER_SYMBOL:
        raise RuntimeError(f"{symbol} 合并行数不一致。")
    for segment, _start, _end, expected in SEGMENTS:
        segment_rows = [row for row in rows if str(row["segment"]) == segment]
        times = [int(row["open_time"]) for row in segment_rows]
        if len(times) != expected or any(current - previous != HOUR_MS for previous, current in zip(times, times[1:])):
            raise RuntimeError(f"{symbol} {segment} 小时路径不连续。")
    stem = f"binance_um_order_flow_{symbol.lower()}_1h_202007_202306_202408_202606"
    csv_path = output_dir / f"{stem}.csv"
    manifest_path = output_dir / f"{stem}.manifest.json"
    _write_csv(csv_path, rows)
    csv_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_protocol_sha256": PROTOCOL_SHA256,
        "availability_audit_sha256": AUDIT_SHA256,
        "provider": "binance_data_vision",
        "market": "USDS_M",
        "data_type": "klines_with_order_flow",
        "interval": INTERVAL,
        "symbol": symbol,
        "segments": [
            {"name": name, "start_month": start, "end_month": end, "row_count": count}
            for name, start, end, count in SEGMENTS
        ],
        "excluded_months": {"start_month": EXCLUDED_START, "end_month": EXCLUDED_END},
        "file_name": csv_path.name,
        "file_sha256": csv_sha,
        "row_count": len(rows),
        "complete_utc_day_count": 1_794,
        "source_archive_count": len(ordered),
        "duplicate_rows": 0,
        "in_segment_missing_hours": 0,
        "official_checksums_verified": True,
        "cross_asset_timestamp_alignment_required": True,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "source_archives": [
            {
                key: item[key]
                for key in (
                    "month",
                    "url",
                    "zip_sha256",
                    "row_count",
                    "first_open_time",
                    "last_open_time",
                    "header_present",
                    "timestamp_normalized_row_count",
                    "gap_count",
                    "invalid_row_count",
                    "zero_volume_neutral_row_count",
                    "official_checksum_verified",
                )
            }
            for item in ordered
        ],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "symbol": symbol,
        "csv": str(csv_path.resolve()),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "file_sha256": csv_sha,
        "row_count": len(rows),
        "source_archive_count": len(ordered),
    }


def _assert_alignment(results: Sequence[Mapping[str, Any]]) -> None:
    keys = []
    for result in results:
        manifest = json.loads(Path(str(result["manifest"])).read_text(encoding="utf-8"))
        csv_path = Path(str(result["manifest"])).parent / str(manifest["file_name"])
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            keys.append([int(row["open_time"]) for row in csv.DictReader(handle)])
    if any(value != keys[0] for value in keys[1:]):
        raise RuntimeError("Round 29 BTC/ETH open_time 未完全对齐。")


async def _run(output_dir: Path, concurrency: int) -> list[dict[str, Any]]:
    if _sha256(AUDIT_PATH.resolve()) != AUDIT_SHA256:
        raise ValueError("Round 29 数据审计哈希不一致。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 29 数据协议哈希不一致。")
    months = _months()
    proxy = load_config().raw.get("proxy")
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    async with httpx.AsyncClient(timeout=60, follow_redirects=True, **_httpx_proxy_kwargs(proxy)) as client:
        downloads = await asyncio.gather(*(_download(client, semaphore, symbol, month) for symbol in SYMBOLS for month in months))
    results = [_freeze_symbol(output_dir, symbol, [item for item in downloads if item["symbol"] == symbol]) for symbol in SYMBOLS]
    _assert_alignment(results)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="冻结 Round 29 Kline 主动买量字段。")
    parser.add_argument("--output-dir", default="data/backtests/round29_order_flow")
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()
    if args.concurrency <= 0:
        raise ValueError("concurrency 必须大于 0。")
    print(json.dumps(asyncio.run(_run(Path(args.output_dir), args.concurrency)), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
