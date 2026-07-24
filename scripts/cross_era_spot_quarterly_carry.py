from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
HOUR_MS = 60 * 60 * 1000
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round28-spot-quarterly-carry-phase-a-protocol.md"
)
PROTOCOL_SHA256 = "605fd2eb2a1b248033cf55faa397daf357bbf35e77e3bb2923353917dad4aaeb"
DATA_PROTOCOL_SHA256 = "8c0c1dc18b02690a921db10db3b0abdbaeb12d7b842acfa0cc7b46318439e680"
DATA_AUDIT_SHA256 = "ab170a920c00e692d27569fa9ab1f9e8ca40cee829dd126f279cfabc75599dd0"
ROUND27_RESULT_PATH = Path(
    "reports/cross-era-oos/round27-absolute-trend-results.json"
)
ROUND27_RESULT_SHA256 = "3a15ae8a970f1ab54fde8e87a8303b2448737a6c618b56df4e78b3522e63c9f0"
EXPECTED_ROWS_PER_ASSET = 11_520
EXPECTED_SOURCE_ARCHIVES = 62
EXPECTED_WINDOW_COUNT = 16
EXPECTED_ROWS_PER_WINDOW = 720
EXPECTED_WINDOW_COUNTS = {"DEVELOPMENT": 5, "VALIDATION": 3, "POSTHISTORY": 8}
ENTRY_BASIS_THRESHOLD = 0.005
SCENARIO_COSTS = {
    "BASE": {"spot": 0.0015, "quarterly": 0.0010},
    "COST50": {"spot": 0.0020, "quarterly": 0.00175},
}
CSV_HEADER = (
    "role",
    "window_id",
    "entry_time",
    "expiry_time",
    "open_time",
    "spot_symbol",
    "quarterly_symbol",
    "spot_open",
    "spot_high",
    "spot_low",
    "spot_close",
    "quarterly_open",
    "quarterly_high",
    "quarterly_low",
    "quarterly_close",
    "spot_source_month",
    "spot_source_zip_sha256",
    "quarterly_source_month",
    "quarterly_source_zip_sha256",
)
ASSET_CONFIG = {
    "BTC": {
        "capital": 500.0,
        "spot_symbol": "BTCUSDT",
        "price_manifest": Path(
            "data/backtests/round28_spot_quarterly_carry/"
            "binance_spot_quarterly_carry_btc_1h_202102_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": (
            "1e997e7dfab329463673428f75fdbae429fd28ff1feed872f3226d462d17963f"
        ),
        "price_csv_sha256": (
            "a2505beed40d8e5167a9968b40b908f7be673a69b24fbdbb181f4765a34bc550"
        ),
    },
    "ETH": {
        "capital": 300.0,
        "spot_symbol": "ETHUSDT",
        "price_manifest": Path(
            "data/backtests/round28_spot_quarterly_carry/"
            "binance_spot_quarterly_carry_eth_1h_202102_202306_202408_202606.manifest.json"
        ),
        "price_manifest_sha256": (
            "2f39980b8c8ad12a80fc147e5bcd601b3ae382b355b65f7e50830be631c72442"
        ),
        "price_csv_sha256": (
            "6fa59c0b14df448434ad28df422b25b1d2bc9cd199f55835c2dd272e50b39d20"
        ),
    },
}


def _read_round27_result() -> dict[str, Any]:
    path = ROUND27_RESULT_PATH.resolve()
    if _sha256(path) != ROUND27_RESULT_SHA256:
        raise ValueError("Round 27 结果哈希不一致。")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not str(payload.get("conclusion", "")).startswith(
        "NO_PREREGISTERED_ABSOLUTE_TREND_CANDIDATE"
    ):
        raise ValueError("Round 27 前置结论不一致。")
    if payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 27 Final OOS 未保持封存。")
    if bool(payload.get("final_oos_authorized")) or bool(
        payload.get("stable_profit_claimed")
    ):
        raise ValueError("Round 27 不得授权 Final OOS 或稳定收益声明。")
    return payload


def _parse_utc(value: Any) -> datetime:
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"非法 UTC 时间: {raw}") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间缺少时区。")
    return parsed.astimezone(UTC)


