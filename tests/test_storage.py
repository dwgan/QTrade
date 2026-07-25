from datetime import date
from pathlib import Path

import polars as pl

from qtrade.data.storage import DuckDBCatalog, ParquetDatasetStore
from qtrade.domain import DataBatch, Dataset


def _batch(close: float) -> DataBatch:
    return DataBatch(
        dataset=Dataset.ADJUST_FACTORS,
        provider="fake",
        as_of_date=date(2026, 7, 24),
        frame=pl.DataFrame(
            {
                "ts_code": ["000001.SZ"],
                "trade_date": ["20260724"],
                "adj_factor": [close],
            }
        ),
    )


def test_store_replaces_same_partition_atomically(tmp_path: Path) -> None:
    store = ParquetDatasetStore(tmp_path, "raw")

    first_path = store.write(_batch(1.0))
    second_path = store.write(_batch(2.0))

    assert first_path == second_path
    assert (
        store.read(Dataset.ADJUST_FACTORS, "fake", date(2026, 7, 24))
        .get_column("adj_factor")
        .item()
        == 2.0
    )
    assert len(list(second_path.parent.glob("*.parquet"))) == 1


def test_duckdb_queries_parquet(tmp_path: Path) -> None:
    store = ParquetDatasetStore(tmp_path, "raw")
    path = store.write(_batch(1.5))

    result = DuckDBCatalog().query_parquet(path, "SELECT ts_code, adj_factor FROM dataset")

    assert result.to_dicts() == [{"ts_code": "000001.SZ", "adj_factor": 1.5}]


def test_store_reads_date_range(tmp_path: Path) -> None:
    store = ParquetDatasetStore(tmp_path, "curated")
    for day, factor in ((23, 1.0), (24, 1.1), (25, 1.2)):
        batch = _batch(factor)
        batch.as_of_date = date(2026, 7, day)
        batch.frame = batch.frame.with_columns(pl.lit(f"202607{day:02d}").alias("trade_date"))
        store.write(batch)

    result = store.read_range(
        Dataset.ADJUST_FACTORS,
        "fake",
        date(2026, 7, 23),
        date(2026, 7, 24),
    )

    assert result.height == 2
    assert result.get_column("adj_factor").to_list() == [1.0, 1.1]


def test_store_reads_latest_partition_on_or_before_date(tmp_path: Path) -> None:
    store = ParquetDatasetStore(tmp_path, "curated")
    for day, factor in ((23, 1.0), (25, 1.2)):
        batch = _batch(factor)
        batch.as_of_date = date(2026, 7, day)
        store.write(batch)

    partition_date, result = store.read_latest_on_or_before(
        Dataset.ADJUST_FACTORS,
        "fake",
        date(2026, 7, 24),
    )

    assert partition_date == date(2026, 7, 23)
    assert result.get_column("adj_factor").item() == 1.0
