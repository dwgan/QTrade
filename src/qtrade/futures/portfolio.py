from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class FuturesSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class FuturesOffset(StrEnum):
    OPEN = "open"
    CLOSE = "close"
    CLOSE_TODAY = "close_today"
    CLOSE_YESTERDAY = "close_yesterday"


@dataclass(frozen=True)
class FuturesFill:
    trade_date: date
    contract_code: str
    side: FuturesSide
    offset: FuturesOffset
    lots: int
    price: float
    multiplier: float
    fee: float = 0.0

    def __post_init__(self) -> None:
        if self.lots <= 0:
            raise ValueError("Futures fill lots must be positive.")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("Futures fill price must be positive.")
        if not math.isfinite(self.multiplier) or self.multiplier <= 0:
            raise ValueError("Futures fill multiplier must be positive.")
        if not math.isfinite(self.fee) or self.fee < 0:
            raise ValueError("Futures fill fee must be non-negative.")


@dataclass(frozen=True)
class FuturesSettlementMark:
    contract_code: str
    settlement_price: float
    margin_rate: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.settlement_price) or self.settlement_price <= 0:
            raise ValueError("Futures settlement price must be positive.")
        if not math.isfinite(self.margin_rate) or not 0 < self.margin_rate < 1:
            raise ValueError("Futures margin rate must be between zero and one.")


@dataclass
class FuturesPosition:
    contract_code: str
    signed_lots: int
    multiplier: float
    settlement_basis: float

    @property
    def direction(self) -> int:
        return 1 if self.signed_lots > 0 else -1

    @property
    def lots(self) -> int:
        return abs(self.signed_lots)


@dataclass(frozen=True)
class FuturesLedgerEntry:
    event_date: date
    event_type: str
    contract_code: str | None
    cash_change: float
    realized_pnl: float
    fee: float
    equity_after: float
    detail: str


@dataclass(frozen=True)
class FuturesAccountSnapshot:
    trade_date: date
    equity: float
    available_cash: float
    margin: float
    stress_margin: float
    daily_pnl: float
    daily_fees: float
    margin_call: bool
    positions: int


