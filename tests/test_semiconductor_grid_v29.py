from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from strategy.semiconductor_grid_v29 import (
    DIAGNOSTIC_CONTROL_ID,
    EX_MU_CANDIDATE_ID,
    FORWARD_OOS_SCENARIOS,
    PRIMARY_CANDIDATE_ID,
    ForwardOOSLedger,
    LedgerInvariantError,
    build_exposure_evidence,
    candidate_registry,
    classify_forward_window,
    evaluate_forward_oos,
    first_eligible_forward_window,
    production_safety_snapshot,
)
from scripts.semiconductor_grid_backtest import ClosedWindow, assign_time_splits
from scripts.semiconductor_grid_forward_oos_v29 import _source_file_sha256


def _dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=UTC)


def _window(
    start: datetime,
    *,
    complete: bool = True,
    first_seen_at: datetime | None = None,
) -> dict[str, object]:
    end = start + timedelta(days=2)
    return {
        "window_key": f"NYSE:{start.isoformat()}:{end.isoformat()}",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "force_close_at": end.isoformat(),
        "first_seen_at": (first_seen_at or start).isoformat(),
        "complete_window": complete,
        "data_complete": complete,
        "funding_complete": complete,
        "rules_frozen": True,
        "force_close_covered": complete,
    }


def _result(
    window_index: int,
    *,
    symbol: str = "SNDKUSDT",
    scenario: str = "PRIMARY_ZERO_MAKER",
    seed: int = 3,
    pnl: float = 1.0,
    candidate_id: str = PRIMARY_CANDIDATE_ID,
    candidate_sha: str = "candidate-a",
) -> dict[str, object]:
    start = _dt(10 + window_index)
    end = start + timedelta(hours=48)
    return {
        "record_type": "OOS_RESULT",
        "status": "COMPLETE",
        "candidate_id": candidate_id,
        "candidate_sha": candidate_sha,
        "symbol": symbol,
        "market_calendar": "NYSE",
        "window_key": f"NYSE:window-{window_index}",
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "force_close_at": end.isoformat(),
        "first_seen_at": start.isoformat(),
        "completed_at": end.isoformat(),
        "scenario": scenario,
        "seed": seed,
        "gate_status": "ALLOWED",
        "regime_status": "ALLOWED",
        "paired_grid_pnl": max(pnl, 0.5),
        "inventory_realized_pnl": min(pnl, 0.0),
        "funding_pnl": 0.0,
        "fees": 0.1,
        "slippage_cost": 0.1,
        "net_pnl": pnl,
        "max_drawdown": 1.0,
        "max_drawdown_pct": 0.01,
        "inventory_drag": 0.1,
        "inventory_drag_ratio": 0.1,
        "pre_exit_inventory_notional": 10.0,
        "max_inventory_utilization": 0.2,
        "mean_inventory_utilization": 0.1,
        "max_unpaired_lots": 2,
        "max_unpaired_lot_age": 30,
        "stop_loss_count": 0,
        "window_force_close_count": 1,
        "complete_window": True,
        "data_complete": True,
        "funding_complete": True,
        "rules_frozen": True,
        "force_close_covered": True,
        "oos_eligible": True,
        "sequence_valid": True,
    }


def _matrix(
    window_index: int,
    *,
    candidate_id: str = PRIMARY_CANDIDATE_ID,
    candidate_sha: str = "candidate-a",
    symbols: tuple[str, ...] = ("SNDKUSDT", "SOXLUSDT"),
) -> list[dict[str, object]]:
    return [
        _result(
            window_index,
            symbol=symbol,
            scenario=scenario,
            seed=seed,
            candidate_id=candidate_id,
            candidate_sha=candidate_sha,
        )
        for symbol in symbols
        for scenario in FORWARD_OOS_SCENARIOS
        for seed in (3, 10, 17, 31, 59, 97)
    ]


