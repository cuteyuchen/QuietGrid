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


UTC = timezone.utc
AUDIT_PATH = Path("reports/cross-era-oos/round27-absolute-trend-data-audit.md")
AUDIT_SHA256 = "adf1fbd3f4845cd5d82c34d043622142cf96c5203c703eeef3521c48ddd149e2"
PROTOCOL_PATH = Path("reports/cross-era-oos/round27-absolute-trend-data-protocol.md")
PROTOCOL_SHA256 = "18fa83410608efb4f81fe7d92e8a3eae4bb32e2bafc3d1e5a207b3cd41358e05"
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
EXPECTED_COMPLETE_DAYS = 1_794
HOUR_MS = 60 * 60 * 1000
MAX_UNCOMPRESSED_BYTES = 10 * 1024 * 1024
EXPECTED_HEADER = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "ignore",
)


def _month_index(value: str) -> int:
    parsed = datetime.strptime(value, "%Y-%m")
    return parsed.year * 12 + parsed.month - 1


def _month_value(index: int) -> str:
    year, month_zero = divmod(index, 12)
    return f"{year:04d}-{month_zero + 1:02d}"


def _month_sequence(start: str, end: str) -> tuple[str, ...]:
    first = _month_index(start)
    last = _month_index(end)
    if first > last:
        raise ValueError("月份起点晚于终点。")
    return tuple(_month_value(index) for index in range(first, last + 1))


def _authorized_months() -> tuple[str, ...]:
    months = tuple(
        month
        for _segment, start, end, _row_count in SEGMENTS
        for month in _month_sequence(start, end)
    )
    excluded = set(_month_sequence(EXCLUDED_START, EXCLUDED_END))
    if len(months) != EXPECTED_ARCHIVES_PER_SYMBOL or len(months) != len(set(months)):
        raise RuntimeError("Round 27 授权月份数量或唯一性不一致。")
    if set(months) & excluded:
        raise RuntimeError("Round 27 授权月份触碰隔离区间。")
    return months


def _segment_for_month(month: str) -> str:
    matching = [
        name for name, start, end, _row_count in SEGMENTS if start <= month <= end
    ]
    if len(matching) != 1:
        raise ValueError(f"月份 {month} 不属于唯一授权段。")
    return matching[0]


def _month_bounds(month: str) -> tuple[int, int]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = datetime.strptime(_month_value(_month_index(month) + 1), "%Y-%m").replace(
        tzinfo=UTC
    )
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _timestamp_ms(raw: Any) -> tuple[int, bool]:
    try:
        value = int(str(raw).strip())
    except ValueError:
        value = int(float(str(raw).strip()))
    normalized = False
    if value >= 100_000_000_000_000:
        if value % 1_000:
            raise ValueError("微秒 open_time 不能无损规范化为毫秒。")
        value //= 1_000
        normalized = True
    if not 100_000_000_000 <= value < 100_000_000_000_000:
        raise ValueError("open_time 必须为 Unix 毫秒或可无损规范化的微秒。")
    return value, normalized


