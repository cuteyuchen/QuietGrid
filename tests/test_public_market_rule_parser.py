from __future__ import annotations

import pytest

from exchange.public_market import ProductionPublicMarketData, PublicMarketRuleParseError


def _adapter() -> ProductionPublicMarketData:
    return ProductionPublicMarketData()


def _symbol_info(symbol: str, filters: list[dict]) -> dict:
    return {
        "symbol": symbol,
        "status": "TRADING",
        "contractType": "TRADIFI_PERPETUAL",
        "filters": filters,
    }


def test_public_market_parses_futures_min_notional_notional_key() -> None:
    rules = _adapter().get_symbol_rules_from_exchange_info(
        _symbol_info(
            "SNDKUSDT",
            [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        )
    )

    assert rules["min_notional"] == 5.0
    assert rules["min_notional_filter_type"] == "MIN_NOTIONAL"
    assert rules["min_notional_source_field"] == "notional"
    assert rules["min_notional_available"] is True


def test_public_market_parses_notional_min_notional_key() -> None:
    rules = _adapter().get_symbol_rules_from_exchange_info(
        _symbol_info(
            "MUUSDT",
            [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                {"filterType": "NOTIONAL", "minNotional": "7.5"},
            ],
        )
    )

    assert rules["min_notional"] == 7.5
    assert rules["min_notional_filter_type"] == "NOTIONAL"
    assert rules["min_notional_source_field"] == "minNotional"


def test_public_market_supports_legacy_min_notional_min_notional_fallback() -> None:
    rules = _adapter().get_symbol_rules_from_exchange_info(
        _symbol_info(
            "SOXLUSDT",
            [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "6"},
            ],
        )
    )

    assert rules["min_notional"] == 6.0
    assert rules["min_notional_source_field"] == "minNotional"


def test_public_market_rejects_invalid_present_notional_filter() -> None:
    with pytest.raises(PublicMarketRuleParseError, match="SNDKUSDT.*MIN_NOTIONAL"):
        _adapter().get_symbol_rules_from_exchange_info(
            _symbol_info("SNDKUSDT", [{"filterType": "MIN_NOTIONAL", "foo": "5"}])
        )


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "0", "-5"])
def test_public_market_rejects_non_positive_or_non_finite_notional(raw: str) -> None:
    with pytest.raises(PublicMarketRuleParseError, match="invalid notional"):
        _adapter().get_symbol_rules_from_exchange_info(
            _symbol_info("MUUSDT", [{"filterType": "MIN_NOTIONAL", "notional": raw}])
        )


def test_public_market_missing_notional_filter_is_distinct_from_parse_failure() -> None:
    rules = _adapter().get_symbol_rules_from_exchange_info(
        _symbol_info(
            "SNDKUSDT",
            [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
            ],
        )
    )

    assert rules["min_notional"] == 0.0
    assert rules["min_notional_available"] is False
    assert rules["min_notional_filter_type"] is None
    assert rules["min_notional_source_field"] is None


@pytest.mark.parametrize("symbol", ["SNDKUSDT", "MUUSDT", "SOXLUSDT", "SKHYNIXUSDT"])
def test_public_market_parses_real_tradifi_min_notional(symbol: str) -> None:
    rules = _adapter().get_symbol_rules_from_exchange_info(
        _symbol_info(
            symbol,
            [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01000"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
        )
    )

    assert rules["min_notional"] == 5.0
    assert rules["min_notional_filter_type"] == "MIN_NOTIONAL"
    assert rules["min_notional_source_field"] == "notional"
