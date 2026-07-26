from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class CandidateStock(BaseModel):
    ts_code: str
    name: str
    industry: str
    close: float
    score: float = Field(ge=0, le=100)
    rank: int = Field(ge=1)
    quality_score: float = Field(ge=0, le=100)
    value_score: float = Field(ge=0, le=100)
    momentum_score: float = Field(ge=0, le=100)
    low_risk_score: float = Field(ge=0, le=100)
    financial_ann_date: date
    financial_period: date
    reasons: list[str]
    risk_flags: list[str] = Field(default_factory=list)


class FactorAnalysis(BaseModel):
    as_of_date: date
    created_at: datetime = Field(default_factory=datetime.now)
    daily_basic_snapshot_date: date
    financial_snapshot_date: date
    security_master_snapshot_date: date
    security_names_snapshot_date: date | None = None
    index_members_snapshot_date: date | None = None
    universe_index_codes: list[str] = Field(default_factory=list)
    index_membership_dates: dict[str, date] = Field(default_factory=dict)
    universe_size: int
    eligible_size: int
    ranked_size: int
    exclusion_counts: dict[str, int]
    candidates: list[CandidateStock]
    data_confidence: str
    warnings: list[str] = Field(default_factory=list)
