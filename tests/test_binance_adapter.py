from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import Any

from exchange.binance import (
    BinanceFuturesClient,
    _futures_price_stream_url,
    _requests_params,
    _server_time_offset_ms,
    parse_order_trade_update,
    parse_price_update,
    symbol_rules_from_exchange_info,
)


class FakeBinanceAsyncClient:
    def __init__(self) -> None:
        self.created_orders: list[dict[str, Any]] = []

    async def futures_exchange_info(self) -> dict[str, Any]:
        return {
            "symbols": [
                {
                    "symbol": "AAPLUSDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.00100000", "minQty": "0.00100000"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5.00000000"},
                    ],
                }
            ]
        }

    async def futures_change_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        return {"symbol": symbol, "leverage": leverage}

    async def futures_change_margin_type(self, symbol: str, marginType: str) -> dict[str, Any]:
        return {"symbol": symbol, "marginType": marginType}

    async def futures_account_balance(self) -> list[dict[str, str]]:
        return [{"asset": "USDT", "availableBalance": "123.45"}]

    async def futures_position_information(self, symbol: str) -> list[dict[str, str]]:
        return [{"symbol": symbol, "positionAmt": "2.5"}]

    async def futures_get_open_orders(self, symbol: str) -> list[dict[str, str]]:
        return [{"symbol": symbol, "orderId": "1"}]

    async def futures_get_order(self, **kwargs) -> dict[str, Any]:
        return {"symbol": kwargs["symbol"], "orderId": kwargs.get("orderId", "123"), "origClientOrderId": kwargs.get("origClientOrderId", ""), "status": "FILLED"}

    async def futures_create_order(self, **kwargs) -> dict[str, Any]:
        self.created_orders.append(kwargs)
        return {"orderId": "123", **kwargs}

    async def _request_futures_api(self, method: str, path: str, signed: bool, data: dict[str, Any]) -> dict[str, Any]:
        if method == "post" and path == "order/test" and signed:
            self.created_orders.append({"test": True, **data})
            return {"orderId": 0, **data}
        if method == "post" and path == "algoOrder" and signed:
            order = {"algoId": 456, "algoStatus": "NEW", **data}
            self.created_orders.append({"algo": True, **data})
            return order
        if method == "delete" and path == "algoOrder" and signed:
            return {"algoId": data["algoId"], "code": "200", "msg": "success"}
        if method == "get" and path == "openAlgoOrders" and signed:
            return [{"symbol": data["symbol"], "algoId": 456, "clientAlgoId": "algo-cid", "algoStatus": "NEW"}]
        raise AssertionError(f"unexpected futures api request: {method} {path}")

    async def futures_get_position_mode(self) -> dict[str, bool]:
        return {"dualSidePosition": True}

    async def futures_stream_get_listen_key(self) -> str:
        return "listen-key"

    async def futures_stream_keepalive(self, listenKey: str) -> dict[str, str]:
        return {"listenKey": listenKey}

    async def futures_stream_close(self, listenKey: str) -> dict[str, Any]:
        return {}

    async def futures_cancel_all_open_orders(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol}

    async def futures_cancel_order(self, symbol: str, orderId: str) -> dict[str, Any]:
        return {"symbol": symbol, "orderId": orderId}

    async def futures_klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        return [[1, "100", "101", "99", "100.5", "10", 2] for _ in range(limit)]

    async def futures_ticker(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "lastPrice": "100.5", "quoteVolume": "1000000"}

    async def futures_order_book(self, symbol: str, limit: int) -> dict[str, list[list[str]]]:
        return {"bids": [["100", "1"]], "asks": [["101", "1"]]}

    async def futures_funding_rate(self, symbol: str, limit: int) -> list[dict[str, str]]:
        return [{"fundingRate": "0.0001"}]

    async def futures_commission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "makerCommissionRate": "0.000000", "takerCommissionRate": "0.000500"}


def test_binance_client_create_uses_futures_only_testnet_initialization(monkeypatch) -> None:
    class FakeAsyncClient:
        init_calls = 0
        create_calls = 0
        closed = 0

        def __init__(self, **kwargs):
            type(self).init_calls += 1
            self.kwargs = kwargs
            self.timestamp_offset = 0

        async def futures_ping(self):
            if type(self).init_calls == 1:
                raise RuntimeError("proxy reset")

        async def futures_time(self):
            return {"serverTime": 1_900_000_000_000}

        async def close_connection(self):
            type(self).closed += 1

        @classmethod
        async def create(cls, **kwargs):
            cls.create_calls += 1
            raise AssertionError("testnet futures client should not ping spot API during creation")

    async def run() -> None:
        monkeypatch.setitem(sys.modules, "binance", SimpleNamespace(AsyncClient=FakeAsyncClient))

        client = await BinanceFuturesClient.create(
            api_key="key",
            api_secret="secret",
            testnet=True,
            proxy_config={"enabled": True, "https": "http://127.0.0.1:7897"},
            create_retry_delay_seconds=0,
            create_retry_sleep=lambda seconds: None,
        )

        assert FakeAsyncClient.init_calls == 2
        assert FakeAsyncClient.create_calls == 0
        assert FakeAsyncClient.closed == 1
        assert client.client.kwargs["requests_params"] == {"proxy": "http://127.0.0.1:7897"}
        assert client.client.kwargs["testnet"] is True
        assert client.client.timestamp_offset != 0

    asyncio.run(run())


def test_server_time_offset_requires_server_time() -> None:
    assert isinstance(_server_time_offset_ms({"serverTime": "1900000000000"}), int)

    try:
        _server_time_offset_ms({"server_time": 1900000000000})
    except ValueError as exc:
        assert "serverTime" in str(exc)
    else:
        raise AssertionError("missing serverTime should fail")


def test_futures_price_stream_url_uses_raw_single_and_combined_multi_streams() -> None:
    assert _futures_price_stream_url("wss://stream.binancefuture.com/", ["btcusdt"]) == (
        "wss://stream.binancefuture.com/ws/btcusdt@ticker"
    )
    assert _futures_price_stream_url("wss://stream.binancefuture.com", ["BTCUSDT", "ETHUSDT"]) == (
        "wss://stream.binancefuture.com/stream?streams=btcusdt@ticker/ethusdt@ticker"
    )


class FlakyBalanceClient(FakeBinanceAsyncClient):
    def __init__(self, failures_before_success: int) -> None:
        super().__init__()
        self.failures_before_success = failures_before_success
        self.calls = 0

    async def futures_account_balance(self) -> list[dict[str, str]]:
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise RuntimeError("temporary network failure")
        return await super().futures_account_balance()


class NonJsonErrorClient(FakeBinanceAsyncClient):
    async def futures_account_balance(self) -> list[dict[str, str]]:
        raise RuntimeError(
            "APIError(code=0): Invalid JSON error message from Binance: "
            "<ClientResponse(https://testnet.binancefuture.com/fapi/v1/order?signature=secret) [502 Bad Gateway]>"
        )


class InvalidExchangeInfoClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_exchange_info(self) -> Any:
        return self.response


class InvalidKlinesClient(FakeBinanceAsyncClient):
    def __init__(self, row: list[Any]) -> None:
        super().__init__()
        self.row = row

    async def futures_klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        return [self.row]


class InvalidKlinesResponseClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_klines(self, symbol: str, interval: str, limit: int) -> Any:
        return self.response


class InvalidLeverageResponseClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_change_leverage(self, symbol: str, leverage: int) -> Any:
        return self.response


class InvalidMarginTypeResponseClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_change_margin_type(self, symbol: str, marginType: str) -> Any:
        return self.response


