from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from qtrade.domain import DataBatch, Dataset


class ParquetDatasetStore:
    def __init__(self, root: Path, layer: str) -> None:
        self.root = Path(root)
        self.layer = layer

    def partition_dir(self, dataset: Dataset, provider: str, as_of_date: date) -> Path:
        return (
            self.root
            / dataset.value
            / f"provider={provider}"
            / f"as_of_date={as_of_date.isoformat()}"
        )

    def data_path(self, dataset: Dataset, provider: str, as_of_date: date) -> Path:
        return self.partition_dir(dataset, provider, as_of_date) / "data.parquet"

    def metadata_path(self, dataset: Dataset, provider: str, as_of_date: date) -> Path:
        return self.partition_dir(dataset, provider, as_of_date) / "metadata.json"

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)

    def write(self, batch: DataBatch) -> Path:
        directory = self.partition_dir(batch.dataset, batch.provider, batch.as_of_date)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "data.parquet"
        temporary = directory / f".data.{uuid.uuid4().hex}.parquet"

        batch.frame.write_parquet(temporary, compression="zstd")
        os.replace(temporary, target)
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
        return target

    def read(self, dataset: Dataset, provider: str, as_of_date: date) -> pl.DataFrame:
        path = self.data_path(dataset, provider, as_of_date)
        if not path.exists():
            raise FileNotFoundError(f"Dataset partition not found: {path}")
        return pl.read_parquet(path)

    def exists(self, dataset: Dataset, provider: str, as_of_date: date) -> bool:
        return self.data_path(dataset, provider, as_of_date).exists()

    def partition_dates(
        self,
        dataset: Dataset,
        provider: str,
        start_date: date,
        end_date: date,
    ) -> set[date]:
        provider_dir = self.root / dataset.value / f"provider={provider}"
        if not provider_dir.exists():
            return set()
        dates: set[date] = set()
        for partition in provider_dir.glob("as_of_date=*"):
            try:
                partition_date = date.fromisoformat(partition.name.split("=", 1)[1])
            except (IndexError, ValueError):
                continue
            if (
                start_date <= partition_date <= end_date
                and (partition / "data.parquet").exists()
            ):
                dates.add(partition_date)
        return dates

    def read_range(
        self,
        dataset: Dataset,
        provider: str,
        start_date: date,
        end_date: date,
    ) -> pl.DataFrame:
        provider_dir = self.root / dataset.value / f"provider={provider}"
        if not provider_dir.exists():
            raise FileNotFoundError(f"Dataset directory not found: {provider_dir}")

        dates = self.partition_dates(dataset, provider, start_date, end_date)
        paths = [
            self.data_path(dataset, provider, partition_date)
            for partition_date in dates
        ]

        if not paths:
            raise FileNotFoundError(
                f"No {dataset.value} partitions found from {start_date} to {end_date}."
            )
        frames = [pl.read_parquet(path) for path in sorted(paths)]
        return (
            frames[0]
            if len(frames) == 1
            else pl.concat(frames, how="diagonal_relaxed")
        )

    def read_latest_on_or_before(
        self,
        dataset: Dataset,
        provider: str,
        as_of_date: date,
    ) -> tuple[date, pl.DataFrame]:
        provider_dir = self.root / dataset.value / f"provider={provider}"
        if not provider_dir.exists():
            raise FileNotFoundError(f"Dataset directory not found: {provider_dir}")

        candidates: list[tuple[date, Path]] = []
        for partition in provider_dir.glob("as_of_date=*"):
            try:
                partition_date = date.fromisoformat(partition.name.split("=", 1)[1])
            except (IndexError, ValueError):
                continue
            data_path = partition / "data.parquet"
            if partition_date <= as_of_date and data_path.exists():
                candidates.append((partition_date, data_path))
        if not candidates:
            raise FileNotFoundError(
                f"No {dataset.value} partition found on or before {as_of_date}."
            )
        partition_date, path = max(candidates, key=lambda item: item[0])
        return partition_date, pl.read_parquet(path)

    def read_latest(
        self,
        dataset: Dataset,
        provider: str,
    ) -> tuple[date, pl.DataFrame]:
        provider_dir = self.root / dataset.value / f"provider={provider}"
        if not provider_dir.exists():
            raise FileNotFoundError(f"Dataset directory not found: {provider_dir}")
        candidates: list[tuple[date, Path]] = []
        for partition in provider_dir.glob("as_of_date=*"):
            try:
                partition_date = date.fromisoformat(partition.name.split("=", 1)[1])
            except (IndexError, ValueError):
                continue
            data_path = partition / "data.parquet"
            if data_path.exists():
                candidates.append((partition_date, data_path))
        if not candidates:
            raise FileNotFoundError(f"No {dataset.value} partitions found.")
        partition_date, path = max(candidates, key=lambda item: item[0])
        return partition_date, pl.read_parquet(path)

    def read_all(self, dataset: Dataset, provider: str) -> pl.DataFrame:
        provider_dir = self.root / dataset.value / f"provider={provider}"
        if not provider_dir.exists():
            raise FileNotFoundError(f"Dataset directory not found: {provider_dir}")
        paths = sorted(provider_dir.glob("as_of_date=*/data.parquet"))
        if not paths:
            raise FileNotFoundError(f"No {dataset.value} partitions found.")
        frames = [pl.read_parquet(path) for path in paths]
        return (
            frames[0]
            if len(frames) == 1
            else pl.concat(frames, how="diagonal_relaxed")
        )


class DuckDBCatalog:
    """Small query facade; no persistent database is required."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)

    def query_parquet(self, parquet_glob: str | Path, sql: str = "SELECT * FROM dataset"):
        escaped = str(parquet_glob).replace("'", "''").replace("\\", "/")
        with duckdb.connect(self.database) as connection:
            connection.execute(
                f"CREATE OR REPLACE VIEW dataset AS SELECT * FROM read_parquet('{escaped}')"
            )
            cursor = connection.execute(sql)
            columns = [description[0] for description in cursor.description]
            return pl.DataFrame(cursor.fetchall(), schema=columns, orient="row")
