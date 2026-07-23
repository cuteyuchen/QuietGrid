from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_funding_carry_upper_bound as round22
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round26-quarterly-calendar-spread-phase-a-protocol.md"
)
PROTOCOL_SHA256 = "351d483c5cbb997acb26842ddd87269869ad8a69d13919885b54450f8df25356"
DATA_PROTOCOL_SHA256 = "8ff68ba3c453ea89caad8ffe7444a0e9d9a79fae5d8db9f9b7fb75eedb6b8cdf"
DATA_AUDIT_SHA256 = "823b62ad7710669cee4be794067d73c7d1fbed22551e2f0c1ac24c8f167282bf"
ROUND25_RESULT_PATH = Path(
    "reports/cross-era-oos/round25-cross-venue-funding-spread-upper-bound-results.json"
)
ROUND25_RESULT_SHA256 = "5177f0137714cf26574da31a8ad1c3bc48776789f88edac46a37f943bb8c0eda"
EXPECTED_WINDOW_COUNTS = {"DEVELOPMENT": 67, "VALIDATION": 48, "POSTHISTORY": 91}
EXPECTED_WINDOW_COUNT = 206
EXPECTED_ROWS_PER_WINDOW = 169
EXPECTED_ROWS_PER_ASSET = EXPECTED_WINDOW_COUNT * EXPECTED_ROWS_PER_WINDOW
EXPECTED_SOURCE_ARCHIVES = 120
SCENARIO_FEE_RATES = {"BASE": 0.0002, "COST50": 0.0003}
EXCLUDED_START = datetime(2023, 7, 1, tzinfo=UTC)
EXCLUDED_END = datetime(2024, 8, 1, tzinfo=UTC)
CSV_HEADER = (
    "role",
    "window_id",
    "window_start",
    "window_end",
    "open_time",
    "perpetual_symbol",
    "quarterly_symbol",
    "perpetual_open",
    "perpetual_high",
    "perpetual_low",
    "perpetual_close",
    "quarterly_open",
    "quarterly_high",
    "quarterly_low",
    "quarterly_close",
    "perpetual_source_month",
    "perpetual_source_zip_sha256",
    "quarterly_source_month",
    "quarterly_source_zip_sha256",
)
ASSET_CONFIG = {
    "BTC": {
        "perpetual_symbol": "BTCUSDT",
        "gross_capital": 500.0,
        "price_manifest": Path(
            "data/backtests/round26_quarterly_calendar_spread/"
            "binance_um_quarterly_calendar_spread_btc_1h_202102_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": (
            "6488e406eb0515b342cf51131f891777ceb494651c7230c727b89ad1cba376e7"
        ),
        "price_csv_sha256": (
            "116ab837c219aba75adf379473a578306f83646cb984cac9632a864c888cc5c3"
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
        "perpetual_symbol": "ETHUSDT",
        "gross_capital": 300.0,
        "price_manifest": Path(
            "data/backtests/round26_quarterly_calendar_spread/"
            "binance_um_quarterly_calendar_spread_eth_1h_202102_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": (
            "0b984e9d45c5cc0c906c0ec37c15246fd92b7e105545fb557ef080be07ea781f"
        ),
        "price_csv_sha256": (
            "9010f93a83e01040429153b62d59852d74af264e6daadbf3a4b8cab3449ed2f1"
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


def _parse_utc(value: Any, *, label: str) -> datetime:
    raw = str(value)
    if not raw.endswith("Z") and "+00:00" not in raw:
        raise ValueError(f"{label} 必须为 UTC。")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} 不是合法 ISO-8601 时间。") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} 缺少时区。")
    return parsed.astimezone(UTC)


def _read_price_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_csv_sha256: str,
    expected_asset: str,
    expected_perpetual_symbol: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    path = manifest_path.resolve()
    if _sha256(path) != expected_manifest_sha256:
        raise ValueError(f"{expected_asset} Round 26 price manifest 哈希不一致。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = {
        "data_protocol_sha256": DATA_PROTOCOL_SHA256,
        "availability_audit_sha256": DATA_AUDIT_SHA256,
        "provider": "binance_data_vision",
        "market": "USDS_M",
        "data_type": "klines",
        "interval": "1h",
        "asset": expected_asset,
        "perpetual_symbol": expected_perpetual_symbol,
        "file_sha256": expected_csv_sha256,
        "row_count": EXPECTED_ROWS_PER_ASSET,
        "window_count": EXPECTED_WINDOW_COUNT,
        "rows_per_window": EXPECTED_ROWS_PER_WINDOW,
        "duplicate_primary_keys": 0,
        "source_archive_count": EXPECTED_SOURCE_ARCHIVES,
        "official_checksums_verified": True,
        "authorized_windows_complete": True,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "initial_week_count": 224,
        "excluded_roll_window_count": 18,
    }
    for key, expected in expected_fields.items():
        if manifest.get(key) != expected:
            raise ValueError(
                f"{expected_asset} Round 26 manifest 字段 {key} 不一致。"
            )
    excluded_roll = list(manifest.get("excluded_roll_windows") or [])
    if len(excluded_roll) != 18 or any(
        str(item.get("reason")) != "QUARTERLY_CONTRACT_EXPIRY_WEEK"
        for item in excluded_roll
    ):
        raise ValueError(f"{expected_asset} Round 26 交割周排除审计失败。")

    archives = list(manifest.get("source_archives") or [])
    if len(archives) != EXPECTED_SOURCE_ARCHIVES:
        raise ValueError(f"{expected_asset} Round 26 source archive 数量不一致。")
    archive_sha_by_key: dict[tuple[str, str], str] = {}
    invalid_source_rows = 0
    for archive in archives:
        key = (str(archive["symbol"]), str(archive["month"]))
        if key in archive_sha_by_key:
            raise ValueError(f"{expected_asset} Round 26 source archive 重复。")
        source_sha = str(archive["zip_sha256"])
        if len(source_sha) != 64:
            raise ValueError(f"{expected_asset} Round 26 source SHA 无效。")
        if not bool(archive.get("official_checksum_verified")):
            raise ValueError(f"{expected_asset} Round 26 source checksum 未通过。")
        invalid_source_rows += int(archive.get("invalid_ohlc_row_count", 0))
        archive_sha_by_key[key] = source_sha

    data_path = path.parent / str(manifest["file_name"])
    if hashlib.sha256(data_path.read_bytes()).hexdigest() != expected_csv_sha256:
        raise ValueError(f"{expected_asset} Round 26 price CSV 哈希不一致。")
    grouped: dict[str, list[dict[str, Any]]] = {}
    primary_keys: set[tuple[str, int]] = set()
    with data_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_HEADER:
            raise ValueError(f"{expected_asset} Round 26 price CSV 表头不一致。")
        for line_number, raw in enumerate(reader, start=2):
            try:
                role = str(raw["role"])
                window_id = str(raw["window_id"])
                window_start = _parse_utc(raw["window_start"], label="window_start")
                window_end = _parse_utc(raw["window_end"], label="window_end")
                open_time = int(raw["open_time"])
                perpetual_symbol = str(raw["perpetual_symbol"])
                quarterly_symbol = str(raw["quarterly_symbol"])
                perpetual_prices = tuple(
                    float(raw[f"perpetual_{name}"])
                    for name in ("open", "high", "low", "close")
                )
                quarterly_prices = tuple(
                    float(raw[f"quarterly_{name}"])
                    for name in ("open", "high", "low", "close")
                )
                perpetual_month = str(raw["perpetual_source_month"])
                perpetual_sha = str(raw["perpetual_source_zip_sha256"])
                quarterly_month = str(raw["quarterly_source_month"])
                quarterly_sha = str(raw["quarterly_source_zip_sha256"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"{expected_asset} Round 26 price CSV 第 {line_number} 行无效。"
                ) from exc
            if role not in EXPECTED_WINDOW_COUNTS:
                raise ValueError(f"{expected_asset} Round 26 role 无效。")
            if perpetual_symbol != expected_perpetual_symbol:
                raise ValueError(f"{expected_asset} Round 26 永续标的不一致。")
            if not quarterly_symbol.startswith(f"{expected_asset}USDT_"):
                raise ValueError(f"{expected_asset} Round 26 季度标的不一致。")
            if window_end - window_start != timedelta(days=7):
                raise ValueError(f"{expected_asset} Round 26 窗口长度不是 7 天。")
            timestamp = datetime.fromtimestamp(open_time / 1000, tz=UTC)
            if EXCLUDED_START <= timestamp < EXCLUDED_END:
                raise ValueError(f"{expected_asset} Round 26 CSV 触碰隔离区间。")
            expected_month = timestamp.strftime("%Y-%m")
            if perpetual_month != expected_month or quarterly_month != expected_month:
                raise ValueError(f"{expected_asset} Round 26 source month 不一致。")
            if archive_sha_by_key.get((perpetual_symbol, perpetual_month)) != perpetual_sha:
                raise ValueError(f"{expected_asset} Round 26 永续行级 source SHA 不一致。")
            if archive_sha_by_key.get((quarterly_symbol, quarterly_month)) != quarterly_sha:
                raise ValueError(f"{expected_asset} Round 26 季度行级 source SHA 不一致。")
            for prices in (perpetual_prices, quarterly_prices):
                open_price, high, low, close = prices
                if any(not math.isfinite(value) or value <= 0 for value in prices):
                    raise ValueError(f"{expected_asset} Round 26 CSV 价格无效。")
                if high < max(open_price, close) or low > min(open_price, close) or high < low:
                    raise ValueError(f"{expected_asset} Round 26 CSV OHLC 关系无效。")
            key = (window_id, open_time)
            if key in primary_keys:
                raise ValueError(f"{expected_asset} Round 26 CSV 主键重复。")
            primary_keys.add(key)
            grouped.setdefault(window_id, []).append(
                {
                    "role": role,
                    "window_id": window_id,
                    "window_start": window_start,
                    "window_end": window_end,
                    "open_time": open_time,
                    "perpetual_symbol": perpetual_symbol,
                    "quarterly_symbol": quarterly_symbol,
                    "perpetual_open": perpetual_prices[0],
                    "quarterly_open": quarterly_prices[0],
                }
            )
    if len(primary_keys) != EXPECTED_ROWS_PER_ASSET:
        raise ValueError(f"{expected_asset} Round 26 CSV 行数不一致。")

    manifest_windows = list(manifest.get("windows") or [])
    if len(manifest_windows) != EXPECTED_WINDOW_COUNT:
        raise ValueError(f"{expected_asset} Round 26 manifest 窗口数不一致。")
    manifest_by_id = {str(item["window_id"]): item for item in manifest_windows}
    if len(manifest_by_id) != EXPECTED_WINDOW_COUNT or set(manifest_by_id) != set(grouped):
        raise ValueError(f"{expected_asset} Round 26 manifest/CSV 窗口集合不一致。")

    windows: list[dict[str, Any]] = []
    role_counts: Counter[str] = Counter()
    for window_id, rows in grouped.items():
        rows.sort(key=lambda item: int(item["open_time"]))
        if len(rows) != EXPECTED_ROWS_PER_WINDOW:
            raise ValueError(f"{expected_asset} {window_id} 行数不一致。")
        times = [int(item["open_time"]) for item in rows]
        if any(
            current - previous != 60 * 60 * 1000
            for previous, current in zip(times, times[1:])
        ):
            raise ValueError(f"{expected_asset} {window_id} 小时路径不连续。")
        first = rows[0]
        if times[0] != int(first["window_start"].timestamp() * 1000):
            raise ValueError(f"{expected_asset} {window_id} 首时间不一致。")
        if times[-1] != int(first["window_end"].timestamp() * 1000):
            raise ValueError(f"{expected_asset} {window_id} 末时间不一致。")
        if any(
            item["role"] != first["role"]
            or item["window_start"] != first["window_start"]
            or item["window_end"] != first["window_end"]
            or item["quarterly_symbol"] != first["quarterly_symbol"]
            for item in rows
        ):
            raise ValueError(f"{expected_asset} {window_id} 行内元数据不一致。")
        manifest_window = manifest_by_id[window_id]
        expected_signature = (
            str(manifest_window["role"]),
            _parse_utc(manifest_window["window_start"], label="manifest window_start"),
            _parse_utc(manifest_window["window_end"], label="manifest window_end"),
            str(manifest_window["quarterly_symbol"]),
            int(manifest_window["row_count"]),
            int(manifest_window["first_open_time"]),
            int(manifest_window["last_open_time"]),
        )
        actual_signature = (
            str(first["role"]),
            first["window_start"],
            first["window_end"],
            str(first["quarterly_symbol"]),
            len(rows),
            times[0],
            times[-1],
        )
        if actual_signature != expected_signature:
            raise ValueError(f"{expected_asset} {window_id} manifest 窗口审计失败。")
        role_counts[str(first["role"])] += 1
        windows.append(
            {
                "role": first["role"],
                "window_id": window_id,
                "window_start": first["window_start"],
                "window_end": first["window_end"],
                "perpetual_symbol": first["perpetual_symbol"],
                "quarterly_symbol": first["quarterly_symbol"],
                "rows": rows,
            }
        )
    if dict(role_counts) != EXPECTED_WINDOW_COUNTS:
        raise ValueError(
            f"{expected_asset} Round 26 role 窗口数不一致: {dict(role_counts)}"
        )
    windows.sort(key=lambda item: item["window_start"])
    return manifest, windows, {
        "row_count": len(primary_keys),
        "window_count": len(windows),
        "role_counts": dict(role_counts),
        "source_archive_count": len(archives),
        "invalid_non_authorized_source_row_count": invalid_source_rows,
        "aligned_hourly_paths": True,
        "row_level_source_hashes_match": True,
        "excluded_interval_untouched": True,
        "passed": True,
    }


def _read_funding_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_csv_sha256: str,
    expected_symbol: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest, events = round22._read_funding_manifest(
        manifest_path,
        expected_sha256=expected_manifest_sha256,
        expected_symbol=expected_symbol,
    )
    if manifest.get("file_sha256") != expected_csv_sha256:
        raise ValueError(f"{expected_symbol} funding CSV 固定哈希不一致。")
    if any(
        int(item["funding_interval_hours"]) != 8
        or not math.isfinite(float(item["funding_rate"]))
        for item in events
    ):
        raise ValueError(f"{expected_symbol} funding interval/rate 审计失败。")
    return manifest, events


def _assert_asset_windows_aligned(
    price_windows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    btc = price_windows["BTC"]
    eth = price_windows["ETH"]
    if len(btc) != len(eth):
        raise ValueError("Round 26 BTC/ETH 窗口数量不一致。")
    for btc_window, eth_window in zip(btc, eth):
        btc_suffix = str(btc_window["quarterly_symbol"]).split("_", 1)[1]
        eth_suffix = str(eth_window["quarterly_symbol"]).split("_", 1)[1]
        if (
            btc_window["role"] != eth_window["role"]
            or btc_window["window_id"] != eth_window["window_id"]
            or btc_window["window_start"] != eth_window["window_start"]
            or btc_window["window_end"] != eth_window["window_end"]
            or btc_suffix != eth_suffix
            or [item["open_time"] for item in btc_window["rows"]]
            != [item["open_time"] for item in eth_window["rows"]]
        ):
            raise ValueError("Round 26 BTC/ETH 窗口、合约日期或小时路径不一致。")
    return {
        "window_count": len(btc),
        "same_window_ids": True,
        "same_contract_dates": True,
        "same_hourly_timestamps": True,
        "passed": True,
    }


def _events_by_window(
    events: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    times = [int(item["funding_time"]) for item in events]
    assigned: dict[str, list[dict[str, Any]]] = {}
    used: set[int] = set()
    for window in windows:
        start_ms = int(window["window_start"].timestamp() * 1000)
        end_ms = int(window["window_end"].timestamp() * 1000)
        left = bisect.bisect_left(times, start_ms)
        right = bisect.bisect_left(times, end_ms)
        selected = [dict(item) for item in events[left:right]]
        if not selected:
            raise ValueError(f"{window['window_id']} 没有 funding event。")
        for item in selected:
            funding_time = int(item["funding_time"])
            if funding_time in used:
                raise ValueError("Round 26 funding event 被分配到多个窗口。")
            used.add(funding_time)
        assigned[str(window["window_id"])] = selected
    return assigned, {
        "window_count": len(windows),
        "covered_window_count": len(assigned),
        "window_coverage_ratio": len(assigned) / len(windows),
        "assigned_event_count": len(used),
        "minimum_events_per_window": min(len(item) for item in assigned.values()),
        "maximum_events_per_window": max(len(item) for item in assigned.values()),
        "events_assigned_once": True,
        "passed": len(assigned) == len(windows),
    }


def _window_result(
    window: Mapping[str, Any],
    funding_events: Sequence[Mapping[str, Any]],
    *,
    asset: str,
    gross_capital: float,
    maker_fee_rate: float,
) -> dict[str, Any]:
    rows = list(window["rows"])
    if len(rows) != EXPECTED_ROWS_PER_WINDOW:
        raise ValueError("Round 26 window 不是 169 小时路径。")
    entry = rows[0]
    exit_row = rows[-1]
    perpetual_entry = float(entry["perpetual_open"])
    quarterly_entry = float(entry["quarterly_open"])
    basis_entry = quarterly_entry - perpetual_entry
    position_sign = 1.0 if basis_entry >= 0 else -1.0
    direction = (
        "LONG_PERPETUAL_SHORT_QUARTERLY"
        if position_sign > 0
        else "SHORT_PERPETUAL_LONG_QUARTERLY"
    )
    quantity = gross_capital / (perpetual_entry + quarterly_entry)
    entry_fee = maker_fee_rate * quantity * (perpetual_entry + quarterly_entry)

    funding_by_time: dict[int, Mapping[str, Any]] = {}
    funding_offsets: list[int] = []
    for event in funding_events:
        funding_time = int(event["funding_time"])
        offset = funding_time % (60 * 60 * 1000)
        if offset >= 1_000:
            raise ValueError(f"{window['window_id']} funding_time 偏离整点达到 1 秒。")
        funding_hour = funding_time - offset
        if funding_hour in funding_by_time:
            raise ValueError(f"{window['window_id']} 多个 funding event 映射到同一小时。")
        funding_by_time[funding_hour] = event
        funding_offsets.append(offset)
    row_times = {int(row["open_time"]) for row in rows}
    if not set(funding_by_time).issubset(row_times):
        raise ValueError(f"{window['window_id']} funding event 无对应 1h open。")

    cumulative_funding = 0.0
    path = [-entry_fee]
    for row in rows:
        open_time = int(row["open_time"])
        perpetual_open = float(row["perpetual_open"])
        quarterly_open = float(row["quarterly_open"])
        event = funding_by_time.get(open_time)
        if event is not None:
            cumulative_funding += (
                -position_sign
                * quantity
                * perpetual_open
                * float(event["funding_rate"])
            )
        price_pnl = position_sign * quantity * (
            (perpetual_open - perpetual_entry)
            - (quarterly_open - quarterly_entry)
        )
        path.append(price_pnl + cumulative_funding - entry_fee)

    perpetual_exit = float(exit_row["perpetual_open"])
    quarterly_exit = float(exit_row["quarterly_open"])
    price_pnl = position_sign * quantity * (
        (perpetual_exit - perpetual_entry)
        - (quarterly_exit - quarterly_entry)
    )
    exit_fee = maker_fee_rate * quantity * (perpetual_exit + quarterly_exit)
    net_pnl = price_pnl + cumulative_funding - entry_fee - exit_fee
    path.append(net_pnl)
    return {
        "role": window["role"],
        "window_id": window["window_id"],
        "window_start": window["window_start"].isoformat(),
        "window_end": window["window_end"].isoformat(),
        "asset": asset,
        "perpetual_symbol": window["perpetual_symbol"],
        "quarterly_symbol": window["quarterly_symbol"],
        "gross_capital": gross_capital,
        "quantity": quantity,
        "hourly_row_count": len(rows),
        "funding_event_count": len(funding_events),
        "funding_timestamp_normalized_count": sum(offset > 0 for offset in funding_offsets),
        "maximum_funding_timestamp_offset_ms": max(funding_offsets, default=0),
        "basis_entry": basis_entry,
        "oracle_direction": False,
        "direction_is_causal": True,
        "direction": direction,
        "position_sign": int(position_sign),
        "price_pnl": price_pnl,
        "funding_pnl": cumulative_funding,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees_paid": entry_fee + exit_fee,
        "net_pnl": net_pnl,
        "minimum_path_pnl": min(path),
        "maximum_path_pnl": max(path),
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
        raise ValueError("Round 26 cell 没有窗口。")
    ordered = sorted(results, key=lambda item: str(item["window_start"]))
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
    complete_price_windows = sum(
        int(item["hourly_row_count"]) == EXPECTED_ROWS_PER_WINDOW for item in ordered
    )
    funding_covered = sum(int(item["funding_event_count"]) > 0 for item in ordered)
    complete_round_trips = sum(
        int(item["round_trip_count"]) == 1
        and int(item["leg_count"]) == 2
        and bool(item["direction_is_causal"])
        and not bool(item["oracle_direction"])
        for item in ordered
    )
    metrics = {
        "window_count": len(ordered),
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
        "price_pnl": sum(float(item["price_pnl"]) for item in ordered),
        "funding_pnl": sum(float(item["funding_pnl"]) for item in ordered),
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
        "ending_equity": equity,
        "price_window_coverage_ratio": complete_price_windows / len(ordered),
        "funding_window_coverage_ratio": funding_covered / len(ordered),
        "complete_round_trip_ratio": complete_round_trips / len(ordered),
        "minimum_funding_events_per_window": min(
            int(item["funding_event_count"]) for item in ordered
        ),
        "maximum_funding_events_per_window": max(
            int(item["funding_event_count"]) for item in ordered
        ),
        "funding_timestamp_normalized_count": sum(
            int(item.get("funding_timestamp_normalized_count", 0)) for item in ordered
        ),
        "maximum_funding_timestamp_offset_ms": max(
            int(item.get("maximum_funding_timestamp_offset_ms", 0)) for item in ordered
        ),
        "direction_counts": dict(Counter(str(item["direction"]) for item in ordered)),
    }
    checks = {
        "total_pnl_positive": total_pnl > 0,
        "profit_factor_gt_1": (
            total_pnl > 0 if profit_factor is None else profit_factor > 1.0
        ),
        "max_drawdown_le_5pct": maximum_drawdown_pct <= 0.05,
        "best_window_concentration_le_35pct": concentration <= 0.35,
        "positive_window_ratio_ge_25pct": positive_ratio >= 0.25,
        "price_window_coverage_100pct": complete_price_windows == len(ordered),
        "funding_window_coverage_100pct": funding_covered == len(ordered),
        "one_causal_two_leg_round_trip_per_window": complete_round_trips == len(ordered),
    }
    return {
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
        "windows": list(ordered),
    }


def _evaluate_cells(
    price_windows: Mapping[str, Sequence[Mapping[str, Any]]],
    funding_events: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    funding_maps: dict[str, dict[str, list[dict[str, Any]]]] = {}
    funding_audit: dict[str, Any] = {}
    for asset in ASSET_CONFIG:
        funding_maps[asset], funding_audit[asset] = _events_by_window(
            funding_events[asset],
            price_windows[asset],
        )
    cells: dict[str, Any] = {}
    for role in EXPECTED_WINDOW_COUNTS:
        for scenario, maker_fee_rate in SCENARIO_FEE_RATES.items():
            symbols: dict[str, Any] = {}
            for asset, config in ASSET_CONFIG.items():
                windows = [item for item in price_windows[asset] if item["role"] == role]
                results = [
                    _window_result(
                        window,
                        funding_maps[asset][str(window["window_id"])],
                        asset=asset,
                        gross_capital=float(config["gross_capital"]),
                        maker_fee_rate=maker_fee_rate,
                    )
                    for window in windows
                ]
                symbols[asset] = _symbol_metrics(
                    results,
                    gross_capital=float(config["gross_capital"]),
                )
            cells[f"{role}_{scenario}"] = {
                "role": role,
                "scenario": scenario,
                "maker_fee_rate": maker_fee_rate,
                "window_count": EXPECTED_WINDOW_COUNTS[role],
                "symbols": symbols,
            }
    return cells, funding_audit


def _summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [
        cell["symbols"][asset]
        for cell in cells.values()
        for asset in ASSET_CONFIG
    ]
    if len(selected) != 12:
        raise RuntimeError(f"Round 26 cell-symbol 数量不一致: {len(selected)} != 12")
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
        "# Round 26：USD-M 季度期限价差单一因果候选结果",
        "",
        "方向仅由入场季度-永续基差符号决定，固定持有 168 小时；完整计入永续实际 funding 与双腿 Maker 往返费用。",
        "",
        "| 单元 | 资产 | 窗口 | 价格 PnL | Funding | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for asset in ASSET_CONFIG:
            item = cell["symbols"][asset]
            metrics = item["metrics"]
            failed = [name for name, passed in item["checks"].items() if not passed]
            pf = metrics["profit_factor"]
            pf_text = "∞" if pf is None and metrics["total_pnl"] > 0 else (
                "N/A" if pf is None else f"{float(pf):.3f}"
            )
            lines.append(
                "| `{cell}` | {asset} | {windows} | {price:.4f} | {funding:.4f} | "
                "{fees:.4f} | {pnl:.4f} | {pf} | {drawdown:.2%} | {positive:.2%} | "
                "{passed} | {failed} |".format(
                    cell=cell_name,
                    asset=asset,
                    windows=metrics["window_count"],
                    price=metrics["price_pnl"],
                    funding=metrics["funding_pnl"],
                    fees=metrics["fees_paid"],
                    pnl=metrics["total_pnl"],
                    pf=pf_text,
                    drawdown=metrics["maximum_drawdown_pct"],
                    positive=metrics["positive_window_ratio"],
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
    parser = argparse.ArgumentParser(
        description="评估 USD-M 永续/季度固定一周期限价差单一因果候选。"
    )
    parser.add_argument("--round25-result", default=str(ROUND25_RESULT_PATH))
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
        raise ValueError("Round 26 Phase A 协议哈希不一致。")
    round25_path = Path(args.round25_result).resolve()
    if _sha256(round25_path) != ROUND25_RESULT_SHA256:
        raise ValueError("Round 25 结果哈希不一致。")
    round25_payload = json.loads(round25_path.read_text(encoding="utf-8"))
    if not str(round25_payload.get("conclusion") or "").startswith(
        "NO_PREREGISTERED_CROSS_VENUE_FUNDING_SPREAD_CANDIDATE"
    ):
        raise ValueError("Round 25 失败结论不匹配。")
    if round25_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 25 之后 CURRENT Final OOS 已不再封存。")
    if round25_payload.get("formal_round25_preregistration_ready") is not False:
        raise ValueError("Round 25 不应允许正式注册。")

    price_manifests: dict[str, Any] = {}
    price_windows: dict[str, list[dict[str, Any]]] = {}
    price_audit: dict[str, Any] = {}
    funding_manifests: dict[str, Any] = {}
    funding_events: dict[str, list[dict[str, Any]]] = {}
    for asset, config in ASSET_CONFIG.items():
        manifest, windows, audit = _read_price_manifest(
            Path(config["price_manifest"]),
            expected_manifest_sha256=str(config["price_manifest_sha256"]),
            expected_csv_sha256=str(config["price_csv_sha256"]),
            expected_asset=asset,
            expected_perpetual_symbol=str(config["perpetual_symbol"]),
        )
        funding_manifest, events = _read_funding_manifest(
            Path(config["funding_manifest"]),
            expected_manifest_sha256=str(config["funding_manifest_sha256"]),
            expected_csv_sha256=str(config["funding_csv_sha256"]),
            expected_symbol=str(config["perpetual_symbol"]),
        )
        price_manifests[asset] = manifest
        price_windows[asset] = windows
        price_audit[asset] = audit
        funding_manifests[asset] = funding_manifest
        funding_events[asset] = events
    cross_asset_audit = _assert_asset_windows_aligned(price_windows)
    cells, funding_audit = _evaluate_cells(price_windows, funding_events)
    summary = _summary(cells)
    candidate_ready = bool(summary["all_cells_passed"])
    conclusion = (
        "QUARTERLY_CALENDAR_SPREAD_WORTH_EXECUTION_PREREGISTRATION：12/12 个年代、成本与标的单元均通过固定一周因果期限价差规则；只允许随后冻结真实盘口滑点、Maker 成交率、保证金和季度流动性约束。"
        if candidate_ready
        else "NO_PREREGISTERED_QUARTERLY_CALENDAR_SPREAD_CANDIDATE：至少一个单元在固定一周、因果基差方向、实际 funding 和理想同步 Maker 假设下仍失败，排除本协议定义的 USD-M 永续/季度期限价差 family。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "PREREGISTERED_CAUSAL_QUARTERLY_CALENDAR_SPREAD_PHASE_A",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {
            str(round25_path): ROUND25_RESULT_SHA256,
        },
        "price_manifests": {
            asset: {
                "path": str(Path(config["price_manifest"]).resolve()),
                "manifest_sha256": config["price_manifest_sha256"],
                "file_sha256": price_manifests[asset]["file_sha256"],
                "row_count": price_manifests[asset]["row_count"],
                "window_count": price_manifests[asset]["window_count"],
            }
            for asset, config in ASSET_CONFIG.items()
        },
        "funding_manifests": {
            asset: {
                "path": str(Path(config["funding_manifest"]).resolve()),
                "manifest_sha256": config["funding_manifest_sha256"],
                "file_sha256": funding_manifests[asset]["file_sha256"],
                "event_count": funding_manifests[asset]["event_count"],
            }
            for asset, config in ASSET_CONFIG.items()
        },
        "direction_mode": "NEUTRAL",
        "gross_capital_by_asset": {
            asset: config["gross_capital"] for asset, config in ASSET_CONFIG.items()
        },
        "maker_fee_rate_by_scenario": SCENARIO_FEE_RATES,
        "holding_hours": 168,
        "direction_rule": "sign(quarterly_entry_open-perpetual_entry_open)",
        "direction_is_causal": True,
        "oracle_is_used": False,
        "ignored_risks": [
            "maker_queue_failure",
            "leg_latency",
            "slippage",
            "margin_allocation",
            "liquidation",
            "adl",
            "exchange_risk",
            "quarterly_contract_liquidity_impact",
        ],
        "price_data_audit": price_audit,
        "cross_asset_alignment_audit": cross_asset_audit,
        "funding_window_audit": funding_audit,
        "window_counts": EXPECTED_WINDOW_COUNTS,
        "cells": cells,
        "summary": summary,
        "formal_round26_execution_preregistration_ready": candidate_ready,
        "selected_candidate_id": (
            "QUARTERLY_CALENDAR_1W_CAUSAL_V1" if candidate_ready else None
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
    result_path = report_dir / "round26-quarterly-calendar-spread-results.json"
    report_path = report_dir / "round26-quarterly-calendar-spread-report.md"
    _write_json(result_path, result)
    report_path.write_text(_report_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "report_path": str(report_path.resolve()),
                "passed_cell_symbol_count": summary["passed_cell_symbol_count"],
                "cell_symbol_count": summary["cell_symbol_count"],
                "formal_round26_execution_preregistration_ready": candidate_ready,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
