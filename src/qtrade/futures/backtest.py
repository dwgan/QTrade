from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from qtrade.futures.execution import (
    FuturesDailyExecutionBar,
    FuturesDailyExecutionEngine,
    FuturesExecutionResult,
    FuturesExecutionStatus,
    FuturesFeeSchedule,
    FuturesOrder,
    FuturesPendingOrderBook,
)
from qtrade.futures.lifecycle import (
    FuturesLiquidationCandidate,
    FuturesLiquidationPlanner,
    FuturesPositionTarget,
    FuturesRollAttempt,
    FuturesRollCoordinator,
    FuturesRollPlan,
    FuturesRollStatus,
    FuturesTargetOrderPlanner,
)
from qtrade.futures.portfolio import (
    FuturesAccountSnapshot,
    FuturesOffset,
    FuturesPortfolioLedger,
    FuturesSettlementMark,
    FuturesSide,
)


@dataclass(frozen=True)
class FuturesDirectionalMarginRates:
    long_rate: float
    short_rate: float

    def __post_init__(self) -> None:
        if any(
            not math.isfinite(value) or not 0 < value < 1
            for value in (self.long_rate, self.short_rate)
        ):
            raise ValueError("Futures directional margin rates must be between zero and one.")

    def for_side(self, side: FuturesSide) -> float:
        return self.long_rate if side == FuturesSide.BUY else self.short_rate

    def for_direction(self, direction: int) -> float:
        return self.long_rate if direction > 0 else self.short_rate


@dataclass(frozen=True)
class FuturesLiquidationSpec:
    contract_code: str
    multiplier: float
    tick_size: float
    fee_schedule: FuturesFeeSchedule
    close_offset: FuturesOffset = FuturesOffset.CLOSE

    def __post_init__(self) -> None:
        if not self.contract_code.strip():
            raise ValueError("Liquidation contract code is required.")
        if any(
            not math.isfinite(value) or value <= 0 for value in (self.multiplier, self.tick_size)
        ):
            raise ValueError("Liquidation multiplier and tick size must be positive.")
        if self.close_offset == FuturesOffset.OPEN:
            raise ValueError("Liquidation spec must use a close offset.")


@dataclass(frozen=True)
class FuturesDailyPortfolioInput:
    trade_date: date
    next_trade_date: date
    bars: dict[str, FuturesDailyExecutionBar]
    settlement_marks: dict[str, FuturesSettlementMark]
    margin_rates: dict[str, FuturesDirectionalMarginRates]
    targets: tuple[FuturesPositionTarget, ...] = ()
    roll_plans: tuple[FuturesRollPlan, ...] = ()
    liquidation_priority: tuple[FuturesLiquidationSpec, ...] = ()
    rebalance_id: str | None = None

    def __post_init__(self) -> None:
        if self.next_trade_date <= self.trade_date:
            raise ValueError("Next futures trade date must follow the current trade date.")
        if self.targets and not self.rebalance_id:
            raise ValueError("Futures targets require a rebalance ID.")


@dataclass(frozen=True)
class FuturesDailyPortfolioResult:
    trade_date: date
    snapshot: FuturesAccountSnapshot
    executions: tuple[FuturesExecutionResult, ...]
    roll_attempts: tuple[FuturesRollAttempt, ...]
    generated_target_orders: tuple[FuturesOrder, ...]
    generated_liquidation_orders: tuple[FuturesOrder, ...]
    signals_suppressed: bool
    liquidation_priority_active: bool
    pending_normal_batches: int
    pending_rolls: int
    pending_liquidation_batches: int


@dataclass
class _OrderSequence:
    orders: tuple[FuturesOrder, ...]
    order_book: FuturesPendingOrderBook
    index: int = 0

    @property
    def completed(self) -> bool:
        return self.index >= len(self.orders)

    @property
    def current(self) -> FuturesOrder:
        return self.orders[self.index]


