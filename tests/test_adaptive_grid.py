from __future__ import annotations

import pytest

from core.models import GridDirectionMode
from strategy.adaptive_grid import AdaptiveGridConfig, AdaptiveGridGenerator, GridEconomicsError
from strategy.grid_calculator import GridCalculationError


def _klines(count: int = 90) -> list[dict[str, float]]:
    rows = []
    for index in range(count):
        close = 100.0 + ((index % 10) - 5) * 0.03
        rows.append({"high": close + 0.08, "low": close - 0.08, "close": close})
    return rows


def test_adaptive_grid_derives_count_spacing_and_weights() -> None:
    params = AdaptiveGridGenerator().generate(
        "BTCUSDT",
        _klines(),
        current_price=99.97,
        funding_rate=0,
        maker_fee_rate=0,
        regime_score=90,
    )

    assert params.grid_mode == "adaptive_v2"
    assert 3 <= params.grid_num <= 20
    assert len(params.grid_prices) == params.grid_num + 1
    assert len(params.qty_weights) == len(params.grid_prices)
    assert sum(params.qty_weights) == pytest.approx(1.0)
    assert params.qty_weights[0] < params.qty_weights[len(params.qty_weights) // 2]
    assert params.step_pct >= params.cost_floor_pct
    assert params.economics["fee_net_edge_pct"] > 0
    assert params.economics["level_count"] == params.grid_num + 1
    assert params.economics["evaluated_candidates"] > 0
    assert params.upper_stop_loss_price is not None


def test_hard_cost_rejects_grid_without_fee_net_edge() -> None:
    generator = AdaptiveGridGenerator(
        AdaptiveGridConfig(
            min_step_pct=0.001,
            max_step_pct=0.002,
            min_grid_num=20,
            max_grid_num=20,
            adverse_selection_buffer_pct=0.0,
            slippage_buffer_pct=0.0,
            safety_margin_pct=0.0,
        )
    )

    with pytest.raises(GridEconomicsError, match="手续费后净边际") as caught:
        generator.generate(
            "BTCUSDT",
            _klines(),
            current_price=99.97,
            funding_rate=0.005,
            maker_fee_rate=0.001,
            regime_score=90,
        )
    assert caught.value.economics["fee_net_edge_pct"] <= 0


def test_current_price_outside_range_is_rejected() -> None:
    with pytest.raises(GridCalculationError, match="漂移"):
        AdaptiveGridGenerator().generate(
            "BTCUSDT",
            _klines(),
            current_price=110,
            funding_rate=0,
            maker_fee_rate=0,
            regime_score=90,
        )


def test_funding_is_not_charged_without_projected_settlement() -> None:
    params = AdaptiveGridGenerator().generate(
        "BTCUSDT",
        _klines(),
        current_price=99.97,
        funding_rate=0.005,
        funding_cost_rate=0.0,
        maker_fee_rate=0.0001,
        regime_score=90,
    )

    expected_cost = 0.0002
    assert params.cost_floor_pct == pytest.approx(expected_cost)
    assert params.economics["projected_funding_pct"] == 0
    assert params.economics["risk_discount_pct"] == pytest.approx(0.0015)


@pytest.mark.parametrize("maker_fee_rate", [0.0, 0.0002])
def test_dynamic_solver_keeps_hard_cost_separate_from_risk_discount(
    maker_fee_rate: float,
) -> None:
    params = AdaptiveGridGenerator().generate(
        "BCHUSDT",
        _klines(180),
        current_price=99.97,
        funding_rate=0,
        funding_cost_rate=0,
        maker_fee_rate=maker_fee_rate,
        regime_score=78,
    )

    assert params.cost_floor_pct == pytest.approx(maker_fee_rate * 2)
    assert params.economics["hard_cost_pct"] == pytest.approx(maker_fee_rate * 2)
    assert params.economics["risk_discount_pct"] == pytest.approx(0.0015)
    assert params.economics["fee_net_edge_pct"] > 0


def test_bch_like_range_has_positive_fee_net_edge_and_dynamic_count() -> None:
    rows: list[dict[str, float]] = []
    for index in range(180):
        close = 218.0 + 1.7 * __import__("math").sin(index / 7)
        rows.append({"high": close + 0.25, "low": close - 0.25, "close": close})

    params = AdaptiveGridGenerator().generate(
        "BCHUSDT",
        rows,
        current_price=218.0,
        funding_rate=0,
        funding_cost_rate=0,
        maker_fee_rate=0.0002,
        regime_score=78,
    )

    assert 3 <= params.grid_num <= 20
    assert params.economics["fee_net_edge_pct"] > 0
    assert params.economics["estimated_crossings_per_hour"] >= 0
    assert isinstance(params.economics["objective_value"], float)


def test_dynamic_solver_uses_exchange_min_notional_before_selecting_grid() -> None:
    params = AdaptiveGridGenerator().generate(
        "BTCUSDT",
        _klines(180),
        current_price=99.97,
        funding_rate=0,
        funding_cost_rate=0,
        maker_fee_rate=0.0002,
        regime_score=78,
        capital=200,
        leverage=1,
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        min_notional=40,
    )

    assert params.grid_num == 3
    assert params.economics["planned_min_order_notional"] >= 40
    assert params.economics["minimum_order_notional"] == 40
    assert params.economics["sizing_rejected_reason"] == ""


def test_dynamic_solver_blocks_before_order_placement_when_min_notional_is_impossible() -> None:
    with pytest.raises(GridEconomicsError, match="最小名义金额") as caught:
        AdaptiveGridGenerator().generate(
            "BTCUSDT",
            _klines(180),
            current_price=99.97,
            funding_rate=0,
            funding_cost_rate=0,
            maker_fee_rate=0.0002,
            regime_score=78,
            capital=200,
            leverage=1,
            tick_size=0.01,
            step_size=0.001,
            min_qty=0.001,
            min_notional=100,
        )

    assert caught.value.economics["rejected_reason"] == "每格名义金额小于交易所最小名义金额"
    assert caught.value.economics["planned_min_order_notional"] < 100


def test_btc_500_usdt_plan_meets_testnet_minimum_notional_and_reports_required_capital() -> None:
    rows = []
    for index in range(180):
        close = 64_000.0 + ((index % 18) - 9) * 8.0
        rows.append({"high": close + 20.0, "low": close - 20.0, "close": close})

    params = AdaptiveGridGenerator().generate(
        "BTCUSDT",
        rows,
        current_price=63_992.0,
        funding_rate=0,
        funding_cost_rate=0,
        maker_fee_rate=0,
        regime_score=85,
        capital=500,
        leverage=1,
        tick_size=0.1,
        step_size=0.001,
        min_qty=0.001,
        min_notional=50,
        direction_mode=GridDirectionMode.LONG,
        taker_fee_rate=0.0005,
    )

    assert params.economics["configured_capital"] == 500
    assert params.economics["planned_min_order_notional"] >= 50
    assert 0 < params.economics["minimum_required_capital"] <= 500
    assert params.economics["direction_mode"] == "LONG"
    assert params.economics["seed_execution_cost_pct"] > 0


def test_directional_candidate_worst_case_loss_stays_within_session_budget() -> None:
    params = AdaptiveGridGenerator().generate(
        "BCHUSDT",
        _klines(180),
        current_price=99.97,
        funding_rate=0,
        funding_cost_rate=0,
        maker_fee_rate=0.0002,
        regime_score=82,
        capital=500,
        leverage=1,
        tick_size=0.01,
        step_size=0.001,
        min_qty=0.001,
        min_notional=5,
        direction_mode=GridDirectionMode.SHORT,
        risk_budget=25,
        taker_fee_rate=0.0005,
    )

    assert params.economics["worst_case_stop_loss"] <= 25
    assert params.economics["risk_budget"] == 25
