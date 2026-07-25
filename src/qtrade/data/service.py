from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from qtrade.data.normalize import normalize_dataset
from qtrade.data.providers.base import DataProvider
from qtrade.data.storage import ParquetDatasetStore
from qtrade.data.validation import DataValidator, write_validation_reports
from qtrade.domain import (
    DataBatch,
    Dataset,
    FetchRequest,
    Severity,
    ValidationIssue,
    ValidationReport,
)


@dataclass
class DatasetUpdate:
    dataset: Dataset
    status: str
    row_count: int = 0
    raw_path: str | None = None
    curated_path: str | None = None
    error: str | None = None


@dataclass
class UpdateResult:
    as_of_date: date
    datasets: list[DatasetUpdate] = field(default_factory=list)
    reports: list[ValidationReport] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return all(item.status == "completed" for item in self.datasets) and all(
            report.passed for report in self.reports
        )


class DataIngestionService:
    def __init__(
        self,
        provider: DataProvider,
        raw_store: ParquetDatasetStore,
        curated_store: ParquetDatasetStore,
        validator: DataValidator,
        snapshots_root: Path,
        reports_root: Path,
    ) -> None:
        self.provider = provider
        self.raw_store = raw_store
        self.curated_store = curated_store
        self.validator = validator
        self.snapshots_root = Path(snapshots_root)
        self.reports_root = Path(reports_root)

    def update(self, as_of_date: date, datasets: list[Dataset]) -> UpdateResult:
        result = UpdateResult(as_of_date=as_of_date)

        for dataset in datasets:
            try:
                raw_batch = self.provider.fetch(dataset, FetchRequest(as_of_date=as_of_date))
                raw_path = self.raw_store.write(raw_batch)

                curated_frame = normalize_dataset(dataset, raw_batch.frame)
                curated_batch = DataBatch(
                    dataset=dataset,
                    provider=raw_batch.provider,
                    as_of_date=as_of_date,
                    frame=curated_frame,
                    fetched_at=raw_batch.fetched_at,
                    request={**raw_batch.request, "normalized": True},
                )
                curated_path = self.curated_store.write(curated_batch)
                report = self.validator.validate(dataset, as_of_date, curated_frame)

                result.datasets.append(
                    DatasetUpdate(
                        dataset=dataset,
                        status="completed",
                        row_count=curated_frame.height,
                        raw_path=str(raw_path),
                        curated_path=str(curated_path),
                    )
                )
                result.reports.append(report)
            except Exception as exc:
                result.datasets.append(
                    DatasetUpdate(dataset=dataset, status="failed", error=str(exc))
                )
                result.reports.append(
                    ValidationReport(
                        dataset=dataset,
                        as_of_date=as_of_date,
                        row_count=0,
                        issues=[
                            ValidationIssue(
                                Severity.ERROR,
                                "ingestion_failed",
                                f"Dataset update failed: {exc}",
                            )
                        ],
                    )
                )

        write_validation_reports(self.reports_root, as_of_date, result.reports)
        self._write_manifest(result)
        return result

    def validate_existing(
        self, as_of_date: date, datasets: list[Dataset]
    ) -> list[ValidationReport]:
        reports: list[ValidationReport] = []
        for dataset in datasets:
            frame = self.curated_store.read(dataset, self.provider.name, as_of_date)
            reports.append(self.validator.validate(dataset, as_of_date, frame))
        write_validation_reports(self.reports_root, as_of_date, reports)
        return reports

    def _write_manifest(self, result: UpdateResult) -> Path:
        directory = self.snapshots_root / result.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "manifest.json"
        temporary = directory / f".manifest.{uuid.uuid4().hex}.tmp"
        payload = {
            "as_of_date": result.as_of_date.isoformat(),
            "provider": self.provider.name,
            "created_at": datetime.now().isoformat(),
            "succeeded": result.succeeded,
            "datasets": [
                {
                    "dataset": item.dataset.value,
                    "status": item.status,
                    "row_count": item.row_count,
                    "raw_path": item.raw_path,
                    "curated_path": item.curated_path,
                    "error": item.error,
                }
                for item in result.datasets
            ],
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
        return target
