from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from data_sources.base import HistoricalDataSource
from data_sources.dataset_service import BacktestDatasetService
from data_sources.models import (
    DatasetPreview,
    DatasetRequest,
    FundingEvent,
    HistoricalSymbol,
    NormalizedKline,
)
from db.database import init_db
from db.repository import Repository


class FakeHistoricalDataSource(HistoricalDataSource):
    provider_id = "fake"

    def __init__(
        self,
        rows: list[NormalizedKline],
        funding: list[FundingEvent] | None = None,
    ) -> None:
        self.rows = rows
        self.funding = funding
        self.supports_funding = funding is not None

    async def list_symbols(self, query: str = "") -> list[HistoricalSymbol]:
        return [HistoricalSymbol("BTCUSDT")]

    async def preview(self, symbol, interval, start_time, end_time) -> DatasetPreview:
        return DatasetPreview("fake", symbol, interval, start_time, end_time, len(self.rows), 1, 100)

    async def fetch_klines(self, symbol, interval, start_time, end_time):
        for row in self.rows:
            yield row

    async def fetch_funding(self, symbol, start_time, end_time):
        for event in self.funding or []:
            yield event


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


def _funding() -> list[FundingEvent]:
    start = int(_request().start_time.timestamp() * 1000)
    return [
        FundingEvent(start, 0.0001, mark_price=100.0),
        FundingEvent(start + 60_000, -0.0002, mark_price=None),
    ]


def _service(tmp_path, rows=None, funding=None):
    db_path = tmp_path / "quietgrid.db"
    init_db(db_path)
    repo = Repository(db_path)
    return BacktestDatasetService(
        repo=repo,
        dataset_root=tmp_path / "datasets",
        staging_root=tmp_path / "staging",
        source_factory=lambda provider: FakeHistoricalDataSource(rows or _rows(), funding),
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


def _funding_request() -> DatasetRequest:
    base = _request()
    return DatasetRequest(
        provider=base.provider,
        symbol=base.symbol,
        interval=base.interval,
        start_time=base.start_time,
        end_time=base.end_time,
        window_mode=base.window_mode,
        include_funding=True,
    )


def test_dataset_service_freezes_and_loads_funding_sidecar(tmp_path) -> None:
    service, repo = _service(tmp_path, funding=_funding())
    request = _funding_request()

    async def run():
        job = await service.create_job(request)
        await service.run_job(job["job_id"], request)
        return repo.get_backtest_dataset_job(job["job_id"])

    completed = asyncio.run(run())
    assert completed["status"] == "READY"
    dataset_id = completed["dataset_id"]

    dataset = repo.get_backtest_dataset(dataset_id)
    assert dataset["has_funding"] == 1
    assert dataset["funding_event_count"] == 2
    sidecar = tmp_path / "datasets" / dataset["funding_file_path"]
    assert sidecar.is_file()

    events = service.load_funding_events(dataset_id)
    assert [event.funding_time for event in events] == [
        int(request.start_time.timestamp() * 1000),
        int(request.start_time.timestamp() * 1000) + 60_000,
    ]
    assert events[0].mark_price == 100.0
    assert events[1].mark_price is None


def test_dataset_service_detects_funding_sidecar_tampering(tmp_path) -> None:
    service, repo = _service(tmp_path, funding=_funding())
    request = _funding_request()

    async def run():
        job = await service.create_job(request)
        await service.run_job(job["job_id"], request)
        return repo.get_backtest_dataset_job(job["job_id"])

    completed = asyncio.run(run())
    dataset_id = completed["dataset_id"]
    dataset = repo.get_backtest_dataset(dataset_id)
    sidecar = tmp_path / "datasets" / dataset["funding_file_path"]
    sidecar.write_text(sidecar.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(RuntimeError, match="校验和不匹配"):
        service.resolve(dataset_id)
    assert repo.get_backtest_dataset(dataset_id)["status"] == "CORRUPTED"


def test_dataset_service_without_funding_has_empty_events(tmp_path) -> None:
    service, repo = _service(tmp_path)

    async def run():
        job = await service.create_job(_request())
        await service.run_job(job["job_id"], _request())
        return repo.get_backtest_dataset_job(job["job_id"])

    completed = asyncio.run(run())
    dataset_id = completed["dataset_id"]
    dataset = repo.get_backtest_dataset(dataset_id)
    assert dataset["has_funding"] == 0
    assert dataset["funding_file_path"] is None
    assert service.load_funding_events(dataset_id) == []


def test_dataset_service_rejects_funding_when_unsupported(tmp_path) -> None:
    service, repo = _service(tmp_path)  # funding=None -> supports_funding False
    request = _funding_request()

    async def run():
        job = await service.create_job(request)
        await service.run_job(job["job_id"], request)
        return repo.get_backtest_dataset_job(job["job_id"])

    completed = asyncio.run(run())
    assert completed["status"] == "FAILED"
    assert "不支持历史资金费" in completed["error"]


def test_dataset_service_does_not_reuse_cache_across_funding_flag(tmp_path) -> None:
    service, repo = _service(tmp_path, funding=_funding())

    async def freeze(request):
        job = await service.create_job(request)
        await service.run_job(job["job_id"], request)
        return repo.get_backtest_dataset_job(job["job_id"])

    plain = asyncio.run(freeze(_request()))
    assert plain["stage"] != "CACHE_HIT"

    # 带资金费的请求不得命中不含资金费的既有数据集。
    with_funding = asyncio.run(service.create_job(_funding_request()))
    assert with_funding["stage"] != "CACHE_HIT"
