"""Refresh post-freeze market data for the frozen semiconductor v2.9 OOS run.

This is a research-only command.  It downloads public USD-M market data into
the existing local research inputs, rejects any historical revision, records a
fresh exchange-rule *observation* without replacing the frozen rules, then
delegates eligible-window execution to the frozen 31111 monitor.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

from data_sources.binance_source import BINANCE_USDS_M_BASE_URL, BinanceHistoricalDataSource
from data_sources.models import FundingEvent, NormalizedKline
from scripts.freeze_semiconductor_grid_rules import extract_rule_snapshots
from scripts.semiconductor_grid_backtest import _audit_funding, _read_klines_with_audit
from scripts.semiconductor_grid_forward_oos_v29 import (
    DEFAULT_DATA,
    DEFAULT_OUTPUT,
    _collect_window_manifest,
    _parse_utc,
    _read_json_mapping,
)
from scripts.semiconductor_grid_forward_oos_v29_monitor import monitor_forward_oos
from strategy.semiconductor_grid_v29 import (
    FORWARD_OOS_SCENARIOS,
    PRIMARY_CANDIDATE_ID,
    ForwardOOSLedger,
    canonical_json_bytes,
    evaluate_forward_oos,
    file_sha256,
    production_safety_snapshot,
)


CSV_FIELDS = (
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
RULE_FIELDS = (
    "status",
    "contract_type",
    "tick_size",
    "step_size",
    "min_qty",
    "min_notional",
    "price_precision",
    "quantity_precision",
)


class DataRevisionDetected(RuntimeError):
    """Raised before any write when a public source contradicts local history."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh frozen v2.9 Forward OOS inputs and append new 31111 results"
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--run-time-utc", default="")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the freeze and report the planned fetch range without writes",
    )
    return parser


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _source_timestamp(value: int) -> str:
    return _iso(datetime.fromtimestamp(value / 1000, tz=UTC))


def _closed_minute(run_time: datetime) -> datetime:
    """Use the start of the current UTC minute as the exclusive fetch bound."""

    return run_time.astimezone(UTC).replace(second=0, microsecond=0)


def _kline_from_mapping(raw: Mapping[str, Any]) -> NormalizedKline:
    open_time = int(float(raw.get("open_time") or raw.get("timestamp") or 0))
    return NormalizedKline(
        open_time=open_time,
        close_time=int(float(raw.get("close_time") or open_time + 59_999)),
        open=float(raw["open"]),
        high=float(raw["high"]),
        low=float(raw["low"]),
        close=float(raw["close"]),
        volume=float(raw.get("volume") or 0),
        quote_volume=float(raw.get("quote_volume") or 0),
        trade_count=int(float(raw.get("trade_count") or 0)),
    )


def _kline_identity(value: NormalizedKline) -> tuple[int, int, float, float, float, float, float, float, int]:
    return (
        value.open_time,
        value.close_time,
        value.open,
        value.high,
        value.low,
        value.close,
        value.volume,
        value.quote_volume,
        value.trade_count,
    )


def _funding_identity(value: FundingEvent) -> tuple[int, float, float | None]:
    return (value.funding_time, value.funding_rate, value.mark_price)


def _read_existing_klines(path: Path) -> list[NormalizedKline]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return [_kline_from_mapping(row) for row in csv.DictReader(handle)]


