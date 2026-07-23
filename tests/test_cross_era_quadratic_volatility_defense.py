from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.cross_era_quadratic_volatility_defense import (
    CANDIDATE_EXPONENT,
    CANDIDATE_ID,
    P4_RESULT_SHA256,
    PROTOCOL_SHA256,
    REFERENCE_EXPONENT,
    REFERENCE_VARIANT_ID,
    ROUND5_RESULT_SHA256,
    ROUND11_RESULT_SHA256,
    VOLATILITY_BREACH_COUNT,
    VOLATILITY_EXPANSION_RATIO,
    VOLATILITY_REDUCE_FRACTION,
    WIND_DOWN_BARS,
    _variant_config_and_policy,
    _verify_variant_execution,
)
from scripts.robustness import ResearchConfig, WindDownMakerPolicy
from strategy.backtest import BacktestConfig


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


def _cache(
    exponent: float,
    *,
    window_ids: tuple[str, ...] = ("dev", "val"),
) -> dict[tuple[object, ...], object]:
    return {
        _cache_key(symbol, window_id, exponent): object()
        for window_id in window_ids
        for symbol in ("BTCUSDT", "ETHUSDT")
    }


def _candidate_config_and_policy() -> tuple[ResearchConfig, WindDownMakerPolicy]:
    return _variant_config_and_policy(
        ResearchConfig(),
        WindDownMakerPolicy(5, 1.10, 1.0),
        CANDIDATE_ID,
    )


def test_round12_protocol_sources_and_candidate_are_frozen() -> None:
    root = Path(__file__).resolve().parents[1]
    protocol = (
        root
        / "reports/cross-era-oos/round12-quadratic-volatility-defense-protocol.md"
    )
    frozen_sources = {
        root / "reports/cross-era-oos/round5-early-wind-down-results.json": (
            ROUND5_RESULT_SHA256
        ),
        root / "reports/cross-era-oos/round11-quadratic-maker-urgency-results.json": (
            ROUND11_RESULT_SHA256
        ),
        root / "reports/volatility-defense/results.json": P4_RESULT_SHA256,
    }

    assert REFERENCE_VARIANT_ID == "W1440_LINEAR_NO_VOL"
    assert CANDIDATE_ID == "Q2_V150_N10_F20"
    assert WIND_DOWN_BARS == 1440
    assert REFERENCE_EXPONENT == pytest.approx(1.0)
    assert CANDIDATE_EXPONENT == pytest.approx(2.0)
    assert VOLATILITY_EXPANSION_RATIO == pytest.approx(1.50)
    assert VOLATILITY_BREACH_COUNT == 10
    assert VOLATILITY_REDUCE_FRACTION == pytest.approx(0.20)
    assert hashlib.sha256(protocol.read_bytes()).hexdigest() == PROTOCOL_SHA256
    for path, expected_sha256 in frozen_sources.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256


def test_round12_variant_override_changes_only_registered_fields() -> None:
    base_config = ResearchConfig(
        wind_down_bars=77,
        volatility_reduce_expansion_ratio=2.5,
        volatility_reduce_after_breaches=3,
        volatility_reduce_fraction=0.80,
        volatility_reduce_mode="WORST_SIDE",
        volatility_reduce_only_when_losing=True,
        volatility_wind_down_after_reduce=True,
        volatility_resume_after_normal_bars=42,
    )
    base_policy = WindDownMakerPolicy(9, 3.0, 0.5, urgency_exponent=4.0)

    reference_config, reference_policy = _variant_config_and_policy(
        base_config,
        base_policy,
        REFERENCE_VARIANT_ID,
    )
    candidate_config, candidate_policy = _variant_config_and_policy(
        base_config,
        base_policy,
        CANDIDATE_ID,
    )

    reference_values = asdict(reference_config)
    candidate_values = asdict(candidate_config)
    assert {
        key
        for key in reference_values
        if reference_values[key] != candidate_values[key]
    } == {
        "volatility_reduce_expansion_ratio",
        "volatility_reduce_after_breaches",
    }
    assert reference_config.wind_down_bars == WIND_DOWN_BARS
    assert reference_config.volatility_reduce_expansion_ratio == pytest.approx(0.0)
    assert reference_config.volatility_reduce_after_breaches == 0
    assert candidate_config.volatility_reduce_expansion_ratio == pytest.approx(1.50)
    assert candidate_config.volatility_reduce_after_breaches == 10
    assert candidate_config.volatility_reduce_fraction == pytest.approx(0.20)
    assert candidate_config.volatility_reduce_mode == "BOTH"
    assert candidate_config.volatility_reduce_only_when_losing is False
    assert candidate_config.volatility_wind_down_after_reduce is False
    assert candidate_config.volatility_resume_after_normal_bars == 0

    reference_policy_values = asdict(reference_policy)
    candidate_policy_values = asdict(candidate_policy)
    assert {
        key
        for key in reference_policy_values
        if reference_policy_values[key] != candidate_policy_values[key]
    } == {"urgency_exponent"}
    assert reference_policy.urgency_exponent == pytest.approx(1.0)
    assert candidate_policy.urgency_exponent == pytest.approx(2.0)
    assert candidate_policy.reprice_interval_bars == base_policy.reprice_interval_bars
    assert candidate_policy.initial_offset_steps == pytest.approx(
        base_policy.initial_offset_steps
    )
    assert candidate_policy.unwind_fraction == pytest.approx(
        base_policy.unwind_fraction
    )


