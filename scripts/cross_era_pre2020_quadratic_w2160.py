from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import scripts.profit_protection_optimize as profit_opt
from scripts.cross_era_early_wind_down import (
    _flatten_results,
    _mechanism_checks,
    _mechanism_metrics,
    _mechanism_summary,
    _sha256,
)
from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
    _symbol_trade_coverage,
)
from scripts.cross_era_extended_development import _cell_checks, _selection_metrics
from scripts.cross_era_oos import (
    _dataset_brief,
    _evidence_payload,
    _market_states,
    _protocol_sha256,
    _registered_candidates,
    _write_json,
)
from scripts.cross_era_quadratic_maker_urgency import FIXED_FILTERS
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    CandidateEvidence,
    _locked_policy,
)
from scripts.robustness import RobustnessResearch, WindowResult, WindDownMakerPolicy


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-prehistory-quadratic-w2160-round13-20260723"
PROTOCOL_SHA256 = "08867a452e9c712dd2918af02316d9946b2bdf9c893b6225da4af463beb671dd"
ROUND5_RESULT_SHA256 = (
    "c9a50588ef5b0bf2f1ca34037d270c45939b67619180c3b4ca1032027c452084"
)
ROUND11_RESULT_SHA256 = (
    "ab769bc64cb4e4b8fd3294bbeb02b8e673be1b3cd1033efc33695bf47c840f0a"
)
ROUND12_RESULT_SHA256 = (
    "d88f9051e30b3bda1a1bd42e798d6b08340c843a1b481648f2254fd983b47c4d"
)
MANIFEST_SHA256 = {
    "BTCUSDT": "995b32ad2693f785020838b0f5a907460e455ff64fc1a8e685c765ed6416c57d",
    "ETHUSDT": "42ff31fa5189fd676324f4c2383ab42c1173320bda7205e4326bc7dde660647c",
}
REFERENCE_VARIANT_ID = "EXT_W1440_LINEAR_E1"
CANDIDATE_ID = "EXT_W2160_QUADRATIC_E2"
REFERENCE_WIND_DOWN_BARS = 1440
CANDIDATE_WIND_DOWN_BARS = 2160
REFERENCE_EXPONENT = 1.0
CANDIDATE_EXPONENT = 2.0
EXPECTED_READY_WINDOW_COUNT = 28
EXPECTED_DATA_START = "2020-01-01T00:00:00+00:00"
EXPECTED_DATA_END = "2020-07-19T00:00:00+00:00"


def _variant_config_and_policy(
    base_config: Any,
    base_policy: WindDownMakerPolicy,
    variant_id: str,
) -> tuple[Any, WindDownMakerPolicy]:
    if variant_id == REFERENCE_VARIANT_ID:
        wind_down_bars = REFERENCE_WIND_DOWN_BARS
        exponent = REFERENCE_EXPONENT
    elif variant_id == CANDIDATE_ID:
        wind_down_bars = CANDIDATE_WIND_DOWN_BARS
        exponent = CANDIDATE_EXPONENT
    else:
        raise ValueError(f"未知 Round 13 变体: {variant_id}")
    return (
        replace(base_config, wind_down_bars=wind_down_bars),
        replace(base_policy, urgency_exponent=exponent),
    )


