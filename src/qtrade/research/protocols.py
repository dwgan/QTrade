from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from pydantic import BaseModel, Field, model_validator

RESEARCH_CODE_PATHS = (
    "src/qtrade/factors",
    "src/qtrade/research",
    "src/qtrade/data",
    "src/qtrade/config.py",
    "src/qtrade/domain.py",
    "config",
    "pyproject.toml",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_git_commit(project_root: Path) -> str:
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                "-1",
                "--format=%H",
                "--",
                *RESEARCH_CODE_PATHS,
            ],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def git_research_tree_is_clean(project_root: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "--untracked-files=normal",
                "--",
                *RESEARCH_CODE_PATHS,
            ],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return not result.stdout.strip()


class ProtocolStatus(StrEnum):
    DRAFT = "draft"
    FROZEN = "frozen"


class PartitionName(StrEnum):
    DEVELOPMENT = "development"
    VALIDATION = "validation"
    HOLDOUT = "holdout"
    FORWARD = "forward"


class ExperimentStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchPartition(BaseModel):
    name: PartitionName
    start_date: date
    end_date: date | None

    @model_validator(mode="after")
    def valid_dates(self) -> ResearchPartition:
        if self.end_date is not None and self.start_date > self.end_date:
            raise ValueError(f"{self.name.value} partition starts after it ends.")
        if self.name != PartitionName.FORWARD and self.end_date is None:
            raise ValueError(f"{self.name.value} partition requires an end date.")
        return self


class StrategyProtocol(BaseModel):
    protocol_id: str
    version: int = Field(default=1, ge=1)
    parent_protocol_id: str | None = None
    title: str
    hypothesis: str
    status: ProtocolStatus = ProtocolStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    frozen_at: datetime | None = None
    partitions: list[ResearchPartition]
    strategy: dict[str, Any]
    execution: dict[str, Any]
    acceptance_criteria: dict[str, Any]
    allowed_trials: int = Field(default=1, ge=1)
    code_commit: str
    config_hash: str
    data_version: str = "unfrozen"
    partition_data_versions: dict[PartitionName, str] = Field(default_factory=dict)
    content_hash: str | None = None

    @model_validator(mode="after")
    def valid_protocol(self) -> StrategyProtocol:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", self.protocol_id):
            raise ValueError(
                "Protocol id must be 3-64 lowercase letters, numbers, hyphens, or underscores."
            )
        names = [item.name for item in self.partitions]
        if len(names) != len(set(names)):
            raise ValueError("Protocol partitions must have unique names.")
        required = {
            PartitionName.DEVELOPMENT,
            PartitionName.VALIDATION,
            PartitionName.HOLDOUT,
        }
        if missing := required - set(names):
            raise ValueError(
                "Protocol is missing partitions: "
                + ", ".join(sorted(item.value for item in missing))
            )
        bounded = sorted(
            (
                item.start_date,
                item.end_date,
                item.name,
            )
            for item in self.partitions
            if item.end_date is not None
        )
        for previous, current in zip(bounded, bounded[1:], strict=False):
            if previous[1] >= current[0]:
                raise ValueError(
                    f"Protocol partitions overlap: {previous[2].value} and "
                    f"{current[2].value}."
                )
        if self.status == ProtocolStatus.FROZEN and (
            self.frozen_at is None or self.content_hash is None
        ):
            raise ValueError("Frozen protocol requires frozen_at and content_hash.")
        return self

    def partition(self, name: PartitionName) -> ResearchPartition:
        for item in self.partitions:
            if item.name == name:
                return item
        raise ValueError(f"Protocol has no {name.value} partition.")

    def hash_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"content_hash"})

    def calculated_hash(self) -> str:
        return canonical_hash(self.hash_payload())


class ProtocolState(BaseModel):
    protocol_id: str
    holdout_revealed_at: datetime | None = None
    contaminated_partitions: list[PartitionName] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)


class ExperimentRecord(BaseModel):
    experiment_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    protocol_id: str | None = None
    partition: PartitionName | None = None
    kind: str
    status: ExperimentStatus = ExperimentStatus.RUNNING
    start_date: date
    end_date: date
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    code_commit: str
    config_hash: str
    data_version: str | None = None
    protocol_hash: str | None = None
    result_json: str | None = None
    result_markdown: str | None = None
    error: str | None = None


class LeakageIssue(BaseModel):
    snapshot_date: date
    column: str
    offending_rows: int
    latest_value: date


