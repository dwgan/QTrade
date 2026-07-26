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


class BackfillProvider:
    name = "fake"

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        if dataset == Dataset.TRADE_CALENDAR:
            frame = pl.DataFrame(
                {
                    "exchange": ["SSE", "SSE", "SSE"],
                    "cal_date": ["20260723", "20260724", "20260725"],
                    "is_open": [1, 1, 0],
                    "pretrade_date": ["20260722", "20260723", "20260724"],
                }
            )
        else:
            assert dataset == Dataset.ADJUST_FACTORS
            frame = pl.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "trade_date": [request.as_of_date.strftime("%Y%m%d")],
                    "adj_factor": [1.0],
                }
            )
        return DataBatch(
            dataset=dataset,
            provider=self.name,
            as_of_date=request.as_of_date,
            frame=frame,
        )


class FinancialProvider:
    name = "fake"

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        assert dataset == Dataset.FINANCIAL_INDICATORS
        assert request.periods == ("20251231", "20260331")
        return DataBatch(
            dataset=dataset,
            provider=self.name,
            as_of_date=request.as_of_date,
            frame=pl.DataFrame(
                {
                    "ts_code": ["000001.SZ"],
                    "ann_date": ["20260430"],
                    "end_date": ["20260331"],
                    "roe": [10.0],
                    "roe_dt": [9.0],
                    "roic": [8.0],
                    "grossprofit_margin": [30.0],
                    "netprofit_margin": [15.0],
                    "ocfps": [1.0],
                    "eps": [0.8],
                    "debt_to_assets": [40.0],
                }
            ),
        )


class FinancialBackfillProvider:
    name = "fake"

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        if dataset == Dataset.TRADE_CALENDAR:
            frame = pl.DataFrame(
                {
                    "exchange": ["SSE", "SSE", "SSE", "SSE"],
                    "cal_date": ["20260130", "20260131", "20260227", "20260228"],
                    "is_open": [1, 0, 1, 0],
                    "pretrade_date": ["20260129", "20260130", "20260226", "20260227"],
                }
            )
        else:
            assert dataset == Dataset.FINANCIAL_INDICATORS
            assert request.periods[-1] == "20251231"
            frame = pl.DataFrame(
                {
                    "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ"],
                    "ann_date": ["20260120", "20260215", "20260301"],
                    "end_date": ["20250930", "20251231", "20251231"],
                    "roe": [9.0, 10.0, 11.0],
                    "roe_dt": [8.0, 9.0, 10.0],
                    "roic": [7.0, 8.0, 9.0],
                    "grossprofit_margin": [28.0, 30.0, 31.0],
                    "netprofit_margin": [14.0, 15.0, 16.0],
                    "ocfps": [0.9, 1.0, 1.1],
                    "eps": [0.7, 0.8, 0.9],
                    "debt_to_assets": [42.0, 40.0, 38.0],
                }
            )
        return DataBatch(
            dataset=dataset,
            provider=self.name,
            as_of_date=request.as_of_date,
            frame=frame,
        )


class InvalidFinancialProvider(FinancialProvider):
    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        batch = super().fetch(dataset, request)
        return DataBatch(
            dataset=batch.dataset,
            provider=batch.provider,
            as_of_date=batch.as_of_date,
            frame=batch.frame.with_columns(pl.lit(None).cast(pl.String).alias("ts_code")),
        )


class BulkIndexProvider:
    name = "fake"

    def fetch(self, dataset: Dataset, request: FetchRequest) -> DataBatch:
        assert dataset == Dataset.INDEX_DAILY
        assert request.start_date == date(2026, 7, 23)
        assert request.end_date == date(2026, 7, 24)
        return DataBatch(
            dataset=dataset,
            provider=self.name,
            as_of_date=request.as_of_date,
            frame=pl.DataFrame(
                {
                    "ts_code": ["000300.SH", "000300.SH"],
                    "trade_date": ["20260723", "20260724"],
                    "open": [100.0, 101.0],
                    "high": [102.0, 103.0],
                    "low": [99.0, 100.0],
                    "close": [101.0, 102.0],
                    "pre_close": [100.0, 101.0],
                    "vol": [1000.0, 1100.0],
                    "amount": [10000.0, 11000.0],
                }
            ),
        )


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
    assert manifest["datasets"][0]["validation_passed"] is True
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


def test_backfill_uses_open_dates_and_skips_existing_partitions(tmp_path: Path) -> None:
    service = make_service(tmp_path, BackfillProvider())

    first = service.backfill(
        date(2026, 7, 23),
        date(2026, 7, 25),
        [Dataset.ADJUST_FACTORS],
    )
    second = service.backfill(
        date(2026, 7, 23),
        date(2026, 7, 25),
        [Dataset.ADJUST_FACTORS],
    )

    assert first.succeeded
    assert first.trading_dates == 2
    assert first.completed_dates == 2
    assert second.completed_dates == 0
    assert second.skipped_dates == 2


