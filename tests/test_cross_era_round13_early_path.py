from __future__ import annotations

import pytest

from strategy.backtest import BacktestEquityPoint, BacktestFill, BacktestResult

from scripts.cross_era_round13_early_path import (
    _aggregate_records,
    _checkpoint_summary,
    _inventory_state,
)


def _fill(
    *,
    side: str,
    position_side: str,
    intent: str,
    qty: float,
    bar: int,
    price: float,
    paired: bool = False,
) -> BacktestFill:
    return BacktestFill(
        symbol="BTCUSDT",
        side=side,
        grid_index=1,
        price=price,
        qty=qty,
        fee=0.0,
        grid_pnl=1.0 if paired else None,
        realized_pnl_after=0.0,
        bar_index=bar,
        position_side=position_side,
        order_intent=intent,
    )


def _result(fills: list[BacktestFill]) -> BacktestResult:
    points = [
        BacktestEquityPoint(
            bar_index=index,
            equity=-float(index),
            realized_pnl=0.0,
            unrealized_pnl=-float(index),
            drawdown=float(index),
            close=100.0 - index,
            gross_inventory_notional=50.0,
            inventory_utilization=0.5,
        )
        for index in range(5)
    ]
    return BacktestResult(
        symbol="BTCUSDT",
        fills=fills,
        equity_curve=points,
        gross_grid_pnl=0.0,
        fees_paid=0.0,
        realized_pnl=0.0,
        unrealized_pnl=-4.0,
        total_pnl=-4.0,
        max_equity=0.0,
        max_drawdown=4.0,
        open_order_count=0,
        net_position_qty=1.0,
        stopped_reason="stop_loss",
        stopped_at_index=4,
        stopped_at_price=96.0,
        last_price=96.0,
        pair_completion_count=sum(fill.grid_pnl is not None for fill in fills),
        max_inventory_utilization=0.5,
    )


def test_inventory_state_applies_open_and_reduce_by_position_side() -> None:
    fills = [
        _fill(side="BUY", position_side="LONG", intent="OPEN", qty=2.0, bar=0, price=100.0),
        _fill(
            side="SELL",
            position_side="LONG",
            intent="REDUCE",
            qty=0.5,
            bar=1,
            price=101.0,
            paired=True,
        ),
        _fill(side="SELL", position_side="SHORT", intent="OPEN", qty=1.0, bar=2, price=99.0),
    ]

    state = _inventory_state(fills, 2)

    assert state["long_qty"] == 1.5
    assert state["short_qty"] == 1.0
    assert state["net_qty"] == 0.5
    assert state["net_side"] == "LONG"


def test_checkpoint_summary_measures_long_adverse_unpaired_path() -> None:
    first = _fill(
        side="BUY",
        position_side="LONG",
        intent="OPEN",
        qty=1.0,
        bar=0,
        price=100.0,
    )
    klines = [
        {"high": 101.0, "low": 99.0, "close": 100.0},
        {"high": 100.0, "low": 97.0, "close": 98.0},
        {"high": 99.0, "low": 96.0, "close": 97.0},
        {"high": 98.0, "low": 95.0, "close": 96.0},
        {"high": 97.0, "low": 94.0, "close": 95.0},
    ]

    checkpoint = _checkpoint_summary(_result([first]), klines, first, 3)

    assert checkpoint["observed_bar_index"] == 2
    assert checkpoint["directional_return_pct"] < 0
    assert checkpoint["adverse_excursion_pct"] == pytest.approx(0.04)
    assert checkpoint["pair_count"] == 0
    assert checkpoint["adverse_unpaired_inventory"] is True


def test_aggregate_records_separates_loss_and_profitable_groups() -> None:
    checkpoint = {
        "full_horizon_observed": True,
        "adverse_excursion_pct": 0.02,
        "favorable_excursion_pct": 0.01,
        "directional_return_pct": -0.01,
        "path_efficiency": 0.5,
        "zero_pair": True,
        "adverse_unpaired_inventory": True,
        "inventory_utilization": 0.4,
        "equity": -1.0,
        "fill_count": 1,
        "pair_count": 0,
    }
    records = []
    labels = {"loss": "PERSISTENT_LOSS", "win": "PROFITABLE"}
    for window_id, pnl in (("loss", -2.0), ("win", 2.0)):
        for seed in (3, 10, 17, 31, 59, 97):
            records.append({
                "seed": seed,
                "window_id": window_id,
                "pnl": pnl,
                "first_entry_side": "LONG",
                "first_entry_bar_index": 10,
                "bars_from_entry_to_stop": 100 if pnl < 0 else None,
                "pair_count": 0,
                "checkpoints": {str(horizon): checkpoint for horizon in (30, 60, 120)},
            })

    windows, summary = _aggregate_records(records, labels)

    assert len(windows) == 2
    assert summary["PERSISTENT_LOSS"]["window_count"] == 1
    assert summary["PROFITABLE"]["mean_pnl"] == 2.0
    assert summary["PERSISTENT_LOSS"]["checkpoints"]["60"][
        "adverse_unpaired_inventory_rate"
    ] == 1.0
