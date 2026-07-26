from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class FactorMetric(BaseModel):
    factor: str
    observations: int
    ic_mean: float | None
    ic_median: float | None
    ic_std: float | None
    ic_positive_ratio: float | None
    icir: float | None


class QuantileMetric(BaseModel):
    quantile: int
    observations: int
    mean_forward_return: float | None


class FactorResearchAnalysis(BaseModel):
    start_date: date
    end_date: date
    created_at: datetime = Field(default_factory=datetime.now)
    forward_horizon_days: int
    requested_quantiles: int
    snapshot_count: int
    evaluated_snapshot_count: int
    factor_metrics: list[FactorMetric]
    quantile_metrics: list[QuantileMetric]
    top_bottom_spread: float | None
    quantile_monotonic: bool | None
    warnings: list[str] = Field(default_factory=list)


class PerformanceMetrics(BaseModel):
    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float | None
    max_drawdown: float
    calmar_ratio: float | None


class CandidateBacktestAnalysis(BaseModel):
    start_date: date
    end_date: date
    created_at: datetime = Field(default_factory=datetime.now)
    benchmark_code: str
    initial_capital: float
    final_equity: float
    execution_rule: str
    transaction_cost_rate: float
    rebalance_count: int
    average_turnover: float
    total_cost: float
    portfolio: PerformanceMetrics
    benchmark: PerformanceMetrics
    warnings: list[str] = Field(default_factory=list)
