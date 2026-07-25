from __future__ import annotations

import random
from datetime import datetime, timezone

from scripts.rebuild_stock_perp_windows_h1_1 import (
    _block_key,
    _candidate_starts,
    _count_audit,
    _overlap_audit,
    _sample_status,
    _select_candidate,
)


UTC = timezone.utc


def _window(
    window_id: str,
    symbol: str,
    group: str,
    start: str,
    end: str,
    *,
    seed: int | str = "",
    split: str = "RESEARCH_DEVELOPMENT",
) -> dict[str, object]:
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    return {
        "window_id": window_id,
        "symbol": symbol,
        "group": group,
        "seed": seed,
        "calendar_key": _block_key(
            group,
            start_ms,
            end_ms,
            seed=int(seed) if seed != "" else None,
        ),
        "market_close": start,
        "force_close_at": end,
        "split": split,
        "month": start[:7],
        "status": "READY",
    }


def test_sample_status_relabels_june_as_exposed() -> None:
    forward = datetime(2026, 7, 26, tzinfo=UTC)

    assert _sample_status(
        int(datetime(2026, 3, 1, tzinfo=UTC).timestamp() * 1000), forward
    ) == "RESEARCH_DEVELOPMENT"
    assert _sample_status(
        int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000), forward
    ) == "RESEARCH_VALIDATION_EXPOSED"
    assert _sample_status(
        int(datetime(2026, 7, 1, tzinfo=UTC).timestamp() * 1000), forward
    ) == "DESCRIPTIVE_EXPOSED"
    assert _sample_status(
        int(datetime(2026, 7, 26, tzinfo=UTC).timestamp() * 1000), forward
    ) == "FORWARD_OOS_FUTURE"


def test_random_block_key_uses_real_utc_interval_and_seed() -> None:
    start = int(datetime(2026, 3, 2, 20, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(2026, 3, 5, 6, tzinfo=UTC).timestamp() * 1000)

    assert _block_key("R", start, end, seed=31) == (
        "R:2026-03-02T20:00:00Z:2026-03-05T06:00:00Z:seed=31"
    )


def test_candidate_starts_match_month_duration_and_utc_hour() -> None:
    start = int(datetime(2026, 3, 6, 21, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(2026, 3, 9, 6, tzinfo=UTC).timestamp() * 1000)

    candidates = _candidate_starts(start, end)

    assert candidates
    for value in candidates:
        candidate = datetime.fromtimestamp(value / 1000, tz=UTC)
        assert candidate.month == 3
        assert min(abs(candidate.hour - 21), 24 - abs(candidate.hour - 21)) <= 1
        assert datetime.fromtimestamp((value + end - start) / 1000, tz=UTC).month == 3


def test_candidate_selection_never_reuses_reserved_or_same_seed_time() -> None:
    candidates = [(0, 10), (20, 30), (40, 50)]

    selected = _select_candidate(
        candidates,
        reserved=[(0, 10)],
        same_seed_selected=[(20, 30)],
        rng=random.Random(31),
    )

    assert selected == (40, 50)


def test_overlap_audit_collapses_symbols_into_one_real_block() -> None:
    start = "2026-03-06T21:00:00Z"
    end = "2026-03-09T06:00:00Z"
    windows = [
        _window("W-A", "AMZNUSDT", "W", start, end),
        _window("W-B", "INTCUSDT", "W", start, end),
    ]

    audit = _overlap_audit(windows)

    assert audit["ready_symbol_window_rows"] == 2
    assert audit["ready_calendar_blocks"] == 1
    assert audit["calendar_blocks_by_group"]["W"] == 1
    assert audit["passed"] is True


def test_overlap_audit_rejects_w_o_and_same_seed_r_overlap() -> None:
    start = "2026-03-06T21:00:00Z"
    end = "2026-03-09T06:00:00Z"
    windows = [
        _window("W", "AMZNUSDT", "W", start, end),
        _window("O", "AMZNUSDT", "O", start, end),
        _window("R1", "AMZNUSDT", "R", start, end, seed=3),
        _window("R2", "AMZNUSDT", "R", "2026-03-07T00:00:00Z", "2026-03-08T00:00:00Z", seed=3),
    ]

    audit = _overlap_audit(windows)

    assert audit["w_o_overlap_count"] == 1
    assert audit["w_r_overlap_count"] == 2
    assert audit["o_r_overlap_count"] == 2
    assert audit["same_seed_r_overlap_count"] == 1
    assert audit["passed"] is False


def test_cross_seed_random_reuse_is_recorded_but_not_invalid() -> None:
    start = "2026-03-02T20:00:00Z"
    end = "2026-03-05T06:00:00Z"
    windows = [
        _window("R3", "AMZNUSDT", "R", start, end, seed=3),
        _window("R10", "AMZNUSDT", "R", start, end, seed=10),
    ]

    audit = _overlap_audit(windows)

    assert audit["cross_seed_r_overlap_count"] == 1
    assert audit["cross_seed_exact_reuse_count"] == 1
    assert audit["same_seed_r_overlap_count"] == 0
    assert audit["passed"] is True


def test_random_count_audit_cannot_delete_o_windows() -> None:
    windows = [
        _window(
            "O1",
            "AMZNUSDT",
            "O",
            "2026-03-02T21:00:00Z",
            "2026-03-03T07:00:00Z",
        )
    ]

    audit = _count_audit(
        windows,
        o_count_before_random=1,
        o_count_after_random=1,
    )

    assert audit["o_count_before_random"] == 1
    assert audit["o_count_after_random"] == 1
    assert audit["o_unchanged_by_random"] is True
