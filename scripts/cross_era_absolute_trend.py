from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_funding_carry_upper_bound as round22
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
HOUR_MS = 60 * 60 * 1000
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round27-absolute-trend-phase-a-protocol.md"
)
PROTOCOL_SHA256 = "034d95c05564c28599f1afc296e19c1034a5c16ac40e715f0f7a4074bd9fab88"
DATA_PROTOCOL_SHA256 = "18fa83410608efb4f81fe7d92e8a3eae4bb32e2bafc3d1e5a207b3cd41358e05"
DATA_AUDIT_SHA256 = "adf1fbd3f4845cd5d82c34d043622142cf96c5203c703eeef3521c48ddd149e2"
ROUND26_RESULT_PATH = Path(
    "reports/cross-era-oos/round26-quarterly-calendar-spread-results.json"
)
ROUND26_RESULT_SHA256 = "171598de0aa94607f1b05ea6bfd4f79f3c58252b0eb39e3b3c2290cbe78965cf"
EXPECTED_ROWS_PER_ASSET = 43_056
EXPECTED_SOURCE_ARCHIVES = 59
EXPECTED_COMPLETE_DAYS = 1_794
SCENARIO_COST_RATES = {"BASE": 0.0010, "COST50": 0.00175}
SPLITS = {
    "DEVELOPMENT": {
        "segment": "HISTORY",
        "start": datetime(2021, 2, 6, 1, tzinfo=UTC),
        "end": datetime(2022, 6, 30, 23, tzinfo=UTC),
        "signal_days": 510,
    },
    "VALIDATION": {
        "segment": "HISTORY",
        "start": datetime(2022, 7, 1, 1, tzinfo=UTC),
        "end": datetime(2023, 6, 30, 23, tzinfo=UTC),
        "signal_days": 365,
    },
    "POSTHISTORY": {
        "segment": "POSTHISTORY",
        "start": datetime(2025, 2, 17, 1, tzinfo=UTC),
        "end": datetime(2026, 6, 30, 23, tzinfo=UTC),
        "signal_days": 499,
    },
}
PRICE_CSV_HEADER = (
    "segment",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "source_month",
    "source_zip_sha256",
)
ASSET_CONFIG = {
    "BTC": {
        "symbol": "BTCUSDT",
        "capital": 500.0,
        "price_manifest": Path(
            "data/backtests/round27_absolute_trend/"
            "binance_um_perpetual_btcusdt_1h_202007_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": (
            "8b7762ba9c478c1cbc3ee168516f0ffb9f134a72415a7d027bfba4ed11254123"
        ),
        "price_csv_sha256": (
            "a1928906a7d7328f85eef7ebde2574b93d20fd0b8a92479ac758acfc624382f4"
        ),
        "funding_manifest": Path(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json"
        ),
        "funding_manifest_sha256": (
            "a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57"
        ),
        "funding_csv_sha256": (
            "08a4fec97e9e2555d28135fc70f49d6115b966868fc912fc29faab80b722c5e2"
        ),
    },
    "ETH": {
        "symbol": "ETHUSDT",
        "capital": 300.0,
        "price_manifest": Path(
            "data/backtests/round27_absolute_trend/"
            "binance_um_perpetual_ethusdt_1h_202007_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": (
            "d9a47fe659f76156565b338811692f6bdf645655d159d795774c52ceb090459a"
        ),
        "price_csv_sha256": (
            "d0bbf425c00cb9574c6e9059f5b8e6b1349069879dc3fb6a5abb82087140faf9"
        ),
        "funding_manifest": Path(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json"
        ),
        "funding_manifest_sha256": (
            "19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f"
        ),
        "funding_csv_sha256": (
            "5ec93a6a3d7397fbe0e6d3b82c28873e11b2559b1be34c7d351b90e5d7b9108a"
        ),
    },
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _read_round26_result(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if _sha256(resolved) != ROUND26_RESULT_SHA256:
        raise ValueError("Round 26 结果哈希不一致。")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not str(payload.get("conclusion", "")).startswith(
        "NO_PREREGISTERED_QUARTERLY_CALENDAR_SPREAD_CANDIDATE"
    ):
        raise ValueError("Round 26 前置结论不一致。")
    if payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 26 CURRENT Final OOS 未保持封存。")
    if bool(payload.get("final_oos_authorized")) or bool(
        payload.get("stable_profit_claimed")
    ):
        raise ValueError("Round 26 不得授权 Final OOS 或稳定收益声明。")
    return payload


def _read_price_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_csv_sha256: str,
    expected_symbol: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    path = manifest_path.resolve()
    if _sha256(path) != expected_manifest_sha256:
        raise ValueError(f"{expected_symbol} price manifest 哈希不一致。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = {
        "data_protocol_sha256": DATA_PROTOCOL_SHA256,
        "availability_audit_sha256": DATA_AUDIT_SHA256,
        "provider": "binance_data_vision",
        "market": "USDS_M",
        "data_type": "klines",
        "interval": "1h",
        "symbol": expected_symbol,
        "file_sha256": expected_csv_sha256,
        "row_count": EXPECTED_ROWS_PER_ASSET,
        "complete_utc_day_count": EXPECTED_COMPLETE_DAYS,
        "duplicate_rows": 0,
        "in_segment_missing_hours": 0,
        "source_archive_count": EXPECTED_SOURCE_ARCHIVES,
        "official_checksums_verified": True,
        "final_oos_status": "SEALED_NOT_EVALUATED",
    }
    for key, expected in expected_fields.items():
        if manifest.get(key) != expected:
            raise ValueError(f"{expected_symbol} price manifest 字段 {key} 不一致。")
    archives = list(manifest.get("source_archives") or [])
    if len(archives) != EXPECTED_SOURCE_ARCHIVES:
        raise ValueError(f"{expected_symbol} price source archive 数量不一致。")
    archive_by_month: dict[str, Mapping[str, Any]] = {}
    for archive in archives:
        month = str(archive.get("month"))
        if month in archive_by_month:
            raise ValueError(f"{expected_symbol} price source month 重复。")
        if "2023-07" <= month <= "2024-07":
            raise ValueError(f"{expected_symbol} price source 触碰隔离区间。")
        if (
            not bool(archive.get("official_checksum_verified"))
            or int(archive.get("missing_hour_count", -1)) != 0
            or int(archive.get("duplicate_row_count", -1)) != 0
            or int(archive.get("invalid_ohlc_row_count", -1)) != 0
        ):
            raise ValueError(f"{expected_symbol} price source 月度审计失败。")
        archive_by_month[month] = archive

    data_path = path.parent / str(manifest["file_name"])
    if hashlib.sha256(data_path.read_bytes()).hexdigest() != expected_csv_sha256:
        raise ValueError(f"{expected_symbol} price CSV 哈希不一致。")
    rows: list[dict[str, Any]] = []
    previous_by_segment: dict[str, int] = {}
    with data_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != PRICE_CSV_HEADER:
            raise ValueError(f"{expected_symbol} price CSV 表头不一致。")
        for line_number, row in enumerate(reader, start=2):
            try:
                segment = str(row["segment"])
                open_time = int(row["open_time"])
                open_price = float(row["open"])
                high = float(row["high"])
                low = float(row["low"])
                close = float(row["close"])
                source_month = str(row["source_month"])
                source_sha = str(row["source_zip_sha256"])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(
                    f"{expected_symbol} price CSV 第 {line_number} 行无效。"
                ) from exc
            archive = archive_by_month.get(source_month)
            if archive is None or source_sha != str(archive.get("zip_sha256")):
                raise ValueError(f"{expected_symbol} price 行级来源不一致。")
            expected_segment = "HISTORY" if source_month <= "2023-06" else "POSTHISTORY"
            if segment != expected_segment:
                raise ValueError(f"{expected_symbol} price segment 不一致。")
            prices = (open_price, high, low, close)
            if any(not math.isfinite(value) or value <= 0 for value in prices):
                raise ValueError(f"{expected_symbol} price 包含无效价格。")
            if high < max(open_price, close) or low > min(open_price, close) or high < low:
                raise ValueError(f"{expected_symbol} price OHLC 关系无效。")
            previous = previous_by_segment.get(segment)
            if previous is not None and open_time != previous + HOUR_MS:
                raise ValueError(f"{expected_symbol} {segment} 小时不连续。")
            previous_by_segment[segment] = open_time
            rows.append(
                {
                    "segment": segment,
                    "open_time": open_time,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                }
            )
    if len(rows) != EXPECTED_ROWS_PER_ASSET:
        raise ValueError(f"{expected_symbol} price CSV 行数不一致。")
    segment_counts = Counter(str(item["segment"]) for item in rows)
    if segment_counts != {"HISTORY": 26_280, "POSTHISTORY": 16_776}:
        raise ValueError(f"{expected_symbol} price segment 行数不一致。")
    return manifest, rows, {
        "row_count": len(rows),
        "source_archive_count": len(archives),
        "segment_counts": dict(segment_counts),
        "official_checksums_verified": True,
        "in_segment_missing_hours": 0,
        "passed": True,
    }


def _assert_price_alignment(prices: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    assets = list(ASSET_CONFIG)
    reference = [int(item["open_time"]) for item in prices[assets[0]]]
    aligned = all(
        [int(item["open_time"]) for item in prices[asset]] == reference
        for asset in assets[1:]
    )
    if not aligned:
        raise ValueError("Round 27 BTC/ETH price open_time 未完全对齐。")
    return {
        "asset_count": len(assets),
        "row_count_per_asset": len(reference),
        "timestamps_identical": True,
        "passed": True,
    }


def _build_signals(
    rows: Sequence[Mapping[str, Any]], split: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    segment = str(split["segment"])
    daily_close: dict[date, tuple[float, int]] = {}
    for row in rows:
        if str(row["segment"]) != segment:
            continue
        timestamp = datetime.fromtimestamp(int(row["open_time"]) / 1000, tz=UTC)
        if timestamp.hour == 23:
            daily_close[timestamp.date()] = (float(row["close"]), int(row["open_time"]))
    start_date = split["start"].date()
    end_date = split["end"].date()
    signals: list[dict[str, Any]] = []
    current = start_date
    while current <= end_date:
        warmup_dates = [current - timedelta(days=offset) for offset in range(200, 0, -1)]
        if any(value not in daily_close for value in warmup_dates):
            raise ValueError(f"{current.isoformat()} 缺少连续 200 日因果 warmup。")
        closes = [daily_close[value][0] for value in warmup_dates]
        sma50 = statistics.fmean(closes[-50:])
        sma200 = statistics.fmean(closes)
        latest_close_time = daily_close[warmup_dates[-1]][1]
        execution = datetime.combine(current, datetime.min.time(), tzinfo=UTC) + timedelta(
            hours=1
        )
        execution_ms = int(execution.timestamp() * 1000)
        if latest_close_time >= execution_ms:
            raise ValueError("Round 27 signal 使用了未完成或未来日线。")
        signals.append(
            {
                "date": current.isoformat(),
                "execution_time": execution_ms,
                "target_position": 1 if sma50 >= sma200 else -1,
                "sma50": sma50,
                "sma200": sma200,
                "warmup_day_count": len(closes),
                "latest_source_close_time": latest_close_time,
            }
        )
        current += timedelta(days=1)
    expected_days = int(split["signal_days"])
    if len(signals) != expected_days:
        raise RuntimeError(f"Round 27 signal 日数不一致: {len(signals)} != {expected_days}")
    canonical = json.dumps(signals, sort_keys=True, separators=(",", ":"))
    audit = {
        "signal_day_count": len(signals),
        "first_signal_time": signals[0]["execution_time"],
        "last_signal_time": signals[-1]["execution_time"],
        "minimum_warmup_day_count": min(item["warmup_day_count"] for item in signals),
        "maximum_warmup_day_count": max(item["warmup_day_count"] for item in signals),
        "latest_source_precedes_execution": all(
            int(item["latest_source_close_time"]) < int(item["execution_time"])
            for item in signals
        ),
        "signal_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "future_data_used": False,
        "passed": True,
    }
    return signals, audit


def _profit_factor(values: Sequence[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return None if losses <= 0 else gains / losses


def _performance_metrics(
    daily_equity: Sequence[tuple[date, float]],
    *,
    initial_capital: float,
    maximum_drawdown_pct: float,
) -> dict[str, Any]:
    if not daily_equity:
        raise ValueError("Round 27 cell 没有日度权益。")
    daily_pnl: list[float] = []
    daily_returns: list[float] = []
    previous = initial_capital
    month_pnl: dict[str, float] = defaultdict(float)
    for day, equity in daily_equity:
        pnl = equity - previous
        daily_pnl.append(pnl)
        daily_returns.append(pnl / previous if previous != 0 else 0.0)
        month_pnl[day.strftime("%Y-%m")] += pnl
        previous = equity
    pf = _profit_factor(daily_pnl)
    if len(daily_returns) >= 2 and statistics.stdev(daily_returns) > 0:
        sharpe = statistics.fmean(daily_returns) / statistics.stdev(daily_returns) * math.sqrt(365)
    else:
        sharpe = None
    positive_months = [value for value in month_pnl.values() if value > 0]
    positive_month_ratio = len(positive_months) / len(month_pnl)
    positive_month_profit = sum(positive_months)
    best_month_concentration = (
        max(positive_months) / positive_month_profit
        if positive_months and positive_month_profit > 0
        else 1.0
    )
    return {
        "daily_observation_count": len(daily_equity),
        "daily_profit_factor": pf,
        "daily_annualized_sharpe": sharpe,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "calendar_month_count": len(month_pnl),
        "positive_calendar_month_count": len(positive_months),
        "positive_calendar_month_ratio": positive_month_ratio,
        "best_profitable_month_concentration": best_month_concentration,
        "calendar_month_pnl": dict(sorted(month_pnl.items())),
    }


def _simulate_cell(
    rows: Sequence[Mapping[str, Any]],
    funding_events: Sequence[Mapping[str, Any]],
    signals: Sequence[Mapping[str, Any]],
    *,
    split: Mapping[str, Any],
    initial_capital: float,
    execution_cost_rate: float,
) -> dict[str, Any]:
    start_ms = int(split["start"].timestamp() * 1000)
    end_ms = int(split["end"].timestamp() * 1000)
    segment = str(split["segment"])
    path = [
        item
        for item in rows
        if str(item["segment"]) == segment
        and start_ms <= int(item["open_time"]) <= end_ms
    ]
    expected_hour_count = (end_ms - start_ms) // HOUR_MS + 1
    if len(path) != expected_hour_count or any(
        int(current["open_time"]) - int(previous["open_time"]) != HOUR_MS
        for previous, current in zip(path, path[1:])
    ):
        raise ValueError("Round 27 cell 价格小时路径不完整。")
    signal_by_time = {int(item["execution_time"]): item for item in signals}
    if len(signal_by_time) != int(split["signal_days"]):
        raise ValueError("Round 27 cell signal 数量或时间唯一性不一致。")

    funding_by_hour: dict[int, Mapping[str, Any]] = {}
    funding_offsets: list[int] = []
    selected_funding_events = 0
    for event in funding_events:
        funding_time = int(event["funding_time"])
        offset = funding_time % HOUR_MS
        if offset >= 1_000:
            raise ValueError("Round 27 funding_time 偏离整点达到 1 秒。")
        funding_hour = funding_time - offset
        if not start_ms <= funding_hour <= end_ms:
            continue
        selected_funding_events += 1
        if funding_hour in funding_by_hour:
            raise ValueError("Round 27 多个 funding event 映射到同一小时。")
        funding_by_hour[funding_hour] = event
        funding_offsets.append(offset)
    path_times = {int(item["open_time"]) for item in path}
    if not set(funding_by_hour).issubset(path_times):
        raise ValueError("Round 27 funding event 无对应 1h open。")

    position = 0
    quantity = 0.0
    entry_price = 0.0
    cash_pnl = 0.0
    price_pnl = 0.0
    funding_pnl = 0.0
    execution_costs = 0.0
    execution_side_count = 0
    signal_flip_count = 0
    funding_events_while_held = 0
    daily_equity: list[tuple[date, float]] = []
    trades: list[dict[str, Any]] = []
    peak_equity = initial_capital
    maximum_drawdown_pct = 0.0

    def mark_equity(price: float) -> float:
        unrealized = position * quantity * (price - entry_price) if position else 0.0
        return initial_capital + cash_pnl + unrealized

    def observe(price: float) -> float:
        nonlocal peak_equity, maximum_drawdown_pct
        equity = mark_equity(price)
        peak_equity = max(peak_equity, equity)
        maximum_drawdown_pct = max(
            maximum_drawdown_pct,
            (peak_equity - equity) / max(peak_equity, 1e-12),
        )
        return equity

    for row in path:
        open_time = int(row["open_time"])
        open_price = float(row["open"])
        close_price = float(row["close"])
        funding = funding_by_hour.get(open_time)
        if funding is not None and position:
            payment = -position * quantity * open_price * float(funding["funding_rate"])
            funding_pnl += payment
            cash_pnl += payment
            funding_events_while_held += 1

        signal = signal_by_time.get(open_time)
        if signal is not None:
            target = int(signal["target_position"])
            if target not in {-1, 1}:
                raise ValueError("Round 27 target position 必须为 long 或 short。")
            if position != target:
                if position:
                    realized = position * quantity * (open_price - entry_price)
                    price_pnl += realized
                    cash_pnl += realized
                    close_cost = quantity * open_price * execution_cost_rate
                    execution_costs += close_cost
                    cash_pnl -= close_cost
                    execution_side_count += 1
                    signal_flip_count += 1
                    trades.append(
                        {
                            "time": open_time,
                            "action": "CLOSE_FOR_FLIP",
                            "position": position,
                            "price": open_price,
                            "quantity": quantity,
                            "cost": close_cost,
                        }
                    )
                position = target
                quantity = initial_capital / open_price
                entry_price = open_price
                open_cost = quantity * open_price * execution_cost_rate
                execution_costs += open_cost
                cash_pnl -= open_cost
                execution_side_count += 1
                trades.append(
                    {
                        "time": open_time,
                        "action": "OPEN",
                        "position": position,
                        "price": open_price,
                        "quantity": quantity,
                        "cost": open_cost,
                    }
                )
        observe(open_price)
        timestamp = datetime.fromtimestamp(open_time / 1000, tz=UTC)
        if open_time == end_ms:
            if not position:
                raise ValueError("Round 27 最终退出前意外空仓。")
            realized = position * quantity * (close_price - entry_price)
            price_pnl += realized
            cash_pnl += realized
            exit_cost = quantity * close_price * execution_cost_rate
            execution_costs += exit_cost
            cash_pnl -= exit_cost
            execution_side_count += 1
            trades.append(
                {
                    "time": open_time,
                    "action": "FINAL_CLOSE",
                    "position": position,
                    "price": close_price,
                    "quantity": quantity,
                    "cost": exit_cost,
                }
            )
            position = 0
            quantity = 0.0
            entry_price = 0.0
            equity = observe(close_price)
            daily_equity.append((timestamp.date(), equity))
        elif timestamp.hour == 23:
            equity = observe(close_price)
            daily_equity.append((timestamp.date(), equity))

    if position != 0 or len(daily_equity) != int(split["signal_days"]):
        raise RuntimeError("Round 27 cell 最终持仓或日度权益数量不一致。")
    total_pnl = cash_pnl
    decomposition_error = total_pnl - (price_pnl + funding_pnl - execution_costs)
    if abs(decomposition_error) > 1e-8:
        raise RuntimeError("Round 27 PnL 分解不闭合。")
    performance = _performance_metrics(
        daily_equity,
        initial_capital=initial_capital,
        maximum_drawdown_pct=maximum_drawdown_pct,
    )
    pf = performance["daily_profit_factor"]
    sharpe = performance["daily_annualized_sharpe"]
    checks = {
        "total_net_profit_strictly_positive": total_pnl > 0,
        "daily_profit_factor_gt_1": total_pnl > 0 if pf is None else float(pf) > 1.0,
        "maximum_drawdown_le_20pct": maximum_drawdown_pct <= 0.20,
        "daily_annualized_sharpe_gt_0_5": sharpe is not None and float(sharpe) > 0.5,
        "positive_calendar_month_ratio_ge_50pct": (
            performance["positive_calendar_month_ratio"] >= 0.50
        ),
        "best_profitable_month_concentration_le_35pct": (
            performance["best_profitable_month_concentration"] <= 0.35
        ),
        "hourly_price_coverage_100pct": len(path) == expected_hour_count,
        "funding_mapping_coverage_100pct": (
            len(funding_by_hour) == selected_funding_events
        ),
        "all_signals_causal_200_day": all(
            int(item["warmup_day_count"]) == 200
            and int(item["latest_source_close_time"]) < int(item["execution_time"])
            for item in signals
        ),
        "all_execution_sides_costed_and_flat": (
            execution_side_count == len(trades) and position == 0
        ),
    }
    return {
        "metrics": {
            **performance,
            "initial_capital": initial_capital,
            "ending_equity": initial_capital + total_pnl,
            "total_pnl": total_pnl,
            "return_pct": total_pnl / initial_capital,
            "price_pnl": price_pnl,
            "funding_pnl": funding_pnl,
            "execution_costs": execution_costs,
            "execution_cost_rate": execution_cost_rate,
            "execution_side_count": execution_side_count,
            "signal_flip_count": signal_flip_count,
            "long_signal_days": sum(
                int(item["target_position"]) == 1 for item in signals
            ),
            "short_signal_days": sum(
                int(item["target_position"]) == -1 for item in signals
            ),
            "hourly_price_row_count": len(path),
            "expected_hourly_price_row_count": expected_hour_count,
            "hourly_price_coverage_ratio": len(path) / expected_hour_count,
            "selected_funding_event_count": selected_funding_events,
            "funding_events_while_held": funding_events_while_held,
            "funding_mapping_coverage_ratio": (
                len(funding_by_hour) / selected_funding_events
                if selected_funding_events
                else 1.0
            ),
            "funding_timestamp_normalized_count": sum(
                offset > 0 for offset in funding_offsets
            ),
            "maximum_funding_timestamp_offset_ms": max(funding_offsets, default=0),
            "final_position": position,
            "pnl_decomposition_error": decomposition_error,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "trades": trades,
    }


def _evaluate_cells(
    prices: Mapping[str, Sequence[Mapping[str, Any]]],
    funding: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    cells: dict[str, Any] = {}
    signal_audit: dict[str, Any] = {}
    signals_by_asset_role: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for role, split in SPLITS.items():
        signal_audit[role] = {}
        for asset in ASSET_CONFIG:
            signals, audit = _build_signals(prices[asset], split)
            signals_by_asset_role[(asset, role)] = signals
            signal_audit[role][asset] = audit
        for scenario, cost_rate in SCENARIO_COST_RATES.items():
            symbols = {}
            for asset, config in ASSET_CONFIG.items():
                symbols[asset] = _simulate_cell(
                    prices[asset],
                    funding[asset],
                    signals_by_asset_role[(asset, role)],
                    split=split,
                    initial_capital=float(config["capital"]),
                    execution_cost_rate=cost_rate,
                )
            cells[f"{role}_{scenario}"] = {
                "role": role,
                "scenario": scenario,
                "execution_cost_rate": cost_rate,
                "symbols": symbols,
            }
    return cells, signal_audit


def _summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [
        cell["symbols"][asset] for cell in cells.values() for asset in ASSET_CONFIG
    ]
    if len(selected) != 12:
        raise RuntimeError(f"Round 27 cell-symbol 数量不一致: {len(selected)} != 12")
    return {
        "cell_symbol_count": len(selected),
        "passed_cell_symbol_count": sum(bool(item["passed"]) for item in selected),
        "all_cells_passed": all(bool(item["passed"]) for item in selected),
        "minimum_total_pnl": min(float(item["metrics"]["total_pnl"]) for item in selected),
        "minimum_daily_sharpe": min(
            float(item["metrics"]["daily_annualized_sharpe"])
            for item in selected
            if item["metrics"]["daily_annualized_sharpe"] is not None
        ),
        "maximum_drawdown_pct": max(
            float(item["metrics"]["maximum_drawdown_pct"]) for item in selected
        ),
        "minimum_positive_calendar_month_ratio": min(
            float(item["metrics"]["positive_calendar_month_ratio"])
            for item in selected
        ),
        "maximum_best_profitable_month_concentration": max(
            float(item["metrics"]["best_profitable_month_concentration"])
            for item in selected
        ),
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 27：BTC/ETH SMA50/200 绝对趋势结果",
        "",
        "每日 01:00 UTC 仅用此前 200 个完整 UTC 日 close 生成信号；完整计入真实 funding、主动费率与滑点。",
        "",
        "| 单元 | 资产 | 净收益 | 收益率 | 日 PF | Sharpe | 最大回撤 | 正收益月 | 最佳月集中度 | Funding | 成本 | 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for asset in ASSET_CONFIG:
            item = cell["symbols"][asset]
            metrics = item["metrics"]
            failed = [name for name, passed in item["checks"].items() if not passed]
            pf = metrics["daily_profit_factor"]
            pf_text = "∞" if pf is None and metrics["total_pnl"] > 0 else (
                "N/A" if pf is None else f"{float(pf):.3f}"
            )
            sharpe = metrics["daily_annualized_sharpe"]
            lines.append(
                "| `{cell}` | {asset} | {pnl:.4f} | {ret:.2%} | {pf} | {sharpe} | "
                "{dd:.2%} | {pm:.2%} | {conc:.2%} | {funding:.4f} | {cost:.4f} | "
                "{passed} | {failed} |".format(
                    cell=cell_name,
                    asset=asset,
                    pnl=metrics["total_pnl"],
                    ret=metrics["return_pct"],
                    pf=pf_text,
                    sharpe="N/A" if sharpe is None else f"{float(sharpe):.3f}",
                    dd=metrics["maximum_drawdown_pct"],
                    pm=metrics["positive_calendar_month_ratio"],
                    conc=metrics["best_profitable_month_concentration"],
                    funding=metrics["funding_pnl"],
                    cost=metrics["execution_costs"],
                    passed="是" if item["passed"] else "否",
                    failed=", ".join(failed),
                )
            )
    summary = payload["summary"]
    lines.extend(
        [
            "",
            f"通过单元：{summary['passed_cell_symbol_count']}/{summary['cell_symbol_count']}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="评估 Round 27 SMA50/200 绝对趋势单一候选。")
    parser.add_argument("--round26-result", default=str(ROUND26_RESULT_PATH))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 27 Phase A 协议哈希不一致。")
    round26_payload = _read_round26_result(Path(args.round26_result))
    prices: dict[str, list[dict[str, Any]]] = {}
    funding: dict[str, list[dict[str, Any]]] = {}
    price_manifests: dict[str, Any] = {}
    funding_manifests: dict[str, Any] = {}
    price_audit: dict[str, Any] = {}
    for asset, config in ASSET_CONFIG.items():
        price_manifest, prices[asset], price_audit[asset] = _read_price_manifest(
            config["price_manifest"],
            expected_manifest_sha256=str(config["price_manifest_sha256"]),
            expected_csv_sha256=str(config["price_csv_sha256"]),
            expected_symbol=str(config["symbol"]),
        )
        funding_manifest, funding[asset] = round22._read_funding_manifest(
            config["funding_manifest"],
            expected_sha256=str(config["funding_manifest_sha256"]),
            expected_symbol=str(config["symbol"]),
        )
        if funding_manifest.get("file_sha256") != config["funding_csv_sha256"]:
            raise ValueError(f"{asset} funding CSV 冻结哈希不一致。")
        price_manifests[asset] = {
            "path": str(config["price_manifest"].resolve()),
            "manifest_sha256": config["price_manifest_sha256"],
            "csv_sha256": price_manifest["file_sha256"],
        }
        funding_manifests[asset] = {
            "path": str(config["funding_manifest"].resolve()),
            "manifest_sha256": config["funding_manifest_sha256"],
            "csv_sha256": funding_manifest["file_sha256"],
        }
    alignment_audit = _assert_price_alignment(prices)
    cells, signal_audit = _evaluate_cells(prices, funding)
    summary = _summary(cells)
    candidate_ready = bool(summary["all_cells_passed"])
    if candidate_ready:
        conclusion = (
            "ABSOLUTE_TREND_WORTH_EXECUTION_PREREGISTRATION：12/12 个严格单元全部通过；"
            "只允许继续冻结真实盘口冲击、最小数量与逐笔执行，不声明生产稳定收益。"
        )
    else:
        conclusion = (
            "NO_PREREGISTERED_ABSOLUTE_TREND_CANDIDATE：至少一个严格单元失败，"
            "排除本协议定义的 SMA50/200、1x、每日 01:00 执行 family。"
        )
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "ROUND27_ABSOLUTE_TREND_PHASE_A",
        "candidate_id": "ABS_TREND_SMA50_200_1X_V1",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {
            "round26_result_sha256": ROUND26_RESULT_SHA256,
            "data_protocol_sha256": DATA_PROTOCOL_SHA256,
            "data_audit_sha256": DATA_AUDIT_SHA256,
        },
        "round26_conclusion": round26_payload["conclusion"],
        "price_manifests": price_manifests,
        "funding_manifests": funding_manifests,
        "price_data_audit": price_audit,
        "cross_asset_alignment_audit": alignment_audit,
        "signal_audit": signal_audit,
        "direction_mode": "NEUTRAL",
        "gross_capital_by_asset": {
            asset: config["capital"] for asset, config in ASSET_CONFIG.items()
        },
        "execution_cost_rate_by_scenario": SCENARIO_COST_RATES,
        "leverage": 1.0,
        "sma_fast_days": 50,
        "sma_slow_days": 200,
        "execution_hour_utc": 1,
        "cells": cells,
        "summary": summary,
        "formal_round27_execution_preregistration_ready": candidate_ready,
        "selected_candidate_id": (
            "ABS_TREND_SMA50_200_1X_V1" if candidate_ready else None
        ),
        "final_oos_authorization_ready": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    result_path = report_dir / "round27-absolute-trend-results.json"
    report_path = report_dir / "round27-absolute-trend-report.md"
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path.write_text(_report_markdown(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "report_path": str(report_path.resolve()),
                "passed_cell_symbol_count": summary["passed_cell_symbol_count"],
                "cell_symbol_count": summary["cell_symbol_count"],
                "formal_round27_execution_preregistration_ready": candidate_ready,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
