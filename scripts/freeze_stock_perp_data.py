"""Freeze public Binance stock-perpetual datasets with auditable hashes.

The default freeze is the formal H1 set (Tier A-Core).  Tier A-Short symbols
remain in the discovery manifest and can be explicitly requested for
descriptive work with ``--include-tier-a-short``; they never affect H1 gates.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

# Support direct script execution.
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_sources.models import FundingEvent, NormalizedKline  # noqa: E402
from scripts.stock_perp_common import (  # noqa: E402
    AGG_TRADES_URL,
    FUNDING_RATE_URL,
    INTERVAL_MS,
    KLINES_URL,
    MARK_KLINES_URL,
    PREMIUM_KLINES_URL,
    PublicDataError,
    PublicHttpClient,
    archive_url,
    day_sequence,
    exchange_info,
    format_float,
    git_branch,
    git_commit,
    immutable_write,
    iso_ms,
    kline_rows_from_archive,
    list_s3_objects,
    load_proxy_config,
    month_sequence,
    next_month,
    normalize_timestamp,
    parse_datetime,
    parse_sidecar_rows,
    timestamp_ms,
    verified_archive,
    write_csv,
    write_json,
)


UTC = timezone.utc
REPORT_DIR = Path("reports/stock-perp-weekend-grid-v1")
DATA_DIR = Path("data/backtests/stock-perp-weekend-grid-v1")
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SIDEcar_UNCOMPRESSED = 4 * 1024 * 1024 * 1024

KLINE_FIELDS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
)
SIDE_FIELDS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
)
AGG_FIELDS = (
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="冻结 Binance 美股永续公开历史数据")
    parser.add_argument("--discovery", default=str(REPORT_DIR / "symbol-discovery.json"))
    parser.add_argument("--output-dir", default=str(DATA_DIR))
    parser.add_argument("--report-dir", default=str(REPORT_DIR))
    parser.add_argument("--proxy-url", default=None)
    parser.add_argument("--include-tier-a-short", action="store_true")
    parser.add_argument("--skip-agg-trades", action="store_true")
    parser.add_argument(
        "--resume-existing",
        action="store_true",
        help="复用本次中断前已完整写入的五类文件，并在 manifest 中标记复用",
    )
    parser.add_argument("--as-of-utc", default=None)
    parser.add_argument("--max-symbols", type=int, default=0)
    return parser


def _month_label(value: date) -> str:
    return value.strftime("%Y-%m")


def _day_label(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def _month_from_key(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1].replace(".zip", "")
    parts = name.split("-")
    for index in range(len(parts) - 1):
        candidate = f"{parts[index]}-{parts[index + 1]}"
        if len(candidate) == 7:
            try:
                datetime.strptime(candidate, "%Y-%m")
            except ValueError:
                continue
            return candidate
    return None


def _day_from_key(key: str) -> str | None:
    name = key.rsplit("/", 1)[-1].replace(".zip", "")
    parts = name.split("-")
    for index in range(len(parts) - 2):
        candidate = "-".join(parts[index : index + 3])
        if len(candidate) == 10:
            try:
                datetime.strptime(candidate, "%Y-%m-%d")
            except ValueError:
                continue
            return candidate
    return None


def _zip_name(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _csv_name(key: str) -> str:
    return _zip_name(key).removesuffix(".zip") + ".csv"


def _symbol_start_end(row: Mapping[str, Any], global_end_ms: int) -> tuple[int, int]:
    onboard = int(
        datetime.fromisoformat(str(row["onboard_date"]).replace("Z", "+00:00"))
        .astimezone(UTC)
        .timestamp()
        * 1000
    )
    first = row.get("first_valid_1m") or {}
    first_ms = int(first.get("open_time") or onboard)
    candidate_ms = max(onboard, first_ms)
    candidate = datetime.fromtimestamp(candidate_ms / 1000, tz=UTC)
    # A partial listing day cannot be used as a complete UTC-day boundary.
    first_complete_day = candidate.date()
    if candidate.time() != datetime.min.time():
        first_complete_day += timedelta(days=1)
    day_start = datetime.combine(first_complete_day, datetime.min.time(), tzinfo=UTC)
    start_ms = max(candidate_ms, int(day_start.timestamp() * 1000))
    return start_ms, global_end_ms


def _global_end_ms(discovery: Mapping[str, Any], override: str | None) -> tuple[datetime, int]:
    if override:
        asof = parse_datetime(override)
    else:
        asof = parse_datetime(str(discovery["as_of_utc"]))
    last_day = asof.date() - timedelta(days=1)
    end = datetime.combine(last_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return asof, int(end.timestamp() * 1000)


def _kind_spec(kind: str, symbol: str) -> tuple[str, str, str]:
    if kind == "klines":
        return (
            f"data/futures/um/monthly/klines/{symbol}/1m/",
            f"data/futures/um/daily/klines/{symbol}/1m/",
            "klines",
        )
    if kind == "mark_price":
        return (
            f"data/futures/um/monthly/markPriceKlines/{symbol}/1m/",
            f"data/futures/um/daily/markPriceKlines/{symbol}/1m/",
            "markPriceKlines",
        )
    if kind == "premium_index":
        return (
            f"data/futures/um/monthly/premiumIndexKlines/{symbol}/1m/",
            f"data/futures/um/daily/premiumIndexKlines/{symbol}/1m/",
            "premiumIndexKlines",
        )
    if kind == "agg_trades":
        return (
            f"data/futures/um/monthly/aggTrades/{symbol}/",
            f"data/futures/um/daily/aggTrades/{symbol}/",
            "aggTrades",
        )
    raise ValueError(f"未知数据类型: {kind}")


def _archive_plan(
    client: PublicHttpClient,
    symbol: str,
    kind: str,
    start_ms: int,
    end_ms: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Plan monthly complete periods and daily boundary periods.

    Returns ``(segments, missing)``.  Missing segments are never silently
    interpolated; the audit later decides which windows are invalid.
    """
    monthly_prefix, daily_prefix, _ = _kind_spec(kind, symbol)
    monthly_objects = list_s3_objects(client, monthly_prefix)
    daily_objects = list_s3_objects(client, daily_prefix)
    monthly = {
        _month_from_key(str(item["key"])): item
        for item in monthly_objects
        if str(item.get("key", "")).endswith(".zip") and _month_from_key(str(item["key"]))
    }
    daily = {
        _day_from_key(str(item["key"])): item
        for item in daily_objects
        if str(item.get("key", "")).endswith(".zip") and _day_from_key(str(item["key"]))
    }
    start_date = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
    end_date = datetime.fromtimestamp((end_ms - 1) / 1000, tz=UTC).date()
    segments: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for month in month_sequence(start_date, end_date):
        month_first = month
        month_last = next_month(month) - timedelta(days=1)
        partial = month == month_sequence(start_date, end_date)[0] and start_date > month_first
        partial = partial or (month == month_sequence(start_date, end_date)[-1] and end_date < month_last)
        if not partial and _month_label(month) in monthly:
            item = monthly[_month_label(month)]
            segments.append(
                {
                    "source": "monthly_archive",
                    "key": item["key"],
                    "start_date": _month_label(month),
                    "end_date": _month_label(month_last),
                }
            )
            continue
        day_start = max(start_date, month_first)
        day_end = min(end_date, month_last)
        for day in day_sequence(day_start, day_end):
            label = _day_label(day)
            item = daily.get(label)
            if item:
                segments.append(
                    {
                        "source": "daily_archive",
                        "key": item["key"],
                        "start_date": label,
                        "end_date": label,
                    }
                )
            else:
                missing.append(
                    {
                        "source": "daily_archive",
                        "date": label,
                        "kind": kind,
                        "reason": "official_archive_object_missing",
                    }
                )
    return segments, missing


