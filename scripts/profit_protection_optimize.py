from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import os
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from core.models import GridDirectionMode
from scripts.robustness import (
    AggregateMetrics,
    EntryFilter,
    ParameterSet,
    ResearchConfig,
    RobustnessResearch,
    SymbolResearchPolicy,
    WeekendWindow,
    WindDownMakerPolicy,
    WindowResult,
    aggregate_joint_results,
    load_weekend_windows,
    split_window_ids,
    verify_frozen_dataset,
)
from strategy.profit_protection import ProfitProtectionConfig


UTC = timezone.utc
DEFAULT_SEEDS = (3, 10, 17, 31, 59, 97)
BASE_COST = (0.0002, 0.0005, 10.0)
COST_50 = (0.0003, 0.00075, 20.0)
TOTAL_CAPITAL = 800.0
_WORKER_STATE: dict[str, Any] = {}


@dataclass(frozen=True)
class ProfitCandidate:
    candidate_id: str
    round_name: str
    enabled: bool
    mode: str
    activation_usdt: float
    minimum_locked_ratio: float
    suppress_drawdown_pct: float
    reduce_drawdown_pct: float
    close_drawdown_pct: float
    estimated_exit_cost_rate: float = 0.0007
    passive_reduce_after_bars: int = 0
    active_reduce_after_bars: int = 0
    passive_reduce_fraction: float = 0.25
    active_reduce_fraction: float = 0.25
    volatility_reduce_expansion_ratio: float = 0.0
    volatility_reduce_after_breaches: int = 0
    volatility_reduce_fraction: float = 0.20
    volatility_reduce_mode: str = "BOTH"
    volatility_reduce_only_when_losing: bool = False
    volatility_wind_down_after_reduce: bool = False
    volatility_resume_after_normal_bars: int = 0

    def research_config(self, base: ResearchConfig) -> ResearchConfig:
        return replace(
            base,
            profit_protection_enabled=self.enabled,
            profit_protection_mode=self.mode,
            profit_activation_usdt=self.activation_usdt,
            profit_minimum_locked_ratio=self.minimum_locked_ratio,
            profit_suppress_drawdown_pct=self.suppress_drawdown_pct,
            profit_reduce_drawdown_pct=self.reduce_drawdown_pct,
            profit_close_drawdown_pct=self.close_drawdown_pct,
            profit_estimated_exit_cost_rate=self.estimated_exit_cost_rate,
            profit_passive_reduce_after_bars=self.passive_reduce_after_bars,
            profit_active_reduce_after_bars=self.active_reduce_after_bars,
            profit_passive_reduce_fraction=self.passive_reduce_fraction,
            profit_active_reduce_fraction=self.active_reduce_fraction,
            volatility_reduce_expansion_ratio=(
                self.volatility_reduce_expansion_ratio
            ),
            volatility_reduce_after_breaches=(
                self.volatility_reduce_after_breaches
            ),
            volatility_reduce_fraction=self.volatility_reduce_fraction,
            volatility_reduce_mode=self.volatility_reduce_mode,
            volatility_reduce_only_when_losing=(
                self.volatility_reduce_only_when_losing
            ),
            volatility_wind_down_after_reduce=(
                self.volatility_wind_down_after_reduce
            ),
            volatility_resume_after_normal_bars=(
                self.volatility_resume_after_normal_bars
            ),
        )


@dataclass
class CandidateEvidence:
    candidate: ProfitCandidate
    runs: dict[int, dict[str, tuple[AggregateMetrics, list[WindowResult]]]]


def _initialize_worker(
    manifests: Sequence[str],
    base_config: ResearchConfig,
) -> None:
    metadata, windows = _load_data(manifests, base_config)
    parameters, symbol_policies, maker_policy = _locked_policy()
    _WORKER_STATE.update({
        "metadata": metadata,
        "windows": windows,
        "parameters": parameters,
        "symbol_policies": symbol_policies,
        "maker_policy": maker_policy,
        "base_config": base_config,
    })


def _evaluate_seed_worker(
    candidate: ProfitCandidate,
    seed: int,
    split_ids: dict[str, Sequence[str]],
    cost: tuple[float, float, float],
) -> tuple[int, dict[str, tuple[AggregateMetrics, list[WindowResult]]]]:
    maker_fee, taker_fee, slippage_bps = cost
    research = RobustnessResearch(
        _WORKER_STATE["windows"],
        _WORKER_STATE["parameters"],
        candidate.research_config(_WORKER_STATE["base_config"]),
        dataset_metadata=_WORKER_STATE["metadata"],
    )
    runs: dict[str, tuple[AggregateMetrics, list[WindowResult]]] = {}
    for split_name, window_ids in split_ids.items():
        runs[split_name] = research.evaluate_joint_policy_windows(
            _WORKER_STATE["symbol_policies"],
            _WORKER_STATE["maker_policy"],
            window_ids,
            maker_fee_rate=maker_fee,
            taker_fee_rate=taker_fee,
            stop_slippage_bps=slippage_bps,
            fill_seed_salt=int(seed),
        )
    return int(seed), runs


