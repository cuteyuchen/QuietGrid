from __future__ import annotations

from datetime import datetime, timezone

from core.models import GridParams
from strategy.backtest import BacktestConfig, LookAheadViolation, run_grid_backtest


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


def test_conservative_fill_requires_price_to_cross_one_tick() -> None:
    exact_touch = run_grid_backtest(
        _params(),
        [{"high": 101.0, "low": 99.0, "close": 100.0}],
        current_price=100.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            maker_fill_probability=1,
        ),
    )
    crossed = run_grid_backtest(
        _params(),
        [{"high": 100.0, "low": 98.98, "close": 99.5}],
        current_price=100.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            maker_fill_probability=1,
            stop_on_range_break=False,
        ),
    )

    assert exact_touch.fills == []
    assert [fill.side for fill in crossed.fills] == ["BUY"]


def test_conservative_fill_model_caps_fills_and_reports_rejections() -> None:
    result = run_grid_backtest(
        _params(),
        [{"high": 102.0, "low": 98.0, "close": 100.0}],
        current_price=100.0,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            max_fills_per_bar=1,
            maker_fill_probability=1,
            stop_on_range_break=False,
            stop_on_stop_loss=False,
        ),
    )

    assert result.attempted_fill_count == 2
    assert len(result.fills) == 1
    assert result.rejected_fill_count == 1
    assert result.max_inventory_utilization > 0


def test_conservative_stop_liquidates_inventory_with_slippage_and_taker_fee() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 100.2, "low": 98.98, "close": 99.5},
            {"high": 99.0, "low": 97.5, "close": 98.0},
        ],
        current_price=101.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            maker_fill_probability=1,
            max_fills_per_bar=1,
            taker_fee_rate=0.001,
            stop_slippage_bps=10,
            stop_on_range_break=False,
        ),
    )

    assert len(result.fills) == 1
    assert result.stopped_reason == "stop_loss"
    assert result.stopped_at_price is not None
    assert result.stopped_at_price < 98.0
    assert result.stop_exit_pnl < 0
    assert result.stop_exit_cost > 0
    assert result.net_position_qty == 0
    assert result.unrealized_pnl == 0


def test_backtest_rejects_future_available_data_and_reverse_time() -> None:
    future_row = {
        "high": 100.0,
        "low": 99.0,
        "close": 99.5,
        "event_time": "2026-07-01T00:00:00Z",
        "available_time": "2026-07-01T00:01:00Z",
    }
    try:
        run_grid_backtest(_params(), [future_row], 101.0)
    except LookAheadViolation as exc:
        assert "之后才可获得" in str(exc)
    else:
        raise AssertionError("future data must invalidate the backtest")

    reverse_rows = [
        {"high": 100.0, "low": 99.0, "close": 99.5, "timestamp": "2026-07-01T00:02:00Z"},
        {"high": 100.0, "low": 99.0, "close": 99.5, "timestamp": "2026-07-01T00:01:00Z"},
    ]
    try:
        run_grid_backtest(_params(), reverse_rows, 101.0)
    except LookAheadViolation as exc:
        assert "倒序" in str(exc)
    else:
        raise AssertionError("reverse event time must invalidate the backtest")
