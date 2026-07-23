from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import scripts.profit_protection_optimize as profit_opt
from core.models import GridDirectionMode
from scripts.cross_era_early_wind_down import (
    FIXED_FILTERS,
    REFERENCE_WIND_DOWN_BARS,
    _assert_close,
    _cell_specs,
    _flatten_results,
    _mechanism_checks,
    _mechanism_metrics,
    _mechanism_summary,
    _scenario_name,
    _sha256,
    _verify_reference_summary,
)
from scripts.cross_era_entry_screen import (
    _filtered_evidence_for_symbols,
    _populate_entry_decisions,
    _symbol_trade_coverage,
)
from scripts.cross_era_extended_development import (
    _cell_checks,
    _select_candidate,
    _selection_metrics,
)
from scripts.cross_era_oos import (
    _dataset_brief,
    _evidence_payload,
    _load_research_state,
    _market_states,
    _protocol_sha256,
    _registered_candidates as _registered_parameter_candidates,
    _write_json,
)
from scripts.profit_protection_optimize import (
    BASE_COST,
    COST_50,
    DEFAULT_SEEDS,
    CandidateEvidence,
    _locked_policy,
)
from scripts.robustness import (
    RobustnessResearch,
    SymbolResearchPolicy,
    WindowResult,
)


UTC = timezone.utc
PROTOCOL_NAME = "cross-era-symbol-specific-maker-offset-round9-20260723"
ROUND8_RESULT_SHA256 = (
    "2a98ad21d638cc82fa3e9556b1a4a40b53658205c0fc274c2d269d0569eb7a5e"
)
MAKER_PLATFORM_REPORT_SHA256 = (
    "995fc316600c0573255e57b3d7eda37b81ddfca44a2dff1644205991ad7821de"
)
REFERENCE_CANDIDATE_ID = "SMO_ETH110"
REFERENCE_SPEC = {
    "BTCUSDT": {
        "wind_down_bars": 2880,
        "wind_down_initial_offset_steps": 1.10,
    },
    "ETHUSDT": {
        "wind_down_bars": 1440,
        "wind_down_initial_offset_steps": 1.10,
    },
}


def _registered_candidates() -> dict[str, dict[str, dict[str, float | int]]]:
    return {
        "SMO_ETH120": {
            "BTCUSDT": {
                "wind_down_bars": 2880,
                "wind_down_initial_offset_steps": 1.10,
            },
            "ETHUSDT": {
                "wind_down_bars": 1440,
                "wind_down_initial_offset_steps": 1.20,
            },
        },
        "SMO_ETH130": {
            "BTCUSDT": {
                "wind_down_bars": 2880,
                "wind_down_initial_offset_steps": 1.10,
            },
            "ETHUSDT": {
                "wind_down_bars": 1440,
                "wind_down_initial_offset_steps": 1.30,
            },
        },
    }


def _candidate_spec(candidate_id: str) -> dict[str, dict[str, float | int]]:
    if candidate_id == REFERENCE_CANDIDATE_ID:
        return {symbol: dict(values) for symbol, values in REFERENCE_SPEC.items()}
    candidates = _registered_candidates()
    if candidate_id not in candidates:
        raise ValueError(f"未知 Round 9 候选: {candidate_id}")
    return {
        symbol: dict(values)
        for symbol, values in candidates[candidate_id].items()
    }


def _symbol_policies_for_candidate(
    symbol_policies: dict[str, SymbolResearchPolicy],
    candidate_id: str,
) -> dict[str, SymbolResearchPolicy]:
    spec = _candidate_spec(candidate_id)
    if set(symbol_policies) != set(spec):
        raise ValueError("Round 9 候选必须且只能覆盖全部锁定标的。")
    return {
        symbol: replace(
            policy,
            wind_down_bars=int(spec[symbol]["wind_down_bars"]),
            wind_down_initial_offset_steps=float(
                spec[symbol]["wind_down_initial_offset_steps"]
            ),
        )
        for symbol, policy in symbol_policies.items()
    }


