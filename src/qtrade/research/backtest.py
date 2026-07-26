from __future__ import annotations

import math
import statistics
from datetime import date

import polars as pl

from qtrade.config import BacktestConfig
from qtrade.research.analyzer import adjusted_prices, date_expression
from qtrade.research.models import (
    CandidateBacktestAnalysis,
    CostSensitivityMetric,
    PerformanceMetrics,
    SamplePerformance,
)


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

    @staticmethod
    def _execution_lookups(
        prices: pl.DataFrame,
        stock_limits: pl.DataFrame | None,
    ) -> tuple[dict[tuple[date, str], float], dict[tuple[date, str], tuple[float, float]]]:
        close_frame = prices.select("ts_code", "trade_date", "close").with_columns(
            date_expression("trade_date")
        )
        closes = {
            (row["trade_date"], row["ts_code"]): float(row["close"])
            for row in close_frame.to_dicts()
        }
        limits: dict[tuple[date, str], tuple[float, float]] = {}
        if stock_limits is not None and not stock_limits.is_empty():
            required = {"ts_code", "trade_date", "up_limit", "down_limit"}
            if missing := required - set(stock_limits.columns):
                raise ValueError(
                    f"Stock limits are missing columns: {', '.join(sorted(missing))}"
                )
            limit_frame = stock_limits.select(
                "ts_code", "trade_date", "up_limit", "down_limit"
            ).with_columns(date_expression("trade_date"))
            limits = {
                (row["trade_date"], row["ts_code"]): (
                    float(row["up_limit"]),
                    float(row["down_limit"]),
                )
                for row in limit_frame.to_dicts()
            }
        return closes, limits

    @staticmethod
    def _attempt_rebalance(
        trade_date: date,
        holdings: dict[str, float],
        targets: dict[str, float],
        closes: dict[tuple[date, str], float],
        limits: dict[tuple[date, str], tuple[float, float]],
    ) -> tuple[dict[str, float], float, int, int]:
        updated = dict(holdings)
        blocked_sells = 0
        blocked_buys = 0

        def state(code: str) -> tuple[bool, bool, bool]:
            close = closes.get((trade_date, code))
            if close is None:
                return False, False, False
            limit = limits.get((trade_date, code))
            at_up = limit is not None and close >= limit[0] - 1e-8
            at_down = limit is not None and close <= limit[1] + 1e-8
            return True, at_up, at_down

        for code in set(holdings) | set(targets):
            current = holdings.get(code, 0.0)
            target = targets.get(code, 0.0)
            has_price, _, at_down = state(code)
            if target < current:
                if has_price and not at_down:
                    updated[code] = target
                else:
                    blocked_sells += 1

        available_cash = max(0.0, 1 - sum(updated.values()))
        buy_needs: dict[str, float] = {}
        for code in set(targets) | set(updated):
            current = updated.get(code, 0.0)
            target = targets.get(code, 0.0)
            if target <= current:
                continue
            has_price, at_up, _ = state(code)
            if has_price and not at_up:
                buy_needs[code] = target - current
            else:
                blocked_buys += 1
        total_need = sum(buy_needs.values())
        fill_ratio = min(1.0, available_cash / total_need) if total_need else 0.0
        for code, need in buy_needs.items():
            updated[code] = updated.get(code, 0.0) + need * fill_ratio
        updated = {code: weight for code, weight in updated.items() if weight > 1e-12}
        turnover = sum(
            abs(updated.get(code, 0.0) - holdings.get(code, 0.0))
            for code in set(updated) | set(holdings)
        )
        return updated, turnover, blocked_buys, blocked_sells

    def _sample_performance(
        self,
        curve: pl.DataFrame,
        split_date: date,
    ) -> list[SamplePerformance]:
        results: list[SamplePerformance] = []
        for label, sample in (
            ("in_sample", curve.filter(pl.col("trade_date") < split_date)),
            ("out_of_sample", curve.filter(pl.col("trade_date") >= split_date)),
        ):
            if sample.is_empty():
                continue
            if label == "in_sample":
                equity_base = self.config.initial_capital
                benchmark_base = self.config.initial_capital
            else:
                history = curve.filter(pl.col("trade_date") < split_date)
                equity_base = (
                    history.get_column("equity").tail(1).item()
                    if not history.is_empty()
                    else self.config.initial_capital
                )
                benchmark_base = (
                    history.get_column("benchmark_equity").tail(1).item()
                    if not history.is_empty()
                    else self.config.initial_capital
                )
            results.append(
                SamplePerformance(
                    sample=label,
                    start_date=sample.get_column("trade_date").min(),
                    end_date=sample.get_column("trade_date").max(),
                    portfolio=_performance(
                        [equity_base, *sample.get_column("equity").to_list()],
                        self.config.annual_risk_free_rate,
                    ),
                    benchmark=_performance(
                        [
                            benchmark_base,
                            *sample.get_column("benchmark_equity").to_list(),
                        ],
                        self.config.annual_risk_free_rate,
                    ),
                )
            )
        return results

    def _cost_sensitivity(self, curve: pl.DataFrame) -> list[CostSensitivityMetric]:
        results: list[CostSensitivityMetric] = []
        for multiplier in self.config.cost_sensitivity_multipliers:
            transaction_rate = self.config.transaction_cost_rate * multiplier
            total_rate = transaction_rate + self.config.slippage_rate
            equity = self.config.initial_capital
            values = [equity]
            for row in curve.select("daily_return", "turnover").to_dicts():
                equity *= 1 + float(row["daily_return"])
                equity *= max(0.0, 1 - float(row["turnover"]) * total_rate)
                values.append(equity)
            performance = _performance(values, self.config.annual_risk_free_rate)
            results.append(
                CostSensitivityMetric(
                    transaction_cost_rate=transaction_rate,
                    total_cost_rate=total_rate,
                    total_return=performance.total_return,
                    max_drawdown=performance.max_drawdown,
                )
            )
        return results

    def run(
        self,
        start_date: date,
        end_date: date,
        snapshots: list[tuple[date, pl.DataFrame]],
        prices: pl.DataFrame,
        adjustments: pl.DataFrame,
        index_daily: pl.DataFrame,
        stock_limits: pl.DataFrame | None = None,
        sample_split_date: date | None = None,
    ) -> tuple[CandidateBacktestAnalysis, pl.DataFrame, pl.DataFrame]:
        if sample_split_date is not None and not start_date < sample_split_date <= end_date:
            raise ValueError("Sample split date must be after start and on or before end.")
        adjusted = adjusted_prices(prices, adjustments).filter(
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
        closes, limits = self._execution_lookups(prices, stock_limits)
        schedules: dict[date, tuple[date, dict[str, float]]] = {}
        warnings: list[str] = []
        if stock_limits is None or stock_limits.is_empty():
            warnings.append("Stock limit data is unavailable; limit-up/down constraints disabled.")
        for signal_date, ranking in snapshots:
            index = date_index.get(signal_date)
            if index is None or index + 1 >= len(trading_dates):
                warnings.append(f"{signal_date}: no next trading day; signal skipped.")
                continue
            schedules[trading_dates[index + 1]] = (
                signal_date,
                self._target_weights(ranking),
            )

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
        pending_targets: dict[str, float] | None = None
        pending_signal_date: date | None = None
        pending_since: date | None = None
        curve_rows: list[dict] = []
        trade_rows: list[dict] = []
        turnover_values: list[float] = []
        total_cost = 0.0
        blocked_buys_total = 0
        blocked_sells_total = 0
        delayed_days = 0

        for trade_date in trading_dates:
            returns: dict[str, float] = {}
            for code in holdings:
                current = price_lookup.get((trade_date, code))
                previous = previous_prices.get(code)
                returns[code] = current / previous - 1 if current and previous else 0.0
            portfolio_return = sum(holdings[code] * returns[code] for code in holdings)
            equity *= 1 + portfolio_return
            cash_weight = max(0.0, 1 - sum(holdings.values()))
            gross = cash_weight + sum(
                holdings[code] * (1 + returns[code]) for code in holdings
            )
            if holdings and gross > 0:
                holdings = {
                    code: holdings[code] * (1 + returns[code]) / gross
                    for code in holdings
                }

            if trade_date in schedules:
                pending_signal_date, pending_targets = schedules[trade_date]
                pending_since = trade_date

            turnover = 0.0
            transaction_cost = 0.0
            slippage_cost = 0.0
            blocked_buys = 0
            blocked_sells = 0
            if pending_targets is not None and pending_signal_date is not None:
                holdings, turnover, blocked_buys, blocked_sells = self._attempt_rebalance(
                    trade_date,
                    holdings,
                    pending_targets,
                    closes,
                    limits,
                )
                transaction_cost = (
                    equity * turnover * self.config.transaction_cost_rate
                )
                slippage_cost = equity * turnover * self.config.slippage_rate
                cost = transaction_cost + slippage_cost
                equity -= cost
                total_cost += cost
                blocked_buys_total += blocked_buys
                blocked_sells_total += blocked_sells
                if pending_since is not None and trade_date > pending_since:
                    delayed_days += 1
                if turnover > 0:
                    turnover_values.append(turnover)
                trade_rows.append(
                    {
                        "signal_date": pending_signal_date,
                        "execution_date": trade_date,
                        "status": (
                            "partial"
                            if blocked_buys or blocked_sells
                            else "completed"
                        ),
                        "holdings": len(holdings),
                        "holding_codes": sorted(holdings),
                        "turnover": turnover,
                        "transaction_cost": transaction_cost,
                        "slippage_cost": slippage_cost,
                        "blocked_buys": blocked_buys,
                        "blocked_sells": blocked_sells,
                        "equity_after_cost": equity,
                    }
                )
                if blocked_buys == 0 and blocked_sells == 0:
                    pending_targets = None
                    pending_signal_date = None
                    pending_since = None
            else:
                cost = 0.0

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
                    "cash_weight": max(0.0, 1 - sum(holdings.values())),
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
        if sample_split_date is None and len(trading_dates) >= 3:
            split_index = min(
                len(trading_dates) - 1,
                max(1, int(len(trading_dates) * self.config.sample_split_ratio)),
            )
            sample_split_date = trading_dates[split_index]
        analysis = CandidateBacktestAnalysis(
            start_date=start_date,
            end_date=end_date,
            benchmark_code=self.config.benchmark_code,
            initial_capital=self.config.initial_capital,
            final_equity=equity,
            execution_rule=(
                "signal close T; attempt at T+1 close; blocked orders retry daily"
            ),
            transaction_cost_rate=self.config.transaction_cost_rate,
            slippage_rate=self.config.slippage_rate,
            rebalance_count=len(turnover_values),
            average_turnover=statistics.fmean(turnover_values) if turnover_values else 0.0,
            total_cost=total_cost,
            blocked_buy_orders=blocked_buys_total,
            blocked_sell_orders=blocked_sells_total,
            delayed_execution_days=delayed_days,
            sample_split_date=sample_split_date,
            portfolio=_performance(
                [self.config.initial_capital, *curve.get_column("equity").to_list()],
                self.config.annual_risk_free_rate,
            ),
            benchmark=_performance(
                [
                    self.config.initial_capital,
                    *curve.get_column("benchmark_equity").to_list(),
                ],
                self.config.annual_risk_free_rate,
            ),
            sample_performance=(
                self._sample_performance(curve, sample_split_date)
                if sample_split_date is not None
                else []
            ),
            cost_sensitivity=self._cost_sensitivity(curve),
            warnings=warnings,
        )
        return analysis, curve, pl.DataFrame(trade_rows)
