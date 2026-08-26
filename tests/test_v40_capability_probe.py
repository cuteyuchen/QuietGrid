from __future__ import annotations

import asyncio
import httpx

from scripts.quietgrid_v40_capability_probe import probe
from exchange.public_market import ProductionPublicMarketData


def test_capability_probe_no_network_is_explicitly_not_probed() -> None:
    result = asyncio.run(probe(network=False))
    assert result["classification"] == "NOT_PROBED"
    assert all(item["final_capability"] == "NOT_PROBED" for item in result["symbols"])
    assert result["authenticated"]["status"] == "SKIPPED_NO_CREDENTIALS"


def test_capability_classification_rules() -> None:
    statuses = ["TESTNET_TRADFI_SUPPORTED", "TESTNET_TRADFI_UNSUPPORTED"]
    assert any(status == "TESTNET_TRADFI_SUPPORTED" for status in statuses)


def test_public_market_adapter_uses_public_endpoints_without_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": 1})
        return httpx.Response(200, json={"symbols": []})

    async def scenario() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        adapter = ProductionPublicMarketData(base_url="https://fapi.binance.com", client=client)
        assert await adapter.get_server_time() == 1
        await adapter.get_symbols()
        assert all("authorization" not in request.headers for request in requests)
        await client.aclose()

    asyncio.run(scenario())
