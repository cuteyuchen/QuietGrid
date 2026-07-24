from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_funding_carry_upper_bound as round22
import scripts.cross_era_order_flow as round29
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
HOUR_MS = 60 * 60 * 1000
PROTOCOL_PATH = Path("reports/cross-era-oos/round31-funding-signal-phase-a-protocol.md")
PROTOCOL_SHA256 = "e5c321d5bd9031a02920b424789ef70df18fc4402ef9b3557509c4661f18346d"
DATA_AUDIT_PATH = Path("reports/cross-era-oos/round31-funding-signal-data-audit.md")
DATA_AUDIT_SHA256 = "37633fc74e6b8f45f78333f3b4d47612f9859652e4cb73ebc3b553aca35a8c94"
ROUND29_READER_SOURCE_SHA256 = "de202d483cc8430ebc5c02b7fab9e8dbd17bd047edc078f8decae26985765da6"
ROUND22_READER_SOURCE_SHA256 = "874736a38de1e6468f1589ea5df1cf91399272ed88fc0b65f2e4cf85c4f643d6"
ROUND29_DATA_PROTOCOL_SHA256 = "46f28027acbafbe687dc14de7fd57203f61e06872a29d71a02766f82c637784d"
ROUND29_DATA_AUDIT_SHA256 = "a63320dc95d9f7a98c05b762731be049691beaa0cca2dc85a9586df7b737fee8"
SCENARIO_COST_RATES = {"BASE": 0.0010, "COST50": 0.00175}
EXPECTED_ROWS_PER_ASSET = 43_056
EXPECTED_SEGMENT_ROWS = {"HISTORY": 26_280, "POSTHISTORY": 16_776}
SCREEN_DIRECTIONS = ("CONTRARIAN", "MOMENTUM")
SCREEN_THRESHOLDS = (0.0001, 0.0002, 0.0005, 0.001, 0.002)
SCREEN_HOLDS = (8, 24, 48, 72)
ENTRY_LAG_HOURS = 1
SELECTED_DIRECTION = "CONTRARIAN"
SELECTED_THRESHOLD = 0.0001
SELECTED_HOLD_HOURS = 72
SELECTED_CANDIDATE_ID = "FUNDING_RATE_CONTRARIAN_1BP_HOLD72_1X_V1"
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
        "price_manifest": Path("data/backtests/round29_order_flow/binance_um_order_flow_btcusdt_1h_202007_202306_202408_202606.manifest.json"),
        "price_manifest_sha256": "c350decb7e30bc32b60616441ea0790627dbab50f328fbbdbab026d3d0735cce",
        "price_csv_sha256": "de6a99cec32a980c7ddf40d69aaffd8e4633bf9570b03d355a192883b9a12d10",
        "funding_manifest": Path("data/backtests/round22_funding_carry/binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json"),
        "funding_manifest_sha256": "a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57",
        "funding_csv_sha256": "08a4fec97e9e2555d28135fc70f49d6115b966868fc912fc29faab80b722c5e2",
    },
    "ETH": {
        "capital": 300.0,
        "symbol": "ETHUSDT",
        "price_manifest": Path("data/backtests/round29_order_flow/binance_um_order_flow_ethusdt_1h_202007_202306_202408_202606.manifest.json"),
        "price_manifest_sha256": "3a4a1ec2cd2ea2a3f5db1a7a95c18a734a8613a1745dfde9e4d9ff2d22572067",
        "price_csv_sha256": "0de08f9129ef4d2227f6f66507795cd6de2d2961b17b6eeef6d07acb969ebef5",
        "funding_manifest": Path("data/backtests/round22_funding_carry/binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json"),
        "funding_manifest_sha256": "19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f",
        "funding_csv_sha256": "5ec93a6a3d7397fbe0e6d3b82c28873e11b2559b1be34c7d351b90e5d7b9108a",
    },
}


def _candidate_id(direction: str, threshold: float, hold_hours: int) -> str:
    prefix = "FUNDING_RATE_CONTRARIAN" if direction == "CONTRARIAN" else "FUNDING_RATE_MOMENTUM"
    bps = int(round(threshold * 10_000))
    return f"{prefix}_{bps}BP_HOLD{hold_hours}_1X_V1"


def _bounds(split: Mapping[str, Any]) -> tuple[int, int]:
    return int(split["start"].timestamp() * 1000), int(split["end"].timestamp() * 1000)


