from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MarketState(StrEnum):
    ATTACK = "attack"
    BALANCED = "balanced"
    DEFENSIVE = "defensive"
    HIGH_RISK = "high_risk"
    INSUFFICIENT_DATA = "insufficient_data"


class IndexMetrics(BaseModel):
    code: str
    close: float
    observations: int
    return_20d: float | None = None
    return_60d: float | None = None
    ma_20: float | None = None
    ma_60: float | None = None
    ma_120: float | None = None
    ma_200: float | None = None
    annualized_volatility_20d: float | None = None
    drawdown_120d: float | None = None
    trend_score: float | None = Field(default=None, ge=0, le=100)


class BreadthMetrics(BaseModel):
    eligible_stocks: int
    above_ma_20: float | None = None
    above_ma_60: float | None = None
    above_ma_120: float | None = None
    advance_ratio: float | None = None
    new_high_60_ratio: float | None = None
    new_low_60_ratio: float | None = None
    score: float | None = Field(default=None, ge=0, le=100)


class RiskMetrics(BaseModel):
    annualized_volatility_20d: float | None = None
    drawdown_120d: float | None = None
    volatility_health_score: float | None = Field(default=None, ge=0, le=100)
    drawdown_health_score: float | None = Field(default=None, ge=0, le=100)
    health_score: float | None = Field(default=None, ge=0, le=100)


class MarketAnalysis(BaseModel):
    as_of_date: date
    created_at: datetime = Field(default_factory=datetime.now)
    primary_index_code: str
    state: MarketState
    temperature: float | None = Field(default=None, ge=0, le=100)
    trend_score: float | None = Field(default=None, ge=0, le=100)
    breadth: BreadthMetrics
    risk: RiskMetrics
    indices: list[IndexMetrics]
    history_start_date: date | None = None
    history_end_date: date | None = None
    data_confidence: str
    warnings: list[str] = Field(default_factory=list)
