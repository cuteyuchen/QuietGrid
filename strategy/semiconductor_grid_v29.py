"""Frozen-candidate Forward OOS invariants for semiconductor grid v2.9.

This module contains no parameter search.  It owns the exposure boundary,
candidate registry, append-only ledger contract, and the formal eight-window
assessment.  The implementation is intentionally independent from the live
controller so research bookkeeping cannot enable trading.
"""

from __future__ import annotations

import csv
import json
import math
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


PRIMARY_CANDIDATE_ID = "31111-NEUTRAL"
DIAGNOSTIC_CONTROL_ID = "31121-NEUTRAL"
EX_MU_CANDIDATE_ID = "31111-NEUTRAL-EX-MU"
PRIMARY_SYMBOL_UNIVERSE = (
    "SNDKUSDT",
    "MUUSDT",
    "SOXLUSDT",
    "SKHYNIXUSDT",
)
EX_MU_SYMBOL_UNIVERSE = (
    "SNDKUSDT",
    "SOXLUSDT",
    "SKHYNIXUSDT",
)
FORWARD_OOS_SCENARIOS = (
    "PRIMARY_ZERO_MAKER",
    "EXECUTION_STRESS",
    "MAKER_PROMO_OFF",
)
FORWARD_OOS_SEEDS = (3, 10, 17, 31, 59, 97)
REQUIRED_FORWARD_WINDOWS = 8

_ISO_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)

LEDGER_FIELDS = (
    "record_type",
    "status",
    "reason",
    "candidate_id",
    "candidate_sha",
    "symbol",
    "market_calendar",
    "window_key",
    "window_start",
    "window_end",
    "force_close_at",
    "first_seen_at",
    "completed_at",
    "data_sha",
    "rules_sha",
    "config_sha",
    "code_sha",
    "scenario",
    "seed",
    "gate_status",
    "regime_status",
    "grid_count",
    "step_pct",
    "range_pct",
    "paired_grid_pnl",
    "inventory_realized_pnl",
    "funding_pnl",
    "fees",
    "slippage_cost",
    "net_pnl",
    "max_drawdown",
    "max_drawdown_pct",
    "inventory_drag",
    "inventory_drag_ratio",
    "pre_exit_inventory_notional",
    "max_inventory_utilization",
    "mean_inventory_utilization",
    "max_unpaired_lots",
    "max_unpaired_lot_age",
    "stop_loss_count",
    "window_force_close_count",
    "complete_window",
    "data_complete",
    "funding_complete",
    "rules_frozen",
    "force_close_covered",
    "expected_symbols",
    "observed_symbols",
    "portfolio_complete",
    "oos_eligible",
    "sequence_valid",
    "exposure_cutoff",
)


class LedgerInvariantError(RuntimeError):
    """Raised when an operation would rewrite frozen Forward OOS history."""


@dataclass(frozen=True)
class ExposureEvidence:
    latest_timestamp_present_in_any_research_input: datetime | None
    latest_window_seen_by_any_phase: datetime | None
    latest_window_seen_by_regime_or_gate: datetime | None
    candidate_freeze_time_utc: datetime

    @property
    def exposure_cutoff(self) -> datetime:
        values = [
            self.candidate_freeze_time_utc,
            self.latest_timestamp_present_in_any_research_input,
            self.latest_window_seen_by_any_phase,
            self.latest_window_seen_by_regime_or_gate,
        ]
        return max(_as_utc(value) for value in values if value is not None)

    def to_mapping(self) -> dict[str, str | None]:
        return {
            "latest_timestamp_present_in_any_research_input": _iso_or_none(
                self.latest_timestamp_present_in_any_research_input
            ),
            "latest_window_seen_by_any_phase": _iso_or_none(
                self.latest_window_seen_by_any_phase
            ),
            "latest_window_seen_by_regime_or_gate": _iso_or_none(
                self.latest_window_seen_by_regime_or_gate
            ),
            "candidate_freeze_time_utc": _iso(self.candidate_freeze_time_utc),
            "exposure_cutoff": _iso(self.exposure_cutoff),
        }


def compute_exposure_cutoff(
    latest_timestamp_present_in_any_research_input: datetime | str | int | float | None,
    latest_window_seen_by_any_phase: datetime | str | int | float | None,
    latest_window_seen_by_regime_or_gate: datetime | str | int | float | None,
    candidate_freeze_time_utc: datetime | str | int | float,
) -> datetime:
    """Return the v2.9 exposure boundary from every registered evidence class."""

    evidence = ExposureEvidence(
        _optional_datetime(latest_timestamp_present_in_any_research_input),
        _optional_datetime(latest_window_seen_by_any_phase),
        _optional_datetime(latest_window_seen_by_regime_or_gate),
        _parse_datetime(candidate_freeze_time_utc),
    )
    return evidence.exposure_cutoff


