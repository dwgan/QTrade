from datetime import date

import polars as pl

from qtrade.config import BacktestConfig
from qtrade.research.backtest import CandidateBacktester


def test_candidate_backtest_uses_next_day_and_charges_turnover_cost() -> None:
    dates = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
    prices = pl.DataFrame(
        [
            {"ts_code": code, "trade_date": trade_date, "close": close}
            for code, closes in {
                "000001.SZ": [10.0, 10.0, 11.0],
                "000002.SZ": [10.0, 10.0, 11.0],
                "000003.SZ": [10.0, 10.0, 10.0],
            }.items()
            for trade_date, close in zip(dates, closes, strict=True)
        ]
    )
    adjustments = prices.select("ts_code", "trade_date").with_columns(
        pl.lit(1.0).alias("adj_factor")
    )
    ranking = pl.DataFrame(
        {
            "ts_code": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "industry": ["A", "B", "C"],
            "score": [100.0, 90.0, 10.0],
        }
    )
    index_daily = pl.DataFrame(
        {
            "ts_code": ["000300.SH"] * 3,
            "trade_date": dates,
            "close": [100.0, 100.0, 100.0],
        }
    )

    analysis, curve, trades = CandidateBacktester(
        BacktestConfig(
            initial_capital=100_000,
            transaction_cost_rate=0.001,
            candidate_count=2,
        )
    ).run(
        dates[0],
        dates[2],
        [(dates[0], ranking)],
        prices,
        adjustments,
        index_daily,
    )

    assert analysis.rebalance_count == 1
    assert trades.row(0, named=True)["execution_date"] == dates[1]
    assert trades.row(0, named=True)["turnover"] == 1
    assert analysis.total_cost == 100
    assert analysis.portfolio.total_return > analysis.benchmark.total_return
    assert curve.height == 3
