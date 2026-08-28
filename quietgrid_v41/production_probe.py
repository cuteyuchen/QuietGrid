from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from exchange.public_market import ProductionPublicMarketData
from quietgrid_v40.safety import CapabilityStatus, ExecutionLane, ExecutionSafetyPolicy


TARGET_SYMBOLS = ("SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT")
LIVE_TARGET_SYMBOLS = ("SNDKUSDT", "MUUSDT", "SOXLUSDT")
RESEARCH_ONLY_SYMBOLS = ("SKHYNIXUSDT",)
PRODUCTION_REST = "https://fapi.binance.com"
PRODUCTION_WS = "wss://fstream.binance.com/stream"
COOLDOWN_FILE = Path(__file__).resolve().parents[1] / "reports" / "testnet-shadow-v4.1" / "production-public-cooldown.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    retry_after = exc.response.headers.get("Retry-After")
    retry_after_seconds: int | None = None
    if retry_after is not None and str(retry_after).strip().isdigit():
        retry_after_seconds = int(str(retry_after).strip())
    headers = {
        key: value
        for key, value in exc.response.headers.items()
        if key.lower() == "retry-after" or key.lower().startswith("x-mbx-used-weight")
    }
    return {
        "status_code": status_code,
        "retry_after": retry_after,
        "retry_after_seconds": retry_after_seconds,
        "headers": headers,
    }


def _load_cooldown(path: str | Path = COOLDOWN_FILE) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cooldown_active(cooldown: dict[str, Any], *, now: datetime | None = None) -> bool:
    cooldown_until = _parse_iso(cooldown.get("cooldown_until"))
    if cooldown_until is None:
        return False
    return cooldown_until > (now or _utc_now())


