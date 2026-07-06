from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from exchange.base import ExchangeClient


@dataclass(frozen=True)
class SelectionConfig:
    max_concurrent: int = 3
    symbol_blacklist: tuple[str, ...] = ()
    symbol_allowlist: tuple[str, ...] = ()
    volume_weight: float = 0.7
    depth_weight: float = 0.3
    depth_levels: int = 5


@dataclass(frozen=True)
class SelectionScore:
    symbol: str
    score: float
    volume_score: float
    depth_score: float
    volume_24h: float
    depth_usdt: float


class Selector:
    def __init__(self, exchange: ExchangeClient, config: SelectionConfig) -> None:
        if config.volume_weight + config.depth_weight <= 0:
            raise ValueError("选币权重之和必须大于 0。")
        self.exchange = exchange
        self.config = config

    async def select(self) -> list[SelectionScore]:
        candidates = await self.candidate_symbols()
        snapshots: list[tuple[str, float, float]] = []
        for symbol in candidates:
            try:
                ticker = await self.exchange.get_24h_ticker(symbol)
                orderbook = await self.exchange.get_orderbook_depth(symbol, self.config.depth_levels)
                volume = _ticker_volume(ticker)
                depth = _depth_usdt(orderbook, self.config.depth_levels)
            except ValueError:
                continue
            snapshots.append((symbol, volume, depth))

        if not snapshots:
            return []

        max_volume = max(item[1] for item in snapshots) or 1.0
        max_depth = max(item[2] for item in snapshots) or 1.0
        weight_sum = self.config.volume_weight + self.config.depth_weight

        scored = []
        for symbol, volume, depth in snapshots:
            volume_score = volume / max_volume
            depth_score = depth / max_depth
            score = (
                self.config.volume_weight * volume_score + self.config.depth_weight * depth_score
            ) / weight_sum
            scored.append(
                SelectionScore(
                    symbol=symbol,
                    score=score,
                    volume_score=volume_score,
                    depth_score=depth_score,
                    volume_24h=volume,
                    depth_usdt=depth,
                )
            )

        return sorted(scored, key=lambda item: item.score, reverse=True)[: self.config.max_concurrent]

    async def candidate_symbols(self) -> list[str]:
        blacklist = _normalized_symbols(self.config.symbol_blacklist)
        allowlist = _normalized_symbols(self.config.symbol_allowlist)
        symbols = await self.exchange.get_symbols()
        candidates = []
        for item in symbols:
            symbol = str(item.get("symbol", ""))
            normalized_symbol = symbol.strip().upper()
            status = str(item.get("status", ""))
            if not normalized_symbol.endswith("USDT"):
                continue
            if not _is_perpetual_contract(item):
                continue
            if allowlist and normalized_symbol not in allowlist:
                continue
            if normalized_symbol in blacklist:
                continue
            if status != "TRADING":
                continue
            candidates.append(symbol)
        return candidates


def _ticker_volume(ticker: dict[str, Any]) -> float:
    for key in ("quoteVolume", "volume_24h", "quote_volume"):
        if key in ticker:
            return _non_negative_float(ticker[key], key)
    return 0.0


def _normalized_symbols(symbols: tuple[str, ...]) -> set[str]:
    return {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}


def _is_perpetual_contract(symbol_info: dict[str, Any]) -> bool:
    contract_type = symbol_info.get("contractType")
    return str(contract_type) == "PERPETUAL"


def _depth_usdt(orderbook: dict[str, Any], levels: int) -> float:
    total = 0.0
    for side in ("bids", "asks"):
        for price, qty, *_ in orderbook.get(side, [])[:levels]:
            total += _positive_float(price, f"{side}.price") * _non_negative_float(qty, f"{side}.qty")
    return total


def _positive_float(value: Any, label: str) -> float:
    number = _finite_float(value, label)
    if number <= 0:
        raise ValueError(f"invalid {label}: {value}")
    return number


def _non_negative_float(value: Any, label: str) -> float:
    number = _finite_float(value, label)
    if number < 0:
        raise ValueError(f"invalid {label}: {value}")
    return number


def _finite_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {label}: {value}") from exc
    if not isfinite(number):
        raise ValueError(f"invalid {label}: {value}")
    return number