def _read_existing_funding(path: Path) -> tuple[dict[str, Any], list[FundingEvent]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("events", []) if isinstance(payload, Mapping) else payload
    if not isinstance(records, list):
        raise ValueError(f"{path} funding events must be a list")
    events = [
        FundingEvent(
            int(item["funding_time"]),
            float(item["funding_rate"]),
            float(item["mark_price"])
            if item.get("mark_price") not in (None, "")
            else None,
        )
        for item in records
    ]
    if any(later.funding_time <= earlier.funding_time for earlier, later in zip(events, events[1:])):
        raise ValueError(f"{path} funding times are not strictly increasing")
    return (dict(payload) if isinstance(payload, Mapping) else {}, events)


def _merge_klines(
    existing: Sequence[NormalizedKline],
    fetched: Iterable[NormalizedKline],
    *,
    now_ms: int,
) -> tuple[list[NormalizedKline], dict[str, int]]:
    """Return strictly appended bars or fail closed on a historical revision."""

    previous = {item.open_time: item for item in existing}
    existing_last = existing[-1].open_time if existing else -1
    additions: list[NormalizedKline] = []
    fresh_by_time: dict[int, NormalizedKline] = {}
    duplicate_count = 0
    for item in fetched:
        if item.close_time >= now_ms:
            raise ValueError("future_or_unclosed_candle")
        prior_fresh = fresh_by_time.get(item.open_time)
        if prior_fresh is not None:
            duplicate_count += 1
            if _kline_identity(prior_fresh) != _kline_identity(item):
                raise DataRevisionDetected(
                    f"DATA_REVISION_DETECTED conflicting downloaded candle {item.open_time}"
                )
            continue
        fresh_by_time[item.open_time] = item
        prior = previous.get(item.open_time)
        if prior is not None:
            duplicate_count += 1
            if _kline_identity(prior) != _kline_identity(item):
                raise DataRevisionDetected(
                    f"DATA_REVISION_DETECTED historical candle {item.open_time}"
                )
            continue
        if item.open_time <= existing_last:
            raise DataRevisionDetected(
                f"DATA_REVISION_DETECTED non-contiguous historical candle {item.open_time}"
            )
        additions.append(item)
    additions.sort(key=lambda item: item.open_time)
    if any(later.open_time - earlier.open_time != 60_000 for earlier, later in zip(existing[-1:] + additions, additions)):
        raise ValueError("gap_detected_in_refreshed_candles")
    merged = [*existing, *additions]
    if any(later.open_time <= earlier.open_time for earlier, later in zip(merged, merged[1:])):
        raise ValueError("duplicate_or_unsorted_candles")
    return merged, {"duplicate_count": duplicate_count, "new_count": len(additions)}


def _merge_funding(
    existing: Sequence[FundingEvent], fetched: Iterable[FundingEvent], *, now_ms: int
) -> tuple[list[FundingEvent], dict[str, int]]:
    previous = {item.funding_time: item for item in existing}
    existing_last = existing[-1].funding_time if existing else -1
    additions: list[FundingEvent] = []
    fresh_by_time: dict[int, FundingEvent] = {}
    duplicate_count = 0
    for item in fetched:
        if item.funding_time >= now_ms:
            raise ValueError("future_funding_event")
        prior_fresh = fresh_by_time.get(item.funding_time)
        if prior_fresh is not None:
            duplicate_count += 1
            if _funding_identity(prior_fresh) != _funding_identity(item):
                raise DataRevisionDetected(
                    f"DATA_REVISION_DETECTED conflicting downloaded funding {item.funding_time}"
                )
            continue
        fresh_by_time[item.funding_time] = item
        prior = previous.get(item.funding_time)
        if prior is not None:
            duplicate_count += 1
            if _funding_identity(prior) != _funding_identity(item):
                raise DataRevisionDetected(
                    f"DATA_REVISION_DETECTED historical funding {item.funding_time}"
                )
            continue
        if item.funding_time <= existing_last:
            raise DataRevisionDetected(
                f"DATA_REVISION_DETECTED non-contiguous historical funding {item.funding_time}"
            )
        additions.append(item)
    additions.sort(key=lambda item: item.funding_time)
    merged = [*existing, *additions]
    if any(later.funding_time <= earlier.funding_time for earlier, later in zip(merged, merged[1:])):
        raise ValueError("duplicate_or_unsorted_funding")
    return merged, {"duplicate_count": duplicate_count, "new_count": len(additions)}


def _csv_bytes(rows: Sequence[NormalizedKline]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    for item in rows:
        writer.writerow(
            {
                "open_time": item.open_time,
                "close_time": item.close_time,
                "open": item.open,
                "high": item.high,
                "low": item.low,
                "close": item.close,
                "volume": item.volume,
                "quote_volume": item.quote_volume,
                "trade_count": item.trade_count,
            }
        )
    return buffer.getvalue().encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _append_csv_rows(path: Path, rows: Sequence[NormalizedKline]) -> None:
    if not rows:
        return
    existing = path.read_bytes()
    suffix = _csv_bytes(rows)
    if not existing.endswith((b"\n", b"\r")):
        suffix = b"\r\n" + suffix
    _atomic_write(path, existing + suffix)


def _funding_payload(
    existing_payload: Mapping[str, Any],
    events: Sequence[FundingEvent],
    *,
    refreshed_at: datetime,
) -> dict[str, Any]:
    payload = dict(existing_payload)
    payload.setdefault("schema_version", 1)
    payload.setdefault("source", "binance_rest")
    payload["events"] = [
        {
            "funding_time": item.funding_time,
            "funding_rate": item.funding_rate,
            "mark_price": item.mark_price,
        }
        for item in events
    ]
    refreshes = list(payload.get("refreshes") or [])
    refreshes.append({"refreshed_at_utc": _iso(refreshed_at), "source": "binance_rest"})
    payload["refreshes"] = refreshes
    return payload


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        ),
    )


def _write_immutable_json(path: Path, payload: Any) -> Path:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if path.exists():
        if path.read_bytes() == data:
            return path
        digest = _sha256_bytes(data)[:12]
        path = path.with_name(f"{path.stem}-{digest}{path.suffix}")
        if path.exists():
            if path.read_bytes() != data:
                raise RuntimeError(f"IMMUTABLE_SNAPSHOT_CONFLICT {path}")
            return path
    with path.open("xb") as handle:
        handle.write(data)
    return path


