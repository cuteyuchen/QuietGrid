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
from data_sources.csv_source import INTERVAL_MILLISECONDS
from data_sources.models import DatasetPreview, DatasetRequest, DatasetStatus, NormalizedKline
from data_sources.normalizer import validate_and_normalize_klines
from db.repository import Repository


SCHEMA_VERSION = 1
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
        max_range_days_1m: int = 180,
    ) -> None:
        self.repo = repo
        self.dataset_root = Path(dataset_root).resolve()
        self.staging_root = Path(staging_root).resolve()
        self.source_factory = source_factory
        self.validation_config = validation_config or {}
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
                window_count=preview.window_count,
                warnings=preview.warnings,
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
                stage="VALIDATING",
                progress=0.9,
            )
            if not normalized:
                raise DataSourceError("指定时间范围没有可用的已闭合K线。")
            if quality.errors:
                raise DataSourceError("；".join(quality.errors))
            self._ensure_not_cancelled(job_id)
            metadata = _checksum_metadata(request, normalized)
            _write_frozen_csv(staging_path, normalized)
            checksum = _dataset_checksum(staging_path, metadata)
            dataset_id = _dataset_id(request, normalized, checksum)
            final_path = (self.dataset_root / f"{dataset_id}.csv").resolve()
            if not final_path.is_relative_to(self.dataset_root):
                raise DataSourceError("冻结数据集路径越界。")
            os.replace(staging_path, final_path)
            status = quality.status.value
            self.repo.save_backtest_dataset(
                {
                    "dataset_id": dataset_id,
                    "provider": request.provider,
                    "market": "USDS_M",
                    "symbol": request.symbol,
                    "interval": request.interval,
                    "price_type": "CONTRACT",
                    "requested_start": request.start_time.isoformat(),
                    "requested_end": request.end_time.isoformat(),
                    "actual_start": normalized[0].open_datetime.isoformat(),
                    "actual_end": normalized[-1].open_datetime.isoformat(),
                    "row_count": len(normalized),
                    "file_format": "csv",
                    "file_path": final_path.relative_to(self.dataset_root).as_posix(),
                    "checksum": checksum,
                    "schema_version": SCHEMA_VERSION,
                    "quality_status": quality.status.value,
                    "quality_report": quality.to_mapping(),
                    "window_mode": request.window_mode,
                    "window_count": None,
                    "status": status,
                }
            )
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
        actual_checksum = _dataset_checksum(path, metadata)
        if actual_checksum != dataset["checksum"]:
            message = "冻结回测数据集校验和不匹配，文件可能已被修改。"
            self.repo.mark_backtest_dataset_corrupted(dataset_id, message)
            raise DataSourceError(message)
        return path, dataset

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


def _checksum_metadata(
    request: DatasetRequest,
    rows: list[NormalizedKline],
) -> dict[str, Any]:
    return {
        "provider": request.provider,
        "symbol": request.symbol,
        "interval": request.interval,
        "requested_start": request.start_time.isoformat(),
        "requested_end": request.end_time.isoformat(),
        "actual_start": rows[0].open_datetime.isoformat(),
        "actual_end": rows[-1].open_datetime.isoformat(),
        "schema_version": SCHEMA_VERSION,
    }


def _dataset_checksum(path: Path, metadata: dict[str, Any]) -> str:
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
