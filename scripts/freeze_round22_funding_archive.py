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
from typing import Any, Sequence

import httpx

from core.config import load_config
from data_sources.binance_source import _httpx_proxy_kwargs
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round22-funding-archive-data-protocol.md"
)
PROTOCOL_SHA256 = "4ccabf8de9df47b0090f8506a2172141ee6d51a7f9b8cadc29c4a4c93bce4b3e"
BASE_URL = "https://data.binance.vision/data/futures/um/monthly/fundingRate"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
SEGMENTS = (
    ("AUTHORIZED_COMPLETE_MONTHS", "2020-01", "2023-06"),
    ("POSTHISTORY_COMPLETE_MONTHS", "2024-08", "2026-06"),
)
EXCLUDED_START = "2023-07"
EXCLUDED_END = "2024-07"
EXPECTED_HEADER = (
    "calc_time",
    "funding_interval_hours",
    "last_funding_rate",
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
        raise ValueError("资金费月份起点晚于终点。")
    return tuple(_month_value(index) for index in range(first, last + 1))


def _authorized_months() -> tuple[str, ...]:
    months = tuple(
        month
        for _name, start, end in SEGMENTS
        for month in _month_sequence(start, end)
    )
    excluded = set(_month_sequence(EXCLUDED_START, EXCLUDED_END))
    if len(months) != len(set(months)):
        raise RuntimeError("Round 22 授权月份发生重复。")
    if set(months) & excluded:
        raise RuntimeError("Round 22 授权月份触碰封存排除区间。")
    return months


def _archive_url(symbol: str, month: str) -> str:
    normalized = str(symbol).strip().upper()
    return f"{BASE_URL}/{normalized}/{normalized}-fundingRate-{month}.zip"


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
    next_index = _month_index(month) + 1
    end = datetime.strptime(_month_value(next_index), "%Y-%m").replace(tzinfo=UTC)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _parse_archive(
    symbol: str,
    month: str,
    payload: bytes,
    *,
    source_sha256: str,
) -> list[dict[str, Any]]:
    expected_csv = f"{symbol}-fundingRate-{month}.csv"
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{symbol} {month} fundingRate ZIP 无效。") from exc
    with archive:
        names = archive.namelist()
        if names != [expected_csv]:
            raise ValueError(
                f"{symbol} {month} ZIP 内容不一致: {names} != {[expected_csv]}"
            )
        text = archive.read(expected_csv).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if tuple(reader.fieldnames or ()) != EXPECTED_HEADER:
        raise ValueError(f"{symbol} {month} fundingRate 表头不一致。")
    start_ms, end_ms = _month_bounds(month)
    events: list[dict[str, Any]] = []
    previous_time: int | None = None
    for line_number, row in enumerate(reader, start=2):
        try:
            funding_time = int(row["calc_time"])
            interval_hours = int(row["funding_interval_hours"])
            funding_rate = float(row["last_funding_rate"])
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(
                f"{symbol} {month} 第 {line_number} 行资金费无效。"
            ) from exc
        if not start_ms <= funding_time < end_ms:
            raise ValueError(f"{symbol} {month} 包含月份外资金费时间。")
        if interval_hours <= 0:
            raise ValueError(f"{symbol} {month} 包含非正 funding interval。")
        if not math.isfinite(funding_rate):
            raise ValueError(f"{symbol} {month} 包含非有限 funding rate。")
        if previous_time is not None and funding_time <= previous_time:
            raise ValueError(f"{symbol} {month} funding_time 未严格递增。")
        previous_time = funding_time
        events.append(
            {
                "funding_time": funding_time,
                "funding_interval_hours": interval_hours,
                "funding_rate": funding_rate,
                "source_month": month,
                "source_zip_sha256": source_sha256,
            }
        )
    if not events:
        raise ValueError(f"{symbol} {month} fundingRate 归档为空。")
    return events


async def _download_month(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    symbol: str,
    month: str,
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
    events = _parse_archive(
        symbol,
        month,
        payload,
        source_sha256=actual_sha256,
    )
    return {
        "symbol": symbol,
        "month": month,
        "url": url,
        "zip_sha256": actual_sha256,
        "event_count": len(events),
        "official_checksum_verified": True,
        "events": events,
    }


def _write_frozen_csv(path: Path, events: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "funding_time",
                "funding_interval_hours",
                "funding_rate",
                "source_month",
                "source_zip_sha256",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(events)


def _freeze_symbol(
    output_dir: Path,
    symbol: str,
    monthly: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    ordered_monthly = sorted(monthly, key=lambda item: str(item["month"]))
    events = [event for item in ordered_monthly for event in item["events"]]
    if not events:
        raise RuntimeError(f"{symbol} 没有资金费事件。")
    times = [int(item["funding_time"]) for item in events]
    if len(times) != len(set(times)):
        raise RuntimeError(f"{symbol} 合并资金费包含重复时间。")
    if times != sorted(times):
        raise RuntimeError(f"{symbol} 合并资金费没有严格递增。")
    stem = f"binance_um_funding_{symbol.lower()}_202001_202306_202408_202606"
    csv_path = output_dir / f"{stem}.csv"
    manifest_path = output_dir / f"{stem}.manifest.json"
    _write_frozen_csv(csv_path, events)
    csv_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_protocol_sha256": PROTOCOL_SHA256,
        "provider": "binance_data_vision",
        "market": "USDS_M",
        "market_path": "futures/um",
        "data_type": "fundingRate",
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
        "event_count": len(events),
        "duplicate_events": 0,
        "actual_start": datetime.fromtimestamp(times[0] / 1000, tz=UTC).isoformat(),
        "actual_end": datetime.fromtimestamp(times[-1] / 1000, tz=UTC).isoformat(),
        "official_checksums_verified": all(
            bool(item["official_checksum_verified"]) for item in ordered_monthly
        ),
        "source_archives": [
            {
                key: item[key]
                for key in (
                    "month",
                    "url",
                    "zip_sha256",
                    "event_count",
                    "official_checksum_verified",
                )
            }
            for item in ordered_monthly
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
        "event_count": len(events),
        "source_archive_count": len(ordered_monthly),
    }


async def _run(output_dir: Path, concurrency: int) -> list[dict[str, Any]]:
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 22 资金费数据协议哈希不一致。")
    months = _authorized_months()
    if len(months) != 65:
        raise RuntimeError(f"Round 22 授权月份数量不一致: {len(months)} != 65")
    proxy_config = load_config().raw.get("proxy")
    semaphore = asyncio.Semaphore(max(1, int(concurrency)))
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        **_httpx_proxy_kwargs(proxy_config),
    ) as client:
        downloads = await asyncio.gather(
            *(
                _download_month(client, semaphore, symbol, month)
                for symbol in SYMBOLS
                for month in months
            )
        )
    result = []
    for symbol in SYMBOLS:
        result.append(
            _freeze_symbol(
                output_dir,
                symbol,
                [item for item in downloads if item["symbol"] == symbol],
            )
        )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 Round 22 BTC/ETH 官方 fundingRate 月度归档。"
    )
    parser.add_argument(
        "--output-dir",
        default="data/backtests/round22_funding_carry",
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
