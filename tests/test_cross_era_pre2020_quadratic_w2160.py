from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.cross_era_pre2020_quadratic_w2160 import (
    CANDIDATE_EXPONENT,
    CANDIDATE_ID,
    CANDIDATE_WIND_DOWN_BARS,
    EXPECTED_READY_WINDOW_COUNT,
    MANIFEST_SHA256,
    PROTOCOL_SHA256,
    REFERENCE_EXPONENT,
    REFERENCE_VARIANT_ID,
    REFERENCE_WIND_DOWN_BARS,
    ROUND5_RESULT_SHA256,
    ROUND11_RESULT_SHA256,
    ROUND12_RESULT_SHA256,
    _paired_ready_window_ids,
    _variant_config_and_policy,
    _verify_worker_cache,
)
from scripts.robustness import ResearchConfig, WindDownMakerPolicy


UTC = timezone.utc


def _cache_key(
    symbol: str,
    window_id: str,
    wind_down_bars: int,
    exponent: float,
) -> tuple[object, ...]:
    return (
        "parameter",
        symbol,
        window_id,
        0.65,
        wind_down_bars,
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
    wind_down_bars: int,
    exponent: float,
    *,
    window_ids: tuple[str, ...] = ("ext1", "ext2"),
) -> dict[tuple[object, ...], object]:
    return {
        _cache_key(symbol, window_id, wind_down_bars, exponent): object()
        for window_id in window_ids
        for symbol in ("BTCUSDT", "ETHUSDT")
    }


def _windows(count: int = EXPECTED_READY_WINDOW_COUNT) -> list[SimpleNamespace]:
    start = datetime(2020, 1, 3, 21, tzinfo=UTC)
    values = []
    for index in range(count):
        for symbol in ("BTCUSDT", "ETHUSDT"):
            values.append(
                SimpleNamespace(
                    status="READY",
                    window_id=f"ext{index:02d}",
                    symbol=symbol,
                    market_close=start + timedelta(days=7 * index),
                )
            )
    values.append(
        SimpleNamespace(
            status="SKIPPED",
            window_id="boundary",
            symbol="BTCUSDT",
            market_close=start - timedelta(days=3),
        )
    )
    return values


def test_round13_protocol_sources_and_external_manifests_are_frozen() -> None:
    root = Path(__file__).resolve().parents[1]
    expected_files = {
        root / "reports/cross-era-oos/round5-early-wind-down-results.json": (
            ROUND5_RESULT_SHA256
        ),
        root / "reports/cross-era-oos/round11-quadratic-maker-urgency-results.json": (
            ROUND11_RESULT_SHA256
        ),
        root / "reports/cross-era-oos/round12-quadratic-volatility-defense-results.json": (
            ROUND12_RESULT_SHA256
        ),
        root
        / "data/backtests/prehistory_2020_h1/binance_um_btcusdt_1m_1577836800000_1595116740000_a03cdb162ec7.manifest.json": MANIFEST_SHA256[
            "BTCUSDT"
        ],
        root
        / "data/backtests/prehistory_2020_h1/binance_um_ethusdt_1m_1577836800000_1595116740000_50e47ed56554.manifest.json": MANIFEST_SHA256[
            "ETHUSDT"
        ],
    }
    protocol = (
        root
        / "reports/cross-era-oos/round13-prehistory-quadratic-w2160-protocol.md"
    )

    assert hashlib.sha256(protocol.read_bytes()).hexdigest() == PROTOCOL_SHA256
    for path, expected_sha256 in expected_files.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha256


def test_round13_candidate_is_the_only_registered_parameter_change() -> None:
    base_config = ResearchConfig(
        wind_down_bars=77,
        profit_protection_enabled=False,
        volatility_reduce_expansion_ratio=0.0,
        volatility_reduce_after_breaches=0,
    )
    base_policy = WindDownMakerPolicy(5, 1.10, 1.0, urgency_exponent=4.0)

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
    } == {"wind_down_bars"}
    assert reference_config.wind_down_bars == REFERENCE_WIND_DOWN_BARS
    assert candidate_config.wind_down_bars == CANDIDATE_WIND_DOWN_BARS

    reference_policy_values = asdict(reference_policy)
    candidate_policy_values = asdict(candidate_policy)
    assert {
        key
        for key in reference_policy_values
        if reference_policy_values[key] != candidate_policy_values[key]
    } == {"urgency_exponent"}
    assert reference_policy.urgency_exponent == pytest.approx(REFERENCE_EXPONENT)
    assert candidate_policy.urgency_exponent == pytest.approx(CANDIDATE_EXPONENT)
    assert candidate_policy.reprice_interval_bars == base_policy.reprice_interval_bars
    assert candidate_policy.initial_offset_steps == pytest.approx(
        base_policy.initial_offset_steps
    )
    assert candidate_policy.unwind_fraction == pytest.approx(
        base_policy.unwind_fraction
    )


def test_round13_external_window_ids_require_28_paired_ready_windows() -> None:
    ids = _paired_ready_window_ids(_windows())

    assert len(ids) == 28
    assert ids[0] == "ext00"
    assert ids[-1] == "ext27"
    assert "boundary" not in ids


