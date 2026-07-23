from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round25-bitmex-funding-data-protocol.md"
)
PROTOCOL_SHA256 = "fa45fc7d07fea75a5bc98a0cfdd07773002c292d2c71732639e7f6f2834dbe53"
AUDIT_PATH = Path(
    "reports/cross-era-oos/round25-cross-venue-funding-data-audit.md"
)
AUDIT_SHA256 = "e2aee11005bef69beb2faa0f6ecced8154fac154be5c828817b33467c6d88d18"
API_URL = "https://www.bitmex.com/api/v1/funding"
SYMBOL_MAPPING = {
    "XBTUSD": "BTCUSDT",
    "ETHUSD": "ETHUSDT",
}
SEGMENTS = (
    (
        "AUTHORIZED_HISTORY",
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2023, 7, 1, tzinfo=UTC),
    ),
    (
        "POSTHISTORY",
        datetime(2024, 8, 1, tzinfo=UTC),
        datetime(2026, 7, 1, tzinfo=UTC),
    ),
)
EXCLUDED_START = datetime(2023, 7, 1, tzinfo=UTC)
EXCLUDED_END = datetime(2024, 8, 1, tzinfo=UTC)
PAGE_SIZE = 500
EXPECTED_SEGMENT_COUNTS = {
    "AUTHORIZED_HISTORY": 3_831,
    "POSTHISTORY": 2_097,
}
EXPECTED_SEGMENT_PAGES = {
    "AUTHORIZED_HISTORY": 8,
    "POSTHISTORY": 5,
}
EXPECTED_SEGMENT_BOUNDARIES = {
    "AUTHORIZED_HISTORY": (
        "2020-01-01T04:00:00.000Z",
        "2023-06-30T20:00:00.000Z",
    ),
    "POSTHISTORY": (
        "2024-08-01T04:00:00.000Z",
        "2026-06-30T20:00:00.000Z",
    ),
}
EXPECTED_EVENT_COUNT = sum(EXPECTED_SEGMENT_COUNTS.values())
EXPECTED_PAGE_COUNT = sum(EXPECTED_SEGMENT_PAGES.values())
FUNDING_INTERVAL = "2000-01-01T08:00:00.000Z"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _parse_timestamp(raw: Any, *, label: str) -> datetime:
    value = str(raw or "")
    if not value.endswith("Z"):
        raise ValueError(f"{label} 必须为带 Z 的 UTC 时间。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} 不是合法 ISO-8601 时间。") from exc
    return parsed.astimezone(UTC)