def _rule_change_details(
    frozen: Mapping[str, Any], observed: Mapping[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    frozen_symbols = dict(frozen.get("symbols") or {})
    observed_symbols = dict(observed.get("symbols") or {})
    changes: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol in sorted(set(frozen_symbols) | set(observed_symbols)):
        old = dict(frozen_symbols.get(symbol) or {})
        new = dict(observed_symbols.get(symbol) or {})
        fields = {
            name: {"frozen": old.get(name), "observed": new.get(name)}
            for name in RULE_FIELDS
            if old.get(name) != new.get(name)
        }
        if fields:
            changes[symbol] = fields
    return changes


async def _fetch_rule_observation(symbols: Sequence[str], checked_at: datetime) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "QuietGrid/v2.9 OOS refresh"}) as client:
        response = await client.get(f"{BINANCE_USDS_M_BASE_URL}/fapi/v1/exchangeInfo")
        response.raise_for_status()
        payload = response.json()
        raw_bytes = response.content
    return {
        "schema_version": 1,
        "observation_type": "POST_FREEZE_RULE_OBSERVATION",
        "observed_at_utc": _iso(checked_at),
        "source_url": f"{BINANCE_USDS_M_BASE_URL}/fapi/v1/exchangeInfo",
        "source_sha256": _sha256_bytes(raw_bytes),
        "symbols": extract_rule_snapshots(payload, list(symbols)),
    }


async def _refresh_inputs(
    *,
    data_dir: Path,
    symbols: Sequence[str],
    checked_at: datetime,
) -> list[dict[str, Any]]:
    now_ms = int(checked_at.timestamp() * 1000)
    fetch_end = _closed_minute(checked_at)
    rows: list[dict[str, Any]] = []
    async with BinanceHistoricalDataSource(
        validate_symbol_listing=False,
        pause_seconds=0.0,
        timeout_seconds=20.0,
        retry_attempts=3,
    ) as source:
        for symbol in symbols:
            csv_path = data_dir / f"{symbol}-1m.csv"
            funding_path = csv_path.with_suffix(".funding.json")
            original_csv_bytes = csv_path.read_bytes()
            original_funding_bytes = funding_path.read_bytes()
            existing_bars = _read_existing_klines(csv_path)
            existing_funding_payload, existing_funding = _read_existing_funding(funding_path)
            if not existing_bars:
                raise ValueError(f"{symbol} has no frozen historical bars")
            start = datetime.fromtimestamp(
                (existing_bars[-1].open_time + 60_000) / 1000, tz=UTC
            )
            funding_start = datetime.fromtimestamp(
                (existing_funding[-1].funding_time + 1) / 1000, tz=UTC
            )
            fetched_bars = (
                [
                    item
                    async for item in source.fetch_klines(symbol, "1m", start, fetch_end)
                ]
                if start < fetch_end
                else []
            )
            fetched_funding = (
                [
                    item
                    async for item in source.fetch_funding(symbol, funding_start, fetch_end)
                ]
                if funding_start < fetch_end
                else []
            )
            merged_bars, bar_merge = _merge_klines(
                existing_bars, fetched_bars, now_ms=now_ms
            )
            merged_funding, funding_merge = _merge_funding(
                existing_funding, fetched_funding, now_ms=now_ms
            )
            # Reuse the production research audit after the merged data is fully in memory.
            _validate_merged_bars(symbol, merged_bars, now_ms)
            _validate_merged_funding(symbol, merged_bars, merged_funding, funding_path)
            rows.append(
                {
                    "symbol": symbol,
                    "csv_path": str(csv_path),
                    "funding_path": str(funding_path),
                    "old_csv_sha256": _sha256_bytes(original_csv_bytes),
                    "old_funding_sha256": _sha256_bytes(original_funding_bytes),
                    "old_latest_bar": _source_timestamp(existing_bars[-1].open_time),
                    "old_latest_funding": _source_timestamp(existing_funding[-1].funding_time),
                    "new_latest_bar": _source_timestamp(merged_bars[-1].open_time),
                    "new_latest_funding": _source_timestamp(merged_funding[-1].funding_time),
                    "new_bar_count": bar_merge["new_count"],
                    "new_funding_count": funding_merge["new_count"],
                    "duplicate_count": bar_merge["duplicate_count"],
                    "funding_duplicate_count": funding_merge["duplicate_count"],
                    "gap_count": 0,
                    "invalid_bar_count": 0,
                    "_original_csv": original_csv_bytes,
                    "_original_funding_payload": existing_funding_payload,
                    "_original_funding_events": existing_funding,
                    "_new_bars": merged_bars[len(existing_bars) :],
                    "_merged_funding": merged_funding,
                }
            )
    return rows


