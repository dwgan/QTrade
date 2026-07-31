from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import polars as pl

from qtrade.config import FuturesConfig


@dataclass
class FuturesSeriesResult:
    roll_schedule: pl.DataFrame
    continuous: pl.DataFrame
    universe: pl.DataFrame
    vendor_comparison: pl.DataFrame
    issues: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(issue["severity"] == "error" for issue in self.issues)


class FuturesSeriesBuilder:
    def __init__(self, config: FuturesConfig) -> None:
        self.config = config

    def build(
        self,
        daily: pl.DataFrame,
        contracts: pl.DataFrame,
        mappings: pl.DataFrame | None = None,
    ) -> FuturesSeriesResult:
        if daily.is_empty():
            raise ValueError("Futures daily data is empty for the requested range.")
        required_daily = {"ts_code", "trade_date", "settle", "vol", "oi"}
        required_contracts = {
            "ts_code",
            "fut_code",
            "exchange",
            "list_date",
            "delist_date",
            "multiplier",
            "trade_time_desc",
        }
        self._require_columns(daily, required_daily, "futures_daily")
        self._require_columns(contracts, required_contracts, "futures_contracts")

        contract_rows = self._contract_rows(contracts)
        daily_rows = self._daily_rows(daily, contract_rows)
        if not daily_rows:
            raise ValueError("No futures daily rows matched the contract master.")
        dates = sorted({row["trade_date"] for row in daily_rows})
        rows_by_date = {
            trading_date: [row for row in daily_rows if row["trade_date"] == trading_date]
            for trading_date in dates
        }
        universe = self._build_universe(dates, rows_by_date, contract_rows)
        schedule = self._build_roll_schedule(dates, rows_by_date, contract_rows, universe)
        continuous, issues = self._build_continuous(schedule, rows_by_date, dates)
        comparison = self._compare_vendor(schedule, mappings)
        issues.extend(self._input_quality_issues(daily))
        issues.extend(self._quality_issues(schedule, continuous, universe))
        return FuturesSeriesResult(
            self._frame(schedule),
            self._frame(continuous),
            self._frame(universe),
            comparison,
            issues,
        )

    @staticmethod
    def _require_columns(frame: pl.DataFrame, required: set[str], name: str) -> None:
        if missing := required - set(frame.columns):
            raise ValueError(f"{name} is missing columns: {', '.join(sorted(missing))}")

    def _contract_rows(self, contracts: pl.DataFrame) -> dict[str, dict[str, Any]]:
        sort_columns = [name for name in ("observed_at", "ts_code") if name in contracts.columns]
        latest = contracts.sort(sort_columns).unique("ts_code", keep="last")
        result: dict[str, dict[str, Any]] = {}
        for raw in latest.iter_rows(named=True):
            product = str(raw.get("fut_code") or "").strip().upper()
            if not product or product in self.config.excluded_product_codes:
                continue
            result[str(raw["ts_code"]).upper()] = {
                **raw,
                "ts_code": str(raw["ts_code"]).upper(),
                "product_code": product,
                "list_date": self._parse_date(raw.get("list_date")),
                "expiry_date": self._parse_date(raw.get("last_ddate") or raw.get("delist_date")),
            }
        return result

    def _daily_rows(
        self,
        daily: pl.DataFrame,
        contracts: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for raw in daily.sort(["trade_date", "ts_code"]).iter_rows(named=True):
            ts_code = str(raw["ts_code"]).upper()
            contract = contracts.get(ts_code)
            trading_date = self._parse_date(raw.get("trade_date"))
            if contract is None or trading_date is None:
                continue
            rows.append(
                {
                    **raw,
                    "ts_code": ts_code,
                    "trade_date": trading_date,
                    "product_code": contract["product_code"],
                    "exchange": contract["exchange"],
                }
            )
        return rows

    def _build_universe(
        self,
        dates: list[date],
        rows_by_date: dict[date, list[dict[str, Any]]],
        contracts: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for index, trading_date in enumerate(dates):
            window_dates = dates[max(0, index - self.config.universe_lookback_days + 1) : index + 1]
            window_rows = [row for value in window_dates for row in rows_by_date[value]]
            products = sorted({row["product_code"] for row in rows_by_date[trading_date]})
            for product in products:
                product_window = [row for row in window_rows if row["product_code"] == product]
                current_rows = [
                    row for row in rows_by_date[trading_date] if row["product_code"] == product
                ]
                eligible_contracts = self._eligible_candidates(
                    current_rows,
                    contracts,
                    trading_date,
                )
                observed_days = len({row["trade_date"] for row in product_window})
                volume = sum(self._number(row.get("vol")) for row in product_window) / max(
                    observed_days, 1
                )
                open_interest = sum(self._number(row.get("oi")) for row in product_window) / max(
                    observed_days, 1
                )
                amount = sum(self._number(row.get("amount")) for row in product_window) / max(
                    observed_days, 1
                )
                reasons: list[str] = []
                if observed_days < self.config.universe_min_history_days:
                    reasons.append("insufficient_history")
                if len(eligible_contracts) < self.config.universe_min_contracts:
                    reasons.append("insufficient_safe_contracts")
                if volume < self.config.universe_minimum_daily_volume:
                    reasons.append("low_volume")
                if open_interest < self.config.universe_minimum_open_interest:
                    reasons.append("low_open_interest")
                if amount < self.config.universe_minimum_daily_amount:
                    reasons.append("low_amount")
                rules_complete = all(
                    self._number(contracts[row["ts_code"]].get("multiplier")) > 0
                    and bool(str(contracts[row["ts_code"]].get("trade_time_desc") or "").strip())
                    for row in eligible_contracts
                )
                if not rules_complete:
                    reasons.append("missing_contract_rules")
                records.append(
                    {
                        "trade_date": trading_date.isoformat(),
                        "product_code": product,
                        "exchange": current_rows[0]["exchange"],
                        "eligible": not reasons,
                        "status": "eligible" if not reasons else "excluded",
                        "exclusion_reasons": ";".join(reasons),
                        "history_days": observed_days,
                        "eligible_contracts": len(eligible_contracts),
                        "average_daily_volume": volume,
                        "average_open_interest": open_interest,
                        "average_daily_amount": amount,
                        "lookback_days": self.config.universe_lookback_days,
                    }
                )
        return records

    def _build_roll_schedule(
        self,
        dates: list[date],
        rows_by_date: dict[date, list[dict[str, Any]]],
        contracts: dict[str, dict[str, Any]],
        universe: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        universe_lookup = {
            (date.fromisoformat(row["trade_date"]), row["product_code"]): row for row in universe
        }
        current: dict[str, str] = {}
        challenger_state: dict[str, tuple[str, int]] = {}
        records: list[dict[str, Any]] = []
        for index, decision_date in enumerate(dates[:-1]):
            effective_date = dates[index + 1]
            product_rows: dict[str, list[dict[str, Any]]] = {}
            for row in rows_by_date[decision_date]:
                product_rows.setdefault(row["product_code"], []).append(row)
            for product, rows in sorted(product_rows.items()):
                candidates = self._eligible_candidates(rows, contracts, decision_date)
                if not candidates:
                    continue
                ranked = sorted(
                    candidates,
                    key=lambda row: (
                        self._number(row.get("oi")),
                        self._number(row.get("vol")),
                        contracts[row["ts_code"]]["expiry_date"] or date.max,
                        row["ts_code"],
                    ),
                    reverse=True,
                )
                previous = current.get(product)
                selected = previous
                reason = "hold"
                current_row = next(
                    (row for row in candidates if row["ts_code"] == previous),
                    None,
                )
                if previous is None:
                    selected = ranked[0]["ts_code"]
                    reason = "initial_selection"
                    challenger_state.pop(product, None)
                elif current_row is None:
                    selected = ranked[0]["ts_code"]
                    reason = "forced_expiry_or_missing"
                    challenger_state.pop(product, None)
                else:
                    challenger = ranked[0]
                    current_expiry = contracts[previous]["expiry_date"] or date.min
                    challenger_expiry = contracts[challenger["ts_code"]]["expiry_date"] or date.min
                    is_later = challenger_expiry > current_expiry
                    dominates = self._number(challenger.get("oi")) > self._number(
                        current_row.get("oi")
                    )
                    if challenger["ts_code"] != previous and is_later and dominates:
                        prior_code, prior_days = challenger_state.get(product, ("", 0))
                        confirmation_days = (
                            prior_days + 1 if prior_code == challenger["ts_code"] else 1
                        )
                        challenger_state[product] = (challenger["ts_code"], confirmation_days)
                        if confirmation_days >= self.config.roll_confirmation_days:
                            selected = challenger["ts_code"]
                            reason = "open_interest_confirmation"
                            challenger_state.pop(product, None)
                    else:
                        challenger_state.pop(product, None)
                if selected is None:
                    continue
                current[product] = selected
                selected_row = next(row for row in candidates if row["ts_code"] == selected)
                candidate_snapshot = [
                    {
                        "ts_code": row["ts_code"],
                        "vol": self._number(row.get("vol")),
                        "oi": self._number(row.get("oi")),
                        "expiry_date": self._iso(contracts[row["ts_code"]]["expiry_date"]),
                    }
                    for row in ranked
                ]
                snapshot_json = json.dumps(
                    candidate_snapshot,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                records.append(
                    {
                        "product_code": product,
                        "exchange": selected_row["exchange"],
                        "decision_date": decision_date.isoformat(),
                        "effective_date": effective_date.isoformat(),
                        "previous_contract": previous,
                        "selected_contract": selected,
                        "roll": previous is not None and previous != selected,
                        "reason": reason,
                        "selected_volume": self._number(selected_row.get("vol")),
                        "selected_open_interest": self._number(selected_row.get("oi")),
                        "candidate_count": len(candidates),
                        "universe_eligible": universe_lookup[(decision_date, product)]["eligible"],
                        "candidate_snapshot": snapshot_json,
                        "input_hash": hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
                    }
                )
        return records

    def _eligible_candidates(
        self,
        rows: list[dict[str, Any]],
        contracts: dict[str, dict[str, Any]],
        trading_date: date,
    ) -> list[dict[str, Any]]:
        cutoff = trading_date + timedelta(days=self.config.roll_expiry_buffer_calendar_days)
        result: list[dict[str, Any]] = []
        for row in rows:
            contract = contracts[row["ts_code"]]
            list_date = contract["list_date"]
            expiry_date = contract["expiry_date"]
            if list_date is not None and list_date > trading_date:
                continue
            if expiry_date is None or expiry_date <= cutoff:
                continue
            if self._number(row.get("settle")) <= 0:
                continue
            if self._number(row.get("vol")) < 0 or self._number(row.get("oi")) < 0:
                continue
            result.append(row)
        return result

    def _build_continuous(
        self,
        schedule: list[dict[str, Any]],
        rows_by_date: dict[date, list[dict[str, Any]]],
        dates: list[date],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        daily_lookup = {
            (row["trade_date"], row["ts_code"]): row
            for rows in rows_by_date.values()
            for row in rows
        }
        previous_date = {dates[index]: dates[index - 1] for index in range(1, len(dates))}
        research_prices: dict[str, float] = {}
        continuous_indices: dict[str, float] = {}
        seen_products: set[str] = set()
        records: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []
        ordered_schedule = sorted(
            schedule,
            key=lambda row: (row["effective_date"], row["product_code"]),
        )
        for mapping in ordered_schedule:
            effective_date = date.fromisoformat(mapping["effective_date"])
            contract_code = mapping["selected_contract"]
            row = daily_lookup.get((effective_date, contract_code))
            if row is None:
                issues.append(
                    self._issue(
                        "error",
                        "missing_selected_contract_price",
                        f"{contract_code} has no daily row on {effective_date}.",
                        1,
                    )
                )
                continue
            prior_row = daily_lookup.get((previous_date[effective_date], contract_code))
            actual_settle = self._number(row.get("settle"))
            continuous_return: float | None = None
            if prior_row is not None and self._number(prior_row.get("settle")) > 0:
                continuous_return = actual_settle / self._number(prior_row.get("settle")) - 1
            elif mapping["product_code"] in seen_products:
                issues.append(
                    self._issue(
                        "error",
                        "missing_previous_settlement",
                        f"{contract_code} lacks the prior settlement needed on {effective_date}.",
                        1,
                    )
                )
            product = mapping["product_code"]
            if product not in research_prices:
                research_price = actual_settle
                continuous_index = 1.0
            elif continuous_return is None:
                research_price = research_prices[product]
                continuous_index = continuous_indices[product]
            else:
                research_price = research_prices[product] * (1 + continuous_return)
                continuous_index = continuous_indices[product] * (1 + continuous_return)
            research_prices[product] = research_price
            continuous_indices[product] = continuous_index
            seen_products.add(product)
            records.append(
                {
                    "trade_date": effective_date.isoformat(),
                    "product_code": product,
                    "exchange": mapping["exchange"],
                    "contract_code": contract_code,
                    "decision_date": mapping["decision_date"],
                    "roll": mapping["roll"],
                    "roll_reason": mapping["reason"],
                    "actual_open": row.get("open"),
                    "actual_high": row.get("high"),
                    "actual_low": row.get("low"),
                    "actual_close": row.get("close"),
                    "actual_settle": actual_settle,
                    "continuous_return": continuous_return,
                    "continuous_index": continuous_index,
                    "research_price": research_price,
                    "adjustment_factor": research_price / actual_settle,
                }
            )
        return records, issues

    def _compare_vendor(
        self,
        schedule: list[dict[str, Any]],
        mappings: pl.DataFrame | None,
    ) -> pl.DataFrame:
        columns = [
            "trade_date",
            "product_code",
            "self_selected_contract",
            "vendor_contract",
            "matched",
        ]
        if mappings is None or mappings.is_empty():
            return pl.DataFrame({name: [] for name in columns})
        self._require_columns(
            mappings,
            {"ts_code", "trade_date", "mapping_ts_code"},
            "futures_mappings",
        )
        vendor: dict[tuple[str, str], str] = {}
        for row in mappings.iter_rows(named=True):
            product = str(row["ts_code"]).split(".", maxsplit=1)[0].upper()
            trading_date = self._parse_date(row["trade_date"])
            if trading_date is not None:
                vendor[(trading_date.isoformat(), product)] = str(row["mapping_ts_code"]).upper()
        records = []
        for row in schedule:
            vendor_contract = vendor.get((row["effective_date"], row["product_code"]))
            if vendor_contract is None:
                continue
            records.append(
                {
                    "trade_date": row["effective_date"],
                    "product_code": row["product_code"],
                    "self_selected_contract": row["selected_contract"],
                    "vendor_contract": vendor_contract,
                    "matched": row["selected_contract"] == vendor_contract,
                }
            )
        return self._frame(records, columns)

    def _input_quality_issues(self, daily: pl.DataFrame) -> list[dict[str, Any]]:
        duplicates = int(daily.select("ts_code", "trade_date").is_duplicated().sum())
        if not duplicates:
            return []
        return [
            self._issue(
                "error",
                "duplicate_contract_date",
                "Input daily data contains duplicate contract-date rows.",
                duplicates,
            )
        ]

    def _quality_issues(
        self,
        schedule: list[dict[str, Any]],
        continuous: list[dict[str, Any]],
        universe: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        checks = (
            (schedule, ("product_code", "effective_date"), "duplicate_roll_date"),
            (continuous, ("product_code", "trade_date"), "duplicate_continuous_date"),
            (universe, ("product_code", "trade_date"), "duplicate_universe_date"),
        )
        for rows, keys, code in checks:
            values = [tuple(row[key] for key in keys) for row in rows]
            duplicates = len(values) - len(set(values))
            if duplicates:
                issues.append(
                    self._issue(
                        "error",
                        code,
                        f"Found {duplicates} duplicate rows.",
                        duplicates,
                    )
                )
        future_rows = sum(
            date.fromisoformat(row["decision_date"]) >= date.fromisoformat(row["effective_date"])
            for row in schedule
        )
        if future_rows:
            issues.append(
                self._issue(
                    "error",
                    "future_decision_date",
                    "Roll decisions must precede their effective dates.",
                    future_rows,
                )
            )
        jumps = sum(
            row["continuous_return"] is not None
            and abs(row["continuous_return"]) > self.config.continuous_max_abs_return
            for row in continuous
        )
        if jumps:
            issues.append(
                self._issue(
                    "warning",
                    "large_continuous_return",
                    "Continuous returns exceeded the configured jump threshold.",
                    jumps,
                )
            )
        return issues

    @staticmethod
    def _frame(
        records: list[dict[str, Any]],
        columns: list[str] | None = None,
    ) -> pl.DataFrame:
        if records:
            return pl.DataFrame(records, infer_schema_length=None, strict=False)
        return pl.DataFrame({name: [] for name in (columns or [])})

    @staticmethod
    def _issue(severity: str, code: str, message: str, rows: int) -> dict[str, Any]:
        return {"severity": severity, "code": code, "message": message, "rows": rows}

    @staticmethod
    def _number(value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return result if result == result else 0.0

    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if value is None:
            return None
        text = str(value).strip().replace("-", "")
        if len(text) != 8 or not text.isdigit():
            return None
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))

    @staticmethod
    def _iso(value: date | None) -> str | None:
        return value.isoformat() if value else None
