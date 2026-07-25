from __future__ import annotations

import math
from datetime import datetime, timezone

from scripts.rebuild_stock_perp_windows_h1_1 import (
    CORE_SYMBOLS,
    CRYPTO_SENSITIVE_EQUITY,
    SEED_VALUES,
    TRADITIONAL_EQUITY,
)
from scripts.stock_perp_market_hypothesis_h1_1 import (
    GROSS_CYCLE_EDGE,
    PER_HOUR_FIELDS,
    _bootstrap_difference,
    _calendar_blocks,
    _comparison_rows,
    _diagnostic_rows,
    _feature_row,
    _hourly_realized_volatility,
    _metric_values,
    _technical_checks,
)


UTC = timezone.utc


def _market_rows(
    start_ms: int,
    closes: list[float],
    *,
    previous_close: float,
) -> list[dict[str, object]]:
    result = []
    left = previous_close
    for index, close in enumerate(closes):
        result.append(
            {
                "open_time": start_ms + index * 60_000,
                "close_time": start_ms + (index + 1) * 60_000 - 1,
                "open": left,
                "high": max(left, close) * 1.0001,
                "low": min(left, close) * 0.9999,
                "close": close,
                "volume": 1.0,
                "quote_volume": close,
                "trade_count": 2,
            }
        )
        left = close
    return result


def _feature(
    *,
    symbol: str,
    group: str,
    calendar_key: str,
    seed: int | str = "",
    month: str = "2026-03",
    value: float = 1.0,
) -> dict[str, object]:
    row: dict[str, object] = {
        "window_id": f"{symbol}-{group}-{calendar_key}",
        "symbol": symbol,
        "asset_group": (
            "TRADITIONAL_EQUITY"
            if symbol in TRADITIONAL_EQUITY
            else "CRYPTO_SENSITIVE_EQUITY"
        ),
        "group": group,
        "seed": seed,
        "calendar_key": calendar_key,
        "month": month,
        "split": "RESEARCH_DEVELOPMENT",
        "listing_stage": "LISTING_AFTER_30_DAYS",
        "window_type": "REGULAR_WEEKEND" if group == "W" else group,
        "status": "READY",
        "window_realized_volatility": value,
        "hourly_realized_volatility": value,
        "directional_efficiency": value,
        "completed_grid_cycles_per_hour": value,
        "fee_adjusted_cycle_capacity_per_hour": value,
        "zero_trade_ratio": value,
        "trades_per_hour": value,
        "base_volume_per_hour": value,
        "quote_volume_per_hour": value,
        "median_trade_size": value,
        "aggtrade_event_count_per_hour": value,
    }
    for field in PER_HOUR_FIELDS:
        row.setdefault(field, value)
    return row


def test_hourly_rv_is_duration_invariant_for_identical_minute_returns() -> None:
    minute_return = 0.001
    short = [100.0 * math.exp(minute_return * (index + 1)) for index in range(60)]
    long = [100.0 * math.exp(minute_return * (index + 1)) for index in range(120)]

    short_window, short_hourly = _hourly_realized_volatility(short, 100.0)
    long_window, long_hourly = _hourly_realized_volatility(long, 100.0)

    assert math.isclose(short_hourly, long_hourly, rel_tol=1e-12)
    assert math.isclose(long_window, short_window * math.sqrt(2), rel_tol=1e-12)


def test_cycles_and_capacity_per_hour_use_tradable_hours() -> None:
    closes = [100.2, 99.9, 100.2, 99.9] * 15
    rows = _market_rows(1_700_000_000_000, closes, previous_close=100.0)

    metrics = _metric_values(
        rows,
        previous_close=100.0,
        funding=[],
        mark={},
        premium={},
        agg_trades=None,
    )

    assert metrics["tradable_hours"] == 1.0
    assert metrics["completed_grid_cycles_per_hour"] == metrics["completed_grid_cycles"]
    assert metrics["fee_adjusted_cycle_capacity_per_hour"] == (
        metrics["completed_grid_cycles_per_hour"] * GROSS_CYCLE_EDGE
    )


def test_feature_uses_last_observation_close_as_first_return_base() -> None:
    start = 1_700_000_000_000
    observation = [100.0] * 180
    tradable = [100.0 * math.exp(0.001 * (index + 1)) for index in range(60)]
    rows = _market_rows(start, observation + tradable, previous_close=100.0)
    window = {
        "window_id": "W-1",
        "symbol": "AMZNUSDT",
        "asset_group": "TRADITIONAL_EQUITY",
        "group": "W",
        "seed": "",
        "calendar_key": "W:test",
        "month": "2026-03",
        "split": "RESEARCH_DEVELOPMENT",
        "listing_stage": "LISTING_AFTER_30_DAYS",
        "window_type": "REGULAR_WEEKEND",
        "row_start_index": 0,
        "row_end_index": 240,
        "observation_rows": 180,
    }

    feature = _feature_row(window, rows, [], {}, {})

    assert feature["tradable_rows"] == 60
    assert math.isclose(
        feature["hourly_realized_volatility"], math.sqrt(60) * 0.001, rel_tol=1e-12
    )


def test_calendar_blocks_aggregate_symbols_before_statistics() -> None:
    features = [
        _feature(
            symbol="AMZNUSDT",
            group="W",
            calendar_key="W:2026-03-06:2026-03-09",
            value=1.0,
        ),
        _feature(
            symbol="INTCUSDT",
            group="W",
            calendar_key="W:2026-03-06:2026-03-09",
            value=3.0,
        ),
    ]

    blocks = _calendar_blocks(
        features, group="W", scope="TRADITIONAL_EQUITY"
    )

    assert len(blocks) == 1
    assert blocks[0]["symbol_count"] == 2
    assert blocks[0]["hourly_realized_volatility"] == 2.0