def _read_manifest(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    expected_csv_sha256: str,
    expected_asset: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    path = manifest_path.resolve()
    if _sha256(path) != expected_manifest_sha256:
        raise ValueError(f"{expected_asset} Round 28 price manifest 哈希不一致。")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = {
        "data_protocol_sha256": DATA_PROTOCOL_SHA256,
        "availability_audit_sha256": DATA_AUDIT_SHA256,
        "provider": "binance_data_vision",
        "data_type": "klines",
        "interval": "1h",
        "asset": expected_asset,
        "file_sha256": expected_csv_sha256,
        "row_count": EXPECTED_ROWS_PER_ASSET,
        "window_count": EXPECTED_WINDOW_COUNT,
        "rows_per_window": EXPECTED_ROWS_PER_WINDOW,
        "duplicate_primary_keys": 0,
        "source_archive_count": EXPECTED_SOURCE_ARCHIVES,
        "official_checksums_verified": True,
        "authorized_windows_complete": True,
        "final_oos_status": "SEALED_NOT_EVALUATED",
    }
    for key, expected in expected_fields.items():
        if manifest.get(key) != expected:
            raise ValueError(f"{expected_asset} manifest 字段 {key} 不一致。")
    if manifest.get("window_counts") != EXPECTED_WINDOW_COUNTS:
        raise ValueError(f"{expected_asset} manifest 窗口角色数量不一致。")
    archives = list(manifest.get("source_archives") or [])
    if len(archives) != EXPECTED_SOURCE_ARCHIVES:
        raise ValueError(f"{expected_asset} source archive 数量不一致。")
    archive_sha: dict[tuple[str, str, str], str] = {}
    for archive in archives:
        key = (str(archive.get("market")), str(archive.get("symbol")), str(archive.get("month")))
        if key in archive_sha:
            raise ValueError(f"{expected_asset} source archive 重复。")
        if not bool(archive.get("official_checksum_verified")):
            raise ValueError(f"{expected_asset} source checksum 未通过。")
        archive_sha[key] = str(archive.get("zip_sha256"))

    csv_path = path.parent / str(manifest["file_name"])
    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != expected_csv_sha256:
        raise ValueError(f"{expected_asset} Round 28 CSV 哈希不一致。")
    rows: list[dict[str, Any]] = []
    by_window: dict[str, list[dict[str, Any]]] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_HEADER:
            raise ValueError(f"{expected_asset} Round 28 CSV 表头不一致。")
        for line_number, row in enumerate(reader, start=2):
            try:
                item = {
                    "role": str(row["role"]),
                    "window_id": str(row["window_id"]),
                    "entry_time": _parse_utc(row["entry_time"]),
                    "expiry_time": _parse_utc(row["expiry_time"]),
                    "open_time": int(row["open_time"]),
                    "spot_symbol": str(row["spot_symbol"]),
                    "quarterly_symbol": str(row["quarterly_symbol"]),
                    "spot_open": float(row["spot_open"]),
                    "spot_high": float(row["spot_high"]),
                    "spot_low": float(row["spot_low"]),
                    "spot_close": float(row["spot_close"]),
                    "quarterly_open": float(row["quarterly_open"]),
                    "quarterly_high": float(row["quarterly_high"]),
                    "quarterly_low": float(row["quarterly_low"]),
                    "quarterly_close": float(row["quarterly_close"]),
                    "spot_source_month": str(row["spot_source_month"]),
                    "spot_source_zip_sha256": str(row["spot_source_zip_sha256"]),
                    "quarterly_source_month": str(row["quarterly_source_month"]),
                    "quarterly_source_zip_sha256": str(row["quarterly_source_zip_sha256"]),
                }
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"{expected_asset} CSV 第 {line_number} 行无效。") from exc
            for prefix in ("spot", "quarterly"):
                values = tuple(item[f"{prefix}_{key}"] for key in ("open", "high", "low", "close"))
                if any(not math.isfinite(value) or value <= 0 for value in values):
                    raise ValueError(f"{expected_asset} CSV 第 {line_number} 行价格无效。")
                if values[1] < max(values[0], values[3]) or values[2] > min(values[0], values[3]) or values[1] < values[2]:
                    raise ValueError(f"{expected_asset} CSV 第 {line_number} 行 OHLC 无效。")
                market = "SPOT" if prefix == "spot" else "QUARTERLY"
                symbol = item[f"{prefix}_symbol"]
                month = item[f"{prefix}_source_month"]
                source_sha = item[f"{prefix}_source_zip_sha256"]
                if archive_sha.get((market, symbol, month)) != source_sha:
                    raise ValueError(f"{expected_asset} CSV 第 {line_number} 行来源 SHA 不一致。")
            by_window.setdefault(item["window_id"], []).append(item)
            rows.append(item)
    if len(rows) != EXPECTED_ROWS_PER_ASSET or len(by_window) != EXPECTED_WINDOW_COUNT:
        raise ValueError(f"{expected_asset} CSV 行数或窗口数不一致。")
    for window_id, window_rows in by_window.items():
        ordered = sorted(window_rows, key=lambda value: value["open_time"])
        if len(ordered) != EXPECTED_ROWS_PER_WINDOW:
            raise ValueError(f"{expected_asset} {window_id} 行数不一致。")
        times = [int(item["open_time"]) for item in ordered]
        if any(current - previous != HOUR_MS for previous, current in zip(times, times[1:])):
            raise ValueError(f"{expected_asset} {window_id} 小时不连续。")
        if ordered[0]["entry_time"] != datetime.fromtimestamp(times[0] / 1000, tz=UTC):
            raise ValueError(f"{expected_asset} {window_id} entry_time 不一致。")
    return manifest, rows, {
        "row_count": len(rows),
        "window_count": len(by_window),
        "window_counts": dict(
            Counter(str(window_rows[0]["role"]) for window_rows in by_window.values())
        ),
        "price_hour_coverage_ratio": 1.0,
        "source_archive_count": len(archives),
        "passed": True,
    }


