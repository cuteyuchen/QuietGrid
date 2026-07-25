from __future__ import annotations

from strategy.grid_viability import GridViabilityConfig, evaluate_grid_viability


def _active_rows(count: int = 60) -> list[dict[str, float | int]]:
    rows = []
    for index in range(count):
        center = 100.0 + (0.08 if index % 2 else -0.08)
        rows.append(
            {
                "open": center,
                "high": 100.25,
                "low": 99.75,
                "close": center,
                "volume": 10.0,
                "quote_volume": 100_000.0,
                "trade_count": 100,
            }
        )
    return rows


def test_active_zero_maker_path_passes_viability_gate() -> None:
    decision = evaluate_grid_viability(
        _active_rows(),
        grid_prices=[99.5, 99.7, 99.9, 100.1, 100.3],
        step_pct=0.001,
        spread_pct=0.0002,
        maker_fee_rate=0.0,
        config=GridViabilityConfig(
            min_crossings_per_hour=1.0,
            min_reversal_ratio=0.25,
            max_zero_activity_ratio=0.10,
            min_trade_count_per_hour=1_000,
            min_quote_volume_per_hour=1_000_000,
            min_net_capacity_per_hour=0.0005,
        ),
    )
    assert decision.allowed is True
    assert decision.snapshot.crossings_per_hour > 1
    assert decision.snapshot.net_edge_pct == 0.001
    assert decision.snapshot.zero_activity_ratio == 0


def test_quiet_but_inactive_path_is_blocked() -> None:
    rows = [{"open":100.0,"high":100.01,"low":99.99,"close":100.0,"volume":0.0,"quote_volume":0.0,"trade_count":0} for _ in range(60)]
    decision = evaluate_grid_viability(rows, grid_prices=[99.5,99.7,99.9,100.1,100.3], step_pct=0.001, spread_pct=0.0002, maker_fee_rate=0.0, config=GridViabilityConfig())
    assert decision.allowed is False
    assert decision.snapshot.zero_activity_ratio == 1.0
    assert any("零成交" in reason for reason in decision.reasons)
    assert any("穿越" in reason for reason in decision.reasons)


def test_spread_must_be_small_relative_to_dense_step() -> None:
    decision = evaluate_grid_viability(_active_rows(), grid_prices=[99.5,99.7,99.9,100.1,100.3], step_pct=0.0006, spread_pct=0.0005, maker_fee_rate=0.0, config=GridViabilityConfig(max_spread_to_step_ratio=0.5))
    assert decision.allowed is False
    assert any("点差占格距" in reason for reason in decision.reasons)
