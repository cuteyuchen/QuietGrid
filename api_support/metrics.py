"""回测与对账用的无状态数值/序列统计函数。

从 api.py 抽出，均为纯函数：输入决定输出，无副作用、无外部依赖。
"""

from __future__ import annotations

from math import isfinite, sqrt
from typing import Any


def _ratio_sum(items: list[dict[str, Any]], numerator: str, denominator: str) -> float:
    upper = sum(float(item.get(numerator) or 0.0) for item in items)
    lower = sum(float(item.get(denominator) or 0.0) for item in items)
    return upper / lower if lower else 0.0


def _series_sharpe(changes: list[float]) -> float:
    if len(changes) < 2:
        return 0.0
    mean = sum(changes) / len(changes)
    variance = sum((value - mean) ** 2 for value in changes) / (len(changes) - 1)
    return mean / sqrt(variance) * sqrt(len(changes)) if variance > 0 else 0.0


def _series_sortino(changes: list[float]) -> float:
    if len(changes) < 2:
        return 0.0
    downside = [min(0.0, value) for value in changes]
    variance = sum(value * value for value in downside) / len(downside)
    return (sum(changes) / len(changes)) / sqrt(variance) * sqrt(len(changes)) if variance > 0 else 0.0


def _series_cvar(changes: list[float]) -> float:
    losses = sorted(-value for value in changes if value < 0)
    if not losses:
        return 0.0
    tail_start = max(0, int(len(losses) * 0.95) - 1)
    tail = losses[tail_start:]
    return sum(tail) / len(tail)


def _numeric_quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _orders_refer_to_same_order(
    local: dict[str, Any],
    exchange: dict[str, Any],
) -> bool:
    local_client = str(local.get("client_id") or "")
    exchange_client = str(exchange.get("client_id") or "")
    if local_client and exchange_client and local_client == exchange_client:
        return True
    local_order = str(local.get("order_id") or "")
    exchange_order = str(exchange.get("order_id") or "")
    return bool(local_order and exchange_order and local_order == exchange_order)


def _numbers_close(left: Any, right: Any) -> bool:
    left_value = _optional_float(left)
    right_value = _optional_float(right)
    if left_value is None or right_value is None:
        return left_value is right_value
    return abs(left_value - right_value) <= max(
        1e-9,
        1e-8 * max(abs(left_value), abs(right_value)),
    )


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None
