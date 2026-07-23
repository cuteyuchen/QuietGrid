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

from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
AUDIT_PATH = Path(
    "reports/cross-era-oos/round26-quarterly-calendar-spread-data-audit.md"
)
AUDIT_SHA256 = "823b62ad7710669cee4be794067d73c7d1fbed22551e2f0c1ac24c8f167282bf"
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round26-quarterly-calendar-spread-data-protocol.md"
)
PROTOCOL_SHA256 = "8ff68ba3c453ea89caad8ffe7444a0e9d9a79fae5d8db9f9b7fb75eedb6b8cdf"
BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
INTERVAL = "1h"
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
SEGMENTS = (
    (
        "DEVELOPMENT",
        datetime(2021, 2, 5, 20, tzinfo=UTC),
        datetime(2022, 7, 1, 20, tzinfo=UTC),
        67,
    ),
    (
        "VALIDATION",
        datetime(2022, 7, 1, 20, tzinfo=UTC),
        datetime(2023, 6, 30, 20, tzinfo=UTC),
        48,
    ),
    (
        "POSTHISTORY",
        datetime(2024, 8, 2, 20, tzinfo=UTC),
        datetime(2026, 6, 26, 20, tzinfo=UTC),
        91,
    ),
)
EXCLUDED_START = datetime(2023, 7, 1, tzinfo=UTC)
EXCLUDED_END = datetime(2024, 8, 1, tzinfo=UTC)
ASSETS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
CONTRACT_DATES = (
    "210326",
    "210625",
    "210924",
    "211231",
    "220325",
    "220624",
    "220930",
    "221230",
    "230331",
    "230630",
    "240927",
    "241227",
    "250328",
    "250627",
    "250926",
    "251226",
    "260327",
    "260626",
)
EXPECTED_INITIAL_WEEK_COUNT = 224
EXPECTED_EXCLUDED_ROLL_WEEK_COUNT = 18
EXPECTED_WINDOW_COUNT = 206
EXPECTED_ROWS_PER_WINDOW = 169
EXPECTED_ROWS_PER_ASSET = EXPECTED_WINDOW_COUNT * EXPECTED_ROWS_PER_WINDOW
EXPECTED_ARCHIVES_PER_ASSET = 120


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


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


def _month_bounds(month: str) -> tuple[int, int]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _contract_symbol(asset: str, window_end: datetime) -> str:
    candidates = []
    for value in CONTRACT_DATES:
        expiry = datetime.strptime(value, "%y%m%d").replace(hour=8, tzinfo=UTC)
        if expiry > window_end:
            candidates.append((expiry, f"{asset}USDT_{value}"))
    if not candidates:
        raise RuntimeError(f"{asset} {window_end.isoformat()} 没有授权季度合约。")
    return min(candidates)[1]


def _weekly_windows() -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    observed_counts: dict[str, int] = {}
    for role, segment_start, segment_end, expected_count in SEGMENTS:
        start = segment_start
        role_windows = []
        while start + timedelta(days=7) <= segment_end:
            end = start + timedelta(days=7)
            if any(
                datetime.strptime(value, "%y%m%d").date() == end.date()
                for value in CONTRACT_DATES
            ):
                start = end
                continue
            role_windows.append(
                {
                    "role": role,
                    "window_id": f"weekly_{start.strftime('%Y%m%dT%H%M%SZ')}",
                    "window_start": start,
                    "window_end": end,
                    "contracts": {
                        asset: _contract_symbol(asset, end) for asset in ASSETS
                    },
                }
            )
            start = end
        if len(role_windows) != expected_count:
            raise RuntimeError(
                f"{role} 周窗口数量不一致: {len(role_windows)} != {expected_count}"
            )
        observed_counts[role] = len(role_windows)
        windows.extend(role_windows)
    if len(windows) != EXPECTED_WINDOW_COUNT:
        raise RuntimeError("Round 26 周窗口总数不一致。")
    if len({str(item["window_id"]) for item in windows}) != len(windows):
        raise RuntimeError("Round 26 周窗口 ID 重复。")
    for previous, current in zip(windows, windows[1:]):
        if previous["window_end"] > current["window_start"]:
            raise RuntimeError("Round 26 周窗口重叠。")
    if any(
        item["window_start"] < EXCLUDED_END and item["window_end"] > EXCLUDED_START
        for item in windows
    ):
        raise RuntimeError("Round 26 周窗口触碰隔离区间。")
    return windows