@dataclass
class FuturesDailyPortfolioEngine:
    initial_equity: float
    slippage_ticks: int = 1
    margin_call_buffer: float = 1.05
    stress_margin_multiplier: float = 1.5
    ledger: FuturesPortfolioLedger = field(init=False)
    execution_engine: FuturesDailyExecutionEngine = field(init=False)
    _normal_sequences: list[_OrderSequence] = field(default_factory=list, init=False)
    _liquidation_sequences: list[_OrderSequence] = field(default_factory=list, init=False)
    _rolls: list[FuturesRollCoordinator] = field(default_factory=list, init=False)
    _used_order_ids: set[str] = field(default_factory=set, init=False)
    _last_trade_date: date | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.margin_call_buffer) or self.margin_call_buffer < 1:
            raise ValueError("Futures margin call buffer must be at least one.")
        self.ledger = FuturesPortfolioLedger(
            self.initial_equity,
            stress_margin_multiplier=self.stress_margin_multiplier,
        )
        self.execution_engine = FuturesDailyExecutionEngine(self.slippage_ticks)

    def run_day(self, day: FuturesDailyPortfolioInput) -> FuturesDailyPortfolioResult:
        if self._last_trade_date is not None and day.trade_date <= self._last_trade_date:
            raise ValueError("Futures portfolio trade dates must be strictly increasing.")
        self._validate_signal_input(day)
        bars = self._normalize_bars(day)
        marks = {code.strip().upper(): mark for code, mark in day.settlement_marks.items()}
        rates = {code.strip().upper(): value for code, value in day.margin_rates.items()}
        self._preflight_day_inputs(bars, marks, rates)
        executions: list[FuturesExecutionResult] = []
        roll_attempts: list[FuturesRollAttempt] = []
        liquidation_priority_active = bool(self._liquidation_sequences)

        if liquidation_priority_active:
            self._process_sequences(
                self._liquidation_sequences,
                day.trade_date,
                bars,
                rates,
                executions,
                stop_after_block=True,
            )
        else:
            self._process_rolls(day.trade_date, bars, rates, roll_attempts)
            self._process_sequences(
                self._normal_sequences,
                day.trade_date,
                bars,
                rates,
                executions,
                stop_after_block=False,
            )

        snapshot = self.ledger.settle(day.trade_date, marks)
        generated_liquidations: tuple[FuturesOrder, ...] = ()
        generated_targets: tuple[FuturesOrder, ...] = ()
        pending_strategy_orders = bool(self._normal_sequences or self._rolls)
        signals_suppressed = snapshot.margin_call or pending_strategy_orders
        if snapshot.margin_call:
            if not self._liquidation_sequences:
                generated_liquidations = self._generate_liquidation(day, snapshot, marks, rates)
                self._append_sequence(self._liquidation_sequences, generated_liquidations)
        elif not pending_strategy_orders:
            generated_targets = self._submit_signals(day)

        self._last_trade_date = day.trade_date
        return FuturesDailyPortfolioResult(
            trade_date=day.trade_date,
            snapshot=snapshot,
            executions=tuple(executions),
            roll_attempts=tuple(roll_attempts),
            generated_target_orders=generated_targets,
            generated_liquidation_orders=generated_liquidations,
            signals_suppressed=signals_suppressed,
            liquidation_priority_active=liquidation_priority_active,
            pending_normal_batches=len(self._normal_sequences),
            pending_rolls=len(self._rolls),
            pending_liquidation_batches=len(self._liquidation_sequences),
        )

    def _normalize_bars(
        self,
        day: FuturesDailyPortfolioInput,
    ) -> dict[str, FuturesDailyExecutionBar]:
        bars = {code.strip().upper(): bar for code, bar in day.bars.items()}
        invalid = sorted(code for code, bar in bars.items() if bar.trade_date != day.trade_date)
        if invalid:
            raise ValueError(
                "Futures execution bars do not match trade date: " + ", ".join(invalid)
            )
        return bars

    def _validate_signal_input(self, day: FuturesDailyPortfolioInput) -> None:
        target_codes = [target.contract_code.strip().upper() for target in day.targets]
        if len(set(target_codes)) != len(target_codes):
            raise ValueError("Futures daily position targets must be unique by contract.")

        roll_codes: list[str] = []
        incoming_order_ids: list[str] = []
        for plan in day.roll_plans:
            if (
                plan.close_order.signal_date != day.trade_date
                or plan.close_order.eligible_date != day.next_trade_date
            ):
                raise ValueError("Futures roll plan dates do not match the daily signal dates.")
            roll_codes.extend(
                (
                    plan.close_order.contract_code.strip().upper(),
                    plan.open_order.contract_code.strip().upper(),
                )
            )
            incoming_order_ids.extend((plan.close_order.order_id, plan.open_order.order_id))
        if len(set(roll_codes)) != len(roll_codes):
            raise ValueError("Futures daily roll plans cannot share contracts.")
        if set(target_codes) & set(roll_codes):
            raise ValueError("Futures targets and roll plans cannot share contracts on one day.")
        if (
            len(set(incoming_order_ids)) != len(incoming_order_ids)
            or set(incoming_order_ids) & self._used_order_ids
        ):
            raise ValueError("Futures portfolio order IDs must be globally unique.")

    def _preflight_day_inputs(
        self,
        bars: dict[str, FuturesDailyExecutionBar],
        marks: dict[str, FuturesSettlementMark],
        rates: dict[str, FuturesDirectionalMarginRates],
    ) -> None:
        active_orders: list[FuturesOrder] = []
        if self._liquidation_sequences:
            active_orders.extend(
                order
                for sequence in self._liquidation_sequences
                for order in sequence.orders[sequence.index :]
            )
        else:
            active_orders.extend(
                order
                for sequence in self._normal_sequences
                for order in sequence.orders[sequence.index :]
            )
            active_orders.extend(coordinator.plan.open_order for coordinator in self._rolls)
            active_orders.extend(coordinator.plan.close_order for coordinator in self._rolls)

        current_codes = set(self.ledger.positions)
        open_codes = {
            order.contract_code.strip().upper()
            for order in active_orders
            if order.offset == FuturesOffset.OPEN
        }
        required_marks = current_codes | open_codes
        missing_marks = sorted(required_marks - set(marks))
        if missing_marks:
            raise ValueError(
                "Missing daily settlement marks before execution: " + ", ".join(missing_marks)
            )
        mismatched_marks = sorted(
            code for code in required_marks if marks[code].contract_code.strip().upper() != code
        )
        if mismatched_marks:
            raise ValueError(
                "Settlement mark contract code does not match lookup key: "
                + ", ".join(mismatched_marks)
            )
        missing_rates = sorted((current_codes | open_codes) - set(rates))
        if missing_rates:
            raise ValueError(
                "Missing directional margin rates before execution: " + ", ".join(missing_rates)
            )
        if active_orders:
            missing_margin_bars = sorted(current_codes - set(bars))
            if missing_margin_bars:
                raise ValueError(
                    "Missing intraday margin bars before execution: "
                    + ", ".join(missing_margin_bars)
                )

    def _process_sequences(
        self,
        sequences: list[_OrderSequence],
        trade_date: date,
        bars: dict[str, FuturesDailyExecutionBar],
        rates: dict[str, FuturesDirectionalMarginRates],
        executions: list[FuturesExecutionResult],
        *,
        stop_after_block: bool,
    ) -> None:
        for sequence in tuple(sequences):
            while not sequence.completed:
                order = sequence.current
                self._validate_order_against_ledger(order)
                available_cash = self._intraday_available_cash(bars, rates)
                margin_rate = self._margin_rate(order, rates)
                result = sequence.order_book.attempt(
                    order.order_id,
                    trade_date,
                    bars.get(order.contract_code.strip().upper()),
                    available_cash=available_cash,
                    margin_rate=margin_rate,
                )
                executions.append(result)
                if result.status != FuturesExecutionStatus.FILLED:
                    if stop_after_block:
                        return
                    break
                if result.fill is None:
                    raise RuntimeError("Filled futures order is missing its fill record.")
                self.ledger.apply_fill(result.fill)
                sequence.index += 1
            if sequence.completed:
                sequences.remove(sequence)

    def _process_rolls(
        self,
        trade_date: date,
        bars: dict[str, FuturesDailyExecutionBar],
        rates: dict[str, FuturesDirectionalMarginRates],
        attempts: list[FuturesRollAttempt],
    ) -> None:
        for coordinator in tuple(self._rolls):
            available_cash = self._intraday_available_cash(bars, rates)
            open_order = coordinator.plan.open_order
            margin_rate = self._margin_rate(open_order, rates)
            result = coordinator.attempt(
                trade_date,
                bars,
                available_cash=available_cash,
                open_available_cash=available_cash,
                open_margin_rate=margin_rate,
            )
            attempts.append(result)
            if result.status == FuturesRollStatus.COMPLETED:
                self._rolls.remove(coordinator)

    def _intraday_available_cash(
        self,
        bars: dict[str, FuturesDailyExecutionBar],
        rates: dict[str, FuturesDirectionalMarginRates],
    ) -> float:
        margin = 0.0
        for code, position in self.ledger.positions.items():
            bar = bars.get(code)
            directional = rates.get(code)
            if bar is None or bar.open_price is None or not math.isfinite(bar.open_price):
                raise ValueError(f"Missing intraday margin price for open contract: {code}")
            if directional is None:
                raise ValueError(f"Missing directional margin rates for open contract: {code}")
            rate = directional.for_direction(position.direction)
            margin += position.lots * position.multiplier * bar.open_price * rate
        return self.ledger.equity - margin

    @staticmethod
    def _margin_rate(
        order: FuturesOrder,
        rates: dict[str, FuturesDirectionalMarginRates],
    ) -> float | None:
        if order.offset != FuturesOffset.OPEN:
            return None
        directional = rates.get(order.contract_code.strip().upper())
        return directional.for_side(order.side) if directional is not None else None

    def _validate_order_against_ledger(self, order: FuturesOrder) -> None:
        code = order.contract_code.strip().upper()
        position = self.ledger.positions.get(code)
        if order.offset == FuturesOffset.OPEN:
            if position is None:
                return
            direction = 1 if order.side == FuturesSide.BUY else -1
            if position.direction != direction or position.multiplier != order.multiplier:
                raise ValueError(f"Futures open order conflicts with current position: {code}")
            return
        if position is None or position.lots < order.lots:
            raise ValueError(f"Futures close order exceeds current position: {code}")
        expected_side = FuturesSide.SELL if position.direction > 0 else FuturesSide.BUY
        if order.side != expected_side or position.multiplier != order.multiplier:
            raise ValueError(f"Futures close order conflicts with current position: {code}")

    def _generate_liquidation(
        self,
        day: FuturesDailyPortfolioInput,
        snapshot: FuturesAccountSnapshot,
        marks: dict[str, FuturesSettlementMark],
        rates: dict[str, FuturesDirectionalMarginRates],
    ) -> tuple[FuturesOrder, ...]:
        candidates: list[FuturesLiquidationCandidate] = []
        for spec in day.liquidation_priority:
            code = spec.contract_code.strip().upper()
            position = self.ledger.positions.get(code)
            if position is None:
                continue
            mark = marks.get(code)
            directional = rates.get(code)
            if mark is None or directional is None:
                raise ValueError(f"Missing liquidation inputs for open contract: {code}")
            margin_per_lot = (
                mark.settlement_price
                * position.multiplier
                * directional.for_direction(position.direction)
            )
            candidates.append(
                FuturesLiquidationCandidate(
                    contract_code=code,
                    margin_per_lot=margin_per_lot,
                    multiplier=spec.multiplier,
                    tick_size=spec.tick_size,
                    fee_schedule=spec.fee_schedule,
                    close_offset=spec.close_offset,
                )
            )
        required_release = -snapshot.available_cash * self.margin_call_buffer
        return FuturesLiquidationPlanner().plan(
            self.ledger,
            liquidation_id=f"margin-call-{day.trade_date.isoformat()}",
            signal_date=day.trade_date,
            eligible_date=day.next_trade_date,
            required_margin_release=required_release,
            candidates=candidates,
        )

    def _submit_signals(self, day: FuturesDailyPortfolioInput) -> tuple[FuturesOrder, ...]:
        generated: tuple[FuturesOrder, ...] = ()
        if day.targets:
            generated = FuturesTargetOrderPlanner().plan(
                self.ledger,
                rebalance_id=str(day.rebalance_id),
                signal_date=day.trade_date,
                eligible_date=day.next_trade_date,
                targets=list(day.targets),
            )
            self._append_sequence(self._normal_sequences, generated)
        for plan in day.roll_plans:
            self._reserve_orders((plan.close_order, plan.open_order))
            self._rolls.append(FuturesRollCoordinator(plan, self.ledger, self.execution_engine))
        return generated

    def _append_sequence(
        self,
        destination: list[_OrderSequence],
        orders: tuple[FuturesOrder, ...],
    ) -> None:
        if not orders:
            return
        self._reserve_orders(orders)
        order_book = FuturesPendingOrderBook(self.execution_engine)
        for order in orders:
            order_book.submit(order)
        destination.append(_OrderSequence(orders, order_book))

    def _reserve_orders(self, orders: tuple[FuturesOrder, ...]) -> None:
        incoming = [order.order_id for order in orders]
        if len(set(incoming)) != len(incoming) or set(incoming) & self._used_order_ids:
            raise ValueError("Futures portfolio order IDs must be globally unique.")
        self._used_order_ids.update(incoming)
