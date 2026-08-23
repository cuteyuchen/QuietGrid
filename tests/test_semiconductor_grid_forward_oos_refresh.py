from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from data_sources.models import FundingEvent, NormalizedKline
from scripts.semiconductor_grid_forward_oos_v29_refresh import (
    DataRevisionDetected,
    _closed_minute,
    _merge_funding,
    _merge_klines,
    _select_material_refresh,
    _write_immutable_json,
    refresh_forward_oos,
)


def _bar(open_time: int, close: float = 10.0) -> NormalizedKline:
    return NormalizedKline(
        open_time=open_time,
        close_time=open_time + 59_999,
        open=close,
        high=close + 0.1,
        low=close - 0.1,
        close=close,
        volume=1.0,
        quote_volume=10.0,
        trade_count=1,
    )


def test_post_freeze_refresh_does_not_move_exposure_cutoff() -> None:
    result = refresh_forward_oos(check_only=True)
    assert result["exposure_cutoff"] == "2026-08-08T20:45:23.438783+00:00"


def test_refresh_is_idempotent_when_data_already_reaches_run_time() -> None:
    result = refresh_forward_oos(
        run_time_utc="2026-08-22T17:34:58.690630+00:00",
        check_only=True,
    )
    assert result["exposure_cutoff"] == "2026-08-08T20:45:23.438783+00:00"


def test_merge_klines_appends_only_new_closed_rows() -> None:
    existing = [_bar(1_000), _bar(61_000)]
    merged, stats = _merge_klines(
        existing,
        [_bar(121_000), _bar(121_000)],
        now_ms=200_000,
    )
    assert [item.open_time for item in merged] == [1_000, 61_000, 121_000]
    assert stats == {"duplicate_count": 1, "new_count": 1}


def test_merge_klines_rejects_historical_revision() -> None:
    with pytest.raises(DataRevisionDetected, match="DATA_REVISION_DETECTED"):
        _merge_klines(
            [_bar(1_000)],
            [_bar(1_000, close=11.0)],
            now_ms=100_000,
        )


def test_merge_funding_rejects_historical_revision() -> None:
    with pytest.raises(DataRevisionDetected, match="DATA_REVISION_DETECTED"):
        _merge_funding(
            [FundingEvent(1_000, 0.001, 10.0)],
            [FundingEvent(1_000, 0.002, 10.0)],
            now_ms=100_000,
        )


def test_closed_minute_is_utc_floor() -> None:
    value = _closed_minute(datetime(2026, 8, 22, 16, 47, 39, 123, tzinfo=UTC))
    assert value == datetime(2026, 8, 22, 16, 47, tzinfo=UTC)


def test_noop_execution_retains_latest_material_refresh() -> None:
    material = {
        "checked_at_utc": "2026-08-22T17:34:58.690630+00:00",
        "rows": [{"new_bar_count": 40_859, "new_funding_count": 85}],
        "monitor": {"appended_rows": 72},
        "ledger": {"old_row_count": 4, "new_row_count": 76},
    }
    previous = {"latest_material_refresh": material}
    noop = {
        "checked_at_utc": "2026-08-22T17:34:58.690630+00:00",
        "rows": [{"new_bar_count": 0, "new_funding_count": 0}],
        "monitor": {"appended_rows": 0},
        "ledger": {"old_row_count": 76, "new_row_count": 76},
    }

    assert _select_material_refresh(previous, noop) == material


def test_immutable_snapshot_cannot_be_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "observation.json"
    original = _write_immutable_json(path, {"status": "RULES_UNCHANGED"})
    repeated = _write_immutable_json(path, {"status": "RULES_UNCHANGED"})
    changed = _write_immutable_json(path, {"status": "RULE_CHANGE_DETECTED"})

    assert repeated == original
    assert changed != original
    assert original.read_text(encoding="utf-8") == repeated.read_text(encoding="utf-8")
    assert changed.read_text(encoding="utf-8") != original.read_text(encoding="utf-8")
