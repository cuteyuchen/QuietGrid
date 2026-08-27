from __future__ import annotations

import os
from typing import Any


AUTH_ENV = "QUIETGRID_V41_ALLOW_TESTNET_ORDER_LIFECYCLE"


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() == "true"


async def run_testnet_order_lifecycle(config: Any) -> dict[str, Any]:
    """Run the explicitly authorized testnet smoke; never falls back to production."""
    if not _enabled(os.getenv(AUTH_ENV)):
        return {
            "status": "SKIPPED_TESTNET_ORDER_LIFECYCLE_NOT_AUTHORIZED",
            "conclusion": "EXECUTION_INFRA_ONLY",
            "authorization_env": AUTH_ENV,
        }
    from core.config import require_testnet

    try:
        require_testnet(config)
    except Exception as exc:
        return {"status": "FAIL_TESTNET_ORDER_LIFECYCLE", "conclusion": "EXECUTION_INFRA_ONLY", "error": str(exc)}
    if not config.binance_api_key or not config.binance_api_secret:
        return {
            "status": "SKIPPED_TESTNET_ORDER_LIFECYCLE_NO_CREDENTIALS",
            "conclusion": "EXECUTION_INFRA_ONLY",
        }

    from trader import (
        _cleanup_smoke_orders,
        _create_binance_client_for_module,
        _place_limit_order_post_only_reconciled,
        _require_binance_symbol_allowlist,
        _require_binance_tradable_allowlist_symbols,
        _smoke_order_params,
    )

    exchange = None
    symbol = ""
    order_ids: list[str] = []
    client_ids: list[str] = []
    try:
        _require_binance_symbol_allowlist(config)
        exchange = await _create_binance_client_for_module(config, "v41_testnet_order_lifecycle")
        eligible = await _require_binance_tradable_allowlist_symbols(exchange, config)
        if not eligible:
            raise RuntimeError("没有可用于 v4.1 Testnet lifecycle 的已验证合约。")
        symbol = eligible[0]
        params = await _smoke_order_params(exchange, symbol)
        suffix = os.urandom(6).hex()
        client_id = f"qg-v41-testnet-{symbol.lower()}-{suffix}-1"
        client_ids.append(client_id)
        order = await _place_limit_order_post_only_reconciled(
            exchange,
            symbol,
            "BUY",
            float(params["limit_price"]),
            float(params["qty"]),
            client_id,
            position_side="LONG",
        )
        order_id = str(order.get("orderId") or order.get("order_id") or "")
        if not order_id:
            raise RuntimeError("Testnet CREATE 未返回 order id。")
        order_ids.append(order_id)
        created = await exchange.get_order(symbol, order_id, client_id)
        canceled = await exchange.cancel_order(symbol, order_id)
        after_cancel = await exchange.get_order(symbol, order_id, client_id)
        position = await exchange.get_position(symbol)
        remaining = await exchange.get_open_orders(symbol)
        final_status = str(after_cancel.get("status", "")).upper()
        position_qty = float(position.get("qty", position.get("positionAmt", 0.0)) or 0.0)
        namespace_remaining = [
            item for item in remaining
            if str(item.get("clientOrderId", item.get("client_id", ""))).startswith("qg-v41-")
        ]
        if final_status not in {"CANCELED", "CANCELLED"} or abs(position_qty) > 1e-12 or namespace_remaining:
            raise RuntimeError(
                f"Testnet lifecycle final invariant failed: status={final_status}, position={position_qty}, remaining={len(namespace_remaining)}"
            )
        return {
            "status": "PASS_TESTNET_ORDER_LIFECYCLE",
            "conclusion": "EXECUTION_INFRA_ONLY",
            "symbol": symbol,
            "client_ids": client_ids,
            "created": created,
            "canceled": canceled,
            "after_cancel": after_cancel,
            "position": position,
            "reconcile": "RECONCILED",
        }
    except Exception as exc:
        return {
            "status": "FAIL_TESTNET_ORDER_LIFECYCLE",
            "conclusion": "EXECUTION_INFRA_ONLY",
            "symbol": symbol,
            "client_ids": client_ids,
            "error": str(exc),
        }
    finally:
        if exchange is not None and symbol:
            try:
                await _cleanup_smoke_orders(exchange, symbol, order_ids)
            except Exception:
                pass
            await exchange.close()
