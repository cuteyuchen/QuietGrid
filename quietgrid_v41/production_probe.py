from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from exchange.public_market import ProductionPublicMarketData
from quietgrid_v40.safety import CapabilityStatus, ExecutionLane, ExecutionSafetyPolicy


TARGET_SYMBOLS = ("SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT")
PRODUCTION_REST = "https://fapi.binance.com"
PRODUCTION_WS = "wss://fstream.binance.com/stream"


def _status_for_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
        return CapabilityStatus.ERROR_FATAL.value
    return CapabilityStatus.ERROR_RETRYABLE.value


async def _public_websocket_probe(symbols: tuple[str, ...], timeout: float) -> dict[str, Any]:
    try:
        import websockets

        streams = "/".join(
            f"{symbol.lower()}@trade/{symbol.lower()}@bookTicker"
            for symbol in symbols
        )
        async with websockets.connect(f"{PRODUCTION_WS}?streams={streams}", open_timeout=timeout, close_timeout=timeout) as socket:
            received = {"trade": False, "bookTicker": False}
            deadline = asyncio.get_running_loop().time() + timeout
            while not all(received.values()):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return {
                        "status": CapabilityStatus.ERROR_RETRYABLE.value,
                        "error": "probe timeout before both trade and bookTicker streams were observed",
                        "received": received,
                    }
                raw = json.loads(await asyncio.wait_for(socket.recv(), timeout=remaining))
                stream = str(raw.get("stream", ""))
                if stream.endswith("@trade"):
                    received["trade"] = True
                elif stream.endswith("@bookTicker"):
                    received["bookTicker"] = True
            return {"status": CapabilityStatus.SUPPORTED.value, "received": received}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {"status": CapabilityStatus.ERROR_RETRYABLE.value, "error": str(exc)}


