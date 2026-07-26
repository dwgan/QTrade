from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from qtrade.config import BacktestConfig, ResearchConfig
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import Dataset
from qtrade.research.analyzer import FactorResearchAnalyzer
from qtrade.research.backtest import CandidateBacktester
from qtrade.research.models import CandidateBacktestAnalysis, FactorResearchAnalysis
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
    ) -> None:
        self.research_config = research_config
        self.backtest_config = backtest_config
        self.curated_store = curated_store
        self.provider = provider
        self.snapshots = FactorSnapshotStore(reports_root)
        self.reporter = ResearchReportWriter(reports_root)

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

    def backtest_candidates(
        self,
        start_date: date,
        end_date: date,
        sample_split_date: date | None = None,
    ) -> CandidateBacktestResult:
        snapshots = self._snapshots(start_date, end_date)
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
        json_path, markdown_path = self.reporter.write_backtest(analysis, curve, trades)
        return CandidateBacktestResult(analysis, json_path, markdown_path)
