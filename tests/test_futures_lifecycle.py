from __future__ import annotations

from datetime import date

import pytest

from qtrade.futures.execution import (
    FuturesDailyExecutionBar,
    FuturesDailyExecutionEngine,
    FuturesFeeRule,
    FuturesFeeSchedule,
    FuturesOrder,
)
from qtrade.futures.lifecycle import (
    FuturesLiquidationCandidate,
    FuturesLiquidationPlanner,
    FuturesPositionTarget,
    FuturesRollCoordinator,
    FuturesRollPlan,
    FuturesRollStatus,
    FuturesTargetOrderPlanner,
)
from qtrade.futures.portfolio import (
    FuturesFill,
    FuturesOffset,
    FuturesPortfolioLedger,
    FuturesSide,
)

SIGNAL_DATE = date(2026, 1, 5)
ELIGIBLE_DATE = date(2026, 1, 6)
OLD_CONTRACT = "CU2608.SHF"
NEW_CONTRACT = "CU2609.SHF"


def execution_bar(
    contract_date: date = ELIGIBLE_DATE,
    *,
    open_price: float = 100.0,
    high_price: float = 105.0,
    low_price: float = 95.0,
    up_limit: float = 110.0,
    down_limit: float = 90.0,
) -> FuturesDailyExecutionBar:
    return FuturesDailyExecutionBar(
        trade_date=contract_date,
        open_price=open_price,
        high_price=high_price,
        low_price=low_price,
        volume=1_000.0,
        up_limit=up_limit,
        down_limit=down_limit,
    )


def roll_plan() -> FuturesRollPlan:
    close_fee = FuturesFeeRule(per_lot=2.0)
    open_fee = FuturesFeeRule(per_lot=1.0)
    return FuturesRollPlan(
        roll_id="roll-cu-1",
        close_order=FuturesOrder(
            order_id="roll-cu-1:close",
            signal_date=SIGNAL_DATE,
            eligible_date=ELIGIBLE_DATE,
            contract_code=OLD_CONTRACT,
            side=FuturesSide.SELL,
            offset=FuturesOffset.CLOSE_YESTERDAY,
            lots=2,
            multiplier=10.0,
            tick_size=0.5,
            fee_rule=close_fee,
        ),
        open_order=FuturesOrder(
            order_id="roll-cu-1:open",
            signal_date=SIGNAL_DATE,
            eligible_date=ELIGIBLE_DATE,
            contract_code=NEW_CONTRACT,
            side=FuturesSide.BUY,
            offset=FuturesOffset.OPEN,
            lots=2,
            multiplier=10.0,
            tick_size=0.5,
            fee_rule=open_fee,
        ),
    )


def ledger_with_old_position() -> FuturesPortfolioLedger:
    ledger = FuturesPortfolioLedger(initial_equity=100_000)
    ledger.apply_fill(
        FuturesFill(
            trade_date=SIGNAL_DATE,
            contract_code=OLD_CONTRACT,
            side=FuturesSide.BUY,
            offset=FuturesOffset.OPEN,
            lots=2,
            price=98.0,
            multiplier=10.0,
        )
    )
    return ledger


def test_roll_does_not_attempt_open_leg_when_close_is_locked() -> None:
    ledger = ledger_with_old_position()
    coordinator = FuturesRollCoordinator(
        roll_plan(),
        ledger,
        FuturesDailyExecutionEngine(slippage_ticks=1),
    )
    locked_down = execution_bar(open_price=90.0, high_price=90.0, low_price=90.0)

    result = coordinator.attempt(
        ELIGIBLE_DATE,
        {OLD_CONTRACT: locked_down, NEW_CONTRACT: execution_bar()},
        available_cash=90_000,
        open_available_cash=90_000,
        open_margin_rate=0.10,
    )

    assert result.status == FuturesRollStatus.CLOSE_PENDING
    assert result.close_result is not None
    assert result.close_result.reason == "locked_limit_down"
    assert result.open_result is None
    assert coordinator.order_book.state("roll-cu-1:open").attempt_count == 0
    assert ledger.positions[OLD_CONTRACT].lots == 2
    assert NEW_CONTRACT not in ledger.positions


