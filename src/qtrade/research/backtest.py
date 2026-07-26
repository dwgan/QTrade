from __future__ import annotations

import math
import statistics
from datetime import date

import polars as pl

from qtrade.config import BacktestConfig
from qtrade.research.analyzer import adjusted_prices, date_expression
from qtrade.research.models import CandidateBacktestAnalysis, PerformanceMetrics


def _performance(values: list[float], annual_risk_free_rate: float) -> PerformanceMetrics:
    if not values:
        raise ValueError("Cannot calculate performance from an empty equity curve.")
    daily_returns = [
        values[index] / values[index - 1] - 1
        for index in range(1, len(values))
        if values[index - 1] > 0
    ]
    total_return = values[-1] / values[0] - 1 if values[0] else 0.0
    years = max(len(daily_returns) / 252, 1 / 252)
    annualized_return = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
    volatility = (
        statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) > 1 else 0.0
    )
    daily_risk_free = (1 + annual_risk_free_rate) ** (1 / 252) - 1
    sharpe = (
        (statistics.fmean(daily_returns) - daily_risk_free)
        / statistics.stdev(daily_returns)
        * math.sqrt(252)
        if len(daily_returns) > 1 and statistics.stdev(daily_returns) > 0
        else None
    )
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)
    return PerformanceMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=volatility,
        sharpe_ratio=sharpe,
        max_drawdown=max_drawdown,
        calmar_ratio=(
            annualized_return / abs(max_drawdown) if max_drawdown < 0 else None
        ),
    )


