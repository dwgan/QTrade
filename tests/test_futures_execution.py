from __future__ import annotations

from datetime import date

import pytest

from qtrade.futures.execution import (
    FuturesDailyExecutionBar,
    FuturesDailyExecutionEngine,
    FuturesExecutionStatus,
    FuturesFeeRule,
    FuturesFeeSchedule,
    FuturesOrder,
    FuturesPendingOrderBook,
)
from qtrade.futures.portfolio import (
    FuturesOffset,
    FuturesPortfolioLedger,
    FuturesSettlementMark,
    FuturesSide,
)

SIGNAL_DATE = date(2026, 1, 5)
ELIGIBLE_DATE = date(2026, 1, 6)
CONTRACT = "CU2608.SHF"


def order(
    *,
    side: FuturesSide = FuturesSide.BUY,
    offset: FuturesOffset = FuturesOffset.OPEN,
    fee_rule: FuturesFeeRule | None = None,
) -> FuturesOrder:
    return FuturesOrder(
        order_id="order-1",
        signal_date=SIGNAL_DATE,
        eligible_date=ELIGIBLE_DATE,
        contract_code=CONTRACT,
        side=side,
        offset=offset,
        lots=2,
        multiplier=10.0,
        tick_size=0.5,
        fee_rule=fee_rule or FuturesFeeRule(),
    )


def bar(
    trade_date: date = ELIGIBLE_DATE,
    *,
    open_price: float | None = 100.0,
    high_price: float | None = 105.0,
    low_price: float | None = 95.0,
    volume: float | None = 1_000.0,
    up_limit: float | None = 110.0,
    down_limit: float | None = 90.0,
) -> FuturesDailyExecutionBar:
    return FuturesDailyExecutionBar(
        trade_date=trade_date,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        volume=volume,
        up_limit=up_limit,
        down_limit=down_limit,
    )


def test_order_waits_until_next_day_then_fills_with_adverse_slippage() -> None:
    engine = FuturesDailyExecutionEngine(slippage_ticks=2)
    target = order(fee_rule=FuturesFeeRule(per_lot=2.0, notional_rate=0.001))

    waiting = engine.attempt(
        target,
        SIGNAL_DATE,
        bar(SIGNAL_DATE),
        available_cash=10_000,
        margin_rate=0.10,
    )
    filled = engine.attempt(
        target,
        ELIGIBLE_DATE,
        bar(),
        available_cash=10_000,
        margin_rate=0.10,
    )

    assert waiting.status == FuturesExecutionStatus.WAITING
    assert waiting.pending
    assert filled.status == FuturesExecutionStatus.FILLED
    assert filled.fill is not None
    assert filled.fill.price == 101.0
    assert filled.fill.fee == pytest.approx(6.02)
    assert filled.required_margin == 202.0

    ledger = FuturesPortfolioLedger(initial_equity=10_000)
    ledger.apply_fill(filled.fill)
    snapshot = ledger.settle(
        ELIGIBLE_DATE,
        {
            CONTRACT: FuturesSettlementMark(
                contract_code=CONTRACT,
                settlement_price=102.0,
                margin_rate=0.10,
            )
        },
    )
    assert snapshot.equity == pytest.approx(10_013.98)


def test_missing_open_is_retained_and_retried_on_later_date() -> None:
    engine = FuturesDailyExecutionEngine()
    target = order()

    blocked = engine.attempt(
        target,
        ELIGIBLE_DATE,
        bar(open_price=None),
        available_cash=10_000,
        margin_rate=0.10,
    )
    retry_date = date(2026, 1, 7)
    filled = engine.attempt(
        target,
        retry_date,
        bar(retry_date),
        available_cash=10_000,
        margin_rate=0.10,
    )

    assert blocked.status == FuturesExecutionStatus.BLOCKED
    assert blocked.reason == "missing_open"
    assert blocked.pending
    assert filled.status == FuturesExecutionStatus.FILLED


def test_limit_lock_is_directional() -> None:
    engine = FuturesDailyExecutionEngine()
    locked_up = bar(open_price=110.0, high_price=110.0, low_price=110.0)

    buy = engine.attempt(
        order(side=FuturesSide.BUY),
        ELIGIBLE_DATE,
        locked_up,
        available_cash=10_000,
        margin_rate=0.10,
    )
    sell = engine.attempt(
        order(side=FuturesSide.SELL),
        ELIGIBLE_DATE,
        locked_up,
        available_cash=10_000,
        margin_rate=0.10,
    )

    assert buy.reason == "locked_limit_up"
    assert buy.pending
    assert sell.status == FuturesExecutionStatus.FILLED


def test_open_requires_margin_but_close_does_not_add_margin() -> None:
    engine = FuturesDailyExecutionEngine()

    opening = engine.attempt(
        order(),
        ELIGIBLE_DATE,
        bar(),
        available_cash=100.0,
        margin_rate=0.10,
    )
    closing = engine.attempt(
        order(side=FuturesSide.SELL, offset=FuturesOffset.CLOSE),
        ELIGIBLE_DATE,
        bar(),
        available_cash=0.0,
        margin_rate=None,
    )

    assert opening.reason == "insufficient_margin"
    assert opening.required_margin == 201.0
    assert closing.status == FuturesExecutionStatus.FILLED
    assert closing.required_margin == 0.0


