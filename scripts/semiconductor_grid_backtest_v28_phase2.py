"""Run the frozen v2.8 Phase-2 Controller-faithful matrix."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.models import GridDirectionMode
from strategy.cooldown import CooldownConfig, CooldownEvaluator
from strategy.regime import RegimeEngine
from strategy.semiconductor_grid import (
    StrategyAdmissionError,
    build_semiconductor_grid_candidate,
    long_signal_from_mapping,
    symbol_profiles_from_mapping,
)
from strategy.semiconductor_grid_v28 import (
    Combination,
    select_phase2_profiles,
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
    _run_controller_faithful,
    _scenarios,
    _viability,
    build_calendar_closed_windows,
)
from scripts.semiconductor_grid_backtest_v28 import (
    _backtest_config,
    _grid_for,
    _profile,
    _row,
    _write_csv,
    _write_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行半导体网格 v2.8 Phase 2 R1")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--data-dir", default="data/backtests/semiconductor-v2.7")
    parser.add_argument("--phase1-dir", default="reports/semiconductor-grid-backtest-v2.8")
    parser.add_argument("--output-dir", default="reports/semiconductor-grid-backtest-v2.8")
    parser.add_argument("--rules-json", default="")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--candidate-catalog", default="")
    parser.add_argument("--artifact-prefix", default="phase2")
    parser.add_argument("--result-name", default="phase2-r1-results.csv")
    parser.add_argument("--phase-label", default="PHASE_2_R1_CONTROLLER_FAITHFUL")
    parser.add_argument("--max-profiles", type=int, default=0)
    parser.add_argument("--max-windows", type=int, default=0)
    return parser


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _catalog_hash(rows: list[dict[str, Any]]) -> str:
    payload = "\n".join(
        f"{row['combination_id']}-{row['direction']}" for row in rows
    ).encode("ascii")
    return sha256(payload).hexdigest()


def _scenario_map(scenarios: tuple[Any, ...]) -> dict[str, Any]:
    return {scenario.name: scenario for scenario in scenarios}


def main() -> None:
    args = _parser().parse_args()
    started = datetime.now(UTC)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    phase2_path = output / args.result_name
    if phase2_path.exists() and not args.overwrite:
        raise RuntimeError(f"Phase 2 已存在: {phase2_path}")

    if args.candidate_catalog:
        catalog = _read_csv(Path(args.candidate_catalog))
    else:
        phase1_rows = _read_csv(Path(args.phase1_dir) / "phase1-r0-results.csv")
        catalog = select_phase2_profiles(phase1_rows)
    if args.max_profiles:
        catalog = catalog[: args.max_profiles]
    catalog_hash = _catalog_hash(catalog)
    _write_csv(output / f"{args.artifact_prefix}-candidate-catalog.csv", catalog)
    _write_json(
        output / f"{args.artifact_prefix}-candidate-catalog.json",
        {"sha256": catalog_hash, "profiles": catalog},
    )

    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    cfg = raw.get("semiconductor_grid", {}) or {}
    symbol_profiles = symbol_profiles_from_mapping(cfg.get("symbol_profiles", {}))
    scenarios = _scenarios(cfg.get("execution", {}))
    scenarios_by_name = _scenario_map(scenarios)
    base_grid = _grid(raw)
    regime = RegimeEngine(_regime(raw.get("regime", {})))
    viability = _viability(cfg.get("viability", {}))
    long_signal = long_signal_from_mapping(cfg.get("long_signal", {}))
    capital_base = float(cfg.get("capital_per_symbol", 500))
    leverage = float(cfg.get("economic_leverage", 1))
    observation_rows = int(cfg.get("observation_rows", 180))
    minimum_trade_rows = int(cfg.get("minimum_trade_rows", 120))
    force_close_minutes = int(cfg.get("force_close_minutes", 120))
    minimum_trade_minutes = int(cfg.get("minimum_trade_minutes", 120))
    rolling_regrid_bars = max(
        1, int((raw.get("grid", {}) or {}).get("rolling_regrid_seconds", 7200)) // 60
    )
    risk_raw = raw.get("risk", {}) or {}
    cooldown_raw = raw.get("cooldown", {}) or {}
    timing_raw = raw.get("timing", {}) or {}
    cooldown = CooldownEvaluator(
        CooldownConfig(
            atr_period=int(cooldown_raw.get("atr_period", 14)),
            calm_window_minutes=int(cooldown_raw.get("calm_window_minutes", 30)),
            atr_recovery_ratio=float(cooldown_raw.get("atr_recovery_ratio", 0.80)),
            amplitude_multiplier=float(cooldown_raw.get("amplitude_multiplier", 2.0)),
            min_calm_minutes=int(timing_raw.get("min_calm_minutes", 15)),
        )
    )
    data_dir = Path(args.data_dir)
    rules_path = Path(args.rules_json) if args.rules_json else data_dir / "exchange-rules.json"
    if not rules_path.is_file():
        rules_path = Path("reports/semiconductor-grid-backtest-v2.7.2/exchange-rules.json")
    rules = _load_rules(rules_path)

    runs: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    regrid_rows: list[dict[str, Any]] = []
    profiles_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for selected in catalog:
        direction = GridDirectionMode(str(selected["direction"]))
        for symbol in RESEARCH_SYMBOLS:
            if direction == GridDirectionMode.LONG and symbol != "SKHYNIXUSDT":
                continue
            profiles_by_symbol.setdefault(symbol, []).append(selected)

    for symbol in RESEARCH_SYMBOLS:
        selected_profiles = profiles_by_symbol.get(symbol, [])
        if not selected_profiles:
            continue
        symbol_profile = symbol_profiles[symbol]
        csv_path = _find_csv(data_dir, symbol)
        bars, _audit = _read_klines_with_audit(csv_path)
        funding_path = _funding_path(csv_path)
        funding = _read_funding(funding_path) if funding_path else []
        _audit_funding(bars, funding, funding_path)
        rule = rules[symbol]
        windows = [
            window
            for window in build_calendar_closed_windows(
                bars,
                market_group=symbol_profile.market_group,
                calendar_name=symbol_profile.calendar_name,
                market_timezone=symbol_profile.market_timezone,
                reference_open_time=symbol_profile.reference_open_time,
                force_close_minutes=force_close_minutes,
                minimum_trade_minutes=minimum_trade_minutes,
                observation_minutes=observation_rows,
                window_key_prefix=symbol_profile.market_group,
            )
            if window.complete
        ]
        if args.max_windows:
            windows = windows[: args.max_windows]
        for window in windows:
            observation_end = int(window.observation_end.timestamp() * 1000)
            force_close = int(window.force_close_at.timestamp() * 1000)
            observation = [
                row for row in window.rows if int(row["open_time"]) < observation_end
            ][-observation_rows:]
            trade = [
                row
                for row in window.rows
                if observation_end <= int(row["open_time"]) < force_close
            ]
            if len(observation) < observation_rows or len(trade) < minimum_trade_rows:
                continue
            window_funding = [
                event
                for event in funding
                if int(trade[0]["open_time"])
                <= event.funding_time
                <= int(trade[-1]["close_time"])
            ]
            for selected in selected_profiles:
                item = Combination.parse(str(selected["combination_id"]))
                direction = GridDirectionMode(str(selected["direction"]))
                strategy_profile = _profile(
                    symbol,
                    item,
                    direction,
                    symbol_profile.normal_min_step_pct,
                )
                for scenario_name, scenario in scenarios_by_name.items():
                    if item.b in {3, 4} and scenario.maker_fee_rate != 0:
                        blocked.append(
                            {
                                "combination_id": item.id,
                                "direction": direction.value,
                                "symbol": symbol,
                                "window_key": window.window_key,
                                "scenario": scenario_name,
                                "reason": "DENSE_REQUIRES_ZERO_MAKER",
                            }
                        )
                        continue

                    def rebuild_candidate(
                        visible_observation: list[dict[str, Any]],
                    ) -> tuple[Any, Any] | None:
                        grid = _grid_for(item, base_grid, visible_observation)
                        if grid is None:
                            return None
                        visible_close = _row_close_dt(visible_observation[-1])
                        previous_rates = [
                            event.funding_rate
                            for event in funding
                            if event.funding_time <= int(visible_observation[-1]["close_time"])
                        ]
                        funding_rate = previous_rates[-1] if previous_rates else 0.0
                        projected = _projected_funding_pct(
                            funding_rate, visible_close, window.force_close_at
                        )
                        depth = statistics.fmean(
                            float(row.get("quote_volume") or 0.0)
                            for row in visible_observation[-regime.config.long_window :]
                        )
                        decision = regime.evaluate(
                            symbol,
                            visible_observation,
                            spread_pct=symbol_profile.assumed_spread_pct,
                            depth_usdt=depth,
                            funding_rate=funding_rate,
                            data_age_seconds=0.0,
                            expected_step_pct=strategy_profile.min_step_pct,
                            include_cost=False,
                            as_of=visible_close,
                        )
                        if not decision.allowed:
                            return None
                        try:
                            candidate = build_semiconductor_grid_candidate(
                                symbol_profile=symbol_profile,
                                strategy_profile=strategy_profile,
                                klines=visible_observation,
                                current_price=float(visible_observation[-1]["close"]),
                                funding_rate=funding_rate,
                                projected_funding_pct=projected,
                                maker_fee_rate=scenario.maker_fee_rate,
                                regime_score=decision.grid_score,
                                capital=capital_base,
                                leverage=leverage,
                                tick_size=rule.tick_size,
                                step_size=rule.step_size,
                                min_qty=rule.min_qty,
                                min_notional=rule.min_notional,
                                taker_fee_rate=scenario.taker_fee_rate,
                                base_grid_config=grid,
                                viability_config=viability,
                                long_signal_config=long_signal,
                            )
                        except (StrategyAdmissionError, ValueError):
                            return None
                        return candidate, decision

                    initial = rebuild_candidate(observation)
                    if initial is None:
                        blocked.append(
                            {
                                "combination_id": item.id,
                                "direction": direction.value,
                                "symbol": symbol,
                                "window_key": window.window_key,
                                "scenario": scenario_name,
                                "reason": "INITIAL_ADMISSION_BLOCKED",
                            }
                        )
                        continue
                    initial_candidate, _initial_regime = initial
                    capital = capital_base * symbol_profile.capital_multiplier
                    for seed in SEEDS:
                        base_config = _backtest_config(
                            item,
                            scenario=scenario,
                            capital=capital,
                            leverage=leverage,
                            rule=rule,
                            direction=direction,
                            seed=seed,
                        )
                        r1 = _run_controller_faithful(
                            symbol=symbol,
                            window=window,
                            profile=strategy_profile.name,
                            scenario=scenario_name,
                            seed=seed,
                            observation_rows=observation,
                            trade_rows=trade,
                            funding_events=window_funding,
                            initial_candidate=initial_candidate,
                            base_config=base_config,
                            rebuild_candidate=rebuild_candidate,
                            rolling_regrid_bars=rolling_regrid_bars,
                            cooldown=cooldown,
                            max_window_loss=capital
                            * float(risk_raw.get("max_weekend_loss_pct", 0.015)),
                            max_stop_count=int(risk_raw.get("max_window_stop_count", 3)),
                            max_consecutive_session_losses=int(
                                risk_raw.get("max_consecutive_session_losses", 2)
                            ),
                        )
                        record = _row(
                            item=item,
                            symbol=symbol,
                            direction=direction,
                            scenario=scenario_name,
                            seed=seed,
                            window=window,
                            result=r1.result,
                            candidate=initial_candidate,
                            capital=capital,
                        )
                        record.update(
                            engine_mode="R1_CONTROLLER_FAITHFUL",
                            grid_count=initial_candidate.params.grid_num,
                            step_pct=initial_candidate.params.step_pct,
                            grid_lower=initial_candidate.params.lower,
                            grid_upper=initial_candidate.params.upper,
                            baseline_atr=initial_candidate.params.baseline_atr,
                            session_count=len(r1.session_rows),
                            cooldown_count=r1.cooldown_count,
                            reentry_count=r1.reentry_count,
                            regrid_count=sum(
                                row["status"] == "COMPLETED" for row in r1.regrid_rows
                            ),
                        )
                        runs.append(record)
                        for row in r1.session_rows:
                            session_rows.append(
                                {"combination_id": item.id, "direction": direction.value, **row}
                            )
                        for row in r1.regrid_rows:
                            regrid_rows.append(
                                {"combination_id": item.id, "direction": direction.value, **row}
                            )

    _write_csv(phase2_path, runs)
    _write_csv(output / f"{args.artifact_prefix}-blocked.csv", blocked)
    _write_csv(output / f"{args.artifact_prefix}-session-breakdown.csv", session_rows)
    _write_csv(output / f"{args.artifact_prefix}-regrid-breakdown.csv", regrid_rows)
    manifest = {
        "protocol": "v2.8",
        "phase": args.phase_label,
        "official": not args.max_profiles and not args.max_windows,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "candidate_catalog_sha256": catalog_hash,
        "candidate_profile_count": len(catalog),
        "run_count": len(runs),
        "blocked_count": len(blocked),
    }
    _write_json(output / f"{args.artifact_prefix}-run-manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
