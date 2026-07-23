from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_cross_asset_premium_dispersion_upper_bound as round24
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _window(start_ms: int, row_count: int = 182) -> dict[str, object]:
    market_close = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    force_close_at = market_close + timedelta(minutes=row_count)
    return {
        "role": "TEST",
        "split": "external",
        "window_id": "w1",
        "market_close": market_close.isoformat(),
        "force_close_at": force_close_at.isoformat(),
        "start_ms": start_ms,
        "end_ms": start_ms + row_count * 60_000,
    }


def _rows(
    start_ms: int,
    *,
    entry: float,
    exits: tuple[float, float],
) -> list[tuple[int, float]]:
    closes = [0.0] * 179 + [entry, *exits]
    return [
        (start_ms + index * 60_000, close)
        for index, close in enumerate(closes)
    ]


def test_protocol_and_round23_result_are_frozen() -> None:
    assert _sha256(round24.PROTOCOL_PATH.resolve()) == round24.PROTOCOL_SHA256
    assert _sha256(round24.ROUND23_RESULT_PATH.resolve()) == round24.ROUND23_RESULT_SHA256


def test_positive_entry_spread_uses_btc_convergence_and_joint_exit() -> None:
    start_ms = 1_700_000_000_000
    entry_time = start_ms + 180 * 60_000
    result = round24._dispersion_window_result(
        _window(start_ms),
        _rows(start_ms, entry=0.01, exits=(0.006, 0.008)),
        _rows(start_ms, entry=0.005, exits=(0.004, 0.0)),
        [{"funding_time": entry_time + 60_000, "funding_rate": 0.001}],
        [{"funding_time": entry_time + 60_000, "funding_rate": -0.001}],
        maker_fee_rate=0.0002,
    )

    assert result["direction"] == "BTC_CONVERGENCE_ETH_REVERSE"
    assert result["holding_minutes"] == 1
    assert result["btc_basis_pnl"] == pytest.approx(0.6)
    assert result["eth_basis_pnl"] == pytest.approx(-0.15)
    assert result["joint_funding_pnl"] == pytest.approx(0.3)
    assert result["fees_paid"] == pytest.approx(0.24)
    assert result["net_pnl"] == pytest.approx(0.51)


def test_negative_entry_spread_reverses_both_basis_books() -> None:
    start_ms = 1_700_000_000_000
    entry_time = start_ms + 180 * 60_000
    result = round24._dispersion_window_result(
        _window(start_ms),
        _rows(start_ms, entry=0.003, exits=(0.002, -0.002)),
        _rows(start_ms, entry=0.01, exits=(0.006, 0.008)),
        [{"funding_time": entry_time + 60_000, "funding_rate": -0.001}],
        [{"funding_time": entry_time + 60_000, "funding_rate": 0.001}],
        maker_fee_rate=0.0002,
    )

    assert result["direction"] == "ETH_CONVERGENCE_BTC_REVERSE"
    assert result["btc_direction"] == -1
    assert result["eth_direction"] == 1
    assert result["joint_basis_pnl"] == pytest.approx(0.45)
    assert result["joint_funding_pnl"] == pytest.approx(0.3)
    assert result["net_pnl"] == pytest.approx(0.51)


def test_joint_oracle_tie_uses_earliest_exit() -> None:
    start_ms = 1_700_000_000_000
    entry_time = start_ms + 180 * 60_000
    result = round24._dispersion_window_result(
        _window(start_ms),
        _rows(start_ms, entry=0.01, exits=(0.008, 0.008)),
        _rows(start_ms, entry=0.005, exits=(0.005, 0.005)),
        [{"funding_time": entry_time, "funding_rate": 0.0}],
        [{"funding_time": entry_time, "funding_rate": 0.0}],
        maker_fee_rate=0.0002,
    )

    assert result["holding_minutes"] == 1


def test_joint_metrics_apply_registered_gates() -> None:
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
                "btc_eligible_funding_event_count": 5,
                "eth_eligible_funding_event_count": 5,
                "joint_basis_pnl": pnl + 0.2,
                "joint_funding_pnl": 0.0,
                "btc_basis_pnl": (pnl + 0.2) / 2,
                "eth_basis_pnl": (pnl + 0.2) / 2,
                "btc_funding_pnl": 0.0,
                "eth_funding_pnl": 0.0,
                "fees_paid": 0.2,
                "holding_minutes": 60,
                "direction": (
                    "BTC_CONVERGENCE_ETH_REVERSE"
                    if index % 2 == 0
                    else "ETH_CONVERGENCE_BTC_REVERSE"
                ),
                "premium_alignment_complete": True,
            }
        )

    item = round24._joint_metrics(results)

    assert item["passed"] is True
    assert item["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert item["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert item["metrics"]["positive_window_ratio"] == pytest.approx(0.75)
    assert all(item["checks"].values())
