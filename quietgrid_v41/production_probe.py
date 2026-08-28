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
    if _rate_limit_details(exc) is not None:
        return "PARTIAL_RATE_LIMITED"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
        return CapabilityStatus.ERROR_FATAL.value
    return CapabilityStatus.ERROR_RETRYABLE.value


def _rate_limit_details(exc: Exception) -> dict[str, Any] | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    status_code = exc.response.status_code
    if status_code not in (418, 429):
        return None
    return {
        "status_code": status_code,
        "retry_after": exc.response.headers.get("Retry-After"),
    }


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


async def probe(*, network: bool = True, websocket: bool = True, timeout: float = 15.0, market_data: Any | None = None, pacing_seconds: float = 0.0) -> dict[str, Any]:
    policy = ExecutionSafetyPolicy(ExecutionLane.PUBLIC_DATA_ONLY, rest_url=PRODUCTION_REST, ws_url=PRODUCTION_WS)
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": PRODUCTION_REST,
        "websocket_endpoint": PRODUCTION_WS,
        "symbols": [],
        "rate_limited_stages": [],
        "request_pacing": {
            "pacing_seconds": pacing_seconds,
            "exchange_info_calls": 1 if network else 0,
        },
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
            if pacing_seconds > 0:
                await asyncio.sleep(pacing_seconds)
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
                if pacing_seconds > 0:
                    await asyncio.sleep(pacing_seconds)
                try:
                    value = await getter()
                    if name == "mark_price" and isinstance(value, dict):
                        checks[name] = _positive_number(value.get("mark_price", value.get("markPrice")))
                    else:
                        checks[name] = value is not None and value != {} and value != []
                except Exception as exc:
                    rate_limit = _rate_limit_details(exc)
                    if rate_limit is not None:
                        detail = {**rate_limit, "stage": f"{symbol}:{name}"}
                        result["rate_limited_stages"].append(detail)
                        errors[name] = (
                            f"PARTIAL_RATE_LIMITED status={detail['status_code']} "
                            f"retry_after={detail.get('retry_after')} stage={detail['stage']}"
                        )
                        break
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
        rate_limit = _rate_limit_details(exc)
        if rate_limit is not None:
            result["rate_limited_stages"].append({**rate_limit, "stage": "exchange_info_or_server_time"})
        result["server_time"] = {"status": status, "error": str(exc)}
        result["symbols"] = [
            {
                "symbol": symbol,
                "status": status,
                "final_capability": "PARTIAL_RATE_LIMITED" if rate_limit is not None else (
                    "ERROR_RETRYABLE" if status == CapabilityStatus.ERROR_RETRYABLE.value else "ERROR_FATAL"
                ),
            }
            for symbol in TARGET_SYMBOLS
        ]
    probe_failed_before_classification = result.get("server_time", {}).get("status") in {
        CapabilityStatus.ERROR_RETRYABLE.value,
        CapabilityStatus.ERROR_FATAL.value,
        "PARTIAL_RATE_LIMITED",
    }
    websocket_ok = result.get("websocket", {}).get("status") == CapabilityStatus.SUPPORTED.value
    for item in result["symbols"]:
        if item.get("exists") is False:
            item["final_capability"] = "UNSUPPORTED"
        elif item.get("rest_capability") == "SUPPORTED" and websocket_ok:
            item["final_capability"] = "SUPPORTED"
        elif item.get("rest_capability") == "SUPPORTED" or item.get("status") == "PARTIAL":
            symbol = str(item.get("symbol", ""))
            if any(str(stage.get("stage", "")).startswith(f"{symbol}:") for stage in result.get("rate_limited_stages", [])):
                item["final_capability"] = "PARTIAL_RATE_LIMITED"
            else:
                item["final_capability"] = "PARTIAL"
        elif item.get("status") == "PARTIAL_RATE_LIMITED":
            item["final_capability"] = "PARTIAL_RATE_LIMITED"
        else:
            item.setdefault("final_capability", "ERROR_RETRYABLE")
    final = [str(item.get("final_capability")) for item in result["symbols"]]
    if probe_failed_before_classification:
        result["classification"] = (
            "PRODUCTION_PUBLIC_PROBE_INCOMPLETE_RATE_LIMITED"
            if result.get("rate_limited_stages")
            else "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"
        )
    elif result.get("rate_limited_stages"):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_PARTIAL_RATE_LIMITED"
    elif final and all(value == "SUPPORTED" for value in final):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_SUPPORTED"
    elif any(value in {"SUPPORTED", "PARTIAL", "PARTIAL_RATE_LIMITED"} for value in final):
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