def _excluded_roll_windows() -> list[dict[str, Any]]:
    excluded: list[dict[str, Any]] = []
    for role, segment_start, segment_end, _expected_count in SEGMENTS:
        start = segment_start
        while start + timedelta(days=7) <= segment_end:
            end = start + timedelta(days=7)
            matching = [
                value
                for value in CONTRACT_DATES
                if datetime.strptime(value, "%y%m%d").date() == end.date()
            ]
            if matching:
                excluded.append(
                    {
                        "role": role,
                        "window_id": f"weekly_{start.strftime('%Y%m%dT%H%M%SZ')}",
                        "window_start": _iso(start),
                        "window_end": _iso(end),
                        "expiry_code": matching[0],
                        "reason": "QUARTERLY_CONTRACT_EXPIRY_WEEK",
                    }
                )
            start = end
    if len(excluded) != EXPECTED_EXCLUDED_ROLL_WEEK_COUNT:
        raise RuntimeError("Round 26 交割周排除数量不一致。")
    return excluded


def _required_times(
    asset: str,
    windows: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], set[int]]:
    required: dict[tuple[str, str], set[int]] = {}
    perpetual_symbol = ASSETS[asset]
    for window in windows:
        contract = str(window["contracts"][asset])
        start = window["window_start"]
        for offset in range(EXPECTED_ROWS_PER_WINDOW):
            timestamp = start + timedelta(hours=offset)
            timestamp_ms = int(timestamp.timestamp() * 1000)
            month = timestamp.strftime("%Y-%m")
            if EXCLUDED_START <= timestamp < EXCLUDED_END:
                raise RuntimeError("Round 26 required time 触碰隔离区间。")
            required.setdefault((perpetual_symbol, month), set()).add(timestamp_ms)
            required.setdefault((contract, month), set()).add(timestamp_ms)
    if len(required) != EXPECTED_ARCHIVES_PER_ASSET:
        raise RuntimeError(
            f"{asset} 唯一月档数量不一致: {len(required)} != "
            f"{EXPECTED_ARCHIVES_PER_ASSET}"
        )
    return required


def _archive_url(symbol: str, month: str) -> str:
    filename = f"{symbol}-{INTERVAL}-{month}.zip"
    return f"{BASE_URL}/{symbol}/{INTERVAL}/{filename}"


