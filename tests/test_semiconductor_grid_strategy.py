from __future__ import annotations

from core.models import GridDirectionMode
from strategy.semiconductor_grid import RESEARCH_SYMBOLS, LongSignalConfig, default_grid_profiles, evaluate_long_signal, symbol_profiles_from_mapping


def _trend_with_pullbacks(count: int = 70) -> list[dict[str, float]]:
    price = 100.0
    rows = []
    for index in range(count):
        price *= 1.0015 if index % 3 else 0.9995
        rows.append({"open":price,"high":price*1.001,"low":price*0.999,"close":price})
    return rows


def test_registered_profiles_keep_neutral_and_long_evidence_separate() -> None:
    profiles = {item.name: item for item in default_grid_profiles()}
    assert profiles["N20"].direction_mode == GridDirectionMode.NEUTRAL
    assert profiles["N100"].min_grid_num == 20
    assert profiles["N100"].max_grid_num == 100
    assert profiles["L100"].direction_mode == GridDirectionMode.LONG
    assert profiles["L100"].requires_long_signal is True


def test_long_signal_accepts_uptrend_with_repeated_pullbacks() -> None:
    decision = evaluate_long_signal(_trend_with_pullbacks(), LongSignalConfig(minimum_long_return_pct=0.005,minimum_directional_efficiency=0.10,maximum_directional_efficiency=0.90,minimum_reversal_ratio=0.20,maximum_short_move_sigma=10.0))
    assert decision.allowed is True
    assert decision.long_return_pct > 0
    assert decision.reversal_ratio >= 0.20


def test_symbol_registry_includes_only_pre_registered_research_pool() -> None:
    mapping = {symbol:{"market_group":"KR_STOCK" if symbol=="SKHYNIXUSDT" else "US_STOCK","calendar_name":"XKRX" if symbol=="SKHYNIXUSDT" else "NYSE","market_timezone":"Asia/Seoul" if symbol=="SKHYNIXUSDT" else "America/New_York","reference_open_time":None if symbol=="SKHYNIXUSDT" else "04:00","allow_long":symbol=="SKHYNIXUSDT"} for symbol in RESEARCH_SYMBOLS}
    profiles = symbol_profiles_from_mapping(mapping)
    assert tuple(profiles) == RESEARCH_SYMBOLS
    assert profiles["SKHYNIXUSDT"].calendar_name == "XKRX"
    assert profiles["SKHYNIXUSDT"].allow_long is True
    assert profiles["SNDKUSDT"].allow_long is False
