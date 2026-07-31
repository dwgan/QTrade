from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from qtrade.futures.execution import (
    FuturesDailyExecutionBar,
    FuturesDailyExecutionEngine,
    FuturesExecutionResult,
    FuturesExecutionStatus,
    FuturesFeeSchedule,
    FuturesOrder,
    FuturesPendingOrderBook,
)
from qtrade.futures.portfolio import FuturesOffset, FuturesPortfolioLedger, FuturesSide


class FuturesRollStatus(StrEnum):
    CLOSE_PENDING = "close_pending"
    OPEN_PENDING = "open_pending"
    COMPLETED = "completed"


@dataclass(frozen=True)
class FuturesRollPlan:
    roll_id: str
    close_order: FuturesOrder
    open_order: FuturesOrder

    def __post_init__(self) -> None:
        if not self.roll_id.strip():
            raise ValueError("Futures roll ID is required.")
        if self.close_order.offset == FuturesOffset.OPEN:
            raise ValueError("Futures roll close leg must use a close offset.")
        if self.open_order.offset != FuturesOffset.OPEN:
            raise ValueError("Futures roll open leg must use the open offset.")
        if (
            self.close_order.contract_code.strip().upper()
            == self.open_order.contract_code.strip().upper()
        ):
            raise ValueError("Futures roll legs must use different contracts.")
        if self.close_order.side == self.open_order.side:
            raise ValueError("Futures roll legs must use opposite transaction sides.")
        if self.close_order.lots != self.open_order.lots:
            raise ValueError("Futures roll legs must use the same lot count.")
        if self.close_order.eligible_date != self.open_order.eligible_date:
            raise ValueError("Futures roll legs must share an eligible date.")
        if self.close_order.signal_date != self.open_order.signal_date:
            raise ValueError("Futures roll legs must share a signal date.")


@dataclass(frozen=True)
class FuturesRollAttempt:
    roll_id: str
    attempt_date: date
    status: FuturesRollStatus
    close_result: FuturesExecutionResult | None = None
    open_result: FuturesExecutionResult | None = None


class FuturesRollCoordinator:
    def __init__(
        self,
        plan: FuturesRollPlan,
        ledger: FuturesPortfolioLedger,
        engine: FuturesDailyExecutionEngine | None = None,
    ) -> None:
        self.plan = plan
        self.ledger = ledger
        self.order_book = FuturesPendingOrderBook(engine)
        self.order_book.submit(plan.close_order)
        self.order_book.submit(plan.open_order)
        self.status = FuturesRollStatus.CLOSE_PENDING

    def attempt(
        self,
        attempt_date: date,
        bars: dict[str, FuturesDailyExecutionBar],
        *,
        available_cash: float,
        open_available_cash: float,
        open_margin_rate: float | None,
    ) -> FuturesRollAttempt:
        if self.status == FuturesRollStatus.COMPLETED:
            raise ValueError(f"Futures roll is already completed: {self.plan.roll_id}")
        if not math.isfinite(available_cash) or not math.isfinite(open_available_cash):
            raise ValueError("Futures roll available cash must be finite.")

        close_result: FuturesExecutionResult | None = None
        if self.status == FuturesRollStatus.CLOSE_PENDING:
            self._require_close_position()
            self._require_open_compatible()
            close_order = self.plan.close_order
            close_result = self.order_book.attempt(
                close_order.order_id,
                attempt_date,
                bars.get(close_order.contract_code),
                available_cash=available_cash,
                margin_rate=None,
            )
            if close_result.status != FuturesExecutionStatus.FILLED:
                return FuturesRollAttempt(
                    self.plan.roll_id,
                    attempt_date,
                    self.status,
                    close_result=close_result,
                )
            if close_result.fill is None:
                raise RuntimeError("Filled futures close leg is missing its fill record.")
            self.ledger.apply_fill(close_result.fill)
            self.status = FuturesRollStatus.OPEN_PENDING

        open_order = self.plan.open_order
        open_result = self.order_book.attempt(
            open_order.order_id,
            attempt_date,
            bars.get(open_order.contract_code),
            available_cash=min(available_cash, open_available_cash),
            margin_rate=open_margin_rate,
        )
        if open_result.status == FuturesExecutionStatus.FILLED:
            if open_result.fill is None:
                raise RuntimeError("Filled futures open leg is missing its fill record.")
            self.ledger.apply_fill(open_result.fill)
            self.status = FuturesRollStatus.COMPLETED
        return FuturesRollAttempt(
            self.plan.roll_id,
            attempt_date,
            self.status,
            close_result=close_result,
            open_result=open_result,
        )

    def _require_close_position(self) -> None:
        order = self.plan.close_order
        code = order.contract_code.strip().upper()
        position = self.ledger.positions.get(code)
        if position is None or position.lots < order.lots:
            raise ValueError(f"Futures roll close position is unavailable: {code}")
        expected_side = FuturesSide.SELL if position.direction > 0 else FuturesSide.BUY
        if order.side != expected_side:
            raise ValueError("Futures roll close side does not offset the existing position.")
        if position.multiplier != order.multiplier:
            raise ValueError("Futures roll close multiplier does not match the position.")

    def _require_open_compatible(self) -> None:
        order = self.plan.open_order
        code = order.contract_code.strip().upper()
        position = self.ledger.positions.get(code)
        if position is None:
            return
        expected_direction = 1 if order.side == FuturesSide.BUY else -1
        if position.direction != expected_direction:
            raise ValueError("Futures roll open side conflicts with the destination position.")
        if position.multiplier != order.multiplier:
            raise ValueError("Futures roll open multiplier does not match the position.")


