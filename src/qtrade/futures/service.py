from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from qtrade.futures.domain import (
    DEFAULT_FUTURES_DATASETS,
    FuturesDataBatch,
    FuturesDataset,
    FuturesValidationReport,
)
from qtrade.futures.normalize import normalize_futures_dataset
from qtrade.futures.storage import FuturesParquetStore
from qtrade.futures.validation import (
    FuturesDataValidator,
    write_futures_validation_reports,
)


class FuturesDataSource(Protocol):
    @property
    def name(self) -> str: ...

    def query(self, operation: str, **params: Any) -> pl.DataFrame: ...


@dataclass
class FuturesDatasetUpdate:
    dataset: FuturesDataset
    status: str
    row_count: int = 0
    raw_path: str | None = None
    curated_path: str | None = None
    error: str | None = None


@dataclass
class FuturesUpdateResult:
    as_of_date: date
    datasets: list[FuturesDatasetUpdate] = field(default_factory=list)
    reports: list[FuturesValidationReport] = field(default_factory=list)
    quality_report: Path | None = None

    @property
    def succeeded(self) -> bool:
        return all(item.status in {"completed", "unavailable"} for item in self.datasets) and all(
            report.passed for report in self.reports
        )


@dataclass
class FuturesBackfillResult:
    start_date: date
    end_date: date
    trading_dates: int
    completed_dates: int = 0
    skipped_dates: int = 0
    failed_dates: list[date] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.failed_dates


FIELDS: dict[FuturesDataset, tuple[str, ...]] = {
    FuturesDataset.CONTRACTS: (
        "ts_code",
        "symbol",
        "name",
        "exchange",
        "fut_code",
        "multiplier",
        "trade_unit",
        "per_unit",
        "quote_unit",
        "quote_unit_desc",
        "d_mode_desc",
        "list_date",
        "delist_date",
        "d_month",
        "last_ddate",
        "trade_time_desc",
    ),
    FuturesDataset.DAILY: (
        "ts_code",
        "trade_date",
        "pre_settle",
        "open",
        "high",
        "low",
        "close",
        "settle",
        "vol",
        "amount",
        "oi",
    ),
    FuturesDataset.SETTLEMENTS: (
        "ts_code",
        "trade_date",
        "settle",
        "trading_fee_rate",
        "trading_fee",
        "long_margin_rate",
        "short_margin_rate",
    ),
    FuturesDataset.MAPPINGS: (
        "ts_code",
        "trade_date",
        "mapping_ts_code",
    ),
    FuturesDataset.LIMITS: (
        "trade_date",
        "ts_code",
        "up_limit",
        "down_limit",
        "m_ratio",
        "cont",
        "exchange",
    ),
    FuturesDataset.CALENDAR: (
        "exchange",
        "cal_date",
        "is_open",
        "pretrade_date",
    ),
}


