from __future__ import annotations

from scripts.cross_era_long_horizon_regime import (
    _apply_long_filter,
    _calibrate_candidates,
    _directional_efficiency,
    _select_candidate,
)
from scripts.robustness import WindowResult


def test_directional_efficiency_distinguishes_trend_from_reversal() -> None:
    assert _directional_efficiency((0.01, 0.01, 0.01)) == 1.0
    assert _directional_efficiency((0.01, -0.01, 0.01, -0.01)) == 0.0


def test_calibrate_candidates_uses_frozen_empirical_quantiles() -> None:
    development_ids = tuple(f"w{index:03d}" for index in range(100))
    features = {
        symbol: {
            window_id: {
                1440: (index + 1) / 1000,
                4320: (index + 1) / 2000,
                10080: (index + 1) / 4000,
            }
            for index, window_id in enumerate(development_ids)
        }
        for symbol in ("BTCUSDT", "ETHUSDT")
    }

    candidates = _calibrate_candidates(features, development_ids)

    assert len(candidates) == 12
    first = candidates[0]
    assert first["candidate_id"] == "LONG_DE_L1440_Q60"
    assert first["thresholds"]["BTCUSDT"] == 0.06
    assert first["thresholds"]["ETHUSDT"] == 0.06


def test_apply_long_filter_blocks_unavailable_or_excessive_feature() -> None:
    baseline = WindowResult(
        parameter_id="p",
        symbol="BTCUSDT",
        window_id="w",
        market_close="2026-01-01T00:00:00+00:00",
        status="TRADED",
        reason="",
        pnl=3.0,
        fees_paid=1.0,
        fill_count=2,
    )

    kept = _apply_long_filter(
        baseline,
        feature=0.10,
        threshold=0.20,
        lookback=1440,
    )
    blocked = _apply_long_filter(
        baseline,
        feature=0.30,
        threshold=0.20,
        lookback=1440,
    )
    unavailable = _apply_long_filter(
        baseline,
        feature=None,
        threshold=0.20,
        lookback=1440,
    )

    assert kept is baseline
    assert blocked.status == "BLOCKED"
    assert blocked.pnl == 0.0
    assert blocked.fees_paid == 0.0
    assert blocked.fill_count == 0
    assert "0.300000 > 0.200000" in blocked.reason
    assert unavailable.status == "BLOCKED"
    assert "HISTORY_UNAVAILABLE" in unavailable.reason


def test_select_candidate_uses_preregistered_tie_break_order() -> None:
    candidates = [
        {
            "candidate_id": "short",
            "lookback": 1440,
            "quantile": 0.9,
            "selection": {
                "all_cells_passed": True,
                "minimum_worst_seed_total_pnl": 1.0,
                "minimum_trade_coverage": 0.4,
            },
        },
        {
            "candidate_id": "long",
            "lookback": 10080,
            "quantile": 0.8,
            "selection": {
                "all_cells_passed": True,
                "minimum_worst_seed_total_pnl": 1.0,
                "minimum_trade_coverage": 0.4,
            },
        },
    ]

    assert _select_candidate(candidates) == "long"
    assert _select_candidate(
        [{**candidates[0], "selection": {**candidates[0]["selection"], "all_cells_passed": False}}]
    ) is None
