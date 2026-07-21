from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from core.models import GridDirectionMode, GridParams
from strategy.backtest import BacktestConfig, LookAheadViolation, run_grid_backtest
from strategy.order_plan import build_initial_grid_order_plan


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


def test_backtest_stops_on_upper_stop_before_same_bar_fills() -> None:
    params = replace(_params(), upper_stop_loss_price=102.0)
    result = run_grid_backtest(
        params,
        [{"high": 102.5, "low": 99.5, "close": 101.0}],
        current_price=100.0,
        config=BacktestConfig(capital=202, leverage=1),
    )

    assert result.fills == []
    assert result.stopped_reason == "stop_loss_upper"
    assert result.stopped_at_index == 0
    assert result.stopped_at_price == 102.0


def test_backtest_uses_adaptive_quantity_weights_and_step_size() -> None:
    params = replace(_params(), qty_weights=(0.1, 0.6, 0.3))
    result = run_grid_backtest(
        params,
        [{"high": 101.1, "low": 98.9, "close": 100.0}],
        current_price=100.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            stop_on_stop_loss=False,
            quantity_step_size=0.1,
        ),
    )

    assert [fill.qty for fill in result.fills] == [0.5, 1.5]


def test_backtest_initial_fill_uses_live_tick_rounded_plan() -> None:
    params = replace(
        _params(),
        upper=100.26,
        lower=99.94,
        center=100.03,
        grid_prices=[99.94, 100.03, 100.26],
        qty_weights=(0.2, 0.5, 0.3),
    )
    plan = build_initial_grid_order_plan(
        params,
        100.03,
        capital=200,
        leverage=1,
        tick_size=0.1,
        quantity_step_size=0.01,
    )

    result = run_grid_backtest(
        params,
        [{"high": 100.0, "low": 99.9, "close": 100.0}],
        current_price=100.03,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            stop_on_stop_loss=False,
            min_tick_size=0.1,
            quantity_step_size=0.01,
        ),
    )

    buy = next(item for item in plan if item.side.value == "BUY")
    assert result.fills[0].grid_index == buy.grid_index
    assert result.fills[0].price == buy.price == 99.9
    assert result.fills[0].qty == buy.qty


def test_backtest_stops_on_range_break_before_same_bar_fills() -> None:
    result = run_grid_backtest(
        _params(),
        [{"high": 100.5, "low": 98.5, "close": 99.0}],
        current_price=101.0,
        config=BacktestConfig(capital=202, leverage=1, stop_on_range_break=True),
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


def test_window_force_close_liquidates_inventory_and_clears_orders() -> None:
    result = run_grid_backtest(
        _params(),
        [{"high": 100.2, "low": 98.98, "close": 99.5, "timestamp": "window-end"}],
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
            force_close_at_end=True,
        ),
    )

    assert [fill.side for fill in result.fills] == ["BUY"]
    assert result.stopped_reason == "window_force_close"
    assert result.stopped_at_index == 1
    assert result.open_order_count == 0
    assert result.net_position_qty == 0
    assert result.unrealized_pnl == 0
    assert result.stop_exit_pnl < 0
    assert result.stop_exit_cost > 0
    assert result.equity_curve[-1].inventory_utilization == 0


def test_backtest_tracks_unpaired_inventory_age_at_force_close() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 100.2, "low": 98.8, "close": 99.0},
            {"high": 100.0, "low": 99.2, "close": 99.5},
            {"high": 100.0, "low": 99.3, "close": 99.7},
        ],
        current_price=100.0,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            stop_on_stop_loss=False,
            force_close_at_end=True,
        ),
    )

    assert result.stopped_reason == "window_force_close"
    assert result.max_unpaired_lot_age_bars == 2
    assert result.exit_oldest_lot_age_bars == 3
    assert result.exit_long_qty > 0
    assert result.exit_short_qty == 0
    assert result.exit_hedged_fraction == 0


