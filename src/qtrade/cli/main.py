from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from qtrade.config import AppConfig, load_config
from qtrade.dashboard.builder import DashboardBuilder
from qtrade.data.providers.tushare import TushareProvider
from qtrade.data.service import DataIngestionService, StoredDataValidationService
from qtrade.data.storage import ParquetDatasetStore
from qtrade.data.validation import DataValidator
from qtrade.domain import Dataset
from qtrade.factors.service import FactorAnalysisService
from qtrade.industry.service import IndustryAnalysisService
from qtrade.market.service import MarketAnalysisService
from qtrade.observation.service import ObservationService
from qtrade.pipeline.service import DailyPipelineService
from qtrade.research.protocols import (
    ExperimentStore,
    PartitionName,
    ProtocolStore,
    ResearchPartition,
    StrategyProtocol,
    canonical_hash,
    current_git_commit,
    git_research_tree_is_clean,
)
from qtrade.research.service import ResearchService
from qtrade.research.signals import HistoricalSignalBuildService, SignalFrequency


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


def build_validation_service(config: AppConfig) -> StoredDataValidationService:
    config.paths.create()
    return StoredDataValidationService(
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        validator=DataValidator(config.validation),
        provider=config.provider.name,
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


def build_factor_service(config: AppConfig) -> FactorAnalysisService:
    config.paths.create()
    return FactorAnalysisService(
        config=config.factors,
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        provider=config.provider.name,
        reports_root=config.paths.reports,
    )


def build_research_service(config: AppConfig) -> ResearchService:
    config.paths.create()
    return ResearchService(
        research_config=config.research,
        backtest_config=config.backtest,
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        provider=config.provider.name,
        reports_root=config.paths.reports,
        runtime_root=config.paths.runtime,
        project_root=config.project_root,
        factor_config=config.factors.model_dump(mode="json"),
    )


def research_config_hash(config: AppConfig) -> str:
    return canonical_hash(
        {
            "factors": config.factors.model_dump(mode="json"),
            "research": config.research.model_dump(mode="json"),
            "backtest": config.backtest.model_dump(mode="json"),
        }
    )


def build_historical_signal_service(
    config: AppConfig,
) -> HistoricalSignalBuildService:
    return HistoricalSignalBuildService(
        factor_service=build_factor_service(config),
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        provider=config.provider.name,
        protocol_store=ProtocolStore(config.paths.runtime),
        project_root=config.project_root,
        config_hash=research_config_hash(config),
    )


def build_observation_service(config: AppConfig) -> ObservationService:
    config.paths.create()
    return ObservationService(
        observation_config=config.observation,
        backtest_config=config.backtest,
        curated_store=ParquetDatasetStore(config.paths.curated, "curated"),
        provider=config.provider.name,
        reports_root=config.paths.reports,
    )


def build_pipeline_service(
    config: AppConfig,
    data_service: DataIngestionService | None,
    data_service_error: str | None = None,
) -> DailyPipelineService:
    config.paths.create()
    return DailyPipelineService(
        data_service=data_service,
        market_service=build_market_service(config),
        industry_service=build_industry_service(config),
        factor_service=build_factor_service(config),
        observation_service=build_observation_service(config),
        dashboard_builder=DashboardBuilder(config.paths.reports),
        reports_root=config.paths.reports,
        data_service_error=data_service_error,
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
        default="daily_prices,adjust_factors,index_daily,daily_basic,stock_limit",
        help="Comma-separated daily dataset names",
    )

    financials = data_commands.add_parser(
        "financials", help="Fetch full-market quarterly financial indicators"
    )
    financials.add_argument("--date", required=True, type=parse_date)
    financials.add_argument(
        "--periods",
        required=True,
        help="Comma-separated quarter-end dates in YYYYMMDD format",
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
    factors = analyze_commands.add_parser("factors", help="Generate multi-factor stock candidates")
    factors.add_argument("--date", required=True, type=parse_date)
    factors.add_argument(
        "--origin",
        choices=["reconstructed", "live_observed"],
        default="reconstructed",
        help="Whether this signal was reconstructed later or observed live",
    )

    research = commands.add_parser("research", help="Historical factor research")
    research_commands = research.add_subparsers(dest="research_command", required=True)
    factor_research = research_commands.add_parser(
        "factors", help="Calculate factor Rank IC and quantile returns"
    )
    factor_research.add_argument("--start", required=True, type=parse_date)
    factor_research.add_argument("--end", required=True, type=parse_date)
    factor_research.add_argument("--horizon", type=int)
    factor_research.add_argument("--quantiles", type=int)
    build_signals = research_commands.add_parser(
        "build-signals",
        help="Reconstruct immutable month-end or week-end signals for a protocol",
    )
    build_signals.add_argument("--protocol", required=True, dest="protocol_id")
    build_signals.add_argument(
        "--partition",
        required=True,
        choices=[
            PartitionName.DEVELOPMENT.value,
            PartitionName.VALIDATION.value,
            PartitionName.HOLDOUT.value,
        ],
    )
    build_signals.add_argument(
        "--frequency",
        choices=[item.value for item in SignalFrequency],
        default=SignalFrequency.MONTH_END.value,
    )

    protocol = commands.add_parser("protocol", help="Anti-overfitting research protocols")
    protocol_commands = protocol.add_subparsers(
        dest="protocol_command", required=True
    )
    protocol_create = protocol_commands.add_parser(
        "create", help="Create a draft strategy protocol"
    )
    protocol_create.add_argument("--id", required=True, dest="protocol_id")
    protocol_create.add_argument("--title", required=True)
    protocol_create.add_argument("--hypothesis", required=True)
    protocol_create.add_argument("--development-start", required=True, type=parse_date)
    protocol_create.add_argument("--development-end", required=True, type=parse_date)
    protocol_create.add_argument("--validation-start", required=True, type=parse_date)
    protocol_create.add_argument("--validation-end", required=True, type=parse_date)
    protocol_create.add_argument("--holdout-start", required=True, type=parse_date)
    protocol_create.add_argument("--holdout-end", required=True, type=parse_date)
    protocol_create.add_argument("--allowed-trials", type=int, default=1)
    protocol_create.add_argument(
        "--signal-frequency",
        choices=[item.value for item in SignalFrequency],
        default=SignalFrequency.MONTH_END.value,
    )
    protocol_create.add_argument("--parent")
    protocol_freeze = protocol_commands.add_parser(
        "freeze", help="Freeze a draft protocol and calculate its immutable hash"
    )
    protocol_freeze.add_argument("--id", required=True, dest="protocol_id")
    protocol_freeze.add_argument(
        "--data-version",
        help="Optional precomputed immutable data version",
    )
    protocol_pin = protocol_commands.add_parser(
        "pin-data", help="Pin an exploratory run data version to a draft partition"
    )
    protocol_pin.add_argument("--id", required=True, dest="protocol_id")
    protocol_pin.add_argument(
        "--partition",
        required=True,
        choices=[
            PartitionName.DEVELOPMENT.value,
            PartitionName.VALIDATION.value,
            PartitionName.HOLDOUT.value,
        ],
    )
    protocol_pin.add_argument(
        "--version",
        dest="data_version",
        help="Optional precomputed hash; omit to calculate without running performance",
    )
    protocol_show = protocol_commands.add_parser("show", help="Show protocol and state")
    protocol_show.add_argument("--id", required=True, dest="protocol_id")
    protocol_commands.add_parser("list", help="List research protocols")
    protocol_experiments = protocol_commands.add_parser(
        "experiments", help="List all recorded trials for a protocol"
    )
    protocol_experiments.add_argument("--id", required=True, dest="protocol_id")

    backtest = commands.add_parser("backtest", help="Portfolio backtesting")
    backtest_commands = backtest.add_subparsers(dest="backtest_command", required=True)
    candidates = backtest_commands.add_parser(
        "candidates", help="Backtest archived factor candidates"
    )
    candidates.add_argument("--start", required=True, type=parse_date)
    candidates.add_argument("--end", required=True, type=parse_date)
    candidates.add_argument(
        "--split-date",
        type=parse_date,
        help="First out-of-sample date; defaults to configured split ratio",
    )
    candidates.add_argument("--protocol", dest="protocol_id")
    candidates.add_argument(
        "--partition",
        choices=[item.value for item in PartitionName if item != PartitionName.FORWARD],
        help="Frozen protocol partition for a formal validation run",
    )
    candidates.add_argument(
        "--reveal-holdout",
        action="store_true",
        help="Explicitly reveal a sealed holdout partition",
    )

    observe = commands.add_parser("observe", help="Daily research observation")
    observe_commands = observe.add_subparsers(dest="observe_command", required=True)
    daily_observation = observe_commands.add_parser(
        "daily", help="Generate candidate, watchlist, and shadow portfolio report"
    )
    daily_observation.add_argument("--date", required=True, type=parse_date)

    pipeline = commands.add_parser("pipeline", help="End-of-day workflow")
    pipeline_commands = pipeline.add_subparsers(dest="pipeline_command", required=True)
    daily_pipeline = pipeline_commands.add_parser(
        "daily", help="Run data, analysis, observation, and dashboard steps"
    )
    daily_pipeline.add_argument("--date", required=True, type=parse_date)
    daily_pipeline.add_argument("--datasets", help="Comma-separated dataset names")
    daily_pipeline.add_argument(
        "--skip-data",
        action="store_true",
        help="Use existing curated data without contacting the provider",
    )

    dashboard = commands.add_parser("dashboard", help="Local read-only dashboard")
    dashboard_commands = dashboard.add_subparsers(
        dest="dashboard_command", required=True
    )
    dashboard_build = dashboard_commands.add_parser(
        "build", help="Build dashboard from existing reports"
    )
    dashboard_build.add_argument("--date", required=True, type=parse_date)

    ui = commands.add_parser("ui", help="Launch the local interactive research interface")
    ui.add_argument("--host", default="127.0.0.1", help="Local bind address")
    ui.add_argument("--port", default=8765, type=int, help="Local HTTP port")
    ui.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the default browser automatically",
    )
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
    if args.command == "protocol":
        protocols = ProtocolStore(config.paths.runtime)
        experiments = ExperimentStore(config.paths.runtime)
        try:
            if args.protocol_command == "create":
                partitions = [
                    ResearchPartition(
                        name=PartitionName.DEVELOPMENT,
                        start_date=args.development_start,
                        end_date=args.development_end,
                    ),
                    ResearchPartition(
                        name=PartitionName.VALIDATION,
                        start_date=args.validation_start,
                        end_date=args.validation_end,
                    ),
                    ResearchPartition(
                        name=PartitionName.HOLDOUT,
                        start_date=args.holdout_start,
                        end_date=args.holdout_end,
                    ),
                    ResearchPartition(
                        name=PartitionName.FORWARD,
                        start_date=args.holdout_end + timedelta(days=1),
                        end_date=None,
                    ),
                ]
                created = StrategyProtocol(
                    protocol_id=args.protocol_id,
                    parent_protocol_id=args.parent,
                    title=args.title,
                    hypothesis=args.hypothesis,
                    partitions=partitions,
                    strategy={
                        "universe": "historical configured A-share universe",
                        "signal_frequency": args.signal_frequency,
                        "factors": config.factors.model_dump(mode="json"),
                        "research": config.research.model_dump(mode="json"),
                    },
                    execution=config.backtest.model_dump(mode="json"),
                    acceptance_criteria={
                        "future_leakage_rows": 0,
                        "positive_validation_excess_return": True,
                        "positive_holdout_excess_return": True,
                        "cost_multiplier_stress": 2.0,
                    },
                    allowed_trials=args.allowed_trials,
                    code_commit=current_git_commit(config.project_root),
                    config_hash=research_config_hash(config),
                )
                path = protocols.create(created)
                print(f"Draft protocol created: {path}")
                print("Review it, commit the code/config, then freeze it before validation.")
                return 0
            if args.protocol_command == "freeze":
                if not git_research_tree_is_clean(config.project_root):
                    raise ValueError(
                        "Commit changes under src, config, and pyproject.toml before "
                        "freezing a formal protocol."
                    )
                frozen = protocols.freeze(
                    args.protocol_id,
                    data_version=args.data_version,
                    code_commit=current_git_commit(config.project_root),
                    config_hash=research_config_hash(config),
                )
                print(f"Protocol frozen: {frozen.protocol_id}")
                print(f"Hash: {frozen.content_hash}")
                return 0
            if args.protocol_command == "pin-data":
                partition = PartitionName(args.partition)
                data_version = args.data_version
                if data_version is None:
                    draft = protocols.load(args.protocol_id)
                    selected = draft.partition(partition)
                    if selected.end_date is None:
                        raise ValueError("Cannot pin an open-ended partition.")
                    data_version = build_research_service(
                        config
                    ).candidate_data_version(
                        selected.start_date,
                        selected.end_date,
                    )
                updated = protocols.pin_data_version(
                    args.protocol_id,
                    partition,
                    data_version,
                )
                print(
                    f"Pinned {args.partition} data version for "
                    f"{updated.protocol_id}: {data_version}"
                )
                return 0
            if args.protocol_command == "show":
                item = protocols.load(args.protocol_id)
                state = protocols.state(args.protocol_id)
                print(
                    json.dumps(
                        {
                            "protocol": item.model_dump(mode="json"),
                            "state": state.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.protocol_command == "experiments":
                for item in experiments.list(args.protocol_id):
                    partition = item.partition.value if item.partition else "exploratory"
                    print(
                        f"{item.experiment_id} {item.status.value} {partition} "
                        f"{item.start_date}..{item.end_date}"
                    )
                return 0
            for item in protocols.list():
                print(
                    f"{item.protocol_id} v{item.version} {item.status.value} "
                    f"{item.title}"
                )
            return 0
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "ui":
        if not 1 <= args.port <= 65535:
            print("Error: port must be between 1 and 65535.", file=sys.stderr)
            return 2
        from qtrade.ui.server import serve_ui

        try:
            serve_ui(
                config=config,
                config_path=Path(args.config),
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
            )
            return 0
        except OSError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "dashboard":
        try:
            path = DashboardBuilder(config.paths.reports).build(args.date)
            print(f"Dashboard: {path}")
            return 0
        except (OSError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "pipeline":
        try:
            datasets = parse_datasets(args.datasets, config.update.datasets)
            data_service = None
            data_service_error = None
            if not args.skip_data:
                try:
                    data_service = build_service(config)
                except RuntimeError as exc:
                    data_service_error = str(exc)
            result = build_pipeline_service(
                config,
                data_service,
                data_service_error,
            ).run(
                args.date,
                datasets,
                skip_data=args.skip_data,
            )
            for step in result.run.steps:
                print(f"[{step.status.value.upper()}] {step.name}: {step.message}")
            print(f"Pipeline report: {result.markdown_path}")
            return 0 if result.run.status == "success" else 1
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "observe":
        try:
            result = build_observation_service(config).run(args.date)
            observation = result.observation
            shadow = observation.shadow_portfolio
            print(
                f"Candidate changes: +{len(observation.entered_candidates)} "
                f"-{len(observation.exited_candidates)}; "
                f"watchlist: {len(observation.watchlist)}; "
                f"shadow equity: {shadow.equity:.2f}"
                if shadow is not None
                else (
                    f"Candidate changes: +{len(observation.entered_candidates)} "
                    f"-{len(observation.exited_candidates)}; "
                    f"watchlist: {len(observation.watchlist)}; shadow unavailable"
                )
            )
            print(f"Report: {result.markdown_path}")
            return 0
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command in {"research", "backtest"}:
        try:
            if (
                args.command == "research"
                and args.research_command == "build-signals"
            ):
                result = build_historical_signal_service(config).build(
                    args.protocol_id,
                    PartitionName(args.partition),
                    SignalFrequency(args.frequency),
                )
                print(
                    f"Built {len(result.signals)}/{result.requested_dates} "
                    f"{result.frequency.value} signals for "
                    f"{result.protocol_id}/{result.partition.value}."
                )
                return 0 if result.signals else 1
            if args.command == "research":
                updates = {}
                if args.horizon is not None:
                    updates["forward_horizon_days"] = args.horizon
                if args.quantiles is not None:
                    updates["quantiles"] = args.quantiles
                if updates:
                    config.research = type(config.research).model_validate(
                        {**config.research.model_dump(), **updates}
                    )
                result = build_research_service(config).research_factors(
                    args.start, args.end
                )
                analysis = result.analysis
                spread = (
                    analysis.top_bottom_spread
                    if analysis.top_bottom_spread is not None
                    else "N/A"
                )
                print(
                    f"Evaluated snapshots: {analysis.evaluated_snapshot_count}/"
                    f"{analysis.snapshot_count}; spread: {spread}"
                )
                succeeded = analysis.evaluated_snapshot_count > 0
            else:
                result = build_research_service(config).backtest_candidates(
                    args.start,
                    args.end,
                    args.split_date,
                    args.protocol_id,
                    PartitionName(args.partition) if args.partition else None,
                    args.reveal_holdout,
                )
                analysis = result.analysis
                print(
                    f"Rebalances: {analysis.rebalance_count}; "
                    f"final equity: {analysis.final_equity:.2f}; "
                    f"return: {analysis.portfolio.total_return:.2%}"
                )
                print(f"Data version: {analysis.data_version}")
                succeeded = analysis.rebalance_count > 0
            print(f"Report: {result.markdown_path}")
            return 0 if succeeded else 1
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if args.command == "analyze":
        try:
            if args.analyze_command == "market":
                result = build_market_service(config).run(args.date)
                analysis = result.analysis
                temperature = analysis.temperature if analysis.temperature is not None else "N/A"
                print(f"Market state: {analysis.state.value}; temperature: {temperature}")
                succeeded = analysis.temperature is not None
            elif args.analyze_command == "industry":
                result = build_industry_service(config).run(args.date)
                analysis = result.analysis
                print(
                    f"Industries: {len(analysis.industries)}; "
                    f"confidence: {analysis.data_confidence}"
                )
                succeeded = bool(analysis.industries)
            else:
                result = build_factor_service(config).run(
                    args.date,
                    signal_origin=args.origin,
                )
                analysis = result.analysis
                print(
                    f"Candidates: {len(analysis.candidates)}; "
                    f"eligible: {analysis.eligible_size}; "
                    f"confidence: {analysis.data_confidence}"
                )
                succeeded = bool(analysis.candidates)
            print(f"Report: {result.markdown_path}")
            return 0 if succeeded else 1
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    try:
        datasets = parse_datasets(getattr(args, "datasets", None), config.update.datasets)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    periods: tuple[str, ...] = ()
    if args.data_command == "financials":
        periods = tuple(value.strip() for value in args.periods.split(",") if value.strip())
        if not periods or any(len(value) != 8 or not value.isdigit() for value in periods):
            print("Periods must be comma-separated YYYYMMDD values.", file=sys.stderr)
            return 2

    try:
        if args.data_command == "validate":
            reports = build_validation_service(config).validate(args.date, datasets)
            _print_validation(reports)
            passed = all(report.passed for report in reports)
            if config.validation.fail_on_warning:
                passed = passed and all(not report.issues for report in reports)
            return 0 if passed else 1

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
        if args.data_command == "financials":
            result = service.update_financial_indicators(args.date, periods)
            _print_update(result)
            return 0 if result.succeeded else 1

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