def test_bootstrap_is_reproducible_and_respects_metric_direction() -> None:
    first = _bootstrap_difference(
        [10.0, 11.0, 12.0],
        [1.0, 2.0, 3.0],
        lower_is_better=False,
        reps=500,
    )
    second = _bootstrap_difference(
        [10.0, 11.0, 12.0],
        [1.0, 2.0, 3.0],
        lower_is_better=False,
        reps=500,
    )
    unfavorable = _bootstrap_difference(
        [10.0, 11.0, 12.0],
        [1.0, 2.0, 3.0],
        lower_is_better=True,
        reps=500,
    )

    assert first == second
    assert first["favorable_support_probability"] > 0.95
    assert unfavorable["favorable_support_probability"] < 0.05
    assert first["ci_2_5"] <= first["ci_97_5"]


def test_w_vs_o_and_w_vs_r_are_never_merged() -> None:
    features = [
        _feature(symbol="AMZNUSDT", group="W", calendar_key="W1", value=1.0),
        _feature(symbol="AMZNUSDT", group="O", calendar_key="O1", value=2.0),
        _feature(
            symbol="AMZNUSDT", group="R", calendar_key="R1", seed=3, value=3.0
        ),
    ]

    w_o = _comparison_rows(features, control_group="O", evaluation_status="TEST")
    w_r = _comparison_rows(features, control_group="R", evaluation_status="TEST")

    assert {row["comparison"] for row in w_o} == {"W_VS_O"}
    assert {row["comparison"] for row in w_r} == {"W_VS_R"}
    assert any(row["seed"] == 3 for row in w_r)
    assert not any(row["seed"] == 3 for row in w_o)


def test_technical_gate_fails_fast_when_random_blocks_are_insufficient() -> None:
    features = []
    for symbol in CORE_SYMBOLS:
        features.extend(
            [
                _feature(symbol=symbol, group="W", calendar_key="W1"),
                _feature(symbol=symbol, group="O", calendar_key="O1"),
            ]
        )
    windows = [
        {
            "status": "READY",
            "split": "RESEARCH_DEVELOPMENT",
            "group": "W",
            "month": month,
        }
        for month in ("2026-03", "2026-04", "2026-05")
    ] + [
        {
            "status": "READY",
            "split": "RESEARCH_VALIDATION_EXPOSED",
            "group": "W",
            "month": "2026-06",
        }
    ]
    payload = {
        "windows": windows,
        "overlap_audit": {"passed": True},
        "o_count_before_random": 100,
        "o_count_after_random": 100,
        "count_audit": {
            "o_unchanged_by_random": True,
            "calendar_block_counts_by_split": {
                "RESEARCH_DEVELOPMENT": {"W": 13, "O": 50}
            },
            "development_r_blocks_by_seed": {
                str(seed): 0 for seed in SEED_VALUES
            },
        },
        "forward_oos_read": False,
    }
    audit = {
        "passed": True,
        "assets": [
            {"symbol": symbol, "status": "PASS"} for symbol in CORE_SYMBOLS
        ],
    }

    result = _technical_checks(
        window_payload=payload,
        input_hash_payload={"passed": True},
        audit_payload=audit,
        features=features,
        pytest_exit_code=0,
    )

    assert result["passed"] is False
    assert result["checks"]["at_least_10_r_development_blocks_each_seed"] is False
    assert result["checks"]["three_traditional_symbols_complete"] is False


def test_future_forward_oos_feature_is_rejected_by_technical_gate() -> None:
    feature = _feature(symbol="AMZNUSDT", group="W", calendar_key="W1")
    feature["split"] = "FORWARD_OOS_FUTURE"
    payload = {
        "windows": [],
        "overlap_audit": {"passed": True},
        "o_count_before_random": 0,
        "o_count_after_random": 0,
        "count_audit": {
            "o_unchanged_by_random": True,
            "calendar_block_counts_by_split": {"RESEARCH_DEVELOPMENT": {}},
            "development_r_blocks_by_seed": {
                str(seed): 0 for seed in SEED_VALUES
            },
        },
        "forward_oos_read": False,
    }

    result = _technical_checks(
        window_payload=payload,
        input_hash_payload={"passed": True},
        audit_payload={"passed": False, "assets": []},
        features=[feature],
        pytest_exit_code=0,
    )

    assert result["checks"]["future_forward_oos_not_computed"] is False


def test_o_full_10h_diagnostic_skips_short_tradable_interval() -> None:
    start = 1_700_000_000_000
    rows = _market_rows(start, [100.0] * 600, previous_close=100.0)
    window = {
        "window_id": "O-1",
        "symbol": "AMZNUSDT",
        "asset_group": "TRADITIONAL_EQUITY",
        "group": "O",
        "seed": "",
        "calendar_key": "O:test",
        "month": "2026-03",
        "split": "RESEARCH_DEVELOPMENT",
        "row_start_index": 0,
        "row_end_index": 600,
        "observation_rows": 180,
    }

    diagnostics = _diagnostic_rows(window, rows, [], {}, {}, None)

    assert len(diagnostics) == 1
    assert diagnostics[0]["diagnostic_view"] == "O_FULL"
    assert diagnostics[0]["status"] == "SKIPPED"
    assert diagnostics[0]["tradable_rows"] == 420


def test_asset_groups_are_preregistered_and_complete() -> None:
    assert set(TRADITIONAL_EQUITY).isdisjoint(CRYPTO_SENSITIVE_EQUITY)
    assert set(TRADITIONAL_EQUITY) | set(CRYPTO_SENSITIVE_EQUITY) == set(
        CORE_SYMBOLS
    )
