from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from qtrade.config import FactorConfig
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import Dataset
from qtrade.factors.analyzer import FactorAnalyzer
from qtrade.factors.models import FactorAnalysis, SignalOrigin
from qtrade.factors.reporting import FactorReportWriter
from qtrade.factors.universe import PointInTimeUniverseBuilder


@dataclass(frozen=True)
class FactorAnalysisResult:
    analysis: FactorAnalysis
    json_path: Path
    markdown_path: Path
    rankings_path: Path


class FactorAnalysisService:
    supports_signal_origin = True

    def __init__(
        self,
        config: FactorConfig,
        curated_store: ParquetDatasetStore,
        provider: str,
        reports_root: Path,
    ) -> None:
        self.config = config
        self.curated_store = curated_store
        self.provider = provider
        self.analyzer = FactorAnalyzer(config)
        self.universe_builder = PointInTimeUniverseBuilder(
            config.universe_index_codes
        )
        self.reporter = FactorReportWriter(reports_root)

    def run(
        self,
        as_of_date: date,
        signal_origin: SignalOrigin | str = SignalOrigin.RECONSTRUCTED,
    ) -> FactorAnalysisResult:
        signal_origin = SignalOrigin(signal_origin)
        start_date = as_of_date - timedelta(days=self.config.history_calendar_days)
        prices = self.curated_store.read_range(
            Dataset.DAILY_PRICES, self.provider, start_date, as_of_date
        )
        adjustments = self.curated_store.read_range(
            Dataset.ADJUST_FACTORS, self.provider, start_date, as_of_date
        )
        basic_date, daily_basic = self.curated_store.read_latest_on_or_before(
            Dataset.DAILY_BASIC, self.provider, as_of_date
        )
        financial_date, financials = self.curated_store.read_latest_on_or_before(
            Dataset.FINANCIAL_INDICATORS, self.provider, as_of_date
        )
        master_date, master = self.curated_store.read_latest_on_or_before(
            Dataset.SECURITY_MASTER, self.provider, as_of_date
        )
        names_date, names = self.curated_store.read_latest_on_or_before(
            Dataset.SECURITY_NAMES, self.provider, as_of_date
        )
        members_date, members = self.curated_store.read_latest_on_or_before(
            Dataset.INDEX_MEMBERS, self.provider, as_of_date
        )
        universe = self.universe_builder.build(
            as_of_date,
            master,
            names,
            members,
        )
        stock_limits = None
        try:
            limit_date, limit_frame = self.curated_store.read_latest_on_or_before(
                Dataset.STOCK_LIMIT, self.provider, as_of_date
            )
            if limit_date == as_of_date:
                stock_limits = limit_frame
        except FileNotFoundError:
            pass

        computation = self.analyzer.analyze(
            as_of_date,
            prices,
            adjustments,
            daily_basic,
            financials,
            universe.frame,
            stock_limits,
            basic_date,
            financial_date,
            master_date,
        )
        computation.analysis.security_names_snapshot_date = names_date
        computation.analysis.index_members_snapshot_date = members_date
        computation.analysis.universe_index_codes = universe.audit.index_codes
        computation.analysis.index_membership_dates = (
            universe.audit.index_membership_dates
        )
        computation.analysis.warnings.extend(universe.audit.warnings)
        computation.analysis.signal_origin = signal_origin
        json_path, markdown_path, rankings_path = self.reporter.write(computation)
        return FactorAnalysisResult(
            computation.analysis,
            json_path,
            markdown_path,
            rankings_path,
        )