def test_fractional_reduce_target_completes_before_full_grid_step() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 99.2, "low": 98.9, "close": 99.0},
            {"high": 99.6, "low": 99.4, "close": 99.5},
        ],
        current_price=100.0,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            maker_fill_probability=1.0,
            reduce_target_step_fraction=0.5,
        ),
    )

    assert [fill.side for fill in result.fills] == ["BUY", "SELL"]
    assert [fill.price for fill in result.fills] == [99.0, 99.5]
    assert result.pair_completion_count == 1
    assert result.gross_grid_pnl == 0.5
    assert result.net_position_qty == 0


def test_wind_down_cancels_opening_orders_but_keeps_reducing_orders() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 99.2, "low": 98.9, "close": 99.0},
            {"high": 99.8, "low": 99.2, "close": 99.5},
            {"high": 100.2, "low": 99.8, "close": 100.0},
        ],
        current_price=100.0,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            wind_down_bars=2,
        ),
    )

    assert [fill.side for fill in result.fills] == ["BUY", "SELL"]
    assert result.net_position_qty == 0
    assert result.open_order_count == 0
    assert result.wind_down_entry_count == 1


def test_wind_down_reprices_reduce_orders_and_avoids_terminal_taker_exit() -> None:
    rows = [
        {"high": 99.2, "low": 98.9, "close": 99.0},
        {"high": 99.2, "low": 98.9, "close": 99.0},
        {"high": 99.6, "low": 99.0, "close": 99.2},
        {"high": 99.0, "low": 98.6, "close": 98.7},
    ]
    baseline = run_grid_backtest(
        _params(),
        rows,
        current_price=100.0,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            maker_fill_probability=1.0,
            force_close_at_end=True,
            taker_fee_rate=0.001,
            wind_down_bars=3,
        ),
    )
    repriced = run_grid_backtest(
        _params(),
        rows,
        current_price=100.0,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            maker_fill_probability=1.0,
            force_close_at_end=True,
            taker_fee_rate=0.001,
            wind_down_bars=3,
            wind_down_reprice_interval_bars=1,
            wind_down_initial_offset_steps=0.5,
        ),
    )

    assert baseline.stop_exit_cost > 0
    assert baseline.stop_exit_pnl < 0
    assert repriced.wind_down_reprice_count >= 1
    assert repriced.wind_down_maker_fill_count == 1
    assert repriced.wind_down_maker_pnl > 0
    assert repriced.stop_exit_cost == 0
    assert repriced.total_pnl > baseline.total_pnl


def test_wind_down_reprice_is_not_eligible_on_creation_bar() -> None:
    result = run_grid_backtest(
        _params(),
        [
            {"high": 99.2, "low": 98.9, "close": 99.0},
            # 本 Bar 的高点足以成交重挂单，但订单只能在收盘后生成。
            {"high": 100.0, "low": 98.9, "close": 99.0},
            {"high": 99.2, "low": 98.9, "close": 99.0},
        ],
        current_price=100.0,
        config=BacktestConfig(
            capital=200,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            maker_fill_probability=1.0,
            force_close_at_end=True,
            wind_down_bars=2,
            wind_down_reprice_interval_bars=10,
            wind_down_initial_offset_steps=0.5,
        ),
    )

    assert result.wind_down_reprice_count == 1
    assert result.wind_down_maker_fill_count == 0
    assert result.stop_exit_cost == 0  # 默认 taker 费为 0，但库存由终场退出处理。
    assert result.stop_exit_pnl == 0


def test_layered_unwind_allocates_exchange_step_across_multiple_lots() -> None:
    from strategy.backtest import _PositionLot, _unwind_allocations

    allocations = _unwind_allocations(
        [_PositionLot(99.0, 0.001), _PositionLot(98.0, 0.001)],
        unwind_fraction=0.5,
        quantity_step_size=0.001,
    )

    assert sum(item[1] for item in allocations) == 0.001
    assert sum(item[2] for item in allocations) == 0.001


