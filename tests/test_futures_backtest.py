from __future__ import annotations

from datetime import date

import pytest

from qtrade.futures.backtest import (
    FuturesDailyPortfolioEngine,
    FuturesDailyPortfolioInput,
    FuturesDirectionalMarginRates,
    FuturesLiquidationSpec,
)
from qtrade.futures.execution import (
    FuturesDailyExecutionBar,
    FuturesFeeSchedule,
    FuturesOrder,
)
from qtrade.futures.lifecycle import (
    FuturesPositionTarget,
    FuturesRollPlan,
)
from qtrade.futures.portfolio import (
    FuturesFill,
    FuturesOffset,
    FuturesSettlementMark,
    FuturesSide,
)


def bar(
    trade_date: date,
    price: float,
    *,
    locked_up: bool = False,
) -> FuturesDailyExecutionBar:
    if locked_up:
        return FuturesDailyExecutionBar(trade_date, price, price, price, 1_000, price, price * 0.8)
    return FuturesDailyExecutionBar(
        trade_date,
        price,
        price * 1.05,
        price * 0.95,
        1_000,
        price * 1.10,
        price * 0.90,
    )


def mark(code: str, price: float, margin_rate: float) -> FuturesSettlementMark:
    return FuturesSettlementMark(code, price, margin_rate)


def target(code: str, signed_lots: int, multiplier: float = 10.0) -> FuturesPositionTarget:
    return FuturesPositionTarget(
        contract_code=code,
        signed_lots=signed_lots,
        multiplier=multiplier,
        tick_size=1.0,
        fee_schedule=FuturesFeeSchedule(),
    )


def rates(
    long_rate: float = 0.10,
    short_rate: float | None = None,
) -> FuturesDirectionalMarginRates:
    return FuturesDirectionalMarginRates(long_rate, short_rate or long_rate)


def test_margin_call_liquidation_preempts_blocked_normal_order() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    third = date(2026, 1, 7)
    fourth = date(2026, 1, 8)
    fifth = date(2026, 1, 9)
    contract_a = "A2609.DCE"
    contract_b = "B2609.DCE"
    engine = FuturesDailyPortfolioEngine(
        initial_equity=3_000,
        slippage_ticks=0,
        margin_call_buffer=1.0,
    )

    day_one = engine.run_day(
        FuturesDailyPortfolioInput(
            trade_date=first,
            next_trade_date=second,
            bars={},
            settlement_marks={},
            margin_rates={contract_a: rates(0.5), contract_b: rates(0.1)},
            targets=(target(contract_a, 5), target(contract_b, 1)),
            rebalance_id="rebalance-1",
        )
    )
    assert day_one.executions == ()
    assert len(day_one.generated_target_orders) == 2

    day_two = engine.run_day(
        FuturesDailyPortfolioInput(
            trade_date=second,
            next_trade_date=third,
            bars={
                contract_a: bar(second, 100),
                contract_b: bar(second, 110, locked_up=True),
            },
            settlement_marks={
                contract_a: mark(contract_a, 70, 0.5),
                contract_b: mark(contract_b, 110, 0.1),
            },
            margin_rates={contract_a: rates(0.5), contract_b: rates(0.1)},
            targets=(target(contract_b, 2),),
            rebalance_id="suppressed-rebalance",
            liquidation_priority=(
                FuturesLiquidationSpec(
                    contract_a,
                    multiplier=10.0,
                    tick_size=1.0,
                    fee_schedule=FuturesFeeSchedule(),
                ),
            ),
        )
    )
    assert [item.reason for item in day_two.executions] == ["filled", "locked_limit_up"]
    assert day_two.snapshot.margin_call
    assert day_two.signals_suppressed
    assert day_two.generated_target_orders == ()
    assert [item.lots for item in day_two.generated_liquidation_orders] == [1]
    assert day_two.pending_normal_batches == 1

    day_three = engine.run_day(
        FuturesDailyPortfolioInput(
            trade_date=third,
            next_trade_date=fourth,
            bars={contract_a: bar(third, 70), contract_b: bar(third, 100)},
            settlement_marks={contract_a: mark(contract_a, 70, 0.5)},
            margin_rates={contract_a: rates(0.5), contract_b: rates(0.1)},
            liquidation_priority=(
                FuturesLiquidationSpec(
                    contract_a,
                    multiplier=10.0,
                    tick_size=1.0,
                    fee_schedule=FuturesFeeSchedule(),
                ),
            ),
        )
    )
    assert day_three.liquidation_priority_active
    assert len(day_three.executions) == 1
    assert day_three.executions[0].order_id.startswith("margin-call-")
    assert not day_three.snapshot.margin_call
    assert engine.ledger.positions[contract_a].lots == 4
    assert contract_b not in engine.ledger.positions
    assert day_three.pending_normal_batches == 1

    day_four = engine.run_day(
        FuturesDailyPortfolioInput(
            trade_date=fourth,
            next_trade_date=fifth,
            bars={contract_a: bar(fourth, 70), contract_b: bar(fourth, 100)},
            settlement_marks={
                contract_a: mark(contract_a, 70, 0.5),
                contract_b: mark(contract_b, 100, 0.1),
            },
            margin_rates={contract_a: rates(0.5), contract_b: rates(0.1)},
        )
    )
    assert len(day_four.executions) == 1
    assert day_four.executions[0].order_id.startswith("rebalance-1")
    assert engine.ledger.positions[contract_b].lots == 1


