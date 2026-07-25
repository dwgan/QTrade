from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from qtrade.config import IndustryConfig
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import Dataset
from qtrade.industry.analyzer import IndustryAnalyzer
from qtrade.industry.models import IndustryAnalysis
from qtrade.industry.reporting import IndustryReportWriter


@dataclass(frozen=True)
class IndustryAnalysisResult:
    analysis: IndustryAnalysis
    json_path: Path
    markdown_path: Path


class IndustryAnalysisService:
    def __init__(
        self,
        config: IndustryConfig,
        benchmark_code: str,
        curated_store: ParquetDatasetStore,
        provider: str,
        reports_root: Path,
    ) -> None:
        self.config = config
        self.curated_store = curated_store
        self.provider = provider
        self.analyzer = IndustryAnalyzer(config, benchmark_code)
        self.reporter = IndustryReportWriter(reports_root, config.top_count)

    def run(self, as_of_date: date) -> IndustryAnalysisResult:
        start_date = as_of_date - timedelta(days=self.config.history_calendar_days)
        stock_history = self.curated_store.read_range(
            Dataset.DAILY_PRICES,
            self.provider,
            start_date,
            as_of_date,
        )
        index_history = self.curated_store.read_range(
            Dataset.INDEX_DAILY,
            self.provider,
            start_date,
            as_of_date,
        )
        snapshot_date, security_master = self.curated_store.read_latest_on_or_before(
            Dataset.SECURITY_MASTER,
            self.provider,
            as_of_date,
        )
        analysis = self.analyzer.analyze(
            as_of_date,
            stock_history,
            index_history,
            security_master,
            snapshot_date,
        )
        json_path, markdown_path = self.reporter.write(analysis)
        return IndustryAnalysisResult(analysis, json_path, markdown_path)
