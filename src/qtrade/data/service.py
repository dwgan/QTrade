from __future__ import annotations

import json
import logging
import os
import uuid
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
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


@dataclass(frozen=True)
class CoverageItem:
    dataset: Dataset
    frequency: str
    expected_dates: int
    existing_dates: int
    missing_dates: tuple[date, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_dates


@dataclass(frozen=True)
class CoverageResult:
    start_date: date
    end_date: date
    items: tuple[CoverageItem, ...]

    @property
    def complete(self) -> bool:
        return all(item.complete for item in self.items)


class StoredDataValidationService:
    def __init__(
        self,
        curated_store: ParquetDatasetStore,
        validator: DataValidator,
        provider: str,
        reports_root: Path,
    ) -> None:
        self.curated_store = curated_store
        self.validator = validator
        self.provider = provider
        self.reports_root = Path(reports_root)

    def validate(
        self,
        as_of_date: date,
        datasets: list[Dataset],
    ) -> list[ValidationReport]:
        reports: list[ValidationReport] = []
        for dataset in datasets:
            frame = self.curated_store.read(dataset, self.provider, as_of_date)
            reports.append(self.validator.validate(dataset, as_of_date, frame))
        write_validation_reports(self.reports_root, as_of_date, reports)
        return reports


class DataIngestionService:
    RESEARCH_FREQUENCIES = {
        Dataset.DAILY_PRICES: "daily",
        Dataset.ADJUST_FACTORS: "daily",
        Dataset.STOCK_LIMIT: "daily",
        Dataset.INDEX_DAILY: "daily",
        Dataset.DAILY_BASIC: "month_end",
        Dataset.INDEX_MEMBERS: "month_end",
        Dataset.FINANCIAL_INDICATORS: "month_end",
    }

    def __init__(
        self,
        provider: DataProvider,
        raw_store: ParquetDatasetStore,
        curated_store: ParquetDatasetStore,
        validator: DataValidator,
        snapshots_root: Path,
        reports_root: Path,
        parallel_requests: int = 1,
        backfill_parallel_dates: int = 1,
    ) -> None:
        self.provider = provider
        self.raw_store = raw_store
        self.curated_store = curated_store
        self.validator = validator
        self.snapshots_root = Path(snapshots_root)
        self.reports_root = Path(reports_root)
        self.parallel_requests = parallel_requests
        self.backfill_parallel_dates = backfill_parallel_dates

    def update(self, as_of_date: date, datasets: list[Dataset]) -> UpdateResult:
        result = UpdateResult(as_of_date=as_of_date)

        workers = min(self.parallel_requests, len(datasets))
        if workers <= 1:
            completed = [
                self._update_dataset(as_of_date, dataset) for dataset in datasets
            ]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                completed = list(
                    executor.map(
                        lambda dataset: self._update_dataset(as_of_date, dataset),
                        datasets,
                    )
                )
        for update, report in completed:
            result.datasets.append(update)
            result.reports.append(report)

        write_validation_reports(self.reports_root, as_of_date, result.reports)
        self._write_manifest(result)
        return result

    def _update_dataset(
        self,
        as_of_date: date,
        dataset: Dataset,
    ) -> tuple[DatasetUpdate, ValidationReport]:
        try:
            raw_batch = self.provider.fetch(
                dataset,
                FetchRequest(as_of_date=as_of_date),
            )
            raw_path = self.raw_store.write(raw_batch)
            curated_frame = normalize_dataset(dataset, raw_batch.frame, as_of_date)
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
            return (
                DatasetUpdate(
                    dataset=dataset,
                    status="completed",
                    row_count=curated_frame.height,
                    raw_path=str(raw_path),
                    curated_path=str(curated_path),
                ),
                report,
            )
        except Exception as exc:
            return (
                DatasetUpdate(dataset=dataset, status="failed", error=str(exc)),
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
                ),
            )

    def backfill(
        self,
        start_date: date,
        end_date: date,
        datasets: list[Dataset],
        frequency: str = "daily",
    ) -> BackfillResult:
        if start_date > end_date:
            raise ValueError("Backfill start date must not be after end date.")
        if frequency not in {"daily", "month_end"}:
            raise ValueError("Backfill frequency must be daily or month_end.")

        calendar_batch = self.provider.fetch(
            Dataset.TRADE_CALENDAR,
            FetchRequest(
                as_of_date=end_date,
                start_date=start_date,
                end_date=end_date,
            ),
        )
        self.raw_store.write(calendar_batch)
        curated_calendar = normalize_dataset(
            Dataset.TRADE_CALENDAR,
            calendar_batch.frame,
            end_date,
        )
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
        if frequency == "month_end":
            month_ends: dict[tuple[int, int], date] = {}
            for trading_date in open_dates:
                month_ends[(trading_date.year, trading_date.month)] = trading_date
            open_dates = list(month_ends.values())
        daily_datasets = [dataset for dataset in datasets if dataset != Dataset.TRADE_CALENDAR]
        result = BackfillResult(start_date, end_date, trading_dates=len(open_dates))
        work: list[tuple[int, date, list[Dataset]]] = []
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
            work.append((position, trading_date, pending))

        def update_date(item: tuple[int, date, list[Dataset]]):
            position, trading_date, pending = item
            LOGGER.info(
                "Backfill %s/%s updating %s (%s)",
                position,
                len(open_dates),
                trading_date,
                ", ".join(dataset.value for dataset in pending),
            )
            return trading_date, self.update(trading_date, pending)

        workers = min(self.backfill_parallel_dates, len(work))
        if workers <= 1:
            completed = [update_date(item) for item in work]
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                completed = list(executor.map(update_date, work))
        for trading_date, update_result in completed:
            if update_result.succeeded:
                result.completed_dates += 1
            else:
                result.failed_dates.append(trading_date)
                LOGGER.warning("Backfill failed for %s", trading_date)
        return result

    def backfill_index_daily(
        self,
        start_date: date,
        end_date: date,
    ) -> BackfillResult:
        if start_date > end_date:
            raise ValueError("Backfill start date must not be after end date.")
        dataset = Dataset.INDEX_DAILY
        raw_batch = self.provider.fetch(
            dataset,
            FetchRequest(
                as_of_date=end_date,
                start_date=start_date,
                end_date=end_date,
            ),
        )
        self.raw_store.write(raw_batch)
        if "trade_date" not in raw_batch.frame.columns:
            raise ValueError("Bulk index daily response is missing trade_date.")
        prepared = raw_batch.frame.with_columns(
            pl.col("trade_date")
            .cast(pl.String)
            .str.replace_all("-", "")
            .str.strptime(pl.Date, "%Y%m%d", strict=False)
            .alias("_partition_date")
        ).drop_nulls("_partition_date")
        dates = prepared.get_column("_partition_date").unique().sort().to_list()
        result = BackfillResult(start_date, end_date, trading_dates=len(dates))
        for partition_date in dates:
            if self.curated_store.exists(dataset, self.provider.name, partition_date):
                result.skipped_dates += 1
                continue
            frame = prepared.filter(
                pl.col("_partition_date") == partition_date
            ).drop("_partition_date")
            curated = normalize_dataset(dataset, frame, partition_date)
            report = self.validator.validate(dataset, partition_date, curated)
            if not report.passed:
                result.failed_dates.append(partition_date)
                continue
            self.curated_store.write(
                DataBatch(
                    dataset=dataset,
                    provider=raw_batch.provider,
                    as_of_date=partition_date,
                    frame=curated,
                    fetched_at=raw_batch.fetched_at,
                    request={**raw_batch.request, "normalized": True, "bulk": True},
                )
            )
            result.completed_dates += 1
        return result

    def update_financial_indicators(
        self,
        as_of_date: date,
        periods: tuple[str, ...],
    ) -> UpdateResult:
        result = UpdateResult(as_of_date=as_of_date)
        dataset = Dataset.FINANCIAL_INDICATORS
        try:
            raw_batch = self.provider.fetch(
                dataset,
                FetchRequest(as_of_date=as_of_date, periods=periods),
            )
            raw_path = self.raw_store.write(raw_batch)
            curated_frame = normalize_dataset(dataset, raw_batch.frame, as_of_date)
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
            result.datasets.append(DatasetUpdate(dataset=dataset, status="failed", error=str(exc)))
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

    @staticmethod
    def financial_periods(
        start_date: date,
        end_date: date,
        lookback_quarters: int = 12,
    ) -> tuple[str, ...]:
        if start_date > end_date:
            raise ValueError("Financial backfill start date must not be after end date.")
        if lookback_quarters < 1:
            raise ValueError("Financial lookback quarters must be positive.")
        start_quarter = start_date.year * 4 + (start_date.month - 1) // 3
        end_quarter = end_date.year * 4 + (end_date.month - 1) // 3
        periods: list[str] = []
        for quarter_index in range(
            start_quarter - lookback_quarters,
            end_quarter + 1,
        ):
            year, zero_based_quarter = divmod(quarter_index, 4)
            month = (zero_based_quarter + 1) * 3
            period_date = date(year, month, monthrange(year, month)[1])
            if period_date <= end_date:
                periods.append(period_date.strftime("%Y%m%d"))
        return tuple(periods)

    def backfill_financial_snapshots(
        self,
        start_date: date,
        end_date: date,
        lookback_quarters: int = 12,
    ) -> BackfillResult:
        periods = self.financial_periods(
            start_date,
            end_date,
            lookback_quarters,
        )
        calendar_batch = self.provider.fetch(
            Dataset.TRADE_CALENDAR,
            FetchRequest(
                as_of_date=end_date,
                start_date=start_date,
                end_date=end_date,
            ),
        )
        calendar = normalize_dataset(
            Dataset.TRADE_CALENDAR,
            calendar_batch.frame,
            end_date,
        )
        if {"cal_date", "is_open"} - set(calendar.columns):
            raise ValueError("Trade calendar is missing cal_date or is_open.")
        open_dates = (
            calendar.with_columns(
                pl.col("cal_date")
                .cast(pl.String)
                .str.replace_all("-", "")
                .str.strptime(pl.Date, "%Y%m%d", strict=False),
                pl.col("is_open").cast(pl.Int8, strict=False),
            )
            .filter(
                (pl.col("is_open") == 1)
                & pl.col("cal_date").is_between(start_date, end_date)
            )
            .get_column("cal_date")
            .to_list()
        )
        month_ends: dict[tuple[int, int], date] = {}
        for trading_date in open_dates:
            month_ends[(trading_date.year, trading_date.month)] = trading_date
        snapshot_dates = list(month_ends.values())
        result = BackfillResult(
            start_date,
            end_date,
            trading_dates=len(snapshot_dates),
        )
        pending_dates = [
            snapshot_date
            for snapshot_date in snapshot_dates
            if not self.curated_store.exists(
                Dataset.FINANCIAL_INDICATORS,
                self.provider.name,
                snapshot_date,
            )
        ]
        result.skipped_dates = len(snapshot_dates) - len(pending_dates)
        if not pending_dates:
            return result

        raw_batch = self.provider.fetch(
            Dataset.FINANCIAL_INDICATORS,
            FetchRequest(as_of_date=end_date, periods=periods),
        )
        self.raw_store.write(raw_batch)
        normalized = normalize_dataset(
            Dataset.FINANCIAL_INDICATORS,
            raw_batch.frame,
            end_date,
        )
        if "available_from" not in normalized.columns:
            raise ValueError("Financial indicators are missing available_from.")
        prepared = normalized.with_columns(
            pl.col("available_from")
            .cast(pl.String)
            .str.replace_all("-", "")
            .str.strptime(pl.Date, "%Y%m%d", strict=False)
            .alias("_available_date")
        )
        for snapshot_date in pending_dates:
            snapshot = (
                prepared.filter(pl.col("_available_date") <= snapshot_date)
                .sort(["ts_code", "_available_date", "end_date"])
                .unique(subset=["ts_code"], keep="last")
                .drop("_available_date")
            )
            report = self.validator.validate(
                Dataset.FINANCIAL_INDICATORS,
                snapshot_date,
                snapshot,
            )
            if not report.passed:
                result.failed_dates.append(snapshot_date)
                continue
            self.curated_store.write(
                DataBatch(
                    dataset=Dataset.FINANCIAL_INDICATORS,
                    provider=raw_batch.provider,
                    as_of_date=snapshot_date,
                    frame=snapshot,
                    fetched_at=raw_batch.fetched_at,
                    request={
                        **raw_batch.request,
                        "normalized": True,
                        "reconstructed_as_of": snapshot_date.isoformat(),
                        "latest_per_security": True,
                    },
                )
            )
            result.completed_dates += 1
        return result

    def research_coverage(
        self,
        start_date: date,
        end_date: date,
    ) -> CoverageResult:
        if start_date > end_date:
            raise ValueError("Coverage start date must not be after end date.")
        _, calendar = self.curated_store.read_latest(
            Dataset.TRADE_CALENDAR,
            self.provider.name,
        )
        if {"cal_date", "is_open"} - set(calendar.columns):
            raise ValueError("Trade calendar is missing cal_date or is_open.")
        daily_dates = (
            calendar.with_columns(
                pl.col("cal_date")
                .cast(pl.String)
                .str.replace_all("-", "")
                .str.strptime(pl.Date, "%Y%m%d", strict=False),
                pl.col("is_open").cast(pl.Int8, strict=False),
            )
            .filter(
                (pl.col("is_open") == 1)
                & pl.col("cal_date").is_between(start_date, end_date)
            )
            .get_column("cal_date")
            .unique()
            .sort()
            .to_list()
        )
        month_ends: dict[tuple[int, int], date] = {}
        for trading_date in daily_dates:
            month_ends[(trading_date.year, trading_date.month)] = trading_date
        expected_by_frequency = {
            "daily": set(daily_dates),
            "month_end": set(month_ends.values()),
        }
        items: list[CoverageItem] = []
        for dataset, frequency in self.RESEARCH_FREQUENCIES.items():
            expected = expected_by_frequency[frequency]
            existing = self.curated_store.partition_dates(
                dataset,
                self.provider.name,
                start_date,
                end_date,
            )
            missing = tuple(sorted(expected - existing))
            items.append(
                CoverageItem(
                    dataset=dataset,
                    frequency=frequency,
                    expected_dates=len(expected),
                    existing_dates=len(expected & existing),
                    missing_dates=missing,
                )
            )
        return CoverageResult(start_date, end_date, tuple(items))

    def validate_existing(
        self, as_of_date: date, datasets: list[Dataset]
    ) -> list[ValidationReport]:
        return StoredDataValidationService(
            self.curated_store,
            self.validator,
            self.provider.name,
            self.reports_root,
        ).validate(as_of_date, datasets)

    def _write_manifest(self, result: UpdateResult) -> Path:
        directory = self.snapshots_root / result.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "manifest.json"
        temporary = directory / f".manifest.{uuid.uuid4().hex}.tmp"
        existing_datasets: dict[str, dict] = {}
        if target.exists():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                existing_datasets = {item["dataset"]: item for item in existing.get("datasets", [])}
            except (json.JSONDecodeError, KeyError, TypeError):
                existing_datasets = {}
        reports_by_dataset = {report.dataset: report for report in result.reports}
        current_datasets = {
            item.dataset.value: {
                "dataset": item.dataset.value,
                "status": item.status,
                "validation_passed": (
                    reports_by_dataset[item.dataset].passed
                    if item.dataset in reports_by_dataset
                    else None
                ),
                "row_count": item.row_count,
                "raw_path": item.raw_path,
                "curated_path": item.curated_path,
                "error": item.error,
            }
            for item in result.datasets
        }
        merged_datasets = {**existing_datasets, **current_datasets}
        payload = {
            "as_of_date": result.as_of_date.isoformat(),
            "provider": self.provider.name,
            "created_at": datetime.now().isoformat(),
            "succeeded": all(
                item.get("status") == "completed"
                and item.get("validation_passed", True) is not False
                for item in merged_datasets.values()
            ),
            "datasets": [merged_datasets[name] for name in sorted(merged_datasets)],
        }
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
        return target
