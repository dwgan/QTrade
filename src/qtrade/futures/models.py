from __future__ import annotations

from datetime import UTC, date, datetime

from pydantic import BaseModel, Field


class FuturesQueryCheck(BaseModel):
    endpoint: str
    exchange: str | None = None
    passed: bool
    row_count: int = 0
    missing_columns: list[str] = Field(default_factory=list)
    error: str | None = None


class FuturesExchangeCoverage(BaseModel):
    exchange: str
    active_contracts: int
    listed_products: int
    daily_contracts: int
    daily_products: int
    settlement_contracts: int
    settlements_missing_margin: int
    limit_contracts: int
    contracts_missing_unit: int
    contracts_missing_trading_hours: int
    liquid_product_codes: list[str] = Field(default_factory=list)


class FuturesAuditReport(BaseModel):
    as_of_date: date
    provider: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ready_for_data_foundation: bool
    query_checks: list[FuturesQueryCheck]
    exchanges: list[FuturesExchangeCoverage]
    mapping_rows: int
    blockers: list[str] = Field(default_factory=list)
    backtest_blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    ready_for_backtest: bool = False