def test_roll_closes_first_then_retries_only_blocked_open_leg() -> None:
    ledger = ledger_with_old_position()
    coordinator = FuturesRollCoordinator(roll_plan(), ledger)
    locked_new = execution_bar(open_price=110.0, high_price=110.0, low_price=110.0)

    first = coordinator.attempt(
        ELIGIBLE_DATE,
        {OLD_CONTRACT: execution_bar(), NEW_CONTRACT: locked_new},
        available_cash=90_000,
        open_available_cash=90_000,
        open_margin_rate=0.10,
    )

    assert first.status == FuturesRollStatus.OPEN_PENDING
    assert first.close_result is not None and first.close_result.fill is not None
    assert first.open_result is not None
    assert first.open_result.reason == "locked_limit_up"
    assert OLD_CONTRACT not in ledger.positions
    assert NEW_CONTRACT not in ledger.positions

    retry_date = date(2026, 1, 7)
    second = coordinator.attempt(
        retry_date,
        {NEW_CONTRACT: execution_bar(retry_date, open_price=101.0)},
        available_cash=90_000,
        open_available_cash=90_000,
        open_margin_rate=0.10,
    )

    assert second.status == FuturesRollStatus.COMPLETED
    assert second.close_result is None
    assert second.open_result is not None and second.open_result.fill is not None
    assert ledger.positions[NEW_CONTRACT].lots == 2
    assert coordinator.order_book.state("roll-cu-1:close").attempt_count == 1
    assert coordinator.order_book.state("roll-cu-1:open").attempt_count == 2
    assert sum(entry.fee for entry in ledger.entries) == 6.0


def test_roll_rejects_close_leg_that_does_not_offset_position() -> None:
    plan = roll_plan()
    invalid = FuturesRollPlan(
        roll_id="bad-roll",
        close_order=FuturesOrder(
            **{
                **plan.close_order.__dict__,
                "order_id": "bad-roll:close",
                "side": FuturesSide.BUY,
            }
        ),
        open_order=FuturesOrder(
            **{
                **plan.open_order.__dict__,
                "order_id": "bad-roll:open",
                "side": FuturesSide.SELL,
            }
        ),
    )
    coordinator = FuturesRollCoordinator(invalid, ledger_with_old_position())

    with pytest.raises(ValueError, match="does not offset"):
        coordinator.attempt(
            ELIGIBLE_DATE,
            {OLD_CONTRACT: execution_bar(), NEW_CONTRACT: execution_bar()},
            available_cash=90_000,
            open_available_cash=90_000,
            open_margin_rate=0.10,
        )


def test_roll_rejects_invalid_cash_before_closing_old_position() -> None:
    ledger = ledger_with_old_position()
    coordinator = FuturesRollCoordinator(roll_plan(), ledger)

    with pytest.raises(ValueError, match="cash must be finite"):
        coordinator.attempt(
            ELIGIBLE_DATE,
            {OLD_CONTRACT: execution_bar(), NEW_CONTRACT: execution_bar()},
            available_cash=90_000,
            open_available_cash=float("nan"),
            open_margin_rate=0.10,
        )

    assert ledger.positions[OLD_CONTRACT].lots == 2
    assert coordinator.order_book.state("roll-cu-1:close").attempt_count == 0


