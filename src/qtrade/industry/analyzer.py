from __future__ import annotations

from datetime import date

import polars as pl

from qtrade.config import IndustryConfig
from qtrade.industry.models import (
    IndustryAnalysis,
    IndustryMetrics,
    IndustryState,
    StyleMetrics,
)


class IndustryAnalyzer:
    def __init__(self, config: IndustryConfig, benchmark_code: str) -> None:
        self.config = config
        self.benchmark_code = benchmark_code

    def analyze(
        self,
        as_of_date: date,
        stock_history: pl.DataFrame,
        index_history: pl.DataFrame,
        security_master: pl.DataFrame,
        classification_snapshot_date: date,
    ) -> IndustryAnalysis:
        stocks = self._prepare_history(stock_history, as_of_date, require_amount=True)
        indices = self._prepare_history(index_history, as_of_date, require_amount=False)
        master = self._prepare_master(security_master)
        index_returns = self._index_returns(indices)
        benchmark = index_returns.get(self.benchmark_code)
        if benchmark is None or benchmark[20] is None or benchmark[60] is None:
            raise ValueError(f"Benchmark {self.benchmark_code} lacks 20-day or 60-day history.")

        stock_metrics = self._stock_metrics(stocks, as_of_date)
        mapped = stock_metrics.join(master, on="ts_code", how="inner")
        if mapped.is_empty():
            raise ValueError("No stock observations could be mapped to an industry.")

        industry_frame = (
            mapped.group_by("industry")
            .agg(
                pl.len().alias("stock_count"),
                pl.col("return_5d").median(),
                pl.col("return_20d").median(),
                pl.col("return_60d").median(),
                pl.col("above_ma_60").mean(),
                pl.col("advance").mean().alias("advance_ratio"),
                pl.col("activity_ratio").median(),
            )
            .filter(pl.col("stock_count") >= self.config.minimum_stocks)
            .with_columns(
                (pl.col("return_20d") - benchmark[20]).alias("relative_return_20d"),
                (pl.col("return_60d") - benchmark[60]).alias("relative_return_60d"),
            )
        )
        if industry_frame.is_empty():
            raise ValueError("No industry meets the configured minimum stock count.")

        industry_frame = industry_frame.with_columns(
            self._percentile_rank("relative_return_20d").alias("rank_relative_20d"),
            self._percentile_rank("relative_return_60d").alias("rank_relative_60d"),
            self._percentile_rank("activity_ratio").alias("rank_activity"),
        ).with_columns(
            (
                100
                * (
                    0.35 * pl.col("rank_relative_20d")
                    + 0.25 * pl.col("rank_relative_60d")
                    + 0.30 * pl.col("above_ma_60")
                    + 0.10 * pl.col("rank_activity")
                )
            ).alias("score")
        )
        industry_frame = industry_frame.sort(
            ["score", "industry"], descending=[True, False]
        ).with_row_index("rank", offset=1)

        industries = [
            IndustryMetrics(
                name=row["industry"],
                stock_count=row["stock_count"],
                return_5d=row["return_5d"],
                return_20d=row["return_20d"],
                return_60d=row["return_60d"],
                relative_return_20d=row["relative_return_20d"],
                relative_return_60d=row["relative_return_60d"],
                above_ma_60=row["above_ma_60"],
                advance_ratio=row["advance_ratio"],
                activity_ratio=row["activity_ratio"],
                score=row["score"],
                rank=row["rank"],
                state=self._industry_state(row),
            )
            for row in industry_frame.to_dicts()
        ]
        warnings: list[str] = []
        confidence = "high"
        if classification_snapshot_date != as_of_date:
            confidence = "medium"
            warnings.append(
                f"行业分类快照日期为 {classification_snapshot_date}，与分析日期 {as_of_date} 不同。"
            )
        mapped_codes = mapped.get_column("ts_code").n_unique()
        total_codes = stock_metrics.get_column("ts_code").n_unique()
        mapping_ratio = mapped_codes / total_codes if total_codes else 0
        if mapping_ratio < 0.9:
            confidence = "medium"
            warnings.append(f"行业映射覆盖率为 {mapping_ratio:.1%}。")

        return IndustryAnalysis(
            as_of_date=as_of_date,
            classification=self.config.classification_column,
            classification_snapshot_date=classification_snapshot_date,
            benchmark_code=self.benchmark_code,
            benchmark_return_20d=benchmark[20],
            benchmark_return_60d=benchmark[60],
            industries=industries,
            styles=self._style_metrics(index_returns),
            data_confidence=confidence,
            warnings=warnings,
        )

    @staticmethod
    def _prepare_history(
        frame: pl.DataFrame, as_of_date: date, require_amount: bool
    ) -> pl.DataFrame:
        required = {"ts_code", "trade_date", "close"}
        if require_amount:
            required |= {"pre_close", "amount"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Industry history is missing columns: {', '.join(sorted(missing))}")
        expressions = [
            pl.col("trade_date")
            .cast(pl.String)
            .str.replace_all("-", "")
            .str.strptime(pl.Date, "%Y%m%d", strict=False),
            pl.col("close").cast(pl.Float64, strict=False),
        ]
        if require_amount:
            expressions.extend(
                [
                    pl.col("pre_close").cast(pl.Float64, strict=False),
                    pl.col("amount").cast(pl.Float64, strict=False),
                ]
            )
        return (
            frame.with_columns(expressions)
            .filter(
                pl.col("trade_date").is_not_null()
                & pl.col("close").is_not_null()
                & (pl.col("trade_date") <= as_of_date)
            )
            .sort(["ts_code", "trade_date"])
        )

    def _prepare_master(self, frame: pl.DataFrame) -> pl.DataFrame:
        column = self.config.classification_column
        required = {"ts_code", column}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Security master is missing columns: {', '.join(sorted(missing))}")
        return (
            frame.select(
                pl.col("ts_code").cast(pl.String),
                pl.col(column).cast(pl.String).str.strip_chars().alias("industry"),
            )
            .filter(pl.col("industry").is_not_null() & (pl.col("industry").str.len_chars() > 0))
            .unique(subset=["ts_code"], keep="last")
        )

    @staticmethod
    def _stock_metrics(frame: pl.DataFrame, as_of_date: date) -> pl.DataFrame:
        return (
            frame.group_by("ts_code")
            .agg(
                pl.col("trade_date").last().alias("latest_date"),
                pl.col("close").last().alias("close"),
                pl.col("pre_close").last().alias("pre_close"),
                (pl.col("close").last() / pl.col("close").shift(5).last() - 1).alias("return_5d"),
                (pl.col("close").last() / pl.col("close").shift(20).last() - 1).alias("return_20d"),
                (pl.col("close").last() / pl.col("close").shift(60).last() - 1).alias("return_60d"),
                (pl.col("close").last() > pl.col("close").tail(60).mean()).alias("above_ma_60"),
                (pl.col("close").last() > pl.col("pre_close").last()).alias("advance"),
                (pl.col("amount").last() / pl.col("amount").tail(20).mean()).alias(
                    "activity_ratio"
                ),
                pl.len().alias("observations"),
            )
            .filter(
                (pl.col("latest_date") == as_of_date)
                & (pl.col("observations") >= 61)
                & pl.col("return_60d").is_finite()
                & pl.col("activity_ratio").is_finite()
            )
        )

    @staticmethod
    def _returns(closes: list[float]) -> dict[int, float | None]:
        return {
            period: (
                closes[-1] / closes[-period - 1] - 1
                if len(closes) > period and closes[-period - 1] > 0
                else None
            )
            for period in (5, 20, 60)
        }

    def _index_returns(self, frame: pl.DataFrame) -> dict[str, dict[int, float | None]]:
        return {
            code: self._returns(
                frame.filter(pl.col("ts_code") == code)
                .sort("trade_date")
                .get_column("close")
                .to_list()
            )
            for code in frame.get_column("ts_code").unique().to_list()
        }

    @staticmethod
    def _percentile_rank(column: str) -> pl.Expr:
        return pl.col(column).rank(method="average") / pl.len()

    @staticmethod
    def _industry_state(row: dict) -> IndustryState:
        if row["return_20d"] > 0 and row["relative_return_20d"] > 0 and row["above_ma_60"] < 0.5:
            return IndustryState.HIGH_LEVEL_DIVERGENCE
        if row["return_20d"] > 0 and row["relative_return_20d"] > 0 and row["above_ma_60"] >= 0.6:
            if row["return_5d"] > row["return_20d"] / 4:
                return IndustryState.TREND_STRENGTHENING
            return IndustryState.STRONG_CONTINUATION
        if row["return_20d"] <= 0 and row["return_5d"] > 0 and row["above_ma_60"] >= 0.45:
            return IndustryState.WEAK_RECOVERY
        if row["return_5d"] < 0 and row["above_ma_60"] < 0.5:
            return IndustryState.WEAKENING
        return IndustryState.NEUTRAL

    def _style_metrics(
        self, index_returns: dict[str, dict[int, float | None]]
    ) -> list[StyleMetrics]:
        results = []
        for name, (numerator, denominator) in self.config.style_pairs.items():
            numerator_returns = index_returns.get(numerator)
            denominator_returns = index_returns.get(denominator)
            if numerator_returns is None or denominator_returns is None:
                results.append(
                    StyleMetrics(
                        name=name,
                        numerator_code=numerator,
                        denominator_code=denominator,
                        leader="insufficient_data",
                    )
                )
                continue
            relative = {
                period: (
                    numerator_returns[period] - denominator_returns[period]
                    if numerator_returns[period] is not None
                    and denominator_returns[period] is not None
                    else None
                )
                for period in (5, 20, 60)
            }
            available = [relative[period] for period in (20, 60) if relative[period] is not None]
            strength = sum(available) / len(available) if available else None
            if strength is None:
                leader = "insufficient_data"
            elif strength > 0.01:
                leader = "numerator"
            elif strength < -0.01:
                leader = "denominator"
            else:
                leader = "balanced"
            results.append(
                StyleMetrics(
                    name=name,
                    numerator_code=numerator,
                    denominator_code=denominator,
                    relative_return_5d=relative[5],
                    relative_return_20d=relative[20],
                    relative_return_60d=relative[60],
                    leader=leader,
                    strength=strength,
                )
            )
        return results
