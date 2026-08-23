from __future__ import annotations

import hashlib
import csv
import json
from datetime import UTC, datetime, timedelta

from scripts.semiconductor_grid_breakout_confirmation_v33 import (
    BASE_CANDIDATE_SHA,
    CONFIRMATION_FREEZE,
    CURRENT_LEDGER,
    OFFICIAL_DIR,
    OUTPUT_DIR,
    BreakoutConfirmationEngine,
    ConfirmationFeatures,
    adverse_inventory,
    breakout_protection_efficiency,
    confirmation_delay_minutes,
    confirmation_delay_inventory_loss,
    confusion_metrics,
    false_breakout_opportunity_cost,
    grid_edge_retention,
    inventory_tail_reduction,
    true_breakout_loss_avoided,
)


def _features(**overrides: float | int | str | bool) -> ConfirmationFeatures:
    values: dict[str, float | int | str | bool] = {
        "timestamp": datetime(2026, 8, 23, 0, 20, tzinfo=UTC).isoformat(),
        "horizon_minutes": 20,
        "confirmation_price": 105.0,
        "outside_close_ratio": 0.95,
        "consecutive_outside_closes": 18,
        "time_since_last_grid_cross_minutes": 18,
        "crossings_per_hour": 0.0,
        "reversal_ratio": 0.1,
        "directional_efficiency": 0.7,
        "signed_return_persistence": 0.8,
        "same_direction_close_ratio": 0.8,
        "cumulative_directional_move_atr": 1.2,
        "adverse_inventory_notional": 100.0,
        "adverse_inventory_utilization": 0.5,
        "adverse_inventory_age_minutes": 300.0,
        "breakout_direction": "UP",
        "future_data_used": False,
    }
    values.update(overrides)
    return ConfirmationFeatures(**values)


def test_d2_only_creates_suspected_state_without_r3() -> None:
    engine = BreakoutConfirmationEngine()
    assert engine.stage1_suspect("2026-08-23T00:00:00+00:00") == "BREAKOUT_SUSPECTED"
    assert engine.r3_action() == "NONE"


def test_confirmation_is_causal_and_rejects_earlier_timestamp() -> None:
    engine = BreakoutConfirmationEngine()
    engine.stage1_suspect("2026-08-23T00:10:00+00:00")
    try:
        engine.stage2_confirm(_features(timestamp="2026-08-23T00:09:00+00:00"), "C2")
    except ValueError:
        pass
    else:
        raise AssertionError("pre-suspicion confirmation timestamp was accepted")
    assert _features().future_data_used is False


def test_confirmation_rejects_explicit_future_data_flag() -> None:
    engine = BreakoutConfirmationEngine()
    engine.stage1_suspect("2026-08-23T00:00:00+00:00")
    try:
        engine.stage2_confirm(_features(future_data_used=True), "C2")
    except ValueError:
        pass
    else:
        raise AssertionError("future-data confirmation was accepted")


def test_false_breakout_can_return_to_normal_grid() -> None:
    engine = BreakoutConfirmationEngine()
    engine.stage1_suspect("2026-08-23T00:00:00+00:00")
    assert engine.stage2_confirm(_features(outside_close_ratio=0.1), "C2") == "BREAKOUT_REJECTED"
    assert engine.reset_after_rejection() == "NORMAL_GRID"


def test_confirmed_breakout_executes_r3_only_once() -> None:
    engine = BreakoutConfirmationEngine()
    engine.stage1_suspect("2026-08-23T00:00:00+00:00")
    assert engine.stage2_confirm(_features(), "C2") == "BREAKOUT_CONFIRMED"
    assert engine.r3_action() == "R3_50PCT_PARTIAL_FLATTEN_REDUCE_ONLY"
    assert engine.r3_action() == "NONE"


def test_inventory_alignment_for_up_and_down_breakouts() -> None:
    assert adverse_inventory(-0.1, "UP") == 0.1
    assert adverse_inventory(0.1, "UP") == 0.0
    assert adverse_inventory(0.1, "DOWN") == 0.1
    assert adverse_inventory(-0.1, "DOWN") == 0.0


def test_no_adverse_inventory_rejects_c2_action() -> None:
    engine = BreakoutConfirmationEngine()
    engine.stage1_suspect("2026-08-23T00:00:00+00:00")
    assert engine.stage2_confirm(_features(adverse_inventory_notional=0.0), "C2") == "BREAKOUT_REJECTED"
    assert engine.r3_action() == "NONE"


def test_registered_confirmation_calculations() -> None:
    features = _features()
    assert features.consecutive_outside_closes == 18
    assert features.outside_close_ratio == 0.95
    assert features.time_since_last_grid_cross_minutes == 18
    assert features.directional_efficiency == 0.7
    assert confirmation_delay_minutes("2026-08-23T00:00:00+00:00", features.timestamp) == 20.0


def test_confirmation_delay_inventory_loss_uses_frozen_r3_fraction() -> None:
    event = {"price": 100.0, "net_inventory": -2.0}
    assert confirmation_delay_inventory_loss(event, _features(confirmation_price=105.0)) == 5.0
    assert confirmation_delay_inventory_loss(event, _features(confirmation_price=95.0)) == 0.0


