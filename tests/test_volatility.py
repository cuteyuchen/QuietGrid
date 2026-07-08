from __future__ import annotations

from math import log, sqrt
from statistics import fmean

import pytest

from strategy.volatility import VolatilityCalculationError, estimate_ohlc_volatility


def _klines() -> list[dict[str, float]]:
    return [
        {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0},
        {"open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0},
        {"open": 102.0, "high": 104.0, "low": 101.0, "close": 103.0},
        {"open": 103.0, "high": 105.0, "low": 102.0, "close": 104.0},
    ]


def test_parkinson_volatility_matches_formula() -> None:
    rows = _klines()
    expected = sqrt(fmean(log(row["high"] / row["low"]) ** 2 for row in rows) / (4 * log(2)))

    assert estimate_ohlc_volatility(rows, "parkinson") == pytest.approx(expected)


def test_garman_klass_volatility_matches_formula() -> None:
    rows = _klines()
    factor = 2 * log(2) - 1
    expected = sqrt(
        fmean(
            0.5 * log(row["high"] / row["low"]) ** 2
            - factor * log(row["close"] / row["open"]) ** 2
            for row in rows
        )
    )

    assert estimate_ohlc_volatility(rows, "garman_klass") == pytest.approx(expected)


def test_rogers_satchell_volatility_matches_formula() -> None:
    rows = _klines()
    expected = sqrt(
        fmean(
            log(row["high"] / row["open"]) * log(row["high"] / row["close"])
            + log(row["low"] / row["open"]) * log(row["low"] / row["close"])
            for row in rows
        )
    )

    assert estimate_ohlc_volatility(rows, "rogers_satchell") == pytest.approx(expected)


def test_yang_zhang_volatility_matches_formula() -> None:
    rows = _klines()
    open_jump_returns = [log(rows[index]["open"] / rows[index - 1]["close"]) for index in range(1, len(rows))]
    open_close_returns = [log(row["close"] / row["open"]) for row in rows]
    rs_variance = estimate_ohlc_volatility(rows, "rogers_satchell") ** 2
    n = len(rows)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    expected = sqrt(
        _sample_variance(open_jump_returns)
        + k * _sample_variance(open_close_returns)
        + (1 - k) * rs_variance
    )

    assert estimate_ohlc_volatility(rows, "yang_zhang") == pytest.approx(expected)


def test_ohlc_volatility_rejects_missing_open() -> None:
    rows = [{key: value for key, value in row.items() if key != "open"} for row in _klines()]

    with pytest.raises(VolatilityCalculationError, match="open"):
        estimate_ohlc_volatility(rows, "garman_klass")


def test_ohlc_volatility_rejects_invalid_prices() -> None:
    invalid_cases = [
        [{**_klines()[0], "open": 0.0}, *_klines()[1:]],
        [{**_klines()[0], "high": _klines()[0]["low"] - 0.1}, *_klines()[1:]],
        [{**_klines()[0], "close": float("nan")}, *_klines()[1:]],
    ]

    for rows in invalid_cases:
        with pytest.raises(VolatilityCalculationError):
            estimate_ohlc_volatility(rows, "parkinson")


def _sample_variance(values: list[float]) -> float:
    mean = fmean(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)
