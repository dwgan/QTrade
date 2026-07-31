from __future__ import annotations

from datetime import date

import pytest

from qtrade.futures.portfolio import (
    FuturesFill,
    FuturesOffset,
    FuturesPortfolioLedger,
    FuturesSettlementMark,
    FuturesSide,
)

CONTRACT = "CU2608.SHF"


def fill(
    trade_date: date,
    side: FuturesSide,
    offset: FuturesOffset,
    lots: int,
    price: float,
    *,
    fee: float = 0.0,
) -> FuturesFill:
    return FuturesFill(
        trade_date=trade_date,
        contract_code=CONTRACT,
        side=side,
        offset=offset,
        lots=lots,
        price=price,
        multiplier=10.0,
        fee=fee,
    )


def mark(price: float, margin_rate: float = 0.1) -> dict[str, FuturesSettlementMark]:
    return {
        CONTRACT: FuturesSettlementMark(
            contract_code=CONTRACT,
            settlement_price=price,
            margin_rate=margin_rate,
        )
    }


def test_long_position_cash_and_margin_reconcile_by_day() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    third = date(2026, 1, 7)
    ledger = FuturesPortfolioLedger(initial_equity=10_000)

    ledger.apply_fill(fill(first, FuturesSide.BUY, FuturesOffset.OPEN, 2, 100.0, fee=5.0))
    day_one = ledger.settle(first, mark(105.0))
    day_two = ledger.settle(second, mark(103.0))
    ledger.apply_fill(fill(third, FuturesSide.SELL, FuturesOffset.CLOSE, 1, 104.0, fee=3.0))
    day_three = ledger.settle(third, mark(102.0))

    assert day_one.daily_pnl == 100.0
    assert day_one.daily_fees == 5.0
    assert day_one.equity == 10_095.0
    assert day_one.margin == 210.0
    assert day_one.available_cash == 9_885.0
    assert day_one.stress_margin == 315.0
    assert day_two.daily_pnl == -40.0
    assert day_two.equity == 10_055.0
    assert day_three.daily_pnl == 0.0
    assert day_three.daily_fees == 3.0
    assert day_three.equity == 10_052.0
    assert day_three.margin == 102.0
    assert day_three.available_cash == 9_950.0
    assert ledger.positions[CONTRACT].lots == 1
    assert ledger.positions[CONTRACT].settlement_basis == 102.0
    assert ledger.equity == pytest.approx(
        ledger.initial_equity + sum(entry.cash_change for entry in ledger.entries)
    )


def test_short_position_profits_when_settlement_falls() -> None:
    trading_date = date(2026, 1, 5)
    ledger = FuturesPortfolioLedger(initial_equity=5_000)

    ledger.apply_fill(fill(trading_date, FuturesSide.SELL, FuturesOffset.OPEN, 3, 200.0, fee=2.0))
    snapshot = ledger.settle(trading_date, mark(190.0, 0.12))

    assert snapshot.daily_pnl == 300.0
    assert snapshot.equity == 5_298.0
    assert snapshot.margin == 684.0
    assert not snapshot.margin_call


def test_missing_margin_data_rejects_settlement_without_mutation() -> None:
    trading_date = date(2026, 1, 5)
    ledger = FuturesPortfolioLedger(initial_equity=5_000)
    ledger.apply_fill(fill(trading_date, FuturesSide.BUY, FuturesOffset.OPEN, 1, 100.0))
    equity_before = ledger.equity
    basis_before = ledger.positions[CONTRACT].settlement_basis

    with pytest.raises(ValueError, match="Missing settlement or margin data"):
        ledger.settle(trading_date, {})

    assert ledger.equity == equity_before
    assert ledger.positions[CONTRACT].settlement_basis == basis_before
    assert not ledger.snapshots


def test_over_close_and_opposite_open_are_rejected() -> None:
    trading_date = date(2026, 1, 5)
    ledger = FuturesPortfolioLedger(initial_equity=5_000)
    ledger.apply_fill(fill(trading_date, FuturesSide.BUY, FuturesOffset.OPEN, 1, 100.0))

    with pytest.raises(ValueError, match="more lots"):
        ledger.apply_fill(fill(trading_date, FuturesSide.SELL, FuturesOffset.CLOSE, 2, 101.0))
    with pytest.raises(ValueError, match="explicit close"):
        ledger.apply_fill(fill(trading_date, FuturesSide.SELL, FuturesOffset.OPEN, 1, 101.0))


def test_fills_and_settlements_cannot_rewrite_closed_days() -> None:
    first = date(2026, 1, 5)
    ledger = FuturesPortfolioLedger(initial_equity=5_000)
    ledger.apply_fill(fill(first, FuturesSide.BUY, FuturesOffset.OPEN, 1, 100.0))
    ledger.settle(first, mark(101.0))

    with pytest.raises(ValueError, match="last settlement date"):
        ledger.apply_fill(fill(first, FuturesSide.SELL, FuturesOffset.CLOSE, 1, 101.0))
    with pytest.raises(ValueError, match="strictly increasing"):
        ledger.settle(first, mark(101.0))


def test_non_finite_account_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="initial equity"):
        FuturesPortfolioLedger(initial_equity=float("nan"))
    with pytest.raises(ValueError, match="settlement price"):
        FuturesSettlementMark(
            contract_code=CONTRACT,
            settlement_price=float("inf"),
            margin_rate=0.10,
        )