def _candidate(
    candidate_id: str,
    round_name: str,
    *,
    enabled: bool = True,
    mode: str = "PEAK_DRAWDOWN",
    activation_usdt: float = 10.0,
    minimum_locked_ratio: float = 0.25,
    suppress_drawdown_pct: float = 0.25,
    reduce_drawdown_pct: float = 0.35,
    close_drawdown_pct: float = 0.50,
    passive_reduce_after_bars: int = 0,
    active_reduce_after_bars: int = 0,
    passive_reduce_fraction: float = 0.25,
    active_reduce_fraction: float = 0.25,
    volatility_reduce_expansion_ratio: float = 0.0,
    volatility_reduce_after_breaches: int = 0,
    volatility_reduce_fraction: float = 0.20,
    volatility_reduce_mode: str = "BOTH",
    volatility_reduce_only_when_losing: bool = False,
    volatility_wind_down_after_reduce: bool = False,
    volatility_resume_after_normal_bars: int = 0,
) -> ProfitCandidate:
    normalized_mode = str(mode).strip().upper()
    ProfitProtectionConfig(
        activation_profit_usdt=activation_usdt,
        enabled=enabled and normalized_mode != "OFF",
        minimum_locked_profit_ratio=minimum_locked_ratio,
        suppress_drawdown_pct=suppress_drawdown_pct,
        reduce_drawdown_pct=reduce_drawdown_pct,
        close_drawdown_pct=close_drawdown_pct,
        estimated_exit_cost_rate=0.0007,
    )
    return ProfitCandidate(
        candidate_id=candidate_id,
        round_name=round_name,
        enabled=enabled,
        mode=normalized_mode,
        activation_usdt=activation_usdt,
        minimum_locked_ratio=minimum_locked_ratio,
        suppress_drawdown_pct=suppress_drawdown_pct,
        reduce_drawdown_pct=reduce_drawdown_pct,
        close_drawdown_pct=close_drawdown_pct,
        passive_reduce_after_bars=passive_reduce_after_bars,
        active_reduce_after_bars=active_reduce_after_bars,
        passive_reduce_fraction=passive_reduce_fraction,
        active_reduce_fraction=active_reduce_fraction,
        volatility_reduce_expansion_ratio=volatility_reduce_expansion_ratio,
        volatility_reduce_after_breaches=volatility_reduce_after_breaches,
        volatility_reduce_fraction=volatility_reduce_fraction,
        volatility_reduce_mode=volatility_reduce_mode,
        volatility_reduce_only_when_losing=volatility_reduce_only_when_losing,
        volatility_wind_down_after_reduce=volatility_wind_down_after_reduce,
        volatility_resume_after_normal_bars=volatility_resume_after_normal_bars,
    )


def _locked_policy() -> tuple[
    list[ParameterSet],
    dict[str, SymbolResearchPolicy],
    WindDownMakerPolicy,
]:
    btc = ParameterSet(
        range_multiplier=1.25,
        min_step_pct=0.0018,
        stop_buffer_pct=0.02,
        direction_mode=GridDirectionMode.NEUTRAL,
    )
    eth = ParameterSet(
        range_multiplier=1.00,
        min_step_pct=0.0018,
        stop_buffer_pct=0.02,
        direction_mode=GridDirectionMode.NEUTRAL,
    )
    policies = {
        "BTCUSDT": SymbolResearchPolicy(
            parameter=btc,
            max_inventory_notional=200.0,
            max_unpaired_lots_per_side=1,
            reduce_target_step_fraction=0.50,
        ),
        "ETHUSDT": SymbolResearchPolicy(
            parameter=eth,
            max_inventory_notional=120.0,
            entry_filter=EntryFilter(0.50, 1.05, 0.25),
            max_unpaired_lots_per_side=0,
            reduce_target_step_fraction=1.00,
        ),
    }
    return [btc, eth], policies, WindDownMakerPolicy(5, 1.10, 1.00)


def _base_research_config() -> ResearchConfig:
    return ResearchConfig(
        capital_per_symbol=400.0,
        capital_by_symbol={"BTCUSDT": 500.0, "ETHUSDT": 300.0},
        maker_fill_probability=0.65,
        wind_down_bars=1440,
        unpaired_lot_cap_enforcement="BAR_BOUNDARY",
    )


def _load_data(
    manifests: Sequence[str],
    config: ResearchConfig,
) -> tuple[list[dict[str, Any]], list[WeekendWindow]]:
    metadata: list[dict[str, Any]] = []
    windows: list[WeekendWindow] = []
    for manifest in manifests:
        item = verify_frozen_dataset(manifest)
        metadata.append(item)
        windows.extend(
            load_weekend_windows(
                manifest,
                observation_rows=config.observation_rows,
                force_close_minutes=config.force_close_minutes,
                minimum_tradable_rows=config.minimum_tradable_rows,
                verified_manifest=item,
            )
        )
    symbols = {str(item.get("symbol") or "").upper() for item in metadata}
    if symbols != {"BTCUSDT", "ETHUSDT"}:
        raise ValueError("利润保护研究必须且只能提供 BTCUSDT、ETHUSDT 数据集。")
    return metadata, windows


def _evaluate_candidate(
    candidate: ProfitCandidate,
    *,
    windows: Sequence[WeekendWindow],
    metadata: Sequence[dict[str, Any]],
    base_config: ResearchConfig,
    parameters: Sequence[ParameterSet],
    symbol_policies: dict[str, SymbolResearchPolicy],
    maker_policy: WindDownMakerPolicy,
    split_ids: dict[str, Sequence[str]],
    seeds: Sequence[int],
    cost: tuple[float, float, float] = BASE_COST,
    executor: concurrent.futures.ProcessPoolExecutor | None = None,
) -> CandidateEvidence:
    if executor is not None:
        futures = [
            executor.submit(
                _evaluate_seed_worker,
                candidate,
                int(seed),
                split_ids,
                cost,
            )
            for seed in seeds
        ]
        runs = {}
        for future in concurrent.futures.as_completed(futures):
            seed, seed_runs = future.result()
            runs[seed] = seed_runs
        return CandidateEvidence(
            candidate,
            {seed: runs[seed] for seed in sorted(runs)},
        )
    maker_fee, taker_fee, slippage_bps = cost
    research = RobustnessResearch(
        windows,
        parameters,
        candidate.research_config(base_config),
        dataset_metadata=metadata,
    )
    runs: dict[int, dict[str, tuple[AggregateMetrics, list[WindowResult]]]] = {}
    for seed in seeds:
        runs[int(seed)] = {}
        for split_name, window_ids in split_ids.items():
            runs[int(seed)][split_name] = research.evaluate_joint_policy_windows(
                symbol_policies,
                maker_policy,
                window_ids,
                maker_fee_rate=maker_fee,
                taker_fee_rate=taker_fee,
                stop_slippage_bps=slippage_bps,
                fill_seed_salt=int(seed),
            )
    return CandidateEvidence(candidate, runs)


