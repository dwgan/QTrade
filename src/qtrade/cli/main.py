from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from qtrade.config import AppConfig, load_config
from qtrade.data.providers.tushare import TushareProvider
from qtrade.data.service import DataIngestionService
from qtrade.data.storage import ParquetDatasetStore
from qtrade.data.validation import DataValidator
from qtrade.domain import Dataset
from qtrade.industry.service import IndustryAnalysisService
from qtrade.market.service import MarketAnalysisService


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Date must use YYYY-MM-DD format.") from exc


def parse_datasets(value: str | None, defaults: list[str]) -> list[Dataset]:
    names = defaults if value is None else [item.strip() for item in value.split(",")]
    if not any(names):
        raise ValueError("At least one dataset is required.")
    try:
        return list(dict.fromkeys(Dataset(name) for name in names if name))
    except ValueError as exc:
        valid = ", ".join(dataset.value for dataset in Dataset)
        raise ValueError(f"Unknown dataset. Valid values: {valid}") from exc


def build_service(config: AppConfig) -> DataIngestionService:
    config.paths.create()
    provider = TushareProvider(config.provider, config.market)
    return DataIngestionService(
        provider=provider,
        raw_store=ParquetDatasetStore(config.paths.raw, "raw"),
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        validator=DataValidator(config.validation),
        snapshots_root=config.paths.snapshots,
        reports_root=config.paths.reports,
    )


def build_market_service(config: AppConfig) -> MarketAnalysisService:
    config.paths.create()
    return MarketAnalysisService(
        config=config.market,
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        provider=config.provider.name,
        reports_root=config.paths.reports,
    )


def build_industry_service(config: AppConfig) -> IndustryAnalysisService:
    config.paths.create()
    return IndustryAnalysisService(
        config=config.industry,
        benchmark_code=config.market.primary_index_code,
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        provider=config.provider.name,
        reports_root=config.paths.reports,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qtrade", description="QTrade research toolkit")
    parser.add_argument("--config", default="config/base.yaml", help="YAML configuration path")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    commands = parser.add_subparsers(dest="command", required=True)

    data = commands.add_parser("data", help="Market data operations")
    data_commands = data.add_subparsers(dest="data_command", required=True)

    update = data_commands.add_parser("update", help="Update datasets for one date")
    update.add_argument("--date", required=True, type=parse_date)
    update.add_argument("--datasets", help="Comma-separated dataset names")

    validate = data_commands.add_parser("validate", help="Validate stored curated datasets")
    validate.add_argument("--date", required=True, type=parse_date)
    validate.add_argument("--datasets", help="Comma-separated dataset names")

    backfill = data_commands.add_parser(
        "backfill", help="Backfill daily datasets over a date range"
    )
    backfill.add_argument("--start", required=True, type=parse_date)
    backfill.add_argument("--end", required=True, type=parse_date)
    backfill.add_argument(
        "--datasets",
        default="daily_prices,adjust_factors,index_daily",
        help="Comma-separated daily dataset names",
    )

    data_commands.add_parser("datasets", help="List supported datasets")

    analyze = commands.add_parser("analyze", help="Research analysis")
    analyze_commands = analyze.add_subparsers(dest="analyze_command", required=True)
    market = analyze_commands.add_parser("market", help="Generate daily market analysis")
    market.add_argument("--date", required=True, type=parse_date)
    industry = analyze_commands.add_parser(
        "industry", help="Generate daily industry and style analysis"
    )
    industry.add_argument("--date", required=True, type=parse_date)
    return parser


def _print_update(result) -> None:
    for item in result.datasets:
        if item.status == "completed":
            print(f"[OK] {item.dataset.value}: {item.row_count} rows")
        else:
            print(f"[FAILED] {item.dataset.value}: {item.error}", file=sys.stderr)
    print(f"Update status: {'success' if result.succeeded else 'failed'}")


def _print_validation(reports) -> None:
    for report in reports:
        print(
            f"[{'OK' if report.passed else 'FAILED'}] "
            f"{report.dataset.value}: {report.row_count} rows, {len(report.issues)} issues"
        )


def run(args: argparse.Namespace) -> int:
    if args.command == "data" and args.data_command == "datasets":
        for dataset in Dataset:
            print(dataset.value)
        return 0

    config = load_config(Path(args.config))
    if args.command == "analyze":
        try:
            if args.analyze_command == "market":
                result = build_market_service(config).run(args.date)
                analysis = result.analysis
                temperature = analysis.temperature if analysis.temperature is not None else "N/A"
                print(f"Market state: {analysis.state.value}; temperature: {temperature}")
                succeeded = analysis.temperature is not None
            else:
                result = build_industry_service(config).run(args.date)
                analysis = result.analysis
                print(
                    f"Industries: {len(analysis.industries)}; "
                    f"confidence: {analysis.data_confidence}"
                )
                succeeded = bool(analysis.industries)
            print(f"Report: {result.markdown_path}")
            return 0 if succeeded else 1
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    try:
        datasets = parse_datasets(args.datasets, config.update.datasets)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        service = build_service(config)
        if args.data_command == "update":
            result = service.update(args.date, datasets)
            _print_update(result)
            return 0 if result.succeeded else 1
        if args.data_command == "backfill":
            result = service.backfill(args.start, args.end, datasets)
            print(
                f"Trading dates: {result.trading_dates}; completed: "
                f"{result.completed_dates}; skipped: {result.skipped_dates}; "
                f"failed: {len(result.failed_dates)}"
            )
            if result.failed_dates:
                print(
                    "Failed dates: "
                    + ", ".join(value.isoformat() for value in result.failed_dates),
                    file=sys.stderr,
                )
            return 0 if result.succeeded else 1

        reports = service.validate_existing(args.date, datasets)
        _print_validation(reports)
        passed = all(report.passed for report in reports)
        if config.validation.fail_on_warning:
            passed = passed and all(not report.issues for report in reports)
        return 0 if passed else 1
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(args)