def _filter_kline_rows(rows: Iterable[NormalizedKline], start_ms: int, end_ms: int) -> list[NormalizedKline]:
    result = [row for row in rows if start_ms <= row.open_time < end_ms and row.close_time < end_ms]
    result.sort(key=lambda row: row.open_time)
    unique: dict[int, NormalizedKline] = {}
    for row in result:
        unique.setdefault(row.open_time, row)
    return list(unique.values())


def _kline_mapping(row: NormalizedKline) -> dict[str, Any]:
    return {
        "open_time": row.open_time,
        "close_time": row.close_time,
        "open": format_float(row.open),
        "high": format_float(row.high),
        "low": format_float(row.low),
        "close": format_float(row.close),
        "volume": format_float(row.volume),
        "quote_volume": format_float(row.quote_volume),
        "trade_count": row.trade_count,
    }


def _side_mapping(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "open_time": row["open_time"],
        "close_time": row.get("close_time", int(row["open_time"]) + INTERVAL_MS - 1),
        "open": format_float(row.get("open")),
        "high": format_float(row.get("high")),
        "low": format_float(row.get("low")),
        "close": format_float(row.get("close")),
        "volume": format_float(row.get("volume", 0)),
    }


def _funding_mapping(event: FundingEvent) -> dict[str, Any]:
    return {
        "funding_time": event.funding_time,
        "funding_rate": format_float(event.funding_rate),
        "mark_price": format_float(event.mark_price),
    }


