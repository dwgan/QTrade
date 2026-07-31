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

    def available_dates(
        self,
        dataset: FuturesDataset,
        provider: str,
        start_date: date,
        end_date: date,
    ) -> list[date]:
        provider_dir = self.root / "futures" / dataset.value / f"provider={provider}"
        if not provider_dir.exists():
            return []
        dates: list[date] = []
        for partition in provider_dir.glob("as_of_date=*"):
            try:
                value = date.fromisoformat(partition.name.removeprefix("as_of_date="))
            except ValueError:
                continue
            if start_date <= value <= end_date and (partition / "data.parquet").exists():
                dates.append(value)
        return sorted(dates)

    def read_range(
        self,
        dataset: FuturesDataset,
        provider: str,
        start_date: date,
        end_date: date,
    ) -> pl.DataFrame:
        if start_date > end_date:
            raise ValueError("Futures range start date must not exceed end date.")
        frames = [
            self.read(dataset, provider, as_of_date)
            for as_of_date in self.available_dates(
                dataset,
                provider,
                start_date,
                end_date,
            )
        ]
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)
