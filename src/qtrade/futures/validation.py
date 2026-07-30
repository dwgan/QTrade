from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path

import polars as pl

from qtrade.domain import Severity, ValidationIssue
from qtrade.futures.domain import FuturesDataset, FuturesValidationReport
from qtrade.futures.schemas import futures_schema_for


class FuturesDataValidator:
    def validate(
        self,
        dataset: FuturesDataset,
        as_of_date: date,
        frame: pl.DataFrame,
    ) -> FuturesValidationReport:
        report = FuturesValidationReport(dataset, as_of_date, frame.height)
        schema = futures_schema_for(dataset)
        if frame.is_empty():
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "empty_dataset",
                    "Dataset contains no rows.",
                )
            )
            return report
        missing = [name for name in schema.required_columns if name not in frame.columns]
        if missing:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "missing_columns",
                    "Required columns are missing: " + ", ".join(missing),
                )
            )
            return report
        duplicate_rows = (
            frame.group_by(list(schema.primary_key))
            .len()
            .filter(pl.col("len") > 1)
            .select(pl.col("len").sum() - pl.len())
            .item()
        )
        if duplicate_rows:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "duplicate_primary_key",
                    "Primary key contains duplicate rows.",
                    rows=int(duplicate_rows),
                )
            )
        null_keys = frame.filter(
            pl.any_horizontal([pl.col(name).is_null() for name in schema.primary_key])
        ).height
        if null_keys:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "null_primary_key",
                    "Primary key contains null values.",
                    rows=null_keys,
                )
            )
        if schema.date_column:
            expected = as_of_date.strftime("%Y%m%d")
            unexpected = frame.filter(pl.col(schema.date_column).cast(pl.String) != expected).height
            if unexpected:
                report.issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        "unexpected_date",
                        f"Rows do not match requested date {expected}.",
                        rows=unexpected,
                    )
                )
        if dataset in {
            FuturesDataset.CONTRACTS,
            FuturesDataset.CONTRACT_RULES,
        }:
            self._validate_contract_rules(frame, report)
        elif dataset == FuturesDataset.DAILY:
            self._validate_daily(frame, report)
        elif dataset == FuturesDataset.SETTLEMENTS:
            self._validate_settlements(frame, report)
        elif dataset == FuturesDataset.LIMITS:
            self._validate_limits(frame, report)
        return report

    @staticmethod
    def _validate_daily(
        frame: pl.DataFrame,
        report: FuturesValidationReport,
    ) -> None:
        numeric = frame.with_columns(
            pl.col(name).cast(pl.Float64, strict=False).alias(name)
            for name in (
                "pre_settle",
                "open",
                "high",
                "low",
                "close",
                "settle",
                "vol",
                "amount",
                "oi",
            )
        )
        invalid = numeric.filter(
            (
                (pl.col("high") > 0)
                & (pl.col("low") > 0)
                & (
                    (pl.col("high") < pl.max_horizontal("open", "close", "low"))
                    | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
                )
            )
            | pl.col("pre_settle").is_null()
            | (pl.col("pre_settle") <= 0)
            | pl.col("settle").is_null()
            | (pl.col("settle") <= 0)
            | pl.col("vol").is_null()
            | (pl.col("vol") < 0)
            | pl.col("oi").is_null()
            | (pl.col("oi") < 0)
        ).height
        if invalid:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "invalid_market_data",
                    "Daily prices or activity are internally inconsistent.",
                    rows=invalid,
                )
            )
        missing_ohlc = numeric.filter(
            pl.col("open").is_null()
            | (pl.col("open") <= 0)
            | pl.col("high").is_null()
            | (pl.col("high") <= 0)
            | pl.col("low").is_null()
            | (pl.col("low") <= 0)
        ).height
        if missing_ohlc:
            report.issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    "missing_intraday_ohlc",
                    "Provider supplied settlement data without valid open/high/low; "
                    "these rows cannot be used for execution-price simulation.",
                    rows=missing_ohlc,
                )
            )

    @staticmethod
    def _validate_contract_rules(
        frame: pl.DataFrame,
        report: FuturesValidationReport,
    ) -> None:
        unit_missing = (
            frame.with_columns(
                pl.col("multiplier").cast(pl.Float64, strict=False).alias("_multiplier"),
                pl.col("per_unit").cast(pl.Float64, strict=False).alias("_per_unit"),
            )
            .filter(
                (pl.col("_multiplier").is_null() | (pl.col("_multiplier") <= 0))
                & (pl.col("_per_unit").is_null() | (pl.col("_per_unit") <= 0))
            )
            .height
        )
        if unit_missing:
            report.issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    "missing_contract_unit",
                    "Neither multiplier nor per-unit contract size is usable.",
                    rows=unit_missing,
                )
            )
        trading_hours_missing = frame.filter(
            pl.col("trade_time_desc").is_null()
            | (pl.col("trade_time_desc").cast(pl.String).str.strip_chars() == "")
        ).height
        if trading_hours_missing:
            report.issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    "missing_trading_hours",
                    "Contract trading hours are missing.",
                    rows=trading_hours_missing,
                )
            )

    @staticmethod
    def _validate_settlements(
        frame: pl.DataFrame,
        report: FuturesValidationReport,
    ) -> None:
        invalid = (
            frame.with_columns(
                pl.col("long_margin_rate").cast(pl.Float64, strict=False).alias("_long"),
                pl.col("short_margin_rate").cast(pl.Float64, strict=False).alias("_short"),
            )
            .filter(
                pl.col("_long").is_null()
                | pl.col("_short").is_null()
                | (pl.col("_long") <= 0)
                | (pl.col("_short") <= 0)
            )
            .height
        )
        if invalid:
            report.issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    "missing_margin_rate",
                    "Provider did not supply positive long and short margin rates; "
                    "these contracts cannot enter a margin-aware backtest.",
                    rows=invalid,
                )
            )

    @staticmethod
    def _validate_limits(
        frame: pl.DataFrame,
        report: FuturesValidationReport,
    ) -> None:
        invalid = (
            frame.with_columns(
                pl.col("up_limit").cast(pl.Float64, strict=False).alias("_up"),
                pl.col("down_limit").cast(pl.Float64, strict=False).alias("_down"),
            )
            .filter(
                pl.col("_up").is_null()
                | pl.col("_down").is_null()
                | (pl.col("_up") <= pl.col("_down"))
            )
            .height
        )
        if invalid:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "invalid_price_limit",
                    "Upper limit must be greater than lower limit.",
                    rows=invalid,
                )
            )


