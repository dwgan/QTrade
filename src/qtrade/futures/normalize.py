from __future__ import annotations

import hashlib
import json
from datetime import date

import polars as pl

from qtrade.futures.domain import FuturesDataset
from qtrade.futures.schemas import futures_schema_for

RULE_FIELDS = (
    "ts_code",
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
)


def normalize_futures_dataset(
    dataset: FuturesDataset,
    frame: pl.DataFrame,
    as_of_date: date,
) -> pl.DataFrame:
    if frame.is_empty():
        return frame.clone()
    normalized = frame.rename({name: name.strip().lower() for name in frame.columns})
    if dataset == FuturesDataset.CONTRACTS:
        normalized = normalized.with_columns(
            pl.lit(as_of_date.strftime("%Y%m%d")).alias("observed_at")
        )
    elif dataset == FuturesDataset.CONTRACT_RULES:
        normalized = _build_rule_versions(normalized, as_of_date)

    schema = futures_schema_for(dataset)
    keys = [name for name in schema.primary_key if name in normalized.columns]
    if len(keys) == len(schema.primary_key):
        normalized = normalized.drop_nulls(keys).unique(
            subset=keys,
            keep="last",
            maintain_order=True,
        )
    sort_columns = [name for name in schema.sort_columns if name in normalized.columns]
    return normalized.sort(sort_columns) if sort_columns else normalized


def _build_rule_versions(frame: pl.DataFrame, as_of_date: date) -> pl.DataFrame:
    available = [name for name in RULE_FIELDS if name in frame.columns]
    records: list[dict[str, object]] = []
    for row in frame.select(available).iter_rows(named=True):
        canonical = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        records.append(
            {
                **row,
                "observed_at": as_of_date.strftime("%Y%m%d"),
                "rule_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            }
        )
    return pl.DataFrame(records, infer_schema_length=None, strict=False)
