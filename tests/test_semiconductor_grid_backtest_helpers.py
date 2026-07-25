from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.semiconductor_grid_backtest import _aggregate, assess_profiles, build_closed_windows
from strategy.window_models import TradingWindow, WindowKind


class _FakeScheduler:
    def classify_window(self, value: datetime) -> TradingWindow:
        key = "US:one" if value.minute < 3 else "US:two"
        return TradingWindow(kind=WindowKind.WEEKEND,allowed=True,window_key=key,previous_market_close=None,next_market_open=None,next_premarket_open=None,force_close_at=None,minutes_to_force_close=999,reason="test")


def test_closed_window_builder_groups_by_frozen_window_key() -> None:
    start = datetime(2026, 7, 4, 0, 0, tzinfo=timezone.utc)
    rows = [{"open_time":int((start+timedelta(minutes=index)).timestamp()*1000),"close_time":int((start+timedelta(minutes=index,seconds=59)).timestamp()*1000),"open":100.0,"high":100.1,"low":99.9,"close":100.0} for index in range(6)]
    windows = build_closed_windows(rows, _FakeScheduler(), "US_STOCK")
    assert [item.window_key for item in windows] == ["US:one", "US:two"]
    assert [len(item.rows) for item in windows] == [3, 3]


def test_aggregate_averages_seeds_before_counting_windows() -> None:
    rows = [{"market_group":"US_STOCK","symbol":"SNDKUSDT","profile":"N100","scenario":"PRIMARY_ZERO_MAKER","window_key":"US:one","seed":seed,"total_pnl":pnl,"max_drawdown":1.0,"max_drawdown_pct":0.002,"fees_paid":0.0,"funding_paid":0.0,"inventory_drag_ratio":0.1} for seed,pnl in [(3,1.0),(10,3.0)]]
    summary = _aggregate(rows, ("market_group", "profile", "scenario"))[0]
    assert summary["runs"] == 2
    assert summary["unique_windows"] == 1
    assert summary["total_pnl"] == 2.0
    assert summary["positive_ratio"] == 1.0


def test_assessment_requires_primary_and_stress() -> None:
    summaries = [
        {"market_group":"US_STOCK","profile":"N100","scenario":"PRIMARY_ZERO_MAKER","unique_windows":8,"total_pnl":10.0,"positive_ratio":0.75,"profit_factor":2.0,"max_drawdown_pct":0.02,"mean_inventory_drag_ratio":0.10,"best_window_concentration":0.20},
        {"market_group":"US_STOCK","profile":"N100","scenario":"EXECUTION_STRESS","unique_windows":8,"total_pnl":2.0,"positive_ratio":0.55,"profit_factor":1.1,"max_drawdown_pct":0.03,"mean_inventory_drag_ratio":0.20,"best_window_concentration":0.30},
    ]
    result = assess_profiles(summaries, {})[0]
    assert result["passed"] is True
    assert result["conclusion"] == "SEMICONDUCTOR_GRID_RESEARCH_CANDIDATE"
