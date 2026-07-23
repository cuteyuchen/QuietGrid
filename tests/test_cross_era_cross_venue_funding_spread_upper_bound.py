from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_cross_venue_funding_spread_upper_bound as round25
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _window(window_id: str, start: datetime) -> dict[str, object]:
    return {
        "window_id": window_id,
        "market_close": start,
        "force_close_at": start + timedelta(hours=48),
    }


def _event(start: datetime, hours: int, rate: float) -> dict[str, object]:
    return {
        "funding_time": int((start + timedelta(hours=hours)).timestamp() * 1000),
        "funding_interval_hours": 8,
        "funding_rate": rate,
    }


def test_protocol_and_prior_results_are_frozen() -> None:
    assert _sha256(round25.PROTOCOL_PATH.resolve()) == round25.PROTOCOL_SHA256
    assert _sha256(round25.ROUND22_RESULT_PATH.resolve()) == round25.ROUND22_RESULT_SHA256
    assert _sha256(round25.ROUND24_RESULT_PATH.resolve()) == round25.ROUND24_RESULT_SHA256


def test_frozen_funding_inputs_pass_strict_audit() -> None:
    for _asset, config in round25.ASSET_CONFIG.items():
        _binance_manifest, binance_events, binance_audit = round25._read_binance_manifest(
            config["binance_manifest"],
            expected_manifest_sha256=config["binance_manifest_sha256"],
            expected_csv_sha256=config["binance_csv_sha256"],
            expected_symbol=config["binance_symbol"],
        )
        _bitmex_manifest, bitmex_events, bitmex_audit = round25._read_bitmex_manifest(
            config["bitmex_manifest"],
            expected_manifest_sha256=config["bitmex_manifest_sha256"],
            expected_csv_sha256=config["bitmex_csv_sha256"],
            expected_symbol=config["bitmex_symbol"],
            expected_binance_symbol=config["binance_symbol"],
        )

        assert len(binance_events) == round25.EXPECTED_EVENT_COUNT
        assert len(bitmex_events) == round25.EXPECTED_EVENT_COUNT
        assert binance_audit["passed"] is True
        assert bitmex_audit["passed"] is True
        assert bitmex_audit["source_page_count"] == round25.EXPECTED_PAGE_COUNT


def test_round22_recovers_one_identical_293_window_definition() -> None:
    payload = json.loads(round25.ROUND22_RESULT_PATH.read_text(encoding="utf-8"))

    datasets, audit = round25._recover_round22_windows(payload)

    assert audit["window_count"] == 293
    assert audit["unique_window_ids"] == 293
    assert {
        role: {split: len(windows) for split, windows in splits.items()}
        for role, splits in datasets.items()
    } == {
        "PREHISTORY": {"external": 28},
        "CURRENT": {"development": 108, "validation_complete_months": 49},
        "POSTHISTORY": {"external": 108},
    }
    assert all(
        item["base_cost_and_symbols_identical"]
        for item in audit["groups"].values()
    )


def test_positive_spread_shorts_binance_and_pays_two_venue_round_trip_fees() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    result = round25._spread_window_result(
        _window("w1", start),
        [_event(start, 3, 0.0006), _event(start, 11, 0.0004)],
        [_event(start, 7, 0.0001), _event(start, 15, 0.0003)],
        asset="BTC",
        binance_symbol="BTCUSDT",
        bitmex_symbol="XBTUSD",
        gross_capital=500.0,
        maker_fee_rate=0.0002,
    )

    assert result["oracle_direction"] == "SHORT_BINANCE_LONG_BITMEX"
    assert result["funding_spread_sum"] == pytest.approx(0.0006)
    assert result["funding_income"] == pytest.approx(0.15)
    assert result["fees_paid"] == pytest.approx(0.2)
    assert result["net_pnl"] == pytest.approx(-0.05)
    assert result["minimum_path_pnl"] == pytest.approx(-0.1)


def test_negative_spread_reverses_both_venues_and_uses_timestamp_path() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    result = round25._spread_window_result(
        _window("w1", start),
        [_event(start, 11, -0.0002)],
        [_event(start, 3, 0.0010)],
        asset="ETH",
        binance_symbol="ETHUSDT",
        bitmex_symbol="ETHUSD",
        gross_capital=300.0,
        maker_fee_rate=0.0002,
    )

    assert result["oracle_direction"] == "LONG_BINANCE_SHORT_BITMEX"
    assert result["funding_spread_sum"] == pytest.approx(-0.0012)
    assert result["funding_income"] == pytest.approx(0.18)
    assert result["fees_paid"] == pytest.approx(0.12)
    assert result["net_pnl"] == pytest.approx(0.06)
    assert result["minimum_path_pnl"] == pytest.approx(-0.06)


def test_events_are_assigned_once_with_full_window_coverage() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    windows = [
        _window("w1", start),
        _window("w2", start + timedelta(days=7)),
    ]
    events = [
        _event(start, 3, 0.0001),
        _event(start + timedelta(days=7), 3, 0.0002),
    ]

    assigned, audit = round25._events_by_window(events, windows, label="TEST")

    assert list(assigned) == ["w1", "w2"]
    assert audit["assigned_event_count"] == 2
    assert audit["window_coverage_ratio"] == 1.0
    assert audit["events_assigned_once"] is True


def test_symbol_metrics_apply_all_registered_gates() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    pnl_values = [1.0, 1.0, 1.0, -0.5]
    results = []
    for index, pnl in enumerate(pnl_values):
        results.append(
            {
                "market_close": (start + timedelta(days=7 * index)).isoformat(),
                "net_pnl": pnl,
                "minimum_path_pnl": min(0.0, pnl - 0.25),
                "round_trip_count": 1,
                "leg_count": 2,
                "binance_event_count": 6,
                "bitmex_event_count": 6,
                "funding_income": max(0.0, pnl) + 0.2,
                "binance_funding_pnl": (max(0.0, pnl) + 0.2) / 2,
                "bitmex_funding_pnl": (max(0.0, pnl) + 0.2) / 2,
                "fees_paid": 0.2,
                "oracle_direction": (
                    "SHORT_BINANCE_LONG_BITMEX"
                    if index % 2 == 0
                    else "LONG_BINANCE_SHORT_BITMEX"
                ),
            }
        )

    item = round25._symbol_metrics(results, gross_capital=500.0)

    assert item["passed"] is True
    assert item["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert item["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert item["metrics"]["positive_window_ratio"] == pytest.approx(0.75)
    assert item["metrics"]["binance_window_coverage_ratio"] == 1.0
    assert item["metrics"]["bitmex_window_coverage_ratio"] == 1.0
    assert all(item["checks"].values())
