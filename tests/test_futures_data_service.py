from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from qtrade.futures.domain import FuturesDataset
from qtrade.futures.service import FuturesDataService
from qtrade.futures.storage import FuturesParquetStore
from qtrade.futures.validation import FuturesDataValidator


class FakeFuturesDataSource:
    name = "fake"

    def __init__(self, *, limits_available: bool = True) -> None:
        self.limits_available = limits_available

    def query(self, operation: str, **params: Any) -> pl.DataFrame:
        if operation == "fut_basic":
            return pl.DataFrame(
                {
                    "ts_code": ["CU2608.SHF"],
                    "symbol": ["CU2608"],
                    "name": ["沪铜2608"],
                    "exchange": ["SHFE"],
                    "fut_code": ["CU"],
                    "multiplier": [5.0],
                    "trade_unit": ["吨/手"],
                    "per_unit": [5.0],
                    "quote_unit": ["元（人民币）/吨"],
                    "quote_unit_desc": ["元/吨"],
                    "d_mode_desc": ["实物交割"],
                    "list_date": ["20250818"],
                    "delist_date": ["20260817"],
                    "d_month": ["202608"],
                    "last_ddate": ["20260820"],
                    "trade_time_desc": ["21:00-01:00; 09:00-15:00"],
                }
            )
        if operation == "fut_daily":
            dates = ["20260723", "20260724"] if "start_date" in params else [params["trade_date"]]
            return pl.DataFrame(
                {
                    "ts_code": ["CU2608.SHF"] * len(dates),
                    "trade_date": dates,
                    "pre_settle": [78000.0] * len(dates),
                    "open": [78100.0] * len(dates),
                    "high": [79000.0] * len(dates),
                    "low": [77900.0] * len(dates),
                    "close": [78800.0] * len(dates),
                    "settle": [78600.0] * len(dates),
                    "vol": [10000.0] * len(dates),
                    "amount": [100000.0] * len(dates),
                    "oi": [12000.0] * len(dates),
                }
            )
        if operation == "fut_settle":
            return pl.DataFrame(
                {
                    "ts_code": ["CU2608.SHF"],
                    "trade_date": [params["trade_date"]],
                    "settle": [78600.0],
                    "trading_fee_rate": [0.00005],
                    "trading_fee": [0.0],
                    "long_margin_rate": [0.1],
                    "short_margin_rate": [0.1],
                }
            )
        if operation == "fut_mapping":
            return pl.DataFrame(
                {
                    "ts_code": ["CU.SHF"],
                    "trade_date": [params["trade_date"]],
                    "mapping_ts_code": ["CU2608.SHF"],
                }
            )
        if operation == "ft_limit":
            if not self.limits_available:
                raise RuntimeError("unsupported secret-value")
            return pl.DataFrame(
                {
                    "trade_date": [params["trade_date"]],
                    "ts_code": ["CU2608.SHF"],
                    "up_limit": [85000.0],
                    "down_limit": [71000.0],
                    "m_ratio": [10.0],
                    "cont": ["CU"],
                    "exchange": ["SHFE"],
                }
            )
        if operation == "trade_cal":
            start = params["start_date"]
            end = params["end_date"]
            dates = [value for value in ("20260723", "20260724") if start <= value <= end]
            return pl.DataFrame(
                {
                    "exchange": ["SHFE"] * len(dates),
                    "cal_date": dates,
                    "is_open": [1] * len(dates),
                    "pretrade_date": ["20260722", "20260723"][: len(dates)],
                }
            )
        raise AssertionError(operation)


class SparseFuturesDataSource(FakeFuturesDataSource):
    def query(self, operation: str, **params: Any) -> pl.DataFrame:
        frame = super().query(operation, **params)
        if operation == "fut_daily":
            return frame.with_columns(
                pl.lit(0.0).alias("open"),
                pl.lit(0.0).alias("high"),
                pl.lit(0.0).alias("low"),
            )
        if operation == "fut_settle":
            return frame.with_columns(
                pl.lit(None).alias("long_margin_rate"),
                pl.lit(None).alias("short_margin_rate"),
            )
        return frame


class InactiveContractFuturesDataSource(FakeFuturesDataSource):
    def query(self, operation: str, **params: Any) -> pl.DataFrame:
        frame = super().query(operation, **params)
        if operation == "fut_daily":
            return frame.with_columns(
                pl.lit(0.0).alias("vol"),
                pl.lit(None).cast(pl.Float64).alias("oi"),
            )
        return frame


