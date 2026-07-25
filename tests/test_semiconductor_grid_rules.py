from __future__ import annotations

import inspect

import httpx

from scripts.freeze_semiconductor_grid_rules import extract_rule_snapshots


def test_exchange_info_rules_are_normalized() -> None:
    payload = {"symbols":[{"symbol":"SNDKUSDT","status":"TRADING","contractType":"PERPETUAL","onboardDate":123,"baseAsset":"SNDK","quoteAsset":"USDT","pricePrecision":2,"quantityPrecision":3,"orderTypes":["LIMIT","MARKET"],"timeInForce":["GTC","GTX"],"filters":[{"filterType":"PRICE_FILTER","tickSize":"0.10"},{"filterType":"LOT_SIZE","stepSize":"0.001","minQty":"0.001"},{"filterType":"MIN_NOTIONAL","notional":"5"}]}]}
    result = extract_rule_snapshots(payload, ["SNDKUSDT"])
    assert result["SNDKUSDT"]["tick_size"] == 0.1
    assert result["SNDKUSDT"]["step_size"] == 0.001
    assert result["SNDKUSDT"]["min_notional"] == 5.0
    assert "GTX" in result["SNDKUSDT"]["time_in_force"]


def test_httpx_proxy_keyword_matches_installed_client_signature() -> None:
    keyword = (
        "proxy"
        if "proxy" in inspect.signature(httpx.Client).parameters
        else "proxies"
    )
    assert keyword in inspect.signature(httpx.Client).parameters
