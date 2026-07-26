from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date

import polars as pl

from qtrade.config import ResearchConfig
from qtrade.research.models import FactorMetric, FactorResearchAnalysis, QuantileMetric

FACTOR_COLUMNS = (
    "quality_score",
    "value_score",
    "momentum_score",
    "low_risk_score",
    "score",
)


def date_expression(column: str) -> pl.Expr:
    return (
        pl.col(column)
        .cast(pl.String)
        .str.replace_all("-", "")
        .str.strptime(pl.Date, "%Y%m%d", strict=False)
    )


def adjusted_prices(prices: pl.DataFrame, adjustments: pl.DataFrame) -> pl.DataFrame:
    required_prices = {"ts_code", "trade_date", "close"}
    required_adjustments = {"ts_code", "trade_date", "adj_factor"}
    if missing := required_prices - set(prices.columns):
        raise ValueError(f"Prices are missing columns: {', '.join(sorted(missing))}")
    if missing := required_adjustments - set(adjustments.columns):
        raise ValueError(f"Adjustments are missing columns: {', '.join(sorted(missing))}")
    return (
        prices.select("ts_code", "trade_date", "close")
        .join(
            adjustments.select("ts_code", "trade_date", "adj_factor"),
            on=["ts_code", "trade_date"],
            how="inner",
        )
        .with_columns(
            date_expression("trade_date"),
            (pl.col("close") * pl.col("adj_factor")).alias("adjusted_close"),
        )
        .sort(["trade_date", "ts_code"])
    )


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    left_rank = pl.Series(left).rank("average").to_list()
    right_rank = pl.Series(right).rank("average").to_list()
    left_mean = statistics.fmean(left_rank)
    right_mean = statistics.fmean(right_rank)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_rank, right_rank, strict=True)
    )
    denominator = math.sqrt(
        sum((value - left_mean) ** 2 for value in left_rank)
        * sum((value - right_mean) ** 2 for value in right_rank)
    )
    if denominator == 0:
        return None
    return numerator / denominator


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


class FactorResearchAnalyzer:
    def __init__(self, config: ResearchConfig) -> None:
        self.config = config

    def analyze(
        self,
        start_date: date,
        end_date: date,
        snapshots: list[tuple[date, pl.DataFrame]],
        prices: pl.DataFrame,
        adjustments: pl.DataFrame,
    ) -> tuple[FactorResearchAnalysis, pl.DataFrame, pl.DataFrame]:
        if start_date > end_date:
            raise ValueError("Start date must not be after end date.")
        adjusted = adjusted_prices(prices, adjustments)
        trading_dates = adjusted.get_column("trade_date").unique().sort().to_list()
        date_index = {value: index for index, value in enumerate(trading_dates)}
        price_lookup = {
            (row["trade_date"], row["ts_code"]): row["adjusted_close"]
            for row in adjusted.select("trade_date", "ts_code", "adjusted_close").to_dicts()
        }
        ic_rows: list[dict] = []
        quantile_values: dict[int, list[float]] = defaultdict(list)
        detail_rows: list[dict] = []
        evaluated = 0
        warnings: list[str] = []

        for signal_date, ranking in snapshots:
            index = date_index.get(signal_date)
            if index is None or index + self.config.forward_horizon_days >= len(trading_dates):
                continue
            forward_date = trading_dates[index + self.config.forward_horizon_days]
            required = {"ts_code", *FACTOR_COLUMNS}
            if missing := required - set(ranking.columns):
                raise ValueError(
                    f"Ranking snapshot {signal_date} is missing columns: "
                    f"{', '.join(sorted(missing))}"
                )
            rows: list[dict] = []
            for row in ranking.select("ts_code", *FACTOR_COLUMNS).to_dicts():
                start = price_lookup.get((signal_date, row["ts_code"]))
                end = price_lookup.get((forward_date, row["ts_code"]))
                if start is None or end is None or start <= 0:
                    continue
                rows.append(
                    {
                        **row,
                        "signal_date": signal_date,
                        "forward_date": forward_date,
                        "forward_return": end / start - 1,
                    }
                )
            if len(rows) < self.config.minimum_cross_section:
                warnings.append(
                    f"{signal_date}: only {len(rows)} valid stocks; cross-section skipped."
                )
                continue
            evaluated += 1
            detail_rows.extend(rows)
            for factor in FACTOR_COLUMNS:
                value = _spearman(
                    [float(row[factor]) for row in rows],
                    [float(row["forward_return"]) for row in rows],
                )
                if value is not None:
                    ic_rows.append({"signal_date": signal_date, "factor": factor, "ic": value})
            ordered = sorted(rows, key=lambda row: (row["score"], row["ts_code"]))
            count = len(ordered)
            for position, row in enumerate(ordered):
                quantile = min(position * self.config.quantiles // count + 1, self.config.quantiles)
                quantile_values[quantile].append(float(row["forward_return"]))

        factor_metrics: list[FactorMetric] = []
        for factor in FACTOR_COLUMNS:
            values = [row["ic"] for row in ic_rows if row["factor"] == factor]
            standard_deviation = statistics.stdev(values) if len(values) > 1 else None
            mean = _mean(values)
            factor_metrics.append(
                FactorMetric(
                    factor=factor,
                    observations=len(values),
                    ic_mean=mean,
                    ic_median=statistics.median(values) if values else None,
                    ic_std=standard_deviation,
                    ic_positive_ratio=(
                        sum(value > 0 for value in values) / len(values) if values else None
                    ),
                    icir=(
                        mean / standard_deviation
                        if mean is not None and standard_deviation not in (None, 0)
                        else None
                    ),
                )
            )
        quantile_metrics = [
            QuantileMetric(
                quantile=quantile,
                observations=len(quantile_values[quantile]),
                mean_forward_return=_mean(quantile_values[quantile]),
            )
            for quantile in range(1, self.config.quantiles + 1)
        ]
        means = [item.mean_forward_return for item in quantile_metrics]
        valid_means = all(value is not None for value in means)
        spread = (
            float(means[-1] - means[0])
            if valid_means and means[-1] is not None and means[0] is not None
            else None
        )
        monotonic = (
            all(left <= right for left, right in zip(means, means[1:], strict=False))
            if valid_means
            else None
        )
        analysis = FactorResearchAnalysis(
            start_date=start_date,
            end_date=end_date,
            forward_horizon_days=self.config.forward_horizon_days,
            requested_quantiles=self.config.quantiles,
            snapshot_count=len(snapshots),
            evaluated_snapshot_count=evaluated,
            factor_metrics=factor_metrics,
            quantile_metrics=quantile_metrics,
            top_bottom_spread=spread,
            quantile_monotonic=monotonic,
            warnings=warnings,
        )
        return analysis, pl.DataFrame(ic_rows), pl.DataFrame(detail_rows)
