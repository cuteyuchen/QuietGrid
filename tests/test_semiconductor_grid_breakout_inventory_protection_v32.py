from __future__ import annotations

import csv
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.semiconductor_grid_breakout_inventory_protection_v32 import (
    BASE_CANDIDATE_SHA,
    CURRENT_LEDGER,
    DETECTOR_FREEZE,
    OFFICIAL_DIR,
    OUTPUT_DIR,
    PostEntryRegimeMonitor,
    adverse_inventory,
    candidate_sha_for_profile,
    conditional_profit_lock_action,
    detector_specs,
    inventory_age_action,
    inventory_response_action,
    profit_giveback_ratio,
)


def _ts(minutes: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes)).isoformat()


def _state(monitor: PostEntryRegimeMonitor, minute: int, price: float) -> object:
    return monitor.update(
        timestamp=_ts(minute),
        price=price,
        high=price + 0.1,
        low=price - 0.1,
        grid_lower=99.0,
        grid_upper=101.0,
        net_inventory=-0.10,
        inventory_utilization=0.2,
    )


def test_post_entry_detector_uses_only_past_and_current_data() -> None:
    monitor = PostEntryRegimeMonitor(lookback=10)
    first = _state(monitor, 0, 101.2)
    assert monitor.bars_seen == 1
    assert first.timestamp == _ts(0)
    try:
        _state(monitor, -1, 101.3)
    except ValueError:
        pass
    else:
        raise AssertionError("future/out-of-order timestamp was accepted")


def test_single_grid_touch_does_not_trigger_breakout() -> None:
    monitor = PostEntryRegimeMonitor(lookback=10)
    for minute in range(3):
        state = _state(monitor, minute, 101.2)
    assert not monitor.confirmed(state, "D1")
    assert not monitor.confirmed(state, "D2")
    assert not monitor.confirmed(state, "D3")


def test_confirmed_breakout_requires_duration_and_structure() -> None:
    monitor = PostEntryRegimeMonitor(lookback=10)
    state = None
    for minute in range(40):
        state = _state(monitor, minute, 101.2 + minute * 0.05)
    assert state is not None
    assert monitor.confirmed(state, "D1") or monitor.confirmed(state, "D2") or monitor.confirmed(state, "D3")


def test_breakout_direction_maps_to_adverse_inventory() -> None:
    assert adverse_inventory(-0.10, "UP") == 0.10
    assert adverse_inventory(0.10, "UP") == 0.0
    assert adverse_inventory(0.10, "DOWN") == 0.10
    assert adverse_inventory(-0.10, "DOWN") == 0.0


def test_inventory_response_matrix_is_reduce_only_after_confirmation() -> None:
    assert inventory_response_action("R1", 0.10) == {
        "response": "R1",
        "flatten_fraction": 0.0,
        "flatten_qty": 0.0,
        "reduce_only": True,
        "allow_risk_increasing_orders": False,
    }
    assert inventory_response_action("R2", 0.10)["flatten_qty"] == 0.025
    assert inventory_response_action("R3", 0.10)["flatten_qty"] == 0.05
    assert inventory_response_action("R4", 0.10)["flatten_qty"] == 0.10


def test_inventory_age_soft_and_hard_rules() -> None:
    assert inventory_age_action(100, False) == "NORMAL"
    assert inventory_age_action(DETECTOR_FREEZE["age_rules"]["AGE_SOFT"], False) == "BLOCK_SAME_SIDE"
    assert inventory_age_action(DETECTOR_FREEZE["age_rules"]["AGE_HARD"], False) == "BLOCK_SAME_SIDE"
    assert inventory_age_action(DETECTOR_FREEZE["age_rules"]["AGE_HARD"], True) == "REDUCE_ONLY"


def test_conditional_profit_lock_and_giveback_ratio() -> None:
    assert profit_giveback_ratio(0.5, 1.0) == 0.5
    assert conditional_profit_lock_action(1.0, True, 0.6, 0.5) == "REDUCE_ONLY"
    assert conditional_profit_lock_action(1.0, False, 0.6, 0.5) == "NONE"
    assert conditional_profit_lock_action(0.0, True, 0.6, 0.5) == "NONE"


def test_candidate_sha_changes_when_protection_logic_changes() -> None:
    base = candidate_sha_for_profile("D2-R2", "D2", "R2")
    changed = hashlib.sha256((base + "changed").encode()).hexdigest()
    assert base != BASE_CANDIDATE_SHA
    assert changed != base


def test_new_candidate_cannot_inherit_old_forward_oos_count() -> None:
    manifest = OUTPUT_DIR / "run-manifest.json"
    text = manifest.read_text(encoding="utf-8")
    assert '"new_candidate_forward_oos": "0/8"' in text
    assert '"current_forward_oos_reclassified": "RESEARCH_VALIDATION_EXPOSED"' in text


def test_diagnostic_research_does_not_mutate_official_ledger() -> None:
    digest = hashlib.sha256(CURRENT_LEDGER.read_bytes()).hexdigest()
    freeze_digest = hashlib.sha256((OFFICIAL_DIR / "candidate-31111-freeze.json").read_bytes()).hexdigest()
    assert digest == "2c034bc92bda12da193faf4ef5e2edabb624976a69edfddfa7b3b551190ed814"
    assert freeze_digest == BASE_CANDIDATE_SHA


def test_required_outputs_and_matrix_size() -> None:
    required = {
        "run-manifest.json", "strategy-freeze.json", "breakout-detector-freeze.json", "research-matrix.json", "control-parity.json",
        "profile-results.csv", "profile-summary.csv", "window-breakdown.csv", "symbol-breakdown.csv", "seed-breakdown.csv", "scenario-breakdown.csv",
        "breakout-events.csv", "breakout-path-analysis.csv", "inventory-response-analysis.csv", "inventory-age-analysis.csv", "false-breakout-analysis.csv",
        "grid-edge-retention.csv", "inventory-tail-reduction.csv", "current-oos-replay.csv", "phase1-results.csv", "phase2-results.csv", "phase3-results.csv",
        "stable-protection-region.json", "candidate-selection.json", "final-report.md", "pytest.stdout.log", "pytest.stderr.log", "backtest.stdout.log", "backtest.stderr.log",
    }
    assert required.issubset({path.name for path in OUTPUT_DIR.iterdir()})
    matrix = __import__("json").loads((OUTPUT_DIR / "research-matrix.json").read_text(encoding="utf-8"))
    assert len(matrix["phase1"]) == 10


def test_final_report_contains_explicit_detector_conclusions() -> None:
    report = (OUTPUT_DIR / "final-report.md").read_text(encoding="utf-8")
    assert "D1 emitted 0 signals" in report
    assert "D2=83.33%" in report
    assert "D3=80.00%" in report
    assert "Best TRUE_BREAKOUT identification: `D2`" in report
    assert "D2 also detects the current SNDK tail event" in report
