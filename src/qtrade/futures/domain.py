from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import polars as pl

from qtrade.domain import Severity, ValidationIssue


class FuturesDataset(StrEnum):
    CONTRACTS = "futures_contracts"
    CONTRACT_RULES = "futures_contract_rules"
    DAILY = "futures_daily"
    SETTLEMENTS = "futures_settlements"
    MAPPINGS = "futures_mappings"
    LIMITS = "futures_limits"
    CALENDAR = "futures_calendar"


DEFAULT_FUTURES_DATASETS = (
    FuturesDataset.CONTRACTS,
    FuturesDataset.CONTRACT_RULES,
    FuturesDataset.DAILY,
    FuturesDataset.SETTLEMENTS,
    FuturesDataset.MAPPINGS,
    FuturesDataset.LIMITS,
    FuturesDataset.CALENDAR,
)


@dataclass
class FuturesDataBatch:
    dataset: FuturesDataset
    provider: str
    as_of_date: date
    frame: pl.DataFrame
    fetched_at: datetime = field(default_factory=datetime.now)
    request: dict[str, Any] = field(default_factory=dict)


@dataclass
class FuturesValidationReport:
    dataset: FuturesDataset
    as_of_date: date
    row_count: int
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(item.severity == Severity.ERROR for item in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset.value,
            "as_of_date": self.as_of_date.isoformat(),
            "row_count": self.row_count,
            "passed": self.passed,
            "issues": [
                {
                    "severity": item.severity.value,
                    "code": item.code,
                    "message": item.message,
                    "rows": item.rows,
                }
                for item in self.issues
            ],
        }