def _cooldown_payload(detail: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    detected_at = _utc_now()
    retry_after = detail.get("retry_after_seconds")
    current_until = detected_at + timedelta(seconds=retry_after) if retry_after is not None else detected_at
    existing_until = _parse_iso((existing or {}).get("cooldown_until"))
    if existing_until is not None and existing_until > current_until:
        cooldown_until = existing_until
        retry_after_seconds = max(0, int((cooldown_until - detected_at).total_seconds()))
    else:
        cooldown_until = current_until
        retry_after_seconds = retry_after
    return {
        "detected_at": detected_at.isoformat(),
        "status_code": detail.get("status_code"),
        "retry_after_seconds": retry_after_seconds,
        "cooldown_until": cooldown_until.isoformat(),
        "stage": detail.get("stage"),
        "headers": detail.get("headers", {}),
    }


def _write_cooldown(detail: dict[str, Any], path: str | Path = COOLDOWN_FILE) -> dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _cooldown_payload(detail, _load_cooldown(target))
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _record_rate_limit(
    result: dict[str, Any],
    exc: Exception,
    stage: str,
    *,
    cooldown_path: str | Path | None = None,
    completed_checks: list[str] | None = None,
    remaining_checks: list[str] | None = None,
) -> dict[str, Any]:
    detail = _rate_limit_details(exc) or {
        "status_code": "UNKNOWN",
        "retry_after": None,
        "retry_after_seconds": None,
        "headers": {},
    }
    detail["stage"] = stage
    if completed_checks is not None:
        detail["completed_checks"] = completed_checks
    if remaining_checks is not None:
        detail["remaining_checks"] = remaining_checks
    result.setdefault("rate_limited_stages", []).append(detail)
    result["cooldown"] = _write_cooldown(detail, cooldown_path or COOLDOWN_FILE)
    result["websocket"] = {
        "status": "SKIPPED_DUE_TO_RATE_LIMIT",
        "reason": f"REST rate limit at {stage}",
        "cooldown_until": result["cooldown"]["cooldown_until"],
    }
    result["classification"] = "PRODUCTION_PUBLIC_PROBE_INCOMPLETE_RATE_LIMITED"
    result["conclusion"] = "RATE_LIMIT_FAIL_FAST"
    result["rate_limited"] = True
    return detail


def _not_run_symbols(result: dict[str, Any]) -> None:
    existing = {str(item.get("symbol", "")) for item in result.get("symbols", [])}
    for symbol in TARGET_SYMBOLS:
        if symbol not in existing:
            result["symbols"].append(
                {
                    "symbol": symbol,
                    "exists": None,
                    "status": "NOT_RUN_DUE_TO_RATE_LIMIT",
                    "final_capability": "NOT_RUN_DUE_TO_RATE_LIMIT",
                    "reason": "rate limit fail fast",
                }
            )


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
        "generated_at": _utc_now().isoformat(),
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
        "rate_limited": False,
        "cooldown": None,
    }
    if not network:
        result["server_time"] = {"status": "SKIPPED_NETWORK_UNAVAILABLE"}
        result["websocket"] = {"status": "SKIPPED_NETWORK_UNAVAILABLE"}
        result["symbols"] = [{"symbol": symbol, "status": "ERROR_RETRYABLE", "final_capability": "NOT_PROBED"} for symbol in TARGET_SYMBOLS]
        result["classification"] = "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"
        result["conclusion"] = "SKIPPED_NETWORK_UNAVAILABLE"
        return result

    active_cooldown = _load_cooldown(COOLDOWN_FILE)
    if _cooldown_active(active_cooldown):
        result["classification"] = "PROBE_COOLDOWN_ACTIVE"
        result["conclusion"] = "PROBE_COOLDOWN_ACTIVE"
        result["cooldown"] = active_cooldown
        result["websocket"] = {
            "status": "SKIPPED_DUE_TO_COOLDOWN",
            "reason": "active cooldown from previous rate limit",
        }
        result["symbols"] = [
            {
                "symbol": symbol,
                "status": "NOT_PROBED_COOLDOWN",
                "final_capability": "NOT_PROBED_COOLDOWN",
            }
            for symbol in TARGET_SYMBOLS
        ]
        result["no_credentials_used"] = True
        return result

    owned = market_data is None
    data = market_data or ProductionPublicMarketData(base_url=PRODUCTION_REST, timeout=timeout)
    try:
        symbols = {str(item.get("symbol", "")).upper(): item for item in await data.get_symbols()}
        server_time = await data.get_server_time() if hasattr(data, "get_server_time") else None
        result["server_time"] = {"status": CapabilityStatus.SUPPORTED.value, "serverTime": server_time}
        for symbol in TARGET_SYMBOLS:
            if result["rate_limited"]:
                break
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
                if _rate_limit_details(exc) is not None:
                    _record_rate_limit(result, exc, f"{symbol}:filters")
                errors["filters"] = str(exc)

            if result["rate_limited"]:
                result["symbols"].append(
                    {
                        "symbol": symbol,
                        "exists": True,
                        "status": "PARTIAL_RATE_LIMITED",
                        "final_capability": "PARTIAL_RATE_LIMITED",
                        "checks": checks,
                        "errors": errors,
                    }
                )
                break

            if symbol in RESEARCH_ONLY_SYMBOLS:
                result["symbols"].append(
                    {
                        "symbol": symbol,
                        "exists": True,
                        "status": "RESEARCH_ONLY",
                        "contractType": checks["contract_type"],
                        "checks": checks,
                        "errors": errors,
                        "rest_capability": "RESEARCH_ONLY",
                        "final_capability": "RESEARCH_ONLY",
                        "research_only": True,
                        "skipped_rest_checks": ["ticker", "depth", "kline", "mark_price", "funding"],
                    }
                )
                continue

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
                    if _rate_limit_details(exc) is not None:
                        completed = [key for key, value in checks.items() if value is True]
                        remaining = [name for name in ("ticker", "depth", "kline", "mark_price", "funding") if name not in completed]
                        _record_rate_limit(result, exc, f"{symbol}:{name}", completed_checks=completed, remaining_checks=remaining)
                        errors[name] = (
                            f"PARTIAL_RATE_LIMITED status={result['rate_limited_stages'][-1]['status_code']} "
                            f"retry_after={result['rate_limited_stages'][-1].get('retry_after')} stage={result['rate_limited_stages'][-1]['stage']}"
                        )
                        break
                    errors[name] = str(exc)

            if result["rate_limited"]:
                result["symbols"].append(
                    {
                        "symbol": symbol,
                        "exists": True,
                        "status": "PARTIAL_RATE_LIMITED",
                        "contractType": checks["contract_type"],
                        "checks": checks,
                        "errors": errors,
                        "rest_capability": "PARTIAL",
                        "final_capability": "PARTIAL_RATE_LIMITED",
                    }
                )
                break

            rest_ok = all(checks.get(name, False) for name in ("symbol_existence", "filters", "tick_size", "step_size", "min_qty", "min_notional", "ticker", "depth", "kline", "mark_price", "funding")) and checks["status"] == "TRADING"
            result["symbols"].append(
                {
                    "symbol": symbol,
                    "exists": True,
                    "status": CapabilityStatus.SUPPORTED.value if rest_ok else "PARTIAL",
                    "contractType": checks["contract_type"],
                    "checks": checks,
                    "errors": errors,
                    "rest_capability": "SUPPORTED" if rest_ok else "PARTIAL",
                }
            )

        if result["rate_limited"]:
            _not_run_symbols(result)
        elif websocket:
            result["websocket"] = await _public_websocket_probe(TARGET_SYMBOLS, timeout)
        else:
            result["websocket"] = {"status": CapabilityStatus.SKIPPED_NOT_REQUESTED.value}
    except Exception as exc:
        if _rate_limit_details(exc) is not None:
            _record_rate_limit(result, exc, "exchange_info_or_server_time")
            result["server_time"] = {"status": "PARTIAL_RATE_LIMITED", "error": str(exc)}
            _not_run_symbols(result)
        else:
            status = _status_for_error(exc)
            result["server_time"] = {"status": status, "error": str(exc)}
            result["symbols"] = [
                {
                    "symbol": symbol,
                    "status": status,
                    "final_capability": "ERROR_RETRYABLE" if status == CapabilityStatus.ERROR_RETRYABLE.value else "ERROR_FATAL",
                }
                for symbol in TARGET_SYMBOLS
            ]

    if result.get("classification") in {
        "PRODUCTION_PUBLIC_PROBE_INCOMPLETE_RATE_LIMITED",
        "PROBE_COOLDOWN_ACTIVE",
    }:
        result["no_credentials_used"] = True
        if owned:
            await data.close()
        return result

    probe_failed_before_classification = result.get("server_time", {}).get("status") in {
        CapabilityStatus.ERROR_RETRYABLE.value,
        CapabilityStatus.ERROR_FATAL.value,
    }
    websocket_ok = result.get("websocket", {}).get("status") == CapabilityStatus.SUPPORTED.value
    for item in result["symbols"]:
        if item.get("exists") is False:
            item["final_capability"] = "UNSUPPORTED"
        elif item.get("symbol") in RESEARCH_ONLY_SYMBOLS and item.get("rest_capability") == "RESEARCH_ONLY":
            item["final_capability"] = "RESEARCH_ONLY"
        elif item.get("rest_capability") == "SUPPORTED" and websocket_ok:
            item["final_capability"] = "SUPPORTED"
        elif item.get("rest_capability") == "SUPPORTED" or item.get("status") == "PARTIAL":
            item["final_capability"] = "PARTIAL"
        else:
            item.setdefault("final_capability", "ERROR_RETRYABLE")

    live_final = [
        str(item.get("final_capability"))
        for item in result["symbols"]
        if str(item.get("symbol", "")) in LIVE_TARGET_SYMBOLS
    ]
    if probe_failed_before_classification:
        result["classification"] = "PRODUCTION_PUBLIC_PROBE_INCOMPLETE"
    elif websocket_ok and live_final and all(value == "SUPPORTED" for value in live_final):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_SUPPORTED"
    elif any(value in {"SUPPORTED", "PARTIAL"} for value in live_final):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_PARTIAL"
    elif any(value == "ERROR_FATAL" for value in live_final):
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_UNSUPPORTED"
    else:
        result["classification"] = "PRODUCTION_PUBLIC_TRADFI_UNSUPPORTED"
    result["live_shadow_gate"] = "PASS" if result["classification"] == "PRODUCTION_PUBLIC_TRADFI_SUPPORTED" else "NOT_PASS"
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
