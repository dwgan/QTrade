from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Any

import polars as pl

from qtrade.futures.trend_buffer import FuturesPositionBuffer, FuturesPositionBufferPolicy
from qtrade.futures.trend_risk import (
    FuturesPortfolioRiskAllocator,
    FuturesPortfolioRiskCandidate,
    FuturesPortfolioRiskLimits,
)


@dataclass(frozen=True)
class FuturesTrendProtocol:
    lookbacks: tuple[int, ...] = (20, 60, 120)
    weights: tuple[float, ...] = (1 / 3, 1 / 3, 1 / 3)
    volatility_lookback: int = 40
    annualization_days: int = 252
    portfolio_target_annual_volatility: float = 0.08
    product_risk_budget_fraction: float = 0.10
    sector_risk_budget_fraction: float = 0.30
    initial_margin_fraction: float = 0.25
    stress_margin_fraction: float = 0.50
    stress_margin_multiplier: float = 1.50
    position_buffer_fraction: float = 0.10
    position_buffer_minimum_lots: int = 1

    def __post_init__(self) -> None:
        if not self.lookbacks or len(self.lookbacks) != len(self.weights):
            raise ValueError("Trend lookbacks and weights must be non-empty and aligned.")
        if any(value <= 0 for value in self.lookbacks):
            raise ValueError("Trend lookbacks must be positive.")
        if any(not math.isfinite(value) or value < 0 for value in self.weights):
            raise ValueError("Trend weights must be finite and non-negative.")
        if not math.isclose(sum(self.weights), 1.0, rel_tol=0, abs_tol=1e-12):
            raise ValueError("Trend weights must sum to one.")
        if self.volatility_lookback < 2:
            raise ValueError("Volatility lookback must be at least two days.")
        if self.annualization_days <= 0:
            raise ValueError("Annualization days must be positive.")
        self._fraction("portfolio_target_annual_volatility")
        self._fraction("product_risk_budget_fraction")
        self._fraction("sector_risk_budget_fraction")
        self._fraction("initial_margin_fraction")
        self._fraction("stress_margin_fraction")
        if not math.isfinite(self.stress_margin_multiplier) or self.stress_margin_multiplier < 1:
            raise ValueError("stress_margin_multiplier must be finite and at least one.")
        FuturesPositionBufferPolicy(
            relative_threshold=self.position_buffer_fraction,
            minimum_lots=self.position_buffer_minimum_lots,
        )

    @property
    def protocol_id(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    def _fraction(self, name: str) -> None:
        value = getattr(self, name)
        if not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"{name} must be finite and in (0, 1].")


@dataclass(frozen=True)
class FuturesTrendTarget:
    signal_date: date
    eligible_date: date
    product_code: str
    contract_code: str
    sector: str
    signal_strength: float
    estimated_daily_volatility: float
    product_daily_risk_budget: float
    allocated_daily_risk: float
    one_lot_daily_risk: float
    one_lot_initial_margin: float
    unconstrained_signed_lots: int
    buffered_signed_lots: int
    buffer_applied: bool
    buffer_reason: str | None
    target_signed_lots: int
    initial_margin: float
    stress_margin: float
    limit_reasons: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class FuturesTrendResult:
    protocol_id: str
    signal_date: date
    eligible_date: date
    targets: tuple[FuturesTrendTarget, ...]
    portfolio_daily_risk_budget: float
    total_daily_risk: float
    sector_daily_risk: dict[str, float]
    initial_margin: float
    stress_margin: float


