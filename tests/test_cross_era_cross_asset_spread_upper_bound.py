from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import scripts.cross_era_cross_asset_spread_upper_bound as round19
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def _prices(returns: list[float], start: float = 100.0) -> list[float]:
    values = [start]
    for value in returns:
        values.append(values[-1] * math.exp(value))
    return values


def _window(
    symbol: str,
    closes: list[float],
    *,
    window_id: str = "nyse_20200103T210000Z",
    time_offset_ms: int = 0,
) -> SimpleNamespace:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    rows = []
    for index, close in enumerate(closes):
        open_time = int(start.timestamp() * 1000) + index * 60_000 + time_offset_ms
        rows.append(
            SimpleNamespace(
                open_time=open_time,
                close_time=open_time + 59_999,
                close=float(close),
            )
        )
    return SimpleNamespace(
        symbol=symbol,
        window_id=window_id,
        market_close=start,
        force_close_at=start + timedelta(minutes=len(rows)),
        rows=tuple(rows),
        observation_rows=round19.OBSERVATION_ROWS,
        status="READY",
    )


def test_protocol_hash_is_frozen() -> None:
    assert _sha256(round19.PROTOCOL_PATH.resolve()) == round19.PROTOCOL_SHA256


def test_observation_beta_recovers_linear_return_beta() -> None:
    btc_returns = [0.001, -0.002, 0.0015, -0.0005, 0.0025, -0.001]
    eth_returns = [1.5 * value for value in btc_returns]

    beta = round19._observation_beta(_prices(btc_returns), _prices(eth_returns, 50.0))

    assert beta == pytest.approx(1.5, abs=1e-12)


def test_max_causal_excursion_keeps_entry_before_exit() -> None:
    result = round19._max_causal_excursion([0.0, 2.0, 1.0, -3.0, 1.5])

    assert result == {
        "magnitude": 5.0,
        "entry_index": 1,
        "exit_index": 3,
        "direction": "SHORT_SPREAD",
    }


def test_pair_windows_rejects_timestamp_mismatch() -> None:
    returns = [0.001 if index % 2 == 0 else -0.001 for index in range(180)]
    closes = _prices(returns)
    btc = _window("BTCUSDT", closes)
    eth = _window("ETHUSDT", closes, time_offset_ms=60_000)

    with pytest.raises(RuntimeError, match="分钟时间戳不一致"):
        round19._pair_windows([btc, eth], [btc.window_id])


def test_flat_spread_window_loses_exact_round_trip_fees() -> None:
    observation_returns = [
        0.001 if index % 2 == 0 else -0.0008
        for index in range(round19.OBSERVATION_ROWS - 1)
    ]
    observation = _prices(observation_returns)
    btc_closes = observation + [observation[-1] * math.exp(0.001)]
    eth_observation = [value * 0.5 for value in observation]
    eth_closes = eth_observation + [eth_observation[-1] * math.exp(0.001)]
    btc = _window("BTCUSDT", btc_closes)
    eth = _window("ETHUSDT", eth_closes)

    result = round19._evaluate_pair_window(btc, eth, maker_fee_rate=0.0002)

    assert result["beta"] == pytest.approx(1.0, abs=1e-10)
    assert result["gross_pnl"] == pytest.approx(0.0, abs=1e-10)
    assert result["fees_paid"] == pytest.approx(0.32)
    assert result["net_pnl"] == pytest.approx(-0.32, abs=1e-10)
    assert result["entry_close_time"] < result["exit_close_time"]


def test_cell_metrics_apply_all_registered_gates() -> None:
    pnl_values = [1.0, 1.0, 1.0, -0.5]
    trades = []
    for index, pnl in enumerate(pnl_values):
        fee = 0.3
        gross = pnl + 2 * fee
        trades.append(
            {
                "market_close": f"2020-01-{index + 1:02d}T00:00:00+00:00",
                "net_pnl": pnl,
                "entry_fee": fee,
                "exit_fee": fee,
                "gross_pnl": gross,
                "fees_paid": 2 * fee,
                "beta": 1.0 + index * 0.1,
                "trade_count": 1,
            }
        )

    result = round19._cell_metrics(trades)

    assert result["passed"] is True
    assert result["metrics"]["total_pnl"] == pytest.approx(2.5)
    assert result["metrics"]["profit_factor"] == pytest.approx(6.0)
    assert result["metrics"]["best_window_concentration"] == pytest.approx(1 / 3)
    assert result["metrics"]["positive_window_ratio"] == pytest.approx(0.75)
    assert all(result["checks"].values())
