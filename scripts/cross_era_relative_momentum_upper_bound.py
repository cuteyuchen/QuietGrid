from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_cross_asset_spread_upper_bound as round19
import scripts.cross_era_cross_asset_zscore_taker as round20
import scripts.cross_era_long_horizon_regime as round15
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.cross_era_spot_feasibility as spot_round
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_oos import _dataset_brief, _write_json
from scripts.cross_era_round13_diagnose import ROUND13_RESULT_SHA256, _sha256
from scripts.robustness import verify_frozen_dataset


UTC = timezone.utc
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round21-relative-momentum-upper-bound-protocol.md"
)
PROTOCOL_SHA256 = "4c25fbb1980a31a34b30ea940ccd96603500d04e28af72efcb2f79dec61f0351"
ROUND20_RESULT_SHA256 = "d57b021867c59f290ca68d8c79250bafc23e8efd62a3e38d280ff8a247afc63b"
ROUND20_SOURCE_SHA256 = "1ea9e41ff1fc0e9c1ecb1e4d0bf18b2b03d87cf2f71c8ed8c9e1a48d104c86bf"


def _relative_momentum_trade(
    btc: Any,
    eth: Any,
    *,
    maker_fee_rate: float,
) -> dict[str, Any]:
    if btc.window_id != eth.window_id:
        raise ValueError("BTC/ETH window_id 不一致。")
    observation_rows = int(btc.observation_rows)
    if observation_rows != round19.OBSERVATION_ROWS:
        raise ValueError("观察长度必须固定为 180。")
    if int(eth.observation_rows) != observation_rows:
        raise ValueError("BTC/ETH 观察长度不一致。")
    btc_observation = [float(row.close) for row in btc.rows[:observation_rows]]
    eth_observation = [float(row.close) for row in eth.rows[:observation_rows]]
    beta = round19._observation_beta(btc_observation, eth_observation)
    observation_spreads = [
        math.log(eth_close) - beta * math.log(btc_close)
        for btc_close, eth_close in zip(btc_observation, eth_observation)
    ]
    momentum = observation_spreads[-1] - observation_spreads[0]
    if not math.isfinite(momentum) or abs(momentum) <= 1e-18:
        raise ValueError(f"{btc.window_id} 观察期相对动量无效: {momentum!r}")
    direction = "LONG_SPREAD" if momentum > 0 else "SHORT_SPREAD"
    direction_sign = 1.0 if momentum > 0 else -1.0

    btc_path = btc.rows[observation_rows - 1 :]
    eth_path = eth.rows[observation_rows - 1 :]
    if len(btc_path) != len(eth_path) or len(btc_path) < 2:
        raise RuntimeError(f"{btc.window_id} 相对动量路径不完整。")
    timestamps = tuple(int(row.close_time) for row in btc_path)
    if timestamps != tuple(int(row.close_time) for row in eth_path):
        raise RuntimeError(f"{btc.window_id} BTC/ETH close_time 不一致。")
    spreads = [
        math.log(float(eth_row.close)) - beta * math.log(float(btc_row.close))
        for btc_row, eth_row in zip(btc_path, eth_path)
    ]
    entry_spread = spreads[0]
    signed_moves = [
        direction_sign * (value - entry_spread)
        for value in spreads[1:]
    ]
    best_offset, best_move = max(
        enumerate(signed_moves, start=1),
        key=lambda item: (float(item[1]), -int(item[0])),
    )
    q = round19.PAIR_GROSS_NOTIONAL / (1.0 + beta)
    gross_pnl = q * float(best_move)
    entry_fee = maker_fee_rate * round19.PAIR_GROSS_NOTIONAL
    exit_fee = maker_fee_rate * round19.PAIR_GROSS_NOTIONAL
    net_pnl = gross_pnl - entry_fee - exit_fee
    path_to_exit = [0.0, *signed_moves[:best_offset]]
    minimum_path_pnl = q * min(path_to_exit) - entry_fee - exit_fee
    return {
        "window_id": str(btc.window_id),
        "market_close": btc.market_close.isoformat(),
        "force_close_at": btc.force_close_at.isoformat(),
        "beta": beta,
        "momentum": momentum,
        "direction": direction,
        "direction_fixed_from_observation": True,
        "entry_close_time": timestamps[0],
        "exit_close_time": timestamps[best_offset],
        "exit_index": best_offset,
        "gross_notional": round19.PAIR_GROSS_NOTIONAL,
        "eth_notional": q,
        "btc_notional": beta * q,
        "signed_spread_move": float(best_move),
        "gross_pnl": gross_pnl,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "fees_paid": entry_fee + exit_fee,
        "net_pnl": net_pnl,
        "minimum_path_pnl": minimum_path_pnl,
        "trade_count": 1,
        "causal_exit": timestamps[best_offset] > timestamps[0],
    }


