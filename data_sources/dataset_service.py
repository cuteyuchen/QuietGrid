"""在线数据下载、校验、冻结、哈希验证与后台任务编排。"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from data_sources.base import DataSourceError, HistoricalDataSource
from data_sources.csv_source import CsvHistoricalDataSource, INTERVAL_MILLISECONDS
from data_sources.models import (
    DatasetPreview,
    DatasetRequest,
    DatasetStatus,
    FundingEvent,
    NormalizedKline,
)
from data_sources.normalizer import validate_and_normalize_klines
from data_sources.window_slicer import NyseWindowSlicer, normalized_klines_from_mappings
from db.repository import Repository


SCHEMA_VERSION = 2
FROZEN_CSV_FIELDS = (
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


class DatasetJobCancelled(DataSourceError):
    pass


class BacktestDatasetService:
    def __init__(
        self,
        *,
        repo: Repository,
        dataset_root: str | Path,
        staging_root: str | Path,
        source_factory: Callable[[str], HistoricalDataSource],
        validation_config: dict[str, Any] | None = None,
        windowing_config: dict[str, Any] | None = None,
        max_range_days_1m: int = 180,
        default_observation_rows: int = 180,
    ) -> None:
        self.repo = repo
        self.default_observation_rows = max(0, int(default_observation_rows))
        self.dataset_root = Path(dataset_root).resolve()
        self.staging_root = Path(staging_root).resolve()
        self.source_factory = source_factory
        self.validation_config = validation_config or {}
        windowing = windowing_config or {}
        self.window_slicer = NyseWindowSlicer(
            force_close_minutes=int(windowing.get("force_close_minutes", 120)),
            minimum_tradable_rows=int(windowing.get("minimum_tradable_rows", 30)),
        )
        self.max_range_days_1m = max(1, int(max_range_days_1m))

    async def preview(self, request: DatasetRequest) -> DatasetPreview:
        self._validate_request_limits(request)
        source = self.source_factory(request.provider)
        try:
            preview = await source.preview(
                request.symbol,
                request.interval,
                request.start_time,
                request.end_time,
            )
            cached = self.repo.find_backtest_dataset(
                provider=request.provider,
                symbol=request.symbol,
                interval=request.interval,
                requested_start=request.start_time,
                requested_end=request.end_time,
                window_mode=request.window_mode,
            )
            warnings = list(preview.warnings)
            window_count = preview.window_count
            if request.window_mode == "NYSE_CLOSED_ONLY":
                window_count = self.window_slicer.estimate_window_count(
                    request.start_time,
                    request.end_time,
                )
                if window_count == 0:
                    warnings.append("所选范围未覆盖完整或部分 NYSE 休市窗口。")
            return DatasetPreview(
                provider=preview.provider,
                symbol=preview.symbol,
                interval=preview.interval,
                start_time=preview.start_time,
                end_time=preview.end_time,
                estimated_rows=preview.estimated_rows,
                estimated_pages=preview.estimated_pages,
                estimated_size_bytes=preview.estimated_size_bytes,
                cache_hit=cached is not None,
                window_count=window_count,
                warnings=tuple(warnings),
            )
        finally:
            await _close_source(source)

    async def create_job(self, request: DatasetRequest) -> dict[str, Any]:
        if self.repo.active_backtest_dataset_job_count() >= 1:
            raise DataSourceError("当前账户已有历史数据任务在运行，请等待完成或先取消。")
        preview = await self.preview(request)
        job_id = f"job_{uuid4().hex}"
        self.repo.create_backtest_dataset_job(
            job_id=job_id,
            provider=request.provider,
            symbol=request.symbol,
            interval=request.interval,
            requested_start=request.start_time,
            requested_end=request.end_time,
            window_mode=request.window_mode,
            total_pages=preview.estimated_pages,
        )
        cached = self.repo.find_backtest_dataset(
            provider=request.provider,
            symbol=request.symbol,
            interval=request.interval,
            requested_start=request.start_time,
            requested_end=request.end_time,
            window_mode=request.window_mode,
        )
        if cached is not None and bool(cached.get("has_funding")) != request.include_funding:
            # 资金费需求不一致的既有数据集不能复用，需重新下载并冻结带/不带 sidecar 的版本。
            cached = None
        if cached is not None:
            try:
                self.resolve(str(cached["dataset_id"]))
            except DataSourceError:
                cached = None
        if cached is not None:
            now = datetime.now(timezone.utc).isoformat()
            self.repo.update_backtest_dataset_job(
                job_id,
                dataset_id=cached["dataset_id"],
                status="READY",
                stage="CACHE_HIT",
                progress=1.0,
                downloaded_rows=int(cached.get("row_count") or 0),
                completed_at=now,
            )
        job = self.repo.get_backtest_dataset_job(job_id)
        if job is None:
            raise DataSourceError("数据集任务创建失败。")
        return job

    async def import_csv(
        self,
        source_path: str | Path,
        *,
        symbol: str,
        interval: str,
        window_mode: str = "NYSE_CLOSED_ONLY",
    ) -> dict[str, Any]:
        """校验用户上传 CSV，并冻结为与在线下载一致的不可变数据集。"""
        if interval not in INTERVAL_MILLISECONDS:
            raise DataSourceError(f"不支持的历史数据周期: {interval}")
        path = Path(source_path).resolve()
        source = CsvHistoricalDataSource(path)
        earliest = datetime(1970, 1, 1, tzinfo=timezone.utc)
        latest = datetime(2100, 1, 1, tzinfo=timezone.utc)
        try:
            rows = [
                row
                async for row in source.fetch_klines(
                    symbol.strip().upper(),
                    interval,
                    earliest,
                    latest,
                )
            ]
        except RuntimeError as exc:
            raise DataSourceError(str(exc)) from exc
        normalized, quality = validate_and_normalize_klines(
            rows,
            interval_ms=INTERVAL_MILLISECONDS[interval],
            now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
            drop_unclosed=bool(self.validation_config.get("drop_unclosed", True)),
            warning_missing_ratio=float(
                self.validation_config.get("warning_missing_ratio", 0.001)
            ),
            reject_missing_ratio=float(
                self.validation_config.get("reject_missing_ratio", 0.01)
            ),
            max_consecutive_missing=int(
                self.validation_config.get("max_consecutive_missing", 5)
            ),
        )
        if not normalized:
            raise DataSourceError("上传 CSV 没有可用的已闭合K线。")
        if quality.errors:
            raise DataSourceError("；".join(quality.errors))
        interval_ms = INTERVAL_MILLISECONDS[interval]
        request = DatasetRequest(
            provider="csv",
            symbol=symbol,
            interval=interval,
            start_time=normalized[0].open_datetime,
            end_time=datetime.fromtimestamp(
                (normalized[-1].open_time + interval_ms) / 1000,
                tz=timezone.utc,
            ),
            window_mode=window_mode,
        )
        self.dataset_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        staging_path = self.staging_root / f"upload_{uuid4().hex}.csv.tmp"
        try:
            metadata = _checksum_metadata(request, normalized)
            _write_frozen_csv(staging_path, normalized)
            checksum = _dataset_checksum(staging_path, metadata)
            dataset_id = _dataset_id(request, normalized, checksum)
            cached = self.repo.get_backtest_dataset(dataset_id)
            if cached is not None:
                self._persist_windows(dataset_id, normalized, request.window_mode)
                return self.repo.get_backtest_dataset(dataset_id) or cached
            final_path = (self.dataset_root / f"{dataset_id}.csv").resolve()
            if not final_path.is_relative_to(self.dataset_root):
                raise DataSourceError("冻结数据集路径越界。")
            os.replace(staging_path, final_path)
            self.repo.save_backtest_dataset(
                self._dataset_row(
                    dataset_id=dataset_id,
                    request=request,
                    normalized=normalized,
                    quality=quality,
                    file_path=final_path.relative_to(self.dataset_root).as_posix(),
                    checksum=checksum,
                    market="LOCAL",
                    source_segments=None,
                )
            )
            self._persist_windows(dataset_id, normalized, request.window_mode)
            dataset = self.repo.get_backtest_dataset(dataset_id)
            if dataset is None:
                raise DataSourceError("上传数据集冻结失败。")
            return dataset
        finally:
            if staging_path.exists():
                staging_path.unlink()

    async def run_job(self, job_id: str, request: DatasetRequest) -> None:
        job = self.repo.get_backtest_dataset_job(job_id)
        if job is None:
            raise DataSourceError(f"数据集任务不存在: {job_id}")
        if job["status"] == "READY":
            return
        self.dataset_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        staging_path = self.staging_root / f"{job_id}.csv.tmp"
        source: HistoricalDataSource | None = None
        try:
            self._ensure_not_cancelled(job_id)
            now = datetime.now(timezone.utc).isoformat()
            self.repo.update_backtest_dataset_job(
                job_id,
                status="DOWNLOADING",
                stage="DOWNLOADING",
                progress=0.01,
                started_at=now,
            )
            source = self.source_factory(request.provider)
            preview = await source.preview(
                request.symbol,
                request.interval,
                request.start_time,
                request.end_time,
            )
            rows: list[NormalizedKline] = []
            async for row in source.fetch_klines(
                request.symbol,
                request.interval,
                request.start_time,
                request.end_time,
            ):
                rows.append(row)
                if len(rows) % 250 == 0:
                    self._ensure_not_cancelled(job_id)
                    ratio = min(0.78, len(rows) / max(1, preview.estimated_rows) * 0.78)
                    self.repo.update_backtest_dataset_job(
                        job_id,
                        progress=ratio,
                        current_page=min(
                            preview.estimated_pages,
                            int(getattr(source, "pages_fetched", 0))
                            or _estimated_current_page(len(rows), preview),
                        ),
                        downloaded_rows=len(rows),
                    )
            self._ensure_not_cancelled(job_id)
            self.repo.update_backtest_dataset_job(
                job_id,
                status="NORMALIZING",
                stage="NORMALIZING",
                progress=0.82,
                downloaded_rows=len(rows),
            )
            normalized, quality = validate_and_normalize_klines(
                rows,
                interval_ms=INTERVAL_MILLISECONDS[request.interval],
                now_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
                drop_unclosed=bool(self.validation_config.get("drop_unclosed", True)),
                warning_missing_ratio=float(
                    self.validation_config.get("warning_missing_ratio", 0.001)
                ),
                reject_missing_ratio=float(
                    self.validation_config.get("reject_missing_ratio", 0.01)
                ),
                max_consecutive_missing=int(
                    self.validation_config.get("max_consecutive_missing", 5)
                ),
            )
            self.repo.update_backtest_dataset_job(
                job_id,
                status="VALIDATING",
                stage="WINDOWING",
                progress=0.9,
            )
            if not normalized:
                raise DataSourceError("指定时间范围没有可用的已闭合K线。")
            if quality.errors:
                raise DataSourceError("；".join(quality.errors))
            self._ensure_not_cancelled(job_id)
            funding_events = await self._maybe_fetch_funding(request, source, normalized)
            metadata = _checksum_metadata(request, normalized, funding_events)
            _write_frozen_csv(staging_path, normalized)
            funding_staging: Path | None = None
            if funding_events is not None:
                funding_staging = self.staging_root / f"{job_id}.funding.json.tmp"
                _write_funding_sidecar(funding_staging, funding_events)
            checksum = _dataset_checksum(staging_path, metadata, funding_staging)
            dataset_id = _dataset_id(request, normalized, checksum)
            final_path = (self.dataset_root / f"{dataset_id}.csv").resolve()
            if not final_path.is_relative_to(self.dataset_root):
                raise DataSourceError("冻结数据集路径越界。")
            funding_file_path: str | None = None
            if funding_staging is not None:
                funding_final = (self.dataset_root / f"{dataset_id}.funding.json").resolve()
                if not funding_final.is_relative_to(self.dataset_root):
                    raise DataSourceError("冻结资金费 sidecar 路径越界。")
                os.replace(funding_staging, funding_final)
                funding_file_path = funding_final.relative_to(self.dataset_root).as_posix()
            os.replace(staging_path, final_path)
            self.repo.save_backtest_dataset(
                self._dataset_row(
                    dataset_id=dataset_id,
                    request=request,
                    normalized=normalized,
                    quality=quality,
                    file_path=final_path.relative_to(self.dataset_root).as_posix(),
                    checksum=checksum,
                    market="USDS_M",
                    source_segments=_source_segments(source),
                    funding_events=funding_events,
                    funding_file_path=funding_file_path,
                )
            )
            self._persist_windows(dataset_id, normalized, request.window_mode)
            completed_at = datetime.now(timezone.utc).isoformat()
            self.repo.update_backtest_dataset_job(
                job_id,
                dataset_id=dataset_id,
                status="READY",
                stage="COMPLETED",
                progress=1.0,
                downloaded_rows=len(normalized),
                current_page=preview.estimated_pages,
                completed_at=completed_at,
            )
        except DatasetJobCancelled as exc:
            self.repo.update_backtest_dataset_job(
                job_id,
                status="CANCELLED",
                stage="CANCELLED",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            self.repo.update_backtest_dataset_job(
                job_id,
                status="FAILED",
                stage="FAILED",
                error=str(exc),
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
        finally:
            if source is not None:
                await _close_source(source)
            if staging_path.exists():
                staging_path.unlink()
            funding_tmp = self.staging_root / f"{job_id}.funding.json.tmp"
            if funding_tmp.exists():
                funding_tmp.unlink()

    def resolve(self, dataset_id: str) -> tuple[Path, dict[str, Any]]:
        dataset = self.repo.get_backtest_dataset(dataset_id)
        if dataset is None:
            raise DataSourceError("冻结回测数据集不存在。")
        if dataset.get("status") not in {
            DatasetStatus.READY.value,
            DatasetStatus.READY_WITH_WARNINGS.value,
        }:
            raise DataSourceError(f"冻结回测数据集不可用: {dataset.get('status')}")
        path = (self.dataset_root / str(dataset["file_path"])).resolve()
        if not path.is_relative_to(self.dataset_root):
            raise DataSourceError("冻结回测数据集路径越界。")
        if not path.is_file():
            raise DataSourceError("冻结回测数据集文件不存在。")
        metadata = {
            "provider": dataset["provider"],
            "symbol": dataset["symbol"],
            "interval": dataset["interval"],
            "requested_start": dataset["requested_start"],
            "requested_end": dataset["requested_end"],
            "actual_start": dataset["actual_start"],
            "actual_end": dataset["actual_end"],
            "schema_version": int(dataset["schema_version"]),
        }
        if int(dataset["schema_version"]) >= 2:
            metadata["window_mode"] = str(dataset.get("window_mode") or "RAW_RANGE")
        funding_path: Path | None = None
        if dataset.get("has_funding"):
            metadata["has_funding"] = True
            funding_path = self._resolve_funding_path(dataset)
        actual_checksum = _dataset_checksum(path, metadata, funding_path)
        if actual_checksum != dataset["checksum"]:
            message = "冻结回测数据集校验和不匹配，文件可能已被修改。"
            self.repo.mark_backtest_dataset_corrupted(dataset_id, message)
            raise DataSourceError(message)
        if (
            str(dataset.get("window_mode") or "RAW_RANGE") == "NYSE_CLOSED_ONLY"
            and not self.repo.backtest_dataset_windows(dataset_id)
        ):
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                mappings = list(csv.DictReader(fh))
            rows = normalized_klines_from_mappings(
                mappings,
                interval_ms=INTERVAL_MILLISECONDS[str(dataset["interval"])],
            )
            self._persist_windows(dataset_id, rows, "NYSE_CLOSED_ONLY")
            dataset = self.repo.get_backtest_dataset(dataset_id) or dataset
        return path, dataset

    def _resolve_funding_path(self, dataset: dict[str, Any]) -> Path:
        relative = dataset.get("funding_file_path")
        if not relative:
            raise DataSourceError("冻结数据集标记含资金费，但缺少 sidecar 路径。")
        funding_path = (self.dataset_root / str(relative)).resolve()
        if not funding_path.is_relative_to(self.dataset_root):
            raise DataSourceError("冻结资金费 sidecar 路径越界。")
        if not funding_path.is_file():
            raise DataSourceError("冻结资金费 sidecar 文件不存在。")
        return funding_path

    def load_funding_events(self, dataset_id: str) -> list[FundingEvent]:
        """读回冻结的资金费事件；数据集未附带资金费时返回空列表。

        会先经 resolve 完成校验和验证，确保 K 线与资金费一致且未被篡改。
        """
        _, dataset = self.resolve(dataset_id)
        if not dataset.get("has_funding"):
            return []
        return _load_funding_sidecar(self._resolve_funding_path(dataset))

    def _window_counts(
        self,
        rows: list[NormalizedKline],
        window_mode: str,
    ) -> dict[str, int]:
        """区分原始窗口数与按默认观察长度实际可回测的窗口数（计划 §5.5）。

        raw_window_count 沿用 observation_rows=0 的口径，仅剔除无数据窗口；
        eligible_window_count 以服务的默认观察行数切分后统计满足观察与可交易
        长度的窗口，skipped 为二者之差。正式回测会以请求中的观察长度重算。
        """
        if window_mode != "NYSE_CLOSED_ONLY":
            raw = 1 if rows else 0
            return {"raw": raw, "eligible": raw, "skipped": 0}
        raw_windows = self.window_slicer.slice(rows, observation_rows=0)
        raw = sum(1 for window in raw_windows if window.skip_reason != "NO_DATA")
        eligible_windows = self.window_slicer.slice(
            rows,
            observation_rows=self.default_observation_rows,
        )
        eligible = sum(1 for window in eligible_windows if window.status == "READY")
        return {"raw": raw, "eligible": eligible, "skipped": max(0, raw - eligible)}

    def _dataset_row(
        self,
        *,
        dataset_id: str,
        request: DatasetRequest,
        normalized: list[NormalizedKline],
        quality: Any,
        file_path: str,
        checksum: str,
        market: str,
        source_segments: list[dict[str, Any]] | None,
        funding_events: list[FundingEvent] | None = None,
        funding_file_path: str | None = None,
    ) -> dict[str, Any]:
        counts = self._window_counts(normalized, request.window_mode)
        return {
            "dataset_id": dataset_id,
            "provider": request.provider,
            "market": market,
            "symbol": request.symbol,
            "interval": request.interval,
            "price_type": "CONTRACT",
            "requested_start": request.start_time.isoformat(),
            "requested_end": request.end_time.isoformat(),
            "actual_start": normalized[0].open_datetime.isoformat(),
            "actual_end": normalized[-1].open_datetime.isoformat(),
            "row_count": len(normalized),
            "file_format": "csv",
            "file_path": file_path,
            "checksum": checksum,
            "schema_version": SCHEMA_VERSION,
            "quality_status": quality.status.value,
            "quality_report": quality.to_mapping(),
            "window_mode": request.window_mode,
            "window_count": counts["raw"],
            "raw_window_count": counts["raw"],
            "eligible_window_count": counts["eligible"],
            "skipped_window_count": counts["skipped"],
            "source_segments": source_segments,
            "has_funding": funding_events is not None,
            "funding_event_count": len(funding_events) if funding_events is not None else None,
            "funding_file_path": funding_file_path,
            "status": quality.status.value,
        }

    async def _maybe_fetch_funding(
        self,
        request: DatasetRequest,
        source: HistoricalDataSource,
        normalized: list[NormalizedKline],
    ) -> list[FundingEvent] | None:
        """按请求抓取资金费历史；未请求时返回 None（区别于抓到零事件的空列表）。"""
        if not request.include_funding:
            return None
        if not getattr(source, "supports_funding", False):
            raise DataSourceError(f"数据源 {request.provider} 不支持历史资金费。")
        events: list[FundingEvent] = []
        async for event in source.fetch_funding(
            request.symbol,
            request.start_time,
            request.end_time,
        ):
            events.append(event)
        events.sort(key=lambda item: item.funding_time)
        return events

    def _persist_windows(
        self,
        dataset_id: str,
        rows: list[NormalizedKline],
        window_mode: str,
    ) -> None:
        if window_mode != "NYSE_CLOSED_ONLY":
            return
        windows = self.window_slicer.slice(rows, observation_rows=0)
        self.repo.replace_backtest_dataset_windows(
            dataset_id,
            [window.to_metadata() for window in windows],
        )

    def _ensure_not_cancelled(self, job_id: str) -> None:
        job = self.repo.get_backtest_dataset_job(job_id)
        if job is None:
            raise DatasetJobCancelled("下载任务已不存在。")
        if bool(job.get("cancel_requested")):
            raise DatasetJobCancelled("用户已取消数据下载任务。")

    def _validate_request_limits(self, request: DatasetRequest) -> None:
        if request.interval not in INTERVAL_MILLISECONDS:
            raise DataSourceError(f"不支持的历史数据周期: {request.interval}")
        if request.interval == "1m":
            days = (request.end_time - request.start_time).total_seconds() / 86400
            if days > self.max_range_days_1m:
                raise DataSourceError(
                    f"1m 数据范围不能超过 {self.max_range_days_1m} 天。"
                )


def _source_segments(source: HistoricalDataSource | None) -> list[dict[str, Any]] | None:
    """从 Hybrid / Archive 数据源读取分段元数据；其它数据源返回 None。"""
    segments = getattr(source, "source_segments", None)
    if not segments:
        return None
    return [
        segment.to_mapping() if hasattr(segment, "to_mapping") else dict(segment)
        for segment in segments
    ]


async def _close_source(source: HistoricalDataSource) -> None:
    close = getattr(source, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _write_frozen_csv(path: Path, rows: list[NormalizedKline]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FROZEN_CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: getattr(row, field) for field in FROZEN_CSV_FIELDS})
        fh.flush()
        os.fsync(fh.fileno())


def _write_funding_sidecar(path: Path, events: list[FundingEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "events": [
            {
                "funding_time": event.funding_time,
                "funding_rate": event.funding_rate,
                "mark_price": event.mark_price,
            }
            for event in events
        ],
    }
    # 逐字段规范化序列化，保证同一批事件在不同平台/进程下产出稳定字节，校验和才可复现。
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _load_funding_sidecar(path: Path) -> list[FundingEvent]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    raw_events = payload.get("events", []) if isinstance(payload, dict) else []
    events = [
        FundingEvent(
            funding_time=int(item["funding_time"]),
            funding_rate=float(item["funding_rate"]),
            mark_price=(None if item.get("mark_price") is None else float(item["mark_price"])),
        )
        for item in raw_events
    ]
    events.sort(key=lambda event: event.funding_time)
    return events


def _checksum_metadata(
    request: DatasetRequest,
    rows: list[NormalizedKline],
    funding_events: list[FundingEvent] | None = None,
) -> dict[str, Any]:
    metadata = {
        "provider": request.provider,
        "symbol": request.symbol,
        "interval": request.interval,
        "requested_start": request.start_time.isoformat(),
        "requested_end": request.end_time.isoformat(),
        "actual_start": rows[0].open_datetime.isoformat(),
        "actual_end": rows[-1].open_datetime.isoformat(),
        "schema_version": SCHEMA_VERSION,
    }
    if SCHEMA_VERSION >= 2:
        metadata["window_mode"] = request.window_mode
    # 仅在存在资金费 sidecar 时写入标记，未附带资金费的数据集元数据与旧版逐字节一致，
    # 避免存量数据集在 resolve 时误判校验和不匹配。
    if funding_events is not None:
        metadata["has_funding"] = True
    return metadata


def _dataset_checksum(
    path: Path,
    metadata: dict[str, Any],
    funding_path: Path | None = None,
) -> str:
    canonical_metadata = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = sha256()
    digest.update(canonical_metadata)
    digest.update(b"\n")
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    # 资金费 sidecar 一并纳入校验和，保证 K 线与资金费一起被冻结、一起被校验。
    if funding_path is not None:
        digest.update(b"\nfunding\n")
        with funding_path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _dataset_id(
    request: DatasetRequest,
    rows: list[NormalizedKline],
    checksum: str,
) -> str:
    return (
        f"ds_{request.provider}_{request.symbol.lower()}_{request.interval}_"
        f"{rows[0].open_time}_{rows[-1].open_time}_{checksum[:12]}"
    )


def _estimated_current_page(downloaded_rows: int, preview: DatasetPreview) -> int:
    if preview.estimated_pages <= 0 or downloaded_rows <= 0:
        return 0
    rows_per_page = max(1, (preview.estimated_rows + preview.estimated_pages - 1) // preview.estimated_pages)
    return min(preview.estimated_pages, (downloaded_rows + rows_per_page - 1) // rows_per_page)
