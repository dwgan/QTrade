from __future__ import annotations

from dataclasses import dataclass

from qtrade.futures.domain import FuturesDataset


@dataclass(frozen=True)
class FuturesDatasetSchema:
    required_columns: tuple[str, ...]
    primary_key: tuple[str, ...]
    sort_columns: tuple[str, ...]
    date_column: str | None = None


SCHEMAS: dict[FuturesDataset, FuturesDatasetSchema] = {
    FuturesDataset.CONTRACTS: FuturesDatasetSchema(
        required_columns=(
            "ts_code",
            "symbol",
            "exchange",
            "fut_code",
            "list_date",
            "delist_date",
            "multiplier",
            "per_unit",
            "trade_time_desc",
            "observed_at",
        ),
        primary_key=("ts_code", "observed_at"),
        sort_columns=("exchange", "fut_code", "ts_code"),
    ),
    FuturesDataset.CONTRACT_RULES: FuturesDatasetSchema(
        required_columns=(
            "ts_code",
            "exchange",
            "fut_code",
            "multiplier",
            "per_unit",
            "trade_time_desc",
            "observed_at",
            "rule_hash",
        ),
        primary_key=("ts_code", "observed_at"),
        sort_columns=("exchange", "fut_code", "ts_code"),
    ),
    FuturesDataset.DAILY: FuturesDatasetSchema(
        required_columns=(
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
        primary_key=("ts_code", "trade_date"),
        sort_columns=("trade_date", "ts_code"),
        date_column="trade_date",
    ),
    FuturesDataset.SETTLEMENTS: FuturesDatasetSchema(
        required_columns=(
            "ts_code",
            "trade_date",
            "settle",
            "trading_fee_rate",
            "trading_fee",
            "long_margin_rate",
            "short_margin_rate",
        ),
        primary_key=("ts_code", "trade_date"),
        sort_columns=("trade_date", "ts_code"),
        date_column="trade_date",
    ),
    FuturesDataset.MAPPINGS: FuturesDatasetSchema(
        required_columns=("ts_code", "trade_date", "mapping_ts_code"),
        primary_key=("ts_code", "trade_date"),
        sort_columns=("trade_date", "ts_code"),
        date_column="trade_date",
    ),
    FuturesDataset.LIMITS: FuturesDatasetSchema(
        required_columns=(
            "ts_code",
            "trade_date",
            "up_limit",
            "down_limit",
            "m_ratio",
            "exchange",
        ),
        primary_key=("ts_code", "trade_date"),
        sort_columns=("trade_date", "ts_code"),
        date_column="trade_date",
    ),
    FuturesDataset.CALENDAR: FuturesDatasetSchema(
        required_columns=("exchange", "cal_date", "is_open", "pretrade_date"),
        primary_key=("exchange", "cal_date"),
        sort_columns=("cal_date", "exchange"),
        date_column="cal_date",
    ),
}


def futures_schema_for(dataset: FuturesDataset) -> FuturesDatasetSchema:
    return SCHEMAS[dataset]
