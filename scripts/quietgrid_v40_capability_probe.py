from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quietgrid_v40.safety import CapabilityStatus, ExecutionLane, ExecutionSafetyPolicy

TARGET_SYMBOLS = ("SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT")
FALLBACK_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BCHUSDT")
TESTNET_REST = "https://testnet.binancefuture.com"


def _rules(item: dict[str, Any]) -> dict[str, Any]:
    filters = {str(f.get("filterType")): f for f in item.get("filters", [])}
    price = filters.get("PRICE_FILTER", {})
    lot = filters.get("LOT_SIZE", {})
    market_lot = filters.get("MARKET_LOT_SIZE", {})
    notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL", {}))
    return {
        "tickSize": price.get("tickSize"),
        "stepSize": lot.get("stepSize"),
        "minQty": lot.get("minQty"),
        "maxQty": lot.get("maxQty"),
        "marketMinQty": market_lot.get("minQty"),
        "marketMaxQty": market_lot.get("maxQty"),
        "minNotional": notional.get("minNotional"),
        "pricePrecision": item.get("pricePrecision"),
        "quantityPrecision": item.get("quantityPrecision"),
        "filterTypes": sorted(filters),
    }


async def probe(network: bool = True) -> dict[str, Any]:
    policy = ExecutionSafetyPolicy(ExecutionLane.TESTNET_EXECUTION, testnet_env="true", rest_url=TESTNET_REST)
    result: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": TESTNET_REST,
        "symbols": [],
        "authenticated": {"status": CapabilityStatus.SKIPPED_NO_CREDENTIALS.value},
        "production_private_api": "DISABLED",
        "safety": policy.describe(),
    }
    if not network:
        result["server_time"] = {"status": "NOT_PROBED"}
        result["symbols"] = [{"symbol": symbol, "status": "NOT_PROBED", "final_capability": "NOT_PROBED"} for symbol in TARGET_SYMBOLS]
        result["classification"] = "NOT_PROBED"
        return result
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            time_response = await client.get(f"{TESTNET_REST}/fapi/v1/time")
            time_response.raise_for_status()
            exchange_response = await client.get(f"{TESTNET_REST}/fapi/v1/exchangeInfo")
            exchange_response.raise_for_status()
            exchange_data = exchange_response.json()
            symbols = {str(item.get("symbol")): item for item in exchange_data.get("symbols", [])}
            result["server_time"] = {"status": CapabilityStatus.SUPPORTED.value, "serverTime": time_response.json().get("serverTime")}
            for symbol in TARGET_SYMBOLS:
                item = symbols.get(symbol)
                if item is None:
                    result["symbols"].append({"symbol": symbol, "exists": False, "status": CapabilityStatus.UNSUPPORTED.value, "final_capability": "TESTNET_TRADFI_UNSUPPORTED"})
                    continue
                active = str(item.get("status")) == "TRADING"
                result["symbols"].append({
                    "symbol": symbol,
                    "exists": True,
                    "status": item.get("status"),
                    "contractType": item.get("contractType"),
                    "orderTypes": item.get("orderTypes", []),
                    "rules": _rules(item),
                    "gtx_order_test": CapabilityStatus.SKIPPED_NO_CREDENTIALS.value,
                    "authenticated_testnet": CapabilityStatus.SKIPPED_NO_CREDENTIALS.value,
                    "final_capability": "TESTNET_TRADFI_SUPPORTED" if active else "TESTNET_TRADFI_UNSUPPORTED",
                })
            result["fallback_symbols"] = [{"symbol": symbol, "exists": symbol in symbols, "status": symbols.get(symbol, {}).get("status"), "evidence_role": "EXECUTION_INFRA_ONLY"} for symbol in FALLBACK_SYMBOLS]
    except (httpx.HTTPError, OSError, ValueError) as exc:
        result["server_time"] = {"status": CapabilityStatus.ERROR_RETRYABLE.value, "error": str(exc)}
        result["symbols"] = [{"symbol": symbol, "status": CapabilityStatus.ERROR_RETRYABLE.value, "final_capability": "NOT_PROBED"} for symbol in TARGET_SYMBOLS]
    statuses = [str(item.get("final_capability")) for item in result["symbols"]]
    if all(status == "TESTNET_TRADFI_SUPPORTED" for status in statuses):
        result["classification"] = "TESTNET_TRADFI_SUPPORTED"
    elif any(status == "TESTNET_TRADFI_SUPPORTED" for status in statuses):
        result["classification"] = "TESTNET_TRADFI_PARTIAL_SUPPORT"
    elif result.get("server_time", {}).get("status") == CapabilityStatus.ERROR_RETRYABLE.value:
        result["classification"] = "TESTNET_PROBE_ERROR_RETRYABLE"
    elif all(status == "NOT_PROBED" for status in statuses):
        result["classification"] = "NOT_PROBED"
    else:
        result["classification"] = "TESTNET_TRADFI_UNSUPPORTED_USE_DUAL_LANE"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="QuietGrid v4.0 public-only Binance Futures Testnet capability probe")
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--output-dir", default="reports/testnet-shadow-v4.0")
    args = parser.parse_args()
    result = asyncio.run(probe(network=not args.no_network))
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "capability-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Binance Futures Testnet Capability Probe", "", f"Classification: {result.get('classification', 'NOT_PROBED')}", "", "| Symbol | Exists | Status | Contract | Rules | GTX/order-test | Authenticated | Final |", "|---|---:|---|---|---|---|---|---|"]
    for item in result.get("symbols", []):
        lines.append(f"| {item['symbol']} | {item.get('exists', 'NOT_PROBED')} | {item.get('status', 'NOT_PROBED')} | {item.get('contractType', 'NOT_PROBED')} | {'YES' if item.get('rules') else 'NOT_PROBED'} | {item.get('gtx_order_test', 'NOT_PROBED')} | {item.get('authenticated_testnet', 'NOT_PROBED')} | {item.get('final_capability', 'NOT_PROBED')} |")
    lines += ["", "Production private API: DISABLED", "", "No production private endpoint was called.", ""]
    (out / "capability-probe.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
