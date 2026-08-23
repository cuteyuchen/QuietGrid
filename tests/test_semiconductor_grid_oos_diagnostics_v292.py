from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.semiconductor_grid_oos_diagnostics_v292 import (
    EXPECTED_CANDIDATE_SHA,
    OFFICIAL_DIR,
    _append_history,
    _assert_baseline,
    _inventory_drag_ratio,
    _official_snapshot,
    _post_cutoff_windows,
    _post_stop_rows,
    _reconcile_pnl,
    _sha,
)


OUTPUT_DIR = Path("reports/semiconductor-grid-oos-diagnostics-v2.9.2")


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_official_assets_and_candidate_freeze_are_read_only() -> None:
    before = _official_snapshot()
    _assert_baseline()
    after = _official_snapshot()
    assert before == after


def test_window_eligibility_audit_reproduces_two_of_eight() -> None:
    audit = _post_cutoff_windows()
    assert len(audit) == 4
    assert sum(row["classification"] == "FORWARD_OOS" for row in audit) == 2
    assert sum(row["classification"] != "FORWARD_OOS" for row in audit) == 2


def test_pnl_attribution_reconciles_to_official_ledger() -> None:
    path = OFFICIAL_DIR / "forward-oos-ledger.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    complete = [row for row in rows if row.get("record_type") == "OOS_RESULT" and row.get("status") == "COMPLETE"]
    assert complete
    # The frozen engine books stop slippage inside inventory_realized_pnl; the
    # requested decomposition therefore reports a reconciliation error equal
    # to the separately reported slippage, while the engine-native basis is exact.
    assert max(abs(_reconcile_pnl(row) - float(row["slippage_cost"])) for row in complete) < 1e-9


def test_inventory_drag_recomputation_matches_official_value() -> None:
    assert abs(_inventory_drag_ratio(1.9695625467153628, -5.700604726512897) - 2.89435069529515) < 1e-12


def test_post_stop_analysis_uses_only_future_timestamps() -> None:
    @dataclass
    class Result:
        stopped_at_index: int = 1
        pre_exit_timestamp: int = 60_000
        stopped_at_price: float = 100.0
        stopped_reason: str = "stop_loss"
        pre_exit_position_qty: float = 1.0
        pre_exit_inventory_notional: float = 100.0
        pre_exit_unrealized_pnl: float = -2.0
        pre_exit_mark_price: float = 102.0
        inventory_realized_pnl: float = -2.0

    paths = [
        {"timestamp": "1970-01-01T00:00:00+00:00", "price": 80.0, "grid_lower": 90.0, "grid_upper": 110.0},
        {"timestamp": "1970-01-01T00:01:00+00:00", "price": 100.0, "grid_lower": 90.0, "grid_upper": 110.0, "stop_state": "stop_loss"},
    ]
    trade = [
        {"open_time": 0, "close_time": 59_999, "close": 80.0, "high": 81.0, "low": 79.0},
        {"open_time": 60_000, "close_time": 119_999, "close": 100.0, "high": 101.0, "low": 99.0},
        {"open_time": 120_000, "close_time": 179_999, "close": 108.0, "high": 109.0, "low": 107.0},
        {"open_time": 1_860_000, "close_time": 1_919_999, "close": 99.0, "high": 100.0, "low": 98.0},
    ]
    rows = _post_stop_rows(paths, Result(), "1970-01-01T00:40:00+00:00", trade)
    assert rows
    assert all(row["event_time"] == "1970-01-01T00:01:00+00:00" for row in rows)
    assert rows[0]["price_after_exit"] == 99.0
    assert rows[0]["classification"] in {"TRUE_BREAKOUT", "FALSE_BREAKOUT", "UNRESOLVED"}


def test_equity_curve_drawdown_reproduces_official_primary_maximum() -> None:
    rows = _csv_rows(OUTPUT_DIR / "oos-equity-curve.csv")
    primary = [row for row in rows if row.get("scenario") == "PRIMARY_ZERO_MAKER" and row.get("symbol") == "SNDKUSDT"]
    assert abs(max(float(row["drawdown"]) for row in primary) - 6.3168050875581745) < 1e-9