def _parse_checksum(text: str, expected_name: str) -> str:
    parts = str(text).strip().split()
    if len(parts) < 2:
        raise ValueError("官方 CHECKSUM 内容无效。")
    checksum = parts[0].strip().lower()
    filename = parts[-1].lstrip("*")
    if filename != expected_name:
        raise ValueError(f"CHECKSUM 文件名不一致: {filename} != {expected_name}")
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ValueError("官方 CHECKSUM 不是有效 SHA-256。")
    return checksum


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
            raise ValueError(
                f"{symbol} {month} ZIP 内容不一致: {info.filename} != {expected_csv}"
            )
        if info.file_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(f"{symbol} {month} CSV 解压体积超过上限。")
        try:
            raw = archive.read(info)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{symbol} {month} CSV CRC 校验失败。") from exc
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"{symbol} {month} CSV 实际解压体积超过上限。")

    start_ms, end_ms = _month_bounds(month)
    expected_rows = (end_ms - start_ms) // HOUR_MS
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
    rows: list[dict[str, Any]] = []
    header_present = False
    normalized_count = 0
    previous_time: int | None = None
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
            open_price = float(fields[1])
            high = float(fields[2])
            low = float(fields[3])
            close = float(fields[4])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{symbol} {month} 第 {line_number} 行 Kline 无效。") from exc
        if open_time % HOUR_MS or not start_ms <= open_time < end_ms:
            raise ValueError(f"{symbol} {month} open_time 范围或整点对齐无效。")
        if previous_time is not None and open_time != previous_time + HOUR_MS:
            raise ValueError(f"{symbol} {month} open_time 不连续或重复。")
        prices = (open_price, high, low, close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise ValueError(f"{symbol} {month} 第 {line_number} 行价格非有限正数。")
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError(f"{symbol} {month} 第 {line_number} 行 OHLC 关系无效。")
        rows.append(
            {
                "segment": _segment_for_month(month),
                "open_time": open_time,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "source_month": month,
                "source_zip_sha256": source_sha256,
            }
        )
        previous_time = open_time
        normalized_count += int(normalized)
    if len(rows) != expected_rows:
        raise ValueError(f"{symbol} {month} 不是完整 UTC 月: {len(rows)} != {expected_rows}")
    if rows[0]["open_time"] != start_ms or rows[-1]["open_time"] != end_ms - HOUR_MS:
        raise ValueError(f"{symbol} {month} 首尾小时不完整。")
    return {
        "symbol": symbol,
        "month": month,
        "row_count": len(rows),
        "first_open_time": rows[0]["open_time"],
        "last_open_time": rows[-1]["open_time"],
        "header_present": header_present,
        "timestamp_normalized_row_count": normalized_count,
        "missing_hour_count": 0,
        "duplicate_row_count": 0,
        "invalid_ohlc_row_count": 0,
        "rows": rows,
    }


async def _download_archive(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    symbol: str,
    month: str,
) -> dict[str, Any]:
    url = _archive_url(symbol, month)
    filename = url.rsplit("/", 1)[-1]
    async with semaphore:
        archive_response, checksum_response = await asyncio.gather(
            client.get(url), client.get(f"{url}.CHECKSUM")
        )
    if archive_response.status_code != 200:
        raise RuntimeError(f"{symbol} {month} ZIP 下载失败: HTTP {archive_response.status_code}")
    if checksum_response.status_code != 200:
        raise RuntimeError(
            f"{symbol} {month} CHECKSUM 下载失败: HTTP {checksum_response.status_code}"
        )
    payload = bytes(archive_response.content)
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    expected_sha256 = _parse_checksum(checksum_response.text, filename)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"{symbol} {month} 官方 checksum 校验失败。")
    parsed = _parse_archive(symbol, month, payload, source_sha256=actual_sha256)
    parsed.update(
        {"url": url, "zip_sha256": actual_sha256, "official_checksum_verified": True}
    )
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
                "source_month",
                "source_zip_sha256",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _freeze_symbol(
    output_dir: Path,
    symbol: str,
    monthly: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    ordered = sorted(monthly, key=lambda item: str(item["month"]))
    if len(ordered) != EXPECTED_ARCHIVES_PER_SYMBOL:
        raise RuntimeError(f"{symbol} 源月档数量不一致。")
    if [str(item["month"]) for item in ordered] != list(_authorized_months()):
        raise RuntimeError(f"{symbol} 源月档月份与协议不一致。")
    rows = [dict(row) for archive in ordered for row in archive["rows"]]
    if len(rows) != EXPECTED_ROWS_PER_SYMBOL:
        raise RuntimeError(f"{symbol} 合并行数不一致。")
    times = [int(item["open_time"]) for item in rows]
    if len(times) != len(set(times)):
        raise RuntimeError(f"{symbol} 合并数据包含重复 open_time。")
    for name, _start, _end, expected_rows in SEGMENTS:
        segment_times = [
            int(item["open_time"]) for item in rows if str(item["segment"]) == name
        ]
        if len(segment_times) != expected_rows or any(
            current - previous != HOUR_MS
            for previous, current in zip(segment_times, segment_times[1:])
        ):
            raise RuntimeError(f"{symbol} {name} 行数或小时连续性不一致。")
    stem = f"binance_um_perpetual_{symbol.lower()}_1h_202007_202306_202408_202606"
    csv_path = output_dir / f"{stem}.csv"
    manifest_path = output_dir / f"{stem}.manifest.json"
    _write_csv(csv_path, rows)
    csv_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_protocol_sha256": PROTOCOL_SHA256,
        "availability_audit_sha256": AUDIT_SHA256,
        "provider": "binance_data_vision",
        "market": "USDS_M",
        "market_path": "futures/um",
        "data_type": "klines",
        "interval": INTERVAL,
        "symbol": symbol,
        "segments": [
            {"name": name, "start_month": start, "end_month": end, "row_count": count}
            for name, start, end, count in SEGMENTS
        ],
        "excluded_months": {
            "start_month": EXCLUDED_START,
            "end_month": EXCLUDED_END,
            "reason": "CURRENT_FINAL_OOS_AND_ISOLATION_BUFFER",
        },
        "file_name": csv_path.name,
        "file_sha256": csv_sha256,
        "row_count": len(rows),
        "complete_utc_day_count": EXPECTED_COMPLETE_DAYS,
        "duplicate_rows": 0,
        "in_segment_missing_hours": 0,
        "source_archive_count": len(ordered),
        "official_checksums_verified": all(
            bool(item["official_checksum_verified"]) for item in ordered
        ),
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
                    "missing_hour_count",
                    "duplicate_row_count",
                    "invalid_ohlc_row_count",
                    "official_checksum_verified",
                )
            }
            for item in ordered
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "symbol": symbol,
        "csv": str(csv_path.resolve()),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "file_sha256": csv_sha256,
        "row_count": len(rows),
        "source_archive_count": len(ordered),
    }


