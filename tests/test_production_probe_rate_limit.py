from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from quietgrid_v41.production_probe import probe


def _rate_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://fapi.binance.com/fapi/v1/depth")
    response = httpx.Response(
        status_code,
        request=request,
        headers={
            "Retry-After": "60",
            "X-MBX-USED-WEIGHT-1M": "1234",
        },
    )
    return httpx.HTTPStatusError(str(status_code), request=request, response=response)


class TrackedMarket:
    def __init__(self, *, fail_code: int | None = None, fail_symbol: str | None = None, fail_stage: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_code = fail_code
        self.fail_symbol = fail_symbol
        self.fail_stage = fail_stage

    def _maybe_fail(self, symbol: str, stage: str) -> None:
        if self.fail_symbol == symbol and self.fail_stage == stage:
            raise _rate_error(self.fail_code or 418)

    async def get_symbols(self):
        self.calls.append("exchangeInfo")
        return [
            {"symbol": symbol, "status": "TRADING", "contractType": "TRADIFI_PERPETUAL"}
            for symbol in ("SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT")
        ]

    async def get_server_time(self):
        self.calls.append("time")
        return 1

    async def get_symbol_rules(self, symbol):
        self.calls.append(f"rules:{symbol}")
        return {"tick_size": 0.01, "step_size": 0.01, "min_qty": 0.01, "min_notional": 5}

    async def get_24h_ticker(self, symbol):
        self.calls.append(f"ticker:{symbol}")
        self._maybe_fail(symbol, "ticker")
        return {"symbol": symbol, "lastPrice": "100"}

    async def get_orderbook_depth(self, symbol, limit):
        self.calls.append(f"depth:{symbol}")
        self._maybe_fail(symbol, "depth")
        return {"bids": [["99", "10"]], "asks": [["101", "10"]]}

    async def get_klines(self, symbol, interval, limit):
        self.calls.append(f"kline:{symbol}")
        self._maybe_fail(symbol, "kline")
        return [{"open": 100}]

    async def get_funding_context(self, symbol):
        self.calls.append(f"mark:{symbol}")
        self._maybe_fail(symbol, "mark")
        return {"markPrice": "100"}

    async def get_funding_rate(self, symbol):
        self.calls.append(f"funding:{symbol}")
        self._maybe_fail(symbol, "funding")
        return 0.0001

    async def close(self):
        return None


def _run_fail_fast(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status_code: int) -> tuple[dict, TrackedMarket, Path]:
    market = TrackedMarket(fail_code=status_code, fail_symbol="SNDKUSDT", fail_stage="depth")
    cooldown_path = tmp_path / "production-public-cooldown.json"
    monkeypatch.setattr("quietgrid_v41.production_probe.COOLDOWN_FILE", cooldown_path)

    def fake_ws(symbols, timeout):
        raise AssertionError("websocket probe must be skipped after REST rate limit")

    monkeypatch.setattr("quietgrid_v41.production_probe._public_websocket_probe", fake_ws)
    result = asyncio.run(probe(network=True, websocket=True, market_data=market))
    return result, market, cooldown_path


def _assert_fail_fast(result: dict, market: TrackedMarket, status_code: int) -> None:
    assert result["classification"] == "PRODUCTION_PUBLIC_PROBE_INCOMPLETE_RATE_LIMITED"
    assert result["websocket"]["status"] == "SKIPPED_DUE_TO_RATE_LIMIT"
    assert result["rate_limited_stages"][0]["status_code"] == status_code
    assert result["rate_limited_stages"][0]["stage"] == "SNDKUSDT:depth"
    assert "kline:SNDKUSDT" not in market.calls
    assert "rules:MUUSDT" not in market.calls
    assert "ticker:SOXLUSDT" not in market.calls
    assert "rules:SKHYNIXUSDT" not in market.calls
    assert "ticker:SKHYNIXUSDT" not in market.calls
    sndk = next(item for item in result["symbols"] if item["symbol"] == "SNDKUSDT")
    assert sndk["final_capability"] == "PARTIAL_RATE_LIMITED"
    assert all(
        item["final_capability"] == "NOT_RUN_DUE_TO_RATE_LIMIT"
        for item in result["symbols"]
        if item["symbol"] != "SNDKUSDT"
    )


def test_probe_first_418_aborts_all_remaining_rest_requests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, market, _ = _run_fail_fast(monkeypatch, tmp_path, 418)
    _assert_fail_fast(result, market, 418)


def test_probe_first_429_aborts_all_remaining_rest_requests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, market, _ = _run_fail_fast(monkeypatch, tmp_path, 429)
    _assert_fail_fast(result, market, 429)


def test_probe_does_not_continue_next_symbol_after_rate_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, market, _ = _run_fail_fast(monkeypatch, tmp_path, 418)
    _assert_fail_fast(result, market, 418)
    assert "ticker:MUUSDT" not in market.calls
    assert "depth:SOXLUSDT" not in market.calls


def test_probe_skips_websocket_after_rest_rate_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, _, _ = _run_fail_fast(monkeypatch, tmp_path, 418)
    assert result["websocket"]["status"] == "SKIPPED_DUE_TO_RATE_LIMIT"
    assert "cooldown_until" in result["websocket"]


def test_probe_persists_retry_after_cooldown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    result, _, cooldown_path = _run_fail_fast(monkeypatch, tmp_path, 418)
    cooldown = json.loads(cooldown_path.read_text(encoding="utf-8"))
    assert cooldown["status_code"] == 418
    assert cooldown["retry_after_seconds"] == 60
    assert cooldown["stage"] == "SNDKUSDT:depth"
    assert datetime.fromisoformat(cooldown["cooldown_until"]) > datetime.now(timezone.utc)
    assert any(key.lower() == "x-mbx-used-weight-1m" for key in cooldown["headers"])
    assert result["cooldown"]["retry_after_seconds"] == 60


def test_probe_does_not_request_during_active_cooldown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    cooldown_path = tmp_path / "production-public-cooldown.json"
    cooldown_path.write_text(
        json.dumps(
            {
                "detected_at": now.isoformat(),
                "status_code": 418,
                "retry_after_seconds": 3600,
                "cooldown_until": (now + timedelta(hours=1)).isoformat(),
                "stage": "SNDKUSDT:depth",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("quietgrid_v41.production_probe.COOLDOWN_FILE", cooldown_path)
    result = asyncio.run(probe(network=True, websocket=True, market_data=object()))
    assert result["classification"] == "PROBE_COOLDOWN_ACTIVE"
    assert result["websocket"]["status"] == "SKIPPED_DUE_TO_COOLDOWN"
    assert all(item["final_capability"] == "NOT_PROBED_COOLDOWN" for item in result["symbols"])


def test_research_only_symbol_does_not_block_live_shadow_gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    market = TrackedMarket()
    monkeypatch.setattr(
        "quietgrid_v41.production_probe.COOLDOWN_FILE",
        tmp_path / "production-public-cooldown.json",
    )

    async def fake_ws(symbols, timeout):
        return {"status": "SUPPORTED", "received": {"trade": True, "bookTicker": True}}

    monkeypatch.setattr("quietgrid_v41.production_probe._public_websocket_probe", fake_ws)
    result = asyncio.run(probe(network=True, websocket=True, market_data=market))
    assert result["classification"] == "PRODUCTION_PUBLIC_TRADFI_SUPPORTED"
    assert result["live_shadow_gate"] == "PASS"
    research_only = next(item for item in result["symbols"] if item["symbol"] == "SKHYNIXUSDT")
    assert research_only["final_capability"] == "RESEARCH_ONLY"
    assert "ticker:SKHYNIXUSDT" not in market.calls
    assert "depth:SKHYNIXUSDT" not in market.calls
    assert "kline:SKHYNIXUSDT" not in market.calls
    assert "mark:SKHYNIXUSDT" not in market.calls
    assert "funding:SKHYNIXUSDT" not in market.calls
