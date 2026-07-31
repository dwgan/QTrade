from __future__ import annotations

import pytest

from qtrade.futures.sectors import futures_sector
from qtrade.futures.trend_risk import (
    FuturesPortfolioRiskAllocator,
    FuturesPortfolioRiskCandidate,
    FuturesPortfolioRiskLimits,
)


def candidate(
    product: str,
    sector: str,
    lots: int = 10,
    *,
    one_lot_risk: float = 2.0,
    one_lot_margin: float = 100.0,
) -> FuturesPortfolioRiskCandidate:
    return FuturesPortfolioRiskCandidate(
        product_code=product,
        sector=sector,
        signed_lots=lots,
        one_lot_daily_risk=one_lot_risk,
        one_lot_initial_margin=one_lot_margin,
    )


def test_sector_limit_scales_absolute_risk_in_whole_lots() -> None:
    allocator = FuturesPortfolioRiskAllocator(FuturesPortfolioRiskLimits())

    result = allocator.allocate(
        equity=10_000,
        candidates=[candidate("CU", "metals"), candidate("AL", "metals")],
    )

    sector_limit = result.portfolio_daily_risk_budget * 0.30
    assert result.sector_daily_risk["metals"] <= sector_limit
    assert sum(abs(item.signed_lots) for item in result.allocations) == 7
    assert all("sector_limit" in item.limit_reasons for item in result.allocations)


def test_initial_margin_limit_reduces_portfolio_deterministically() -> None:
    allocator = FuturesPortfolioRiskAllocator(FuturesPortfolioRiskLimits())
    candidates = [
        candidate("CU", "metals", one_lot_risk=1.0, one_lot_margin=200.0),
        candidate("A", "agriculture", one_lot_risk=1.0, one_lot_margin=200.0),
    ]

    result = allocator.allocate(equity=10_000, candidates=candidates)

    assert result.initial_margin <= 2_500
    assert result.stress_margin <= 5_000
    assert [item.signed_lots for item in result.allocations] == [6, 6]
    assert all("initial_margin_limit" in item.limit_reasons for item in result.allocations)


def test_short_direction_is_preserved_under_stress_margin_limit() -> None:
    limits = FuturesPortfolioRiskLimits(
        initial_margin_fraction=1.0,
        stress_margin_fraction=0.10,
        stress_margin_multiplier=1.5,
    )
    allocator = FuturesPortfolioRiskAllocator(limits)

    result = allocator.allocate(
        equity=10_000,
        candidates=[
            candidate(
                "CU",
                "metals",
                lots=-10,
                one_lot_risk=1.0,
                one_lot_margin=200.0,
            )
        ],
    )

    allocation = result.allocations[0]
    assert allocation.signed_lots == -3
    assert allocation.limit_reasons == ("stress_margin_limit",)
    assert result.stress_margin == 900.0


def test_total_absolute_risk_cannot_exceed_portfolio_budget() -> None:
    allocator = FuturesPortfolioRiskAllocator(FuturesPortfolioRiskLimits())
    candidates = [
        candidate(
            f"P{index:02d}",
            f"sector-{index}",
            lots=10,
            one_lot_risk=1.0,
            one_lot_margin=1.0,
        )
        for index in range(6)
    ]

    result = allocator.allocate(equity=10_000, candidates=candidates)

    assert result.total_daily_risk <= result.portfolio_daily_risk_budget
    assert any("portfolio_risk_limit" in item.limit_reasons for item in result.allocations)


def test_unknown_product_cannot_bypass_frozen_sector_limits() -> None:
    with pytest.raises(ValueError, match="No frozen futures sector mapping"):
        futures_sector("UNKNOWN")