def roll_plan(
    roll_id: str,
    signal_date: date,
    eligible_date: date,
    old_code: str,
    new_code: str,
    side: FuturesSide,
) -> FuturesRollPlan:
    close_side = FuturesSide.SELL if side == FuturesSide.BUY else FuturesSide.BUY
    return FuturesRollPlan(
        roll_id=roll_id,
        close_order=FuturesOrder(
            f"{roll_id}:close",
            signal_date,
            eligible_date,
            old_code,
            close_side,
            FuturesOffset.CLOSE_YESTERDAY,
            1,
            10.0,
            1.0,
        ),
        open_order=FuturesOrder(
            f"{roll_id}:open",
            signal_date,
            eligible_date,
            new_code,
            side,
            FuturesOffset.OPEN,
            1,
            10.0,
            1.0,
        ),
    )


def test_two_products_complete_three_point_in_time_rolls() -> None:
    first = date(2026, 2, 2)
    second = date(2026, 2, 3)
    third = date(2026, 2, 4)
    fourth = date(2026, 2, 5)
    a1, a2, a3 = "A2605.DCE", "A2609.DCE", "A2701.DCE"
    b1, b2 = "B2605.DCE", "B2609.DCE"
    all_codes = (a1, a2, a3, b1, b2)
    engine = FuturesDailyPortfolioEngine(100_000, slippage_ticks=0)
    engine.ledger.apply_fill(
        FuturesFill(date(2026, 1, 30), a1, FuturesSide.BUY, FuturesOffset.OPEN, 1, 100, 10)
    )
    engine.ledger.apply_fill(
        FuturesFill(date(2026, 1, 30), b1, FuturesSide.SELL, FuturesOffset.OPEN, 1, 100, 10)
    )

    result_one = engine.run_day(
        FuturesDailyPortfolioInput(
            first,
            second,
            bars={a1: bar(first, 100), b1: bar(first, 100)},
            settlement_marks={a1: mark(a1, 100, 0.1), b1: mark(b1, 100, 0.1)},
            margin_rates={code: rates() for code in all_codes},
            roll_plans=(
                roll_plan("roll-a-1", first, second, a1, a2, FuturesSide.BUY),
                roll_plan("roll-b-1", first, second, b1, b2, FuturesSide.SELL),
            ),
        )
    )
    assert result_one.pending_rolls == 2

    result_two = engine.run_day(
        FuturesDailyPortfolioInput(
            second,
            third,
            bars={code: bar(second, 100) for code in (a1, a2, b1, b2)},
            settlement_marks={
                a1: mark(a1, 100, 0.1),
                a2: mark(a2, 100, 0.1),
                b1: mark(b1, 100, 0.1),
                b2: mark(b2, 100, 0.1),
            },
            margin_rates={code: rates() for code in all_codes},
            roll_plans=(roll_plan("roll-a-2", second, third, a2, a3, FuturesSide.BUY),),
        )
    )
    assert len(result_two.roll_attempts) == 2
    assert result_two.pending_rolls == 1
    assert set(engine.ledger.positions) == {a2, b2}

    result_three = engine.run_day(
        FuturesDailyPortfolioInput(
            third,
            fourth,
            bars={code: bar(third, 100) for code in (a2, a3, b2)},
            settlement_marks={
                a2: mark(a2, 100, 0.1),
                a3: mark(a3, 100, 0.1),
                b2: mark(b2, 100, 0.1),
            },
            margin_rates={code: rates() for code in all_codes},
        )
    )
    assert len(result_three.roll_attempts) == 1
    assert result_three.pending_rolls == 0
    assert engine.ledger.positions[a3].signed_lots == 1
    assert engine.ledger.positions[b2].signed_lots == -1
    assert a1 not in engine.ledger.positions
    assert a2 not in engine.ledger.positions
    assert b1 not in engine.ledger.positions


