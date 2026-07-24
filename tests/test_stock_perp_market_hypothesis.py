from __future__ import annotations

from scripts.stock_perp_market_hypothesis import (
    BASE_MAKER_FEE,
    STEP_PCT,
    _bootstrap_w_vs_control,
    _feature_row,
    _grid_cycles,
    _h1_checks,
)


def test_grid_cycles_count_only_completed_threshold_reversals() -> None:
    legs, cycles = _grid_cycles([100.0, 100.2, 99.9, 100.2], 0.0015)

    assert legs == 3
    assert cycles == 2


def test_feature_row_excludes_observation_bars_from_market_metrics() -> None:
    start = 1_700_000_000_000
    rows = []
    for index in range(185):
        close = 100.0 if index < 180 else (100.2 if index % 2 else 99.9)
        rows.append(
            {
                "open_time": start + index * 60_000,
                "close_time": start + (index + 1) * 60_000 - 1,
                "open": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "close": close,
                "volume": 1.0,
                "quote_volume": 100.0,
                "trade_count": 1,
            }
        )
    window = {
        "window_id": "W-1",
        "symbol": "TESTUSDT",
        "group": "W",
        "calendar_key": "NYSE:1",
        "month": "2026-03",
        "split": "RESEARCH_DEVELOPMENT",
        "listing_stage": "LISTING_AFTER_30_DAYS",
        "row_start_index": 0,
        "row_end_index": len(rows),
        "observation_rows": 180,
    }
    mark = {row["open_time"]: row["close"] for row in rows}
    premium = {row["open_time"]: 0.0001 for row in rows}

    feature = _feature_row(
        window,
        rows,
        [{"funding_time": rows[-1]["open_time"], "funding_rate": -0.0002}],
        mark,
        premium,
    )

    assert feature["status"] == "READY"
    assert feature["row_count"] == 5
    assert feature["tradable_rows"] == 5
    assert feature["funding_abs_sum"] == 0.0002
    assert feature["fee_adjusted_cycle_capacity"] == feature["completed_grid_cycles"] * (
        STEP_PCT - 2 * BASE_MAKER_FEE
    )


def test_bootstrap_support_probability_uses_metric_direction() -> None:
    w = {"a": {"x": 10.0}, "b": {"x": 11.0}}

    higher = _bootstrap_w_vs_control(
        w,
        [1.0, 2.0],
        field="x",
        reps=200,
        lower_is_better=False,
    )
    lower = _bootstrap_w_vs_control(
        w,
        [1.0, 2.0],
        field="x",
        reps=200,
        lower_is_better=True,
    )

    assert higher["support_probability"] == 1.0
    assert lower["support_probability"] == 0.0


def _feature(
    *,
    symbol: str,
    month: str,
    group: str,
    volatility: float,
    efficiency: float,
    capacity: float,
    zero_trade_ratio: float,
) -> dict[str, object]:
    return {
        "window_id": f"{symbol}-{group}-{month}",
        "symbol": symbol,
        "group": group,
        "calendar_key": f"{group}-{month}",
        "month": month,
        "split": "RESEARCH_DEVELOPMENT",
        "listing_stage": "LISTING_AFTER_30_DAYS",
        "status": "READY",
        "realized_volatility": volatility,
        "directional_efficiency": efficiency,
        "fee_adjusted_cycle_capacity": capacity,
        "zero_trade_ratio": zero_trade_ratio,
        "hourly_volume": 100.0,
        "hourly_trade_count": 100.0,
    }


def test_h1_checks_use_calendar_clusters_and_can_pass_all_registered_gates() -> None:
    features = []
    for month in ("2026-02", "2026-03", "2026-04"):
        for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            features.extend(
                [
                    _feature(
                        symbol=symbol,
                        month=month,
                        group="W",
                        volatility=0.5,
                        efficiency=0.1,
                        capacity=2.0,
                        zero_trade_ratio=0.01,
                    ),
                    _feature(
                        symbol=symbol,
                        month=month,
                        group="O",
                        volatility=1.0,
                        efficiency=0.2,
                        capacity=1.0,
                        zero_trade_ratio=0.02,
                    ),
                    _feature(
                        symbol=symbol,
                        month=month,
                        group="R",
                        volatility=1.0,
                        efficiency=0.2,
                        capacity=1.0,
                        zero_trade_ratio=0.02,
                    ),
                ]
            )

    checks = _h1_checks(features, bootstrap_reps=200)

    assert checks["development_window_counts"] == {"W": 9, "O": 9, "R": 9}
    assert checks["development_calendar_window_counts"] == {"W": 3, "O": 3, "R": 3}
    assert checks["passed"] is True
    assert checks["failed_checks"] == []


def test_h1_failure_does_not_create_a_false_bootstrap_advantage() -> None:
    features = []
    for month in ("2026-02", "2026-03", "2026-04"):
        for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT"):
            features.extend(
                [
                    _feature(
                        symbol=symbol,
                        month=month,
                        group="W",
                        volatility=1.2,
                        efficiency=0.3,
                        capacity=0.5,
                        zero_trade_ratio=0.05,
                    ),
                    _feature(
                        symbol=symbol,
                        month=month,
                        group="O",
                        volatility=1.0,
                        efficiency=0.2,
                        capacity=1.0,
                        zero_trade_ratio=0.02,
                    ),
                    _feature(
                        symbol=symbol,
                        month=month,
                        group="R",
                        volatility=1.0,
                        efficiency=0.2,
                        capacity=1.0,
                        zero_trade_ratio=0.02,
                    ),
                ]
            )

    checks = _h1_checks(features, bootstrap_reps=200)

    assert checks["passed"] is False
    assert checks["bootstrap_supports_non_noise"] is False
    assert "cycle_capacity_not_lower" in checks["failed_checks"]
