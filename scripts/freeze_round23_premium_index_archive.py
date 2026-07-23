from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from core.config import load_config
from data_sources.binance_source import _httpx_proxy_kwargs
import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_funding_carry_upper_bound as round22
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round23-premium-index-data-protocol.md"
)
PROTOCOL_SHA256 = "be795fa8fac4af4bede6cb7418c8624ea9dc5064704eabda44025a6e569b1a8f"
AUDIT_PATH = Path(
    "reports/cross-era-oos/round23-independent-yield-data-audit.md"
)
AUDIT_SHA256 = "8e4e218b8a8000d9ab87053e555027e22f74b2ce70beb5131e8483c5c8db25c0"
ROUND22_PROTOCOL_SHA256 = (
    "d5df0db9557946b06efa2e8990fe483a6cf8e9a8806fff892f8da53cb6652579"
)
BASE_URL = (
    "https://data.binance.vision/data/futures/um/monthly/premiumIndexKlines"
)
DAILY_BASE_URL = (
    "https://data.binance.vision/data/futures/um/daily/premiumIndexKlines"
)
SYMBOLS = ("BTCUSDT", "ETHUSDT")
INTERVAL = "1m"
SEGMENTS = (
    ("AUTHORIZED_COMPLETE_MONTHS", "2020-01", "2023-06"),
    ("POSTHISTORY_COMPLETE_MONTHS", "2024-08", "2026-06"),
)
EXCLUDED_START = "2023-07"
EXCLUDED_END = "2024-07"
EXPECTED_WINDOW_COUNTS = {
    "PREHISTORY_external": 28,
    "CURRENT_development": 108,
    "CURRENT_validation_complete_months": 49,
    "POSTHISTORY_external": 108,
}
EXPECTED_WINDOW_COUNT = sum(EXPECTED_WINDOW_COUNTS.values())
EXCLUDED_INCOMPLETE_WINDOWS = {
    "nyse_20200117T210000Z": 29,
    "nyse_20260626T200000Z": 360,
}
EXPECTED_FROZEN_WINDOW_COUNTS = {
    "PREHISTORY_external": 27,
    "CURRENT_development": 108,
    "CURRENT_validation_complete_months": 49,
    "POSTHISTORY_external": 107,
}
EXPECTED_FROZEN_WINDOW_COUNT = sum(EXPECTED_FROZEN_WINDOW_COUNTS.values())
EXPECTED_MONTHLY_MISSING_MINUTES = {
    "BTCUSDT": 11_707,
    "ETHUSDT": 11_704,
}
EXPECTED_DAILY_RECOVERED_MINUTES = {
    "BTCUSDT": 10_080,
    "ETHUSDT": 10_080,
}
EXPECTED_SOURCE_REMAINING_MISSING_MINUTES = {
    "BTCUSDT": 1_627,
    "ETHUSDT": 1_624,
}
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
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


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
        raise ValueError("Premium Index 月份起点晚于终点。")
    return tuple(_month_value(index) for index in range(first, last + 1))


def _authorized_months() -> tuple[str, ...]:
    months = tuple(
        month
        for _name, start, end in SEGMENTS
        for month in _month_sequence(start, end)
    )
    excluded = set(_month_sequence(EXCLUDED_START, EXCLUDED_END))
    if len(months) != len(set(months)):
        raise RuntimeError("Round 23 Premium Index 授权月份发生重复。")
    if set(months) & excluded:
        raise RuntimeError("Round 23 Premium Index 授权月份触碰封存区间。")
    return months


def _archive_url(symbol: str, month: str) -> str:
    normalized = str(symbol).strip().upper()
    return (
        f"{BASE_URL}/{normalized}/{INTERVAL}/"
        f"{normalized}-{INTERVAL}-{month}.zip"
    )


def _daily_archive_url(symbol: str, day: str) -> str:
    normalized = str(symbol).strip().upper()
    return (
        f"{DAILY_BASE_URL}/{normalized}/{INTERVAL}/"
        f"{normalized}-{INTERVAL}-{day}.zip"
    )


