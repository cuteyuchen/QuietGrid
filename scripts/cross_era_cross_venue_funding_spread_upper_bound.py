from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_funding_carry_upper_bound as round22
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round25-cross-venue-funding-spread-upper-bound-protocol.md"
)
PROTOCOL_SHA256 = "4637c7c844c68322a9c2d93fe2a6384f435637d4ee44f22b3a98123c1ffcfba8"
ROUND22_RESULT_PATH = Path(
    "reports/cross-era-oos/round22-funding-carry-upper-bound-results.json"
)
ROUND22_RESULT_SHA256 = "622d359710b3f4e6f6371211a946ae4f33ed24510d5d1262def7ada29c47ab41"
ROUND24_RESULT_PATH = Path(
    "reports/cross-era-oos/round24-cross-asset-premium-dispersion-upper-bound-results.json"
)
ROUND24_RESULT_SHA256 = "c9dafcbf47770711a998b58bbb02f1c5a56d967bc408d0619ec93a04659f79b3"
BITMEX_DATA_PROTOCOL_SHA256 = (
    "fa45fc7d07fea75a5bc98a0cfdd07773002c292d2c71732639e7f6f2834dbe53"
)
BITMEX_AVAILABILITY_AUDIT_SHA256 = (
    "e2aee11005bef69beb2faa0f6ecced8154fac154be5c828817b33467c6d88d18"
)

EXPECTED_EVENT_COUNT = 5_928
EXPECTED_PAGE_COUNT = 13
EXCLUDED_START = datetime(2023, 7, 1, tzinfo=UTC)
EXCLUDED_END = datetime(2024, 8, 1, tzinfo=UTC)
AUTHORIZED_SEGMENTS = (
    ("AUTHORIZED_HISTORY", datetime(2020, 1, 1, tzinfo=UTC), EXCLUDED_START, 3_831),
    ("POSTHISTORY", EXCLUDED_END, datetime(2026, 7, 1, tzinfo=UTC), 2_097),
)
WINDOW_GROUPS = (
    ("PREHISTORY", "external", 28),
    ("CURRENT", "development", 108),
    ("CURRENT", "validation_complete_months", 49),
    ("POSTHISTORY", "external", 108),
)
SCENARIO_FEE_RATES = {"BASE": 0.0002, "COST50": 0.0003}
ASSET_CONFIG = {
    "BTC": {
        "binance_symbol": "BTCUSDT",
        "bitmex_symbol": "XBTUSD",
        "gross_capital": 500.0,
        "binance_manifest": Path(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json"
        ),
        "binance_manifest_sha256": (
            "a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57"
        ),
        "binance_csv_sha256": (
            "08a4fec97e9e2555d28135fc70f49d6115b966868fc912fc29faab80b722c5e2"
        ),
        "bitmex_manifest": Path(
            "data/backtests/round25_cross_venue_funding/"
            "bitmex_funding_xbtusd_202001_202306_202408_202606.manifest.json"
        ),
        "bitmex_manifest_sha256": (
            "4474476261ec4cb9c815c74993dc4b83e57eec55dcf1b887006b81b45a93162c"
        ),
        "bitmex_csv_sha256": (
            "d0c1256650e2be768f4c1541fdd837e15527bae50d4dca528295a822fd72c247"
        ),
    },
    "ETH": {
        "binance_symbol": "ETHUSDT",
        "bitmex_symbol": "ETHUSD",
        "gross_capital": 300.0,
        "binance_manifest": Path(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json"
        ),
        "binance_manifest_sha256": (
            "19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f"
        ),
        "binance_csv_sha256": (
            "5ec93a6a3d7397fbe0e6d3b82c28873e11b2559b1be34c7d351b90e5d7b9108a"
        ),
        "bitmex_manifest": Path(
            "data/backtests/round25_cross_venue_funding/"
            "bitmex_funding_ethusd_202001_202306_202408_202606.manifest.json"
        ),
        "bitmex_manifest_sha256": (
            "e052002c6308b226f22fc22f17c6de90b8f3cdad1fba4d42b575af40b55f03ed"
        ),
        "bitmex_csv_sha256": (
            "1b82811d30e819fdbba2623b7ba07722b0a917c804b13eefe40744ebd3a20457"
        ),
    },
}


def _parse_utc(value: Any, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} 不是合法 ISO-8601 时间。") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 缺少时区。")
    return parsed.astimezone(UTC)


