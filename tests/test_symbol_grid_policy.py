from __future__ import annotations

from datetime import datetime, timezone

import pytest

from db.database import init_db
from db.repository import Repository
from exchange.mock import MockExchangeClient
from strategy.adaptive_grid import AdaptiveGridConfig, AdaptiveGridGenerator
from strategy.controller import ControllerConfig, TradingController
from strategy.grid_calculator import GridConfig
from strategy.observer import ObserverConfig
from strategy.selector import SelectionConfig


class _WindowScheduler:
    def is_in_window(self, now_utc=None) -> bool:
        return True

    def should_force_close(self, now_utc=None) -> bool:
        return False


def _klines() -> list[dict[str, float]]:
    rows = []
    for index in range(180):
        close = 100.0 + ((index % 10) - 4.5) * 0.05
        rows.append({"high": close + 0.08, "low": close - 0.08, "close": close})
    return rows


def test_controller_symbol_grid_policy_matches_research_geometry(tmp_path) -> None:
    db_path = tmp_path / "symbol-grid-policy.db"
    init_db(db_path)
    base_config = AdaptiveGridConfig(
        k_atr_range=2.0,
        k_sigma_range=2.0,
        min_step_pct=0.0015,
    )
    controller = TradingController(
        exchange=MockExchangeClient(),
        scheduler=_WindowScheduler(),  # type: ignore[arg-type]
        repository=Repository(db_path),
        selector_config=SelectionConfig(max_concurrent=1),
        observer_config=ObserverConfig(observe_hours=3, min_samples=30),
        grid_config=GridConfig(),
        controller_config=ControllerConfig(
            capital_per_symbol=500,
            leverage=1,
            max_concurrent=1,
            take_profit_usdt=10,
            total_capital_limit=1000,
            grid_range_multiplier_by_symbol={"BTCUSDT": 1.25},
            grid_min_step_pct_by_symbol={"BTCUSDT": 0.0018},
        ),
        adaptive_grid_config=base_config,
    )

    effective = controller._adaptive_grid_for_symbol("BTCUSDT")
    expected = AdaptiveGridGenerator(
        AdaptiveGridConfig(
            k_atr_range=2.5,
            k_sigma_range=2.5,
            min_step_pct=0.0018,
        )
    )
    generated_at = datetime(2026, 7, 18, tzinfo=timezone.utc)
    inputs = {
        "current_price": 100.0,
        "funding_rate": 0.0,
        "maker_fee_rate": 0.0001,
        "regime_score": 80.0,
        "calculated_at": generated_at,
    }

    actual_params = effective.generate("BTCUSDT", _klines(), **inputs)
    expected_params = expected.generate("BTCUSDT", _klines(), **inputs)

    assert effective.config.k_atr_range == pytest.approx(2.5)
    assert effective.config.k_sigma_range == pytest.approx(2.5)
    assert effective.config.min_step_pct == pytest.approx(0.0018)
    assert actual_params.lower == pytest.approx(expected_params.lower)
    assert actual_params.upper == pytest.approx(expected_params.upper)
    assert actual_params.step_pct == pytest.approx(expected_params.step_pct)
    assert actual_params.grid_prices == pytest.approx(expected_params.grid_prices)
    assert controller._adaptive_grid_for_symbol("ETHUSDT") is controller.adaptive_grid