def test_fill_sampling_ignores_internal_grid_index_for_same_order_semantics() -> None:
    from core.models import OrderIntent, OrderSide
    from strategy.backtest import _BacktestOrder, _deterministic_fill_allowed

    first = _BacktestOrder(
        -1000,
        OrderSide.SELL,
        100.5,
        0.001,
        99.0,
        "LONG",
        OrderIntent.REDUCE,
        True,
    )
    renumbered = _BacktestOrder(
        -2000,
        OrderSide.SELL,
        100.5,
        0.001,
        99.0,
        "LONG",
        OrderIntent.REDUCE,
        True,
    )

    outcomes = [
        _deterministic_fill_allowed("BTCUSDT", bar, first, 0.5, 17)
        for bar in range(20)
    ]
    renumbered_outcomes = [
        _deterministic_fill_allowed("BTCUSDT", bar, renumbered, 0.5, 17)
        for bar in range(20)
    ]

    assert outcomes == renumbered_outcomes


def test_inventory_caution_suppresses_same_side_opening_orders() -> None:
    params = replace(
        _params(),
        lower=98.0,
        upper=102.0,
        grid_num=4,
        grid_prices=[98.0, 99.0, 100.0, 101.0, 102.0],
        stop_loss_price=90.0,
        upper_stop_loss_price=110.0,
    )
    result = run_grid_backtest(
        params,
        [
            {"high": 99.1, "low": 98.9, "close": 99.0},
            {"high": 98.1, "low": 97.9, "close": 98.0},
        ],
        current_price=100.0,
        config=BacktestConfig(
            capital=400,
            leverage=1,
            stop_on_stop_loss=False,
            max_inventory_notional=200,
        ),
    )

    assert [fill.price for fill in result.fills] == [99.0]
    assert result.inventory_suppression_count == 1
    assert result.stopped_reason is None


def test_unpaired_lot_cap_suppresses_additional_opening_layers() -> None:
    params = replace(
        _params(),
        lower=98.0,
        upper=102.0,
        grid_num=4,
        grid_prices=[98.0, 99.0, 100.0, 101.0, 102.0],
        stop_loss_price=90.0,
        upper_stop_loss_price=110.0,
    )

    result = run_grid_backtest(
        params,
        [
            {"high": 99.1, "low": 98.9, "close": 99.0},
            {"high": 98.1, "low": 97.9, "close": 98.0},
        ],
        current_price=100.0,
        config=BacktestConfig(
            capital=400,
            leverage=1,
            stop_on_stop_loss=False,
            max_unpaired_lots_per_side=1,
        ),
    )

    assert [fill.price for fill in result.fills] == [99.0]
    assert result.inventory_suppression_count == 1
    assert result.net_position_qty > 0


def test_bar_boundary_lot_cap_allows_same_bar_fills_before_next_bar_suppression() -> None:
    params = replace(
        _params(),
        lower=98.0,
        upper=102.0,
        grid_num=4,
        grid_prices=[98.0, 99.0, 100.0, 101.0, 102.0],
        stop_loss_price=90.0,
        upper_stop_loss_price=110.0,
    )

    intrabar = run_grid_backtest(
        params,
        [{"high": 99.1, "low": 97.9, "close": 98.0}],
        current_price=100.0,
        config=BacktestConfig(
            capital=400,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            stop_on_stop_loss=False,
            max_unpaired_lots_per_side=1,
            unpaired_lot_cap_enforcement="INTRABAR",
        ),
    )
    bar_boundary = run_grid_backtest(
        params,
        [{"high": 99.1, "low": 97.9, "close": 98.0}],
        current_price=100.0,
        config=BacktestConfig(
            capital=400,
            leverage=1,
            fill_model="L0_CONSERVATIVE",
            min_tick_size=0.01,
            stop_on_stop_loss=False,
            max_unpaired_lots_per_side=1,
            unpaired_lot_cap_enforcement="BAR_BOUNDARY",
        ),
    )

    assert [fill.price for fill in intrabar.fills] == [99.0]
    assert [fill.price for fill in bar_boundary.fills] == [99.0, 98.0]
    assert bar_boundary.inventory_suppression_count == 0