def _parse_checksum(text: str, expected_name: str) -> str:
    parts = str(text).strip().split()
    if len(parts) < 2:
        raise ValueError("官方 CHECKSUM 内容无效。")
    checksum = parts[0].strip().lower()
    filename = parts[-1].lstrip("*")
    if filename != expected_name:
        raise ValueError(f"CHECKSUM 文件名不一致: {filename} != {expected_name}")
    if len(checksum) != 64 or any(ch not in "0123456789abcdef" for ch in checksum):
        raise ValueError("官方 CHECKSUM 不是有效 SHA-256。")
    return checksum


def _month_bounds(month: str) -> tuple[int, int]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = datetime.strptime(_month_value(_month_index(month) + 1), "%Y-%m").replace(
        tzinfo=UTC
    )
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _day_bounds(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    end = start.timestamp() * 1000 + 24 * 60 * 60 * 1000
    return int(start.timestamp() * 1000), int(end)


def _timestamp_ms(raw: str) -> tuple[int, bool]:
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


def _load_authorized_windows(
    round12_path: Path,
    round13_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    resolved12 = round12_path.resolve()
    resolved13 = round13_path.resolve()
    if _sha256(resolved12) != asset_audit.ROUND12_RESULT_SHA256:
        raise ValueError("Round 12 冻结结果哈希不一致。")
    if _sha256(resolved13) != ROUND13_RESULT_SHA256:
        raise ValueError("Round 13 冻结结果哈希不一致。")
    round12_payload = json.loads(resolved12.read_text(encoding="utf-8"))
    round13_payload = json.loads(resolved13.read_text(encoding="utf-8"))
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")
    datasets, current_isolation = round22._authorized_windows(
        round12_payload,
        round13_payload,
    )

    windows: list[dict[str, Any]] = []
    observed_counts: dict[str, int] = {}
    for role, splits in datasets.items():
        for split_name, items in splits.items():
            key = f"{role}_{split_name}"
            observed_counts[key] = len(items)
            for item in items:
                market_close = item["market_close"]
                force_close_at = item["force_close_at"]
                start_ms = int(market_close.timestamp() * 1000)
                end_ms = int(force_close_at.timestamp() * 1000)
                if start_ms % 60_000 or end_ms % 60_000 or end_ms <= start_ms:
                    raise RuntimeError(f"{item['window_id']} 窗口边界不是完整分钟。")
                windows.append(
                    {
                        "role": role,
                        "split": split_name,
                        "window_id": str(item["window_id"]),
                        "market_close": market_close,
                        "force_close_at": force_close_at,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "expected_row_count": (end_ms - start_ms) // 60_000,
                    }
                )
    if observed_counts != EXPECTED_WINDOW_COUNTS:
        raise RuntimeError(
            f"Round 23 授权窗口数量不一致: {observed_counts} != "
            f"{EXPECTED_WINDOW_COUNTS}"
        )
    windows.sort(key=lambda item: int(item["start_ms"]))
    if len(windows) != EXPECTED_WINDOW_COUNT:
        raise RuntimeError("Round 23 授权窗口总数不一致。")
    excluded_start_ms, _ = _month_bounds(EXCLUDED_START)
    _, excluded_end_ms = _month_bounds(EXCLUDED_END)
    seen_ids: set[str] = set()
    previous_end: int | None = None
    for item in windows:
        window_id = str(item["window_id"])
        start_ms = int(item["start_ms"])
        end_ms = int(item["end_ms"])
        if window_id in seen_ids:
            raise RuntimeError(f"Round 23 授权窗口 ID 重复: {window_id}")
        if previous_end is not None and start_ms < previous_end:
            raise RuntimeError("Round 23 授权窗口发生重叠。")
        if start_ms < excluded_end_ms and end_ms > excluded_start_ms:
            raise RuntimeError(f"{window_id} 触碰封存月份。")
        seen_ids.add(window_id)
        previous_end = end_ms
    return windows, current_isolation


def _parse_period_archive(
    symbol: str,
    period: str,
    payload: bytes,
    *,
    source_sha256: str,
    windows: Sequence[Mapping[str, Any]],
    source_granularity: str,
) -> dict[str, Any]:
    if source_granularity == "monthly":
        start_ms, end_ms = _month_bounds(period)
    elif source_granularity == "daily":
        start_ms, end_ms = _day_bounds(period)
    else:
        raise ValueError("Premium Index source_granularity 无效。")
    expected_csv = f"{symbol}-{INTERVAL}-{period}.csv"
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{symbol} {period} premiumIndexKlines ZIP 无效。") from exc
    with archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) != 1:
            raise ValueError(f"{symbol} {period} ZIP 必须只包含一个 CSV。")
        info = entries[0]
        if (
            info.filename != expected_csv
            or "/" in info.filename
            or "\\" in info.filename
            or info.filename.startswith("..")
        ):
            raise ValueError(
                f"{symbol} {period} ZIP 内容不一致: {info.filename} != {expected_csv}"
            )
        if info.file_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError(f"{symbol} {period} CSV 解压体积超过上限。")
        try:
            raw = archive.read(info)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{symbol} {period} CSV CRC 校验失败。") from exc
    if len(raw) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError(f"{symbol} {period} CSV 实际解压体积超过上限。")

    intervals = sorted(
        (
            int(item["start_ms"]),
            int(item["end_ms"]),
            str(item["window_id"]),
        )
        for item in windows
        if int(item["start_ms"]) < end_ms and int(item["end_ms"]) > start_ms
    )
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
    selected_rows: list[tuple[str, int, float, str, str, str, str]] = []
    open_times: list[int] = []
    normalized_timestamp_count = 0
    header_present = False
    interval_index = 0
    first_open_time: int | None = None
    last_open_time: int | None = None
    previous_open_time: int | None = None
    gap_ranges: list[dict[str, int]] = []
    for line_number, fields in enumerate(reader, start=1):
        if not fields:
            continue
        if not open_times and fields[0].strip().lower() == "open_time":
            if tuple(value.strip().lower() for value in fields) != EXPECTED_HEADER:
                raise ValueError(f"{symbol} {period} Premium Index 表头不一致。")
            header_present = True
            continue
        if len(fields) != len(EXPECTED_HEADER):
            raise ValueError(
                f"{symbol} {period} 第 {line_number} 行不是标准 12 列 Kline。"
            )
        try:
            open_time, normalized = _timestamp_ms(fields[0])
            premium_close = float(fields[4])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{symbol} {period} 第 {line_number} 行 Premium Index 无效。"
            ) from exc
        if not math.isfinite(premium_close):
            raise ValueError(f"{symbol} {period} 包含非有限 premium close。")
        if open_time % 60_000 or not start_ms <= open_time < end_ms:
            raise ValueError(f"{symbol} {period} 包含范围外或未对齐 open_time。")
        expected_open_time = (
            start_ms if previous_open_time is None else previous_open_time + 60_000
        )
        if open_time <= (previous_open_time if previous_open_time is not None else -1):
            raise ValueError(f"{symbol} {period} open_time 未严格递增。")
        if open_time > expected_open_time:
            gap_ranges.append(
                {
                    "start_ms": expected_open_time,
                    "end_ms": open_time,
                    "missing_minutes": (open_time - expected_open_time) // 60_000,
                }
            )
        if first_open_time is None:
            first_open_time = open_time
        last_open_time = open_time
        previous_open_time = open_time
        open_times.append(open_time)
        normalized_timestamp_count += int(normalized)

        while interval_index < len(intervals) and open_time >= intervals[interval_index][1]:
            interval_index += 1
        if interval_index < len(intervals):
            window_start, window_end, window_id = intervals[interval_index]
            if window_start <= open_time < window_end:
                selected_rows.append(
                    (
                        window_id,
                        open_time,
                        premium_close,
                        datetime.fromtimestamp(open_time / 1000, tz=UTC).strftime(
                            "%Y-%m"
                        ),
                        source_granularity,
                        period,
                        source_sha256,
                    )
                )
    if not open_times:
        raise ValueError(f"{symbol} {period} Premium Index 归档为空。")
    trailing_start = int(last_open_time) + 60_000
    if trailing_start < end_ms:
        gap_ranges.append(
            {
                "start_ms": trailing_start,
                "end_ms": end_ms,
                "missing_minutes": (end_ms - trailing_start) // 60_000,
            }
        )
    return {
        "symbol": symbol,
        "period": period,
        "source_granularity": source_granularity,
        "row_count": len(open_times),
        "selected_row_count": len(selected_rows),
        "first_open_time": first_open_time,
        "last_open_time": last_open_time,
        "header_present": header_present,
        "timestamp_normalized_row_count": normalized_timestamp_count,
        "gap_ranges": gap_ranges,
        "missing_minute_count": sum(
            int(item["missing_minutes"]) for item in gap_ranges
        ),
        "open_times": open_times,
        "rows": selected_rows,
    }


