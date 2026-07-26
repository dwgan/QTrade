from datetime import date, timedelta

import polars as pl

from qtrade.config import FactorConfig
from qtrade.factors.analyzer import FactorAnalyzer

AS_OF_DATE = date(2026, 7, 24)


def trading_dates(count: int) -> list[date]:
    current = AS_OF_DATE
    values: list[date] = []
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current -= timedelta(days=1)
    return list(reversed(values))


def factor_inputs(stock_count: int = 16):
    price_rows = []
    adjustment_rows = []
    basic_rows = []
    financial_rows = []
    master_rows = []
    limit_rows = []
    dates = trading_dates(130)
    industries = ["科技", "消费", "制造", "医药"]

    for stock in range(stock_count):
        code = f"{stock + 1:06d}.SZ"
        industry = industries[stock % len(industries)]
        slope = 0.005 + stock * 0.0008
        for offset, trading_date in enumerate(dates):
            close = 10 + stock * 0.2 + offset * slope
            trade_date = trading_date.strftime("%Y%m%d")
            price_rows.append(
                {
                    "ts_code": code,
                    "trade_date": trade_date,
                    "close": close,
                    "amount": 100_000 + stock * 10_000 + offset * 100,
                }
            )
            adjustment_rows.append(
                {
                    "ts_code": code,
                    "trade_date": trade_date,
                    "adj_factor": 1.0,
                }
            )
        basic_rows.append(
            {
                "ts_code": code,
                "trade_date": AS_OF_DATE.strftime("%Y%m%d"),
                "pe_ttm": 8 + stock,
                "pb": 1 + stock * 0.1,
                "ps_ttm": 1.5 + stock * 0.1,
                "dv_ttm": stock % 4,
                "total_mv": 500_000 + stock * 50_000,
            }
        )
        financial_rows.append(
            {
                "ts_code": code,
                "ann_date": "20260430",
                "end_date": "20260331",
                "roe": 8 + stock,
                "roe_dt": 7 + stock,
                "roic": 6 + stock,
                "netprofit_margin": 5 + stock,
                "ocfps": 0.8 + stock * 0.05,
                "eps": 0.5 + stock * 0.02,
                "debt_to_assets": 60 - stock,
            }
        )
        master_rows.append(
            {
                "ts_code": code,
                "name": f"样本{stock + 1}",
                "industry": industry,
                "list_date": "20200101",
            }
        )
        limit_rows.append(
            {
                "ts_code": code,
                "trade_date": AS_OF_DATE.strftime("%Y%m%d"),
                "up_limit": 100.0,
                "down_limit": 1.0,
            }
        )

    return (
        pl.DataFrame(price_rows),
        pl.DataFrame(adjustment_rows),
        pl.DataFrame(basic_rows),
        pl.DataFrame(financial_rows),
        pl.DataFrame(master_rows),
        pl.DataFrame(limit_rows),
    )


def test_factor_ranking_generates_bounded_candidates() -> None:
    inputs = factor_inputs()
    config = FactorConfig(
        minimum_listing_days=0,
        liquidity_exclusion_percentile=0,
        candidate_count=8,
        max_candidates_per_industry=2,
    )

    computation = FactorAnalyzer(config).analyze(
        AS_OF_DATE,
        *inputs,
        AS_OF_DATE,
        AS_OF_DATE,
        AS_OF_DATE,
    )

    analysis = computation.analysis
    assert analysis.universe_size == 16
    assert analysis.eligible_size == 16
    assert len(analysis.candidates) == 8
    assert all(0 <= candidate.score <= 100 for candidate in analysis.candidates)
    assert all(candidate.reasons for candidate in analysis.candidates)
    industry_counts = {}
    for candidate in analysis.candidates:
        industry_counts[candidate.industry] = industry_counts.get(candidate.industry, 0) + 1
    assert max(industry_counts.values()) <= 2
    assert computation.rankings.height == 16


def test_future_financial_announcement_is_not_used() -> None:
    inputs = list(factor_inputs())
    future = (
        inputs[3]
        .head(1)
        .with_columns(
            pl.lit("20260830").alias("ann_date"),
            pl.lit(999).alias("roe_dt"),
        )
    )
    inputs[3] = pl.concat([inputs[3], future], how="vertical_relaxed")
    config = FactorConfig(
        minimum_listing_days=0,
        liquidity_exclusion_percentile=0,
        candidate_count=5,
    )

    computation = FactorAnalyzer(config).analyze(
        AS_OF_DATE,
        *inputs,
        AS_OF_DATE,
        AS_OF_DATE,
        AS_OF_DATE,
    )

    first_stock = (
        computation.rankings.filter(pl.col("ts_code") == "000001.SZ").get_column("ann_date").item()
    )
    assert first_stock == date(2026, 4, 30)


def test_risk_filters_exclude_st_and_financial_industry() -> None:
    inputs = list(factor_inputs())
    inputs[4] = inputs[4].with_columns(
        pl.when(pl.col("ts_code") == "000001.SZ")
        .then(pl.lit("*ST样本"))
        .otherwise(pl.col("name"))
        .alias("name"),
        pl.when(pl.col("ts_code") == "000002.SZ")
        .then(pl.lit("银行"))
        .otherwise(pl.col("industry"))
        .alias("industry"),
    )
    config = FactorConfig(
        minimum_listing_days=0,
        liquidity_exclusion_percentile=0,
    )

    analysis = (
        FactorAnalyzer(config)
        .analyze(
            AS_OF_DATE,
            *inputs,
            AS_OF_DATE,
            AS_OF_DATE,
            AS_OF_DATE,
        )
        .analysis
    )

    assert analysis.eligible_size == 14
    assert analysis.exclusion_counts["special_treatment_or_delisting"] == 1
    assert analysis.exclusion_counts["excluded_financial_industry"] == 1
