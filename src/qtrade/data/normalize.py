from __future__ import annotations

from datetime import date

import polars as pl

from qtrade.data.schemas import schema_for
from qtrade.domain import Dataset


def _date_expression(column: str) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.String)
        .str.replace_all("-", "")
        .str.strptime(pl.Date, "%Y%m%d", strict=False)
    )


def _availability_expression(
    dataset: Dataset,
    frame: pl.DataFrame,
    as_of_date: date | None,
) -> pl.Expr | None:
    if "available_from" in frame.columns:
        return _date_expression("available_from")
    if dataset == Dataset.FINANCIAL_INDICATORS and "ann_date" in frame.columns:
        return _date_expression("ann_date") + pl.duration(days=1)
    if dataset == Dataset.SECURITY_NAMES and {
        "start_date",
        "ann_date",
    } <= set(frame.columns):
        return pl.max_horizontal(
            _date_expression("start_date"),
            _date_expression("ann_date") + pl.duration(days=1),
        )
    if dataset == Dataset.SECURITY_MASTER:
        if as_of_date is not None:
            return pl.lit(as_of_date)
        if "list_date" in frame.columns:
            return _date_expression("list_date")
    if dataset == Dataset.INDUSTRY_MEMBERS and "in_date" in frame.columns:
        return _date_expression("in_date")
    date_columns = {
        Dataset.TRADE_CALENDAR: "cal_date",
        Dataset.DAILY_PRICES: "trade_date",
        Dataset.ADJUST_FACTORS: "trade_date",
        Dataset.INDEX_DAILY: "trade_date",
        Dataset.INDEX_MEMBERS: "trade_date",
        Dataset.DAILY_BASIC: "trade_date",
        Dataset.STOCK_LIMIT: "trade_date",
    }
    column = date_columns.get(dataset)
    if column and column in frame.columns:
        if dataset == Dataset.TRADE_CALENDAR and as_of_date is not None:
            return pl.lit(as_of_date)
        return _date_expression(column)
    if as_of_date is not None:
        return pl.lit(as_of_date)
    return None


def normalize_dataset(
    dataset: Dataset,
    frame: pl.DataFrame,
    as_of_date: date | None = None,
) -> pl.DataFrame:
    """Return a deterministic curated frame without changing provider field values."""
    if frame.is_empty():
        return frame.clone()

    normalized = frame.rename({name: name.strip().lower() for name in frame.columns})
    schema = schema_for(dataset)
    availability = _availability_expression(dataset, normalized, as_of_date)
    if availability is not None:
        normalized = normalized.with_columns(
            availability.dt.strftime("%Y%m%d").alias("available_from")
        )

    if dataset == Dataset.FINANCIAL_INDICATORS:
        available_key = [
            column for column in schema.primary_key if column in normalized.columns
        ]
        if len(available_key) == len(schema.primary_key):
            normalized = normalized.drop_nulls(available_key)

    available_key = [column for column in schema.primary_key if column in normalized.columns]
    if available_key:
        normalized = normalized.unique(subset=available_key, keep="last", maintain_order=True)

    available_sort = [column for column in schema.sort_columns if column in normalized.columns]
    if available_sort:
        normalized = normalized.sort(available_sort)

    return normalized
