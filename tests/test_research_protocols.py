from datetime import date
from pathlib import Path

import polars as pl
import pytest
import yaml

from qtrade.research.protocols import (
    ExperimentRecord,
    ExperimentStatus,
    ExperimentStore,
    PartitionName,
    ProtocolStatus,
    ProtocolStore,
    ResearchPartition,
    StrategyProtocol,
    TemporalLeakageAuditor,
)


def make_protocol(protocol_id: str = "quality_v1") -> StrategyProtocol:
    return StrategyProtocol(
        protocol_id=protocol_id,
        title="Quality factor v1",
        hypothesis="Profitable companies outperform after costs.",
        partitions=[
            ResearchPartition(
                name=PartitionName.DEVELOPMENT,
                start_date=date(2018, 1, 1),
                end_date=date(2020, 12, 31),
            ),
            ResearchPartition(
                name=PartitionName.VALIDATION,
                start_date=date(2021, 1, 1),
                end_date=date(2022, 12, 31),
            ),
            ResearchPartition(
                name=PartitionName.HOLDOUT,
                start_date=date(2023, 1, 1),
                end_date=date(2024, 12, 31),
            ),
        ],
        strategy={"factors": {"quality": 1.0}},
        execution={"rule": "T+1"},
        acceptance_criteria={"positive_excess": True},
        code_commit="abc123",
        config_hash="config123",
    )


def test_frozen_protocol_is_hash_verified(tmp_path: Path) -> None:
    store = ProtocolStore(tmp_path)
    store.create(make_protocol())

    frozen = store.freeze("quality_v1", data_version="data123")

    assert frozen.status == ProtocolStatus.FROZEN
    assert frozen.content_hash
    assert store.load("quality_v1") == frozen

    path = tmp_path / "research/protocols/quality_v1/protocol.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["hypothesis"] = "Changed after seeing holdout."
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="hash mismatch"):
        store.load("quality_v1")


def test_protocol_partitions_cannot_overlap() -> None:
    protocol = make_protocol()
    protocol.partitions[1].start_date = date(2020, 12, 31)

    with pytest.raises(ValueError, match="overlap"):
        StrategyProtocol.model_validate(protocol.model_dump())


def test_holdout_reveal_is_recorded_separately_from_frozen_protocol(
    tmp_path: Path,
) -> None:
    store = ProtocolStore(tmp_path)
    store.create(make_protocol())
    frozen = store.freeze("quality_v1")

    state = store.reveal_holdout("quality_v1")

    assert state.holdout_revealed_at is not None
    assert store.load("quality_v1").content_hash == frozen.content_hash


def test_temporal_leakage_audit_rejects_future_availability() -> None:
    snapshot_date = date(2024, 4, 30)
    ranking = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ"],
            "available_from": ["20240429", "20240506"],
        }
    )

    audit = TemporalLeakageAuditor.audit_snapshots([(snapshot_date, ranking)])

    assert not audit.passed
    assert audit.issues[0].offending_rows == 1
    assert audit.issues[0].latest_value == date(2024, 5, 6)


def test_experiment_failures_are_retained(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path)
    record = store.create(
        ExperimentRecord(
            protocol_id="quality_v1",
            partition=PartitionName.VALIDATION,
            kind="candidate_backtest",
            start_date=date(2021, 1, 1),
            end_date=date(2022, 12, 31),
            code_commit="abc123",
            config_hash="config123",
        )
    )

    failed = store.fail(record.experiment_id, "missing point-in-time universe")

    assert failed.status == ExperimentStatus.FAILED
    assert store.list("quality_v1")[0].error == "missing point-in-time universe"
