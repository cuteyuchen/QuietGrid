from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_pre2020_quadratic_w2160 as round13
import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_oos import _dataset_brief, _write_json
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc
CANDIDATE_ID = "SPOT_201803_202001_W2160_QUADRATIC_E2_FIXED"
PROTOCOL_PATH = Path(
    "reports/cross-era-oos/round14-spot-2018-2020-feasibility-protocol.md"
)
PROTOCOL_SHA256 = "c6a3dc7c37d57884193e9538984ef2740668b3207d653c02ca1c939d39b05876"
MIN_PAIRED_CONTIGUOUS_WINDOWS = 80
EXPECTED_MANIFEST_SHA256 = {
    "BTCUSDT": "d51cacc3cf6842f20a5aa257ad425a666f056618745a1ad1ed3742b554ba7ee3",
    "ETHUSDT": "519bc610925f9f9f7197d5f6b13810fca2aef8f26d5c69c50be2218e58bf4e32",
}
DEFAULT_MANIFESTS = (
    "data/backtests/spot_2018_2020/"
    "binance_spot_btcusdt_1m_1519862400000_1577836740000_6a2be5ad147f.manifest.json",
    "data/backtests/spot_2018_2020/"
    "binance_spot_ethusdt_1m_1519862400000_1577836740000_07f64d7d1711.manifest.json",
)


def _window_is_contiguous(window: Any) -> bool:
    if window.status != "READY" or len(window.rows) <= int(window.observation_rows):
        return False
    times = [int(row.open_time) for row in window.rows]
    return bool(times) and times[0] % 60_000 == 0 and all(
        current - previous == 60_000
        for previous, current in zip(times, times[1:])
    )


