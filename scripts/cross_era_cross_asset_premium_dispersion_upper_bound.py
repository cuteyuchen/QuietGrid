from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_basis_convergence_upper_bound as round23
import scripts.cross_era_funding_carry_upper_bound as round22
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/"
    "round24-cross-asset-premium-dispersion-upper-bound-protocol.md"
)
PROTOCOL_SHA256 = "8b8a45b9420e594805cd086d68860c31cb64acc6a4a462625218422ca070ccfa"
ROUND23_RESULT_PATH = Path(
    "reports/cross-era-oos/round23-basis-convergence-upper-bound-results.json"
)
ROUND23_RESULT_SHA256 = (
    "cb7d46463744cb3a0df092ed1081d1f7577537984cb1ae38ed06f63654ddd206"
)
TOTAL_GROSS_CAPITAL = 600.0
BOOK_GROSS_CAPITAL = 300.0
PERPETUAL_NOTIONAL = 150.0
OBSERVATION_ROWS = 180
SCENARIOS = asset_audit.SCENARIOS


def _event_bounds(
    funding_events: Sequence[Mapping[str, Any]],
    entry_time: int,
    end_time: int,
) -> tuple[list[int], int, int]:
    times = [int(item["funding_time"]) for item in funding_events]
    if times != sorted(times) or len(times) != len(set(times)):
        raise ValueError("Funding events 未严格递增。")
    start_index = bisect.bisect_left(times, entry_time)
    end_index = bisect.bisect_right(times, end_time)
    if end_index <= start_index:
        raise RuntimeError("入场后没有 funding event。")
    return times, start_index, end_index


