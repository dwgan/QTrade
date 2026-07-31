from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class FuturesPortfolioRiskLimits:
    annualization_days: int = 252
    portfolio_target_annual_volatility: float = 0.08
    sector_risk_budget_fraction: float = 0.30
    initial_margin_fraction: float = 0.25
    stress_margin_fraction: float = 0.50
    stress_margin_multiplier: float = 1.50

    def __post_init__(self) -> None:
        if self.annualization_days <= 0:
            raise ValueError("annualization_days must be positive.")
        for name in (
            "portfolio_target_annual_volatility",
            "sector_risk_budget_fraction",
            "initial_margin_fraction",
            "stress_margin_fraction",
        ):
            self._fraction(name)
        if not math.isfinite(self.stress_margin_multiplier) or self.stress_margin_multiplier < 1:
            raise ValueError("stress_margin_multiplier must be finite and at least one.")

    def _fraction(self, name: str) -> None:
        value = getattr(self, name)
        if not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"{name} must be finite and in (0, 1].")


@dataclass(frozen=True)
class FuturesPortfolioRiskCandidate:
    product_code: str
    sector: str
    signed_lots: int
    one_lot_daily_risk: float
    one_lot_initial_margin: float

    def __post_init__(self) -> None:
        if not self.product_code.strip() or not self.sector.strip():
            raise ValueError("Risk candidates require product_code and sector.")
        if isinstance(self.signed_lots, bool) or not isinstance(self.signed_lots, int):
            raise ValueError("signed_lots must be an integer.")
        for name in ("one_lot_daily_risk", "one_lot_initial_margin"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")


@dataclass(frozen=True)
class FuturesPortfolioRiskAllocation:
    product_code: str
    sector: str
    original_signed_lots: int
    signed_lots: int
    daily_risk: float
    initial_margin: float
    stress_margin: float
    limit_reasons: tuple[str, ...]


@dataclass(frozen=True)
class FuturesPortfolioRiskResult:
    portfolio_daily_risk_budget: float
    allocations: tuple[FuturesPortfolioRiskAllocation, ...]
    sector_daily_risk: dict[str, float]
    total_daily_risk: float
    initial_margin: float
    stress_margin: float


@dataclass
class _WorkingAllocation:
    candidate: FuturesPortfolioRiskCandidate
    lots: int
    limit_reasons: list[str]


class FuturesPortfolioRiskAllocator:
    def __init__(self, limits: FuturesPortfolioRiskLimits) -> None:
        self.limits = limits

    def allocate(
        self,
        equity: float,
        candidates: list[FuturesPortfolioRiskCandidate],
    ) -> FuturesPortfolioRiskResult:
        equity = self._positive(equity, "equity")
        product_codes = [item.product_code for item in candidates]
        if len(set(product_codes)) != len(product_codes):
            raise ValueError("Risk candidates must contain unique product codes.")
        working = [
            _WorkingAllocation(item, abs(item.signed_lots), [])
            for item in sorted(candidates, key=lambda value: value.product_code)
        ]
        daily_budget = (
            equity
            * self.limits.portfolio_target_annual_volatility
            / math.sqrt(self.limits.annualization_days)
        )
        sector_limit = daily_budget * self.limits.sector_risk_budget_fraction
        sectors = sorted({item.candidate.sector for item in working})
        for sector in sectors:
            members = [item for item in working if item.candidate.sector == sector]
            self._scale(
                members,
                sector_limit,
                lambda item: item.candidate.one_lot_daily_risk,
                "sector_limit",
            )
        self._scale(
            working,
            daily_budget,
            lambda item: item.candidate.one_lot_daily_risk,
            "portfolio_risk_limit",
        )
        self._scale(
            working,
            equity * self.limits.initial_margin_fraction,
            lambda item: item.candidate.one_lot_initial_margin,
            "initial_margin_limit",
        )
        self._scale(
            working,
            equity * self.limits.stress_margin_fraction,
            lambda item: (
                item.candidate.one_lot_initial_margin * self.limits.stress_margin_multiplier
            ),
            "stress_margin_limit",
        )
        allocations = tuple(self._finalize(item) for item in working)
        sector_daily_risk = {
            sector: sum(item.daily_risk for item in allocations if item.sector == sector)
            for sector in sectors
        }
        return FuturesPortfolioRiskResult(
            portfolio_daily_risk_budget=daily_budget,
            allocations=allocations,
            sector_daily_risk=sector_daily_risk,
            total_daily_risk=sum(item.daily_risk for item in allocations),
            initial_margin=sum(item.initial_margin for item in allocations),
            stress_margin=sum(item.stress_margin for item in allocations),
        )

    @staticmethod
    def _scale(
        allocations: list[_WorkingAllocation],
        limit: float,
        unit_value: Callable[[_WorkingAllocation], float],
        reason: str,
    ) -> None:
        values = {item.candidate.product_code: unit_value(item) for item in allocations}
        current = sum(item.lots * values[item.candidate.product_code] for item in allocations)
        if current <= limit:
            return
        ratio = limit / current
        original_lots = {item.candidate.product_code: item.lots for item in allocations}
        remainders: list[tuple[float, str, _WorkingAllocation]] = []
        for item in allocations:
            scaled = item.lots * ratio
            item.lots = math.floor(scaled)
            remainders.append((scaled - item.lots, item.candidate.product_code, item))
        used = sum(item.lots * values[item.candidate.product_code] for item in allocations)
        remaining = limit - used
        for _, _, item in sorted(remainders, key=lambda value: (-value[0], value[1])):
            code = item.candidate.product_code
            if item.lots < original_lots[code] and values[code] <= remaining + 1e-12:
                item.lots += 1
                remaining -= values[code]
        for item in allocations:
            if item.lots != original_lots[item.candidate.product_code]:
                item.limit_reasons.append(reason)

    def _finalize(self, item: _WorkingAllocation) -> FuturesPortfolioRiskAllocation:
        direction = (item.candidate.signed_lots > 0) - (item.candidate.signed_lots < 0)
        signed_lots = direction * item.lots
        initial_margin = item.lots * item.candidate.one_lot_initial_margin
        return FuturesPortfolioRiskAllocation(
            product_code=item.candidate.product_code,
            sector=item.candidate.sector,
            original_signed_lots=item.candidate.signed_lots,
            signed_lots=signed_lots,
            daily_risk=item.lots * item.candidate.one_lot_daily_risk,
            initial_margin=initial_margin,
            stress_margin=initial_margin * self.limits.stress_margin_multiplier,
            limit_reasons=tuple(item.limit_reasons),
        )

    @staticmethod
    def _positive(value: float, name: str) -> float:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be finite and positive.")
        return number
