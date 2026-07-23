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

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_funding_carry_upper_bound as round22
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round23-basis-convergence-upper-bound-protocol.md"
)
PROTOCOL_SHA256 = "2504457017ee6b413ab6afaa63c4926dde6ce4c3d5e238778db85170571bf83e"
ROUND22_RESULT_PATH = Path(
    "reports/cross-era-oos/round22-funding-carry-upper-bound-results.json"
)
ROUND22_RESULT_SHA256 = (
    "622d359710b3f4e6f6371211a946ae4f33ed24510d5d1262def7ada29c47ab41"
)
DATA_PROTOCOL_SHA256 = (
    "be795fa8fac4af4bede6cb7418c8624ea9dc5064704eabda44025a6e569b1a8f"
)
PREMIUM_MANIFEST_SHA256 = {
    "BTCUSDT": "420bab13264b2cfcc45b816c1fe30ad83bc1ff8cbc1467f20de68d9626785684",
    "ETHUSDT": "2a2c2a7a17f14e48f6f43da84b5b0a4e7a93e763e81d41e3e0eebecf6cbc0fc1",
}
FUNDING_MANIFEST_SHA256 = {
    "BTCUSDT": round22.BTC_MANIFEST_SHA256,
    "ETHUSDT": round22.ETH_MANIFEST_SHA256,
}
EXPECTED_ROW_COUNT = 1_056_000
EXPECTED_WINDOW_COUNTS = {
    "PREHISTORY_external": 27,
    "CURRENT_development": 108,
    "CURRENT_validation_complete_months": 49,
    "POSTHISTORY_external": 107,
}
EXPECTED_WINDOW_COUNT = sum(EXPECTED_WINDOW_COUNTS.values())
EXPECTED_MONTHLY_ARCHIVE_COUNT = 65
EXPECTED_DAILY_SUPPLEMENT_COUNT = 15
EXCLUDED_INCOMPLETE_WINDOWS = {
    "nyse_20200117T210000Z": 29,
    "nyse_20260626T200000Z": 360,
}
EXPECTED_SOURCE_GAP_AUDIT = {
    "BTCUSDT": {
        "monthly_missing_minute_count": 11_707,
        "daily_recovered_minute_count": 10_080,
        "remaining_source_missing_minute_count": 1_627,
        "excluded_window_missing_minute_count": 389,
    },
    "ETHUSDT": {
        "monthly_missing_minute_count": 11_704,
        "daily_recovered_minute_count": 10_080,
        "remaining_source_missing_minute_count": 1_624,
        "excluded_window_missing_minute_count": 389,
    },
}
CAPITAL_BY_SYMBOL = {"BTCUSDT": 500.0, "ETHUSDT": 300.0}
OBSERVATION_ROWS = 180
SCENARIOS = asset_audit.SCENARIOS
CSV_HEADER = (
    "window_id",
    "open_time",
    "premium_close",
    "source_month",
    "source_granularity",
    "source_period",
    "source_zip_sha256",
)


def _window_signature(item: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(item["role"]),
        str(item["split"]),
        str(item["window_id"]),
        str(item["market_close"]),
        str(item["force_close_at"]),
        int(item["row_count"]),
        int(item["expected_row_count"]),
        bool(item["complete"]),
    )


