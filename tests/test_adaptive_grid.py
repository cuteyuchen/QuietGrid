from __future__ import annotations

import pytest

from strategy.adaptive_grid import AdaptiveGridConfig, AdaptiveGridGenerator
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
    assert 6 <= params.grid_num <= 20
    assert len(params.grid_prices) == params.grid_num + 1
    assert len(params.qty_weights) == len(params.grid_prices)
    assert sum(params.qty_weights) == pytest.approx(1.0)
    assert params.qty_weights[0] < params.qty_weights[len(params.qty_weights) // 2]
    assert params.step_pct >= params.cost_floor_pct
    assert params.upper_stop_loss_price is not None


def test_cost_floor_rejects_grid_without_net_edge() -> None:
    generator = AdaptiveGridGenerator(
        AdaptiveGridConfig(
            min_step_pct=0.001,
            max_step_pct=0.002,
            min_grid_num=20,
            max_grid_num=20,
            adverse_selection_buffer_pct=0.002,
            slippage_buffer_pct=0.002,
            safety_margin_pct=0.002,
        )
    )

    with pytest.raises(GridCalculationError, match="成本地板"):
        generator.generate(
            "BTCUSDT",
            _klines(),
            current_price=99.97,
            funding_rate=0,
            maker_fee_rate=0,
            regime_score=90,
        )


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