def _verify_worker_cache(
    research: RobustnessResearch,
    candidate_id: str,
    allowed_window_ids: set[str],
) -> dict[str, dict[str, float | int]]:
    expected = _candidate_spec(candidate_id)
    observed: dict[str, dict[str, set[float | int]]] = {
        symbol: {"wind_down_bars": set(), "wind_down_initial_offset_steps": set()}
        for symbol in expected
    }
    for cache_key in research._cache:
        symbol = str(cache_key[1]).strip().upper()
        window_id = str(cache_key[2])
        if window_id not in allowed_window_ids:
            raise RuntimeError("Round 9 worker 访问了 Development/Validation 之外的窗口。")
        if symbol not in observed:
            continue
        observed[symbol]["wind_down_bars"].add(int(cache_key[4]))
        observed[symbol]["wind_down_initial_offset_steps"].add(float(cache_key[7]))

    normalized: dict[str, dict[str, float | int]] = {}
    for symbol, values in observed.items():
        wind_downs = values["wind_down_bars"]
        offsets = values["wind_down_initial_offset_steps"]
        if len(wind_downs) != 1 or len(offsets) != 1:
            raise RuntimeError(
                f"Round 9 worker 的按标的覆盖不唯一: {symbol} {values}"
            )
        wind_down = int(next(iter(wind_downs)))
        offset = float(next(iter(offsets)))
        expected_values = expected[symbol]
        if wind_down != int(expected_values["wind_down_bars"]) or not math.isclose(
            offset,
            float(expected_values["wind_down_initial_offset_steps"]),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RuntimeError(
                f"Round 9 按标的覆盖不一致: expected={expected} observed={observed}"
            )
        normalized[symbol] = {
            "wind_down_bars": wind_down,
            "wind_down_initial_offset_steps": offset,
        }
    return normalized


def _maker_offset_seed_worker(
    candidate_id: str,
    seed: int,
    split_ids: dict[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[
    str,
    int,
    dict[str, tuple[Any, list[WindowResult]]],
    dict[str, dict[str, float | int]],
]:
    state = profit_opt._WORKER_STATE
    maker_fee, taker_fee, slippage_bps = cost
    config = replace(
        state["base_config"],
        wind_down_bars=REFERENCE_WIND_DOWN_BARS,
    )
    symbol_policies = _symbol_policies_for_candidate(
        state["symbol_policies"],
        candidate_id,
    )
    research = RobustnessResearch(
        state["windows"],
        state["parameters"],
        config,
        dataset_metadata=state["metadata"],
    )
    runs = {}
    for split_name, window_ids in split_ids.items():
        runs[split_name] = research.evaluate_joint_policy_windows(
            symbol_policies,
            state["maker_policy"],
            window_ids,
            maker_fee_rate=maker_fee,
            taker_fee_rate=taker_fee,
            stop_slippage_bps=slippage_bps,
            fill_seed_salt=seed,
        )
    observed = _verify_worker_cache(
        research,
        candidate_id,
        {
            str(window_id)
            for window_ids in split_ids.values()
            for window_id in window_ids
        },
    )
    return candidate_id, seed, runs, observed


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Round 9 按标的 Maker 偏移扩展开发筛选",
        "",
        "- 证据角色：Development + 已消费 Validation，仅用于扩展开发",
        "- 稳定收益声明：否",
        f"- 候选数：{len(payload['candidates'])}",
        f"- 合格候选数：{len(payload['eligible_candidate_ids'])}",
        f"- 选中候选：`{payload['selected_candidate_id'] or 'NONE'}`",
        "- Final OOS：`SEALED_NOT_EVALUATED`",
        "",
        "| 候选 | ETH 偏移 | DEV BASE | DEV COST50 | VAL BASE | VAL COST50 | 退出损失改善 | 配对收益保留 | 最弱种子 | 最大集中度 | 全过 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for candidate_id, item in sorted(payload["candidates"].items()):
        counts = {
            name: (sum(cell["checks"].values()), len(cell["checks"]))
            for name, cell in item["cells"].items()
        }
        mechanism = item["mechanism_metrics"]
        metrics = item["selection_metrics"]
        eth_offset = item["symbol_policy_overrides"]["ETHUSDT"][
            "wind_down_initial_offset_steps"
        ]
        lines.append(
            "| {candidate_id} | {offset:.2f} | {db[0]}/{db[1]} | {dc[0]}/{dc[1]} | "
            "{vb[0]}/{vb[1]} | {vc[0]}/{vc[1]} | {stop:.2%} | {paired:.2%} | "
            "{worst:.4f} | {concentration:.2%} | {passed} |".format(
                candidate_id=candidate_id,
                offset=eth_offset,
                db=counts["DEV_BASE"],
                dc=counts["DEV_COST50"],
                vb=counts["VAL_BASE"],
                vc=counts["VAL_COST50"],
                stop=mechanism["stop_exit_loss_improvement"],
                paired=mechanism["paired_grid_pnl_retention"],
                worst=metrics["minimum_worst_seed_total_pnl"],
                concentration=metrics["maximum_best_window_concentration"],
                passed="PASS" if item["all_checks_passed"] else "FAIL",
            )
        )
    lines.extend(["", f"结论：{payload['conclusion']}", ""])
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="评估 ETH 按标的 Maker wind-down 初始偏移的跨周期稳健性。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--workers", type=int, default=min(6, os.cpu_count() or 1))
    parser.add_argument("--report-dir", default="reports/cross-era-oos")
    parser.add_argument(
        "--protocol",
        default="reports/cross-era-oos/round9-symbol-specific-maker-offset-protocol.md",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    if seeds != DEFAULT_SEEDS:
        raise ValueError("Round 9 必须使用协议冻结的六个种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")

    report_dir = Path(args.report_dir).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "round9-symbol-specific-maker-offset-results.json"
    if output.exists():
        raise FileExistsError(f"Round 9 结果已存在，拒绝覆盖: {output}")

    round8_path = report_dir / "round8-symbol-specific-wind-down-results.json"
    maker_platform_path = (
        report_dir.parent
        / "robustness"
        / "maker-unwind-local-platform-r090-s255-20260719.md"
    )
    if _sha256(round8_path) != ROUND8_RESULT_SHA256:
        raise ValueError("Round 8 冻结结果已变化。")
    if _sha256(maker_platform_path) != MAKER_PLATFORM_REPORT_SHA256:
        raise ValueError("Maker 偏移平台冻结报告已变化。")
    round8 = json.loads(round8_path.read_text(encoding="utf-8"))
    if round8.get("eligible_candidate_ids"):
        raise ValueError("Round 8 已有合格候选，不应执行 Round 9。")
    if round8.get("final_oos_status") != "SEALED_NOT_EVALUATED":
        raise ValueError("Round 8 的 Final OOS 已不再封存。")
    if round8.get("production_defaults_changed") is not False:
        raise ValueError("Round 8 未确认生产默认值保持不变。")

    base_config, metadata, windows, split = _load_research_state(args.manifests)
    datasets = _dataset_brief(args.manifests, metadata)
    if round8.get("datasets") != datasets:
        raise ValueError("当前冻结数据与 Round 8 不一致。")
    locked_parameters, locked_policies, maker_policy = _locked_policy()
    if any(
        parameter.direction_mode != GridDirectionMode.NEUTRAL
        for parameter in locked_parameters
    ):
        raise ValueError("Round 9 只允许 NEUTRAL 参数。")
    if maker_policy.reprice_interval_bars != 5:
        raise ValueError("Round 9 Maker 重挂间隔必须保持 5 bar。")
    if not math.isclose(maker_policy.initial_offset_steps, 1.10):
        raise ValueError("Round 9 全局 Maker 偏移必须保持 1.10。")
    if not math.isclose(maker_policy.unwind_fraction, 1.00):
        raise ValueError("Round 9 Maker unwind fraction 必须保持 1.00。")
    if any(
        policy.wind_down_bars is not None
        or policy.wind_down_initial_offset_steps is not None
        for policy in locked_policies.values()
    ):
        raise ValueError("锁定策略的按标的覆盖默认值必须为 None。")

    market_states = {
        "development": _market_states(windows, split.development),
        "validation": _market_states(windows, split.validation),
    }
    split_ids = {
        "development": split.development,
        "validation": split.validation,
    }
    candidate_ids = (REFERENCE_CANDIDATE_ID, *tuple(_registered_candidates()))
    raw_runs: dict[
        tuple[str, str],
        dict[int, dict[str, tuple[Any, list[WindowResult]]]],
    ] = {
        (candidate_id, scenario): {}
        for candidate_id in candidate_ids
        for scenario in ("BASE", "COST50")
    }
    observed_overrides: dict[
        str,
        dict[str, dict[str, set[float | int]]],
    ] = {
        candidate_id: {
            symbol: {
                "wind_down_bars": set(),
                "wind_down_initial_offset_steps": set(),
            }
            for symbol in REFERENCE_SPEC
        }
        for candidate_id in candidate_ids
    }
    futures = {}
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=profit_opt._initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    ) as executor:
        for candidate_id in candidate_ids:
            for scenario, cost in (("BASE", BASE_COST), ("COST50", COST_50)):
                print(f"EVALUATING {candidate_id} {scenario}", flush=True)
                for seed in seeds:
                    future = executor.submit(
                        _maker_offset_seed_worker,
                        candidate_id,
                        seed,
                        split_ids,
                        cost,
                    )
                    futures[future] = (candidate_id, scenario, seed)
        for future in concurrent.futures.as_completed(futures):
            expected_candidate, scenario, expected_seed = futures[future]
            candidate_id, seed, runs, observed = future.result()
            if candidate_id != expected_candidate or seed != expected_seed:
                raise RuntimeError("Round 9 worker 返回了错误任务标识。")
            raw_runs[(candidate_id, scenario)][seed] = runs
            for symbol, values in observed.items():
                for name, value in values.items():
                    observed_overrides[candidate_id][symbol][name].add(value)

    execution_integrity = {}
    for candidate_id in candidate_ids:
        expected = _candidate_spec(candidate_id)
        normalized = {}
        for symbol, values in observed_overrides[candidate_id].items():
            if any(len(item) != 1 for item in values.values()):
                raise RuntimeError(
                    f"Round 9 汇总的按标的覆盖不唯一: {candidate_id} {values}"
                )
            normalized[symbol] = {
                "wind_down_bars": int(next(iter(values["wind_down_bars"]))),
                "wind_down_initial_offset_steps": float(next(iter(
                    values["wind_down_initial_offset_steps"]
                ))),
            }
            _assert_close(
                f"execution.{candidate_id}.{symbol}",
                normalized[symbol],
                expected[symbol],
            )
        execution_integrity[candidate_id] = {
            "expected": expected,
            "observed": normalized,
            "passed": True,
        }

    baseline_candidate = _registered_parameter_candidates()[0]
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
    contexts = {
        "development": _populate_entry_decisions(research, split.development),
        "validation": _populate_entry_decisions(research, split.validation),
    }
    cells = _cell_specs(split)
    reference_cells = {}
    reference_filtered_evidence = {}
    round8_reference = round8["candidates"]["SW_BTC2880_ETH1440"]
    for cell_name, split_name, _window_ids, cost in cells:
        scenario = _scenario_name(cost)
        raw_reference = evidences[(REFERENCE_CANDIDATE_ID, scenario)]
        filtered = _filtered_evidence_for_symbols(
            raw_reference,
            FIXED_FILTERS,
            contexts[split_name],
            candidate_id=REFERENCE_CANDIDATE_ID,
            round_name="round9_reference",
            split_name=split_name,
        )
        filtered_payload = _evidence_payload(
            filtered,
            split_name,
            market_states[split_name],
        )
        _verify_reference_summary(
            filtered_payload["summary"],
            round8_reference["cells"][cell_name]["candidate"]["summary"],
        )
        coverage = {
            symbol: _symbol_trade_coverage(
                raw_reference,
                filtered,
                symbol,
                split_name,
            )
            for symbol in ("BTCUSDT", "ETHUSDT")
        }
        _assert_close(
            f"reference_coverage.{cell_name}",
            coverage,
            round8_reference["cells"][cell_name]["trade_coverage"],
        )
        reference_filtered_evidence[cell_name] = filtered
        reference_cells[cell_name] = {
            "candidate": filtered_payload,
            "trade_coverage": coverage,
        }

    reference_mechanism = round8["reference_mechanism"]
    w2880_reference_mechanism = _mechanism_summary(
        _flatten_results(reference_filtered_evidence["DEV_COST50"], "development")
    )
    _assert_close(
        "w2880_reference_mechanism",
        w2880_reference_mechanism,
        round8_reference["mechanism"]["candidate"],
    )

    candidate_payloads = {}
    for candidate_id, spec in _registered_candidates().items():
        cell_payloads = {}
        cell_summaries = {}
        candidate_evidence_by_cell = {}
        for cell_name, split_name, _window_ids, cost in cells:
            scenario = _scenario_name(cost)
            raw_reference = evidences[(REFERENCE_CANDIDATE_ID, scenario)]
            raw_evidence = evidences[(candidate_id, scenario)]
            filtered = _filtered_evidence_for_symbols(
                raw_evidence,
                FIXED_FILTERS,
                contexts[split_name],
                candidate_id=candidate_id,
                round_name="round9_symbol_specific_maker_offset",
                split_name=split_name,
            )
            candidate_evidence_by_cell[cell_name] = filtered
            candidate_payload = _evidence_payload(
                filtered,
                split_name,
                market_states[split_name],
            )
            coverage = {
                symbol: _symbol_trade_coverage(
                    raw_reference,
                    filtered,
                    symbol,
                    split_name,
                )
                for symbol in ("BTCUSDT", "ETHUSDT")
            }
            _assert_close(
                f"candidate_coverage.{candidate_id}.{cell_name}",
                coverage,
                round8_reference["cells"][cell_name]["trade_coverage"],
            )
            checks = _cell_checks(
                round8["raw_baselines"][cell_name]["evidence"]["summary"],
                candidate_payload["summary"],
                seed_count=len(seeds),
                btc_coverage=coverage["BTCUSDT"],
                eth_coverage=coverage["ETHUSDT"],
            )
            cell_payloads[cell_name] = {
                "candidate": candidate_payload,
                "trade_coverage": coverage,
                "checks": checks,
                "passed": all(checks.values()),
            }
            cell_summaries[cell_name] = candidate_payload["summary"]
        candidate_mechanism = _mechanism_summary(
            _flatten_results(
                candidate_evidence_by_cell["DEV_COST50"],
                "development",
            )
        )
        mechanism_checks = _mechanism_checks(
            reference_mechanism,
            candidate_mechanism,
        )
        candidate_payloads[candidate_id] = {
            "symbol_policy_overrides": spec,
            "cells": cell_payloads,
            "mechanism": {
                "reference": reference_mechanism,
                "w2880_eth110_reference": w2880_reference_mechanism,
                "candidate": candidate_mechanism,
            },
            "mechanism_metrics": _mechanism_metrics(
                reference_mechanism,
                candidate_mechanism,
            ),
            "mechanism_checks": mechanism_checks,
            "all_cells_passed": all(
                item["passed"] for item in cell_payloads.values()
            ),
            "all_checks_passed": (
                all(item["passed"] for item in cell_payloads.values())
                and all(mechanism_checks.values())
            ),
            "selection_metrics": _selection_metrics(cell_summaries),
        }

    selection_input = {
        candidate_id: {
            **payload,
            "all_cells_passed": payload["all_checks_passed"],
        }
        for candidate_id, payload in candidate_payloads.items()
    }
    selected = _select_candidate(selection_input)
    eligible = sorted(
        candidate_id
        for candidate_id, payload in candidate_payloads.items()
        if payload["all_checks_passed"]
    )
    result = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": PROTOCOL_NAME,
        "protocol_sha256": _protocol_sha256(Path(args.protocol).resolve()),
        "source_sha256": {
            "round8_results": ROUND8_RESULT_SHA256,
            "maker_platform_report": MAKER_PLATFORM_REPORT_SHA256,
        },
        "datasets": datasets,
        "seeds": list(seeds),
        "direction_mode": "NEUTRAL",
        "split": {
            "development_count": len(split.development),
            "validation_count": len(split.validation),
            "final_oos_count": len(split.final_oos),
            "validation_role": "CONSUMED_AS_EXTENDED_DEVELOPMENT",
            "final_oos_status": "SEALED_NOT_EVALUATED",
        },
        "fixed_filters": {
            symbol: asdict(entry_filter) | {"filter_id": entry_filter.filter_id}
            for symbol, entry_filter in FIXED_FILTERS.items()
        },
        "reference_candidate_id": REFERENCE_CANDIDATE_ID,
        "reference_spec": REFERENCE_SPEC,
        "execution_integrity": execution_integrity,
        "raw_baselines": round8["raw_baselines"],
        "reference_cells": reference_cells,
        "reference_mechanism": reference_mechanism,
        "w2880_eth110_reference_mechanism": w2880_reference_mechanism,
        "candidates": candidate_payloads,
        "eligible_candidate_ids": eligible,
        "selected_candidate_id": selected,
        "final_oos_authorized": selected is not None,
        "final_oos_status": "SEALED_NOT_EVALUATED",
        "production_defaults_changed": False,
        "stable_profit_claimed": False,
        "conclusion": (
            "按标的 Maker 偏移选出唯一候选；只允许先预注册精确 Final OOS 协议。"
            if selected
            else "NO_ROBUST_CANDIDATE：按标的 Maker 偏移未通过四单元与机制门槛。"
        ),
    }
    _write_json(output, result)
    (report_dir / "round9-symbol-specific-maker-offset-report.md").write_text(
        _report_markdown(result),
        encoding="utf-8",
    )
    print(f"RESULT {output}")
    print(f"ELIGIBLE {len(eligible)}")
    print(f"SELECTED {selected or 'NONE'}")


if __name__ == "__main__":
    main()
