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
    args = build_parser().parse_args(
        [
            "analyze",
            "factors",
            "--date",
            "2026-07-24",
            "--origin",
            "live_observed",
        ]
    )

    assert args.analyze_command == "factors"
    assert args.date == date(2026, 7, 24)
    assert args.origin == "live_observed"


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


def test_historical_signal_build_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "research",
            "build-signals",
            "--protocol",
            "quality_v1",
            "--partition",
            "development",
            "--frequency",
            "month_end",
        ]
    )

    assert args.research_command == "build-signals"
    assert args.frequency == "month_end"


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


def test_formal_candidate_backtest_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "backtest",
            "candidates",
            "--start",
            "2023-01-01",
            "--end",
            "2024-12-31",
            "--protocol",
            "quality_v1",
            "--partition",
            "holdout",
            "--reveal-holdout",
        ]
    )

    assert args.protocol_id == "quality_v1"
    assert args.partition == "holdout"
    assert args.reveal_holdout is True


def test_protocol_create_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "protocol",
            "create",
            "--id",
            "quality_v1",
            "--title",
            "Quality v1",
            "--hypothesis",
            "Quality persists",
            "--development-start",
            "2018-01-01",
            "--development-end",
            "2020-12-31",
            "--validation-start",
            "2021-01-01",
            "--validation-end",
            "2022-12-31",
            "--holdout-start",
            "2023-01-01",
            "--holdout-end",
            "2024-12-31",
        ]
    )

    assert args.protocol_command == "create"
    assert args.protocol_id == "quality_v1"
    assert args.signal_frequency == "month_end"


def test_protocol_pin_data_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "protocol",
            "pin-data",
            "--id",
            "quality_v1",
            "--partition",
            "validation",
            "--version",
            "a" * 64,
        ]
    )

    assert args.protocol_command == "pin-data"
    assert args.partition == "validation"


def test_daily_observation_command_parses() -> None:
    args = build_parser().parse_args(["observe", "daily", "--date", "2026-07-24"])

    assert args.observe_command == "daily"
    assert args.date == date(2026, 7, 24)


def test_daily_pipeline_command_parses() -> None:
    args = build_parser().parse_args(
        ["pipeline", "daily", "--date", "2026-07-24", "--skip-data"]
    )

    assert args.pipeline_command == "daily"
    assert args.skip_data is True


def test_dashboard_build_command_parses() -> None:
    args = build_parser().parse_args(["dashboard", "build", "--date", "2026-07-24"])

    assert args.dashboard_command == "build"
    assert args.date == date(2026, 7, 24)


def test_ui_command_parses() -> None:
    args = build_parser().parse_args(["ui", "--port", "9000", "--no-browser"])

    assert args.command == "ui"
    assert args.host == "127.0.0.1"
    assert args.port == 9000
    assert args.no_browser is True


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


def test_financial_backfill_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "data",
            "financial-backfill",
            "--start",
            "2015-01-01",
            "--end",
            "2026-07-24",
            "--lookback-quarters",
            "8",
        ]
    )

    assert args.data_command == "financial-backfill"
    assert args.start == date(2015, 1, 1)
    assert args.lookback_quarters == 8


def test_research_backfill_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "data",
            "research-backfill",
            "--start",
            "2015-01-01",
            "--end",
            "2026-07-24",
        ]
    )

    assert args.data_command == "research-backfill"
    assert args.end == date(2026, 7, 24)
    assert args.lookback_quarters == 12


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
    assert args.frequency == "daily"


def test_bulk_index_backfill_command_parses() -> None:
    args = build_parser().parse_args(
        [
            "data",
            "index-backfill",
            "--start",
            "2015-01-01",
            "--end",
            "2026-07-24",
        ]
    )

    assert args.data_command == "index-backfill"
    assert args.start == date(2015, 1, 1)