def build_exposure_evidence(
    *,
    research_input_timestamps: Iterable[datetime | str | int | float],
    phase_records: Iterable[Mapping[str, Any]],
    gate_records: Iterable[Mapping[str, Any]],
    candidate_freeze_time_utc: datetime | str | int | float,
) -> ExposureEvidence:
    return ExposureEvidence(
        _maximum_timestamp(research_input_timestamps),
        latest_window_seen(phase_records),
        latest_window_seen(gate_records),
        _parse_datetime(candidate_freeze_time_utc),
    )


def latest_window_seen(records: Iterable[Mapping[str, Any]]) -> datetime | None:
    """Include completed, blocked, Regime, and Gate-observed windows."""

    values: list[datetime] = []
    for record in records:
        for field in ("window_end", "force_close_at", "next_reference_open"):
            value = record.get(field)
            if value not in (None, ""):
                try:
                    values.append(_parse_datetime(value))
                except (TypeError, ValueError):
                    pass
        window_key = str(record.get("window_key") or "")
        timestamps = [_parse_datetime(value) for value in _ISO_TIMESTAMP.findall(window_key)]
        if timestamps:
            values.append(max(timestamps))
    return max(values) if values else None


def latest_timestamp_in_research_inputs(paths: Iterable[str | Path]) -> datetime | None:
    """Stream registered CSV/JSON inputs and return their latest market timestamp."""

    latest: datetime | None = None
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    for field in (
                        "close_time",
                        "open_time",
                        "funding_time",
                        "timestamp",
                        "time",
                    ):
                        value = row.get(field)
                        if value in (None, ""):
                            continue
                        latest = _max_datetime(latest, _parse_datetime(value))
        elif path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            for value in _json_market_timestamps(payload):
                latest = _max_datetime(latest, value)
    return latest


def classify_forward_window(
    window: Mapping[str, Any] | Any,
    exposure_cutoff: datetime | str | int | float,
) -> str:
    """Classify a window without letting a partial/pre-freeze window become OOS."""

    cutoff = _parse_datetime(exposure_cutoff)
    start = _window_datetime(window, "window_start", "observation_start", "start_time")
    end = _window_datetime(window, "window_end", "force_close_at", "end_time")
    complete = _window_bool(window, "complete_window", "complete", default=False)
    data_complete = _window_bool(window, "data_complete", default=complete)
    funding_complete = _window_bool(window, "funding_complete", default=complete)
    rules_frozen = _window_bool(window, "rules_frozen", default=True)
    force_close_covered = _window_bool(
        window, "force_close_covered", default=complete
    )
    if (
        not complete
        or not data_complete
        or not funding_complete
        or not rules_frozen
        or not force_close_covered
        or start is None
        or end is None
        or end <= start
    ):
        return "INCOMPLETE_WINDOW"
    first_seen = _window_datetime(window, "first_seen_at")
    if start <= cutoff or (first_seen is not None and first_seen <= cutoff):
        return "EXPOSED_HISTORY"
    return "FORWARD_OOS"


def is_forward_oos_eligible(
    window: Mapping[str, Any] | Any,
    exposure_cutoff: datetime | str | int | float,
) -> bool:
    return classify_forward_window(window, exposure_cutoff) == "FORWARD_OOS"


def first_eligible_forward_window(
    windows: Iterable[Mapping[str, Any] | Any],
    exposure_cutoff: datetime | str | int | float,
) -> Mapping[str, Any] | Any | None:
    eligible = [
        window
        for window in windows
        if is_forward_oos_eligible(window, exposure_cutoff)
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda window: _window_datetime(
            window, "window_start", "observation_start", "start_time"
        )
        or datetime.max.replace(tzinfo=UTC),
    )