def _read_premium_manifest(
    manifest_path: Path,
    *,
    expected_sha256: str,
    expected_symbol: str,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, list[tuple[int, float]]],
]:
    path = manifest_path.resolve()
    if _sha256(path) != expected_sha256:
        raise ValueError(f"{expected_symbol} Premium Index manifest 哈希不一致。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("data_protocol_sha256") != DATA_PROTOCOL_SHA256:
        raise ValueError(f"{expected_symbol} Premium Index 数据协议哈希不一致。")
    if str(manifest.get("symbol") or "").upper() != expected_symbol:
        raise ValueError(f"{expected_symbol} Premium Index 标的不一致。")
    if (
        manifest.get("market") != "USDS_M"
        or manifest.get("data_type") != "premiumIndexKlines"
        or manifest.get("interval") != "1m"
    ):
        raise ValueError(f"{expected_symbol} Premium Index 市场或类型不一致。")
    if int(manifest.get("row_count", -1)) != EXPECTED_ROW_COUNT:
        raise ValueError(f"{expected_symbol} Premium Index 行数不一致。")
    if int(manifest.get("window_count", -1)) != EXPECTED_WINDOW_COUNT:
        raise ValueError(f"{expected_symbol} Premium Index 窗口数不一致。")
    if int(manifest.get("duplicate_rows", -1)) != 0:
        raise ValueError(f"{expected_symbol} Premium Index 包含重复行。")
    if not bool(manifest.get("official_monthly_checksums_verified")):
        raise ValueError(f"{expected_symbol} Premium Index 月档 checksum 未通过。")
    if not bool(manifest.get("available_daily_checksums_verified")):
        raise ValueError(f"{expected_symbol} Premium Index 日档 checksum 未通过。")
    if not bool(manifest.get("source_gaps_recorded")):
        raise ValueError(f"{expected_symbol} Premium Index 源缺口未记录。")
    if not bool(manifest.get("authorized_windows_complete")):
        raise ValueError(f"{expected_symbol} Premium Index 授权窗口不完整。")
    isolation = dict(manifest.get("current_isolation") or {})
    if int(isolation.get("sealed_final_oos_count", -1)) != 54:
        raise ValueError(f"{expected_symbol} CURRENT Final OOS 隔离记录不一致。")
    if dict(manifest.get("source_gap_audit") or {}) != EXPECTED_SOURCE_GAP_AUDIT[
        expected_symbol
    ]:
        raise ValueError(f"{expected_symbol} Premium Index 源缺口审计不一致。")

    monthly = list(manifest.get("monthly_source_archives") or [])
    daily = list(manifest.get("daily_supplements") or [])
    if len(monthly) != EXPECTED_MONTHLY_ARCHIVE_COUNT:
        raise ValueError(f"{expected_symbol} Premium Index 月档数量不一致。")
    if len(daily) != EXPECTED_DAILY_SUPPLEMENT_COUNT:
        raise ValueError(f"{expected_symbol} Premium Index 日档数量不一致。")
    if any(not bool(item.get("official_checksum_verified")) for item in monthly):
        raise ValueError(f"{expected_symbol} Premium Index 存在未验证月档。")
    if any(
        int(item.get("http_status", -1)) == 200
        and not bool(item.get("official_checksum_verified"))
        for item in daily
    ):
        raise ValueError(f"{expected_symbol} Premium Index 存在未验证可用日档。")
    monthly_by_period = {str(item["month"]): item for item in monthly}
    daily_by_period = {str(item["day"]): item for item in daily}
    if len(monthly_by_period) != EXPECTED_MONTHLY_ARCHIVE_COUNT:
        raise ValueError(f"{expected_symbol} Premium Index 月档 period 重复。")
    if len(daily_by_period) != EXPECTED_DAILY_SUPPLEMENT_COUNT:
        raise ValueError(f"{expected_symbol} Premium Index 日档 period 重复。")
    if set(monthly_by_period) & round22.EXCLUDED_MONTHS:
        raise ValueError(f"{expected_symbol} Premium Index manifest 触碰封存月份。")

    excluded = list(manifest.get("excluded_incomplete_windows") or [])
    if {
        str(item["window_id"]): int(item["missing_minute_count"])
        for item in excluded
    } != EXCLUDED_INCOMPLETE_WINDOWS:
        raise ValueError(f"{expected_symbol} Premium Index 固定排除窗口不一致。")
    windows = list(manifest.get("windows") or [])
    counts = Counter(f"{item['role']}_{item['split']}" for item in windows)
    if dict(counts) != EXPECTED_WINDOW_COUNTS:
        raise ValueError(f"{expected_symbol} Premium Index 窗口拆分不一致。")
    window_by_id: dict[str, dict[str, Any]] = {}
    for item in windows:
        window_id = str(item["window_id"])
        if window_id in EXCLUDED_INCOMPLETE_WINDOWS or window_id in window_by_id:
            raise ValueError(f"{expected_symbol} Premium Index 窗口 ID 无效。")
        if not bool(item.get("complete")):
            raise ValueError(f"{expected_symbol} Premium Index manifest 包含不完整窗口。")
        start = datetime.fromisoformat(str(item["market_close"]))
        end = datetime.fromisoformat(str(item["force_close_at"]))
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        expected_rows = (end_ms - start_ms) // 60_000
        if (
            start_ms % 60_000
            or end_ms % 60_000
            or expected_rows != int(item["expected_row_count"])
            or expected_rows != int(item["row_count"])
        ):
            raise ValueError(f"{expected_symbol} {window_id} 窗口边界无效。")
        normalized = dict(item)
        normalized["start_ms"] = start_ms
        normalized["end_ms"] = end_ms
        window_by_id[window_id] = normalized
    windows = sorted(window_by_id.values(), key=lambda item: int(item["start_ms"]))

    data_path = path.parent / str(manifest["file_name"])
    if hashlib.sha256(data_path.read_bytes()).hexdigest() != manifest.get("file_sha256"):
        raise ValueError(f"{expected_symbol} Premium Index CSV 哈希不一致。")
    rows_by_window: dict[str, list[tuple[int, float]]] = {
        str(item["window_id"]): [] for item in windows
    }
    previous_time: int | None = None
    row_count = 0
    with data_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_HEADER:
            raise ValueError(f"{expected_symbol} Premium Index CSV 表头不一致。")
        for line_number, row in enumerate(reader, start=2):
            try:
                window_id = str(row["window_id"])
                open_time = int(row["open_time"])
                premium_close = float(row["premium_close"])
                source_month = str(row["source_month"])
                source_granularity = str(row["source_granularity"])
                source_period = str(row["source_period"])
                source_sha256 = str(row["source_zip_sha256"])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(
                    f"{expected_symbol} Premium Index CSV 第 {line_number} 行无效。"
                ) from exc
            if window_id not in window_by_id or window_id in EXCLUDED_INCOMPLETE_WINDOWS:
                raise ValueError(f"{expected_symbol} Premium Index CSV 包含未授权窗口。")
            if previous_time is not None and open_time <= previous_time:
                raise ValueError(f"{expected_symbol} Premium Index open_time 未严格递增。")
            if not math.isfinite(premium_close):
                raise ValueError(f"{expected_symbol} Premium Index close 非有限。")
            if source_month in round22.EXCLUDED_MONTHS:
                raise ValueError(f"{expected_symbol} Premium Index CSV 触碰封存月份。")
            if datetime.fromtimestamp(open_time / 1000, tz=UTC).strftime(
                "%Y-%m"
            ) != source_month:
                raise ValueError(f"{expected_symbol} Premium Index source_month 无效。")
            source = (
                monthly_by_period.get(source_period)
                if source_granularity == "monthly"
                else daily_by_period.get(source_period)
                if source_granularity == "daily"
                else None
            )
            if source is None or source_sha256 != str(source.get("zip_sha256")):
                raise ValueError(f"{expected_symbol} Premium Index 行级 source SHA 无效。")
            rows_by_window[window_id].append((open_time, premium_close))
            previous_time = open_time
            row_count += 1
    if row_count != EXPECTED_ROW_COUNT:
        raise ValueError(f"{expected_symbol} Premium Index CSV 行数不一致。")
    for window in windows:
        window_id = str(window["window_id"])
        rows = rows_by_window[window_id]
        expected_count = int(window["expected_row_count"])
        if len(rows) != expected_count:
            raise ValueError(f"{expected_symbol} {window_id} Premium 路径行数不一致。")
        start_ms = int(window["start_ms"])
        for index, (open_time, _close) in enumerate(rows):
            if open_time != start_ms + index * 60_000:
                raise ValueError(f"{expected_symbol} {window_id} Premium 路径不连续。")
    return manifest, windows, rows_by_window


def _basis_window_result(
    window: Mapping[str, Any],
    premium_rows: Sequence[tuple[int, float]],
    funding_events: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    capital: float,
    maker_fee_rate: float,
) -> dict[str, Any]:
    if len(premium_rows) <= OBSERVATION_ROWS:
        raise ValueError(f"{window['window_id']} 没有 Premium 退出路径。")
    start_ms = int(window["start_ms"])
    end_ms = int(window["end_ms"])
    if any(
        int(open_time) != start_ms + index * 60_000
        for index, (open_time, _close) in enumerate(premium_rows)
    ):
        raise ValueError(f"{window['window_id']} Premium 路径不连续。")
    if int(premium_rows[-1][0]) + 60_000 != end_ms:
        raise ValueError(f"{window['window_id']} Premium 路径未覆盖窗口终点。")

    entry_open_time, entry_premium = premium_rows[OBSERVATION_ROWS - 1]
    entry_time = int(entry_open_time) + 60_000
    direction = 1 if float(entry_premium) >= 0 else -1
    direction_name = (
        "LONG_SPOT_SHORT_PERP" if direction > 0 else "SHORT_SPOT_LONG_PERP"
    )
    perpetual_notional = capital / 2.0
    entry_fee = capital * maker_fee_rate
    round_trip_fees = 2.0 * entry_fee

    event_times = [int(item["funding_time"]) for item in funding_events]
    if event_times != sorted(event_times) or len(event_times) != len(set(event_times)):
        raise ValueError(f"{symbol} funding events 未严格递增。")
    event_index = bisect.bisect_left(event_times, entry_time)
    eligible_end = bisect.bisect_right(event_times, end_ms)
    if eligible_end <= event_index:
        raise RuntimeError(f"{window['window_id']} 入场后没有 funding event。")

    cumulative_funding_rate = 0.0
    gross_path: list[float] = []
    funding_counts: list[int] = []
    funding_cursor = event_index
    best_index = -1
    best_gross_pnl = -math.inf
    best_basis_pnl = 0.0
    best_funding_pnl = 0.0
    for path_index, (open_time, exit_premium) in enumerate(
        premium_rows[OBSERVATION_ROWS:]
    ):
        exit_time = int(open_time) + 60_000
        while funding_cursor < eligible_end and event_times[funding_cursor] <= exit_time:
            rate = float(funding_events[funding_cursor]["funding_rate"])
            if not math.isfinite(rate):
                raise ValueError(f"{symbol} funding rate 非有限。")
            cumulative_funding_rate += rate
            funding_cursor += 1
        basis_pnl = (
            perpetual_notional
            * direction
            * (float(entry_premium) - float(exit_premium))
        )
        funding_pnl = perpetual_notional * direction * cumulative_funding_rate
        gross_pnl = basis_pnl + funding_pnl
        gross_path.append(gross_pnl)
        funding_counts.append(funding_cursor - event_index)
        if gross_pnl > best_gross_pnl:
            best_index = path_index
            best_gross_pnl = gross_pnl
            best_basis_pnl = basis_pnl
            best_funding_pnl = funding_pnl
    if best_index < 0:
        raise RuntimeError(f"{window['window_id']} Oracle 没有退出候选。")

    exit_open_time, exit_premium = premium_rows[OBSERVATION_ROWS + best_index]
    exit_time = int(exit_open_time) + 60_000
    net_pnl = best_gross_pnl - round_trip_fees
    path_after_entry_fee = [value - entry_fee for value in gross_path[: best_index + 1]]
    minimum_path_pnl = min([-entry_fee, net_pnl, *path_after_entry_fee])
    return {
        "window_id": str(window["window_id"]),
        "role": str(window["role"]),
        "split": str(window["split"]),
        "symbol": symbol,
        "market_close": str(window["market_close"]),
        "force_close_at": str(window["force_close_at"]),
        "observation_rows": OBSERVATION_ROWS,
        "entry_open_time": int(entry_open_time),
        "entry_time": entry_time,
        "entry_premium": float(entry_premium),
        "direction_coefficient": direction,
        "direction": direction_name,
        "exit_open_time": int(exit_open_time),
        "exit_time": exit_time,
        "exit_premium": float(exit_premium),
        "holding_minutes": (exit_time - entry_time) // 60_000,
        "eligible_funding_event_count": eligible_end - event_index,
        "realized_funding_event_count": funding_counts[best_index],
        "basis_pnl": best_basis_pnl,
        "funding_pnl": best_funding_pnl,
        "oracle_gross_pnl": best_gross_pnl,
        "fees_paid": round_trip_fees,
        "net_pnl": net_pnl,
        "minimum_path_pnl": minimum_path_pnl,
        "premium_row_count": len(premium_rows),
        "premium_coverage_complete": True,
        "trade_count": 1,
    }


def _profit_factor(gains: float, losses: float) -> float | None:
    return None if losses <= 0 else gains / losses


def _symbol_metrics(
    results: Sequence[Mapping[str, Any]],
    *,
    capital: float,
) -> dict[str, Any]:
    if not results:
        raise ValueError("Basis convergence cell 没有窗口。")
    ordered = sorted(results, key=lambda item: str(item["market_close"]))
    pnl_values = [float(item["net_pnl"]) for item in ordered]
    positive = [value for value in pnl_values if value > 0]
    negative = [value for value in pnl_values if value < 0]
    gains = sum(positive)
    losses = -sum(negative)
    total_pnl = sum(pnl_values)
    profit_factor = _profit_factor(gains, losses)

    equity = capital
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
    eligible_counts = [int(item["eligible_funding_event_count"]) for item in ordered]
    metrics = {
        "window_count": len(ordered),
        "trade_count": sum(int(item["trade_count"]) for item in ordered),
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
        "basis_pnl": sum(float(item["basis_pnl"]) for item in ordered),
        "funding_pnl": sum(float(item["funding_pnl"]) for item in ordered),
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
        "minimum_eligible_funding_events": min(eligible_counts),
        "maximum_eligible_funding_events": max(eligible_counts),
        "realized_funding_event_count": sum(
            int(item["realized_funding_event_count"]) for item in ordered
        ),
        "mean_holding_minutes": statistics.fmean(
            float(item["holding_minutes"]) for item in ordered
        ),
        "maximum_holding_minutes": max(int(item["holding_minutes"]) for item in ordered),
        "long_spot_short_perp_count": sum(
            item["direction"] == "LONG_SPOT_SHORT_PERP" for item in ordered
        ),
        "short_spot_long_perp_count": sum(
            item["direction"] == "SHORT_SPOT_LONG_PERP" for item in ordered
        ),
        "ending_equity": equity,
    }
    checks = {
        "total_pnl_positive": total_pnl > 0,
        "profit_factor_gt_1": (
            total_pnl > 0 if profit_factor is None else profit_factor > 1.0
        ),
        "max_drawdown_le_5pct": maximum_drawdown_pct <= 0.05,
        "best_window_concentration_le_35pct": concentration <= 0.35,
        "positive_window_ratio_ge_25pct": positive_ratio >= 0.25,
        "premium_coverage_100pct": all(
            bool(item["premium_coverage_complete"]) for item in ordered
        ),
        "all_windows_have_post_entry_funding": min(eligible_counts) > 0,
        "one_trade_per_window": metrics["trade_count"] == metrics["window_count"],
    }
    return {
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
        "windows": list(ordered),
    }


def _group_windows(
    windows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for item in windows:
        grouped.setdefault(str(item["role"]), {}).setdefault(
            str(item["split"]), []
        ).append(dict(item))
    counts = {
        f"{role}_{split}": len(items)
        for role, splits in grouped.items()
        for split, items in splits.items()
    }
    if counts != EXPECTED_WINDOW_COUNTS:
        raise RuntimeError(f"Round 23 窗口拆分数量不一致: {counts}")
    return grouped


def _evaluate_cells(
    windows: Sequence[Mapping[str, Any]],
    premium_by_symbol: Mapping[str, Mapping[str, Sequence[tuple[int, float]]]],
    funding_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    grouped = _group_windows(windows)
    cells: dict[str, Any] = {}
    funding_audit: dict[str, Any] = {}
    for role, splits in grouped.items():
        for split_name, split_windows in splits.items():
            cell_base = f"{role}_{split_name.upper()}"
            funding_audit[cell_base] = {}
            for symbol in asset_audit.SYMBOLS:
                events = funding_by_symbol[symbol]
                times = [int(item["funding_time"]) for item in events]
                assigned: set[int] = set()
                minimum = math.inf
                maximum = 0
                for window in split_windows:
                    rows = premium_by_symbol[symbol][str(window["window_id"])]
                    entry_time = int(rows[OBSERVATION_ROWS - 1][0]) + 60_000
                    start_index = bisect.bisect_left(times, entry_time)
                    end_index = bisect.bisect_right(times, int(window["end_ms"]))
                    selected = times[start_index:end_index]
                    if not selected:
                        raise RuntimeError(
                            f"{symbol} {window['window_id']} 入场后没有 funding event。"
                        )
                    if assigned & set(selected):
                        raise RuntimeError(f"{symbol} funding event 被分配到多个窗口。")
                    assigned.update(selected)
                    minimum = min(minimum, len(selected))
                    maximum = max(maximum, len(selected))
                funding_audit[cell_base][symbol] = {
                    "window_count": len(split_windows),
                    "assigned_event_count": len(assigned),
                    "minimum_events_per_window": int(minimum),
                    "maximum_events_per_window": maximum,
                    "events_assigned_once": True,
                    "all_windows_have_post_entry_events": True,
                    "passed": True,
                }
            for scenario, cost in SCENARIOS.items():
                maker_fee_rate = float(cost[0])
                symbols = {}
                for symbol in asset_audit.SYMBOLS:
                    capital = CAPITAL_BY_SYMBOL[symbol]
                    results = [
                        _basis_window_result(
                            window,
                            premium_by_symbol[symbol][str(window["window_id"])],
                            funding_by_symbol[symbol],
                            symbol=symbol,
                            capital=capital,
                            maker_fee_rate=maker_fee_rate,
                        )
                        for window in split_windows
                    ]
                    symbols[symbol] = _symbol_metrics(results, capital=capital)
                cells[f"{cell_base}_{scenario}"] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "maker_fee_rate": maker_fee_rate,
                    "window_count": len(split_windows),
                    "symbols": symbols,
                }
    return cells, funding_audit


def _upper_bound_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [
        cell["symbols"][symbol]
        for cell in cells.values()
        for symbol in asset_audit.SYMBOLS
    ]
    if len(selected) != 16:
        raise RuntimeError(
            f"Basis convergence cell-symbol 数量不一致: {len(selected)} != 16"
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
        "# Round 23：Premium Index 基差收敛乐观上界结果",
        "",
        "方向仅由观察期结束时 premium 符号决定；Oracle 只事后选择退出分钟。结果忽略真实成交基差、借币与执行风险，不可部署。",
        "",
        "| 单元 | 标的 | 窗口 | 基差收益 | Funding | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for symbol in asset_audit.SYMBOLS:
            item = cell["symbols"][symbol]
            metrics = item["metrics"]
            failed = [name for name, passed in item["checks"].items() if not passed]
            profit_factor = metrics["profit_factor"]
            lines.append(
                "| "
                f"`{cell_name}` | {symbol} | {metrics['window_count']} | "
                f"{metrics['basis_pnl']:.4f} | {metrics['funding_pnl']:.4f} | "
                f"{metrics['fees_paid']:.4f} | {metrics['total_pnl']:.4f} | "
                f"{'∞' if profit_factor is None else f'{profit_factor:.3f}'} | "
                f"{metrics['maximum_drawdown_pct']:.2%} | "
                f"{metrics['positive_window_ratio']:.2%} | "
                f"{'是' if item['passed'] else '否'} | {', '.join(failed)} |"
            )
    summary = payload["upper_bound_summary"]
    lines.extend(
        [
            "",
            f"通过单元：{summary['passed_cell_symbol_count']}/{summary['cell_symbol_count']}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "两个官方数据缺口窗口已在任何 PnL 计算前对 BTC/ETH 同时固定排除；CURRENT Final OOS 未读取；生产默认值未修改。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估 BTC/ETH Premium Index 基差收敛的不可部署乐观上界。"
    )
    parser.add_argument(
        "--btc-premium-manifest",
        default=(
            "data/backtests/round23_premium_index/"
            "binance_um_premium_index_btcusdt_202001_202306_202408_202606.manifest.json"
        ),
    )
    parser.add_argument(
        "--eth-premium-manifest",
        default=(
            "data/backtests/round23_premium_index/"
            "binance_um_premium_index_ethusdt_202001_202306_202408_202606.manifest.json"
        ),
    )
    parser.add_argument(
        "--btc-funding-manifest",
        default=(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json"
        ),
    )
    parser.add_argument(
        "--eth-funding-manifest",
        default=(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json"
        ),
    )
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
        raise ValueError("Round 23 basis convergence 协议哈希不一致。")
    round22_path = ROUND22_RESULT_PATH.resolve()
    if _sha256(round22_path) != ROUND22_RESULT_SHA256:
        raise ValueError("Round 22 冻结结果哈希不一致。")
    round22_payload = json.loads(round22_path.read_text(encoding="utf-8"))
    if round22_payload.get("formal_round22_preregistration_ready"):
        raise ValueError("Round 22 不应允许正式注册。")
    if not str(round22_payload.get("conclusion") or "").startswith(
        "NO_PREREGISTERED_FUNDING_CARRY_CANDIDATE"
    ):
        raise ValueError("Round 22 失败结论不匹配。")
    if round22_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 22 之后 CURRENT Final OOS 已不再封存。")

    premium_manifest_paths = {
        "BTCUSDT": Path(args.btc_premium_manifest),
        "ETHUSDT": Path(args.eth_premium_manifest),
    }
    premium_manifests: dict[str, dict[str, Any]] = {}
    premium_windows: dict[str, list[dict[str, Any]]] = {}
    premium_rows: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for symbol in asset_audit.SYMBOLS:
        manifest, windows, rows = _read_premium_manifest(
            premium_manifest_paths[symbol],
            expected_sha256=PREMIUM_MANIFEST_SHA256[symbol],
            expected_symbol=symbol,
        )
        premium_manifests[symbol] = manifest
        premium_windows[symbol] = windows
        premium_rows[symbol] = rows
    btc_signatures = [_window_signature(item) for item in premium_windows["BTCUSDT"]]
    eth_signatures = [_window_signature(item) for item in premium_windows["ETHUSDT"]]
    if btc_signatures != eth_signatures:
        raise RuntimeError("BTC/ETH Premium Index 窗口定义不一致。")
    windows = premium_windows["BTCUSDT"]

    funding_manifest_paths = {
        "BTCUSDT": Path(args.btc_funding_manifest),
        "ETHUSDT": Path(args.eth_funding_manifest),
    }
    funding_manifests: dict[str, dict[str, Any]] = {}
    funding_events: dict[str, list[dict[str, Any]]] = {}
    for symbol in asset_audit.SYMBOLS:
        manifest, events = round22._read_funding_manifest(
            funding_manifest_paths[symbol],
            expected_sha256=FUNDING_MANIFEST_SHA256[symbol],
            expected_symbol=symbol,
        )
        funding_manifests[symbol] = manifest
        funding_events[symbol] = events

    cells, funding_audit = _evaluate_cells(
        windows,
        premium_rows,
        funding_events,
    )
    upper_bound = _upper_bound_summary(cells)
    family_ready = bool(upper_bound["all_cells_passed"])
    conclusion = (
        "BASIS_CONVERGENCE_FAMILY_WORTH_PREREGISTRATION：16/16 个年代、成本与标的单元均通过乐观上界；仅允许随后冻结真实成交基差、借币和盘口数据并另写单一因果退出协议。"
        if family_ready
        else "NO_PREREGISTERED_BASIS_CONVERGENCE_CANDIDATE：至少一个单元在 causal premium 方向、Oracle 退出和理想同步 Maker 假设下仍失败，排除本协议定义的基差收敛 family。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "NON_DEPLOYABLE_BASIS_CONVERGENCE_UPPER_BOUND",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {
            str(round22_path): ROUND22_RESULT_SHA256,
            **{
                str(premium_manifest_paths[symbol].resolve()): PREMIUM_MANIFEST_SHA256[
                    symbol
                ]
                for symbol in asset_audit.SYMBOLS
            },
            **{
                str(funding_manifest_paths[symbol].resolve()): FUNDING_MANIFEST_SHA256[
                    symbol
                ]
                for symbol in asset_audit.SYMBOLS
            },
        },
        "premium_manifests": {
            symbol: {
                "path": str(premium_manifest_paths[symbol].resolve()),
                "manifest_sha256": PREMIUM_MANIFEST_SHA256[symbol],
                "file_sha256": premium_manifests[symbol]["file_sha256"],
                "row_count": premium_manifests[symbol]["row_count"],
                "window_count": premium_manifests[symbol]["window_count"],
            }
            for symbol in asset_audit.SYMBOLS
        },
        "funding_manifests": {
            symbol: {
                "path": str(funding_manifest_paths[symbol].resolve()),
                "manifest_sha256": FUNDING_MANIFEST_SHA256[symbol],
                "file_sha256": funding_manifests[symbol]["file_sha256"],
                "event_count": funding_manifests[symbol]["event_count"],
            }
            for symbol in asset_audit.SYMBOLS
        },
        "direction_mode": "NEUTRAL",
        "capital_by_symbol": CAPITAL_BY_SYMBOL,
        "observation_rows": OBSERVATION_ROWS,
        "direction_rule": "ENTRY_PREMIUM_GE_ZERO_LONG_SPOT_SHORT_PERP_ELSE_REVERSE",
        "oracle_selects_future_exit": True,
        "oracle_is_deployable": False,
        "excluded_incomplete_windows": EXCLUDED_INCOMPLETE_WINDOWS,
        "ignored_risks": [
            "premium_index_vs_tradable_basis",
            "delta_drift_and_rebalancing",
            "borrow_interest_and_availability",
            "maker_queue_failure",
            "leg_latency",
            "slippage_and_market_impact",
            "margin_and_liquidation",
            "funding_prediction_error",
        ],
        "window_counts": {
            f"{role}_{split}": len(items)
            for role, splits in _group_windows(windows).items()
            for split, items in splits.items()
        },
        "funding_audit": funding_audit,
        "cells": cells,
        "upper_bound_summary": upper_bound,
        "formal_round23_preregistration_ready": family_ready,
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
    result_path = report_dir / "round23-basis-convergence-upper-bound-results.json"
    report_path = report_dir / "round23-basis-convergence-upper-bound-report.md"
    _write_json(result_path, result)
    report_path.write_text(_report_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "result": str(result_path.resolve()),
                "report": str(report_path.resolve()),
                "result_sha256": _sha256(result_path.resolve()),
                "upper_bound_summary": upper_bound,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
