from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.models import GridDirectionMode
from scripts.cross_era_symbol_specific_maker_offset import (
    REFERENCE_CANDIDATE_ID,
    _candidate_spec,
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


def test_round9_candidates_match_protocol() -> None:
    assert _registered_candidates() == {
        "SMO_ETH120": {
            "BTCUSDT": {
                "wind_down_bars": 2880,
                "wind_down_initial_offset_steps": 1.10,
            },
            "ETHUSDT": {
                "wind_down_bars": 1440,
                "wind_down_initial_offset_steps": 1.20,
            },
        },
        "SMO_ETH130": {
            "BTCUSDT": {
                "wind_down_bars": 2880,
                "wind_down_initial_offset_steps": 1.10,
            },
            "ETHUSDT": {
                "wind_down_bars": 1440,
                "wind_down_initial_offset_steps": 1.30,
            },
        },
    }
    assert _candidate_spec(REFERENCE_CANDIDATE_ID)["ETHUSDT"] == {
        "wind_down_bars": 1440,
        "wind_down_initial_offset_steps": 1.10,
    }


def test_round9_symbol_policy_override_is_surgical() -> None:
    original = _policies()

    candidate = _symbol_policies_for_candidate(original, "SMO_ETH120")

    assert original["BTCUSDT"].wind_down_bars is None
    assert original["BTCUSDT"].wind_down_initial_offset_steps is None
    assert original["ETHUSDT"].wind_down_bars is None
    assert original["ETHUSDT"].wind_down_initial_offset_steps is None
    assert candidate["BTCUSDT"].wind_down_bars == 2880
    assert candidate["BTCUSDT"].wind_down_initial_offset_steps == pytest.approx(1.10)
    assert candidate["ETHUSDT"].wind_down_bars == 1440
    assert candidate["ETHUSDT"].wind_down_initial_offset_steps == pytest.approx(1.20)
    assert candidate["BTCUSDT"].parameter is original["BTCUSDT"].parameter
    assert candidate["ETHUSDT"].parameter is original["ETHUSDT"].parameter


def test_round9_cells_exclude_final_oos() -> None:
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


def test_round9_worker_cache_confirms_symbol_overrides() -> None:
    research = SimpleNamespace(
        _cache={
            ("btc", "BTCUSDT", "dev", 0.65, 2880, 200.0, 5, 1.10): object(),
            ("btc", "BTCUSDT", "val", 0.65, 2880, 200.0, 5, 1.10): object(),
            ("eth", "ETHUSDT", "dev", 0.65, 1440, 120.0, 5, 1.20): object(),
            ("eth", "ETHUSDT", "val", 0.65, 1440, 120.0, 5, 1.20): object(),
        }
    )

    observed = _verify_worker_cache(
        research,
        "SMO_ETH120",
        {"dev", "val"},
    )

    assert observed == {
        "BTCUSDT": {
            "wind_down_bars": 2880,
            "wind_down_initial_offset_steps": 1.10,
        },
        "ETHUSDT": {
            "wind_down_bars": 1440,
            "wind_down_initial_offset_steps": 1.20,
        },
    }


def test_round9_worker_cache_rejects_final_oos_access() -> None:
    research = SimpleNamespace(
        _cache={
            ("btc", "BTCUSDT", "final", 0.65, 2880, 200.0, 5, 1.10): object(),
            ("eth", "ETHUSDT", "dev", 0.65, 1440, 120.0, 5, 1.20): object(),
        }
    )

    with pytest.raises(RuntimeError, match="Development/Validation"):
        _verify_worker_cache(
            research,
            "SMO_ETH120",
            {"dev", "val"},
        )
