from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from qtrade.config import BacktestConfig, ObservationConfig
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import Dataset
from qtrade.observation.analyzer import ObservationAnalyzer
from qtrade.observation.models import DailyObservation, ShadowPortfolioSummary
from qtrade.observation.reporting import ObservationReportWriter
from qtrade.research.backtest import CandidateBacktester
from qtrade.research.snapshots import FactorSnapshotStore


@dataclass(frozen=True)
class DailyObservationResult:
    observation: DailyObservation
    json_path: Path
    markdown_path: Path


class ObservationService:
    def __init__(
        self,
        observation_config: ObservationConfig,
        backtest_config: BacktestConfig,
        curated_store: ParquetDatasetStore,
        provider: str,
        reports_root: Path,
    ) -> None:
        self.config = observation_config
        self.backtest_config = backtest_config
        self.curated_store = curated_store
        self.provider = provider
        self.snapshots = FactorSnapshotStore(reports_root)
        self.reporter = ObservationReportWriter(reports_root)

    @staticmethod
    def _shadow_summary(
        analysis,
        curve: pl.DataFrame,
        trades: pl.DataFrame,
    ) -> ShadowPortfolioSummary:
        latest = curve.tail(1).row(0, named=True)
        holdings: list[str] = []
        last_execution_date = None
        if not trades.is_empty():
            last_trade = trades.tail(1).row(0, named=True)
            holdings = list(last_trade["holding_codes"])
            last_execution_date = last_trade["execution_date"]
        return ShadowPortfolioSummary(
            start_date=analysis.start_date,
            end_date=analysis.end_date,
            equity=float(latest["equity"]),
            benchmark_equity=float(latest["benchmark_equity"]),
            total_return=analysis.portfolio.total_return,
            benchmark_return=analysis.benchmark.total_return,
            max_drawdown=analysis.portfolio.max_drawdown,
            rebalance_count=analysis.rebalance_count,
            holdings=holdings,
            cash_weight=float(latest["cash_weight"]),
            last_execution_date=last_execution_date,
        )

    def run(self, as_of_date: date) -> DailyObservationResult:
        lookback_start = as_of_date - timedelta(
            days=self.config.shadow_lookback_calendar_days
        )
        snapshot_dates = self.snapshots.available_dates(lookback_start, as_of_date)
        if not snapshot_dates or snapshot_dates[-1] != as_of_date:
            raise FileNotFoundError(
                f"Factor ranking snapshot for {as_of_date} is required before observation."
            )
        current = self.snapshots.read(as_of_date)
        previous_date = snapshot_dates[-2] if len(snapshot_dates) >= 2 else None
        previous = self.snapshots.read(previous_date) if previous_date is not None else None
        entered, exited, movers, watchlist = ObservationAnalyzer(self.config).analyze(
            current, previous
        )
        warnings: list[str] = []
        if previous_date is None:
            warnings.append("No prior factor snapshot is available; changes cannot be compared.")
        if not self.config.watchlist_symbols:
            warnings.append("Watchlist is empty; configure observation.watchlist_symbols.")

        shadow = None
        shadow_curve = None
        shadow_trades = None
        if len(snapshot_dates) >= 2:
            shadow_start = snapshot_dates[0]
            try:
                prices = self.curated_store.read_range(
                    Dataset.DAILY_PRICES, self.provider, shadow_start, as_of_date
                )
                adjustments = self.curated_store.read_range(
                    Dataset.ADJUST_FACTORS, self.provider, shadow_start, as_of_date
                )
                index_daily = self.curated_store.read_range(
                    Dataset.INDEX_DAILY, self.provider, shadow_start, as_of_date
                )
                stock_limits = None
                with suppress(FileNotFoundError):
                    stock_limits = self.curated_store.read_range(
                        Dataset.STOCK_LIMIT,
                        self.provider,
                        shadow_start,
                        as_of_date,
                    )
                analysis, curve, trades = CandidateBacktester(self.backtest_config).run(
                    shadow_start,
                    as_of_date,
                    [
                        (value, self.snapshots.read(value))
                        for value in snapshot_dates
                        if value < as_of_date
                    ],
                    prices,
                    adjustments,
                    index_daily,
                    stock_limits,
                )
                shadow = self._shadow_summary(analysis, curve, trades)
                shadow_curve = curve
                shadow_trades = trades
                warnings.extend(analysis.warnings)
            except (FileNotFoundError, ValueError) as exc:
                warnings.append(f"Shadow portfolio unavailable: {exc}")
        else:
            warnings.append("At least two factor snapshots are required for a shadow portfolio.")

        observation = DailyObservation(
            as_of_date=as_of_date,
            current_snapshot_date=as_of_date,
            previous_snapshot_date=previous_date,
            entered_candidates=entered,
            exited_candidates=exited,
            rank_movers=movers,
            watchlist=watchlist,
            shadow_portfolio=shadow,
            warnings=warnings,
        )
        json_path, markdown_path = self.reporter.write(
            observation,
            shadow_curve,
            shadow_trades,
        )
        return DailyObservationResult(observation, json_path, markdown_path)
