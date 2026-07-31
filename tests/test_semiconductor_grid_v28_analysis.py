from types import SimpleNamespace
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from core.models import GridDirectionMode
from scripts.semiconductor_grid_backtest_v28 import _row
import scripts.semiconductor_grid_v28_analysis as analysis
from scripts.semiconductor_grid_v28_analysis import (
    _combination_summary,
    _post_stop_paths,
    _report_insights,
    _time_splits,
    _write_forward_oos_ledger,
)
from strategy.semiconductor_grid_v28 import Combination


def test_time_split_keeps_same_timestamp_symbols_together() -> None:
    rows = []
    for index in range(5):
        timestamp = f"2026-07-{index + 1:02d}T00:00:00+00:00"
        for symbol in ("SNDKUSDT", "MUUSDT"):
            rows.append(
                {
                    "symbol": symbol,
                    "window_key": f"window-{index}",
                    "force_close_at": timestamp,
                }
            )

    splits = _time_splits(rows)

    assert splits[("SNDKUSDT", "window-2")] == "EXPOSED_EARLY"
    assert splits[("MUUSDT", "window-2")] == "EXPOSED_EARLY"
    assert splits[("SNDKUSDT", "window-3")] == "EXPOSED_LATE"
    assert splits[("MUUSDT", "window-3")] == "EXPOSED_LATE"


def test_report_insights_directly_answer_all_protocol_questions() -> None:
    answers = _report_insights([], [], [], [], [], [], [])

    assert len(answers) == 10
    for index, answer in enumerate(answers, start=1):
        assert answer.startswith(f"{index}.")
    assert "AxB" not in answers[1]
    assert "0/8" in answers[9]


def test_v28_result_row_preserves_protocol_metrics() -> None:
    result = SimpleNamespace(
        paired_grid_pnl=10.0,
        pre_exit_unrealized_pnl=-2.0,
        inventory_realized_pnl=-1.0,
        total_pnl=7.0,
        max_drawdown=5.0,
        pre_exit_inventory_notional=80.0,
        peak_negative_unrealized_pnl=3.0,
        max_inventory_utilization=0.4,
        mean_inventory_utilization=0.2,
        max_unpaired_lots=2,
        max_unpaired_lot_age_bars=30,
        pair_completion_count=4,
        accepted_fill_count=9,
        rejected_fill_count=1,
        take_profit_count=0,
        profit_protection_suppress_count=1,
        profit_protection_reduce_count=2,
        profit_protection_close_count=0,
        stopped_reason="stop_loss",
        force_close_count=1,
        inventory_critical_exit_count=0,
        stopped_at_index=12,
        stopped_at_price=98.0,
    )
    candidate = SimpleNamespace(
        params=SimpleNamespace(grid_num=8, step_pct=0.002),
        viability=SimpleNamespace(
            snapshot=SimpleNamespace(crossings_per_hour=2.5, net_capacity_per_hour=0.004)
        ),
    )
    window = SimpleNamespace(window_key="w", force_close_at=SimpleNamespace(isoformat=lambda: "t"))

    row = _row(
        item=Combination.parse("11111"),
        symbol="SNDKUSDT",
        direction=GridDirectionMode.NEUTRAL,
        scenario="PRIMARY_ZERO_MAKER",
        seed=3,
        window=window,
        result=result,
        candidate=candidate,
        capital=500.0,
    )

    assert row["max_drawdown_pct"] == 0.01
    assert row["pre_exit_inventory_notional"] == 80.0
    assert row["crossings_per_hour"] == 2.5
    assert row["net_capacity_per_hour"] == 0.004

    summary = _combination_summary(
        [row],
        [
            {
                "combination_id": "11111",
                "direction": "NEUTRAL",
                "neighbor_positive_ratio": 0.5,
                "neighbor_stress_nonnegative_ratio": 0.4,
            }
        ],
    )[0]
    required = {
        "paired_grid_pnl",
        "inventory_realized_pnl",
        "net_pnl",
        "median_window_pnl",
        "mean_window_pnl",
        "profit_factor",
        "positive_window_ratio",
        "max_drawdown",
        "max_drawdown_pct",
        "CVaR_95",
        "worst_window_pnl",
        "worst_5pct_mean",
        "max_window_loss",
        "inventory_drag",
        "inventory_drag_ratio",
        "pre_exit_inventory_notional",
        "crossings_per_hour",
        "net_capacity_per_hour",
        "seed_positive_count",
        "neighbor_positive_ratio",
        "neighbor_stress_nonnegative_ratio",
        "symbol_contribution",
        "month_contribution",
    }
    assert required <= summary.keys()