def test_round12_worker_execution_verifies_candidate_parameters() -> None:
    config, policy = _candidate_config_and_policy()
    research = SimpleNamespace(_cache=_cache(2.0), config=config)

    observed = _verify_variant_execution(
        research,
        allowed_window_ids={"dev", "val"},
        expected_exponent=policy.urgency_exponent,
        volatility_enabled=True,
    )

    assert observed["window_count"] == 2
    assert observed["symbol_window_count"] == 4
    assert observed["wind_down_bars"] == 1440
    assert observed["urgency_exponent"] == pytest.approx(2.0)
    assert observed["volatility_reduce_expansion_ratio"] == pytest.approx(1.50)
    assert observed["volatility_reduce_after_breaches"] == 10
    assert observed["volatility_reduce_fraction"] == pytest.approx(0.20)
    assert observed["volatility_reduce_mode"] == "BOTH"
    assert observed["volatility_enabled"] is True
    assert observed["passed"] is True


def test_round12_worker_cache_rejects_final_oos_access() -> None:
    config, policy = _candidate_config_and_policy()
    research = SimpleNamespace(
        _cache=_cache(2.0, window_ids=("dev", "final_oos")),
        config=config,
    )

    with pytest.raises(RuntimeError, match="Development/Validation"):
        _verify_variant_execution(
            research,
            allowed_window_ids={"dev", "val"},
            expected_exponent=policy.urgency_exponent,
            volatility_enabled=True,
        )


def test_round12_worker_cache_rejects_wrong_exponent() -> None:
    config, policy = _candidate_config_and_policy()
    research = SimpleNamespace(_cache=_cache(1.0), config=config)

    with pytest.raises(RuntimeError, match="紧迫度指数"):
        _verify_variant_execution(
            research,
            allowed_window_ids={"dev", "val"},
            expected_exponent=policy.urgency_exponent,
            volatility_enabled=True,
        )


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("volatility_reduce_expansion_ratio", 1.40, "波动扩张阈值"),
        ("volatility_reduce_after_breaches", 9, "波动连续次数"),
        ("volatility_reduce_fraction", 0.30, "波动减仓比例"),
        ("volatility_reduce_mode", "WORST_SIDE", "方向必须为 BOTH"),
        ("volatility_reduce_only_when_losing", True, "仅亏损时减仓"),
        ("volatility_wind_down_after_reduce", True, "冻结 OPEN"),
        ("volatility_resume_after_normal_bars", 1, "恢复冷却参数"),
    ],
)
def test_round12_worker_execution_rejects_unregistered_volatility_parameters(
    field: str,
    invalid_value: object,
    message: str,
) -> None:
    config, policy = _candidate_config_and_policy()
    research = SimpleNamespace(
        _cache=_cache(2.0),
        config=replace(config, **{field: invalid_value}),
    )

    with pytest.raises(RuntimeError, match=message):
        _verify_variant_execution(
            research,
            allowed_window_ids={"dev", "val"},
            expected_exponent=policy.urgency_exponent,
            volatility_enabled=True,
        )


def test_round12_reference_and_production_defaults_keep_volatility_disabled() -> None:
    config, policy = _variant_config_and_policy(
        ResearchConfig(),
        WindDownMakerPolicy(5, 1.10, 1.0),
        REFERENCE_VARIANT_ID,
    )
    research = SimpleNamespace(_cache=_cache(1.0), config=config)

    observed = _verify_variant_execution(
        research,
        allowed_window_ids={"dev", "val"},
        expected_exponent=policy.urgency_exponent,
        volatility_enabled=False,
    )
    defaults = BacktestConfig()

    assert observed["urgency_exponent"] == pytest.approx(1.0)
    assert observed["volatility_reduce_expansion_ratio"] == pytest.approx(0.0)
    assert observed["volatility_reduce_after_breaches"] == 0
    assert observed["volatility_enabled"] is False
    assert defaults.wind_down_urgency_exponent == pytest.approx(1.0)
    assert defaults.volatility_reduce_expansion_ratio == pytest.approx(0.0)
    assert defaults.volatility_reduce_after_breaches == 0


def test_round12_rejects_unknown_variant() -> None:
    with pytest.raises(ValueError, match="未知 Round 12 变体"):
        _variant_config_and_policy(
            ResearchConfig(),
            WindDownMakerPolicy(5, 1.10, 1.0),
            "UNREGISTERED",
        )
