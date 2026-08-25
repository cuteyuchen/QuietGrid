from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta

from scripts.semiconductor_grid_inventory_tail_v35 import (
    BASE_COMMIT,
    OUTPUT_DIR,
    deduplicate_events,
    lead_time_minutes,
    validate_no_future_leakage,
)


def _event(*, scenario: str = "PRIMARY_ZERO_MAKER", seed: str = "3") -> dict[str, str]:
    return {
        "symbol": "SNDKUSDT",
        "window_key": "NYSE:window",
        "breakout_direction": "UP",
        "signal_time": "2026-08-17T03:16:59.999000+00:00",
        "scenario": scenario,
        "seed": seed,
        "posthoc_label": "TRUE_BREAKOUT",
    }


def _csv(name: str) -> list[dict[str, str]]:
    with (OUTPUT_DIR / name).open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_event_deduplication_and_no_seed_inflation() -> None:
    unique = deduplicate_events(
        [
            _event(),
            _event(scenario="EXECUTION_STRESS", seed="97"),
            _event(scenario="MAKER_PROMO_OFF", seed="10"),
        ]
    )
    assert len(unique) == 1
    assert unique[0]["run_level_duplicate_count"] == 3
    assert unique[0]["scenario_count"] == 3
    assert unique[0]["seed_count"] == 3


def test_feature_timestamps_are_causal_and_future_labels_are_excluded() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    valid = {
        "feature_timestamp": now.isoformat(),
        "observation_timestamp": (now + timedelta(minutes=1)).isoformat(),
        "predictor_fields": "gross_inventory_utilization|reversal_ratio_20m",
    }
    invalid = {
        "feature_timestamp": (now + timedelta(minutes=2)).isoformat(),
        "observation_timestamp": now.isoformat(),
        "predictor_fields": "future_tail_loss",
    }
    assert validate_no_future_leakage([valid]) == []
    errors = validate_no_future_leakage([invalid])
    assert len(errors) == 2


def test_lead_time_calculation() -> None:
    assert lead_time_minutes("2026-08-17T03:00:00+00:00", "2026-08-17T03:20:00+00:00") == 20.0


def test_v34_truth_and_control_path_parity() -> None:
    parity = json.loads((OUTPUT_DIR / "event-parity.json").read_text(encoding="utf-8"))
    quality = json.loads((OUTPUT_DIR / "data-quality.json").read_text(encoding="utf-8"))
    assert parity["status"] == "PASS_V34_EVENT_PARITY"
    assert parity["control_parity"]["status"] == "PASS_CONTROL_PARITY"
    assert parity["v33_parity"]["status"] == "PASS_V33_PARITY"
    assert parity["observed_unique_event_confusion"] == {"TP": 2, "FP": 10, "FN": 0, "TN": 0}
    assert parity["observed_run_level_confusion"] == {"TP": 36, "FP": 180, "FN": 0, "TN": 0}
    assert quality["status"] == "PASS_DATA_QUALITY"
    assert quality["canonical_path"] == "31111-NEUTRAL CONTROL"
    assert quality["defense_paths_excluded"] == ["S0", "S1", "S2", "S3"]


def test_unique_event_artifacts_use_twelve_independent_events() -> None:
    events = _csv("unique-events.csv")
    assert len(events) == 12
    assert sum(row["truth_label"] == "TRUE_BREAKOUT" for row in events) == 2
    assert sum(row["truth_label"] == "FALSE_BREAKOUT" for row in events) == 10
    assert {row["statistical_unit"] for row in events} == {"UNIQUE_MARKET_EVENT"}
    assert all(int(row["run_level_duplicate_count"]) == 18 for row in events)


def test_event_checkpoints_and_true_false_reconstruction() -> None:
    snapshots = _csv("feature-snapshots.csv")
    fixed = [row for row in snapshots if row["checkpoint"].startswith("T")]
    assert {row["checkpoint"] for row in fixed} == {"T-60", "T-30", "T-20", "T-15", "T-10", "T-5", "T0"}
    assert len({row["event_id"] for row in fixed}) == 12
    assert all(row["canonical_path"] == "31111-NEUTRAL CONTROL" for row in fixed)
    assert len(_csv("false-positive-analysis.csv")) == 10
    assert len(_csv("true-event-analysis.csv")) == 2


def test_candidate_creation_is_disabled_and_small_sample_is_explicit() -> None:
    manifest = json.loads((OUTPUT_DIR / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["base_commit"] == BASE_COMMIT
    assert manifest["new_forward_oos_candidate"] == "NONE"
    assert manifest["production_config_changed"] is False
    assert manifest["auto_entry_enabled"] is False
    assert manifest["economic_leverage"] == 1.0
    assert manifest["small_event_sample"] is True
    assert manifest["LOEO_status"] == "INSUFFICIENT_EVENT_SAMPLE_FOR_LOEO"
    assert not (OUTPUT_DIR / "candidate-freeze.json").exists()


def test_required_outputs_and_event_paths_exist() -> None:
    required = {
        "final-report.md",
        "run-manifest.json",
        "event-definition.json",
        "event-parity.json",
        "unique-events.csv",
        "event-timeline.csv",
        "feature-snapshots.csv",
        "feature-main-effects.csv",
        "feature-interactions.csv",
        "event-classification.csv",
        "false-positive-analysis.csv",
        "true-event-analysis.csv",
        "tail-formation-summary.csv",
        "early-warning-candidates.csv",
        "robustness-check.csv",
        "symbol-breakdown.csv",
        "data-quality.json",
        "tests.txt",
    }
    assert required.issubset({path.name for path in OUTPUT_DIR.iterdir()})
    paths = list((OUTPUT_DIR / "event-paths").glob("*.csv"))
    assert len(paths) == 12
    assert all(path.stat().st_size > 0 for path in paths)
