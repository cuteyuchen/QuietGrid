"""Freeze W/O/R fair windows for the stock-perpetual market hypothesis."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.scheduler import Scheduler  # noqa: E402
from data_sources.models import NormalizedKline  # noqa: E402
from data_sources.window_slicer import NyseWindowSlicer  # noqa: E402
from strategy.window_models import WindowKind  # noqa: E402
from scripts.stock_perp_common import (  # noqa: E402
    INTERVAL_MS,
    SEED_VALUES,
    immutable_write,
    write_csv,
    write_json,
)


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
REPORT_DIR = Path("reports/stock-perp-weekend-grid-v1")
DATA_DIR = Path("data/backtests/stock-perp-weekend-grid-v1")
OBSERVATION_ROWS = 180
MIN_TRADABLE_ROWS = 30

WINDOW_FIELDS = (
    "window_id",
    "symbol",
    "group",
    "seed",
    "matched_window_id",
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
    "duration_minutes",
    "month",
    "split",
    "listing_stage",
    "status",
    "skip_reason",
    "data_gap_count",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="冻结股票永续 W/O/R 公平窗口")
    parser.add_argument("--data-manifest", default=str(REPORT_DIR / "asset-data-manifest.json"))
    parser.add_argument("--audit-json", default=str(REPORT_DIR / "asset-data-audit.json"))
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--output-dir", default=str(REPORT_DIR))
    parser.add_argument("--symbols", default="")
    return parser


def _parse_kline_file(path: str | Path) -> list[NormalizedKline]:
    rows: list[NormalizedKline] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            try:
                rows.append(
                    NormalizedKline(
                        open_time=int(row["open_time"]),
                        close_time=int(row["close_time"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        quote_volume=float(row["quote_volume"]),
                        trade_count=int(row["trade_count"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path} 第 {line_no} 行 K 线无效: {exc}") from exc
    rows.sort(key=lambda item: item.open_time)
    return rows


def _iso_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()


def _window_month(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=NY).strftime("%Y-%m")


def _listing_stage(timestamp_ms: int, onboard_ms: int) -> str:
    days = (timestamp_ms - onboard_ms) / 86_400_000
    if days < 14:
        return "LISTING_DAYS_1_14"
    if days < 30:
        return "LISTING_DAYS_15_30"
    return "LISTING_AFTER_30_DAYS"


def _complete_slice(rows: Sequence[NormalizedKline], start: int, end: int) -> tuple[bool, int]:
    if end <= start or end > len(rows):
        return False, max(0, end - start)
    gaps = 0
    for left, right in zip(rows[start : end - 1], rows[start + 1 : end]):
        if right.open_time - left.open_time != INTERVAL_MS or left.close_time >= right.open_time:
            gaps += 1
    return gaps == 0, gaps


def _row_indices(rows: Sequence[NormalizedKline]) -> list[int]:
    return [row.open_time for row in rows]


def _slice_indices(open_times: Sequence[int], start_ms: int, end_ms: int) -> tuple[int, int]:
    return bisect.bisect_left(open_times, start_ms), bisect.bisect_left(open_times, end_ms)


def _base_window(
    *,
    window_id: str,
    symbol: str,
    group: str,
    seed: int | None,
    matched_window_id: str | None,
    calendar_key: str,
    start_ms: int,
    force_close_ms: int,
    rows: Sequence[NormalizedKline],
    row_start: int,
    row_end: int,
    onboard_ms: int,
    status: str = "READY",
    skip_reason: str | None = None,
) -> dict[str, Any]:
    row_count = max(0, row_end - row_start)
    observation_end_index = min(row_end, row_start + OBSERVATION_ROWS) - 1
    tradable_start_index = min(row_end, row_start + OBSERVATION_ROWS)
    tradable_end_index = row_end - 1
    observation_end = rows[observation_end_index].close_time if observation_end_index >= row_start else start_ms
    tradable_start = rows[tradable_start_index].open_time if tradable_start_index < row_end else None
    tradable_end = rows[tradable_end_index].close_time if tradable_end_index >= row_start else None
    complete, gap_count = _complete_slice(rows, row_start, row_end)
    if status == "READY" and not complete:
        status = "DATA_INVALID"
        skip_reason = "WINDOW_DATA_GAP"
    tradable_rows = max(0, row_count - OBSERVATION_ROWS)
    return {
        "window_id": window_id,
        "symbol": symbol,
        "group": group,
        "seed": seed if seed is not None else "",
        "matched_window_id": matched_window_id or "",
        "calendar_key": calendar_key,
        "market_close": _iso_ms(start_ms),
        "force_close_at": _iso_ms(force_close_ms),
        "observation_start": _iso_ms(start_ms),
        "observation_end": _iso_ms(observation_end),
        "tradable_start": _iso_ms(tradable_start) if tradable_start is not None else "",
        "tradable_end": _iso_ms(tradable_end) if tradable_end is not None else "",
        "row_start_index": row_start,
        "row_end_index": row_end,
        "row_count": row_count,
        "observation_rows": min(OBSERVATION_ROWS, row_count),
        "tradable_rows": tradable_rows,
        "duration_minutes": round((force_close_ms - start_ms) / 60_000, 3),
        "month": _window_month(start_ms),
        "split": "UNASSIGNED",
        "listing_stage": _listing_stage(start_ms, onboard_ms),
        "status": status,
        "skip_reason": skip_reason or "",
        "data_gap_count": gap_count,
    }


def _scheduler_sessions(scheduler: Scheduler, start: datetime, end: datetime) -> list[tuple[Any, Any]]:
    # Scheduler owns the NYSE calendar.  The window builder only consumes its
    # schedule; it does not create a second holiday calendar.
    schedule = scheduler._calendar.schedule(
        start_date=start.astimezone(NY).date() - timedelta(days=14),
        end_date=end.astimezone(NY).date() + timedelta(days=14),
    )
    return list(schedule.iterrows())


def _w_windows(
    rows: Sequence[NormalizedKline],
    *,
    symbol: str,
    onboard_ms: int,
    scheduler: Scheduler,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    slicer = NyseWindowSlicer(
        force_close_minutes=scheduler.force_close_minutes,
        minimum_tradable_rows=MIN_TRADABLE_ROWS,
        calendar_name=scheduler.calendar_name,
    )
    sliced = slicer.slice(rows, observation_rows=OBSERVATION_ROWS)
    open_times = _row_indices(rows)
    ready: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in sliced:
        kind = scheduler.classify_window(item.market_close + timedelta(minutes=1)).kind
        if kind not in {WindowKind.WEEKEND, WindowKind.HOLIDAY}:
            continue
        start_ms = int(item.market_close.timestamp() * 1000)
        force_ms = int(item.force_close_at.timestamp() * 1000)
        row_start, row_end = _slice_indices(open_times, start_ms, force_ms)
        status = "READY" if item.status == "READY" else "SKIPPED"
        record = _base_window(
            window_id=f"{symbol}-{item.window_id}",
            symbol=symbol,
            group="W",
            seed=None,
            matched_window_id=None,
            calendar_key=scheduler.current_window_key(item.market_close, item.force_close_at),
            start_ms=start_ms,
            force_close_ms=force_ms,
            rows=rows,
            row_start=row_start,
            row_end=row_end,
            onboard_ms=onboard_ms,
            status=status,
            skip_reason=item.skip_reason,
        )
        (ready if record["status"] == "READY" else skipped).append(record)
    return ready, skipped


def _o_windows(
    rows: Sequence[NormalizedKline],
    *,
    symbol: str,
    onboard_ms: int,
    scheduler: Scheduler,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not rows:
        return [], []
    start = rows[0].open_datetime
    end = datetime.fromtimestamp(rows[-1].close_time / 1000, tz=UTC)
    sessions = _scheduler_sessions(scheduler, start, end)
    open_times = _row_indices(rows)
    ready: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index in range(len(sessions) - 1):
        current_label, current = sessions[index]
        following_label, following = sessions[index + 1]
        if (following_label.date() - current_label.date()).days > 1:
            continue
        market_close = current["market_close"].to_pydatetime().astimezone(UTC)
        # Ask Scheduler for the canonical force-close boundary.
        classified = scheduler.classify_window(market_close + timedelta(minutes=1))
        if classified.kind != WindowKind.WEEKDAY_OVERNIGHT or classified.force_close_at is None:
            continue
        start_ms = int(market_close.timestamp() * 1000)
        force_ms = int(classified.force_close_at.timestamp() * 1000)
        row_start, row_end = _slice_indices(open_times, start_ms, force_ms)
        record = _base_window(
            window_id=f"{symbol}-O-{market_close:%Y%m%dT%H%M%SZ}",
            symbol=symbol,
            group="O",
            seed=None,
            matched_window_id=None,
            calendar_key=scheduler.current_window_key(market_close, classified.force_close_at),
            start_ms=start_ms,
            force_close_ms=force_ms,
            rows=rows,
            row_start=row_start,
            row_end=row_end,
            onboard_ms=onboard_ms,
            status="READY" if row_end - row_start >= OBSERVATION_ROWS + MIN_TRADABLE_ROWS else "SKIPPED",
            skip_reason=None if row_end - row_start >= OBSERVATION_ROWS + MIN_TRADABLE_ROWS else "INSUFFICIENT_TRADABLE_ROWS",
        )
        (ready if record["status"] == "READY" else skipped).append(record)
    return ready, skipped


def _overlap(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    if a["symbol"] != b["symbol"]:
        return False
    a_start = int(datetime.fromisoformat(a["market_close"].replace("Z", "+00:00")).timestamp() * 1000)
    a_end = int(datetime.fromisoformat(a["force_close_at"].replace("Z", "+00:00")).timestamp() * 1000)
    b_start = int(datetime.fromisoformat(b["market_close"].replace("Z", "+00:00")).timestamp() * 1000)
    b_end = int(datetime.fromisoformat(b["force_close_at"].replace("Z", "+00:00")).timestamp() * 1000)
    return a_start < b_end and b_start < a_end


def _random_windows(
    rows: Sequence[NormalizedKline],
    w_windows: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    onboard_ms: int,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    open_times = _row_indices(rows)
    # O windows are filtered after R is frozen.  Reserving every ordinary
    # overnight here would make a multi-day R interval mathematically
    # impossible, because it necessarily crosses at least one weekday night.
    reserved = list(w_windows)
    result: list[dict[str, Any]] = []
    for w in w_windows:
        w_start = int(datetime.fromisoformat(w["market_close"].replace("Z", "+00:00")).timestamp() * 1000)
        w_end = int(datetime.fromisoformat(w["force_close_at"].replace("Z", "+00:00")).timestamp() * 1000)
        target_rows = int(w["row_count"])
        target_month = w["month"]
        target_hour = datetime.fromtimestamp(w_start / 1000, tz=UTC).hour
        candidates: list[tuple[int, int]] = []
        # Hourly candidates make the matching rule explicit and keep the
        # manifest deterministic while retaining enough choices per month.
        for index in range(0, len(rows) - target_rows, 60):
            candidate_start = rows[index].open_time
            candidate_end_index = index + target_rows
            candidate_end = rows[candidate_end_index - 1].close_time
            candidate_dt = datetime.fromtimestamp(candidate_start / 1000, tz=UTC)
            if candidate_dt.strftime("%Y-%m") != target_month:
                continue
            if abs(candidate_dt.hour - target_hour) > 1:
                continue
            if candidate_start < onboard_ms:
                continue
            if _listing_stage(candidate_start, onboard_ms) != str(w["listing_stage"]):
                continue
            complete, _ = _complete_slice(rows, index, candidate_end_index)
            if not complete:
                continue
            candidates.append((index, candidate_end_index))
        for seed in SEED_VALUES:
            rng = random.Random(seed)
            shuffled = list(candidates)
            rng.shuffle(shuffled)
            selected: tuple[int, int] | None = None
            for start_index, end_index in shuffled:
                candidate = {
                    "symbol": symbol,
                    "market_close": _iso_ms(rows[start_index].open_time),
                    "force_close_at": _iso_ms(rows[end_index - 1].close_time),
                }
                # The six fixed seeds are replicate matches for the same W
                # window.  They may share a candidate across different seeds;
                # within one seed we still prevent reusing the same rows.
                same_seed_random = [
                    existing
                    for existing in result
                    if existing.get("status") == "READY" and int(existing.get("seed") or -1) == seed
                ]
                if any(_overlap(candidate, existing) for existing in reserved + same_seed_random):
                    continue
                selected = (start_index, end_index)
                break
            if selected is None:
                result.append(
                    _base_window(
                        window_id=f"{symbol}-R-{w['window_id']}-s{seed}",
                        symbol=symbol,
                        group="R",
                        seed=seed,
                        matched_window_id=str(w["window_id"]),
                        calendar_key=f"R:{symbol}:{target_month}:{seed}",
                        start_ms=w_start,
                        force_close_ms=w_end,
                        rows=rows,
                        row_start=0,
                        row_end=0,
                        onboard_ms=onboard_ms,
                        status="SKIPPED",
                        skip_reason="NO_MATCHED_RANDOM_WINDOW",
                    )
                )
                continue
            start_index, end_index = selected
            record = _base_window(
                window_id=f"{symbol}-R-{w['window_id']}-s{seed}",
                symbol=symbol,
                group="R",
                seed=seed,
                matched_window_id=str(w["window_id"]),
                calendar_key=f"R:{symbol}:{target_month}:{seed}",
                start_ms=rows[start_index].open_time,
                force_close_ms=rows[end_index - 1].close_time + 1,
                rows=rows,
                row_start=start_index,
                row_end=end_index,
                onboard_ms=onboard_ms,
            )
            result.append(record)
    return result


def _exclude_o_overlaps(
    o_windows: Sequence[Mapping[str, Any]],
    r_windows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in o_windows:
        record = dict(item)
        if any(_overlap(record, random_window) for random_window in r_windows if random_window.get("status") == "READY"):
            record["status"] = "SKIPPED"
            record["skip_reason"] = "OVERLAPS_FROZEN_RANDOM_WINDOW"
            filtered.append(record)
        else:
            filtered.append(record)
    return filtered


def _assign_splits(windows: list[dict[str, Any]], *, complete_months: Sequence[str]) -> None:
    months = sorted(set(complete_months))
    if len(months) >= 5:
        research = set(months[:3])
        validation = {months[-2]}
        short_oos = {months[-1]}
    elif len(months) == 4:
        research = set(months[:3])
        validation = set()
        short_oos = {months[-1]}
    else:
        research = validation = short_oos = set()
    for item in windows:
        month = item["month"]
        if month in research:
            item["split"] = "RESEARCH_DEVELOPMENT"
        elif month in validation:
            item["split"] = "VALIDATION"
        elif month in short_oos:
            item["split"] = "SEALED_SHORT_OOS"
        else:
            item["split"] = "DESCRIPTIVE_ONLY"


def _overlap_audit(windows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ready = [item for item in windows if item.get("status") == "READY"]
    overlaps: list[tuple[str, str]] = []
    replicate_overlaps: list[tuple[str, str]] = []
    for index, left in enumerate(ready):
        for right in ready[index + 1 :]:
            if _overlap(left, right):
                if left.get("group") == right.get("group") == "R":
                    # Different fixed seeds are replicate draws.  Their
                    # overlap is recorded, but it is not reuse of a W/O
                    # observation and therefore does not invalidate the
                    # paired design.
                    if left.get("seed") != right.get("seed"):
                        replicate_overlaps.append((str(left["window_id"]), str(right["window_id"])))
                    else:
                        overlaps.append((str(left["window_id"]), str(right["window_id"])))
                else:
                    overlaps.append((str(left["window_id"]), str(right["window_id"])))
    return {
        "ready_window_count": len(ready),
        "skipped_window_count": len(windows) - len(ready),
        "overlap_count": len(overlaps),
        "overlaps": overlaps[:100],
        "replicate_overlap_count": len(replicate_overlaps),
        "replicate_overlaps": replicate_overlaps[:100],
        "passed": not overlaps,
    }


def _audit_markdown(payload: Mapping[str, Any]) -> str:
    audit = payload["overlap_audit"]
    lines = [
        "# W/O/R 窗口重叠审计",
        "",
        f"- observation_rows：`{payload['observation_rows']}`",
        f"- force_close_minutes：`{payload['force_close_minutes']}`",
        f"- 随机种子：`{', '.join(map(str, payload['random_seeds']))}`",
        f"- W/O/R 总窗口：`{len(payload['windows'])}`",
        f"- READY：`{audit['ready_window_count']}`；SKIPPED：`{audit['skipped_window_count']}`",
        f"- 重叠：`{audit['overlap_count']}`；通过：`{audit['passed']}`",
        f"- 不同随机种子之间的复用记录：`{audit.get('replicate_overlap_count', 0)}`（不与 W/O 共用）",
        "",
        "W 由 NyseWindowSlicer + Scheduler 的 NYSE 会话边界生成；O 使用同一 Scheduler 的普通工作日隔夜边界；R 在同标的/月份/阶段/持续时间/UTC 小时约束下固定抽样。",
        "",
    ]
    if audit["overlaps"]:
        lines.append("## 重叠明细")
        lines.append("")
        for left, right in audit["overlaps"]:
            lines.append(f"- `{left}` 与 `{right}`")
    return "\n".join(lines).rstrip() + "\n"


def build(args: argparse.Namespace) -> dict[str, Any]:
    manifest_path = Path(args.data_manifest)
    data_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit = json.loads(Path(args.audit_json).read_text(encoding="utf-8"))
    audit_by_symbol = {item["symbol"]: item for item in audit["assets"]}
    wanted = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
    assets = [
        item for symbol, item in data_manifest["assets"].items()
        if not wanted or symbol in wanted
    ]
    if not assets:
        raise ValueError("没有匹配的冻结资产。")
    scheduler = Scheduler(force_close_minutes=120, minimum_trade_minutes=120)
    all_windows: list[dict[str, Any]] = []
    split_summary: dict[str, Any] = {}
    for item in sorted(assets, key=lambda value: str(value["symbol"])):
        symbol = str(item["symbol"])
        audit_item = audit_by_symbol.get(symbol)
        if not audit_item or audit_item["status"] != "PASS":
            raise ValueError(f"{symbol} 数据审计未通过，不能冻结窗口。")
        rows = _parse_kline_file(item["files"]["klines"]["path"])
        onboard_ms = int(datetime.fromisoformat(str(item["start_time"]).replace("Z", "+00:00")).timestamp() * 1000)
        w_ready, w_skipped = _w_windows(rows, symbol=symbol, onboard_ms=onboard_ms, scheduler=scheduler)
        o_ready, o_skipped = _o_windows(rows, symbol=symbol, onboard_ms=onboard_ms, scheduler=scheduler)
        r_windows = _random_windows(rows, w_ready, symbol=symbol, onboard_ms=onboard_ms)
        o_filtered = _exclude_o_overlaps(o_ready, r_windows)
        o_filtered_ready = [item for item in o_filtered if item["status"] == "READY"]
        o_filtered_skipped = [item for item in o_filtered if item["status"] != "READY"]
        windows = w_ready + w_skipped + o_filtered_ready + o_filtered_skipped + r_windows
        # Complete months are computed from the actual full-month data span,
        # not from a manually typed month list.
        first_date = rows[0].open_datetime.astimezone(NY).date()
        last_date = rows[-1].open_datetime.astimezone(NY).date()
        months = sorted({item["month"] for item in windows if item["status"] == "READY"})
        if first_date.day != 1 and months:
            months = [value for value in months if value != first_date.strftime("%Y-%m")]
        if (last_date + timedelta(days=1)).month == last_date.month and months:
            months = [value for value in months if value != last_date.strftime("%Y-%m")]
        _assign_splits(windows, complete_months=months)
        split_summary[symbol] = {
            "complete_months": months,
            "research_development": sorted({w["month"] for w in windows if w["split"] == "RESEARCH_DEVELOPMENT"}),
            "validation": sorted({w["month"] for w in windows if w["split"] == "VALIDATION"}),
            "sealed_short_oos": sorted({w["month"] for w in windows if w["split"] == "SEALED_SHORT_OOS"}),
            "window_counts": {
                group: sum(1 for w in windows if w["group"] == group and w["status"] == "READY")
                for group in ("W", "O", "R")
            },
        }
        all_windows.extend(windows)

    overlap_audit = _overlap_audit(all_windows)
    result = {
        "schema_version": 1,
        "protocol": "docs/codex-stock-perp-weekend-grid-backtest-v2.5.md",
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "audit_path": str(Path(args.audit_json).resolve()),
        "observation_rows": OBSERVATION_ROWS,
        "minimum_tradable_rows": MIN_TRADABLE_ROWS,
        "force_close_minutes": 120,
        "random_seeds": list(SEED_VALUES),
        "calendar": "core.scheduler.Scheduler / NYSE",
        "window_groups": ["W", "O", "R"],
        "split_summary": split_summary,
        "windows": all_windows,
        "overlap_audit": overlap_audit,
        "production_defaults_changed": False,
        "short_oos_opened": False,
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "window-manifest.json", result)
    write_csv(output_dir / "window-manifest.csv", WINDOW_FIELDS, all_windows)
    immutable_write(output_dir / "window-overlap-audit.md", _audit_markdown(result))
    return result


def main() -> None:
    args = _parser().parse_args()
    result = build(args)
    print(
        json.dumps(
            {
                "window_manifest": str((Path(args.output_dir) / "window-manifest.json").resolve()),
                "window_count": len(result["windows"]),
                "ready": result["overlap_audit"]["ready_window_count"],
                "overlap_passed": result["overlap_audit"]["passed"],
                "splits": result["split_summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result["overlap_audit"]["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