def _profit_factor(gains: float, losses: float) -> float | None:
    return None if losses <= 0 else gains / losses


def _cell_metrics(
    trades: Sequence[Mapping[str, Any]],
    *,
    authorized_window_count: int,
) -> dict[str, Any]:
    if not trades:
        raise ValueError("Round 21 cell 没有交易。")
    ordered = sorted(trades, key=lambda item: str(item["market_close"]))
    if authorized_window_count < len(ordered):
        raise ValueError("授权窗口数量少于完整窗口数量。")
    pnl_values = [float(item["net_pnl"]) for item in ordered]
    positive = [value for value in pnl_values if value > 0]
    negative = [value for value in pnl_values if value < 0]
    gains = sum(positive)
    losses = -sum(negative)
    total_pnl = sum(pnl_values)
    profit_factor = _profit_factor(gains, losses)

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
    positive_window_ratio = len(positive) / authorized_window_count
    data_coverage = len(ordered) / authorized_window_count
    metrics = {
        "authorized_window_count": authorized_window_count,
        "window_count": len(ordered),
        "trade_count": sum(int(item["trade_count"]) for item in ordered),
        "data_coverage": data_coverage,
        "positive_window_count": len(positive),
        "negative_window_count": len(negative),
        "positive_window_ratio": positive_window_ratio,
        "total_pnl": total_pnl,
        "mean_window_pnl": statistics.fmean(pnl_values),
        "median_window_pnl": statistics.median(pnl_values),
        "gross_profit": gains,
        "gross_loss": losses,
        "profit_factor": profit_factor,
        "maximum_drawdown_pct": maximum_drawdown_pct,
        "best_window_concentration": concentration,
        "fees_paid": sum(float(item["fees_paid"]) for item in ordered),
        "minimum_beta": min(float(item["beta"]) for item in ordered),
        "maximum_beta": max(float(item["beta"]) for item in ordered),
        "long_spread_count": sum(
            str(item["direction"]) == "LONG_SPREAD" for item in ordered
        ),
        "short_spread_count": sum(
            str(item["direction"]) == "SHORT_SPREAD" for item in ordered
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
        "positive_window_ratio_ge_25pct": positive_window_ratio >= 0.25,
        "data_coverage_ge_99pct": data_coverage >= 0.99,
        "all_observation_states_valid": all(
            math.isfinite(float(item["beta"]))
            and float(item["beta"]) > 0
            and math.isfinite(float(item["momentum"]))
            and abs(float(item["momentum"])) > 0
            for item in ordered
        ),
        "all_directions_observation_fixed": all(
            bool(item["direction_fixed_from_observation"]) for item in ordered
        ),
        "all_exits_causal": all(bool(item["causal_exit"]) for item in ordered),
        "one_trade_per_ready_window": metrics["trade_count"] == len(ordered),
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
                maker_fee_rate = float(cost[0])
                trades = [
                    _relative_momentum_trade(
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
                        authorized_window_count=authorized_count,
                    ),
                }
    return cells


def _upper_bound_summary(cells: Mapping[str, Any]) -> dict[str, Any]:
    if len(cells) != 8:
        raise RuntimeError(f"Round 21 单元数量不一致: {len(cells)} != 8")
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
    }


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 21：BTC/ETH 观察期相对动量方向上界结果",
        "",
        "方向只由前 180 分钟观察期决定；oracle 仅事后选择固定方向的最优离场点。该结果不可部署。",
        "",
        "| 单元 | 窗口 | 总净收益 | PF | 最大回撤 | 最大集中度 | 正收益窗口 | Long/Short | 通过 | 失败检查 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for cell_name, item in payload["cells"].items():
        metrics = item["metrics"]
        failed = [name for name, passed in item["checks"].items() if not passed]
        pf = metrics["profit_factor"]
        pf_text = "∞" if pf is None and metrics["total_pnl"] > 0 else (
            "N/A" if pf is None else f"{float(pf):.3f}"
        )
        lines.append(
            "| `{cell}` | {windows}/{authorized} | {pnl:.4f} | {pf} | "
            "{drawdown:.2%} | {concentration:.2%} | {positive:.2%} | "
            "{long_count}/{short_count} | {passed} | {failed} |".format(
                cell=cell_name,
                windows=metrics["window_count"],
                authorized=metrics["authorized_window_count"],
                pnl=metrics["total_pnl"],
                pf=pf_text,
                drawdown=metrics["maximum_drawdown_pct"],
                concentration=metrics["best_window_concentration"],
                positive=metrics["positive_window_ratio"],
                long_count=metrics["long_spread_count"],
                short_count=metrics["short_spread_count"],
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
        description="评估 BTC/ETH 观察期相对动量固定方向的不可部署离场上界。"
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
        "--round20-result",
        default="reports/cross-era-oos/round20-cross-asset-zscore-taker-results.json",
    )
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def _load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    args = _parser().parse_args()
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 21 协议哈希不一致。")
    if _sha256(Path(round20.__file__).resolve()) != ROUND20_SOURCE_SHA256:
        raise ValueError("Round 20 依赖源码哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    round13_path = Path(args.round13_result).resolve()
    round14_path = Path(args.round14_result).resolve()
    round20_path = Path(args.round20_result).resolve()
    expected_hashes = {
        round12_path: asset_audit.ROUND12_RESULT_SHA256,
        round13_path: ROUND13_RESULT_SHA256,
        round14_path: round19.round18.ROUND14_RESULT_SHA256,
        round20_path: ROUND20_RESULT_SHA256,
    }
    for path, expected in expected_hashes.items():
        if _sha256(path) != expected:
            raise ValueError(f"冻结输入哈希不一致: {path}")

    round12_payload = _load_payload(round12_path)
    round13_payload = _load_payload(round13_path)
    round14_payload = _load_payload(round14_path)
    round20_payload = _load_payload(round20_path)
    if round20_payload.get("selected_candidate_id") is not None:
        raise ValueError("Round 20 不应存在已选候选。")
    if bool(round20_payload.get("final_oos_authorization_ready")):
        raise ValueError("Round 20 不应允许 Final OOS 授权。")
    if not str(round20_payload.get("conclusion") or "").startswith(
        "NO_ROBUST_CROSS_ASSET_ZSCORE_CANDIDATE"
    ):
        raise ValueError("Round 20 失败结论不匹配。")
    if round20_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 20 之后 CURRENT Final OOS 已不再封存。")
    if bool(round20_payload.get("final_oos_authorized")):
        raise ValueError("Round 20 之后 Final OOS 被错误授权。")
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("CURRENT Final OOS 已不再封存。")

    base_config = profit_opt._base_research_config()
    if int(base_config.observation_rows) != round19.OBSERVATION_ROWS:
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
    upper_bound = _upper_bound_summary(cells)
    family_ready = bool(upper_bound["all_cells_passed"])
    conclusion = (
        "RELATIVE_MOMENTUM_FAMILY_WORTH_PREREGISTRATION：观察期固定方向在 8/8 个跨年代 cell 的 oracle 离场上界中通过；仅允许另写单一真实退出候选协议。"
        if family_ready
        else "NO_PREREGISTERED_RELATIVE_MOMENTUM_CANDIDATE：至少一个 cell 在固定因果方向和未来最优离场下仍失败，排除本协议定义的观察期相对动量家族。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_type": "CAUSAL_DIRECTION_NON_DEPLOYABLE_EXIT_UPPER_BOUND",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "input_hashes": {str(path): value for path, value in expected_hashes.items()},
        "direction_mode": "NEUTRAL",
        "observation_rows": round19.OBSERVATION_ROWS,
        "gross_notional": round19.PAIR_GROSS_NOTIONAL,
        "direction_uses_future": False,
        "exit_uses_future": True,
        "oracle_is_deployable": False,
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
        "formal_round21_preregistration_ready": family_ready,
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
    result_path = report_dir / "round21-relative-momentum-upper-bound-results.json"
    report_path = report_dir / "round21-relative-momentum-upper-bound-report.md"
    _write_json(result_path, result)
    report_path.write_text(_report_markdown(result), encoding="utf-8")
    print(
        json.dumps(
            {
                "result_path": str(result_path.resolve()),
                "report_path": str(report_path.resolve()),
                "passed_cell_count": upper_bound["passed_cell_count"],
                "cell_count": upper_bound["cell_count"],
                "formal_round21_preregistration_ready": family_ready,
                "conclusion": conclusion,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