def _validate_merged_bars(symbol: str, rows: Sequence[NormalizedKline], now_ms: int) -> None:
    if any(item.close_time >= now_ms for item in rows):
        raise ValueError(f"{symbol} contains future_or_unclosed_candle")
    if any(later.open_time - earlier.open_time != 60_000 for earlier, later in zip(rows, rows[1:])):
        raise ValueError(f"{symbol} gap_detected")
    if any(later.open_time <= earlier.open_time for earlier, later in zip(rows, rows[1:])):
        raise ValueError(f"{symbol} duplicate_candle")


def _validate_merged_funding(
    symbol: str,
    bars: Sequence[NormalizedKline],
    events: Sequence[FundingEvent],
    original_path: Path,
) -> None:
    if any(later.funding_time <= earlier.funding_time for earlier, later in zip(events, events[1:])):
        raise ValueError(f"{symbol} duplicate_funding")
    temporary = original_path.with_name(f".{original_path.name}.audit.json")
    try:
        _write_json(
            temporary,
            {
                "events": [
                    {
                        "funding_time": item.funding_time,
                        "funding_rate": item.funding_rate,
                        "mark_price": item.mark_price,
                    }
                    for item in events
                ]
            },
        )
        audit = _audit_funding(
            [
                {
                    "open_time": item.open_time,
                    "close_time": item.close_time,
                }
                for item in bars
            ],
            events,
            temporary,
        )
    finally:
        temporary.unlink(missing_ok=True)
    if not audit["ok"]:
        raise ValueError(f"{symbol} funding_audit_failed: {audit['notes']}")


def _commit_refreshed_inputs(rows: Sequence[Mapping[str, Any]], checked_at: datetime) -> None:
    for row in rows:
        csv_path = Path(str(row["csv_path"]))
        funding_path = Path(str(row["funding_path"]))
        original_csv = bytes(row["_original_csv"])
        if csv_path.read_bytes() != original_csv:
            raise DataRevisionDetected("DATA_REVISION_DETECTED local CSV changed during refresh")
        _append_csv_rows(csv_path, list(row["_new_bars"]))
        payload = _funding_payload(
            dict(row["_original_funding_payload"]),
            list(row["_merged_funding"]),
            refreshed_at=checked_at,
        )
        original_events = list(row["_original_funding_events"])
        if payload["events"][: len(original_events)] != [
            {
                "funding_time": item.funding_time,
                "funding_rate": item.funding_rate,
                "mark_price": item.mark_price,
            }
            for item in original_events
        ]:
            raise DataRevisionDetected("DATA_REVISION_DETECTED funding historical prefix")
        if int(row.get("new_funding_count") or 0) > 0:
            _write_json(funding_path, payload)
        row["new_csv_sha256"] = file_sha256(csv_path)
        row["new_funding_sha256"] = file_sha256(funding_path)


@contextmanager
def _refresh_lock(output: Path):
    path = output / ".forward-oos-refresh.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Forward OOS data refresh is already running: {path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"pid={os.getpid()}\n")
        yield
    finally:
        path.unlink(missing_ok=True)


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _has_material_refresh(execution: Mapping[str, Any]) -> bool:
    rows = execution.get("rows") or []
    row_activity = any(
        int(row.get("new_bar_count") or 0) > 0
        or int(row.get("new_funding_count") or 0) > 0
        for row in rows
        if isinstance(row, Mapping)
    )
    monitor = execution.get("monitor") or {}
    return row_activity or int(monitor.get("appended_rows") or 0) > 0


def _material_refresh_snapshot(execution: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "checked_at_utc",
        "rows",
        "rule_observation",
        "theoretical_post_cutoff_window_count",
        "eligible_post_cutoff_window_count",
        "monitor",
        "ledger",
    )
    return {field: execution[field] for field in fields if field in execution}