def _assert_alignment(prices: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    reference = [(str(item["window_id"]), int(item["open_time"])) for item in prices["BTC"]]
    if any(
        [(str(item["window_id"]), int(item["open_time"])) for item in prices[asset]] != reference
        for asset in ("ETH",)
    ):
        raise ValueError("Round 28 BTC/ETH 配对时间键未完全对齐。")
    return {"asset_count": 2, "row_count_per_asset": len(reference), "timestamps_identical": True, "passed": True}


def _window_result(
    rows: Sequence[Mapping[str, Any]],
    *,
    role: str,
    asset: str,
    initial_capital: float,
    costs: Mapping[str, float],
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: int(item["open_time"]))
    if len(ordered) != EXPECTED_ROWS_PER_WINDOW:
        raise ValueError("Round 28 window 不是 720 小时路径。")
    entry = ordered[0]
    exit_row = ordered[-1]
    spot_entry = float(entry["spot_open"])
    quarterly_entry = float(entry["quarterly_open"])
    basis_pct = quarterly_entry / spot_entry - 1.0
    entered = basis_pct > ENTRY_BASIS_THRESHOLD
    if not entered:
        return {
            "role": role,
            "asset": asset,
            "window_id": str(entry["window_id"]),
            "entry_time": entry["entry_time"].isoformat(),
            "expiry_time": exit_row["expiry_time"].isoformat(),
            "entered": False,
            "basis_pct": basis_pct,
            "hourly_row_count": len(ordered),
            "price_pnl": 0.0,
            "execution_costs": 0.0,
            "net_pnl": 0.0,
            "minimum_path_pnl": 0.0,
            "maximum_path_pnl": 0.0,
            "execution_side_count": 0,
            "funding_pnl": 0.0,
            "entry_is_causal": True,
            "final_position_flat": True,
            "hourly_path_pnl": [0.0],
        }
    quantity = initial_capital / (spot_entry + quarterly_entry)
    entry_cost = quantity * (
        spot_entry * float(costs["spot"]) + quarterly_entry * float(costs["quarterly"])
    )
    path = [-entry_cost]
    for row in ordered:
        spot_open = float(row["spot_open"])
        quarterly_open = float(row["quarterly_open"])
        path.append(
            quantity
            * ((spot_open - spot_entry) - (quarterly_open - quarterly_entry))
            - entry_cost
        )
    spot_exit = float(exit_row["spot_close"])
    quarterly_exit = float(exit_row["quarterly_close"])
    price_pnl = quantity * ((spot_exit - spot_entry) - (quarterly_exit - quarterly_entry))
    exit_cost = quantity * (
        spot_exit * float(costs["spot"]) + quarterly_exit * float(costs["quarterly"])
    )
    net_pnl = price_pnl - entry_cost - exit_cost
    path.append(net_pnl)
    return {
        "role": role,
        "asset": asset,
        "window_id": str(entry["window_id"]),
        "entry_time": entry["entry_time"].isoformat(),
        "expiry_time": exit_row["expiry_time"].isoformat(),
        "entered": True,
        "basis_pct": basis_pct,
        "quantity": quantity,
        "hourly_row_count": len(ordered),
        "price_pnl": price_pnl,
        "execution_costs": entry_cost + exit_cost,
        "net_pnl": net_pnl,
        "minimum_path_pnl": min(path),
        "maximum_path_pnl": max(path),
        "execution_side_count": 4,
        "funding_pnl": 0.0,
        "entry_is_causal": True,
        "final_position_flat": True,
        "hourly_path_pnl": path,
        "entry_cost": entry_cost,
        "exit_cost": exit_cost,
    }


def _metrics(
    results: Sequence[Mapping[str, Any]],
    *,
    initial_capital: float,
    role: str,
) -> dict[str, Any]:
    ordered = sorted(results, key=lambda item: str(item["entry_time"]))
    entered = [item for item in ordered if bool(item["entered"])]
    trade_pnl = [float(item["net_pnl"]) for item in entered]
    gains = sum(value for value in trade_pnl if value > 0)
    losses = -sum(value for value in trade_pnl if value < 0)
    pf = None if losses <= 0 else gains / losses
    total_pnl = sum(float(item["net_pnl"]) for item in ordered)
    equity = initial_capital
    peak = equity
    maximum_drawdown = 0.0
    for item in ordered:
        if bool(item["entered"]):
            for pnl in (float(value) for value in item["hourly_path_pnl"]):
                mark = equity + pnl
                peak = max(peak, mark)
                maximum_drawdown = max(maximum_drawdown, (peak - mark) / max(peak, 1e-12))
        equity += float(item["net_pnl"])
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, (peak - equity) / max(peak, 1e-12))
    positive_trade_ratio = (
        sum(value > 0 for value in trade_pnl) / len(trade_pnl) if trade_pnl else 0.0
    )
    concentration = max((value for value in trade_pnl if value > 0), default=0.0) / gains if gains > 0 else 1.0
    minimum_active = {"DEVELOPMENT": 3, "VALIDATION": 2, "POSTHISTORY": 6}[role]
    checks = {
        "total_net_profit_strictly_positive": total_pnl > 0,
        "profit_factor_gt_1": total_pnl > 0 if pf is None else pf > 1.0,
        "maximum_drawdown_le_5pct": maximum_drawdown <= 0.05,
        "positive_trade_ratio_ge_75pct": positive_trade_ratio >= 0.75,
        "best_profitable_trade_concentration_le_35pct": concentration <= 0.35,
        "minimum_active_window_count": len(entered) >= minimum_active,
        "spot_and_quarterly_price_coverage_100pct": all(
            int(item["hourly_row_count"]) == EXPECTED_ROWS_PER_WINDOW for item in ordered
        ),
        "causal_entry_and_four_costed_sides": all(
            bool(item["entry_is_causal"])
            and (int(item["execution_side_count"]) == 4 if item["entered"] else int(item["execution_side_count"]) == 0)
            for item in ordered
        ),
        "funding_pnl_strictly_zero_and_flat": all(
            float(item["funding_pnl"]) == 0.0 and bool(item["final_position_flat"])
            for item in ordered
        ),
    }
    return {
        "metrics": {
            "window_count": len(ordered),
            "entered_window_count": len(entered),
            "skipped_window_count": len(ordered) - len(entered),
            "total_pnl": total_pnl,
            "return_pct": total_pnl / initial_capital,
            "ending_equity": initial_capital + total_pnl,
            "gross_profit": gains,
            "gross_loss": losses,
            "profit_factor": pf,
            "positive_trade_ratio": positive_trade_ratio,
            "best_profitable_trade_concentration": concentration,
            "maximum_drawdown_pct": maximum_drawdown,
            "price_pnl": sum(float(item["price_pnl"]) for item in ordered),
            "execution_costs": sum(float(item["execution_costs"]) for item in ordered),
            "funding_pnl": sum(float(item["funding_pnl"]) for item in ordered),
            "minimum_entry_basis_pct": min(float(item["basis_pct"]) for item in entered) if entered else None,
            "maximum_entry_basis_pct": max(float(item["basis_pct"]) for item in entered) if entered else None,
            "active_window_net_pnl": trade_pnl,
        },
        "checks": checks,
        "passed": all(checks.values()),
        "windows": list(ordered),
    }