def test_exposure_cutoff_uses_latest_input_not_last_successful_window() -> None:
    evidence = build_exposure_evidence(
        research_input_timestamps=[_dt(7)],
        phase_records=[{"force_close_at": _dt(3).isoformat()}],
        gate_records=[],
        candidate_freeze_time_utc=_dt(5),
    )

    assert evidence.exposure_cutoff == _dt(7)


def test_blocked_window_is_still_exposure() -> None:
    blocked_key = f"NYSE:{_dt(2).isoformat()}:{_dt(8).isoformat()}"
    evidence = build_exposure_evidence(
        research_input_timestamps=[_dt(4)],
        phase_records=[{"force_close_at": _dt(5).isoformat()}],
        gate_records=[{"reason": "REGIME_BLOCKED", "window_key": blocked_key}],
        candidate_freeze_time_utc=_dt(6),
    )

    assert evidence.latest_window_seen_by_regime_or_gate == _dt(8)
    assert evidence.exposure_cutoff == _dt(8)


def test_partial_and_prefreeze_windows_cannot_enter_oos() -> None:
    cutoff = _dt(8)
    partial = _window(_dt(9), complete=False)
    overlapping = _window(_dt(7), complete=True)

    assert classify_forward_window(partial, cutoff) == "INCOMPLETE_WINDOW"
    assert classify_forward_window(overlapping, cutoff) == "EXPOSED_HISTORY"


def test_first_complete_window_strictly_after_freeze_is_selected() -> None:
    cutoff = _dt(8)
    windows = [
        _window(_dt(7)),
        _window(_dt(9), complete=False),
        _window(_dt(10)),
        _window(_dt(12)),
    ]

    selected = first_eligible_forward_window(windows, cutoff)

    assert selected is windows[2]
    assert classify_forward_window(selected, cutoff) == "FORWARD_OOS"


def test_shared_window_split_rejects_window_that_started_before_cutoff() -> None:
    start = _dt(8)
    end = _dt(10)
    row = {
        "open_time": int(start.timestamp() * 1000),
        "close_time": int((start + timedelta(minutes=1)).timestamp() * 1000) - 1,
    }
    window = ClosedWindow(
        window_key="NYSE:overlap",
        market_group="US_STOCK",
        rows=(row,),
        observation_start=start,
        observation_end=start + timedelta(minutes=180),
        force_close_at=end,
        complete=True,
    )

    assigned = assign_time_splits(
        [window], forward_oos_start=start + timedelta(hours=1)
    )

    assert assigned[0].split == "RESEARCH_VALIDATION_EXPOSED"


def test_ledger_is_append_only_in_csv_and_json(tmp_path: Path) -> None:
    ledger = ForwardOOSLedger(tmp_path / "ledger.csv", tmp_path / "ledger.json")
    frozen = [
        {
            "record_type": "CANDIDATE_FREEZE",
            "candidate_id": PRIMARY_CANDIDATE_ID,
            "candidate_sha": "candidate-a",
            "complete_window": False,
            "oos_eligible": False,
            "sequence_valid": True,
        }
    ]
    ledger.initialize(frozen)
    csv_prefix = ledger.csv_path.read_bytes()
    original_records = ledger.records()

    ledger.append([_result(1)], candidate_sha="candidate-a")

    assert ledger.csv_path.read_bytes().startswith(csv_prefix)
    assert ledger.records()[:1] == original_records
    changed = [{**frozen[0], "candidate_sha": "candidate-b"}]
    with pytest.raises(LedgerInvariantError, match="cannot rewrite"):
        ledger.initialize(changed)


