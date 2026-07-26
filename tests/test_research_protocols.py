import hashlib
import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest
import yaml

from qtrade.config import BacktestConfig, ResearchConfig
from qtrade.data.storage import ParquetDatasetStore
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
from qtrade.research.service import ResearchService


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


def test_partition_data_version_must_be_pinned_before_freeze(
    tmp_path: Path,
) -> None:
    store = ProtocolStore(tmp_path)
    store.create(make_protocol())
    version = "b" * 64

    updated = store.pin_data_version(
        "quality_v1",
        PartitionName.VALIDATION,
        version,
    )

    assert updated.partition_data_versions[PartitionName.VALIDATION] == version
    store.freeze("quality_v1")
    with pytest.raises(ValueError, match="draft"):
        store.pin_data_version(
            "quality_v1",
            PartitionName.HOLDOUT,
            "c" * 64,
        )


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


def test_temporal_leakage_audit_warns_when_complete_lineage_is_missing() -> None:
    ranking = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "ann_date": ["20240429"],
        }
    )

    audit = TemporalLeakageAuditor.audit_snapshots(
        [(date(2024, 4, 30), ranking)]
    )

    assert audit.passed
    assert any("lack available_from" in warning for warning in audit.warnings)


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


def test_formal_backtest_rejects_rankings_without_complete_lineage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reports = tmp_path / "reports"
    ranking_directory = reports / "factors/2018-01-02"
    signal_id = "a" * 64
    version_directory = ranking_directory / "versions" / signal_id
    version_directory.mkdir(parents=True)
    ranking_path = version_directory / "rankings.parquet"
    pl.DataFrame(
        {
            "ts_code": ["000001.SZ"],
            "industry": ["A"],
            "score": [100.0],
            "ann_date": ["20171231"],
        }
    ).write_parquet(ranking_path)
    (version_directory / "manifest.json").write_text(
        json.dumps(
            {
                "signal_id": signal_id,
                "origin": "reconstructed",
                "files": {
                    "rankings.parquet": hashlib.sha256(
                        ranking_path.read_bytes()
                    ).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    (ranking_directory / "latest.json").write_text(
        json.dumps({"signal_id": signal_id}),
        encoding="utf-8",
    )
    service = ResearchService(
        research_config=ResearchConfig(),
        backtest_config=BacktestConfig(),
        curated_store=ParquetDatasetStore(tmp_path / "curated", "curated"),
        provider="fake",
        reports_root=reports,
        runtime_root=tmp_path / "runtime",
        project_root=tmp_path,
        factor_config={},
    )
    protocol = make_protocol("strict_v1").model_copy(
        update={
            "code_commit": "unknown",
            "config_hash": service.config_hash(),
        }
    )
    service.protocols.create(protocol)
    service.protocols.freeze("strict_v1")
    monkeypatch.setattr(
        "qtrade.research.service.git_research_tree_is_clean",
        lambda _: True,
    )

    with pytest.raises(ValueError, match="complete point-in-time"):
        service.backtest_candidates(
            date(2018, 1, 1),
            date(2020, 12, 31),
            protocol_id="strict_v1",
            partition=PartitionName.DEVELOPMENT,
        )

    experiments = service.experiments.list("strict_v1")
    assert len(experiments) == 1
    assert experiments[0].status == ExperimentStatus.FAILED
