import polars as pl

from qtrade.data.normalize import normalize_dataset
from qtrade.domain import Dataset


def test_normalize_deduplicates_primary_key_and_keeps_last_row() -> None:
    frame = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ", "000002.SZ"],
            "trade_date": ["20260724", "20260724", "20260724"],
            "open": [10.0, 10.1, 20.0],
            "high": [10.5, 10.6, 20.5],
            "low": [9.8, 9.9, 19.8],
            "close": [10.2, 10.3, 20.2],
            "pre_close": [9.9, 9.9, 19.9],
            "vol": [100.0, 101.0, 200.0],
            "amount": [1000.0, 1010.0, 4000.0],
        }
    )

    result = normalize_dataset(Dataset.DAILY_PRICES, frame)

    assert result.height == 2
    assert result.filter(pl.col("ts_code") == "000001.SZ").get_column("close").item() == 10.3


def test_normalize_financials_drops_rows_without_point_in_time_keys() -> None:
    frame = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "ann_date": ["20260430", None],
            "end_date": ["20260331", "20260331"],
            "roe": [10.0, 11.0],
        }
    )

    result = normalize_dataset(Dataset.FINANCIAL_INDICATORS, frame)

    assert result.height == 1
    assert result.get_column("ts_code").to_list() == ["000001.SZ"]