def _verify_worker_cache(
    research: RobustnessResearch,
    *,
    allowed_window_ids: set[str],
    expected_wind_down_bars: int,
    expected_exponent: float,
) -> dict[str, Any]:
    symbols_by_window: dict[str, set[str]] = {}
    observed_wind_down: set[int] = set()
    observed_reprice: set[int] = set()
    observed_offset: set[float] = set()
    observed_unwind: set[float] = set()
    observed_exponent: set[float] = set()

    for cache_key in research._cache:
        if len(cache_key) < 16:
            raise RuntimeError("Round 13 worker cache 缺少紧迫度指数。")
        symbol = str(cache_key[1]).strip().upper()
        window_id = str(cache_key[2])
        if window_id not in allowed_window_ids:
            raise RuntimeError("Round 13 worker 访问了 2020H1 外部窗口之外的数据。")
        symbols_by_window.setdefault(window_id, set()).add(symbol)
        observed_wind_down.add(int(cache_key[4]))
        observed_reprice.add(int(cache_key[6]))
        observed_offset.add(float(cache_key[7]))
        observed_unwind.add(float(cache_key[8]))
        observed_exponent.add(float(cache_key[-2]))

    if set(symbols_by_window) != allowed_window_ids:
        raise RuntimeError("Round 13 worker 未覆盖全部 2020H1 外部窗口。")
    if any(values != {"BTCUSDT", "ETHUSDT"} for values in symbols_by_window.values()):
        raise RuntimeError("Round 13 worker 未对同一窗口同时覆盖 BTC/ETH。")
    if observed_wind_down != {expected_wind_down_bars}:
        raise RuntimeError(f"Round 13 wind-down 不一致: {observed_wind_down}")
    if observed_reprice != {5}:
        raise RuntimeError(f"Round 13 Maker 重挂间隔不一致: {observed_reprice}")
    if observed_offset != {1.10}:
        raise RuntimeError(f"Round 13 Maker 初始偏移不一致: {observed_offset}")
    if observed_unwind != {1.0}:
        raise RuntimeError(f"Round 13 Maker unwind fraction 不一致: {observed_unwind}")
    if observed_exponent != {expected_exponent}:
        raise RuntimeError(f"Round 13 紧迫度指数不一致: {observed_exponent}")

    config = research.config
    if config.profit_protection_enabled:
        raise RuntimeError("Round 13 不允许启用利润保护。")
    if config.volatility_reduce_expansion_ratio != 0.0:
        raise RuntimeError("Round 13 不允许启用波动市场减仓。")
    if config.volatility_reduce_after_breaches != 0:
        raise RuntimeError("Round 13 不允许启用波动市场减仓。")

    return {
        "window_count": len(symbols_by_window),
        "symbol_window_count": sum(len(values) for values in symbols_by_window.values()),
        "wind_down_bars": expected_wind_down_bars,
        "reprice_interval_bars": 5,
        "initial_offset_steps": 1.10,
        "unwind_fraction": 1.0,
        "urgency_exponent": expected_exponent,
        "cache_entry_count": len(research._cache),
        "profit_protection_enabled": False,
        "volatility_reduce_enabled": False,
        "passed": True,
    }


def _external_seed_worker(
    variant_id: str,
    seed: int,
    window_ids: Sequence[str],
    cost: tuple[float, float, float],
) -> tuple[str, int, dict[str, tuple[Any, list[WindowResult]]], dict[str, Any]]:
    state = profit_opt._WORKER_STATE
    maker_fee, taker_fee, slippage_bps = cost
    config, maker_policy = _variant_config_and_policy(
        state["base_config"],
        state["maker_policy"],
        variant_id,
    )
    research = RobustnessResearch(
        state["windows"],
        state["parameters"],
        config,
        dataset_metadata=state["metadata"],
    )
    runs = {
        "external": research.evaluate_joint_policy_windows(
            state["symbol_policies"],
            maker_policy,
            window_ids,
            maker_fee_rate=maker_fee,
            taker_fee_rate=taker_fee,
            stop_slippage_bps=slippage_bps,
            fill_seed_salt=seed,
        )
    }
    observation = _verify_worker_cache(
        research,
        allowed_window_ids=set(window_ids),
        expected_wind_down_bars=config.wind_down_bars,
        expected_exponent=maker_policy.urgency_exponent,
    )
    return variant_id, seed, runs, observation


