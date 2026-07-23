from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_cross_asset_spread_upper_bound as round19
import scripts.cross_era_long_horizon_regime as round15
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.cross_era_spot_feasibility as spot_round
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_oos import _dataset_brief, _write_json
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256
from scripts.robustness import verify_frozen_dataset


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round20-cross-asset-zscore-taker-protocol.md"
)
PROTOCOL_SHA256 = "914ee8f7f488aeb6904d5dcb4a48455abd353257c98a5afb61c8f3506cc874df"
ROUND19_RESULT_SHA256 = "d90a031388032f8665c7af95055705d39fa98872dac7d858a7639c23738424f3"
ROUND19_SOURCE_SHA256 = "fd04c8acd2868be534e38c30ba2e67796189a884e4639b651e7af3982853ec92"
CANDIDATE_ID = "PAIR_Z2_STOP4_TAKER_V1"
ENTRY_Z = 2.0
STOP_Z = 4.0
FUNDING_RATE_PER_SETTLEMENT = 0.0001
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000


def _spread_state(btc: Any, eth: Any) -> dict[str, float]:
    observation_rows = int(btc.observation_rows)
    if observation_rows != round19.OBSERVATION_ROWS:
        raise ValueError("Round 20 观察长度必须固定为 180。")
    if int(eth.observation_rows) != observation_rows:
        raise ValueError("BTC/ETH 观察长度不一致。")
    btc_closes = [float(row.close) for row in btc.rows[:observation_rows]]
    eth_closes = [float(row.close) for row in eth.rows[:observation_rows]]
    beta = round19._observation_beta(btc_closes, eth_closes)
    spreads = [
        math.log(eth_close) - beta * math.log(btc_close)
        for btc_close, eth_close in zip(btc_closes, eth_closes)
    ]
    mean = statistics.fmean(spreads)
    std = statistics.pstdev(spreads)
    if not math.isfinite(std) or std <= 1e-12:
        raise ValueError(f"{btc.window_id} 观察价差标准差无效: {std!r}")
    return {"beta": beta, "spread_mean": mean, "spread_std": std}


def _z_scores(btc: Any, eth: Any, state: Mapping[str, float]) -> list[float]:
    if len(btc.rows) != len(eth.rows):
        raise ValueError("BTC/ETH K 线数量不一致。")
    beta = float(state["beta"])
    mean = float(state["spread_mean"])
    std = float(state["spread_std"])
    values = []
    for btc_row, eth_row in zip(btc.rows, eth.rows):
        if float(btc_row.close) <= 0 or float(eth_row.close) <= 0:
            raise ValueError("Z-score 遇到非正收盘价。")
        spread = math.log(float(eth_row.close)) - beta * math.log(float(btc_row.close))
        values.append((spread - mean) / std)
    return values


def _trade_plan(
    z_scores: Sequence[float],
    *,
    observation_rows: int = round19.OBSERVATION_ROWS,
) -> dict[str, Any]:
    if len(z_scores) <= observation_rows:
        raise ValueError("Z-score 路径没有可交易 K 线。")
    entry_signal_index: int | None = None
    direction: str | None = None
    for index in range(observation_rows, len(z_scores) - 1):
        value = float(z_scores[index])
        if value <= -ENTRY_Z:
            entry_signal_index = index
            direction = "LONG_SPREAD"
            break
        if value >= ENTRY_Z:
            entry_signal_index = index
            direction = "SHORT_SPREAD"
            break
    if entry_signal_index is None or direction is None:
        return {"status": "NO_ENTRY"}

    entry_execution_index = entry_signal_index + 1
    exit_signal_index: int | None = None
    exit_execution_index: int | None = None
    exit_reason = "FORCE_CLOSE"
    for index in range(entry_execution_index, len(z_scores) - 1):
        value = float(z_scores[index])
        mean_reverted = (
            direction == "LONG_SPREAD" and value >= 0.0
        ) or (
            direction == "SHORT_SPREAD" and value <= 0.0
        )
        stopped = abs(value) >= STOP_Z
        if mean_reverted or stopped:
            exit_signal_index = index
            exit_execution_index = index + 1
            exit_reason = "MEAN_REVERSION" if mean_reverted else "STOP_Z4"
            break
    return {
        "status": "TRADED",
        "direction": direction,
        "entry_signal_index": entry_signal_index,
        "entry_execution_index": entry_execution_index,
        "exit_signal_index": exit_signal_index,
        "exit_execution_index": exit_execution_index,
        "exit_reason": exit_reason,
        "force_close": exit_execution_index is None,
    }


