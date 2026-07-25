from __future__ import annotations

from dataclasses import dataclass

from qtrade.domain import Dataset


@dataclass(frozen=True)
class DatasetSchema:
    required_columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    sort_columns: tuple[str, ...]


SCHEMAS: dict[Dataset, DatasetSchema] = {
    Dataset.TRADE_CALENDAR: DatasetSchema(
        required_columns=("exchange", "cal_date", "is_open", "pretrade_date"),
        primary_key=("exchange", "cal_date"),
        sort_columns=("exchange", "cal_date"),
    ),
    Dataset.SECURITY_MASTER: DatasetSchema(
        required_columns=(
            "ts_code",
            "symbol",
            "name",
            "area",
            "industry",
            "market",
            "exchange",
            "list_status",
            "list_date",
            "delist_date",
        ),
        primary_key=("ts_code", "list_status"),
        sort_columns=("ts_code", "list_status"),
    ),
    Dataset.SECURITY_NAMES: DatasetSchema(
        required_columns=("ts_code", "name", "start_date", "end_date", "ann_date"),
        primary_key=("ts_code", "name", "start_date"),
        sort_columns=("ts_code", "start_date"),
    ),
    Dataset.DAILY_PRICES: DatasetSchema(
        required_columns=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "vol",
            "amount",
        ),
        primary_key=("ts_code", "trade_date"),
        sort_columns=("trade_date", "ts_code"),
    ),
    Dataset.ADJUST_FACTORS: DatasetSchema(
        required_columns=("ts_code", "trade_date", "adj_factor"),
        primary_key=("ts_code", "trade_date"),
        sort_columns=("trade_date", "ts_code"),
    ),
    Dataset.INDEX_DAILY: DatasetSchema(
        required_columns=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "vol",
            "amount",
        ),
        primary_key=("ts_code", "trade_date"),
        sort_columns=("trade_date", "ts_code"),
    ),
    Dataset.INDEX_MEMBERS: DatasetSchema(
        required_columns=("index_code", "con_code", "trade_date", "weight"),
        primary_key=("index_code", "con_code", "trade_date"),
        sort_columns=("trade_date", "index_code", "con_code"),
    ),
}


def schema_for(dataset: Dataset) -> DatasetSchema:
    return SCHEMAS[dataset]