class FuturesTrendEngine:
    def __init__(self, protocol: FuturesTrendProtocol) -> None:
        self.protocol = protocol

    def generate(
        self,
        signal_date: date,
        eligible_date: date,
        equity: float,
        continuous: pl.DataFrame,
        universe: pl.DataFrame,
        roll_schedule: pl.DataFrame,
        contracts: pl.DataFrame,
        previous_targets: dict[str, int] | None = None,
    ) -> FuturesTrendResult:
        if signal_date >= eligible_date:
            raise ValueError("Trend targets require an eligible date after the signal date.")
        equity = self._positive(equity, "equity")
        continuous_rows = self._rows(continuous, "continuous")
        universe_rows = self._rows(universe, "universe")
        roll_rows = self._rows(roll_schedule, "roll_schedule")
        eligible_products = sorted(
            {
                str(row["product_code"])
                for row in universe_rows
                if self._date(row["trade_date"]) == signal_date and bool(row["eligible"])
            }
        )
        portfolio_daily_risk_budget = (
            equity
            * self.protocol.portfolio_target_annual_volatility
            / math.sqrt(self.protocol.annualization_days)
        )
        if not eligible_products:
            return FuturesTrendResult(
                protocol_id=self.protocol.protocol_id,
                signal_date=signal_date,
                eligible_date=eligible_date,
                targets=(),
                portfolio_daily_risk_budget=portfolio_daily_risk_budget,
                total_daily_risk=0.0,
                sector_daily_risk={},
                initial_margin=0.0,
                stress_margin=0.0,
            )
        contract_rows = self._rows(contracts, "contracts")
        product_daily_risk_budget = (
            equity
            * self.protocol.portfolio_target_annual_volatility
            / math.sqrt(self.protocol.annualization_days)
            * self.protocol.product_risk_budget_fraction
        )
        unconstrained_targets = tuple(
            self._target(
                product_code,
                signal_date,
                eligible_date,
                product_daily_risk_budget,
                continuous_rows,
                roll_rows,
                contract_rows,
            )
            for product_code in eligible_products
        )
        buffered_targets = tuple(
            self._buffer_target(target, (previous_targets or {}).get(target.product_code, 0))
            for target in unconstrained_targets
        )
        risk_result = FuturesPortfolioRiskAllocator(self._risk_limits()).allocate(
            equity,
            [
                FuturesPortfolioRiskCandidate(
                    product_code=target.product_code,
                    sector=target.sector,
                    signed_lots=target.buffered_signed_lots,
                    one_lot_daily_risk=target.one_lot_daily_risk,
                    one_lot_initial_margin=target.one_lot_initial_margin,
                )
                for target in buffered_targets
            ],
        )
        allocations = {item.product_code: item for item in risk_result.allocations}
        targets = tuple(
            self._apply_allocation(target, allocations[target.product_code])
            for target in buffered_targets
        )
        return FuturesTrendResult(
            protocol_id=self.protocol.protocol_id,
            signal_date=signal_date,
            eligible_date=eligible_date,
            targets=targets,
            portfolio_daily_risk_budget=risk_result.portfolio_daily_risk_budget,
            total_daily_risk=risk_result.total_daily_risk,
            sector_daily_risk=risk_result.sector_daily_risk,
            initial_margin=risk_result.initial_margin,
            stress_margin=risk_result.stress_margin,
        )

    def _target(
        self,
        product_code: str,
        signal_date: date,
        eligible_date: date,
        product_daily_risk_budget: float,
        continuous_rows: list[dict[str, Any]],
        roll_rows: list[dict[str, Any]],
        contract_rows: list[dict[str, Any]],
    ) -> FuturesTrendTarget:
        mappings = [
            row
            for row in roll_rows
            if str(row["product_code"]) == product_code
            and self._date(row["decision_date"]) == signal_date
            and self._date(row["effective_date"]) == eligible_date
            and bool(row["universe_eligible"])
        ]
        mapping = self._one(mappings, f"roll mapping for {product_code}")
        contract_code = str(mapping["selected_contract"])
        specs = [
            row
            for row in contract_rows
            if str(row["contract_code"]) == contract_code
            and self._date(row["trade_date"]) == signal_date
        ]
        spec = self._one(specs, f"contract specification for {contract_code}")
        settlement = self._positive(spec["settle"], f"settlement for {contract_code}")
        multiplier = self._positive(spec["multiplier"], f"multiplier for {contract_code}")
        sector = str(spec["sector"]).strip()
        if not sector:
            raise ValueError(f"sector for {contract_code} must not be empty.")
        long_margin_rate = self._margin_rate(
            spec["long_margin_rate"], f"long margin rate for {contract_code}"
        )
        short_margin_rate = self._margin_rate(
            spec["short_margin_rate"], f"short margin rate for {contract_code}"
        )
        history = sorted(
            (
                self._date(row["trade_date"]),
                self._positive(row["continuous_index"], "continuous_index"),
            )
            for row in continuous_rows
            if str(row["product_code"]) == product_code
            and self._date(row["trade_date"]) <= signal_date
        )
        if len({trading_date for trading_date, _ in history}) != len(history):
            raise ValueError(f"Duplicate continuous dates for {product_code}.")
        required_prices = max(
            max(self.protocol.lookbacks) + 1,
            self.protocol.volatility_lookback + 1,
        )
        if len(history) < required_prices or history[-1][0] != signal_date:
            raise ValueError(
                f"Insufficient point-in-time history for {product_code} on {signal_date}."
            )
        prices = [value for _, value in history]
        signs = [
            self._sign(prices[-1] / prices[-lookback - 1] - 1)
            for lookback in self.protocol.lookbacks
        ]
        signal_strength = sum(
            weight * direction
            for weight, direction in zip(self.protocol.weights, signs, strict=True)
        )
        returns = [
            prices[index] / prices[index - 1] - 1
            for index in range(len(prices) - self.protocol.volatility_lookback, len(prices))
        ]
        volatility = statistics.stdev(returns)
        self._positive(volatility, f"estimated volatility for {product_code}")
        allocated_daily_risk = product_daily_risk_budget * abs(signal_strength)
        one_lot_daily_risk = settlement * multiplier * volatility
        lots = math.floor(allocated_daily_risk / one_lot_daily_risk)
        if signal_strength == 0:
            status = "flat_signal"
            signed_lots = 0
        elif lots == 0:
            status = "insufficient_capital"
            signed_lots = 0
        else:
            status = "targeted"
            signed_lots = lots * self._sign(signal_strength)
        margin_rate = (
            long_margin_rate
            if signal_strength > 0
            else short_margin_rate
            if signal_strength < 0
            else max(long_margin_rate, short_margin_rate)
        )
        one_lot_initial_margin = settlement * multiplier * margin_rate
        return FuturesTrendTarget(
            signal_date=signal_date,
            eligible_date=eligible_date,
            product_code=product_code,
            contract_code=contract_code,
            sector=sector,
            signal_strength=signal_strength,
            estimated_daily_volatility=volatility,
            product_daily_risk_budget=product_daily_risk_budget,
            allocated_daily_risk=allocated_daily_risk,
            one_lot_daily_risk=one_lot_daily_risk,
            one_lot_initial_margin=one_lot_initial_margin,
            unconstrained_signed_lots=signed_lots,
            buffered_signed_lots=signed_lots,
            buffer_applied=False,
            buffer_reason=None,
            target_signed_lots=signed_lots,
            initial_margin=abs(signed_lots) * one_lot_initial_margin,
            stress_margin=(
                abs(signed_lots) * one_lot_initial_margin * self.protocol.stress_margin_multiplier
            ),
            limit_reasons=(),
            status=status,
        )

    def _buffer_target(
        self,
        target: FuturesTrendTarget,
        previous_signed_lots: int,
    ) -> FuturesTrendTarget:
        result = FuturesPositionBuffer(
            FuturesPositionBufferPolicy(
                relative_threshold=self.protocol.position_buffer_fraction,
                minimum_lots=self.protocol.position_buffer_minimum_lots,
            )
        ).apply(previous_signed_lots, target.unconstrained_signed_lots)
        within_product_budget = (
            abs(result.signed_lots) * target.one_lot_daily_risk
            <= target.allocated_daily_risk + 1e-12
        )
        if not result.applied or not within_product_budget:
            return target
        return replace(
            target,
            buffered_signed_lots=result.signed_lots,
            buffer_applied=True,
            buffer_reason=result.reason,
            target_signed_lots=result.signed_lots,
            status="buffered",
        )

    def _risk_limits(self) -> FuturesPortfolioRiskLimits:
        return FuturesPortfolioRiskLimits(
            annualization_days=self.protocol.annualization_days,
            portfolio_target_annual_volatility=self.protocol.portfolio_target_annual_volatility,
            sector_risk_budget_fraction=self.protocol.sector_risk_budget_fraction,
            initial_margin_fraction=self.protocol.initial_margin_fraction,
            stress_margin_fraction=self.protocol.stress_margin_fraction,
            stress_margin_multiplier=self.protocol.stress_margin_multiplier,
        )

    @staticmethod
    def _apply_allocation(target: FuturesTrendTarget, allocation: Any) -> FuturesTrendTarget:
        status = target.status
        if allocation.signed_lots != target.target_signed_lots:
            status = "risk_limited" if allocation.signed_lots else "risk_limited_to_zero"
        return replace(
            target,
            target_signed_lots=allocation.signed_lots,
            initial_margin=allocation.initial_margin,
            stress_margin=allocation.stress_margin,
            limit_reasons=allocation.limit_reasons,
            status=status,
        )

    @staticmethod
    def _rows(frame: pl.DataFrame, name: str) -> list[dict[str, Any]]:
        if frame.is_empty():
            raise ValueError(f"{name} must not be empty.")
        return frame.to_dicts()

    @staticmethod
    def _one(rows: list[dict[str, Any]], description: str) -> dict[str, Any]:
        if len(rows) != 1:
            raise ValueError(f"Expected exactly one {description}; found {len(rows)}.")
        return rows[0]

    @staticmethod
    def _date(value: Any) -> date:
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    @staticmethod
    def _positive(value: Any, name: str) -> float:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be finite and positive.")
        return number

    @staticmethod
    def _margin_rate(value: Any, name: str) -> float:
        number = float(value)
        if not math.isfinite(number) or not 0 < number < 1:
            raise ValueError(f"{name} must be finite and in (0, 1).")
        return number

    @staticmethod
    def _sign(value: float) -> int:
        return (value > 0) - (value < 0)
