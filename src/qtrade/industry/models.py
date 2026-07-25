from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class IndustryState(StrEnum):
    TREND_STRENGTHENING = "trend_strengthening"
    STRONG_CONTINUATION = "strong_continuation"
    HIGH_LEVEL_DIVERGENCE = "high_level_divergence"
    WEAK_RECOVERY = "weak_recovery"
    WEAKENING = "weakening"
    NEUTRAL = "neutral"


class IndustryMetrics(BaseModel):
    name: str
    stock_count: int
    return_5d: float
    return_20d: float
    return_60d: float
    relative_return_20d: float
    relative_return_60d: float
    above_ma_60: float
    advance_ratio: float
    activity_ratio: float | None = None
    score: float = Field(ge=0, le=100)
    rank: int = Field(ge=1)
    state: IndustryState


class StyleMetrics(BaseModel):
    name: str
    numerator_code: str
    denominator_code: str
    relative_return_5d: float | None = None
    relative_return_20d: float | None = None
    relative_return_60d: float | None = None
    leader: str
    strength: float | None = None


class IndustryAnalysis(BaseModel):
    as_of_date: date
    created_at: datetime = Field(default_factory=datetime.now)
    classification: str
    classification_snapshot_date: date
    benchmark_code: str
    benchmark_return_20d: float
    benchmark_return_60d: float
    industries: list[IndustryMetrics]
    styles: list[StyleMetrics]
    data_confidence: str
    warnings: list[str] = Field(default_factory=list)
