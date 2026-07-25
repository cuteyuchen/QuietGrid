"""Rebuild the stock-perpetual H1.1 W/O/R windows without touching v2.5."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.scheduler import Scheduler  # noqa: E402
from data_sources.models import NormalizedKline  # noqa: E402
from scripts import build_stock_perp_windows as v2_5_windows  # noqa: E402
from scripts.stock_perp_common import (  # noqa: E402
    SEED_VALUES,
    git_branch,
    git_commit,
    immutable_write,
    write_csv,
    write_json,
)
from strategy.window_models import WindowKind  # noqa: E402


UTC = timezone.utc
PROTOCOL_PATH = Path("docs/codex-stock-perp-weekend-grid-h1-1-retest-v2.6.md")
SOURCE_REPORT_DIR = Path("reports/stock-perp-weekend-grid-v1")
OUTPUT_DIR = Path("reports/stock-perp-weekend-grid-h1-1-v2.6")
DEVELOPMENT_MONTHS = ("2026-03", "2026-04", "2026-05")
EXPOSED_VALIDATION_MONTH = "2026-06"
OBSERVATION_ROWS = 180
MIN_TRADABLE_ROWS = 30

CORE_SYMBOLS = (
    "AMZNUSDT",
    "COINUSDT",
    "CRCLUSDT",
    "HOODUSDT",
    "INTCUSDT",
    "MSTRUSDT",
    "PLTRUSDT",
    "TSLAUSDT",
)
TRADITIONAL_EQUITY = (
    "AMZNUSDT",
    "INTCUSDT",
    "PLTRUSDT",
    "TSLAUSDT",
)
CRYPTO_SENSITIVE_EQUITY = (
    "COINUSDT",
    "CRCLUSDT",
    "HOODUSDT",
    "MSTRUSDT",
)

WINDOW_FIELDS = (
    "window_id",
    "symbol",
    "asset_group",
    "group",
    "seed",
    "matched_window_id",
    "matched_calendar_key",
    "calendar_key",
    "market_close",
    "force_close_at",
    "observation_start",
    "observation_end",
    "tradable_start",
    "tradable_end",
    "row_start_index",
    "row_end_index",
    "row_count",
    "observation_rows",
    "tradable_rows",
    "tradable_hours",
    "duration_minutes",
    "month",
    "split",
    "listing_stage",
    "window_type",
    "status",
    "skip_reason",
    "data_gap_count",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rebuild fair H1.1 W/O/R windows")
    parser.add_argument(
        "--discovery",
        default=str(SOURCE_REPORT_DIR / "symbol-discovery.json"),
    )
    parser.add_argument(
        "--data-manifest",
        default=str(SOURCE_REPORT_DIR / "asset-data-manifest.json"),
    )
    parser.add_argument(
        "--audit-json",
        default=str(SOURCE_REPORT_DIR / "asset-data-audit.json"),
    )
    parser.add_argument(
        "--v2-5-window-manifest",
        default=str(SOURCE_REPORT_DIR / "window-manifest.json"),
    )
    parser.add_argument(
        "--v2-5-results",
        default=str(SOURCE_REPORT_DIR / "results.json"),
    )
    parser.add_argument("--protocol", default=str(PROTOCOL_PATH))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    return parser


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def _iso(value: datetime | int) -> str:
    parsed = (
        datetime.fromtimestamp(value / 1000, tz=UTC)
        if isinstance(value, int)
        else value.astimezone(UTC)
    )
    return parsed.isoformat().replace("+00:00", "Z")


def _timestamp_ms(value: str) -> int:
    return int(_parse_iso(value).timestamp() * 1000)


def _asset_group(symbol: str) -> str:
    if symbol in TRADITIONAL_EQUITY:
        return "TRADITIONAL_EQUITY"
    if symbol in CRYPTO_SENSITIVE_EQUITY:
        return "CRYPTO_SENSITIVE_EQUITY"
    raise ValueError(f"Unregistered Tier A-Core symbol: {symbol}")


def _protocol_revision(protocol_path: Path) -> tuple[str, datetime, datetime]:
    relative = protocol_path.resolve().relative_to(ROOT.resolve())
    commit = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(relative)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    committed_at = _parse_iso(
        subprocess.run(
            ["git", "show", "-s", "--format=%cI", commit],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    forward_start = datetime.combine(
        committed_at.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    )
    return commit, committed_at, forward_start


def _sample_status(start_ms: int, forward_oos_start: datetime) -> str:
    start = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
    if start >= forward_oos_start:
        return "FORWARD_OOS_FUTURE"
    month = start.strftime("%Y-%m")
    if month in DEVELOPMENT_MONTHS:
        return "RESEARCH_DEVELOPMENT"
    if month == EXPOSED_VALIDATION_MONTH:
        return "RESEARCH_VALIDATION_EXPOSED"
    return "DESCRIPTIVE_EXPOSED"


def _block_key(
    group: str,
    start_ms: int,
    end_ms: int,
    *,
    seed: int | None = None,
) -> str:
    key = f"{group}:{_iso(start_ms)}:{_iso(end_ms)}"
    return f"{key}:seed={seed}" if seed is not None else key


def _interval(item: Mapping[str, Any]) -> tuple[int, int]:
    return _timestamp_ms(str(item["market_close"])), _timestamp_ms(
        str(item["force_close_at"])
    )


def _intervals_overlap(
    left: tuple[int, int],
    right: tuple[int, int],
) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _hour_distance(left: int, right: int) -> int:
    difference = abs(left - right) % 24
    return min(difference, 24 - difference)


def _window_type(record: Mapping[str, Any], scheduler: Scheduler) -> str:
    start = _parse_iso(str(record["market_close"]))
    classified = scheduler.classify_window(start + timedelta(minutes=1))
    next_open = classified.next_market_open
    if next_open is None:
        return "UNKNOWN_CLOSURE"
    day_gap = (
        next_open.astimezone(v2_5_windows.NY).date()
        - start.astimezone(v2_5_windows.NY).date()
    ).days
    if classified.kind == WindowKind.WEEKEND:
        return "THREE_DAY_LONG_WEEKEND" if day_gap >= 4 else "REGULAR_WEEKEND"
    if classified.kind == WindowKind.HOLIDAY:
        return "MULTI_DAY_HOLIDAY" if day_gap > 2 else "SINGLE_DAY_HOLIDAY"
    return str(classified.kind.value)


def _normalize_window(
    record: Mapping[str, Any],
    *,
    forward_oos_start: datetime,
    scheduler: Scheduler,
    matched_calendar_key: str = "",
    window_type: str | None = None,
) -> dict[str, Any]:
    normalized = dict(record)
    start_ms, end_ms = _interval(record)
    group = str(record["group"])
    seed_raw = record.get("seed")
    seed = int(seed_raw) if seed_raw not in (None, "") else None
    normalized.update(
        {
            "asset_group": _asset_group(str(record["symbol"])),
            "matched_calendar_key": matched_calendar_key,
            "calendar_key": _block_key(group, start_ms, end_ms, seed=seed),
            "market_close": _iso(start_ms),
            "force_close_at": _iso(end_ms),
            "tradable_hours": float(record.get("tradable_rows") or 0) / 60.0,
            "duration_minutes": (end_ms - start_ms) / 60_000,
            "month": datetime.fromtimestamp(start_ms / 1000, tz=UTC).strftime(
                "%Y-%m"
            ),
            "split": _sample_status(start_ms, forward_oos_start),
            "window_type": window_type
            or (_window_type(record, scheduler) if group == "W" else group),
        }
    )
    return normalized


def _candidate_starts(target_start_ms: int, target_end_ms: int) -> list[int]:
    target_start = datetime.fromtimestamp(target_start_ms / 1000, tz=UTC)
    duration = timedelta(milliseconds=target_end_ms - target_start_ms)
    month_start = target_start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    candidates: list[int] = []
    cursor = month_start
    while cursor + duration <= next_month:
        if _hour_distance(cursor.hour, target_start.hour) <= 1:
            candidates.append(int(cursor.timestamp() * 1000))
        cursor += timedelta(hours=1)
    return candidates


def _candidate_is_available(
    start_ms: int,
    end_ms: int,
    *,
    symbols: Sequence[str],
    target_by_symbol: Mapping[str, Mapping[str, Any]],
    rows_by_symbol: Mapping[str, Sequence[NormalizedKline]],
    open_times_by_symbol: Mapping[str, Sequence[int]],
    onboard_by_symbol: Mapping[str, int],
) -> bool:
    expected_rows = (end_ms - start_ms) // 60_000
    if expected_rows <= OBSERVATION_ROWS + MIN_TRADABLE_ROWS:
        return False
    for symbol in symbols:
        target = target_by_symbol.get(symbol)
        if target is None:
            return False
        if (
            v2_5_windows._listing_stage(start_ms, onboard_by_symbol[symbol])
            != target["listing_stage"]
        ):
            return False
        rows = rows_by_symbol[symbol]
        row_start, row_end = v2_5_windows._slice_indices(
            open_times_by_symbol[symbol], start_ms, end_ms
        )
        if row_end - row_start != expected_rows:
            return False
        complete, _ = v2_5_windows._complete_slice(rows, row_start, row_end)
        if not complete:
            return False
    return True


def _select_candidate(
    candidates: Sequence[tuple[int, int]],
    *,
    reserved: Sequence[tuple[int, int]],
    same_seed_selected: Sequence[tuple[int, int]],
    rng: random.Random,
) -> tuple[int, int] | None:
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    for candidate in shuffled:
        if any(_intervals_overlap(candidate, item) for item in reserved):
            continue
        if any(_intervals_overlap(candidate, item) for item in same_seed_selected):
            continue
        return candidate
    return None


def _ready_block_map(
    windows: Iterable[Mapping[str, Any]],
    group: str,
) -> dict[str, dict[str, Mapping[str, Any]]]:
    result: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for item in windows:
        if item.get("group") != group or item.get("status") != "READY":
            continue
        result[str(item["calendar_key"])][str(item["symbol"])] = item
    return dict(result)


def _skipped_random_record(
    *,
    symbol: str,
    target: Mapping[str, Any] | None,
    target_key: str,
    seed: int,
    reason: str,
    target_start_ms: int,
    forward_oos_start: datetime,
) -> dict[str, Any]:
    month = datetime.fromtimestamp(target_start_ms / 1000, tz=UTC).strftime("%Y-%m")
    return {
        "window_id": f"{symbol}-R-{target_start_ms}-s{seed}",
        "symbol": symbol,
        "asset_group": _asset_group(symbol),
        "group": "R",
        "seed": seed,
        "matched_window_id": str((target or {}).get("window_id") or ""),
        "matched_calendar_key": target_key,
        "calendar_key": "",
        "market_close": "",
        "force_close_at": "",
        "observation_start": "",
        "observation_end": "",
        "tradable_start": "",
        "tradable_end": "",
        "row_start_index": "",
        "row_end_index": "",
        "row_count": 0,
        "observation_rows": 0,
        "tradable_rows": 0,
        "tradable_hours": 0.0,
        "duration_minutes": 0.0,
        "month": month,
        "split": _sample_status(target_start_ms, forward_oos_start),
        "listing_stage": str((target or {}).get("listing_stage") or ""),
        "window_type": "MATCHED_RANDOM",
        "status": "SKIPPED",
        "skip_reason": reason,
        "data_gap_count": 0,
    }


def _random_windows(
    *,
    symbols: Sequence[str],
    w_windows: Sequence[Mapping[str, Any]],
    o_windows: Sequence[Mapping[str, Any]],
    rows_by_symbol: Mapping[str, Sequence[NormalizedKline]],
    onboard_by_symbol: Mapping[str, int],
    forward_oos_start: datetime,
    scheduler: Scheduler,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    w_blocks = _ready_block_map(w_windows, "W")
    reserved = sorted(
        {
            _interval(item)
            for item in [*w_windows, *o_windows]
            if item.get("status") == "READY"
        }
    )
    rng_by_seed = {seed: random.Random(seed) for seed in SEED_VALUES}
    selected_by_seed: dict[int, list[tuple[int, int]]] = {
        seed: [] for seed in SEED_VALUES
    }
    open_times_by_symbol = {
        symbol: v2_5_windows._row_indices(rows_by_symbol[symbol])
        for symbol in symbols
    }
    result: list[dict[str, Any]] = []
    selection_audit: list[dict[str, Any]] = []

    block_items = sorted(
        w_blocks.items(), key=lambda item: min(_interval(row)[0] for row in item[1].values())
    )
    for target_key, target_by_symbol in block_items:
        representative = next(iter(target_by_symbol.values()))
        target_start_ms, target_end_ms = _interval(representative)
        duration_ms = target_end_ms - target_start_ms
        all_core_ready = set(target_by_symbol) == set(symbols)
        eligible: list[tuple[int, int]] = []
        time_candidate_count = 0
        nonoverlap_candidate_count = 0
        if all_core_ready:
            for start_ms in _candidate_starts(target_start_ms, target_end_ms):
                end_ms = start_ms + duration_ms
                time_candidate_count += 1
                if any(
                    _intervals_overlap((start_ms, end_ms), item)
                    for item in reserved
                ):
                    continue
                nonoverlap_candidate_count += 1
                if _candidate_is_available(
                    start_ms,
                    end_ms,
                    symbols=symbols,
                    target_by_symbol=target_by_symbol,
                    rows_by_symbol=rows_by_symbol,
                    open_times_by_symbol=open_times_by_symbol,
                    onboard_by_symbol=onboard_by_symbol,
                ):
                    eligible.append((start_ms, end_ms))

        for seed in SEED_VALUES:
            selected = (
                _select_candidate(
                    eligible,
                    reserved=reserved,
                    same_seed_selected=selected_by_seed[seed],
                    rng=rng_by_seed[seed],
                )
                if all_core_ready
                else None
            )
            reason = ""
            if not all_core_ready:
                reason = "W_BLOCK_NOT_ALL_CORE_READY"
            elif selected is None:
                reason = "NO_MATCHED_RANDOM_BLOCK"
            selection_audit.append(
                {
                    "matched_calendar_key": target_key,
                    "seed": seed,
                    "target_start": _iso(target_start_ms),
                    "target_end": _iso(target_end_ms),
                    "target_duration_minutes": duration_ms / 60_000,
                    "all_core_ready": all_core_ready,
                    "time_candidate_count": time_candidate_count,
                    "nonoverlap_candidate_count": nonoverlap_candidate_count,
                    "eligible_candidate_count": len(eligible),
                    "status": "READY" if selected else "SKIPPED",
                    "skip_reason": reason,
                    "selected_start": _iso(selected[0]) if selected else "",
                    "selected_end": _iso(selected[1]) if selected else "",
                    "calendar_key": (
                        _block_key("R", selected[0], selected[1], seed=seed)
                        if selected
                        else ""
                    ),
                }
            )
            if selected is None:
                for symbol in symbols:
                    result.append(
                        _skipped_random_record(
                            symbol=symbol,
                            target=target_by_symbol.get(symbol),
                            target_key=target_key,
                            seed=seed,
                            reason=reason,
                            target_start_ms=target_start_ms,
                            forward_oos_start=forward_oos_start,
                        )
                    )
                continue

            selected_by_seed[seed].append(selected)
            start_ms, end_ms = selected
            calendar_key = _block_key("R", start_ms, end_ms, seed=seed)
            for symbol in symbols:
                rows = rows_by_symbol[symbol]
                row_start, row_end = v2_5_windows._slice_indices(
                    open_times_by_symbol[symbol], start_ms, end_ms
                )
                record = v2_5_windows._base_window(
                    window_id=f"{symbol}-R-{start_ms}-s{seed}",
                    symbol=symbol,
                    group="R",
                    seed=seed,
                    matched_window_id=str(target_by_symbol[symbol]["window_id"]),
                    calendar_key=calendar_key,
                    start_ms=start_ms,
                    force_close_ms=end_ms,
                    rows=rows,
                    row_start=row_start,
                    row_end=row_end,
                    onboard_ms=onboard_by_symbol[symbol],
                )
                normalized = _normalize_window(
                    record,
                    forward_oos_start=forward_oos_start,
                    scheduler=scheduler,
                    matched_calendar_key=target_key,
                    window_type="MATCHED_RANDOM",
                )
                result.append(normalized)
    return result, selection_audit


def _unique_ready_blocks(
    windows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for item in windows:
        if item.get("status") != "READY" or not item.get("calendar_key"):
            continue
        grouped[(str(item["group"]), str(item["calendar_key"]))].append(item)
    result = []
    for (group, key), rows in sorted(grouped.items()):
        intervals = {_interval(item) for item in rows}
        if len(intervals) != 1:
            raise ValueError(f"Block {key} has inconsistent symbol intervals")
        start_ms, end_ms = next(iter(intervals))
        seeds = {str(item.get("seed") or "") for item in rows}
        if len(seeds) != 1:
            raise ValueError(f"Block {key} has inconsistent seeds")
        result.append(
            {
                "group": group,
                "calendar_key": key,
                "seed": next(iter(seeds)),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "symbol_count": len({str(item["symbol"]) for item in rows}),
                "symbols": sorted({str(item["symbol"]) for item in rows}),
                "split": str(rows[0]["split"]),
                "month": str(rows[0]["month"]),
            }
        )
    return result


def _overlap_audit(windows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    blocks = _unique_ready_blocks(windows)
    by_group = {
        group: [item for item in blocks if item["group"] == group]
        for group in ("W", "O", "R")
    }

    def cross(left_group: str, right_group: str) -> list[dict[str, str]]:
        found = []
        for left in by_group[left_group]:
            for right in by_group[right_group]:
                if _intervals_overlap(
                    (left["start_ms"], left["end_ms"]),
                    (right["start_ms"], right["end_ms"]),
                ):
                    found.append(
                        {
                            "left": str(left["calendar_key"]),
                            "right": str(right["calendar_key"]),
                        }
                    )
        return found

    same_seed_r: list[dict[str, str]] = []
    cross_seed_r: list[dict[str, str]] = []
    exact_cross_seed_reuse: list[dict[str, str]] = []
    random_blocks = by_group["R"]
    for index, left in enumerate(random_blocks):
        for right in random_blocks[index + 1 :]:
            left_interval = (left["start_ms"], left["end_ms"])
            right_interval = (right["start_ms"], right["end_ms"])
            if not _intervals_overlap(left_interval, right_interval):
                continue
            pair = {
                "left": str(left["calendar_key"]),
                "right": str(right["calendar_key"]),
            }
            if left["seed"] == right["seed"]:
                same_seed_r.append(pair)
            else:
                cross_seed_r.append(pair)
                if left_interval == right_interval:
                    exact_cross_seed_reuse.append(pair)

    w_o = cross("W", "O")
    w_r = cross("W", "R")
    o_r = cross("O", "R")
    passed = not (w_o or w_r or o_r or same_seed_r)
    return {
        "ready_symbol_window_rows": sum(
            1 for item in windows if item.get("status") == "READY"
        ),
        "ready_calendar_blocks": len(blocks),
        "calendar_blocks_by_group": {
            group: len(items) for group, items in by_group.items()
        },
        "same_interval_symbols_collapsed_to_one_block": True,
        "w_o_overlap_count": len(w_o),
        "w_r_overlap_count": len(w_r),
        "o_r_overlap_count": len(o_r),
        "same_seed_r_overlap_count": len(same_seed_r),
        "cross_seed_r_overlap_count": len(cross_seed_r),
        "cross_seed_exact_reuse_count": len(exact_cross_seed_reuse),
        "w_o_overlaps": w_o[:100],
        "w_r_overlaps": w_r[:100],
        "o_r_overlaps": o_r[:100],
        "same_seed_r_overlaps": same_seed_r[:100],
        "cross_seed_r_overlaps": cross_seed_r[:100],
        "passed": passed,
    }


def _count_audit(
    windows: Sequence[Mapping[str, Any]],
    *,
    o_count_before_random: int,
    o_count_after_random: int,
    random_selection_audit: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    blocks = _unique_ready_blocks(windows)
    rows_by_status: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in windows:
        rows_by_status[str(item["group"])][str(item["status"])] += 1
    block_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in blocks:
        block_counts[str(item["split"])][str(item["group"])] += 1
    development_r_by_seed = {
        str(seed): sum(
            1
            for item in blocks
            if item["group"] == "R"
            and item["split"] == "RESEARCH_DEVELOPMENT"
            and str(item["seed"]) == str(seed)
        )
        for seed in SEED_VALUES
    }
    random_candidate_space_by_seed = {
        str(seed): {
            "target_count": sum(
                1 for item in random_selection_audit if int(item["seed"]) == seed
            ),
            "time_candidate_count": sum(
                int(item.get("time_candidate_count") or 0)
                for item in random_selection_audit
                if int(item["seed"]) == seed
            ),
            "nonoverlap_candidate_count": sum(
                int(item.get("nonoverlap_candidate_count") or 0)
                for item in random_selection_audit
                if int(item["seed"]) == seed
            ),
            "eligible_candidate_count": sum(
                int(item.get("eligible_candidate_count") or 0)
                for item in random_selection_audit
                if int(item["seed"]) == seed
            ),
            "ready_selection_count": sum(
                1
                for item in random_selection_audit
                if int(item["seed"]) == seed and item.get("status") == "READY"
            ),
        }
        for seed in SEED_VALUES
    }
    return {
        "symbol_window_rows": {
            group: dict(sorted(statuses.items()))
            for group, statuses in sorted(rows_by_status.items())
        },
        "calendar_block_counts_by_split": {
            split: dict(sorted(groups.items()))
            for split, groups in sorted(block_counts.items())
        },
        "development_r_blocks_by_seed": development_r_by_seed,
        "random_candidate_space_by_seed": random_candidate_space_by_seed,
        "o_count_before_random": o_count_before_random,
        "o_count_after_random": o_count_after_random,
        "o_unchanged_by_random": o_count_before_random == o_count_after_random,
        "forward_oos_window_rows": sum(
            1 for item in windows if item.get("split") == "FORWARD_OOS_FUTURE"
        ),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _input_hash_audit(
    *,
    discovery_path: Path,
    data_manifest_path: Path,
    audit_path: Path,
    v2_5_window_path: Path,
    v2_5_results_path: Path,
    protocol_path: Path,
) -> dict[str, Any]:
    data_manifest = json.loads(data_manifest_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
    prior_results = json.loads(v2_5_results_path.read_text(encoding="utf-8"))
    files: list[dict[str, Any]] = []
    for symbol, item in sorted(data_manifest["assets"].items()):
        for kind, meta in sorted(item["files"].items()):
            path = Path(str(meta["path"]))
            actual_sha = _sha256(path) if path.exists() else ""
            actual_size = path.stat().st_size if path.exists() else None
            files.append(
                {
                    "symbol": symbol,
                    "kind": kind,
                    "path": str(path.resolve()),
                    "expected_sha256": str(meta["sha256"]),
                    "actual_sha256": actual_sha,
                    "sha256_match": actual_sha == str(meta["sha256"]),
                    "expected_size_bytes": int(meta["size_bytes"]),
                    "actual_size_bytes": actual_size,
                    "size_match": actual_size == int(meta["size_bytes"]),
                }
            )
    source_hashes = {
        "protocol": _sha256(protocol_path),
        "symbol_discovery": _sha256(discovery_path),
        "asset_data_manifest": _sha256(data_manifest_path),
        "asset_data_audit": _sha256(audit_path),
        "v2_5_window_manifest": _sha256(v2_5_window_path),
        "v2_5_results": _sha256(v2_5_results_path),
    }
    core = sorted(
        str(item["symbol"])
        for item in discovery.get("symbols") or []
        if item.get("tier") == "TIER_A_CORE"
    )
    audit_core = sorted(
        str(item["symbol"])
        for item in audit.get("assets") or []
        if item.get("status") == "PASS"
    )
    checks = {
        "all_40_data_file_hashes_match": len(files) == 40
        and all(item["sha256_match"] for item in files),
        "all_40_data_file_sizes_match": len(files) == 40
        and all(item["size_match"] for item in files),
        "tier_a_core_exactly_preregistered": tuple(core) == tuple(sorted(CORE_SYMBOLS)),
        "eight_core_audits_pass": bool(audit.get("passed"))
        and tuple(audit_core) == tuple(sorted(CORE_SYMBOLS)),
        "neutral_1x_frozen": data_manifest.get("direction_mode") == "NEUTRAL"
        and data_manifest.get("leverage") == 1,
        "production_defaults_unchanged": data_manifest.get(
            "production_defaults_changed"
        )
        is False,
        "prior_results_reference_current_discovery": prior_results.get(
            "discovery_sha256"
        )
        == source_hashes["symbol_discovery"],
        "prior_results_reference_current_asset_manifest": prior_results.get(
            "asset_manifest_sha256"
        )
        == source_hashes["asset_data_manifest"],
        "prior_results_reference_current_data_audit": prior_results.get(
            "data_audit_sha256"
        )
        == source_hashes["asset_data_audit"],
        "prior_results_reference_current_window_manifest": prior_results.get(
            "window_manifest_sha256"
        )
        == source_hashes["v2_5_window_manifest"],
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "protocol": str(protocol_path),
        "git_branch": git_branch(),
        "git_commit": git_commit(),
        "source_hashes": source_hashes,
        "data_files": files,
        "file_count": len(files),
        "total_size_bytes": sum(int(item["actual_size_bytes"] or 0) for item in files),
        "checks": checks,
        "passed": all(checks.values()),
    }


def _hash_audit_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# H1.1 input hash audit",
        "",
        f"- Files: `{payload['file_count']}`",
        f"- Bytes: `{payload['total_size_bytes']}`",
        f"- Passed: `{payload['passed']}`",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, value in payload["checks"].items():
        lines.append(f"| `{name}` | **{'PASS' if value else 'FAIL'}** |")
    return "\n".join(lines) + "\n"


def _overlap_markdown(payload: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# H1.1 W/O/R overlap audit",
            "",
            f"- W vs O overlap: `{payload['w_o_overlap_count']}`",
            f"- W vs R overlap: `{payload['w_r_overlap_count']}`",
            f"- O vs R overlap: `{payload['o_r_overlap_count']}`",
            f"- Same-seed R overlap: `{payload['same_seed_r_overlap_count']}`",
            f"- Cross-seed R overlap recorded: `{payload['cross_seed_r_overlap_count']}`",
            f"- Passed: `{payload['passed']}`",
            "",
            "Multiple symbols sharing one real interval are collapsed to one calendar block.",
            "",
        ]
    )


def _count_markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# H1.1 window count audit",
        "",
        f"- O before random: `{payload['o_count_before_random']}`",
        f"- O after random: `{payload['o_count_after_random']}`",
        f"- O unchanged: `{payload['o_unchanged_by_random']}`",
        f"- Development R blocks by seed: `{payload['development_r_blocks_by_seed']}`",
        f"- Random candidate space by seed: `{payload['random_candidate_space_by_seed']}`",
        f"- Forward OOS rows: `{payload['forward_oos_window_rows']}`",
        "",
    ]
    return "\n".join(lines)


def build(args: argparse.Namespace) -> dict[str, Any]:
    if git_branch() != "codex/profit-protection-backtest-v2.3":
        raise ValueError("H1.1 must run on codex/profit-protection-backtest-v2.3")

    discovery_path = Path(args.discovery)
    data_manifest_path = Path(args.data_manifest)
    audit_path = Path(args.audit_json)
    v2_5_window_path = Path(args.v2_5_window_manifest)
    v2_5_results_path = Path(args.v2_5_results)
    protocol_path = Path(args.protocol)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    protocol_commit, protocol_committed_at, forward_oos_start = _protocol_revision(
        protocol_path
    )
    input_audit = _input_hash_audit(
        discovery_path=discovery_path,
        data_manifest_path=data_manifest_path,
        audit_path=audit_path,
        v2_5_window_path=v2_5_window_path,
        v2_5_results_path=v2_5_results_path,
        protocol_path=protocol_path,
    )
    write_json(output_dir / "input-hash-manifest.json", input_audit)
    immutable_write(
        output_dir / "input-hash-audit.md", _hash_audit_markdown(input_audit)
    )
    if not input_audit["passed"]:
        raise ValueError("Frozen H1.1 inputs failed SHA-256 or audit validation")

    data_manifest = json.loads(data_manifest_path.read_text(encoding="utf-8"))
    assets = data_manifest.get("assets") or {}
    if tuple(sorted(assets)) != tuple(sorted(CORE_SYMBOLS)):
        raise ValueError("Frozen asset manifest does not exactly match Tier A-Core")

    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    rows_by_symbol: dict[str, list[NormalizedKline]] = {}
    onboard_by_symbol: dict[str, int] = {}
    w_windows: list[dict[str, Any]] = []
    o_windows: list[dict[str, Any]] = []

    for symbol in CORE_SYMBOLS:
        item = assets[symbol]
        rows = v2_5_windows._parse_kline_file(item["files"]["klines"]["path"])
        onboard_ms = int(_parse_iso(str(item["start_time"])).timestamp() * 1000)
        rows_by_symbol[symbol] = rows
        onboard_by_symbol[symbol] = onboard_ms

        w_ready, w_skipped = v2_5_windows._w_windows(
            rows,
            symbol=symbol,
            onboard_ms=onboard_ms,
            scheduler=scheduler,
        )
        for record in [*w_ready, *w_skipped]:
            w_windows.append(
                _normalize_window(
                    record,
                    forward_oos_start=forward_oos_start,
                    scheduler=scheduler,
                )
            )

        o_ready, o_skipped = v2_5_windows._o_windows(
            rows,
            symbol=symbol,
            onboard_ms=onboard_ms,
            scheduler=scheduler,
        )
        for record in [*o_ready, *o_skipped]:
            o_windows.append(
                _normalize_window(
                    record,
                    forward_oos_start=forward_oos_start,
                    scheduler=scheduler,
                    window_type="ORDINARY_WEEKDAY_OVERNIGHT",
                )
            )

    o_count_before_random = sum(
        1 for item in o_windows if item.get("status") == "READY"
    )
    r_windows, selection_audit = _random_windows(
        symbols=CORE_SYMBOLS,
        w_windows=w_windows,
        o_windows=o_windows,
        rows_by_symbol=rows_by_symbol,
        onboard_by_symbol=onboard_by_symbol,
        forward_oos_start=forward_oos_start,
        scheduler=scheduler,
    )
    o_count_after_random = sum(
        1 for item in o_windows if item.get("status") == "READY"
    )
    windows = [*w_windows, *o_windows, *r_windows]
    overlap_audit = _overlap_audit(windows)
    count_audit = _count_audit(
        windows,
        o_count_before_random=o_count_before_random,
        o_count_after_random=o_count_after_random,
        random_selection_audit=selection_audit,
    )
    payload = {
        "schema_version": 2,
        "protocol": str(protocol_path),
        "protocol_commit": protocol_commit,
        "protocol_committed_at": protocol_committed_at.isoformat(),
        "forward_oos_start": forward_oos_start.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "git_branch": git_branch(),
        "git_commit": git_commit(),
        "source_v2_5_manifest": str(v2_5_window_path.resolve()),
        "source_v2_5_manifest_sha256": _sha256(v2_5_window_path),
        "input_hash_manifest_sha256": _sha256(
            output_dir / "input-hash-manifest.json"
        ),
        "observation_rows": OBSERVATION_ROWS,
        "minimum_tradable_rows": MIN_TRADABLE_ROWS,
        "force_close_minutes": 120,
        "random_seeds": list(SEED_VALUES),
        "direction_mode": "NEUTRAL",
        "leverage": 1,
        "asset_groups": {
            "TRADITIONAL_EQUITY": list(TRADITIONAL_EQUITY),
            "CRYPTO_SENSITIVE_EQUITY": list(CRYPTO_SENSITIVE_EQUITY),
        },
        "sample_status": {
            "2026-03_to_2026-05": "RESEARCH_DEVELOPMENT",
            "2026-06": "RESEARCH_VALIDATION_EXPOSED",
            "other_existing": "DESCRIPTIVE_EXPOSED",
            "new_after_forward_start": "FORWARD_OOS_FUTURE",
        },
        "freeze_order": ["W", "O", "R"],
        "o_count_before_random": o_count_before_random,
        "o_count_after_random": o_count_after_random,
        "windows": windows,
        "random_selection_audit": selection_audit,
        "overlap_audit": overlap_audit,
        "count_audit": count_audit,
        "forward_oos_read": False,
        "production_defaults_changed": False,
        "master_modified": False,
    }
    write_json(output_dir / "window-manifest-h1-1.json", payload)
    write_csv(output_dir / "window-manifest-h1-1.csv", WINDOW_FIELDS, windows)
    immutable_write(
        output_dir / "window-overlap-audit-h1-1.md",
        _overlap_markdown(overlap_audit),
    )
    immutable_write(
        output_dir / "window-count-audit.md", _count_markdown(count_audit)
    )
    return payload


def main() -> None:
    args = _parser().parse_args()
    result = build(args)
    print(
        json.dumps(
            {
                "window_manifest": str(
                    (Path(args.output_dir) / "window-manifest-h1-1.json").resolve()
                ),
                "o_count_before_random": result["o_count_before_random"],
                "o_count_after_random": result["o_count_after_random"],
                "overlap_passed": result["overlap_audit"]["passed"],
                "development_r_blocks_by_seed": result["count_audit"][
                    "development_r_blocks_by_seed"
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result["overlap_audit"]["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