def _portfolio_window_pnls(
    evidence: CandidateEvidence,
    split_names: set[str],
) -> list[float]:
    values: list[float] = []
    for seed_runs in evidence.runs.values():
        for split_name, (_metrics, results) in seed_runs.items():
            if split_name not in split_names:
                continue
            grouped: dict[str, float] = {}
            for result in results:
                grouped[result.window_id] = grouped.get(result.window_id, 0.0) + result.pnl
            values.extend(grouped.values())
    return values


def _tail_mean(values: Sequence[float], fraction: float = 0.05) -> float:
    if not values:
        return 0.0
    count = max(1, math.ceil(len(values) * fraction))
    return statistics.mean(sorted(float(value) for value in values)[:count])


def _median_optional(values: Iterable[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    return statistics.median(present) if present else None


def _summarize(
    evidence: CandidateEvidence,
    split_names: set[str],
    market_states: dict[str, str],
) -> dict[str, Any]:
    selected_runs = [
        (seed, split_name, metrics, results)
        for seed, seed_runs in evidence.runs.items()
        for split_name, (metrics, results) in seed_runs.items()
        if split_name in split_names
    ]
    results = [
        result
        for _seed, _split, _metrics, items in selected_runs
        for result in items
    ]
    traded = [item for item in results if item.status == "TRADED"]
    profitable = [item for item in traded if item.profit_peak_net_pnl > 0]
    activated = [
        item for item in traded if item.profit_protection_activation_count > 0
    ]
    seed_pnl = {
        str(seed): sum(
            metrics.total_pnl
            for run_seed, _split, metrics, _items in selected_runs
            if run_seed == seed
        )
        for seed in sorted(evidence.runs)
    }
    state_pnl: dict[str, float] = {}
    symbol_pnl: dict[str, float] = {}
    for item in results:
        state = market_states[item.window_id]
        state_pnl[state] = state_pnl.get(state, 0.0) + item.pnl
        symbol_pnl[item.symbol] = symbol_pnl.get(item.symbol, 0.0) + item.pnl
    gross_positive = sum(max(0.0, item.gross_grid_pnl) for item in traded)
    fees = sum(item.fees_paid for item in traded)
    window_pnls = _portfolio_window_pnls(evidence, split_names)
    return {
        "run_count": len(selected_runs),
        "traded_symbol_windows": len(traded),
        "mean_seed_total_pnl": statistics.mean(seed_pnl.values()) if seed_pnl else 0.0,
        "worst_seed_total_pnl": min(seed_pnl.values()) if seed_pnl else 0.0,
        "positive_seed_count": sum(value > 0 for value in seed_pnl.values()),
        "seed_total_pnl": seed_pnl,
        "worst_5pct_window_mean_pnl": _tail_mean(window_pnls),
        "cvar_95_pnl": _tail_mean(window_pnls),
        "max_drawdown_pct": max(
            (metrics.max_drawdown_pct for _s, _n, metrics, _r in selected_runs),
            default=0.0,
        ),
        "worst_best_window_concentration": max(
            (
                metrics.best_window_concentration
                for _s, _n, metrics, _r in selected_runs
            ),
            default=0.0,
        ),
        "profitable_to_losing_ratio": (
            sum(item.profitable_to_losing_count for item in profitable)
            / len(profitable)
            if profitable
            else 0.0
        ),
        "activated_positive_lock_ratio": (
            sum(item.pnl > 0 for item in activated) / len(activated)
            if activated
            else 0.0
        ),
        "median_peak_profit_giveback_pct": (
            statistics.median(item.peak_profit_giveback_pct for item in profitable)
            if profitable
            else 0.0
        ),
        "max_peak_net_pnl": max(
            (item.profit_peak_net_pnl for item in profitable),
            default=0.0,
        ),
        "p90_peak_profit_giveback_pct": (
            _tail_quantile(
                [item.peak_profit_giveback_pct for item in profitable],
                0.90,
            )
            if profitable
            else 0.0
        ),
        "profit_protection_activation_count": sum(
            item.profit_protection_activation_count for item in traded
        ),
        "profit_suppress_count": sum(item.profit_suppress_count for item in traded),
        "profit_reduce_count": sum(item.profit_reduce_count for item in traded),
        "profit_close_count": sum(item.profit_close_count for item in traded),
        "locked_profit_usdt": sum(item.locked_profit_usdt for item in traded),
        "profit_exit_cost": sum(item.profit_exit_cost for item in traded),
        "exit_slippage_cost": sum(item.exit_slippage_cost for item in traded),
        "fees_paid": fees,
        "gross_positive_grid_pnl": gross_positive,
        "fee_to_gross_profit_ratio": fees / gross_positive if gross_positive > 0 else None,
        "median_reduce_inventory_reduction_30_pct": _median_optional(
            item.profit_reduce_inventory_reduction_30_pct for item in traded
        ),
        "median_reduce_inventory_reduction_60_pct": _median_optional(
            item.profit_reduce_inventory_reduction_60_pct for item in traded
        ),
        "median_reduce_inventory_reduction_120_pct": _median_optional(
            item.profit_reduce_inventory_reduction_120_pct for item in traded
        ),
        "max_suppress_inventory_growth_usdt": max(
            (item.profit_suppress_inventory_growth_usdt for item in traded),
            default=0.0,
        ),
        "median_close_net_pnl_error": _median_optional(
            item.profit_close_net_pnl_error for item in traded
        ),
        "profit_passive_reduce_reprice_count": sum(
            item.profit_passive_reduce_reprice_count for item in traded
        ),
        "profit_passive_reduce_fill_count": sum(
            item.profit_passive_reduce_fill_count for item in traded
        ),
        "profit_active_reduce_count": sum(
            item.profit_active_reduce_count for item in traded
        ),
        "profit_active_reduce_pnl": sum(
            item.profit_active_reduce_pnl for item in traded
        ),
        "profit_active_reduce_cost": sum(
            item.profit_active_reduce_cost for item in traded
        ),
        "median_profit_active_reduce_inventory_reduction_pct": _median_optional(
            item.profit_active_reduce_inventory_reduction_pct for item in traded
        ),
        "volatility_breach_count": sum(
            item.volatility_breach_count for item in traded
        ),
        "volatility_max_consecutive_breaches": max(
            (item.volatility_max_consecutive_breaches for item in traded),
            default=0,
        ),
        "volatility_reduce_count": sum(
            item.volatility_reduce_count for item in traded
        ),
        "volatility_reduce_pnl": sum(
            item.volatility_reduce_pnl for item in traded
        ),
        "volatility_reduce_cost": sum(
            item.volatility_reduce_cost for item in traded
        ),
        "median_volatility_reduce_inventory_reduction_pct": _median_optional(
            item.volatility_reduce_inventory_reduction_pct for item in traded
        ),
        "state_pnl": state_pnl,
        "symbol_pnl": symbol_pnl,
    }


def _tail_quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def _summary_payload(
    evidence: CandidateEvidence,
    market_states: dict[str, str],
) -> dict[str, Any]:
    return {
        "candidate": asdict(evidence.candidate),
        "development": _summarize(evidence, {"development"}, market_states),
        "validation": _summarize(evidence, {"validation"}, market_states),
        "combined": _summarize(
            evidence,
            {"development", "validation"},
            market_states,
        ),
        "runs": {
            str(seed): {
                split_name: asdict(metrics)
                for split_name, (metrics, _results) in seed_runs.items()
            }
            for seed, seed_runs in evidence.runs.items()
        },
    }


def _relative_improvement(baseline: float, candidate: float) -> float:
    if baseline < 0:
        return (candidate - baseline) / abs(baseline)
    return 1.0 if candidate >= baseline else -1.0


def _candidate_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    tests_passed: bool,
) -> dict[str, bool]:
    base = baseline["combined"]
    item = candidate["combined"]
    base_losing = float(base["profitable_to_losing_ratio"])
    losing_improvement = (
        (base_losing - float(item["profitable_to_losing_ratio"])) / base_losing
        if base_losing > 0
        else (1.0 if float(item["profitable_to_losing_ratio"]) <= 0 else -1.0)
    )
    base_range = float(base["state_pnl"].get("RANGE", 0.0))
    candidate_range = float(item["state_pnl"].get("RANGE", 0.0))
    range_retention = (
        candidate_range / base_range
        if base_range > 0
        else (1.0 if candidate_range >= base_range else -1.0)
    )
    base_fee_ratio = base["fee_to_gross_profit_ratio"]
    candidate_fee_ratio = item["fee_to_gross_profit_ratio"]
    fee_ok = (
        candidate_fee_ratio is not None
        and (
            base_fee_ratio is None
            or float(candidate_fee_ratio)
            <= max(float(base_fee_ratio) * 1.15, float(base_fee_ratio) + 0.05)
        )
    )
    symbol_ok = True
    for symbol, base_pnl in base["symbol_pnl"].items():
        candidate_pnl = float(item["symbol_pnl"].get(symbol, 0.0))
        allowed_deterioration = max(5.0, abs(float(base_pnl)) * 0.30)
        if candidate_pnl < float(base_pnl) - allowed_deterioration:
            symbol_ok = False
    baseline_drawdown = float(base["max_drawdown_pct"])
    candidate_drawdown = float(item["max_drawdown_pct"])
    return {
        "activation_observed": int(item["profit_protection_activation_count"]) > 0,
        "profitable_to_losing_reduction_ge_30pct": losing_improvement >= 0.30,
        "worst_5pct_loss_improvement_ge_20pct": _relative_improvement(
            float(base["worst_5pct_window_mean_pnl"]),
            float(item["worst_5pct_window_mean_pnl"]),
        )
        >= 0.20,
        "max_drawdown_not_worse_than_5pct": candidate_drawdown
        <= (baseline_drawdown * 1.05 if baseline_drawdown > 0 else 0.0),
        "range_profit_retention_ge_75pct": range_retention >= 0.75,
        "median_giveback_le_45pct": float(
            item["median_peak_profit_giveback_pct"]
        )
        <= 0.45,
        "positive_seed_count_ge_4": int(item["positive_seed_count"]) >= 4,
        "both_symbols_no_catastrophic_deterioration": symbol_ok,
        "fee_ratio_not_materially_worse": fee_ok,
        "best_window_concentration_le_35pct": float(
            item["worst_best_window_concentration"]
        )
        <= 0.35,
        "full_pytest_passed": tests_passed,
    }


def _development_rank(summary: dict[str, Any]) -> tuple[float, ...]:
    item = summary["development"]
    return (
        float(item["positive_seed_count"]),
        -float(item["profitable_to_losing_ratio"]),
        float(item["worst_5pct_window_mean_pnl"]),
        float(item["mean_seed_total_pnl"]),
        -float(item["max_drawdown_pct"]),
    )


def _classify_market_state(window: WeekendWindow) -> str:
    rows = list(window.rows[window.observation_rows :])
    closes = [float(row.close) for row in rows]
    if len(closes) < 8:
        return "TRANSITION"
    returns = [math.log(current / previous) for previous, current in zip(closes, closes[1:])]
    midpoint = max(2, len(returns) // 2)
    first = returns[:midpoint]
    second = returns[midpoint:]
    first_vol = statistics.pstdev(first) if len(first) > 1 else 0.0
    second_vol = statistics.pstdev(second) if len(second) > 1 else 0.0
    expansion = second_vol / max(first_vol, 1e-12)
    total_move = sum(returns)
    path = sum(abs(value) for value in returns)
    efficiency = abs(total_move) / max(path, 1e-12)
    first_move = sum(first)
    second_move = sum(second)
    sign_flip = first_move * second_move < 0
    if expansion >= 1.50:
        return "VOLATILITY_EXPANSION"
    if sign_flip and min(abs(first_move), abs(second_move)) >= 0.0025:
        return "TRANSITION"
    if efficiency >= 0.55 and abs(total_move) >= 0.005:
        return "UP_TREND" if total_move > 0 else "DOWN_TREND"
    return "RANGE"


def _run_pytest(repo_root: Path, *, skip: bool) -> dict[str, Any]:
    if skip:
        return {"command": "python -m pytest -q", "passed": False, "skipped": True}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )
    combined = (completed.stdout + "\n" + completed.stderr).strip()
    return {
        "command": "python -m pytest -q",
        "passed": completed.returncode == 0,
        "skipped": False,
        "returncode": completed.returncode,
        "tail": "\n".join(combined.splitlines()[-12:]),
    }


def _stress_passed(summary: dict[str, Any]) -> bool:
    combined = summary["combined"]
    return (
        int(combined["positive_seed_count"]) >= 4
        and float(combined["max_drawdown_pct"]) <= 0.05
        and float(combined["worst_seed_total_pnl"]) > -0.05 * TOTAL_CAPITAL
    )


def _window_results(evidence: CandidateEvidence, seed: int) -> list[WindowResult]:
    return [
        result
        for split_name in ("development", "validation")
        for result in evidence.runs[seed][split_name][1]
    ]


def _walk_forward_rows(
    evidences: Sequence[CandidateEvidence],
    ordered_window_ids: Sequence[str],
    seeds: Sequence[int],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    train = 26
    test = 8
    step = 8
    capital = {"BTCUSDT": 500.0, "ETHUSDT": 300.0}
    for start in range(0, len(ordered_window_ids) - train - test + 1, step):
        test_ids = set(ordered_window_ids[start + train : start + train + test])
        fold = start // step + 1
        for evidence in evidences:
            for seed in seeds:
                selected = [
                    item for item in _window_results(evidence, int(seed))
                    if item.window_id in test_ids
                ]
                metrics = aggregate_joint_results(selected, capital_by_symbol=capital)
                rows.append({
                    "fold": fold,
                    "candidate_id": evidence.candidate.candidate_id,
                    "seed": seed,
                    "test_start": ordered_window_ids[start + train],
                    "test_end": ordered_window_ids[start + train + test - 1],
                    "total_pnl": metrics.total_pnl,
                    "max_drawdown_pct": metrics.max_drawdown_pct,
                    "profit_factor": metrics.profit_factor,
                    "positive_window_ratio": metrics.positive_window_ratio,
                })
    return rows


def _state_breakdown_rows(
    evidences: Sequence[CandidateEvidence],
    market_states: dict[str, str],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str, str, str], list[WindowResult]] = {}
    for evidence in evidences:
        for seed, seed_runs in evidence.runs.items():
            for split_name, (_metrics, results) in seed_runs.items():
                for result in results:
                    key = (
                        evidence.candidate.candidate_id,
                        seed,
                        split_name,
                        market_states[result.window_id],
                        result.symbol,
                    )
                    grouped.setdefault(key, []).append(result)
    rows: list[dict[str, Any]] = []
    for key, results in sorted(grouped.items()):
        candidate_id, seed, split_name, state, symbol = key
        rows.append({
            "candidate_id": candidate_id,
            "seed": seed,
            "split": split_name,
            "state": state,
            "symbol": symbol,
            "window_count": len(results),
            "traded_count": sum(item.status == "TRADED" for item in results),
            "total_pnl": sum(item.pnl for item in results),
            "positive_ratio": sum(item.pnl > 0 for item in results) / len(results),
            "activation_count": sum(
                item.profit_protection_activation_count for item in results
            ),
            "close_count": sum(item.profit_close_count for item in results),
        })
    return rows


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["empty"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _markdown_table(rows: Sequence[Sequence[Any]], headers: Sequence[str]) -> str:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(output)


def _write_reports(
    output_dir: Path,
    *,
    metadata: Sequence[dict[str, Any]],
    split: Any,
    windows: Sequence[WeekendWindow],
    market_states: dict[str, str],
    candidate_payloads: dict[str, dict[str, Any]],
    evidences: dict[str, CandidateEvidence],
    checks: dict[str, dict[str, bool]],
    selected_id: str,
    stress_summary: dict[str, Any],
    stress_ok: bool,
    test_result: dict[str, Any],
    consumed_oos: dict[str, Any] | None,
    seeds: Sequence[int],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = candidate_payloads[selected_id]
    selected_checks = checks[selected_id]
    base_pass = all(selected_checks.values())
    robust_candidate = base_pass and stress_ok
    final_oos_status = (
        "CONSUMED_RESEARCH_VALIDATION_ONLY"
        if consumed_oos is not None
        else "SEALED_NOT_EVALUATED"
    )
    reduce_60 = selected["combined"]["median_reduce_inventory_reduction_60_pct"]
    p3_required = (
        int(selected["combined"]["profit_reduce_count"]) >= 3
        and (reduce_60 is None or float(reduce_60) < 0.10)
    )

    parameter_rows: list[dict[str, Any]] = []
    for candidate_id, payload in candidate_payloads.items():
        candidate = payload["candidate"]
        combined = payload["combined"]
        item_checks = checks.get(candidate_id, {})
        parameter_rows.append({
            "candidate_id": candidate_id,
            "round": candidate["round_name"],
            "mode": candidate["mode"],
            "activation_usdt": candidate["activation_usdt"],
            "minimum_locked_ratio": candidate["minimum_locked_ratio"],
            "suppress_drawdown_pct": candidate["suppress_drawdown_pct"],
            "reduce_drawdown_pct": candidate["reduce_drawdown_pct"],
            "close_drawdown_pct": candidate["close_drawdown_pct"],
            "passive_reduce_after_bars": candidate["passive_reduce_after_bars"],
            "active_reduce_after_bars": candidate["active_reduce_after_bars"],
            "passive_reduce_fraction": candidate["passive_reduce_fraction"],
            "active_reduce_fraction": candidate["active_reduce_fraction"],
            "mean_seed_total_pnl": combined["mean_seed_total_pnl"],
            "worst_seed_total_pnl": combined["worst_seed_total_pnl"],
            "positive_seed_count": combined["positive_seed_count"],
            "worst_5pct_window_mean_pnl": combined["worst_5pct_window_mean_pnl"],
            "max_drawdown_pct": combined["max_drawdown_pct"],
            "profitable_to_losing_ratio": combined["profitable_to_losing_ratio"],
            "median_peak_profit_giveback_pct": combined["median_peak_profit_giveback_pct"],
            "activation_count": combined["profit_protection_activation_count"],
            "reduce_count": combined["profit_reduce_count"],
            "close_count": combined["profit_close_count"],
            "passed_gate_count": sum(item_checks.values()),
            "all_base_gates_passed": bool(item_checks) and all(item_checks.values()),
        })
    _write_csv(output_dir / "parameter-search.csv", parameter_rows)

    ordered_ids = [
        item.window_id
        for item in sorted(
            {item.window_id: item for item in windows}.values(),
            key=lambda item: item.market_close,
        )
        if item.window_id in set(split.development) | set(split.validation)
    ]
    baseline_ids = ["P0_OFF", "P1_FIXED_A10", "P2_DEFAULT_A10"]
    report_evidences = [evidences[item] for item in baseline_ids if item in evidences]
    if selected_id not in baseline_ids:
        report_evidences.append(evidences[selected_id])
    walk_rows = _walk_forward_rows(report_evidences, ordered_ids, seeds)
    state_rows = _state_breakdown_rows(report_evidences, market_states)
    _write_csv(output_dir / "walk-forward.csv", walk_rows)
    _write_csv(output_dir / "state-breakdown.csv", state_rows)

    audit_lines = [
        "# 利润保护数据审计",
        "",
        f"生成时间：{datetime.now(UTC).isoformat()}",
        "",
        _markdown_table(
            [
                (
                    item.get("symbol"),
                    item.get("actual_start"),
                    item.get("actual_end"),
                    item.get("row_count"),
                    item.get("missing_ratio"),
                    item.get("official_checksums_verified"),
                )
                for item in metadata
            ],
            ("标的", "起始", "结束", "行数", "缺失率", "官方校验"),
        ),
        "",
        f"- Development 窗口：{len(split.development)}",
        f"- Validation 窗口：{len(split.validation)}",
        f"- Final OOS 窗口：{len(split.final_oos)}",
        f"- Final OOS 状态：`{final_oos_status}`",
        "- 本轮只评估 Development 与 Validation；Final OOS 未参与参数选择或门槛计算。",
        "- 市场状态为事后报告标签，不参与任何交易决策。",
    ]
    (output_dir / "data-audit.md").write_text("\n".join(audit_lines) + "\n", encoding="utf-8")

    baseline_rows = []
    for item_id in baseline_ids:
        payload = candidate_payloads[item_id]["combined"]
        baseline_rows.append((
            item_id,
            f"{payload['mean_seed_total_pnl']:.4f}",
            f"{payload['worst_5pct_window_mean_pnl']:.4f}",
            f"{payload['max_drawdown_pct']:.2%}",
            f"{payload['profitable_to_losing_ratio']:.2%}",
            payload["positive_seed_count"],
        ))
    baseline_md = "\n".join([
        "# P0 / P1 / P2 基线对照",
        "",
        _markdown_table(
            baseline_rows,
            ("方案", "六种子平均净收益", "最差 5% 窗口", "最坏回撤", "盈利转亏率", "正收益种子"),
        ),
        "",
        "P1 仅为固定净利润止盈诊断，不作为生产候选。",
    ])
    (output_dir / "baseline.md").write_text(baseline_md + "\n", encoding="utf-8")

    check_rows = [(name, "PASS" if passed else "FAIL") for name, passed in selected_checks.items()]
    conclusion = (
        "存在通过开发/验证与 COST_50 压力门槛的测试网候选；仍需新的未查看 Final OOS。"
        if robust_candidate
        else "本轮没有稳健候选，保持生产参数不变。"
    )
    final_lines = [
        "# 利润保护优化最终报告",
        "",
        f"结论：**{conclusion}**",
        "",
        f"- 诊断最优候选：`{selected_id}`",
        f"- Final OOS：`{final_oos_status}`",
        f"- 固定种子：{', '.join(str(value) for value in seeds)}",
        "- 成交模型：L0_CONSERVATIVE，Maker 概率 65%，BASE 费用与 COST_50 压力。",
        "- 策略方向：NEUTRAL；未调整杠杆、网格层数、趋势阈值或真实费用。",
        "",
        "## 门槛",
        "",
        _markdown_table(check_rows, ("检查", "结果")),
        "",
        f"- COST_50 压力通过：{stress_ok}",
        f"- 完整 pytest：{test_result.get('passed', False)}",
        f"- P3 主动分批减仓是否需要：{p3_required}",
        "",
        "## 下一步",
        "",
        (
            "P2 的 REDUCE 触发后 60 分钟库存下降不足 10%，下一轮应实现 P3 主动分批减仓，不再继续下调 REDUCE 阈值。"
            if p3_required
            else "保留当前生产参数；等待新的完整未查看 OOS 窗口后再执行一次锁定验证。"
        ),
    ]
    (output_dir / "final-report.md").write_text("\n".join(final_lines) + "\n", encoding="utf-8")

    results_payload = {
        "schema_version": "profit-protection-v2.3",
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "interval": "1m",
            "direction_mode": "NEUTRAL",
            "seed_salts": list(seeds),
            "final_oos_status": final_oos_status,
            "final_oos_reuse_for_tuning_forbidden": True,
            "market_state_definition": (
                "事后标签：后半段波动/前半段>=1.5 为 VOLATILITY_EXPANSION；"
                "半程方向反转且两段绝对收益>=0.25% 为 TRANSITION；"
                "方向效率>=0.55 且净移动>=0.5% 为 UP/DOWN_TREND；其余 RANGE。"
            ),
        },
        "datasets": [
            {
                key: item.get(key)
                for key in (
                    "dataset_id", "symbol", "interval", "actual_start", "actual_end",
                    "row_count", "missing_ratio", "file_sha256", "official_checksums_verified",
                )
            }
            for item in metadata
        ],
        "split": {
            "development": {"count": len(split.development)},
            "validation": {"count": len(split.validation)},
            "final_oos": {"count": len(split.final_oos), "status": final_oos_status},
        },
        "candidates": candidate_payloads,
        "candidate_checks": checks,
        "selected_candidate_id": selected_id,
        "selected_cost_50": stress_summary,
        "selected_cost_50_passed": stress_ok,
        "pytest": test_result,
        "p3_active_reduce_required": p3_required,
        "robust_research_candidate": robust_candidate,
        "testnet_recommended": robust_candidate,
        "production_defaults_changed": False,
        "conclusion": conclusion,
    }
    (output_dir / "results.json").write_text(
        json.dumps(results_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results_payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只在 Development/Validation 上执行利润保护 P0/P1/P2 与受约束优化。"
    )
    parser.add_argument("manifests", nargs=2)
    parser.add_argument("--seeds", default=",".join(str(value) for value in DEFAULT_SEEDS))
    parser.add_argument("--max-rounds", type=int, choices=(0, 1, 2, 3), default=3)
    parser.add_argument("--report-dir", default="reports/profit-protection")
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="按 seed 并行的独立进程数，默认最多 4。",
    )
    parser.add_argument(
        "--consumed-final-oos-report",
        default="reports/robustness/btc-eth-final-oos-locked-20260720.json",
    )
    parser.add_argument("--skip-tests", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    seeds = [int(value.strip()) for value in args.seeds.split(",") if value.strip()]
    if not seeds:
        raise ValueError("至少需要一个固定种子。")
    if args.workers <= 0:
        raise ValueError("workers 必须大于 0。")
    repo_root = Path(__file__).resolve().parents[1]
    base_config = _base_research_config()
    metadata, windows = _load_data(args.manifests, base_config)
    split = split_window_ids(
        windows,
        dev_ratio=base_config.dev_ratio,
        validation_ratio=base_config.validation_ratio,
        min_windows_per_split=base_config.min_windows_per_split,
    )
    split_ids = {
        "development": split.development,
        "validation": split.validation,
    }
    market_states = {
        item.window_id: _classify_market_state(item)
        for item in windows
    }
    parameters, symbol_policies, maker_policy = _locked_policy()
    evidences: dict[str, CandidateEvidence] = {}
    payloads: dict[str, dict[str, Any]] = {}

    executor = concurrent.futures.ProcessPoolExecutor(
        max_workers=min(args.workers, len(seeds)),
        initializer=_initialize_worker,
        initargs=(tuple(args.manifests), base_config),
    )

    def evaluate(candidate: ProfitCandidate) -> None:
        if candidate.candidate_id in evidences:
            return
        for existing in evidences.values():
            if replace(existing.candidate, candidate_id="", round_name="") == replace(
                candidate,
                candidate_id="",
                round_name="",
            ):
                print(
                    f"REUSING {existing.candidate.candidate_id} AS {candidate.candidate_id}",
                    flush=True,
                )
                evidence = CandidateEvidence(candidate, existing.runs)
                evidences[candidate.candidate_id] = evidence
                payloads[candidate.candidate_id] = _summary_payload(
                    evidence,
                    market_states,
                )
                return
        print(f"EVALUATING {candidate.candidate_id}", flush=True)
        evidence = _evaluate_candidate(
            candidate,
            windows=windows,
            metadata=metadata,
            base_config=base_config,
            parameters=parameters,
            symbol_policies=symbol_policies,
            maker_policy=maker_policy,
            split_ids=split_ids,
            seeds=seeds,
            executor=executor,
        )
        evidences[candidate.candidate_id] = evidence
        payloads[candidate.candidate_id] = _summary_payload(evidence, market_states)

    def reuse(source_id: str, candidate: ProfitCandidate, reason: str) -> None:
        print(f"REUSING {source_id} AS {candidate.candidate_id}: {reason}", flush=True)
        evidence = CandidateEvidence(candidate, evidences[source_id].runs)
        evidences[candidate.candidate_id] = evidence
        payloads[candidate.candidate_id] = _summary_payload(evidence, market_states)

    evaluate(_candidate("P0_OFF", "baseline", enabled=False, mode="OFF"))
    p0_max_peak = float(payloads["P0_OFF"]["combined"]["max_peak_net_pnl"])
    fixed = _candidate("P1_FIXED_A10", "baseline", mode="FIXED_CLOSE")
    if p0_max_peak < fixed.activation_usdt:
        reuse(
            "P0_OFF",
            fixed,
            f"P0 最大净利润峰值 {p0_max_peak:.6f} < 激活线 {fixed.activation_usdt:.6f}",
        )
    else:
        evaluate(fixed)
    default = _candidate("P2_DEFAULT_A10", "baseline")
    if p0_max_peak < default.activation_usdt:
        reuse(
            "P0_OFF",
            default,
            f"P0 最大净利润峰值 {p0_max_peak:.6f} < 激活线 {default.activation_usdt:.6f}",
        )
    else:
        evaluate(default)

    activation_candidates = [default]
    if args.max_rounds >= 1:
        for activation in (2.0, 4.0, 6.0):
            item = _candidate(
                f"R1_A{int(activation)}",
                "round_1_activation",
                activation_usdt=activation,
            )
            if p0_max_peak < item.activation_usdt:
                reuse(
                    "P0_OFF",
                    item,
                    f"P0 最大净利润峰值 {p0_max_peak:.6f} < 激活线 {item.activation_usdt:.6f}",
                )
            else:
                evaluate(item)
            activation_candidates.append(item)
    best_activation = max(
        activation_candidates,
        key=lambda item: _development_rank(payloads[item.candidate_id]),
    )

    close_candidates = [best_activation]
    if args.max_rounds >= 2:
        for close in (0.40, 0.45, 0.50, 0.55, 0.60):
            item = _candidate(
                f"R2_A{best_activation.activation_usdt:g}_C{int(close * 100)}",
                "round_2_close_drawdown",
                activation_usdt=best_activation.activation_usdt,
                close_drawdown_pct=close,
            )
            evaluate(item)
            close_candidates.append(item)
    best_close = max(
        close_candidates,
        key=lambda item: _development_rank(payloads[item.candidate_id]),
    )

    lock_candidates = [best_close]
    if args.max_rounds >= 3:
        for locked in (0.20, 0.30, 0.40):
            item = _candidate(
                (
                    f"R3_A{best_close.activation_usdt:g}_"
                    f"C{int(best_close.close_drawdown_pct * 100)}_L{int(locked * 100)}"
                ),
                "round_3_minimum_lock",
                activation_usdt=best_close.activation_usdt,
                close_drawdown_pct=best_close.close_drawdown_pct,
                minimum_locked_ratio=locked,
            )
            evaluate(item)
            lock_candidates.append(item)

    provisional_checks = {
        candidate_id: _candidate_checks(
            payloads["P0_OFF"],
            payload,
            tests_passed=True,
        )
        for candidate_id, payload in payloads.items()
        if payload["candidate"]["mode"] == "PEAK_DRAWDOWN"
    }
    ranked_ids = sorted(
        provisional_checks,
        key=lambda candidate_id: (
            all(provisional_checks[candidate_id].values()),
            sum(provisional_checks[candidate_id].values()),
            _development_rank(payloads[candidate_id]),
        ),
        reverse=True,
    )
    selected_id = ranked_ids[0]
    selected_candidate = evidences[selected_id].candidate
    print(f"STRESSING {selected_id} COST_50", flush=True)
    stress_evidence = _evaluate_candidate(
        selected_candidate,
        windows=windows,
        metadata=metadata,
        base_config=base_config,
        parameters=parameters,
        symbol_policies=symbol_policies,
        maker_policy=maker_policy,
        split_ids=split_ids,
        seeds=seeds,
        cost=COST_50,
        executor=executor,
    )
    executor.shutdown(wait=True, cancel_futures=False)
    stress_summary = _summary_payload(stress_evidence, market_states)
    stress_ok = _stress_passed(stress_summary)
    pytest_result = _run_pytest(repo_root, skip=args.skip_tests)
    checks = {
        candidate_id: _candidate_checks(
            payloads["P0_OFF"],
            payload,
            tests_passed=bool(pytest_result.get("passed")),
        )
        for candidate_id, payload in payloads.items()
        if payload["candidate"]["mode"] == "PEAK_DRAWDOWN"
    }

    consumed_path = (repo_root / args.consumed_final_oos_report).resolve()
    consumed_oos = (
        json.loads(consumed_path.read_text(encoding="utf-8"))
        if consumed_path.exists()
        else None
    )
    results = _write_reports(
        (repo_root / args.report_dir).resolve(),
        metadata=metadata,
        split=split,
        windows=windows,
        market_states=market_states,
        candidate_payloads=payloads,
        evidences=evidences,
        checks=checks,
        selected_id=selected_id,
        stress_summary=stress_summary,
        stress_ok=stress_ok,
        test_result=pytest_result,
        consumed_oos=consumed_oos,
        seeds=seeds,
    )
    print(f"SELECTED {selected_id}")
    print(f"ROBUST_RESEARCH_CANDIDATE {results['robust_research_candidate']}")
    print(f"CONCLUSION {results['conclusion']}")
    print(f"REPORT_DIR {(repo_root / args.report_dir).resolve()}")


if __name__ == "__main__":
    main()