def _select_material_refresh(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep the latest non-noop refresh evidence across idempotent executions."""

    if _has_material_refresh(current):
        return _material_refresh_snapshot(current)
    if previous:
        retained = previous.get("latest_material_refresh")
        if isinstance(retained, Mapping):
            return dict(retained)
        if _has_material_refresh(previous):
            return _material_refresh_snapshot(previous)
    return _material_refresh_snapshot(current)


def _execution_record(execution: Mapping[str, Any]) -> dict[str, Any]:
    rows = [row for row in execution.get("rows") or [] if isinstance(row, Mapping)]
    monitor = execution.get("monitor") or {}
    ledger = execution.get("ledger") or {}
    return {
        "checked_at_utc": execution.get("checked_at_utc"),
        "new_bar_count": sum(int(row.get("new_bar_count") or 0) for row in rows),
        "new_funding_count": sum(int(row.get("new_funding_count") or 0) for row in rows),
        "new_complete_window_count": int(monitor.get("new_complete_window_count") or 0),
        "appended_rows": int(monitor.get("appended_rows") or 0),
        "ledger_old_sha256": ledger.get("old_sha256"),
        "ledger_new_sha256": ledger.get("new_sha256"),
        "historical_prefix_unchanged": ledger.get("historical_prefix_unchanged"),
    }


def _execution_history(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if previous:
        prior_history = previous.get("execution_history")
        if isinstance(prior_history, list):
            history.extend(dict(item) for item in prior_history if isinstance(item, Mapping))
        else:
            history.append(_execution_record(previous))
    history.append(_execution_record(current))
    return history


def _write_audit_report(
    path: Path,
    *,
    checked_at: datetime,
    candidate_sha: str,
    exposure_cutoff: str,
    refresh_rows: Sequence[Mapping[str, Any]],
    rules_changed: bool,
    rule_changes: Mapping[str, Any],
) -> None:
    lines = [
        "# Forward OOS Data Refresh Audit",
        "",
        f"- Checked at UTC: `{_iso(checked_at)}`",
        f"- Candidate SHA: `{candidate_sha}`",
        f"- Exposure cutoff (unchanged): `{exposure_cutoff}`",
        f"- Rule observation: `{'RULE_CHANGE_DETECTED' if rules_changed else 'RULES_UNCHANGED'}`",
        "",
        "| Symbol | Old latest bar | New latest bar | New bars | Old latest funding | New latest funding | New funding | Duplicates | Gaps | Invalid bars |",
        "|---|---|---|---:|---|---|---:|---:|---:|---:|",
    ]
    for row in refresh_rows:
        lines.append(
            "| {symbol} | {old_latest_bar} | {new_latest_bar} | {new_bar_count} | "
            "{old_latest_funding} | {new_latest_funding} | {new_funding_count} | "
            "{duplicate_count} | {gap_count} | {invalid_bar_count} |".format(**row)
        )
    if rule_changes:
        lines.extend(["", "## Rule Changes", "", "```json", json.dumps(rule_changes, ensure_ascii=False, indent=2), "```"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _write_status_report(
    path: Path,
    *,
    checked_at: datetime,
    candidate: Mapping[str, Any],
    assessment: Mapping[str, Any],
    refresh_rows: Sequence[Mapping[str, Any]],
    monitor_result: Mapping[str, Any],
) -> None:
    latest = max((str(row["new_latest_bar"]) for row in refresh_rows), default="NONE")
    lines = [
        "# Semiconductor Grid Forward OOS v2.9.1 Status",
        "",
        f"状态日期：`{checked_at.date().isoformat()}`",
        "",
        "## 当前结论",
        "",
        f"`{assessment['conclusion_code']}`",
        "",
        f"- 冻结候选：`{candidate['candidate_id']}`",
        f"- Candidate SHA：`{file_sha256(path.parent / 'candidate-31111-freeze.json')}`",
        f"- Exposure cutoff（永久冻结）：`{candidate['exposure_cutoff']}`",
        f"- 最新市场数据时间：`{latest}`",
        f"- 上次 monitor 执行：`{monitor_result['checked_at_utc']}`",
        f"- 本次新合格窗口：`{monitor_result['new_complete_window_count']}`",
        f"- 本次新增 OOS 行：`{monitor_result['appended_rows']}`",
        f"- 完整 Forward OOS：`{assessment['complete_forward_oos_windows']}/{assessment['required_forward_oos_windows']}`",
        f"- 正式验收：`{assessment['formal_assessment_status']}`",
        "",
        "## 场景累计",
        "",
        "| Scenario | Net PnL | Median window PnL | Positive window ratio | Profit factor | Max drawdown | Inventory drag ratio |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario in FORWARD_OOS_SCENARIOS:
        item = assessment["scenarios"][scenario]
        lines.append(
            f"| {scenario} | {item['net_pnl']} | {item['median_window_pnl']} | "
            f"{item['positive_window_ratio']} | {item['profit_factor']} | "
            f"{item['max_drawdown']} | {item['inventory_drag_ratio']} |"
        )
    lines.extend(
        [
            "",
            "## 冻结与安全边界",
            "",
            "- `31111-NEUTRAL` 的参数、标的、场景、seed 和 exposure cutoff 均未修改。",
            "- 不允许参数搜索、候选重选或以本次 OOS 结果调参。",
            "- 冻结 exchange rules 未被覆盖；当前规则只作为 observation 单独记录。",
            "- 未修改生产配置，自动交易仍关闭，经济杠杆为 `1x`。",
            "- Forward OOS ledger 只允许追加；历史前缀在运行前后校验。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _write_final_report(
    path: Path,
    *,
    checked_at: datetime,
    candidate: Mapping[str, Any],
    refresh_rows: Sequence[Mapping[str, Any]],
    rules_changed: bool,
    theoretical_windows: int,
    eligible_windows: int,
    monitor_result: Mapping[str, Any],
    assessment: Mapping[str, Any],
    safety: Mapping[str, Any],
    latest_execution: Mapping[str, Any],
) -> None:
    primary = assessment["scenarios"]["PRIMARY_ZERO_MAKER"]
    stress = assessment["scenarios"]["EXECUTION_STRESS"]
    maker_off = assessment["scenarios"]["MAKER_PROMO_OFF"]
    lines = [
        "# Semiconductor Grid v2.9.1 Forward OOS Data Refresh Report",
        "",
        f"1. 数据抓取时间：`{_iso(checked_at)}`。",
        "2. 各标的旧/新数据截止与增量如下。",
        "",
        "| Symbol | Old latest bar | New latest bar | New 1m bars | Old latest funding | New latest funding | New funding events |",
        "|---|---|---|---:|---|---|---:|",
    ]
    for row in refresh_rows:
        lines.append(
            "| {symbol} | {old_latest_bar} | {new_latest_bar} | {new_bar_count} | "
            "{old_latest_funding} | {new_latest_funding} | {new_funding_count} |".format(**row)
        )
    lines.extend(
        [
            "",
            f"3. Historical data revision：`{'DATA_REVISION_DETECTED' if False else 'NONE'}`。",
            f"4. Exposure cutoff 仍固定为：`{candidate['exposure_cutoff']}`。",
            f"5. cutoff 后理论 closed-market windows：`{theoretical_windows}`。",
            f"6. 其中符合 Forward OOS 的完整 portfolio windows：`{eligible_windows}`。",
            f"7. 本次 append 的主候选 ledger rows：`{monitor_result['appended_rows']}`。",
            f"8. 当前 Forward OOS：`{assessment['complete_forward_oos_windows']}/{assessment['required_forward_oos_windows']}`。",
            f"9. PRIMARY net PnL：`{primary['net_pnl']}`。",
            f"10. EXECUTION_STRESS net PnL：`{stress['net_pnl']}`。",
            f"11. MAKER_PROMO_OFF net PnL：`{maker_off['net_pnl']}`。",
            "",
            "## Symbol Breakdown",
            "",
            "| Symbol | Net PnL | Profit factor | Positive window ratio | Max drawdown | Inventory drag | Complete windows |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in assessment["symbol_breakdown"]:
        lines.append(
            f"| {row['symbol']} | {row['net_pnl']} | {row['profit_factor']} | "
            f"{row['positive_window_ratio']} | {row['max_drawdown']} | "
            f"{row['inventory_drag']} | {row['complete_forward_oos_windows']} |"
        )
    lines.extend(
        [
            "",
            f"- PRIMARY 最大回撤：`{primary['max_drawdown']}` / `{primary['max_drawdown_pct']}`。",
            f"- PRIMARY inventory drag ratio：`{primary['inventory_drag_ratio']}`。",
            f"- 规则观察：`{'RULE_CHANGE_DETECTED' if rules_changed else 'RULES_UNCHANGED'}`。",
            "- 数据质量：`OK`；未发现 duplicate candles、gaps、invalid bars 或 historical revision。",
            f"- 自动交易仍关闭：`{not bool(safety.get('startup_auto_entry')) and safety.get('safe')}`。",
            f"- 当前结论：`{assessment['conclusion_code']}`。",
            "",
            "## Latest Execution / Idempotency",
            "",
            f"- 最近执行时间：`{latest_execution['checked_at_utc']}`。",
            f"- 新增 1m bars：`{sum(int(row.get('new_bar_count') or 0) for row in latest_execution['rows'])}`。",
            f"- 新增 funding events：`{sum(int(row.get('new_funding_count') or 0) for row in latest_execution['rows'])}`。",
            f"- 新完整窗口：`{latest_execution['monitor']['new_complete_window_count']}`。",
            f"- 新增 ledger rows：`{latest_execution['monitor']['appended_rows']}`。",
            f"- Ledger SHA（执行前/后）：`{latest_execution['ledger']['old_sha256']}` / `{latest_execution['ledger']['new_sha256']}`。",
            f"- 历史前缀未变化：`{latest_execution['ledger']['historical_prefix_unchanged']}`。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _rewrite_assessment_artifacts(output: Path, assessment: Mapping[str, Any]) -> None:
    _write_json(output / "forward-oos-summary.json", assessment)
    _write_json(output / "acceptance-gates.json", assessment)
    buffer = io.StringIO(newline="")
    fields = (
        "symbol",
        "net_pnl",
        "profit_factor",
        "positive_window_ratio",
        "inventory_drag",
        "max_drawdown",
        "complete_forward_oos_windows",
    )
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(assessment["symbol_breakdown"])
    _atomic_write(output / "symbol-breakdown.csv", buffer.getvalue().encode("utf-8"))


def refresh_forward_oos(
    *,
    output_dir: str | Path = DEFAULT_OUTPUT,
    data_dir: str | Path = DEFAULT_DATA,
    run_time_utc: datetime | str | None = None,
    check_only: bool = False,
) -> dict[str, Any]:
    output = Path(output_dir)
    data = Path(data_dir)
    checked_at = _parse_utc(run_time_utc or datetime.now(UTC))
    candidate_path = output / "candidate-31111-freeze.json"
    candidate = _read_json_mapping(candidate_path)
    candidate_sha = file_sha256(candidate_path)
    if candidate.get("candidate_id") != PRIMARY_CANDIDATE_ID:
        raise RuntimeError("FAIL_CANDIDATE_FREEZE_MISMATCH candidate id")
    if not bool(dict(candidate.get("selection_lock") or {}).get("parameter_search_allowed") is False):
        raise RuntimeError("FAIL_CANDIDATE_FREEZE_MISMATCH parameter search lock")
    expected_cutoff = "2026-08-08T20:45:23.438783+00:00"
    if str(candidate.get("exposure_cutoff")) != expected_cutoff:
        raise RuntimeError("FAIL_CANDIDATE_FREEZE_MISMATCH exposure cutoff")
    registry = _read_json_mapping(output / "candidate-registry.json")
    primary = list(registry.get("primary_forward_oos_candidates") or [{}])[0]
    if primary.get("candidate_sha") != candidate_sha:
        raise RuntimeError("FAIL_CANDIDATE_FREEZE_MISMATCH candidate sha")
    symbols = tuple(str(symbol) for symbol in dict(candidate.get("symbol_universe") or {}))
    if not symbols:
        raise RuntimeError("FAIL_CANDIDATE_FREEZE_MISMATCH symbol universe")
    frozen_rules = _read_json_mapping(output / "exchange-rules.json")
    config = _read_json_mapping(output / "config-freeze.json")
    frozen_sections = dict(config.get("frozen_sections") or {})
    safety = production_safety_snapshot(frozen_sections)
    if not safety["safe"]:
        raise RuntimeError("Frozen production safety contract is invalid")
    ledger = ForwardOOSLedger(output / "forward-oos-ledger.csv", output / "forward-oos-ledger.json")
    before_records = ledger.records()
    before_csv = ledger.csv_path.read_bytes()
    before_json = ledger.json_path.read_bytes()
    data_manifest_path = output / "data-refresh-manifest.json"
    previous_data_manifest = (
        _read_json_mapping(data_manifest_path) if data_manifest_path.exists() else None
    )
    if check_only:
        ranges = []
        for symbol in symbols:
            bars = _read_existing_klines(data / f"{symbol}-1m.csv")
            _, funding = _read_existing_funding(data / f"{symbol}-1m.funding.json")
            ranges.append(
                {
                    "symbol": symbol,
                    "start": _source_timestamp(bars[-1].open_time + 60_000),
                    "funding_start": _source_timestamp(funding[-1].funding_time + 1),
                    "end": _iso(_closed_minute(checked_at)),
                }
            )
        return {
            "mode": "CHECK_ONLY",
            "candidate_id": PRIMARY_CANDIDATE_ID,
            "candidate_sha": candidate_sha,
            "exposure_cutoff": expected_cutoff,
            "planned_ranges": ranges,
            "ledger_sha256": _sha256_bytes(before_csv),
            "ledger_row_count": len(before_records),
            "production_safety": safety,
        }
    output.mkdir(parents=True, exist_ok=True)
    with _refresh_lock(output):
        refreshed = asyncio.run(
            _refresh_inputs(data_dir=data, symbols=symbols, checked_at=checked_at)
        )
        observation = asyncio.run(_fetch_rule_observation(symbols, checked_at))
        changes = _rule_change_details(frozen_rules, observation)
        observation["frozen_rules_sha256"] = file_sha256(output / "exchange-rules.json")
        observation["comparison_status"] = (
            "RULE_CHANGE_DETECTED" if changes else "RULES_UNCHANGED"
        )
        observation["rule_changes"] = changes
        observation_path = output / f"exchange-rules-observation-{checked_at.strftime('%Y%m%dT%H%M%SZ')}.json"
        observation_path = _write_immutable_json(observation_path, observation)
        if changes:
            # A changed public trading rule cannot silently become part of the frozen
            # execution contract.  Do not write research inputs or append OOS rows.
            raise RuntimeError("RULE_CHANGE_DETECTED frozen rules no longer match observation")
        _commit_refreshed_inputs(refreshed, checked_at)
        public_rows = [_public_row(row) for row in refreshed]
        manifest, _input_audit = _collect_window_manifest(
            raw_config=frozen_sections,
            data_dir=data,
            rules_path=output / "exchange-rules.json",
            exposure_cutoff=_parse_utc(expected_cutoff),
        )
        post_cutoff = {
            str(row["window_key"])
            for row in manifest
            if _parse_utc(str(row["window_start"])) > _parse_utc(expected_cutoff)
        }
        eligible = {
            str(row["window_key"])
            for row in manifest
            if bool(row.get("oos_eligible")) and bool(row.get("complete_window"))
        }
        _write_json(
            output / "data-refresh-window-manifest.json",
            {
                "checked_at_utc": _iso(checked_at),
                "exposure_cutoff": expected_cutoff,
                "rules_frozen": True,
                "windows": manifest,
            },
        )
        monitor_result = monitor_forward_oos(
            output_dir=output,
            data_dir=data,
            run_time_utc=checked_at,
        )
        after_records = ledger.records()
        after_csv = ledger.csv_path.read_bytes()
        after_json = ledger.json_path.read_bytes()
        if not after_csv.startswith(before_csv) or not after_json.startswith(before_json.rstrip()[:-1]):
            # The monitor has its own stronger structural prefix check; this is a
            # cheap second guard around the refresh orchestration.
            if after_records[: len(before_records)] != before_records:
                raise RuntimeError("FAIL_LEDGER_HISTORY_MUTATED")
        assessment = evaluate_forward_oos(after_records)
        _rewrite_assessment_artifacts(output, assessment)
        _write_status_report(
            output / "forward_oos_status.md",
            checked_at=checked_at,
            candidate=candidate,
            assessment=assessment,
            refresh_rows=public_rows,
            monitor_result=monitor_result,
        )
        data_manifest = {
            "schema_version": 2,
            "mode": "FORWARD_OOS_DATA_REFRESH_AND_APPEND",
            "checked_at_utc": _iso(checked_at),
            "candidate_id": PRIMARY_CANDIDATE_ID,
            "candidate_sha": candidate_sha,
            "exposure_cutoff": expected_cutoff,
            "parameter_search_allowed": False,
            "rows": public_rows,
            "rule_observation": {
                "path": str(observation_path),
                "sha256": file_sha256(observation_path),
                "status": observation["comparison_status"],
                "rule_changes": changes,
            },
            "theoretical_post_cutoff_window_count": len(post_cutoff),
            "eligible_post_cutoff_window_count": len(eligible),
            "monitor": monitor_result,
            "ledger": {
                "old_sha256": _sha256_bytes(before_csv),
                "new_sha256": _sha256_bytes(after_csv),
                "old_row_count": len(before_records),
                "new_row_count": len(after_records),
                "historical_prefix_unchanged": after_records[: len(before_records)] == before_records,
            },
            "production_safety": safety,
        }
        material_refresh = _select_material_refresh(previous_data_manifest, data_manifest)
        data_manifest["latest_material_refresh"] = material_refresh
        data_manifest["execution_history"] = _execution_history(
            previous_data_manifest, data_manifest
        )
        _write_json(data_manifest_path, data_manifest)
        material_rows = list(material_refresh.get("rows") or public_rows)
        material_monitor = dict(material_refresh.get("monitor") or monitor_result)
        material_rule = dict(material_refresh.get("rule_observation") or {})
        material_rule_changes = dict(material_rule.get("rule_changes") or {})
        _write_audit_report(
            output / "data-refresh-audit.md",
            checked_at=_parse_utc(str(material_refresh.get("checked_at_utc") or _iso(checked_at))),
            candidate_sha=candidate_sha,
            exposure_cutoff=expected_cutoff,
            refresh_rows=material_rows,
            rules_changed=material_rule.get("status") == "RULE_CHANGE_DETECTED",
            rule_changes=material_rule_changes,
        )
        _write_final_report(
            output / "data-refresh-report.md",
            checked_at=_parse_utc(str(material_refresh.get("checked_at_utc") or _iso(checked_at))),
            candidate=candidate,
            refresh_rows=material_rows,
            rules_changed=material_rule.get("status") == "RULE_CHANGE_DETECTED",
            theoretical_windows=int(
                material_refresh.get("theoretical_post_cutoff_window_count")
                or len(post_cutoff)
            ),
            eligible_windows=int(
                material_refresh.get("eligible_post_cutoff_window_count") or len(eligible)
            ),
            monitor_result=material_monitor,
            assessment=assessment,
            safety=safety,
            latest_execution=data_manifest,
        )
        return data_manifest


def main() -> None:
    args = _parser().parse_args()
    result = refresh_forward_oos(
        output_dir=args.output_dir,
        data_dir=args.data_dir,
        run_time_utc=args.run_time_utc or None,
        check_only=args.check_only,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