def test_candidate_hash_change_appends_invalidation_and_disables_sequence(
    tmp_path: Path,
) -> None:
    ledger = ForwardOOSLedger(tmp_path / "ledger.csv", tmp_path / "ledger.json")
    ledger.initialize(
        [
            {
                "record_type": "CANDIDATE_FREEZE",
                "candidate_id": PRIMARY_CANDIDATE_ID,
                "candidate_sha": "candidate-a",
                "complete_window": False,
                "oos_eligible": False,
                "sequence_valid": True,
            }
        ]
    )
    ledger.append(_matrix(0), candidate_sha="candidate-a")
    assert evaluate_forward_oos(ledger.records())["complete_forward_oos_windows"] == 1

    appended = ledger.append(
        [_result(1, candidate_sha="candidate-b")],
        candidate_sha="candidate-b",
        completed_at=_dt(20),
    )

    assert appended[0]["record_type"] == "SEQUENCE_INVALIDATED"
    assert appended[-1]["oos_eligible"] is False
    assert ledger.sequence_valid is False
    assert evaluate_forward_oos(ledger.records())["complete_forward_oos_windows"] == 0


def test_primary_and_ex_mu_ledger_sequences_are_independent(tmp_path: Path) -> None:
    ledger = ForwardOOSLedger(tmp_path / "ledger.csv", tmp_path / "ledger.json")
    ledger.initialize(
        [
            {
                "record_type": "CANDIDATE_FREEZE",
                "candidate_id": PRIMARY_CANDIDATE_ID,
                "candidate_sha": "primary-a",
                "complete_window": False,
                "oos_eligible": False,
                "sequence_valid": True,
            },
            {
                "record_type": "POST_HOC_CANDIDATE",
                "candidate_id": EX_MU_CANDIDATE_ID,
                "candidate_sha": "ex-mu-a",
                "complete_window": False,
                "oos_eligible": False,
                "sequence_valid": True,
            },
        ]
    )
    ledger.append(_matrix(0, candidate_sha="primary-a"), candidate_sha="primary-a")
    ledger.append(
        _matrix(
            0,
            candidate_id=EX_MU_CANDIDATE_ID,
            candidate_sha="ex-mu-a",
        ),
        candidate_shas={EX_MU_CANDIDATE_ID: "ex-mu-a"},
    )

    records = ledger.records()
    assert evaluate_forward_oos(records)["complete_forward_oos_windows"] == 1
    assert (
        evaluate_forward_oos(records, candidate_id=EX_MU_CANDIDATE_ID)[
            "complete_forward_oos_windows"
        ]
        == 1
    )

    invalidated = ledger.append(
        [
            _result(
                1,
                candidate_id=EX_MU_CANDIDATE_ID,
                candidate_sha="ex-mu-b",
            )
        ],
        candidate_shas={EX_MU_CANDIDATE_ID: "ex-mu-b"},
    )

    assert invalidated[0]["candidate_id"] == EX_MU_CANDIDATE_ID
    records = ledger.records()
    assert evaluate_forward_oos(records)["complete_forward_oos_windows"] == 1
    assert (
        evaluate_forward_oos(records, candidate_id=EX_MU_CANDIDATE_ID)[
            "complete_forward_oos_windows"
        ]
        == 0
    )


def test_new_candidate_freeze_starts_a_fresh_sequence_after_invalidation(
    tmp_path: Path,
) -> None:
    ledger = ForwardOOSLedger(tmp_path / "ledger.csv", tmp_path / "ledger.json")
    ledger.initialize(
        [
            {
                "record_type": "CANDIDATE_FREEZE",
                "candidate_id": PRIMARY_CANDIDATE_ID,
                "candidate_sha": "candidate-a",
                "complete_window": False,
                "oos_eligible": False,
                "sequence_valid": True,
            }
        ]
    )
    ledger.append(_matrix(0), candidate_sha="candidate-a")
    ledger.append(
        [_result(1, candidate_sha="candidate-b")],
        candidate_sha="candidate-b",
    )
    assert ledger.sequence_valid is False

    ledger.append(
        [
            {
                "record_type": "CANDIDATE_FREEZE",
                "status": "PRIMARY_FORWARD_OOS_CANDIDATE",
                "candidate_id": PRIMARY_CANDIDATE_ID,
                "candidate_sha": "candidate-b",
                "complete_window": False,
                "oos_eligible": False,
                "sequence_valid": True,
            }
        ],
        candidate_sha="candidate-b",
    )
    ledger.append(
        _matrix(2, candidate_sha="candidate-b"),
        candidate_sha="candidate-b",
    )

    assert ledger.sequence_valid is True
    assessment = evaluate_forward_oos(ledger.records())
    assert assessment["complete_forward_oos_windows"] == 1
    assert assessment["scenarios"]["PRIMARY_ZERO_MAKER"]["window_metrics"][0][
        "window_key"
    ] == "NYSE:window-2"


