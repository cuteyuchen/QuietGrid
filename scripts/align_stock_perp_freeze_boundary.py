"""Re-freeze an already checksum-verified dataset at complete UTC-day boundaries.

This is a deterministic local transformation: it never downloads or invents
market rows.  It keeps the prior archive/source metadata and records new local
SHA-256 values for the boundary-aligned files.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.freeze_stock_perp_data import _symbol_start_end  # noqa: E402
from scripts.stock_perp_common import (  # noqa: E402
    git_branch,
    git_commit,
    write_json,
)


UTC = timezone.utc
REPORT_DIR = Path("reports/stock-perp-weekend-grid-v1")
DATA_DIR = Path("data/backtests/stock-perp-weekend-grid-v1")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按完整 UTC 日边界重冻结股票永续数据")
    parser.add_argument("--source-manifest", default=str(REPORT_DIR / "asset-data-manifest.json"))
    parser.add_argument("--source-data-dir", default=str(DATA_DIR))
    parser.add_argument("--output-manifest", default=str(REPORT_DIR / "asset-data-manifest.json"))
    parser.add_argument("--output-data-dir", default=str(DATA_DIR))
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_filtered_csv(
    source: Path,
    target: Path,
    *,
    timestamp_field: str,
    start_ms: int,
    end_ms: int,
    close_field: str | None = None,
) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    first: int | None = None
    last: int | None = None
    with source.open("r", encoding="utf-8", newline="") as input_handle:
        reader = csv.DictReader(input_handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV 缺少表头: {source}")
        with target.open("w", encoding="utf-8", newline="") as output_handle:
            writer = csv.DictWriter(
                output_handle,
                fieldnames=reader.fieldnames,
                lineterminator="\n",
            )
            writer.writeheader()
            for row in reader:
                timestamp = int(row[timestamp_field])
                if timestamp < start_ms or timestamp >= end_ms:
                    continue
                if close_field is not None and int(row[close_field]) >= end_ms:
                    continue
                writer.writerow(row)
                count += 1
                first = timestamp if first is None else first
                last = timestamp
    return {
        "path": str(target.resolve()),
        "sha256": _sha256(target),
        "size_bytes": target.stat().st_size,
        "row_count": count,
        **({"first_open_time": first, "last_open_time": last} if timestamp_field == "open_time" else {}),
    }


def _copy_funding(
    source: Path,
    target: Path,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    events = [
        event
        for event in payload.get("events") or []
        if start_ms <= int(event["funding_time"]) < end_ms
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {**payload, "events": events},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "path": str(target.resolve()),
        "sha256": _sha256(target),
        "size_bytes": target.stat().st_size,
        "row_count": len(events),
        "first_funding_time": events[0].get("funding_time") if events else None,
        "last_funding_time": events[-1].get("funding_time") if events else None,
    }


def _copy_asset(
    old_manifest_item: Mapping[str, Any],
    *,
    source_data_dir: Path,
    output_data_dir: Path,
    global_end_ms: int,
) -> dict[str, Any]:
    item = deepcopy(dict(old_manifest_item))
    klines_meta = (old_manifest_item.get("files") or {}).get("klines") or {}
    boundary_row = {
        "onboard_date": old_manifest_item.get("start_time"),
        "first_valid_1m": {"open_time": klines_meta.get("first_open_time")},
    }
    start_ms, end_ms = _symbol_start_end(boundary_row, global_end_ms)
    symbol = str(item["symbol"])
    item["start_time"] = datetime.fromtimestamp(start_ms / 1000, tz=UTC).isoformat()
    item["end_time_exclusive"] = datetime.fromtimestamp(end_ms / 1000, tz=UTC).isoformat()
    item["derived_from_prior_freeze"] = True
    item["boundary_alignment"] = "FIRST_COMPLETE_UTC_DAY"
    item.pop("reused_existing_files", None)
    item["files"] = {}

    source_files = old_manifest_item.get("files") or {}
    for kind, meta in sorted(source_files.items()):
        if not isinstance(meta, Mapping) or not meta.get("path"):
            item["files"][kind] = dict(meta) if isinstance(meta, Mapping) else meta
            continue
        source = Path(str(meta["path"]))
        if not source.exists():
            source = source_data_dir / source.name
        if not source.exists():
            raise FileNotFoundError(f"冻结文件不存在: {source}")
        target = output_data_dir / source.name
        if kind == "klines":
            new_meta = _copy_filtered_csv(
                source,
                target,
                timestamp_field="open_time",
                close_field="close_time",
                start_ms=start_ms,
                end_ms=end_ms,
            )
        elif kind in {"mark_price", "premium_index"}:
            new_meta = _copy_filtered_csv(
                source,
                target,
                timestamp_field="open_time",
                close_field="close_time",
                start_ms=start_ms,
                end_ms=end_ms,
            )
        elif kind == "agg_trades":
            new_meta = _copy_filtered_csv(
                source,
                target,
                timestamp_field="transact_time",
                start_ms=start_ms,
                end_ms=end_ms,
            )
        elif kind == "funding":
            new_meta = _copy_funding(source, target, start_ms=start_ms, end_ms=end_ms)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            new_meta = {
                "path": str(target.resolve()),
                "sha256": _sha256(target),
                "size_bytes": target.stat().st_size,
                "row_count": meta.get("row_count"),
            }
        item["files"][kind] = new_meta
    return item


def align(args: argparse.Namespace) -> dict[str, Any]:
    source_manifest_path = Path(args.source_manifest)
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    source_data_dir = Path(args.source_data_dir)
    output_data_dir = Path(args.output_data_dir)
    output_data_dir.mkdir(parents=True, exist_ok=True)
    asof = datetime.fromisoformat(str(source_manifest["as_of_utc"]).replace("Z", "+00:00")).astimezone(UTC)
    global_end_ms = int(datetime.fromisoformat(str(source_manifest["assets"][next(iter(source_manifest["assets"]))]["end_time_exclusive"]).replace("Z", "+00:00")).timestamp() * 1000)
    assets = {
        symbol: _copy_asset(
            item,
            source_data_dir=source_data_dir,
            output_data_dir=output_data_dir,
            global_end_ms=global_end_ms,
        )
        for symbol, item in sorted((source_manifest.get("assets") or {}).items())
    }
    result = deepcopy(source_manifest)
    result.update(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "as_of_utc": asof.isoformat(),
            "git_branch": git_branch(),
            "git_commit": git_commit(),
            "source_manifest_sha256": _sha256(source_manifest_path),
            "boundary_alignment": "FIRST_COMPLETE_UTC_DAY",
            "assets": assets,
            "notes": list(source_manifest.get("notes") or [])
            + [
                "由此前已完成官方 checksum 校验的本地文件按首个完整 UTC 日确定性裁剪；未插值、未新增市场数据。",
            ],
        }
    )
    write_json(Path(args.output_manifest), result)
    return result


def main() -> None:
    args = _parser().parse_args()
    result = align(args)
    print(
        json.dumps(
            {
                "manifest": str(Path(args.output_manifest).resolve()),
                "assets": sorted(result["assets"]),
                "boundary_alignment": result["boundary_alignment"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