def test_missing_settlement_mark_rejects_day_before_order_attempt() -> None:
    first = date(2026, 3, 2)
    second = date(2026, 3, 3)
    third = date(2026, 3, 4)
    code = "A2609.DCE"
    engine = FuturesDailyPortfolioEngine(10_000, slippage_ticks=0)
    engine.run_day(
        FuturesDailyPortfolioInput(
            first,
            second,
            bars={},
            settlement_marks={},
            margin_rates={code: rates()},
            targets=(target(code, 1),),
            rebalance_id="missing-mark-test",
        )
    )

    with pytest.raises(ValueError, match="Missing daily settlement marks"):
        engine.run_day(
            FuturesDailyPortfolioInput(
                second,
                third,
                bars={code: bar(second, 100)},
                settlement_marks={},
                margin_rates={code: rates()},
            )
        )

    assert engine.ledger.positions == {}


def test_same_day_target_and_roll_contract_conflict_is_rejected_before_settlement() -> None:
    first = date(2026, 4, 1)
    second = date(2026, 4, 2)
    old_code = "A2605.DCE"
    new_code = "A2609.DCE"
    engine = FuturesDailyPortfolioEngine(10_000)
    engine.ledger.apply_fill(
        FuturesFill(
            date(2026, 3, 31),
            old_code,
            FuturesSide.BUY,
            FuturesOffset.OPEN,
            1,
            100,
            10,
        )
    )

    with pytest.raises(ValueError, match="cannot share contracts"):
        engine.run_day(
            FuturesDailyPortfolioInput(
                first,
                second,
                bars={old_code: bar(first, 100)},
                settlement_marks={old_code: mark(old_code, 100, 0.1)},
                margin_rates={old_code: rates(), new_code: rates()},
                targets=(target(old_code, 0),),
                roll_plans=(
                    roll_plan(
                        "conflicting-roll",
                        first,
                        second,
                        old_code,
                        new_code,
                        FuturesSide.BUY,
                    ),
                ),
                rebalance_id="conflicting-target",
            )
        )

    assert engine.ledger.snapshots == []


def test_target_can_be_frozen_for_an_extra_execution_delay_day() -> None:
    first = date(2026, 5, 6)
    second = date(2026, 5, 7)
    third = date(2026, 5, 8)
    fourth = date(2026, 5, 11)
    code = "A2609.DCE"
    engine = FuturesDailyPortfolioEngine(10_000, slippage_ticks=0)

    submitted = engine.run_day(
        FuturesDailyPortfolioInput(
            first,
            second,
            bars={},
            settlement_marks={},
            margin_rates={code: rates()},
            targets=(target(code, 1),),
            rebalance_id="delayed-target",
            target_eligible_date=third,
        )
    )
    waiting = engine.run_day(
        FuturesDailyPortfolioInput(
            second,
            third,
            bars={code: bar(second, 100)},
            settlement_marks={code: mark(code, 100, 0.1)},
            margin_rates={code: rates()},
        )
    )
    filled = engine.run_day(
        FuturesDailyPortfolioInput(
            third,
            fourth,
            bars={code: bar(third, 101)},
            settlement_marks={code: mark(code, 101, 0.1)},
            margin_rates={code: rates()},
        )
    )

    assert submitted.generated_target_orders[0].eligible_date == third
    assert waiting.executions[0].reason == "not_eligible"
    assert filled.executions[0].fill is not None
    assert engine.ledger.positions[code].lots == 1
