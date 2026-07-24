from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_spot_quarterly_carry as round28
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def test_protocol_and_frozen_inputs_are_locked() -> None:
    assert _sha256(round28.PROTOCOL_PATH.resolve()) == round28.PROTOCOL_SHA256
    assert _sha256(round28.ROUND27_RESULT_PATH.resolve()) == round28.ROUND27_RESULT_SHA256
    for config in round28.ASSET_CONFIG.values():
        assert _sha256(config["price_manifest"].resolve()) == config["price_manifest_sha256"]


def _window(*, spot_entry: float, quarterly_entry: float, spot_exit: float, quarterly_exit: float) -> list[dict[str, object]]:
    start = datetime(2022, 1, 1, 8, tzinfo=UTC)
    rows = []
    for index in range(round28.EXPECTED_ROWS_PER_WINDOW):
        timestamp = start + timedelta(hours=index)
        rows.append(
            {
                "window_id": "delivery_test",
                "role": "DEVELOPMENT",
                "entry_time": start,
                "expiry_time": start + timedelta(hours=round28.EXPECTED_ROWS_PER_WINDOW),
                "open_time": int(timestamp.timestamp() * 1000),
                "spot_open": spot_entry + (spot_exit - spot_entry) * index / (round28.EXPECTED_ROWS_PER_WINDOW - 1),
                "spot_high": spot_entry + 2,
                "spot_low": spot_entry - 2,
                "spot_close": spot_entry,
                "quarterly_open": quarterly_entry + (quarterly_exit - quarterly_entry) * index / (round28.EXPECTED_ROWS_PER_WINDOW - 1),
                "quarterly_high": quarterly_entry + 2,
                "quarterly_low": quarterly_entry - 2,
                "quarterly_close": quarterly_entry,
            }
        )
    rows[-1]["spot_close"] = spot_exit
    rows[-1]["quarterly_close"] = quarterly_exit
    return rows


def test_positive_basis_enters_and_costs_all_four_sides() -> None:
    result = round28._window_result(
        _window(spot_entry=100, quarterly_entry=101, spot_exit=100, quarterly_exit=100),
        role="DEVELOPMENT",
        asset="BTC",
        initial_capital=500,
        costs=round28.SCENARIO_COSTS["BASE"],
    )

    assert result["entered"] is True
    assert result["basis_pct"] == pytest.approx(0.01)
    assert result["price_pnl"] == pytest.approx(500 / 201)
    assert result["execution_side_count"] == 4
    assert result["funding_pnl"] == 0.0
    assert result["final_position_flat"] is True


def test_basis_at_threshold_skips_without_cost() -> None:
    result = round28._window_result(
        _window(spot_entry=100, quarterly_entry=100.5, spot_exit=1, quarterly_exit=10),
        role="VALIDATION",
        asset="ETH",
        initial_capital=300,
        costs=round28.SCENARIO_COSTS["COST50"],
    )

    assert result["entered"] is False
    assert result["net_pnl"] == 0.0
    assert result["execution_side_count"] == 0


def test_metrics_register_positive_trade_ratio_and_concentration() -> None:
    base = {
        "role": "VALIDATION",
        "asset": "BTC",
        "entry_is_causal": True,
        "final_position_flat": True,
        "hourly_row_count": round28.EXPECTED_ROWS_PER_WINDOW,
        "funding_pnl": 0.0,
        "execution_side_count": 4,
        "entered": True,
        "minimum_path_pnl": 0.0,
        "maximum_path_pnl": 1.0,
        "price_pnl": 1.0,
        "execution_costs": 0.1,
        "basis_pct": 0.01,
        "hourly_path_pnl": [0.0, 1.0],
    }
    results = []
    for index, pnl in enumerate([1.0, 1.0, 1.0]):
        item = dict(base)
        item.update({"window_id": f"w{index}", "entry_time": f"2022-0{index + 1}-01T08:00:00+00:00", "net_pnl": pnl})
        results.append(item)

    metrics = round28._metrics(results, initial_capital=300, role="VALIDATION")

    assert metrics["passed"] is True
    assert metrics["metrics"]["positive_trade_ratio"] == 1.0
    assert metrics["metrics"]["profit_factor"] is None
