from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class CandidateChange(BaseModel):
    ts_code: str
    name: str
    industry: str
    change_type: str
    previous_rank: int | None
    current_rank: int | None
    rank_change: int | None
    score: float | None


class WatchlistItem(BaseModel):
    ts_code: str
    name: str
    industry: str
    status: str
    current_rank: int | None
    previous_rank: int | None
    rank_change: int | None
    score: float | None


class ShadowPortfolioSummary(BaseModel):
    start_date: date
    end_date: date
    equity: float
    benchmark_equity: float
    total_return: float
    benchmark_return: float
    max_drawdown: float
    rebalance_count: int
    holdings: list[str]
    cash_weight: float
    last_execution_date: date | None


class DailyObservation(BaseModel):
    as_of_date: date
    created_at: datetime = Field(default_factory=datetime.now)
    current_snapshot_date: date
    previous_snapshot_date: date | None
    entered_candidates: list[CandidateChange]
    exited_candidates: list[CandidateChange]
    rank_movers: list[CandidateChange]
    watchlist: list[WatchlistItem]
    shadow_portfolio: ShadowPortfolioSummary | None
    warnings: list[str] = Field(default_factory=list)
