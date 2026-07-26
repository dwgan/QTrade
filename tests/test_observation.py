from datetime import date
from pathlib import Path

import polars as pl

from qtrade.config import BacktestConfig, ObservationConfig
from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import DataBatch, Dataset
from qtrade.observation.analyzer import ObservationAnalyzer
from qtrade.observation.service import ObservationService


def _ranking(order: list[str]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "rank": rank,
                "ts_code": code,
                "name": f"Stock {code[:6]}",
                "industry": f"Industry {rank}",
                "score": float(100 - rank),
            }
            for rank, code in enumerate(order, start=1)
        ]
    )


def test_observation_analyzer_tracks_candidates_movers_and_watchlist() -> None:
    previous = _ranking(["000001.SZ", "000002.SZ", "000003.SZ"])
    current = _ranking(["000003.SZ", "000002.SZ", "000001.SZ"])
    analyzer = ObservationAnalyzer(
        ObservationConfig(
            watchlist_symbols=["000001.sz", "999999.SZ"],
            candidate_count=2,
            rank_mover_count=5,
        )
    )

    entered, exited, movers, watchlist = analyzer.analyze(current, previous)

    assert [item.ts_code for item in entered] == ["000003.SZ"]
    assert [item.ts_code for item in exited] == ["000001.SZ"]
    assert [(item.ts_code, item.rank_change) for item in movers] == [
        ("000003.SZ", 2),
        ("000001.SZ", -2),
    ]
    assert watchlist[0].status == "ranked"
    assert watchlist[1].status == "not_ranked"


def test_observation_service_writes_daily_report_with_shadow_portfolio(
    tmp_path: Path,
) -> None:
    first_date = date(2026, 1, 2)
    second_date = date(2026, 1, 5)
    reports_root = tmp_path / "reports"
    for snapshot_date, order in (
        (first_date, ["000001.SZ", "000002.SZ"]),
        (second_date, ["000002.SZ", "000001.SZ"]),
    ):
        directory = reports_root / "factors" / snapshot_date.isoformat()
        directory.mkdir(parents=True)
        _ranking(order).write_parquet(directory / "rankings.parquet")

    curated = ParquetDatasetStore(tmp_path / "curated", "curated")
    for trade_date, closes in (
        (first_date, [10.0, 10.0]),
        (second_date, [10.5, 10.0]),
    ):
        codes = ["000001.SZ", "000002.SZ"]
        frames = {
            Dataset.DAILY_PRICES: pl.DataFrame(
                {"ts_code": codes, "trade_date": [trade_date] * 2, "close": closes}
            ),
            Dataset.ADJUST_FACTORS: pl.DataFrame(
                {
                    "ts_code": codes,
                    "trade_date": [trade_date] * 2,
                    "adj_factor": [1.0, 1.0],
                }
            ),
            Dataset.INDEX_DAILY: pl.DataFrame(
                {
                    "ts_code": ["000300.SH"],
                    "trade_date": [trade_date],
                    "close": [100.0],
                }
            ),
            Dataset.STOCK_LIMIT: pl.DataFrame(
                {
                    "ts_code": codes,
                    "trade_date": [trade_date] * 2,
                    "up_limit": [20.0, 20.0],
                    "down_limit": [1.0, 1.0],
                }
            ),
        }
        for dataset, frame in frames.items():
            curated.write(DataBatch(dataset, "test", trade_date, frame))

    result = ObservationService(
        observation_config=ObservationConfig(
            watchlist_symbols=["000001.SZ"],
            shadow_lookback_calendar_days=30,
        ),
        backtest_config=BacktestConfig(
            transaction_cost_rate=0,
            slippage_rate=0,
            candidate_count=2,
        ),
        curated_store=curated,
        provider="test",
        reports_root=reports_root,
    ).run(second_date)

    assert result.observation.shadow_portfolio is not None
    assert result.observation.shadow_portfolio.holdings == [
        "000001.SZ",
        "000002.SZ",
    ]
    assert result.json_path.exists()
    assert "影子组合" in result.markdown_path.read_text(encoding="utf-8")
    assert (result.markdown_path.parent / "shadow_equity_curve.parquet").exists()
    assert (result.markdown_path.parent / "shadow_rebalances.parquet").exists()