def _dispersion_window_result(
    window: Mapping[str, Any],
    btc_rows: Sequence[tuple[int, float]],
    eth_rows: Sequence[tuple[int, float]],
    btc_funding_events: Sequence[Mapping[str, Any]],
    eth_funding_events: Sequence[Mapping[str, Any]],
    *,
    maker_fee_rate: float,
) -> dict[str, Any]:
    if len(btc_rows) != len(eth_rows) or len(btc_rows) <= OBSERVATION_ROWS:
        raise ValueError(f"{window['window_id']} BTC/ETH Premium 路径长度不一致。")
    start_ms = int(window["start_ms"])
    end_ms = int(window["end_ms"])
    for index, ((btc_time, _btc_close), (eth_time, _eth_close)) in enumerate(
        zip(btc_rows, eth_rows)
    ):
        expected_time = start_ms + index * 60_000
        if int(btc_time) != expected_time or int(eth_time) != expected_time:
            raise ValueError(f"{window['window_id']} BTC/ETH Premium 未逐分钟对齐。")
    if int(btc_rows[-1][0]) + 60_000 != end_ms:
        raise ValueError(f"{window['window_id']} Premium 路径未覆盖窗口终点。")

    entry_open_time = int(btc_rows[OBSERVATION_ROWS - 1][0])
    entry_time = entry_open_time + 60_000
    btc_entry_premium = float(btc_rows[OBSERVATION_ROWS - 1][1])
    eth_entry_premium = float(eth_rows[OBSERVATION_ROWS - 1][1])
    entry_spread = btc_entry_premium - eth_entry_premium
    btc_direction = 1 if entry_spread >= 0 else -1
    eth_direction = -btc_direction
    direction_name = (
        "BTC_CONVERGENCE_ETH_REVERSE"
        if btc_direction > 0
        else "ETH_CONVERGENCE_BTC_REVERSE"
    )

    btc_times, btc_cursor, btc_end = _event_bounds(
        btc_funding_events,
        entry_time,
        end_ms,
    )
    eth_times, eth_cursor, eth_end = _event_bounds(
        eth_funding_events,
        entry_time,
        end_ms,
    )
    btc_start = btc_cursor
    eth_start = eth_cursor
    btc_cumulative_rate = 0.0
    eth_cumulative_rate = 0.0
    entry_fee = TOTAL_GROSS_CAPITAL * maker_fee_rate
    round_trip_fees = 2.0 * entry_fee

    gross_path: list[float] = []
    btc_event_counts: list[int] = []
    eth_event_counts: list[int] = []
    best_index = -1
    best_gross = -math.inf
    best_components: dict[str, float] = {}
    for path_index, (
        (btc_open_time, btc_exit_premium),
        (eth_open_time, eth_exit_premium),
    ) in enumerate(
        zip(
            btc_rows[OBSERVATION_ROWS:],
            eth_rows[OBSERVATION_ROWS:],
        )
    ):
        if int(btc_open_time) != int(eth_open_time):
            raise ValueError(f"{window['window_id']} BTC/ETH 退出分钟不一致。")
        exit_time = int(btc_open_time) + 60_000
        while btc_cursor < btc_end and btc_times[btc_cursor] <= exit_time:
            rate = float(btc_funding_events[btc_cursor]["funding_rate"])
            if not math.isfinite(rate):
                raise ValueError("BTC funding rate 非有限。")
            btc_cumulative_rate += rate
            btc_cursor += 1
        while eth_cursor < eth_end and eth_times[eth_cursor] <= exit_time:
            rate = float(eth_funding_events[eth_cursor]["funding_rate"])
            if not math.isfinite(rate):
                raise ValueError("ETH funding rate 非有限。")
            eth_cumulative_rate += rate
            eth_cursor += 1

        btc_basis_pnl = (
            PERPETUAL_NOTIONAL
            * btc_direction
            * (btc_entry_premium - float(btc_exit_premium))
        )
        eth_basis_pnl = (
            PERPETUAL_NOTIONAL
            * eth_direction
            * (eth_entry_premium - float(eth_exit_premium))
        )
        btc_funding_pnl = (
            PERPETUAL_NOTIONAL * btc_direction * btc_cumulative_rate
        )
        eth_funding_pnl = (
            PERPETUAL_NOTIONAL * eth_direction * eth_cumulative_rate
        )
        joint_gross = (
            btc_basis_pnl
            + eth_basis_pnl
            + btc_funding_pnl
            + eth_funding_pnl
        )
        gross_path.append(joint_gross)
        btc_event_counts.append(btc_cursor - btc_start)
        eth_event_counts.append(eth_cursor - eth_start)
        if joint_gross > best_gross:
            best_index = path_index
            best_gross = joint_gross
            best_components = {
                "btc_basis_pnl": btc_basis_pnl,
                "eth_basis_pnl": eth_basis_pnl,
                "btc_funding_pnl": btc_funding_pnl,
                "eth_funding_pnl": eth_funding_pnl,
            }
    if best_index < 0:
        raise RuntimeError(f"{window['window_id']} Oracle 没有联合退出候选。")

    exit_open_time = int(btc_rows[OBSERVATION_ROWS + best_index][0])
    exit_time = exit_open_time + 60_000
    btc_exit_premium = float(btc_rows[OBSERVATION_ROWS + best_index][1])
    eth_exit_premium = float(eth_rows[OBSERVATION_ROWS + best_index][1])
    exit_spread = btc_exit_premium - eth_exit_premium
    net_pnl = best_gross - round_trip_fees
    path_after_entry_fee = [value - entry_fee for value in gross_path[: best_index + 1]]
    minimum_path_pnl = min([-entry_fee, net_pnl, *path_after_entry_fee])
    return {
        "window_id": str(window["window_id"]),
        "role": str(window["role"]),
        "split": str(window["split"]),
        "market_close": str(window["market_close"]),
        "force_close_at": str(window["force_close_at"]),
        "observation_rows": OBSERVATION_ROWS,
        "entry_open_time": entry_open_time,
        "entry_time": entry_time,
        "btc_entry_premium": btc_entry_premium,
        "eth_entry_premium": eth_entry_premium,
        "entry_spread": entry_spread,
        "btc_direction": btc_direction,
        "eth_direction": eth_direction,
        "direction": direction_name,
        "exit_open_time": exit_open_time,
        "exit_time": exit_time,
        "btc_exit_premium": btc_exit_premium,
        "eth_exit_premium": eth_exit_premium,
        "exit_spread": exit_spread,
        "holding_minutes": (exit_time - entry_time) // 60_000,
        "btc_eligible_funding_event_count": btc_end - btc_start,
        "eth_eligible_funding_event_count": eth_end - eth_start,
        "btc_realized_funding_event_count": btc_event_counts[best_index],
        "eth_realized_funding_event_count": eth_event_counts[best_index],
        **best_components,
        "joint_basis_pnl": (
            best_components["btc_basis_pnl"] + best_components["eth_basis_pnl"]
        ),
        "joint_funding_pnl": (
            best_components["btc_funding_pnl"]
            + best_components["eth_funding_pnl"]
        ),
        "oracle_joint_gross_pnl": best_gross,
        "fees_paid": round_trip_fees,
        "net_pnl": net_pnl,
        "minimum_path_pnl": minimum_path_pnl,
        "premium_row_count": len(btc_rows),
        "premium_alignment_complete": True,
        "trade_count": 1,
    }