class FuturesDataService:
    OPTIONAL_DATASETS = frozenset({FuturesDataset.LIMITS})

    def __init__(
        self,
        source: FuturesDataSource,
        exchanges: list[str],
        raw_store: FuturesParquetStore,
        curated_store: FuturesParquetStore,
        validator: FuturesDataValidator,
        reports_root: Path,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.source = source
        self.exchanges = exchanges
        self.raw_store = raw_store
        self.curated_store = curated_store
        self.validator = validator
        self.reports_root = Path(reports_root)
        self.secrets = tuple(value for value in secrets if value)

    def update(
        self,
        as_of_date: date,
        datasets: tuple[FuturesDataset, ...] = DEFAULT_FUTURES_DATASETS,
    ) -> FuturesUpdateResult:
        if as_of_date > date.today():
            raise ValueError("Futures update date cannot be in the future.")
        result = FuturesUpdateResult(as_of_date)
        contracts: pl.DataFrame | None = None
        for dataset in datasets:
            try:
                if dataset == FuturesDataset.CONTRACT_RULES:
                    if contracts is None:
                        contracts = self._fetch(
                            FuturesDataset.CONTRACTS,
                            as_of_date,
                        )
                    self._persist_dataset(
                        result,
                        dataset,
                        contracts,
                        as_of_date,
                        {"derived_from": FuturesDataset.CONTRACTS.value},
                        write_raw=False,
                    )
                    continue
                frame = self._fetch(dataset, as_of_date)
                if frame.is_empty():
                    raise RuntimeError(f"{dataset.value} returned no rows for {as_of_date}.")
                if dataset == FuturesDataset.CONTRACTS:
                    contracts = frame
                self._persist_dataset(
                    result,
                    dataset,
                    frame,
                    as_of_date,
                    self._request_metadata(dataset, as_of_date),
                )
            except Exception as exc:
                self._record_failure(result, dataset, exc)

        unavailable = {
            item.dataset.value: item.error or "unavailable"
            for item in result.datasets
            if item.status == "unavailable"
        }
        _, result.quality_report = write_futures_validation_reports(
            self.reports_root,
            as_of_date,
            result.reports,
            unavailable,
        )
        self._write_manifest(result)
        return result

    def backfill(
        self,
        start_date: date,
        end_date: date,
        datasets: tuple[FuturesDataset, ...] = (
            FuturesDataset.DAILY,
            FuturesDataset.SETTLEMENTS,
            FuturesDataset.MAPPINGS,
            FuturesDataset.LIMITS,
        ),
    ) -> FuturesBackfillResult:
        if start_date > end_date:
            raise ValueError("Futures backfill start date must not exceed end date.")
        dates = self._open_dates(start_date, end_date)
        result = FuturesBackfillResult(start_date, end_date, len(dates))
        for trading_date in dates:
            if all(
                self.curated_store.exists(
                    dataset,
                    self.source.name,
                    trading_date,
                )
                for dataset in datasets
            ):
                result.skipped_dates += 1
                continue
            update = self.update(trading_date, datasets)
            if update.succeeded:
                result.completed_dates += 1
            else:
                result.failed_dates.append(trading_date)
        return result

    def backfill_contract(
        self,
        ts_code: str,
        start_date: date,
        end_date: date,
    ) -> FuturesBackfillResult:
        if start_date > end_date:
            raise ValueError("Contract backfill start date must not exceed end date.")
        frame = self.source.query(
            "fut_daily",
            ts_code=ts_code.strip().upper(),
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            fields=",".join(FIELDS[FuturesDataset.DAILY]),
        )
        dates = self._open_dates(start_date, end_date)
        result = FuturesBackfillResult(start_date, end_date, len(dates))
        for trading_date in dates:
            day_frame = frame.filter(
                pl.col("trade_date").cast(pl.String) == trading_date.strftime("%Y%m%d")
            )
            if day_frame.is_empty():
                continue
            day_result = FuturesUpdateResult(trading_date)
            self._persist_dataset(
                day_result,
                FuturesDataset.DAILY,
                day_frame,
                trading_date,
                {
                    "ts_code": ts_code.strip().upper(),
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "bulk": True,
                },
                merge_existing=True,
            )
            if day_result.succeeded:
                result.completed_dates += 1
            else:
                result.failed_dates.append(trading_date)
        return result

    def _fetch(
        self,
        dataset: FuturesDataset,
        as_of_date: date,
    ) -> pl.DataFrame:
        ymd = as_of_date.strftime("%Y%m%d")
        fields = ",".join(FIELDS.get(dataset, ()))
        if dataset == FuturesDataset.CONTRACTS:
            return self._concat(
                self.source.query(
                    "fut_basic",
                    exchange=exchange,
                    fut_type="1",
                    fields=fields,
                )
                for exchange in self.exchanges
            )
        if dataset in {
            FuturesDataset.DAILY,
            FuturesDataset.SETTLEMENTS,
        }:
            operation = "fut_daily" if dataset == FuturesDataset.DAILY else "fut_settle"
            return self._concat(
                self.source.query(
                    operation,
                    trade_date=ymd,
                    exchange=exchange,
                    fields=fields,
                )
                for exchange in self.exchanges
            )
        if dataset == FuturesDataset.MAPPINGS:
            return self.source.query(
                "fut_mapping",
                trade_date=ymd,
                fields=fields,
            )
        if dataset == FuturesDataset.LIMITS:
            return self.source.query(
                "ft_limit",
                trade_date=ymd,
                fields=fields,
            )
        if dataset == FuturesDataset.CALENDAR:
            calendars: list[pl.DataFrame] = []
            fallback: pl.DataFrame | None = None
            for exchange in self.exchanges:
                frame = self.source.query(
                    "trade_cal",
                    exchange=exchange,
                    start_date=ymd,
                    end_date=ymd,
                    fields=fields,
                )
                if not frame.is_empty():
                    frame = frame.with_columns(
                        pl.lit(exchange).alias("exchange"),
                        pl.lit(exchange).alias("calendar_source_exchange"),
                    )
                    calendars.append(frame)
                    fallback = fallback if fallback is not None else frame
                elif fallback is not None:
                    calendars.append(fallback.with_columns(pl.lit(exchange).alias("exchange")))
            return self._concat(calendars)
        raise ValueError(f"Unsupported futures dataset: {dataset.value}")

    def _persist_dataset(
        self,
        result: FuturesUpdateResult,
        dataset: FuturesDataset,
        frame: pl.DataFrame,
        as_of_date: date,
        request: dict[str, Any],
        *,
        write_raw: bool = True,
        merge_existing: bool = False,
    ) -> None:
        raw_path: Path | None = None
        if write_raw:
            raw_frame = (
                self._merge_existing(
                    self.raw_store,
                    dataset,
                    as_of_date,
                    frame,
                )
                if merge_existing
                else frame
            )
            raw_path = self.raw_store.write(
                FuturesDataBatch(
                    dataset,
                    self.source.name,
                    as_of_date,
                    raw_frame,
                    request={**request, "merged_existing": merge_existing},
                )
            )
        curated = normalize_futures_dataset(dataset, frame, as_of_date)
        if merge_existing:
            curated = self._merge_existing(
                self.curated_store,
                dataset,
                as_of_date,
                curated,
            )
            curated = normalize_futures_dataset(
                dataset,
                curated,
                as_of_date,
            )
        curated_path = self.curated_store.write(
            FuturesDataBatch(
                dataset,
                self.source.name,
                as_of_date,
                curated,
                request={
                    **request,
                    "normalized": True,
                    "merged_existing": merge_existing,
                },
            )
        )
        report = self.validator.validate(dataset, as_of_date, curated)
        result.datasets.append(
            FuturesDatasetUpdate(
                dataset,
                "completed",
                curated.height,
                str(raw_path) if raw_path else None,
                str(curated_path),
            )
        )
        result.reports.append(report)

    def _merge_existing(
        self,
        store: FuturesParquetStore,
        dataset: FuturesDataset,
        as_of_date: date,
        frame: pl.DataFrame,
    ) -> pl.DataFrame:
        if not store.exists(dataset, self.source.name, as_of_date):
            return frame
        existing = store.read(dataset, self.source.name, as_of_date)
        return pl.concat([existing, frame], how="diagonal_relaxed")

    def _record_failure(
        self,
        result: FuturesUpdateResult,
        dataset: FuturesDataset,
        exc: Exception,
    ) -> None:
        status = "unavailable" if dataset in self.OPTIONAL_DATASETS else "failed"
        result.datasets.append(
            FuturesDatasetUpdate(
                dataset,
                status,
                error=self._safe_error(str(exc)),
            )
        )

    def _open_dates(self, start_date: date, end_date: date) -> list[date]:
        calendar = self.source.query(
            "trade_cal",
            exchange=self.exchanges[0],
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            fields="exchange,cal_date,is_open,pretrade_date",
        )
        if {"cal_date", "is_open"} <= set(calendar.columns):
            return sorted(
                date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:]}")
                for value in calendar.filter(pl.col("is_open").cast(pl.Int8, strict=False) == 1)
                .get_column("cal_date")
                .cast(pl.String)
                .to_list()
            )
        values: list[date] = []
        current = start_date
        while current <= end_date:
            if current.weekday() < 5:
                values.append(current)
            current += timedelta(days=1)
        return values

    def _request_metadata(
        self,
        dataset: FuturesDataset,
        as_of_date: date,
    ) -> dict[str, Any]:
        return {
            "as_of_date": as_of_date.isoformat(),
            "exchanges": self.exchanges,
            "fields": list(FIELDS.get(dataset, ())),
        }

    def _safe_error(self, message: str) -> str:
        for secret in self.secrets:
            message = message.replace(secret, "***")
        return message[:500]

    @staticmethod
    def _concat(frames) -> pl.DataFrame:
        values = [frame for frame in frames if not frame.is_empty()]
        return pl.concat(values, how="diagonal_relaxed") if values else pl.DataFrame()

    def _write_manifest(self, result: FuturesUpdateResult) -> None:
        directory = self.reports_root / "futures" / "manifests" / result.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "as_of_date": result.as_of_date.isoformat(),
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
        path = directory / "manifest.json"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
