from __future__ import annotations

import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.semiconductor_grid_staged_derisking_v34 import (
    BASE_CANDIDATE_SHA,
    CURRENT_LEDGER,
    OFFICIAL_DIR,
    OUTPUT_DIR,
    R3_FRACTION,
    StagedDeRiskingEngine,
    adverse_inventory_components,
    confusion_metrics,
    defense_efficiency_ratio,
    event_cluster_id,
    unique_market_event_key,
)


def _timestamp(minutes: int) -> str:
    return (datetime(2026, 8, 24, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()


def _event(**overrides: str) -> dict[str, str]:
    event = {
        "symbol": "SNDKUSDT",
        "window_key": "NYSE:window",
        "breakout_direction": "UP",
        "signal_time": _timestamp(0),
        "scenario": "PRIMARY_ZERO_MAKER",
        "seed": "3",
    }
    event.update(overrides)
    return event


def _summary() -> dict[str, dict[str, str]]:
    with (OUTPUT_DIR / "profile-summary.csv").open(encoding="utf-8", newline="") as handle:
        return {row["profile_id"]: row for row in csv.DictReader(handle)}


def test_unique_market_events_do_not_multiply_by_scenario_or_seed() -> None:
    base = _event()
    duplicate = _event(scenario="EXECUTION_STRESS", seed="97")
    assert unique_market_event_key(base) == unique_market_event_key(duplicate)
    assert event_cluster_id(base) == event_cluster_id(duplicate)


def test_run_and_unique_event_confusion_are_separate() -> None:
    with (OUTPUT_DIR / "run-level-confusion-matrix.csv").open(encoding="utf-8", newline="") as handle:
        run = {row["profile_id"]: row for row in csv.DictReader(handle)}
    with (OUTPUT_DIR / "event-level-confusion-matrix.csv").open(encoding="utf-8", newline="") as handle:
        event = {row["profile_id"]: row for row in csv.DictReader(handle)}
    assert (run["D2-R3"]["TP"], run["D2-R3"]["FP"]) == ("36", "180")
    assert (event["D2-R3"]["TP"], event["D2-R3"]["FP"]) == ("2", "10")
    assert run["D2-R3"]["accounting_level"] == "RUN_LEVEL"
    assert event["D2-R3"]["accounting_level"] == "UNIQUE_MARKET_EVENT"


def test_suspected_episode_executes_early_action_once() -> None:
    engine = StagedDeRiskingEngine("S2")
    first = engine.stage1_suspect(_timestamp(0), "UP", 10.0, "episode-1")
    duplicate = engine.stage1_suspect(_timestamp(1), "UP", 10.0, "episode-1")
    assert first.early_flatten_qty == 1.0
    assert duplicate.early_flatten_qty == 0.0
    assert engine.state == "BREAKOUT_SUSPECTED"


def test_s1_only_blocks_adverse_risk_increasing_orders() -> None:
    engine = StagedDeRiskingEngine("S1")
    engine.stage1_suspect(_timestamp(0), "UP", 10.0, "episode-1")
    assert engine.allows_order(increases_adverse_inventory=False) is True
    assert engine.allows_order(increases_adverse_inventory=True) is False


def test_s2_and_s3_flatten_exact_reference_fractions() -> None:
    s2 = StagedDeRiskingEngine("S2")
    s3 = StagedDeRiskingEngine("S3")
    assert s2.stage1_suspect(_timestamp(0), "DOWN", 8.0, "s2").early_flatten_qty == pytest.approx(0.8)
    assert s3.stage1_suspect(_timestamp(0), "DOWN", 8.0, "s3").early_flatten_qty == pytest.approx(2.0)


def test_confirmed_r3_is_cumulative_fifty_percent_not_another_fifty() -> None:
    engine = StagedDeRiskingEngine("S3")
    early = engine.stage1_suspect(_timestamp(0), "UP", 8.0, "episode-1")
    confirmed = engine.stage2_confirm(_timestamp(20))
    assert early.early_flatten_qty == pytest.approx(2.0)
    assert confirmed == pytest.approx(2.0)
    assert early.early_flatten_qty + confirmed == pytest.approx(8.0 * R3_FRACTION)
    assert engine.reduce_only is True


def test_false_breakout_recovery_returns_to_normal_without_releverage() -> None:
    engine = StagedDeRiskingEngine("S2")
    action = engine.stage1_suspect(_timestamp(0), "UP", 10.0, "episode-1")
    assert action.early_flatten_qty == 1.0
    assert engine.reject_suspicion(_timestamp(20)) == "NORMAL_GRID"
    assert engine.reduce_only is False
    assert engine.allows_order(increases_adverse_inventory=True) is True
    assert engine.early_flatten_qty == 1.0


def test_confirmation_and_rejection_cannot_use_earlier_time() -> None:
    engine = StagedDeRiskingEngine("S2")
    engine.stage1_suspect(_timestamp(10), "UP", 10.0, "episode-1")
    with pytest.raises(ValueError):
        engine.stage2_confirm(_timestamp(9))
    with pytest.raises(ValueError):
        engine.reject_suspicion(_timestamp(9))


def test_up_and_down_map_to_correct_adverse_inventory() -> None:
    up = adverse_inventory_components(-0.5, "UP")
    down = adverse_inventory_components(0.5, "DOWN")
    assert up["short_inventory_qty"] == 0.5
    assert up["adverse_inventory_qty"] == 0.5
    assert down["long_inventory_qty"] == 0.5
    assert down["adverse_inventory_qty"] == 0.5
    assert adverse_inventory_components(0.5, "UP")["adverse_inventory_qty"] == 0.0


def test_preconfirmation_tail_metrics_and_true_false_economics_exist() -> None:
    summary = _summary()
    assert float(summary["S2"]["tail_saved_before_confirmation_unique"]) > 0.0
    assert float(summary["S3"]["tail_saved_before_confirmation_unique"]) > float(summary["S2"]["tail_saved_before_confirmation_unique"])
    assert float(summary["S2"]["true_breakout_loss_avoided_unique"]) > 0.0
    assert float(summary["S2"]["false_breakout_defense_cost_unique"]) > 0.0
    assert float(summary["S2"]["net_defense_value_unique"]) > 0.0
    assert float(summary["S2"]["defense_efficiency_ratio_unique"]) > 1.0


def test_grid_edge_and_inventory_tail_metrics_are_reported() -> None:
    summary = _summary()
    assert float(summary["S2"]["grid_edge_retention"]) >= 0.80
    assert float(summary["S2"]["inventory_tail_reduction"]) < 0.50
    assert float(summary["S3"]["inventory_tail_reduction"]) > float(summary["S2"]["inventory_tail_reduction"])


def test_pnl_reconciliation_and_slippage_audit_pass() -> None:
    audit = json.loads((OUTPUT_DIR / "pnl-accounting-audit.json").read_text(encoding="utf-8"))
    assert audit["status"] == "PASS_PNL_RECONCILIATION"
    assert audit["max_abs_residual"] == 0.0
    assert audit["slippage_double_counted"] is False


def test_v33_parity_and_protected_artifacts_are_unchanged() -> None:
    parity = json.loads((OUTPUT_DIR / "v33-parity.json").read_text(encoding="utf-8"))
    manifest = json.loads((OUTPUT_DIR / "run-manifest.json").read_text(encoding="utf-8"))
    assert parity["status"] == "PASS_V33_PARITY"
    assert manifest["official_ledger_unchanged"] is True
    assert manifest["candidate_freeze_unchanged"] is True
    assert hashlib.sha256(CURRENT_LEDGER.read_bytes()).hexdigest() == "2c034bc92bda12da193faf4ef5e2edabb624976a69edfddfa7b3b551190ed814"
    assert hashlib.sha256((OFFICIAL_DIR / "candidate-31111-freeze.json").read_bytes()).hexdigest() == BASE_CANDIDATE_SHA


def test_new_candidate_remains_zero_of_eight_and_no_candidate_is_frozen() -> None:
    selection = json.loads((OUTPUT_DIR / "candidate-selection.json").read_text(encoding="utf-8"))
    assert selection["recommended_forward_oos_candidate"] == "NONE"
    assert selection["new_candidate_sha"] == ""
    assert selection["forward_oos_count"] == "0/8"
    assert not (OUTPUT_DIR / "candidate-freeze-v3.4.json").exists()


def test_limited_phase2_runs_only_for_directionally_consistent_s2_s3_region() -> None:
    manifest = json.loads((OUTPUT_DIR / "run-manifest.json").read_text(encoding="utf-8"))
    with (OUTPUT_DIR / "phase2-robustness.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert manifest["phase2_status"] == "COMPLETED_LIMITED_ROBUSTNESS"
    assert manifest["phase2_profiles"] == ["S2", "S3"]
    assert {row["profile_id"] for row in rows} == {
        "S2-C1-10m", "S2-C1-20m", "S2-C1-30m",
        "S3-C1-10m", "S3-C1-20m", "S3-C1-30m",
    }


def test_expected_output_artifacts_exist() -> None:
    required = {
        "run-manifest.json", "strategy-freeze.json", "v33-parity.json", "staged-derisking-freeze.json", "pnl-accounting-audit.json",
        "unique-market-event-audit.csv", "run-level-confusion-matrix.csv", "event-level-confusion-matrix.csv", "profile-results.csv", "profile-summary.csv",
        "symbol-breakdown.csv", "window-breakdown.csv", "seed-breakdown.csv", "scenario-breakdown.csv", "time-split-breakdown.csv", "suspected-episodes.csv", "true-breakout-events.csv",
        "false-breakout-events.csv", "pre-confirmation-inventory-loss.csv", "early-defense-cost-attribution.csv", "true-breakout-loss-avoided.csv",
        "false-breakout-defense-cost.csv", "net-defense-value.csv", "defense-efficiency.csv", "tail-saved-before-confirmation.csv", "false-defense-recovery.csv",
        "grid-edge-retention.csv", "inventory-tail-reduction.csv", "risk-path-analysis.csv", "current-oos-replay.csv", "phase1-results.csv", "phase2-profile-results.csv", "phase2-robustness.csv",
        "stable-staged-defense-region.json", "candidate-selection.json", "final-report.md", "pytest.stdout.log", "pytest.stderr.log", "backtest.stdout.log", "backtest.stderr.log",
    }
    assert required.issubset({path.name for path in OUTPUT_DIR.iterdir()})


def test_confusion_and_efficiency_helpers_are_well_defined() -> None:
    metrics = confusion_metrics(2, 10, 0, 0)
    assert metrics["precision"] == pytest.approx(1 / 6)
    assert metrics["recall"] == 1.0
    assert defense_efficiency_ratio(2.0, 1.0) == 2.0