def _assert_cross_asset_alignment(results: Sequence[Mapping[str, Any]]) -> None:
    if len(results) != len(SYMBOLS):
        raise RuntimeError("Round 27 冻结结果资产数量不一致。")
    time_sets: list[list[int]] = []
    for result in results:
        manifest_path = Path(str(result["manifest"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        csv_path = manifest_path.parent / str(manifest["file_name"])
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            time_sets.append([int(row["open_time"]) for row in csv.DictReader(handle)])
    if any(times != time_sets[0] for times in time_sets[1:]):
        raise RuntimeError("BTC/ETH 冻结 open_time 未完全对齐。")


async def _run(output_dir: Path, concurrency: int) -> list[dict[str, Any]]:
    if _sha256(AUDIT_PATH.resolve()) != AUDIT_SHA256:
        raise ValueError("Round 27 数据可用性审计哈希不一致。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 27 数据协议哈希不一致。")
    months = _authorized_months()
    proxy_config = load_config().raw.get("proxy")
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=True,
        **_httpx_proxy_kwargs(proxy_config),
    ) as client:
        downloads = await asyncio.gather(
            *(
                _download_archive(client, semaphore, symbol, month)
                for symbol in SYMBOLS
                for month in months
            )
        )
    results = [
        _freeze_symbol(
            output_dir,
            symbol,
            [item for item in downloads if item["symbol"] == symbol],
        )
        for symbol in SYMBOLS
    ]
    _assert_cross_asset_alignment(results)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 Round 27 BTC/ETH USD-M 永续 1h 官方月度 Kline。"
    )
    parser.add_argument(
        "--output-dir", default="data/backtests/round27_absolute_trend"
    )
    parser.add_argument("--concurrency", type=int, default=12)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.concurrency <= 0:
        raise ValueError("concurrency 必须大于 0。")
    result = asyncio.run(_run(Path(args.output_dir), args.concurrency))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