class InvalidFundingRateClient(FakeBinanceAsyncClient):
    async def futures_funding_rate(self, symbol: str, limit: int) -> list[dict[str, str]]:
        return [{"fundingRate": "inf"}]


class InvalidAccountBalanceResponseClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_account_balance(self) -> Any:
        return self.response


class InvalidFundingRateResponseClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_funding_rate(self, symbol: str, limit: int) -> Any:
        return self.response


class InvalidCommissionRateResponseClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_commission_rate(self, symbol: str) -> Any:
        return self.response


class InvalidTickerClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_ticker(self, symbol: str) -> Any:
        return self.response


class InvalidOrderBookClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_order_book(self, symbol: str, limit: int) -> Any:
        return self.response


class InvalidOpenOrdersClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_get_open_orders(self, symbol: str) -> Any:
        return self.response


class InvalidOrderLookupClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_get_order(self, **kwargs) -> Any:
        return self.response


class InvalidCancelOrderClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_cancel_order(self, symbol: str, orderId: str) -> Any:
        return self.response


class InvalidCancelAllOrdersClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_cancel_all_open_orders(self, symbol: str) -> Any:
        return self.response


class MissingUsdtBalanceClient(FakeBinanceAsyncClient):
    async def futures_account_balance(self) -> list[dict[str, str]]:
        return [{"asset": "USDT"}]


class BalanceOnlyClient(FakeBinanceAsyncClient):
    async def futures_account_balance(self) -> list[dict[str, str]]:
        return [{"asset": "USDT", "balance": "321.0"}]


class InvalidUsdtBalanceClient(FakeBinanceAsyncClient):
    async def futures_account_balance(self) -> list[dict[str, str]]:
        return [{"asset": "USDT", "availableBalance": "nan"}]


class RejectPostOnlyClient(FakeBinanceAsyncClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def futures_create_order(self, **kwargs) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("Order would immediately match and take. Post only rejected.")


class UnsupportedStopMarketClient(FakeBinanceAsyncClient):
    async def futures_create_order(self, **kwargs) -> dict[str, Any]:
        self.created_orders.append(kwargs)
        if kwargs.get("type") == "STOP_MARKET":
            raise RuntimeError(
                "APIError(code=-4120): Order type not supported for this endpoint. Please use the Algo Order API endpoints instead."
            )
        return {"orderId": "123", **kwargs}


class CancelOrderFailsWithOpenAlgoClient(FakeBinanceAsyncClient):
    def __init__(self) -> None:
        super().__init__()
        self.cancelled_algo_ids: list[int] = []

    async def futures_cancel_order(self, symbol: str, orderId: str) -> dict[str, Any]:
        raise RuntimeError("APIError(code=-2011): Unknown order sent.")

    async def _request_futures_api(self, method: str, path: str, signed: bool, data: dict[str, Any]) -> Any:
        if method == "get" and path == "openAlgoOrders" and signed:
            return [{"symbol": data["symbol"], "algoId": 456, "clientAlgoId": "stop-cid", "algoStatus": "NEW"}]
        if method == "delete" and path == "algoOrder" and signed:
            self.cancelled_algo_ids.append(int(data["algoId"]))
            return {"algoId": data["algoId"], "code": "200", "msg": "success"}
        return await super()._request_futures_api(method, path, signed, data)


class MissingOrderIdClient(FakeBinanceAsyncClient):
    async def futures_create_order(self, **kwargs) -> dict[str, Any]:
        self.created_orders.append(kwargs)
        return {**kwargs}


class OrderCreateStatusUnknownClient(FakeBinanceAsyncClient):
    async def futures_create_order(self, **kwargs) -> dict[str, Any]:
        self.created_orders.append(kwargs)
        raise RuntimeError(
            "APIError(code=-1007): Timeout waiting for response from backend server. "
            "Send status unknown; execution status unknown."
        )


class MismatchedOrderEchoClient(FakeBinanceAsyncClient):
    def __init__(self, overrides: dict[str, Any]) -> None:
        super().__init__()
        self.overrides = overrides

    async def futures_create_order(self, **kwargs) -> dict[str, Any]:
        self.created_orders.append(kwargs)
        return {"orderId": "123", **kwargs, **self.overrides}


class MismatchedAlgoOrderEchoClient(FakeBinanceAsyncClient):
    def __init__(self, overrides: dict[str, Any]) -> None:
        super().__init__()
        self.overrides = overrides

    async def _request_futures_api(self, method: str, path: str, signed: bool, data: dict[str, Any]) -> dict[str, Any]:
        if method == "post" and path == "algoOrder" and signed:
            self.created_orders.append({"algo": True, **data})
            return {"algoId": 456, "algoStatus": "NEW", **data, **self.overrides}
        return await super()._request_futures_api(method, path, signed, data)


class AlgoCreateStatusUnknownClient(FakeBinanceAsyncClient):
    def __init__(self) -> None:
        super().__init__()
        self.open_algo_orders: list[dict[str, Any]] = []

    async def _request_futures_api(self, method: str, path: str, signed: bool, data: dict[str, Any]) -> Any:
        if method == "post" and path == "algoOrder" and signed:
            order = {"algoId": 456, "algoStatus": "NEW", **data}
            self.created_orders.append({"algo": True, **data})
            self.open_algo_orders.append(order)
            raise RuntimeError(
                "APIError(code=-1007): Timeout waiting for response from backend server. "
                "Send status unknown; execution status unknown."
            )
        if method == "get" and path == "openAlgoOrders" and signed:
            return [order for order in self.open_algo_orders if order["symbol"] == data["symbol"]]
        return await super()._request_futures_api(method, path, signed, data)


class AlgoCreateStatusUnknownAfterStopFallbackClient(UnsupportedStopMarketClient):
    def __init__(self) -> None:
        super().__init__()
        self.open_algo_orders: list[dict[str, Any]] = []

    async def _request_futures_api(self, method: str, path: str, signed: bool, data: dict[str, Any]) -> Any:
        if method == "post" and path == "algoOrder" and signed:
            order = {"algoId": 456, "algoStatus": "NEW", **data}
            self.created_orders.append({"algo": True, **data})
            self.open_algo_orders.append(order)
            raise RuntimeError(
                "APIError(code=-1007): Timeout waiting for response from backend server. "
                "Send status unknown; execution status unknown."
            )
        if method == "get" and path == "openAlgoOrders" and signed:
            return [order for order in self.open_algo_orders if order["symbol"] == data["symbol"]]
        return await super()._request_futures_api(method, path, signed, data)


class MissingMakerCommissionClient(FakeBinanceAsyncClient):
    async def futures_commission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "takerCommissionRate": "0.000500"}


class MisspelledCommissionMethodClient(FakeBinanceAsyncClient):
    futures_commission_rate = None

    async def futures_comission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "makerCommissionRate": "0.000000", "takerCommissionRate": "0.000500"}


class InvalidMakerCommissionClient(FakeBinanceAsyncClient):
    async def futures_commission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "makerCommissionRate": "inf", "takerCommissionRate": "0.000500"}


class InvalidTakerCommissionClient(FakeBinanceAsyncClient):
    async def futures_commission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "makerCommissionRate": "0", "takerCommissionRate": "nan"}


class NegativeMakerCommissionClient(FakeBinanceAsyncClient):
    async def futures_commission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "makerCommissionRate": "-0.000100", "takerCommissionRate": "0.000500"}


class NegativeTakerCommissionClient(FakeBinanceAsyncClient):
    async def futures_commission_rate(self, symbol: str) -> dict[str, str]:
        return {"symbol": symbol, "makerCommissionRate": "0", "takerCommissionRate": "-0.000500"}


