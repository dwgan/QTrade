from datetime import date

import polars as pl
import pytest

from qtrade.config import ResearchConfig
from qtrade.research.analyzer import FactorResearchAnalyzer, adjusted_prices


def _market_data():
    dates = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    prices = pl.DataFrame(
        [
            {"ts_code": f"{code:06d}.SZ", "trade_date": trade_date, "close": value}
            for trade_date, multiplier in zip(dates, [1.0, 1.0, 1.0], strict=True)
            for code, value in enumerate(
                [10 * multiplier, 11 * multiplier, 12 * multiplier, 13 * multiplier],
                start=1,
            )
        ]
    )
    # Cross-sectional one-day returns increase with factor score.
    prices = prices.with_columns(
        pl.when(pl.col("trade_date") == dates[1])
        .then(pl.col("close") * (1 + pl.col("ts_code").str.slice(4, 2).cast(pl.Int64) / 100))
        .when(pl.col("trade_date") == dates[2])
        .then(pl.col("close") * (1 + pl.col("ts_code").str.slice(4, 2).cast(pl.Int64) / 50))
        .otherwise(pl.col("close"))
        .alias("close")
    )
    adjustments = prices.select("ts_code", "trade_date").with_columns(
        pl.lit(1.0).alias("adj_factor")
    )
    return dates, prices, adjustments


def _ranking() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "ts_code": f"{code:06d}.SZ",
                "score": float(code),
                "quality_score": float(code),
                "value_score": float(code),
                "momentum_score": float(code),
                "low_risk_score": float(code),
            }
            for code in range(1, 5)
        ]
    )


def test_factor_research_calculates_ic_and_quantile_spread() -> None:
    dates, prices, adjustments = _market_data()
    analysis, ic_detail, returns = FactorResearchAnalyzer(
        ResearchConfig(forward_horizon_days=1, quantiles=2, minimum_cross_section=2)
    ).analyze(
        dates[0],
        dates[2],
        [(dates[0], _ranking()), (dates[1], _ranking())],
        prices,
        adjustments,
    )

    composite = next(item for item in analysis.factor_metrics if item.factor == "score")
    assert analysis.evaluated_snapshot_count == 2
    assert composite.ic_mean == pytest.approx(1.0)
    assert analysis.top_bottom_spread is not None
    assert analysis.top_bottom_spread > 0
    assert analysis.quantile_monotonic is True
    assert ic_detail.height == 10
    assert returns.height == 8


def test_adjusted_prices_parses_provider_date_strings() -> None:
    prices = pl.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20260102"], "close": [10.0]}
    )
    adjustments = pl.DataFrame(
        {"ts_code": ["000001.SZ"], "trade_date": ["20260102"], "adj_factor": [2.0]}
    )

    row = adjusted_prices(prices, adjustments).row(0, named=True)

    assert row["trade_date"] == date(2026, 1, 2)
    assert row["adjusted_close"] == 20