def _parse_archive(
    symbol: str,
    month: str,
    payload: bytes,
    *,
    source_sha256: str,
    required_times: set[int],
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
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
    selected: dict[int, dict[str, Any]] = {}
    open_times: list[int] = []
    normalized_timestamp_count = 0
    header_present = False
    previous_time: int | None = None
    gap_ranges: list[dict[str, int]] = []
    invalid_ohlc_rows: list[dict[str, Any]] = []
    for line_number, fields in enumerate(reader, start=1):
        if not fields:
            continue
        if not open_times and fields[0].strip().lower() == "open_time":
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
        if open_time % (60 * 60 * 1000) or not start_ms <= open_time < end_ms:
            raise ValueError(f"{symbol} {month} open_time 范围或整点对齐无效。")
        if previous_time is not None:
            if open_time <= previous_time:
                raise ValueError(f"{symbol} {month} open_time 未严格递增。")
            expected = previous_time + 60 * 60 * 1000
            if open_time > expected:
                gap_ranges.append(
                    {
                        "start_ms": expected,
                        "end_ms": open_time,
                        "missing_hours": (open_time - expected) // (60 * 60 * 1000),
                    }
                )
        previous_time = open_time
        open_times.append(open_time)
        normalized_timestamp_count += int(normalized)
        prices = (open_price, high, low, close)
        invalid_reason = None
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            invalid_reason = "NON_FINITE_OR_NON_POSITIVE_PRICE"
        elif high < max(open_price, close) or low > min(open_price, close) or high < low:
            invalid_reason = "INVALID_OHLC_RELATION"
        if invalid_reason is not None:
            invalid = {
                "line_number": line_number,
                "open_time": open_time,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "reason": invalid_reason,
            }
            if open_time in required_times:
                raise ValueError(
                    f"{symbol} {month} 授权窗口整点 OHLC 无效: {invalid}"
                )
            invalid_ohlc_rows.append(invalid)
            continue
        if open_time in required_times:
            selected[open_time] = {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "source_month": month,
                "source_zip_sha256": source_sha256,
            }
    if not open_times:
        raise ValueError(f"{symbol} {month} Kline 归档为空。")
    missing_required = sorted(required_times - set(selected))
    if missing_required:
        raise RuntimeError(
            f"{symbol} {month} 缺少 {len(missing_required)} 个冻结窗口整点，"
            f"首个={missing_required[0]}。"
        )
    return {
        "symbol": symbol,
        "month": month,
        "row_count": len(open_times),
        "selected_row_count": len(selected),
        "first_open_time": open_times[0],
        "last_open_time": open_times[-1],
        "header_present": header_present,
        "timestamp_normalized_row_count": normalized_timestamp_count,
        "gap_ranges": gap_ranges,
        "missing_hour_count": sum(int(item["missing_hours"]) for item in gap_ranges),
        "invalid_ohlc_row_count": len(invalid_ohlc_rows),
        "invalid_ohlc_rows": invalid_ohlc_rows,
        "rows": selected,
    }


async def _download_archive(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    symbol: str,
    month: str,
    required_times: set[int],
) -> dict[str, Any]:
    url = _archive_url(symbol, month)
    filename = url.rsplit("/", 1)[-1]
    async with semaphore:
        archive_response, checksum_response = await asyncio.gather(
            client.get(url),
            client.get(f"{url}.CHECKSUM"),
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
    parsed = _parse_archive(
        symbol,
        month,
        payload,
        source_sha256=actual_sha256,
        required_times=required_times,
    )
    parsed.update(
        {
            "url": url,
            "zip_sha256": actual_sha256,
            "official_checksum_verified": True,
        }
    )
    print(
        f"VERIFIED {symbol} {month} rows={parsed['row_count']} "
        f"selected={parsed['selected_row_count']}",
        flush=True,
    )
    return parsed


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "role",
        "window_id",
        "window_start",
        "window_end",
        "open_time",
        "perpetual_symbol",
        "quarterly_symbol",
        "perpetual_open",
        "perpetual_high",
        "perpetual_low",
        "perpetual_close",
        "quarterly_open",
        "quarterly_high",
        "quarterly_low",
        "quarterly_close",
        "perpetual_source_month",
        "perpetual_source_zip_sha256",
        "quarterly_source_month",
        "quarterly_source_zip_sha256",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _freeze_asset(
    output_dir: Path,
    asset: str,
    windows: Sequence[Mapping[str, Any]],
    archives: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if len(archives) != EXPECTED_ARCHIVES_PER_ASSET:
        raise RuntimeError(f"{asset} 源月档数量不一致。")
    rows_by_symbol_time: dict[tuple[str, int], Mapping[str, Any]] = {}
    archive_by_symbol_month: dict[tuple[str, str], Mapping[str, Any]] = {}
    for archive in archives:
        symbol = str(archive["symbol"])
        month = str(archive["month"])
        key = (symbol, month)
        if key in archive_by_symbol_month:
            raise RuntimeError(f"{asset} 源月档重复: {symbol} {month}")
        archive_by_symbol_month[key] = archive
        for open_time, item in archive["rows"].items():
            row_key = (symbol, int(open_time))
            if row_key in rows_by_symbol_time:
                raise RuntimeError(f"{asset} Kline 主键重复: {row_key}")
            rows_by_symbol_time[row_key] = item

    frozen_rows: list[dict[str, Any]] = []
    window_audit: list[dict[str, Any]] = []
    perpetual_symbol = ASSETS[asset]
    for window in windows:
        contract = str(window["contracts"][asset])
        window_rows = []
        for offset in range(EXPECTED_ROWS_PER_WINDOW):
            timestamp = window["window_start"] + timedelta(hours=offset)
            open_time = int(timestamp.timestamp() * 1000)
            perpetual = rows_by_symbol_time[(perpetual_symbol, open_time)]
            quarterly = rows_by_symbol_time[(contract, open_time)]
            window_rows.append(
                {
                    "role": window["role"],
                    "window_id": window["window_id"],
                    "window_start": _iso(window["window_start"]),
                    "window_end": _iso(window["window_end"]),
                    "open_time": open_time,
                    "perpetual_symbol": perpetual_symbol,
                    "quarterly_symbol": contract,
                    "perpetual_open": perpetual["open"],
                    "perpetual_high": perpetual["high"],
                    "perpetual_low": perpetual["low"],
                    "perpetual_close": perpetual["close"],
                    "quarterly_open": quarterly["open"],
                    "quarterly_high": quarterly["high"],
                    "quarterly_low": quarterly["low"],
                    "quarterly_close": quarterly["close"],
                    "perpetual_source_month": perpetual["source_month"],
                    "perpetual_source_zip_sha256": perpetual[
                        "source_zip_sha256"
                    ],
                    "quarterly_source_month": quarterly["source_month"],
                    "quarterly_source_zip_sha256": quarterly[
                        "source_zip_sha256"
                    ],
                }
            )
        times = [int(item["open_time"]) for item in window_rows]
        if len(window_rows) != EXPECTED_ROWS_PER_WINDOW or any(
            current - previous != 60 * 60 * 1000
            for previous, current in zip(times, times[1:])
        ):
            raise RuntimeError(f"{asset} {window['window_id']} 不是完整 169 小时路径。")
        frozen_rows.extend(window_rows)
        window_audit.append(
            {
                "role": window["role"],
                "window_id": window["window_id"],
                "window_start": _iso(window["window_start"]),
                "window_end": _iso(window["window_end"]),
                "perpetual_symbol": perpetual_symbol,
                "quarterly_symbol": contract,
                "row_count": len(window_rows),
                "first_open_time": times[0],
                "last_open_time": times[-1],
                "perpetual_source_zip_sha256": sorted(
                    {str(item["perpetual_source_zip_sha256"]) for item in window_rows}
                ),
                "quarterly_source_zip_sha256": sorted(
                    {str(item["quarterly_source_zip_sha256"]) for item in window_rows}
                ),
                "aligned": True,
            }
        )

    if len(frozen_rows) != EXPECTED_ROWS_PER_ASSET:
        raise RuntimeError(f"{asset} 冻结行数不一致。")
    primary_keys = {
        (str(item["window_id"]), int(item["open_time"])) for item in frozen_rows
    }
    if len(primary_keys) != len(frozen_rows):
        raise RuntimeError(f"{asset} 冻结 CSV 包含重复主键。")

    stem = f"binance_um_quarterly_calendar_spread_{asset.lower()}_1h_202102_202306_202408_202606"
    csv_path = output_dir / f"{stem}.csv"
    manifest_path = output_dir / f"{stem}.manifest.json"
    _write_csv(csv_path, frozen_rows)
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
        "asset": asset,
        "perpetual_symbol": perpetual_symbol,
        "contract_dates": list(CONTRACT_DATES),
        "segments": [
            {
                "role": role,
                "start": _iso(start),
                "end": _iso(end),
                "window_count": expected_count,
            }
            for role, start, end, expected_count in SEGMENTS
        ],
        "excluded_interval": {
            "start": _iso(EXCLUDED_START),
            "end": _iso(EXCLUDED_END),
            "reason": "CURRENT_FINAL_OOS_AND_ISOLATION_BUFFER",
        },
        "file_name": csv_path.name,
        "file_sha256": csv_sha256,
        "row_count": len(frozen_rows),
        "window_count": len(window_audit),
        "initial_week_count": EXPECTED_INITIAL_WEEK_COUNT,
        "excluded_roll_window_count": EXPECTED_EXCLUDED_ROLL_WEEK_COUNT,
        "rows_per_window": EXPECTED_ROWS_PER_WINDOW,
        "duplicate_primary_keys": 0,
        "source_archive_count": len(archives),
        "official_checksums_verified": all(
            bool(item["official_checksum_verified"]) for item in archives
        ),
        "authorized_windows_complete": True,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "excluded_roll_windows": _excluded_roll_windows(),
        "windows": window_audit,
        "source_archives": [
            {
                key: item[key]
                for key in (
                    "symbol",
                    "month",
                    "url",
                    "zip_sha256",
                    "row_count",
                    "selected_row_count",
                    "first_open_time",
                    "last_open_time",
                    "header_present",
                    "timestamp_normalized_row_count",
                    "missing_hour_count",
                    "gap_ranges",
                    "invalid_ohlc_row_count",
                    "invalid_ohlc_rows",
                    "official_checksum_verified",
                )
            }
            for item in sorted(archives, key=lambda value: (value["symbol"], value["month"]))
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "asset": asset,
        "csv": str(csv_path.resolve()),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "file_sha256": csv_sha256,
        "row_count": len(frozen_rows),
        "window_count": len(window_audit),
        "source_archive_count": len(archives),
    }


async def _run(output_dir: Path, concurrency: int) -> list[dict[str, Any]]:
    if _sha256(AUDIT_PATH.resolve()) != AUDIT_SHA256:
        raise ValueError("Round 26 数据可用性审计哈希不一致。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 26 数据冻结协议哈希不一致。")
    windows = _weekly_windows()
    requirements = {asset: _required_times(asset, windows) for asset in ASSETS}
    timeout = httpx.Timeout(90.0, connect=30.0)
    limits = httpx.Limits(
        max_connections=max(4, concurrency * 2),
        max_keepalive_connections=max(2, concurrency),
    )
    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        headers={"User-Agent": "QuietGrid-Research/1.0"},
    ) as client:
        semaphore = asyncio.Semaphore(concurrency)
        downloaded: dict[str, list[dict[str, Any]]] = {}
        for asset in ASSETS:
            downloaded[asset] = list(
                await asyncio.gather(
                    *(
                        _download_archive(client, semaphore, symbol, month, times)
                        for (symbol, month), times in sorted(requirements[asset].items())
                    )
                )
            )
    return [
        _freeze_asset(output_dir, asset, windows, downloaded[asset])
        for asset in ASSETS
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 Round 26 USD-M 永续/季度期限价差 1h 路径。"
    )
    parser.add_argument(
        "--output-dir",
        default="data/backtests/round26_quarterly_calendar_spread",
    )
    parser.add_argument("--concurrency", type=int, default=12)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.concurrency < 1 or args.concurrency > 32:
        raise ValueError("concurrency 必须位于 1..32。")
    results = asyncio.run(_run(Path(args.output_dir), args.concurrency))
    print(json.dumps(results, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