def _iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _validate_event_series(
    events: Sequence[Mapping[str, Any]],
    *,
    label: str,
    require_strict_cadence: bool,
) -> dict[str, Any]:
    if len(events) != EXPECTED_EVENT_COUNT:
        raise ValueError(f"{label} funding event 数量不一致。")
    times = [int(item["funding_time"]) for item in events]
    if times != sorted(times) or len(times) != len(set(times)):
        raise ValueError(f"{label} funding_time 未严格递增或包含重复事件。")
    if any(int(item["funding_interval_hours"]) != 8 for item in events):
        raise ValueError(f"{label} funding interval 不是 8 小时。")
    if any(not math.isfinite(float(item["funding_rate"])) for item in events):
        raise ValueError(f"{label} funding rate 包含非有限数。")

    excluded_start_ms = int(EXCLUDED_START.timestamp() * 1000)
    excluded_end_ms = int(EXCLUDED_END.timestamp() * 1000)
    if any(excluded_start_ms <= value < excluded_end_ms for value in times):
        raise ValueError(f"{label} funding 数据触碰隔离区间。")

    segment_audit: dict[str, Any] = {}
    covered: set[int] = set()
    for name, start, end, expected_count in AUTHORIZED_SEGMENTS:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        selected = [value for value in times if start_ms <= value < end_ms]
        if len(selected) != expected_count:
            raise ValueError(
                f"{label} {name} funding event 数量不一致: "
                f"{len(selected)} != {expected_count}。"
            )
        cadence_gaps = [
            current - previous
            for previous, current in zip(selected, selected[1:])
            if current - previous != 8 * 60 * 60 * 1000
        ]
        if require_strict_cadence and cadence_gaps:
            raise ValueError(f"{label} {name} funding cadence 不是严格 8 小时。")
        covered.update(selected)
        segment_audit[name] = {
            "event_count": len(selected),
            "first_event": _iso_ms(selected[0]),
            "last_event": _iso_ms(selected[-1]),
            "cadence_hours": 8,
            "strict_cadence_required": require_strict_cadence,
            "strict_cadence_verified": not cadence_gaps,
            "cadence_gap_count": len(cadence_gaps),
            "maximum_gap_hours": (
                max(cadence_gaps) / (60 * 60 * 1000) if cadence_gaps else 8
            ),
        }
    if covered != set(times):
        raise ValueError(f"{label} funding 数据包含授权段外事件。")
    return {
        "event_count": len(events),
        "duplicate_events": 0,
        "strictly_increasing": True,
        "interval_hours": 8,
        "excluded_interval_untouched": True,
        "segments": segment_audit,
        "passed": True,
    }


def _read_binance_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_csv_sha256: str,
    expected_symbol: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    manifest, events = round22._read_funding_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
        expected_symbol=expected_symbol,
    )
    if str(manifest.get("file_sha256")) != expected_csv_sha256:
        raise ValueError(f"{expected_symbol} Binance funding CSV 固定哈希不一致。")
    audit = _validate_event_series(
        events,
        label=f"Binance {expected_symbol}",
        require_strict_cadence=False,
    )
    return manifest, events, audit