def test_direct_new_freeze_records_old_sequence_invalidation(tmp_path: Path) -> None:
    ledger = ForwardOOSLedger(tmp_path / "ledger.csv", tmp_path / "ledger.json")
    ledger.initialize(
        [
            {
                "record_type": "CANDIDATE_FREEZE",
                "candidate_id": PRIMARY_CANDIDATE_ID,
                "candidate_sha": "candidate-a",
                "complete_window": False,
                "oos_eligible": False,
                "sequence_valid": True,
            }
        ]
    )
    ledger.append(_matrix(0), candidate_sha="candidate-a")

    appended = ledger.append(
        [
            {
                "record_type": "CANDIDATE_FREEZE",
                "candidate_id": PRIMARY_CANDIDATE_ID,
                "candidate_sha": "candidate-b",
                "complete_window": False,
                "oos_eligible": False,
                "sequence_valid": True,
            }
        ],
        candidate_sha="candidate-b",
    )

    assert [row["record_type"] for row in appended] == [
        "SEQUENCE_INVALIDATED",
        "CANDIDATE_FREEZE",
    ]
    assert evaluate_forward_oos(ledger.records())["complete_forward_oos_windows"] == 0
    ledger.append(_matrix(1, candidate_sha="candidate-b"), candidate_sha="candidate-b")
    assert evaluate_forward_oos(ledger.records())["complete_forward_oos_windows"] == 1


def test_31121_is_diagnostic_not_an_independent_primary_candidate() -> None:
    registry = candidate_registry(primary_candidate_sha="primary-sha")

    assert [
        row["candidate_id"]
        for row in registry["primary_forward_oos_candidates"]
    ] == [PRIMARY_CANDIDATE_ID]
    assert registry["primary_forward_oos_candidates"][0]["candidate_sha"] == "primary-sha"
    assert registry["diagnostic_controls"][0]["candidate_id"] == DIAGNOSTIC_CONTROL_ID
    assert registry["diagnostic_controls"][0]["independent_primary_candidate"] is False


def test_ex_mu_uses_an_independent_zero_count_sequence() -> None:
    registry = candidate_registry(include_ex_mu=True)
    ex_mu = registry["post_hoc_research_candidates"][0]
    rows = _matrix(1) + _matrix(1, candidate_id=EX_MU_CANDIDATE_ID)

    primary = evaluate_forward_oos(rows, candidate_id=PRIMARY_CANDIDATE_ID)
    separate = evaluate_forward_oos(rows, candidate_id=EX_MU_CANDIDATE_ID)

    assert ex_mu["candidate_id"] == EX_MU_CANDIDATE_ID
    assert ex_mu["historical_validation"] == "NOT_CLAIMED"
    assert ex_mu["forward_oos_count"] == 0
    assert primary["complete_forward_oos_windows"] == 1
    assert separate["complete_forward_oos_windows"] == 1


def test_eight_window_threshold_counts_window_not_symbol_seed_or_scenario() -> None:
    one_window = [
        _result(
            1,
            symbol=symbol,
            scenario=scenario,
            seed=seed,
        )
        for symbol in ("SNDKUSDT", "MUUSDT", "SOXLUSDT")
        for scenario in FORWARD_OOS_SCENARIOS
        for seed in (3, 10, 17, 31, 59, 97)
    ]

    assessment = evaluate_forward_oos(one_window)

    assert len(one_window) == 54
    assert assessment["complete_forward_oos_windows"] == 1
    assert assessment["conclusion_code"] == "INSUFFICIENT_FORWARD_OOS"