def _read_price_data(config: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if _sha256(Path(round29.__file__).resolve()) != ROUND29_READER_SOURCE_SHA256:
        raise ValueError("Round 31 Round 29 价格读取器源码哈希不一致。")
    manifest, rows, audit = round29._read_price_manifest(
        config["price_manifest"],
        expected_manifest_sha256=str(config["price_manifest_sha256"]),
        expected_csv_sha256=str(config["price_csv_sha256"]),
        expected_symbol=str(config["symbol"]),
    )
    if len(rows) != EXPECTED_ROWS_PER_ASSET or dict(Counter(item["segment"] for item in rows)) != EXPECTED_SEGMENT_ROWS:
        raise ValueError("Round 31 价格行数或 segment 不一致。")
    return manifest, rows, audit


def _read_funding_data(config: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if _sha256(Path(round22.__file__).resolve()) != ROUND22_READER_SOURCE_SHA256:
        raise ValueError("Round 31 Round 22 funding 读取器源码哈希不一致。")
    manifest, events = round22._read_funding_manifest(
        config["funding_manifest"],
        expected_sha256=str(config["funding_manifest_sha256"]),
        expected_symbol=str(config["symbol"]),
    )
    if manifest.get("file_sha256") != config["funding_csv_sha256"]:
        raise ValueError("Round 31 funding CSV 哈希不一致。")
    return manifest, events


def _assert_alignment(prices: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    btc_times = [int(item["open_time"]) for item in prices["BTC"]]
    if [int(item["open_time"]) for item in prices["ETH"]] != btc_times:
        raise ValueError("Round 31 BTC/ETH open_time 未对齐。")
    return {"asset_count": 2, "row_count_per_asset": len(btc_times), "timestamps_identical": True, "passed": True}


def _build_signals(
    funding_events: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    split: Mapping[str, Any],
    *,
    direction: str,
    threshold: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if direction not in SCREEN_DIRECTIONS or threshold <= 0:
        raise ValueError("Round 31 signal 参数无效。")
    start_ms, end_ms = _bounds(split)
    path_times = {
        int(item["open_time"])
        for item in rows
        if str(item["segment"]) == str(split["segment"])
        and start_ms <= int(item["open_time"]) <= end_ms
    }
    signals: list[dict[str, Any]] = []
    seen_execution_times: set[int] = set()
    normalized_event_count = 0
    for event in funding_events:
        funding_time = int(event["funding_time"])
        offset = funding_time % HOUR_MS
        if offset >= 1_000:
            raise ValueError("Round 31 funding_time 偏离整点达到 1 秒。")
        event_hour = funding_time - offset
        execution_time = event_hour + ENTRY_LAG_HOURS * HOUR_MS
        if execution_time not in path_times:
            continue
        normalized_event_count += 1
        if execution_time in seen_execution_times:
            raise ValueError("Round 31 funding signal execution_time 碰撞。")
        seen_execution_times.add(execution_time)
        rate = float(event["funding_rate"])
        if direction == "CONTRARIAN":
            target = -1 if rate >= threshold else (1 if rate <= -threshold else 0)
        else:
            target = 1 if rate >= threshold else (-1 if rate <= -threshold else 0)
        signals.append(
            {
                "execution_time": execution_time,
                "event_time": funding_time,
                "funding_rate": rate,
                "target_position": target,
                "event_to_execution_lag_hours": ENTRY_LAG_HOURS,
            }
        )
    signals.sort(key=lambda item: int(item["execution_time"]))
    canonical = json.dumps(signals, sort_keys=True, separators=(",", ":"))
    audit = {
        "normalized_event_count": normalized_event_count,
        "signal_row_count": len(signals),
        "long_signal_count": sum(item["target_position"] == 1 for item in signals),
        "short_signal_count": sum(item["target_position"] == -1 for item in signals),
        "flat_signal_count": sum(item["target_position"] == 0 for item in signals),
        "entry_lag_hours": ENTRY_LAG_HOURS,
        "all_events_precede_execution": all(int(item["event_time"]) < int(item["execution_time"]) for item in signals),
        "future_data_used": False,
        "signal_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "passed": True,
    }
    return signals, audit


def _performance(daily_equity: Sequence[tuple[date, float]], initial_capital: float, maximum_drawdown_pct: float) -> dict[str, Any]:
    daily_pnl: list[float] = []
    daily_returns: list[float] = []
    month_pnl: dict[str, float] = defaultdict(float)
    previous = initial_capital
    for day, equity in daily_equity:
        pnl = equity - previous
        daily_pnl.append(pnl)
        daily_returns.append(pnl / previous if previous else 0.0)
        month_pnl[day.strftime("%Y-%m")] += pnl
        previous = equity
    gains = sum(item for item in daily_pnl if item > 0)
    losses = -sum(item for item in daily_pnl if item < 0)
    pf = None if losses <= 0 else gains / losses
    stdev = statistics.stdev(daily_returns) if len(daily_returns) >= 2 else 0.0
    sharpe = statistics.fmean(daily_returns) / stdev * math.sqrt(365) if stdev > 0 else None
    positive_months = [item for item in month_pnl.values() if item > 0]
    positive_profit = sum(positive_months)
    return {
        "daily_observation_count": len(daily_equity),
        "daily_profit_factor": pf,
        "daily_annualized_sharpe": sharpe,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "calendar_month_count": len(month_pnl),
        "positive_calendar_month_ratio": len(positive_months) / len(month_pnl) if month_pnl else 0.0,
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
    hold_hours: int,
) -> dict[str, Any]:
    start_ms, end_ms = _bounds(split)
    path = [
        item for item in rows
        if str(item["segment"]) == str(split["segment"])
        and start_ms <= int(item["open_time"]) <= end_ms
    ]
    expected_hours = (end_ms - start_ms) // HOUR_MS + 1
    if len(path) != expected_hours or any(int(cur["open_time"]) - int(prev["open_time"]) != HOUR_MS for prev, cur in zip(path, path[1:])):
        raise ValueError("Round 31 cell 小时路径不完整。")
    signal_by_time = {int(item["execution_time"]): item for item in signals}
    if len(signal_by_time) != len(signals) or not set(signal_by_time).issubset({int(item["open_time"]) for item in path}):
        raise ValueError("Round 31 signal 时间与路径不一致。")
    funding_by_hour: dict[int, Mapping[str, Any]] = {}
    funding_offsets: list[int] = []
    selected_funding_count = 0
    for event in funding_events:
        funding_time = int(event["funding_time"])
        offset = funding_time % HOUR_MS
        if offset >= 1_000:
            raise ValueError("Round 31 funding_time 偏离整点达到 1 秒。")
        hour = funding_time - offset
        if start_ms <= hour <= end_ms:
            selected_funding_count += 1
            if hour in funding_by_hour:
                raise ValueError("Round 31 funding event 映射碰撞。")
            funding_by_hour[hour] = event
            funding_offsets.append(offset)
    path_times = {int(item["open_time"]) for item in path}
    if not set(funding_by_hour).issubset(path_times):
        raise ValueError("Round 31 funding event 无对应小时。")

    position = 0
    quantity = 0.0
    entry_price = 0.0
    scheduled_exit: int | None = None
    cash = price_pnl = funding_pnl = costs = 0.0
    execution_events: list[dict[str, Any]] = []
    completed_trade_count = 0
    scheduled_trade_count = 0
    final_truncated_trade_count = 0
    ignored_signal_count = 0
    held_funding_count = 0
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
        funding = funding_by_hour.get(timestamp)
        if funding is not None and position:
            payment = -position * quantity * open_price * float(funding["funding_rate"])
            cash += payment
            funding_pnl += payment
            held_funding_count += 1
        closed_this_hour = False
        if position and scheduled_exit == timestamp:
            realized = position * quantity * (open_price - entry_price)
            cash += realized
            price_pnl += realized
            close_cost = quantity * open_price * execution_cost_rate
            cash -= close_cost
            costs += close_cost
            execution_events.append({"action": "CLOSE", "cost": close_cost})
            position = 0
            quantity = 0.0
            entry_price = 0.0
            scheduled_exit = None
            completed_trade_count += 1
            scheduled_trade_count += 1
            closed_this_hour = True
        signal = signal_by_time.get(timestamp)
        if signal is not None and int(signal["target_position"]) not in {-1, 0, 1}:
            raise ValueError("Round 31 signal target 无效。")
        if signal is not None and int(signal["target_position"]) and position:
            ignored_signal_count += 1
        if signal is not None and int(signal["target_position"]) and not position and not closed_this_hour:
            target = int(signal["target_position"])
            position = target
            quantity = initial_capital / open_price
            entry_price = open_price
            scheduled_exit = timestamp + hold_hours * HOUR_MS
            open_cost = quantity * open_price * execution_cost_rate
            cash -= open_cost
            costs += open_cost
            execution_events.append({"action": "OPEN", "cost": open_cost})
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
                execution_events.append({"action": "FINAL_CLOSE", "cost": exit_cost})
                position = 0
                quantity = 0.0
                entry_price = 0.0
                scheduled_exit = None
                completed_trade_count += 1
                final_truncated_trade_count += 1
            daily_equity.append((current.date(), observe(close_price)))
        elif current.hour == 23:
            daily_equity.append((current.date(), observe(close_price)))
    expected_days = split["end"].date().toordinal() - split["start"].date().toordinal() + 1
    if position != 0 or len(daily_equity) != expected_days:
        raise RuntimeError("Round 31 最终持仓或日度权益数量不一致。")
    total_pnl = cash
    decomposition_error = total_pnl - (price_pnl + funding_pnl - costs)
    if abs(decomposition_error) > 1e-8:
        raise RuntimeError("Round 31 PnL 分解不闭合。")
    performance = _performance(daily_equity, initial_capital, maximum_drawdown)
    pf = performance["daily_profit_factor"]
    sharpe = performance["daily_annualized_sharpe"]
    checks = {
        "total_net_profit_strictly_positive": total_pnl > 0,
        "daily_profit_factor_gt_1": total_pnl > 0 if pf is None else pf > 1.0,
        "maximum_drawdown_le_20pct": maximum_drawdown <= 0.20,
        "daily_annualized_sharpe_gt_0_5": sharpe is not None and sharpe > 0.5,
        "positive_calendar_month_ratio_ge_50pct": performance["positive_calendar_month_ratio"] >= 0.50,
        "best_profitable_month_concentration_le_35pct": performance["best_profitable_month_concentration"] <= 0.35,
        "hourly_price_coverage_100pct": len(path) == expected_hours,
        "funding_mapping_coverage_100pct": len(funding_by_hour) == selected_funding_count,
        "all_signals_causal": all(int(item["event_time"]) < int(item["execution_time"]) for item in signals),
        "all_execution_sides_costed_and_flat": position == 0 and len(execution_events) == 2 * completed_trade_count and all(math.isfinite(float(item["cost"])) and float(item["cost"]) > 0 for item in execution_events),
        "pnl_decomposition_zero": abs(decomposition_error) <= 1e-8,
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
            "execution_side_count": len(execution_events),
            "costed_execution_side_count": len(execution_events),
            "completed_trade_count": completed_trade_count,
            "scheduled_trade_count": scheduled_trade_count,
            "final_truncated_trade_count": final_truncated_trade_count,
            "ignored_signal_count": ignored_signal_count,
            "held_funding_event_count": held_funding_count,
            "selected_funding_event_count": selected_funding_count,
            "funding_mapping_coverage_ratio": len(funding_by_hour) / selected_funding_count if selected_funding_count else 1.0,
            "funding_timestamp_normalized_count": sum(offset > 0 for offset in funding_offsets),
            "maximum_funding_timestamp_offset_ms": max(funding_offsets, default=0),
            "hourly_row_count": len(path),
            "expected_hourly_row_count": expected_hours,
            "hourly_price_coverage_ratio": len(path) / expected_hours,
            "pnl_decomposition_error": decomposition_error,
            "final_position": position,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _screen_development(prices: Mapping[str, Sequence[Mapping[str, Any]]], funding: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    split = SPLITS["DEVELOPMENT"]
    candidates: list[dict[str, Any]] = []
    for direction in SCREEN_DIRECTIONS:
        for threshold in SCREEN_THRESHOLDS:
            for hold in SCREEN_HOLDS:
                candidate_id = _candidate_id(direction, threshold, hold)
                symbols: dict[str, Any] = {}
                for asset, config in ASSET_CONFIG.items():
                    signals, _ = _build_signals(funding[asset], prices[asset], split, direction=direction, threshold=threshold)
                    symbols[asset] = _simulate(prices[asset], funding[asset], signals, split=split, initial_capital=float(config["capital"]), execution_cost_rate=SCENARIO_COST_RATES["COST50"], hold_hours=hold)
                metrics = {asset: symbols[asset]["metrics"] for asset in ASSET_CONFIG}
                eligible = all(float(metrics[asset]["total_pnl"]) > 0 for asset in ASSET_CONFIG)
                candidates.append({
                    "candidate_id": candidate_id,
                    "direction": direction,
                    "threshold": threshold,
                    "hold_hours": hold,
                    "eligible_positive_pnl_both_assets": eligible,
                    "minimum_cost50_pnl": min(float(metrics[asset]["total_pnl"]) for asset in ASSET_CONFIG),
                    "minimum_cost50_sharpe": min(float(metrics[asset]["daily_annualized_sharpe"]) if metrics[asset]["daily_annualized_sharpe"] is not None else float("-inf") for asset in ASSET_CONFIG),
                    "minimum_cost50_positive_month_ratio": min(float(metrics[asset]["positive_calendar_month_ratio"]) for asset in ASSET_CONFIG),
                    "symbols": symbols,
                })
    eligible = [item for item in candidates if item["eligible_positive_pnl_both_assets"]]
    if not eligible:
        raise RuntimeError("Round 31 Development screening 没有双资产正收益候选。")
    ordered = sorted(eligible, key=lambda item: (-float(item["minimum_cost50_pnl"]), -float(item["minimum_cost50_sharpe"]), -float(item["minimum_cost50_positive_month_ratio"]), str(item["candidate_id"])))
    if ordered[0]["candidate_id"] != SELECTED_CANDIDATE_ID:
        raise RuntimeError(f"Round 31 Development 选择结果改变: {ordered[0]['candidate_id']} != {SELECTED_CANDIDATE_ID}")
    return {
        "candidate_count": len(candidates),
        "eligible_positive_pnl_both_assets_count": len(eligible),
        "selection_rule": ["minimum_cost50_pnl_desc", "minimum_cost50_sharpe_desc", "minimum_cost50_positive_month_ratio_desc", "candidate_id_asc"],
        "selected_candidate": ordered[0],
        "top_candidates": ordered[:10],
    }


def _evaluate(prices: Mapping[str, Sequence[Mapping[str, Any]]], funding: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    cells: dict[str, Any] = {}
    audits: dict[str, Any] = {}
    for role, split in SPLITS.items():
        audits[role] = {}
        for asset, config in ASSET_CONFIG.items():
            signals, audit = _build_signals(funding[asset], prices[asset], split, direction=SELECTED_DIRECTION, threshold=SELECTED_THRESHOLD)
            audits[role][asset] = audit
            for scenario, cost in SCENARIO_COST_RATES.items():
                cells[f"{role}_{scenario}_{asset}"] = {
                    "role": role,
                    "scenario": scenario,
                    "asset": asset,
                    "direction": SELECTED_DIRECTION,
                    "threshold": SELECTED_THRESHOLD,
                    "hold_hours": SELECTED_HOLD_HOURS,
                    "entry_lag_hours": ENTRY_LAG_HOURS,
                    "execution_cost_rate": cost,
                    **_simulate(prices[asset], funding[asset], signals, split=split, initial_capital=float(config["capital"]), execution_cost_rate=cost, hold_hours=SELECTED_HOLD_HOURS),
                }
    return cells, audits


def _summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    values = list(cells.values())
    sharpes = [float(item["metrics"]["daily_annualized_sharpe"]) for item in values if item["metrics"]["daily_annualized_sharpe"] is not None]
    return {
        "cell_count": len(values),
        "passed_cell_count": sum(bool(item["passed"]) for item in values),
        "all_cells_passed": all(bool(item["passed"]) for item in values),
        "minimum_total_pnl": min(float(item["metrics"]["total_pnl"]) for item in values),
        "minimum_daily_sharpe": min(sharpes) if sharpes else None,
        "maximum_drawdown_pct": max(float(item["metrics"]["maximum_drawdown_pct"]) for item in values),
        "minimum_positive_calendar_month_ratio": min(float(item["metrics"]["positive_calendar_month_ratio"]) for item in values),
    }


def _report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 31：因果 Funding Rate 方向信号结果",
        "",
        "Funding event 发生后等待 1 小时；正费率反向做空、负费率反向做多，固定持有 72 小时，完整计入真实 funding 与主动成本。",
        "",
        "| 单元 | 净收益 | 日 PF | Sharpe | 最大回撤 | 正收益月 | 交易数 | 价格 PnL | Funding | 成本 | 通过 | 失败检查 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for name, cell in payload["cells"].items():
        m = cell["metrics"]
        failed = ", ".join(key for key, passed in cell["checks"].items() if not passed)
        pf = m["daily_profit_factor"]
        sharpe = m["daily_annualized_sharpe"]
        lines.append(
            "| `{name}` | {pnl:.4f} | {pf} | {sharpe} | {dd:.2%} | {months:.2%} | {trades} | {price:.4f} | {funding:.4f} | {cost:.4f} | {passed} | {failed} |".format(
                name=name,
                pnl=m["total_pnl"],
                pf="∞" if pf is None and m["total_pnl"] > 0 else ("N/A" if pf is None else f"{pf:.3f}"),
                sharpe="N/A" if sharpe is None else f"{sharpe:.3f}",
                dd=m["maximum_drawdown_pct"],
                months=m["positive_calendar_month_ratio"],
                trades=m["completed_trade_count"],
                price=m["price_pnl"],
                funding=m["funding_pnl"],
                cost=m["execution_costs"],
                passed="是" if cell["passed"] else "否",
                failed=failed,
            )
        )
    lines.extend(["", f"通过单元：{payload['summary']['passed_cell_count']}/{payload['summary']['cell_count']}。", "", f"结论：{payload['conclusion']}", "", "CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="评估 Round 31 因果 funding-rate 方向信号。")
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    args = parser.parse_args()
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 31 Phase A 协议哈希不一致。")
    if _sha256(DATA_AUDIT_PATH.resolve()) != DATA_AUDIT_SHA256:
        raise ValueError("Round 31 数据审计哈希不一致。")
    prices: dict[str, list[dict[str, Any]]] = {}
    funding: dict[str, list[dict[str, Any]]] = {}
    price_manifests: dict[str, Any] = {}
    funding_manifests: dict[str, Any] = {}
    price_audit: dict[str, Any] = {}
    for asset, config in ASSET_CONFIG.items():
        manifest, rows, audit = _read_price_data(config)
        funding_manifest, events = _read_funding_data(config)
        prices[asset] = rows
        funding[asset] = events
        price_audit[asset] = audit
        price_manifests[asset] = {"path": str(config["price_manifest"].resolve()), "manifest_sha256": config["price_manifest_sha256"], "csv_sha256": manifest["file_sha256"]}
        funding_manifests[asset] = {"path": str(config["funding_manifest"].resolve()), "manifest_sha256": config["funding_manifest_sha256"], "csv_sha256": funding_manifest["file_sha256"]}
    alignment = _assert_alignment(prices)
    development_screen = _screen_development(prices, funding)
    cells, signal_audit = _evaluate(prices, funding)
    summary = _summary(cells)
    candidate_ready = bool(summary["all_cells_passed"])
    conclusion = "FUNDING_SIGNAL_WORTH_EXECUTION_PREREGISTRATION：12/12 个严格单元全部通过；只允许继续冻结真实执行细节和独立稳健性。" if candidate_ready else "NO_PREREGISTERED_FUNDING_SIGNAL_CANDIDATE：至少一个严格单元失败，排除本协议定义的因果 funding-rate 方向信号 family。"
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "ROUND31_FUNDING_SIGNAL_PHASE_A",
        "candidate_id": SELECTED_CANDIDATE_ID,
        "protocol_sha256": PROTOCOL_SHA256,
        "data_audit_sha256": DATA_AUDIT_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {
            "round29_data_protocol_sha256": ROUND29_DATA_PROTOCOL_SHA256,
            "round29_data_audit_sha256": ROUND29_DATA_AUDIT_SHA256,
            "round29_reader_source_sha256": ROUND29_READER_SOURCE_SHA256,
            "round22_funding_reader_source_sha256": ROUND22_READER_SOURCE_SHA256,
        },
        "price_manifests": price_manifests,
        "funding_manifests": funding_manifests,
        "price_data_audit": price_audit,
        "cross_asset_alignment_audit": alignment,
        "development_screen": development_screen,
        "signal_audit": signal_audit,
        "direction_mode": "NEUTRAL",
        "direction": SELECTED_DIRECTION,
        "threshold": SELECTED_THRESHOLD,
        "hold_hours": SELECTED_HOLD_HOURS,
        "entry_lag_hours": ENTRY_LAG_HOURS,
        "leverage": 1.0,
        "execution_cost_rate_by_scenario": SCENARIO_COST_RATES,
        "cells": cells,
        "summary": summary,
        "formal_round31_execution_preregistration_ready": candidate_ready,
        "selected_candidate_id": SELECTED_CANDIDATE_ID if candidate_ready else None,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    result_path = report_dir / "round31-funding-signal-results.json"
    report_path = report_dir / "round31-funding-signal-report.md"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_report(payload), encoding="utf-8")
    print(json.dumps({"result_path": str(result_path.resolve()), "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(), "report_path": str(report_path.resolve()), "passed_cell_count": summary["passed_cell_count"], "cell_count": summary["cell_count"], "formal_round31_execution_preregistration_ready": candidate_ready, "conclusion": conclusion}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
