from __future__ import annotations

import math
import statistics
from datetime import date

import polars as pl

from qtrade.config import MarketConfig
from qtrade.market.models import (
    BreadthMetrics,
    IndexMetrics,
    MarketAnalysis,
    MarketState,
    RiskMetrics,
)


class MarketAnalyzer:
    def __init__(self, config: MarketConfig) -> None:
        self.config = config

    def analyze(
        self,
        as_of_date: date,
        index_history: pl.DataFrame,
        stock_history: pl.DataFrame,
    ) -> MarketAnalysis:
        indices = self._prepare_history(index_history, as_of_date)
        stocks = self._prepare_history(stock_history, as_of_date)
        warnings: list[str] = []

        index_metrics = []
        for code in self.config.index_codes:
            history = indices.filter(pl.col("ts_code") == code)
            if not history.is_empty():
                index_metrics.append(self._index_metrics(code, history))
        primary = next(
            (
                metrics
                for metrics in index_metrics
                if metrics.code == self.config.primary_index_code
            ),
            None,
        )
        breadth = self._breadth_metrics(stocks, as_of_date)
        risk = self._risk_metrics(primary)

        if primary is None:
            warnings.append(f"缺少主要指数 {self.config.primary_index_code} 的历史数据。")
        elif primary.observations < self.config.minimum_history_days:
            warnings.append(
                f"主要指数仅有 {primary.observations} 个观测，少于要求的 "
                f"{self.config.minimum_history_days} 个。"
            )
        if breadth.eligible_stocks < self.config.minimum_breadth_stocks:
            warnings.append(
                f"市场宽度仅覆盖 {breadth.eligible_stocks} 只股票，少于要求的 "
                f"{self.config.minimum_breadth_stocks} 只。"
            )

        latest_index_date = indices.get_column("trade_date").max()
        if latest_index_date != as_of_date:
            warnings.append(f"指数数据最新日期为 {latest_index_date}，并非分析日期。")
        latest_stock_date = stocks.get_column("trade_date").max()
        if latest_stock_date != as_of_date:
            warnings.append(f"股票数据最新日期为 {latest_stock_date}，并非分析日期。")

        sufficient = (
            primary is not None
            and primary.observations >= self.config.minimum_history_days
            and breadth.eligible_stocks >= self.config.minimum_breadth_stocks
            and latest_index_date == as_of_date
            and latest_stock_date == as_of_date
            and breadth.score is not None
            and risk.health_score is not None
        )

        trend_score = self._combined_trend_score(index_metrics)
        temperature = None
        state = MarketState.INSUFFICIENT_DATA
        if sufficient and trend_score is not None:
            temperature = round(
                0.45 * trend_score + 0.35 * breadth.score + 0.20 * risk.health_score,
                2,
            )
            state = self._state(temperature)

        history_start = min(
            indices.get_column("trade_date").min(),
            stocks.get_column("trade_date").min(),
        )
        history_end = max(latest_index_date, latest_stock_date)
        return MarketAnalysis(
            as_of_date=as_of_date,
            primary_index_code=self.config.primary_index_code,
            state=state,
            temperature=temperature,
            trend_score=trend_score,
            breadth=breadth,
            risk=risk,
            indices=index_metrics,
            history_start_date=history_start,
            history_end_date=history_end,
            data_confidence="high" if sufficient else "insufficient",
            warnings=warnings,
        )

    @staticmethod
    def _prepare_history(frame: pl.DataFrame, as_of_date: date) -> pl.DataFrame:
        required = {"ts_code", "trade_date", "close"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Market history is missing columns: {', '.join(sorted(missing))}")
        prepared = frame.with_columns(
            pl.col("trade_date")
            .cast(pl.String)
            .str.replace_all("-", "")
            .str.strptime(pl.Date, "%Y%m%d", strict=False),
            pl.col("close").cast(pl.Float64, strict=False),
        ).filter(
            pl.col("trade_date").is_not_null()
            & pl.col("close").is_not_null()
            & (pl.col("trade_date") <= as_of_date)
        )
        if prepared.is_empty():
            raise ValueError("Market history contains no usable observations.")
        return prepared.sort(["ts_code", "trade_date"])

    @staticmethod
    def _index_metrics(code: str, frame: pl.DataFrame) -> IndexMetrics:
        closes = frame.get_column("close").to_list()

        def moving_average(period: int) -> float | None:
            if len(closes) < period:
                return None
            return sum(closes[-period:]) / period

        def period_return(period: int) -> float | None:
            if len(closes) <= period or closes[-period - 1] <= 0:
                return None
            return closes[-1] / closes[-period - 1] - 1

        ma_values = {period: moving_average(period) for period in (20, 60, 120, 200)}
        available = [value for value in ma_values.values() if value is not None]
        trend_score = (
            100 * sum(closes[-1] > value for value in available) / len(available)
            if available
            else None
        )

        daily_returns = [
            math.log(current / previous)
            for previous, current in zip(closes[-21:-1], closes[-20:], strict=False)
            if previous > 0 and current > 0
        ]
        volatility = (
            statistics.stdev(daily_returns) * math.sqrt(252) if len(daily_returns) >= 2 else None
        )
        window = closes[-120:]
        drawdown = closes[-1] / max(window) - 1 if window else None

        return IndexMetrics(
            code=code,
            close=closes[-1],
            observations=len(closes),
            return_20d=period_return(20),
            return_60d=period_return(60),
            ma_20=ma_values[20],
            ma_60=ma_values[60],
            ma_120=ma_values[120],
            ma_200=ma_values[200],
            annualized_volatility_20d=volatility,
            drawdown_120d=drawdown,
            trend_score=trend_score,
        )

    @staticmethod
    def _breadth_metrics(frame: pl.DataFrame, as_of_date: date) -> BreadthMetrics:
        missing = {"pre_close"} - set(frame.columns)
        if missing:
            raise ValueError(f"Stock history is missing columns: {', '.join(sorted(missing))}")
        metrics = (
            frame.with_columns(pl.col("pre_close").cast(pl.Float64, strict=False))
            .group_by("ts_code")
            .agg(
                pl.col("trade_date").last().alias("latest_date"),
                pl.col("close").last().alias("close"),
                pl.col("pre_close").last().alias("pre_close"),
                pl.col("close").tail(20).mean().alias("ma20"),
                pl.col("close").tail(60).mean().alias("ma60"),
                pl.col("close").tail(120).mean().alias("ma120"),
                pl.col("close").shift(1).tail(60).max().alias("prior_high60"),
                pl.col("close").shift(1).tail(60).min().alias("prior_low60"),
                pl.len().alias("observations"),
            )
            .filter(pl.col("latest_date") == as_of_date)
        )

        def ratio(expression: pl.Expr) -> float | None:
            eligible = metrics.filter(expression.is_not_null())
            if eligible.is_empty():
                return None
            return eligible.select(expression.cast(pl.Float64).mean()).item()

        above20 = ratio(
            pl.when(pl.col("observations") >= 20)
            .then(pl.col("close") > pl.col("ma20"))
            .otherwise(None)
        )
        above60 = ratio(
            pl.when(pl.col("observations") >= 60)
            .then(pl.col("close") > pl.col("ma60"))
            .otherwise(None)
        )
        above120 = ratio(
            pl.when(pl.col("observations") >= 120)
            .then(pl.col("close") > pl.col("ma120"))
            .otherwise(None)
        )
        advance = ratio(pl.col("close") > pl.col("pre_close"))
        new_high = ratio(
            pl.when(pl.col("observations") >= 61)
            .then(pl.col("close") >= pl.col("prior_high60"))
            .otherwise(None)
        )
        new_low = ratio(
            pl.when(pl.col("observations") >= 61)
            .then(pl.col("close") <= pl.col("prior_low60"))
            .otherwise(None)
        )

        components = [
            (above20, 0.20),
            (above60, 0.30),
            (above120, 0.35),
            (advance, 0.15),
        ]
        available = [(value, weight) for value, weight in components if value is not None]
        score = (
            100
            * sum(value * weight for value, weight in available)
            / sum(weight for _, weight in available)
            if available
            else None
        )
        return BreadthMetrics(
            eligible_stocks=metrics.height,
            above_ma_20=above20,
            above_ma_60=above60,
            above_ma_120=above120,
            advance_ratio=advance,
            new_high_60_ratio=new_high,
            new_low_60_ratio=new_low,
            score=score,
        )

    @staticmethod
    def _risk_metrics(primary: IndexMetrics | None) -> RiskMetrics:
        if primary is None:
            return RiskMetrics()
        volatility = primary.annualized_volatility_20d
        drawdown = primary.drawdown_120d
        volatility_score = (
            max(0.0, min(100.0, 100 * (0.40 - volatility) / 0.30))
            if volatility is not None
            else None
        )
        drawdown_score = (
            max(0.0, min(100.0, 100 * (1 - abs(min(drawdown, 0)) / 0.25)))
            if drawdown is not None
            else None
        )
        available = [score for score in (volatility_score, drawdown_score) if score is not None]
        return RiskMetrics(
            annualized_volatility_20d=volatility,
            drawdown_120d=drawdown,
            volatility_health_score=volatility_score,
            drawdown_health_score=drawdown_score,
            health_score=sum(available) / len(available) if available else None,
        )

    def _combined_trend_score(self, metrics: list[IndexMetrics]) -> float | None:
        by_code = {item.code: item for item in metrics}
        weighted: list[tuple[float, float]] = []
        for code in self.config.index_codes:
            metric = by_code.get(code)
            if metric is None or metric.trend_score is None:
                continue
            weight = 0.5 if code == self.config.primary_index_code else 0.25
            weighted.append((metric.trend_score, weight))
        if not weighted:
            return None
        return sum(score * weight for score, weight in weighted) / sum(
            weight for _, weight in weighted
        )

    def _state(self, temperature: float) -> MarketState:
        if temperature >= self.config.attack_threshold:
            return MarketState.ATTACK
        if temperature >= self.config.balanced_threshold:
            return MarketState.BALANCED
        if temperature >= self.config.defensive_threshold:
            return MarketState.DEFENSIVE
        return MarketState.HIGH_RISK