def make_service(
    tmp_path: Path,
    source: FakeFuturesDataSource | None = None,
) -> FuturesDataService:
    return FuturesDataService(
        source or FakeFuturesDataSource(),
        ["SHFE"],
        FuturesParquetStore(tmp_path / "raw", "raw"),
        FuturesParquetStore(tmp_path / "curated", "curated"),
        FuturesDataValidator(),
        tmp_path / "reports",
        secrets=("secret-value",),
    )


def test_update_persists_all_standardized_futures_datasets(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)

    result = service.update(date(2026, 7, 24))

    assert result.succeeded
    assert {item.dataset for item in result.datasets} == set(FuturesDataset)
    rules = service.curated_store.read(
        FuturesDataset.CONTRACT_RULES,
        "fake",
        date(2026, 7, 24),
    )
    assert len(rules["rule_hash"][0]) == 64
    assert rules["observed_at"][0] == date.today().strftime("%Y%m%d")
    assert result.quality_report and result.quality_report.is_file()


def test_missing_limits_are_visible_but_do_not_fail_foundation(
    tmp_path: Path,
) -> None:
    service = make_service(
        tmp_path,
        FakeFuturesDataSource(limits_available=False),
    )

    result = service.update(date(2026, 7, 24))

    assert result.succeeded
    missing = next(item for item in result.datasets if item.dataset == FuturesDataset.LIMITS)
    assert missing.status == "unavailable"
    assert missing.error == "unsupported ***"
    assert "futures_limits" in result.quality_report.read_text(encoding="utf-8")


def test_sparse_ohlc_and_margin_are_warnings_not_silent_errors(
    tmp_path: Path,
) -> None:
    result = make_service(tmp_path, SparseFuturesDataSource()).update(
        date(2026, 7, 24),
        (FuturesDataset.DAILY, FuturesDataset.SETTLEMENTS),
    )

    assert result.succeeded
    codes = {issue.code for report in result.reports for issue in report.issues}
    assert codes == {"missing_intraday_ohlc", "missing_margin_rate"}


def test_inactive_contract_missing_open_interest_is_visible_but_not_an_error(
    tmp_path: Path,
) -> None:
    result = make_service(tmp_path, InactiveContractFuturesDataSource()).update(
        date(2026, 7, 24),
        (FuturesDataset.DAILY,),
    )

    assert result.succeeded
    issues = [issue for report in result.reports for issue in report.issues]
    assert [(issue.code, issue.rows) for issue in issues] == [
        ("missing_inactive_open_interest", 1)
    ]


def test_backfill_skips_existing_required_partitions(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    datasets = (
        FuturesDataset.DAILY,
        FuturesDataset.SETTLEMENTS,
        FuturesDataset.MAPPINGS,
    )

    first = service.backfill(
        date(2026, 7, 23),
        date(2026, 7, 24),
        datasets,
    )
    second = service.backfill(
        date(2026, 7, 23),
        date(2026, 7, 24),
        datasets,
    )

    assert first.completed_dates == 2
    assert second.skipped_dates == 2


def test_backfill_stops_when_requested_optional_dataset_is_unavailable(
    tmp_path: Path,
) -> None:
    service = make_service(
        tmp_path,
        FakeFuturesDataSource(limits_available=False),
    )

    result = service.backfill(
        date(2026, 7, 23),
        date(2026, 7, 24),
        (FuturesDataset.LIMITS,),
    )

    assert result.completed_dates == 0
    assert result.failed_dates == [date(2026, 7, 23)]


def test_contract_backfill_partitions_bulk_response_by_date(
    tmp_path: Path,
) -> None:
    service = make_service(tmp_path)

    result = service.backfill_contract(
        "cu2608.shf",
        date(2026, 7, 23),
        date(2026, 7, 24),
    )

    assert result.succeeded
    assert result.completed_dates == 2
    assert service.curated_store.exists(
        FuturesDataset.DAILY,
        "fake",
        date(2026, 7, 23),
    )
    service.backfill_contract(
        "CU2608.SHF",
        date(2026, 7, 23),
        date(2026, 7, 24),
    )
    stored = service.curated_store.read(
        FuturesDataset.DAILY,
        "fake",
        date(2026, 7, 23),
    )
    assert stored.height == 1


def test_store_reads_available_partitions_in_date_order(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    service.backfill_contract(
        "CU2608.SHF",
        date(2026, 7, 23),
        date(2026, 7, 24),
    )

    stored = service.curated_store.read_range(
        FuturesDataset.DAILY,
        "fake",
        date(2026, 7, 23),
        date(2026, 7, 24),
    )

    assert stored.get_column("trade_date").to_list() == ["20260723", "20260724"]
