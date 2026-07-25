"""Freeze Binance REST data required by the semiconductor v2.7 backtest."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data_sources.binance_source import BinanceHistoricalDataSource
from strategy.semiconductor_grid import RESEARCH_SYMBOLS


UTC = timezone.utc
CSV_FIELDS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="冻结半导体休市网格回测所需 Binance 1m K 线与 Funding",
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--rules-json", required=True)
    parser.add_argument("--data-dir", default="data/backtests/semiconductor-v2.7")
    parser.add_argument("--symbols", nargs="*", default=list(RESEARCH_SYMBOLS))
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    asyncio.run(_freeze(args, raw))


async def _freeze(args: argparse.Namespace, raw_config: dict[str, Any]) -> None:
    rules = json.loads(Path(args.rules_json).read_text(encoding="utf-8"))
    records = rules.get("symbols", {})
    symbols = tuple(str(value).strip().upper() for value in args.symbols)
    unknown = sorted(set(symbols) - set(RESEARCH_SYMBOLS))
    if unknown:
        raise ValueError("未注册标的: " + ", ".join(unknown))
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    source = BinanceHistoricalDataSource(
        proxy_config=raw_config.get("proxy", {}) or {},
        validate_symbol_listing=False,
        pause_seconds=0.0,
    )
    try:
        outputs: list[dict[str, Any]] = []
        for symbol in symbols:
            rule = records.get(symbol)
            if not isinstance(rule, dict):
                raise ValueError(f"规则快照缺少 {symbol}。")
            onboard = int(rule.get("onboard_date") or 0)
            if onboard <= 0:
                raise ValueError(f"规则快照缺少 {symbol} 上线时间。")
            start = datetime.fromtimestamp(onboard / 1000, tz=UTC)
            csv_path = data_dir / f"{symbol}-1m.csv"
            funding_path = csv_path.with_suffix(".funding.json")
            if not args.overwrite and (csv_path.exists() or funding_path.exists()):
                raise FileExistsError(f"冻结文件已存在: {csv_path}")
            rows = []
            previous_open: int | None = None
            async for item in source.fetch_klines(symbol, "1m", start, end):
                if previous_open is not None and item.open_time <= previous_open:
                    raise ValueError(f"{symbol} K 线时间不严格递增。")
                rows.append(item)
                previous_open = item.open_time
            if not rows:
                raise ValueError(f"{symbol} 未返回任何已闭合 1m K 线。")
            funding = [
                event
                async for event in source.fetch_funding(symbol, start, end)
            ]
            with csv_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for item in rows:
                    writer.writerow({field: getattr(item, field) for field in CSV_FIELDS})
            funding_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source": "binance_rest",
                        "symbol": symbol,
                        "generated_at": datetime.now(UTC).isoformat(),
                        "events": [
                            {
                                "funding_time": event.funding_time,
                                "funding_rate": event.funding_rate,
                                "mark_price": event.mark_price,
                            }
                            for event in funding
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            outputs.append(
                {
                    "symbol": symbol,
                    "csv": str(csv_path),
                    "rows": len(rows),
                    "funding": str(funding_path),
                    "funding_events": len(funding),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }
            )
    finally:
        await source.close()
    print(json.dumps({"outputs": outputs}, ensure_ascii=False))


if __name__ == "__main__":
    main()