def test_inventory_critical_closes_session_after_fill() -> None:
    params = replace(
        _params(),
        lower=98.0,
        upper=102.0,
        grid_num=4,
        grid_prices=[98.0, 99.0, 100.0, 101.0, 102.0],
        stop_loss_price=90.0,
        upper_stop_loss_price=110.0,
    )
    result = run_grid_backtest(
        params,
        [{"high": 99.1, "low": 98.9, "close": 99.0}],
        current_price=100.0,
        config=BacktestConfig(
            capital=400,
            leverage=1,
            stop_on_stop_loss=False,
            max_inventory_notional=100,
        ),
    )

    assert result.stopped_reason == "inventory_critical"
    assert result.inventory_critical_exit_count == 1
    assert result.net_position_qty == 0
    assert result.open_order_count == 0


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


def _funding_event(minutes_from_start: int, rate: float, base_time):
    from datetime import timedelta
    from data_sources.models import FundingEvent

    funding_time = base_time + timedelta(minutes=minutes_from_start)
    return FundingEvent(
        funding_time=int(funding_time.timestamp() * 1000),
        funding_rate=rate,
    )


def test_event_funding_only_charged_when_crossing_event_with_inventory():
    from datetime import datetime, timedelta, timezone
    from core.models import GridParams, OrderSide
    from strategy.backtest import BacktestConfig, run_grid_backtest

    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    # 构造能成交并留下多头库存的下跌行情。
    params = GridParams(
        symbol="BTCUSDT",
        upper=105.0,
        lower=95.0,
        center=100.0,
        grid_num=4,
        step_pct=0.02,
        grid_prices=[95.0, 97.5, 100.0, 102.5, 105.0],
        baseline_atr=1.0,
        stop_loss_price=90.0,
        calculated_at=base,
    )
    klines = []
    for index in range(6):
        close = 100.0 - index  # 持续下探，买单逐格成交，产生多头库存
        klines.append(
            {
                "open_time": int((base + timedelta(minutes=index)).timestamp() * 1000),
                "close_time": int((base + timedelta(minutes=index, seconds=59)).timestamp() * 1000),
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
            }
        )
    config = BacktestConfig(fill_model="L0_CONSERVATIVE", maker_fill_probability=1.0)

    # funding 事件落在第 3 分钟，此时已有多头库存 → 扣费。
    with_event = run_grid_backtest(
        params,
        klines,
        current_price=100.0,
        config=config,
        funding_events=[_funding_event(3, 0.001, base)],
    )
    # funding 事件落在库存产生之前的第 0 分钟 → 不扣费。
    before_inventory = run_grid_backtest(
        params,
        klines,
        current_price=100.0,
        config=config,
        funding_events=[_funding_event(0, 0.001, base)],
    )

    assert with_event.funding_paid > 0
    assert before_inventory.funding_paid == 0.0


def test_event_funding_ignores_rate_per_bar_fallback():
    from datetime import datetime, timedelta, timezone
    from core.models import GridParams
    from strategy.backtest import BacktestConfig, run_grid_backtest

    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    params = GridParams(
        symbol="BTCUSDT",
        upper=105.0,
        lower=95.0,
        center=100.0,
        grid_num=4,
        step_pct=0.02,
        grid_prices=[95.0, 97.5, 100.0, 102.5, 105.0],
        baseline_atr=1.0,
        stop_loss_price=90.0,
        calculated_at=base,
    )
    klines = []
    for index in range(4):
        close = 100.0 - index
        klines.append(
            {
                "open_time": int((base + timedelta(minutes=index)).timestamp() * 1000),
                "close_time": int((base + timedelta(minutes=index, seconds=59)).timestamp() * 1000),
                "high": close + 0.5,
                "low": close - 0.5,
                "close": close,
            }
        )
    # 提供空的 funding 事件列表：既不按事件扣，也不回退到 per-bar。
    config = BacktestConfig(
        fill_model="L0_CONSERVATIVE",
        maker_fill_probability=1.0,
        funding_rate_per_bar=0.001,
    )
    result = run_grid_backtest(
        params,
        klines,
        current_price=100.0,
        config=config,
        funding_events=[],
    )
    assert result.funding_paid == 0.0