def _adverse_price(mid_price: float, side: str, slippage_bps: float) -> float:
    mid = float(mid_price)
    if mid <= 0:
        raise ValueError("成交中间价必须为正。")
    slippage = float(slippage_bps) / 10_000.0
    if slippage < 0 or slippage >= 1:
        raise ValueError("滑点 bps 无效。")
    normalized_side = str(side).upper()
    if normalized_side == "BUY":
        return mid * (1.0 + slippage)
    if normalized_side == "SELL":
        return mid * (1.0 - slippage)
    raise ValueError(f"不支持的成交方向: {side}")


def _funding_settlement_count(entry_time: int, exit_time: int) -> int:
    if exit_time < entry_time:
        raise ValueError("资金费结束时间早于开始时间。")
    return max(
        0,
        int(exit_time) // FUNDING_INTERVAL_MS
        - int(entry_time) // FUNDING_INTERVAL_MS,
    )


def _leg_pnl(
    entry_side: str,
    quantity: float,
    entry_price: float,
    exit_price: float,
) -> float:
    if str(entry_side).upper() == "BUY":
        return float(quantity) * (float(exit_price) - float(entry_price))
    if str(entry_side).upper() == "SELL":
        return float(quantity) * (float(entry_price) - float(exit_price))
    raise ValueError(f"不支持的持仓方向: {entry_side}")


def _execution_sides(direction: str) -> dict[str, str]:
    if direction == "LONG_SPREAD":
        return {"eth_entry": "BUY", "btc_entry": "SELL"}
    if direction == "SHORT_SPREAD":
        return {"eth_entry": "SELL", "btc_entry": "BUY"}
    raise ValueError(f"不支持的 pair 方向: {direction}")


def _exit_side(entry_side: str) -> str:
    return "SELL" if entry_side == "BUY" else "BUY"


def _mark_to_market_pnl(
    *,
    direction: str,
    eth_quantity: float,
    btc_quantity: float,
    eth_entry_price: float,
    btc_entry_price: float,
    eth_mid: float,
    btc_mid: float,
    taker_fee_rate: float,
    slippage_bps: float,
    entry_fees: float,
    entry_time: int,
    mark_time: int,
) -> float:
    sides = _execution_sides(direction)
    eth_exit_side = _exit_side(sides["eth_entry"])
    btc_exit_side = _exit_side(sides["btc_entry"])
    eth_exit_price = _adverse_price(eth_mid, eth_exit_side, slippage_bps)
    btc_exit_price = _adverse_price(btc_mid, btc_exit_side, slippage_bps)
    gross = _leg_pnl(
        sides["eth_entry"], eth_quantity, eth_entry_price, eth_exit_price
    ) + _leg_pnl(
        sides["btc_entry"], btc_quantity, btc_entry_price, btc_exit_price
    )
    exit_fees = taker_fee_rate * (
        eth_quantity * eth_exit_price + btc_quantity * btc_exit_price
    )
    funding = (
        _funding_settlement_count(entry_time, mark_time)
        * FUNDING_RATE_PER_SETTLEMENT
        * round19.PAIR_GROSS_NOTIONAL
    )
    return gross - entry_fees - exit_fees - funding


