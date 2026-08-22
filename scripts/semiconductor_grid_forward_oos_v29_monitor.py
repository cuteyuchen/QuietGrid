"""Detect and append newly completed semiconductor v2.9 Forward OOS windows.

The monitor is deliberately research-only.  It reads the committed v2.9
freeze bundle, runs only the frozen 31111-NEUTRAL candidate, and never imports
the live application configuration or any trading controller.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from scripts.semiconductor_grid_forward_oos_v29 import (
    DEFAULT_DATA,
    DEFAULT_OUTPUT,
    _collect_window_manifest,
    _frozen_code_hashes,
    _ledger_candidate_hash,
    _parse_utc,
    _read_json_mapping,
    _run_eligible_windows,
    _source_file_sha256,
    _verify_frozen_hash,
)
from strategy.semiconductor_grid_v29 import (
    FORWARD_OOS_SCENARIOS,
    FORWARD_OOS_SEEDS,
    PRIMARY_CANDIDATE_ID,
    ForwardOOSLedger,
    canonical_json_bytes,
    file_sha256,
    production_safety_snapshot,
)


FROZEN_V29_RUNNER_SHA256 = (
    "79d87b1405507a77367751749ee7fea711c2db5d4aea05fefb482e9f8036525f"
)
EXPECTED_SCENARIOS = (
    "PRIMARY_ZERO_MAKER",
    "EXECUTION_STRESS",
    "MAKER_PROMO_OFF",
)
EXPECTED_SEEDS = (3, 10, 17, 31, 59, 97)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Detect complete market-closure windows and append frozen 31111 "
            "Forward OOS results"
        )
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--run-time-utc", default="")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help=(
            "report pending complete windows without running 31111 or changing "
            "the ledger/report artifacts"
        ),
    )
    return parser


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    seed = row.get("seed", "")
    try:
        normalized_seed = str(int(float(seed)))
    except (TypeError, ValueError):
        normalized_seed = str(seed)
    return (
        str(row.get("candidate_id") or ""),
        str(row.get("symbol") or ""),
        str(row.get("window_key") or ""),
        str(row.get("scenario") or ""),
        normalized_seed,
    )


def _eligible_manifest_rows(
    manifest: Iterable[Mapping[str, Any]], checked_at: datetime
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in manifest:
        row = dict(raw)
        if not bool(row.get("oos_eligible")) or not bool(row.get("complete_window")):
            continue
        window_end = row.get("window_end") or row.get("force_close_at")
        if not window_end or _parse_utc(str(window_end)) > checked_at:
            continue
        rows.append(row)
    return rows


def pending_complete_windows(
    manifest: Iterable[Mapping[str, Any]],
    ledger_records: Sequence[Mapping[str, Any]],
    *,
    candidate_sha: str,
    checked_at: datetime | str,
) -> list[dict[str, Any]]:
    """Return complete windows with at least one missing scenario/seed result."""

    now = _parse_utc(checked_at)
    eligible = _eligible_manifest_rows(manifest, now)
    seen = {
        _identity(row)
        for row in ledger_records
        if str(row.get("record_type") or "") == "OOS_RESULT"
        and str(row.get("candidate_id") or "") == PRIMARY_CANDIDATE_ID
        and str(row.get("candidate_sha") or "") == candidate_sha
        and bool(row.get("sequence_valid", True))
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in eligible:
        grouped.setdefault(str(row["window_key"]), []).append(row)

    pending: list[dict[str, Any]] = []
    for window_key, rows in sorted(grouped.items()):
        expected = {
            (
                PRIMARY_CANDIDATE_ID,
                str(row["symbol"]),
                window_key,
                scenario,
                str(seed),
            )
            for row in rows
            for scenario in FORWARD_OOS_SCENARIOS
            for seed in FORWARD_OOS_SEEDS
        }
        missing = expected - seen
        if not missing:
            continue
        first = rows[0]
        pending.append(
            {
                "window_key": window_key,
                "market_calendar": first.get("market_calendar", ""),
                "window_start": first.get("window_start", ""),
                "window_end": first.get("window_end", ""),
                "symbols": sorted({str(row["symbol"]) for row in rows}),
                "expected_result_rows": len(expected),
                "existing_result_rows": len(expected & seen),
                "missing_result_rows": len(missing),
                "manifest_rows": rows,
                "missing_identities": missing,
            }
        )
    return pending


def _validate_frozen_contract(
    output: Path,
) -> tuple[dict[str, Any], dict[str, Any], Path, str, str, dict[str, str]]:
    candidate_path = output / "candidate-31111-freeze.json"
    candidate_alias = output / "candidate-freeze.json"
    config_path = output / "config-freeze.json"
    rules_path = output / "exchange-rules.json"
    required = (
        candidate_path,
        candidate_alias,
        config_path,
        rules_path,
        output / "forward-oos-ledger.csv",
        output / "forward-oos-ledger.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing frozen v2.9 artifacts: " + ", ".join(missing))

    candidate = _read_json_mapping(candidate_path)
    combination = candidate.get("combination_definition")
    combination_id = (
        str(combination.get("combination_id") or "")
        if isinstance(combination, Mapping)
        else ""
    )
    if (
        candidate.get("protocol") != "semiconductor-grid-forward-oos-v2.9"
        or candidate.get("candidate_id") != PRIMARY_CANDIDATE_ID
        or combination_id != "31111"
        or candidate.get("direction") != "NEUTRAL"
    ):
        raise RuntimeError("Frozen candidate is not the registered 31111-NEUTRAL contract.")

    candidate_sha = file_sha256(candidate_path)
    _verify_frozen_hash("candidate alias", candidate_sha, file_sha256(candidate_alias))
    config_sha = file_sha256(config_path)
    rules_sha = file_sha256(rules_path)
    _verify_frozen_hash("config", candidate.get("config_sha"), config_sha)
    _verify_frozen_hash("exchange rules", candidate.get("exchange_rules_sha"), rules_sha)
    code_hashes = _frozen_code_hashes()
    for label in (
        "strategy_sha",
        "backtest_engine_sha",
        "calendar_engine_sha",
    ):
        actual = code_hashes[label]
        _verify_frozen_hash(label, candidate.get(label), actual)
    _verify_frozen_hash(
        "v2.9 execution runner",
        FROZEN_V29_RUNNER_SHA256,
        _source_file_sha256(
            Path(__file__).with_name("semiconductor_grid_forward_oos_v29.py")
        ),
    )
    if (
        FORWARD_OOS_SCENARIOS != EXPECTED_SCENARIOS
        or FORWARD_OOS_SEEDS != EXPECTED_SEEDS
    ):
        raise RuntimeError("Frozen Forward OOS scenario/seed matrix changed.")
    # ``code_sha`` covers the v2.9 research/evaluation module too.  The
    # monitor deliberately freezes the execution surface above while allowing
    # this branch's NOT_EVALUATED bookkeeping fix; ledger rows retain the
    # original candidate hash and never claim a new parameter sequence.
    code_hashes["code_sha"] = str(candidate.get("code_sha") or "")
    if not code_hashes["code_sha"]:
        raise RuntimeError("Frozen candidate is missing code_sha.")

    config_payload = _read_json_mapping(config_path)
    frozen_sections = config_payload.get("frozen_sections")
    if not isinstance(frozen_sections, Mapping):
        raise RuntimeError("config-freeze.json is missing frozen_sections.")
    raw_config = dict(frozen_sections)
    safety = production_safety_snapshot(raw_config)
    if not safety["safe"]:
        raise RuntimeError("Frozen production safety flags or 1x leverage are invalid.")
    execution = dict(
        dict(raw_config.get("semiconductor_grid") or {}).get("execution") or {}
    )
    scenarios = set(dict(execution.get("scenarios") or {}))
    if not set(FORWARD_OOS_SCENARIOS).issubset(scenarios):
        raise RuntimeError("Frozen 31111 execution scenario matrix is incomplete.")
    return candidate, raw_config, rules_path, candidate_sha, config_sha, code_hashes


def _assert_history_unchanged(
    before_records: Sequence[Mapping[str, Any]],
    after_records: Sequence[Mapping[str, Any]],
    before_csv: bytes,
    after_csv: bytes,
) -> None:
    if not after_csv.startswith(before_csv):
        raise RuntimeError("Forward OOS CSV history was modified instead of appended.")
    before = canonical_json_bytes(list(before_records))
    after_prefix = canonical_json_bytes(list(after_records[: len(before_records)]))
    if before != after_prefix:
        raise RuntimeError("Forward OOS ledger historical records were modified.")


@contextmanager
def _exclusive_monitor_lock(output: Path):
    lock_path = output / ".forward-oos-monitor.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Forward OOS monitor is already running: {lock_path}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"pid={os.getpid()}\n")
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def monitor_forward_oos(
    *,
    output_dir: str | Path = DEFAULT_OUTPUT,
    data_dir: str | Path = DEFAULT_DATA,
    run_time_utc: datetime | str | None = None,
    check_only: bool = False,
) -> dict[str, Any]:
    """Detect complete windows and append only missing frozen 31111 rows."""

    output = Path(output_dir)
    data = Path(data_dir)
    checked_at = _parse_utc(run_time_utc or datetime.now(UTC))
    output.mkdir(parents=True, exist_ok=True)
    with _exclusive_monitor_lock(output):
        (
            candidate,
            raw_config,
            rules_path,
            candidate_sha,
            config_sha,
            code_hashes,
        ) = _validate_frozen_contract(output)
        rules_sha = file_sha256(rules_path)
        ledger = ForwardOOSLedger(
            output / "forward-oos-ledger.csv",
            output / "forward-oos-ledger.json",
        )
        before_records = ledger.records()
        _verify_frozen_hash(
            "primary ledger candidate",
            candidate_sha,
            _ledger_candidate_hash(before_records, PRIMARY_CANDIDATE_ID),
        )
        cutoff = _parse_utc(str(candidate["exposure_cutoff"]))
        manifest, input_audit = _collect_window_manifest(
            raw_config=raw_config,
            data_dir=data,
            rules_path=rules_path,
            exposure_cutoff=cutoff,
        )
        pending = pending_complete_windows(
            manifest,
            before_records,
            candidate_sha=candidate_sha,
            checked_at=checked_at,
        )
        result: dict[str, Any] = {
            "checked_at_utc": checked_at.isoformat(),
            "mode": "CHECK_ONLY" if check_only else "MONITOR_FROZEN_31111",
            "candidate_id": PRIMARY_CANDIDATE_ID,
            "candidate_sha": candidate_sha,
            "exposure_cutoff": cutoff.isoformat(),
            "new_complete_window_count": sum(
                item["existing_result_rows"] == 0 for item in pending
            ),
            "pending_window_count": len(pending),
            "pending_windows": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"manifest_rows", "missing_identities"}
                }
                for item in pending
            ],
            "appended_rows": 0,
            "appended_primary_rows": 0,
            "appended_ex_mu_rows": 0,
            "input_audit": input_audit,
            "production_safety": production_safety_snapshot(raw_config),
        }
        if check_only or not pending:
            return result

        pending_manifest = [
            row for item in pending for row in item["manifest_rows"]
        ]
        expected_missing = {
            identity for item in pending for identity in item["missing_identities"]
        }
        before_csv = ledger.csv_path.read_bytes()
        frozen_hashes = {
            name: file_sha256(output / name)
            for name in (
                "candidate-31111-freeze.json",
                "candidate-freeze.json",
                "config-freeze.json",
                "exchange-rules.json",
            )
        }
        appended = _run_eligible_windows(
            raw_config=raw_config,
            data_dir=data,
            rules_path=rules_path,
            eligible_manifest=pending_manifest,
            candidate_id=PRIMARY_CANDIDATE_ID,
            candidate_sha=candidate_sha,
            config_sha=config_sha,
            rules_sha=rules_sha,
            code_sha=code_hashes["code_sha"],
            ledger=ledger,
        )
        appended_identities = {_identity(row) for row in appended}
        if appended_identities != expected_missing or len(appended) != len(expected_missing):
            raise RuntimeError(
                "Frozen 31111 run did not produce the complete missing scenario/seed matrix."
            )
        after_records = ledger.records()
        _assert_history_unchanged(
            before_records,
            after_records,
            before_csv,
            ledger.csv_path.read_bytes(),
        )
        for name, digest in frozen_hashes.items():
            _verify_frozen_hash(name, digest, file_sha256(output / name))
        result["appended_rows"] = len(appended)
        # Keep the legacy append CLI's primary-candidate field available for
        # callers that have not migrated to the monitor name yet.
        result["appended_primary_rows"] = len(appended)
        result["appended_ex_mu_rows"] = 0
        result["ledger_record_count_before"] = len(before_records)
        result["ledger_record_count_after"] = len(after_records)
        return result


def main() -> None:
    args = _parser().parse_args()
    result = monitor_forward_oos(
        output_dir=args.output_dir,
        data_dir=args.data_dir,
        run_time_utc=args.run_time_utc or None,
        check_only=args.check_only,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
