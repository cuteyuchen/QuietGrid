from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.models import GridDirectionMode
from scripts.cross_era_duration_adaptive_wind_down import (
    ANCHOR_WIND_DOWN_BARS,
    CANDIDATE_ID,
    MAX_WIND_DOWN_BARS,
    MIN_WIND_DOWN_BARS,
    REFERENCE_TRADABLE_ROWS,
    _candidate_symbol_policies,
    _duration_summary,
    _resolved_wind_down_bars,
    _verify_worker_cache,
)
from scripts.robustness import ParameterSet, SymbolResearchPolicy


def _policies() -> dict[str, SymbolResearchPolicy]:
    return {
        "BTCUSDT": SymbolResearchPolicy(
            ParameterSet(1.25, 0.0018, 0.02, GridDirectionMode.NEUTRAL),
            200.0,
        ),
        "ETHUSDT": SymbolResearchPolicy(
            ParameterSet(1.00, 0.0018, 0.02, GridDirectionMode.NEUTRAL),
            120.0,
        ),
    }


def test_round10_schedule_matches_protocol() -> None:
    assert CANDIDATE_ID == "DAW_1440_2160_2880"
    assert ANCHOR_WIND_DOWN_BARS == 2160
    assert REFERENCE_TRADABLE_ROWS == 3300
    assert MIN_WIND_DOWN_BARS == 1440
    assert MAX_WIND_DOWN_BARS == 2880
    assert _resolved_wind_down_bars(3300) == 2160
    assert _resolved_wind_down_bars(4740) == 2880
    assert _resolved_wind_down_bars(1860) == 1440


def test_round10_symbol_policy_override_is_surgical() -> None:
    original = _policies()

    candidate = _candidate_symbol_policies(original)

    for symbol in original:
        assert original[symbol].wind_down_bars is None
        assert original[symbol].wind_down_reference_tradable_rows is None
        assert original[symbol].wind_down_min_bars is None
        assert original[symbol].wind_down_max_bars is None
        assert candidate[symbol].parameter is original[symbol].parameter
        assert candidate[symbol].wind_down_bars == 2160
        assert candidate[symbol].wind_down_reference_tradable_rows == 3300
        assert candidate[symbol].wind_down_min_bars == 1440
        assert candidate[symbol].wind_down_max_bars == 2880


def test_round10_worker_cache_verifies_formula_and_both_symbols() -> None:
    contexts = []
    cache = {}
    for window_id, rows in (("dev", 3300), ("val", 4740)):
        actual = _resolved_wind_down_bars(rows)
        for symbol in ("BTCUSDT", "ETHUSDT"):
            contexts.append(SimpleNamespace(window=SimpleNamespace(
                window_id=window_id,
                symbol=symbol,
                tradable_rows=rows,
            )))
            cache[("parameter", symbol, window_id, 0.65, actual)] = object()
    research = SimpleNamespace(contexts=contexts, _cache=cache)

    observed = _verify_worker_cache(
        research,
        allowed_window_ids={"dev", "val"},
        adaptive=True,
    )

    assert observed["window_count"] == 2
    assert observed["symbol_window_count"] == 4
    assert observed["wind_down_distribution"] == {"2160": 1, "2880": 1}
    assert observed["passed"] is True


def test_round10_worker_cache_rejects_final_oos_access() -> None:
    research = SimpleNamespace(
        contexts=[SimpleNamespace(window=SimpleNamespace(
            window_id="final",
            symbol="BTCUSDT",
            tradable_rows=3300,
        ))],
        _cache={("parameter", "BTCUSDT", "final", 0.65, 2160): object()},
    )

    with pytest.raises(RuntimeError, match="Development/Validation"):
        _verify_worker_cache(
            research,
            allowed_window_ids={"dev", "val"},
            adaptive=True,
        )


def test_round10_duration_summary_ignores_unrequested_final_window() -> None:
    windows = [
        SimpleNamespace(window_id="dev", symbol="BTCUSDT", tradable_rows=3300),
        SimpleNamespace(window_id="dev", symbol="ETHUSDT", tradable_rows=3300),
        SimpleNamespace(window_id="final", symbol="BTCUSDT", tradable_rows=9999),
        SimpleNamespace(window_id="final", symbol="ETHUSDT", tradable_rows=9999),
    ]

    summary = _duration_summary(windows, ("dev",))

    assert summary["window_count"] == 1
    assert summary["minimum_tradable_rows"] == 3300
    assert summary["maximum_tradable_rows"] == 3300
    assert summary["resolved_wind_down_distribution"] == {"2160": 1}
