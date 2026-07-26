from datetime import date
from pathlib import Path

import polars as pl

from qtrade.research.snapshots import FactorSnapshotStore


def test_factor_snapshot_store_discovers_only_valid_archives(tmp_path: Path) -> None:
    directory = tmp_path / "factors" / "2026-01-02"
    directory.mkdir(parents=True)
    pl.DataFrame({"ts_code": ["000001.SZ"]}).write_parquet(
        directory / "rankings.parquet"
    )
    (tmp_path / "factors" / "notes").mkdir()

    store = FactorSnapshotStore(tmp_path)

    assert store.available_dates(date(2026, 1, 1), date(2026, 1, 3)) == [
        date(2026, 1, 2)
    ]
    assert store.read(date(2026, 1, 2)).height == 1