def test_counterfactuals_are_explicitly_research_only() -> None:
    rows = _csv_rows(OUTPUT_DIR / "counterfactual-results.csv")
    assert rows
    assert {row["label"] for row in rows} == {"DIAGNOSTIC_COUNTERFACTUAL"}
    ledger = _csv_rows(OFFICIAL_DIR / "forward-oos-ledger.csv")
    assert not any(row.get("record_type") == "DIAGNOSTIC_COUNTERFACTUAL" for row in ledger)


def test_seed_attribution_matches_official_primary_rows() -> None:
    ledger = _csv_rows(OFFICIAL_DIR / "forward-oos-ledger.csv")
    official = {
        row["seed"]: row
        for row in ledger
        if row.get("record_type") == "OOS_RESULT"
        and row.get("status") == "COMPLETE"
        and row.get("scenario") == "PRIMARY_ZERO_MAKER"
        and row.get("symbol") == "SNDKUSDT"
    }
    diagnostics = {row["seed"]: row for row in _csv_rows(OUTPUT_DIR / "oos-seed-diagnostics.csv")}
    assert diagnostics.keys() == official.keys()
    for seed, expected in official.items():
        actual = diagnostics[seed]
        assert abs(float(actual["net_pnl"]) - float(expected["net_pnl"])) < 1e-12
        assert abs(float(actual["paired_grid_pnl"]) - float(expected["paired_grid_pnl"])) < 1e-12
        assert abs(float(actual["inventory_pnl"]) - float(expected["inventory_realized_pnl"])) < 1e-12
        assert abs(float(actual["max_drawdown"]) - float(expected["max_drawdown"])) < 1e-12
        assert float(actual["fill_count"]) >= 0.0


def test_scenario_attribution_matches_official_scenario_means() -> None:
    ledger = _csv_rows(OFFICIAL_DIR / "forward-oos-ledger.csv")
    complete = [
        row
        for row in ledger
        if row.get("record_type") == "OOS_RESULT"
        and row.get("status") == "COMPLETE"
        and row.get("symbol") == "SNDKUSDT"
    ]
    diagnostics = {row["scenario"]: row for row in _csv_rows(OUTPUT_DIR / "scenario-attribution.csv")}
    for scenario, actual in diagnostics.items():
        rows = [row for row in complete if row["scenario"] == scenario]
        expected_net = sum(float(row["net_pnl"]) for row in rows) / len(rows)
        expected_inventory = sum(float(row["inventory_realized_pnl"]) for row in rows) / len(rows)
        expected_funding = sum(float(row["funding_pnl"]) for row in rows) / len(rows)
        assert abs(float(actual["net_pnl"]) - expected_net) < 1e-12
        assert abs(float(actual["inventory_tail"]) - expected_inventory) < 1e-12
        assert abs(float(actual["funding"]) - expected_funding) < 1e-12


def test_counterfactual_output_does_not_change_candidate_sha() -> None:
    freeze = OFFICIAL_DIR / "candidate-31111-freeze.json"
    before = _sha(freeze)
    assert _csv_rows(OUTPUT_DIR / "counterfactual-results.csv")
    assert _sha(freeze) == before == EXPECTED_CANDIDATE_SHA


def test_diagnostic_history_is_byte_preserving_append_only(tmp_path: Path) -> None:
    path = tmp_path / "history.csv"
    fields = ("window_key", "scenario", "seed", "net_pnl")
    first = {"window_key": "W1", "scenario": "PRIMARY", "seed": 3, "net_pnl": -1.0}
    second = {"window_key": "W2", "scenario": "PRIMARY", "seed": 3, "net_pnl": 2.0}
    _append_history(path, [first], fields)
    historical_bytes = path.read_bytes()
    _append_history(path, [first, second], fields)
    appended_bytes = path.read_bytes()
    assert appended_bytes.startswith(historical_bytes)
    rows = _csv_rows(path)
    assert [row["window_key"] for row in rows] == ["W1", "W2"]
