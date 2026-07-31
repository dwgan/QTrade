from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum

from qtrade.futures.portfolio import FuturesFill, FuturesOffset, FuturesSide


class FuturesExecutionStatus(StrEnum):
    WAITING = "waiting"
    BLOCKED = "blocked"
    FILLED = "filled"


@dataclass(frozen=True)
class FuturesFeeRule:
    per_lot: float = 0.0
    notional_rate: float = 0.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.per_lot)
            or not math.isfinite(self.notional_rate)
            or self.per_lot < 0
            or self.notional_rate < 0
        ):
            raise ValueError("Futures fee rates must be non-negative.")

    def calculate(self, price: float, multiplier: float, lots: int) -> float:
        return self.per_lot * lots + self.notional_rate * price * multiplier * lots


@dataclass(frozen=True)
class FuturesFeeSchedule:
    open_rule: FuturesFeeRule = FuturesFeeRule()
    close_rule: FuturesFeeRule = FuturesFeeRule()
    close_today_rule: FuturesFeeRule | None = None
    close_yesterday_rule: FuturesFeeRule | None = None

    def rule_for(self, offset: FuturesOffset) -> FuturesFeeRule:
        if offset == FuturesOffset.OPEN:
            return self.open_rule
        if offset == FuturesOffset.CLOSE_TODAY:
            return self.close_today_rule or self.close_rule
        if offset == FuturesOffset.CLOSE_YESTERDAY:
            return self.close_yesterday_rule or self.close_rule
        return self.close_rule


@dataclass(frozen=True)
class FuturesOrder:
    order_id: str
    signal_date: date
    eligible_date: date
    contract_code: str
    side: FuturesSide
    offset: FuturesOffset
    lots: int
    multiplier: float
    tick_size: float
    fee_rule: FuturesFeeRule = FuturesFeeRule()

    def __post_init__(self) -> None:
        if not self.order_id.strip():
            raise ValueError("Futures order ID is required.")
        if self.eligible_date <= self.signal_date:
            raise ValueError("Futures order eligible date must follow its signal date.")
        if not self.contract_code.strip():
            raise ValueError("Futures order contract code is required.")
        if self.lots <= 0:
            raise ValueError("Futures order lots must be positive.")
        if (
            not math.isfinite(self.multiplier)
            or not math.isfinite(self.tick_size)
            or self.multiplier <= 0
            or self.tick_size <= 0
        ):
            raise ValueError("Futures order multiplier and tick size must be positive.")


@dataclass(frozen=True)
class FuturesDailyExecutionBar:
    trade_date: date
    open_price: float | None
    high_price: float | None
    low_price: float | None
    volume: float | None
    up_limit: float | None
    down_limit: float | None


@dataclass(frozen=True)
class FuturesExecutionResult:
    order_id: str
    attempt_date: date
    status: FuturesExecutionStatus
    reason: str
    fill: FuturesFill | None = None
    required_margin: float = 0.0

    @property
    def pending(self) -> bool:
        return self.status != FuturesExecutionStatus.FILLED


@dataclass(frozen=True)
class FuturesOrderState:
    order: FuturesOrder
    attempt_count: int = 0
    first_attempt_date: date | None = None
    last_attempt_date: date | None = None
    last_status: FuturesExecutionStatus = FuturesExecutionStatus.WAITING
    last_reason: str = "submitted"
    fill_date: date | None = None

    @property
    def pending(self) -> bool:
        return self.fill_date is None