def _evaluate(
    prices: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
        role: {asset: [] for asset in ASSET_CONFIG} for role in EXPECTED_WINDOW_COUNTS
    }
    for asset, rows in prices.items():
        by_window: dict[str, list[Mapping[str, Any]]] = {}
        for row in rows:
            by_window.setdefault(str(row["window_id"]), []).append(row)
        for window_rows in by_window.values():
            role = str(window_rows[0]["role"])
            grouped[role][asset].append(window_rows)
    cells: dict[str, Any] = {}
    for role in EXPECTED_WINDOW_COUNTS:
        for scenario, costs in SCENARIO_COSTS.items():
            symbols = {}
            for asset, config in ASSET_CONFIG.items():
                results = [
                    _window_result(
                        window_rows,
                        role=role,
                        asset=asset,
                        initial_capital=float(config["capital"]),
                        costs=costs,
                    )
                    for window_rows in sorted(
                        grouped[role][asset], key=lambda value: str(value[0]["entry_time"])
                    )
                ]
                symbols[asset] = _metrics(
                    results,
                    initial_capital=float(config["capital"]),
                    role=role,
                )
            cells[f"{role}_{scenario}"] = {
                "role": role,
                "scenario": scenario,
                "entry_basis_threshold": ENTRY_BASIS_THRESHOLD,
                "spot_cost_rate": costs["spot"],
                "quarterly_cost_rate": costs["quarterly"],
                "symbols": symbols,
            }
    return cells