def test_slice_funding_events_partitions_contiguous_segments_without_double_count():
    from datetime import datetime, timedelta, timezone
    from strategy.backtest import slice_funding_events_for_klines

    base = datetime(2026, 3, 1, tzinfo=timezone.utc)

    def _kline(minute: int) -> dict:
        return {
            "open_time": int((base + timedelta(minutes=minute)).timestamp() * 1000),
            "close_time": int((base + timedelta(minutes=minute, seconds=59)).timestamp() * 1000),
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
        }

    # 事件分别落在第 0、3、6、9 分钟。
    events = [_funding_event(m, 0.001, base) for m in (0, 3, 6, 9)]

    # 观察期 = 前 4 根（0..3），回测区间 = 第 4..9 分钟。
    observe = [_kline(m) for m in range(4)]
    backtest = [_kline(m) for m in range(4, 10)]

    # 观察期开盘之前不应误纳早于区间的事件到第一根 Bar；只保留区间内的 6、9。
    sliced_backtest = slice_funding_events_for_klines(events, backtest)
    assert [event.funding_time for event in sliced_backtest] == [
        events[2].funding_time,
        events[3].funding_time,
    ]

    # 相邻但不重叠的两段拼接应正好覆盖全部事件、且不重复计入边界事件。
    sliced_observe = slice_funding_events_for_klines(events, observe)
    combined = [event.funding_time for event in sliced_observe] + [
        event.funding_time for event in sliced_backtest
    ]
    assert combined == [event.funding_time for event in events]
    assert len(combined) == len(set(combined))


def test_slice_funding_events_handles_empty_inputs():
    from datetime import datetime, timezone
    from strategy.backtest import slice_funding_events_for_klines

    base = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert slice_funding_events_for_klines([], [{"open_time": 1, "close_time": 2}]) == []
    assert slice_funding_events_for_klines([_funding_event(0, 0.001, base)], []) == []


def test_backtest_direction_modes_seed_position_and_charge_taker_cost():
    quiet_bars = [
        {
            "open_time": minute * 60_000,
            "close_time": minute * 60_000 + 59_999,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
        }
        for minute in range(2)
    ]

    neutral = run_grid_backtest(
        _params(),
        quiet_bars,
        current_price=100.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            direction_mode=GridDirectionMode.NEUTRAL,
            taker_fee_rate=0.0005,
            seed_slippage_bps=10,
        ),
    )
    long_result = run_grid_backtest(
        _params(),
        quiet_bars,
        current_price=100.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            direction_mode=GridDirectionMode.LONG,
            taker_fee_rate=0.0005,
            seed_slippage_bps=10,
        ),
    )
    short_result = run_grid_backtest(
        _params(),
        quiet_bars,
        current_price=100.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            direction_mode=GridDirectionMode.SHORT,
            taker_fee_rate=0.0005,
            seed_slippage_bps=10,
        ),
    )

    assert neutral.seed_qty == 0
    assert neutral.fills == []
    assert long_result.direction_mode == "LONG"
    assert long_result.seed_qty > 0
    assert long_result.seed_entry_price == 100.1
    assert long_result.seed_fee > 0
    assert long_result.net_position_qty == long_result.seed_qty
    assert long_result.fills[0].order_intent == "SEED"
    assert long_result.fills[0].position_side == "LONG"
    assert short_result.direction_mode == "SHORT"
    assert short_result.seed_qty > 0
    assert short_result.seed_entry_price == 99.9
    assert short_result.seed_fee > 0
    assert short_result.net_position_qty == -short_result.seed_qty
    assert short_result.fills[0].order_intent == "SEED"
    assert short_result.fills[0].position_side == "SHORT"


def test_backtest_enters_defensive_after_three_unique_bars_without_force_close():
    rows = [
        {
            "open_time": minute * 60_000,
            "close_time": minute * 60_000 + 59_999,
            "high": 100.2,
            "low": 99.8,
            "close": 100.0,
            "regime_score": score,
        }
        for minute, score in enumerate((60, 60, 60, 60, 70))
    ]

    result = run_grid_backtest(
        _params(),
        rows,
        current_price=100.0,
        config=BacktestConfig(
            capital=202,
            leverage=1,
            retention_score_threshold=65,
            retention_soft_breach_limit=3,
        ),
    )

    assert result.defensive_entry_count == 1
    assert result.stopped_reason is None
    assert result.net_position_qty == 0
    assert result.open_order_count == 2
