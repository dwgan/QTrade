from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import polars as pl

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

LOGGER = logging.getLogger(__name__)


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


@dataclass
class BackfillResult:
    start_date: date
    end_date: date
    trading_dates: int
    completed_dates: int = 0
    skipped_dates: int = 0
    failed_dates: list[date] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.failed_dates


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

    def backfill(
        self,
        start_date: date,
        end_date: date,
        datasets: list[Dataset],
    ) -> BackfillResult:
        if start_date > end_date:
            raise ValueError("Backfill start date must not be after end date.")

        calendar_batch = self.provider.fetch(
            Dataset.TRADE_CALENDAR,
            FetchRequest(
                as_of_date=end_date,
                start_date=start_date,
                end_date=end_date,
            ),
        )
        self.raw_store.write(calendar_batch)
        curated_calendar = normalize_dataset(Dataset.TRADE_CALENDAR, calendar_batch.frame)
        self.curated_store.write(
            DataBatch(
                dataset=Dataset.TRADE_CALENDAR,
                provider=calendar_batch.provider,
                as_of_date=end_date,
                frame=curated_calendar,
                fetched_at=calendar_batch.fetched_at,
                request={**calendar_batch.request, "normalized": True},
            )
        )
        if {"cal_date", "is_open"} - set(curated_calendar.columns):
            raise ValueError("Trade calendar is missing cal_date or is_open.")

        open_dates = (
            curated_calendar.with_columns(
                pl.col("cal_date")
                .cast(pl.String)
                .str.replace_all("-", "")
                .str.strptime(pl.Date, "%Y%m%d", strict=False),
                pl.col("is_open").cast(pl.Int8, strict=False),
            )
            .filter((pl.col("is_open") == 1) & pl.col("cal_date").is_between(start_date, end_date))
            .get_column("cal_date")
            .to_list()
        )
        daily_datasets = [dataset for dataset in datasets if dataset != Dataset.TRADE_CALENDAR]
        result = BackfillResult(start_date, end_date, trading_dates=len(open_dates))
        for position, trading_date in enumerate(open_dates, start=1):
            pending = [
                dataset
                for dataset in daily_datasets
                if not self.curated_store.exists(dataset, self.provider.name, trading_date)
            ]
            if not pending:
                result.skipped_dates += 1
                LOGGER.debug(
                    "Backfill %s/%s skipped %s",
                    position,
                    len(open_dates),
                    trading_date,
                )
                continue
            LOGGER.info(
                "Backfill %s/%s updating %s (%s)",
                position,
                len(open_dates),
                trading_date,
                ", ".join(dataset.value for dataset in pending),
            )
            update_result = self.update(trading_date, pending)
            if update_result.succeeded:
                result.completed_dates += 1
            else:
                result.failed_dates.append(trading_date)
                LOGGER.warning("Backfill failed for %s", trading_date)
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
