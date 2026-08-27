from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolSessionContext:
    symbol: str
    calendar: str
    market_group: str
    capital_multiplier: float


SYMBOL_SESSIONS = {
    "SNDKUSDT": SymbolSessionContext("SNDKUSDT", "NYSE", "US_STOCK", 1.0),
    "MUUSDT": SymbolSessionContext("MUUSDT", "NYSE", "US_STOCK", 1.0),
    "SOXLUSDT": SymbolSessionContext("SOXLUSDT", "NYSE", "US_LEVERAGED_ETF", 0.5),
    "SKHYNIXUSDT": SymbolSessionContext("SKHYNIXUSDT", "XKRX", "KR_STOCK", 1.0),
}


def session_context(symbol: str) -> SymbolSessionContext:
    normalized = symbol.upper()
    try:
        return SYMBOL_SESSIONS[normalized]
    except KeyError as exc:
        raise ValueError(f"v4.1 frozen symbol has no session context: {symbol}") from exc


def capital_multiplier(symbol: str) -> float:
    return session_context(symbol).capital_multiplier