class MissingPositionAmountClient(FakeBinanceAsyncClient):
    async def futures_position_information(self, symbol: str) -> list[dict[str, str]]:
        return [{"symbol": symbol}]


class InvalidPositionAmountClient(FakeBinanceAsyncClient):
    async def futures_position_information(self, symbol: str) -> list[dict[str, str]]:
        return [{"symbol": symbol, "positionAmt": "-inf"}]


class InvalidPositionResponseClient(FakeBinanceAsyncClient):
    def __init__(self, response: Any) -> None:
        super().__init__()
        self.response = response

    async def futures_position_information(self, symbol: str) -> Any:
        return self.response


class HedgePositionClient(FakeBinanceAsyncClient):
    async def futures_position_information(self, symbol: str) -> list[dict[str, str]]:
        return [
            {"symbol": symbol, "positionSide": "LONG", "positionAmt": "0.3"},
            {"symbol": symbol, "positionSide": "SHORT", "positionAmt": "-0.2"},
        ]


class NoNeedMarginTypeClient(FakeBinanceAsyncClient):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def futures_change_margin_type(self, symbol: str, marginType: str) -> dict[str, Any]:
        self.calls += 1
        raise RuntimeError("No need to change margin type.")


class FakeSocket:
    def __init__(self, messages: list[dict[str, Any]], fail_after_messages: bool = False) -> None:
        self.messages = list(messages)
        self.fail_after_messages = fail_after_messages

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def recv(self):
        if self.messages:
            return self.messages.pop(0)
        if self.fail_after_messages:
            raise RuntimeError("socket disconnected")
        raise StopAsyncIteration


class BrokenExitSocket(FakeSocket):
    async def __aexit__(self, exc_type, exc, tb):
        raise AttributeError("'ClientConnection' object has no attribute 'fail_connection'")


def test_binance_adapter_maps_core_calls() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        assert (await client.get_symbols())[0]["symbol"] == "AAPLUSDT"
        assert await client.get_symbol_rules("AAPLUSDT") == {
            "tick_size": 0.01,
            "step_size": 0.001,
            "min_qty": 0.001,
            "min_notional": 5.0,
        }
        assert await client.get_account_balance() == 123.45
        assert (await client.get_position("AAPLUSDT"))["qty"] == 2.5
        assert (await client.get_order("AAPLUSDT", "123", "cid"))["status"] == "FILLED"
        assert len(await client.get_klines("AAPLUSDT", "1m", 2)) == 2
        assert await client.get_funding_rate("AAPLUSDT") == 0.0001
        assert await client.get_commission_rate("AAPLUSDT") == {"maker": 0.0, "taker": 0.0005}

        await client.place_limit_order_post_only("AAPLUSDT", "BUY", 100.0, 1.0, "cid")
        await client.place_market_order("AAPLUSDT", "SELL", 1.0)
        await client.place_stop_market_order("AAPLUSDT", "SELL", 98.0, "stop-cid")
        await client.cancel_order("AAPLUSDT", "123")

        assert raw.created_orders[0]["timeInForce"] == "GTX"
        assert raw.created_orders[0]["newClientOrderId"] == "cid"
        assert raw.created_orders[1]["type"] == "MARKET"
        assert raw.created_orders[1]["reduceOnly"] is True
        assert raw.created_orders[2]["type"] == "STOP_MARKET"
        assert raw.created_orders[2]["stopPrice"] == 98.0
        assert raw.created_orders[2]["closePosition"] is True

    asyncio.run(run())


def test_requests_params_uses_aiohttp_proxy_argument() -> None:
    params = _requests_params(
        {
            "enabled": True,
            "http": "http://127.0.0.1:7897",
            "https": "http://127.0.0.1:7897",
        }
    )

    assert params == {"proxy": "http://127.0.0.1:7897"}


def test_binance_adapter_parses_hedge_mode_positions() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(HedgePositionClient())

        position = await client.get_position("BTCUSDT")

        assert abs(position["qty"] - 0.1) < 1e-12
        assert position["long_qty"] == 0.3
        assert position["short_qty"] == 0.2
        assert len(position["positions"]) == 2

    asyncio.run(run())


def test_binance_adapter_places_market_order_with_position_side() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        order = await client.place_market_order("BTCUSDT", "SELL", 0.3, reduce_only=True, position_side="LONG")

        assert order["positionSide"] == "LONG"
        assert raw.created_orders[-1]["positionSide"] == "LONG"
        assert "reduceOnly" not in raw.created_orders[-1]

    asyncio.run(run())


def test_binance_adapter_omits_false_reduce_only_for_market_open() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        await client.place_market_order("BTCUSDT", "BUY", 0.3, reduce_only=False)

        assert "reduceOnly" not in raw.created_orders[-1]

    asyncio.run(run())


def test_binance_adapter_places_market_order_with_client_id() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        order = await client.place_market_order("BTCUSDT", "SELL", 0.3, reduce_only=True, client_id="close-cid")

        assert order["newClientOrderId"] == "close-cid"
        assert raw.created_orders[-1]["newClientOrderId"] == "close-cid"

    asyncio.run(run())


def test_binance_adapter_places_limit_order_with_position_side() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        order = await client.place_limit_order_post_only(
            "BTCUSDT",
            "BUY",
            100.0,
            1.0,
            "cid",
            position_side="LONG",
        )

        assert order["positionSide"] == "LONG"
        assert raw.created_orders[-1]["positionSide"] == "LONG"

    asyncio.run(run())


def test_binance_adapter_supports_misspelled_commission_method_name() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(MisspelledCommissionMethodClient())

        assert await client.get_commission_rate("AAPLUSDT") == {"maker": 0.0, "taker": 0.0005}

    asyncio.run(run())


def test_binance_adapter_tests_limit_order_post_only_without_creating_order() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        response = await client.test_limit_order_post_only("BTCUSDT", "BUY", 100.0, 1.0, "cid")

        assert response["orderId"] == 0
        assert raw.created_orders == [
            {
                "test": True,
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "LIMIT",
                "timeInForce": "GTX",
                "quantity": 1.0,
                "price": 100.0,
                "newClientOrderId": "cid",
            }
        ]

    asyncio.run(run())


def test_binance_adapter_tests_market_and_stop_orders_without_creating_order() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        market = await client.test_market_order("BTCUSDT", "SELL", 1.0, reduce_only=True)
        stop = await client.test_stop_market_order("BTCUSDT", "SELL", 90.0, "stop-cid", close_position=True)

        assert market["orderId"] == 0
        assert stop["orderId"] == 0
        assert raw.created_orders == [
            {
                "test": True,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "MARKET",
                "quantity": 1.0,
                "reduceOnly": True,
            },
            {
                "test": True,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "stopPrice": 90.0,
                "closePosition": True,
                "newClientOrderId": "stop-cid",
            },
        ]

    asyncio.run(run())


def test_binance_adapter_manages_futures_listen_key() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())

        listen_key = await client.create_futures_listen_key()
        keepalive = await client.keepalive_futures_listen_key(listen_key)
        close = await client.close_futures_listen_key(listen_key)

        assert listen_key == "listen-key"
        assert keepalive == {"listenKey": "listen-key"}
        assert close == {}

    asyncio.run(run())