def _simulate_pair_window(
    btc: Any,
    eth: Any,
    *,
    taker_fee_rate: float,
    slippage_bps: float,
) -> dict[str, Any]:
    if btc.window_id != eth.window_id:
        raise ValueError("BTC/ETH window_id 不一致。")
    if taker_fee_rate < 0:
        raise ValueError("Taker fee 不能为负。")
    state = _spread_state(btc, eth)
    z_scores = _z_scores(btc, eth, state)
    plan = _trade_plan(z_scores, observation_rows=int(btc.observation_rows))
    base = {
        "window_id": str(btc.window_id),
        "market_close": btc.market_close.isoformat(),
        "force_close_at": btc.force_close_at.isoformat(),
        "beta": float(state["beta"]),
        "spread_mean": float(state["spread_mean"]),
        "spread_std": float(state["spread_std"]),
        "status": str(plan["status"]),
        "trade_count": 0,
        "gross_pnl": 0.0,
        "fees_paid": 0.0,
        "funding_paid": 0.0,
        "net_pnl": 0.0,
        "minimum_path_pnl": 0.0,
        "causal_execution": True,
        "flat_at_end": True,
    }
    if plan["status"] == "NO_ENTRY":
        return base

    direction = str(plan["direction"])
    entry_signal_index = int(plan["entry_signal_index"])
    entry_execution_index = int(plan["entry_execution_index"])
    entry_btc = btc.rows[entry_execution_index]
    entry_eth = eth.rows[entry_execution_index]
    if int(entry_btc.open_time) <= int(btc.rows[entry_signal_index].close_time):
        raise RuntimeError("入场成交没有严格晚于已闭合信号 K 线。")

    beta = float(state["beta"])
    eth_notional = round19.PAIR_GROSS_NOTIONAL / (1.0 + beta)
    btc_notional = beta * eth_notional
    eth_quantity = eth_notional / float(entry_eth.open)
    btc_quantity = btc_notional / float(entry_btc.open)
    sides = _execution_sides(direction)
    eth_entry_price = _adverse_price(
        float(entry_eth.open), sides["eth_entry"], slippage_bps
    )
    btc_entry_price = _adverse_price(
        float(entry_btc.open), sides["btc_entry"], slippage_bps
    )
    entry_fees = taker_fee_rate * (
        eth_quantity * eth_entry_price + btc_quantity * btc_entry_price
    )

    if bool(plan["force_close"]):
        exit_signal_index = len(btc.rows) - 1
        exit_execution_index = len(btc.rows) - 1
        exit_btc_mid = float(btc.rows[-1].close)
        exit_eth_mid = float(eth.rows[-1].close)
        exit_time = int(btc.rows[-1].close_time)
    else:
        exit_signal_index = int(plan["exit_signal_index"])
        exit_execution_index = int(plan["exit_execution_index"])
        if int(btc.rows[exit_execution_index].open_time) <= int(
            btc.rows[exit_signal_index].close_time
        ):
            raise RuntimeError("离场成交没有严格晚于已闭合信号 K 线。")
        exit_btc_mid = float(btc.rows[exit_execution_index].open)
        exit_eth_mid = float(eth.rows[exit_execution_index].open)
        exit_time = int(btc.rows[exit_execution_index].open_time)

    eth_exit_side = _exit_side(sides["eth_entry"])
    btc_exit_side = _exit_side(sides["btc_entry"])
    eth_exit_price = _adverse_price(exit_eth_mid, eth_exit_side, slippage_bps)
    btc_exit_price = _adverse_price(exit_btc_mid, btc_exit_side, slippage_bps)
    gross_pnl = _leg_pnl(
        sides["eth_entry"], eth_quantity, eth_entry_price, eth_exit_price
    ) + _leg_pnl(
        sides["btc_entry"], btc_quantity, btc_entry_price, btc_exit_price
    )
    exit_fees = taker_fee_rate * (
        eth_quantity * eth_exit_price + btc_quantity * btc_exit_price
    )
    entry_time = int(entry_btc.open_time)
    funding_settlements = _funding_settlement_count(entry_time, exit_time)
    funding_paid = (
        funding_settlements
        * FUNDING_RATE_PER_SETTLEMENT
        * round19.PAIR_GROSS_NOTIONAL
    )
    net_pnl = gross_pnl - entry_fees - exit_fees - funding_paid

    marks = [0.0]
    mark_end = min(exit_execution_index, len(btc.rows) - 1)
    for index in range(entry_execution_index, mark_end + 1):
        marks.append(
            _mark_to_market_pnl(
                direction=direction,
                eth_quantity=eth_quantity,
                btc_quantity=btc_quantity,
                eth_entry_price=eth_entry_price,
                btc_entry_price=btc_entry_price,
                eth_mid=float(eth.rows[index].close),
                btc_mid=float(btc.rows[index].close),
                taker_fee_rate=taker_fee_rate,
                slippage_bps=slippage_bps,
                entry_fees=entry_fees,
                entry_time=entry_time,
                mark_time=int(btc.rows[index].close_time),
            )
        )
    marks.append(net_pnl)
    return {
        **base,
        "status": "TRADED",
        "trade_count": 1,
        "direction": direction,
        "entry_signal_index": entry_signal_index,
        "entry_execution_index": entry_execution_index,
        "entry_signal_close_time": int(btc.rows[entry_signal_index].close_time),
        "entry_execution_time": entry_time,
        "entry_z": float(z_scores[entry_signal_index]),
        "exit_signal_index": exit_signal_index,
        "exit_execution_index": exit_execution_index,
        "exit_reason": str(plan["exit_reason"]),
        "exit_signal_close_time": int(btc.rows[exit_signal_index].close_time),
        "exit_execution_time": exit_time,
        "exit_z": float(z_scores[exit_signal_index]),
        "holding_minutes": max(0.0, (exit_time - entry_time) / 60_000.0),
        "eth_notional": eth_notional,
        "btc_notional": btc_notional,
        "gross_notional": eth_notional + btc_notional,
        "eth_quantity": eth_quantity,
        "btc_quantity": btc_quantity,
        "gross_pnl": gross_pnl,
        "entry_fees": entry_fees,
        "exit_fees": exit_fees,
        "fees_paid": entry_fees + exit_fees,
        "funding_settlements": funding_settlements,
        "funding_paid": funding_paid,
        "net_pnl": net_pnl,
        "minimum_path_pnl": min(marks),
        "causal_execution": (
            entry_time > int(btc.rows[entry_signal_index].close_time)
            and (
                bool(plan["force_close"])
                or exit_time > int(btc.rows[exit_signal_index].close_time)
            )
        ),
        "flat_at_end": True,
    }


