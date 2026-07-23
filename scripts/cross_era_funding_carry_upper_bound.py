from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_long_horizon_regime as round15
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.cross_era_relative_momentum_upper_bound as round21
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256
from scripts.robustness import _weekend_boundaries, verify_frozen_dataset


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round22-funding-carry-upper-bound-protocol.md"
)
PROTOCOL_SHA256 = "d5df0db9557946b06efa2e8990fe483a6cf8e9a8806fff892f8da53cb6652579"
ROUND21_RESULT_SHA256 = "e244d00bb3488868483f79ccee93c465f8d1972778a5307cd357df10d989b0f0"
BTC_MANIFEST_SHA256 = "a0ab7085778dfd1c35f42d7981d6ff2fa4fc2d75b279f5c1785a391c23280b57"
ETH_MANIFEST_SHA256 = "19bbf5d31ed381652c6893ab2b6e709bcdc40086a629f40423fccf93c63ddc7f"
DATA_PROTOCOL_SHA256 = "4ccabf8de9df47b0090f8506a2172141ee6d51a7f9b8cadc29c4a4c93bce4b3e"
EXPECTED_EVENT_COUNT = 5928
EXPECTED_ARCHIVE_COUNT = 65
CAPITAL_BY_SYMBOL = {"BTCUSDT": 500.0, "ETHUSDT": 300.0}
POSTHISTORY_START = datetime(2024, 8, 1, tzinfo=UTC)
POSTHISTORY_END = datetime(2026, 7, 1, tzinfo=UTC)
VALIDATION_FUNDING_CUTOFF = datetime(2023, 7, 1, tzinfo=UTC)
EXCLUDED_MONTHS = {
    f"{year:04d}-{month:02d}"
    for year, month in (
        (2023, 7),
        (2023, 8),
        (2023, 9),
        (2023, 10),
        (2023, 11),
        (2023, 12),
        (2024, 1),
        (2024, 2),
        (2024, 3),
        (2024, 4),
        (2024, 5),
        (2024, 6),
        (2024, 7),
    )
}