def _read_bitmex_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_csv_sha256: str,
    expected_symbol: str,
    expected_binance_symbol: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    path = manifest_path.resolve()
    if _sha256(path) != expected_manifest_sha256:
        raise ValueError(f"{expected_symbol} BitMEX funding manifest 哈希不一致。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = {
        "data_protocol_sha256": BITMEX_DATA_PROTOCOL_SHA256,
        "availability_audit_sha256": BITMEX_AVAILABILITY_AUDIT_SHA256,
        "provider": "bitmex",
        "symbol": expected_symbol,
        "binance_symbol": expected_binance_symbol,
        "file_sha256": expected_csv_sha256,
        "event_count": EXPECTED_EVENT_COUNT,
        "duplicate_events": 0,
        "page_count": EXPECTED_PAGE_COUNT,
        "official_api_pages_verified": True,
        "segment_cadence_verified": True,
        "excluded_interval_not_requested": True,
    }
    for key, expected in expected_fields.items():
        if manifest.get(key) != expected:
            raise ValueError(
                f"{expected_symbol} BitMEX manifest 字段 {key} 不一致。"
            )

    pages = list(manifest.get("source_pages") or [])
    if len(pages) != EXPECTED_PAGE_COUNT:
        raise ValueError(f"{expected_symbol} BitMEX source page 数量不一致。")
    page_by_sha: dict[str, Mapping[str, Any]] = {}
    for page in pages:
        page_sha = str(page.get("raw_response_sha256") or "")
        if len(page_sha) != 64 or any(char not in "0123456789abcdef" for char in page_sha):
            raise ValueError(f"{expected_symbol} BitMEX source page SHA 无效。")
        if page_sha in page_by_sha:
            raise ValueError(f"{expected_symbol} BitMEX source page SHA 重复。")
        page_by_sha[page_sha] = page
    if sum(int(page.get("event_count", -1)) for page in pages) != EXPECTED_EVENT_COUNT:
        raise ValueError(f"{expected_symbol} BitMEX page event 总数不一致。")

    data_path = path.parent / str(manifest["file_name"])
    if hashlib.sha256(data_path.read_bytes()).hexdigest() != expected_csv_sha256:
        raise ValueError(f"{expected_symbol} BitMEX funding CSV 哈希不一致。")
    events: list[dict[str, Any]] = []
    page_times: dict[str, list[int]] = {key: [] for key in page_by_sha}
    with data_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_header = (
            "funding_time",
            "funding_interval_hours",
            "funding_rate",
            "funding_rate_daily",
            "segment",
            "source_page_sha256",
        )
        if tuple(reader.fieldnames or ()) != expected_header:
            raise ValueError(f"{expected_symbol} BitMEX funding CSV 表头不一致。")
        previous_time: int | None = None
        for line_number, row in enumerate(reader, start=2):
            try:
                funding_time = int(row["funding_time"])
                interval_hours = int(row["funding_interval_hours"])
                funding_rate = float(row["funding_rate"])
                funding_rate_daily = float(row["funding_rate_daily"])
                segment = str(row["segment"])
                page_sha = str(row["source_page_sha256"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"{expected_symbol} BitMEX funding CSV 第 {line_number} 行无效。"
                ) from exc
            if page_sha not in page_by_sha:
                raise ValueError(
                    f"{expected_symbol} BitMEX CSV 行级 page SHA 未出现在 manifest。"
                )
            if segment not in {item[0] for item in AUTHORIZED_SEGMENTS}:
                raise ValueError(f"{expected_symbol} BitMEX CSV segment 无效。")
            if previous_time is not None and funding_time <= previous_time:
                raise ValueError(f"{expected_symbol} BitMEX funding_time 未严格递增。")
            if not math.isfinite(funding_rate) or not math.isfinite(funding_rate_daily):
                raise ValueError(f"{expected_symbol} BitMEX funding rate 无效。")
            if not math.isclose(
                funding_rate_daily,
                funding_rate * 3.0,
                rel_tol=1e-12,
                abs_tol=1e-15,
            ):
                raise ValueError(
                    f"{expected_symbol} BitMEX fundingRateDaily 审计失败。"
                )
            previous_time = funding_time
            page_times[page_sha].append(funding_time)
            events.append(
                {
                    "funding_time": funding_time,
                    "funding_interval_hours": interval_hours,
                    "funding_rate": funding_rate,
                    "segment": segment,
                }
            )

    for page_sha, page in page_by_sha.items():
        times = page_times[page_sha]
        if len(times) != int(page["event_count"]):
            raise ValueError(f"{expected_symbol} BitMEX page 行数审计失败。")
        if _iso_ms(times[0]) != str(page["first_event"]):
            raise ValueError(f"{expected_symbol} BitMEX page 首事件审计失败。")
        if _iso_ms(times[-1]) != str(page["last_event"]):
            raise ValueError(f"{expected_symbol} BitMEX page 末事件审计失败。")

    audit = _validate_event_series(
        events,
        label=f"BitMEX {expected_symbol}",
        require_strict_cadence=True,
    )
    audit["source_page_count"] = len(page_by_sha)
    audit["source_page_row_hashes_match"] = True
    return manifest, events, audit


def _window_signature(item: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(item["window_id"]),
        _parse_utc(item["market_close"], label="market_close").isoformat(),
        _parse_utc(item["force_close_at"], label="force_close_at").isoformat(),
    )


def _recover_round22_windows(
    payload: Mapping[str, Any],
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, Any]]:
    if payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 22 之后 CURRENT Final OOS 已不再封存。")
    if payload.get("formal_round22_preregistration_ready") is not False:
        raise ValueError("Round 22 不应允许正式注册。")
    if not str(payload.get("conclusion") or "").startswith(
        "NO_PREREGISTERED_FUNDING_CARRY_CANDIDATE"
    ):
        raise ValueError("Round 22 失败结论不匹配。")

    expected_cell_names = {
        f"{role}_{split.upper()}_{scenario}"
        for role, split, _count in WINDOW_GROUPS
        for scenario in SCENARIO_FEE_RATES
    }
    cells = payload.get("cells")
    if not isinstance(cells, Mapping) or set(cells) != expected_cell_names:
        raise ValueError("Round 22 cell 集合不一致。")

    datasets: dict[str, dict[str, list[dict[str, Any]]]] = {}
    signature_audit: dict[str, Any] = {}
    all_signatures: list[tuple[str, str, str]] = []
    for role, split, expected_count in WINDOW_GROUPS:
        canonical: list[tuple[str, str, str]] | None = None
        compared_sources = 0
        for scenario in SCENARIO_FEE_RATES:
            cell_name = f"{role}_{split.upper()}_{scenario}"
            cell = cells[cell_name]
            if (
                str(cell.get("role")) != role
                or str(cell.get("split")) != split
                or str(cell.get("scenario")) != scenario
                or int(cell.get("window_count", -1)) != expected_count
            ):
                raise ValueError(f"Round 22 {cell_name} 元数据不一致。")
            symbols = cell.get("symbols")
            if not isinstance(symbols, Mapping) or set(symbols) != {
                "BTCUSDT",
                "ETHUSDT",
            }:
                raise ValueError(f"Round 22 {cell_name} 标的集合不一致。")
            for symbol in ("BTCUSDT", "ETHUSDT"):
                windows = list(symbols[symbol].get("windows") or [])
                if len(windows) != expected_count:
                    raise ValueError(f"Round 22 {cell_name} {symbol} 窗口数不一致。")
                if any(str(item.get("symbol")) != symbol for item in windows):
                    raise ValueError(f"Round 22 {cell_name} {symbol} 窗口标的不一致。")
                signatures = [_window_signature(item) for item in windows]
                if canonical is None:
                    canonical = signatures
                elif signatures != canonical:
                    raise ValueError(
                        f"Round 22 {role}/{split} 的 BASE/COST50、BTC/ETH 窗口定义不一致。"
                    )
                compared_sources += 1
        if canonical is None:
            raise RuntimeError(f"Round 22 {role}/{split} 没有可恢复窗口。")
        restored = [
            {
                "window_id": window_id,
                "market_close": _parse_utc(market_close, label="market_close"),
                "force_close_at": _parse_utc(force_close_at, label="force_close_at"),
            }
            for window_id, market_close, force_close_at in canonical
        ]
        datasets.setdefault(role, {})[split] = restored
        all_signatures.extend(canonical)
        signature_audit[f"{role}_{split}"] = {
            "window_count": len(restored),
            "compared_sources": compared_sources,
            "base_cost_and_symbols_identical": True,
        }

    if len(all_signatures) != 293:
        raise ValueError(f"Round 22 恢复窗口总数不一致: {len(all_signatures)} != 293。")
    window_ids = [item[0] for item in all_signatures]
    if len(window_ids) != len(set(window_ids)):
        raise ValueError("Round 22 授权窗口 ID 重复。")
    ordered = sorted(
        [window for splits in datasets.values() for values in splits.values() for window in values],
        key=lambda item: item["market_close"],
    )
    for previous, current in zip(ordered, ordered[1:]):
        if previous["force_close_at"] > current["market_close"]:
            raise ValueError("Round 22 授权窗口互相重叠。")
    if any(
        window["market_close"] < EXCLUDED_END
        and window["force_close_at"] > EXCLUDED_START
        for window in ordered
    ):
        raise ValueError("Round 22 授权窗口触碰隔离区间。")
    return datasets, {
        "window_count": len(ordered),
        "unique_window_ids": len(set(window_ids)),
        "groups": signature_audit,
        "windows_non_overlapping": True,
        "excluded_interval_untouched": True,
        "passed": True,
    }