def _profit_factor(gains: float, losses: float) -> float | None:
    return None if losses <= 0 else gains / losses


def _cell_metrics(
    windows: Sequence[Mapping[str, Any]],
    *,
    authorized_window_count: int,
) -> dict[str, Any]:
    if not windows:
        raise ValueError("Round 20 cell 没有完整窗口。")
    ordered = sorted(windows, key=lambda item: str(item["market_close"]))
    if authorized_window_count < len(ordered):
        raise ValueError("授权窗口数量少于完整窗口数量。")
    pnl_values = [float(item["net_pnl"]) for item in ordered]
    positive = [value for value in pnl_values if value > 0]
    negative = [value for value in pnl_values if value < 0]
    gains = sum(positive)
    losses = -sum(negative)
    total_pnl = sum(pnl_values)
    profit_factor = _profit_factor(gains, losses)
    trade_count = sum(int(item["trade_count"]) for item in ordered)
    data_coverage = len(ordered) / authorized_window_count
    trade_coverage = trade_count / authorized_window_count

    equity = round19.PAIR_GROSS_NOTIONAL
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
    traded = [item for item in ordered if int(item["trade_count"]) == 1]
    exit_reasons = Counter(str(item.get("exit_reason")) for item in traded)
    metrics = {
        "authorized_window_count": authorized_window_count,
        "complete_window_count": len(ordered),
        "trade_count": trade_count,
        "data_coverage": data_coverage,
        "trade_coverage": trade_coverage,
        "total_pnl": total_pnl,
        "mean_window_pnl": statistics.fmean(pnl_values),
        "median_window_pnl": statistics.median(pnl_values),
        "positive_window_count": len(positive),
        "negative_window_count": len(negative),
        "gross_profit": gains,
        "gross_loss": losses,
        "profit_factor": profit_factor,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "best_window_concentration": concentration,
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
        "funding_paid": sum(float(item["funding_paid"]) for item in ordered),
        "minimum_beta": min(float(item["beta"]) for item in ordered),
        "maximum_beta": max(float(item["beta"]) for item in ordered),
        "mean_holding_minutes": (
            statistics.fmean(float(item["holding_minutes"]) for item in traded)
            if traded
            else 0.0
        ),
        "exit_reasons": dict(sorted(exit_reasons.items())),
        "ending_equity": equity,
    }
    checks = {
        "total_pnl_positive": total_pnl > 0,
        "profit_factor_gt_1": (
            total_pnl > 0 if profit_factor is None else profit_factor > 1.0
        ),
        "max_drawdown_le_5pct": maximum_drawdown_pct <= 0.05,
        "best_window_concentration_le_35pct": concentration <= 0.35,
        "trade_coverage_ge_25pct": trade_coverage >= 0.25,
        "data_coverage_ge_99pct": data_coverage >= 0.99,
        "all_beta_and_spread_states_valid": all(
            math.isfinite(float(item["beta"]))
            and float(item["beta"]) > 0
            and math.isfinite(float(item["spread_std"]))
            and float(item["spread_std"]) > 0
            for item in ordered
        ),
        "all_trades_causal": all(bool(item["causal_execution"]) for item in traded),
        "all_trades_flat_at_end": all(bool(item["flat_at_end"]) for item in traded),
        "at_most_one_trade_per_window": all(
            int(item["trade_count"]) in (0, 1) for item in ordered
        ),
    }
    return {
        "metrics": metrics,
        "checks": checks,
        "passed": all(checks.values()),
        "windows": list(ordered),
    }


