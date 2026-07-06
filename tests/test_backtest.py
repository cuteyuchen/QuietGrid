from __future__ import annotations

from datetime import datetime, timezone

from core.models import GridParams
from strategy.backtest import BacktestConfig, run_grid_backtest


def _params() -> GridParams:
    return GridParams(
        symbol="AAPLUSDT",
        upper=101.0,
        lower=99.0,
        center=100.0,
        grid_num=2,
        step_pct=0.01,
        grid_prices=[99.0, 100.0, 101.0],
        baseline_atr=0.2,
        stop_loss_price=98.0,
        calculated_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
    )


def test_backtest_realizes_long_grid_cycle() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 100.2, "low": 99.8, "close": 100.0, "timestamp": "bar-1"},
            {"high": 101.0, "low": 100.8, "close": 101.0, "timestamp": "bar-2"},
        ],
        current_price=101.0,
        config=BacktestConfig(capital=202, leverage=1),
    )

    assert [fill.side for fill in result.fills] == ["BUY", "SELL"]
    assert [fill.timestamp for fill in result.fills] == ["bar-1", "bar-2"]
    assert result.gross_grid_pnl == 1.0
    assert result.fees_paid == 0.0
    assert result.realized_pnl == 1.0
    assert result.unrealized_pnl == 0.0
    assert result.total_pnl == 1.0
    assert [point.equity for point in result.equity_curve] == [0.0, 1.0]
    assert result.max_equity == 1.0
    assert result.max_drawdown == 0.0
    assert result.net_position_qty == 0.0
    assert result.stopped_reason is None


def test_backtest_realizes_short_grid_cycle() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 100.2, "low": 99.8, "close": 100.0},
            {"high": 99.2, "low": 99.0, "close": 99.0},
        ],
        current_price=99.0,
        config=BacktestConfig(capital=198, leverage=1),
    )

    assert [fill.side for fill in result.fills] == ["SELL", "BUY"]
    assert result.gross_grid_pnl == 1.0
    assert result.realized_pnl == 1.0
    assert result.net_position_qty == 0.0


def test_backtest_fees_reduce_realized_pnl() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 100.2, "low": 99.8, "close": 100.0},
            {"high": 101.0, "low": 100.8, "close": 101.0},
        ],
        current_price=101.0,
        config=BacktestConfig(capital=202, leverage=1, maker_fee_rate=0.001),
    )

    assert result.gross_grid_pnl == 1.0
    assert round(result.fees_paid, 6) == 0.201
    assert round(result.realized_pnl, 6) == 0.799
    assert round(result.total_pnl, 6) == 0.799


def test_backtest_tracks_equity_curve_and_max_drawdown() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 100.2, "low": 99.8, "close": 100.0, "timestamp": "entry"},
            {"high": 100.2, "low": 99.5, "close": 99.5, "timestamp": "drawdown"},
            {"high": 101.0, "low": 100.8, "close": 101.0, "timestamp": "exit"},
        ],
        current_price=101.0,
        config=BacktestConfig(capital=202, leverage=1),
    )

    assert [point.timestamp for point in result.equity_curve] == ["entry", "drawdown", "exit"]
    assert [point.equity for point in result.equity_curve] == [0.0, -0.5, 1.0]
    assert [point.drawdown for point in result.equity_curve] == [0.0, 0.5, 0.0]
    assert result.max_equity == 1.0
    assert result.max_drawdown == 0.5


def test_backtest_stops_on_stop_loss_before_same_bar_fills() -> None:
    result = run_grid_backtest(
        _params(),
        [{"high": 100.5, "low": 97.5, "close": 98.0}],
        current_price=101.0,
        config=BacktestConfig(capital=202, leverage=1),
    )

    assert result.fills == []
    assert result.stopped_reason == "stop_loss"
    assert result.stopped_at_index == 0
    assert result.stopped_at_price == 98.0
    assert result.last_price == 98.0


def test_backtest_stops_on_range_break_before_same_bar_fills() -> None:
    result = run_grid_backtest(
        _params(),
        [{"high": 100.5, "low": 98.5, "close": 99.0}],
        current_price=101.0,
        config=BacktestConfig(capital=202, leverage=1),
    )

    assert result.fills == []
    assert result.stopped_reason == "range_break"
    assert result.stopped_at_price == 98.5


def test_backtest_rejects_invalid_inputs() -> None:
    invalid_cases = [
        ([], 101.0, BacktestConfig(), "回测K线不能为空"),
        ([{"high": 100.0, "low": 101.0, "close": 100.5}], 101.0, BacktestConfig(), "K线价格关系非法"),
        ([{"high": 100.0, "low": 99.0, "close": 99.5}], 0.0, BacktestConfig(), "current_price"),
        ([{"high": 100.0, "low": 99.0, "close": 99.5}], 101.0, BacktestConfig(maker_fee_rate=-0.1), "maker_fee_rate"),
    ]

    for klines, current_price, config, expected in invalid_cases:
        try:
            run_grid_backtest(_params(), klines, current_price, config)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("invalid backtest input should be rejected")