def test_slippage_is_capped_at_the_daily_limit() -> None:
    result = FuturesDailyExecutionEngine(slippage_ticks=2).attempt(
        order(),
        ELIGIBLE_DATE,
        bar(open_price=109.5, high_price=110.0, low_price=100.0),
        available_cash=10_000,
        margin_rate=0.10,
    )

    assert result.fill is not None
    assert result.fill.price == 110.0


def test_missing_or_invalid_intraday_range_blocks_execution() -> None:
    engine = FuturesDailyExecutionEngine()

    missing = engine.attempt(
        order(),
        ELIGIBLE_DATE,
        bar(high_price=None),
        available_cash=10_000,
        margin_rate=0.10,
    )
    invalid = engine.attempt(
        order(),
        ELIGIBLE_DATE,
        bar(open_price=100.0, high_price=99.0, low_price=95.0),
        available_cash=10_000,
        margin_rate=0.10,
    )

    assert missing.reason == "missing_intraday_range"
    assert invalid.reason == "invalid_intraday_range"


def test_non_finite_money_and_market_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="fee rates"):
        FuturesFeeRule(notional_rate=float("nan"))

    result = FuturesDailyExecutionEngine().attempt(
        order(),
        ELIGIBLE_DATE,
        bar(),
        available_cash=float("nan"),
        margin_rate=0.10,
    )

    assert result.reason == "invalid_available_cash"


def test_pending_order_book_audits_retries_and_final_fill() -> None:
    order_book = FuturesPendingOrderBook(FuturesDailyExecutionEngine())
    target = order()
    order_book.submit(target)

    order_book.attempt(
        target.order_id,
        SIGNAL_DATE,
        bar(SIGNAL_DATE),
        available_cash=10_000,
        margin_rate=0.10,
    )
    order_book.attempt(
        target.order_id,
        ELIGIBLE_DATE,
        bar(volume=0),
        available_cash=10_000,
        margin_rate=0.10,
    )
    retry_date = date(2026, 1, 7)
    result = order_book.attempt(
        target.order_id,
        retry_date,
        bar(retry_date),
        available_cash=10_000,
        margin_rate=0.10,
    )

    state = order_book.state(target.order_id)
    assert result.status == FuturesExecutionStatus.FILLED
    assert state.attempt_count == 3
    assert state.first_attempt_date == SIGNAL_DATE
    assert state.last_attempt_date == retry_date
    assert state.last_reason == "filled"
    assert state.fill_date == retry_date
    assert not state.pending
    assert order_book.pending_states() == ()

    with pytest.raises(ValueError, match="already filled"):
        order_book.attempt(
            target.order_id,
            date(2026, 1, 8),
            bar(date(2026, 1, 8)),
            available_cash=10_000,
            margin_rate=0.10,
        )


def test_pending_order_book_rejects_duplicate_and_repeated_dates() -> None:
    order_book = FuturesPendingOrderBook()
    target = order()
    order_book.submit(target)

    with pytest.raises(ValueError, match="Duplicate"):
        order_book.submit(target)

    order_book.attempt(
        target.order_id,
        SIGNAL_DATE,
        None,
        available_cash=10_000,
        margin_rate=0.10,
    )
    with pytest.raises(ValueError, match="strictly increasing"):
        order_book.attempt(
            target.order_id,
            SIGNAL_DATE,
            None,
            available_cash=10_000,
            margin_rate=0.10,
        )


def test_fee_schedule_distinguishes_open_close_today_and_close_yesterday() -> None:
    schedule = FuturesFeeSchedule(
        open_rule=FuturesFeeRule(per_lot=1.0),
        close_rule=FuturesFeeRule(per_lot=2.0),
        close_today_rule=FuturesFeeRule(per_lot=8.0),
        close_yesterday_rule=FuturesFeeRule(per_lot=3.0),
    )

    assert schedule.rule_for(FuturesOffset.OPEN).calculate(100, 10, 2) == 2.0
    assert schedule.rule_for(FuturesOffset.CLOSE).calculate(100, 10, 2) == 4.0
    assert schedule.rule_for(FuturesOffset.CLOSE_TODAY).calculate(100, 10, 2) == 16.0
    assert schedule.rule_for(FuturesOffset.CLOSE_YESTERDAY).calculate(100, 10, 2) == 6.0


def test_ledger_accepts_explicit_close_today_offset() -> None:
    ledger = FuturesPortfolioLedger(initial_equity=10_000)
    open_result = FuturesDailyExecutionEngine().attempt(
        order(offset=FuturesOffset.OPEN),
        ELIGIBLE_DATE,
        bar(),
        available_cash=10_000,
        margin_rate=0.10,
    )
    assert open_result.fill is not None
    ledger.apply_fill(open_result.fill)
    close_order = order(side=FuturesSide.SELL, offset=FuturesOffset.CLOSE_TODAY)
    result = FuturesDailyExecutionEngine().attempt(
        close_order,
        ELIGIBLE_DATE,
        bar(),
        available_cash=0.0,
        margin_rate=None,
    )

    assert result.fill is not None
    ledger.apply_fill(result.fill)
    assert CONTRACT not in ledger.positions
