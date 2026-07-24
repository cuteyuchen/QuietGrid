from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import scripts.cross_era_absolute_trend as round27
from scripts.cross_era_round13_diagnose import _sha256


UTC = timezone.utc


def test_protocol_and_frozen_inputs_are_locked() -> None:
    assert _sha256(round27.PROTOCOL_PATH.resolve()) == round27.PROTOCOL_SHA256
    assert _sha256(round27.ROUND26_RESULT_PATH.resolve()) == round27.ROUND26_RESULT_SHA256
    for config in round27.ASSET_CONFIG.values():
        assert _sha256(config["price_manifest"].resolve()) == config["price_manifest_sha256"]
        assert _sha256(config["funding_manifest"].resolve()) == config["funding_manifest_sha256"]


def test_build_signals_uses_only_previous_200_complete_days() -> None:
    first = datetime(2020, 1, 1, 23, tzinfo=UTC)
    rows = []
    for index in range(202):
        timestamp = first + timedelta(days=index)
        rows.append(
            {
                "segment": "TEST",
                "open_time": int(timestamp.timestamp() * 1000),
                "close": float(index + 1),
            }
        )
    split = {
        "segment": "TEST",
        "start": datetime(2020, 7, 19, 1, tzinfo=UTC),
        "end": datetime(2020, 7, 20, 23, tzinfo=UTC),
        "signal_days": 2,
    }

    signals, audit = round27._build_signals(rows, split)

    assert len(signals) == 2
    assert signals[0]["sma50"] == pytest.approx(sum(range(151, 201)) / 50)
    assert signals[0]["sma200"] == pytest.approx(sum(range(1, 201)) / 200)
    assert signals[0]["target_position"] == 1
    assert audit["latest_source_precedes_execution"] is True
    assert audit["minimum_warmup_day_count"] == 200


def _hourly_path(start: datetime, days: int) -> list[dict[str, object]]:
    end = start.replace(hour=23) + timedelta(days=days - 1)
    rows = []
    timestamp = start
    while timestamp <= end:
        elapsed = int((timestamp - start).total_seconds() // 3600)
        price = 100.0 + elapsed
        rows.append(
            {
                "segment": "TEST",
                "open_time": int(timestamp.timestamp() * 1000),
                "open": price,
                "close": price + 0.25,
            }
        )
        timestamp += timedelta(hours=1)
    return rows


def test_simulator_settles_midnight_funding_before_next_signal_flip() -> None:
    start = datetime(2022, 1, 1, 1, tzinfo=UTC)
    rows = _hourly_path(start, 2)
    end = datetime(2022, 1, 2, 23, tzinfo=UTC)
    signals = [
        {
            "execution_time": int(start.timestamp() * 1000),
            "target_position": 1,
            "warmup_day_count": 200,
            "latest_source_close_time": int((start - timedelta(hours=2)).timestamp() * 1000),
        },
        {
            "execution_time": int(datetime(2022, 1, 2, 1, tzinfo=UTC).timestamp() * 1000),
            "target_position": -1,
            "warmup_day_count": 200,
            "latest_source_close_time": int(datetime(2022, 1, 1, 23, tzinfo=UTC).timestamp() * 1000),
        },
    ]
    funding_time = datetime(2022, 1, 2, 0, tzinfo=UTC)
    split = {
        "segment": "TEST",
        "start": start,
        "end": end,
        "signal_days": 2,
    }

    result = round27._simulate_cell(
        rows,
        [{"funding_time": int(funding_time.timestamp() * 1000) + 5, "funding_rate": 0.001}],
        signals,
        split=split,
        initial_capital=500.0,
        execution_cost_rate=0.001,
    )

    funding_open = next(
        float(row["open"])
        for row in rows
        if int(row["open_time"]) == int(funding_time.timestamp() * 1000)
    )
    assert result["metrics"]["funding_pnl"] == pytest.approx(
        -(500.0 / 100.0) * funding_open * 0.001
    )
    assert result["metrics"]["execution_side_count"] == 4
    assert result["metrics"]["funding_timestamp_normalized_count"] == 1
    assert result["metrics"]["final_position"] == 0
    assert result["checks"]["all_execution_sides_costed_and_flat"] is True


def test_simulator_rejects_funding_hour_collision() -> None:
    start = datetime(2022, 1, 1, 1, tzinfo=UTC)
    rows = _hourly_path(start, 1)
    split = {"segment": "TEST", "start": start, "end": start.replace(hour=23), "signal_days": 1}
    signal = {
        "execution_time": int(start.timestamp() * 1000),
        "target_position": 1,
        "warmup_day_count": 200,
        "latest_source_close_time": int((start - timedelta(hours=2)).timestamp() * 1000),
    }
    funding_hour = datetime(2022, 1, 1, 8, tzinfo=UTC)
    events = [
        {"funding_time": int(funding_hour.timestamp() * 1000), "funding_rate": 0.001},
        {"funding_time": int(funding_hour.timestamp() * 1000) + 5, "funding_rate": 0.001},
    ]

    with pytest.raises(ValueError, match="映射到同一小时"):
        round27._simulate_cell(
            rows,
            events,
            [signal],
            split=split,
            initial_capital=500.0,
            execution_cost_rate=0.001,
        )


def test_performance_metrics_use_daily_returns_and_calendar_months() -> None:
    daily = [
        (datetime(2022, 1, 31, tzinfo=UTC).date(), 101.0),
        (datetime(2022, 2, 1, tzinfo=UTC).date(), 100.0),
        (datetime(2022, 2, 2, tzinfo=UTC).date(), 102.0),
    ]

    metrics = round27._performance_metrics(
        daily, initial_capital=100.0, maximum_drawdown_pct=0.01
    )

    assert metrics["daily_profit_factor"] == pytest.approx(3.0)
    assert metrics["calendar_month_count"] == 2
    assert metrics["positive_calendar_month_ratio"] == 1.0
    assert metrics["best_profitable_month_concentration"] == pytest.approx(0.5)
