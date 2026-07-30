from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from qtrade.futures.domain import FuturesDataBatch, FuturesDataset


class FuturesParquetStore:
    def __init__(self, root: Path, layer: str) -> None:
        self.root = Path(root)
        self.layer = layer

    def partition_dir(
        self,
        dataset: FuturesDataset,
        provider: str,
        as_of_date: date,
    ) -> Path:
        return (
            self.root
            / "futures"
            / dataset.value
            / f"provider={provider}"
            / f"as_of_date={as_of_date.isoformat()}"
        )

    def data_path(
        self,
        dataset: FuturesDataset,
        provider: str,
        as_of_date: date,
    ) -> Path:
        return self.partition_dir(dataset, provider, as_of_date) / "data.parquet"

    def write(self, batch: FuturesDataBatch) -> Path:
        directory = self.partition_dir(
            batch.dataset,
            batch.provider,
            batch.as_of_date,
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "data.parquet"
        temporary = directory / f".data.{uuid.uuid4().hex}.parquet"
        batch.frame.write_parquet(temporary, compression="zstd")
        os.replace(temporary, path)
        self._atomic_json(
            directory / "metadata.json",
            {
                "dataset": batch.dataset.value,
                "layer": self.layer,
                "provider": batch.provider,
                "as_of_date": batch.as_of_date.isoformat(),
                "fetched_at": batch.fetched_at.isoformat(),
                "row_count": batch.frame.height,
                "columns": batch.frame.columns,
                "request": batch.request,
            },
        )
        return path

    def read(
        self,
        dataset: FuturesDataset,
        provider: str,
        as_of_date: date,
    ) -> pl.DataFrame:
        path = self.data_path(dataset, provider, as_of_date)
        if not path.exists():
            raise FileNotFoundError(f"Futures dataset partition not found: {path}")
        return pl.read_parquet(path)

    def exists(
        self,
        dataset: FuturesDataset,
        provider: str,
        as_of_date: date,
    ) -> bool:
        return self.data_path(dataset, provider, as_of_date).exists()

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)
