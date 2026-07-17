"""本地 CSV 历史数据源与旧版回测 CSV 兼容读取器。"""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from data_sources.base import DataSourceError, HistoricalDataSource
from data_sources.models import DatasetPreview, HistoricalSymbol, NormalizedKline


INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
}


class CsvHistoricalDataSource(HistoricalDataSource):
    provider_id = "csv"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def list_symbols(self, query: str = "") -> list[HistoricalSymbol]:
        symbol = self.path.stem.upper()
        if query and query.strip().upper() not in symbol:
            return []
        return [HistoricalSymbol(symbol=symbol, status="LOCAL", market="LOCAL")]

    async def preview(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> DatasetPreview:
        _validate_time_range(start_time, end_time)
        rows = _read_csv_dicts(self.path)
        estimated_rows = sum(
            1
            for line_number, row in rows
            if start_time <= _parse_timestamp(row, line_number) < end_time
        )
        return DatasetPreview(
            provider=self.provider_id,
            symbol=symbol.strip().upper() or self.path.stem.upper(),
            interval=interval,
            start_time=start_time,
            end_time=end_time,
            estimated_rows=estimated_rows,
            estimated_pages=1 if estimated_rows else 0,
            estimated_size_bytes=self.path.stat().st_size,
            cache_hit=True,
        )

    async def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_time: datetime,
        end_time: datetime,
    ) -> AsyncIterator[NormalizedKline]:
        _validate_time_range(start_time, end_time)
        interval_ms = _interval_ms(interval)
        for line_number, row in _read_csv_dicts(self.path):
            normalized = _standard_kline(row, line_number, interval_ms)
            if start_time <= normalized.open_datetime < end_time:
                yield normalized


def read_legacy_backtest_csv(csv_path: str | Path) -> list[dict[str, Any]]:
    """保持 v2.0 以前的宽松 CSV 契约，供 CLI 与旧 API 继续使用。"""
    rows_with_lines = _read_csv_dicts(Path(csv_path))
    rows = [_normalize_legacy_row(row, line_number) for line_number, row in rows_with_lines]
    if not rows:
        raise RuntimeError("回测CSV没有数据行。")
    return rows


def _read_csv_dicts(path: Path) -> list[tuple[int, dict[str, Any]]]:
    if not path.exists():
        raise RuntimeError(f"回测CSV不存在: {path}")
    if not path.is_file():
        raise RuntimeError(f"回测CSV不是文件: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise RuntimeError("回测CSV缺少表头。")
        fieldnames = {name.strip() for name in reader.fieldnames}
        missing = {"high", "low", "close"} - fieldnames
        if missing:
            raise RuntimeError(f"回测CSV缺少必要列: {', '.join(sorted(missing))}")
        return [(line_number, _strip_mapping(row)) for line_number, row in enumerate(reader, start=2)]


def _normalize_legacy_row(row: Mapping[str, Any], line_number: int) -> dict[str, Any]:
    normalized = dict(row)
    for key in ("high", "low", "close"):
        try:
            normalized[key] = float(normalized[key])
        except (TypeError, ValueError, KeyError) as exc:
            raise RuntimeError(f"回测CSV第{line_number}行 {key} 无效。") from exc
    return normalized


def _standard_kline(row: Mapping[str, Any], line_number: int, interval_ms: int) -> NormalizedKline:
    open_time = int(_parse_timestamp(row, line_number).timestamp() * 1000)
    close_time = _optional_timestamp_ms(row.get("close_time"), line_number) or open_time + interval_ms - 1
    open_value = row.get("open", row.get("close"))
    try:
        return NormalizedKline(
            open_time=open_time,
            close_time=close_time,
            open=float(open_value),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume") or 0.0),
            quote_volume=float(row.get("quote_volume") or row.get("quote_asset_volume") or 0.0),
            trade_count=int(float(row.get("trade_count") or row.get("number_of_trades") or 0)),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise DataSourceError(f"回测CSV第{line_number}行标准K线无效: {exc}") from exc


def _parse_timestamp(row: Mapping[str, Any], line_number: int) -> datetime:
    raw = row.get("open_time", row.get("timestamp"))
    if raw in (None, ""):
        raise DataSourceError(f"回测CSV第{line_number}行缺少 open_time 或 timestamp。")
    parsed = _parse_timestamp_value(raw)
    if parsed is None:
        raise DataSourceError(f"回测CSV第{line_number}行时间戳无效。")
    return parsed


def _optional_timestamp_ms(raw: Any, line_number: int) -> int | None:
    if raw in (None, ""):
        return None
    parsed = _parse_timestamp_value(raw)
    if parsed is None:
        raise DataSourceError(f"回测CSV第{line_number}行 close_time 无效。")
    return int(parsed.timestamp() * 1000)


def _parse_timestamp_value(raw: Any) -> datetime | None:
    text = str(raw).strip()
    try:
        numeric = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    seconds = numeric / 1000 if abs(numeric) >= 100_000_000_000 else numeric
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _strip_mapping(row: Mapping[str | None, Any]) -> dict[str, Any]:
    return {key.strip(): value for key, value in row.items() if key is not None}


def _interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MILLISECONDS[interval]
    except KeyError as exc:
        raise DataSourceError(f"CSV 数据源暂不支持周期: {interval}") from exc


def _validate_time_range(start_time: datetime, end_time: datetime) -> None:
    if start_time.tzinfo is None or end_time.tzinfo is None:
        raise ValueError("start_time 和 end_time 必须包含时区。")
    if start_time >= end_time:
        raise ValueError("start_time 必须早于 end_time。")