class CandidateBacktester:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config

    def _target_weights(self, ranking: pl.DataFrame) -> dict[str, float]:
        required = {"ts_code", "industry", "score"}
        if missing := required - set(ranking.columns):
            raise ValueError(f"Ranking is missing columns: {', '.join(sorted(missing))}")
        selected: list[str] = []
        industries: dict[str, int] = {}
        rows = ranking.sort(["score", "ts_code"], descending=[True, False]).to_dicts()
        for row in rows:
            industry = str(row["industry"] or "unknown")
            if industries.get(industry, 0) >= self.config.max_candidates_per_industry:
                continue
            selected.append(str(row["ts_code"]))
            industries[industry] = industries.get(industry, 0) + 1
            if len(selected) == self.config.candidate_count:
                break
        return {code: 1 / len(selected) for code in selected} if selected else {}

    def run(
        self,
        start_date: date,
        end_date: date,
        snapshots: list[tuple[date, pl.DataFrame]],
        prices: pl.DataFrame,
        adjustments: pl.DataFrame,
        index_daily: pl.DataFrame,
    ) -> tuple[CandidateBacktestAnalysis, pl.DataFrame, pl.DataFrame]:
        adjusted = adjusted_prices(prices, adjustments)
        adjusted = adjusted.filter(
            (pl.col("trade_date") >= start_date) & (pl.col("trade_date") <= end_date)
        )
        trading_dates = adjusted.get_column("trade_date").unique().sort().to_list()
        if len(trading_dates) < 2:
            raise ValueError("At least two trading dates are required for a backtest.")
        date_index = {value: index for index, value in enumerate(trading_dates)}
        price_lookup = {
            (row["trade_date"], row["ts_code"]): float(row["adjusted_close"])
            for row in adjusted.select("trade_date", "ts_code", "adjusted_close").to_dicts()
        }
        schedules: dict[date, tuple[date, dict[str, float]]] = {}
        warnings: list[str] = []
        for signal_date, ranking in snapshots:
            index = date_index.get(signal_date)
            if index is None or index + 1 >= len(trading_dates):
                warnings.append(f"{signal_date}: no next trading day; signal skipped.")
                continue
            execution_date = trading_dates[index + 1]
            schedules[execution_date] = (signal_date, self._target_weights(ranking))

        index_frame = (
            index_daily.filter(pl.col("ts_code") == self.config.benchmark_code)
            .with_columns(date_expression("trade_date"))
            .filter((pl.col("trade_date") >= start_date) & (pl.col("trade_date") <= end_date))
            .sort("trade_date")
        )
        benchmark_close = {
            row["trade_date"]: float(row["close"])
            for row in index_frame.select("trade_date", "close").to_dicts()
        }
        if len(benchmark_close) < 2:
            raise ValueError(
                f"Benchmark {self.config.benchmark_code} has fewer than two observations."
            )

        equity = self.config.initial_capital
        benchmark_equity = self.config.initial_capital
        holdings: dict[str, float] = {}
        previous_prices: dict[str, float] = {}
        previous_benchmark: float | None = None
        curve_rows: list[dict] = []
        trade_rows: list[dict] = []
        turnover_values: list[float] = []
        total_cost = 0.0

        for trade_date in trading_dates:
            returns: dict[str, float] = {}
            for code in holdings:
                current = price_lookup.get((trade_date, code))
                previous = previous_prices.get(code)
                returns[code] = current / previous - 1 if current and previous else 0.0
            portfolio_return = sum(holdings[code] * returns[code] for code in holdings)
            equity *= 1 + portfolio_return
            if holdings:
                gross = sum(holdings[code] * (1 + returns[code]) for code in holdings)
                holdings = {
                    code: holdings[code] * (1 + returns[code]) / gross
                    for code in holdings
                    if gross > 0
                }

            turnover = 0.0
            cost = 0.0
            signal_date: date | None = None
            if trade_date in schedules:
                signal_date, targets = schedules[trade_date]
                available = {
                    code: weight
                    for code, weight in targets.items()
                    if price_lookup.get((trade_date, code)) is not None
                }
                if len(available) != len(targets):
                    warnings.append(
                        f"{trade_date}: {len(targets) - len(available)} selected stocks "
                        "had no execution-date price and were skipped."
                    )
                available_total = sum(available.values())
                targets = {
                    code: weight / available_total for code, weight in available.items()
                }
                turnover = sum(
                    abs(targets.get(code, 0.0) - holdings.get(code, 0.0))
                    for code in set(targets) | set(holdings)
                )
                cost = equity * turnover * self.config.transaction_cost_rate
                equity -= cost
                total_cost += cost
                turnover_values.append(turnover)
                holdings = targets
                trade_rows.append(
                    {
                        "signal_date": signal_date,
                        "execution_date": trade_date,
                        "holdings": len(targets),
                        "turnover": turnover,
                        "cost": cost,
                        "equity_after_cost": equity,
                    }
                )

            current_benchmark = benchmark_close.get(trade_date)
            benchmark_return = (
                current_benchmark / previous_benchmark - 1
                if current_benchmark is not None and previous_benchmark is not None
                else 0.0
            )
            benchmark_equity *= 1 + benchmark_return
            if current_benchmark is not None:
                previous_benchmark = current_benchmark
            curve_rows.append(
                {
                    "trade_date": trade_date,
                    "equity": equity,
                    "benchmark_equity": benchmark_equity,
                    "daily_return": portfolio_return,
                    "benchmark_daily_return": benchmark_return,
                    "turnover": turnover,
                    "cost": cost,
                }
            )
            previous_prices = {
                code: current
                if (current := price_lookup.get((trade_date, code))) is not None
                else previous_prices[code]
                for code in holdings
                if price_lookup.get((trade_date, code)) is not None
                or code in previous_prices
            }

        curve = pl.DataFrame(curve_rows)
        analysis = CandidateBacktestAnalysis(
            start_date=start_date,
            end_date=end_date,
            benchmark_code=self.config.benchmark_code,
            initial_capital=self.config.initial_capital,
            final_equity=equity,
            execution_rule="signal close T; rebalance at next trading-day close T+1",
            transaction_cost_rate=self.config.transaction_cost_rate,
            rebalance_count=len(trade_rows),
            average_turnover=statistics.fmean(turnover_values) if turnover_values else 0.0,
            total_cost=total_cost,
            portfolio=_performance(
                curve.get_column("equity").to_list(),
                self.config.annual_risk_free_rate,
            ),
            benchmark=_performance(
                curve.get_column("benchmark_equity").to_list(),
                self.config.annual_risk_free_rate,
            ),
            warnings=warnings,
        )
        return analysis, curve, pl.DataFrame(trade_rows)
