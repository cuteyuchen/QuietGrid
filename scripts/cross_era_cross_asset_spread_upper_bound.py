from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_cycle_capacity_upper_bound as round18
import scripts.cross_era_long_horizon_regime as round15
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.cross_era_spot_feasibility as spot_round
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_oos import _dataset_brief, _write_json
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256
from scripts.robustness import verify_frozen_dataset


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round19-cross-asset-spread-upper-bound-protocol.md"
)
PROTOCOL_SHA256 = "29903fb5508b2816472b427bb1cbfd2a04c849db8524f996513dc071d93ef99b"
ROUND18_RESULT_SHA256 = "25a2b1d178a9b6072b3e864762b41c51f3d9f7c0f0a9566df88836cf08312818"
PAIR_GROSS_NOTIONAL = 800.0
OBSERVATION_ROWS = 180
CURRENT_BOUNDARY_SKIP_ID = "nyse_20200717T200000Z"


def _observation_beta(
    btc_closes: Sequence[float],
    eth_closes: Sequence[float],
) -> float:
    if len(btc_closes) != len(eth_closes):
        raise ValueError("BTC/ETH 观察序列长度不一致。")
    if len(btc_closes) < 3:
        raise ValueError("β 估计至少需要三根收盘价。")
    if any(float(value) <= 0 for value in (*btc_closes, *eth_closes)):
        raise ValueError("β 估计遇到非正收盘价。")
    btc_returns = [
        math.log(float(btc_closes[index]) / float(btc_closes[index - 1]))
        for index in range(1, len(btc_closes))
    ]
    eth_returns = [
        math.log(float(eth_closes[index]) / float(eth_closes[index - 1]))
        for index in range(1, len(eth_closes))
    ]
    btc_mean = statistics.fmean(btc_returns)
    eth_mean = statistics.fmean(eth_returns)
    variance = sum((value - btc_mean) ** 2 for value in btc_returns)
    if variance <= 1e-24:
        raise ValueError("BTC 观察收益方差不足，无法估计 β。")
    covariance = sum(
        (btc - btc_mean) * (eth - eth_mean)
        for btc, eth in zip(btc_returns, eth_returns)
    )
    beta = covariance / variance
    if not math.isfinite(beta) or beta <= 0:
        raise ValueError(f"观察期 β 必须有限且为正，实际为 {beta!r}。")
    return beta


def _max_causal_excursion(values: Sequence[float]) -> dict[str, Any]:
    if len(values) < 2:
        raise ValueError("价差 oracle 至少需要两个时间点。")
    normalized = [float(value) for value in values]
    if any(not math.isfinite(value) for value in normalized):
        raise ValueError("价差 oracle 遇到非有限值。")
    minimum = normalized[0]
    maximum = normalized[0]
    minimum_index = 0
    maximum_index = 0
    best = {
        "magnitude": -1.0,
        "entry_index": 0,
        "exit_index": 1,
        "direction": "LONG_SPREAD",
    }
    for exit_index in range(1, len(normalized)):
        value = normalized[exit_index]
        candidates = (
            (value - minimum, minimum_index, "LONG_SPREAD"),
            (maximum - value, maximum_index, "SHORT_SPREAD"),
        )
        for magnitude, entry_index, direction in candidates:
            if magnitude > float(best["magnitude"]) + 1e-18:
                best = {
                    "magnitude": max(0.0, float(magnitude)),
                    "entry_index": entry_index,
                    "exit_index": exit_index,
                    "direction": direction,
                }
        if value < minimum:
            minimum = value
            minimum_index = exit_index
        if value > maximum:
            maximum = value
            maximum_index = exit_index
    return best