def candidate_registry(
    *,
    include_ex_mu: bool = True,
    primary_candidate_sha: str | None = None,
    ex_mu_candidate_sha: str | None = None,
) -> dict[str, Any]:
    research_candidates: list[dict[str, Any]] = []
    if include_ex_mu:
        ex_mu = {
            "candidate_id": EX_MU_CANDIDATE_ID,
            "status": "NEW_POST_HOC_RESEARCH_CANDIDATE",
            "historical_validation": "NOT_CLAIMED",
            "forward_oos_count": 0,
            "symbol_universe": list(EX_MU_SYMBOL_UNIVERSE),
            "excluded_symbols": ["MUUSDT"],
            "independent_sequence": True,
        }
        if ex_mu_candidate_sha:
            ex_mu["candidate_sha"] = ex_mu_candidate_sha
        research_candidates.append(ex_mu)
    primary = {
        "candidate_id": PRIMARY_CANDIDATE_ID,
        "status": "PRIMARY_FORWARD_OOS_CANDIDATE",
        "combination_id": "31111",
        "direction": "NEUTRAL",
        "forward_oos_count": 0,
    }
    if primary_candidate_sha:
        primary["candidate_sha"] = primary_candidate_sha
    return {
        "primary_forward_oos_candidates": [primary],
        "diagnostic_controls": [
            {
                "candidate_id": DIAGNOSTIC_CONTROL_ID,
                "status": "DIAGNOSTIC_CONTROL_ONLY",
                "independent_primary_candidate": False,
                "forward_oos_count": 0,
                "reason": "D2 did not produce an observable historical behavior difference.",
            }
        ],
        "post_hoc_research_candidates": research_candidates,
    }


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(payload: Any) -> str:
    return sha256(canonical_json_bytes(payload)).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ForwardOOSLedger:
    """CSV + JSON ledger with immutable-prefix and candidate-hash checks."""

    def __init__(self, csv_path: str | Path, json_path: str | Path) -> None:
        self.csv_path = Path(csv_path)
        self.json_path = Path(json_path)

    def records(self) -> list[dict[str, Any]]:
        csv_rows = self._read_csv()
        json_rows = self._read_json()
        if bool(csv_rows) != bool(json_rows):
            raise LedgerInvariantError("CSV/JSON ledger presence mismatch.")
        if csv_rows and not _records_equal(csv_rows, json_rows):
            raise LedgerInvariantError("CSV/JSON ledger history mismatch.")
        return json_rows

    def initialize(self, records: Iterable[Mapping[str, Any]]) -> None:
        incoming = [_normalize_ledger_record(record) for record in records]
        existing = self.records()
        if existing:
            if not _records_equal(existing, incoming):
                raise LedgerInvariantError(
                    "Forward OOS ledger is frozen; initialization cannot rewrite it."
                )
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_initial(incoming)

    def append(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        candidate_sha: str | None = None,
        candidate_id: str = PRIMARY_CANDIDATE_ID,
        candidate_shas: Mapping[str, str] | None = None,
        completed_at: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Append rows without rewriting the frozen history.

        ``candidate_sha`` remains the convenient primary-candidate argument
        used by the v2.9 runner.  ``candidate_shas`` and the per-row
        ``candidate_id``/``candidate_sha`` fields allow EX-MU (or a future
        candidate) to use an independent sequence in the same ledger.
        A changed hash is accepted only as an explicit ``CANDIDATE_FREEZE``
        event; otherwise a ``SEQUENCE_INVALIDATED`` marker is appended and
        the incoming rows are excluded from assessment.
        """
        existing = self.records()
        if not existing:
            raise LedgerInvariantError("Ledger must be initialized before append.")
        incoming = [_normalize_ledger_record(record) for record in records]
        if not incoming:
            return []
        supplied_shas = dict(candidate_shas or {})
        if candidate_sha:
            supplied_shas.setdefault(candidate_id, candidate_sha)

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in incoming:
            row_candidate_id = str(row.get("candidate_id") or candidate_id)
            row["candidate_id"] = row_candidate_id
            supplied_sha = supplied_shas.get(row_candidate_id)
            explicit_sha = str(row.get("candidate_sha") or "")
            if supplied_sha and explicit_sha and explicit_sha != supplied_sha:
                raise LedgerInvariantError("Appended row candidate hash mismatch.")
            row_sha = str(explicit_sha or supplied_sha or "")
            if not row_sha:
                raise LedgerInvariantError(
                    f"Candidate {row_candidate_id} requires a candidate_sha."
                )
            row["candidate_sha"] = row_sha
            grouped.setdefault(row_candidate_id, []).append(row)

        new_rows: list[dict[str, Any]] = []
        completed_iso = _iso(_parse_datetime(completed_at or datetime.now(UTC)))
        for row_candidate_id, rows in grouped.items():
            state = _candidate_sequence_state(existing, row_candidate_id)
            incoming_hashes = {str(row["candidate_sha"]) for row in rows}
            if len(incoming_hashes) != 1:
                raise LedgerInvariantError(
                    f"Candidate {row_candidate_id} rows contain multiple hashes."
                )
            incoming_sha = next(iter(incoming_hashes))
            starts_new_sequence = any(
                str(row.get("record_type") or "") == "CANDIDATE_FREEZE"
                for row in rows
            )
            if starts_new_sequence:
                # A freeze event is the only legal boundary for a new hash.
                if state["valid"] and state["hash"] and incoming_sha != state["hash"]:
                    new_rows.append(
                        _normalize_ledger_record(
                            {
                                "record_type": "SEQUENCE_INVALIDATED",
                                "status": "CANDIDATE_HASH_CHANGED",
                                "reason": (
                                    "Candidate hash changed; old OOS sequence cannot be reused."
                                ),
                                "candidate_id": row_candidate_id,
                                "candidate_sha": incoming_sha,
                                "completed_at": completed_iso,
                                "complete_window": False,
                                "oos_eligible": False,
                                "sequence_valid": False,
                            }
                        )
                    )
                for row in rows:
                    row["sequence_valid"] = True
                new_rows.extend(rows)
                continue

            if state["hash"] and incoming_sha != state["hash"]:
                invalidation = _normalize_ledger_record(
                    {
                        "record_type": "SEQUENCE_INVALIDATED",
                        "status": "CANDIDATE_HASH_CHANGED",
                        "reason": "Candidate hash changed; old OOS sequence cannot be reused.",
                        "candidate_id": row_candidate_id,
                        "candidate_sha": incoming_sha,
                        "completed_at": completed_iso,
                        "complete_window": False,
                        "oos_eligible": False,
                        "sequence_valid": False,
                    }
                )
                new_rows.append(invalidation)
                new_rows.extend(
                    {
                        **row,
                        "oos_eligible": False,
                        "sequence_valid": False,
                        "status": "CANDIDATE_HASH_CHANGED",
                    }
                    for row in rows
                )
                continue

            for row in rows:
                row["sequence_valid"] = bool(state["valid"] or not state["hash"])
            new_rows.extend(rows)

        if not new_rows:
            return []
        self._append_csv(new_rows)
        self._write_json(existing + new_rows)
        return new_rows

    @property
    def sequence_valid(self) -> bool:
        records = self.records()
        return bool(_candidate_sequence_state(records, PRIMARY_CANDIDATE_ID)["valid"])

    def _read_csv(self) -> list[dict[str, Any]]:
        if not self.csv_path.is_file() or self.csv_path.stat().st_size == 0:
            return []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [_coerce_ledger_record(row) for row in csv.DictReader(handle)]

    def _read_json(self) -> list[dict[str, Any]]:
        if not self.json_path.is_file() or self.json_path.stat().st_size == 0:
            return []
        payload = json.loads(self.json_path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            payload = payload.get("records", [])
        if not isinstance(payload, list):
            raise LedgerInvariantError("JSON ledger must contain a record list.")
        return [_normalize_ledger_record(row) for row in payload]

    def _write_initial(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with self.csv_path.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=LEDGER_FIELDS, lineterminator="\n"
            )
            writer.writeheader()
            writer.writerows(rows)
        self._write_json(list(rows), exclusive=True)

    def _append_csv(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with self.csv_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=LEDGER_FIELDS, lineterminator="\n"
            )
            writer.writerows(rows)

    def _write_json(
        self, rows: Sequence[Mapping[str, Any]], *, exclusive: bool = False
    ) -> None:
        mode = "x" if exclusive else "w"
        with self.json_path.open(mode, encoding="utf-8", newline="\n") as handle:
            json.dump(list(rows), handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")


def aggregate_forward_oos(
    records: Iterable[Mapping[str, Any]],
    *,
    candidate_id: str = PRIMARY_CANDIDATE_ID,
    scenario: str = "PRIMARY_ZERO_MAKER",
) -> dict[str, Any]:
    all_records = list(records)
    valid_oos_rows = _valid_oos_rows(all_records, candidate_id)
    rows = [
        row
        for row in valid_oos_rows
        if str(row.get("scenario") or "") == scenario
        and _truthy(row.get("complete_window"))
        and _truthy(row.get("oos_eligible"))
    ]
    window_groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        window_groups.setdefault(str(row.get("window_key") or ""), []).append(row)
    window_rows: list[dict[str, Any]] = []
    for window_key, values in sorted(window_groups.items()):
        window_rows.append(
            {
                "window_key": window_key,
                "window_end": max(str(row.get("window_end") or "") for row in values),
                "net_pnl": _mean_seed_sum(values, "net_pnl"),
                "paired_grid_pnl": _mean_seed_sum(values, "paired_grid_pnl"),
                "inventory_realized_pnl": _mean_seed_sum(
                    values, "inventory_realized_pnl"
                ),
                "funding_pnl": _mean_seed_sum(values, "funding_pnl"),
                "fees": _mean_seed_sum(values, "fees"),
                "slippage_cost": _mean_seed_sum(values, "slippage_cost"),
                "inventory_drag": _mean_seed_sum(values, "inventory_drag"),
                "max_drawdown": max(_number(row.get("max_drawdown")) for row in values),
                "max_drawdown_pct": max(
                    _number(row.get("max_drawdown_pct")) for row in values
                ),
                "pre_exit_inventory_notional": _mean_seed_sum(
                    values, "pre_exit_inventory_notional"
                ),
                "max_inventory_utilization": max(
                    _number(row.get("max_inventory_utilization")) for row in values
                ),
                "mean_inventory_utilization": _mean_field(
                    values, "mean_inventory_utilization"
                ),
                "max_unpaired_lots": max(
                    _number(row.get("max_unpaired_lots")) for row in values
                ),
                "max_unpaired_lot_age": max(
                    _number(row.get("max_unpaired_lot_age")) for row in values
                ),
                "stop_loss_count": _mean_seed_sum(values, "stop_loss_count"),
                "window_force_close_count": _mean_seed_sum(
                    values, "window_force_close_count"
                ),
                "grid_count": _mean_field(values, "grid_count"),
                "step_pct": _mean_field(values, "step_pct"),
                "range_pct": _mean_field(values, "range_pct"),
            }
        )
    pnls = [row["net_pnl"] for row in window_rows]
    gains = sum(max(value, 0.0) for value in pnls)
    losses = sum(max(-value, 0.0) for value in pnls)
    paired = sum(row["paired_grid_pnl"] for row in window_rows)
    drag = sum(row["inventory_drag"] for row in window_rows)
    positive_sorted = sorted((max(value, 0.0) for value in pnls), reverse=True)
    symbol_contribution: dict[str, float] = {}
    month_contribution: dict[str, float] = {}
    seed_totals: dict[str, float] = {}
    symbol_window_groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "UNKNOWN")
        window_key = str(row.get("window_key") or "")
        symbol_window_groups.setdefault((symbol, window_key), []).append(row)
        seed = str(row.get("seed") or "")
        seed_totals[seed] = seed_totals.get(seed, 0.0) + _number(row.get("net_pnl"))
    for (symbol, _window_key), values in symbol_window_groups.items():
        symbol_contribution[symbol] = symbol_contribution.get(symbol, 0.0) + _mean_seed_sum(
            values, "net_pnl"
        )
    for row in window_rows:
        month = str(row.get("window_end") or "")[:7] or "UNKNOWN"
        month_contribution[month] = month_contribution.get(month, 0.0) + _number(
            row.get("net_pnl")
        )
    worst_count = max(1, math.ceil(len(pnls) * 0.05)) if pnls else 0
    worst_values = sorted(pnls)[:worst_count] if worst_count else []
    return {
        "candidate_id": candidate_id,
        "scenario": scenario,
        "complete_forward_oos_windows": len(window_groups),
        "net_pnl": sum(pnls),
        "paired_grid_pnl": paired,
        "inventory_realized_pnl": sum(
            row["inventory_realized_pnl"] for row in window_rows
        ),
        "funding_pnl": sum(row["funding_pnl"] for row in window_rows),
        "fees": sum(row["fees"] for row in window_rows),
        "slippage_cost": sum(row["slippage_cost"] for row in window_rows),
        "mean_window_pnl": statistics.fmean(pnls) if pnls else 0.0,
        "median_window_pnl": statistics.median(pnls) if pnls else 0.0,
        "positive_window_ratio": (
            sum(value > 0 for value in pnls) / len(pnls) if pnls else 0.0
        ),
        "profit_factor": gains / losses if losses else (gains / 1e-12 if gains else 0.0),
        "max_drawdown": max(
            (row["max_drawdown"] for row in window_rows), default=0.0
        ),
        "max_drawdown_pct": max(
            (row["max_drawdown_pct"] for row in window_rows), default=0.0
        ),
        "worst_window_pnl": min(pnls, default=0.0),
        "worst_5pct_mean": statistics.fmean(worst_values) if worst_values else 0.0,
        "CVaR_95": statistics.fmean(worst_values) if worst_values else 0.0,
        "inventory_drag": drag,
        "inventory_drag_ratio": drag / max(paired, 0.01),
        "pre_exit_inventory_notional": sum(
            row["pre_exit_inventory_notional"] for row in window_rows
        ),
        "max_inventory_utilization": max(
            (row["max_inventory_utilization"] for row in window_rows), default=0.0
        ),
        "mean_inventory_utilization": statistics.fmean(
            [row["mean_inventory_utilization"] for row in window_rows]
        )
        if window_rows
        else 0.0,
        "max_unpaired_lots": max(
            (row["max_unpaired_lots"] for row in window_rows), default=0.0
        ),
        "max_unpaired_lot_age": max(
            (row["max_unpaired_lot_age"] for row in window_rows), default=0.0
        ),
        "stop_loss_count": sum(row["stop_loss_count"] for row in window_rows),
        "window_force_close_count": sum(
            row["window_force_close_count"] for row in window_rows
        ),
        "mean_grid_count": statistics.fmean(
            row["grid_count"] for row in window_rows
        )
        if window_rows
        else 0.0,
        "mean_step_pct": statistics.fmean(row["step_pct"] for row in window_rows)
        if window_rows
        else 0.0,
        "mean_range_pct": statistics.fmean(row["range_pct"] for row in window_rows)
        if window_rows
        else 0.0,
        "seed_positive_count": sum(value > 0 for value in seed_totals.values()),
        "best_window_concentration": _concentration(positive_sorted, 1),
        "top_2_window_concentration": _concentration(positive_sorted, 2),
        "top_3_window_concentration": _concentration(positive_sorted, 3),
        "symbol_contribution": symbol_contribution,
        "month_contribution": month_contribution,
        "window_metrics": window_rows,
    }


def symbol_breakdown(
    records: Iterable[Mapping[str, Any]],
    *,
    candidate_id: str = PRIMARY_CANDIDATE_ID,
    scenario: str = "PRIMARY_ZERO_MAKER",
    symbol_universe: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    rows = list(records)
    symbols = sorted(
        set(symbol_universe or ())
        | {
            str(row.get("symbol") or "")
            for row in rows
            if row.get("candidate_id") == candidate_id and row.get("symbol")
        }
    )
    output: list[dict[str, Any]] = []
    for symbol in symbols:
        metrics = aggregate_forward_oos(
            [row for row in rows if str(row.get("symbol") or "") == symbol],
            candidate_id=candidate_id,
            scenario=scenario,
        )
        output.append(
            {
                "symbol": symbol,
                "net_pnl": metrics["net_pnl"],
                "profit_factor": metrics["profit_factor"],
                "positive_window_ratio": metrics["positive_window_ratio"],
                "inventory_drag": metrics["inventory_drag"],
                "max_drawdown": metrics["max_drawdown"],
                "complete_forward_oos_windows": metrics[
                    "complete_forward_oos_windows"
                ],
            }
        )
    return output


def evaluate_forward_oos(
    records: Iterable[Mapping[str, Any]],
    *,
    candidate_id: str = PRIMARY_CANDIDATE_ID,
    required_windows: int = REQUIRED_FORWARD_WINDOWS,
) -> dict[str, Any]:
    rows = list(records)
    if candidate_id == PRIMARY_CANDIDATE_ID:
        symbol_universe = PRIMARY_SYMBOL_UNIVERSE
    elif candidate_id == EX_MU_CANDIDATE_ID:
        symbol_universe = EX_MU_SYMBOL_UNIVERSE
    else:
        symbol_universe = None
    complete_keys = complete_forward_window_keys(rows, candidate_id=candidate_id)
    valid_oos = _valid_oos_rows(rows, candidate_id)
    valid_oos_ids = {id(row) for row in valid_oos}
    assessment_rows = [
        row
        for row in rows
        if (
            str(row.get("record_type") or "") != "OOS_RESULT"
            or str(row.get("candidate_id") or "") != candidate_id
            or (
                id(row) in valid_oos_ids
                and str(row.get("window_key") or "") in complete_keys
            )
        )
    ]
    scenarios = {
        scenario: aggregate_forward_oos(
            assessment_rows, candidate_id=candidate_id, scenario=scenario
        )
        for scenario in FORWARD_OOS_SCENARIOS
    }
    primary = scenarios["PRIMARY_ZERO_MAKER"]
    complete_windows = len(complete_keys)
    symbols = symbol_breakdown(
        assessment_rows,
        candidate_id=candidate_id,
        symbol_universe=symbol_universe,
    )
    positive_symbols = sum(row["net_pnl"] > 0 for row in symbols)
    data_quality_error = any(
        str(row.get("record_type") or "") == "DATA_QUALITY_ERROR"
        and str(row.get("candidate_id") or "") == candidate_id
        for row in rows
    )
    data_quality_ok = not data_quality_error and all(
        _truthy(row.get("complete_window"))
        and _truthy(row.get("data_complete"))
        and _truthy(row.get("funding_complete"))
        and _truthy(row.get("rules_frozen"))
        and _truthy(row.get("force_close_covered"))
        for row in assessment_rows
        if str(row.get("record_type") or "") == "OOS_RESULT"
        and str(row.get("candidate_id") or "") == candidate_id
        and _truthy(row.get("oos_eligible"))
    )
    gates = {
        "complete_forward_oos_windows": complete_windows >= required_windows,
        "primary_net_pnl": primary["net_pnl"] > 0,
        "primary_median_window_pnl": primary["median_window_pnl"] >= 0,
        "primary_positive_window_ratio": primary["positive_window_ratio"] >= 0.50,
        "primary_profit_factor": primary["profit_factor"] >= 1.05,
        "execution_stress_net_pnl": scenarios["EXECUTION_STRESS"]["net_pnl"] >= 0,
        "maker_promo_off_net_pnl": scenarios["MAKER_PROMO_OFF"]["net_pnl"] >= 0,
        "seed_positive_count": primary["seed_positive_count"] >= 4,
        "inventory_drag_ratio": primary["inventory_drag_ratio"] <= 0.75,
        "max_drawdown_pct": primary["max_drawdown_pct"] <= 0.05,
        "best_window_concentration": primary["best_window_concentration"] <= 0.50,
        "multiple_symbol_support": positive_symbols >= 2,
        "data_quality": data_quality_ok,
    }
    if complete_windows <= 3:
        conclusion = "INSUFFICIENT_FORWARD_OOS"
    elif complete_windows < required_windows:
        conclusion = "FORWARD_OOS_ACCUMULATING"
    elif not gates["data_quality"]:
        conclusion = "FAIL_FORWARD_OOS_DATA_QUALITY"
    elif not gates["execution_stress_net_pnl"]:
        conclusion = "FAIL_FORWARD_OOS_EXECUTION_STRESS"
    elif not gates["inventory_drag_ratio"] or not gates["max_drawdown_pct"]:
        conclusion = "FAIL_FORWARD_OOS_INVENTORY_TAIL"
    elif not gates["best_window_concentration"] or not gates["multiple_symbol_support"]:
        conclusion = "FAIL_FORWARD_OOS_CONCENTRATION"
    elif not all(
        passed
        for name, passed in gates.items()
        if name != "maker_promo_off_net_pnl"
    ):
        conclusion = "FAIL_FORWARD_OOS_NO_EDGE"
    elif not gates["maker_promo_off_net_pnl"]:
        conclusion = "PASS_FORWARD_OOS_MAKER_DEPENDENT"
    else:
        conclusion = "PASS_FORWARD_OOS_RESEARCH_CANDIDATE"
    return {
        "candidate_id": candidate_id,
        "conclusion_code": conclusion,
        "complete_forward_oos_windows": complete_windows,
        "required_forward_oos_windows": required_windows,
        "formal_assessment_allowed": complete_windows >= required_windows,
        "gates": gates,
        "scenarios": scenarios,
        "symbol_breakdown": symbols,
    }


def complete_forward_window_keys(
    records: Iterable[Mapping[str, Any]],
    *,
    candidate_id: str = PRIMARY_CANDIDATE_ID,
    scenarios: Sequence[str] = FORWARD_OOS_SCENARIOS,
    seeds: Sequence[int] = FORWARD_OOS_SEEDS,
) -> set[str]:
    """Return windows with the complete registered scenario/seed matrix."""

    matrix: dict[str, dict[str, dict[str, set[int]]]] = {}
    expected_symbols: dict[str, set[str]] = {}
    for row in _valid_oos_rows(list(records), candidate_id):
        if (
            not _truthy(row.get("complete_window"))
            or not _truthy(row.get("data_complete"))
            or not _truthy(row.get("funding_complete"))
            or not _truthy(row.get("rules_frozen"))
            or not _truthy(row.get("force_close_covered"))
            or (
                row.get("portfolio_complete") not in (None, "")
                and not _truthy(row.get("portfolio_complete"))
            )
            or not _truthy(row.get("oos_eligible"))
        ):
            continue
        window_key = str(row.get("window_key") or "")
        symbol = str(row.get("symbol") or "")
        scenario = str(row.get("scenario") or "")
        if not window_key or not symbol:
            continue
        expected_symbols.setdefault(window_key, set()).update(
            value
            for value in str(row.get("expected_symbols") or "").split(";")
            if value
        )
        try:
            seed = int(float(row.get("seed")))
        except (TypeError, ValueError):
            continue
        matrix.setdefault(window_key, {}).setdefault(symbol, {}).setdefault(
            scenario, set()
        ).add(seed)
    required_seeds = set(seeds)
    return {
        window_key
        for window_key, symbols in matrix.items()
        if symbols
        and (
            not expected_symbols.get(window_key)
            or set(symbols) >= expected_symbols[window_key]
        )
        and all(
            all(values.get(scenario, set()) >= required_seeds for scenario in scenarios)
            for values in symbols.values()
        )
    }


def _candidate_sequence_state(
    records: Sequence[Mapping[str, Any]], candidate_id: str
) -> dict[str, Any]:
    """Resolve the latest valid hash and freeze boundary for one candidate.

    Ledger rows are ordered append-only events.  A later ``CANDIDATE_FREEZE``
    starts a new sequence after an invalidation; rows from the previous
    sequence remain in the file for audit but are not eligible for metrics.
    """

    active_hash = ""
    valid = True
    freeze_index = -1
    saw_oos = False
    for index, row in enumerate(records):
        if str(row.get("candidate_id") or "") != candidate_id:
            continue
        record_type = str(row.get("record_type") or "")
        row_hash = str(row.get("candidate_sha") or "")
        if record_type in {"CANDIDATE_FREEZE", "POST_HOC_CANDIDATE"}:
            if row_hash:
                active_hash = row_hash
                valid = True
                freeze_index = index
            continue
        if record_type == "SEQUENCE_INVALIDATED":
            valid = False
            continue
        if record_type == "OOS_RESULT":
            saw_oos = True
            if not active_hash and row_hash:
                active_hash = row_hash
            elif active_hash and row_hash and row_hash != active_hash:
                valid = False
    return {
        "hash": active_hash,
        "valid": valid,
        "freeze_index": freeze_index,
        "saw_oos": saw_oos,
    }


def _valid_oos_rows(
    records: Sequence[Mapping[str, Any]], candidate_id: str
) -> list[Mapping[str, Any]]:
    state = _candidate_sequence_state(records, candidate_id)
    if not state["valid"]:
        return []
    active_hash = str(state["hash"] or "")
    freeze_index = int(state["freeze_index"])
    rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(records):
        if index <= freeze_index:
            continue
        if (
            str(row.get("record_type") or "") == "OOS_RESULT"
            and str(row.get("candidate_id") or "") == candidate_id
            and (not active_hash or str(row.get("candidate_sha") or "") == active_hash)
            and not _falsey(row.get("sequence_valid"))
        ):
            rows.append(row)
    return rows


def production_safety_snapshot(config: Mapping[str, Any]) -> dict[str, Any]:
    timing = dict(config.get("timing") or {})
    trading = dict(config.get("trading") or {})
    research = dict(config.get("semiconductor_grid") or {})
    risk = dict(config.get("risk") or {})
    try:
        trading_leverage = float(trading.get("leverage", 1))
        economic_leverage = float(research.get("economic_leverage", 1))
        effective_cap = float(risk.get("effective_leverage_cap", 1))
    except (TypeError, ValueError):
        trading_leverage = economic_leverage = effective_cap = math.nan
    snapshot = {
        "startup_auto_entry": _truthy(timing.get("startup_auto_entry", False)),
        "testnet_force_window": _truthy(timing.get("testnet_force_window", False)),
        "testnet_fast_observation": _truthy(
            timing.get("testnet_fast_observation", False)
        ),
        "trading_leverage": trading_leverage,
        "economic_leverage": economic_leverage,
        "effective_leverage_cap": effective_cap,
    }
    snapshot["safe"] = (
        not snapshot["startup_auto_entry"]
        and not snapshot["testnet_force_window"]
        and not snapshot["testnet_fast_observation"]
        and math.isfinite(snapshot["trading_leverage"])
        and snapshot["trading_leverage"] == 1
        and math.isfinite(snapshot["economic_leverage"])
        and snapshot["economic_leverage"] == 1
        and math.isfinite(snapshot["effective_leverage_cap"])
        and snapshot["effective_leverage_cap"] == 1
    )
    return snapshot


def _normalize_ledger_record(record: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    bool_fields = {
        "complete_window",
        "data_complete",
        "funding_complete",
        "rules_frozen",
        "force_close_covered",
        "portfolio_complete",
        "oos_eligible",
        "sequence_valid",
    }
    for field in LEDGER_FIELDS:
        value = record.get(field, "")
        if field in bool_fields and value != "":
            value = _truthy(value)
        normalized[field] = value if value is not None else ""
    return normalized


def _coerce_ledger_record(record: Mapping[str, Any]) -> dict[str, Any]:
    result = _normalize_ledger_record(record)
    number_fields = {
        "grid_count",
        "step_pct",
        "range_pct",
        "paired_grid_pnl",
        "inventory_realized_pnl",
        "funding_pnl",
        "fees",
        "slippage_cost",
        "net_pnl",
        "max_drawdown",
        "max_drawdown_pct",
        "inventory_drag",
        "inventory_drag_ratio",
        "pre_exit_inventory_notional",
        "max_inventory_utilization",
        "mean_inventory_utilization",
        "max_unpaired_lots",
        "max_unpaired_lot_age",
        "stop_loss_count",
        "window_force_close_count",
    }
    for field in number_fields:
        if result[field] != "":
            result[field] = _number(result[field])
    if result["seed"] != "":
        result["seed"] = int(float(result["seed"]))
    return result


def _records_equal(
    left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]]
) -> bool:
    return canonical_json_bytes([_coerce_ledger_record(row) for row in left]) == canonical_json_bytes(
        [_coerce_ledger_record(row) for row in right]
    )


def _json_market_timestamps(value: Any, key: str = "") -> Iterable[datetime]:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            yield from _json_market_timestamps(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _json_market_timestamps(child, key)
    elif any(token in key.lower() for token in ("time", "timestamp", "funding")):
        try:
            yield _parse_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return


def _window_value(window: Mapping[str, Any] | Any, name: str) -> Any:
    if isinstance(window, Mapping):
        return window.get(name)
    value = getattr(window, name, None)
    return value() if callable(value) else value


def _window_datetime(
    window: Mapping[str, Any] | Any, *names: str
) -> datetime | None:
    for name in names:
        value = _window_value(window, name)
        if value not in (None, ""):
            try:
                return _parse_datetime(value)
            except (TypeError, ValueError):
                pass
    return None


def _window_bool(
    window: Mapping[str, Any] | Any, *names: str, default: bool
) -> bool:
    for name in names:
        value = _window_value(window, name)
        if value not in (None, ""):
            return _truthy(value)
    return default


def _parse_datetime(value: datetime | str | int | float) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)) or str(value).strip().isdigit():
        number = float(value)
        if abs(number) >= 1e11:
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=UTC)
    raw = str(value).strip().replace("Z", "+00:00")
    return _as_utc(datetime.fromisoformat(raw))


def _optional_datetime(
    value: datetime | str | int | float | None,
) -> datetime | None:
    return None if value in (None, "") else _parse_datetime(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _iso_or_none(value: datetime | None) -> str | None:
    return _iso(value) if value is not None else None


def _maximum_timestamp(
    values: Iterable[datetime | str | int | float],
) -> datetime | None:
    parsed = [_parse_datetime(value) for value in values]
    return max(parsed) if parsed else None


def _max_datetime(left: datetime | None, right: datetime) -> datetime:
    return right if left is None or right > left else left


def _number(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _mean_field(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    return statistics.fmean(_number(row.get(field)) for row in rows) if rows else 0.0


def _mean_seed_sum(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    """Average portfolio totals across seeds without multiplying symbols."""

    by_seed: dict[str, float] = {}
    for row in rows:
        seed = str(row.get("seed") or "")
        by_seed[seed] = by_seed.get(seed, 0.0) + _number(row.get(field))
    return statistics.fmean(by_seed.values()) if by_seed else 0.0


def _concentration(values: Sequence[float], count: int) -> float:
    total = sum(values)
    return sum(values[:count]) / total if total > 0 else 0.0


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _falsey(value: Any) -> bool:
    if value in (None, ""):
        return False
    return not _truthy(value)
