from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path

import polars as pl

from qtrade.config import ValidationConfig
from qtrade.data.schemas import schema_for
from qtrade.domain import Dataset, Severity, ValidationIssue, ValidationReport


class DataValidator:
    def __init__(self, config: ValidationConfig) -> None:
        self.config = config

    def validate(self, dataset: Dataset, as_of_date: date, frame: pl.DataFrame) -> ValidationReport:
        report = ValidationReport(
            dataset=dataset,
            as_of_date=as_of_date,
            row_count=frame.height,
        )
        schema = schema_for(dataset)

        if frame.is_empty():
            report.issues.append(
                ValidationIssue(Severity.ERROR, "empty_dataset", "Dataset contains no rows.")
            )
            return report

        missing = [column for column in schema.required_columns if column not in frame.columns]
        if missing:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "missing_columns",
                    f"Required columns are missing: {', '.join(missing)}",
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

        null_key_rows = frame.filter(
            pl.any_horizontal([pl.col(column).is_null() for column in schema.primary_key])
        ).height
        if null_key_rows:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "null_primary_key",
                    "Primary key contains null values.",
                    rows=null_key_rows,
                )
            )

        if dataset in {Dataset.DAILY_PRICES, Dataset.INDEX_DAILY}:
            self._validate_ohlc(frame, report)
            self._validate_trade_date(frame, as_of_date, report)
        elif dataset in {Dataset.DAILY_BASIC, Dataset.STOCK_LIMIT}:
            self._validate_trade_date(frame, as_of_date, report)
        elif dataset == Dataset.ADJUST_FACTORS:
            adjustment = pl.col("adj_factor").cast(pl.Float64, strict=False)
            invalid = frame.filter(adjustment.is_null() | (adjustment <= 0)).height
            if invalid:
                report.issues.append(
                    ValidationIssue(
                        Severity.ERROR,
                        "invalid_adjust_factor",
                        "Adjustment factor must be positive.",
                        rows=invalid,
                    )
                )
            self._validate_trade_date(frame, as_of_date, report)

        if dataset in {Dataset.DAILY_PRICES, Dataset.DAILY_BASIC} and frame.height < (
            self.config.minimum_daily_rows
        ):
            report.issues.append(
                ValidationIssue(
                    Severity.WARNING,
                    "low_daily_row_count",
                    (
                        f"Daily row count {frame.height} is below configured minimum "
                        f"{self.config.minimum_daily_rows}."
                    ),
                    rows=frame.height,
                )
            )

        if "available_from" in frame.columns:
            self._validate_available_from(frame, as_of_date, report)

        return report

    @staticmethod
    def _validate_available_from(
        frame: pl.DataFrame,
        as_of_date: date,
        report: ValidationReport,
    ) -> None:
        available = (
            pl.col("available_from")
            .cast(pl.String)
            .str.replace_all("-", "")
            .str.strptime(pl.Date, "%Y%m%d", strict=False)
        )
        invalid = frame.filter(available.is_null()).height
        if invalid:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "invalid_available_from",
                    "available_from must contain a valid date.",
                    rows=invalid,
                )
            )
        future = frame.filter(available > as_of_date).height
        if future:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "future_available_from",
                    "Rows were not available on the requested snapshot date.",
                    rows=future,
                )
            )

    @staticmethod
    def _validate_trade_date(
        frame: pl.DataFrame, as_of_date: date, report: ValidationReport
    ) -> None:
        expected = as_of_date.strftime("%Y%m%d")
        invalid = frame.filter(
            pl.col("trade_date").cast(pl.String, strict=False) != expected
        ).height
        if invalid:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "unexpected_trade_date",
                    f"Rows do not match requested trade date {expected}.",
                    rows=invalid,
                )
            )

    @staticmethod
    def _validate_ohlc(frame: pl.DataFrame, report: ValidationReport) -> None:
        numeric_columns = ("open", "high", "low", "close", "pre_close", "vol", "amount")
        numeric = frame.with_columns(
            [
                pl.col(column).cast(pl.Float64, strict=False).alias(column)
                for column in numeric_columns
            ]
        )
        invalid_numeric = numeric.filter(
            pl.any_horizontal([pl.col(column).is_null() for column in numeric_columns])
        ).height
        if invalid_numeric:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "invalid_numeric_value",
                    "Required price, volume or amount values are null or non-numeric.",
                    rows=invalid_numeric,
                )
            )

        invalid_price = numeric.filter(
            (pl.col("high") < pl.max_horizontal("open", "close", "low"))
            | (pl.col("low") > pl.min_horizontal("open", "close", "high"))
            | (pl.col("open") <= 0)
            | (pl.col("high") <= 0)
            | (pl.col("low") <= 0)
            | (pl.col("close") <= 0)
            | (pl.col("pre_close") <= 0)
        ).height
        if invalid_price:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "invalid_ohlc",
                    "OHLC prices are non-positive or internally inconsistent.",
                    rows=invalid_price,
                )
            )

        negative_activity = numeric.filter((pl.col("vol") < 0) | (pl.col("amount") < 0)).height
        if negative_activity:
            report.issues.append(
                ValidationIssue(
                    Severity.ERROR,
                    "negative_market_activity",
                    "Volume or amount is negative.",
                    rows=negative_activity,
                )
            )


def write_validation_reports(
    output_root: Path,
    as_of_date: date,
    reports: list[ValidationReport],
) -> tuple[Path, Path]:
    directory = Path(output_root) / "data-quality" / as_of_date.isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "report.json"
    markdown_path = directory / "report.md"

    payload = {
        "as_of_date": as_of_date.isoformat(),
        "passed": all(report.passed for report in reports),
        "datasets": [report.to_dict() for report in reports],
    }
    _atomic_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))

    lines = [
        f"# 数据质量报告：{as_of_date.isoformat()}",
        "",
        f"总体状态：{'通过' if payload['passed'] else '失败'}",
        "",
        "| 数据集 | 行数 | 状态 | 问题数 |",
        "| --- | ---: | --- | ---: |",
    ]
    for report in reports:
        lines.append(
            f"| {report.dataset.value} | {report.row_count} | "
            f"{'通过' if report.passed else '失败'} | {len(report.issues)} |"
        )
    lines.extend(["", "## 问题明细", ""])
    issues_found = False
    for report in reports:
        for issue in report.issues:
            issues_found = True
            rows = f"，影响行数：{issue.rows}" if issue.rows is not None else ""
            lines.append(
                f"- **{issue.severity.value.upper()} / {report.dataset.value} / "
                f"{issue.code}**：{issue.message}{rows}"
            )
    if not issues_found:
        lines.append("- 未发现问题。")
    lines.append("")
    _atomic_text(markdown_path, "\n".join(lines))
    return json_path, markdown_path


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(temporary, path)