class LeakageAudit(BaseModel):
    passed: bool
    checked_snapshots: int
    checked_availability_columns: list[str]
    issues: list[LeakageIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(temporary, path)


class ProtocolStore:
    def __init__(self, runtime_root: Path) -> None:
        self.root = Path(runtime_root) / "research" / "protocols"

    def _directory(self, protocol_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", protocol_id):
            raise ValueError("Invalid protocol id.")
        return self.root / protocol_id

    def create(self, protocol: StrategyProtocol) -> Path:
        if protocol.status != ProtocolStatus.DRAFT:
            raise ValueError("A new protocol must start as draft.")
        path = self._directory(protocol.protocol_id) / "protocol.yaml"
        if path.exists():
            raise FileExistsError(f"Protocol already exists: {protocol.protocol_id}")
        self._write_protocol(path, protocol)
        self._write_state(ProtocolState(protocol_id=protocol.protocol_id))
        return path

    def load(self, protocol_id: str, *, verify: bool = True) -> StrategyProtocol:
        path = self._directory(protocol_id) / "protocol.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Protocol not found: {protocol_id}")
        with path.open("r", encoding="utf-8") as stream:
            protocol = StrategyProtocol.model_validate(yaml.safe_load(stream))
        if verify and protocol.status == ProtocolStatus.FROZEN:
            actual = protocol.calculated_hash()
            if actual != protocol.content_hash:
                raise ValueError(
                    f"Frozen protocol hash mismatch: {protocol_id}; file may have been modified."
                )
        return protocol

    def freeze(
        self,
        protocol_id: str,
        *,
        data_version: str | None = None,
        code_commit: str | None = None,
        config_hash: str | None = None,
    ) -> StrategyProtocol:
        protocol = self.load(protocol_id)
        if protocol.status == ProtocolStatus.FROZEN:
            return protocol
        values = protocol.model_dump(mode="json", exclude={"content_hash"})
        values.update(
            status=ProtocolStatus.FROZEN.value,
            frozen_at=utc_now().isoformat(),
            data_version=data_version or protocol.data_version,
            code_commit=code_commit or protocol.code_commit,
            config_hash=config_hash or protocol.config_hash,
        )
        values["content_hash"] = "pending"
        normalized = StrategyProtocol.model_validate(values)
        frozen = normalized.model_copy(
            update={"content_hash": normalized.calculated_hash()}
        )
        self._write_protocol(
            self._directory(protocol_id) / "protocol.yaml",
            frozen,
        )
        return frozen

    def pin_data_version(
        self,
        protocol_id: str,
        partition: PartitionName,
        data_version: str,
    ) -> StrategyProtocol:
        protocol = self.load(protocol_id)
        if protocol.status != ProtocolStatus.DRAFT:
            raise ValueError("Data versions can only be pinned on a draft protocol.")
        if partition == PartitionName.FORWARD:
            raise ValueError("Forward observation data cannot be pinned in advance.")
        if not re.fullmatch(r"[0-9a-f]{64}", data_version):
            raise ValueError("Data version must be a 64-character lowercase SHA-256.")
        protocol.partition(partition)
        protocol.partition_data_versions[partition] = data_version
        self._write_protocol(
            self._directory(protocol_id) / "protocol.yaml",
            protocol,
        )
        return protocol

    def state(self, protocol_id: str) -> ProtocolState:
        self.load(protocol_id)
        path = self._directory(protocol_id) / "state.json"
        if not path.exists():
            state = ProtocolState(protocol_id=protocol_id)
            self._write_state(state)
            return state
        return ProtocolState.model_validate_json(path.read_text(encoding="utf-8"))

    def reveal_holdout(self, protocol_id: str) -> ProtocolState:
        protocol = self.load(protocol_id)
        if protocol.status != ProtocolStatus.FROZEN:
            raise ValueError("Protocol must be frozen before revealing holdout data.")
        state = self.state(protocol_id)
        if state.holdout_revealed_at is None:
            state.holdout_revealed_at = utc_now()
            state.updated_at = utc_now()
            self._write_state(state)
        return state

    def list(self) -> list[StrategyProtocol]:
        if not self.root.exists():
            return []
        protocols = []
        for path in sorted(self.root.glob("*/protocol.yaml")):
            protocols.append(self.load(path.parent.name))
        return protocols

    @staticmethod
    def _write_protocol(path: Path, protocol: StrategyProtocol) -> None:
        payload = protocol.model_dump(mode="json")
        _atomic_text(
            path,
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        )

    def _write_state(self, state: ProtocolState) -> None:
        path = self._directory(state.protocol_id) / "state.json"
        _atomic_text(
            path,
            json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )


class ExperimentStore:
    def __init__(self, runtime_root: Path) -> None:
        self.root = Path(runtime_root) / "research" / "experiments"

    def create(self, record: ExperimentRecord) -> ExperimentRecord:
        path = self.root / f"{record.experiment_id}.json"
        if path.exists():
            raise FileExistsError(f"Experiment already exists: {record.experiment_id}")
        self._write(record)
        return record

    def complete(
        self,
        experiment_id: str,
        *,
        data_version: str,
        result_json: Path,
        result_markdown: Path,
    ) -> ExperimentRecord:
        record = self.load(experiment_id)
        if record.status != ExperimentStatus.RUNNING:
            raise ValueError("Only a running experiment can be completed.")
        record.status = ExperimentStatus.COMPLETED
        record.completed_at = utc_now()
        record.data_version = data_version
        record.result_json = str(result_json)
        record.result_markdown = str(result_markdown)
        self._write(record)
        return record

    def fail(self, experiment_id: str, error: str) -> ExperimentRecord:
        record = self.load(experiment_id)
        if record.status != ExperimentStatus.RUNNING:
            return record
        record.status = ExperimentStatus.FAILED
        record.completed_at = utc_now()
        record.error = error
        self._write(record)
        return record

    def load(self, experiment_id: str) -> ExperimentRecord:
        path = self.root / f"{experiment_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Experiment not found: {experiment_id}")
        return ExperimentRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self, protocol_id: str | None = None) -> list[ExperimentRecord]:
        if not self.root.exists():
            return []
        records = [
            ExperimentRecord.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*.json"))
        ]
        if protocol_id is not None:
            records = [item for item in records if item.protocol_id == protocol_id]
        return records

    def _write(self, record: ExperimentRecord) -> None:
        _atomic_text(
            self.root / f"{record.experiment_id}.json",
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )


class TemporalLeakageAuditor:
    AVAILABILITY_COLUMNS = ("available_from", "ann_date", "announcement_date")

    @classmethod
    def audit_snapshots(
        cls,
        snapshots: list[tuple[date, pl.DataFrame]],
    ) -> LeakageAudit:
        issues: list[LeakageIssue] = []
        checked: set[str] = set()
        warnings: list[str] = []
        missing_available_from = 0
        for snapshot_date, frame in snapshots:
            if "available_from" not in frame.columns:
                missing_available_from += 1
            columns = [name for name in cls.AVAILABILITY_COLUMNS if name in frame.columns]
            if not columns:
                continue
            for column in columns:
                checked.add(column)
                values = (
                    frame.select(
                        pl.col(column)
                        .cast(pl.String)
                        .str.replace_all("-", "")
                        .str.to_date("%Y%m%d", strict=False)
                        .alias(column)
                    )
                    .get_column(column)
                )
                future = values.filter(values > snapshot_date)
                if future.len():
                    issues.append(
                        LeakageIssue(
                            snapshot_date=snapshot_date,
                            column=column,
                            offending_rows=future.len(),
                            latest_value=future.max(),
                        )
                    )
        if snapshots and missing_available_from:
            warnings.append(
                f"{missing_available_from}/{len(snapshots)} ranking snapshots lack "
                "available_from; complete input lineage cannot be verified."
            )
        if snapshots and not checked:
            warnings.append(
                "Ranking snapshots contain no availability column; upstream point-in-time "
                "construction cannot yet be independently verified."
            )
        return LeakageAudit(
            passed=not issues,
            checked_snapshots=len(snapshots),
            checked_availability_columns=sorted(checked),
            issues=issues,
            warnings=warnings,
        )


def frame_manifest_hash(frames: dict[str, pl.DataFrame]) -> str:
    manifest: dict[str, Any] = {}
    for name, frame in sorted(frames.items()):
        row_hashes = frame.hash_rows(seed=0)
        manifest[name] = {
            "rows": frame.height,
            "columns": frame.columns,
            "schema": {key: str(value) for key, value in frame.schema.items()},
            "row_hash_sum": int(row_hashes.sum()) if frame.height else 0,
            "row_hash_min": int(row_hashes.min()) if frame.height else 0,
            "row_hash_max": int(row_hashes.max()) if frame.height else 0,
        }
    return canonical_hash(manifest)