class FuturesDailyExecutionEngine:
    def __init__(self, slippage_ticks: int = 1) -> None:
        if slippage_ticks < 0:
            raise ValueError("Futures slippage ticks must be non-negative.")
        self.slippage_ticks = slippage_ticks

    def attempt(
        self,
        order: FuturesOrder,
        attempt_date: date,
        bar: FuturesDailyExecutionBar | None,
        *,
        available_cash: float,
        margin_rate: float | None,
    ) -> FuturesExecutionResult:
        if attempt_date < order.eligible_date:
            return self._result(order, attempt_date, FuturesExecutionStatus.WAITING, "not_eligible")
        if not math.isfinite(available_cash):
            return self._result(
                order,
                attempt_date,
                FuturesExecutionStatus.BLOCKED,
                "invalid_available_cash",
            )
        if bar is None or bar.trade_date != attempt_date:
            return self._result(order, attempt_date, FuturesExecutionStatus.BLOCKED, "missing_bar")
        if not self._positive(bar.open_price):
            return self._result(order, attempt_date, FuturesExecutionStatus.BLOCKED, "missing_open")
        if not self._positive(bar.volume):
            return self._result(order, attempt_date, FuturesExecutionStatus.BLOCKED, "no_volume")
        if not self._valid_limits(bar):
            return self._result(
                order,
                attempt_date,
                FuturesExecutionStatus.BLOCKED,
                "missing_price_limits",
            )

        open_price = float(bar.open_price)
        if not self._positive(bar.high_price) or not self._positive(bar.low_price):
            return self._result(
                order,
                attempt_date,
                FuturesExecutionStatus.BLOCKED,
                "missing_intraday_range",
            )
        high_price = float(bar.high_price)
        low_price = float(bar.low_price)
        if low_price > high_price or not low_price <= open_price <= high_price:
            return self._result(
                order,
                attempt_date,
                FuturesExecutionStatus.BLOCKED,
                "invalid_intraday_range",
            )
        up_limit = float(bar.up_limit)
        down_limit = float(bar.down_limit)
        if not down_limit <= open_price <= up_limit:
            return self._result(
                order,
                attempt_date,
                FuturesExecutionStatus.BLOCKED,
                "open_outside_limits",
            )
        if self._locked_against_order(order.side, bar):
            reason = "locked_limit_up" if order.side == FuturesSide.BUY else "locked_limit_down"
            return self._result(order, attempt_date, FuturesExecutionStatus.BLOCKED, reason)

        slippage = self.slippage_ticks * order.tick_size
        if order.side == FuturesSide.BUY:
            fill_price = min(up_limit, open_price + slippage)
        else:
            fill_price = max(down_limit, open_price - slippage)
        fee = order.fee_rule.calculate(fill_price, order.multiplier, order.lots)
        required_margin = 0.0
        if order.offset == FuturesOffset.OPEN:
            if margin_rate is None or not math.isfinite(margin_rate) or not 0 < margin_rate < 1:
                return self._result(
                    order,
                    attempt_date,
                    FuturesExecutionStatus.BLOCKED,
                    "missing_margin_rate",
                )
            required_margin = fill_price * order.multiplier * order.lots * margin_rate
            if available_cash < required_margin + fee:
                return self._result(
                    order,
                    attempt_date,
                    FuturesExecutionStatus.BLOCKED,
                    "insufficient_margin",
                    required_margin=required_margin,
                )

        fill = FuturesFill(
            trade_date=attempt_date,
            contract_code=order.contract_code.strip().upper(),
            side=order.side,
            offset=order.offset,
            lots=order.lots,
            price=fill_price,
            multiplier=order.multiplier,
            fee=fee,
        )
        return FuturesExecutionResult(
            order_id=order.order_id,
            attempt_date=attempt_date,
            status=FuturesExecutionStatus.FILLED,
            reason="filled",
            fill=fill,
            required_margin=required_margin,
        )

    @staticmethod
    def _locked_against_order(
        side: FuturesSide,
        bar: FuturesDailyExecutionBar,
    ) -> bool:
        high = float(bar.high_price)
        low = float(bar.low_price)
        if side == FuturesSide.BUY:
            return low >= float(bar.up_limit) and high >= float(bar.up_limit)
        return high <= float(bar.down_limit) and low <= float(bar.down_limit)

    @classmethod
    def _valid_limits(cls, bar: FuturesDailyExecutionBar) -> bool:
        return (
            cls._positive(bar.up_limit)
            and cls._positive(bar.down_limit)
            and float(bar.up_limit) > float(bar.down_limit)
        )

    @staticmethod
    def _positive(value: float | None) -> bool:
        return value is not None and math.isfinite(value) and value > 0

    @staticmethod
    def _result(
        order: FuturesOrder,
        attempt_date: date,
        status: FuturesExecutionStatus,
        reason: str,
        *,
        required_margin: float = 0.0,
    ) -> FuturesExecutionResult:
        return FuturesExecutionResult(
            order_id=order.order_id,
            attempt_date=attempt_date,
            status=status,
            reason=reason,
            required_margin=required_margin,
        )


class FuturesPendingOrderBook:
    def __init__(self, engine: FuturesDailyExecutionEngine | None = None) -> None:
        self.engine = engine or FuturesDailyExecutionEngine()
        self._states: dict[str, FuturesOrderState] = {}

    def submit(self, order: FuturesOrder) -> FuturesOrderState:
        if order.order_id in self._states:
            raise ValueError(f"Duplicate futures order ID: {order.order_id}")
        state = FuturesOrderState(order=order)
        self._states[order.order_id] = state
        return state

    def attempt(
        self,
        order_id: str,
        attempt_date: date,
        bar: FuturesDailyExecutionBar | None,
        *,
        available_cash: float,
        margin_rate: float | None,
    ) -> FuturesExecutionResult:
        state = self.state(order_id)
        if not state.pending:
            raise ValueError(f"Futures order is already filled: {order_id}")
        if state.last_attempt_date is not None and attempt_date <= state.last_attempt_date:
            raise ValueError("Futures order attempt dates must be strictly increasing.")

        result = self.engine.attempt(
            state.order,
            attempt_date,
            bar,
            available_cash=available_cash,
            margin_rate=margin_rate,
        )
        self._states[order_id] = replace(
            state,
            attempt_count=state.attempt_count + 1,
            first_attempt_date=state.first_attempt_date or attempt_date,
            last_attempt_date=attempt_date,
            last_status=result.status,
            last_reason=result.reason,
            fill_date=result.fill.trade_date if result.fill is not None else None,
        )
        return result

    def state(self, order_id: str) -> FuturesOrderState:
        try:
            return self._states[order_id]
        except KeyError as error:
            raise KeyError(f"Unknown futures order ID: {order_id}") from error

    def pending_states(self) -> tuple[FuturesOrderState, ...]:
        return tuple(state for state in self._states.values() if state.pending)
