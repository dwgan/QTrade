from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from qtrade.config import BacktestConfig, ResearchConfig
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import Dataset
from qtrade.research.analyzer import FactorResearchAnalyzer
from qtrade.research.backtest import CandidateBacktester
from qtrade.research.models import CandidateBacktestAnalysis, FactorResearchAnalysis
from qtrade.research.protocols import (
    ExperimentRecord,
    ExperimentStore,
    PartitionName,
    ProtocolStatus,
    ProtocolStore,
    TemporalLeakageAuditor,
    canonical_hash,
    current_git_commit,
    frame_manifest_hash,
    git_research_tree_is_clean,
)
from qtrade.research.reporting import ResearchReportWriter
from qtrade.research.snapshots import FactorSnapshotStore


@dataclass(frozen=True)
class FactorResearchResult:
    analysis: FactorResearchAnalysis
    json_path: Path
    markdown_path: Path


@dataclass(frozen=True)
class CandidateBacktestResult:
    analysis: CandidateBacktestAnalysis
    json_path: Path
    markdown_path: Path


class ResearchService:
    def __init__(
        self,
        research_config: ResearchConfig,
        backtest_config: BacktestConfig,
        curated_store: ParquetDatasetStore,
        provider: str,
        reports_root: Path,
        runtime_root: Path | None = None,
        project_root: Path | None = None,
        factor_config: dict[str, Any] | None = None,
    ) -> None:
        self.research_config = research_config
        self.backtest_config = backtest_config
        self.curated_store = curated_store
        self.provider = provider
        self.project_root = Path(project_root or Path.cwd())
        self.factor_config = factor_config or {}
        self.snapshots = FactorSnapshotStore(reports_root)
        self.reporter = ResearchReportWriter(reports_root)
        runtime = Path(runtime_root or Path(reports_root).parent / "runtime")
        self.protocols = ProtocolStore(runtime)
        self.experiments = ExperimentStore(runtime)

    def config_hash(self) -> str:
        return canonical_hash(
            {
                "factors": self.factor_config,
                "research": self.research_config.model_dump(mode="json"),
                "backtest": self.backtest_config.model_dump(mode="json"),
            }
        )

    def _snapshots(self, start_date: date, end_date: date):
        dates = self.snapshots.available_dates(start_date, end_date)
        if not dates:
            raise FileNotFoundError(
                f"No archived factor rankings found from {start_date} to {end_date}."
            )
        return [(value, self.snapshots.read(value)) for value in dates]

    def research_factors(self, start_date: date, end_date: date) -> FactorResearchResult:
        snapshots = self._snapshots(start_date, end_date)
        buffer_days = self.research_config.forward_horizon_days * 2 + 15
        price_end = end_date + timedelta(days=buffer_days)
        prices = self.curated_store.read_range(
            Dataset.DAILY_PRICES, self.provider, start_date, price_end
        )
        adjustments = self.curated_store.read_range(
            Dataset.ADJUST_FACTORS, self.provider, start_date, price_end
        )
        analysis, ic_detail, return_detail = FactorResearchAnalyzer(
            self.research_config
        ).analyze(start_date, end_date, snapshots, prices, adjustments)
        json_path, markdown_path = self.reporter.write_factor(
            analysis, ic_detail, return_detail
        )
        return FactorResearchResult(analysis, json_path, markdown_path)

    def candidate_data_version(
        self,
        start_date: date,
        end_date: date,
    ) -> str:
        snapshots = self._snapshots(start_date, end_date)
        signal_manifests = {
            snapshot_date: self.snapshots.manifest(snapshot_date)
            for snapshot_date, _ in snapshots
        }
        if any(manifest is None for manifest in signal_manifests.values()):
            raise ValueError(
                "Data version pinning requires immutable version manifests for "
                "every ranking snapshot."
            )
        audit = TemporalLeakageAuditor.audit_snapshots(snapshots)
        if not audit.passed or audit.warnings:
            details = (
                f"{audit.issues[0].column} contains future rows."
                if audit.issues
                else " ".join(audit.warnings)
            )
            raise ValueError(
                "Data version pinning requires complete point-in-time provenance: "
                + details
            )
        prices = self.curated_store.read_range(
            Dataset.DAILY_PRICES, self.provider, start_date, end_date
        )
        adjustments = self.curated_store.read_range(
            Dataset.ADJUST_FACTORS, self.provider, start_date, end_date
        )
        index_daily = self.curated_store.read_range(
            Dataset.INDEX_DAILY, self.provider, start_date, end_date
        )
        stock_limits = None
        with suppress(FileNotFoundError):
            stock_limits = self.curated_store.read_range(
                Dataset.STOCK_LIMIT, self.provider, start_date, end_date
            )
        frames = {
            "daily_prices": prices,
            "adjust_factors": adjustments,
            "index_daily": index_daily,
            **{
                f"ranking_{snapshot_date.isoformat()}": ranking
                for snapshot_date, ranking in snapshots
            },
        }
        if stock_limits is not None:
            frames["stock_limit"] = stock_limits
        signal_versions = {
            snapshot_date.isoformat(): manifest["signal_id"]
            for snapshot_date, manifest in signal_manifests.items()
            if manifest is not None
        }
        return canonical_hash(
            {
                "frames": frame_manifest_hash(frames),
                "signal_versions": signal_versions,
            }
        )

    def backtest_candidates(
        self,
        start_date: date,
        end_date: date,
        sample_split_date: date | None = None,
        protocol_id: str | None = None,
        partition: PartitionName | None = None,
        reveal_holdout: bool = False,
    ) -> CandidateBacktestResult:
        protocol = None
        protocol_hash = None
        code_commit = current_git_commit(self.project_root)
        config_hash = self.config_hash()
        if protocol_id is not None:
            if partition is None:
                raise ValueError("A protocol backtest requires --partition.")
            protocol = self.protocols.load(protocol_id)
            if protocol.status != ProtocolStatus.FROZEN:
                raise ValueError("A formal backtest requires a frozen protocol.")
            if not git_research_tree_is_clean(self.project_root):
                raise ValueError(
                    "Formal backtest requires a clean Git worktree for src, config, "
                    "and pyproject.toml. Commit the research implementation first."
                )
            expected = protocol.partition(partition)
            if expected.end_date is None:
                raise ValueError("Forward partitions are not supported by historical backtest.")
            if start_date != expected.start_date or end_date != expected.end_date:
                raise ValueError(
                    f"Requested dates must exactly match the frozen {partition.value} "
                    f"partition: {expected.start_date} to {expected.end_date}."
                )
            if protocol.config_hash != config_hash:
                raise ValueError(
                    "Current strategy/research/backtest configuration does not match "
                    "the frozen protocol."
                )
            if (
                protocol.code_commit != "unknown"
                and code_commit != "unknown"
                and protocol.code_commit != code_commit
            ):
                raise ValueError(
                    f"Current code commit {code_commit} does not match frozen protocol "
                    f"commit {protocol.code_commit}."
                )
            if (
                partition in {PartitionName.VALIDATION, PartitionName.HOLDOUT}
                and partition not in protocol.partition_data_versions
                and protocol.data_version == "unfrozen"
            ):
                raise ValueError(
                    f"Frozen protocol has no pinned data version for "
                    f"{partition.value}. Run an exploratory backtest, pin its data "
                    "version on the draft protocol, then freeze."
                )
            if partition == PartitionName.HOLDOUT:
                state = self.protocols.state(protocol_id)
                if state.holdout_revealed_at is None:
                    if not reveal_holdout:
                        raise ValueError(
                            "Holdout is sealed. Pass --reveal-holdout for the one-time reveal."
                        )
                    self.protocols.reveal_holdout(protocol_id)
            if partition == PartitionName.VALIDATION:
                validation_trials = [
                    item
                    for item in self.experiments.list(protocol_id)
                    if item.partition == PartitionName.VALIDATION
                ]
                if len(validation_trials) >= protocol.allowed_trials:
                    raise ValueError(
                        f"Protocol validation trial limit reached "
                        f"({protocol.allowed_trials}). Create a new protocol version."
                    )
            protocol_hash = protocol.content_hash
        elif partition is not None or reveal_holdout:
            raise ValueError("--partition and --reveal-holdout require --protocol.")

        record = self.experiments.create(
            ExperimentRecord(
                protocol_id=protocol_id,
                partition=partition,
                kind="candidate_backtest",
                start_date=start_date,
                end_date=end_date,
                code_commit=code_commit,
                config_hash=config_hash,
                protocol_hash=protocol_hash,
            )
        )
        try:
            snapshots = self._snapshots(start_date, end_date)
            signal_manifests = {
                snapshot_date: self.snapshots.manifest(snapshot_date)
                for snapshot_date, _ in snapshots
            }
            if protocol is not None and any(
                manifest is None for manifest in signal_manifests.values()
            ):
                raise ValueError(
                    "Formal backtest requires immutable version manifests for every "
                    "ranking snapshot. Regenerate legacy snapshots first."
                )
            audit = TemporalLeakageAuditor.audit_snapshots(snapshots)
            if not audit.passed:
                first = audit.issues[0]
                raise ValueError(
                    f"Temporal leakage detected in {first.column}: "
                    f"{first.offending_rows} rows on snapshot {first.snapshot_date}."
                )
            if protocol is not None and audit.warnings:
                raise ValueError(
                    "Formal backtest requires complete point-in-time ranking provenance: "
                    + " ".join(audit.warnings)
                )
            prices = self.curated_store.read_range(
                Dataset.DAILY_PRICES, self.provider, start_date, end_date
            )
            adjustments = self.curated_store.read_range(
                Dataset.ADJUST_FACTORS, self.provider, start_date, end_date
            )
            index_daily = self.curated_store.read_range(
                Dataset.INDEX_DAILY, self.provider, start_date, end_date
            )
            stock_limits = None
            with suppress(FileNotFoundError):
                stock_limits = self.curated_store.read_range(
                    Dataset.STOCK_LIMIT, self.provider, start_date, end_date
                )
            frames = {
                "daily_prices": prices,
                "adjust_factors": adjustments,
                "index_daily": index_daily,
                **{
                    f"ranking_{snapshot_date.isoformat()}": ranking
                    for snapshot_date, ranking in snapshots
                },
            }
            if stock_limits is not None:
                frames["stock_limit"] = stock_limits
            signal_versions = {
                snapshot_date.isoformat(): manifest["signal_id"]
                for snapshot_date, manifest in signal_manifests.items()
                if manifest is not None
            }
            signal_origins = sorted(
                {
                    str(manifest["origin"])
                    for manifest in signal_manifests.values()
                    if manifest is not None
                }
            )
            data_version = canonical_hash(
                {
                    "frames": frame_manifest_hash(frames),
                    "signal_versions": signal_versions,
                }
            )
            if protocol is not None and partition is not None:
                expected_data_version = protocol.partition_data_versions.get(partition)
                if expected_data_version is None and partition in {
                    PartitionName.VALIDATION,
                    PartitionName.HOLDOUT,
                }:
                    raise ValueError(
                        f"Frozen protocol has no pinned data version for "
                        f"{partition.value}. Run an exploratory backtest, pin its "
                        "data version on the draft protocol, then freeze."
                    )
                expected_data_version = expected_data_version or (
                    protocol.data_version
                    if protocol.data_version != "unfrozen"
                    else None
                )
                if (
                    expected_data_version is not None
                    and expected_data_version != data_version
                ):
                    raise ValueError(
                        "Loaded data and signal versions do not match the version "
                        f"pinned for {partition.value}."
                    )
            analysis, curve, trades = CandidateBacktester(self.backtest_config).run(
                start_date,
                end_date,
                snapshots,
                prices,
                adjustments,
                index_daily,
                stock_limits,
                sample_split_date,
            )
            analysis = analysis.model_copy(
                update={
                    "experiment_id": record.experiment_id,
                    "protocol_id": protocol_id,
                    "protocol_hash": protocol_hash,
                    "research_partition": partition.value if partition else "exploratory",
                    "code_commit": code_commit,
                    "config_hash": config_hash,
                    "data_version": data_version,
                    "signal_versions": signal_versions,
                    "signal_origins": signal_origins,
                    "leakage_audit_passed": audit.passed,
                    "leakage_audit_warnings": audit.warnings,
                }
            )
            json_path, markdown_path = self.reporter.write_backtest(
                analysis, curve, trades
            )
            self.experiments.complete(
                record.experiment_id,
                data_version=data_version,
                result_json=json_path,
                result_markdown=markdown_path,
            )
            return CandidateBacktestResult(analysis, json_path, markdown_path)
        except Exception as exc:
            self.experiments.fail(record.experiment_id, str(exc))
            raise
