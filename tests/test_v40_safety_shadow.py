from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from exchange.shadow import PAPER_BASELINE, ShadowBroker
from quietgrid_v40.frozen import load_frozen_31111
from quietgrid_v40.safety import ExecutionLane, ExecutionSafetyPolicy, LaneConfigurationError, ProductionPrivateApiBlocked


def test_frozen_candidate_integrity() -> None:
    frozen = load_frozen_31111(".")
    assert frozen.candidate_id == "31111-NEUTRAL"
    assert frozen.candidate_sha == "c65c75506f4070608ddbfb9a9b3731dc8dab2fee0261c33bda36278a495a1774"
    assert frozen.exposure_cutoff == "2026-08-08T20:45:23.438783+00:00"
    assert frozen.economic_leverage == 1
    assert frozen.symbols == ("SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT")


def test_production_private_and_missing_lane_are_fail_closed() -> None:
    ExecutionSafetyPolicy(ExecutionLane.TRADFI_SHADOW_BASELINE).require_public_read("https://fapi.binance.com/fapi/v1/exchangeInfo")
    try:
        ExecutionSafetyPolicy(ExecutionLane.TRADFI_SHADOW_BASELINE).require_public_read("https://fapi.binance.com/fapi/v1/account")
    except ProductionPrivateApiBlocked:
        pass
    else:
        raise AssertionError("production private endpoint was not blocked")
    try:
        ExecutionSafetyPolicy(None).require_paper_mutation()
    except LaneConfigurationError:
        pass
    else:
        raise AssertionError("missing lane was not blocked")
    assert ExecutionSafetyPolicy(None).describe()["data_environment"] == "NONE"
    try:
        ExecutionSafetyPolicy(ExecutionLane.PUBLIC_DATA_ONLY).require_public_read("https://fapi.binance.com/fapi/v1/tickerfoo")
    except ProductionPrivateApiBlocked:
        pass
    else:
        raise AssertionError("private path sharing a public prefix was not blocked")


def test_shadow_touch_partial_fill_and_idempotency(tmp_path) -> None:
    async def scenario() -> None:
        broker = ShadowBroker(tmp_path / "shadow.db", PAPER_BASELINE)
        broker._last_market["MUUSDT"] = {"bid": 99.0, "ask": 101.0, "last": 100.0}
        order = await broker.place_limit_order_post_only("MUUSDT", "BUY", 99.5, 10.0, "qg-v40-test-1")
        assert order["status"] == "NEW"
        at = datetime.now(timezone.utc) + timedelta(seconds=1)
        assert await broker.process_market_event("MUUSDT", {"timestamp": at, "trade_price": 99.5, "trade_qty": 1.0}) == []
        fills = await broker.process_market_event("MUUSDT", {"timestamp": at + timedelta(seconds=1), "trade_price": 99.4, "trade_qty": 100.0})
        assert fills and fills[0]["status"] == "FILLED"
        duplicate = await broker.place_limit_order_post_only("MUUSDT", "BUY", 99.5, 10.0, "qg-v40-test-1")
        assert duplicate["orderId"] == order["orderId"]
        assert (await broker.get_position("MUUSDT"))["qty"] == 10.0
    asyncio.run(scenario())


def test_marketable_post_only_is_rejected(tmp_path) -> None:
    async def scenario() -> None:
        broker = ShadowBroker(tmp_path / "shadow.db", PAPER_BASELINE)
        broker._last_market["MUUSDT"] = {"bid": 99.0, "ask": 101.0}
        result = await broker.place_limit_order_post_only("MUUSDT", "BUY", 101.0, 1.0, "qg-v40-test-reject")
        assert result["status"] == "REJECTED"
        assert result["rejectReason"] == "POST_ONLY_MARKETABLE"
    asyncio.run(scenario())
