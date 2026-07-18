from __future__ import annotations

from typing import Any

from exchange.base import ExchangeClient


class MockExchangeClient(ExchangeClient):
    def __init__(self) -> None:
        self.orders: dict[str, list[dict[str, Any]]] = {}
        self.order_statuses: dict[str, str] = {}
        self.stop_orders: dict[str, list[dict[str, Any]]] = {}
        self.positions: dict[str, float] = {}
        self.hedge_positions: dict[str, dict[str, float]] = {}
        self.market_orders: list[dict[str, Any]] = []
        self.order_trades: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.balance = 10_000.0
        self.symbols = [
            {"symbol": "AAPLUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "MSFTUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "TSLAPREUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "BADUSDT", "status": "BREAK", "contractType": "PERPETUAL"},
        ]

    async def get_symbols(self) -> list[dict[str, Any]]:
        return list(self.symbols)

    async def get_symbol_rules(self, symbol: str) -> dict[str, Any]:
        return {"tick_size": 0.000001, "step_size": 0.000001, "min_qty": 0.000001}

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        return None

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        return None

    async def get_account_balance(self) -> float:
        return self.balance

    async def get_account_summary(self) -> dict[str, Any]:
        exposure = sum(abs(qty) * 100.0 for qty in self.positions.values())
        return {
            "asset": "USDT",
            "balance": self.balance,
            "available_balance": self.balance,
            "margin_balance": self.balance,
            "initial_margin": 0.0,
            "maintenance_margin": 0.0,
            "unrealized_pnl": 0.0,
            "current_exposure": exposure,
            "positions": [],
        }

    async def get_position(self, symbol: str) -> dict[str, Any]:
        if symbol in self.hedge_positions:
            hedge = self.hedge_positions[symbol]
            return {
                "symbol": symbol,
                "long_qty": hedge.get("LONG", 0.0),
                "short_qty": hedge.get("SHORT", 0.0),
            }
        return {"symbol": symbol, "qty": self.positions.get(symbol, 0.0)}

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        return list(self.orders.get(symbol, []))

    async def get_order(self, symbol: str, order_id: str, client_id: str) -> dict[str, Any]:
        for order in self.orders.get(symbol, []):
            if str(order.get("orderId", order.get("client_id", ""))) == str(order_id) or str(order.get("client_id", "")) == str(client_id):
                return {**order, "status": self.order_statuses.get(str(order.get("client_id", "")), order.get("status", "open"))}
        return {"symbol": symbol, "orderId": order_id, "client_id": client_id, "status": self.order_statuses.get(client_id, "unknown")}

    async def get_order_trades(self, symbol: str, order_id: str) -> list[dict[str, Any]]:
        return list(self.order_trades.get((symbol, str(order_id)), []))

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        order = {
            "symbol": symbol,
            "side": side,
            "price": price,
            "qty": qty,
            "client_id": client_id,
            "timeInForce": "GTX",
            "status": "open",
        }
        if position_side is not None:
            order["position_side"] = position_side
        self.orders.setdefault(symbol, []).append(order)
        self.order_statuses[client_id] = "open"
        return order

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        order = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "reduce_only": reduce_only,
            "status": "filled",
        }
        if position_side is not None:
            order["position_side"] = position_side
        if client_id is not None:
            order["client_id"] = client_id
        self.market_orders.append(order)
        if position_side is not None:
            hedge = self.hedge_positions.setdefault(symbol, {"LONG": 0.0, "SHORT": 0.0})
            if position_side == "LONG":
                hedge["LONG"] = (
                    max(0.0, hedge["LONG"] - qty)
                    if side == "SELL"
                    else hedge["LONG"] + qty
                )
            elif position_side == "SHORT":
                hedge["SHORT"] = (
                    max(0.0, hedge["SHORT"] - qty)
                    if side == "BUY"
                    else hedge["SHORT"] + qty
                )
        elif reduce_only:
            current = self.positions.get(symbol, 0.0)
            if side == "SELL":
                self.positions[symbol] = max(0.0, current - qty) if current > 0 else current
            else:
                self.positions[symbol] = min(0.0, current + qty) if current < 0 else current
        response = {
            **order,
            "orderId": f"market-{len(self.market_orders)}",
            "executedQty": qty,
            "avgPrice": 100.0,
        }
        if client_id is not None:
            response["clientOrderId"] = client_id
        return response

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ) -> dict[str, Any]:
        order = {
            "symbol": symbol,
            "side": side,
            "stopPrice": stop_price,
            "client_id": client_id,
            "type": "STOP_MARKET",
            "closePosition": close_position,
            "status": "open",
        }
        self.stop_orders.setdefault(symbol, []).append(order)
        return order

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        matched = next(
            (
                order
                for order in self.orders.get(symbol, [])
                if str(order.get("orderId", order.get("client_id", ""))) == str(order_id)
            ),
            None,
        )
        for order in self.orders.get(symbol, []):
            if str(order.get("orderId", order.get("client_id", ""))) == str(order_id):
                self.order_statuses[str(order.get("client_id", ""))] = "CANCELED"
        self.orders[symbol] = [
            order
            for order in self.orders.get(symbol, [])
            if str(order.get("orderId", order.get("client_id", ""))) != str(order_id)
        ]
        return {
            **(matched or {}),
            "symbol": symbol,
            "orderId": str(order_id),
            "status": "CANCELED",
            "executedQty": str((matched or {}).get("executedQty", 0.0)),
        }

    async def cancel_all_orders(self, symbol: str) -> None:
        for order in self.orders.get(symbol, []):
            self.order_statuses[str(order.get("client_id", ""))] = "CANCELED"
        self.orders[symbol] = []
        self.stop_orders[symbol] = []

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        # 不含 open_time 时由 RecentMarketHistoryService 按 as_of 合成已闭合时间轴，
        # 以兼容测试中传入的固定历史 now。
        return [
            {
                "open": 100,
                "high": 100.1,
                "low": 99.9,
                "close": 100 + ((idx % 5) - 2) * 0.03,
                "volume": 10.0,
            }
            for idx in range(limit)
        ]

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "quoteVolume": "1000000", "lastPrice": "100.0"}

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]:
        return {"bids": [["99.9", "10"]], "asks": [["100.1", "10"]]}

    async def get_funding_rate(self, symbol: str) -> float:
        return 0.0001

    async def get_commission_rate(self, symbol: str) -> dict[str, float]:
        return {"maker": 0.0, "taker": 0.0005}
