from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl


class FactorSnapshotStore:
    """Read immutable ranking outputs produced by daily factor analysis."""

    def __init__(self, reports_root: Path) -> None:
        self.root = Path(reports_root) / "factors"

    def available_dates(self, start_date: date, end_date: date) -> list[date]:
        if start_date > end_date:
            raise ValueError("Start date must not be after end date.")
        if not self.root.exists():
            return []
        values: list[date] = []
        for directory in self.root.iterdir():
            try:
                snapshot_date = date.fromisoformat(directory.name)
            except ValueError:
                continue
            if start_date <= snapshot_date <= end_date and (
                directory / "rankings.parquet"
            ).exists():
                values.append(snapshot_date)
        return sorted(values)

    def read(self, snapshot_date: date) -> pl.DataFrame:
        path = self.root / snapshot_date.isoformat() / "rankings.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Factor ranking snapshot not found: {path}")
        return pl.read_parquet(path)

