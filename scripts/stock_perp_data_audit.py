"""Audit the immutable stock-perpetual freeze before window construction."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stock_perp_common import (  # noqa: E402
    INTERVAL_MS,
    immutable_write,
    iso_ms,
    write_json,
)


UTC = timezone.utc
REPORT_DIR = Path("reports/stock-perp-weekend-grid-v1")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="审计冻结的股票永续数据")
    parser.add_argument("--manifest", default=str(REPORT_DIR / "asset-data-manifest.json"))
    parser.add_argument("--output", default=str(REPORT_DIR / "asset-data-audit.md"))
    parser.add_argument("--audit-json", default=str(REPORT_DIR / "asset-data-audit.json"))
    return parser


def _finite_positive(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _finite_nonnegative(value: Any) -> bool:
    try:
        return math.isfinite(float(value)) and float(value) >= 0
    except (TypeError, ValueError):
        return False


def audit_klines(
    path: str | Path,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, Any]:
    """Validate 1m K-lines without filling gaps."""
    result: dict[str, Any] = {
        "path": str(Path(path).resolve()),
        "row_count": 0,
        "duplicate_timestamps": 0,
        "non_increasing_timestamps": 0,
        "interval_gaps": 0,
        "missing_minutes": 0,
        "invalid_rows": 0,
        "future_rows": 0,
        "unclosed_rows": 0,
        "first_open_time": None,
        "last_open_time": None,
        "last_close_time": None,
        "zero_trade_rows": 0,
        "total_trade_count": 0,
        "total_volume": 0.0,
        "max_gap_minutes": 0,
        "errors": [],
    }
    previous: int | None = None
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    try:
        handle = Path(path).open("r", encoding="utf-8", newline="")
    except OSError as exc:
        result["errors"].append(str(exc))
        result["status"] = "DATA_INVALID"
        return result
    with handle:
        reader = csv.DictReader(handle)
        for line_no, row in enumerate(reader, start=2):
            result["row_count"] += 1
            try:
                open_time = int(row["open_time"])
                close_time = int(row["close_time"])
                open_price = float(row["open"])
                high = float(row["high"])
                low = float(row["low"])
                close = float(row["close"])
                volume = float(row["volume"])
                quote_volume = float(row["quote_volume"])
                trade_count = int(row["trade_count"])
            except (KeyError, TypeError, ValueError) as exc:
                result["invalid_rows"] += 1
                result["errors"].append(f"line {line_no}: {exc}")
                continue
            if result["first_open_time"] is None:
                result["first_open_time"] = open_time
            result["last_open_time"] = open_time
            result["last_close_time"] = close_time
            if previous is not None:
                if open_time == previous:
                    result["duplicate_timestamps"] += 1
                if open_time <= previous:
                    result["non_increasing_timestamps"] += 1
                elif open_time - previous != INTERVAL_MS:
                    gap = (open_time - previous) // INTERVAL_MS - 1
                    result["interval_gaps"] += 1
                    result["missing_minutes"] += max(0, gap)
                    result["max_gap_minutes"] = max(result["max_gap_minutes"], gap)
            previous = open_time
            if start_ms is not None and open_time < start_ms:
                result["errors"].append(f"line {line_no}: open_time before start")
            if end_ms is not None and open_time >= end_ms:
                result["errors"].append(f"line {line_no}: open_time beyond end")
            if open_time >= now_ms or close_time >= now_ms:
                result["future_rows"] += 1
            if close_time >= now_ms:
                result["unclosed_rows"] += 1
            if not all(
                (
                    math.isfinite(open_price),
                    math.isfinite(high),
                    math.isfinite(low),
                    math.isfinite(close),
                )
            ) or min(open_price, high, low, close) <= 0:
                result["invalid_rows"] += 1
            if high < max(open_price, close) or low > min(open_price, close) or high < low:
                result["invalid_rows"] += 1
            if not _finite_nonnegative(volume) or not _finite_nonnegative(quote_volume) or trade_count < 0:
                result["invalid_rows"] += 1
            if trade_count == 0 or volume == 0:
                result["zero_trade_rows"] += 1
            result["total_trade_count"] += trade_count
            result["total_volume"] += volume
    result["zero_trade_ratio"] = (
        result["zero_trade_rows"] / result["row_count"] if result["row_count"] else None
    )
    result["status"] = (
        "PASS"
        if not any(
            result[key]
            for key in (
                "duplicate_timestamps",
                "non_increasing_timestamps",
                "invalid_rows",
                "future_rows",
                "unclosed_rows",
            )
        )
        else "DATA_INVALID"
    )
    return result


def audit_funding(path: str | Path, *, start_ms: int, end_ms: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(Path(path).resolve()),
        "event_count": 0,
        "duplicate_times": 0,
        "non_increasing_times": 0,
        "out_of_range": 0,
        "invalid_rates": 0,
        "first_funding_time": None,
        "last_funding_time": None,
        "interval_hours": None,
        "errors": [],
    }
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        events = list(payload.get("events") or [])
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result["errors"].append(str(exc))
        result["status"] = "DATA_INVALID"
        return result
    previous: int | None = None
    intervals: list[float] = []
    for index, event in enumerate(events, start=1):
        result["event_count"] += 1
        try:
            timestamp = int(event["funding_time"])
            rate = float(event["funding_rate"])
        except (KeyError, TypeError, ValueError) as exc:
            result["invalid_rates"] += 1
            result["errors"].append(f"event {index}: {exc}")
            continue
        if result["first_funding_time"] is None:
            result["first_funding_time"] = timestamp
        result["last_funding_time"] = timestamp
        if not math.isfinite(rate):
            result["invalid_rates"] += 1
        if not start_ms <= timestamp < end_ms:
            result["out_of_range"] += 1
        if previous is not None:
            if timestamp == previous:
                result["duplicate_times"] += 1
            if timestamp <= previous:
                result["non_increasing_times"] += 1
            else:
                intervals.append((timestamp - previous) / 3_600_000)
        previous = timestamp
    if intervals:
        intervals.sort()
        middle = len(intervals) // 2
        result["interval_hours"] = round(
            intervals[middle]
            if len(intervals) % 2
            else (intervals[middle - 1] + intervals[middle]) / 2,
            3,
        )
    result["status"] = (
        "PASS"
        if not any(
            result[key]
            for key in (
                "duplicate_times",
                "non_increasing_times",
                "out_of_range",
                "invalid_rates",
            )
        )
        else "DATA_INVALID"
    )
    return result


def audit_sidecar(
    path: str | Path,
    *,
    start_ms: int,
    end_ms: int,
    require_positive_prices: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(Path(path).resolve()),
        "row_count": 0,
        "duplicate_timestamps": 0,
        "non_increasing_timestamps": 0,
        "out_of_range": 0,
        "invalid_rows": 0,
        "first_open_time": None,
        "last_open_time": None,
        "errors": [],
    }
    previous: int | None = None
    try:
        handle = Path(path).open("r", encoding="utf-8", newline="")
    except OSError as exc:
        result["errors"].append(str(exc))
        result["status"] = "DATA_INVALID"
        return result
    with handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            result["row_count"] += 1
            try:
                timestamp = int(row["open_time"])
                close_time = int(row.get("close_time") or timestamp + INTERVAL_MS - 1)
                values = [float(row[name]) for name in ("open", "high", "low", "close")]
            except (KeyError, TypeError, ValueError) as exc:
                result["invalid_rows"] += 1
                result["errors"].append(f"line {line_no}: {exc}")
                continue
            result["first_open_time"] = timestamp if result["first_open_time"] is None else result["first_open_time"]
            result["last_open_time"] = timestamp
            if previous is not None:
                if timestamp == previous:
                    result["duplicate_timestamps"] += 1
                if timestamp <= previous:
                    result["non_increasing_timestamps"] += 1
            previous = timestamp
            if not start_ms <= timestamp < end_ms or close_time >= end_ms:
                result["out_of_range"] += 1
            if not all(
                math.isfinite(value) and (value > 0 if require_positive_prices else True)
                for value in values
            ):
                result["invalid_rows"] += 1
    result["status"] = (
        "PASS"
        if not any(
            result[key]
            for key in (
                "duplicate_timestamps",
                "non_increasing_timestamps",
                "out_of_range",
                "invalid_rows",
            )
        )
        else "DATA_INVALID"
    )
    return result


def audit_agg_trades(path: str | Path, *, start_ms: int, end_ms: int) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(Path(path).resolve()),
        "row_count": 0,
        "duplicate_ids": 0,
        "non_increasing_ids": 0,
        "out_of_range": 0,
        "invalid_rows": 0,
        "first_transact_time": None,
        "last_transact_time": None,
        "errors": [],
    }
    previous_id: int | None = None
    seen: set[int] = set()
    try:
        handle = Path(path).open("r", encoding="utf-8", newline="")
    except OSError as exc:
        result["errors"].append(str(exc))
        result["status"] = "DATA_INVALID"
        return result
    with handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            result["row_count"] += 1
            try:
                trade_id = int(row["agg_trade_id"])
                timestamp = int(row["transact_time"])
                price = float(row["price"])
                quantity = float(row["quantity"])
            except (KeyError, TypeError, ValueError) as exc:
                result["invalid_rows"] += 1
                result["errors"].append(f"line {line_no}: {exc}")
                continue
            result["first_transact_time"] = timestamp if result["first_transact_time"] is None else result["first_transact_time"]
            result["last_transact_time"] = timestamp
            if trade_id in seen:
                result["duplicate_ids"] += 1
            if previous_id is not None and trade_id <= previous_id:
                result["non_increasing_ids"] += 1
            seen.add(trade_id)
            previous_id = trade_id
            if not start_ms <= timestamp < end_ms:
                result["out_of_range"] += 1
            if not _finite_positive(price) or not _finite_nonnegative(quantity):
                result["invalid_rows"] += 1
    result["status"] = (
        "PASS"
        if not any(
            result[key]
            for key in ("duplicate_ids", "non_increasing_ids", "out_of_range", "invalid_rows")
        )
        else "DATA_INVALID"
    )
    return result


def audit_asset(item: Mapping[str, Any]) -> dict[str, Any]:
    start_ms = int(datetime.fromisoformat(str(item["start_time"]).replace("Z", "+00:00")).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(str(item["end_time_exclusive"]).replace("Z", "+00:00")).timestamp() * 1000)
    files = item["files"]
    result = {
        "symbol": item["symbol"],
        "tier": item.get("tier"),
        "start_time": item["start_time"],
        "end_time_exclusive": item["end_time_exclusive"],
        "klines": audit_klines(files["klines"]["path"], start_ms=start_ms, end_ms=end_ms),
        "funding": audit_funding(files["funding"]["path"], start_ms=start_ms, end_ms=end_ms),
        "mark_price": audit_sidecar(files["mark_price"]["path"], start_ms=start_ms, end_ms=end_ms),
        "premium_index": audit_sidecar(
            files["premium_index"]["path"],
            start_ms=start_ms,
            end_ms=end_ms,
            require_positive_prices=False,
        ),
    }
    agg = files.get("agg_trades") or {}
    result["agg_trades"] = (
        audit_agg_trades(agg["path"], start_ms=start_ms, end_ms=end_ms)
        if agg.get("path")
        else {"status": "NOT_DOWNLOADED"}
    )
    result["status"] = (
        "PASS"
        if all(
            value.get("status") in {"PASS", "NOT_DOWNLOADED"}
            for key, value in result.items()
            if isinstance(value, dict) and key not in {"files"}
        )
        else "DATA_INVALID"
    )
    return result


def _markdown(payload: Mapping[str, Any]) -> str:
    lines = [
        "# Stock Perpetual 数据质量审计",
        "",
        f"- Manifest：`{payload['manifest_path']}`",
        f"- 审计时间：`{payload['generated_at']}`",
        "- 缺口处理：观察期/交易期缺口标记为不可交易，未做线性插值。",
        "",
        "| Symbol | Tier | Status | 1m rows | 1m gaps | Missing minutes | Future/unclosed | Funding | Mark | Premium | AggTrades |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | --- |",
    ]
    for item in payload["assets"]:
        k = item["klines"]
        lines.append(
            f"| `{item['symbol']}` | {item['tier']} | **{item['status']}** | "
            f"{k.get('row_count', 0)} | {k.get('interval_gaps', 0)} | "
            f"{k.get('missing_minutes', 0)} | {k.get('future_rows', 0)}/{k.get('unclosed_rows', 0)} | "
            f"{item['funding'].get('status')} | {item['mark_price'].get('status')} | "
            f"{item['premium_index'].get('status')} | {item['agg_trades'].get('status')} |"
        )
    lines.extend(
        [
            "",
            "Funding 只在真实结算时间进入后续回测；本审计不把费率摊入 K 线。",
            "所有结果均保留 `data_previously_viewed` 语义，不将本轮数据宣称为未查看 Final OOS。",
            "",
        ]
    )
    return "\n".join(lines)


def run_audit(manifest_path: str | Path, output_path: str | Path, audit_json: str | Path) -> dict[str, Any]:
    path = Path(manifest_path)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assets = [audit_asset(item) for item in manifest["assets"].values()]
    result = {
        "schema_version": 1,
        "protocol": manifest["protocol"],
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest_path": str(path.resolve()),
        "manifest_sha256": __import__("hashlib").sha256(path.read_bytes()).hexdigest(),
        "data_previously_viewed": manifest.get("data_previously_viewed", True),
        "assets": assets,
        "passed": all(item["status"] == "PASS" for item in assets),
    }
    write_json(Path(audit_json), result)
    immutable_write(Path(output_path), _markdown(result))
    return result


def main() -> None:
    args = _parser().parse_args()
    audit_json_path = Path(args.audit_json)
    output_path = Path(args.output)
    if audit_json_path.exists() and not output_path.exists():
        # A previous scan may have completed before a report publish conflict;
        # render that immutable scan rather than traversing multi-GB files again.
        result = json.loads(audit_json_path.read_text(encoding="utf-8"))
        immutable_write(output_path, _markdown(result))
    else:
        result = run_audit(args.manifest, args.output, args.audit_json)
    print(json.dumps({"audit": str(Path(args.output).resolve()), "passed": result["passed"]}, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