def test_binance_adapter_manages_algo_stop_market_orders() -> None:
    async def run() -> None:
        raw = FakeBinanceAsyncClient()
        client = BinanceFuturesClient(raw)

        assert await client.get_position_mode() == {"dualSidePosition": True}
        order = await client.place_algo_stop_market_order(
            "BTCUSDT",
            "SELL",
            "SHORT",
            90.0,
            1.0,
            "algo-cid",
        )
        open_orders = await client.get_open_algo_orders("BTCUSDT")
        cancel = await client.cancel_algo_order("BTCUSDT", 456)

        assert order["algoId"] == 456
        assert raw.created_orders[-1] == {
            "algo": True,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "SHORT",
            "type": "STOP_MARKET",
            "algoType": "CONDITIONAL",
            "triggerPrice": 90.0,
            "quantity": 1.0,
            "workingType": "MARK_PRICE",
            "clientAlgoId": "algo-cid",
            "newOrderRespType": "ACK",
        }
        assert open_orders[0]["algoId"] == 456
        assert cancel["code"] == "200"

    asyncio.run(run())


def test_binance_adapter_recovers_algo_stop_after_unknown_create() -> None:
    async def run() -> None:
        raw = AlgoCreateStatusUnknownClient()
        client = BinanceFuturesClient(raw, retry_attempts=1)

        order = await client.place_algo_stop_market_order(
            "BTCUSDT",
            "SELL",
            "SHORT",
            90.0,
            1.0,
            "algo-cid",
        )

        assert order["algoId"] == 456
        assert order["clientAlgoId"] == "algo-cid"
        assert order["quantity"] == 1.0
        assert raw.created_orders[-1]["clientAlgoId"] == "algo-cid"

    asyncio.run(run())


def test_binance_adapter_falls_back_to_algo_close_position_stop_market_order() -> None:
    async def run() -> None:
        raw = UnsupportedStopMarketClient()
        client = BinanceFuturesClient(raw)

        order = await client.place_stop_market_order("BTCUSDT", "SELL", 90.0, "stop-cid", close_position=True)

        assert order["orderId"] == "456"
        assert order["algoId"] == 456
        assert order["clientOrderId"] == "stop-cid"
        assert order["clientAlgoId"] == "stop-cid"
        assert order["positionSide"] == "LONG"
        assert order["closePosition"] is True
        assert raw.created_orders == [
            {
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "STOP_MARKET",
                "stopPrice": 90.0,
                "closePosition": True,
                "newClientOrderId": "stop-cid",
            },
            {
                "algo": True,
                "symbol": "BTCUSDT",
                "side": "SELL",
                "positionSide": "LONG",
                "type": "STOP_MARKET",
                "algoType": "CONDITIONAL",
                "triggerPrice": 90.0,
                "closePosition": True,
                "workingType": "MARK_PRICE",
                "clientAlgoId": "stop-cid",
                "newOrderRespType": "ACK",
            },
        ]

    asyncio.run(run())


def test_binance_adapter_recovers_algo_close_position_after_unknown_create() -> None:
    async def run() -> None:
        raw = AlgoCreateStatusUnknownAfterStopFallbackClient()
        client = BinanceFuturesClient(raw, retry_attempts=1)

        order = await client.place_stop_market_order("BTCUSDT", "SELL", 90.0, "stop-cid", close_position=True)

        assert order["orderId"] == "456"
        assert order["algoId"] == 456
        assert order["clientOrderId"] == "stop-cid"
        assert order["clientAlgoId"] == "stop-cid"
        assert order["positionSide"] == "LONG"
        assert order["closePosition"] is True
        assert raw.created_orders[-1]["clientAlgoId"] == "stop-cid"

    asyncio.run(run())


def test_binance_adapter_cancel_order_can_cancel_algo_order_id() -> None:
    async def run() -> None:
        raw = CancelOrderFailsWithOpenAlgoClient()
        client = BinanceFuturesClient(raw)

        await client.cancel_order("BTCUSDT", "456")

        assert raw.cancelled_algo_ids == [456]

    asyncio.run(run())


def test_binance_adapter_cancel_all_orders_cancels_open_algo_orders() -> None:
    async def run() -> None:
        raw = CancelOrderFailsWithOpenAlgoClient()
        client = BinanceFuturesClient(raw)

        await client.cancel_all_orders("BTCUSDT")

        assert raw.cancelled_algo_ids == [456]

    asyncio.run(run())


def test_binance_adapter_does_not_retry_order_create_when_status_unknown() -> None:
    async def run() -> None:
        cases = [
            lambda client: client.place_limit_order_post_only("BTCUSDT", "BUY", 100.0, 0.1, "limit-cid"),
            lambda client: client.place_market_order("BTCUSDT", "BUY", 0.1, reduce_only=False, client_id="market-cid"),
            lambda client: client.place_stop_market_order("BTCUSDT", "SELL", 90.0, "stop-cid"),
        ]

        for call in cases:
            raw = OrderCreateStatusUnknownClient()
            client = BinanceFuturesClient(raw, retry_attempts=3, retry_delay_seconds=0, retry_sleep=lambda seconds: None)
            try:
                await call(client)
            except RuntimeError as exc:
                assert "execution status unknown" in str(exc)
            else:
                raise AssertionError("status unknown create should surface for caller reconciliation")
            assert len(raw.created_orders) == 1

    asyncio.run(run())


def test_binance_adapter_does_not_retry_algo_create_when_status_unknown() -> None:
    async def run() -> None:
        raw = AlgoCreateStatusUnknownClient()
        client = BinanceFuturesClient(raw, retry_attempts=3, retry_delay_seconds=0, retry_sleep=lambda seconds: None)

        order = await client.place_algo_stop_market_order("BTCUSDT", "SELL", "SHORT", 90.0, 1.0, "algo-cid")

        assert order["algoId"] == 456
        assert len(raw.created_orders) == 1
        assert raw.created_orders[0]["clientAlgoId"] == "algo-cid"

    asyncio.run(run())


