from datetime import date
from pathlib import Path

import polars as pl

from qtrade.cli.main import main
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import DataBatch, Dataset


def test_validate_uses_local_data_without_provider_token(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "test.yaml"
    config_path.write_text(
        """
paths:
  curated: data/curated
  reports: reports
provider:
  name: tushare
  token_env: TOKEN_THAT_IS_NOT_SET
validation:
  minimum_daily_rows: 0
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("TOKEN_THAT_IS_NOT_SET", raising=False)
    as_of_date = date(2026, 7, 24)
    frame = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "trade_date": ["20260724"],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "pre_close": [9.9],
            "vol": [100.0],
            "amount": [1000.0],
        }
    )
    ParquetDatasetStore(tmp_path / "data" / "curated", "curated").write(
        DataBatch(Dataset.DAILY_PRICES, "tushare", as_of_date, frame)
    )

    result = main(
        [
            "--config",
            str(config_path),
            "data",
            "validate",
            "--date",
            as_of_date.isoformat(),
            "--datasets",
            "daily_prices",
        ]
    )

    assert result == 0
