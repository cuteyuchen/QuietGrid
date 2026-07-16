from __future__ import annotations

import asyncio
from typing import Any

from exchange.mock import MockExchangeClient
from strategy.selector import SelectionConfig, Selector


class SelectionExchange(MockExchangeClient):
    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        volumes = {
            "AAPLUSDT": "1000",
            "MSFTUSDT": "500",
            "TSLAPREUSDT": "100000",
        }
        return {"symbol": symbol, "quoteVolume": volumes.get(symbol, "1")}

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]:
        depths = {
            "AAPLUSDT": ("100", "20"),
            "MSFTUSDT": ("100", "5"),
            "TSLAPREUSDT": ("100", "100"),
        }
        price, qty = depths.get(symbol, ("100", "1"))
        return {"bids": [[price, qty]], "asks": [[price, qty]]}


class MixedMarketExchange(SelectionExchange):
    def __init__(self) -> None:
        super().__init__()
        self.symbols = [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "AAPLUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "MSFTUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        ]

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        volumes = {
            "BTCUSDT": "100000000",
            "AAPLUSDT": "1000",
            "MSFTUSDT": "500",
        }
        return {"symbol": symbol, "quoteVolume": volumes.get(symbol, "1")}


class MixedContractTypeExchange(SelectionExchange):
    def __init__(self) -> None:
        super().__init__()
        self.symbols = [
            {"symbol": "AAPLUSDT", "status": "TRADING", "contractType": "CURRENT_QUARTER"},
            {"symbol": "MSFTUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "TSLAPREUSDT", "status": "TRADING"},
        ]


class MissingRequiredSymbolFieldsExchange(SelectionExchange):
    def __init__(self) -> None:
        super().__init__()
        self.symbols = [
            {"symbol": "AAPLUSDT", "contractType": "PERPETUAL"},
            {"symbol": "MSFTUSDT", "status": "TRADING"},
            {"symbol": "TSLAPREUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        ]


class InvalidLiquidityExchange(SelectionExchange):
    def __init__(self) -> None:
        super().__init__()
        self.symbols = [
            {"symbol": "AAPLUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "MSFTUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "TSLAPREUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        ]

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        volumes = {
            "AAPLUSDT": "nan",
            "MSFTUSDT": "1000",
            "TSLAPREUSDT": "500",
        }
        return {"symbol": symbol, "quoteVolume": volumes[symbol]}

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]:
        if symbol == "TSLAPREUSDT":
            return {"bids": [["100", "inf"]], "asks": [["101", "1"]]}
        return {"bids": [["100", "2"]], "asks": [["101", "2"]]}


class RaisingLiquidityExchange(SelectionExchange):
    def __init__(self) -> None:
        super().__init__()
        self.symbols = [
            {"symbol": "AAPLUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "MSFTUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
            {"symbol": "TSLAPREUSDT", "status": "TRADING", "contractType": "PERPETUAL"},
        ]

    async def get_24h_ticker(self, symbol: str) -> dict[str, Any]:
        if symbol == "AAPLUSDT":
            raise ValueError("invalid ticker response")
        return await super().get_24h_ticker(symbol)

    async def get_orderbook_depth(self, symbol: str, limit: int) -> dict[str, Any]:
        if symbol == "TSLAPREUSDT":
            raise ValueError("invalid orderbook response")
        return await super().get_orderbook_depth(symbol, limit)


def test_selector_filters_blacklist_and_inactive_symbols() -> None:
    async def run() -> None:
        selector = Selector(
            SelectionExchange(),
            SelectionConfig(max_concurrent=3, symbol_blacklist=("TSLAPREUSDT",)),
        )

        selected = await selector.select()

        assert [item.symbol for item in selected] == ["AAPLUSDT", "MSFTUSDT"]
        assert all(item.symbol != "BADUSDT" for item in selected)
        assert selected[0].score >= selected[1].score

    asyncio.run(run())


def test_selector_skips_symbols_with_invalid_liquidity_snapshots() -> None:
    async def run() -> None:
        selector = Selector(InvalidLiquidityExchange(), SelectionConfig(max_concurrent=3))

        selected = await selector.select()

        assert [item.symbol for item in selected] == ["MSFTUSDT"]
        assert selected[0].volume_24h == 1000
        assert selected[0].depth_usdt == 402
        assert selected[0].bid_price == 100
        assert selected[0].ask_price == 101
        assert selected[0].spread_pct > 0

    asyncio.run(run())


def test_selector_skips_symbols_when_liquidity_fetch_raises_value_error() -> None:
    async def run() -> None:
        selector = Selector(RaisingLiquidityExchange(), SelectionConfig(max_concurrent=3))

        selected = await selector.select()

        assert [item.symbol for item in selected] == ["MSFTUSDT"]

    asyncio.run(run())


def test_selector_filters_symbols_outside_allowlist() -> None:
    async def run() -> None:
        selector = Selector(
            MixedMarketExchange(),
            SelectionConfig(max_concurrent=3, symbol_allowlist=("AAPLUSDT", "MSFTUSDT")),
        )

        candidates = await selector.candidate_symbols()
        selected = await selector.select()

        assert candidates == ["AAPLUSDT", "MSFTUSDT"]
        assert [item.symbol for item in selected] == ["AAPLUSDT", "MSFTUSDT"]

    asyncio.run(run())


def test_selector_normalizes_allowlist_and_blacklist_symbols() -> None:
    async def run() -> None:
        selector = Selector(
            MixedMarketExchange(),
            SelectionConfig(
                max_concurrent=3,
                symbol_allowlist=(" AAPLUSDT ", " MSFTUSDT "),
                symbol_blacklist=(" msftusdt ",),
            ),
        )

        candidates = await selector.candidate_symbols()

        assert candidates == ["AAPLUSDT"]

    asyncio.run(run())


def test_selector_filters_non_perpetual_contracts_when_contract_type_is_available() -> None:
    async def run() -> None:
        selector = Selector(
            MixedContractTypeExchange(),
            SelectionConfig(max_concurrent=3),
        )

        candidates = await selector.candidate_symbols()

        assert candidates == ["MSFTUSDT"]

    asyncio.run(run())


def test_selector_filters_symbols_missing_status_or_contract_type() -> None:
    async def run() -> None:
        selector = Selector(
            MissingRequiredSymbolFieldsExchange(),
            SelectionConfig(max_concurrent=3),
        )

        candidates = await selector.candidate_symbols()

        assert candidates == ["TSLAPREUSDT"]

    asyncio.run(run())


def test_selector_respects_max_concurrent() -> None:
    async def run() -> None:
        selector = Selector(SelectionExchange(), SelectionConfig(max_concurrent=1))

        selected = await selector.select()

        assert len(selected) == 1
        assert selected[0].symbol == "TSLAPREUSDT"

    asyncio.run(run())


def test_selector_score_candidates_returns_full_ranked_liquidity_board() -> None:
    async def run() -> None:
        selector = Selector(SelectionExchange(), SelectionConfig(max_concurrent=1))

        scored = await selector.score_candidates()
        selected = await selector.select()

        assert [item.symbol for item in scored] == ["TSLAPREUSDT", "AAPLUSDT", "MSFTUSDT"]
        assert [item.symbol for item in selected] == ["TSLAPREUSDT"]
        assert scored[0].bid_price == 100
        assert scored[0].ask_price == 100
        assert scored[0].spread_pct == 0

    asyncio.run(run())
