from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import polars as pl


@dataclass(frozen=True)
class FuturesTrendProtocol:
    lookbacks: tuple[int, ...] = (20, 60, 120)
    weights: tuple[float, ...] = (1 / 3, 1 / 3, 1 / 3)
    volatility_lookback: int = 40
    annualization_days: int = 252
    portfolio_target_annual_volatility: float = 0.08
    product_risk_budget_fraction: float = 0.10

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
    signal_strength: float
    estimated_daily_volatility: float
    product_daily_risk_budget: float
    allocated_daily_risk: float
    one_lot_daily_risk: float
    target_signed_lots: int
    status: str


@dataclass(frozen=True)
class FuturesTrendResult:
    protocol_id: str
    signal_date: date
    eligible_date: date
    targets: tuple[FuturesTrendTarget, ...]


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
    ) -> FuturesTrendResult:
        if signal_date >= eligible_date:
            raise ValueError("Trend targets require an eligible date after the signal date.")
        self._positive(equity, "equity")
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
        if not eligible_products:
            return FuturesTrendResult(
                protocol_id=self.protocol.protocol_id,
                signal_date=signal_date,
                eligible_date=eligible_date,
                targets=(),
            )
        contract_rows = self._rows(contracts, "contracts")
        product_daily_risk_budget = (
            equity
            * self.protocol.portfolio_target_annual_volatility
            / math.sqrt(self.protocol.annualization_days)
            * self.protocol.product_risk_budget_fraction
        )
        targets = tuple(
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
        return FuturesTrendResult(
            protocol_id=self.protocol.protocol_id,
            signal_date=signal_date,
            eligible_date=eligible_date,
            targets=targets,
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
        return FuturesTrendTarget(
            signal_date=signal_date,
            eligible_date=eligible_date,
            product_code=product_code,
            contract_code=contract_code,
            signal_strength=signal_strength,
            estimated_daily_volatility=volatility,
            product_daily_risk_budget=product_daily_risk_budget,
            allocated_daily_risk=allocated_daily_risk,
            one_lot_daily_risk=one_lot_daily_risk,
            target_signed_lots=signed_lots,
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
    def _sign(value: float) -> int:
        return (value > 0) - (value < 0)
