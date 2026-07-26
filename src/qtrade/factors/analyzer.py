from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date

import polars as pl

from qtrade.config import FactorConfig
from qtrade.factors.models import CandidateStock, FactorAnalysis


@dataclass
class FactorComputation:
    analysis: FactorAnalysis
    rankings: pl.DataFrame


class FactorAnalyzer:
    QUALITY_FACTORS = (
        "roe_quality",
        "roic_quality",
        "cash_quality",
        "margin_quality",
        "debt_quality",
    )
    VALUE_FACTORS = (
        "earnings_yield",
        "book_yield",
        "sales_yield",
        "dividend_yield",
    )
    MOMENTUM_FACTORS = ("return_20d", "return_60d_ex5", "return_120d_ex5")
    LOW_RISK_FACTORS = ("negative_volatility", "drawdown_120d")

    def __init__(self, config: FactorConfig) -> None:
        self.config = config

    def analyze(
        self,
        as_of_date: date,
        prices: pl.DataFrame,
        adjust_factors: pl.DataFrame,
        daily_basic: pl.DataFrame,
        financial_indicators: pl.DataFrame,
        security_master: pl.DataFrame,
        stock_limits: pl.DataFrame | None,
        daily_basic_snapshot_date: date,
        financial_snapshot_date: date,
        security_master_snapshot_date: date,
    ) -> FactorComputation:
        price_metrics = self._price_metrics(prices, adjust_factors, as_of_date)
        basic = self._prepare_daily_basic(daily_basic, as_of_date)
        financials = self._prepare_financials(financial_indicators, as_of_date)
        master = self._prepare_master(security_master)
        limits = self._prepare_limits(stock_limits, as_of_date)

        joined = (
            price_metrics.join(basic, on="ts_code", how="left")
            .join(financials, on="ts_code", how="left")
            .join(master, on="ts_code", how="left")
        )
        if limits is not None:
            joined = joined.join(limits, on="ts_code", how="left")

        universe_size = joined.height
        liquidity_threshold = joined.get_column("avg_amount_20d").quantile(
            self.config.liquidity_exclusion_percentile
        )
        eligible_rows: list[dict] = []
        exclusions: Counter[str] = Counter()
        for row in joined.to_dicts():
            reason = self._exclusion_reason(
                row,
                as_of_date,
                float(liquidity_threshold or 0),
                limits is not None,
            )
            if reason:
                exclusions[reason] += 1
            else:
                eligible_rows.append(row)
        if len(eligible_rows) < 2:
            raise ValueError("Fewer than two stocks remain after risk and data filters.")

        eligible = pl.DataFrame(eligible_rows)
        factored = self._raw_factors(eligible)
        factor_groups = {
            "quality": self.QUALITY_FACTORS,
            "value": self.VALUE_FACTORS,
            "momentum": self.MOMENTUM_FACTORS,
            "low_risk": self.LOW_RISK_FACTORS,
        }
        for columns in factor_groups.values():
            for column in columns:
                factored = self._neutralize(factored, column)

        for group, columns in factor_groups.items():
            factored = factored.with_columns(
                pl.mean_horizontal([pl.col(f"{column}_z") for column in columns]).alias(
                    f"{group}_z"
                )
            )
            factored = self._add_percentile(factored, f"{group}_z", f"{group}_score")

        composite = sum(
            pl.col(f"{group}_z") * weight for group, weight in self.config.weights.items()
        )
        factored = factored.with_columns(composite.alias("composite_z"))
        factored = self._add_percentile(factored, "composite_z", "score")
        factored = factored.sort(["score", "ts_code"], descending=[True, False]).with_row_index(
            "rank", offset=1
        )

        candidates = self._select_candidates(factored, as_of_date)
        warnings: list[str] = []
        confidence = "high"
        snapshots = {
            "每日估值": daily_basic_snapshot_date,
            "财务指标": financial_snapshot_date,
            "股票基础": security_master_snapshot_date,
        }
        for label, snapshot_date in snapshots.items():
            if snapshot_date != as_of_date:
                confidence = "medium"
                warnings.append(f"{label}快照日期为 {snapshot_date}。")
        if stock_limits is None:
            confidence = "medium"
            warnings.append("缺少分析日涨跌停价格，未执行涨跌停风险过滤。")

        return FactorComputation(
            analysis=FactorAnalysis(
                as_of_date=as_of_date,
                daily_basic_snapshot_date=daily_basic_snapshot_date,
                financial_snapshot_date=financial_snapshot_date,
                security_master_snapshot_date=security_master_snapshot_date,
                universe_size=universe_size,
                eligible_size=len(eligible_rows),
                ranked_size=factored.height,
                exclusion_counts=dict(sorted(exclusions.items())),
                candidates=candidates,
                data_confidence=confidence,
                warnings=warnings,
            ),
            rankings=factored.select(
                "rank",
                "ts_code",
                "name",
                "industry",
                "close",
                "score",
                "quality_score",
                "value_score",
                "momentum_score",
                "low_risk_score",
                "ann_date",
                "end_date",
            ),
        )

    @staticmethod
    def _date_expression(column: str) -> pl.Expr:
        return (
            pl.col(column)
            .cast(pl.String)
            .str.replace_all("-", "")
            .str.strptime(pl.Date, "%Y%m%d", strict=False)
        )

    def _price_metrics(
        self,
        prices: pl.DataFrame,
        adjust_factors: pl.DataFrame,
        as_of_date: date,
    ) -> pl.DataFrame:
        price_required = {"ts_code", "trade_date", "close", "amount"}
        adjust_required = {"ts_code", "trade_date", "adj_factor"}
        if missing := price_required - set(prices.columns):
            raise ValueError(f"Prices are missing columns: {', '.join(sorted(missing))}")
        if missing := adjust_required - set(adjust_factors.columns):
            raise ValueError(
                f"Adjustment factors are missing columns: {', '.join(sorted(missing))}"
            )
        prepared_prices = prices.select(
            pl.col("ts_code").cast(pl.String),
            self._date_expression("trade_date"),
            pl.col("close").cast(pl.Float64, strict=False),
            pl.col("amount").cast(pl.Float64, strict=False),
        )
        prepared_adjustments = adjust_factors.select(
            pl.col("ts_code").cast(pl.String),
            self._date_expression("trade_date"),
            pl.col("adj_factor").cast(pl.Float64, strict=False),
        )
        history = (
            prepared_prices.join(prepared_adjustments, on=["ts_code", "trade_date"], how="inner")
            .filter(
                (pl.col("trade_date") <= as_of_date)
                & (pl.col("adj_factor") > 0)
                & (pl.col("close") > 0)
            )
            .with_columns((pl.col("close") * pl.col("adj_factor")).alias("adjusted_close"))
            .sort(["ts_code", "trade_date"])
        )
        return (
            history.group_by("ts_code")
            .agg(
                pl.col("trade_date").last().alias("latest_date"),
                pl.col("close").last().alias("close"),
                pl.col("amount").tail(20).mean().alias("avg_amount_20d"),
                (
                    pl.col("adjusted_close").last() / pl.col("adjusted_close").shift(20).last() - 1
                ).alias("return_20d"),
                (
                    pl.col("adjusted_close").shift(5).last()
                    / pl.col("adjusted_close").shift(60).last()
                    - 1
                ).alias("return_60d_ex5"),
                (
                    pl.col("adjusted_close").shift(5).last()
                    / pl.col("adjusted_close").shift(120).last()
                    - 1
                ).alias("return_120d_ex5"),
                (pl.col("adjusted_close").pct_change().tail(60).std() * math.sqrt(252)).alias(
                    "volatility_60d"
                ),
                (
                    pl.col("adjusted_close").last() / pl.col("adjusted_close").tail(120).max() - 1
                ).alias("drawdown_120d"),
                pl.len().alias("observations"),
            )
            .filter(
                (pl.col("latest_date") == as_of_date)
                & (pl.col("observations") >= self.config.minimum_history_days)
                & pl.col("return_120d_ex5").is_finite()
                & pl.col("volatility_60d").is_finite()
            )
        )

    def _prepare_daily_basic(self, frame: pl.DataFrame, as_of_date: date) -> pl.DataFrame:
        columns = {
            "ts_code",
            "trade_date",
            "pe_ttm",
            "pb",
            "ps_ttm",
            "dv_ttm",
            "total_mv",
        }
        if missing := columns - set(frame.columns):
            raise ValueError(f"Daily basic data are missing columns: {', '.join(sorted(missing))}")
        numeric = columns - {"ts_code", "trade_date"}
        return (
            frame.select(
                pl.col("ts_code").cast(pl.String),
                self._date_expression("trade_date"),
                *[pl.col(column).cast(pl.Float64, strict=False) for column in numeric],
            )
            .filter(pl.col("trade_date") <= as_of_date)
            .sort(["ts_code", "trade_date"])
            .unique(subset=["ts_code"], keep="last")
        )

    def _prepare_financials(self, frame: pl.DataFrame, as_of_date: date) -> pl.DataFrame:
        columns = {
            "ts_code",
            "ann_date",
            "end_date",
            "roe",
            "roe_dt",
            "roic",
            "netprofit_margin",
            "ocfps",
            "eps",
            "debt_to_assets",
        }
        if missing := columns - set(frame.columns):
            raise ValueError(
                f"Financial indicators are missing columns: {', '.join(sorted(missing))}"
            )
        numeric = columns - {"ts_code", "ann_date", "end_date"}
        return (
            frame.select(
                pl.col("ts_code").cast(pl.String),
                self._date_expression("ann_date"),
                self._date_expression("end_date"),
                *[pl.col(column).cast(pl.Float64, strict=False) for column in numeric],
            )
            .filter(pl.col("ann_date").is_not_null() & (pl.col("ann_date") <= as_of_date))
            .sort(["ts_code", "ann_date", "end_date"])
            .unique(subset=["ts_code"], keep="last")
        )

    @staticmethod
    def _prepare_master(frame: pl.DataFrame) -> pl.DataFrame:
        columns = {"ts_code", "name", "industry", "list_date"}
        if missing := columns - set(frame.columns):
            raise ValueError(f"Security master is missing columns: {', '.join(sorted(missing))}")
        return frame.select(
            pl.col("ts_code").cast(pl.String),
            pl.col("name").cast(pl.String),
            pl.col("industry").cast(pl.String),
            FactorAnalyzer._date_expression("list_date"),
        ).unique(subset=["ts_code"], keep="last")

    @staticmethod
    def _prepare_limits(frame: pl.DataFrame | None, as_of_date: date) -> pl.DataFrame | None:
        if frame is None:
            return None
        required = {"ts_code", "trade_date", "up_limit", "down_limit"}
        if required - set(frame.columns):
            return None
        return (
            frame.select(
                pl.col("ts_code").cast(pl.String),
                FactorAnalyzer._date_expression("trade_date"),
                pl.col("up_limit").cast(pl.Float64, strict=False),
                pl.col("down_limit").cast(pl.Float64, strict=False),
            )
            .filter(pl.col("trade_date") == as_of_date)
            .unique(subset=["ts_code"], keep="last")
        )

    def _exclusion_reason(
        self,
        row: dict,
        as_of_date: date,
        liquidity_threshold: float,
        has_limits: bool,
    ) -> str | None:
        if not row.get("name") or not row.get("industry"):
            return "missing_security_metadata"
        name = str(row["name"]).upper()
        if "ST" in name or "退" in name:
            return "special_treatment_or_delisting"
        if any(
            keyword in str(row["industry"]) for keyword in self.config.exclude_industry_keywords
        ):
            return "excluded_financial_industry"
        list_date = row.get("list_date")
        if list_date is None or (as_of_date - list_date).days < (self.config.minimum_listing_days):
            return "insufficient_listing_history"
        if row.get("pe_ttm") is None or row.get("total_mv") is None or row["total_mv"] <= 0:
            return "missing_valuation_data"
        if row.get("ann_date") is None:
            return "missing_financial_data"
        if (row.get("avg_amount_20d") or 0) < liquidity_threshold:
            return "low_liquidity"
        if has_limits and row.get("up_limit") is not None and row.get("down_limit") is not None:
            close = row["close"]
            if close >= row["up_limit"] * 0.9999:
                return "at_up_limit"
            if close <= row["down_limit"] * 1.0001:
                return "at_down_limit"
        return None

    @staticmethod
    def _raw_factors(frame: pl.DataFrame) -> pl.DataFrame:
        return frame.with_columns(
            pl.coalesce(["roe_dt", "roe"]).alias("roe_quality"),
            pl.col("roic").alias("roic_quality"),
            pl.when(pl.col("eps").abs() > 0.01)
            .then(pl.col("ocfps") / pl.col("eps").abs())
            .otherwise(None)
            .alias("cash_quality"),
            pl.col("netprofit_margin").alias("margin_quality"),
            (-pl.col("debt_to_assets")).alias("debt_quality"),
            pl.when(pl.col("pe_ttm") > 0)
            .then(1 / pl.col("pe_ttm"))
            .otherwise(None)
            .alias("earnings_yield"),
            pl.when(pl.col("pb") > 0).then(1 / pl.col("pb")).otherwise(None).alias("book_yield"),
            pl.when(pl.col("ps_ttm") > 0)
            .then(1 / pl.col("ps_ttm"))
            .otherwise(None)
            .alias("sales_yield"),
            (pl.col("dv_ttm").fill_null(0) / 100).alias("dividend_yield"),
            (-pl.col("volatility_60d")).alias("negative_volatility"),
            pl.col("total_mv").log().alias("log_market_value"),
        )

    @staticmethod
    def _neutralize(frame: pl.DataFrame, column: str) -> pl.DataFrame:
        values = pl.col(column).cast(pl.Float64, strict=False).fill_nan(None)
        frame = frame.with_columns(values.alias(column))
        global_median = frame.get_column(column).median()
        fill_value = float(global_median) if global_median is not None else 0.0
        frame = frame.with_columns(
            pl.col(column)
            .fill_null(pl.col(column).median().over("industry"))
            .fill_null(fill_value)
            .alias(column)
        )
        lower = frame.get_column(column).quantile(0.025)
        upper = frame.get_column(column).quantile(0.975)
        frame = frame.with_columns(pl.col(column).clip(float(lower), float(upper)).alias(column))
        frame = frame.with_columns(
            (pl.col(column) - pl.col(column).mean().over("industry")).alias(
                f"{column}_industry_neutral"
            )
        )
        x = frame.get_column("log_market_value")
        y = frame.get_column(f"{column}_industry_neutral")
        x_centered = x - x.mean()
        denominator = (x_centered * x_centered).sum()
        beta = (
            (x_centered * y).sum() / denominator
            if denominator is not None and denominator > 0
            else 0.0
        )
        residual_name = f"{column}_residual"
        frame = frame.with_columns(
            (
                pl.col(f"{column}_industry_neutral")
                - beta * (pl.col("log_market_value") - x.mean())
            ).alias(residual_name)
        )
        std = frame.get_column(residual_name).std()
        if std is None or std == 0:
            return frame.with_columns(pl.lit(0.0).alias(f"{column}_z"))
        return frame.with_columns(
            ((pl.col(residual_name) - pl.col(residual_name).mean()) / std).alias(f"{column}_z")
        )

    @staticmethod
    def _add_percentile(frame: pl.DataFrame, source: str, target: str) -> pl.DataFrame:
        if frame.height == 1:
            return frame.with_columns(pl.lit(50.0).alias(target))
        return frame.with_columns(
            ((pl.col(source).rank(method="average") - 1) / (pl.len() - 1) * 100).alias(target)
        )

    def _select_candidates(self, rankings: pl.DataFrame, as_of_date: date) -> list[CandidateStock]:
        industry_counts: defaultdict[str, int] = defaultdict(int)
        candidates: list[CandidateStock] = []
        labels = {
            "quality_score": "财务质量",
            "value_score": "估值",
            "momentum_score": "中期动量",
            "low_risk_score": "低波动",
        }
        for row in rankings.to_dicts():
            industry = row["industry"]
            if industry_counts[industry] >= self.config.max_candidates_per_industry:
                continue
            group_scores = sorted(
                ((column, row[column]) for column in labels),
                key=lambda item: item[1],
                reverse=True,
            )
            reasons = [f"{labels[column]}得分 {score:.1f}" for column, score in group_scores[:2]]
            risk_flags = []
            if (as_of_date - row["ann_date"]).days > 200:
                risk_flags.append("财务公告距分析日超过200天")
            candidates.append(
                CandidateStock(
                    ts_code=row["ts_code"],
                    name=row["name"],
                    industry=industry,
                    close=row["close"],
                    score=row["score"],
                    rank=row["rank"],
                    quality_score=row["quality_score"],
                    value_score=row["value_score"],
                    momentum_score=row["momentum_score"],
                    low_risk_score=row["low_risk_score"],
                    financial_ann_date=row["ann_date"],
                    financial_period=row["end_date"],
                    reasons=reasons,
                    risk_flags=risk_flags,
                )
            )
            industry_counts[industry] += 1
            if len(candidates) >= self.config.candidate_count:
                break
        return candidates
