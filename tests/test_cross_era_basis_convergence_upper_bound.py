from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scripts.cross_era_basis_convergence_upper_bound as round23
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
        "row_count": row_count,
        "expected_row_count": row_count,
        "complete": True,
    }


def _premium_rows(
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


def test_protocol_and_frozen_inputs_match() -> None:
    assert _sha256(round23.PROTOCOL_PATH.resolve()) == round23.PROTOCOL_SHA256
    assert _sha256(round23.ROUND22_RESULT_PATH.resolve()) == round23.ROUND22_RESULT_SHA256
    base = Path("data/backtests/round23_premium_index")
    assert _sha256(
        (
            base
            / "binance_um_premium_index_btcusdt_202001_202306_202408_202606.manifest.json"
        ).resolve()
    ) == round23.PREMIUM_MANIFEST_SHA256["BTCUSDT"]
    assert _sha256(
        (
            base
            / "binance_um_premium_index_ethusdt_202001_202306_202408_202606.manifest.json"
        ).resolve()
    ) == round23.PREMIUM_MANIFEST_SHA256["ETHUSDT"]


def test_positive_entry_premium_uses_short_perp_and_oracle_exit() -> None:
    start_ms = 1_700_000_000_000
    entry_time = start_ms + 180 * 60_000
    result = round23._basis_window_result(
        _window(start_ms),
        _premium_rows(start_ms, entry=0.01, exits=(0.006, 0.008)),
        [{"funding_time": entry_time + 60_000, "funding_rate": 0.001}],
        symbol="BTCUSDT",
        capital=500.0,
        maker_fee_rate=0.0002,
    )

    assert result["direction"] == "LONG_SPOT_SHORT_PERP"
    assert result["holding_minutes"] == 1
    assert result["basis_pnl"] == pytest.approx(1.0)
    assert result["funding_pnl"] == pytest.approx(0.25)
    assert result["fees_paid"] == pytest.approx(0.2)
    assert result["net_pnl"] == pytest.approx(1.05)


def test_negative_entry_premium_reverses_both_basis_and_funding_sign() -> None:
    start_ms = 1_700_000_000_000
    entry_time = start_ms + 180 * 60_000
    result = round23._basis_window_result(
        _window(start_ms),
        _premium_rows(start_ms, entry=-0.01, exits=(-0.006, -0.008)),
        [{"funding_time": entry_time + 60_000, "funding_rate": -0.001}],
        symbol="ETHUSDT",
        capital=300.0,
        maker_fee_rate=0.0002,
    )

    assert result["direction"] == "SHORT_SPOT_LONG_PERP"
    assert result["basis_pnl"] == pytest.approx(0.6)
    assert result["funding_pnl"] == pytest.approx(0.15)
    assert result["fees_paid"] == pytest.approx(0.12)
    assert result["net_pnl"] == pytest.approx(0.63)


def test_oracle_tie_uses_earliest_exit() -> None:
    start_ms = 1_700_000_000_000
    entry_time = start_ms + 180 * 60_000
    result = round23._basis_window_result(
        _window(start_ms),
        _premium_rows(start_ms, entry=0.01, exits=(0.008, 0.008)),
        [{"funding_time": entry_time, "funding_rate": 0.0}],
        symbol="BTCUSDT",
        capital=500.0,
        maker_fee_rate=0.0002,
    )

    assert result["holding_minutes"] == 1


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
                "eligible_funding_event_count": 5,
                "realized_funding_event_count": 2,
                "basis_pnl": pnl + 0.2,
                "funding_pnl": 0.0,
                "fees_paid": 0.2,
                "holding_minutes": 60,
                "direction": (
                    "LONG_SPOT_SHORT_PERP"
                    if index % 2 == 0
                    else "SHORT_SPOT_LONG_PERP"
                ),
                "premium_coverage_complete": True,
            }
        )

    item = round23._symbol_metrics(results, capital=500.0)

    assert item["passed"] is True
    assert item["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert item["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert item["metrics"]["positive_window_ratio"] == pytest.approx(0.75)
    assert all(item["checks"].values())