@dataclass
class FuturesPortfolioLedger:
    initial_equity: float
    stress_margin_multiplier: float = 1.5
    equity: float = field(init=False)
    positions: dict[str, FuturesPosition] = field(default_factory=dict, init=False)
    entries: list[FuturesLedgerEntry] = field(default_factory=list, init=False)
    snapshots: list[FuturesAccountSnapshot] = field(default_factory=list, init=False)
    _daily_pnl: dict[date, float] = field(default_factory=dict, init=False)
    _daily_fees: dict[date, float] = field(default_factory=dict, init=False)
    _latest_fill_date: date | None = field(default=None, init=False)
    _last_settlement_date: date | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_equity) or self.initial_equity <= 0:
            raise ValueError("Futures initial equity must be positive.")
        if not math.isfinite(self.stress_margin_multiplier) or self.stress_margin_multiplier < 1:
            raise ValueError("Stress margin multiplier must be at least one.")
        self.equity = float(self.initial_equity)

    def apply_fill(self, fill: FuturesFill) -> None:
        if self._last_settlement_date is not None and fill.trade_date <= self._last_settlement_date:
            raise ValueError("Cannot apply a fill on or before the last settlement date.")
        if self._latest_fill_date is not None and fill.trade_date < self._latest_fill_date:
            raise ValueError("Futures fills must be applied in date order.")
        code = fill.contract_code.strip().upper()
        if not code:
            raise ValueError("Futures fill contract code is required.")
        if fill.offset == FuturesOffset.OPEN:
            self._open(code, fill)
        else:
            self._close(code, fill)
        self._charge_fee(code, fill)
        self._latest_fill_date = fill.trade_date

    def settle(
        self,
        trade_date: date,
        marks: dict[str, FuturesSettlementMark],
    ) -> FuturesAccountSnapshot:
        if self._last_settlement_date is not None and trade_date <= self._last_settlement_date:
            raise ValueError("Futures settlements must be strictly increasing by date.")
        if self._latest_fill_date is not None and trade_date < self._latest_fill_date:
            raise ValueError("Cannot settle before the latest fill date.")
        normalized = {code.strip().upper(): mark for code, mark in marks.items()}
        missing = sorted(set(self.positions) - set(normalized))
        if missing:
            raise ValueError(
                "Missing settlement or margin data for open contracts: " + ", ".join(missing)
            )
        mismatched = sorted(
            code
            for code, mark in normalized.items()
            if code in self.positions and mark.contract_code.strip().upper() != code
        )
        if mismatched:
            raise ValueError(
                "Settlement mark contract code does not match its lookup key: "
                + ", ".join(mismatched)
            )

        daily_pnl = self._daily_pnl.get(trade_date, 0.0)
        margin = 0.0
        for code, position in sorted(self.positions.items()):
            mark = normalized[code]
            pnl = (
                position.direction
                * position.lots
                * position.multiplier
                * (mark.settlement_price - position.settlement_basis)
            )
            self.equity += pnl
            daily_pnl += pnl
            position.settlement_basis = mark.settlement_price
            margin += position.lots * position.multiplier * mark.settlement_price * mark.margin_rate
            self.entries.append(
                FuturesLedgerEntry(
                    event_date=trade_date,
                    event_type="settlement",
                    contract_code=code,
                    cash_change=pnl,
                    realized_pnl=pnl,
                    fee=0.0,
                    equity_after=self.equity,
                    detail=f"settled {position.lots} lots at {mark.settlement_price}",
                )
            )
        self._daily_pnl[trade_date] = daily_pnl
        stress_margin = margin * self.stress_margin_multiplier
        available_cash = self.equity - margin
        snapshot = FuturesAccountSnapshot(
            trade_date=trade_date,
            equity=self.equity,
            available_cash=available_cash,
            margin=margin,
            stress_margin=stress_margin,
            daily_pnl=daily_pnl,
            daily_fees=self._daily_fees.get(trade_date, 0.0),
            margin_call=available_cash < 0,
            positions=len(self.positions),
        )
        self.snapshots.append(snapshot)
        self._last_settlement_date = trade_date
        return snapshot

    def _open(self, code: str, fill: FuturesFill) -> None:
        signed_lots = fill.lots if fill.side == FuturesSide.BUY else -fill.lots
        existing = self.positions.get(code)
        if existing is None:
            self.positions[code] = FuturesPosition(
                contract_code=code,
                signed_lots=signed_lots,
                multiplier=fill.multiplier,
                settlement_basis=fill.price,
            )
            return
        if existing.multiplier != fill.multiplier:
            raise ValueError(f"Contract multiplier changed while {code} is open.")
        if existing.signed_lots * signed_lots < 0:
            raise ValueError("Opposite-side open fill requires an explicit close first.")
        total_lots = existing.lots + fill.lots
        existing.settlement_basis = (
            existing.settlement_basis * existing.lots + fill.price * fill.lots
        ) / total_lots
        existing.signed_lots += signed_lots

    def _close(self, code: str, fill: FuturesFill) -> None:
        existing = self.positions.get(code)
        if existing is None:
            raise ValueError(f"Cannot close absent futures position: {code}")
        if existing.multiplier != fill.multiplier:
            raise ValueError(f"Contract multiplier changed while {code} is open.")
        closing_direction = -1 if fill.side == FuturesSide.SELL else 1
        if closing_direction != -existing.direction:
            raise ValueError("Close fill side does not offset the existing position.")
        if fill.lots > existing.lots:
            raise ValueError(f"Cannot close more lots than the open position for {code}.")
        realized = (
            existing.direction
            * fill.lots
            * existing.multiplier
            * (fill.price - existing.settlement_basis)
        )
        self.equity += realized
        self._daily_pnl[fill.trade_date] = self._daily_pnl.get(fill.trade_date, 0.0) + realized
        existing.signed_lots -= existing.direction * fill.lots
        if existing.signed_lots == 0:
            del self.positions[code]
        self.entries.append(
            FuturesLedgerEntry(
                event_date=fill.trade_date,
                event_type="close",
                contract_code=code,
                cash_change=realized,
                realized_pnl=realized,
                fee=0.0,
                equity_after=self.equity,
                detail=f"closed {fill.lots} lots at {fill.price}",
            )
        )

    def _charge_fee(self, code: str, fill: FuturesFill) -> None:
        self.equity -= fill.fee
        self._daily_fees[fill.trade_date] = self._daily_fees.get(fill.trade_date, 0.0) + fill.fee
        self.entries.append(
            FuturesLedgerEntry(
                event_date=fill.trade_date,
                event_type="fee",
                contract_code=code,
                cash_change=-fill.fee,
                realized_pnl=0.0,
                fee=fill.fee,
                equity_after=self.equity,
                detail=f"{fill.side.value} {fill.lots} lots at {fill.price}",
            )
        )
