from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.cross_era_funding_carry_upper_bound as round22
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _window(window_id: str, start: datetime) -> dict[str, object]:
    return {
        "window_id": window_id,
        "market_close": start,
        "force_close_at": start + timedelta(hours=48),
    }


def test_protocol_and_funding_manifests_are_frozen() -> None:
    assert _sha256(round22.PROTOCOL_PATH.resolve()) == round22.PROTOCOL_SHA256
    base = Path("data/backtests/round22_funding_carry")
    assert _sha256(
        (base / "binance_um_funding_btcusdt_202001_202306_202408_202606.manifest.json").resolve()
    ) == round22.BTC_MANIFEST_SHA256
    assert _sha256(
        (base / "binance_um_funding_ethusdt_202001_202306_202408_202606.manifest.json").resolve()
    ) == round22.ETH_MANIFEST_SHA256


def test_events_are_assigned_once_to_disjoint_windows() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    windows = [
        _window("w1", start),
        _window("w2", start + timedelta(days=7)),
    ]
    events = [
        {"funding_time": int((start + timedelta(hours=3)).timestamp() * 1000), "funding_rate": 0.0001},
        {"funding_time": int((start + timedelta(days=7, hours=3)).timestamp() * 1000), "funding_rate": 0.0002},
    ]

    assigned, audit = round22._events_by_window(events, windows)

    assert list(assigned) == ["w1", "w2"]
    assert audit["assigned_event_count"] == 2
    assert audit["events_assigned_once"] is True


def test_carry_window_uses_oracle_sign_but_pays_round_trip_fees() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    events = [
        {"funding_rate": 0.0004},
        {"funding_rate": 0.0006},
    ]

    result = round22._carry_window_result(
        _window("w1", start),
        events,
        symbol="BTCUSDT",
        capital=500.0,
        maker_fee_rate=0.0002,
    )

    assert result["oracle_direction"] == "LONG_SPOT_SHORT_PERP"
    assert result["funding_income"] == pytest.approx(0.25)
    assert result["fees_paid"] == pytest.approx(0.2)
    assert result["net_pnl"] == pytest.approx(0.05)


def test_negative_funding_reverses_oracle_direction() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    result = round22._carry_window_result(
        _window("w1", start),
        [{"funding_rate": -0.001}],
        symbol="ETHUSDT",
        capital=300.0,
        maker_fee_rate=0.0002,
    )

    assert result["oracle_direction"] == "SHORT_SPOT_LONG_PERP"
    assert result["funding_income"] == pytest.approx(0.15)
    assert result["net_pnl"] == pytest.approx(0.03)


def test_symbol_metrics_apply_registered_gates() -> None:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    pnl_values = [1.0, 1.0, 1.0, -0.5]
    results = []
    for index, pnl in enumerate(pnl_values):
        results.append(
            {
                "market_close": (start + timedelta(days=7 * index)).isoformat(),
                "net_pnl": pnl,
                "minimum_path_pnl": min(0.0, pnl - 0.25),
                "trade_count": 1,
                "event_count": 6,
                "funding_income": max(0.0, pnl) + 0.2,
                "fees_paid": 0.2,
            }
        )

    item = round22._symbol_metrics(results, capital=500.0)

    assert item["passed"] is True
    assert item["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert item["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert item["metrics"]["positive_window_ratio"] == pytest.approx(0.75)
    assert all(item["checks"].values())