def test_binance_adapter_retries_transient_rest_failures() -> None:
    async def run() -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        raw = FlakyBalanceClient(failures_before_success=1)
        client = BinanceFuturesClient(raw, retry_attempts=3, retry_delay_seconds=0.5, retry_sleep=fake_sleep)

        balance = await client.get_account_balance()

        assert balance == 123.45
        assert raw.calls == 2
        assert sleeps == [0.5]

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_exchange_info_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "exchange info"),
            ({}, "symbols"),
            ({"symbols": "bad"}, "symbols"),
            ({"symbols": ["bad"]}, "symbol row"),
            ({"symbols": [{}]}, "symbol"),
            ({"symbols": [{"symbol": ""}]}, "symbol"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidExchangeInfoClient(response))
            try:
                await client.get_symbols()
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid exchange info response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_non_finite_or_non_positive_klines() -> None:
    async def run() -> None:
        invalid_rows = [
            [1, "nan", "101", "99", "100.5", "10", 2],
            [1, "100", "inf", "99", "100.5", "10", 2],
            [1, "100", "101", "0", "100.5", "10", 2],
            [1, "100", "101", "99", "-1", "10", 2],
            [1, "100", "101", "99", "100.5", "-1", 2],
            [1, "100", "101", "99", "100.5"],
        ]
        for row in invalid_rows:
            client = BinanceFuturesClient(InvalidKlinesClient(row))
            try:
                await client.get_klines("AAPLUSDT", "1m", 1)
            except ValueError as exc:
                assert "kline" in str(exc)
            else:
                raise AssertionError("invalid kline should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_klines_response_shape() -> None:
    async def run() -> None:
        cases = [
            ("bad", "klines"),
            (["bad"], "kline row"),
            ([{"open": "100"}], "kline row"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidKlinesResponseClient(response))
            try:
                await client.get_klines("AAPLUSDT", "1m", 1)
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid klines response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_leverage_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "object"),
            ({"symbol": "MSFTUSDT", "leverage": 10}, "symbol"),
            ({"symbol": "AAPLUSDT"}, "leverage"),
            ({"symbol": "AAPLUSDT", "leverage": 5}, "leverage"),
            ({"symbol": "AAPLUSDT", "leverage": "nan"}, "leverage"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidLeverageResponseClient(response))
            try:
                await client.set_leverage("AAPLUSDT", 10)
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid leverage response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_margin_type_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "object"),
            ({"code": 400, "msg": "bad request"}, "code"),
            ({"symbol": "MSFTUSDT", "marginType": "ISOLATED"}, "symbol"),
            ({"symbol": "AAPLUSDT", "marginType": "CROSSED"}, "margin type"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidMarginTypeResponseClient(response))
            try:
                await client.set_margin_type("AAPLUSDT", "ISOLATED")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid margin type response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_inconsistent_klines() -> None:
    async def run() -> None:
        invalid_rows = [
            [1, "98.5", "101", "99", "100.5", "10", 2],
            [1, "101.5", "101", "99", "100.5", "10", 2],
            [1, "100", "98", "99", "100.5", "10", 2],
            [1, "100", "101", "99", "101.5", "10", 2],
            [1, "100", "101", "99", "98.5", "10", 2],
        ]
        for row in invalid_rows:
            client = BinanceFuturesClient(InvalidKlinesClient(row))
            try:
                await client.get_klines("AAPLUSDT", "1m", 1)
            except ValueError as exc:
                assert "kline price relationship" in str(exc)
            else:
                raise AssertionError("inconsistent kline should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_non_finite_funding_rate() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(InvalidFundingRateClient())

        try:
            await client.get_funding_rate("AAPLUSDT")
        except ValueError as exc:
            assert "funding rate" in str(exc)
        else:
            raise AssertionError("non-finite funding rate should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_account_balance_response_shape() -> None:
    async def run() -> None:
        cases = [
            ("bad", "account balance"),
            (["bad"], "balance row"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidAccountBalanceResponseClient(response))
            try:
                await client.get_account_balance()
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid account balance response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_funding_rate_response_shape() -> None:
    async def run() -> None:
        cases = [
            ("bad", "funding rate"),
            (["bad"], "funding rate row"),
            ([{"symbol": "MSFTUSDT", "fundingRate": "0.0001"}], "symbol"),
            ([{"symbol": "AAPLUSDT"}], "funding rate"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidFundingRateResponseClient(response))
            try:
                await client.get_funding_rate("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid funding rate response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_ticker_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "ticker"),
            ({"symbol": "MSFTUSDT", "quoteVolume": "1000"}, "symbol"),
            ({"symbol": "AAPLUSDT"}, "quoteVolume"),
            ({"symbol": "AAPLUSDT", "quoteVolume": "nan"}, "quoteVolume"),
            ({"symbol": "AAPLUSDT", "quoteVolume": "-1"}, "quoteVolume"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidTickerClient(response))
            try:
                await client.get_24h_ticker("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid ticker response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_orderbook_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "orderbook"),
            ({"bids": "bad", "asks": [["101", "1"]]}, "bids"),
            ({"bids": [["100", "1"]], "asks": "bad"}, "asks"),
            ({"bids": [["0", "1"]], "asks": [["101", "1"]]}, "bids.price"),
            ({"bids": [["100", "-1"]], "asks": [["101", "1"]]}, "bids.qty"),
            ({"bids": [["100"]], "asks": [["101", "1"]]}, "bids"),
            ({"bids": [["102", "1"]], "asks": [["101", "1"]]}, "spread"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidOrderBookClient(response))
            try:
                await client.get_orderbook_depth("AAPLUSDT", 5)
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid orderbook response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_open_orders_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "open orders"),
            (["bad"], "open order"),
            ([{"symbol": "MSFTUSDT", "orderId": "1"}], "symbol"),
            ([{"symbol": "AAPLUSDT"}], "order id"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidOpenOrdersClient(response))
            try:
                await client.get_open_orders("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid open orders response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_order_lookup_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "order lookup"),
            ({"symbol": "MSFTUSDT", "orderId": "1", "status": "FILLED"}, "symbol"),
            ({"symbol": "AAPLUSDT", "status": "FILLED"}, "order id"),
            ({"symbol": "AAPLUSDT", "orderId": "1"}, "status"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidOrderLookupClient(response))
            try:
                await client.get_order("AAPLUSDT", "1", "cid")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid order lookup response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_cancel_order_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "cancel order"),
            ({"code": 400, "msg": "bad request"}, "code"),
            ({"symbol": "MSFTUSDT", "orderId": "1"}, "symbol"),
            ({"symbol": "AAPLUSDT"}, "order id"),
            ({"symbol": "AAPLUSDT", "orderId": "2"}, "order id"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidCancelOrderClient(response))
            try:
                await client.cancel_order("AAPLUSDT", "1")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid cancel order response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_cancel_all_orders_response() -> None:
    async def run() -> None:
        cases = [
            ("bad", "cancel all orders"),
            ({"code": 400, "msg": "bad request"}, "code"),
            ({"symbol": "MSFTUSDT"}, "symbol"),
            ({"msg": "done"}, "success"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidCancelAllOrdersClient(response))
            try:
                await client.cancel_all_orders("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid cancel all orders response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_requires_usdt_balance_field() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(MissingUsdtBalanceClient())

        try:
            await client.get_account_balance()
        except ValueError as exc:
            assert "USDT balance" in str(exc)
        else:
            raise AssertionError("missing USDT balance should fail closed")

    asyncio.run(run())


def test_binance_adapter_accepts_balance_when_available_balance_is_missing() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(BalanceOnlyClient())

        assert await client.get_account_balance() == 321.0

    asyncio.run(run())


def test_binance_adapter_rejects_non_finite_usdt_balance() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(InvalidUsdtBalanceClient())

        try:
            await client.get_account_balance()
        except ValueError as exc:
            assert "USDT balance" in str(exc)
        else:
            raise AssertionError("non-finite USDT balance should fail closed")

    asyncio.run(run())


def test_binance_adapter_requires_maker_commission_rate() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(MissingMakerCommissionClient())

        try:
            await client.get_commission_rate("AAPLUSDT")
        except ValueError as exc:
            assert "maker commission" in str(exc)
        else:
            raise AssertionError("missing maker commission should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_commission_rate_response_shape() -> None:
    async def run() -> None:
        cases = [
            ("bad", "commission"),
            ({"symbol": "MSFTUSDT", "makerCommissionRate": "0", "takerCommissionRate": "0.0005"}, "symbol"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidCommissionRateResponseClient(response))
            try:
                await client.get_commission_rate("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid commission response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_rejects_non_finite_commission_rates() -> None:
    async def run() -> None:
        for raw_client, label in (
            (InvalidMakerCommissionClient(), "maker commission"),
            (InvalidTakerCommissionClient(), "taker commission"),
        ):
            client = BinanceFuturesClient(raw_client)
            try:
                await client.get_commission_rate("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"non-finite {label} should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_negative_commission_rates() -> None:
    async def run() -> None:
        for raw_client, label in (
            (NegativeMakerCommissionClient(), "maker commission"),
            (NegativeTakerCommissionClient(), "taker commission"),
        ):
            client = BinanceFuturesClient(raw_client)
            try:
                await client.get_commission_rate("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"negative {label} should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_invalid_position_response_shape() -> None:
    async def run() -> None:
        cases = [
            ("bad", "position"),
            (["bad"], "position row"),
            ([{"symbol": "MSFTUSDT", "positionAmt": "1"}], "symbol"),
            ([{"symbol": "AAPLUSDT", "positionSide": "BAD", "positionAmt": "1"}], "positionSide"),
        ]

        for response, label in cases:
            client = BinanceFuturesClient(InvalidPositionResponseClient(response))
            try:
                await client.get_position("AAPLUSDT")
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"invalid position response should fail closed: {label}")

    asyncio.run(run())


def test_binance_adapter_requires_position_amount_when_position_row_exists() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(MissingPositionAmountClient())

        try:
            await client.get_position("AAPLUSDT")
        except ValueError as exc:
            assert "positionAmt" in str(exc)
        else:
            raise AssertionError("missing position amount should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_non_finite_position_amount() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(InvalidPositionAmountClient())

        try:
            await client.get_position("AAPLUSDT")
        except ValueError as exc:
            assert "positionAmt" in str(exc)
        else:
            raise AssertionError("non-finite position amount should fail closed")

    asyncio.run(run())


def test_binance_adapter_does_not_retry_post_only_rejections() -> None:
    async def run() -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        raw = RejectPostOnlyClient()
        client = BinanceFuturesClient(raw, retry_attempts=3, retry_delay_seconds=0.5, retry_sleep=fake_sleep)

        try:
            await client.place_limit_order_post_only("AAPLUSDT", "BUY", 100.0, 1.0, "cid")
        except RuntimeError as exc:
            assert "Post only rejected" in str(exc)
        else:
            raise AssertionError("post-only rejection should propagate without retry")

        assert raw.calls == 1
        assert sleeps == []

    asyncio.run(run())


def test_binance_adapter_requires_order_id_in_create_order_response() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(MissingOrderIdClient())

        try:
            await client.place_market_order("AAPLUSDT", "SELL", 1.0)
        except ValueError as exc:
            assert "order id" in str(exc)
        else:
            raise AssertionError("create order response without order id should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_mismatched_create_order_response_fields() -> None:
    async def run() -> None:
        cases = [
            (
                {"symbol": "MSFTUSDT"},
                lambda client: client.place_limit_order_post_only("AAPLUSDT", "BUY", 100.0, 1.0, "cid"),
                "symbol",
            ),
            (
                {"side": "SELL"},
                lambda client: client.place_limit_order_post_only("AAPLUSDT", "BUY", 100.0, 1.0, "cid"),
                "side",
            ),
            (
                {"type": "MARKET"},
                lambda client: client.place_limit_order_post_only("AAPLUSDT", "BUY", 100.0, 1.0, "cid"),
                "type",
            ),
            (
                {"clientOrderId": "other-cid"},
                lambda client: client.place_limit_order_post_only("AAPLUSDT", "BUY", 100.0, 1.0, "cid"),
                "client id",
            ),
            (
                {"timeInForce": "GTC"},
                lambda client: client.place_limit_order_post_only("AAPLUSDT", "BUY", 100.0, 1.0, "cid"),
                "timeInForce",
            ),
            (
                {"reduceOnly": False},
                lambda client: client.place_market_order("AAPLUSDT", "SELL", 1.0, reduce_only=True),
                "reduceOnly",
            ),
            (
                {"closePosition": False},
                lambda client: client.place_stop_market_order("AAPLUSDT", "SELL", 98.0, "stop-cid"),
                "closePosition",
            ),
            (
                {"positionSide": "SHORT"},
                lambda client: client.place_market_order(
                    "AAPLUSDT",
                    "SELL",
                    1.0,
                    reduce_only=True,
                    position_side="LONG",
                ),
                "positionSide",
            ),
        ]

        for overrides, call, label in cases:
            client = BinanceFuturesClient(MismatchedOrderEchoClient(overrides))
            try:
                await call(client)
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"mismatched {label} should fail closed")

    asyncio.run(run())


def test_binance_adapter_rejects_mismatched_algo_order_response_fields() -> None:
    async def run() -> None:
        cases = [
            ({"clientAlgoId": "other-cid"}, "client algo id"),
            ({"symbol": "ETHUSDT"}, "symbol"),
            ({"side": "BUY"}, "side"),
            ({"positionSide": "LONG"}, "positionSide"),
            ({"orderType": "LIMIT"}, "type"),
            ({"algoType": "TRAILING_STOP_MARKET"}, "algoType"),
            ({"workingType": "CONTRACT_PRICE"}, "workingType"),
            ({"triggerPrice": "91.0"}, "triggerPrice"),
            ({"quantity": "2.0"}, "quantity"),
        ]

        for overrides, label in cases:
            client = BinanceFuturesClient(MismatchedAlgoOrderEchoClient(overrides))
            try:
                await client.place_algo_stop_market_order(
                    "BTCUSDT",
                    "SELL",
                    "SHORT",
                    90.0,
                    1.0,
                    "algo-cid",
                )
            except ValueError as exc:
                assert label in str(exc)
            else:
                raise AssertionError(f"mismatched algo {label} should fail closed")

    asyncio.run(run())


def test_binance_adapter_does_not_retry_noop_margin_type_change() -> None:
    async def run() -> None:
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        raw = NoNeedMarginTypeClient()
        client = BinanceFuturesClient(raw, retry_attempts=3, retry_delay_seconds=0.5, retry_sleep=fake_sleep)

        await client.set_margin_type("AAPLUSDT", "ISOLATED")

        assert raw.calls == 1
        assert sleeps == []

    asyncio.run(run())


def test_binance_adapter_raises_after_retry_exhaustion() -> None:
    async def run() -> None:
        raw = FlakyBalanceClient(failures_before_success=3)
        client = BinanceFuturesClient(raw, retry_attempts=2, retry_delay_seconds=0, retry_sleep=lambda seconds: None)

        try:
            await client.get_account_balance()
        except RuntimeError as exc:
            assert "temporary network failure" in str(exc)
        else:
            raise AssertionError("retry exhaustion should raise")
        assert raw.calls == 2

    asyncio.run(run())


def test_binance_adapter_redacts_non_json_transport_errors() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(NonJsonErrorClient(), retry_attempts=1)

        try:
            await client.get_account_balance()
        except RuntimeError as exc:
            message = str(exc)
            assert "non-JSON or raw transport error" in message
            assert "502 Bad Gateway" in message
            assert "signature=" not in message
            assert "secret" not in message
            assert exc.__cause__ is None
        else:
            raise AssertionError("non-JSON Binance error should fail closed")

    asyncio.run(run())


def test_parse_order_trade_update_only_returns_filled_orders() -> None:
    message = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1780000000000,
        "T": 1780000001000,
        "o": {
            "s": "AAPLUSDT",
            "c": "qg-1-2-buy",
            "i": 123,
            "S": "BUY",
            "X": "FILLED",
            "ap": "100.25",
            "z": "0.5",
        },
    }

    event = parse_order_trade_update(message)

    assert event is not None
    assert event["symbol"] == "AAPLUSDT"
    assert event["client_id"] == "qg-1-2-buy"
    assert event["order_id"] == "123"
    assert event["side"] == "BUY"
    assert event["status"] == "FILLED"
    assert event["price"] == 100.25
    assert event["qty"] == 0.5


def test_parse_order_trade_update_returns_partial_fill_events() -> None:
    message = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 1780000000000,
        "o": {
            "s": "AAPLUSDT",
            "c": "qg-1-2-buy",
            "i": 123,
            "S": "BUY",
            "X": "PARTIALLY_FILLED",
            "t": 456,
            "ap": "100.25",
            "z": "0.2",
        },
    }

    event = parse_order_trade_update(message)

    assert event is not None
    assert event["status"] == "PARTIALLY_FILLED"
    assert event["trade_id"] == "456"
    assert event["qty"] == 0.2


def test_parse_order_trade_update_prefers_last_fill_qty_over_cumulative_qty() -> None:
    event = parse_order_trade_update(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1780000000000,
            "o": {
                "s": "AAPLUSDT",
                "c": "qg-1-2-buy",
                "i": 123,
                "S": "BUY",
                "X": "FILLED",
                "ap": "100.25",
                "l": "0.2",
                "z": "0.5",
            },
        }
    )

    assert event is not None
    assert event["qty"] == 0.2


def test_parse_order_trade_update_pairs_last_fill_qty_with_last_fill_price() -> None:
    event = parse_order_trade_update(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1780000000000,
            "o": {
                "s": "AAPLUSDT",
                "c": "qg-1-2-buy",
                "i": 123,
                "S": "BUY",
                "X": "FILLED",
                "ap": "100.25",
                "L": "100.75",
                "l": "0.2",
                "z": "0.5",
            },
        }
    )

    assert event is not None
    assert event["qty"] == 0.2
    assert event["price"] == 100.75


def test_parse_order_trade_update_does_not_fallback_when_last_fill_fields_are_invalid() -> None:
    base_order = {
        "s": "AAPLUSDT",
        "c": "qg-1-2-buy",
        "i": 123,
        "S": "BUY",
        "X": "FILLED",
        "ap": "100.25",
        "z": "0.5",
    }
    invalid_orders = [
        {"l": "bad"},
        {"l": "nan"},
        {"l": "0"},
        {"l": "-0.1"},
        {"l": "0.2", "L": "bad"},
        {"l": "0.2", "L": "nan"},
        {"l": "0.2", "L": "0"},
    ]

    for invalid_order in invalid_orders:
        assert parse_order_trade_update(
            {
                "e": "ORDER_TRADE_UPDATE",
                "E": 1780000000000,
                "o": {**base_order, **invalid_order},
            }
        ) is None


def test_parse_order_trade_update_ignores_fills_without_valid_qty_or_price() -> None:
    base_order = {
        "s": "AAPLUSDT",
        "c": "qg-1-2-buy",
        "i": 123,
        "S": "BUY",
    }
    invalid_orders = [
        {"X": "FILLED", "ap": "100.25"},
        {"X": "FILLED", "ap": "100.25", "l": "0", "z": "0", "q": "0"},
        {"X": "FILLED", "ap": "100.25", "z": "bad"},
        {"X": "FILLED", "ap": "100.25", "z": "nan"},
        {"X": "FILLED", "ap": "100.25", "z": "inf"},
        {"X": "FILLED", "z": "0.2"},
        {"X": "FILLED", "z": "0.2", "ap": "bad"},
        {"X": "FILLED", "z": "0.2", "ap": "nan"},
        {"X": "FILLED", "z": "0.2", "ap": "-inf"},
        {"X": "FILLED", "z": "0.2", "L": "0", "p": "0", "ap": "0"},
        {"X": "FILLED", "ap": "100.25", "z": "0.2", "s": ""},
        {"X": "FILLED", "ap": "100.25", "z": "0.2", "S": ""},
        {"X": "FILLED", "ap": "100.25", "z": "0.2", "S": "HOLD"},
        {"X": "FILLED", "ap": "100.25", "z": "0.2", "c": ""},
        {"X": "FILLED", "ap": "100.25", "z": "0.2", "i": ""},
        {"X": "PARTIALLY_FILLED", "ap": "100.25"},
        {"X": "PARTIALLY_FILLED", "z": "0.2"},
    ]

    for invalid_order in invalid_orders:
        message = {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1780000000000,
            "o": {**base_order, **invalid_order},
        }

        assert parse_order_trade_update(message) is None

    assert parse_order_trade_update({"e": "ORDER_TRADE_UPDATE", "o": "bad"}) is None


def test_parse_order_trade_update_tolerates_invalid_event_time() -> None:
    event = parse_order_trade_update(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": "bad",
            "o": {
                "s": "AAPLUSDT",
                "c": "qg-1-2-buy",
                "i": 123,
                "S": "BUY",
                "X": "FILLED",
                "ap": "100.25",
                "z": "0.5",
            },
        }
    )

    assert event is not None
    assert event["trade_time"] is not None


def test_parse_order_trade_update_supports_multiplex_payload() -> None:
    event = parse_order_trade_update(
        {
            "stream": "aaplusdt@user",
            "data": {
                "e": "ORDER_TRADE_UPDATE",
                "E": 1780000000000,
                "o": {
                    "s": "AAPLUSDT",
                    "c": "cid",
                    "i": 1,
                    "S": "SELL",
                    "X": "FILLED",
                    "ap": "101",
                    "z": "0.3",
                },
            },
        }
    )

    assert event is not None
    assert event["symbol"] == "AAPLUSDT"
    assert event["side"] == "SELL"
    assert event["price"] == 101.0


def test_parse_order_trade_update_ignores_open_orders() -> None:
    assert parse_order_trade_update({"e": "ORDER_TRADE_UPDATE", "o": {"X": "NEW"}}) is None


def test_parse_order_trade_update_ignores_non_dict_messages() -> None:
    assert parse_order_trade_update("bad message") is None
    assert parse_order_trade_update(None) is None


def test_parse_price_update_supports_mark_price_and_book_ticker() -> None:
    mark = parse_price_update({"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "101.5"})
    book = parse_price_update({"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "100", "a": "102"})

    assert mark is not None
    assert mark["symbol"] == "AAPLUSDT"
    assert mark["price"] == 101.5
    assert book is not None
    assert book["price"] == 101.0


def test_parse_price_update_ignores_invalid_prices() -> None:
    invalid_messages = [
        {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT"},
        {"e": "markPriceUpdate", "E": 1780000000000, "p": "101.5"},
        {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "0"},
        {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "-1"},
        {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "bad"},
        {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "nan"},
        {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "inf"},
        {"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "100"},
        {"e": "bookTicker", "E": 1780000000000, "b": "100", "a": "102"},
        {"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "0", "a": "102"},
        {"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "100", "a": "-1"},
        {"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "bad", "a": "102"},
        {"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "nan", "a": "102"},
        {"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "100", "a": "-inf"},
        {"e": "bookTicker", "E": 1780000000000, "s": "AAPLUSDT", "b": "103", "a": "102"},
    ]

    for message in invalid_messages:
        assert parse_price_update(message) is None


def test_parse_price_update_tolerates_invalid_event_time() -> None:
    event = parse_price_update({"e": "markPriceUpdate", "E": "bad", "s": "AAPLUSDT", "p": "101.5"})

    assert event is not None
    assert event["event_time"] is not None


def test_parse_price_update_supports_multiplex_payload() -> None:
    event = parse_price_update(
        {
            "stream": "aaplusdt@markPrice@1s",
            "data": {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "99.5"},
        }
    )

    assert event is not None
    assert event["symbol"] == "AAPLUSDT"
    assert event["price"] == 99.5


def test_parse_price_update_supports_futures_ticker_payload() -> None:
    event = parse_price_update(
        {
            "stream": "btcusdt@ticker",
            "data": {"e": "24hrTicker", "E": 1780000000000, "s": "BTCUSDT", "c": "63524.00"},
        }
    )

    assert event is not None
    assert event["symbol"] == "BTCUSDT"
    assert event["price"] == 63524.0


def test_parse_price_update_ignores_non_dict_messages() -> None:
    assert parse_price_update("bad message") is None
    assert parse_price_update(None) is None


def test_symbol_rules_from_exchange_info_extracts_filters() -> None:
    rules = symbol_rules_from_exchange_info(
        {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.05"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.1"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]
        }
    )

    assert rules == {"tick_size": 0.05, "step_size": 0.01, "min_qty": 0.1, "min_notional": 5.0}


def test_symbol_rules_from_exchange_info_supports_notional_min_notional_key() -> None:
    rules = symbol_rules_from_exchange_info(
        {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.05"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.1"},
                {"filterType": "NOTIONAL", "minNotional": "10"},
            ]
        }
    )

    assert rules["min_notional"] == 10.0


def test_symbol_rules_from_exchange_info_requires_price_filter() -> None:
    try:
        symbol_rules_from_exchange_info(
            {
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.1"},
                ]
            }
        )
    except ValueError as exc:
        assert "PRICE_FILTER.tickSize" in str(exc)
    else:
        raise AssertionError("missing price filter should fail closed")


def test_symbol_rules_from_exchange_info_requires_lot_size_filter() -> None:
    try:
        symbol_rules_from_exchange_info(
            {
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.05"},
                ]
            }
        )
    except ValueError as exc:
        assert "LOT_SIZE.stepSize" in str(exc)
    else:
        raise AssertionError("missing lot size filter should fail closed")


def test_symbol_rules_from_exchange_info_rejects_non_positive_rules() -> None:
    try:
        symbol_rules_from_exchange_info(
            {
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.1"},
                ]
            }
        )
    except ValueError as exc:
        assert "PRICE_FILTER.tickSize" in str(exc)
    else:
        raise AssertionError("non-positive tick size should fail closed")


def test_symbol_rules_from_exchange_info_rejects_non_finite_rules() -> None:
    invalid_payloads = [
        {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "nan"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.1"},
            ]
        },
        {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.05"},
                {"filterType": "LOT_SIZE", "stepSize": "inf", "minQty": "0.1"},
            ]
        },
        {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.05"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.1"},
                {"filterType": "MIN_NOTIONAL", "notional": "-inf"},
            ]
        },
    ]

    for payload in invalid_payloads:
        try:
            symbol_rules_from_exchange_info(payload)
        except ValueError as exc:
            assert "invalid" in str(exc)
        else:
            raise AssertionError("non-finite trading rule should fail closed")


def test_symbol_rules_from_exchange_info_rejects_invalid_filter_shape() -> None:
    invalid_payloads = [
        ("bad", "symbol info"),
        ({}, "filters"),
        ({"filters": "bad"}, "filters"),
        ({"filters": ["bad"]}, "filter row"),
        ({"filters": [{"tickSize": "0.05"}]}, "filterType"),
        ({"filters": [{"filterType": ""}]}, "filterType"),
    ]

    for payload, label in invalid_payloads:
        try:
            symbol_rules_from_exchange_info(payload)
        except ValueError as exc:
            assert label in str(exc)
        else:
            raise AssertionError(f"invalid symbol rule filters should fail closed: {label}")


def test_symbol_rules_from_exchange_info_falls_back_to_market_lot_size() -> None:
    rules = symbol_rules_from_exchange_info(
        {
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.05"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.02", "minQty": "0.2"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ]
        }
    )

    assert rules["step_size"] == 0.02
    assert rules["min_qty"] == 0.2


def test_run_user_stream_dispatches_filled_events() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())
        events: list[dict[str, Any]] = []

        await client.run_user_stream(
            events.append,
            socket_factory=lambda: FakeSocket(
                [
                    {"e": "ORDER_TRADE_UPDATE", "o": {"X": "NEW"}},
                    {
                        "e": "ORDER_TRADE_UPDATE",
                        "E": 1780000000000,
                        "o": {
                            "s": "AAPLUSDT",
                            "c": "cid",
                            "i": 1,
                            "S": "BUY",
                            "X": "FILLED",
                            "ap": "100",
                            "z": "0.2",
                        },
                    },
                ]
            ),
        )

        assert len(events) == 1
        assert events[0]["client_id"] == "cid"

    asyncio.run(run())


def test_run_user_stream_propagates_handler_errors() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())
        calls = 0

        def factory():
            nonlocal calls
            calls += 1
            return FakeSocket(
                [
                    {
                        "e": "ORDER_TRADE_UPDATE",
                        "E": 1780000000000,
                        "o": {
                            "s": "AAPLUSDT",
                            "c": "cid",
                            "i": 1,
                            "S": "BUY",
                            "X": "FILLED",
                            "ap": "100",
                            "z": "0.2",
                        },
                    }
                ]
            )

        async def failing_handler(event):
            raise RuntimeError("handler failed")

        try:
            await client.run_user_stream(
                failing_handler,
                socket_factory=factory,
                reconnect_delay_seconds=0,
                max_reconnects=1,
            )
        except RuntimeError as exc:
            assert "handler failed" in str(exc)
        else:
            raise AssertionError("handler failure should propagate")

        assert calls == 1

    asyncio.run(run())


def test_run_price_stream_preserves_handler_error_when_socket_exit_breaks() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())

        async def failing_handler(event):
            raise RuntimeError("stop after first event")

        try:
            await client.run_price_stream(
                ["AAPLUSDT"],
                failing_handler,
                socket_factory=lambda: BrokenExitSocket(
                    [{"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "100"}]
                ),
                reconnect_delay_seconds=0,
                max_reconnects=0,
            )
        except RuntimeError as exc:
            assert "stop after first event" in str(exc)
        else:
            raise AssertionError("handler failure should be preserved when socket exit fails")

    asyncio.run(run())


def test_run_price_stream_reconnects_after_socket_error() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())
        calls = 0
        events: list[dict[str, Any]] = []

        def factory():
            nonlocal calls
            calls += 1
            if calls == 1:
                return FakeSocket(
                    [{"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "100"}],
                    fail_after_messages=True,
                )
            return FakeSocket(
                [{"e": "markPriceUpdate", "E": 1780000001000, "s": "AAPLUSDT", "p": "101"}]
            )

        await client.run_price_stream(
            ["AAPLUSDT"],
            events.append,
            socket_factory=factory,
            reconnect_delay_seconds=0,
            max_reconnects=1,
        )

        assert calls == 2
        assert [event["price"] for event in events] == [100.0, 101.0]

    asyncio.run(run())


def test_run_price_stream_dispatches_multiplex_events() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())
        events: list[dict[str, Any]] = []

        await client.run_price_stream(
            ["AAPLUSDT"],
            events.append,
            socket_factory=lambda: FakeSocket(
                [
                    {
                        "stream": "aaplusdt@markPrice@1s",
                        "data": {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "100"},
                    }
                ]
            ),
        )

        assert len(events) == 1
        assert events[0]["price"] == 100.0

    asyncio.run(run())


def test_run_price_stream_decodes_raw_json_text_messages() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())
        events: list[dict[str, Any]] = []

        await client.run_price_stream(
            ["BTCUSDT"],
            events.append,
            socket_factory=lambda: FakeSocket(
                ['{"e":"24hrTicker","E":1780000000000,"s":"BTCUSDT","c":"63524.00"}']
            ),
        )

        assert len(events) == 1
        assert events[0]["symbol"] == "BTCUSDT"
        assert events[0]["price"] == 63524.0

    asyncio.run(run())


def test_run_price_stream_ignores_malformed_messages_without_reconnecting() -> None:
    async def run() -> None:
        client = BinanceFuturesClient(FakeBinanceAsyncClient())
        calls = 0
        events: list[dict[str, Any]] = []

        def factory():
            nonlocal calls
            calls += 1
            return FakeSocket(
                [
                    "bad message",
                    None,
                    {"e": "markPriceUpdate", "E": 1780000000000, "s": "AAPLUSDT", "p": "100"},
                ]
            )

        await client.run_price_stream(
            ["AAPLUSDT"],
            events.append,
            socket_factory=factory,
            reconnect_delay_seconds=0,
            max_reconnects=0,
        )

        assert calls == 1
        assert [event["price"] for event in events] == [100.0]

    asyncio.run(run())
