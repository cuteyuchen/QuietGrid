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
AUDIT_PATH = Path(
    "reports/cross-era-oos/round28-spot-quarterly-carry-data-audit.md"
)
AUDIT_SHA256 = "ab170a920c00e692d27569fa9ab1f9e8ca40cee829dd126f279cfabc75599dd0"
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round28-spot-quarterly-carry-data-protocol.md"
)
PROTOCOL_SHA256 = "8c0c1dc18b02690a921db10db3b0abdbaeb12d7b842acfa0cc7b46318439e680"
SPOT_BASE_URL = "https://data.binance.vision/data/spot/monthly/klines"
QUARTERLY_BASE_URL = "https://data.binance.vision/data/futures/um/monthly/klines"
INTERVAL = "1h"
ASSETS = ("BTC", "ETH")
CONTRACTS = (
    ("DEVELOPMENT", "210625"),
    ("DEVELOPMENT", "210924"),
    ("DEVELOPMENT", "211231"),
    ("DEVELOPMENT", "220325"),
    ("DEVELOPMENT", "220624"),
    ("VALIDATION", "220930"),
    ("VALIDATION", "221230"),
    ("VALIDATION", "230630"),
    ("POSTHISTORY", "240927"),
    ("POSTHISTORY", "241227"),
    ("POSTHISTORY", "250328"),
    ("POSTHISTORY", "250627"),
    ("POSTHISTORY", "250926"),
    ("POSTHISTORY", "251226"),
    ("POSTHISTORY", "260327"),
    ("POSTHISTORY", "260626"),
)
EXPECTED_WINDOW_COUNTS = {"DEVELOPMENT": 5, "VALIDATION": 3, "POSTHISTORY": 8}
EXPECTED_WINDOW_COUNT = 16
EXPECTED_ROWS_PER_WINDOW = 720
EXPECTED_ROWS_PER_ASSET = 11_520
EXPECTED_ARCHIVES_PER_ASSET = 62
EXCLUDED_START = datetime(2023, 7, 1, tzinfo=UTC)
EXCLUDED_END = datetime(2024, 8, 1, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _expiry(code: str) -> datetime:
    return datetime.strptime(code, "%y%m%d").replace(hour=8, tzinfo=UTC)


def _month_values(start: datetime, end: datetime) -> tuple[str, ...]:
    cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    values = []
    while cursor <= end:
        values.append(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return tuple(values)


def _windows() -> list[dict[str, Any]]:
    windows = []
    for role, code in CONTRACTS:
        expiry = _expiry(code)
        entry = expiry - timedelta(days=30)
        end = expiry - timedelta(hours=1)
        if entry < EXCLUDED_END and expiry > EXCLUDED_START:
            raise RuntimeError(f"Round 28 {code} 触碰隔离区间。")
        windows.append(
            {
                "role": role,
                "contract_code": code,
                "window_id": f"delivery_{code}",
                "entry_time": entry,
                "expiry_time": expiry,
                "end_time": end,
            }
        )
    if len(windows) != EXPECTED_WINDOW_COUNT:
        raise RuntimeError("Round 28 窗口数量不一致。")
    counts = {role: sum(item["role"] == role for item in windows) for role in EXPECTED_WINDOW_COUNTS}
    if counts != EXPECTED_WINDOW_COUNTS:
        raise RuntimeError("Round 28 窗口角色数量不一致。")
    if any(
        previous["expiry_time"] > current["entry_time"]
        for previous, current in zip(windows, windows[1:])
    ):
        raise RuntimeError("Round 28 交割窗口重叠。")
    return windows


def _required_archives(
    asset: str, windows: Sequence[Mapping[str, Any]]
) -> dict[tuple[str, str, str], set[int]]:
    spot_symbol = f"{asset}USDT"
    required: dict[tuple[str, str, str], set[int]] = {}
    for window in windows:
        quarterly_symbol = f"{asset}USDT_{window['contract_code']}"
        for offset in range(EXPECTED_ROWS_PER_WINDOW):
            timestamp = window["entry_time"] + timedelta(hours=offset)
            if timestamp >= window["expiry_time"]:
                raise RuntimeError("Round 28 required path 越过交割时间。")
            month = timestamp.strftime("%Y-%m")
            timestamp_ms = int(timestamp.timestamp() * 1000)
            required.setdefault(("SPOT", spot_symbol, month), set()).add(timestamp_ms)
            required.setdefault(("QUARTERLY", quarterly_symbol, month), set()).add(
                timestamp_ms
            )
    if len(required) != EXPECTED_ARCHIVES_PER_ASSET:
        raise RuntimeError(
            f"{asset} 源月档数量不一致: {len(required)} != {EXPECTED_ARCHIVES_PER_ASSET}"
        )
    if any("2023-07" <= month <= "2024-07" for _market, _symbol, month in required):
        raise RuntimeError("Round 28 required archive 触碰隔离区间。")
    return required


def _archive_url(market: str, symbol: str, month: str) -> str:
    filename = f"{symbol}-{INTERVAL}-{month}.zip"
    base = SPOT_BASE_URL if market == "SPOT" else QUARTERLY_BASE_URL
    return f"{base}/{symbol}/{INTERVAL}/{filename}"


def _parse_archive(
    market: str,
    symbol: str,
    month: str,
    payload: bytes,
    *,
    source_sha256: str,
    required_times: set[int],
) -> dict[str, Any]:
    if market not in {"SPOT", "QUARTERLY"}:
        raise ValueError("Round 28 market 必须为 SPOT 或 QUARTERLY。")
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
    normalized_count = 0
    header_present = False
    previous_time: int | None = None
    gap_count = 0
    invalid_ohlc_count = 0
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
        if open_time % HOUR_MS or not start_ms <= open_time < end_ms:
            raise ValueError(f"{symbol} {month} open_time 范围或整点对齐无效。")
        if previous_time is not None:
            if open_time <= previous_time:
                raise ValueError(f"{symbol} {month} open_time 未严格递增。")
            if open_time != previous_time + HOUR_MS:
                gap_count += (open_time - previous_time) // HOUR_MS - 1
        prices = (open_price, high, low, close)
        invalid = any(not math.isfinite(value) or value <= 0 for value in prices) or (
            high < max(open_price, close) or low > min(open_price, close) or high < low
        )
        if invalid:
            if open_time in required_times:
                raise ValueError(f"{symbol} {month} 第 {line_number} 行 OHLC 关系无效。")
            invalid_ohlc_count += 1
        elif open_time in required_times:
            selected[open_time] = {
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "source_month": month,
                "source_zip_sha256": source_sha256,
            }
        open_times.append(open_time)
        normalized_count += int(normalized)
        previous_time = open_time
    if not open_times:
        raise ValueError(f"{symbol} {month} Kline 归档为空。")
    missing = sorted(required_times - set(selected))
    if missing:
        raise RuntimeError(
            f"{symbol} {month} 缺少 {len(missing)} 个授权窗口整点，首个={missing[0]}。"
        )
    return {
        "market": market,
        "symbol": symbol,
        "month": month,
        "row_count": len(open_times),
        "selected_row_count": len(selected),
        "first_open_time": open_times[0],
        "last_open_time": open_times[-1],
        "header_present": header_present,
        "timestamp_normalized_row_count": normalized_count,
        "gap_count": gap_count,
        "missing_required_hour_count": 0,
        "duplicate_row_count": 0,
        "invalid_ohlc_row_count": invalid_ohlc_count,
        "rows": selected,
    }


async def _download_archive(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    market: str,
    symbol: str,
    month: str,
    required_times: set[int],
) -> dict[str, Any]:
    url = _archive_url(market, symbol, month)
    filename = url.rsplit("/", 1)[-1]

    async def _get(target: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                return await client.get(target)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"{target} 网络读取失败。") from last_error

    async with semaphore:
        archive_response, checksum_response = await asyncio.gather(
            _get(url), _get(f"{url}.CHECKSUM")
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
        market,
        symbol,
        month,
        payload,
        source_sha256=actual_sha256,
        required_times=required_times,
    )
    parsed.update(
        {"url": url, "zip_sha256": actual_sha256, "official_checksum_verified": True}
    )
    print(
        f"VERIFIED {market} {symbol} {month} rows={parsed['row_count']} "
        f"selected={parsed['selected_row_count']}",
        flush=True,
    )
    return parsed


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "role",
        "window_id",
        "entry_time",
        "expiry_time",
        "open_time",
        "spot_symbol",
        "quarterly_symbol",
        "spot_open",
        "spot_high",
        "spot_low",
        "spot_close",
        "quarterly_open",
        "quarterly_high",
        "quarterly_low",
        "quarterly_close",
        "spot_source_month",
        "spot_source_zip_sha256",
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
    archive_keys: set[tuple[str, str, str]] = set()
    rows_by_key: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for archive in archives:
        archive_key = (
            str(archive["market"]),
            str(archive["symbol"]),
            str(archive["month"]),
        )
        if archive_key in archive_keys:
            raise RuntimeError(f"{asset} 源月档重复: {archive_key}")
        archive_keys.add(archive_key)
        for open_time, row in archive["rows"].items():
            key = (str(archive["market"]), str(archive["symbol"]), int(open_time))
            if key in rows_by_key:
                raise RuntimeError(f"{asset} Kline 主键重复: {key}")
            rows_by_key[key] = row

    spot_symbol = f"{asset}USDT"
    frozen_rows: list[dict[str, Any]] = []
    window_audit: list[dict[str, Any]] = []
    for window in windows:
        quarterly_symbol = f"{asset}USDT_{window['contract_code']}"
        window_rows = []
        for offset in range(EXPECTED_ROWS_PER_WINDOW):
            open_time = int((window["entry_time"] + timedelta(hours=offset)).timestamp() * 1000)
            spot = rows_by_key[("SPOT", spot_symbol, open_time)]
            quarterly = rows_by_key[("QUARTERLY", quarterly_symbol, open_time)]
            window_rows.append(
                {
                    "role": window["role"],
                    "window_id": window["window_id"],
                    "entry_time": _iso(window["entry_time"]),
                    "expiry_time": _iso(window["expiry_time"]),
                    "open_time": open_time,
                    "spot_symbol": spot_symbol,
                    "quarterly_symbol": quarterly_symbol,
                    "spot_open": spot["open"],
                    "spot_high": spot["high"],
                    "spot_low": spot["low"],
                    "spot_close": spot["close"],
                    "quarterly_open": quarterly["open"],
                    "quarterly_high": quarterly["high"],
                    "quarterly_low": quarterly["low"],
                    "quarterly_close": quarterly["close"],
                    "spot_source_month": spot["source_month"],
                    "spot_source_zip_sha256": spot["source_zip_sha256"],
                    "quarterly_source_month": quarterly["source_month"],
                    "quarterly_source_zip_sha256": quarterly["source_zip_sha256"],
                }
            )
        times = [int(item["open_time"]) for item in window_rows]
        if len(window_rows) != EXPECTED_ROWS_PER_WINDOW or any(
            current - previous != HOUR_MS for previous, current in zip(times, times[1:])
        ):
            raise RuntimeError(f"{asset} {window['window_id']} 不是完整 720 小时路径。")
        frozen_rows.extend(window_rows)
        window_audit.append(
            {
                "role": window["role"],
                "window_id": window["window_id"],
                "entry_time": _iso(window["entry_time"]),
                "expiry_time": _iso(window["expiry_time"]),
                "spot_symbol": spot_symbol,
                "quarterly_symbol": quarterly_symbol,
                "row_count": len(window_rows),
                "first_open_time": times[0],
                "last_open_time": times[-1],
                "aligned": True,
            }
        )
    if len(frozen_rows) != EXPECTED_ROWS_PER_ASSET:
        raise RuntimeError(f"{asset} 冻结行数不一致。")
    primary_keys = {(str(item["window_id"]), int(item["open_time"])) for item in frozen_rows}
    if len(primary_keys) != len(frozen_rows):
        raise RuntimeError(f"{asset} 冻结主键重复。")

    stem = f"binance_spot_quarterly_carry_{asset.lower()}_1h_202102_202306_202408_202606"
    csv_path = output_dir / f"{stem}.csv"
    manifest_path = output_dir / f"{stem}.manifest.json"
    _write_csv(csv_path, frozen_rows)
    csv_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    ordered_archives = sorted(
        archives, key=lambda item: (str(item["market"]), str(item["symbol"]), str(item["month"]))
    )
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_protocol_sha256": PROTOCOL_SHA256,
        "availability_audit_sha256": AUDIT_SHA256,
        "provider": "binance_data_vision",
        "spot_market": "SPOT",
        "quarterly_market": "USDS_M",
        "data_type": "klines",
        "interval": INTERVAL,
        "asset": asset,
        "spot_symbol": spot_symbol,
        "file_name": csv_path.name,
        "file_sha256": csv_sha256,
        "row_count": len(frozen_rows),
        "window_count": len(window_audit),
        "rows_per_window": EXPECTED_ROWS_PER_WINDOW,
        "window_counts": EXPECTED_WINDOW_COUNTS,
        "duplicate_primary_keys": 0,
        "source_archive_count": len(ordered_archives),
        "official_checksums_verified": all(
            bool(item["official_checksum_verified"]) for item in ordered_archives
        ),
        "authorized_windows_complete": True,
        "excluded_interval": {
            "start": _iso(EXCLUDED_START),
            "end": _iso(EXCLUDED_END),
            "reason": "CURRENT_FINAL_OOS_AND_ISOLATION_BUFFER",
        },
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "windows": window_audit,
        "source_archives": [
            {
                key: item[key]
                for key in (
                    "market",
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
                    "gap_count",
                    "missing_required_hour_count",
                    "duplicate_row_count",
                    "invalid_ohlc_row_count",
                    "official_checksum_verified",
                )
            }
            for item in ordered_archives
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "asset": asset,
        "csv": str(csv_path.resolve()),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "file_sha256": csv_sha256,
        "row_count": len(frozen_rows),
        "window_count": len(window_audit),
        "source_archive_count": len(ordered_archives),
    }


def _assert_asset_alignment(results: Sequence[Mapping[str, Any]]) -> None:
    keys_by_asset = []
    for result in results:
        manifest_path = Path(str(result["manifest"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        csv_path = manifest_path.parent / str(manifest["file_name"])
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            keys_by_asset.append(
                [(row["window_id"], int(row["open_time"])) for row in csv.DictReader(handle)]
            )
    if len(keys_by_asset) != len(ASSETS) or any(
        keys != keys_by_asset[0] for keys in keys_by_asset[1:]
    ):
        raise RuntimeError("Round 28 BTC/ETH 窗口时间键未完全对齐。")


async def _run(output_dir: Path, concurrency: int) -> list[dict[str, Any]]:
    if _sha256(AUDIT_PATH.resolve()) != AUDIT_SHA256:
        raise ValueError("Round 28 数据可用性审计哈希不一致。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 28 数据协议哈希不一致。")
    windows = _windows()
    required_by_asset = {
        asset: _required_archives(asset, windows) for asset in ASSETS
    }
    proxy_config = load_config().raw.get("proxy")
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    async with httpx.AsyncClient(
        timeout=60.0,
        follow_redirects=True,
        **_httpx_proxy_kwargs(proxy_config),
    ) as client:
        downloads = await asyncio.gather(
            *(
                _download_archive(
                    client, semaphore, market, symbol, month, required_times
                )
                for required in required_by_asset.values()
                for (market, symbol, month), required_times in required.items()
            )
        )
    results = []
    for asset in ASSETS:
        authorized_keys = set(required_by_asset[asset])
        asset_archives = [
            item
            for item in downloads
            if (str(item["market"]), str(item["symbol"]), str(item["month"]))
            in authorized_keys
        ]
        results.append(_freeze_asset(output_dir, asset, windows, asset_archives))
    _assert_asset_alignment(results)
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 Round 28 现货/季度交割合约 30 日配对小时路径。"
    )
    parser.add_argument(
        "--output-dir", default="data/backtests/round28_spot_quarterly_carry"
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