def _summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [cell["symbols"][asset] for cell in cells.values() for asset in ASSET_CONFIG]
    if len(selected) != 12:
        raise RuntimeError(f"Round 28 cell 数量不一致: {len(selected)}")
    return {
        "cell_symbol_count": len(selected),
        "passed_cell_symbol_count": sum(bool(item["passed"]) for item in selected),
        "all_cells_passed": all(bool(item["passed"]) for item in selected),
        "minimum_total_pnl": min(float(item["metrics"]["total_pnl"]) for item in selected),
        "minimum_active_window_count": min(int(item["metrics"]["entered_window_count"]) for item in selected),
        "maximum_drawdown_pct": max(float(item["metrics"]["maximum_drawdown_pct"]) for item in selected),
        "minimum_positive_trade_ratio": min(float(item["metrics"]["positive_trade_ratio"]) for item in selected),
        "maximum_best_trade_concentration": max(float(item["metrics"]["best_profitable_trade_concentration"]) for item in selected),
    }


def _report(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 28：现货/季度交割合约现金套利结果",
        "",
        "固定交割前 30 日入场、50 bps 最低正基差，只做多现货/做空季度合约并持有至交割前最后小时；不含 funding。",
        "",
        "| 单元 | 资产 | 入场窗 | 净收益 | PF | 最大回撤 | 正收益窗 | 最佳窗集中度 | 价格 PnL | 成本 | 通过 | 失败检查 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for asset in ASSET_CONFIG:
            item = cell["symbols"][asset]
            metrics = item["metrics"]
            failed = ", ".join(name for name, passed in item["checks"].items() if not passed)
            pf = metrics["profit_factor"]
            lines.append(
                "| `{cell}` | {asset} | {active} | {pnl:.4f} | {pf} | {dd:.2%} | {ratio:.2%} | "
                "{conc:.2%} | {price:.4f} | {cost:.4f} | {passed} | {failed} |".format(
                    cell=cell_name,
                    asset=asset,
                    active=metrics["entered_window_count"],
                    pnl=metrics["total_pnl"],
                    pf="∞" if pf is None and metrics["total_pnl"] > 0 else ("N/A" if pf is None else f"{pf:.3f}"),
                    dd=metrics["maximum_drawdown_pct"],
                    ratio=metrics["positive_trade_ratio"],
                    conc=metrics["best_profitable_trade_concentration"],
                    price=metrics["price_pnl"],
                    cost=metrics["execution_costs"],
                    passed="是" if item["passed"] else "否",
                    failed=failed,
                )
            )
    lines.extend(
        [
            "",
            f"通过单元：{payload['summary']['passed_cell_symbol_count']}/{payload['summary']['cell_symbol_count']}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "CURRENT Final OOS 保持封存；未修改生产默认值；direction_mode 仍为 NEUTRAL。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="评估 Round 28 现货/季度现金套利候选。")
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    args = parser.parse_args()
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 28 Phase A 协议哈希不一致。")
    round27_payload = _read_round27_result()
    prices: dict[str, list[dict[str, Any]]] = {}
    manifests: dict[str, Any] = {}
    audits: dict[str, Any] = {}
    for asset, config in ASSET_CONFIG.items():
        manifest, prices[asset], audits[asset] = _read_manifest(
            config["price_manifest"],
            expected_manifest_sha256=str(config["price_manifest_sha256"]),
            expected_csv_sha256=str(config["price_csv_sha256"]),
            expected_asset=asset,
        )
        manifests[asset] = {
            "path": str(config["price_manifest"].resolve()),
            "manifest_sha256": config["price_manifest_sha256"],
            "csv_sha256": manifest["file_sha256"],
        }
    alignment = _assert_alignment(prices)
    cells = _evaluate(prices)
    summary = _summary(cells)
    candidate_ready = bool(summary["all_cells_passed"])
    conclusion = (
        "SPOT_QUARTERLY_CARRY_WORTH_EXECUTION_PREREGISTRATION：12/12 个严格单元全部通过；"
        "只允许继续冻结交易所最小数量、保证金、交割机制、盘口冲击和现货托管风险。"
        if candidate_ready
        else
        "NO_PREREGISTERED_SPOT_QUARTERLY_CARRY_CANDIDATE：至少一个严格单元失败，"
        "排除本协议定义的 30 日、50 bps、正向现金套利 family。"
    )
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "ROUND28_SPOT_QUARTERLY_CARRY_PHASE_A",
        "candidate_id": "SPOT_QUARTERLY_CARRY_30D_50BPS_V1",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {
            "round27_result_sha256": ROUND27_RESULT_SHA256,
            "data_protocol_sha256": DATA_PROTOCOL_SHA256,
            "data_audit_sha256": DATA_AUDIT_SHA256,
        },
        "round27_conclusion": round27_payload["conclusion"],
        "price_manifests": manifests,
        "price_data_audit": audits,
        "cross_asset_alignment_audit": alignment,
        "direction_mode": "NEUTRAL",
        "gross_capital_by_asset": {asset: config["capital"] for asset, config in ASSET_CONFIG.items()},
        "entry_basis_threshold": ENTRY_BASIS_THRESHOLD,
        "scenario_costs": SCENARIO_COSTS,
        "holding_hours": 720,
        "funding_pnl": 0.0,
        "cells": cells,
        "summary": summary,
        "formal_round28_execution_preregistration_ready": candidate_ready,
        "selected_candidate_id": "SPOT_QUARTERLY_CARRY_30D_50BPS_V1" if candidate_ready else None,
        "final_oos_authorization_ready": False,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    result_path = report_dir / "round28-spot-quarterly-carry-results.json"
    report_path = report_dir / "round28-spot-quarterly-carry-report.md"
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_report(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
                "report_path": str(report_path.resolve()),
                "passed_cell_symbol_count": summary["passed_cell_symbol_count"],
                "cell_symbol_count": summary["cell_symbol_count"],
                "formal_round28_execution_preregistration_ready": candidate_ready,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
