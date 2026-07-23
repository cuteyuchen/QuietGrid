from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.models import GridDirectionMode
from scripts.cross_era_symbol_specific_wind_down import (
    REFERENCE_CANDIDATE_ID,
    _candidate_wind_downs,
    _cell_specs,
    _registered_candidates,
    _symbol_policies_for_candidate,
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


def test_round8_candidates_match_protocol() -> None:
    assert _registered_candidates() == {
        "SW_BTC2160_ETH1440": {"BTCUSDT": 2160, "ETHUSDT": 1440},
        "SW_BTC2880_ETH1440": {"BTCUSDT": 2880, "ETHUSDT": 1440},
    }
    assert _candidate_wind_downs(REFERENCE_CANDIDATE_ID) == {
        "BTCUSDT": 1440,
        "ETHUSDT": 1440,
    }


def test_round8_symbol_policy_override_is_surgical() -> None:
    original = _policies()

    candidate = _symbol_policies_for_candidate(
        original,
        "SW_BTC2160_ETH1440",
    )

    assert original["BTCUSDT"].wind_down_bars is None
    assert original["ETHUSDT"].wind_down_bars is None
    assert candidate["BTCUSDT"].wind_down_bars == 2160
    assert candidate["ETHUSDT"].wind_down_bars == 1440
    assert candidate["BTCUSDT"].parameter is original["BTCUSDT"].parameter
    assert candidate["ETHUSDT"].parameter is original["ETHUSDT"].parameter


def test_round8_cells_exclude_final_oos() -> None:
    split = SimpleNamespace(
        development=("dev",),
        validation=("val",),
        final_oos=("final",),
    )

    cells = _cell_specs(split)

    assert {window_id for _, _, ids, _ in cells for window_id in ids} == {
        "dev",
        "val",
    }


def test_round8_worker_cache_confirms_symbol_overrides() -> None:
    research = SimpleNamespace(
        _cache={
            ("btc", "BTCUSDT", "dev", 0.65, 2160): object(),
            ("btc", "BTCUSDT", "val", 0.65, 2160): object(),
            ("eth", "ETHUSDT", "dev", 0.65, 1440): object(),
            ("eth", "ETHUSDT", "val", 0.65, 1440): object(),
        }
    )

    observed = _verify_worker_cache(
        research,
        "SW_BTC2160_ETH1440",
        {"dev", "val"},
    )

    assert observed == {"BTCUSDT": 2160, "ETHUSDT": 1440}


def test_round8_worker_cache_rejects_final_oos_access() -> None:
    research = SimpleNamespace(
        _cache={
            ("btc", "BTCUSDT", "final", 0.65, 2160): object(),
            ("eth", "ETHUSDT", "dev", 0.65, 1440): object(),
        }
    )

    with pytest.raises(RuntimeError, match="Development/Validation"):
        _verify_worker_cache(
            research,
            "SW_BTC2160_ETH1440",
            {"dev", "val"},
        )
