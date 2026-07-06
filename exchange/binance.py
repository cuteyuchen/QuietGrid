from __future__ import annotations

import asyncio
import inspect
import re
from math import isfinite
from typing import Any, Callable

from exchange.base import ExchangeClient


class _HandlerError(Exception):
    pass


class BinanceFuturesClient(ExchangeClient):
    def __init__(
        self,
        client: Any,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1,
        retry_sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.retry_attempts = retry_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.retry_sleep = retry_sleep
        self._order_filled_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._price_update_callbacks: list[Callable[[dict[str, Any]], None]] = []

    @classmethod
    async def create(
        cls,
        api_key: str,
        api_secret: str,
        testnet: bool,
        proxy_config: dict[str, Any] | None = None,
    ) -> "BinanceFuturesClient":
        try:
            from binance import AsyncClient
        except ImportError as exc:
            raise RuntimeError("缺少 python-binance 依赖，请先安装 requirements.txt。") from exc

        requests_params = _requests_params(proxy_config)
        client = await AsyncClient.create(
            api_key=api_key,
            api_secret=api_secret,
            testnet=testnet,
            requests_params=requests_params,
        )
        return cls(client)

    async def close(self) -> None:
        close = getattr(self.client, "close_connection", None)
        if close is not None:
            await close()

    async def get_symbols(self) -> list[dict[str, Any]]:
        info = await self._call(self.client.futures_exchange_info)
        return _validate_exchange_info_response(info)

    async def get_symbol_rules(self, symbol: str) -> dict[str, Any]:
        for item in await self.get_symbols():
            if item.get("symbol") == symbol:
                return symbol_rules_from_exchange_info(item)
        raise ValueError(f"未找到交易规则: {symbol}")

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        response = await self._call(self.client.futures_change_leverage, symbol=symbol, leverage=leverage)
        _validate_leverage_response(response, symbol, leverage)

    async def set_margin_type(self, symbol: str, margin_type: str) -> None:
        try:
            response = await self._call(self.client.futures_change_margin_type, symbol=symbol, marginType=margin_type)
        except Exception as exc:
            if "No need to change margin type" not in str(exc):
                raise
            return
        _validate_margin_type_response(response, symbol, margin_type)

    async def get_account_balance(self) -> float:
        response = await self._call(self.client.futures_account_balance)
        return _validate_account_balance_response(response)

    async def get_position(self, symbol: str) -> dict[str, Any]:
        response = await self._call(self.client.futures_position_information, symbol=symbol)
        return _validate_position_response(response, symbol)

    async def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        response = await self._call(self.client.futures_get_open_orders, symbol=symbol)
        return _validate_open_orders_response(response, symbol)

    async def get_order(self, symbol: str, order_id: str, client_id: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"symbol": symbol}
        if client_id:
            kwargs["origClientOrderId"] = client_id
        else:
            kwargs["orderId"] = order_id
        response = await self._call(self.client.futures_get_order, **kwargs)
        return _validate_order_lookup_response(response, symbol)

    async def place_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTX",
            "quantity": qty,
            "price": price,
            "newClientOrderId": client_id,
        }
        if position_side is not None:
            kwargs["positionSide"] = position_side
        response = await self._call(
            self.client.futures_create_order,
            retry_status_unknown=False,
            **kwargs,
        )
        return _validate_create_order_response(
            response,
            symbol=symbol,
            side=side,
            order_type="LIMIT",
            client_id=client_id,
            time_in_force="GTX",
            position_side=position_side,
        )

    async def test_limit_order_post_only(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        client_id: str,
        position_side: str | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTX",
            "quantity": qty,
            "price": price,
            "newClientOrderId": client_id,
        }
        if position_side is not None:
            kwargs["positionSide"] = position_side
        response = await self._call(
            lambda **kwargs: self.client._request_futures_api("post", "order/test", True, data=kwargs),
            **kwargs,
        )
        return _validate_test_order_response(response)

    async def test_market_order(
        self, symbol: str, side: str, qty: float, reduce_only: bool = True
    ) -> dict[str, Any]:
        response = await self._call(
            lambda **kwargs: self.client._request_futures_api("post", "order/test", True, data=kwargs),
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=qty,
            reduceOnly=reduce_only,
        )
        return _validate_test_order_response(response)

    async def test_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ) -> dict[str, Any]:
        response = await self._call(
            lambda **kwargs: self.client._request_futures_api("post", "order/test", True, data=kwargs),
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            stopPrice=stop_price,
            closePosition=close_position,
            newClientOrderId=client_id,
        )
        return _validate_test_order_response(response)

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = True,
        position_side: str | None = None,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        sent_reduce_only = reduce_only if reduce_only and position_side is None else None
        kwargs: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": qty,
        }
        if sent_reduce_only is not None:
            kwargs["reduceOnly"] = sent_reduce_only
        if position_side is not None:
            kwargs["positionSide"] = position_side
        if client_id is not None:
            kwargs["newClientOrderId"] = client_id
        response = await self._call(
            self.client.futures_create_order,
            retry_status_unknown=False,
            **kwargs,
        )
        return _validate_create_order_response(
            response,
            symbol=symbol,
            side=side,
            order_type="MARKET",
            client_id=client_id,
            reduce_only=sent_reduce_only,
            position_side=position_side,
        )

    async def place_stop_market_order(
        self,
        symbol: str,
        side: str,
        stop_price: float,
        client_id: str,
        close_position: bool = True,
    ) -> dict[str, Any]:
        try:
            response = await self._call(
                self.client.futures_create_order,
                retry_status_unknown=False,
                symbol=symbol,
                side=side,
                type="STOP_MARKET",
                stopPrice=stop_price,
                closePosition=close_position,
                newClientOrderId=client_id,
            )
        except Exception as exc:
            if close_position and _is_stop_market_requires_algo_order(exc):
                return await self._place_algo_close_position_stop_market_order(
                    symbol,
                    side,
                    stop_price,
                    client_id,
                )
            raise
        return _validate_create_order_response(
            response,
            symbol=symbol,
            side=side,
            order_type="STOP_MARKET",
            client_id=client_id,
            close_position=close_position,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> None:
        try:
            response = await self._call(self.client.futures_cancel_order, symbol=symbol, orderId=order_id)
            _validate_cancel_order_response(response, symbol, order_id)
        except Exception as exc:
            if await self._cancel_open_algo_order_if_present(symbol, order_id):
                return
            raise exc

    async def cancel_all_orders(self, symbol: str) -> None:
        response = await self._call(self.client.futures_cancel_all_open_orders, symbol=symbol)
        _validate_cancel_all_orders_response(response, symbol)
        for order in await self.get_open_algo_orders(symbol):
            await self.cancel_algo_order(symbol, order["algoId"])

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
        response = await self._call(self.client.futures_klines, symbol=symbol, interval=interval, limit=limit)
        return _validate_klines_response(response)

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        response = await self._call(self.client.futures_ticker, symbol=symbol)
        return _validate_ticker_response(response, symbol)

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]:
        response = await self._call(self.client.futures_order_book, symbol=symbol, limit=limit)
        return _validate_orderbook_response(response, limit)

    async def get_funding_rate(self, symbol: str) -> float:
        response = await self._call(self.client.futures_funding_rate, symbol=symbol, limit=1)
        return _validate_funding_rate_response(response, symbol)

    async def get_commission_rate(self, symbol: str) -> dict[str, float]:
        commission_rate = getattr(self.client, "futures_commission_rate", None)
        if commission_rate is None:
            commission_rate = getattr(self.client, "futures_comission_rate", None)
        if commission_rate is None:
            raise RuntimeError("当前 python-binance 客户端不支持查询合约手续费率。")
        response = await self._call(commission_rate, symbol=symbol)
        row = _validate_commission_rate_response(response, symbol)
        return {
            "maker": _required_non_negative_float(row, ("makerCommissionRate", "maker"), "maker commission"),
            "taker": _optional_non_negative_float(row, ("takerCommissionRate", "taker"), "taker commission"),
        }

    async def create_futures_listen_key(self) -> str:
        listen_key = await self._call(self.client.futures_stream_get_listen_key)
        text = _non_empty_str_or_none(listen_key)
        if text is None:
            raise ValueError("Binance futures listen key response missing listenKey")
        return text

    async def keepalive_futures_listen_key(self, listen_key: str) -> dict[str, Any]:
        response = await self._call(self.client.futures_stream_keepalive, listenKey=listen_key)
        return _validate_listen_key_response(response, allow_empty=True)

    async def close_futures_listen_key(self, listen_key: str) -> dict[str, Any]:
        response = await self._call(self.client.futures_stream_close, listenKey=listen_key)
        return _validate_listen_key_response(response, allow_empty=True)

    async def get_position_mode(self) -> dict[str, Any]:
        response = await self._call(self.client.futures_get_position_mode)
        if not isinstance(response, dict):
            raise ValueError("Binance position mode response must be an object")
        if "dualSidePosition" not in response:
            raise ValueError("Binance position mode response missing dualSidePosition")
        return response

    async def place_algo_stop_market_order(
        self,
        symbol: str,
        side: str,
        position_side: str,
        trigger_price: float,
        qty: float,
        client_algo_id: str,
    ) -> dict[str, Any]:
        try:
            response = await self._call(
                lambda **kwargs: self.client._request_futures_api("post", "algoOrder", True, data=kwargs),
                retry_status_unknown=False,
                symbol=symbol,
                side=side,
                positionSide=position_side,
                type="STOP_MARKET",
                algoType="CONDITIONAL",
                triggerPrice=trigger_price,
                quantity=qty,
                workingType="MARK_PRICE",
                clientAlgoId=client_algo_id,
                newOrderRespType="ACK",
            )
        except Exception as exc:
            recovered = await self._recover_open_algo_order_by_client_id_after_create_exception(
                symbol, client_algo_id, exc
            )
            if recovered is not None:
                response = recovered
            else:
                raise
        return _validate_algo_order_response(
            response,
            symbol=symbol,
            side=side,
            position_side=position_side,
            trigger_price=trigger_price,
            qty=qty,
            client_algo_id=client_algo_id,
        )

    async def _place_algo_close_position_stop_market_order(
        self,
        symbol: str,
        side: str,
        trigger_price: float,
        client_algo_id: str,
    ) -> dict[str, Any]:
        position_side = await self._close_position_side_for_stop_side(side)
        try:
            response = await self._call(
                lambda **kwargs: self.client._request_futures_api("post", "algoOrder", True, data=kwargs),
                retry_status_unknown=False,
                symbol=symbol,
                side=side,
                positionSide=position_side,
                type="STOP_MARKET",
                algoType="CONDITIONAL",
                triggerPrice=trigger_price,
                closePosition=True,
                workingType="MARK_PRICE",
                clientAlgoId=client_algo_id,
                newOrderRespType="ACK",
            )
        except Exception as exc:
            recovered = await self._recover_open_algo_order_by_client_id_after_create_exception(
                symbol, client_algo_id, exc
            )
            if recovered is not None:
                response = recovered
            else:
                raise
        validated = _validate_algo_close_position_order_response(
            response,
            symbol=symbol,
            side=side,
            position_side=position_side,
            trigger_price=trigger_price,
            client_algo_id=client_algo_id,
        )
        return _standardize_algo_close_position_response(
            validated,
            symbol=symbol,
            side=side,
            position_side=position_side,
            trigger_price=trigger_price,
            client_algo_id=client_algo_id,
        )

    async def _close_position_side_for_stop_side(self, side: str) -> str:
        normalized = side.upper()
        if normalized not in {"BUY", "SELL"}:
            raise ValueError("Binance stop side must be BUY or SELL")
        mode = await self.get_position_mode()
        if not bool(mode.get("dualSidePosition")):
            return "BOTH"
        return "LONG" if normalized == "SELL" else "SHORT"

    async def get_open_algo_orders(self, symbol: str) -> list[dict[str, Any]]:
        response = await self._call(
            lambda **kwargs: self.client._request_futures_api("get", "openAlgoOrders", True, data=kwargs),
            symbol=symbol,
        )
        return _validate_open_algo_orders_response(response, symbol)

    async def cancel_algo_order(self, symbol: str, algo_id: int | str) -> dict[str, Any]:
        response = await self._call(
            lambda **kwargs: self.client._request_futures_api("delete", "algoOrder", True, data=kwargs),
            symbol=symbol,
            algoId=algo_id,
        )
        return _validate_cancel_algo_order_response(response)

    async def _recover_open_algo_order_by_client_id_after_create_exception(
        self, symbol: str, client_algo_id: str, exc: Exception
    ) -> dict[str, Any] | None:
        if not _is_order_create_status_unknown(exc):
            return None
        try:
            open_algo_orders = await self.get_open_algo_orders(symbol)
        except Exception:
            return None
        for order in open_algo_orders:
            if str(order.get("clientAlgoId", "")) == client_algo_id:
                return order
        return None

    async def _cancel_open_algo_order_if_present(self, symbol: str, order_id: str) -> bool:
        try:
            open_algo_orders = await self.get_open_algo_orders(symbol)
        except Exception:
            return False
        for order in open_algo_orders:
            if str(order.get("algoId", "")) == str(order_id):
                await self.cancel_algo_order(symbol, order["algoId"])
                return True
        return False

    def on_order_filled(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._order_filled_callbacks.append(callback)

    def on_price_update(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._price_update_callbacks.append(callback)

    def dispatch_user_event(self, message: dict[str, Any]) -> dict[str, Any] | None:
        event = parse_order_trade_update(message)
        if event is None:
            return None
        for callback in self._order_filled_callbacks:
            callback(event)
        return event

    def dispatch_price_event(self, message: dict[str, Any]) -> dict[str, Any] | None:
        event = parse_price_update(message)
        if event is None:
            return None
        for callback in self._price_update_callbacks:
            callback(event)
        return event

    async def run_user_stream(
        self,
        handler: Callable[[dict[str, Any]], Any],
        socket_factory: Callable[[], Any] | None = None,
        reconnect_delay_seconds: float = 5,
        max_reconnects: int | None = None,
    ) -> None:
        await self._run_socket_loop(
            socket_factory or self._user_socket,
            parse_order_trade_update,
            handler,
            reconnect_delay_seconds,
            max_reconnects,
        )

    async def run_price_stream(
        self,
        symbols: list[str],
        handler: Callable[[dict[str, Any]], Any],
        socket_factory: Callable[[], Any] | None = None,
        reconnect_delay_seconds: float = 5,
        max_reconnects: int | None = None,
    ) -> None:
        await self._run_socket_loop(
            socket_factory or (lambda: self._price_socket(symbols)),
            parse_price_update,
            handler,
            reconnect_delay_seconds,
            max_reconnects,
        )

    async def _run_socket_loop(
        self,
        socket_factory: Callable[[], Any],
        parser: Callable[[dict[str, Any]], dict[str, Any] | None],
        handler: Callable[[dict[str, Any]], Any],
        reconnect_delay_seconds: float,
        max_reconnects: int | None,
    ) -> None:
        reconnects = 0
        while max_reconnects is None or reconnects <= max_reconnects:
            handler_error: Exception | None = None
            try:
                socket_error: Exception | None = None
                socket = socket_factory()
                try:
                    async with socket as stream:
                        while True:
                            try:
                                message = await stream.recv()
                            except (asyncio.CancelledError, StopAsyncIteration):
                                raise
                            except Exception as exc:
                                socket_error = exc
                                break
                            event = parser(message)
                            if event is None:
                                continue
                            try:
                                result = handler(event)
                                if inspect.isawaitable(result):
                                    await result
                            except Exception as exc:
                                handler_error = exc
                                break
                except Exception as exc:
                    if handler_error is not None and _is_socket_exit_compat_error(exc):
                        raise _HandlerError from handler_error
                    raise
                if handler_error is not None:
                    raise _HandlerError from handler_error
                if socket_error is None:
                    return
                reconnects += 1
                if max_reconnects is not None and reconnects > max_reconnects:
                    raise socket_error
                await asyncio.sleep(reconnect_delay_seconds)
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                return
            except _HandlerError as exc:
                raise exc.__cause__ or exc
            except Exception:
                reconnects += 1
                if max_reconnects is not None and reconnects > max_reconnects:
                    raise
                await asyncio.sleep(reconnect_delay_seconds)

    def _user_socket(self):
        try:
            from binance import BinanceSocketManager
        except ImportError as exc:
            raise RuntimeError("缺少 python-binance 依赖，请先安装 requirements.txt。") from exc
        return BinanceSocketManager(self.client).futures_user_socket()

    def _price_socket(self, symbols: list[str]):
        try:
            from binance import BinanceSocketManager
        except ImportError as exc:
            raise RuntimeError("缺少 python-binance 依赖，请先安装 requirements.txt。") from exc
        streams = [f"{symbol.lower()}@markPrice@1s" for symbol in symbols]
        return BinanceSocketManager(self.client).futures_multiplex_socket(streams)

    async def _call(self, func: Callable[..., Any], retry_status_unknown: bool = True, **kwargs) -> Any:
        attempts = max(1, self.retry_attempts)
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await func(**kwargs)
            except Exception as exc:
                last_exc = exc
                if not retry_status_unknown and _is_order_create_status_unknown(exc):
                    sanitized_exc = _sanitize_exchange_exception(exc)
                    if sanitized_exc is not exc:
                        raise sanitized_exc from None
                    raise
                if _is_non_retryable_exchange_error(exc):
                    raise
                if attempt == attempts - 1:
                    break
                delay = self.retry_delay_seconds * (2**attempt)
                result = self.retry_sleep(delay)
                if inspect.isawaitable(result):
                    await result
        if last_exc is None:
            raise RuntimeError("Binance API call failed without an exception")
        sanitized_exc = _sanitize_exchange_exception(last_exc)
        if sanitized_exc is not last_exc:
            raise sanitized_exc from None
        raise last_exc  # type: ignore[misc]


def _requests_params(proxy_config: dict[str, Any] | None) -> dict[str, Any] | None:
    if not proxy_config or not proxy_config.get("enabled"):
        return None
    proxy = proxy_config.get("https") or proxy_config.get("http")
    if not proxy:
        return None
    return {"proxy": proxy}


def _is_non_retryable_exchange_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "no need to change margin type",
        "post only",
        "would immediately match",
        "-5022",
        "-4120",
        "use the algo order api",
    )
    return any(marker in text for marker in markers)


def _sanitize_exchange_exception(exc: Exception) -> Exception:
    text = str(exc)
    if "Invalid JSON error message from Binance" not in text and "signature=" not in text:
        return exc
    status_match = re.search(r"\[(\d{3}(?: [^\]]+)?)\]", text)
    suffix = f"; status={status_match.group(1)}" if status_match else ""
    return RuntimeError(f"Binance API returned a non-JSON or raw transport error{suffix}.")


def _is_order_create_status_unknown(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "status unknown",
        "timeout waiting for response from backend server",
        "send status unknown",
        "execution status unknown",
        "bad gateway",
        "non-json or raw transport error",
    )
    return any(marker in text for marker in markers)


def _is_stop_market_requires_algo_order(exc: Exception) -> bool:
    text = str(exc).lower()
    return "-4120" in text or "use the algo order api" in text


def _is_socket_exit_compat_error(exc: Exception) -> bool:
    text = str(exc)
    return isinstance(exc, AttributeError) and "fail_connection" in text


def _validate_leverage_response(response: Any, symbol: str, leverage: int) -> None:
    if not isinstance(response, dict):
        raise ValueError("Binance leverage response must be an object")
    _require_optional_echo(response, "symbol", symbol, "symbol")
    echoed_leverage = _required_float(response, ("leverage",), "leverage")
    if echoed_leverage != leverage:
        raise ValueError("Binance leverage response leverage mismatch")


def _validate_margin_type_response(response: Any, symbol: str, margin_type: str) -> None:
    if not isinstance(response, dict):
        raise ValueError("Binance margin type response must be an object")
    code = response.get("code")
    has_success_code = False
    if code not in (None, ""):
        has_success_code = _response_code_is_success(code)
        if not has_success_code:
            raise ValueError("Binance margin type response code mismatch")
    _require_optional_echo(response, "symbol", symbol, "symbol")
    echoed_margin_type = _non_empty_str_or_none(response.get("marginType"))
    if echoed_margin_type is not None and echoed_margin_type.upper() != margin_type.upper():
        raise ValueError("Binance margin type response margin type mismatch")
    if not has_success_code and echoed_margin_type is None:
        raise ValueError("Binance margin type response missing success code or margin type")


def _response_code_is_success(value: Any) -> bool:
    try:
        return int(value) == 200
    except (TypeError, ValueError):
        return False


def _validate_exchange_info_response(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise ValueError("Binance exchange info response must be an object")
    rows = response.get("symbols")
    if not isinstance(rows, list):
        raise ValueError("Binance exchange info response invalid symbols")
    symbols: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Binance exchange info response invalid symbol row")
        if _non_empty_str_or_none(row.get("symbol")) is None:
            raise ValueError("Binance exchange info response missing symbol")
        symbols.append(row)
    return symbols


def _validate_account_balance_response(response: Any) -> float:
    if not isinstance(response, list):
        raise ValueError("Binance account balance response must be a list")
    for row in response:
        if not isinstance(row, dict):
            raise ValueError("Binance account balance row must be an object")
        if row.get("asset") == "USDT":
            return _required_float(row, ("availableBalance", "balance"), "USDT balance")
    return 0.0


def _validate_funding_rate_response(response: Any, symbol: str) -> float:
    if not isinstance(response, list):
        raise ValueError("Binance funding rate response must be a list")
    if not response:
        return 0.0
    row = response[-1]
    if not isinstance(row, dict):
        raise ValueError("Binance funding rate row must be an object")
    echoed_symbol = _non_empty_str_or_none(row.get("symbol"))
    if echoed_symbol is not None and echoed_symbol != symbol:
        raise ValueError("Binance funding rate response symbol mismatch")
    return _required_float(row, ("fundingRate",), "funding rate")


def _validate_commission_rate_response(response: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance commission response must be an object")
    echoed_symbol = _non_empty_str_or_none(response.get("symbol"))
    if echoed_symbol is not None and echoed_symbol != symbol:
        raise ValueError("Binance commission response symbol mismatch")
    return response


def _validate_klines_response(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, list):
        raise ValueError("Binance klines response must be a list")
    klines = []
    for row in response:
        if not isinstance(row, (list, tuple)):
            raise ValueError("Binance kline row must be a list")
        klines.append(_kline_from_row(row))
    return klines


def _validate_ticker_response(response: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance ticker response must be an object")
    echoed_symbol = _non_empty_str_or_none(response.get("symbol"))
    if echoed_symbol is not None and echoed_symbol != symbol:
        raise ValueError("Binance ticker response symbol mismatch")
    _required_non_negative_float(response, ("quoteVolume",), "quoteVolume")
    if response.get("lastPrice") not in (None, ""):
        _required_positive_float(response, ("lastPrice",), "lastPrice")
    return response


def _validate_orderbook_response(response: Any, limit: int) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance orderbook response must be an object")
    bids = _orderbook_side(response, "bids")
    asks = _orderbook_side(response, "asks")
    max_rows = max(0, limit)
    for side_name, rows in (("bids", bids[:max_rows]), ("asks", asks[:max_rows])):
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise ValueError(f"Binance orderbook response invalid {side_name} row")
            _positive_number(row[0], f"{side_name}.price")
            _non_negative_number(row[1], f"{side_name}.qty")
    if bids and asks:
        best_bid = _positive_number(bids[0][0], "bids.price")
        best_ask = _positive_number(asks[0][0], "asks.price")
        if best_bid > best_ask:
            raise ValueError("Binance orderbook response invalid spread")
    return response


def _validate_open_orders_response(response: Any, symbol: str) -> list[dict[str, Any]]:
    if not isinstance(response, list):
        raise ValueError("Binance open orders response must be a list")
    validated = []
    for order in response:
        if not isinstance(order, dict):
            raise ValueError("Binance open order response must be an object")
        _validate_order_identity(order, symbol, require_status=False)
        validated.append(order)
    return validated


def _validate_order_lookup_response(response: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance order lookup response must be an object")
    _validate_order_identity(response, symbol, require_status=True)
    return response


def _validate_position_response(response: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(response, list):
        raise ValueError("Binance position response must be a list")
    if not response:
        return {"symbol": symbol, "qty": 0.0, "long_qty": 0.0, "short_qty": 0.0, "positions": []}
    rows: list[dict[str, Any]] = []
    net_qty = 0.0
    long_qty = 0.0
    short_qty = 0.0
    for row in response:
        if not isinstance(row, dict):
            raise ValueError("Binance position row must be an object")
        echoed_symbol = _non_empty_str_or_none(row.get("symbol"))
        if echoed_symbol is not None and echoed_symbol != symbol:
            raise ValueError("Binance position response symbol mismatch")
        validated_row = dict(row)
        qty = _required_float(validated_row, ("positionAmt",), "positionAmt")
        validated_row["qty"] = qty
        position_side = (_non_empty_str_or_none(validated_row.get("positionSide")) or "BOTH").upper()
        if position_side == "LONG":
            long_qty += abs(qty)
        elif position_side == "SHORT":
            short_qty += abs(qty)
        elif position_side == "BOTH":
            if qty > 0:
                long_qty += qty
            elif qty < 0:
                short_qty += abs(qty)
        else:
            raise ValueError("Binance position response invalid positionSide")
        net_qty += qty
        rows.append(validated_row)
    return {
        "symbol": symbol,
        "qty": net_qty,
        "long_qty": long_qty,
        "short_qty": short_qty,
        "positions": rows,
    }


def _validate_cancel_order_response(response: Any, symbol: str, order_id: str) -> None:
    if not isinstance(response, dict):
        raise ValueError("Binance cancel order response must be an object")
    _validate_optional_success_code(response, "Binance cancel order response")
    _require_optional_echo(response, "symbol", symbol, "symbol")
    echoed_order_id = _non_empty_str_or_none(response.get("orderId"))
    if echoed_order_id is None:
        raise ValueError("Binance cancel order response missing order id")
    if echoed_order_id != str(order_id):
        raise ValueError("Binance cancel order response order id mismatch")


def _validate_cancel_all_orders_response(response: Any, symbol: str) -> None:
    if not isinstance(response, dict):
        raise ValueError("Binance cancel all orders response must be an object")
    has_success_code = _validate_optional_success_code(response, "Binance cancel all orders response")
    echoed_symbol = _non_empty_str_or_none(response.get("symbol"))
    if echoed_symbol is not None and echoed_symbol != symbol:
        raise ValueError("Binance cancel all orders response symbol mismatch")
    if not has_success_code and echoed_symbol is None:
        raise ValueError("Binance cancel all orders response missing success marker")


def _validate_optional_success_code(response: dict[str, Any], label: str) -> bool:
    code = response.get("code")
    if code in (None, ""):
        return False
    if not _response_code_is_success(code):
        raise ValueError(f"{label} code mismatch")
    return True


def _validate_order_identity(order: dict[str, Any], symbol: str, require_status: bool) -> None:
    echoed_symbol = _non_empty_str_or_none(order.get("symbol"))
    if echoed_symbol is not None and echoed_symbol != symbol:
        raise ValueError("Binance order response symbol mismatch")
    if _first_non_empty_str(order, ("orderId", "clientOrderId", "origClientOrderId", "client_id")) is None:
        raise ValueError("Binance order response missing order id")
    if require_status and _first_non_empty_str(order, ("status", "X")) is None:
        raise ValueError("Binance order response missing status")


def _orderbook_side(response: dict[str, Any], side: str) -> list[Any]:
    rows = response.get(side)
    if not isinstance(rows, list):
        raise ValueError(f"Binance orderbook response invalid {side}")
    return rows


def _positive_number(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return number


def _non_negative_number(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return number


def _finite_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Binance response invalid {label}: {value}") from exc
    if not isfinite(number):
        raise ValueError(f"Binance response invalid {label}: {value}")
    return number


def _validate_create_order_response(
    response: Any,
    *,
    symbol: str,
    side: str,
    order_type: str,
    client_id: str | None = None,
    time_in_force: str | None = None,
    reduce_only: bool | None = None,
    close_position: bool | None = None,
    position_side: str | None = None,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance create order response must be an object")
    if _non_empty_str_or_none(response.get("orderId")) is None:
        raise ValueError("Binance create order response missing order id")

    _require_optional_echo(response, "symbol", symbol, "symbol")
    _require_optional_echo(response, "side", side, "side")
    _require_optional_echo(response, "type", order_type, "type")
    if time_in_force is not None:
        _require_optional_echo(response, "timeInForce", time_in_force, "timeInForce")
    if client_id is not None:
        echoed_client_id = _first_non_empty_str(
            response,
            ("clientOrderId", "origClientOrderId", "client_id", "newClientOrderId"),
        )
        if echoed_client_id is None:
            raise ValueError("Binance create order response missing client id")
        if echoed_client_id != client_id:
            raise ValueError("Binance create order response client id mismatch")
    if reduce_only is not None:
        _require_optional_bool_echo(response, "reduceOnly", reduce_only, "reduceOnly")
    if close_position is not None:
        _require_optional_bool_echo(response, "closePosition", close_position, "closePosition")
    if position_side is not None:
        _require_optional_echo(response, "positionSide", position_side, "positionSide")
    return response


def _validate_test_order_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance test order response must be an object")
    return response


def _validate_listen_key_response(response: Any, allow_empty: bool = False) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance listen key response must be an object")
    if not allow_empty and _non_empty_str_or_none(response.get("listenKey")) is None:
        raise ValueError("Binance listen key response missing listenKey")
    return response


def _validate_algo_order_response(
    response: Any,
    *,
    symbol: str,
    side: str,
    position_side: str,
    trigger_price: float,
    qty: float,
    client_algo_id: str,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance algo order response must be an object")
    if _non_empty_str_or_none(response.get("algoId")) is None:
        raise ValueError("Binance algo order response missing algo id")
    _require_optional_echo(response, "symbol", symbol, "symbol")
    _require_optional_echo(response, "side", side, "side")
    _require_optional_echo(response, "positionSide", position_side, "positionSide")
    _require_optional_echo(response, "algoType", "CONDITIONAL", "algoType")
    _require_optional_echo(response, "workingType", "MARK_PRICE", "workingType")
    _require_algo_order_type_echo(response, "STOP_MARKET")
    _require_required_echo(response, "clientAlgoId", client_algo_id, "client algo id")
    _require_required_number_echo(response, "triggerPrice", trigger_price, "triggerPrice")
    _require_required_number_echo(response, "quantity", qty, "quantity")
    return response


def _validate_algo_close_position_order_response(
    response: Any,
    *,
    symbol: str,
    side: str,
    position_side: str,
    trigger_price: float,
    client_algo_id: str,
) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance algo order response must be an object")
    if _non_empty_str_or_none(response.get("algoId")) is None:
        raise ValueError("Binance algo order response missing algo id")
    _require_optional_echo(response, "symbol", symbol, "symbol")
    _require_optional_echo(response, "side", side, "side")
    _require_optional_echo(response, "positionSide", position_side, "positionSide")
    _require_optional_echo(response, "algoType", "CONDITIONAL", "algoType")
    _require_optional_echo(response, "workingType", "MARK_PRICE", "workingType")
    _require_algo_order_type_echo(response, "STOP_MARKET")
    _require_required_echo(response, "clientAlgoId", client_algo_id, "client algo id")
    _require_required_number_echo(response, "triggerPrice", trigger_price, "triggerPrice")
    _require_required_bool_echo(response, "closePosition", True, "closePosition")
    return response


def _standardize_algo_close_position_response(
    response: dict[str, Any],
    *,
    symbol: str,
    side: str,
    position_side: str,
    trigger_price: float,
    client_algo_id: str,
) -> dict[str, Any]:
    algo_id = response["algoId"]
    return {
        **response,
        "orderId": str(algo_id),
        "algoId": algo_id,
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "positionSide": position_side,
        "stopPrice": trigger_price,
        "triggerPrice": trigger_price,
        "closePosition": True,
        "clientOrderId": client_algo_id,
        "clientAlgoId": client_algo_id,
    }


def _validate_open_algo_orders_response(response: Any, symbol: str) -> list[dict[str, Any]]:
    if not isinstance(response, list):
        raise ValueError("Binance open algo orders response must be a list")
    orders = []
    for order in response:
        if not isinstance(order, dict):
            raise ValueError("Binance open algo order row must be an object")
        _require_optional_echo(order, "symbol", symbol, "symbol")
        if _non_empty_str_or_none(order.get("algoId")) is None:
            raise ValueError("Binance open algo order response missing algo id")
        orders.append(order)
    return orders


def _validate_cancel_algo_order_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise ValueError("Binance cancel algo order response must be an object")
    if _non_empty_str_or_none(response.get("algoId")) is None:
        raise ValueError("Binance cancel algo order response missing algo id")
    return response


def _require_required_echo(response: dict[str, Any], key: str, expected: str, label: str) -> None:
    value = _non_empty_str_or_none(response.get(key))
    if value is None:
        raise ValueError(f"Binance algo order response missing {label}")
    if value != expected:
        raise ValueError(f"Binance algo order response {label} mismatch")


def _require_optional_echo(response: dict[str, Any], key: str, expected: str, label: str) -> None:
    value = _non_empty_str_or_none(response.get(key))
    if value is not None and value.upper() != expected.upper():
        raise ValueError(f"Binance create order response {label} mismatch")


def _require_algo_order_type_echo(response: dict[str, Any], expected: str) -> None:
    value = _first_non_empty_str(response, ("orderType", "type"))
    if value is not None and value.upper() != expected.upper():
        raise ValueError("Binance algo order response type mismatch")


def _require_required_number_echo(response: dict[str, Any], key: str, expected: float, label: str) -> None:
    value = response.get(key)
    if value in (None, ""):
        raise ValueError(f"Binance algo order response missing {label}")
    actual = _finite_number(value, label)
    if abs(actual - float(expected)) > 1e-12:
        raise ValueError(f"Binance algo order response {label} mismatch")


def _require_required_bool_echo(response: dict[str, Any], key: str, expected: bool, label: str) -> None:
    value = response.get(key)
    if value in (None, ""):
        raise ValueError(f"Binance algo order response missing {label}")
    parsed = _bool_or_none(value)
    if parsed is None or parsed is not expected:
        raise ValueError(f"Binance algo order response {label} mismatch")


def _require_optional_bool_echo(response: dict[str, Any], key: str, expected: bool, label: str) -> None:
    value = response.get(key)
    if value in (None, ""):
        return
    parsed = _bool_or_none(value)
    if parsed is None or parsed is not expected:
        raise ValueError(f"Binance create order response {label} mismatch")


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


def _first_non_empty_str(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _non_empty_str_or_none(data.get(key))
        if value is not None:
            return value
    return None


def symbol_rules_from_exchange_info(symbol_info: dict[str, Any]) -> dict[str, float]:
    filters = _filters_by_type(symbol_info)
    price_filter = filters.get("PRICE_FILTER", {})
    lot_size = filters.get("LOT_SIZE", filters.get("MARKET_LOT_SIZE", {}))
    notional_filter = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
    return {
        "tick_size": _required_positive_float(price_filter, ("tickSize",), "PRICE_FILTER.tickSize"),
        "step_size": _required_positive_float(lot_size, ("stepSize",), "LOT_SIZE.stepSize"),
        "min_qty": _required_positive_float(lot_size, ("minQty",), "LOT_SIZE.minQty"),
        "min_notional": _optional_positive_float(notional_filter, ("notional", "minNotional"), "MIN_NOTIONAL.notional"),
    }


def _filters_by_type(symbol_info: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(symbol_info, dict):
        raise ValueError("Binance symbol info must be an object")
    raw_filters = symbol_info.get("filters")
    if not isinstance(raw_filters, list):
        raise ValueError("Binance symbol rules invalid filters")
    filters: dict[str, dict[str, Any]] = {}
    for row in raw_filters:
        if not isinstance(row, dict):
            raise ValueError("Binance symbol rules invalid filter row")
        filter_type = _non_empty_str_or_none(row.get("filterType"))
        if filter_type is None:
            raise ValueError("Binance symbol rules missing filterType")
        filters[filter_type] = row
    return filters


def parse_order_trade_update(message: dict[str, Any]) -> dict[str, Any] | None:
    message = _event_payload(message)
    if message.get("e") != "ORDER_TRADE_UPDATE":
        return None

    order = message.get("o", {})
    if not isinstance(order, dict):
        return None
    status = str(order.get("X", ""))
    if status not in {"FILLED", "PARTIALLY_FILLED"}:
        return None

    qty = _order_fill_qty(order)
    price = _order_fill_price(order)
    if qty <= 0 or price <= 0:
        return None
    symbol = _non_empty_str_or_none(order.get("s"))
    side = _non_empty_str_or_none(order.get("S"))
    client_id = _non_empty_str_or_none(order.get("c"))
    order_id = _non_empty_str_or_none(order.get("i"))
    if symbol is None or side not in {"BUY", "SELL"} or client_id is None or order_id is None:
        return None
    return {
        "symbol": symbol,
        "client_id": client_id,
        "order_id": order_id,
        "side": side,
        "status": status,
        "price": price,
        "qty": qty,
        "trade_id": str(order.get("t", "")),
        "trade_time": _event_time(message),
    }


def parse_price_update(message: dict[str, Any]) -> dict[str, Any] | None:
    message = _event_payload(message)
    event_type = message.get("e")
    if event_type == "markPriceUpdate":
        symbol = _non_empty_str_or_none(message.get("s"))
        price = _positive_float_or_none(message.get("p"))
        if symbol is None or price is None:
            return None
        return {
            "symbol": symbol,
            "price": price,
            "event_time": _event_time(message),
        }
    if event_type == "bookTicker":
        symbol = _non_empty_str_or_none(message.get("s"))
        bid = _positive_float_or_none(message.get("b"))
        ask = _positive_float_or_none(message.get("a"))
        if symbol is None or bid is None or ask is None or bid > ask:
            return None
        return {
            "symbol": symbol,
            "price": (bid + ask) / 2,
            "event_time": _event_time(message),
        }
    return None


def _event_payload(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {}
    data = message.get("data")
    if isinstance(data, dict):
        return data
    return message


def _first_float(data: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", "0", 0):
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if isfinite(number):
                return number
    return 0.0


def _positive_field_or_zero(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if value in (None, ""):
        return 0.0
    number = _positive_float_or_none(value)
    return number if number is not None else 0.0


def _positive_float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(number) or number <= 0:
        return None
    return number


def _non_empty_str_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def _required_float(data: dict[str, Any], keys: tuple[str, ...], label: str) -> float:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Binance response invalid {label}: {value}") from exc
            if not isfinite(number):
                raise ValueError(f"Binance response invalid {label}: {value}")
            return number
    raise ValueError(f"Binance response missing {label}")


def _optional_float(data: dict[str, Any], keys: tuple[str, ...], label: str) -> float:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Binance response invalid {label}: {value}") from exc
            if not isfinite(number):
                raise ValueError(f"Binance response invalid {label}: {value}")
            return number
    return 0.0


def _required_row_positive_float(row: list[Any], index: int, label: str) -> float:
    value = _required_row_float(row, index, label)
    if value <= 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return value


def _required_row_non_negative_float(row: list[Any], index: int, label: str) -> float:
    value = _required_row_float(row, index, label)
    if value < 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return value


def _required_row_float(row: list[Any], index: int, label: str) -> float:
    try:
        value = row[index]
    except IndexError as exc:
        raise ValueError(f"Binance response missing {label}") from exc
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Binance response invalid {label}: {value}") from exc
    if not isfinite(number):
        raise ValueError(f"Binance response invalid {label}: {value}")
    return number


def _kline_from_row(row: list[Any]) -> dict[str, Any]:
    open_price = _required_row_positive_float(row, 1, "kline open")
    high = _required_row_positive_float(row, 2, "kline high")
    low = _required_row_positive_float(row, 3, "kline low")
    close = _required_row_positive_float(row, 4, "kline close")
    if high < low or open_price < low or open_price > high or close < low or close > high:
        raise ValueError("Binance response invalid kline price relationship")
    return {
        "open_time": row[0],
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": _required_row_non_negative_float(row, 5, "kline volume"),
        "close_time": row[6],
    }


def _required_positive_float(data: dict[str, Any], keys: tuple[str, ...], label: str) -> float:
    value = _required_float(data, keys, label)
    if value <= 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return value


def _required_non_negative_float(data: dict[str, Any], keys: tuple[str, ...], label: str) -> float:
    value = _required_float(data, keys, label)
    if value < 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return value


def _optional_positive_float(data: dict[str, Any], keys: tuple[str, ...], label: str) -> float:
    if not data:
        return 0.0
    value = _required_float(data, keys, label)
    if value <= 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return value


def _optional_non_negative_float(data: dict[str, Any], keys: tuple[str, ...], label: str) -> float:
    if not data:
        return 0.0
    value = _optional_float(data, keys, label)
    if value < 0:
        raise ValueError(f"Binance response invalid {label}: {value}")
    return value


def _order_fill_price(order: dict[str, Any]) -> float:
    if order.get("l") not in (None, "", "0", 0):
        if order.get("L") not in (None, ""):
            return _positive_field_or_zero(order, "L")
        return _first_float(order, ("L", "p", "ap"))
    return _first_float(order, ("ap", "L", "p"))


def _order_fill_qty(order: dict[str, Any]) -> float:
    if order.get("l") not in (None, ""):
        return _positive_field_or_zero(order, "l")
    return _first_float(order, ("z", "q"))


def _event_time(message: dict[str, Any]):
    from datetime import datetime, timezone

    raw_time = message.get("T", message.get("E"))
    if raw_time is None:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromtimestamp(int(raw_time) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc)
