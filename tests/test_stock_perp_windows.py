from __future__ import annotations

from data_sources.models import NormalizedKline
from scripts.build_stock_perp_windows import (
    OBSERVATION_ROWS,
    _assign_splits,
    _base_window,
    _listing_stage,
    _overlap_audit,
)


def _rows(start_ms: int, count: int) -> list[NormalizedKline]:
    return [
        NormalizedKline(
            open_time=start_ms + index * 60_000,
            close_time=start_ms + (index + 1) * 60_000 - 1,
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
            quote_volume=100,
            trade_count=1,
        )
        for index in range(count)
    ]


def _window(window_id: str, group: str, start: str, end: str, *, seed="") -> dict[str, object]:
    return {
        "window_id": window_id,
        "symbol": "TESTUSDT",
        "group": group,
        "seed": seed,
        "market_close": start,
        "force_close_at": end,
        "status": "READY",
    }


def test_base_window_uses_only_pretrade_observation_rows() -> None:
    start = 1_700_000_000_000
    rows = _rows(start, OBSERVATION_ROWS + 60)

    window = _base_window(
        window_id="TEST-W",
        symbol="TESTUSDT",
        group="W",
        seed=None,
        matched_window_id=None,
        calendar_key="NYSE:test",
        start_ms=start,
        force_close_ms=rows[-1].close_time + 1,
        rows=rows,
        row_start=0,
        row_end=len(rows),
        onboard_ms=start - 31 * 86_400_000,
    )

    assert window["status"] == "READY"
    assert window["observation_rows"] == OBSERVATION_ROWS
    assert window["tradable_rows"] == 60
    assert window["tradable_start"].startswith("2023-")
    assert window["observation_end"] < window["tradable_start"]


def test_split_assignment_seals_validation_and_short_oos() -> None:
    windows = [{"month": month} for month in ("2026-02", "2026-03", "2026-04", "2026-05", "2026-06")]

    _assign_splits(windows, complete_months=[item["month"] for item in windows])

    assert [item["split"] for item in windows] == [
        "RESEARCH_DEVELOPMENT",
        "RESEARCH_DEVELOPMENT",
        "RESEARCH_DEVELOPMENT",
        "VALIDATION",
        "SEALED_SHORT_OOS",
    ]


def test_four_month_split_has_no_validation() -> None:
    windows = [{"month": month} for month in ("2026-03", "2026-04", "2026-05", "2026-06")]

    _assign_splits(windows, complete_months=[item["month"] for item in windows])

    assert [item["split"] for item in windows] == [
        "RESEARCH_DEVELOPMENT",
        "RESEARCH_DEVELOPMENT",
        "RESEARCH_DEVELOPMENT",
        "SEALED_SHORT_OOS",
    ]


def test_overlap_audit_records_cross_seed_replicates_but_rejects_w_o_reuse() -> None:
    start = "2026-03-02T00:00:00+00:00"
    end = "2026-03-03T00:00:00+00:00"
    replicates = [
        _window("R-3", "R", start, end, seed=3),
        _window("R-10", "R", start, end, seed=10),
    ]

    audit = _overlap_audit(replicates)

    assert audit["passed"] is True
    assert audit["replicate_overlap_count"] == 1
    invalid = _overlap_audit(
        replicates + [_window("W", "W", start, end)]
    )
    assert invalid["passed"] is False
    assert invalid["overlap_count"] == 2


def test_listing_stage_boundaries_are_fixed() -> None:
    onboard = 1_700_000_000_000

    assert _listing_stage(onboard + 13 * 86_400_000, onboard) == "LISTING_DAYS_1_14"
    assert _listing_stage(onboard + 14 * 86_400_000, onboard) == "LISTING_DAYS_15_30"
    assert _listing_stage(onboard + 30 * 86_400_000, onboard) == "LISTING_AFTER_30_DAYS"
