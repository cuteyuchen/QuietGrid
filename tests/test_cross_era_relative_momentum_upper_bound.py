from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import scripts.cross_era_relative_momentum_upper_bound as round21
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _window(symbol: str, closes: list[float]) -> SimpleNamespace:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    rows = []
    for index, close in enumerate(closes):
        open_time = int(start.timestamp() * 1000) + index * 60_000
        rows.append(
            SimpleNamespace(
                open_time=open_time,
                close_time=open_time + 59_999,
                close=float(close),
            )
        )
    return SimpleNamespace(
        symbol=symbol,
        window_id="nyse_20200103T210000Z",
        market_close=start,
        force_close_at=start + timedelta(minutes=len(rows)),
        rows=tuple(rows),
        observation_rows=180,
        status="READY",
    )


def _momentum_prices(future_spread_offsets: list[float]) -> tuple[list[float], list[float]]:
    btc_logs = [math.log(10_000.0)]
    spreads = [0.0]
    for index in range(1, 180):
        btc_logs.append(
            btc_logs[-1] + (0.001 if index % 2 == 0 else -0.0008)
        )
        spreads.append(spreads[-1] + 0.0001)
    for index, offset in enumerate(future_spread_offsets, start=1):
        btc_logs.append(btc_logs[-1] + 0.00005 * index)
        spreads.append(spreads[179] + offset)
    btc = [math.exp(value) for value in btc_logs]
    eth = [math.exp(value + spread) for value, spread in zip(btc_logs, spreads)]
    return btc, eth


def test_protocol_hash_is_frozen() -> None:
    assert _sha256(round21.PROTOCOL_PATH.resolve()) == round21.PROTOCOL_SHA256


def test_observation_momentum_fixes_long_direction_and_best_later_exit() -> None:
    btc, eth = _momentum_prices([0.001, 0.003, 0.002])

    result = round21._relative_momentum_trade(
        _window("BTCUSDT", btc),
        _window("ETHUSDT", eth),
        maker_fee_rate=0.0,
    )

    assert result["direction"] == "LONG_SPREAD"
    assert result["direction_fixed_from_observation"] is True
    assert result["exit_index"] == 2
    assert result["causal_exit"] is True
    assert result["gross_pnl"] > 0


def test_fixed_direction_can_still_have_negative_oracle_result() -> None:
    btc, eth = _momentum_prices([-0.001, -0.002, -0.003])

    result = round21._relative_momentum_trade(
        _window("BTCUSDT", btc),
        _window("ETHUSDT", eth),
        maker_fee_rate=0.0002,
    )

    assert result["direction"] == "LONG_SPREAD"
    assert result["gross_pnl"] < 0
    assert result["net_pnl"] < result["gross_pnl"]


def test_cell_metrics_apply_upper_bound_gates() -> None:
    pnl_values = [1.0, 1.0, 1.0, -0.5]
    trades = []
    for index, pnl in enumerate(pnl_values):
        trades.append(
            {
                "market_close": f"2020-01-{index + 1:02d}T00:00:00+00:00",
                "net_pnl": pnl,
                "minimum_path_pnl": min(0.0, pnl - 0.25),
                "fees_paid": 0.2,
                "beta": 1.0,
                "momentum": 0.01,
                "direction": "LONG_SPREAD",
                "direction_fixed_from_observation": True,
                "causal_exit": True,
                "trade_count": 1,
            }
        )

    result = round21._cell_metrics(trades, authorized_window_count=4)

    assert result["passed"] is True
    assert result["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert result["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert result["metrics"]["positive_window_ratio"] == pytest.approx(0.75)
    assert all(result["checks"].values())