def test_incomplete_scenario_seed_matrix_does_not_enter_window_denominator() -> None:
    partial = _matrix(1)
    partial = [
        row
        for row in partial
        if not (
            row["scenario"] == "MAKER_PROMO_OFF"
            and row["seed"] == 97
        )
    ]

    assessment = evaluate_forward_oos(partial)

    assert assessment["complete_forward_oos_windows"] == 0
    assert assessment["conclusion_code"] == "INSUFFICIENT_FORWARD_OOS"


def test_each_observed_symbol_requires_its_own_complete_execution_matrix() -> None:
    rows = _matrix(1)
    rows = [
        row
        for row in rows
        if not (
            row["symbol"] == "SOXLUSDT"
            and row["scenario"] == "EXECUTION_STRESS"
            and row["seed"] == 97
        )
    ]

    assessment = evaluate_forward_oos(rows)

    assert assessment["complete_forward_oos_windows"] == 0


def test_portfolio_incomplete_marker_excludes_window_from_denominator() -> None:
    rows = [
        {**row, "portfolio_complete": False}
        for row in _matrix(1)
    ]

    assert evaluate_forward_oos(rows)["complete_forward_oos_windows"] == 0


def test_expected_symbol_set_prevents_partial_portfolio_counting() -> None:
    rows = [
        {
            **row,
            "expected_symbols": "MUUSDT;SNDKUSDT;SOXLUSDT",
            "portfolio_complete": True,
        }
        for row in _matrix(1, symbols=("SNDKUSDT", "SOXLUSDT"))
    ]

    assert evaluate_forward_oos(rows)["complete_forward_oos_windows"] == 0


def test_forward_oos_status_moves_at_four_and_eight_unique_windows() -> None:
    four = [row for index in range(4) for row in _matrix(index)]
    eight = [row for index in range(8) for row in _matrix(index)]

    assert evaluate_forward_oos(four)["conclusion_code"] == "FORWARD_OOS_ACCUMULATING"
    passed = evaluate_forward_oos(eight)
    assert passed["complete_forward_oos_windows"] == 8
    assert passed["conclusion_code"] == "PASS_FORWARD_OOS_RESEARCH_CANDIDATE"


def test_production_safety_requires_disabled_auto_entry_and_one_x() -> None:
    safe = production_safety_snapshot(
        {
            "timing": {
                "startup_auto_entry": False,
                "testnet_force_window": False,
                "testnet_fast_observation": False,
            },
            "trading": {"leverage": 1},
            "semiconductor_grid": {"economic_leverage": 1},
            "risk": {"effective_leverage_cap": 1},
        }
    )
    unsafe = production_safety_snapshot(
        {
            "timing": {"startup_auto_entry": True},
            "trading": {"leverage": 1},
            "semiconductor_grid": {"economic_leverage": 1},
            "risk": {"effective_leverage_cap": 1},
        }
    )

    assert safe["safe"] is True
    assert unsafe["safe"] is False

    string_flags = production_safety_snapshot(
        {
            "timing": {
                "startup_auto_entry": "false",
                "testnet_force_window": "false",
                "testnet_fast_observation": "false",
            },
            "trading": {"leverage": "1"},
            "semiconductor_grid": {"economic_leverage": "1"},
            "risk": {"effective_leverage_cap": "1"},
        }
    )
    assert string_flags["safe"] is True


def test_frozen_source_hash_is_independent_of_checkout_line_endings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"alpha\nbeta\n")
    lf_sha = _source_file_sha256(source)
    source.write_bytes(b"alpha\r\nbeta\r\n")

    assert _source_file_sha256(source) == lf_sha