def _events_by_window(
    events: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    times = [int(item["funding_time"]) for item in events]
    assigned: dict[str, list[dict[str, Any]]] = {}
    assigned_event_times: set[int] = set()
    for window in sorted(windows, key=lambda item: item["market_close"]):
        start_ms = int(window["market_close"].timestamp() * 1000)
        end_ms = int(window["force_close_at"].timestamp() * 1000)
        start_index = bisect.bisect_left(times, start_ms)
        end_index = bisect.bisect_left(times, end_ms)
        selected = [dict(item) for item in events[start_index:end_index]]
        if not selected:
            raise RuntimeError(f"{label} {window['window_id']} 没有 funding event。")
        for item in selected:
            funding_time = int(item["funding_time"])
            if funding_time in assigned_event_times:
                raise RuntimeError(f"{label} funding event 被分配到多个窗口。")
            assigned_event_times.add(funding_time)
        assigned[str(window["window_id"])] = selected
    coverage_ratio = len(assigned) / len(windows) if windows else 0.0
    return assigned, {
        "window_count": len(windows),
        "covered_window_count": len(assigned),
        "window_coverage_ratio": coverage_ratio,
        "assigned_event_count": len(assigned_event_times),
        "minimum_events_per_window": min(len(value) for value in assigned.values()),
        "maximum_events_per_window": max(len(value) for value in assigned.values()),
        "all_windows_have_events": coverage_ratio == 1.0,
        "events_assigned_once": True,
        "passed": coverage_ratio == 1.0,
    }


def _spread_window_result(
    window: Mapping[str, Any],
    binance_events: Sequence[Mapping[str, Any]],
    bitmex_events: Sequence[Mapping[str, Any]],
    *,
    asset: str,
    binance_symbol: str,
    bitmex_symbol: str,
    gross_capital: float,
    maker_fee_rate: float,
) -> dict[str, Any]:
    if not binance_events or not bitmex_events:
        raise ValueError("跨所 funding spread 窗口的两家交易所都必须有事件。")
    binance_sum = sum(float(item["funding_rate"]) for item in binance_events)
    bitmex_sum = sum(float(item["funding_rate"]) for item in bitmex_events)
    spread_sum = binance_sum - bitmex_sum
    direction_sign = 1.0 if spread_sum >= 0 else -1.0
    direction = (
        "SHORT_BINANCE_LONG_BITMEX"
        if spread_sum >= 0
        else "LONG_BINANCE_SHORT_BITMEX"
    )
    per_leg_notional = gross_capital / 2.0
    binance_funding_pnl = per_leg_notional * direction_sign * binance_sum
    bitmex_funding_pnl = -per_leg_notional * direction_sign * bitmex_sum
    funding_income = per_leg_notional * abs(spread_sum)
    if not math.isclose(
        binance_funding_pnl + bitmex_funding_pnl,
        funding_income,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise RuntimeError("跨所 funding spread 收益恒等式失败。")

    entry_fee = gross_capital * maker_fee_rate
    exit_fee = gross_capital * maker_fee_rate
    merged_events = [
        (
            int(item["funding_time"]),
            "BINANCE",
            per_leg_notional * direction_sign * float(item["funding_rate"]),
        )
        for item in binance_events
    ] + [
        (
            int(item["funding_time"]),
            "BITMEX",
            -per_leg_notional * direction_sign * float(item["funding_rate"]),
        )
        for item in bitmex_events
    ]
    merged_events.sort(key=lambda item: (item[0], item[1]))
    timestamp_counts = Counter(item[0] for item in merged_events)
    cumulative = -entry_fee
    path = [cumulative]
    for _funding_time, _venue, contribution in merged_events:
        cumulative += contribution
        path.append(cumulative)
    cumulative -= exit_fee
    path.append(cumulative)
    net_pnl = funding_income - entry_fee - exit_fee
    if not math.isclose(cumulative, net_pnl, rel_tol=1e-12, abs_tol=1e-12):
        raise RuntimeError("跨所 funding spread 路径终值与净收益不一致。")

    return {
        "window_id": str(window["window_id"]),
        "market_close": window["market_close"].isoformat(),
        "force_close_at": window["force_close_at"].isoformat(),
        "asset": asset,
        "binance_symbol": binance_symbol,
        "bitmex_symbol": bitmex_symbol,
        "gross_capital": gross_capital,
        "per_leg_notional": per_leg_notional,
        "binance_event_count": len(binance_events),
        "bitmex_event_count": len(bitmex_events),
        "binance_funding_sum": binance_sum,
        "bitmex_funding_sum": bitmex_sum,
        "funding_spread_sum": spread_sum,
        "oracle_direction": direction,
        "binance_funding_pnl": binance_funding_pnl,
        "bitmex_funding_pnl": bitmex_funding_pnl,
        "funding_income": funding_income,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees_paid": entry_fee + exit_fee,
        "net_pnl": net_pnl,
        "minimum_path_pnl": min(path),
        "maximum_path_pnl": max(path),
        "path_funding_event_count": len(merged_events),
        "same_timestamp_collision_count": sum(
            count - 1 for count in timestamp_counts.values() if count > 1
        ),
        "round_trip_count": 1,
        "leg_count": 2,
    }


def _profit_factor(gains: float, losses: float) -> float | None:
    return None if losses <= 0 else gains / losses


def _symbol_metrics(
    results: Sequence[Mapping[str, Any]],
    *,
    gross_capital: float,
) -> dict[str, Any]:
    if not results:
        raise ValueError("跨所 funding spread cell 没有窗口。")
    ordered = sorted(results, key=lambda item: str(item["market_close"]))
    pnl_values = [float(item["net_pnl"]) for item in ordered]
    positive = [value for value in pnl_values if value > 0]
    negative = [value for value in pnl_values if value < 0]
    gains = sum(positive)
    losses = -sum(negative)
    total_pnl = sum(pnl_values)
    profit_factor = _profit_factor(gains, losses)

    equity = gross_capital
    peak = equity
    maximum_drawdown_pct = 0.0
    for item in ordered:
        path_equity = equity + float(item["minimum_path_pnl"])
        maximum_drawdown_pct = max(
            maximum_drawdown_pct,
            (peak - path_equity) / max(peak, 1e-12),
        )
        equity += float(item["net_pnl"])
        peak = max(peak, equity)
        maximum_drawdown_pct = max(
            maximum_drawdown_pct,
            (peak - equity) / max(peak, 1e-12),
        )

    concentration = max(positive) / gains if positive and gains > 0 else 1.0
    positive_ratio = len(positive) / len(ordered)
    binance_covered = sum(int(item["binance_event_count"]) > 0 for item in ordered)
    bitmex_covered = sum(int(item["bitmex_event_count"]) > 0 for item in ordered)
    complete_round_trips = sum(
        int(item["round_trip_count"]) == 1 and int(item["leg_count"]) == 2
        for item in ordered
    )
    metrics = {
        "window_count": len(ordered),
        "round_trip_count": sum(int(item["round_trip_count"]) for item in ordered),
        "complete_two_leg_round_trip_count": complete_round_trips,
        "binance_event_count": sum(int(item["binance_event_count"]) for item in ordered),
        "bitmex_event_count": sum(int(item["bitmex_event_count"]) for item in ordered),
        "minimum_binance_events_per_window": min(
            int(item["binance_event_count"]) for item in ordered
        ),
        "maximum_binance_events_per_window": max(
            int(item["binance_event_count"]) for item in ordered
        ),
        "minimum_bitmex_events_per_window": min(
            int(item["bitmex_event_count"]) for item in ordered
        ),
        "maximum_bitmex_events_per_window": max(
            int(item["bitmex_event_count"]) for item in ordered
        ),
        "binance_window_coverage_ratio": binance_covered / len(ordered),
        "bitmex_window_coverage_ratio": bitmex_covered / len(ordered),
        "total_pnl": total_pnl,
        "mean_window_pnl": statistics.fmean(pnl_values),
        "median_window_pnl": statistics.median(pnl_values),
        "positive_window_count": len(positive),
        "negative_window_count": len(negative),
        "positive_window_ratio": positive_ratio,
        "gross_profit": gains,
        "gross_loss": losses,
        "profit_factor": profit_factor,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "best_window_concentration": concentration,
        "funding_income": sum(float(item["funding_income"]) for item in ordered),
        "binance_funding_pnl": sum(
            float(item["binance_funding_pnl"]) for item in ordered
        ),
        "bitmex_funding_pnl": sum(
            float(item["bitmex_funding_pnl"]) for item in ordered
        ),
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
        "ending_equity": equity,
        "oracle_direction_counts": dict(
            Counter(str(item["oracle_direction"]) for item in ordered)
        ),
    }
    checks = {
        "total_pnl_positive": total_pnl > 0,
        "profit_factor_gt_1": (
            total_pnl > 0 if profit_factor is None else profit_factor > 1.0
        ),
        "max_drawdown_le_5pct": maximum_drawdown_pct <= 0.05,
        "best_window_concentration_le_35pct": concentration <= 0.35,
        "positive_window_ratio_ge_25pct": positive_ratio >= 0.25,
        "binance_event_coverage_100pct": binance_covered == len(ordered),
        "bitmex_event_coverage_100pct": bitmex_covered == len(ordered),
        "one_two_leg_round_trip_per_window": complete_round_trips == len(ordered),
    }
    return {
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
        "windows": list(ordered),
    }


def _evaluate_cells(
    datasets: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    funding: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    all_windows = [
        window
        for splits in datasets.values()
        for windows in splits.values()
        for window in windows
    ]
    event_maps: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    event_audit: dict[str, Any] = {}
    for asset, config in ASSET_CONFIG.items():
        event_maps[asset] = {}
        event_audit[asset] = {}
        for venue in ("BINANCE", "BITMEX"):
            event_maps[asset][venue], event_audit[asset][venue] = _events_by_window(
                funding[asset][venue],
                all_windows,
                label=f"{asset} {venue}",
            )

    cells: dict[str, Any] = {}
    for role, splits in datasets.items():
        for split, windows in splits.items():
            cell_base = f"{role}_{split.upper()}"
            for scenario, maker_fee_rate in SCENARIO_FEE_RATES.items():
                symbols: dict[str, Any] = {}
                for asset, config in ASSET_CONFIG.items():
                    results = [
                        _spread_window_result(
                            window,
                            event_maps[asset]["BINANCE"][str(window["window_id"])],
                            event_maps[asset]["BITMEX"][str(window["window_id"])],
                            asset=asset,
                            binance_symbol=str(config["binance_symbol"]),
                            bitmex_symbol=str(config["bitmex_symbol"]),
                            gross_capital=float(config["gross_capital"]),
                            maker_fee_rate=maker_fee_rate,
                        )
                        for window in windows
                    ]
                    symbols[asset] = _symbol_metrics(
                        results,
                        gross_capital=float(config["gross_capital"]),
                    )
                cells[f"{cell_base}_{scenario}"] = {
                    "role": role,
                    "split": split,
                    "scenario": scenario,
                    "maker_fee_rate": maker_fee_rate,
                    "window_count": len(windows),
                    "symbols": symbols,
                }
    return cells, event_audit


def _upper_bound_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [
        cell["symbols"][asset]
        for cell in cells.values()
        for asset in ASSET_CONFIG
    ]
    if len(selected) != 16:
        raise RuntimeError(
            f"跨所 funding spread cell-symbol 数量不一致: {len(selected)} != 16"
        )
    return {
        "cell_symbol_count": len(selected),
        "passed_cell_symbol_count": sum(bool(item["passed"]) for item in selected),
        "all_cells_passed": all(bool(item["passed"]) for item in selected),
        "minimum_total_pnl": min(
            float(item["metrics"]["total_pnl"]) for item in selected
        ),
        "minimum_positive_window_ratio": min(
            float(item["metrics"]["positive_window_ratio"]) for item in selected
        ),
        "maximum_drawdown_pct": max(
            float(item["metrics"]["maximum_drawdown_pct"]) for item in selected
        ),
        "maximum_best_window_concentration": max(
            float(item["metrics"]["best_window_concentration"]) for item in selected
        ),
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 25：Binance–BitMEX Funding Spread 乐观上界结果",
        "",
        "Oracle 事后使用完整窗口 funding spread 符号选择跨所方向；忽略成交基差、反向合约换算、保证金、腿间延迟与交易所风险。",
        "",
        "| 单元 | 资产/交易对 | 窗口 | Funding 收入 | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | B/M 覆盖 | 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for asset, config in ASSET_CONFIG.items():
            item = cell["symbols"][asset]
            metrics = item["metrics"]
            failed = [name for name, passed in item["checks"].items() if not passed]
            pf = metrics["profit_factor"]
            pf_text = "∞" if pf is None and metrics["total_pnl"] > 0 else (
                "N/A" if pf is None else f"{float(pf):.3f}"
            )
            lines.append(
                "| `{cell}` | {asset} `{binance}/{bitmex}` | {windows} | "
                "{income:.4f} | {fees:.4f} | {pnl:.4f} | {pf} | {drawdown:.2%} | "
                "{positive:.2%} | {binance_coverage:.0%}/{bitmex_coverage:.0%} | "
                "{passed} | {failed} |".format(
                    cell=cell_name,
                    asset=asset,
                    binance=config["binance_symbol"],
                    bitmex=config["bitmex_symbol"],
                    windows=metrics["window_count"],
                    income=metrics["funding_income"],
                    fees=metrics["fees_paid"],
                    pnl=metrics["total_pnl"],
                    pf=pf_text,
                    drawdown=metrics["maximum_drawdown_pct"],
                    positive=metrics["positive_window_ratio"],
                    binance_coverage=metrics["binance_window_coverage_ratio"],
                    bitmex_coverage=metrics["bitmex_window_coverage_ratio"],
                    passed="是" if item["passed"] else "否",
                    failed=", ".join(failed),
                )
            )
    summary = payload["upper_bound_summary"]
    lines.extend(
        [
            "",
            f"通过单元：{summary['passed_cell_symbol_count']}/{summary['cell_symbol_count']}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "CURRENT Final OOS 保持封存；未注册生产候选；direction_mode 仍为 NEUTRAL；生产默认值未修改。",
            "",
        ]
    )
    return "\n".join(lines)


def _validate_prior_result(
    path: Path,
    *,
    expected_sha256: str,
    expected_conclusion_prefix: str,
    ready_key: str,
) -> dict[str, Any]:
    resolved = path.resolve()
    if _sha256(resolved) != expected_sha256:
        raise ValueError(f"冻结输入哈希不一致: {resolved}")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError(f"{resolved.name} 之后 CURRENT Final OOS 已不再封存。")
    if payload.get(ready_key) is not False:
        raise ValueError(f"{resolved.name} 不应允许正式注册。")
    if not str(payload.get("conclusion") or "").startswith(expected_conclusion_prefix):
        raise ValueError(f"{resolved.name} 失败结论不匹配。")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估 Binance–BitMEX 周末双永续 funding spread 的不可部署乐观上界。"
    )
    parser.add_argument("--round22-result", default=str(ROUND22_RESULT_PATH))
    parser.add_argument("--round24-result", default=str(ROUND24_RESULT_PATH))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    args = _parser().parse_args()
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 25 cross-venue funding spread 协议哈希不一致。")

    round22_path = Path(args.round22_result)
    round24_path = Path(args.round24_result)
    round22_payload = _validate_prior_result(
        round22_path,
        expected_sha256=ROUND22_RESULT_SHA256,
        expected_conclusion_prefix="NO_PREREGISTERED_FUNDING_CARRY_CANDIDATE",
        ready_key="formal_round22_preregistration_ready",
    )
    round24_payload = _validate_prior_result(
        round24_path,
        expected_sha256=ROUND24_RESULT_SHA256,
        expected_conclusion_prefix=(
            "NO_PREREGISTERED_CROSS_ASSET_PREMIUM_DISPERSION_CANDIDATE"
        ),
        ready_key="formal_round24_preregistration_ready",
    )
    datasets, window_recovery_audit = _recover_round22_windows(round22_payload)

    funding: dict[str, dict[str, list[dict[str, Any]]]] = {}
    manifest_records: dict[str, Any] = {}
    source_data_audit: dict[str, Any] = {}
    for asset, config in ASSET_CONFIG.items():
        binance_manifest, binance_events, binance_audit = _read_binance_manifest(
            Path(config["binance_manifest"]),
            expected_manifest_sha256=str(config["binance_manifest_sha256"]),
            expected_csv_sha256=str(config["binance_csv_sha256"]),
            expected_symbol=str(config["binance_symbol"]),
        )
        bitmex_manifest, bitmex_events, bitmex_audit = _read_bitmex_manifest(
            Path(config["bitmex_manifest"]),
            expected_manifest_sha256=str(config["bitmex_manifest_sha256"]),
            expected_csv_sha256=str(config["bitmex_csv_sha256"]),
            expected_symbol=str(config["bitmex_symbol"]),
            expected_binance_symbol=str(config["binance_symbol"]),
        )
        funding[asset] = {"BINANCE": binance_events, "BITMEX": bitmex_events}
        source_data_audit[asset] = {
            "BINANCE": binance_audit,
            "BITMEX": bitmex_audit,
        }
        manifest_records[asset] = {
            "BINANCE": {
                "symbol": config["binance_symbol"],
                "path": str(Path(config["binance_manifest"]).resolve()),
                "manifest_sha256": config["binance_manifest_sha256"],
                "file_sha256": binance_manifest["file_sha256"],
                "event_count": len(binance_events),
            },
            "BITMEX": {
                "symbol": config["bitmex_symbol"],
                "path": str(Path(config["bitmex_manifest"]).resolve()),
                "manifest_sha256": config["bitmex_manifest_sha256"],
                "file_sha256": bitmex_manifest["file_sha256"],
                "event_count": len(bitmex_events),
                "page_count": bitmex_manifest["page_count"],
            },
        }

    cells, event_audit = _evaluate_cells(datasets, funding)
    upper_bound = _upper_bound_summary(cells)
    family_ready = bool(upper_bound["all_cells_passed"])
    conclusion = (
        "CROSS_VENUE_FUNDING_SPREAD_WORTH_PREREGISTRATION：16/16 个年代、成本与标的单元均通过乐观上界；只允许随后冻结跨所成交基差、历史实际费率、币本位换算和保证金风险，并定义单一因果方向候选。"
        if family_ready
        else "NO_PREREGISTERED_CROSS_VENUE_FUNDING_SPREAD_CANDIDATE：至少一个单元在未来已知 funding spread 方向、零成交基差和同步 Maker 假设下仍失败，排除本协议定义的周末双永续 funding spread family。"
    )
    input_hashes = {
        str(round22_path.resolve()): ROUND22_RESULT_SHA256,
        str(round24_path.resolve()): ROUND24_RESULT_SHA256,
    }
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "NON_DEPLOYABLE_CROSS_VENUE_FUNDING_SPREAD_UPPER_BOUND",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": input_hashes,
        "prior_result_validation": {
            "round22_conclusion": round22_payload["conclusion"],
            "round24_conclusion": round24_payload["conclusion"],
            "final_oos_status": "SEALED_NOT_EVALUATED",
        },
        "funding_manifests": manifest_records,
        "source_data_audit": source_data_audit,
        "direction_mode": "NEUTRAL",
        "asset_mapping": {
            asset: {
                "binance_symbol": config["binance_symbol"],
                "bitmex_symbol": config["bitmex_symbol"],
                "gross_capital": config["gross_capital"],
                "per_leg_notional": float(config["gross_capital"]) / 2.0,
            }
            for asset, config in ASSET_CONFIG.items()
        },
        "maker_fee_rate_by_scenario": SCENARIO_FEE_RATES,
        "direction_rule": "sign(sum(binance_rate)-sum(bitmex_rate))",
        "oracle_uses_future_funding_spread": True,
        "oracle_is_deployable": False,
        "ignored_risks": [
            "cross_venue_execution_basis",
            "inverse_contract_coin_settlement_conversion",
            "collateral_price_risk",
            "cross_venue_transfer_risk",
            "margin_and_liquidation",
            "maker_queue_failure",
            "leg_latency",
            "slippage",
            "api_latency",
            "exchange_and_custody_risk",
        ],
        "window_recovery_audit": window_recovery_audit,
        "window_counts": {
            role: {split: len(windows) for split, windows in splits.items()}
            for role, splits in datasets.items()
        },
        "event_audit": event_audit,
        "cells": cells,
        "upper_bound_summary": upper_bound,
        "formal_round25_preregistration_ready": family_ready,
        "selected_candidate_id": None,
        "final_oos_authorization_ready": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    result_path = report_dir / "round25-cross-venue-funding-spread-upper-bound-results.json"
    report_path = report_dir / "round25-cross-venue-funding-spread-upper-bound-report.md"
    _write_json(result_path, result)
    report_path.write_text(_report_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "report_path": str(report_path.resolve()),
                "passed_cell_symbol_count": upper_bound["passed_cell_symbol_count"],
                "cell_symbol_count": upper_bound["cell_symbol_count"],
                "formal_round25_preregistration_ready": family_ready,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