def _pair_windows(
    windows: Sequence[Any],
    allowed_ids: Sequence[str],
    *,
    allowed_skipped_ids: Sequence[str] = (),
) -> tuple[list[tuple[Any, Any]], dict[str, Any]]:
    requested = tuple(dict.fromkeys(str(value) for value in allowed_ids))
    if not requested:
        raise ValueError("跨资产上界至少需要一个授权窗口。")
    allowed = set(requested)
    allowed_skips = set(str(value) for value in allowed_skipped_ids)
    if not allowed_skips <= allowed:
        raise ValueError("允许跳过的窗口不在授权集合内。")
    indexed: dict[tuple[str, str], Any] = {}
    for window in windows:
        window_id = str(window.window_id)
        if window_id not in allowed:
            continue
        symbol = str(window.symbol).strip().upper()
        key = (window_id, symbol)
        if key in indexed:
            raise RuntimeError(f"窗口重复: {window_id} {symbol}")
        indexed[key] = window

    pairs: list[tuple[Any, Any]] = []
    total_aligned_rows = 0
    observed_skips: dict[str, dict[str, Any]] = {}
    for window_id in requested:
        try:
            btc = indexed[(window_id, "BTCUSDT")]
            eth = indexed[(window_id, "ETHUSDT")]
        except KeyError as exc:
            raise RuntimeError(f"{window_id} 缺少 BTC/ETH 成对窗口。") from exc
        if btc.status != "READY" or eth.status != "READY":
            if (
                window_id in allowed_skips
                and btc.status == "SKIPPED"
                and eth.status == "SKIPPED"
                and btc.skip_reason == eth.skip_reason
            ):
                observed_skips[window_id] = {
                    "btc_status": btc.status,
                    "eth_status": eth.status,
                    "skip_reason": btc.skip_reason,
                    "btc_row_count": len(btc.rows),
                    "eth_row_count": len(eth.rows),
                }
                continue
            raise RuntimeError(f"{window_id} 包含未授权的非 READY 窗口。")
        if int(btc.observation_rows) != OBSERVATION_ROWS:
            raise RuntimeError(f"{window_id} BTC 观察长度不是 {OBSERVATION_ROWS}。")
        if int(eth.observation_rows) != OBSERVATION_ROWS:
            raise RuntimeError(f"{window_id} ETH 观察长度不是 {OBSERVATION_ROWS}。")
        if btc.market_close != eth.market_close or btc.force_close_at != eth.force_close_at:
            raise RuntimeError(f"{window_id} BTC/ETH 窗口边界不一致。")
        if len(btc.rows) != len(eth.rows):
            raise RuntimeError(f"{window_id} BTC/ETH 行数不一致。")
        if len(btc.rows) <= OBSERVATION_ROWS:
            raise RuntimeError(f"{window_id} 没有可交易 K 线。")
        btc_times = tuple(int(row.open_time) for row in btc.rows)
        eth_times = tuple(int(row.open_time) for row in eth.rows)
        if btc_times != eth_times:
            raise RuntimeError(f"{window_id} BTC/ETH 分钟时间戳不一致。")
        total_aligned_rows += len(btc.rows)
        pairs.append((btc, eth))
    if set(observed_skips) != allowed_skips:
        raise RuntimeError(
            f"授权边界 skip 不一致: {sorted(observed_skips)} != {sorted(allowed_skips)}"
        )
    expected_pair_count = len(requested) - len(allowed_skips)
    if len(pairs) != expected_pair_count:
        raise RuntimeError("跨资产成对窗口数量不完整。")
    return pairs, {
        "authorized_window_count": len(requested),
        "window_count": len(pairs),
        "symbol_window_count": len(pairs) * 2,
        "aligned_row_count_per_symbol": total_aligned_rows,
        "observation_rows": OBSERVATION_ROWS,
        "skipped_window_count": len(observed_skips),
        "skipped_windows": observed_skips,
        "data_coverage": len(pairs) / len(requested),
        "all_pairs_ready": True,
        "all_timestamps_aligned": True,
        "passed": True,
    }


