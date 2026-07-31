from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import polars as pl


@dataclass(frozen=True)
class FuturesBacktestDataIssue:
    code: str
    message: str
    rows: int


@dataclass(frozen=True)
class FuturesBacktestReadiness:
    required_rows: int
    issues: list[FuturesBacktestDataIssue] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.issues

    def require_ready(self) -> None:
        if self.ready:
            return
        summary = "; ".join(f"{issue.code}={issue.rows}" for issue in self.issues)
        raise ValueError(f"Futures backtest data is incomplete: {summary}")


class FuturesBacktestDataGate:
    REQUIRED_COLUMNS = {
        "requirements": {"contract_code", "trade_date", "direction"},
        "contracts": {"ts_code", "multiplier"},
        "daily": {"ts_code", "trade_date", "open", "settle", "vol"},
        "settlements": {
            "ts_code",
            "trade_date",
            "long_margin_rate",
            "short_margin_rate",
        },
        "limits": {"ts_code", "trade_date", "up_limit", "down_limit"},
    }

    def validate(
        self,
        requirements: pl.DataFrame,
        contracts: pl.DataFrame,
        daily: pl.DataFrame,
        settlements: pl.DataFrame,
        limits: pl.DataFrame,
    ) -> FuturesBacktestReadiness:
        frames = {
            "requirements": requirements,
            "contracts": contracts,
            "daily": daily,
            "settlements": settlements,
            "limits": limits,
        }
        issues = self._schema_issues(frames)
        if issues:
            return FuturesBacktestReadiness(requirements.height, issues)

        required_rows = self._requirements(requirements)
        duplicate_requirements = len(required_rows) - len(
            {(row["contract_code"], row["trade_date"], row["direction"]) for row in required_rows}
        )
        if duplicate_requirements:
            issues.append(
                FuturesBacktestDataIssue(
                    "duplicate_requirement",
                    "Required contract-date-direction rows must be unique.",
                    duplicate_requirements,
                )
            )

        for name in ("daily", "settlements", "limits"):
            duplicates = int(frames[name].select("ts_code", "trade_date").is_duplicated().sum())
            if duplicates:
                issues.append(
                    FuturesBacktestDataIssue(
                        f"duplicate_{name}_key",
                        f"{name} contains duplicate contract-date rows.",
                        duplicates,
                    )
                )

        contracts_lookup = self._contract_lookup(contracts)
        daily_lookup = self._dated_lookup(daily)
        settlement_lookup = self._dated_lookup(settlements)
        limit_lookup = self._dated_lookup(limits)
        counters = {
            "missing_contract_multiplier": 0,
            "missing_execution_bar": 0,
            "missing_margin_rate": 0,
            "missing_price_limits": 0,
        }
        for requirement in required_rows:
            code = requirement["contract_code"]
            key = (code, requirement["trade_date"])
            contract = contracts_lookup.get(code)
            if contract is None or self._number(contract.get("multiplier")) <= 0:
                counters["missing_contract_multiplier"] += 1

            bar = daily_lookup.get(key)
            if (
                bar is None
                or self._number(bar.get("open")) <= 0
                or self._number(bar.get("settle")) <= 0
                or self._number(bar.get("vol")) <= 0
            ):
                counters["missing_execution_bar"] += 1

            settlement = settlement_lookup.get(key)
            margin_column = (
                "long_margin_rate" if requirement["direction"] == "long" else "short_margin_rate"
            )
            if settlement is None or self._number(settlement.get(margin_column)) <= 0:
                counters["missing_margin_rate"] += 1

            limit = limit_lookup.get(key)
            up_limit = self._number(limit.get("up_limit")) if limit else 0.0
            down_limit = self._number(limit.get("down_limit")) if limit else 0.0
            if up_limit <= 0 or down_limit <= 0 or up_limit <= down_limit:
                counters["missing_price_limits"] += 1

        messages = {
            "missing_contract_multiplier": (
                "A positive contract multiplier is required for every execution row."
            ),
            "missing_execution_bar": (
                "A positive open, settlement, and volume are required for execution."
            ),
            "missing_margin_rate": (
                "The historical margin rate for the requested direction is required."
            ),
            "missing_price_limits": (
                "Historical upper and lower price limits are required and must be ordered."
            ),
        }
        issues.extend(
            FuturesBacktestDataIssue(code, messages[code], rows)
            for code, rows in counters.items()
            if rows
        )
        return FuturesBacktestReadiness(len(required_rows), issues)

    def _schema_issues(
        self,
        frames: dict[str, pl.DataFrame],
    ) -> list[FuturesBacktestDataIssue]:
        issues: list[FuturesBacktestDataIssue] = []
        for name, required in self.REQUIRED_COLUMNS.items():
            if missing := required - set(frames[name].columns):
                issues.append(
                    FuturesBacktestDataIssue(
                        f"missing_{name}_columns",
                        f"{name} is missing columns: {', '.join(sorted(missing))}",
                        len(missing),
                    )
                )
        return issues

    def _requirements(self, frame: pl.DataFrame) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for row in frame.iter_rows(named=True):
            direction = str(row["direction"]).strip().lower()
            if direction not in {"long", "short"}:
                raise ValueError("Futures requirement direction must be long or short.")
            rows.append(
                {
                    "contract_code": str(row["contract_code"]).strip().upper(),
                    "trade_date": self._date_key(row["trade_date"]),
                    "direction": direction,
                }
            )
        return rows

    @staticmethod
    def _contract_lookup(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
        sort_columns = [name for name in ("observed_at", "ts_code") if name in frame.columns]
        values = frame.sort(sort_columns).unique("ts_code", keep="last")
        return {str(row["ts_code"]).strip().upper(): row for row in values.iter_rows(named=True)}

    def _dated_lookup(self, frame: pl.DataFrame) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (
                str(row["ts_code"]).strip().upper(),
                self._date_key(row["trade_date"]),
            ): row
            for row in frame.iter_rows(named=True)
        }

    @staticmethod
    def _date_key(value: Any) -> str:
        text = str(value).strip().replace("-", "")
        if len(text) != 8 or not text.isdigit():
            raise ValueError("Futures data dates must use YYYYMMDD or YYYY-MM-DD.")
        return text

    @staticmethod
    def _number(value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return result if math.isfinite(result) else 0.0
