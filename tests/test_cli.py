from datetime import date

from qtrade.cli.main import build_parser, parse_datasets
from qtrade.domain import Dataset


def test_market_analysis_command_parses() -> None:
    args = build_parser().parse_args(["analyze", "market", "--date", "2026-07-24"])

    assert args.command == "analyze"
    assert args.analyze_command == "market"
    assert args.date == date(2026, 7, 24)


def test_backfill_command_parses_default_datasets() -> None:
    args = build_parser().parse_args(
        ["data", "backfill", "--start", "2026-01-01", "--end", "2026-07-24"]
    )

    datasets = parse_datasets(args.datasets, [])
    assert datasets == [
        Dataset.DAILY_PRICES,
        Dataset.ADJUST_FACTORS,
        Dataset.INDEX_DAILY,
    ]
