"""TradingController 使用的无状态辅助函数。

从 strategy/controller.py 抽出，均为纯函数：以显式参数接收 ticker/position/
orderbook/trade 等字典或标量，无实例状态、无副作用。controller.py 通过显式
import 重新导出这些名字，因此 `from strategy.controller import _xxx` 仍然可用。
"""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from core.models import OrderSide, SymbolSession


def _ticker_last_price(ticker: dict[str, Any]) -> float:
    for key in ("lastPrice", "last_price", "price", "bidPrice"):
        value = ticker.get(key)
        if value is not None:
            return _positive_price(value, key)
    raise ValueError("ticker 缺少当前价格字段。")


def _position_qty(position: dict[str, Any]) -> float:
    for key in ("qty", "positionAmt"):
        value = position.get(key)
        if value not in (None, ""):
            return _finite_float(value, key)
    raise ValueError("持仓响应缺少数量字段。")


def _has_hedge_position_fields(position: dict[str, Any]) -> bool:
    return "long_qty" in position or "short_qty" in position


def _position_hedge_qty(position: dict[str, Any]) -> tuple[float, float]:
    long_qty = _non_negative_float(position.get("long_qty", 0.0), "long_qty")
    short_qty = _non_negative_float(position.get("short_qty", 0.0), "short_qty")
    return long_qty, short_qty


def _position_exposure(position: dict[str, Any]) -> float:
    if _has_hedge_position_fields(position):
        long_qty, short_qty = _position_hedge_qty(position)
        return long_qty + short_qty
    return abs(_position_qty(position))


def _closed_klines_as_of(
    klines: list[dict[str, Any]],
    at: datetime,
) -> list[dict[str, Any]]:
    cutoff_ms = at.timestamp() * 1000
    closed: list[dict[str, Any]] = []
    for row in klines:
        close_time = row.get("close_time")
        if close_time in (None, ""):
            closed.append(row)
            continue
        try:
            timestamp_ms = float(close_time)
        except (TypeError, ValueError):
            continue
        if isfinite(timestamp_ms) and timestamp_ms <= cutoff_ms:
            closed.append(row)
    return closed


def _kline_data_age_seconds(
    klines: list[dict[str, Any]],
    at: datetime,
) -> float:
    if not klines:
        return float("inf")
    close_time = klines[-1].get("close_time")
    if close_time in (None, ""):
        return 0.0
    try:
        timestamp_ms = float(close_time)
    except (TypeError, ValueError):
        return float("inf")
    if not isfinite(timestamp_ms):
        return float("inf")
    return max(0.0, at.timestamp() - timestamp_ms / 1000)


def _orderbook_liquidity(
    orderbook: dict[str, Any],
    levels: int,
) -> tuple[float, float]:
    bids = list(orderbook.get("bids") or [])[:levels]
    asks = list(orderbook.get("asks") or [])[:levels]
    if not bids or not asks:
        raise ValueError("订单簿缺少买一或卖一。")
    best_bid = _positive_price(bids[0][0], "best bid")
    best_ask = _positive_price(asks[0][0], "best ask")
    if best_ask <= best_bid:
        raise ValueError("订单簿买卖价差异常。")
    center = (best_bid + best_ask) / 2
    depth_usdt = 0.0
    for row in (*bids, *asks):
        if len(row) < 2:
            raise ValueError("订单簿档位数据不完整。")
        price = _positive_price(row[0], "orderbook price")
        qty = _non_negative_float(row[1], "orderbook qty")
        depth_usdt += price * qty
    return (best_ask - best_bid) / center, depth_usdt


def _position_close_specs(position: dict[str, Any]) -> list[tuple[str, float, str | None]]:
    if _has_hedge_position_fields(position):
        long_qty, short_qty = _position_hedge_qty(position)
        specs: list[tuple[str, float, str | None]] = []
        if long_qty > 0:
            specs.append((OrderSide.SELL.value, long_qty, "LONG"))
        if short_qty > 0:
            specs.append((OrderSide.BUY.value, short_qty, "SHORT"))
        return specs

    actual_qty = _position_qty(position)
    qty = abs(actual_qty)
    if qty <= 0:
        return []
    side = OrderSide.SELL.value if actual_qty > 0 else OrderSide.BUY.value
    return [(side, qty, None)]


