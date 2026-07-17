from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from data_sources.base import HistoricalDataSource
from data_sources.dataset_service import BacktestDatasetService
from data_sources.models import DatasetPreview, DatasetRequest, HistoricalSymbol, NormalizedKline
from db.database import init_db
from db.repository import Repository


class FakeHistoricalDataSource(HistoricalDataSource):
    provider_id = "fake"

    def __init__(self, rows: list[NormalizedKline]) -> None:
        self.rows = rows

    async def list_symbols(self, query: str = "") -> list[HistoricalSymbol]:
        return [HistoricalSymbol("BTCUSDT")]

    async def preview(self, symbol, interval, start_time, end_time) -> DatasetPreview:
        return DatasetPreview("fake", symbol, interval, start_time, end_time, len(self.rows), 1, 100)

    async def fetch_klines(self, symbol, interval, start_time, end_time):
        for row in self.rows:
            yield row


def _request() -> DatasetRequest:
    return DatasetRequest(
        provider="fake",
        symbol="BTCUSDT",
        interval="1m",
        start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, 0, 3, tzinfo=timezone.utc),
        window_mode="RAW_RANGE",
    )


def _rows() -> list[NormalizedKline]:
    start = int(_request().start_time.timestamp() * 1000)
    return [
        NormalizedKline(start + index * 60_000, start + index * 60_000 + 59_999, 100, 101, 99, 100)
        for index in range(3)
    ]


def _service(tmp_path, rows=None):
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    return BacktestDatasetService(
        repo=repo,
        dataset_root=tmp_path / "datasets",
        staging_root=tmp_path / "staging",
        source_factory=lambda provider: FakeHistoricalDataSource(rows or _rows()),
    ), repo


def test_dataset_service_freezes_resolves_and_reuses_cache(tmp_path) -> None:
    service, repo = _service(tmp_path)

    async def run():
        job = await service.create_job(_request())
        await service.run_job(job["job_id"], _request())
        return repo.get_backtest_dataset_job(job["job_id"])

    completed = asyncio.run(run())
    assert completed is not None
    assert completed["status"] == "READY"
    dataset_id = completed["dataset_id"]
    path, dataset = service.resolve(dataset_id)
    assert path.is_file()
    assert dataset["row_count"] == 3

    cached_job = asyncio.run(service.create_job(_request()))
    assert cached_job["status"] == "READY"
    assert cached_job["stage"] == "CACHE_HIT"
    assert cached_job["dataset_id"] == dataset_id


def test_dataset_service_detects_file_tampering(tmp_path) -> None:
    service, repo = _service(tmp_path)

    async def run():
        job = await service.create_job(_request())
        await service.run_job(job["job_id"], _request())
        return repo.get_backtest_dataset_job(job["job_id"])

    completed = asyncio.run(run())
    dataset_id = completed["dataset_id"]
    path, _ = service.resolve(dataset_id)
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="校验和不匹配"):
        service.resolve(dataset_id)
    assert repo.get_backtest_dataset(dataset_id)["status"] == "CORRUPTED"

    async def redownload():
        job = await service.create_job(_request())
        await service.run_job(job["job_id"], _request())
        return repo.get_backtest_dataset_job(job["job_id"])

    repaired = asyncio.run(redownload())
    assert repaired["status"] == "READY"
    repaired_path, repaired_dataset = service.resolve(dataset_id)
    assert repaired_path.is_file()
    assert repaired_dataset["status"] == "READY"


def test_dataset_service_honors_cancel_before_download(tmp_path) -> None:
    service, repo = _service(tmp_path)

    async def run():
        job = await service.create_job(_request())
        repo.request_backtest_dataset_job_cancel(job["job_id"])
        await service.run_job(job["job_id"], _request())
        return repo.get_backtest_dataset_job(job["job_id"])

    cancelled = asyncio.run(run())
    assert cancelled["status"] == "CANCELLED"
    assert repo.backtest_datasets() == []


def test_dataset_service_rejects_large_one_minute_range(tmp_path) -> None:
    service, _ = _service(tmp_path)
    request = DatasetRequest(
        provider="fake",
        symbol="BTCUSDT",
        interval="1m",
        start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(RuntimeError, match="不能超过"):
        asyncio.run(service.preview(request))