@dataclass(frozen=True)
class FuturesPositionTarget:
    contract_code: str
    signed_lots: int
    multiplier: float
    tick_size: float
    fee_schedule: FuturesFeeSchedule
    close_offset: FuturesOffset = FuturesOffset.CLOSE

    def __post_init__(self) -> None:
        if not self.contract_code.strip():
            raise ValueError("Futures target contract code is required.")
        values = (self.multiplier, self.tick_size)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Futures target multiplier and tick size must be positive.")
        if self.close_offset == FuturesOffset.OPEN:
            raise ValueError("Futures target close offset cannot be open.")


class FuturesTargetOrderPlanner:
    def plan(
        self,
        ledger: FuturesPortfolioLedger,
        *,
        rebalance_id: str,
        signal_date: date,
        eligible_date: date,
        targets: list[FuturesPositionTarget],
    ) -> tuple[FuturesOrder, ...]:
        if not rebalance_id.strip():
            raise ValueError("Futures rebalance ID is required.")
        if eligible_date <= signal_date:
            raise ValueError("Futures rebalance eligible date must follow its signal date.")

        orders: list[FuturesOrder] = []
        seen: set[str] = set()
        for target in targets:
            code = target.contract_code.strip().upper()
            if code in seen:
                raise ValueError(f"Duplicate futures position target: {code}")
            seen.add(code)
            position = ledger.positions.get(code)
            current = position.signed_lots if position is not None else 0
            if position is not None and position.multiplier != target.multiplier:
                raise ValueError(f"Futures target multiplier does not match position: {code}")
            self._append_orders(
                orders,
                rebalance_id,
                signal_date,
                eligible_date,
                target,
                current,
            )
        return tuple(orders)

    @staticmethod
    def _append_orders(
        orders: list[FuturesOrder],
        rebalance_id: str,
        signal_date: date,
        eligible_date: date,
        target: FuturesPositionTarget,
        current: int,
    ) -> None:
        desired = target.signed_lots
        if current == desired:
            return
        same_direction = current * desired > 0
        close_lots = 0
        open_lots = 0
        if current and (not same_direction or abs(desired) < abs(current)):
            close_lots = abs(current) if not same_direction else abs(current) - abs(desired)
        if desired and (not same_direction or abs(desired) > abs(current)):
            open_lots = abs(desired) if not same_direction else abs(desired) - abs(current)

        code = target.contract_code.strip().upper()
        if close_lots:
            close_side = FuturesSide.SELL if current > 0 else FuturesSide.BUY
            orders.append(
                FuturesTargetOrderPlanner._order(
                    orders,
                    rebalance_id,
                    signal_date,
                    eligible_date,
                    target,
                    code,
                    close_side,
                    target.close_offset,
                    close_lots,
                )
            )
        if open_lots:
            open_side = FuturesSide.BUY if desired > 0 else FuturesSide.SELL
            orders.append(
                FuturesTargetOrderPlanner._order(
                    orders,
                    rebalance_id,
                    signal_date,
                    eligible_date,
                    target,
                    code,
                    open_side,
                    FuturesOffset.OPEN,
                    open_lots,
                )
            )

    @staticmethod
    def _order(
        orders: list[FuturesOrder],
        rebalance_id: str,
        signal_date: date,
        eligible_date: date,
        target: FuturesPositionTarget,
        code: str,
        side: FuturesSide,
        offset: FuturesOffset,
        lots: int,
    ) -> FuturesOrder:
        return FuturesOrder(
            order_id=f"{rebalance_id}:{len(orders) + 1}:{code}:{offset.value}",
            signal_date=signal_date,
            eligible_date=eligible_date,
            contract_code=code,
            side=side,
            offset=offset,
            lots=lots,
            multiplier=target.multiplier,
            tick_size=target.tick_size,
            fee_rule=target.fee_schedule.rule_for(offset),
        )


