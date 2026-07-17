"""标准 K 线排序、去重、缺口和闭合状态校验。"""

from __future__ import annotations

from collections.abc import Iterable

from data_sources.base import DataSourceError
from data_sources.models import DatasetQualityReport, NormalizedKline


def validate_and_normalize_klines(
    rows: Iterable[NormalizedKline],
    *,
    interval_ms: int,
    now_ms: int | None = None,
    drop_unclosed: bool = True,
    warning_missing_ratio: float = 0.001,
    reject_missing_ratio: float = 0.01,
    max_consecutive_missing: int = 5,
) -> tuple[list[NormalizedKline], DatasetQualityReport]:
    if interval_ms <= 0:
        raise ValueError("interval_ms 必须为正整数。")
    if not 0 <= warning_missing_ratio <= reject_missing_ratio <= 1:
        raise ValueError("缺口告警与拒绝阈值无效。")
    if max_consecutive_missing < 0:
        raise ValueError("max_consecutive_missing 不能为负数。")

    source_rows = list(rows)
    report = DatasetQualityReport(input_rows=len(source_rows))
    ordered = sorted(source_rows, key=lambda item: item.open_time)
    unique: dict[int, NormalizedKline] = {}
    for row in ordered:
        if now_ms is not None and row.close_time >= now_ms:
            report.unclosed_rows += 1
            if drop_unclosed:
                continue
        existing = unique.get(row.open_time)
        if existing is None:
            unique[row.open_time] = row
            continue
        if existing == row:
            report.duplicate_rows += 1
            continue
        report.conflicting_duplicates += 1
        message = f"发现冲突重复K线: open_time={row.open_time}"
        report.errors.append(message)
        raise DataSourceError(message)

    normalized = list(unique.values())
    report.output_rows = len(normalized)
    if normalized:
        report.first_open_time = normalized[0].open_time
        report.last_open_time = normalized[-1].open_time

    for previous, current in zip(normalized, normalized[1:]):
        delta = current.open_time - previous.open_time
        if delta <= interval_ms:
            continue
        missing = max(0, delta // interval_ms - 1)
        report.missing_intervals += missing
        report.max_consecutive_missing = max(report.max_consecutive_missing, missing)

    if report.unclosed_rows and drop_unclosed:
        report.warnings.append(f"已移除 {report.unclosed_rows} 根尚未闭合的K线。")
    if report.missing_ratio > reject_missing_ratio:
        report.errors.append(
            f"K线缺口比例 {report.missing_ratio:.4%} 超过拒绝阈值 {reject_missing_ratio:.4%}。"
        )
    elif report.missing_ratio > warning_missing_ratio:
        report.warnings.append(
            f"K线缺口比例 {report.missing_ratio:.4%} 超过告警阈值 {warning_missing_ratio:.4%}。"
        )
    if report.max_consecutive_missing > max_consecutive_missing:
        report.errors.append(
            f"最大连续缺失 {report.max_consecutive_missing} 根，超过上限 {max_consecutive_missing}。"
        )
    return normalized, report
