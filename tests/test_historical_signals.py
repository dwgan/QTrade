from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
from test_research_protocols import make_protocol

from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import DataBatch, Dataset
from qtrade.research.protocols import PartitionName, ProtocolStore
from qtrade.research.signals import HistoricalSignalBuildService, SignalFrequency


class RecordingFactorService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict] = []

    def run(self, signal_date: date, **kwargs):
        self.calls.append({"signal_date": signal_date, **kwargs})
        path = self.root / f"{signal_date}.parquet"
        return SimpleNamespace(rankings_path=path)


def test_historical_signal_builder_uses_month_end_dates_and_provenance(
    tmp_path: Path,
    monkeypatch,
) -> None:
    curated = ParquetDatasetStore(tmp_path / "curated", "curated")
    # A calendar snapshot may be captured after the historical research period.
    calendar_date = date(2026, 7, 24)
    curated.write(
        DataBatch(
            dataset=Dataset.TRADE_CALENDAR,
            provider="fake",
            as_of_date=calendar_date,
            frame=pl.DataFrame(
                {
                    "exchange": ["SSE"] * 6,
                    "cal_date": [
                        "20180102",
                        "20180130",
                        "20180131",
                        "20180201",
                        "20180227",
                        "20180228",
                    ],
                    "is_open": [1, 1, 0, 1, 1, 1],
                    "pretrade_date": [
                        "20171229",
                        "20180129",
                        "20180130",
                        "20180130",
                        "20180226",
                        "20180227",
                    ],
                }
            ),
        )
    )
    protocols = ProtocolStore(tmp_path / "runtime")
    protocol = make_protocol("batch_v1").model_copy(
        update={"code_commit": "unknown", "config_hash": "config123"}
    )
    protocols.create(protocol)
    factors = RecordingFactorService(tmp_path)
    service = HistoricalSignalBuildService(
        factor_service=factors,
        curated_store=curated,
        provider="fake",
        protocol_store=protocols,
        project_root=tmp_path,
        config_hash="config123",
    )
    monkeypatch.setattr(
        "qtrade.research.signals.git_research_tree_is_clean",
        lambda _: True,
    )

    result = service.build(
        "batch_v1",
        PartitionName.DEVELOPMENT,
        SignalFrequency.MONTH_END,
    )

    assert [item.signal_date for item in result.signals[:2]] == [
        date(2018, 1, 30),
        date(2018, 2, 28),
    ]
    assert factors.calls[0]["signal_origin"] == "reconstructed"
    assert factors.calls[0]["protocol_id"] == "batch_v1"
    assert factors.calls[0]["config_hash"] == "config123"
