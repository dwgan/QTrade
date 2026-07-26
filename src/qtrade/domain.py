from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import polars as pl


class Dataset(StrEnum):
    TRADE_CALENDAR = "trade_calendar"
    SECURITY_MASTER = "security_master"
    SECURITY_NAMES = "security_names"
    DAILY_PRICES = "daily_prices"
    ADJUST_FACTORS = "adjust_factors"
    INDEX_DAILY = "index_daily"
    INDEX_MEMBERS = "index_members"
    INDUSTRY_MEMBERS = "industry_members"
    DAILY_BASIC = "daily_basic"
    STOCK_LIMIT = "stock_limit"
    FINANCIAL_INDICATORS = "financial_indicators"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class FetchRequest:
    as_of_date: date
    start_date: date | None = None
    end_date: date | None = None
    periods: tuple[str, ...] = ()


@dataclass
class DataBatch:
    dataset: Dataset
    provider: str
    as_of_date: date
    frame: pl.DataFrame
    fetched_at: datetime = field(default_factory=datetime.now)
    request: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    severity: Severity
    code: str
    message: str
    rows: int | None = None


@dataclass
class ValidationReport:
    dataset: Dataset
    as_of_date: date
    row_count: int
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(issue.severity == Severity.ERROR for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset.value,
            "as_of_date": self.as_of_date.isoformat(),
            "row_count": self.row_count,
            "passed": self.passed,
            "issues": [
                {
                    "severity": issue.severity.value,
                    "code": issue.code,
                    "message": issue.message,
                    "rows": issue.rows,
                }
                for issue in self.issues
            ],
        }
