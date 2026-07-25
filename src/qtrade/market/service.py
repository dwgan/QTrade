from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from qtrade.config import MarketConfig
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import Dataset
from qtrade.market.analyzer import MarketAnalyzer
from qtrade.market.models import MarketAnalysis
from qtrade.market.reporting import MarketReportWriter


@dataclass(frozen=True)
class MarketAnalysisResult:
    analysis: MarketAnalysis
    json_path: Path
    markdown_path: Path


class MarketAnalysisService:
    def __init__(
        self,
        config: MarketConfig,
        curated_store: ParquetDatasetStore,
        provider: str,
        reports_root: Path,
    ) -> None:
        self.config = config
        self.curated_store = curated_store
        self.provider = provider
        self.analyzer = MarketAnalyzer(config)
        self.reporter = MarketReportWriter(reports_root)

    def run(self, as_of_date: date) -> MarketAnalysisResult:
        start_date = as_of_date - timedelta(days=self.config.history_calendar_days)
        index_history = self.curated_store.read_range(
            Dataset.INDEX_DAILY,
            self.provider,
            start_date,
            as_of_date,
        )
        stock_history = self.curated_store.read_range(
            Dataset.DAILY_PRICES,
            self.provider,
            start_date,
            as_of_date,
        )
        analysis = self.analyzer.analyze(as_of_date, index_history, stock_history)
        json_path, markdown_path = self.reporter.write(analysis)
        return MarketAnalysisResult(analysis, json_path, markdown_path)