def write_futures_validation_reports(
    reports_root: Path,
    as_of_date: date,
    reports: list[FuturesValidationReport],
    unavailable: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    directory = Path(reports_root) / "futures" / "data-quality" / as_of_date.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    unavailable = unavailable or {}
    payload = {
        "as_of_date": as_of_date.isoformat(),
        "passed": all(item.passed for item in reports),
        "datasets": [item.to_dict() for item in reports],
        "unavailable_optional_datasets": unavailable,
    }
    json_path = directory / "report.json"
    markdown_path = directory / "report.md"
    _atomic_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
    lines = [
        f"# 期货数据质量报告：{as_of_date.isoformat()}",
        "",
        f"关键数据状态：{'通过' if payload['passed'] else '失败'}",
        "",
        "| 数据集 | 行数 | 状态 | 问题数 |",
        "| --- | ---: | --- | ---: |",
    ]
    lines.extend(
        f"| {item.dataset.value} | {item.row_count} | "
        f"{'通过' if item.passed else '失败'} | {len(item.issues)} |"
        for item in reports
    )
    lines.extend(["", "## 问题明细", ""])
    issues = [
        f"- **{issue.severity.value.upper()} / {report.dataset.value} / "
        f"{issue.code}**：{issue.message}"
        for report in reports
        for issue in report.issues
    ]
    lines.extend(issues or ["- 未发现关键数据问题。"])
    lines.extend(["", "## 暂不可用的可选数据集", ""])
    lines.extend(f"- **{name}**：{error}" for name, error in unavailable.items())
    if not unavailable:
        lines.append("- 无。")
    _atomic_text(markdown_path, "\n".join(lines) + "\n")
    return json_path, markdown_path


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(temporary, path)