def test_month_end_backfill_uses_only_last_open_date(tmp_path: Path) -> None:
    service = make_service(tmp_path, BackfillProvider())

    result = service.backfill(
        date(2026, 7, 23),
        date(2026, 7, 25),
        [Dataset.ADJUST_FACTORS],
        frequency="month_end",
    )

    assert result.trading_dates == 1
    assert result.completed_dates == 1
    assert service.curated_store.exists(
        Dataset.ADJUST_FACTORS,
        "fake",
        date(2026, 7, 24),
    )


def test_financial_snapshot_update_persists_requested_periods(tmp_path: Path) -> None:
    service = make_service(tmp_path, FinancialProvider())

    result = service.update_financial_indicators(date(2026, 7, 24), ("20251231", "20260331"))

    assert result.succeeded
    stored = service.curated_store.read(Dataset.FINANCIAL_INDICATORS, "fake", date(2026, 7, 24))
    assert stored.height == 1


def test_financial_backfill_builds_point_in_time_month_end_snapshots(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path, FinancialBackfillProvider())

    result = service.backfill_financial_snapshots(
        date(2026, 1, 1),
        date(2026, 2, 28),
        lookback_quarters=4,
    )

    january = service.curated_store.read(
        Dataset.FINANCIAL_INDICATORS,
        "fake",
        date(2026, 1, 30),
    )
    february = service.curated_store.read(
        Dataset.FINANCIAL_INDICATORS,
        "fake",
        date(2026, 2, 27),
    )
    assert result.trading_dates == 2
    assert result.completed_dates == 2
    assert january.get_column("end_date").to_list() == ["20250930"]
    assert february.get_column("end_date").to_list() == ["20251231"]
    assert "000002.SZ" not in february.get_column("ts_code").to_list()


def test_financial_periods_include_configured_history() -> None:
    periods = DataIngestionService.financial_periods(
        date(2026, 1, 1),
        date(2026, 7, 24),
        lookback_quarters=4,
    )

    assert periods == (
        "20250331",
        "20250630",
        "20250930",
        "20251231",
        "20260331",
        "20260630",
    )


def test_research_coverage_distinguishes_daily_and_month_end(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path, BackfillProvider())
    service.backfill(
        date(2026, 7, 23),
        date(2026, 7, 25),
        [Dataset.ADJUST_FACTORS],
    )

    result = service.research_coverage(
        date(2026, 7, 23),
        date(2026, 7, 25),
    )
    by_dataset = {item.dataset: item for item in result.items}
    assert by_dataset[Dataset.ADJUST_FACTORS].complete
    assert by_dataset[Dataset.ADJUST_FACTORS].expected_dates == 2
    assert by_dataset[Dataset.DAILY_BASIC].expected_dates == 1
    assert by_dataset[Dataset.DAILY_BASIC].missing_dates == (date(2026, 7, 24),)
    assert not result.complete


def test_manifest_merges_multiple_updates_for_same_date(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.update(date(2026, 7, 24), [Dataset.ADJUST_FACTORS])
    service.provider = FinancialProvider()

    service.update_financial_indicators(date(2026, 7, 24), ("20251231", "20260331"))

    manifest = json.loads(
        (tmp_path / "snapshots/2026-07-24/manifest.json").read_text(encoding="utf-8")
    )
    assert {item["dataset"] for item in manifest["datasets"]} == {
        "adjust_factors",
        "financial_indicators",
    }


def test_manifest_reports_validation_failure_for_completed_dataset(tmp_path: Path) -> None:
    service = make_service(tmp_path, InvalidFinancialProvider())

    result = service.update_financial_indicators(
        date(2026, 7, 24), ("20251231", "20260331")
    )

    manifest = json.loads(
        (tmp_path / "snapshots/2026-07-24/manifest.json").read_text(encoding="utf-8")
    )
    financials = manifest["datasets"][0]
    assert not result.succeeded
    assert financials["status"] == "completed"
    assert financials["validation_passed"] is False
    assert manifest["succeeded"] is False


def test_bulk_index_backfill_splits_range_into_daily_partitions(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path, BulkIndexProvider())

    result = service.backfill_index_daily(
        date(2026, 7, 23),
        date(2026, 7, 24),
    )

    assert result.completed_dates == 2
    assert service.curated_store.exists(
        Dataset.INDEX_DAILY,
        "fake",
        date(2026, 7, 23),
    )
