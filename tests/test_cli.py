from datetime import date

from qtrade.cli.main import build_parser, main, parse_datasets
from qtrade.domain import Dataset


def test_market_analysis_command_parses() -> None:
    args = build_parser().parse_args(["analyze", "market", "--date", "2026-07-24"])

    assert args.command == "analyze"
    assert args.analyze_command == "market"
    assert args.date == date(2026, 7, 24)


def test_industry_analysis_command_parses() -> None:
    args = build_parser().parse_args(["analyze", "industry", "--date", "2026-07-24"])

    assert args.command == "analyze"
    assert args.analyze_command == "industry"
    assert args.date == date(2026, 7, 24)


def test_factor_analysis_command_parses() -> None:
    args = build_parser().parse_args(["analyze", "factors", "--date", "2026-07-24"])

    assert args.analyze_command == "factors"
    assert args.date == date(2026, 7, 24)


def test_factor_research_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "research",
            "factors",
            "--start",
            "2025-01-01",
            "--end",
            "2025-12-31",
            "--horizon",
            "20",
            "--quantiles",
            "5",
        ]
    )

    assert args.research_command == "factors"
    assert args.horizon == 20


def test_candidate_backtest_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "backtest",
            "candidates",
            "--start",
            "2025-01-01",
            "--end",
            "2025-12-31",
            "--split-date",
            "2025-09-01",
        ]
    )

    assert args.backtest_command == "candidates"
    assert args.split_date == date(2025, 9, 1)


def test_daily_observation_command_parses() -> None:
    args = build_parser().parse_args(["observe", "daily", "--date", "2026-07-24"])

    assert args.observe_command == "daily"
    assert args.date == date(2026, 7, 24)


def test_financial_snapshot_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "data",
            "financials",
            "--date",
            "2026-07-24",
            "--periods",
            "20250331,20250630",
        ]
    )

    assert args.data_command == "financials"
    assert args.periods == "20250331,20250630"


def test_invalid_financial_period_is_rejected_before_provider_setup() -> None:
    assert (
        main(
            [
                "data",
                "financials",
                "--date",
                "2026-07-24",
                "--periods",
                "bad",
            ]
        )
        == 2
    )


def test_backfill_command_parses_default_datasets() -> None:
    args = build_parser().parse_args(
        ["data", "backfill", "--start", "2026-01-01", "--end", "2026-07-24"]
    )

    datasets = parse_datasets(args.datasets, [])
    assert datasets == [
        Dataset.DAILY_PRICES,
        Dataset.ADJUST_FACTORS,
        Dataset.INDEX_DAILY,
        Dataset.DAILY_BASIC,
        Dataset.STOCK_LIMIT,
    ]
