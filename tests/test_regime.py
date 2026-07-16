from __future__ import annotations

from datetime import datetime, timezone

import pytest

from strategy.regime import RegimeConfig, RegimeEngine


def _range_klines(count: int = 90) -> list[dict[str, float]]:
    rows = []
    for index in range(count):
        close = 100.0 + (0.08 if index % 2 == 0 else -0.08)
        rows.append({"high": close + 0.06, "low": close - 0.06, "close": close})
    return rows


def _trend_klines(count: int = 90) -> list[dict[str, float]]:
    rows = []
    for index in range(count):
        close = 100.0 + index * 0.08
        rows.append({"high": close + 0.03, "low": close - 0.03, "close": close})
    return rows


def test_quiet_range_is_allowed_with_explainable_scores() -> None:
    decision = RegimeEngine().evaluate(
        "BTCUSDT",
        _range_klines(),
        spread_pct=0.0001,
        depth_usdt=20_000,
        expected_step_pct=0.003,
        cost_floor_pct=0.001,
        as_of=datetime.now(timezone.utc),
    )

    assert decision.allowed is True
    assert decision.state == "QUIET_RANGE"
    assert decision.grid_score >= 75
    assert set(decision.component_scores) == {
        "volatility",
        "trend",
        "liquidity",
        "mean_reversion",
        "cost",
        "event",
    }
    assert decision.hard_blocks == ()


def test_low_volatility_trend_is_not_misclassified_as_range() -> None:
    decision = RegimeEngine().evaluate(
        "BTCUSDT",
        _trend_klines(),
        spread_pct=0.0001,
        depth_usdt=20_000,
        expected_step_pct=0.003,
    )

    assert decision.allowed is False
    assert decision.state == "TREND_UP"
    assert decision.features.directional_efficiency > 0.9


@pytest.mark.parametrize(
    ("kwargs", "expected_state"),
    [
        ({"spread_pct": 0.002, "depth_usdt": 20_000}, "ILLIQUID"),
        ({"spread_pct": 0.0001, "depth_usdt": 100}, "ILLIQUID"),
        ({"spread_pct": 0.0001, "depth_usdt": 20_000, "data_age_seconds": 120}, "UNKNOWN"),
        ({"spread_pct": 0.0001, "depth_usdt": 20_000, "event_risk": True}, "EVENT_RISK"),
    ],
)
def test_hard_blocks_override_score(kwargs: dict[str, float], expected_state: str) -> None:
    decision = RegimeEngine().evaluate("BTCUSDT", _range_klines(), **kwargs)

    assert decision.allowed is False
    assert decision.state == expected_state
    assert decision.hard_blocks


def test_hysteresis_uses_lower_stay_threshold_for_running_session() -> None:
    config = RegimeConfig(enter_threshold=95, stay_threshold=80)
    engine = RegimeEngine(config)

    entry = engine.evaluate(
        "BTCUSDT",
        _range_klines(),
        spread_pct=0.0001,
        depth_usdt=20_000,
        expected_step_pct=0.003,
        cost_floor_pct=0.001,
    )
    running = engine.evaluate(
        "BTCUSDT",
        _range_klines(),
        spread_pct=0.0001,
        depth_usdt=20_000,
        expected_step_pct=0.003,
        cost_floor_pct=0.001,
        running=True,
    )

    assert entry.allowed is False
    assert running.allowed is True


def test_insufficient_samples_fail_closed() -> None:
    with pytest.raises(ValueError, match="样本不足"):
        RegimeEngine().evaluate(
            "BTCUSDT",
            _range_klines(10),
            spread_pct=0.0001,
            depth_usdt=20_000,
        )