def _evaluate_pair_window(
    btc: Any,
    eth: Any,
    *,
    maker_fee_rate: float,
) -> dict[str, Any]:
    if maker_fee_rate < 0:
        raise ValueError("Maker fee 不能为负。")
    if btc.window_id != eth.window_id:
        raise ValueError("BTC/ETH window_id 不一致。")
    observation_rows = int(btc.observation_rows)
    if observation_rows != OBSERVATION_ROWS or int(eth.observation_rows) != OBSERVATION_ROWS:
        raise ValueError("跨资产上界观察长度不一致。")
    btc_observation = [float(row.close) for row in btc.rows[:observation_rows]]
    eth_observation = [float(row.close) for row in eth.rows[:observation_rows]]
    beta = _observation_beta(btc_observation, eth_observation)

    btc_path = btc.rows[observation_rows - 1 :]
    eth_path = eth.rows[observation_rows - 1 :]
    if len(btc_path) != len(eth_path) or len(btc_path) < 2:
        raise RuntimeError(f"{btc.window_id} oracle 路径不完整。")
    timestamps = tuple(int(row.close_time) for row in btc_path)
    if timestamps != tuple(int(row.close_time) for row in eth_path):
        raise RuntimeError(f"{btc.window_id} BTC/ETH close_time 不一致。")
    spreads = [
        math.log(float(eth_row.close)) - beta * math.log(float(btc_row.close))
        for btc_row, eth_row in zip(btc_path, eth_path)
    ]
    oracle = _max_causal_excursion(spreads)
    q = PAIR_GROSS_NOTIONAL / (1.0 + beta)
    eth_notional = q
    btc_notional = beta * q
    gross_pnl = q * float(oracle["magnitude"])
    entry_fee = maker_fee_rate * PAIR_GROSS_NOTIONAL
    exit_fee = maker_fee_rate * PAIR_GROSS_NOTIONAL
    net_pnl = gross_pnl - entry_fee - exit_fee
    entry_index = int(oracle["entry_index"])
    exit_index = int(oracle["exit_index"])
    if entry_index >= exit_index:
        raise RuntimeError("价差 oracle 产生了非因果进出点。")
    return {
        "window_id": str(btc.window_id),
        "market_close": btc.market_close.isoformat(),
        "force_close_at": btc.force_close_at.isoformat(),
        "beta": beta,
        "eth_notional": eth_notional,
        "btc_notional": btc_notional,
        "gross_notional": eth_notional + btc_notional,
        "direction": str(oracle["direction"]),
        "spread_excursion": float(oracle["magnitude"]),
        "entry_close_time": timestamps[entry_index],
        "exit_close_time": timestamps[exit_index],
        "observation_last_close_time": int(btc.rows[observation_rows - 1].close_time),
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees_paid": entry_fee + exit_fee,
        "net_pnl": net_pnl,
        "trade_count": 1,
    }


def _profit_factor(gains: float, losses: float) -> float | None:
    if losses <= 0:
        return None
    return gains / losses


