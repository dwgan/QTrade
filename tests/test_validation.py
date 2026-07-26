from datetime import date

import polars as pl

from qtrade.config import ValidationConfig
from qtrade.data.validation import DataValidator
from qtrade.domain import Dataset


def valid_daily_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "trade_date": ["20260724", "20260724"],
            "open": [10.0, 20.0],
            "high": [10.5, 20.5],
            "low": [9.8, 19.8],
            "close": [10.2, 20.2],
            "pre_close": [9.9, 19.9],
            "vol": [100.0, 200.0],
            "amount": [1000.0, 4000.0],
        }
    )


def test_valid_daily_prices_pass() -> None:
    validator = DataValidator(ValidationConfig(minimum_daily_rows=1))

    report = validator.validate(Dataset.DAILY_PRICES, date(2026, 7, 24), valid_daily_frame())

    assert report.passed
    assert report.issues == []


def test_invalid_ohlc_and_duplicate_key_fail() -> None:
    frame = pl.concat([valid_daily_frame().head(1)] * 2).with_columns(pl.lit(9.0).alias("high"))
    validator = DataValidator(ValidationConfig(minimum_daily_rows=1))

    report = validator.validate(Dataset.DAILY_PRICES, date(2026, 7, 24), frame)

    assert not report.passed
    assert {issue.code for issue in report.issues} >= {
        "duplicate_primary_key",
        "invalid_ohlc",
    }


def test_wrong_trade_date_fails() -> None:
    frame = valid_daily_frame().with_columns(pl.lit("20260723").alias("trade_date"))
    validator = DataValidator(ValidationConfig(minimum_daily_rows=1))

    report = validator.validate(Dataset.DAILY_PRICES, date(2026, 7, 24), frame)

    assert any(issue.code == "unexpected_trade_date" for issue in report.issues)


def test_non_numeric_price_fails() -> None:
    frame = valid_daily_frame().with_columns(pl.lit("bad").alias("close"))
    validator = DataValidator(ValidationConfig(minimum_daily_rows=1))

    report = validator.validate(Dataset.DAILY_PRICES, date(2026, 7, 24), frame)

    assert any(issue.code == "invalid_numeric_value" for issue in report.issues)


def test_future_available_from_fails_validation() -> None:
    frame = valid_daily_frame().with_columns(
        pl.lit("20260725").alias("available_from")
    )
    validator = DataValidator(ValidationConfig(minimum_daily_rows=1))

    report = validator.validate(Dataset.DAILY_PRICES, date(2026, 7, 24), frame)

    assert any(issue.code == "future_available_from" for issue in report.issues)
