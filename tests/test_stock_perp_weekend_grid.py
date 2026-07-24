from __future__ import annotations

import csv

import pytest

from scripts.stock_perp_common import SEED_VALUES
from scripts.stock_perp_market_hypothesis import (
    DOWNSTREAM_CSV_FIELDS,
    NOT_RUN_STATUS,
    _data_inventory,
    _tier_classification,
    _validate_frozen_inputs,
    _write_not_run_artifacts,
)


def _valid_inputs():
    discovery = {
        "data_previously_viewed": True,
        "tier_counts": {"TIER_A_CORE": 1, "TIER_A_SHORT": 1, "EXCLUDED": 1},
        "symbols": [
            {"symbol": "AAAUSDT", "tier": "TIER_A_CORE", "exclusion_reasons": []},
            {
                "symbol": "BBBUSDT",
                "tier": "TIER_A_SHORT",
                "exclusion_reasons": ["insufficient_complete_months_or_weekends"],
            },
            {
                "symbol": "CCCUSDT",
                "tier": "EXCLUDED",
                "exclusion_reasons": ["no_nasdaq_or_nyse_listing_match"],
            },
        ],
    }
    data_manifest = {
        "data_previously_viewed": True,
        "direction_mode": "NEUTRAL",
        "leverage": 1,
        "production_defaults_changed": False,
        "assets": {
            "AAAUSDT": {
                "rules": {"tick_size": 0.01},
                "files": {
                    "klines": {
                        "path": "AAAUSDT-1m.csv",
                        "sha256": "a" * 64,
                        "size_bytes": 10,
                        "row_count": 1,
                    }
                },
            }
        },
    }
    windows = {
        "random_seeds": list(SEED_VALUES),
        "overlap_audit": {"passed": True},
    }
    audit = {"passed": True}
    return discovery, data_manifest, windows, audit


def test_h1_refuses_unvalidated_or_overlapping_frozen_inputs() -> None:
    discovery, data_manifest, windows, audit = _valid_inputs()

    _validate_frozen_inputs(discovery, data_manifest, windows, audit)

    audit["passed"] = False
    with pytest.raises(ValueError, match="数据审计未通过"):
        _validate_frozen_inputs(discovery, data_manifest, windows, audit)
    audit["passed"] = True
    windows["overlap_audit"]["passed"] = False
    with pytest.raises(ValueError, match="重叠审计未通过"):
        _validate_frozen_inputs(discovery, data_manifest, windows, audit)


def test_results_inventory_keeps_tiers_rules_and_every_file_hash() -> None:
    discovery, data_manifest, _windows, _audit = _valid_inputs()

    tiers = _tier_classification(discovery)
    files, rules = _data_inventory(data_manifest)

    assert tiers["symbols"]["TIER_A_CORE"] == ["AAAUSDT"]
    assert tiers["reasons"]["CCCUSDT"] == ["no_nasdaq_or_nyse_listing_match"]
    assert files == [
        {
            "symbol": "AAAUSDT",
            "kind": "klines",
            "path": "AAAUSDT-1m.csv",
            "sha256": "a" * 64,
            "size_bytes": 10,
            "row_count": 1,
        }
    ]
    assert rules == {"AAAUSDT": {"tick_size": 0.01}}


def test_h1_failure_writes_only_explicit_not_run_placeholders(tmp_path) -> None:
    hashes = _write_not_run_artifacts(
        tmp_path,
        "STOCK_PERP_WEEKEND_LOW_VOLATILITY_HYPOTHESIS_NOT_SUPPORTED",
    )

    assert set(hashes) == {*DOWNSTREAM_CSV_FIELDS, "baseline-comparison.md"}
    for filename in DOWNSTREAM_CSV_FIELDS:
        with (tmp_path / filename).open("r", encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        assert row["status"] == NOT_RUN_STATUS
        assert row["reason"] == "H1_FAILED_STOP_RULE"
    report = (tmp_path / "baseline-comparison.md").read_text(encoding="utf-8")
    assert NOT_RUN_STATUS in report
    assert "B0–B5" in report
