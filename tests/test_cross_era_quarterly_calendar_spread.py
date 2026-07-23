from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_quarterly_calendar_spread as round26
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _window(
    *,
    perpetual: tuple[float, float],
    quarterly: tuple[float, float],
) -> dict[str, object]:
    start = datetime(2022, 1, 7, 20, tzinfo=UTC)
    rows = []
    for index in range(round26.EXPECTED_ROWS_PER_WINDOW):
        ratio = index / (round26.EXPECTED_ROWS_PER_WINDOW - 1)
        rows.append(
            {
                "open_time": int((start + timedelta(hours=index)).timestamp() * 1000),
                "perpetual_open": perpetual[0] + (perpetual[1] - perpetual[0]) * ratio,
                "quarterly_open": quarterly[0] + (quarterly[1] - quarterly[0]) * ratio,
            }
        )
    return {
        "role": "TEST",
        "window_id": "w1",
        "window_start": start,
        "window_end": start + timedelta(days=7),
        "perpetual_symbol": "BTCUSDT",
        "quarterly_symbol": "BTCUSDT_220325",
        "rows": rows,
    }


def test_protocol_results_and_frozen_inputs_are_locked() -> None:
    assert _sha256(round26.PROTOCOL_PATH.resolve()) == round26.PROTOCOL_SHA256
    assert _sha256(round26.ROUND25_RESULT_PATH.resolve()) == round26.ROUND25_RESULT_SHA256
    for _asset, config in round26.ASSET_CONFIG.items():
        assert _sha256(config["price_manifest"].resolve()) == config["price_manifest_sha256"]
        assert _sha256(config["funding_manifest"].resolve()) == config["funding_manifest_sha256"]


def test_price_manifests_have_206_aligned_windows() -> None:
    windows = {}
    for asset, config in round26.ASSET_CONFIG.items():
        _manifest, asset_windows, audit = round26._read_price_manifest(
            config["price_manifest"],
            expected_manifest_sha256=config["price_manifest_sha256"],
            expected_csv_sha256=config["price_csv_sha256"],
            expected_asset=asset,
            expected_perpetual_symbol=config["perpetual_symbol"],
        )
        windows[asset] = asset_windows
        assert audit["window_count"] == 206
        assert audit["row_count"] == 34_814
        assert audit["passed"] is True

    assert round26._assert_asset_windows_aligned(windows)["passed"] is True


def test_positive_basis_longs_perpetual_and_includes_funding() -> None:
    window = _window(perpetual=(100.0, 110.0), quarterly=(105.0, 112.0))
    funding_time = window["rows"][4]["open_time"] + 47

    result = round26._window_result(
        window,
        [{"funding_time": funding_time, "funding_rate": 0.001}],
        asset="BTC",
        gross_capital=500.0,
        maker_fee_rate=0.0002,
    )

    quantity = 500.0 / 205.0
    assert result["direction"] == "LONG_PERPETUAL_SHORT_QUARTERLY"
    assert result["price_pnl"] == pytest.approx(quantity * 3.0)
    expected_funding = -quantity * window["rows"][4]["perpetual_open"] * 0.001
    assert result["funding_pnl"] == pytest.approx(expected_funding)
    assert result["entry_fee"] == pytest.approx(0.1)
    assert result["direction_is_causal"] is True
    assert result["oracle_direction"] is False
    assert result["maximum_funding_timestamp_offset_ms"] == 47


def test_negative_basis_reverses_both_legs() -> None:
    window = _window(perpetual=(100.0, 95.0), quarterly=(98.0, 94.0))

    result = round26._window_result(
        window,
        [{"funding_time": window["rows"][4]["open_time"], "funding_rate": 0.001}],
        asset="ETH",
        gross_capital=300.0,
        maker_fee_rate=0.0002,
    )

    assert result["direction"] == "SHORT_PERPETUAL_LONG_QUARTERLY"
    assert result["position_sign"] == -1
    assert result["price_pnl"] > 0
    assert result["funding_pnl"] > 0


def test_symbol_metrics_apply_registered_gates() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    pnl_values = [1.0, 1.0, 1.0, -0.5]
    results = []
    for index, pnl in enumerate(pnl_values):
        results.append(
            {
                "window_start": (start + timedelta(days=7 * index)).isoformat(),
                "net_pnl": pnl,
                "minimum_path_pnl": min(0.0, pnl - 0.25),
                "hourly_row_count": 169,
                "funding_event_count": 21,
                "round_trip_count": 1,
                "leg_count": 2,
                "direction_is_causal": True,
                "oracle_direction": False,
                "direction": (
                    "LONG_PERPETUAL_SHORT_QUARTERLY"
                    if index % 2 == 0
                    else "SHORT_PERPETUAL_LONG_QUARTERLY"
                ),
                "price_pnl": pnl + 0.2,
                "funding_pnl": 0.0,
                "fees_paid": 0.2,
            }
        )

    item = round26._symbol_metrics(results, gross_capital=500.0)

    assert item["passed"] is True
    assert item["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert item["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert item["metrics"]["positive_window_ratio"] == pytest.approx(0.75)
    assert all(item["checks"].values())
