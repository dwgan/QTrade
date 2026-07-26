from datetime import date
from pathlib import Path

import polars as pl

from qtrade.config import BacktestConfig
from qtrade.research.backtest import CandidateBacktester
from qtrade.research.reporting import ResearchReportWriter


def test_candidate_backtest_uses_next_day_and_charges_turnover_cost(tmp_path: Path) -> None:
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
    assert trades.row(0, named=True)["transaction_cost"] == 100
    assert trades.row(0, named=True)["slippage_cost"] == 50
    assert analysis.total_cost == 150
    assert analysis.portfolio.total_return > analysis.benchmark.total_return
    assert curve.height == 3
    json_path, markdown_path = ResearchReportWriter(tmp_path).write_backtest(
        analysis, curve, trades
    )
    assert json_path.exists()
    assert "成本敏感性" in markdown_path.read_text(encoding="utf-8")
    assert (markdown_path.parent / "equity_curve.parquet").exists()


def test_limit_up_buy_is_retried_on_the_next_tradable_day() -> None:
    dates = [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
    ]
    prices = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"] * 4,
            "trade_date": dates,
            "close": [10.0, 11.0, 11.0, 12.0],
        }
    )
    adjustments = prices.select("ts_code", "trade_date").with_columns(
        pl.lit(1.0).alias("adj_factor")
    )
    limits = pl.DataFrame(
        {
            "ts_code": ["000001.SZ"] * 4,
            "trade_date": dates,
            "up_limit": [11.0, 11.0, 12.1, 12.1],
            "down_limit": [9.0, 9.0, 9.9, 9.9],
        }
    )
    ranking = pl.DataFrame(
        {"ts_code": ["000001.SZ"], "industry": ["A"], "score": [100.0]}
    )
    index_daily = pl.DataFrame(
        {
            "ts_code": ["000300.SH"] * 4,
            "trade_date": dates,
            "close": [100.0] * 4,
        }
    )

    analysis, _, trades = CandidateBacktester(
        BacktestConfig(transaction_cost_rate=0, slippage_rate=0, candidate_count=1)
    ).run(
        dates[0],
        dates[-1],
        [(dates[0], ranking)],
        prices,
        adjustments,
        index_daily,
        limits,
    )

    assert trades.get_column("status").to_list() == ["partial", "completed"]
    assert trades.get_column("execution_date").to_list() == [dates[1], dates[2]]
    assert analysis.blocked_buy_orders == 1
    assert analysis.delayed_execution_days == 1


def test_limit_down_sell_remains_held_until_it_can_trade() -> None:
    dates = [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
    ]
    prices = pl.DataFrame(
        [
            {"ts_code": code, "trade_date": trade_date, "close": close}
            for code, closes in {
                "000001.SZ": [10.0, 10.0, 9.0, 9.0],
                "000002.SZ": [10.0, 10.0, 10.0, 10.0],
            }.items()
            for trade_date, close in zip(dates, closes, strict=True)
        ]
    )
    adjustments = prices.select("ts_code", "trade_date").with_columns(
        pl.lit(1.0).alias("adj_factor")
    )
    limits = prices.select("ts_code", "trade_date").with_columns(
        pl.when(
            (pl.col("ts_code") == "000001.SZ")
            & (pl.col("trade_date") == dates[2])
        )
        .then(10.0)
        .otherwise(20.0)
        .alias("up_limit"),
        pl.when(
            (pl.col("ts_code") == "000001.SZ")
            & (pl.col("trade_date") == dates[2])
        )
        .then(9.0)
        .otherwise(1.0)
        .alias("down_limit"),
    )
    first = pl.DataFrame(
        {"ts_code": ["000001.SZ"], "industry": ["A"], "score": [100.0]}
    )
    second = pl.DataFrame(
        {"ts_code": ["000002.SZ"], "industry": ["B"], "score": [100.0]}
    )
    index_daily = pl.DataFrame(
        {
            "ts_code": ["000300.SH"] * 4,
            "trade_date": dates,
            "close": [100.0] * 4,
        }
    )

    analysis, _, trades = CandidateBacktester(
        BacktestConfig(transaction_cost_rate=0, slippage_rate=0, candidate_count=1)
    ).run(
        dates[0],
        dates[-1],
        [(dates[0], first), (dates[1], second)],
        prices,
        adjustments,
        index_daily,
        limits,
    )

    delayed = trades.filter(pl.col("signal_date") == dates[1])
    assert delayed.get_column("status").to_list() == ["partial", "completed"]
    assert analysis.blocked_sell_orders == 1
    assert analysis.sample_split_date is not None
    assert len(analysis.sample_performance) == 2
    assert len(analysis.cost_sensitivity) == 3
