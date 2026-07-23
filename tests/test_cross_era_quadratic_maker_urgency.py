from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.cross_era_quadratic_maker_urgency import (
    CANDIDATE_EXPONENT,
    CANDIDATE_ID,
    PROTOCOL_SHA256,
    REFERENCE_EXPONENT,
    REFERENCE_VARIANT_ID,
    WIND_DOWN_BARS,
    _maker_policy_for_variant,
    _verify_worker_cache,
)
from scripts.robustness import WindDownMakerPolicy


def _cache_key(symbol: str, window_id: str, exponent: float) -> tuple[object, ...]:
    return (
        "parameter",
        symbol,
        window_id,
        0.65,
        1440,
        120.0,
        5,
        1.10,
        1.0,
        2,
        0.5,
        0.0002,
        0.0005,
        10.0,
        exponent,
        17,
    )


def test_round11_protocol_and_candidate_are_frozen() -> None:
    protocol = (
        Path(__file__).resolve().parents[1]
        / "reports/cross-era-oos/round11-quadratic-maker-urgency-protocol.md"
    )

    assert REFERENCE_VARIANT_ID == "W1440_LINEAR_E1"
    assert CANDIDATE_ID == "W1440_QUADRATIC_E2"
    assert WIND_DOWN_BARS == 1440
    assert REFERENCE_EXPONENT == pytest.approx(1.0)
    assert CANDIDATE_EXPONENT == pytest.approx(2.0)
    assert hashlib.sha256(protocol.read_bytes()).hexdigest() == PROTOCOL_SHA256


def test_round11_maker_policy_override_changes_only_exponent() -> None:
    base = WindDownMakerPolicy(5, 1.10, 1.0)

    reference = _maker_policy_for_variant(base, REFERENCE_VARIANT_ID)
    candidate = _maker_policy_for_variant(base, CANDIDATE_ID)

    assert reference == base
    assert candidate.reprice_interval_bars == base.reprice_interval_bars
    assert candidate.initial_offset_steps == pytest.approx(base.initial_offset_steps)
    assert candidate.unwind_fraction == pytest.approx(base.unwind_fraction)
    assert candidate.urgency_exponent == pytest.approx(2.0)


def test_round11_worker_cache_verifies_policy_and_both_symbols() -> None:
    cache = {
        _cache_key(symbol, window_id, 2.0): object()
        for window_id in ("dev", "val")
        for symbol in ("BTCUSDT", "ETHUSDT")
    }
    research = SimpleNamespace(_cache=cache)

    observed = _verify_worker_cache(
        research,
        allowed_window_ids={"dev", "val"},
        expected_exponent=2.0,
    )

    assert observed["window_count"] == 2
    assert observed["symbol_window_count"] == 4
    assert observed["wind_down_bars"] == 1440
    assert observed["urgency_exponent"] == pytest.approx(2.0)
    assert observed["passed"] is True


def test_round11_worker_cache_rejects_final_oos_access() -> None:
    research = SimpleNamespace(
        _cache={_cache_key("BTCUSDT", "final", 2.0): object()},
    )

    with pytest.raises(RuntimeError, match="Development/Validation"):
        _verify_worker_cache(
            research,
            allowed_window_ids={"dev", "val"},
            expected_exponent=2.0,
        )


def test_round11_worker_cache_rejects_wrong_exponent() -> None:
    cache = {
        _cache_key(symbol, "dev", 1.0): object()
        for symbol in ("BTCUSDT", "ETHUSDT")
    }
    research = SimpleNamespace(_cache=cache)

    with pytest.raises(RuntimeError, match="紧迫度指数"):
        _verify_worker_cache(
            research,
            allowed_window_ids={"dev"},
            expected_exponent=2.0,
        )