def _profit_factor(gains: float, losses: float) -> float | None:
    return None if losses <= 0 else gains / losses


def _joint_metrics(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not results:
        raise ValueError("Premium dispersion cell 没有窗口。")
    ordered = sorted(results, key=lambda item: str(item["market_close"]))
    pnl_values = [float(item["net_pnl"]) for item in ordered]
    positive = [value for value in pnl_values if value > 0]
    negative = [value for value in pnl_values if value < 0]
    gains = sum(positive)
    losses = -sum(negative)
    total_pnl = sum(pnl_values)
    profit_factor = _profit_factor(gains, losses)

    equity = TOTAL_GROSS_CAPITAL
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
    btc_eligible = [int(item["btc_eligible_funding_event_count"]) for item in ordered]
    eth_eligible = [int(item["eth_eligible_funding_event_count"]) for item in ordered]
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
        "joint_basis_pnl": sum(float(item["joint_basis_pnl"]) for item in ordered),
        "joint_funding_pnl": sum(
            float(item["joint_funding_pnl"]) for item in ordered
        ),
        "btc_basis_pnl": sum(float(item["btc_basis_pnl"]) for item in ordered),
        "eth_basis_pnl": sum(float(item["eth_basis_pnl"]) for item in ordered),
        "btc_funding_pnl": sum(
            float(item["btc_funding_pnl"]) for item in ordered
        ),
        "eth_funding_pnl": sum(
            float(item["eth_funding_pnl"]) for item in ordered
        ),
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
        "minimum_btc_eligible_funding_events": min(btc_eligible),
        "minimum_eth_eligible_funding_events": min(eth_eligible),
        "mean_holding_minutes": statistics.fmean(
            float(item["holding_minutes"]) for item in ordered
        ),
        "maximum_holding_minutes": max(int(item["holding_minutes"]) for item in ordered),
        "btc_convergence_count": sum(
            item["direction"] == "BTC_CONVERGENCE_ETH_REVERSE" for item in ordered
        ),
        "eth_convergence_count": sum(
            item["direction"] == "ETH_CONVERGENCE_BTC_REVERSE" for item in ordered
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
        "premium_alignment_100pct": all(
            bool(item["premium_alignment_complete"]) for item in ordered
        ),
        "all_windows_have_btc_funding": min(btc_eligible) > 0,
        "all_windows_have_eth_funding": min(eth_eligible) > 0,
        "one_trade_per_window": metrics["trade_count"] == metrics["window_count"],
    }
    return {
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
        "windows": list(ordered),
    }


def _evaluate_cells(
    windows: Sequence[Mapping[str, Any]],
    premium_rows: Mapping[str, Mapping[str, Sequence[tuple[int, float]]]],
    funding_events: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    grouped = round23._group_windows(windows)
    cells: dict[str, Any] = {}
    for role, splits in grouped.items():
        for split_name, split_windows in splits.items():
            cell_base = f"{role}_{split_name.upper()}"
            for scenario, cost in SCENARIOS.items():
                maker_fee_rate = float(cost[0])
                results = [
                    _dispersion_window_result(
                        window,
                        premium_rows["BTCUSDT"][str(window["window_id"])],
                        premium_rows["ETHUSDT"][str(window["window_id"])],
                        funding_events["BTCUSDT"],
                        funding_events["ETHUSDT"],
                        maker_fee_rate=maker_fee_rate,
                    )
                    for window in split_windows
                ]
                cells[f"{cell_base}_{scenario}"] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "maker_fee_rate": maker_fee_rate,
                    "window_count": len(split_windows),
                    "joint": _joint_metrics(results),
                }
    return cells


def _upper_bound_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    selected = [cell["joint"] for cell in cells.values()]
    if len(selected) != 8:
        raise RuntimeError(f"Premium dispersion cell 数量不一致: {len(selected)} != 8")
    return {
        "cell_count": len(selected),
        "passed_cell_count": sum(bool(item["passed"]) for item in selected),
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
        "# Round 24：BTC/ETH Cross-Asset Premium Dispersion 乐观上界结果",
        "",
        "BTC 与 ETH 各自保持 Spot/永续 delta-neutral，两个 basis book 等名义、方向相反；Oracle 只事后选择四腿共同退出分钟。",
        "",
        "| 单元 | 窗口 | 联合基差 | 联合 Funding | 费用 | 净收益 | PF | 最大回撤 | 正收益窗口 | 通过 | 失败检查 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        item = cell["joint"]
        metrics = item["metrics"]
        failed = [name for name, passed in item["checks"].items() if not passed]
        profit_factor = metrics["profit_factor"]
        lines.append(
            "| "
            f"`{cell_name}` | {metrics['window_count']} | "
            f"{metrics['joint_basis_pnl']:.4f} | "
            f"{metrics['joint_funding_pnl']:.4f} | "
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
            f"通过单元：{summary['passed_cell_count']}/{summary['cell_count']}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "CURRENT Final OOS 未读取；没有调整 Round 23 相邻参数；生产默认值未修改。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估 BTC/ETH 四腿 Premium Dispersion 不可部署乐观上界。"
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
        raise ValueError("Round 24 premium dispersion 协议哈希不一致。")
    round23_path = ROUND23_RESULT_PATH.resolve()
    if _sha256(round23_path) != ROUND23_RESULT_SHA256:
        raise ValueError("Round 23 冻结结果哈希不一致。")
    round23_payload = json.loads(round23_path.read_text(encoding="utf-8"))
    if round23_payload.get("formal_round23_preregistration_ready"):
        raise ValueError("Round 23 不应允许正式注册。")
    if not str(round23_payload.get("conclusion") or "").startswith(
        "NO_PREREGISTERED_BASIS_CONVERGENCE_CANDIDATE"
    ):
        raise ValueError("Round 23 失败结论不匹配。")
    if round23_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 23 之后 CURRENT Final OOS 已不再封存。")

    premium_paths = {
        "BTCUSDT": Path(args.btc_premium_manifest),
        "ETHUSDT": Path(args.eth_premium_manifest),
    }
    premium_manifests: dict[str, dict[str, Any]] = {}
    premium_windows: dict[str, list[dict[str, Any]]] = {}
    premium_rows: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for symbol in asset_audit.SYMBOLS:
        manifest, windows, rows = round23._read_premium_manifest(
            premium_paths[symbol],
            expected_sha256=round23.PREMIUM_MANIFEST_SHA256[symbol],
            expected_symbol=symbol,
        )
        premium_manifests[symbol] = manifest
        premium_windows[symbol] = windows
        premium_rows[symbol] = rows
    if [round23._window_signature(item) for item in premium_windows["BTCUSDT"]] != [
        round23._window_signature(item) for item in premium_windows["ETHUSDT"]
    ]:
        raise RuntimeError("BTC/ETH Premium Index 窗口定义不一致。")
    windows = premium_windows["BTCUSDT"]

    funding_paths = {
        "BTCUSDT": Path(args.btc_funding_manifest),
        "ETHUSDT": Path(args.eth_funding_manifest),
    }
    funding_manifests: dict[str, dict[str, Any]] = {}
    funding_events: dict[str, list[dict[str, Any]]] = {}
    for symbol in asset_audit.SYMBOLS:
        manifest, events = round22._read_funding_manifest(
            funding_paths[symbol],
            expected_sha256=round23.FUNDING_MANIFEST_SHA256[symbol],
            expected_symbol=symbol,
        )
        funding_manifests[symbol] = manifest
        funding_events[symbol] = events

    cells = _evaluate_cells(windows, premium_rows, funding_events)
    upper_bound = _upper_bound_summary(cells)
    family_ready = bool(upper_bound["all_cells_passed"])
    conclusion = (
        "CROSS_ASSET_PREMIUM_DISPERSION_WORTH_PREREGISTRATION：8/8 个年代与成本单元均通过乐观上界；仅允许随后定义单一因果退出并冻结真实成交基差与借币成本。"
        if family_ready
        else "NO_PREREGISTERED_CROSS_ASSET_PREMIUM_DISPERSION_CANDIDATE：至少一个单元在等名义四腿、causal spread 方向和 Oracle 联合退出下仍失败，排除本协议定义的 dispersion family。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "NON_DEPLOYABLE_CROSS_ASSET_PREMIUM_DISPERSION_UPPER_BOUND",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {
            str(round23_path): ROUND23_RESULT_SHA256,
            **{
                str(premium_paths[symbol].resolve()): round23.PREMIUM_MANIFEST_SHA256[
                    symbol
                ]
                for symbol in asset_audit.SYMBOLS
            },
            **{
                str(funding_paths[symbol].resolve()): round23.FUNDING_MANIFEST_SHA256[
                    symbol
                ]
                for symbol in asset_audit.SYMBOLS
            },
        },
        "premium_manifests": {
            symbol: {
                "path": str(premium_paths[symbol].resolve()),
                "manifest_sha256": round23.PREMIUM_MANIFEST_SHA256[symbol],
                "file_sha256": premium_manifests[symbol]["file_sha256"],
                "row_count": premium_manifests[symbol]["row_count"],
                "window_count": premium_manifests[symbol]["window_count"],
            }
            for symbol in asset_audit.SYMBOLS
        },
        "funding_manifests": {
            symbol: {
                "path": str(funding_paths[symbol].resolve()),
                "manifest_sha256": round23.FUNDING_MANIFEST_SHA256[symbol],
                "file_sha256": funding_manifests[symbol]["file_sha256"],
                "event_count": funding_manifests[symbol]["event_count"],
            }
            for symbol in asset_audit.SYMBOLS
        },
        "direction_mode": "NEUTRAL",
        "total_gross_capital": TOTAL_GROSS_CAPITAL,
        "book_gross_capital": BOOK_GROSS_CAPITAL,
        "perpetual_notional_per_symbol": PERPETUAL_NOTIONAL,
        "observation_rows": OBSERVATION_ROWS,
        "direction_rule": "SIGN_OF_BTC_PREMIUM_MINUS_ETH_PREMIUM",
        "oracle_selects_future_joint_exit": True,
        "oracle_is_deployable": False,
        "window_counts": {
            f"{role}_{split}": len(items)
            for role, splits in round23._group_windows(windows).items()
            for split, items in splits.items()
        },
        "cells": cells,
        "upper_bound_summary": upper_bound,
        "formal_round24_preregistration_ready": family_ready,
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
    result_path = (
        report_dir / "round24-cross-asset-premium-dispersion-upper-bound-results.json"
    )
    report_path = (
        report_dir / "round24-cross-asset-premium-dispersion-upper-bound-report.md"
    )
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