def test_post_stop_path_analysis_does_not_mutate_trade_results(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    rows = [
        {
            "open_time": int((start + timedelta(minutes=index)).timestamp() * 1000),
            "close": close,
            "high": close + 0.2,
            "low": close - 0.2,
        }
        for index, close in enumerate((98.0, 100.0, 100.5, 100.2))
    ]
    window = SimpleNamespace(
        complete=True,
        observation_end=start,
        force_close_at=start + timedelta(minutes=4),
        window_key="window-1",
        rows=rows,
    )
    profile = SimpleNamespace(
        market_group="US_STOCK",
        calendar_name="XNYS",
        market_timezone="America/New_York",
        reference_open_time="09:30",
    )
    monkeypatch.setattr(analysis, "RESEARCH_SYMBOLS", ("SNDKUSDT",))
    monkeypatch.setattr(analysis, "symbol_profiles_from_mapping", lambda _raw: {"SNDKUSDT": profile})
    monkeypatch.setattr(analysis, "_find_csv", lambda *_args: tmp_path / "bars.csv")
    monkeypatch.setattr(analysis, "_read_klines_with_audit", lambda *_args: ([], {}))
    monkeypatch.setattr(analysis, "build_calendar_closed_windows", lambda *_args, **_kwargs: [window])
    config = tmp_path / "config.yaml"
    config.write_text("semiconductor_grid: {}\n", encoding="utf-8")
    trade_results = [
        {
            "combination_id": "11111",
            "direction": "NEUTRAL",
            "symbol": "SNDKUSDT",
            "scenario": "PRIMARY_ZERO_MAKER",
            "seed": "3",
            "window_key": "window-1",
            "stopped_reason": "stop_loss",
            "stopped_at_index": "0",
            "grid_lower": "99",
            "grid_upper": "101",
            "baseline_atr": "1",
        }
    ]
    before = deepcopy(trade_results)

    paths, _summary = _post_stop_paths(trade_results, config_path=config, data_dir=tmp_path)

    assert trade_results == before
    assert paths[0]["classification"] == "FALSE_BREAK_LIKELY"


def test_forward_oos_freeze_is_immutable(tmp_path) -> None:
    path = tmp_path / "forward-oos-ledger.csv"
    frozen = [
        {
            "record_type": "LEDGER_METADATA",
            "code_sha256": "code-a",
            "config_sha256": "config-a",
            "trading_rules_sha256": "rules-a",
            "source_data_cutoff_utc": "2026-07-01T00:00:00+00:00",
            "execution_scenarios": "PRIMARY_ZERO_MAKER",
        },
        {
            "record_type": "CANDIDATE_FREEZE",
            "rank": 1,
            "combination_id": "31111",
            "direction": "NEUTRAL",
            "code_sha256": "code-a",
            "config_sha256": "config-a",
            "trading_rules_sha256": "rules-a",
            "source_data_cutoff_utc": "2026-07-01T00:00:00+00:00",
            "execution_scenarios": "PRIMARY_ZERO_MAKER",
        },
    ]
    _write_forward_oos_ledger(path, frozen)
    original = path.read_bytes()
    _write_forward_oos_ledger(path, frozen)
    assert path.read_bytes() == original

    changed = deepcopy(frozen)
    changed[1]["combination_id"] = "31121"
    with pytest.raises(RuntimeError, match="已冻结"):
        _write_forward_oos_ledger(path, changed)