def _evaluate_cells(datasets: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    cells: dict[str, Any] = {}
    for role, dataset in datasets.items():
        for split_name, pairs in dataset["pairs_by_split"].items():
            authorized_count = int(dataset["authorized_counts"][split_name])
            for scenario, cost in asset_audit.SCENARIOS.items():
                _maker_fee, taker_fee, slippage_bps = cost
                windows = [
                    _simulate_pair_window(
                        btc,
                        eth,
                        taker_fee_rate=float(taker_fee),
                        slippage_bps=float(slippage_bps),
                    )
                    for btc, eth in pairs
                ]
                cell_name = f"{role}_{split_name.upper()}_{scenario}"
                cells[cell_name] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "taker_fee_rate": float(taker_fee),
                    "slippage_bps": float(slippage_bps),
                    **_cell_metrics(
                        windows,
                        authorized_window_count=authorized_count,
                    ),
                }
    return cells


def _selection_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    if len(cells) != 8:
        raise RuntimeError(f"Round 20 单元数量不一致: {len(cells)} != 8")
    metrics = [item["metrics"] for item in cells.values()]
    return {
        "cell_count": len(cells),
        "passed_cell_count": sum(bool(item["passed"]) for item in cells.values()),
        "all_cells_passed": all(bool(item["passed"]) for item in cells.values()),
        "minimum_cell_total_pnl": min(float(item["total_pnl"]) for item in metrics),
        "minimum_trade_coverage": min(
            float(item["trade_coverage"]) for item in metrics
        ),
        "maximum_drawdown_pct": max(
            float(item["maximum_drawdown_pct"]) for item in metrics
        ),
        "maximum_best_window_concentration": max(
            float(item["best_window_concentration"]) for item in metrics
        ),
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 20：BTC/ETH β 中性 Z-score Taker Phase A 结果",
        "",
        "固定 180 分钟观察、2σ 入场、均值退出、4σ 止损、同步 Taker；CURRENT Final OOS 未读取。",
        "",
        "| 单元 | 交易/授权 | 总净收益 | PF | 最大回撤 | 最大集中度 | 覆盖率 | 平均持有 | 通过 | 失败检查 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for cell_name, item in payload["cells"].items():
        metrics = item["metrics"]
        failed = [name for name, passed in item["checks"].items() if not passed]
        pf = metrics["profit_factor"]
        pf_text = "∞" if pf is None and metrics["total_pnl"] > 0 else (
            "N/A" if pf is None else f"{float(pf):.3f}"
        )
        lines.append(
            "| `{cell}` | {trades}/{authorized} | {pnl:.4f} | {pf} | "
            "{drawdown:.2%} | {concentration:.2%} | {coverage:.2%} | "
            "{holding:.1f}m | {passed} | {failed} |".format(
                cell=cell_name,
                trades=metrics["trade_count"],
                authorized=metrics["authorized_window_count"],
                pnl=metrics["total_pnl"],
                pf=pf_text,
                drawdown=metrics["maximum_drawdown_pct"],
                concentration=metrics["best_window_concentration"],
                coverage=metrics["trade_coverage"],
                holding=metrics["mean_holding_minutes"],
                passed="是" if item["passed"] else "否",
                failed=", ".join(failed),
            )
        )
    selection = payload["selection"]
    lines.extend(
        [
            "",
            f"通过单元：{selection['passed_cell_count']}/{selection['cell_count']}。",
            "",
            f"选中候选：{payload['selected_candidate_id'] or '无'}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "生产默认值未修改；没有独立授权文件时 CURRENT Final OOS 继续封存。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 BTC/ETH β 中性 Z-score 同步 Taker 单一候选 Phase A。"
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
        "--round14-result",
        default="reports/cross-era-oos/round14-spot-feasibility-results.json",
    )
    parser.add_argument(
        "--round19-result",
        default="reports/cross-era-oos/round19-cross-asset-spread-upper-bound-results.json",
    )
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def _load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    args = _parser().parse_args()
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 20 协议哈希不一致。")
    if _sha256(Path(round19.__file__).resolve()) != ROUND19_SOURCE_SHA256:
        raise ValueError("Round 19 依赖源码哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    round14_path = Path(args.round14_result).resolve()
    round19_path = Path(args.round19_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        round14_path: round19.round18.ROUND14_RESULT_SHA256,
        round19_path: ROUND19_RESULT_SHA256,
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")

    round12_payload = _load_payload(round12_path)
    round13_payload = _load_payload(round13_path)
    round14_payload = _load_payload(round14_path)
    round19_payload = _load_payload(round19_path)
    if not bool(round19_payload.get("formal_round19_preregistration_ready")):
        raise ValueError("Round 19 未允许正式候选注册。")
    if not bool(round19_payload.get("upper_bound_summary", {}).get("all_cells_passed")):
        raise ValueError("Round 19 上界没有通过全部单元。")
    if not str(round19_payload.get("conclusion") or "").startswith(
        "CROSS_ASSET_SPREAD_FAMILY_WORTH_PREREGISTRATION"
    ):
        raise ValueError("Round 19 结论不匹配。")
    if round19_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 19 不应选中生产候选。")
    if round19_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 19 之后 CURRENT Final OOS 已不再封存。")
    if bool(round19_payload.get("final_oos_authorized")):
        raise ValueError("Round 19 之后 Final OOS 被错误授权。")
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")

    base_config = profit_opt._base_research_config()
    if int(base_config.observation_rows) != round19.OBSERVATION_ROWS:
        raise ValueError("基础研究观察长度不再是固定 180。")
    if abs(float(base_config.funding_rate_per_settlement) - FUNDING_RATE_PER_SETTLEMENT) > 1e-12:
        raise ValueError("基础资金费压力假设已变化。")

    datasets: dict[str, dict[str, Any]] = {}

    current_manifests = tuple(str(item["manifest"]) for item in round12_payload["datasets"])
    current_metadata_for_end = [verify_frozen_dataset(path) for path in current_manifests]
    current_end, development_ids, validation_ids, current_isolation = (
        round15._current_authorized_end(current_metadata_for_end, base_config)
    )
    current_metadata, current_windows = round15._load_dataset(
        current_manifests,
        base_config,
        end_time=current_end,
    )
    asset_audit._validate_frozen_dataset(
        _dataset_brief(current_manifests, current_metadata),
        round12_payload["datasets"],
        label="CURRENT",
    )
    current_allowed = development_ids + validation_ids
    if {window.window_id for window in current_windows} != set(current_allowed):
        raise RuntimeError("CURRENT 授权窗口与日历切分不一致。")
    current_pairs, current_pair_audit = round19._pair_windows(
        current_windows,
        current_allowed,
        allowed_skipped_ids=(round19.CURRENT_BOUNDARY_SKIP_ID,),
    )
    current_pair_map = {str(pair[0].window_id): pair for pair in current_pairs}
    datasets["CURRENT"] = {
        "manifests": _dataset_brief(current_manifests, current_metadata),
        "pairs_by_split": {
            "development": [
                current_pair_map[value]
                for value in development_ids
                if value in current_pair_map
            ],
            "validation": [current_pair_map[value] for value in validation_ids],
        },
        "authorized_counts": {
            "development": len(development_ids),
            "validation": len(validation_ids),
        },
        "pair_audit": current_pair_audit,
    }

    prehistory_manifests = tuple(
        str(item["manifest"]) for item in round13_payload["datasets"]
    )
    prehistory_metadata, prehistory_windows = round15._load_dataset(
        prehistory_manifests,
        base_config,
    )
    asset_audit._validate_frozen_dataset(
        _dataset_brief(prehistory_manifests, prehistory_metadata),
        round13_payload["datasets"],
        label="PREHISTORY",
    )
    prehistory_ids = round13._paired_ready_window_ids(prehistory_windows)
    prehistory_pairs, prehistory_pair_audit = round19._pair_windows(
        prehistory_windows,
        prehistory_ids,
    )
    datasets["PREHISTORY"] = {
        "manifests": _dataset_brief(prehistory_manifests, prehistory_metadata),
        "pairs_by_split": {"external": prehistory_pairs},
        "authorized_counts": {"external": len(prehistory_ids)},
        "pair_audit": prehistory_pair_audit,
    }

    spot_manifests = tuple(str(item["manifest"]) for item in round14_payload["datasets"])
    spot_metadata, spot_windows = round15._load_dataset(spot_manifests, base_config)
    asset_audit._validate_frozen_dataset(
        _dataset_brief(spot_manifests, spot_metadata),
        round14_payload["datasets"],
        label="SPOT",
    )
    spot_ids, spot_quality = spot_round._paired_contiguous_window_ids(spot_windows)
    if spot_quality != round14_payload["data_quality"]:
        raise ValueError("Round 14 Spot 连续窗口质量记录不一致。")
    spot_pairs, spot_pair_audit = round19._pair_windows(spot_windows, spot_ids)
    datasets["SPOT"] = {
        "manifests": _dataset_brief(spot_manifests, spot_metadata),
        "pairs_by_split": {"external": spot_pairs},
        "authorized_counts": {"external": len(spot_ids)},
        "pair_audit": spot_pair_audit,
        "data_quality": spot_quality,
    }

    expected_counts = {
        ("CURRENT", "development"): 107,
        ("CURRENT", "validation"): 54,
        ("PREHISTORY", "external"): 28,
        ("SPOT", "external"): 101,
    }
    for (role, split_name), expected in expected_counts.items():
        actual = len(datasets[role]["pairs_by_split"][split_name])
        if actual != expected:
            raise RuntimeError(
                f"{role} {split_name} 完整 pair 数量不一致: {actual} != {expected}"
            )

    cells = _evaluate_cells(datasets)
    selection = _selection_summary(cells)
    eligible = bool(selection["all_cells_passed"])
    selected_candidate_id = CANDIDATE_ID if eligible else None
    conclusion = (
        "CROSS_ASSET_ZSCORE_CANDIDATE_READY_FOR_AUTHORIZATION：唯一注册候选通过全部 8 个 Phase A pair cell；仍需独立授权文件才能运行 Final OOS。"
        if eligible
        else "NO_ROBUST_CROSS_ASSET_ZSCORE_CANDIDATE：唯一注册候选未通过全部 8 个 Phase A pair cell，禁止搜索相邻 Z-score 或执行参数。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "PREREGISTERED_SINGLE_CANDIDATE_PHASE_A",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "candidate_id": CANDIDATE_ID,
        "candidate": {
            "observation_rows": round19.OBSERVATION_ROWS,
            "entry_z": ENTRY_Z,
            "stop_z": STOP_Z,
            "exit_z": 0.0,
            "max_trades_per_window": 1,
            "gross_notional": round19.PAIR_GROSS_NOTIONAL,
            "funding_rate_per_settlement": FUNDING_RATE_PER_SETTLEMENT,
            "execution": "SYNCHRONOUS_TAKER_NEXT_BAR_OPEN",
        },
        "direction_mode": "NEUTRAL",
        "current_isolation": current_isolation,
        "datasets": {
            role: {
                "manifests": item["manifests"],
                "pair_audit": item["pair_audit"],
                "split_counts": {
                    name: len(pairs) for name, pairs in item["pairs_by_split"].items()
                },
                "authorized_counts": item["authorized_counts"],
                **(
                    {"data_quality": item["data_quality"]}
                    if "data_quality" in item
                    else {}
                ),
            }
            for role, item in datasets.items()
        },
        "cells": cells,
        "selection": selection,
        "selected_candidate_id": selected_candidate_id,
        "final_oos_authorization_ready": eligible,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }

    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    result_path = report_dir / "round20-cross-asset-zscore-taker-results.json"
    report_path = report_dir / "round20-cross-asset-zscore-taker-report.md"
    _write_json(result_path, result)
    report_path.write_text(_report_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "report_path": str(report_path.resolve()),
                "passed_cell_count": selection["passed_cell_count"],
                "cell_count": selection["cell_count"],
                "selected_candidate_id": selected_candidate_id,
                "final_oos_authorization_ready": eligible,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