def _read_funding_manifest(
    manifest_path: Path,
    *,
    expected_sha256: str,
    expected_symbol: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = manifest_path.resolve()
    if _sha256(path) != expected_sha256:
        raise ValueError(f"{expected_symbol} funding manifest 哈希不一致。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("data_protocol_sha256") != DATA_PROTOCOL_SHA256:
        raise ValueError(f"{expected_symbol} funding 数据协议哈希不一致。")
    if str(manifest.get("symbol") or "").upper() != expected_symbol:
        raise ValueError(f"{expected_symbol} funding manifest 标的不一致。")
    if manifest.get("market") != "USDS_M" or manifest.get("data_type") != "fundingRate":
        raise ValueError(f"{expected_symbol} funding manifest 市场或类型不一致。")
    if int(manifest.get("event_count", -1)) != EXPECTED_EVENT_COUNT:
        raise ValueError(f"{expected_symbol} funding event 数量不一致。")
    if int(manifest.get("duplicate_events", -1)) != 0:
        raise ValueError(f"{expected_symbol} funding 包含重复事件。")
    archives = list(manifest.get("source_archives") or [])
    if len(archives) != EXPECTED_ARCHIVE_COUNT:
        raise ValueError(f"{expected_symbol} funding source archive 数量不一致。")
    if not bool(manifest.get("official_checksums_verified")):
        raise ValueError(f"{expected_symbol} funding 官方 checksum 未全部通过。")
    archive_by_month = {str(item["month"]): item for item in archives}
    if len(archive_by_month) != EXPECTED_ARCHIVE_COUNT:
        raise ValueError(f"{expected_symbol} funding source month 重复。")
    if set(archive_by_month) & EXCLUDED_MONTHS:
        raise ValueError(f"{expected_symbol} funding manifest 触碰封存月份。")
    if any(not bool(item.get("official_checksum_verified")) for item in archives):
        raise ValueError(f"{expected_symbol} funding 月度 checksum 审计失败。")

    data_path = path.parent / str(manifest["file_name"])
    if hashlib.sha256(data_path.read_bytes()).hexdigest() != manifest.get("file_sha256"):
        raise ValueError(f"{expected_symbol} funding CSV 哈希不一致。")
    events: list[dict[str, Any]] = []
    with data_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_header = (
            "funding_time",
            "funding_interval_hours",
            "funding_rate",
            "source_month",
            "source_zip_sha256",
        )
        if tuple(reader.fieldnames or ()) != expected_header:
            raise ValueError(f"{expected_symbol} funding CSV 表头不一致。")
        previous_time: int | None = None
        for line_number, row in enumerate(reader, start=2):
            try:
                funding_time = int(row["funding_time"])
                interval_hours = int(row["funding_interval_hours"])
                funding_rate = float(row["funding_rate"])
                source_month = str(row["source_month"])
                source_zip_sha256 = str(row["source_zip_sha256"])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(
                    f"{expected_symbol} funding CSV 第 {line_number} 行无效。"
                ) from exc
            archive = archive_by_month.get(source_month)
            if archive is None or source_month in EXCLUDED_MONTHS:
                raise ValueError(f"{expected_symbol} funding CSV 包含未授权月份。")
            if source_zip_sha256 != str(archive["zip_sha256"]):
                raise ValueError(f"{expected_symbol} funding 行级 source SHA 不一致。")
            if interval_hours <= 0 or not math.isfinite(funding_rate):
                raise ValueError(f"{expected_symbol} funding rate 或 interval 无效。")
            if previous_time is not None and funding_time <= previous_time:
                raise ValueError(f"{expected_symbol} funding_time 未严格递增。")
            previous_time = funding_time
            events.append(
                {
                    "funding_time": funding_time,
                    "funding_interval_hours": interval_hours,
                    "funding_rate": funding_rate,
                    "source_month": source_month,
                }
            )
    if len(events) != EXPECTED_EVENT_COUNT:
        raise ValueError(f"{expected_symbol} funding CSV 事件数量不一致。")
    return manifest, events


def _window_mapping(
    boundaries: Sequence[tuple[datetime, datetime]],
) -> dict[str, dict[str, Any]]:
    return {
        f"nyse_{market_close.strftime('%Y%m%dT%H%M%SZ')}": {
            "window_id": f"nyse_{market_close.strftime('%Y%m%dT%H%M%SZ')}",
            "market_close": market_close,
            "force_close_at": force_close_at,
        }
        for market_close, force_close_at in boundaries
    }


def _authorized_windows(
    round12_payload: Mapping[str, Any],
    round13_payload: Mapping[str, Any],
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, Any]]:
    config = profit_opt._base_research_config()
    current_manifests = tuple(str(item["manifest"]) for item in round12_payload["datasets"])
    current_metadata = [verify_frozen_dataset(path) for path in current_manifests]
    _current_end, development_ids, validation_ids, current_isolation = (
        round15._current_authorized_end(current_metadata, config)
    )
    current_boundaries = _weekend_boundaries(
        datetime.fromisoformat(str(current_metadata[0]["actual_start"])),
        datetime.fromisoformat(str(current_metadata[0]["actual_end"])),
        config.force_close_minutes,
    )
    current_by_id = _window_mapping(current_boundaries)
    development = [current_by_id[value] for value in development_ids]
    validation = [
        current_by_id[value]
        for value in validation_ids
        if current_by_id[value]["force_close_at"] <= VALIDATION_FUNDING_CUTOFF
    ]
    if len(development) != 108 or len(validation) != 49:
        raise RuntimeError("Round 22 CURRENT 窗口数量不一致。")

    prehistory_manifests = tuple(
        str(item["manifest"]) for item in round13_payload["datasets"]
    )
    _prehistory_metadata, prehistory_windows = round15._load_dataset(
        prehistory_manifests,
        config,
    )
    prehistory_ids = round13._paired_ready_window_ids(prehistory_windows)
    prehistory_by_id: dict[str, dict[str, Any]] = {}
    for window in prehistory_windows:
        if window.window_id not in prehistory_ids:
            continue
        prehistory_by_id.setdefault(
            str(window.window_id),
            {
                "window_id": str(window.window_id),
                "market_close": window.market_close,
                "force_close_at": window.force_close_at,
            },
        )
    prehistory = [prehistory_by_id[value] for value in prehistory_ids]
    if len(prehistory) != 28:
        raise RuntimeError("Round 22 PREHISTORY 窗口数量不一致。")

    posthistory_boundaries = _weekend_boundaries(
        POSTHISTORY_START,
        POSTHISTORY_END,
        config.force_close_minutes,
    )
    posthistory = list(_window_mapping(posthistory_boundaries).values())
    if len(posthistory) != 108:
        raise RuntimeError("Round 22 POSTHISTORY 窗口数量不一致。")

    return {
        "PREHISTORY": {"external": prehistory},
        "CURRENT": {
            "development": development,
            "validation_complete_months": validation,
        },
        "POSTHISTORY": {"external": posthistory},
    }, current_isolation


def _events_by_window(
    events: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    times = [int(item["funding_time"]) for item in events]
    assigned: dict[str, list[dict[str, Any]]] = {}
    assigned_event_times: set[int] = set()
    for window in windows:
        start_ms = int(window["market_close"].timestamp() * 1000)
        end_ms = int(window["force_close_at"].timestamp() * 1000)
        start_index = bisect.bisect_left(times, start_ms)
        end_index = bisect.bisect_left(times, end_ms)
        selected = [dict(item) for item in events[start_index:end_index]]
        if not selected:
            raise RuntimeError(f"{window['window_id']} 没有 funding event。")
        for item in selected:
            funding_time = int(item["funding_time"])
            if funding_time in assigned_event_times:
                raise RuntimeError("funding event 被分配到多个窗口。")
            assigned_event_times.add(funding_time)
        assigned[str(window["window_id"])] = selected
    return assigned, {
        "window_count": len(windows),
        "assigned_event_count": len(assigned_event_times),
        "minimum_events_per_window": min(len(value) for value in assigned.values()),
        "maximum_events_per_window": max(len(value) for value in assigned.values()),
        "all_windows_have_events": True,
        "events_assigned_once": True,
        "passed": True,
    }


def _carry_window_result(
    window: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    capital: float,
    maker_fee_rate: float,
) -> dict[str, Any]:
    rates = [float(item["funding_rate"]) for item in events]
    funding_sum = sum(rates)
    direction_sign = 1.0 if funding_sum >= 0 else -1.0
    direction = (
        "LONG_SPOT_SHORT_PERP"
        if funding_sum >= 0
        else "SHORT_SPOT_LONG_PERP"
    )
    perpetual_notional = capital / 2.0
    funding_income = perpetual_notional * abs(funding_sum)
    entry_fee = capital * maker_fee_rate
    exit_fee = capital * maker_fee_rate
    net_pnl = funding_income - entry_fee - exit_fee
    cumulative = -entry_fee
    path = [cumulative]
    for rate in rates:
        cumulative += perpetual_notional * direction_sign * rate
        path.append(cumulative)
    cumulative -= exit_fee
    path.append(cumulative)
    return {
        "window_id": str(window["window_id"]),
        "market_close": window["market_close"].isoformat(),
        "force_close_at": window["force_close_at"].isoformat(),
        "symbol": symbol,
        "capital": capital,
        "spot_notional": capital / 2.0,
        "perpetual_notional": perpetual_notional,
        "event_count": len(events),
        "funding_sum": funding_sum,
        "oracle_direction": direction,
        "funding_income": funding_income,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees_paid": entry_fee + exit_fee,
        "net_pnl": net_pnl,
        "minimum_path_pnl": min(path),
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
        raise ValueError("Funding carry cell 没有窗口。")
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
    metrics = {
        "window_count": len(ordered),
        "trade_count": sum(int(item["trade_count"]) for item in ordered),
        "event_count": sum(int(item["event_count"]) for item in ordered),
        "minimum_events_per_window": min(int(item["event_count"]) for item in ordered),
        "maximum_events_per_window": max(int(item["event_count"]) for item in ordered),
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
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
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
        "all_windows_have_funding_events": metrics["minimum_events_per_window"] > 0,
        "one_trade_per_window": metrics["trade_count"] == metrics["window_count"],
    }
    return {
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
        "windows": list(ordered),
    }


def _evaluate_cells(
    datasets: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    funding_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    cells: dict[str, Any] = {}
    event_audit: dict[str, Any] = {}
    for role, splits in datasets.items():
        for split_name, windows in splits.items():
            cell_base = f"{role}_{split_name.upper()}"
            event_maps = {}
            event_audit[cell_base] = {}
            for symbol in asset_audit.SYMBOLS:
                event_maps[symbol], event_audit[cell_base][symbol] = _events_by_window(
                    funding_by_symbol[symbol],
                    windows,
                )
            for scenario, cost in asset_audit.SCENARIOS.items():
                maker_fee_rate = float(cost[0])
                symbols = {}
                for symbol in asset_audit.SYMBOLS:
                    capital = CAPITAL_BY_SYMBOL[symbol]
                    results = [
                        _carry_window_result(
                            window,
                            event_maps[symbol][str(window["window_id"])],
                            symbol=symbol,
                            capital=capital,
                            maker_fee_rate=maker_fee_rate,
                        )
                        for window in windows
                    ]
                    symbols[symbol] = _symbol_metrics(results, capital=capital)
                cells[f"{cell_base}_{scenario}"] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "maker_fee_rate": maker_fee_rate,
                    "window_count": len(windows),
                    "symbols": symbols,
                }
    return cells, event_audit


def _upper_bound_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [
        cell["symbols"][symbol]
        for cell in cells.values()
        for symbol in asset_audit.SYMBOLS
    ]
    if len(selected) != 16:
        raise RuntimeError(f"Funding carry cell-symbol 数量不一致: {len(selected)} != 16")
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
        "# Round 22：现货-永续 Funding Carry 乐观上界结果",
        "",
        "Oracle 事后选择每个窗口的 carry 方向；忽略基差、借币与执行风险，仅保留实际 funding 和双腿 Maker 往返费用。",
        "",
        "| 单元 | 标的 | 窗口 | Funding 收入 | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for symbol in asset_audit.SYMBOLS:
            item = cell["symbols"][symbol]
            metrics = item["metrics"]
            failed = [name for name, passed in item["checks"].items() if not passed]
            pf = metrics["profit_factor"]
            pf_text = "∞" if pf is None and metrics["total_pnl"] > 0 else (
                "N/A" if pf is None else f"{float(pf):.3f}"
            )
            lines.append(
                "| `{cell}` | {symbol} | {windows} | {income:.4f} | {fees:.4f} | "
                "{pnl:.4f} | {pf} | {drawdown:.2%} | {positive:.2%} | {passed} | {failed} |".format(
                    cell=cell_name,
                    symbol=symbol,
                    windows=metrics["window_count"],
                    income=metrics["funding_income"],
                    fees=metrics["fees_paid"],
                    pnl=metrics["total_pnl"],
                    pf=pf_text,
                    drawdown=metrics["maximum_drawdown_pct"],
                    positive=metrics["positive_window_ratio"],
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
            "封存月份与 CURRENT Final OOS 未读取；没有注册候选；生产默认值未修改。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估 BTC/ETH 周末现货-永续 funding carry 的不可部署乐观上界。"
    )
    parser.add_argument(
        "--round12-result",
        default="reports/cross-era-oos/round12-quadratic-volatility-defense-results.json",
    )
    parser.add_argument(
        "--round13-result",
        default="reports/cross-era-oos/round13-prehistory-quadratic-w2160-results.json",
    )
    parser.add_argument(
        "--round21-result",
        default="reports/cross-era-oos/round21-relative-momentum-upper-bound-results.json",
    )
    parser.add_argument(
        "--btc-funding-manifest",
        default="data/backtests/round22_funding_carry/binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json",
    )
    parser.add_argument(
        "--eth-funding-manifest",
        default="data/backtests/round22_funding_carry/binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json",
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
        raise ValueError("Round 22 funding carry 协议哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    round21_path = Path(args.round21_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        round21_path: ROUND21_RESULT_SHA256,
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")
    round12_payload = json.loads(round12_path.read_text(encoding="utf-8"))
    round13_payload = json.loads(round13_path.read_text(encoding="utf-8"))
    round21_payload = json.loads(round21_path.read_text(encoding="utf-8"))
    if round21_payload.get("formal_round21_preregistration_ready"):
        raise ValueError("Round 21 不应允许正式注册。")
    if not str(round21_payload.get("conclusion") or "").startswith(
        "NO_PREREGISTERED_RELATIVE_MOMENTUM_CANDIDATE"
    ):
        raise ValueError("Round 21 失败结论不匹配。")
    if round21_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 21 之后 CURRENT Final OOS 已不再封存。")
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")

    btc_manifest, btc_events = _read_funding_manifest(
        Path(args.btc_funding_manifest),
        expected_sha256=BTC_MANIFEST_SHA256,
        expected_symbol="BTCUSDT",
    )
    eth_manifest, eth_events = _read_funding_manifest(
        Path(args.eth_funding_manifest),
        expected_sha256=ETH_MANIFEST_SHA256,
        expected_symbol="ETHUSDT",
    )
    datasets, current_isolation = _authorized_windows(round12_payload, round13_payload)
    cells, event_audit = _evaluate_cells(
        datasets,
        {"BTCUSDT": btc_events, "ETHUSDT": eth_events},
    )
    upper_bound = _upper_bound_summary(cells)
    family_ready = bool(upper_bound["all_cells_passed"])
    conclusion = (
        "FUNDING_CARRY_FAMILY_WORTH_PREREGISTRATION：16/16 个年代、成本与标的单元均通过乐观上界；仅允许随后冻结价格和借币成本并另写正式协议。"
        if family_ready
        else "NO_PREREGISTERED_FUNDING_CARRY_CANDIDATE：至少一个单元在未来已知方向、零基差风险和同步 Maker 假设下仍失败，排除本协议定义的周末 funding carry 家族。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "NON_DEPLOYABLE_FUNDING_CARRY_UPPER_BOUND",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "funding_manifests": {
            "BTCUSDT": {
                "path": str(Path(args.btc_funding_manifest).resolve()),
                "manifest_sha256": BTC_MANIFEST_SHA256,
                "file_sha256": btc_manifest["file_sha256"],
                "event_count": len(btc_events),
            },
            "ETHUSDT": {
                "path": str(Path(args.eth_funding_manifest).resolve()),
                "manifest_sha256": ETH_MANIFEST_SHA256,
                "file_sha256": eth_manifest["file_sha256"],
                "event_count": len(eth_events),
            },
        },
        "direction_mode": "NEUTRAL",
        "capital_by_symbol": CAPITAL_BY_SYMBOL,
        "oracle_uses_future_funding_sum": True,
        "oracle_is_deployable": False,
        "ignored_risks": [
            "spot_perpetual_basis",
            "borrow_interest",
            "spot_short_availability",
            "margin_and_liquidation",
            "maker_queue_failure",
            "leg_latency",
            "slippage",
            "funding_prediction_error",
        ],
        "current_isolation": current_isolation,
        "window_counts": {
            role: {name: len(windows) for name, windows in splits.items()}
            for role, splits in datasets.items()
        },
        "event_audit": event_audit,
        "cells": cells,
        "upper_bound_summary": upper_bound,
        "formal_round22_preregistration_ready": family_ready,
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
    result_path = report_dir / "round22-funding-carry-upper-bound-results.json"
    report_path = report_dir / "round22-funding-carry-upper-bound-report.md"
    _write_json(result_path, result)
    report_path.write_text(_report_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "report_path": str(report_path.resolve()),
                "passed_cell_symbol_count": upper_bound["passed_cell_symbol_count"],
                "cell_symbol_count": upper_bound["cell_symbol_count"],
                "formal_round22_preregistration_ready": family_ready,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