def test_confusion_metrics_include_precision_recall_and_f1() -> None:
    metrics = confusion_metrics(8, 2, 2, 88)
    assert metrics["precision"] == 0.8
    assert metrics["recall"] == 0.8
    assert metrics["F1"] == 0.8000000000000002
    assert metrics["false_breakout_rate"] == 0.2


def test_economic_detection_metrics() -> None:
    assert false_breakout_opportunity_cost(1.0, 0.1, 0.2, 0.5) == 1.8
    assert true_breakout_loss_avoided(-4.0, 0.4) == 4.4
    assert breakout_protection_efficiency(4.4, 1.1) == 4.0


def test_grid_and_inventory_metrics() -> None:
    assert grid_edge_retention(8.0, 10.0) == 0.8
    assert inventory_tail_reduction(3.0, 10.0) == 0.7


def test_confirmation_freeze_is_small_and_r3_is_fixed() -> None:
    assert set(CONFIRMATION_FREEZE["stage2_profiles"]) == {"C1", "C2", "C3"}
    assert CONFIRMATION_FREEZE["horizons_minutes"] == [10, 20, 30]
    assert CONFIRMATION_FREEZE["response"] == "R3"
    assert CONFIRMATION_FREEZE["profit_lock"] == "PROFIT_LOCK_DISABLED"


def test_official_ledger_and_original_candidate_are_immutable() -> None:
    assert hashlib.sha256(CURRENT_LEDGER.read_bytes()).hexdigest() == "2c034bc92bda12da193faf4ef5e2edabb624976a69edfddfa7b3b551190ed814"
    assert hashlib.sha256((OFFICIAL_DIR / "candidate-31111-freeze.json").read_bytes()).hexdigest() == BASE_CANDIDATE_SHA


def test_v32_parity_reproduces_frozen_sndk_replay() -> None:
    parity = json.loads((OUTPUT_DIR / "v32-parity.json").read_text(encoding="utf-8"))
    assert parity["status"] == "PASS_V32_PARITY"
    assert parity["current_sndk_primary"]["control_net_pnl"] == -4.03406218985135
    assert parity["current_sndk_primary"]["d2_r3_net_pnl"] == 0.40978529801050295
    assert parity["execution_stress"]["control"] == -137.08175137379615
    assert parity["execution_stress"]["d2_r3"] == 55.51243127972497


def test_required_outputs_keep_new_candidate_at_zero_of_eight() -> None:
    required = {
        "run-manifest.json", "strategy-freeze.json", "v32-parity.json",
        "breakout-label-freeze.json", "confirmation-freeze.json",
        "v32-breakout-confusion-audit.csv", "profile-results.csv",
        "profile-summary.csv", "confusion-matrix.csv", "detector-event-counts.csv",
        "true-breakout-events.csv", "false-breakout-events.csv", "missed-breakout-events.csv",
        "confirmation-delay.csv", "true-breakout-loss-avoided.csv",
        "false-breakout-opportunity-cost.csv", "breakout-protection-efficiency.csv",
        "grid-edge-retention.csv", "inventory-tail-reduction.csv", "symbol-breakdown.csv",
        "window-breakdown.csv", "seed-breakdown.csv", "scenario-breakdown.csv",
        "current-oos-replay.csv", "stable-confirmation-region.json", "candidate-selection.json",
        "final-report.md", "pytest.stdout.log", "pytest.stderr.log",
        "backtest.stdout.log", "backtest.stderr.log",
    }
    assert required.issubset({path.name for path in OUTPUT_DIR.iterdir()})
    selection = json.loads((OUTPUT_DIR / "candidate-selection.json").read_text(encoding="utf-8"))
    assert selection["recommended_forward_oos_candidate"] == "NONE"
    assert selection["forward_oos_count"] == "0/8"
    manifest = json.loads((OUTPUT_DIR / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["control_parity"] == "PASS_CONTROL_PARITY"
    assert manifest["official_ledger_unchanged"] is True
    assert manifest["candidate_freeze_unchanged"] is True


def test_breakdown_false_breakout_rates_are_event_conditioned_and_bounded() -> None:
    with (OUTPUT_DIR / "scenario-breakdown.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        rate = float(row["false_breakout_rate"])
        assert 0.0 <= rate <= 1.0
    d2_r3 = [row for row in rows if row["profile_id"] == "D2-R3"]
    c1_20 = [row for row in rows if row["profile_id"] == "D2-C1-R3-20m"]
    assert all(float(row["false_breakout_rate"]) == 5 / 6 for row in d2_r3)
    assert all(float(row["false_breakout_rate"]) == 0.0 for row in c1_20)


def test_confusion_matrix_keeps_true_negatives_for_confirmation_profiles() -> None:
    with (OUTPUT_DIR / "confusion-matrix.csv").open(encoding="utf-8", newline="") as handle:
        rows = {row["profile_id"]: row for row in csv.DictReader(handle)}
    assert rows["D2-R3"]["TN"] == "0"
    assert rows["D2-C1-R3-20m"]["TP"] == "36"
    assert rows["D2-C1-R3-20m"]["FP"] == "0"
    assert rows["D2-C1-R3-20m"]["FN"] == "0"
    assert rows["D2-C1-R3-20m"]["TN"] == "180"