def _paired_ready_window_ids(windows: Sequence[Any]) -> tuple[str, ...]:
    symbols_by_window: dict[str, set[str]] = {}
    close_by_window: dict[str, datetime] = {}
    for window in windows:
        if window.status != "READY":
            continue
        symbols_by_window.setdefault(window.window_id, set()).add(window.symbol)
        close_by_window[window.window_id] = window.market_close
    if any(values != {"BTCUSDT", "ETHUSDT"} for values in symbols_by_window.values()):
        raise ValueError("2020H1 外部数据存在未成对的 READY 窗口。")
    ordered = tuple(sorted(symbols_by_window, key=close_by_window.__getitem__))
    if len(ordered) != EXPECTED_READY_WINDOW_COUNT:
        raise ValueError(
            "2020H1 外部 READY 窗口数量变化: "
            f"expected={EXPECTED_READY_WINDOW_COUNT} actual={len(ordered)}"
        )
    return ordered


def _maker_mechanism(results: Sequence[WindowResult]) -> dict[str, float | int]:
    return _mechanism_summary(results) | {
        "wind_down_maker_fill_count": sum(
            item.wind_down_maker_fill_count for item in results
        ),
        "wind_down_maker_pnl": sum(item.wind_down_maker_pnl for item in results),
    }


