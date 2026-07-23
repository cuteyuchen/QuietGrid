from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.cross_era_spot_feasibility import (
    _cross_market_passed,
    _paired_contiguous_window_ids,
    _validate_execution_integrity,
)


def _window(
    window_id: str,
    symbol: str,
    times: tuple[int, ...],
    *,
    status: str = "READY",
) -> SimpleNamespace:
    return SimpleNamespace(
        window_id=window_id,
        symbol=symbol,
        observation_rows=1,
        status=status,
        rows=tuple(SimpleNamespace(open_time=value) for value in times),
    )


def test_paired_contiguous_window_ids_excludes_gaps_and_requires_both_symbols() -> None:
    windows = [
        _window("good", "BTCUSDT", (0, 60_000, 120_000)),
        _window("good", "ETHUSDT", (0, 60_000, 120_000)),
        _window("gap", "BTCUSDT", (0, 60_000, 180_000)),
        _window("gap", "ETHUSDT", (0, 60_000, 120_000)),
        _window("single", "BTCUSDT", (0, 60_000, 120_000)),
    ]

    paired, quality = _paired_contiguous_window_ids(windows, minimum=1)

    assert paired == ("good",)
    assert quality["paired_contiguous_count"] == 1
    assert quality["excluded_window_ids"] == ["gap", "single"]


def test_paired_contiguous_window_ids_enforces_minimum() -> None:
    windows = [
        _window("only", "BTCUSDT", (0, 60_000, 120_000)),
        _window("only", "ETHUSDT", (0, 60_000, 120_000)),
    ]

    with pytest.raises(RuntimeError, match="成对连续窗口不足"):
        _paired_contiguous_window_ids(windows, minimum=2)


def test_validate_execution_integrity_requires_exact_frozen_policy() -> None:
    integrity = {
        "window_count": 101,
        "symbol_window_count": 202,
        "wind_down_bars": 2160,
        "reprice_interval_bars": 5,
        "initial_offset_steps": 1.1,
        "unwind_fraction": 1.0,
        "urgency_exponent": 2.0,
        "cache_entry_count": 202,
        "profit_protection_enabled": False,
        "volatility_reduce_enabled": False,
        "passed": True,
    }

    _validate_execution_integrity(integrity, window_count=101)

    with pytest.raises(RuntimeError, match="执行完整性"):
        _validate_execution_integrity(
            {**integrity, "urgency_exponent": 1.0},
            window_count=101,
        )


def test_cross_market_passed_requires_all_four_symbol_cells() -> None:
    cells = {
        name: {
            "symbols": {
                "BTCUSDT": {"passed": True},
                "ETHUSDT": {"passed": True},
            }
        }
        for name in (
            "SPOT_2018_2020_EXTERNAL_BASE",
            "SPOT_2018_2020_EXTERNAL_COST50",
        )
    }

    assert _cross_market_passed(cells) is True
    cells["SPOT_2018_2020_EXTERNAL_COST50"]["symbols"]["ETHUSDT"][
        "passed"
    ] = False
    assert _cross_market_passed(cells) is False
