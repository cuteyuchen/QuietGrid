from __future__ import annotations

from math import isfinite, log, sqrt
from statistics import fmean
from typing import Any, Iterable


OHLC_VOLATILITY_METHODS = {"parkinson", "garman_klass", "rogers_satchell", "yang_zhang"}


class VolatilityCalculationError(ValueError):
    pass


def estimate_ohlc_volatility(klines: Iterable[dict[str, Any]], method: str) -> float:
    rows = [_ohlc_row(row) for row in klines]
    if len(rows) < 2:
        raise VolatilityCalculationError("波动率样本不足。")
    normalized = _normalize_method(method)
    if normalized == "parkinson":
        return _positive_result(_parkinson(rows), normalized)
    if normalized == "garman_klass":
        return _positive_result(_garman_klass(rows), normalized)
    if normalized == "rogers_satchell":
        return _positive_result(_rogers_satchell(rows), normalized)
    if normalized == "yang_zhang":
        return _positive_result(_yang_zhang(rows), normalized)
    raise VolatilityCalculationError(f"不支持的波动率算法: {method}")


def _parkinson(rows: list[tuple[float, float, float, float]]) -> float:
    variance = fmean(log(high / low) ** 2 for _open, high, low, _close in rows) / (4 * log(2))
    return sqrt(max(variance, 0.0))


def _garman_klass(rows: list[tuple[float, float, float, float]]) -> float:
    factor = 2 * log(2) - 1
    variance = fmean(
        0.5 * log(high / low) ** 2 - factor * log(close / open_price) ** 2
        for open_price, high, low, close in rows
    )
    return sqrt(max(variance, 0.0))


def _rogers_satchell(rows: list[tuple[float, float, float, float]]) -> float:
    variance = fmean(
        log(high / open_price) * log(high / close) + log(low / open_price) * log(low / close)
        for open_price, high, low, close in rows
    )
    return sqrt(max(variance, 0.0))


def _yang_zhang(rows: list[tuple[float, float, float, float]]) -> float:
    open_jump_returns = [
        log(rows[index][0] / rows[index - 1][3])
        for index in range(1, len(rows))
    ]
    open_close_returns = [log(close / open_price) for open_price, _high, _low, close in rows]
    open_jump_variance = _sample_variance(open_jump_returns)
    open_close_variance = _sample_variance(open_close_returns)
    rs_variance = _rogers_satchell(rows) ** 2
    n = len(rows)
    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    variance = open_jump_variance + k * open_close_variance + (1 - k) * rs_variance
    return sqrt(max(variance, 0.0))


def _sample_variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = fmean(values)
    return sum((value - mean) ** 2 for value in values) / (len(values) - 1)


def _ohlc_row(row: dict[str, Any]) -> tuple[float, float, float, float]:
    open_price = _positive_number(row, "open")
    high = _positive_number(row, "high")
    low = _positive_number(row, "low")
    close = _positive_number(row, "close")
    if high < low or open_price < low or open_price > high or close < low or close > high:
        raise VolatilityCalculationError("K线价格关系非法。")
    return open_price, high, low, close


def _positive_number(row: dict[str, Any], key: str) -> float:
    try:
        raw_value = row[key]
    except KeyError as exc:
        raise VolatilityCalculationError(f"K线缺少有效字段: {key}") from exc
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise VolatilityCalculationError(f"K线字段 {key} 无效。") from exc
    if not isfinite(value) or value <= 0:
        raise VolatilityCalculationError(f"K线字段 {key} 无效。")
    return value


def _positive_result(value: float, method: str) -> float:
    if not isfinite(value) or value <= 0:
        raise VolatilityCalculationError(f"{method} 波动率计算结果非法。")
    return value


def _normalize_method(method: str) -> str:
    return str(method).strip().lower()