def _report_markdown(payload: dict[str, Any]) -> str:
    candidate = payload["candidates"][CANDIDATE_ID]
    counts = {
        name: (sum(cell["checks"].values()), len(cell["checks"]))
        for name, cell in candidate["cells"].items()
    }
    mechanism = candidate["mechanism_metrics"]
    metrics = candidate["selection_metrics"]
    return "\n".join([
        "# Round 13 2020H1 独立区间 W2160 二次 Maker 紧迫度",
        "",
        "- 证据角色：此前未读取收益的 2020H1 外部区间",
        "- 稳定收益声明：否",
        f"- 合格候选数：{len(payload['eligible_candidate_ids'])}",
        f"- 选中候选：`{payload['selected_candidate_id'] or 'NONE'}`",
        f"- Phase B 授权：`{payload['phase_b_authorized']}`",
        "- 当前 Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "组合：固定 `W2160` 与二次 Maker 紧迫度 `E2`，不启用波动市场减仓。",
        "",
        "| 候选 | EXT BASE | EXT COST50 | 退出损失改善 | 配对收益保留 | Maker 去库存成交 | 最弱种子 | 最大回撤 | 全过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        (
            "| {candidate_id} | {base[0]}/{base[1]} | {cost[0]}/{cost[1]} | "
            "{stop:.2%} | {paired:.2%} | {fills} | {worst:.4f} | {drawdown:.2%} | {passed} |"
        ).format(
            candidate_id=CANDIDATE_ID,
            base=counts["EXT_BASE"],
            cost=counts["EXT_COST50"],
            stop=mechanism["stop_exit_loss_improvement"],
            paired=mechanism["paired_grid_pnl_retention"],
            fills=candidate["mechanism"]["candidate"]["wind_down_maker_fill_count"],
            worst=metrics["minimum_worst_seed_total_pnl"],
            drawdown=metrics["maximum_drawdown_pct"],
            passed="PASS" if candidate["all_checks_passed"] else "FAIL",
        ),
        "",
        f"结论：{payload['conclusion']}",
        "",
    ])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="在此前未读取收益的 2020H1 区间评估 W2160 二次 Maker 紧迫度。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round13-prehistory-quadratic-w2160-protocol.md",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    if seeds != DEFAULT_SEEDS:
        raise ValueError("Round 13 必须使用协议冻结的六个种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")

    protocol_path = Path(args.protocol).resolve()
    if _sha256(protocol_path) != PROTOCOL_SHA256:
        raise ValueError("Round 13 协议哈希已变化。")
    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "round13-prehistory-quadratic-w2160-results.json"
    if output.exists():
        raise FileExistsError(f"Round 13 结果已存在，拒绝覆盖: {output}")

    prior_paths = {
        "round5": report_dir / "round5-early-wind-down-results.json",
        "round11": report_dir / "round11-quadratic-maker-urgency-results.json",
        "round12": report_dir / "round12-quadratic-volatility-defense-results.json",
    }
    expected_prior_hashes = {
        "round5": ROUND5_RESULT_SHA256,
        "round11": ROUND11_RESULT_SHA256,
        "round12": ROUND12_RESULT_SHA256,
    }
    prior_payloads = {}
    for name, path in prior_paths.items():
        if _sha256(path) != expected_prior_hashes[name]:
            raise ValueError(f"{name} 冻结结果已变化。")
        prior_payloads[name] = json.loads(path.read_text(encoding="utf-8"))
    for name, payload in prior_payloads.items():
        if payload.get("eligible_candidate_ids"):
            raise ValueError(f"{name} 已有合格候选，不应执行 Round 13。")
        if payload.get("final_oos_status") != "SEALED_NOT_EVALUATED":
            raise ValueError(f"{name} 的 Final OOS 已不再封存。")

    manifest_symbols = set()
    for manifest in args.manifests:
        path = Path(manifest).resolve()
        raw = json.loads(path.read_text(encoding="utf-8"))
        symbol = str(raw.get("symbol") or "").strip().upper()
        manifest_symbols.add(symbol)
        expected_hash = MANIFEST_SHA256.get(symbol)
        if expected_hash is None or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"Round 13 {symbol or 'UNKNOWN'} manifest 哈希不一致。")
        if raw.get("actual_start") != EXPECTED_DATA_START:
            raise ValueError(f"Round 13 {symbol} 数据起点不一致。")
        if raw.get("actual_end") != EXPECTED_DATA_END:
            raise ValueError(f"Round 13 {symbol} 数据终点不一致。")
        if int(raw.get("row_count") or 0) != 288_000:
            raise ValueError(f"Round 13 {symbol} 数据行数不一致。")
        if float(raw.get("missing_ratio") or 0.0) != 0.0:
            raise ValueError(f"Round 13 {symbol} 数据存在缺失。")
        if int(raw.get("duplicate_rows") or 0) != 0:
            raise ValueError(f"Round 13 {symbol} 数据存在重复。")
        if raw.get("official_checksums_verified") is not True:
            raise ValueError(f"Round 13 {symbol} 官方 checksum 未验证。")
    if manifest_symbols != {"BTCUSDT", "ETHUSDT"}:
        raise ValueError("Round 13 必须且只能使用 BTCUSDT、ETHUSDT 外部数据。")

    base_config = profit_opt._base_research_config()
    metadata, windows = profit_opt._load_data(args.manifests, base_config)
    datasets = _dataset_brief(args.manifests, metadata)
    external_ids = _paired_ready_window_ids(windows)
    market_states = _market_states(windows, external_ids)

    locked_parameters, locked_policies, maker_policy = _locked_policy()
    if any(
        policy.wind_down_bars is not None
        or policy.wind_down_initial_offset_steps is not None
        or policy.wind_down_reference_tradable_rows is not None
        or policy.wind_down_min_bars is not None
        or policy.wind_down_max_bars is not None
        for policy in locked_policies.values()
    ):
        raise ValueError("Round 13 不允许按标的覆盖 wind-down 参数。")
    if maker_policy.reprice_interval_bars != 5:
        raise ValueError("Round 13 Maker 重挂间隔必须保持 5 bars。")
    if abs(maker_policy.initial_offset_steps - 1.10) > 1e-12:
        raise ValueError("Round 13 Maker 初始偏移必须保持 1.10。")
    if abs(maker_policy.unwind_fraction - 1.0) > 1e-12:
        raise ValueError("Round 13 Maker unwind fraction 必须保持 1.00。")
    if abs(maker_policy.urgency_exponent - REFERENCE_EXPONENT) > 1e-12:
        raise ValueError("Round 13 默认紧迫度指数必须保持 1.0。")
    if base_config.profit_protection_enabled:
        raise ValueError("Round 13 基线不得启用利润保护。")
    if base_config.volatility_reduce_expansion_ratio != 0.0:
        raise ValueError("Round 13 基线不得启用波动市场减仓。")
    if base_config.volatility_reduce_after_breaches != 0:
        raise ValueError("Round 13 基线不得启用波动市场减仓。")

    variants = (REFERENCE_VARIANT_ID, CANDIDATE_ID)
    raw_runs: dict[
        tuple[str, str],
        dict[int, dict[str, tuple[Any, list[WindowResult]]]],
    ] = {
        (variant_id, scenario): {}
        for variant_id in variants
        for scenario in ("BASE", "COST50")
    }
    observations: dict[str, list[dict[str, Any]]] = {variant_id: [] for variant_id in variants}
    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=profit_opt._initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    ) as executor:
        for variant_id in variants:
            for scenario, cost in (("BASE", BASE_COST), ("COST50", COST_50)):
                print(f"EVALUATING {variant_id} {scenario}", flush=True)
                for seed in seeds:
                    future = executor.submit(
                        _external_seed_worker,
                        variant_id,
                        seed,
                        external_ids,
                        cost,
                    )
                    futures[future] = (variant_id, scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            expected_variant, scenario, expected_seed = futures[future]
            variant_id, seed, runs, observation = future.result()
            if variant_id != expected_variant or seed != expected_seed:
                raise RuntimeError("Round 13 worker 返回了错误任务标识。")
            raw_runs[(variant_id, scenario)][seed] = runs
            observations[variant_id].append(observation)

    execution_integrity = {}
    for variant_id, items in observations.items():
        if not items or any(item != items[0] for item in items[1:]):
            raise RuntimeError(f"Round 13 worker 执行参数不一致: {variant_id}")
        execution_integrity[variant_id] = items[0]

    baseline_candidate = _registered_candidates()[0]
    evidences = {
        key: CandidateEvidence(
            baseline_candidate,
            {seed: runs[seed] for seed in sorted(runs)},
        )
        for key, runs in raw_runs.items()
    }
    research = RobustnessResearch(
        windows,
        locked_parameters,
        base_config,
        dataset_metadata=metadata,
    )
    contexts = _populate_entry_decisions(research, external_ids)
    raw_baselines = {}
    reference_cells = {}
    reference_filtered_evidence = {}
    candidate_cells = {}
    candidate_filtered_evidence = {}
    cell_summaries = {}

    for cell_name, scenario in (("EXT_BASE", "BASE"), ("EXT_COST50", "COST50")):
        raw_reference = evidences[(REFERENCE_VARIANT_ID, scenario)]
        raw_payload = _evidence_payload(raw_reference, "external", market_states)
        raw_baselines[cell_name] = {
            "window_count": len(external_ids),
            "evidence": raw_payload,
        }
        reference_filtered = _filtered_evidence_for_symbols(
            raw_reference,
            FIXED_FILTERS,
            contexts,
            candidate_id=REFERENCE_VARIANT_ID,
            round_name="round13_external_reference",
            split_name="external",
        )
        reference_filtered_evidence[cell_name] = reference_filtered
        reference_cells[cell_name] = {
            "candidate": _evidence_payload(
                reference_filtered,
                "external",
                market_states,
            ),
            "trade_coverage": {
                symbol: _symbol_trade_coverage(
                    raw_reference,
                    reference_filtered,
                    symbol,
                    "external",
                )
                for symbol in FIXED_FILTERS
            },
        }

        raw_candidate = evidences[(CANDIDATE_ID, scenario)]
        filtered = _filtered_evidence_for_symbols(
            raw_candidate,
            FIXED_FILTERS,
            contexts,
            candidate_id=CANDIDATE_ID,
            round_name="round13_external_quadratic_w2160",
            split_name="external",
        )
        candidate_filtered_evidence[cell_name] = filtered
        candidate_payload = _evidence_payload(filtered, "external", market_states)
        btc_coverage = _symbol_trade_coverage(
            raw_reference,
            filtered,
            "BTCUSDT",
            "external",
        )
        eth_coverage = _symbol_trade_coverage(
            raw_reference,
            filtered,
            "ETHUSDT",
            "external",
        )
        checks = _cell_checks(
            raw_payload["summary"],
            candidate_payload["summary"],
            seed_count=len(seeds),
            btc_coverage=btc_coverage,
            eth_coverage=eth_coverage,
        )
        candidate_cells[cell_name] = {
            "candidate": candidate_payload,
            "trade_coverage": {
                "BTCUSDT": btc_coverage,
                "ETHUSDT": eth_coverage,
            },
            "checks": checks,
            "passed": all(checks.values()),
        }
        cell_summaries[cell_name] = candidate_payload["summary"]

    reference_results = _flatten_results(
        reference_filtered_evidence["EXT_COST50"],
        "external",
    )
    candidate_results = _flatten_results(
        candidate_filtered_evidence["EXT_COST50"],
        "external",
    )
    reference_mechanism = _maker_mechanism(reference_results)
    candidate_mechanism = _maker_mechanism(candidate_results)
    mechanism_checks = _mechanism_checks(reference_mechanism, candidate_mechanism) | {
        "wind_down_maker_fill_observed": (
            candidate_mechanism["wind_down_maker_fill_count"] > 0
        ),
    }
    all_cells_passed = all(item["passed"] for item in candidate_cells.values())
    all_checks_passed = all_cells_passed and all(mechanism_checks.values())
    candidate_payloads = {
        CANDIDATE_ID: {
            "wind_down_bars": CANDIDATE_WIND_DOWN_BARS,
            "urgency_exponent": CANDIDATE_EXPONENT,
            "cells": candidate_cells,
            "mechanism": {
                "reference": reference_mechanism,
                "candidate": candidate_mechanism,
            },
            "mechanism_metrics": _mechanism_metrics(
                reference_mechanism,
                candidate_mechanism,
            ),
            "mechanism_checks": mechanism_checks,
            "all_cells_passed": all_cells_passed,
            "all_checks_passed": all_checks_passed,
            "selection_metrics": _selection_metrics(cell_summaries),
        }
    }
    eligible = [CANDIDATE_ID] if all_checks_passed else []
    selected = CANDIDATE_ID if all_checks_passed else None
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": _protocol_sha256(protocol_path),
        "source_sha256": {
            "round5_results": ROUND5_RESULT_SHA256,
            "round11_results": ROUND11_RESULT_SHA256,
            "round12_results": ROUND12_RESULT_SHA256,
            "manifests": MANIFEST_SHA256,
        },
        "datasets": datasets,
        "seeds": list(seeds),
        "direction_mode": "NEUTRAL",
        "external_evidence": {
            "role": "PREVIOUSLY_UNREAD_EXTERNAL_VALIDATION",
            "ready_window_count": len(external_ids),
            "window_ids": list(external_ids),
            "cells": ["EXT_BASE", "EXT_COST50"],
        },
        "fixed_filters": {
            symbol: asdict(entry_filter) | {"filter_id": entry_filter.filter_id}
            for symbol, entry_filter in FIXED_FILTERS.items()
        },
        "execution_integrity": execution_integrity,
        "raw_baselines": raw_baselines,
        "reference_cells": reference_cells,
        "reference_mechanism": reference_mechanism,
        "candidates": candidate_payloads,
        "eligible_candidate_ids": eligible,
        "selected_candidate_id": selected,
        "phase_b_authorized": selected is not None,
        "final_oos_authorized": False,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": (
            "2020H1 外部区间通过；只允许锁定相同候选进入已消费 Development/Validation Phase B。"
            if selected
            else "NO_ROBUST_CANDIDATE：W2160 二次 Maker 紧迫度未通过 2020H1 外部区间门槛。"
        ),
    }
    _write_json(output, result)
    (report_dir / "round13-prehistory-quadratic-w2160-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"ELIGIBLE {len(eligible)}")
    print(f"SELECTED {selected or 'NONE'}")
    print(f"PHASE_B_AUTHORIZED {selected is not None}")


if __name__ == "__main__":
    main()