@dataclass(frozen=True)
class FuturesLiquidationCandidate:
    contract_code: str
    margin_per_lot: float
    multiplier: float
    tick_size: float
    fee_schedule: FuturesFeeSchedule
    close_offset: FuturesOffset = FuturesOffset.CLOSE

    def __post_init__(self) -> None:
        if not self.contract_code.strip():
            raise ValueError("Liquidation contract code is required.")
        values = (self.margin_per_lot, self.multiplier, self.tick_size)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("Liquidation margin, multiplier, and tick size must be positive.")
        if self.close_offset == FuturesOffset.OPEN:
            raise ValueError("Liquidation candidate must use a close offset.")


class FuturesLiquidationPlanner:
    def plan(
        self,
        ledger: FuturesPortfolioLedger,
        *,
        liquidation_id: str,
        signal_date: date,
        eligible_date: date,
        required_margin_release: float,
        candidates: list[FuturesLiquidationCandidate],
    ) -> tuple[FuturesOrder, ...]:
        if not liquidation_id.strip():
            raise ValueError("Futures liquidation ID is required.")
        if eligible_date <= signal_date:
            raise ValueError("Liquidation eligible date must follow its signal date.")
        if not math.isfinite(required_margin_release) or required_margin_release <= 0:
            raise ValueError("Required margin release must be positive.")

        remaining = required_margin_release
        orders: list[FuturesOrder] = []
        seen: set[str] = set()
        for candidate in candidates:
            code = candidate.contract_code.strip().upper()
            if code in seen:
                raise ValueError(f"Duplicate liquidation candidate: {code}")
            seen.add(code)
            position = ledger.positions.get(code)
            if position is None:
                continue
            if position.multiplier != candidate.multiplier:
                raise ValueError(f"Liquidation multiplier does not match position: {code}")
            lots = min(position.lots, math.ceil(remaining / candidate.margin_per_lot))
            side = FuturesSide.SELL if position.direction > 0 else FuturesSide.BUY
            orders.append(
                FuturesOrder(
                    order_id=f"{liquidation_id}:{len(orders) + 1}:{code}",
                    signal_date=signal_date,
                    eligible_date=eligible_date,
                    contract_code=code,
                    side=side,
                    offset=candidate.close_offset,
                    lots=lots,
                    multiplier=candidate.multiplier,
                    tick_size=candidate.tick_size,
                    fee_rule=candidate.fee_schedule.rule_for(candidate.close_offset),
                )
            )
            remaining -= lots * candidate.margin_per_lot
            if remaining <= 0:
                break
        if remaining > 0:
            raise ValueError("Open futures positions cannot release the required margin.")
        return tuple(orders)
