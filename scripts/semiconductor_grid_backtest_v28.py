"""Phase-1 runner for the pre-registered semiconductor grid v2.8 study.

The runner is intentionally offline-only: it consumes the already-frozen CSV,
funding and exchange-rule snapshots, never touches the exchange, and writes a
catalog hash before evaluating a single combination.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import shutil
import statistics
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.models import GridDirectionMode
from strategy.adaptive_grid import AdaptiveGridConfig
from strategy.backtest import BacktestConfig, run_grid_backtest
from strategy.profit_protection import ProfitProtectionConfig
from strategy.regime import RegimeEngine
from strategy.semiconductor_grid import (
    GridStrategyProfile,
    StrategyAdmissionError,
    build_semiconductor_grid_candidate,
    long_signal_from_mapping,
    symbol_profiles_from_mapping,
)
from strategy.semiconductor_grid_v28 import (
    ANCHOR_IDS,
    Combination,
    catalog_sha256,
    factor_snapshot,
    generate_phase1_covering_array,
    pairwise_audit,
)

from scripts.semiconductor_grid_backtest import (
    RESEARCH_SYMBOLS,
    SEEDS,
    _audit_funding,
    _find_csv,
    _funding_path,
    _grid,
    _load_rules,
    _projected_funding_pct,
    _read_funding,
    _read_klines_with_audit,
    _regime,
    _row_close_dt,
    _scenarios,
    _sha,
    build_calendar_closed_windows,
)


DEFAULT_OUTPUT = Path("reports/semiconductor-grid-backtest-v2.8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行半导体网格 v2.8 Phase 1 回测")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--data-dir", default="data/backtests/semiconductor-v2.7")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--rules-json")
    parser.add_argument("--overwrite", action="store_true")
    # These two switches exist only for fast deterministic CI/smoke execution;
    # any restricted run is labelled NON_OFFICIAL in its manifest.
    parser.add_argument("--max-combinations", type=int, default=0)
    parser.add_argument("--max-windows", type=int, default=0)
    parser.add_argument("--preflight-log-dir", default="")
    return parser


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _adaptive_multiplier(rows: list[dict[str, Any]]) -> float | None:
    """A4 multiplier using only the pre-window observation bars."""
    closes = [float(row["close"]) for row in rows]
    if len(closes) < 61:
        return None
    current = statistics.pstdev(math.log(closes[index] / closes[index - 1]) for index in range(len(closes) - 30, len(closes)))
    history = [
        statistics.pstdev(math.log(closes[index] / closes[index - 1]) for index in range(start, start + 30))
        for start in range(1, len(closes) - 30)
    ]
    if not history:
        return None
    percentile = sum(value <= current for value in history) / len(history)
    if percentile <= 0.40:
        return 1.25
    if percentile <= 0.70:
        return 1.75
    if percentile <= 0.90:
        return 2.25
    return None


def _profile(symbol: str, item: Combination, direction: GridDirectionMode, normal_step: float) -> GridStrategyProfile:
    values = factor_snapshot(item)["grid"]
    minimum_step = float(values.get("minimum_step_pct", normal_step * float(values.get("step_multiplier", 1.0))))
    return GridStrategyProfile(
        name=item.with_direction("L" if direction == GridDirectionMode.LONG else "N"),
        direction_mode=direction,
        min_grid_num=int(values["min_grid_num"]),
        max_grid_num=int(values["max_grid_num"]),
        min_step_pct=minimum_step,
        requires_long_signal=direction == GridDirectionMode.LONG,
    )


def _grid_for(item: Combination, base: AdaptiveGridConfig, observation: list[dict[str, Any]]) -> AdaptiveGridConfig | None:
    data = factor_snapshot(item)["range"]
    multiplier = data["multiplier"]
    if data["adaptive"]:
        multiplier = _adaptive_multiplier(observation)
    if multiplier is None:
        return None
    return replace(
        base,
        k_atr_range=base.k_atr_range * float(multiplier),
        k_sigma_range=base.k_sigma_range * float(multiplier),
        max_range_pct=base.max_range_pct * float(multiplier),
    )


def _backtest_config(
    item: Combination,
    *,
    scenario: Any,
    capital: float,
    leverage: float,
    rule: Any,
    direction: GridDirectionMode,
    seed: int,
) -> BacktestConfig:
    snapshot = factor_snapshot(item)
    profit = snapshot["profit"]
    inventory = snapshot["inventory"]
    stop = snapshot["stop"]
    kwargs: dict[str, Any] = {}
    if profit["mode"] == "FIXED":
        kwargs["fixed_take_profit_usdt"] = capital * float(profit["activation_pct"])
    elif profit["mode"] == "PEAK":
        kwargs["profit_protection"] = ProfitProtectionConfig(
            activation_profit_usdt=capital * float(profit["activation_pct"]),
            minimum_locked_profit_ratio=float(profit["locked"]),
            suppress_drawdown_pct=float(profit["suppress"]),
            reduce_drawdown_pct=float(profit["reduce"]),
            close_drawdown_pct=float(profit["close"]),
        )
    elif profit["mode"] == "INVENTORY_AWARE":
        kwargs.update(
            profit_inventory_activation_usdt=capital * float(profit["activation_pct"]),
            profit_inventory_drag_suppress_ratio=float(profit["suppress_drag"]),
            profit_inventory_drag_reduce_ratio=float(profit["reduce_drag"]),
            profit_inventory_drag_close_ratio=float(profit["close_drag"]),
            profit_peak_close_drawdown_pct=float(profit["peak_close"]),
            # The tracker provides the frozen peak net-profit accounting.  Its
            # ordinary staged thresholds are deliberately inert for C4; C4's
            # actions are controlled by inventory drag plus the 40% hard peak guard.
            profit_protection=ProfitProtectionConfig(
                activation_profit_usdt=capital * float(profit["activation_pct"]),
                suppress_drawdown_pct=0.97,
                reduce_drawdown_pct=0.98,
                close_drawdown_pct=0.99,
            ),
        )
    if item.d == 4:
        kwargs["inventory_drag_close_ratio"] = float(inventory["close_drag"])
    return BacktestConfig(
        capital=capital,
        leverage=leverage,
        maker_fee_rate=float(scenario.maker_fee_rate),
        taker_fee_rate=float(scenario.taker_fee_rate),
        fill_model="L0_CONSERVATIVE",
        min_tick_size=float(rule.tick_size),
        quantity_step_size=float(rule.step_size),
        max_fills_per_bar=int(scenario.max_fills_per_bar),
        maker_fill_probability=float(scenario.maker_fill_probability),
        fill_probability_seed=seed,
        stop_slippage_bps=float(scenario.stop_slippage_bps),
        force_close_at_end=True,
        direction_mode=direction,
        max_inventory_notional=capital,
        inventory_caution_utilization=float(inventory["caution"]),
        inventory_critical_utilization=0.95,
        inventory_reduce_only_utilization=float(inventory["reduce_only"]),
        max_unpaired_lots_per_side=8,
        stop_atr_buffer=float(stop["atr_buffer"]),
        stop_time_confirm_bars=int(stop.get("confirm_bars", 0)),
        **kwargs,
    )


def _row(
    *,
    item: Combination,
    symbol: str,
    direction: GridDirectionMode,
    scenario: str,
    seed: int,
    window: Any,
    result: Any,
    candidate: Any,
    capital: float,
) -> dict[str, Any]:
    paired = float(result.paired_grid_pnl)
    drag = max(0.0, -float(result.pre_exit_unrealized_pnl))
    viability = candidate.viability.snapshot
    return {
        "combination_id": item.id,
        "direction": direction.value,
        "symbol": symbol,
        "scenario": scenario,
        "seed": seed,
        "window_key": window.window_key,
        "force_close_at": window.force_close_at.isoformat(),
        "paired_grid_pnl": paired,
        "inventory_realized_pnl": result.inventory_realized_pnl,
        "net_pnl": result.total_pnl,
        "profit_factor_component": max(result.total_pnl, 0.0),
        "loss_component": max(-result.total_pnl, 0.0),
        "max_drawdown": result.max_drawdown,
        "max_drawdown_pct": result.max_drawdown / max(capital, 1e-12),
        "pre_exit_inventory_notional": result.pre_exit_inventory_notional,
        "pre_exit_unrealized_pnl": result.pre_exit_unrealized_pnl,
        "peak_negative_unrealized_pnl": result.peak_negative_unrealized_pnl,
        "inventory_drag": drag,
        "inventory_drag_ratio": drag / max(paired, 0.01),
        "max_inventory_utilization": result.max_inventory_utilization,
        "mean_inventory_utilization": result.mean_inventory_utilization,
        "max_unpaired_lots": result.max_unpaired_lots,
        "max_unpaired_lot_age": result.max_unpaired_lot_age_bars,
        "grid_count": candidate.params.grid_num,
        "step_pct": candidate.params.step_pct,
        "crossings_per_hour": viability.crossings_per_hour,
        "net_capacity_per_hour": viability.net_capacity_per_hour,
        "pair_completion_count": result.pair_completion_count,
        "accepted_fill_count": result.accepted_fill_count,
        "rejected_fill_count": result.rejected_fill_count,
        "take_profit_count": result.take_profit_count,
        "profit_protection_suppress_count": result.profit_protection_suppress_count,
        "profit_protection_reduce_count": result.profit_protection_reduce_count,
        "profit_protection_close_count": result.profit_protection_close_count,
        "stop_loss_count": int(bool(result.stopped_reason and "stop" in result.stopped_reason)),
        "window_force_close_count": result.force_close_count,
        "inventory_forced_exit_count": result.inventory_critical_exit_count,
        "stopped_reason": result.stopped_reason,
        "stopped_at_index": result.stopped_at_index,
        "stopped_at_price": result.stopped_at_price,
    }


def _aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault((row["combination_id"], row["direction"], row["scenario"]), []).append(row)
    output: list[dict[str, Any]] = []
    for (combination_id, direction, scenario), values in sorted(buckets.items()):
        pnls = [float(row["net_pnl"]) for row in values]
        gains = sum(float(row["profit_factor_component"]) for row in values)
        losses = sum(float(row["loss_component"]) for row in values)
        output.append({
            "combination_id": combination_id,
            "direction": direction,
            "scenario": scenario,
            "run_count": len(values),
            "net_pnl": sum(pnls),
            "median_window_pnl": statistics.median(pnls),
            "profit_factor": gains / losses if losses else (gains if gains else 0.0),
            "positive_window_ratio": sum(value > 0 for value in pnls) / len(pnls),
            "inventory_drag_ratio": statistics.fmean(float(row["inventory_drag_ratio"]) for row in values),
            "max_drawdown": max(float(row["max_drawdown"]) for row in values),
        })
    return output


def main() -> None:
    args = _parser().parse_args()
    started = datetime.now(UTC)
    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()) and not args.overwrite:
        raise RuntimeError(f"输出目录非空: {output}")
    output.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    cfg = raw.get("semiconductor_grid", {}) or {}
    profiles = symbol_profiles_from_mapping(cfg.get("symbol_profiles", {}))
    scenarios = _scenarios(cfg.get("execution", {}))
    base_grid = _grid(raw)
    regime = RegimeEngine(_regime(raw.get("regime", {})))
    viability = __import__("scripts.semiconductor_grid_backtest", fromlist=["_viability"])._viability(cfg.get("viability", {}))
    long_signal = long_signal_from_mapping(cfg.get("long_signal", {}))
    capital_base = float(cfg.get("capital_per_symbol", 500))
    leverage = float(cfg.get("economic_leverage", 1))
    observation_rows = int(cfg.get("observation_rows", 180))
    minimum_trade_rows = int(cfg.get("minimum_trade_rows", 120))
    force_close_minutes = int(cfg.get("force_close_minutes", 120))
    data_dir = Path(args.data_dir)
    rules_path = Path(args.rules_json) if args.rules_json else data_dir / "exchange-rules.json"
    if not rules_path.is_file():
        # v2.7.2 committed the exact exchange snapshot alongside its immutable
        # report, while the large data directory intentionally contains only
        # raw bars/funding.  Reuse that frozen artifact, never a live lookup.
        rules_path = Path("reports/semiconductor-grid-backtest-v2.7.2/exchange-rules.json")
    if not rules_path.is_file():
        raise FileNotFoundError("缺少冻结 exchange-rules.json，拒绝运行 v2.8 矩阵。")
    rules = _load_rules(rules_path)

    catalog = list(generate_phase1_covering_array())
    if args.max_combinations:
        catalog = catalog[: args.max_combinations]
    catalog_hash = catalog_sha256(catalog)
    _write_csv(output / "combination-catalog.csv", [{"combination_id": item.id, **factor_snapshot(item)} for item in catalog])
    _write_json(output / "combination-catalog.json", [factor_snapshot(item) for item in catalog])
    _write_csv(
        output / "anchor-combinations.csv",
        [{"combination_id": combination_id} for combination_id in ANCHOR_IDS],
    )
    audit = pairwise_audit(catalog)
    (output / "covering-array-audit.md").write_text("# Covering Array Audit\n\n```json\n" + json.dumps(audit, ensure_ascii=False, indent=2) + "\n```\n", encoding="utf-8")

    runs: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    inputs: list[dict[str, Any]] = []
    for symbol in RESEARCH_SYMBOLS:
        symbol_profile = profiles[symbol]
        bars, audit_data = _read_klines_with_audit(_find_csv(data_dir, symbol))
        funding_path = _funding_path(_find_csv(data_dir, symbol))
        funding = _read_funding(funding_path) if funding_path else []
        _audit_funding(bars, funding, funding_path)
        rule = rules[symbol]
        inputs.append({"symbol": symbol, "csv_sha256": _sha(_find_csv(data_dir, symbol)), "funding_sha256": _sha(funding_path) if funding_path else None, "rows": len(bars), "audit": audit_data})
        windows = [window for window in build_calendar_closed_windows(bars, market_group=symbol_profile.market_group, calendar_name=symbol_profile.calendar_name, market_timezone=symbol_profile.market_timezone, reference_open_time=symbol_profile.reference_open_time, force_close_minutes=force_close_minutes, minimum_trade_minutes=int(cfg.get("minimum_trade_minutes", 120)), observation_minutes=observation_rows, window_key_prefix=symbol_profile.market_group) if window.complete]
        if args.max_windows:
            windows = windows[: args.max_windows]
        for window in windows:
            observation_end = int(window.observation_end.timestamp() * 1000)
            force_close = int(window.force_close_at.timestamp() * 1000)
            observation = [row for row in window.rows if int(row["open_time"]) < observation_end][-observation_rows:]
            trade = [row for row in window.rows if observation_end <= int(row["open_time"]) < force_close]
            if len(observation) < observation_rows or len(trade) < minimum_trade_rows:
                blocked.append({"symbol": symbol, "window_key": window.window_key, "reason": "INSUFFICIENT_WINDOW_ROWS"})
                continue
            funding_events = [event for event in funding if int(trade[0]["open_time"]) <= event.funding_time <= int(trade[-1]["close_time"])]
            previous_rates = [event.funding_rate for event in funding if event.funding_time <= int(observation[-1]["close_time"])]
            funding_rate = previous_rates[-1] if previous_rates else 0.0
            projected = _projected_funding_pct(funding_rate, _row_close_dt(observation[-1]), window.force_close_at)
            for item in catalog:
                for direction in (GridDirectionMode.NEUTRAL, GridDirectionMode.LONG):
                    if direction == GridDirectionMode.LONG and symbol != "SKHYNIXUSDT":
                        continue
                    grid = _grid_for(item, base_grid, observation)
                    if grid is None:
                        blocked.append({"combination_id": item.id, "symbol": symbol, "window_key": window.window_key, "reason": "A4_VOLATILITY_BLOCK"})
                        continue
                    profile = _profile(symbol, item, direction, symbol_profile.normal_min_step_pct)
                    for scenario in scenarios:
                        if item.b in {3, 4} and scenario.maker_fee_rate != 0:
                            blocked.append({"combination_id": item.id, "symbol": symbol, "window_key": window.window_key, "scenario": scenario.name, "reason": "DENSE_REQUIRES_ZERO_MAKER"})
                            continue
                        depth = statistics.fmean(float(row.get("quote_volume") or 0.0) for row in observation[-regime.config.long_window:])
                        decision = regime.evaluate(symbol, observation, spread_pct=symbol_profile.assumed_spread_pct, depth_usdt=depth, funding_rate=funding_rate, data_age_seconds=0.0, expected_step_pct=profile.min_step_pct, include_cost=False, as_of=_row_close_dt(observation[-1]))
                        if not decision.allowed:
                            blocked.append({"combination_id": item.id, "symbol": symbol, "window_key": window.window_key, "scenario": scenario.name, "reason": "REGIME_BLOCKED"})
                            continue
                        try:
                            candidate = build_semiconductor_grid_candidate(symbol_profile=symbol_profile, strategy_profile=profile, klines=observation, current_price=float(observation[-1]["close"]), funding_rate=funding_rate, projected_funding_pct=projected, maker_fee_rate=scenario.maker_fee_rate, regime_score=decision.grid_score, capital=capital_base, leverage=leverage, tick_size=rule.tick_size, step_size=rule.step_size, min_qty=rule.min_qty, min_notional=rule.min_notional, taker_fee_rate=scenario.taker_fee_rate, base_grid_config=grid, viability_config=viability, long_signal_config=long_signal)
                        except (StrategyAdmissionError, ValueError) as exc:
                            blocked.append({"combination_id": item.id, "symbol": symbol, "window_key": window.window_key, "scenario": scenario.name, "reason": str(exc)})
                            continue
                        for seed in SEEDS:
                            capital = capital_base * symbol_profile.capital_multiplier
                            result = run_grid_backtest(candidate.params, trade, current_price=float(observation[-1]["close"]), config=_backtest_config(item, scenario=scenario, capital=capital, leverage=leverage, rule=rule, direction=direction, seed=seed), funding_events=funding_events)
                            record = _row(
                                item=item,
                                symbol=symbol,
                                direction=direction,
                                scenario=scenario.name,
                                seed=seed,
                                window=window,
                                result=result,
                                candidate=candidate,
                                capital=capital,
                            )
                            record["grid_lower"] = candidate.params.lower
                            record["grid_upper"] = candidate.params.upper
                            record["baseline_atr"] = candidate.params.baseline_atr
                            runs.append(record)

    summary = _aggregate(runs)
    _write_csv(output / "phase1-r0-results.csv", runs)
    _write_csv(output / "combination-summary.csv", summary)
    _write_csv(output / "blocked-windows.csv", blocked)
    source_paths = (
        Path(args.config),
        Path(__file__),
        ROOT / "strategy" / "backtest.py",
        ROOT / "strategy" / "semiconductor_grid.py",
        ROOT / "strategy" / "semiconductor_grid_v28.py",
        rules_path,
    )
    _write_json(
        output / "input-hash-manifest.json",
        {
            "data": inputs,
            "files": {str(path): _sha(path) for path in source_paths},
        },
    )
    (output / "exchange-rules.json").write_text(
        rules_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    if args.preflight_log_dir:
        preflight = Path(args.preflight_log_dir)
        for name in (
            "compileall.stdout.log",
            "compileall.stderr.log",
            "pytest.stdout.log",
            "pytest.stderr.log",
        ):
            source = preflight / name
            if source.is_file():
                shutil.copyfile(source, output / name)
    manifest = {"protocol": "v2.8", "phase": "PHASE_1_R0_STATIC_REPAIRED", "started_at_utc": started.isoformat(), "finished_at_utc": datetime.now(UTC).isoformat(), "catalog_sha256": catalog_hash, "catalog_size": len(catalog), "official": not args.max_combinations and not args.max_windows, "seeds": list(SEEDS), "run_count": len(runs), "blocked_count": len(blocked), "python": platform.python_version()}
    manifest["rules_snapshot"] = str(rules_path)
    manifest["rules_snapshot_sha256"] = _sha(rules_path)
    _write_json(output / "run-manifest.json", manifest)
    _write_json(
        output / "repair-manifest.json",
        {
            "r1_multi_session_drawdown_includes_session_equity": True,
            "profit_protection_connected": True,
            "immutable_factor_snapshot": True,
            "combination_catalog_sha256": catalog_hash,
        },
    )
    _write_json(
        output / "dependency-manifest.json",
        {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    )
    _write_json(output / "acceptance-gates.json", {"phase0_ready": True, "pairwise_coverage": audit, "phase1_started": True, "official": manifest["official"]})
    (output / "final-report.md").write_text("# Semiconductor Grid v2.8\n\nPhase 1 R0 run has been generated. Phase 2–5 are intentionally pending the fixed Phase-1 gate.\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "runs": len(runs), "blocked": len(blocked), "catalog_sha256": catalog_hash}, ensure_ascii=False))


if __name__ == "__main__":
    main()