def test_target_planner_closes_before_opening_a_reversed_position() -> None:
    ledger = ledger_with_old_position()
    schedule = FuturesFeeSchedule(
        open_rule=FuturesFeeRule(per_lot=1.0),
        close_yesterday_rule=FuturesFeeRule(per_lot=3.0),
    )

    orders = FuturesTargetOrderPlanner().plan(
        ledger,
        rebalance_id="rebalance-1",
        signal_date=SIGNAL_DATE,
        eligible_date=ELIGIBLE_DATE,
        targets=[
            FuturesPositionTarget(
                OLD_CONTRACT,
                signed_lots=-1,
                multiplier=10.0,
                tick_size=0.5,
                fee_schedule=schedule,
                close_offset=FuturesOffset.CLOSE_YESTERDAY,
            )
        ],
    )

    assert [(item.side, item.offset, item.lots) for item in orders] == [
        (FuturesSide.SELL, FuturesOffset.CLOSE_YESTERDAY, 2),
        (FuturesSide.SELL, FuturesOffset.OPEN, 1),
    ]
    assert orders[0].fee_rule.per_lot == 3.0
    assert orders[1].fee_rule.per_lot == 1.0


def test_target_planner_generates_only_the_position_delta() -> None:
    ledger = ledger_with_old_position()
    target = FuturesPositionTarget(
        OLD_CONTRACT,
        signed_lots=5,
        multiplier=10.0,
        tick_size=0.5,
        fee_schedule=FuturesFeeSchedule(),
    )

    orders = FuturesTargetOrderPlanner().plan(
        ledger,
        rebalance_id="rebalance-2",
        signal_date=SIGNAL_DATE,
        eligible_date=ELIGIBLE_DATE,
        targets=[target],
    )

    assert len(orders) == 1
    assert (orders[0].side, orders[0].offset, orders[0].lots) == (
        FuturesSide.BUY,
        FuturesOffset.OPEN,
        3,
    )


def test_liquidation_uses_preregistered_order_and_minimum_whole_lots() -> None:
    ledger = FuturesPortfolioLedger(initial_equity=100_000)
    ledger.apply_fill(
        FuturesFill(SIGNAL_DATE, "A2609.DCE", FuturesSide.BUY, FuturesOffset.OPEN, 2, 100, 10)
    )
    ledger.apply_fill(
        FuturesFill(SIGNAL_DATE, "B2609.DCE", FuturesSide.SELL, FuturesOffset.OPEN, 3, 80, 10)
    )
    schedule = FuturesFeeSchedule(
        close_rule=FuturesFeeRule(per_lot=2.0),
        close_today_rule=FuturesFeeRule(per_lot=8.0),
    )
    candidates = [
        FuturesLiquidationCandidate(
            "A2609.DCE", 100.0, 10.0, 1.0, schedule, FuturesOffset.CLOSE_TODAY
        ),
        FuturesLiquidationCandidate("B2609.DCE", 80.0, 10.0, 1.0, schedule),
    ]

    orders = FuturesLiquidationPlanner().plan(
        ledger,
        liquidation_id="margin-call-1",
        signal_date=SIGNAL_DATE,
        eligible_date=ELIGIBLE_DATE,
        required_margin_release=250.0,
        candidates=candidates,
    )

    assert [(item.contract_code, item.side, item.lots) for item in orders] == [
        ("A2609.DCE", FuturesSide.SELL, 2),
        ("B2609.DCE", FuturesSide.BUY, 1),
    ]
    assert orders[0].fee_rule.per_lot == 8.0
    assert orders[1].fee_rule.per_lot == 2.0


def test_liquidation_fails_when_positions_cannot_release_required_margin() -> None:
    ledger = ledger_with_old_position()
    candidate = FuturesLiquidationCandidate(
        OLD_CONTRACT,
        margin_per_lot=100.0,
        multiplier=10.0,
        tick_size=0.5,
        fee_schedule=FuturesFeeSchedule(),
    )

    with pytest.raises(ValueError, match="cannot release"):
        FuturesLiquidationPlanner().plan(
            ledger,
            liquidation_id="margin-call-2",
            signal_date=SIGNAL_DATE,
            eligible_date=ELIGIBLE_DATE,
            required_margin_release=300.0,
            candidates=[candidate],
        )