def _rest_funding(
    client: PublicHttpClient,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> tuple[list[FundingEvent], list[dict[str, Any]]]:
    events: list[FundingEvent] = []
    requests: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "startTime": cursor,
            "endTime": end_ms - 1,
            "limit": 1000,
        }
        response = client.request("GET", FUNDING_RATE_URL, params=params)
        raw = response.content
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataError(f"Funding REST 返回无效 JSON: {symbol}") from exc
        requests.append(
            {
                "url": FUNDING_RATE_URL,
                "params": params,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "response_sha256": __import__("hashlib").sha256(raw).hexdigest(),
                "row_count": len(payload) if isinstance(payload, list) else 0,
            }
        )
        if not isinstance(payload, list) or not payload:
            break
        page_times: list[int] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                event = FundingEvent(
                    funding_time=int(row["fundingTime"]),
                    funding_rate=float(row["fundingRate"]),
                    mark_price=(
                        float(row["markPrice"])
                        if row.get("markPrice") not in (None, "") and float(row["markPrice"]) > 0
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if start_ms <= event.funding_time < end_ms:
                events.append(event)
                page_times.append(event.funding_time)
        if not page_times:
            break
        next_cursor = max(page_times) + 1
        if next_cursor <= cursor:
            raise PublicDataError(f"Funding REST 游标未前进: {symbol}")
        cursor = next_cursor
        if len(payload) < 1000:
            break
    unique = {event.funding_time: event for event in events}
    return [unique[key] for key in sorted(unique)], requests


def _read_agg_stream(data: bytes, expected_csv_name: str) -> Iterable[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PublicDataError(f"aggTrades ZIP 损坏: {expected_csv_name}") from exc
    with archive:
        entries = [item for item in archive.infolist() if not item.is_dir()]
        if len(entries) != 1 or entries[0].filename != expected_csv_name:
            raise PublicDataError(
                f"aggTrades ZIP 文件名不匹配: {[item.filename for item in entries]}"
            )
        item = entries[0]
        if item.file_size > MAX_SIDEcar_UNCOMPRESSED:
            raise PublicDataError(f"aggTrades 解压体积超过上限: {expected_csv_name}")
        try:
            raw = archive.read(item)
        except zipfile.BadZipFile as exc:
            raise PublicDataError(f"aggTrades CRC 校验失败: {expected_csv_name}") from exc
    text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    for line_no, fields in enumerate(reader, start=1):
        if not fields or not any(str(value).strip() for value in fields):
            continue
        if line_no == 1 and str(fields[0]).strip().lower() in {"agg_trade_id", "id"}:
            continue
        if len(fields) < 7:
            raise PublicDataError(f"aggTrades 第 {line_no} 行列数不足")
        try:
            yield {
                "agg_trade_id": int(fields[0]),
                "price": format_float(fields[1]),
                "quantity": format_float(fields[2]),
                "first_trade_id": int(fields[3]),
                "last_trade_id": int(fields[4]),
                "transact_time": normalize_timestamp(fields[5]),
                "is_buyer_maker": str(fields[6]).strip().lower(),
            }
        except (TypeError, ValueError) as exc:
            raise PublicDataError(f"aggTrades 第 {line_no} 行无效") from exc


def _write_agg_file(
    path: Path,
    segments: Sequence[tuple[bytes, dict[str, Any]]],
    start_ms: int,
    end_ms: int,
) -> tuple[str, int, list[dict[str, Any]]]:
    """Stream aggTrades into a bounded temporary file before immutable publish."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    count = 0
    seen: set[int] = set()
    source_meta: list[dict[str, Any]] = []
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=AGG_FIELDS)
            writer.writeheader()
            for data, meta in segments:
                segment_count = 0
                key = str(meta.get("url", "")).rsplit("/", 1)[-1]
                expected = key.removesuffix(".zip") + ".csv"
                for row in _read_agg_stream(data, expected):
                    timestamp = int(row["transact_time"])
                    trade_id = int(row["agg_trade_id"])
                    if not start_ms <= timestamp < end_ms or trade_id in seen:
                        continue
                    seen.add(trade_id)
                    writer.writerow(row)
                    count += 1
                    segment_count += 1
                item = dict(meta)
                item["rows"] = segment_count
                source_meta.append(item)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path = Path(temp_name)
        digest = __import__("hashlib").sha256(temp_path.read_bytes()).hexdigest()
        if path.exists():
            existing_digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            if existing_digest != digest:
                raise PublicDataError(f"不可变 aggTrades 文件内容不同: {path}")
            temp_path.unlink(missing_ok=True)
        else:
            temp_path.replace(path)
        return digest, count, source_meta
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _archive_kind(
    client: PublicHttpClient,
    symbol: str,
    kind: str,
    start_ms: int,
    end_ms: int,
) -> tuple[list[Any], list[dict[str, Any]], list[dict[str, Any]], list[tuple[bytes, dict[str, Any]]]]:
    segments, missing = _archive_plan(client, symbol, kind, start_ms, end_ms)
    parsed: list[Any] = []
    source_meta: list[dict[str, Any]] = []
    raw_agg: list[tuple[bytes, dict[str, Any]]] = []
    for segment in segments:
        key = str(segment["key"])
        data, meta = verified_archive(client, key)
        meta.update(
            {
                "kind": kind,
                "source": segment["source"],
                "segment_start": segment["start_date"],
                "segment_end": segment["end_date"],
            }
        )
        if kind == "klines":
            parsed.extend(kline_rows_from_archive(data, expected_csv_name=_csv_name(key)))
            meta["rows"] = 0  # filled after range filtering
        elif kind == "agg_trades":
            raw_agg.append((data, meta))
        else:
            parsed.extend(
                parse_sidecar_rows(
                    data,
                    expected_csv_name=_csv_name(key),
                    kind=kind,
                )
            )
        source_meta.append(meta)
    return parsed, missing, source_meta, raw_agg


def _write_asset(
    client: PublicHttpClient,
    row: Mapping[str, Any],
    *,
    output_dir: Path,
    asof: datetime,
    end_ms: int,
    include_agg: bool,
) -> dict[str, Any]:
    symbol = str(row["symbol"])
    start_ms, end_ms = _symbol_start_end(row, end_ms)
    symbol_manifest: dict[str, Any] = {
        "symbol": symbol,
        "tier": row.get("tier"),
        "base_asset": row.get("base_asset"),
        "listing": row.get("listing"),
        "start_time": iso_ms(start_ms),
        "end_time_exclusive": iso_ms(end_ms),
        "rules": row.get("rules"),
        "data_previously_viewed": True,
        "files": {},
        "source_segments": {},
        "missing_segments": [],
        "rest_requests": [],
    }
    # Klines and mark/premium sidecars are small enough to parse in memory.
    parsed_klines, missing, source_meta, _ = _archive_kind(
        client, symbol, "klines", start_ms, end_ms
    )
    klines = _filter_kline_rows(parsed_klines, start_ms, end_ms)
    if not klines:
        raise PublicDataError(f"{symbol} 冻结后没有有效 1m K 线。")
    kline_path = output_dir / f"{symbol}-1m.csv"
    kline_digest = write_csv(kline_path, KLINE_FIELDS, (_kline_mapping(item) for item in klines))
    symbol_manifest["files"]["klines"] = {
        "path": str(kline_path.resolve()),
        "sha256": kline_digest,
        "size_bytes": kline_path.stat().st_size,
        "row_count": len(klines),
        "first_open_time": klines[0].open_time,
        "last_open_time": klines[-1].open_time,
    }
    symbol_manifest["source_segments"]["klines"] = source_meta
    symbol_manifest["missing_segments"].extend(missing)

    for kind, filename in (("mark_price", f"{symbol}-mark-price.csv"), ("premium_index", f"{symbol}-premium-index.csv")):
        parsed, missing, source_meta, _ = _archive_kind(client, symbol, kind, start_ms, end_ms)
        filtered = [
            item
            for item in parsed
            if start_ms <= int(item["open_time"]) < end_ms
            and int(item.get("close_time", int(item["open_time"]) + INTERVAL_MS - 1)) < end_ms
        ]
        filtered.sort(key=lambda item: int(item["open_time"]))
        unique: dict[int, Mapping[str, Any]] = {}
        for item in filtered:
            unique.setdefault(int(item["open_time"]), item)
        ordered = list(unique.values())
        path = output_dir / filename
        digest = write_csv(path, SIDE_FIELDS, (_side_mapping(item) for item in ordered))
        symbol_manifest["files"][kind] = {
            "path": str(path.resolve()),
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "row_count": len(ordered),
        }
        symbol_manifest["source_segments"][kind] = source_meta
        symbol_manifest["missing_segments"].extend(missing)

    # Funding has monthly official archives and a REST tail for the incomplete
    # current month.  Funding is stored as events, never smeared per bar.
    # ``funding_rate`` has a different archive layout, handled explicitly below.
    funding_prefix = f"data/futures/um/monthly/fundingRate/{symbol}/"
    funding_objects = list_s3_objects(client, funding_prefix)
    funding_by_month = {
        _month_from_key(str(item["key"])): item
        for item in funding_objects
        if str(item.get("key", "")).endswith(".zip") and _month_from_key(str(item["key"]))
    }
    events: list[FundingEvent] = []
    funding_segments: list[dict[str, Any]] = []
    for month in month_sequence(
        datetime.fromtimestamp(start_ms / 1000, tz=UTC).date(),
        datetime.fromtimestamp((end_ms - 1) / 1000, tz=UTC).date(),
    ):
        key_item = funding_by_month.get(_month_label(month))
        if not key_item:
            continue
        key = str(key_item["key"])
        data, meta = verified_archive(client, key)
        raw_events = []
        try:
            from data_sources.archive_funding_reader import read_archive_funding

            raw_events = read_archive_funding(
                data,
                expected_csv_name=_csv_name(key),
                max_uncompressed_bytes=256 * 1024 * 1024,
            )
        except Exception as exc:
            raise PublicDataError(f"{symbol} Funding 归档解析失败: {key}: {exc}") from exc
        selected = [event for event in raw_events if start_ms <= event.funding_time < end_ms]
        events.extend(selected)
        meta.update({"kind": "funding_rate", "source": "monthly_archive", "rows": len(selected)})
        funding_segments.append(meta)
    archived_last = max((event.funding_time for event in events), default=start_ms - 1)
    rest_start = max(start_ms, archived_last + 1)
    rest_events, rest_requests = _rest_funding(client, symbol, rest_start, end_ms)
    events.extend(rest_events)
    unique_events = {event.funding_time: event for event in events}
    ordered_events = [unique_events[key] for key in sorted(unique_events)]
    funding_path = output_dir / f"{symbol}-funding.json"
    funding_digest = write_json(
        funding_path,
        {
            "symbol": symbol,
            "events": [_funding_mapping(event) for event in ordered_events],
        },
    )
    symbol_manifest["files"]["funding"] = {
        "path": str(funding_path.resolve()),
        "sha256": funding_digest,
        "size_bytes": funding_path.stat().st_size,
        "row_count": len(ordered_events),
        "first_funding_time": ordered_events[0].funding_time if ordered_events else None,
        "last_funding_time": ordered_events[-1].funding_time if ordered_events else None,
    }
    symbol_manifest["source_segments"]["funding"] = funding_segments
    symbol_manifest["rest_requests"].extend(rest_requests)
    symbol_manifest["missing_segments"].extend(missing)

    if include_agg:
        _, missing, agg_meta, raw_agg = _archive_kind(client, symbol, "agg_trades", start_ms, end_ms)
        agg_path = output_dir / f"{symbol}-agg-trades.csv"
        digest, count, streamed_meta = _write_agg_file(agg_path, raw_agg, start_ms, end_ms)
        symbol_manifest["files"]["agg_trades"] = {
            "path": str(agg_path.resolve()),
            "sha256": digest,
            "size_bytes": agg_path.stat().st_size,
            "row_count": count,
        }
        # Preserve one metadata record per downloaded ZIP and attach the rows
        # selected into the immutable range.
        symbol_manifest["source_segments"]["agg_trades"] = streamed_meta or agg_meta
        symbol_manifest["missing_segments"].extend(missing)
    else:
        symbol_manifest["files"]["agg_trades"] = {
            "status": "NOT_DOWNLOADED",
            "reason": "explicitly_disabled_before_H1",
        }

    symbol_manifest["funding_interval_hours"] = _median_interval_hours(ordered_events)
    symbol_manifest["asof_utc"] = asof.isoformat()
    return symbol_manifest


def _median_interval_hours(events: Sequence[FundingEvent]) -> float | None:
    if len(events) < 2:
        return None
    values = [
        (right.funding_time - left.funding_time) / 3_600_000
        for left, right in zip(events, events[1:])
        if right.funding_time > left.funding_time
    ]
    if not values:
        return None
    values.sort()
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    return round(median, 3)


def _read_csv_meta(path: Path, *, kind: str) -> dict[str, Any]:
    import hashlib

    if kind == "agg_trades":
        digest = hashlib.sha256()
        line_count = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                line_count += chunk.count(b"\n")
        return {
            "path": str(path.resolve()),
            "sha256": digest.hexdigest(),
            "size_bytes": path.stat().st_size,
            "row_count": max(0, line_count - 1),
        }
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        count = 0
        first: int | None = None
        last: int | None = None
        for row in reader:
            count += 1
            if kind == "klines":
                value = int(row["open_time"])
                first = value if first is None else first
                last = value
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "row_count": count,
        **({"first_open_time": first, "last_open_time": last} if kind == "klines" else {}),
    }


def _reuse_existing_asset(
    client: PublicHttpClient,
    row: Mapping[str, Any],
    *,
    output_dir: Path,
    asof: datetime,
    end_ms: int,
    include_agg: bool,
) -> dict[str, Any] | None:
    """Rebuild a checkpoint after a network interruption without redownloading ZIPs."""
    symbol = str(row["symbol"])
    expected = {
        "klines": output_dir / f"{symbol}-1m.csv",
        "funding": output_dir / f"{symbol}-funding.json",
        "mark_price": output_dir / f"{symbol}-mark-price.csv",
        "premium_index": output_dir / f"{symbol}-premium-index.csv",
    }
    if include_agg:
        expected["agg_trades"] = output_dir / f"{symbol}-agg-trades.csv"
    if any(not path.exists() for path in expected.values()):
        return None
    try:
        files = {
            "klines": _read_csv_meta(expected["klines"], kind="klines"),
            "mark_price": _read_csv_meta(expected["mark_price"], kind="mark_price"),
            "premium_index": _read_csv_meta(expected["premium_index"], kind="premium_index"),
            "agg_trades": _read_csv_meta(expected["agg_trades"], kind="agg_trades")
            if include_agg
            else {"status": "NOT_DOWNLOADED", "reason": "explicitly_disabled_before_H1"},
        }
        funding_payload = json.loads(expected["funding"].read_text(encoding="utf-8"))
        funding_events = list(funding_payload.get("events") or [])
        files["funding"] = {
            "path": str(expected["funding"].resolve()),
            "sha256": __import__("hashlib").sha256(expected["funding"].read_bytes()).hexdigest(),
            "size_bytes": expected["funding"].stat().st_size,
            "row_count": len(funding_events),
            "first_funding_time": funding_events[0].get("funding_time") if funding_events else None,
            "last_funding_time": funding_events[-1].get("funding_time") if funding_events else None,
        }
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None
    start_ms, end_ms = _symbol_start_end(row, end_ms)
    return {
        "symbol": symbol,
        "tier": row.get("tier"),
        "base_asset": row.get("base_asset"),
        "listing": row.get("listing"),
        "start_time": iso_ms(start_ms),
        "end_time_exclusive": iso_ms(end_ms),
        "rules": row.get("rules"),
        "data_previously_viewed": True,
        "reused_existing_files": True,
        "files": files,
        "source_segments": {
            "status": "REUSED_PRIOR_CHECKSUM_VERIFIED_RUN",
            "note": "本进程在网络中断后复用已完成文件；原始 ZIP 官方 checksum 已在前一阶段验证。",
        },
        "missing_segments": [],
        "rest_requests": [],
        "funding_interval_hours": _median_interval_hours(
            [
                FundingEvent(
                    funding_time=int(event["funding_time"]),
                    funding_rate=float(event["funding_rate"]),
                    mark_price=(
                        float(event["mark_price"])
                        if event.get("mark_price") not in (None, "")
                        else None
                    ),
                )
                for event in funding_events
            ]
        ),
        "asof_utc": asof.isoformat(),
    }


def _audit_markdown(manifest: Mapping[str, Any]) -> str:
    lines = [
        "# Stock Perpetual 数据冻结审计",
        "",
        f"- 协议：`{manifest['protocol']}`",
        f"- 冻结时间：`{manifest['generated_at']}`",
        f"- Git：`{manifest['git_commit']}`",
        f"- 分支：`{manifest['git_branch']}`",
        f"- `data_previously_viewed`：`{manifest['data_previously_viewed']}`",
        "",
        "所有官方 ZIP 均在写入前校验相邻 `.CHECKSUM`；缺失归档被记录为缺口，未做插值。",
        "",
        "| Symbol | Tier | 1m rows | Funding events | Mark rows | Premium rows | Agg rows | Gaps |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for symbol, item in manifest["assets"].items():
        files = item.get("files", {})
        lines.append(
            f"| `{symbol}` | {item.get('tier')} | "
            f"{files.get('klines', {}).get('row_count', 0)} | "
            f"{files.get('funding', {}).get('row_count', 0)} | "
            f"{files.get('mark_price', {}).get('row_count', 0)} | "
            f"{files.get('premium_index', {}).get('row_count', 0)} | "
            f"{files.get('agg_trades', {}).get('row_count', 0)} | "
            f"{len(item.get('missing_segments', []))} |"
        )
    lines.extend(
        [
            "",
            f"冻结资产数：`{len(manifest['assets'])}`；跳过的短样本：`{len(manifest['skipped_symbols'])}`。",
            "",
        ]
    )
    return "\n".join(lines)


def freeze(args: argparse.Namespace) -> dict[str, Any]:
    discovery_path = Path(args.discovery)
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    asof, global_end_ms = _global_end_ms(discovery, args.as_of_utc)
    selected_tiers = {"TIER_A_CORE"}
    if args.include_tier_a_short:
        selected_tiers.add("TIER_A_SHORT")
    selected = [
        row for row in discovery["symbols"] if row.get("tier") in selected_tiers
    ]
    selected.sort(key=lambda row: str(row.get("symbol", "")))
    if args.max_symbols > 0:
        selected = selected[: args.max_symbols]
    if not selected:
        raise PublicDataError("发现报告中没有可冻结的 Tier A 标的。")
    output_dir = Path(args.output_dir)
    report_dir = Path(args.report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    proxy = load_proxy_config(args.proxy_url)
    assets: dict[str, Any] = {}
    with PublicHttpClient(proxy_config=proxy, pause_seconds=0.05) as client:
        for row in selected:
            symbol = str(row["symbol"])
            print(f"冻结 {symbol} ...", flush=True)
            reused = (
                _reuse_existing_asset(
                    client,
                    row,
                    output_dir=output_dir,
                    asof=asof,
                    end_ms=global_end_ms,
                    include_agg=not args.skip_agg_trades,
                )
                if args.resume_existing
                else None
            )
            assets[symbol] = reused or _write_asset(
                client,
                row,
                output_dir=output_dir,
                asof=asof,
                end_ms=global_end_ms,
                include_agg=not args.skip_agg_trades,
            )
    skipped = [
        {
            "symbol": row["symbol"],
            "tier": row.get("tier"),
            "reason": "not_in_formal_H1_freeze_set",
        }
        for row in discovery["symbols"]
        if row.get("tier") not in selected_tiers
    ]
    manifest = {
        "schema_version": 1,
        "protocol": "docs/codex-stock-perp-weekend-grid-backtest-v2.5.md",
        "generated_at": asof.isoformat(),
        "as_of_utc": asof.isoformat(),
        "last_complete_utc_date": (asof.date() - timedelta(days=1)).isoformat(),
        "git_branch": git_branch(),
        "git_commit": git_commit(),
        "discovery_path": str(discovery_path.resolve()),
        "discovery_sha256": __import__("hashlib").sha256(discovery_path.read_bytes()).hexdigest(),
        "data_previously_viewed": True,
        "direction_mode": "NEUTRAL",
        "leverage": 1,
        "selected_tiers": sorted(selected_tiers),
        "agg_trades_downloaded": not args.skip_agg_trades,
        "assets": assets,
        "skipped_symbols": skipped,
        "production_defaults_changed": False,
        "notes": [
            "只冻结正式 H1 所需的 Tier A-Core；Tier A-Short 保留在发现报告，不参与正式门槛。",
            "所有随机种子和窗口定义在收益计算前另由 window manifest 冻结。",
            "本次公开数据在协议探索阶段已查看，故明确标记 data_previously_viewed=true。",
        ],
    }
    manifest_path = report_dir / "asset-data-manifest.json"
    write_json(manifest_path, manifest)
    immutable_write(report_dir / "asset-data-audit.md", _audit_markdown(manifest))
    return manifest


def main() -> None:
    args = _parser().parse_args()
    try:
        result = freeze(args)
    except (PublicDataError, ValueError) as exc:
        raise SystemExit(f"数据冻结失败：{exc}") from exc
    print(
        json.dumps(
            {
                "manifest": str((Path(args.report_dir) / "asset-data-manifest.json").resolve()),
                "assets": sorted(result["assets"]),
                "skipped": len(result["skipped_symbols"]),
                "agg_trades_downloaded": result["agg_trades_downloaded"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
