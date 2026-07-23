from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.cross_era_asset_scope_audit as asset_audit
import scripts.cross_era_trailing_shadow_pnl as trailing
from scripts.cross_era_round13_diagnose import _sha256
from scripts.robustness import WindowResult


UTC = timezone.utc


def _result(symbol: str, window_id: str, pnl: float, *, status: str = "TRADED") -> WindowResult:
    return WindowResult(
        parameter_id="P",
        symbol=symbol,
        window_id=window_id,
        market_close="2020-01-01T00:00:00+00:00",
        status=status,
        reason="window_force_close" if status == "TRADED" else "blocked",
        pnl=pnl,
    )


def _raw_runs(window_ids: tuple[str, ...]) -> dict:
    runs = {"BASE": {}}
    for seed in asset_audit.DEFAULT_SEEDS:
        results = [
            _result(symbol, window_id, float(seed + index))
            for index, window_id in enumerate(window_ids)
            for symbol in asset_audit.SYMBOLS
        ]
        runs["BASE"][seed] = {"development": (None, results)}
    return runs


def test_protocol_hash_matches_frozen_file() -> None:
    assert _sha256(trailing.PROTOCOL_PATH.resolve()) == trailing.PROTOCOL_SHA256


def test_ordered_window_ids_requires_paired_symbols_and_sorts_by_time() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    windows = [
        SimpleNamespace(window_id="w2", symbol=symbol, market_close=start + timedelta(days=7))
        for symbol in asset_audit.SYMBOLS
    ] + [
        SimpleNamespace(window_id="w1", symbol=symbol, market_close=start)
        for symbol in asset_audit.SYMBOLS
    ]

    assert trailing._ordered_window_ids(windows, ("w1", "w2")) == ("w1", "w2")

    with pytest.raises(RuntimeError, match="同时覆盖"):
        trailing._ordered_window_ids(windows[:-1], ("w1", "w2"))


def test_build_shadow_means_uses_exact_registered_seed_set() -> None:
    window_ids = ("w1", "w2")
    raw_runs = _raw_runs(window_ids)

    means, audit = trailing._build_shadow_means(raw_runs, window_ids)

    expected_w1 = statistics.mean(float(seed) for seed in asset_audit.DEFAULT_SEEDS)
    expected_w2 = statistics.mean(float(seed + 1) for seed in asset_audit.DEFAULT_SEEDS)
    assert means["BASE"]["BTCUSDT"]["w1"] == pytest.approx(expected_w1)
    assert means["BASE"]["ETHUSDT"]["w2"] == pytest.approx(expected_w2)
    assert audit["BASE"]["seed_count_per_window"] == 6
    assert audit["BASE"]["complete"] is True

    del raw_runs["BASE"][asset_audit.DEFAULT_SEEDS[-1]]
    with pytest.raises(ValueError, match="种子集合不完整"):
        trailing._build_shadow_means(raw_runs, window_ids)


def test_trailing_signal_uses_only_strictly_prior_windows() -> None:
    ordered = ("d1", "d2", "v1", "v2")
    shadow_means = {
        "BASE": {
            "BTCUSDT": {"d1": 2.0, "d2": 4.0, "v1": -100.0, "v2": 8.0},
            "ETHUSDT": {"d1": -2.0, "d2": 2.0, "v1": 6.0, "v2": 10.0},
        }
    }

    signals, audit = trailing._trailing_signal_maps(shadow_means, ordered, 2)

    assert signals["BASE"]["BTCUSDT"]["d1"] is None
    assert signals["BASE"]["BTCUSDT"]["d2"] is None
    assert signals["BASE"]["BTCUSDT"]["v1"] == pytest.approx(3.0)
    assert signals["BASE"]["BTCUSDT"]["v2"] == pytest.approx(-48.0)
    assert signals["BASE"]["ETHUSDT"]["v1"] == pytest.approx(0.0)
    assert audit["BASE"]["BTCUSDT"]["first_signal_window_id"] == "v1"
    assert audit["BASE"]["BTCUSDT"]["self_reference_count"] == 0


def test_apply_trailing_filter_blocks_missing_and_nonpositive_signal() -> None:
    traded = _result("BTCUSDT", "w1", 5.0)

    unavailable = trailing._apply_trailing_filter(traded, signal=None, lookback=4)
    assert unavailable.status == "BLOCKED"
    assert unavailable.pnl == 0.0
    assert "HISTORY_UNAVAILABLE" in unavailable.reason

    nonpositive = trailing._apply_trailing_filter(traded, signal=0.0, lookback=4)
    assert nonpositive.status == "BLOCKED"
    assert "<= 0" in nonpositive.reason

    allowed = trailing._apply_trailing_filter(traded, signal=0.01, lookback=4)
    assert allowed is traded

    already_blocked = _result("BTCUSDT", "w1", 0.0, status="BLOCKED")
    assert (
        trailing._apply_trailing_filter(already_blocked, signal=1.0, lookback=4)
        is already_blocked
    )


def test_select_candidate_uses_minimax_then_coverage_then_longer_lookback() -> None:
    candidates = [
        {
            "candidate_id": "K4",
            "lookback_windows": 4,
            "selection": {
                "all_cells_passed": True,
                "minimum_worst_seed_total_pnl": 1.0,
                "minimum_trade_coverage": 0.4,
            },
        },
        {
            "candidate_id": "K13",
            "lookback_windows": 13,
            "selection": {
                "all_cells_passed": True,
                "minimum_worst_seed_total_pnl": 1.0,
                "minimum_trade_coverage": 0.4,
            },
        },
    ]

    assert trailing._select_candidate(candidates) == "K13"