def test_round13_external_window_ids_reject_unpaired_window() -> None:
    windows = _windows()
    windows = [
        item
        for item in windows
        if not (item.window_id == "ext27" and item.symbol == "ETHUSDT")
    ]

    with pytest.raises(ValueError, match="未成对"):
        _paired_ready_window_ids(windows)


def test_round13_external_window_ids_reject_changed_count() -> None:
    with pytest.raises(ValueError, match="数量变化"):
        _paired_ready_window_ids(_windows(27))


@pytest.mark.parametrize(
    ("variant_id", "wind_down_bars", "exponent"),
    [
        (REFERENCE_VARIANT_ID, REFERENCE_WIND_DOWN_BARS, REFERENCE_EXPONENT),
        (CANDIDATE_ID, CANDIDATE_WIND_DOWN_BARS, CANDIDATE_EXPONENT),
    ],
)
def test_round13_worker_cache_verifies_external_execution(
    variant_id: str,
    wind_down_bars: int,
    exponent: float,
) -> None:
    config, _policy = _variant_config_and_policy(
        ResearchConfig(),
        WindDownMakerPolicy(5, 1.10, 1.0),
        variant_id,
    )
    research = SimpleNamespace(
        _cache=_cache(wind_down_bars, exponent),
        config=config,
    )

    observed = _verify_worker_cache(
        research,
        allowed_window_ids={"ext1", "ext2"},
        expected_wind_down_bars=wind_down_bars,
        expected_exponent=exponent,
    )

    assert observed["window_count"] == 2
    assert observed["symbol_window_count"] == 4
    assert observed["wind_down_bars"] == wind_down_bars
    assert observed["urgency_exponent"] == pytest.approx(exponent)
    assert observed["profit_protection_enabled"] is False
    assert observed["volatility_reduce_enabled"] is False
    assert observed["passed"] is True


def test_round13_worker_cache_rejects_non_external_access() -> None:
    config, _policy = _variant_config_and_policy(
        ResearchConfig(),
        WindDownMakerPolicy(5, 1.10, 1.0),
        CANDIDATE_ID,
    )
    research = SimpleNamespace(
        _cache=_cache(
            CANDIDATE_WIND_DOWN_BARS,
            CANDIDATE_EXPONENT,
            window_ids=("ext1", "current_final_oos"),
        ),
        config=config,
    )

    with pytest.raises(RuntimeError, match="外部窗口之外"):
        _verify_worker_cache(
            research,
            allowed_window_ids={"ext1", "ext2"},
            expected_wind_down_bars=CANDIDATE_WIND_DOWN_BARS,
            expected_exponent=CANDIDATE_EXPONENT,
        )


@pytest.mark.parametrize(
    ("cache_wind_down", "cache_exponent", "message"),
    [
        (1440, 2.0, "wind-down"),
        (2160, 1.0, "紧迫度指数"),
    ],
)
def test_round13_worker_cache_rejects_wrong_registered_parameters(
    cache_wind_down: int,
    cache_exponent: float,
    message: str,
) -> None:
    config, _policy = _variant_config_and_policy(
        ResearchConfig(),
        WindDownMakerPolicy(5, 1.10, 1.0),
        CANDIDATE_ID,
    )
    research = SimpleNamespace(
        _cache=_cache(cache_wind_down, cache_exponent),
        config=config,
    )

    with pytest.raises(RuntimeError, match=message):
        _verify_worker_cache(
            research,
            allowed_window_ids={"ext1", "ext2"},
            expected_wind_down_bars=CANDIDATE_WIND_DOWN_BARS,
            expected_exponent=CANDIDATE_EXPONENT,
        )


@pytest.mark.parametrize(
    ("field", "invalid_value", "message"),
    [
        ("profit_protection_enabled", True, "利润保护"),
        ("volatility_reduce_expansion_ratio", 1.5, "波动市场减仓"),
        ("volatility_reduce_after_breaches", 10, "波动市场减仓"),
    ],
)
def test_round13_worker_cache_rejects_unregistered_mechanisms(
    field: str,
    invalid_value: object,
    message: str,
) -> None:
    config, _policy = _variant_config_and_policy(
        ResearchConfig(),
        WindDownMakerPolicy(5, 1.10, 1.0),
        CANDIDATE_ID,
    )
    research = SimpleNamespace(
        _cache=_cache(CANDIDATE_WIND_DOWN_BARS, CANDIDATE_EXPONENT),
        config=replace(config, **{field: invalid_value}),
    )

    with pytest.raises(RuntimeError, match=message):
        _verify_worker_cache(
            research,
            allowed_window_ids={"ext1", "ext2"},
            expected_wind_down_bars=CANDIDATE_WIND_DOWN_BARS,
            expected_exponent=CANDIDATE_EXPONENT,
        )


def test_round13_rejects_unknown_variant() -> None:
    with pytest.raises(ValueError, match="未知 Round 13 变体"):
        _variant_config_and_policy(
            ResearchConfig(),
            WindDownMakerPolicy(5, 1.10, 1.0),
            "UNREGISTERED",
        )