def _parse_archive(
    symbol: str,
    month: str,
    payload: bytes,
    *,
    source_sha256: str,
    windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    parsed = _parse_period_archive(
        symbol,
        month,
        payload,
        source_sha256=source_sha256,
        windows=windows,
        source_granularity="monthly",
    )
    parsed["month"] = month
    parsed["full_row_count"] = parsed["row_count"]
    return parsed


async def _download_month(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    symbol: str,
    month: str,
    windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    url = _archive_url(symbol, month)
    filename = url.rsplit("/", 1)[-1]
    async with semaphore:
        archive_response, checksum_response = await asyncio.gather(
            client.get(url),
            client.get(f"{url}.CHECKSUM"),
        )
    if archive_response.status_code != 200:
        raise RuntimeError(
            f"{symbol} {month} ZIP 下载失败: HTTP {archive_response.status_code}"
        )
    if checksum_response.status_code != 200:
        raise RuntimeError(
            f"{symbol} {month} CHECKSUM 下载失败: HTTP {checksum_response.status_code}"
        )
    payload = bytes(archive_response.content)
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    expected_sha256 = _parse_checksum(checksum_response.text, filename)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"{symbol} {month} 官方 checksum 校验失败。")
    parsed = _parse_archive(
        symbol,
        month,
        payload,
        source_sha256=actual_sha256,
        windows=windows,
    )
    parsed.pop("open_times", None)
    parsed.update(
        {
            "url": url,
            "zip_sha256": actual_sha256,
            "official_checksum_verified": True,
        }
    )
    print(
        f"VERIFIED {symbol} {month} rows={parsed['full_row_count']} "
        f"selected={parsed['selected_row_count']}",
        flush=True,
    )
    return parsed


def _gap_dates(monthly: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    days: set[str] = set()
    for item in monthly:
        for gap in item["gap_ranges"]:
            cursor = datetime.fromtimestamp(int(gap["start_ms"]) / 1000, tz=UTC)
            final = datetime.fromtimestamp((int(gap["end_ms"]) - 1) / 1000, tz=UTC)
            while cursor.date() <= final.date():
                days.add(cursor.date().isoformat())
                cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
                cursor += timedelta(days=1)
    return tuple(sorted(days))


async def _download_daily_supplement(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    symbol: str,
    day: str,
    windows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    url = _daily_archive_url(symbol, day)
    filename = url.rsplit("/", 1)[-1]
    async with semaphore:
        archive_response, checksum_response = await asyncio.gather(
            client.get(url),
            client.get(f"{url}.CHECKSUM"),
        )
    if archive_response.status_code == 404 and checksum_response.status_code == 404:
        print(f"DAILY_UNAVAILABLE {symbol} {day} HTTP=404", flush=True)
        return {
            "symbol": symbol,
            "day": day,
            "period": day,
            "source_granularity": "daily",
            "url": url,
            "http_status": 404,
            "zip_sha256": None,
            "official_checksum_verified": False,
            "row_count": 0,
            "selected_row_count": 0,
            "first_open_time": None,
            "last_open_time": None,
            "header_present": False,
            "timestamp_normalized_row_count": 0,
            "gap_ranges": [],
            "missing_minute_count": 1_440,
            "open_times": [],
            "rows": [],
        }
    if archive_response.status_code != 200 or checksum_response.status_code != 200:
        raise RuntimeError(
            f"{symbol} {day} 日档状态不一致: "
            f"ZIP={archive_response.status_code}, "
            f"CHECKSUM={checksum_response.status_code}"
        )
    payload = bytes(archive_response.content)
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    expected_sha256 = _parse_checksum(checksum_response.text, filename)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"{symbol} {day} 日档官方 checksum 校验失败。")
    parsed = _parse_period_archive(
        symbol,
        day,
        payload,
        source_sha256=actual_sha256,
        windows=windows,
        source_granularity="daily",
    )
    parsed.update(
        {
            "day": day,
            "url": url,
            "http_status": 200,
            "zip_sha256": actual_sha256,
            "official_checksum_verified": True,
        }
    )
    print(
        f"VERIFIED_DAILY {symbol} {day} rows={parsed['row_count']} "
        f"selected={parsed['selected_row_count']}",
        flush=True,
    )
    return parsed


def _write_frozen_csv(
    path: Path,
    rows: Sequence[tuple[str, int, float, str, str, str, str]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            (
                "window_id",
                "open_time",
                "premium_close",
                "source_month",
                "source_granularity",
                "source_period",
                "source_zip_sha256",
            )
        )
        for (
            window_id,
            open_time,
            premium_close,
            source_month,
            source_granularity,
            source_period,
            source_sha256,
        ) in rows:
            writer.writerow(
                (
                    window_id,
                    open_time,
                    format(premium_close, ".17g"),
                    source_month,
                    source_granularity,
                    source_period,
                    source_sha256,
                )
            )


def _freeze_symbol(
    output_dir: Path,
    symbol: str,
    monthly: Sequence[Mapping[str, Any]],
    daily: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
    *,
    current_isolation: Mapping[str, Any],
) -> dict[str, Any]:
    ordered_monthly = sorted(monthly, key=lambda item: str(item["month"]))
    expected_months = _authorized_months()
    if tuple(str(item["month"]) for item in ordered_monthly) != expected_months:
        raise RuntimeError(f"{symbol} Premium Index 月份集合不一致。")
    ordered_daily = sorted(daily, key=lambda item: str(item["day"]))

    monthly_missing_times = {
        timestamp
        for item in ordered_monthly
        for gap in item["gap_ranges"]
        for timestamp in range(
            int(gap["start_ms"]),
            int(gap["end_ms"]),
            60_000,
        )
    }
    daily_available_times = {
        int(timestamp)
        for item in ordered_daily
        if int(item["http_status"]) == 200
        for timestamp in item["open_times"]
    }
    recovered_missing_times = monthly_missing_times & daily_available_times
    remaining_source_missing_times = monthly_missing_times - daily_available_times
    if len(monthly_missing_times) != EXPECTED_MONTHLY_MISSING_MINUTES[symbol]:
        raise RuntimeError(f"{symbol} 月档缺失分钟数量与协议不一致。")
    if len(recovered_missing_times) != EXPECTED_DAILY_RECOVERED_MINUTES[symbol]:
        raise RuntimeError(f"{symbol} 日档补回分钟数量与协议不一致。")
    if (
        len(remaining_source_missing_times)
        != EXPECTED_SOURCE_REMAINING_MISSING_MINUTES[symbol]
    ):
        raise RuntimeError(f"{symbol} 补源后剩余缺失分钟数量与协议不一致。")

    merged_by_time: dict[
        int,
        tuple[str, int, float, str, str, str, str],
    ] = {}
    for item in ordered_monthly:
        for row in item["rows"]:
            open_time = int(row[1])
            if open_time in merged_by_time:
                raise RuntimeError(f"{symbol} 月档授权窗口包含重复分钟。")
            merged_by_time[open_time] = row
    for item in ordered_daily:
        for row in item["rows"]:
            open_time = int(row[1])
            existing = merged_by_time.get(open_time)
            if existing is not None:
                if float(existing[2]) != float(row[2]):
                    raise RuntimeError(f"{symbol} 月档与日档重叠 close 不一致。")
                continue
            if open_time in monthly_missing_times:
                merged_by_time[open_time] = row
    if not merged_by_time:
        raise RuntimeError(f"{symbol} Premium Index 没有授权窗口行。")

    expected_windows = sorted(windows, key=lambda item: int(item["start_ms"]))
    window_by_id = {str(item["window_id"]): item for item in expected_windows}
    monthly_by_period = {str(item["month"]): item for item in ordered_monthly}
    daily_by_period = {str(item["day"]): item for item in ordered_daily}
    rows_by_window: dict[
        str,
        list[tuple[str, int, float, str, str, str, str]],
    ] = {str(item["window_id"]): [] for item in expected_windows}
    for row in merged_by_time.values():
        (
            window_id,
            open_time,
            premium_close,
            source_month,
            source_granularity,
            source_period,
            source_sha256,
        ) = row
        window = window_by_id.get(window_id)
        if window is None:
            raise RuntimeError(f"{symbol} Premium Index 包含未授权窗口。")
        if not math.isfinite(float(premium_close)):
            raise RuntimeError(f"{symbol} Premium Index 包含非有限 close。")
        if datetime.fromtimestamp(open_time / 1000, tz=UTC).strftime(
            "%Y-%m"
        ) != source_month:
            raise RuntimeError(f"{symbol} Premium Index 行级 source_month 不一致。")
        archive = (
            monthly_by_period.get(source_period)
            if source_granularity == "monthly"
            else daily_by_period.get(source_period)
            if source_granularity == "daily"
            else None
        )
        if archive is None or source_sha256 != str(archive["zip_sha256"]):
            raise RuntimeError(f"{symbol} Premium Index 行级 source SHA 不一致。")
        rows_by_window[window_id].append(row)

    window_audit = []
    excluded_window_audit = []
    frozen_rows: list[tuple[str, int, float, str, str, str, str]] = []
    frozen_counts: dict[str, int] = {}
    for item in expected_windows:
        window_id = str(item["window_id"])
        expected_count = int(item["expected_row_count"])
        actual_rows = sorted(rows_by_window[window_id], key=lambda row: int(row[1]))
        actual_times = {int(row[1]) for row in actual_rows}
        expected_times = set(
            range(int(item["start_ms"]), int(item["end_ms"]), 60_000)
        )
        if not actual_times <= expected_times:
            raise RuntimeError(f"{symbol} {window_id} 包含窗口边界外分钟。")
        missing_times = expected_times - actual_times
        if window_id in EXCLUDED_INCOMPLETE_WINDOWS:
            expected_missing = EXCLUDED_INCOMPLETE_WINDOWS[window_id]
            if len(missing_times) != expected_missing:
                raise RuntimeError(
                    f"{symbol} {window_id} 固定排除缺失分钟不一致: "
                    f"{len(missing_times)} != {expected_missing}"
                )
            excluded_window_audit.append(
                {
                    "role": item["role"],
                    "split": item["split"],
                    "window_id": window_id,
                    "market_close": item["market_close"].astimezone(UTC).isoformat(),
                    "force_close_at": item["force_close_at"].astimezone(UTC).isoformat(),
                    "actual_row_count": len(actual_rows),
                    "expected_row_count": expected_count,
                    "missing_minute_count": len(missing_times),
                    "reason": "OFFICIAL_PREMIUM_INDEX_DATA_GAP",
                }
            )
            continue
        if missing_times:
            raise RuntimeError(
                f"{symbol} {window_id} 日档补源后仍缺 "
                f"{len(missing_times)} 分钟。"
            )
        key = f"{item['role']}_{item['split']}"
        frozen_counts[key] = frozen_counts.get(key, 0) + 1
        frozen_rows.extend(actual_rows)
        window_audit.append(
            {
                "role": item["role"],
                "split": item["split"],
                "window_id": window_id,
                "market_close": item["market_close"].astimezone(UTC).isoformat(),
                "force_close_at": item["force_close_at"].astimezone(UTC).isoformat(),
                "row_count": len(actual_rows),
                "expected_row_count": expected_count,
                "complete": True,
            }
        )
    if frozen_counts != EXPECTED_FROZEN_WINDOW_COUNTS:
        raise RuntimeError(
            f"{symbol} 冻结窗口数量不一致: {frozen_counts} != "
            f"{EXPECTED_FROZEN_WINDOW_COUNTS}"
        )
    if len(window_audit) != EXPECTED_FROZEN_WINDOW_COUNT:
        raise RuntimeError(f"{symbol} 冻结窗口总数不一致。")
    if {item["window_id"] for item in excluded_window_audit} != set(
        EXCLUDED_INCOMPLETE_WINDOWS
    ):
        raise RuntimeError(f"{symbol} 固定排除窗口集合不一致。")
    frozen_rows.sort(key=lambda row: int(row[1]))
    if any(
        int(current[1]) <= int(previous[1])
        for previous, current in zip(frozen_rows, frozen_rows[1:])
    ):
        raise RuntimeError(f"{symbol} 冻结 Premium Index open_time 未严格递增。")

    stem = (
        f"binance_um_premium_index_{symbol.lower()}_"
        "202001_202306_202408_202606"
    )
    csv_path = output_dir / f"{stem}.csv"
    manifest_path = output_dir / f"{stem}.manifest.json"
    _write_frozen_csv(csv_path, frozen_rows)
    csv_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    daily_recovery_by_period = {
        str(item["day"]): sum(
            int(timestamp) in monthly_missing_times for timestamp in item["open_times"]
        )
        for item in ordered_daily
    }
    manifest = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_protocol_sha256": PROTOCOL_SHA256,
        "availability_audit_sha256": AUDIT_SHA256,
        "window_protocol_sha256": ROUND22_PROTOCOL_SHA256,
        "provider": "binance_data_vision",
        "market": "USDS_M",
        "market_path": "futures/um",
        "data_type": "premiumIndexKlines",
        "interval": INTERVAL,
        "symbol": symbol,
        "segments": [
            {"name": name, "start_month": start, "end_month": end}
            for name, start, end in SEGMENTS
        ],
        "excluded_months": {
            "start_month": EXCLUDED_START,
            "end_month": EXCLUDED_END,
            "reason": "CURRENT_FINAL_OOS_AND_ISOLATION_BUFFER",
        },
        "file_name": csv_path.name,
        "file_sha256": csv_sha256,
        "row_count": len(frozen_rows),
        "window_count": len(window_audit),
        "duplicate_rows": 0,
        "actual_start": datetime.fromtimestamp(
            frozen_rows[0][1] / 1000, tz=UTC
        ).isoformat(),
        "actual_end": datetime.fromtimestamp(
            frozen_rows[-1][1] / 1000, tz=UTC
        ).isoformat(),
        "official_monthly_checksums_verified": all(
            bool(item["official_checksum_verified"]) for item in ordered_monthly
        ),
        "available_daily_checksums_verified": all(
            bool(item["official_checksum_verified"])
            for item in ordered_daily
            if int(item["http_status"]) == 200
        ),
        "source_gaps_recorded": True,
        "authorized_windows_complete": True,
        "current_isolation": dict(current_isolation),
        "windows": window_audit,
        "excluded_incomplete_windows": excluded_window_audit,
        "source_gap_audit": {
            "monthly_missing_minute_count": len(monthly_missing_times),
            "daily_recovered_minute_count": len(recovered_missing_times),
            "remaining_source_missing_minute_count": len(
                remaining_source_missing_times
            ),
            "excluded_window_missing_minute_count": sum(
                EXCLUDED_INCOMPLETE_WINDOWS.values()
            ),
        },
        "monthly_source_archives": [
            {
                "month": item["month"],
                "url": item["url"],
                "zip_sha256": item["zip_sha256"],
                "full_row_count": item["full_row_count"],
                "missing_minute_count": item["missing_minute_count"],
                "gap_ranges": item["gap_ranges"],
                "selected_row_count": item["selected_row_count"],
                "first_open_time": item["first_open_time"],
                "last_open_time": item["last_open_time"],
                "header_present": item["header_present"],
                "timestamp_normalized_row_count": item[
                    "timestamp_normalized_row_count"
                ],
                "official_checksum_verified": item[
                    "official_checksum_verified"
                ],
            }
            for item in ordered_monthly
        ],
        "daily_supplements": [
            {
                "day": item["day"],
                "url": item["url"],
                "http_status": item["http_status"],
                "zip_sha256": item["zip_sha256"],
                "row_count": item["row_count"],
                "missing_minute_count": item["missing_minute_count"],
                "gap_ranges": item["gap_ranges"],
                "selected_row_count": item["selected_row_count"],
                "recovered_missing_row_count": daily_recovery_by_period[
                    str(item["day"])
                ],
                "header_present": item["header_present"],
                "timestamp_normalized_row_count": item[
                    "timestamp_normalized_row_count"
                ],
                "official_checksum_verified": item[
                    "official_checksum_verified"
                ],
            }
            for item in ordered_daily
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "symbol": symbol,
        "csv": str(csv_path.resolve()),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "file_sha256": csv_sha256,
        "row_count": len(frozen_rows),
        "window_count": len(window_audit),
        "monthly_source_archive_count": len(ordered_monthly),
        "daily_supplement_count": len(ordered_daily),
    }


async def _run(
    output_dir: Path,
    concurrency: int,
    round12_path: Path,
    round13_path: Path,
) -> list[dict[str, Any]]:
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 23 Premium Index 数据协议哈希不一致。")
    if _sha256(AUDIT_PATH.resolve()) != AUDIT_SHA256:
        raise ValueError("Round 23 数据可得性审计哈希不一致。")
    if _sha256(round22.PROTOCOL_PATH.resolve()) != ROUND22_PROTOCOL_SHA256:
        raise ValueError("Round 22 窗口协议哈希不一致。")
    months = _authorized_months()
    if len(months) != 65:
        raise RuntimeError(f"Round 23 授权月份数量不一致: {len(months)} != 65")
    windows, current_isolation = _load_authorized_windows(
        round12_path,
        round13_path,
    )

    proxy_config = load_config().raw.get("proxy")
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    results = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=30.0),
        follow_redirects=True,
        **_httpx_proxy_kwargs(proxy_config),
    ) as client:
        for symbol in SYMBOLS:
            monthly = await asyncio.gather(
                *(
                    _download_month(client, semaphore, symbol, month, windows)
                    for month in months
                )
            )
            daily = await asyncio.gather(
                *(
                    _download_daily_supplement(
                        client,
                        semaphore,
                        symbol,
                        day,
                        windows,
                    )
                    for day in _gap_dates(monthly)
                )
            )
            results.append(
                _freeze_symbol(
                    output_dir,
                    symbol,
                    monthly,
                    daily,
                    windows,
                    current_isolation=current_isolation,
                )
            )
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 Round 23 BTC/ETH 官方 Premium Index 月度归档。"
    )
    parser.add_argument(
        "--output-dir",
        default="data/backtests/round23_premium_index",
    )
    parser.add_argument(
        "--round12-result",
        default="reports/cross-era-oos/round12-quadratic-volatility-defense-results.json",
    )
    parser.add_argument(
        "--round13-result",
        default="reports/cross-era-oos/round13-prehistory-quadratic-w2160-results.json",
    )
    parser.add_argument("--concurrency", type=int, default=12)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.concurrency <= 0:
        raise ValueError("concurrency 必须大于 0。")
    result = asyncio.run(
        _run(
            Path(args.output_dir),
            args.concurrency,
            Path(args.round12_result),
            Path(args.round13_result),
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