def _paired_contiguous_window_ids(
    windows: Sequence[Any],
    *,
    minimum: int = MIN_PAIRED_CONTIGUOUS_WINDOWS,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    ordered_ids = list(dict.fromkeys(str(window.window_id) for window in windows))
    by_id: dict[str, dict[str, Any]] = {}
    counts_by_symbol: dict[str, dict[str, int]] = {}
    for window in windows:
        symbol = str(window.symbol).strip().upper()
        window_id = str(window.window_id)
        by_id.setdefault(window_id, {})[symbol] = window
        counts = counts_by_symbol.setdefault(
            symbol,
            {"all": 0, "ready": 0, "contiguous": 0},
        )
        counts["all"] += 1
        counts["ready"] += int(window.status == "READY")
        counts["contiguous"] += int(_window_is_contiguous(window))

    required_symbols = set(asset_audit.SYMBOLS)
    paired = tuple(
        window_id
        for window_id in ordered_ids
        if set(by_id[window_id]) == required_symbols
        and all(_window_is_contiguous(window) for window in by_id[window_id].values())
    )
    excluded = tuple(window_id for window_id in ordered_ids if window_id not in set(paired))
    if len(paired) < minimum:
        raise RuntimeError(
            f"Spot 成对连续窗口不足: {len(paired)} < {minimum}。"
        )
    return paired, {
        "minimum_required": minimum,
        "paired_contiguous_count": len(paired),
        "excluded_window_count": len(excluded),
        "excluded_window_ids": list(excluded),
        "counts_by_symbol": counts_by_symbol,
    }


def _validate_dataset_metadata(
    manifests: Sequence[str],
    metadata: Sequence[Mapping[str, Any]],
) -> None:
    if len(manifests) != len(metadata):
        raise ValueError("Spot manifest 与 metadata 数量不一致。")
    found: set[str] = set()
    for manifest, item in zip(manifests, metadata):
        symbol = str(item.get("symbol") or "").strip().upper()
        found.add(symbol)
        expected_hash = EXPECTED_MANIFEST_SHA256.get(symbol)
        if expected_hash is None or _sha256(Path(manifest).resolve()) != expected_hash:
            raise ValueError(f"{symbol or 'UNKNOWN'} Spot manifest 哈希不一致。")
        if item.get("market") != "SPOT" or item.get("market_path") != "spot":
            raise ValueError(f"{symbol} 不是冻结的 Binance Spot 数据。")
        if item.get("actual_start") != "2018-03-01T00:00:00+00:00":
            raise ValueError(f"{symbol} Spot 起始时间不一致。")
        if item.get("actual_end") != "2020-01-01T00:00:00+00:00":
            raise ValueError(f"{symbol} Spot 结束时间不一致。")
        if int(item.get("duplicate_rows", -1)) != 0:
            raise ValueError(f"{symbol} Spot 数据包含重复分钟。")
        if float(item.get("missing_ratio", 1.0)) > 0.005:
            raise ValueError(f"{symbol} Spot 数据缺口率超过 0.5%。")
        if not bool(item.get("official_checksums_verified")):
            raise ValueError(f"{symbol} Spot 官方 checksum 未全部验证。")
    if found != set(asset_audit.SYMBOLS):
        raise ValueError("Spot 可行性研究必须且只能包含 BTCUSDT、ETHUSDT。")


def _validate_execution_integrity(
    actual: Mapping[str, Any],
    *,
    window_count: int,
) -> None:
    expected = {
        "window_count": window_count,
        "symbol_window_count": window_count * len(asset_audit.SYMBOLS),
        "wind_down_bars": round13.CANDIDATE_WIND_DOWN_BARS,
        "reprice_interval_bars": 5,
        "initial_offset_steps": 1.1,
        "unwind_fraction": 1.0,
        "urgency_exponent": round13.CANDIDATE_EXPONENT,
        "cache_entry_count": window_count * len(asset_audit.SYMBOLS),
        "profit_protection_enabled": False,
        "volatility_reduce_enabled": False,
        "passed": True,
    }
    if dict(actual) != expected:
        raise RuntimeError("Round 14 worker 执行完整性与冻结策略不一致。")


def _cross_market_passed(cells: Mapping[str, Any]) -> bool:
    expected_cells = {
        "SPOT_2018_2020_EXTERNAL_BASE",
        "SPOT_2018_2020_EXTERNAL_COST50",
    }
    if set(cells) != expected_cells:
        raise ValueError("Round 14 结果单元不完整。")
    return all(
        bool(cell["symbols"][symbol]["passed"])
        for cell in cells.values()
        for symbol in asset_audit.SYMBOLS
    )


def _report_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Round 14：2018-03 至 2019 Spot 跨市场可行性结果",
        "",
        "固定 W2160 + E2、固定入口过滤、六个固定种子；没有搜索或调整参数。",
        "",
        "| 情景 | 标的 | 平均种子 PnL | 最差种子 | 正种子 | PF 全通过 | 最大回撤 | 最大集中度 | 最低覆盖 | 通过 |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |",
    ]
    for cell_name, cell in payload["cells"].items():
        for symbol in asset_audit.SYMBOLS:
            item = cell["symbols"][symbol]
            summary = item["summary"]
            lines.append(
                "| `{cell}` | {symbol} | {mean:.4f} | {worst:.4f} | {positive}/6 | "
                "{pf} | {drawdown:.2%} | {concentration:.2%} | {coverage:.2%} | {passed} |".format(
                    cell=cell_name,
                    symbol=symbol,
                    mean=summary["mean_seed_total_pnl"],
                    worst=summary["worst_seed_total_pnl"],
                    positive=summary["positive_seed_count"],
                    pf="是" if summary["all_seed_profit_factors_gt_1"] else "否",
                    drawdown=summary["maximum_drawdown_pct"],
                    concentration=summary["worst_best_window_concentration"],
                    coverage=summary["minimum_trade_coverage"],
                    passed="是" if item["passed"] else "否",
                )
            )
    quality = payload["data_quality"]
    lines.extend(
        [
            "",
            "## 数据与边界",
            "",
            f"- 成对连续窗口：{quality['paired_contiguous_count']}；排除窗口：{quality['excluded_window_count']}。",
            "- Spot 价格序列仅作为跨市场结构压力测试，不是期货收益证明。",
            "- 旧 CURRENT Final OOS 未读取，生产默认值未修改。",
            "",
            f"结论：{payload['conclusion']}",
            "",
        ]
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="固定 W2160+E2 在 2018-03 至 2019 Binance Spot 上的跨市场可行性检验。"
    )
    parser.add_argument("manifests", nargs="*", default=list(DEFAULT_MANIFESTS))
    parser.add_argument(
        "--round12-result",
        default="reports/cross-era-oos/round12-quadratic-volatility-defense-results.json",
    )
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    if _sha256(PROTOCOL_PATH.resolve()) != PROTOCOL_SHA256:
        raise ValueError("Round 14 协议哈希不一致。")

    round12_path = Path(args.round12_result).resolve()
    if _sha256(round12_path) != asset_audit.ROUND12_RESULT_SHA256:
        raise ValueError("Round 12 冻结结果哈希不一致。")
    round12_payload = json.loads(round12_path.read_text(encoding="utf-8"))
    if round12_payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("旧 CURRENT Final OOS 已不再封存。")
    if bool(round12_payload.get("final_oos_authorized")):
        raise ValueError("旧 CURRENT Final OOS 被错误授权。")

    manifests = tuple(str(Path(value).resolve()) for value in args.manifests)
    base_config = profit_opt._base_research_config()
    metadata, windows = profit_opt._load_data(manifests, base_config)
    _validate_dataset_metadata(manifests, metadata)
    paired_ids, quality = _paired_contiguous_window_ids(windows)

    raw_runs, execution_integrity = asset_audit._run_dataset(
        manifests,
        base_config,
        {"external": paired_ids},
        args.workers,
    )
    _validate_execution_integrity(
        execution_integrity,
        window_count=len(paired_ids),
    )
    cells = asset_audit._filtered_symbol_cells(
        raw_runs,
        windows=windows,
        metadata=metadata,
        base_config=base_config,
        split_ids={"external": paired_ids},
        role="SPOT_2018_2020",
    )
    passed = _cross_market_passed(cells)
    conclusion = (
        "CROSS_MARKET_FEASIBILITY_ONLY：四个 Spot 压力单元全部通过；"
        "仅允许设计新的期货候选，不构成稳定收益证明。"
        if passed
        else "NO_CROSS_MARKET_FEASIBILITY：固定中性周末网格未通过四个 Spot 跨市场压力单元。"
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "candidate_id": CANDIDATE_ID,
        "diagnostic_role": "CROSS_MARKET_FEASIBILITY_ONLY",
        "protocol_sha256": PROTOCOL_SHA256,
        "source_sha256": _sha256(Path(__file__).resolve()),
        "round12_result_sha256": asset_audit.ROUND12_RESULT_SHA256,
        "datasets": _dataset_brief(manifests, metadata),
        "data_quality": quality,
        "seeds": list(asset_audit.DEFAULT_SEEDS),
        "policy": {
            "wind_down_bars": round13.CANDIDATE_WIND_DOWN_BARS,
            "urgency_exponent": round13.CANDIDATE_EXPONENT,
            "direction_mode": "NEUTRAL",
            "fixed_filters": {
                symbol: vars(entry_filter)
                for symbol, entry_filter in round13.FIXED_FILTERS.items()
            },
        },
        "execution_integrity": execution_integrity,
        "cells": cells,
        "cross_market_passed": passed,
        "candidate_preregistered": True,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": conclusion,
    }
    output_dir = Path(args.report_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_output = output_dir / "round14-spot-feasibility-results.json"
    markdown_output = output_dir / "round14-spot-feasibility-report.md"
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"Round 14 结果已存在，拒绝覆盖: {output}")
    _write_json(json_output, result)
    markdown_output.write_text(_report_markdown(result), encoding="utf-8")
    print(f"RESULT {json_output}")


if __name__ == "__main__":
    main()
