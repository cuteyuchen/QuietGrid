from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import scripts.cross_era_cross_asset_zscore_taker as round20
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _rows(opens: list[float], closes: list[float]) -> tuple[SimpleNamespace, ...]:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    result = []
    for index, (open_price, close_price) in enumerate(zip(opens, closes)):
        open_time = int(start.timestamp() * 1000) + index * 60_000
        result.append(
            SimpleNamespace(
                open_time=open_time,
                close_time=open_time + 59_999,
                open=float(open_price),
                high=max(float(open_price), float(close_price)),
                low=min(float(open_price), float(close_price)),
                close=float(close_price),
            )
        )
    return tuple(result)


def _window(symbol: str, rows: tuple[SimpleNamespace, ...]) -> SimpleNamespace:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    return SimpleNamespace(
        symbol=symbol,
        window_id="nyse_20200103T210000Z",
        market_close=start,
        force_close_at=start + timedelta(minutes=len(rows)),
        rows=rows,
        observation_rows=180,
        status="READY",
    )


def _observation_prices() -> tuple[list[float], list[float]]:
    btc_logs = [math.log(10_000.0)]
    spread = []
    for index in range(180):
        if index:
            btc_logs.append(
                btc_logs[-1] + (0.0012 if index % 2 == 0 else -0.0009)
            )
        spread.append(0.018 * math.sin(index / 7.0))
    btc = [math.exp(value) for value in btc_logs]
    eth = [math.exp(value + item) for value, item in zip(btc_logs, spread)]
    return btc, eth


def test_protocol_hash_is_frozen() -> None:
    assert _sha256(round20.PROTOCOL_PATH.resolve()) == round20.PROTOCOL_SHA256


def test_trade_plan_is_next_bar_causal_and_exits_on_mean() -> None:
    values = [0.0] * 180 + [-2.2, -1.1, 0.2, 0.1]

    plan = round20._trade_plan(values)

    assert plan["direction"] == "LONG_SPREAD"
    assert plan["entry_signal_index"] == 180
    assert plan["entry_execution_index"] == 181
    assert plan["exit_signal_index"] == 182
    assert plan["exit_execution_index"] == 183
    assert plan["exit_reason"] == "MEAN_REVERSION"


def test_trade_plan_does_not_enter_on_last_bar_signal() -> None:
    values = [0.0] * 180 + [2.5]

    assert round20._trade_plan(values) == {"status": "NO_ENTRY"}


def test_adverse_price_and_funding_boundaries() -> None:
    assert round20._adverse_price(100.0, "BUY", 10.0) == pytest.approx(100.1)
    assert round20._adverse_price(100.0, "SELL", 10.0) == pytest.approx(99.9)
    interval = round20.FUNDING_INTERVAL_MS
    assert round20._funding_settlement_count(interval - 1, 3 * interval) == 3
    assert round20._funding_settlement_count(interval, 3 * interval - 1) == 1


def test_spread_state_uses_observation_only_and_has_positive_beta() -> None:
    btc, eth = _observation_prices()
    btc_window = _window("BTCUSDT", _rows(btc, btc))
    eth_window = _window("ETHUSDT", _rows(eth, eth))

    state = round20._spread_state(btc_window, eth_window)

    assert state["beta"] > 0
    assert state["spread_std"] > 0


def test_simulated_pair_trade_is_causal_and_flat_at_end() -> None:
    btc_observation, eth_observation = _observation_prices()
    provisional_btc = _window(
        "BTCUSDT",
        _rows(btc_observation, btc_observation),
    )
    provisional_eth = _window(
        "ETHUSDT",
        _rows(eth_observation, eth_observation),
    )
    state = round20._spread_state(provisional_btc, provisional_eth)
    beta = state["beta"]
    mean = state["spread_mean"]
    std = state["spread_std"]
    desired_z = [-2.3, -1.2, 0.2, 0.1]
    btc_extra = [btc_observation[-1] * math.exp(0.0002 * (index + 1)) for index in range(4)]
    eth_extra = [
        math.exp(beta * math.log(btc_price) + mean + z_value * std)
        for btc_price, z_value in zip(btc_extra, desired_z)
    ]
    btc = btc_observation + btc_extra
    eth = eth_observation + eth_extra
    btc_window = _window("BTCUSDT", _rows(btc, btc))
    eth_window = _window("ETHUSDT", _rows(eth, eth))

    result = round20._simulate_pair_window(
        btc_window,
        eth_window,
        taker_fee_rate=0.0,
        slippage_bps=0.0,
    )

    assert result["status"] == "TRADED"
    assert result["direction"] == "LONG_SPREAD"
    assert result["exit_reason"] == "MEAN_REVERSION"
    assert result["causal_execution"] is True
    assert result["flat_at_end"] is True
    assert result["entry_execution_time"] > result["entry_signal_close_time"]
    assert result["exit_execution_time"] > result["exit_signal_close_time"]
    assert result["net_pnl"] > 0


def test_cell_metrics_apply_registered_phase_a_gates() -> None:
    pnl_values = [1.0, 1.0, 1.0, -0.5]
    windows = []
    for index, pnl in enumerate(pnl_values):
        windows.append(
            {
                "market_close": f"2020-01-{index + 1:02d}T00:00:00+00:00",
                "net_pnl": pnl,
                "minimum_path_pnl": min(0.0, pnl - 0.5),
                "trade_count": 1,
                "fees_paid": 0.5,
                "funding_paid": 0.0,
                "beta": 1.0,
                "spread_std": 0.01,
                "causal_execution": True,
                "flat_at_end": True,
                "holding_minutes": 30.0,
                "exit_reason": "MEAN_REVERSION",
            }
        )

    result = round20._cell_metrics(windows, authorized_window_count=4)

    assert result["passed"] is True
    assert result["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert result["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert result["metrics"]["trade_coverage"] == pytest.approx(1.0)
    assert all(result["checks"].values())