async def probe(*, network: bool = True, websocket: bool = True, timeout: float = 15.0, market_data: Any | None = None) -> dict[str, Any]:
    policy = ExecutionSafetyPolicy(ExecutionLane.PUBLIC_DATA_ONLY, rest_url=PRODUCTION_REST, ws_url=PRODUCTION_WS)
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": PRODUCTION_REST,
        "websocket_endpoint": PRODUCTION_WS,
        "symbols": [],
        "production_private_api": "DISABLED",
        "signed_requests": "DISABLED",
        "safety": policy.describe(),
    }
    if not network:
        result["server_time"] = {"status": "SKIPPED_NETWORK_UNAVAILABLE"}
        result["websocket"] = {"status": "SKIPPED_NETWORK_UNAVAILABLE"}
        result["symbols"] = [{"symbol": symbol, "status": "ERROR_RETRYABLE", "final_capability": "NOT_PROBED"} for symbol in TARGET_SYMBOLS]
        result["classification"] = "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"
        result["conclusion"] = "SKIPPED_NETWORK_UNAVAILABLE"
        return result
    owned = market_data is None
    data = market_data or ProductionPublicMarketData(base_url=PRODUCTION_REST, timeout=timeout)
    try:
        symbols = {str(item.get("symbol", "")).upper(): item for item in await data.get_symbols()}
        server_time = await data.get_server_time() if hasattr(data, "get_server_time") else None
        result["server_time"] = {"status": CapabilityStatus.SUPPORTED.value, "serverTime": server_time}
        for symbol in TARGET_SYMBOLS:
            item = symbols.get(symbol)
            if item is None:
                result["symbols"].append({"symbol": symbol, "exists": False, "status": CapabilityStatus.UNSUPPORTED.value, "final_capability": "UNSUPPORTED", "checks": {"symbol_existence": False}})
                continue
            checks: dict[str, Any] = {
                "symbol_existence": True,
                "status": str(item.get("status") or ""),
                "contract_type": item.get("contractType"),
                "filters": False,
                "ticker": False,
                "depth": False,
                "kline": False,
                "mark_price": False,
                "funding": False,
            }
            errors: dict[str, str] = {}
            try:
                rules = await data.get_symbol_rules(symbol)
                checks["filters"] = bool(rules)
                for field in ("tick_size", "step_size", "min_qty", "min_notional"):
                    checks[field] = _positive_number(rules.get(field))
            except Exception as exc:
                errors["filters"] = str(exc)
            for name, getter in (
                ("ticker", lambda: data.get_24h_ticker(symbol)),
                ("depth", lambda: data.get_orderbook_depth(symbol, 5)),
                ("kline", lambda: data.get_klines(symbol, "1m", 2)),
                ("mark_price", lambda: data.get_funding_context(symbol) if hasattr(data, "get_funding_context") else data.get_24h_ticker(symbol)),
                ("funding", lambda: data.get_funding_rate(symbol)),
            ):
                try:
                    value = await getter()
                    if name == "mark_price" and isinstance(value, dict):
                        checks[name] = _positive_number(value.get("mark_price", value.get("markPrice")))
                    else:
                        checks[name] = value is not None and value != {} and value != []
                except Exception as exc:
                    errors[name] = str(exc)
            rest_ok = all(checks.get(name, False) for name in ("symbol_existence", "filters", "tick_size", "step_size", "min_qty", "min_notional", "ticker", "depth", "kline", "mark_price", "funding")) and checks["status"] == "TRADING"
            result["symbols"].append({
                "symbol": symbol,
                "exists": True,
                "status": CapabilityStatus.SUPPORTED.value if rest_ok else "PARTIAL",
                "contractType": checks["contract_type"],
                "checks": checks,
                "errors": errors,
                "rest_capability": "SUPPORTED" if rest_ok else "PARTIAL",
            })
        if websocket:
            result["websocket"] = await _public_websocket_probe(TARGET_SYMBOLS, timeout)
        else:
            result["websocket"] = {"status": CapabilityStatus.SKIPPED_NOT_REQUESTED.value}
    except Exception as exc:
        status = _status_for_error(exc)
        result["server_time"] = {"status": status, "error": str(exc)}
        result["symbols"] = [{"symbol": symbol, "status": status, "final_capability": "ERROR_RETRYABLE" if status == CapabilityStatus.ERROR_RETRYABLE.value else "ERROR_FATAL"} for symbol in TARGET_SYMBOLS]
    probe_failed_before_classification = result.get("server_time", {}).get("status") in {
        CapabilityStatus.ERROR_RETRYABLE.value,
        CapabilityStatus.ERROR_FATAL.value,
    }
    websocket_ok = result.get("websocket", {}).get("status") == CapabilityStatus.SUPPORTED.value
    for item in result["symbols"]:
        if item.get("exists") is False:
            item["final_capability"] = "UNSUPPORTED"
        elif item.get("rest_capability") == "SUPPORTED" and websocket_ok:
            item["final_capability"] = "SUPPORTED"
        elif item.get("rest_capability") == "SUPPORTED" or item.get("status") == "PARTIAL":
            item["final_capability"] = "PARTIAL"
        else:
            item.setdefault("final_capability", "ERROR_RETRYABLE")
    final = [str(item.get("final_capability")) for item in result["symbols"]]
    if probe_failed_before_classification:
        result["classification"] = "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"
    elif final and all(value == "SUPPORTED" for value in final):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_SUPPORTED"
    elif any(value in {"SUPPORTED", "PARTIAL"} for value in final):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_PARTIAL"
    elif any(value == "ERROR_FATAL" for value in final):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_UNSUPPORTED"
    else:
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_UNSUPPORTED"
    result["no_credentials_used"] = True
    if owned:
        await data.close()
    return result


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def write_probe_reports(result: dict[str, Any], output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "production-public-capability.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# QuietGrid v4.1 Production Public TradFi Capability", "", f"Classification: {result.get('classification')}", "", "| Symbol | Status | Final | REST checks |", "|---|---|---|---|"]
    for item in result.get("symbols", []):
        checks = item.get("checks", {})
        lines.append(f"| {item.get('symbol')} | {item.get('status')} | {item.get('final_capability')} | {sum(bool(value is True) for value in checks.values())}/{len(checks) or 0} |")
    lines.extend(["", "Production private REST: DISABLED", "Production signed requests: DISABLED", "Production account/position/order/listenKey endpoints: NOT CALLED", ""])
    (out / "production-public-capability.md").write_text("\n".join(lines), encoding="utf-8")
