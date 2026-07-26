from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import polars as pl

from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import Dataset
from qtrade.research.protocols import (
    PartitionName,
    ProtocolStatus,
    ProtocolStore,
    current_git_commit,
    git_research_tree_is_clean,
)


class SignalFrequency(StrEnum):
    MONTH_END = "month_end"
    WEEK_END = "week_end"


@dataclass(frozen=True)
class BuiltSignal:
    signal_date: date
    rankings_path: Path


@dataclass
class HistoricalSignalBuildResult:
    protocol_id: str
    partition: PartitionName
    frequency: SignalFrequency
    requested_dates: int
    signals: list[BuiltSignal] = field(default_factory=list)


class HistoricalSignalBuildService:
    def __init__(
        self,
        factor_service: Any,
        curated_store: ParquetDatasetStore,
        provider: str,
        protocol_store: ProtocolStore,
        project_root: Path,
        config_hash: str,
    ) -> None:
        self.factor_service = factor_service
        self.curated_store = curated_store
        self.provider = provider
        self.protocol_store = protocol_store
        self.project_root = Path(project_root)
        self.config_hash = config_hash

    def build(
        self,
        protocol_id: str,
        partition: PartitionName,
        frequency: SignalFrequency,
    ) -> HistoricalSignalBuildResult:
        protocol = self.protocol_store.load(protocol_id)
        if protocol.status != ProtocolStatus.DRAFT:
            raise ValueError(
                "Historical signals must be generated and pinned while the protocol "
                "is draft; freeze it immediately afterward."
            )
        if not git_research_tree_is_clean(self.project_root):
            raise ValueError(
                "Historical signal generation requires a clean Git worktree for "
                "src, config, and pyproject.toml."
            )
        commit = current_git_commit(self.project_root)
        if protocol.code_commit not in {"unknown", commit}:
            raise ValueError(
                "Draft protocol code commit does not match the current Git commit. "
                "Create a new protocol draft from the current implementation."
            )
        if protocol.config_hash != self.config_hash:
            raise ValueError(
                "Draft protocol configuration does not match current factor/research/"
                "backtest configuration."
            )
        frozen_frequency = protocol.strategy.get("signal_frequency")
        if frozen_frequency != frequency.value:
            raise ValueError(
                f"Requested frequency {frequency.value} does not match protocol "
                f"frequency {frozen_frequency or 'missing'}."
            )
        selected = protocol.partition(partition)
        if selected.end_date is None:
            raise ValueError("Cannot reconstruct an open-ended forward partition.")
        dates = self.rebalance_dates(
            selected.start_date,
            selected.end_date,
            frequency,
        )
        result = HistoricalSignalBuildResult(
            protocol_id=protocol_id,
            partition=partition,
            frequency=frequency,
            requested_dates=len(dates),
        )
        for signal_date in dates:
            try:
                built = self.factor_service.run(
                    signal_date,
                    signal_origin="reconstructed",
                    protocol_id=protocol_id,
                    code_commit=commit,
                    config_hash=self.config_hash,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Historical signal build stopped at {signal_date}: {exc}"
                ) from exc
            result.signals.append(
                BuiltSignal(
                    signal_date=signal_date,
                    rankings_path=built.rankings_path,
                )
            )
        return result

    def rebalance_dates(
        self,
        start_date: date,
        end_date: date,
        frequency: SignalFrequency,
    ) -> list[date]:
        calendar = self.curated_store.read_all(
            Dataset.TRADE_CALENDAR,
            self.provider,
        )
        required = {"cal_date", "is_open"}
        if missing := required - set(calendar.columns):
            raise ValueError(
                f"Trade calendar is missing columns: {', '.join(sorted(missing))}"
            )
        dates = (
            calendar.select(
                pl.col("cal_date")
                .cast(pl.String)
                .str.replace_all("-", "")
                .str.strptime(pl.Date, "%Y%m%d", strict=False),
                pl.col("is_open").cast(pl.Int8, strict=False),
            )
            .filter(
                (pl.col("is_open") == 1)
                & pl.col("cal_date").is_between(start_date, end_date)
            )
            .get_column("cal_date")
            .drop_nulls()
            .unique()
            .sort()
            .to_list()
        )
        grouped: dict[tuple[int, int], date] = {}
        for trading_date in dates:
            if frequency == SignalFrequency.MONTH_END:
                key = (trading_date.year, trading_date.month)
            else:
                iso = trading_date.isocalendar()
                key = (iso.year, iso.week)
            grouped[key] = trading_date
        return list(grouped.values())
