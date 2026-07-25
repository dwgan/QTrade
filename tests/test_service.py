import json
from datetime import date
from pathlib import Path

import polars as pl

from qtrade.config import ValidationConfig
from qtrade.data.service import DataIngestionService
from qtrade.data.storage import ParquetDatasetStore
from qtrade.data.validation import DataValidator
from qtrade.domain import DataBatch, Dataset, FetchRequest


class FakeProvider:
    name = "fake"

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        assert dataset == Dataset.ADJUST_FACTORS
        return DataBatch(
            dataset=dataset,
            provider=self.name,
            as_of_date=request.as_of_date,
            frame=pl.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000001.SZ"],
                    "trade_date": ["20260724", "20260724"],
                    "adj_factor": [1.0, 1.1],
                }
            ),
        )


class FailingProvider:
    name = "fake"

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        raise RuntimeError("provider unavailable")


def make_service(tmp_path: Path, provider=None) -> DataIngestionService:
    return DataIngestionService(
        provider=provider or FakeProvider(),
        raw_store=ParquetDatasetStore(tmp_path / "raw", "raw"),
        curated_store=ParquetDatasetStore(tmp_path / "curated", "curated"),
        validator=DataValidator(ValidationConfig()),
        snapshots_root=tmp_path / "snapshots",
        reports_root=tmp_path / "reports",
    )


def test_ingestion_writes_raw_curated_manifest_and_report(tmp_path: Path) -> None:
    service = make_service(tmp_path)

    result = service.update(date(2026, 7, 24), [Dataset.ADJUST_FACTORS])

    assert result.succeeded
    assert result.datasets[0].row_count == 1
    raw = service.raw_store.read(Dataset.ADJUST_FACTORS, "fake", date(2026, 7, 24))
    curated = service.curated_store.read(Dataset.ADJUST_FACTORS, "fake", date(2026, 7, 24))
    assert raw.height == 2
    assert curated.height == 1

    manifest = json.loads(
        (tmp_path / "snapshots/2026-07-24/manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["succeeded"] is True
    assert (tmp_path / "reports/data-quality/2026-07-24/report.md").exists()


def test_ingestion_failure_is_visible_in_manifest_and_quality_report(tmp_path: Path) -> None:
    service = make_service(tmp_path, FailingProvider())

    result = service.update(date(2026, 7, 24), [Dataset.ADJUST_FACTORS])

    assert not result.succeeded
    assert result.reports[0].issues[0].code == "ingestion_failed"
    quality = json.loads(
        (tmp_path / "reports/data-quality/2026-07-24/report.json").read_text(encoding="utf-8")
    )
    assert quality["passed"] is False