def _reconciliation_close_client_id(symbol: str, side: str, position_side: str | None) -> str:
    symbol_part = "".join(char.lower() for char in str(symbol) if char.isalnum())[:20] or "symbol"
    close_side = str(position_side or side).lower()
    return f"qgr-{symbol_part}-{close_side}"


def _position_log_fields(position: dict[str, Any]) -> dict[str, Any]:
    fields = {"actual_qty": _position_qty(position)}
    if _has_hedge_position_fields(position):
        long_qty, short_qty = _position_hedge_qty(position)
        fields["actual_long_qty"] = long_qty
        fields["actual_short_qty"] = short_qty
    return fields


def _position_log_detail(position: dict[str, Any]) -> str:
    fields = _position_log_fields(position)
    return ", ".join(f"{key}={value}" for key, value in fields.items())


def _position_close_specs_log(specs: list[tuple[str, float, str | None]]) -> list[dict[str, Any]]:
    return [
        {"side": side, "qty": qty, "position_side": position_side}
        for side, qty, position_side in specs
    ]


def _required_float(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if value in (None, ""):
        raise ValueError(f"缺少 {key} 字段")
    return _finite_float(value, key)


def _positive_price(value: Any, label: str) -> float:
    price = _finite_float(value, label)
    if price <= 0:
        raise ValueError(f"{label} 必须大于 0。")
    return price


def _positive_qty(value: Any, label: str) -> float:
    qty = _finite_float(value, label)
    if qty <= 0:
        raise ValueError(f"{label} 必须大于 0。")
    return qty


def _non_negative_float(value: Any, label: str) -> float:
    number = _finite_float(value, label)
    if number < 0:
        raise ValueError(f"{label} 必须为非负数。")
    return number


def _positive_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0


def _non_negative_finite_number(value: Any) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number >= 0


def _finite_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 不是有效数字。") from exc
    if not isfinite(number):
        raise ValueError(f"{label} 不是有限数字。")
    return number


def _response_client_id_or_none(response: dict[str, Any]) -> str | None:
    for key in ("clientOrderId", "client_id", "origClientOrderId"):
        value = response.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _summarize_exchange_trades(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not trades:
        return None
    total_qty = 0.0
    total_quote = 0.0
    total_fee = 0.0
    realized_pnl = 0.0
    latest_time = 0
    side = ""
    for trade in trades:
        qty = _positive_qty(trade.get("qty"), "exchange trade qty")
        price = _positive_price(trade.get("price"), "exchange trade price")
        fee = _non_negative_float(trade.get("commission", 0.0), "exchange trade commission")
        pnl = _finite_float(trade.get("realizedPnl", 0.0), "exchange trade realized pnl")
        trade_side = str(trade.get("side", "")).upper()
        if trade_side not in {"BUY", "SELL"}:
            raise ValueError("exchange trade side invalid")
        if side and side != trade_side:
            raise ValueError("exchange order trades include mixed sides")
        side = trade_side
        total_qty += qty
        total_quote += price * qty
        total_fee += fee
        realized_pnl += pnl
        latest_time = max(latest_time, int(trade.get("time", 0) or 0))
    trade_time = (
        datetime.fromtimestamp(latest_time / 1000, tz=timezone.utc)
        if latest_time > 0
        else datetime.now(timezone.utc)
    )
    return {
        "side": side,
        "price": total_quote / total_qty,
        "qty": total_qty,
        "fee": total_fee,
        "realized_pnl": realized_pnl,
        "trade_time": trade_time,
    }


def _is_session_stop_order_client_id(session: SymbolSession, client_id: str) -> bool:
    prefixes = (
        f"qg-{session.session_id}-stop-long",
        f"qg-{session.session_id}-stop-short",
    )
    return any(client_id == prefix or client_id.startswith(f"{prefix}-") for prefix in prefixes)