def _cell_metrics(
    trades: Sequence[Mapping[str, Any]],
    *,
    authorized_window_count: int | None = None,
) -> dict[str, Any]:
    if not trades:
        raise ValueError("pair cell 没有交易窗口。")
    ordered = sorted(trades, key=lambda item: str(item["market_close"]))
    authorized_count = (
        len(ordered)
        if authorized_window_count is None
        else int(authorized_window_count)
    )
    if authorized_count < len(ordered):
        raise ValueError("授权窗口数量不能少于完整交易窗口数量。")
    pnl_values = [float(item["net_pnl"]) for item in ordered]
    positive_values = [value for value in pnl_values if value > 0]
    negative_values = [value for value in pnl_values if value < 0]
    gains = sum(positive_values)
    losses = -sum(negative_values)
    total_pnl = sum(pnl_values)
    profit_factor = _profit_factor(gains, losses)

    equity = PAIR_GROSS_NOTIONAL
    peak = equity
    maximum_drawdown_pct = 0.0
    for item in ordered:
        equity -= float(item["entry_fee"])
        maximum_drawdown_pct = max(
            maximum_drawdown_pct,
            (peak - equity) / max(peak, 1e-12),
        )
        equity += float(item["gross_pnl"]) - float(item["exit_fee"])
        peak = max(peak, equity)
        maximum_drawdown_pct = max(
            maximum_drawdown_pct,
            (peak - equity) / max(peak, 1e-12),
        )

    best_window_concentration = (
        max(positive_values) / gains if positive_values and gains > 0 else 1.0
    )
    positive_window_ratio = len(positive_values) / authorized_count
    data_coverage = len(ordered) / authorized_count
    betas = [float(item["beta"]) for item in ordered]
    metrics = {
        "authorized_window_count": authorized_count,
        "window_count": len(ordered),
        "trade_count": sum(int(item["trade_count"]) for item in ordered),
        "total_pnl": total_pnl,
        "mean_window_pnl": statistics.fmean(pnl_values),
        "median_window_pnl": statistics.median(pnl_values),
        "positive_window_count": len(positive_values),
        "negative_window_count": len(negative_values),
        "positive_window_ratio": positive_window_ratio,
        "data_coverage": data_coverage,
        "gross_profit": gains,
        "gross_loss": losses,
        "profit_factor": profit_factor,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "best_window_concentration": best_window_concentration,
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
        "minimum_beta": min(betas),
        "maximum_beta": max(betas),
        "mean_beta": statistics.fmean(betas),
        "ending_equity": equity,
    }
    checks = {
        "total_pnl_positive": total_pnl > 0,
        "profit_factor_gt_1": (
            total_pnl > 0 if profit_factor is None else profit_factor > 1.0
        ),
        "max_drawdown_le_5pct": maximum_drawdown_pct <= 0.05,
        "best_window_concentration_le_35pct": best_window_concentration <= 0.35,
        "positive_window_ratio_ge_25pct": positive_window_ratio >= 0.25,
        "data_coverage_ge_99pct": data_coverage >= 0.99,
        "all_windows_have_valid_beta": len(betas) == len(ordered)
        and all(math.isfinite(value) and value > 0 for value in betas),
        "one_trade_per_ready_window": metrics["trade_count"] == metrics["window_count"],
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
            authorized_window_count = int(dataset["authorized_counts"][split_name])
            for scenario, cost in asset_audit.SCENARIOS.items():
                maker_fee_rate = float(cost[0])
                trades = [
                    _evaluate_pair_window(
                        btc,
                        eth,
                        maker_fee_rate=maker_fee_rate,
                    )
                    for btc, eth in pairs
                ]
                cell_name = f"{role}_{split_name.upper()}_{scenario}"
                cells[cell_name] = {
                    "role": role,
                    "split": split_name,
                    "scenario": scenario,
                    "maker_fee_rate": maker_fee_rate,
                    **_cell_metrics(
                        trades,
                        authorized_window_count=authorized_window_count,
                    ),
                }
    return cells


def _upper_bound_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    if len(cells) != 8:
        raise RuntimeError(f"跨资产上界单元数量不一致: {len(cells)} != 8")
    metrics = [item["metrics"] for item in cells.values()]
    return {
        "cell_count": len(cells),
        "passed_cell_count": sum(bool(item["passed"]) for item in cells.values()),
        "all_cells_passed": all(bool(item["passed"]) for item in cells.values()),
        "minimum_cell_total_pnl": min(float(item["total_pnl"]) for item in metrics),
        "minimum_positive_window_ratio": min(
            float(item["positive_window_ratio"]) for item in metrics
        ),
        "maximum_drawdown_pct": max(
            float(item["maximum_drawdown_pct"]) for item in metrics
        ),
        "maximum_best_window_concentration": max(
            float(item["best_window_concentration"]) for item in metrics
        ),
        "minimum_beta": min(float(item["minimum_beta"]) for item in metrics),
        "maximum_beta": max(float(item["maximum_beta"]) for item in metrics),
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 19：跨资产 β 中性价差反事实上界结果",
        "",
        "每个窗口使用观察期 β，并允许 oracle 事后选择连续对数价差的最优方向和进出点；该结构不可部署。",
        "",
        "| 单元 | 窗口 | 总净收益 | PF | 最大回撤 | 最大集中度 | 正收益窗口 | β 范围 | 通过 | 失败检查 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for cell_name, item in payload["cells"].items():
        metrics = item["metrics"]
        failed = [name for name, passed in item["checks"].items() if not passed]
        profit_factor = metrics["profit_factor"]
        pf_text = "∞" if profit_factor is None and metrics["total_pnl"] > 0 else (
            "N/A" if profit_factor is None else f"{float(profit_factor):.3f}"
        )
        lines.append(
            "| `{cell}` | {windows}/{authorized} | {pnl:.4f} | {pf} | {drawdown:.2%} | "
            "{concentration:.2%} | {positive:.2%} | {min_beta:.3f}–{max_beta:.3f} | "
            "{passed} | {failed} |".format(
                cell=cell_name,
                windows=metrics["window_count"],
                authorized=metrics["authorized_window_count"],
                pnl=metrics["total_pnl"],
                pf=pf_text,
                drawdown=metrics["maximum_drawdown_pct"],
                concentration=metrics["best_window_concentration"],
                positive=metrics["positive_window_ratio"],
                min_beta=metrics["minimum_beta"],
                max_beta=metrics["maximum_beta"],
                passed="是" if item["passed"] else "否",
                failed=", ".join(failed),
            )
        )
    summary = payload["upper_bound_summary"]
    lines.extend(
        [
            "",
            f"通过单元：{summary['passed_cell_count']}/{summary['cell_count']}。",
            "",
            f"结论：{payload['conclusion']}",
            "",
            "CURRENT Final OOS 未读取；没有注册候选；生产默认值未修改。",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估 BTC/ETH β 中性连续对数价差的不可部署反事实上界。"
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
        "--round18-result",
        default="reports/cross-era-oos/round18-cycle-capacity-upper-bound-results.json",
    )
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def _load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    args = _parser().parse_args()
    protocol_path = PROTOCOL_PATH.resolve()
    if _sha256(protocol_path) != PROTOCOL_SHA256:
        raise ValueError("Round 19 跨资产上界协议哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    round14_path = Path(args.round14_result).resolve()
    round18_path = Path(args.round18_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        round14_path: round18.ROUND14_RESULT_SHA256,
        round18_path: ROUND18_RESULT_SHA256,
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")

    round12_payload = _load_payload(round12_path)
    round13_payload = _load_payload(round13_path)
    round14_payload = _load_payload(round14_path)
    round18_payload = _load_payload(round18_path)
    if round18_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 18 之后 CURRENT Final OOS 已不再封存。")
    if bool(round18_payload.get("final_oos_authorized")):
        raise ValueError("Round 18 之后 CURRENT Final OOS 被错误授权。")
    if round18_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 18 已存在候选，不能启动跨资产上界。")
    if bool(round18_payload.get("formal_round18_preregistration_ready")):
        raise ValueError("Round 18 已允许正式注册，跨资产上界前置条件不匹配。")
    if not str(round18_payload.get("conclusion") or "").startswith(
        "NO_PREREGISTERED_CYCLE_CAPACITY_CANDIDATE"
    ):
        raise ValueError("Round 18 失败结论不匹配。")
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")
    if bool(round12_payload.get("final_oos_authorized")):
        raise ValueError("CURRENT Final OOS 被错误授权。")

    base_config = profit_opt._base_research_config()
    if int(base_config.observation_rows) != OBSERVATION_ROWS:
        raise ValueError("基础研究观察长度不再是固定 180。")

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
    current_pairs, current_pair_audit = _pair_windows(
        current_windows,
        current_allowed,
        allowed_skipped_ids=(CURRENT_BOUNDARY_SKIP_ID,),
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
    prehistory_pairs, prehistory_pair_audit = _pair_windows(
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
    spot_pairs, spot_pair_audit = _pair_windows(spot_windows, spot_ids)
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
                f"{role} {split_name} 成对窗口数量不一致: {actual} != {expected}"
            )

    cells = _evaluate_cells(datasets)
    upper_bound = _upper_bound_summary(cells)
    family_ready = bool(upper_bound["all_cells_passed"])
    conclusion = (
        "CROSS_ASSET_SPREAD_FAMILY_WORTH_PREREGISTRATION：8/8 个跨年代 pair cell 均通过不可部署反事实上界；仅允许另写单一可部署候选协议。"
        if family_ready
        else "NO_PREREGISTERED_CROSS_ASSET_SPREAD_CANDIDATE：至少一个 pair cell 在完美方向、完美进出和同步 Maker 假设下仍失败，排除本协议定义的跨资产价差家族。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "NON_DEPLOYABLE_COUNTERFACTUAL_UPPER_BOUND",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "direction_mode": "NEUTRAL",
        "pair_gross_notional": PAIR_GROSS_NOTIONAL,
        "observation_rows": OBSERVATION_ROWS,
        "oracle_uses_future_path": True,
        "oracle_is_deployable": False,
        "continuous_log_contract_is_deployable": False,
        "ignored_costs": [
            "maker_queue_failure",
            "leg_latency",
            "rebalancing_turnover",
            "funding",
            "taker_fee",
            "slippage",
        ],
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
        "upper_bound_summary": upper_bound,
        "formal_round19_preregistration_ready": family_ready,
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
    result_path = report_dir / "round19-cross-asset-spread-upper-bound-results.json"
    report_path = report_dir / "round19-cross-asset-spread-upper-bound-report.md"
    _write_json(result_path, result)
    report_path.write_text(_report_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "report_path": str(report_path.resolve()),
                "passed_cell_count": upper_bound["passed_cell_count"],
                "cell_count": upper_bound["cell_count"],
                "formal_round19_preregistration_ready": family_ready,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
