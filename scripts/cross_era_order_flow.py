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
PROTOCOL_PATH = Path("reports/cross-era-oos/round29-order-flow-phase-a-protocol.md")
PROTOCOL_SHA256 = "721088ca67ed4141896c40ae75f91d7249e54a64fc9bd9ff30bd6994de669911"
DATA_PROTOCOL_SHA256 = "46f28027acbafbe687dc14de7fd57203f61e06872a29d71a02766f82c637784d"
DATA_AUDIT_SHA256 = "a63320dc95d9f7a98c05b762731be049691beaa0cca2dc85a9586df7b737fee8"
ROUND28_RESULT_PATH = Path(
    "reports/cross-era-oos/round28-spot-quarterly-carry-results.json"
)
ROUND28_RESULT_SHA256 = "f0134d155d6f8435aeca66f66d826c7ff24eb05f46d295050aec93ec00f30f8f"
EXPECTED_ROWS_PER_ASSET = 43_056
EXPECTED_SOURCE_ARCHIVES = 59
EXPECTED_HEADER = (
    "segment",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_volume",
    "taker_buy_quote_volume",
    "source_month",
    "source_zip_sha256",
)
SCENARIO_COST_RATES = {"BASE": 0.0010, "COST50": 0.00175}
FLOW_WINDOW_HOURS = 8
FLOW_THRESHOLD = 0.15
EXPECTED_SEGMENT_ROWS = {
    "HISTORY": 26_280,
    "POSTHISTORY": 16_776,
}
SPLITS = {
    "DEVELOPMENT": {
        "segment": "HISTORY",
        "start": datetime(2021, 2, 6, 1, tzinfo=UTC),
        "end": datetime(2022, 6, 30, 23, tzinfo=UTC),
        "signal_rows": 12_239,
    },
    "VALIDATION": {
        "segment": "HISTORY",
        "start": datetime(2022, 7, 1, 1, tzinfo=UTC),
        "end": datetime(2023, 6, 30, 23, tzinfo=UTC),
        "signal_rows": 8_759,
    },
    "POSTHISTORY": {
        "segment": "POSTHISTORY",
        "start": datetime(2025, 2, 17, 1, tzinfo=UTC),
        "end": datetime(2026, 6, 30, 23, tzinfo=UTC),
        "signal_rows": 11_975,
    },
}
ASSET_CONFIG = {
    "BTC": {
        "capital": 500.0,
        "symbol": "BTCUSDT",
        "price_manifest": Path(
            "data/backtests/round29_order_flow/"
            "binance_um_order_flow_btcusdt_1h_202007_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": "c350decb7e30bc32b60616441ea0790627dbab50f328fbbdbab026d3d0735cce",
        "price_csv_sha256": "de6a99cec32a980c7ddf40d69aaffd8e4633bf9570b03d355a192883b9a12d10",
        "funding_manifest": Path(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json"
        ),
        "funding_manifest_sha256": "a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57",
        "funding_csv_sha256": "08a4fec97e9e2555d28135fc70f49d6115b966868fc912fc29faab80b722c5e2",
    },
    "ETH": {
        "capital": 300.0,
        "symbol": "ETHUSDT",
        "price_manifest": Path(
            "data/backtests/round29_order_flow/"
            "binance_um_order_flow_ethusdt_1h_202007_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": "3a4a1ec2cd2ea2a3f5db1a7a95c18a734a8613a1745dfde9e4d9ff2d22572067",
        "price_csv_sha256": "0de08f9129ef4d2227f6f66507795cd6de2d2961b17b6eeef6d07acb969ebef5",
        "funding_manifest": Path(
            "data/backtests/round22_funding_carry/"
            "binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json"
        ),
        "funding_manifest_sha256": "19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f",
        "funding_csv_sha256": "5ec93a6a3d7397fbe0e6d3b82c28873e11b2559b1be34c7d351b90e5d7b9108a",
    },
}


def _read_round28_result() -> dict[str, Any]:
    path = ROUND28_RESULT_PATH.resolve()
    if _sha256(path) != ROUND28_RESULT_SHA256:
        raise ValueError("Round 28 结果哈希不一致。")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not str(payload.get("conclusion", "")).startswith(
        "NO_PREREGISTERED_SPOT_QUARTERLY_CARRY_CANDIDATE"
    ):
        raise ValueError("Round 28 前置结论不一致。")
    if payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 28 Final OOS 未保持封存。")
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
        raise ValueError(f"{expected_symbol} Round 29 manifest 哈希不一致。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    fields = {
        "data_protocol_sha256": DATA_PROTOCOL_SHA256,
        "availability_audit_sha256": DATA_AUDIT_SHA256,
        "provider": "binance_data_vision",
        "market": "USDS_M",
        "data_type": "klines_with_order_flow",
        "interval": "1h",
        "symbol": expected_symbol,
        "file_sha256": expected_csv_sha256,
        "row_count": EXPECTED_ROWS_PER_ASSET,
        "source_archive_count": EXPECTED_SOURCE_ARCHIVES,
        "duplicate_rows": 0,
        "in_segment_missing_hours": 0,
        "official_checksums_verified": True,
        "final_oos_status": "SEALED_NOT_EVALUATED",
    }
    for key, expected in fields.items():
        if manifest.get(key) != expected:
            raise ValueError(f"{expected_symbol} manifest 字段 {key} 不一致。")
    archives = list(manifest.get("source_archives") or [])
    if len(archives) != EXPECTED_SOURCE_ARCHIVES:
        raise ValueError(f"{expected_symbol} source archive 数量不一致。")
    archive_sha = {str(item["month"]): str(item["zip_sha256"]) for item in archives}
    if len(archive_sha) != EXPECTED_SOURCE_ARCHIVES or any(
        not bool(item.get("official_checksum_verified")) for item in archives
    ):
        raise ValueError(f"{expected_symbol} source archive 审计失败。")
    csv_path = path.parent / str(manifest["file_name"])
    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != expected_csv_sha256:
        raise ValueError(f"{expected_symbol} Round 29 CSV 哈希不一致。")
    rows: list[dict[str, Any]] = []
    previous_by_segment: dict[str, int] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != EXPECTED_HEADER:
            raise ValueError(f"{expected_symbol} Round 29 CSV 表头不一致。")
        for line_number, row in enumerate(reader, start=2):
            try:
                item = {
                    "segment": str(row["segment"]),
                    "open_time": int(row["open_time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row["volume"]),
                    "quote_volume": float(row["quote_volume"]),
                    "taker_buy_volume": float(row["taker_buy_volume"]),
                    "taker_buy_quote_volume": float(row["taker_buy_quote_volume"]),
                    "source_month": str(row["source_month"]),
                    "source_zip_sha256": str(row["source_zip_sha256"]),
                }
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"{expected_symbol} CSV 第 {line_number} 行无效。") from exc
            ohlc = (item["open"], item["high"], item["low"], item["close"])
            volumes = (
                item["volume"],
                item["quote_volume"],
                item["taker_buy_volume"],
                item["taker_buy_quote_volume"],
            )
            if any(not math.isfinite(x) or x <= 0 for x in ohlc) or any(
                not math.isfinite(x) or x < 0 for x in volumes
            ):
                raise ValueError(f"{expected_symbol} CSV 第 {line_number} 行数值无效。")
            if (
                item["high"] < max(item["open"], item["close"])
                or item["low"] > min(item["open"], item["close"])
                or item["high"] < item["low"]
            ):
                raise ValueError(f"{expected_symbol} CSV 第 {line_number} 行 OHLC 关系无效。")
            if item["segment"] not in EXPECTED_SEGMENT_ROWS:
                raise ValueError(f"{expected_symbol} CSV 第 {line_number} 行 segment 无效。")
            if item["quote_volume"] == 0:
                if not (
                    item["volume"] == item["taker_buy_volume"] == item["taker_buy_quote_volume"] == 0
                    and item["open"] == item["high"] == item["low"] == item["close"]
                ):
                    raise ValueError(f"{expected_symbol} CSV 第 {line_number} 行零量形态无效。")
            elif item["taker_buy_quote_volume"] > item["quote_volume"] + 1e-9:
                raise ValueError(f"{expected_symbol} CSV 第 {line_number} 行主动买量超过总量。")
            if archive_sha.get(item["source_month"]) != item["source_zip_sha256"]:
                raise ValueError(f"{expected_symbol} CSV 第 {line_number} 行 source SHA 不一致。")
            previous = previous_by_segment.get(item["segment"])
            if previous is not None and item["open_time"] != previous + HOUR_MS:
                raise ValueError(f"{expected_symbol} {item['segment']} 小时不连续。")
            previous_by_segment[item["segment"]] = item["open_time"]
            rows.append(item)
    if len(rows) != EXPECTED_ROWS_PER_ASSET:
        raise ValueError(f"{expected_symbol} CSV 行数不一致。")
    segment_counts = Counter(item["segment"] for item in rows)
    if dict(segment_counts) != EXPECTED_SEGMENT_ROWS:
        raise ValueError(f"{expected_symbol} CSV segment 行数不一致。")
    return manifest, rows, {
        "row_count": len(rows),
        "source_archive_count": len(archives),
        "zero_volume_neutral_row_count": sum(item["quote_volume"] == 0 for item in rows),
        "price_and_order_flow_coverage_ratio": 1.0,
        "passed": True,
    }


def _assert_alignment(prices: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    reference = [int(item["open_time"]) for item in prices["BTC"]]
    if [int(item["open_time"]) for item in prices["ETH"]] != reference:
        raise ValueError("Round 29 BTC/ETH open_time 未完全对齐。")
    return {"asset_count": 2, "row_count_per_asset": len(reference), "timestamps_identical": True, "passed": True}


def _build_signals(rows: Sequence[Mapping[str, Any]], split: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    segment_rows = [item for item in rows if str(item["segment"]) == str(split["segment"])]
    by_time = {int(item["open_time"]): item for item in segment_rows}
    start_ms = int(split["start"].timestamp() * 1000)
    end_ms = int(split["end"].timestamp() * 1000)
    path = [item for item in segment_rows if start_ms <= int(item["open_time"]) <= end_ms]
    if len(path) != int(split["signal_rows"]):
        raise ValueError(f"Round 29 signal 行数不一致: {len(path)} != {split['signal_rows']}")
    signals: list[dict[str, Any]] = []
    for row in path:
        execution_time = int(row["open_time"])
        prior_times = [execution_time - offset * HOUR_MS for offset in range(FLOW_WINDOW_HOURS, 0, -1)]
        if any(value not in by_time for value in prior_times):
            raise ValueError("Round 29 signal 缺少 8 根因果小时。")
        imbalances = []
        for timestamp in prior_times:
            source = by_time[timestamp]
            quote = float(source["quote_volume"])
            if quote == 0:
                imbalance = 0.0
            else:
                imbalance = 2.0 * float(source["taker_buy_quote_volume"]) / quote - 1.0
            imbalances.append(imbalance)
        mean_imbalance = statistics.fmean(imbalances)
        target = 1 if mean_imbalance >= FLOW_THRESHOLD else (-1 if mean_imbalance <= -FLOW_THRESHOLD else 0)
        signals.append(
            {
                "execution_time": execution_time,
                "target_position": target,
                "mean_imbalance": mean_imbalance,
                "warmup_row_count": FLOW_WINDOW_HOURS,
                "latest_source_time": prior_times[-1],
                "zero_volume_warmup_count": sum(
                    float(by_time[timestamp]["quote_volume"]) == 0 for timestamp in prior_times
                ),
            }
        )
    canonical = json.dumps(signals, sort_keys=True, separators=(",", ":"))
    audit = {
        "signal_row_count": len(signals),
        "long_signal_count": sum(item["target_position"] == 1 for item in signals),
        "short_signal_count": sum(item["target_position"] == -1 for item in signals),
        "flat_signal_count": sum(item["target_position"] == 0 for item in signals),
        "warmup_row_count": FLOW_WINDOW_HOURS,
        "latest_source_precedes_execution": all(item["latest_source_time"] < item["execution_time"] for item in signals),
        "signal_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "future_data_used": False,
        "passed": True,
    }
    return signals, audit


def _performance(
    daily_equity: Sequence[tuple[date, float]],
    *,
    initial_capital: float,
    maximum_drawdown_pct: float,
) -> dict[str, Any]:
    daily_pnl = []
    daily_returns = []
    month_pnl: dict[str, float] = defaultdict(float)
    previous = initial_capital
    for day, equity in daily_equity:
        pnl = equity - previous
        daily_pnl.append(pnl)
        daily_returns.append(pnl / previous if previous != 0 else 0.0)
        month_pnl[day.strftime("%Y-%m")] += pnl
        previous = equity
    gains = sum(x for x in daily_pnl if x > 0)
    losses = -sum(x for x in daily_pnl if x < 0)
    pf = None if losses <= 0 else gains / losses
    stdev = statistics.stdev(daily_returns) if len(daily_returns) >= 2 else 0.0
    sharpe = statistics.fmean(daily_returns) / stdev * math.sqrt(365) if stdev > 0 else None
    positive_months = [x for x in month_pnl.values() if x > 0]
    positive_profit = sum(positive_months)
    return {
        "daily_observation_count": len(daily_equity),
        "daily_profit_factor": pf,
        "daily_annualized_sharpe": sharpe,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "calendar_month_count": len(month_pnl),
        "positive_calendar_month_ratio": len(positive_months) / len(month_pnl),
        "best_profitable_month_concentration": max(positive_months) / positive_profit if positive_profit > 0 else 1.0,
        "calendar_month_pnl": dict(sorted(month_pnl.items())),
    }


def _simulate(
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
    path = [item for item in rows if str(item["segment"]) == str(split["segment"]) and start_ms <= int(item["open_time"]) <= end_ms]
    expected_hours = (end_ms - start_ms) // HOUR_MS + 1
    if len(path) != expected_hours or any(int(current["open_time"]) - int(previous["open_time"]) != HOUR_MS for previous, current in zip(path, path[1:])):
        raise ValueError("Round 29 cell 小时路径不完整。")
    signal_by_time = {int(item["execution_time"]): item for item in signals}
    path_times = {int(item["open_time"]) for item in path}
    if len(signal_by_time) != len(signals) or not set(signal_by_time).issubset(path_times):
        raise ValueError("Round 29 signal 时间与 cell 路径不一致。")
    funding_by_hour: dict[int, Mapping[str, Any]] = {}
    offsets = []
    selected_funding = 0
    for event in funding_events:
        timestamp = int(event["funding_time"])
        offset = timestamp % HOUR_MS
        if offset >= 1_000:
            raise ValueError("Round 29 funding_time 偏离整点达到 1 秒。")
        hour = timestamp - offset
        if start_ms <= hour <= end_ms:
            selected_funding += 1
            if hour in funding_by_hour:
                raise ValueError("Round 29 funding event 映射碰撞。")
            funding_by_hour[hour] = event
            offsets.append(offset)
    if not set(funding_by_hour).issubset(path_times):
        raise ValueError("Round 29 funding event 无对应小时。")

    position = 0
    quantity = 0.0
    entry_price = 0.0
    cash = 0.0
    price_pnl = 0.0
    funding_pnl = 0.0
    costs = 0.0
    sides = 0
    execution_events: list[dict[str, Any]] = []
    flips = 0
    held_funding_events = 0
    daily_equity: list[tuple[date, float]] = []
    peak = initial_capital
    maximum_drawdown = 0.0

    def mark(price: float) -> float:
        return initial_capital + cash + (position * quantity * (price - entry_price) if position else 0.0)

    def observe(price: float) -> float:
        nonlocal peak, maximum_drawdown
        equity = mark(price)
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak - equity) / max(peak, 1e-12))
        return equity

    for row in path:
        timestamp = int(row["open_time"])
        open_price = float(row["open"])
        close_price = float(row["close"])
        event = funding_by_hour.get(timestamp)
        if event is not None and position:
            payment = -position * quantity * open_price * float(event["funding_rate"])
            cash += payment
            funding_pnl += payment
            held_funding_events += 1
        signal = signal_by_time.get(timestamp)
        if signal is not None:
            target = int(signal["target_position"])
            if target not in {-1, 0, 1}:
                raise ValueError("Round 29 signal target 无效。")
            if target != position:
                if position:
                    realized = position * quantity * (open_price - entry_price)
                    cash += realized
                    price_pnl += realized
                    close_cost = quantity * open_price * execution_cost_rate
                    cash -= close_cost
                    costs += close_cost
                    sides += 1
                    execution_events.append({"action": "CLOSE", "cost": close_cost})
                    flips += 1 if target else 0
                position = target
                if target:
                    quantity = initial_capital / open_price
                    entry_price = open_price
                    open_cost = quantity * open_price * execution_cost_rate
                    cash -= open_cost
                    costs += open_cost
                    sides += 1
                    execution_events.append({"action": "OPEN", "cost": open_cost})
                else:
                    quantity = 0.0
                    entry_price = 0.0
        observe(open_price)
        current = datetime.fromtimestamp(timestamp / 1000, tz=UTC)
        if timestamp == end_ms:
            if position:
                realized = position * quantity * (close_price - entry_price)
                cash += realized
                price_pnl += realized
                exit_cost = quantity * close_price * execution_cost_rate
                cash -= exit_cost
                costs += exit_cost
                sides += 1
                execution_events.append({"action": "FINAL_CLOSE", "cost": exit_cost})
                position = 0
                quantity = 0.0
                entry_price = 0.0
            equity = observe(close_price)
            daily_equity.append((current.date(), equity))
        elif current.hour == 23:
            daily_equity.append((current.date(), observe(close_price)))
    if position != 0 or len(daily_equity) != int(split["end"].date().toordinal() - split["start"].date().toordinal() + 1):
        raise RuntimeError("Round 29 最终持仓或日度权益数量不一致。")
    total_pnl = cash
    error = total_pnl - (price_pnl + funding_pnl - costs)
    if abs(error) > 1e-8:
        raise RuntimeError("Round 29 PnL 分解不闭合。")
    performance = _performance(daily_equity, initial_capital=initial_capital, maximum_drawdown_pct=maximum_drawdown)
    pf = performance["daily_profit_factor"]
    sharpe = performance["daily_annualized_sharpe"]
    checks = {
        "total_net_profit_strictly_positive": total_pnl > 0,
        "daily_profit_factor_gt_1": total_pnl > 0 if pf is None else pf > 1.0,
        "maximum_drawdown_le_20pct": maximum_drawdown <= 0.20,
        "daily_annualized_sharpe_gt_0_5": sharpe is not None and sharpe > 0.5,
        "positive_calendar_month_ratio_ge_50pct": performance["positive_calendar_month_ratio"] >= 0.50,
        "best_profitable_month_concentration_le_35pct": performance["best_profitable_month_concentration"] <= 0.35,
        "hourly_price_order_flow_coverage_100pct": len(path) == expected_hours,
        "funding_mapping_coverage_100pct": len(funding_by_hour) == selected_funding,
        "all_signals_causal_8_hour": all(int(item["warmup_row_count"]) == FLOW_WINDOW_HOURS and int(item["latest_source_time"]) < int(item["execution_time"]) for item in signals),
        "all_execution_sides_costed_and_flat": (
            position == 0
            and sides == len(execution_events)
            and all(
                math.isfinite(float(item["cost"])) and float(item["cost"]) > 0
                for item in execution_events
            )
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
            "execution_costs": costs,
            "execution_side_count": sides,
            "costed_execution_side_count": len(execution_events),
            "position_flip_count": flips,
            "held_funding_event_count": held_funding_events,
            "selected_funding_event_count": selected_funding,
            "funding_mapping_coverage_ratio": len(funding_by_hour) / selected_funding if selected_funding else 1.0,
            "funding_timestamp_normalized_count": sum(offset > 0 for offset in offsets),
            "maximum_funding_timestamp_offset_ms": max(offsets, default=0),
            "hourly_row_count": len(path),
            "expected_hourly_row_count": expected_hours,
            "hourly_price_order_flow_coverage_ratio": len(path) / expected_hours,
            "zero_volume_neutral_signal_count": sum(int(item["zero_volume_warmup_count"]) > 0 for item in signals),
            "pnl_decomposition_error": error,
            "final_position": position,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _read_funding(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    manifest, events = round22._read_funding_manifest(
        config["funding_manifest"],
        expected_sha256=str(config["funding_manifest_sha256"]),
        expected_symbol=str(config["symbol"]),
    )
    if manifest.get("file_sha256") != config["funding_csv_sha256"]:
        raise ValueError("Round 29 funding CSV 哈希不一致。")
    return events


def _evaluate(prices: Mapping[str, Sequence[Mapping[str, Any]]], funding: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    cells: dict[str, Any] = {}
    signal_audit: dict[str, Any] = {}
    signal_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for role, split in SPLITS.items():
        signal_audit[role] = {}
        for asset in ASSET_CONFIG:
            signal_cache[(asset, role)], signal_audit[role][asset] = _build_signals(prices[asset], split)
        for scenario, cost in SCENARIO_COST_RATES.items():
            symbols = {}
            for asset, config in ASSET_CONFIG.items():
                symbols[asset] = _simulate(
                    prices[asset],
                    funding[asset],
                    signal_cache[(asset, role)],
                    split=split,
                    initial_capital=float(config["capital"]),
                    execution_cost_rate=cost,
                )
            cells[f"{role}_{scenario}"] = {
                "role": role,
                "scenario": scenario,
                "flow_window_hours": FLOW_WINDOW_HOURS,
                "flow_threshold": FLOW_THRESHOLD,
                "execution_cost_rate": cost,
                "symbols": symbols,
            }
    return cells, signal_audit


def _summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [cell["symbols"][asset] for cell in cells.values() for asset in ASSET_CONFIG]
    return {
        "cell_symbol_count": len(selected),
        "passed_cell_symbol_count": sum(bool(item["passed"]) for item in selected),
        "all_cells_passed": all(bool(item["passed"]) for item in selected),
        "minimum_total_pnl": min(float(item["metrics"]["total_pnl"]) for item in selected),
        "minimum_daily_sharpe": min(float(item["metrics"]["daily_annualized_sharpe"]) for item in selected if item["metrics"]["daily_annualized_sharpe"] is not None),
        "maximum_drawdown_pct": max(float(item["metrics"]["maximum_drawdown_pct"]) for item in selected),
        "minimum_positive_calendar_month_ratio": min(float(item["metrics"]["positive_calendar_month_ratio"]) for item in selected),
    }


def _report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 29：小时主动买量不平衡结果",
        "",
        "过去 8 根完整小时的主动买量不平衡均值达到 ±15% 后顺势执行；完整计入真实 funding、主动费率与滑点。",
        "",
        "| 单元 | 资产 | 净收益 | 日 PF | Sharpe | 最大回撤 | 正收益月 | 价格 PnL | Funding | 成本 | 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for asset in ASSET_CONFIG:
            item = cell["symbols"][asset]
            m = item["metrics"]
            failed = ", ".join(name for name, passed in item["checks"].items() if not passed)
            lines.append(
                "| `{cell}` | {asset} | {pnl:.4f} | {pf} | {sharpe} | {dd:.2%} | {pm:.2%} | {price:.4f} | {fund:.4f} | {cost:.4f} | {passed} | {failed} |".format(
                    cell=cell_name,
                    asset=asset,
                    pnl=m["total_pnl"],
                    pf="∞" if m["daily_profit_factor"] is None and m["total_pnl"] > 0 else ("N/A" if m["daily_profit_factor"] is None else f"{m['daily_profit_factor']:.3f}"),
                    sharpe="N/A" if m["daily_annualized_sharpe"] is None else f"{m['daily_annualized_sharpe']:.3f}",
                    dd=m["maximum_drawdown_pct"],
                    pm=m["positive_calendar_month_ratio"],
                    price=m["price_pnl"],
                    fund=m["funding_pnl"],
                    cost=m["execution_costs"],
                    passed="是" if item["passed"] else "否",
                    failed=failed,
                )
            )
    lines.extend(["", f"通过单元：{payload['summary']['passed_cell_symbol_count']}/{payload['summary']['cell_symbol_count']}。", "", f"结论：{payload['conclusion']}", "", "CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="评估 Round 29 主动买量不平衡候选。")
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    args = parser.parse_args()
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 29 Phase A 协议哈希不一致。")
    round28_payload = _read_round28_result()
    prices: dict[str, list[dict[str, Any]]] = {}
    funding: dict[str, list[dict[str, Any]]] = {}
    manifests: dict[str, Any] = {}
    price_audit: dict[str, Any] = {}
    funding_manifest_hashes: dict[str, Any] = {}
    for asset, config in ASSET_CONFIG.items():
        manifest, prices[asset], price_audit[asset] = _read_price_manifest(
            config["price_manifest"],
            expected_manifest_sha256=str(config["price_manifest_sha256"]),
            expected_csv_sha256=str(config["price_csv_sha256"]),
            expected_symbol=str(config["symbol"]),
        )
        funding[asset] = _read_funding(config)
        manifests[asset] = {
            "path": str(config["price_manifest"].resolve()),
            "manifest_sha256": config["price_manifest_sha256"],
            "csv_sha256": manifest["file_sha256"],
        }
        funding_manifest_hashes[asset] = {
            "path": str(config["funding_manifest"].resolve()),
            "manifest_sha256": config["funding_manifest_sha256"],
            "csv_sha256": config["funding_csv_sha256"],
        }
    alignment = _assert_alignment(prices)
    cells, signal_audit = _evaluate(prices, funding)
    summary = _summary(cells)
    candidate_ready = bool(summary["all_cells_passed"])
    conclusion = (
        "ORDER_FLOW_WORTH_EXECUTION_PREREGISTRATION：12/12 个严格单元全部通过；只允许继续冻结真实盘口冲击和逐笔执行。"
        if candidate_ready
        else
        "NO_PREREGISTERED_ORDER_FLOW_CANDIDATE：至少一个严格单元失败，排除本协议定义的 8 小时、15%、1x 主动买量不平衡 family。"
    )
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "ROUND29_ORDER_FLOW_PHASE_A",
        "candidate_id": "ORDER_FLOW_IMBALANCE_8H_15PCT_1X_V1",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {
            "round28_result_sha256": ROUND28_RESULT_SHA256,
            "data_protocol_sha256": DATA_PROTOCOL_SHA256,
            "data_audit_sha256": DATA_AUDIT_SHA256,
        },
        "round28_conclusion": round28_payload["conclusion"],
        "price_manifests": manifests,
        "funding_manifests": funding_manifest_hashes,
        "price_data_audit": price_audit,
        "cross_asset_alignment_audit": alignment,
        "signal_audit": signal_audit,
        "direction_mode": "NEUTRAL",
        "flow_window_hours": FLOW_WINDOW_HOURS,
        "flow_threshold": FLOW_THRESHOLD,
        "leverage": 1.0,
        "execution_cost_rate_by_scenario": SCENARIO_COST_RATES,
        "cells": cells,
        "summary": summary,
        "formal_round29_execution_preregistration_ready": candidate_ready,
        "selected_candidate_id": "ORDER_FLOW_IMBALANCE_8H_15PCT_1X_V1" if candidate_ready else None,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    result_path = report_dir / "round29-order-flow-results.json"
    report_path = report_dir / "round29-order-flow-report.md"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_report(payload), encoding="utf-8")
    print(json.dumps({"result_path": str(result_path.resolve()), "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(), "report_path": str(report_path.resolve()), "passed_cell_symbol_count": summary["passed_cell_symbol_count"], "cell_symbol_count": summary["cell_symbol_count"], "formal_round29_execution_preregistration_ready": candidate_ready, "conclusion": conclusion}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