def _parse_page(
    payload: bytes,
    *,
    symbol: str,
    segment_name: str,
    segment_start: datetime,
    segment_end: datetime,
    source_page_sha256: str,
) -> list[dict[str, Any]]:
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("BitMEX funding 响应不是合法 UTF-8 JSON。") from exc
    if not isinstance(raw, list):
        raise ValueError("BitMEX funding 响应不是数组。")
    events: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"BitMEX funding 第 {index + 1} 条不是对象。")
        if str(item.get("symbol") or "") != symbol:
            raise ValueError(f"BitMEX funding 第 {index + 1} 条标的不一致。")
        timestamp = _parse_timestamp(item.get("timestamp"), label="timestamp")
        if not segment_start <= timestamp < segment_end:
            raise ValueError("BitMEX funding 响应包含授权段外事件。")
        if str(item.get("fundingInterval") or "") != FUNDING_INTERVAL:
            raise ValueError("BitMEX funding interval 不是固定 8 小时。")
        try:
            funding_rate = float(item["fundingRate"])
            funding_rate_daily = float(item["fundingRateDaily"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("BitMEX funding rate 字段无效。") from exc
        if not math.isfinite(funding_rate) or not math.isfinite(funding_rate_daily):
            raise ValueError("BitMEX funding rate 包含非有限数。")
        if not math.isclose(
            funding_rate_daily,
            funding_rate * 3.0,
            rel_tol=1e-12,
            abs_tol=1e-15,
        ):
            raise ValueError("BitMEX fundingRateDaily 不等于三倍 fundingRate。")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("BitMEX funding 页内 timestamp 未严格递增。")
        previous_time = timestamp
        events.append(
            {
                "funding_time": int(timestamp.timestamp() * 1000),
                "funding_interval_hours": 8,
                "funding_rate": funding_rate,
                "funding_rate_daily": funding_rate_daily,
                "segment": segment_name,
                "source_page_sha256": source_page_sha256,
            }
        )
    return events


async def _get_page(
    client: httpx.AsyncClient,
    params: Mapping[str, Any],
) -> httpx.Response:
    for attempt in range(3):
        response = await client.get(API_URL, params=params)
        if response.status_code == 200:
            return response
        if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
            raise RuntimeError(
                f"BitMEX funding 请求失败: HTTP {response.status_code} "
                f"{response.text[:300]}"
            )
        retry_after = float(response.headers.get("retry-after") or 1.0)
        await asyncio.sleep(min(max(retry_after, 0.1), 5.0))
    raise RuntimeError("BitMEX funding 请求重试耗尽。")


async def _fetch_segment(
    client: httpx.AsyncClient,
    symbol: str,
    segment_name: str,
    segment_start: datetime,
    segment_end: datetime,
) -> dict[str, Any]:
    cursor = segment_start
    events: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    previous_time: int | None = None
    while cursor < segment_end:
        params = {
            "symbol": symbol,
            "count": PAGE_SIZE,
            "reverse": "false",
            "startTime": _iso(cursor),
            "endTime": _iso(segment_end - timedelta(milliseconds=1)),
        }
        response = await _get_page(client, params)
        payload = bytes(response.content)
        page_sha256 = hashlib.sha256(payload).hexdigest()
        page_events = _parse_page(
            payload,
            symbol=symbol,
            segment_name=segment_name,
            segment_start=segment_start,
            segment_end=segment_end,
            source_page_sha256=page_sha256,
        )
        if not page_events:
            break
        for item in page_events:
            funding_time = int(item["funding_time"])
            if previous_time is not None and funding_time <= previous_time:
                raise RuntimeError(f"{symbol} {segment_name} 跨页时间未严格递增。")
            previous_time = funding_time
            events.append(item)
        first_ms = int(page_events[0]["funding_time"])
        last_ms = int(page_events[-1]["funding_time"])
        pages.append(
            {
                "page_index": len(pages) + 1,
                "request_start_time": params["startTime"],
                "request_end_time": params["endTime"],
                "request_url": str(response.request.url),
                "raw_response_sha256": page_sha256,
                "event_count": len(page_events),
                "first_event": _iso(datetime.fromtimestamp(first_ms / 1000, tz=UTC)),
                "last_event": _iso(datetime.fromtimestamp(last_ms / 1000, tz=UTC)),
            }
        )
        next_cursor = datetime.fromtimestamp(last_ms / 1000, tz=UTC) + timedelta(
            milliseconds=1
        )
        if next_cursor <= cursor:
            raise RuntimeError(f"{symbol} {segment_name} 分页游标未前进。")
        cursor = next_cursor
        if len(page_events) < PAGE_SIZE:
            break

    expected_count = EXPECTED_SEGMENT_COUNTS[segment_name]
    expected_pages = EXPECTED_SEGMENT_PAGES[segment_name]
    if len(events) != expected_count:
        raise RuntimeError(
            f"{symbol} {segment_name} 事件数量不一致: "
            f"{len(events)} != {expected_count}"
        )
    if len(pages) != expected_pages:
        raise RuntimeError(
            f"{symbol} {segment_name} 页数不一致: {len(pages)} != {expected_pages}"
        )
    times = [int(item["funding_time"]) for item in events]
    if any(current - previous != 8 * 60 * 60 * 1000 for previous, current in zip(times, times[1:])):
        raise RuntimeError(f"{symbol} {segment_name} funding cadence 不是严格 8 小时。")
    actual_bounds = (
        _iso(datetime.fromtimestamp(times[0] / 1000, tz=UTC)),
        _iso(datetime.fromtimestamp(times[-1] / 1000, tz=UTC)),
    )
    if actual_bounds != EXPECTED_SEGMENT_BOUNDARIES[segment_name]:
        raise RuntimeError(
            f"{symbol} {segment_name} 首尾事件不一致: "
            f"{actual_bounds} != {EXPECTED_SEGMENT_BOUNDARIES[segment_name]}"
        )
    print(
        f"VERIFIED {symbol} {segment_name} events={len(events)} pages={len(pages)}",
        flush=True,
    )
    return {
        "name": segment_name,
        "start": _iso(segment_start),
        "end": _iso(segment_end),
        "event_count": len(events),
        "page_count": len(pages),
        "first_event": actual_bounds[0],
        "last_event": actual_bounds[1],
        "cadence_hours": 8,
        "cadence_verified": True,
        "events": events,
        "pages": pages,
    }


def _write_csv(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "funding_time",
                "funding_interval_hours",
                "funding_rate",
                "funding_rate_daily",
                "segment",
                "source_page_sha256",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(events)


def _freeze_symbol(
    output_dir: Path,
    symbol: str,
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if tuple(str(item["name"]) for item in segments) != tuple(
        item[0] for item in SEGMENTS
    ):
        raise RuntimeError(f"{symbol} BitMEX funding 段顺序不一致。")
    events = [event for segment in segments for event in segment["events"]]
    if len(events) != EXPECTED_EVENT_COUNT:
        raise RuntimeError(f"{symbol} BitMEX funding 总事件数不一致。")
    times = [int(item["funding_time"]) for item in events]
    if len(times) != len(set(times)) or times != sorted(times):
        raise RuntimeError(f"{symbol} BitMEX funding 包含重复或乱序事件。")
    excluded_start_ms = int(EXCLUDED_START.timestamp() * 1000)
    excluded_end_ms = int(EXCLUDED_END.timestamp() * 1000)
    if any(excluded_start_ms <= value < excluded_end_ms for value in times):
        raise RuntimeError(f"{symbol} BitMEX funding 触碰隔离区间。")

    stem = f"bitmex_funding_{symbol.lower()}_202001_202306_202408_202606"
    csv_path = output_dir / f"{stem}.csv"
    manifest_path = output_dir / f"{stem}.manifest.json"
    _write_csv(csv_path, events)
    csv_sha256 = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "data_protocol_sha256": PROTOCOL_SHA256,
        "availability_audit_sha256": AUDIT_SHA256,
        "provider": "bitmex",
        "api_url": API_URL,
        "symbol": symbol,
        "binance_symbol": SYMBOL_MAPPING[symbol],
        "segments": [
            {
                key: item[key]
                for key in (
                    "name",
                    "start",
                    "end",
                    "event_count",
                    "page_count",
                    "first_event",
                    "last_event",
                    "cadence_hours",
                    "cadence_verified",
                )
            }
            for item in segments
        ],
        "excluded_interval": {
            "start": _iso(EXCLUDED_START),
            "end": _iso(EXCLUDED_END),
            "reason": "CURRENT_FINAL_OOS_AND_ISOLATION_BUFFER",
        },
        "file_name": csv_path.name,
        "file_sha256": csv_sha256,
        "event_count": len(events),
        "duplicate_events": 0,
        "actual_start": _iso(datetime.fromtimestamp(times[0] / 1000, tz=UTC)),
        "actual_end": _iso(datetime.fromtimestamp(times[-1] / 1000, tz=UTC)),
        "page_count": sum(int(item["page_count"]) for item in segments),
        "official_api_pages_verified": True,
        "segment_cadence_verified": True,
        "excluded_interval_not_requested": True,
        "source_pages": [page for item in segments for page in item["pages"]],
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
        "page_count": manifest["page_count"],
    }


async def _run(output_dir: Path) -> list[dict[str, Any]]:
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 25 BitMEX funding 数据协议哈希不一致。")
    if _sha256(AUDIT_PATH.resolve()) != AUDIT_SHA256:
        raise ValueError("Round 25 cross-venue 数据审计哈希不一致。")
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=30.0),
        follow_redirects=True,
        headers={"User-Agent": "QuietGrid-Research/1.0"},
    ) as client:
        async def fetch_symbol(symbol: str) -> dict[str, Any]:
            fetched = []
            for name, start, end in SEGMENTS:
                fetched.append(
                    await _fetch_segment(client, symbol, name, start, end)
                )
            return _freeze_symbol(output_dir, symbol, fetched)

        return list(
            await asyncio.gather(*(fetch_symbol(symbol) for symbol in SYMBOL_MAPPING))
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结 Round 25 BitMEX XBTUSD/ETHUSD 官方 funding API 数据。"
    )
    parser.add_argument(
        "--output-dir",
        default="data/backtests/round25_cross_venue_funding",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(_run(Path(args.output_dir)))
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
