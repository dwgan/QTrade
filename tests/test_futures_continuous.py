from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

import polars as pl
import pytest

from qtrade.config import FuturesConfig
from qtrade.futures.continuous import FuturesSeriesBuilder
from qtrade.futures.domain import FuturesDataBatch, FuturesDataset
from qtrade.futures.research import FuturesResearchService
from qtrade.futures.storage import FuturesParquetStore

DATES = [date(2026, 1, day) for day in (5, 6, 7, 8)]


def config() -> FuturesConfig:
    return FuturesConfig(
        exchanges=["SHFE"],
        excluded_product_codes=[],
        roll_confirmation_days=2,
        roll_expiry_buffer_calendar_days=15,
        universe_lookback_days=2,
        universe_min_history_days=1,
        universe_min_contracts=2,
        universe_minimum_daily_volume=0,
        universe_minimum_open_interest=0,
    )


def contracts() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["CU2606.SHF", "CU2607.SHF"],
            "fut_code": ["CU", "CU"],
            "exchange": ["SHFE", "SHFE"],
            "list_date": ["20250101", "20250201"],
            "delist_date": ["20260615", "20260715"],
            "last_ddate": ["20260615", "20260715"],
            "multiplier": [5.0, 5.0],
            "trade_time_desc": ["09:00-15:00", "09:00-15:00"],
            "observed_at": ["20260105", "20260105"],
        }
    )


def daily(*, final_day_second_oi: float = 130.0) -> pl.DataFrame:
    rows = []
    first_oi = [100.0, 90.0, 80.0, 70.0]
    second_oi = [80.0, 110.0, 120.0, final_day_second_oi]
    for index, trading_date in enumerate(DATES):
        for code, settle, open_interest in (
            ("CU2606.SHF", 100.0 + index, first_oi[index]),
            ("CU2607.SHF", 110.0 + index, second_oi[index]),
        ):
            rows.append(
                {
                    "ts_code": code,
                    "trade_date": trading_date.strftime("%Y%m%d"),
                    "open": settle,
                    "high": settle + 1,
                    "low": settle - 1,
                    "close": settle,
                    "settle": settle,
                    "vol": 1000.0,
                    "amount": 10000.0,
                    "oi": open_interest,
                }
            )
    return pl.DataFrame(rows)


def mappings() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["CU.SHF"] * 3,
            "trade_date": [value.strftime("%Y%m%d") for value in DATES[1:]],
            "mapping_ts_code": ["CU2606.SHF", "CU2606.SHF", "CU2607.SHF"],
        }
    )


def test_roll_uses_two_prior_decisions_and_removes_contract_gap() -> None:
    result = FuturesSeriesBuilder(config()).build(daily(), contracts(), mappings())

    assert result.passed
    assert result.roll_schedule.get_column("selected_contract").to_list() == [
        "CU2606.SHF",
        "CU2606.SHF",
        "CU2607.SHF",
    ]
    assert result.roll_schedule.get_column("reason").to_list()[-1] == ("open_interest_confirmation")
    last = result.continuous.row(-1, named=True)
    assert last["contract_code"] == "CU2607.SHF"
    assert last["continuous_return"] == 113.0 / 112.0 - 1
    assert result.vendor_comparison.get_column("matched").to_list() == [True, True, True]


def test_effective_day_data_cannot_change_earlier_roll_decision() -> None:
    builder = FuturesSeriesBuilder(config())

    baseline = builder.build(daily(final_day_second_oi=130), contracts())
    changed = builder.build(daily(final_day_second_oi=1_000_000), contracts())

    assert baseline.roll_schedule.equals(changed.roll_schedule)
    decisions = baseline.roll_schedule.select("decision_date", "effective_date")
    assert all(
        date.fromisoformat(decision) < date.fromisoformat(effective)
        for decision, effective in decisions.iter_rows()
    )


def test_dynamic_universe_is_point_in_time() -> None:
    result = FuturesSeriesBuilder(config()).build(daily(), contracts())

    first = result.universe.row(0, named=True)
    assert first["eligible"]
    assert first["history_days"] == 1
    assert first["eligible_contracts"] == 2


def test_duplicate_contract_dates_fail_quality_gate() -> None:
    duplicated = pl.concat([daily(), daily().head(1)])

    result = FuturesSeriesBuilder(config()).build(duplicated, contracts())

    assert not result.passed
    assert {issue["code"] for issue in result.issues} == {"duplicate_contract_date"}


def test_research_build_is_content_addressed_and_reused(tmp_path: Path) -> None:
    store = FuturesParquetStore(tmp_path / "curated", "curated")
    store.write(
        FuturesDataBatch(
            FuturesDataset.CONTRACTS,
            "fake",
            DATES[0],
            contracts(),
            fetched_at=datetime.combine(DATES[0], time.min),
        )
    )
    source = daily()
    for trading_date in DATES:
        store.write(
            FuturesDataBatch(
                FuturesDataset.DAILY,
                "fake",
                trading_date,
                source.filter(pl.col("trade_date") == trading_date.strftime("%Y%m%d")),
            )
        )
    service = FuturesResearchService(config(), store, "fake", tmp_path / "reports")

    first = service.build(DATES[0], DATES[-1])
    second = service.build(DATES[0], DATES[-1])

    assert first.passed
    assert first.manifest_path.is_file()
    assert first.report_path.is_file()
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["contract_master_partition_date"] == "2026-01-05"
    assert manifest["contract_master_observed_at"] == "2026-01-05"
    assert any(item["path"].endswith("metadata.json") for item in manifest["inputs"])
    assert {item["path"] for item in manifest["output_versions"]} == set(
        service.OUTPUT_FILES.values()
    )
    assert second.build_id == first.build_id
    assert second.reused


def test_research_rejects_contract_master_fetched_after_start(tmp_path: Path) -> None:
    store = FuturesParquetStore(tmp_path / "curated", "curated")
    store.write(
        FuturesDataBatch(
            FuturesDataset.CONTRACTS,
            "fake",
            DATES[0],
            contracts(),
        )
    )
    source = daily()
    for trading_date in DATES[:2]:
        store.write(
            FuturesDataBatch(
                FuturesDataset.DAILY,
                "fake",
                trading_date,
                source.filter(pl.col("trade_date") == trading_date.strftime("%Y%m%d")),
            )
        )

    service = FuturesResearchService(config(), store, "fake", tmp_path / "reports")

    with pytest.raises(FileNotFoundError, match="research start date"):
        service.build(DATES[0], DATES[1])
