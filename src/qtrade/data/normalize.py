from __future__ import annotations

import polars as pl

from qtrade.data.schemas import schema_for
from qtrade.domain import Dataset


def normalize_dataset(dataset: Dataset, frame: pl.DataFrame) -> pl.DataFrame:
    """Return a deterministic curated frame without changing provider field values."""
    if frame.is_empty():
        return frame.clone()

    normalized = frame.rename({name: name.strip().lower() for name in frame.columns})
    schema = schema_for(dataset)

    available_key = [column for column in schema.primary_key if column in normalized.columns]
    if available_key:
        normalized = normalized.unique(subset=available_key, keep="last", maintain_order=True)

    available_sort = [column for column in schema.sort_columns if column in normalized.columns]
    if available_sort:
        normalized = normalized.sort(available_sort)

    return normalized
