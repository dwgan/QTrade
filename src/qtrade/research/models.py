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


class SamplePerformance(BaseModel):
    sample: str
    start_date: date
    end_date: date
    portfolio: PerformanceMetrics
    benchmark: PerformanceMetrics


class CostSensitivityMetric(BaseModel):
    transaction_cost_rate: float
    total_cost_rate: float
    total_return: float
    max_drawdown: float


class CandidateBacktestAnalysis(BaseModel):
    start_date: date
    end_date: date
    created_at: datetime = Field(default_factory=datetime.now)
    benchmark_code: str
    initial_capital: float
    final_equity: float
    execution_rule: str
    transaction_cost_rate: float
    slippage_rate: float
    rebalance_count: int
    average_turnover: float
    total_cost: float
    blocked_buy_orders: int
    blocked_sell_orders: int
    delayed_execution_days: int
    sample_split_date: date | None
    portfolio: PerformanceMetrics
    benchmark: PerformanceMetrics
    sample_performance: list[SamplePerformance] = Field(default_factory=list)
    cost_sensitivity: list[CostSensitivityMetric] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
