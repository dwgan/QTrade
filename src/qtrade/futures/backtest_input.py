from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from qtrade.futures.trend import FuturesTrendProtocol
from qtrade.research.protocols import PartitionName, ProtocolStatus, StrategyProtocol


@dataclass(frozen=True)
class FuturesBacktestInputBuildResult:
    build_id: str
    output_dir: Path
    input_path: Path
    manifest_path: Path
    report_path: Path
    day_rows: int
    target_rows: int
    roll_rows: int
    deferred_resize_rows: int
    data_version: str
    reused: bool = False


class FuturesBacktestInputCompiler:
    def __init__(self, curated_root: Path, reports_root: Path) -> None:
        self.curated_root = Path(curated_root)
        self.reports_root = Path(reports_root)

    def build(self, request_path: Path) -> FuturesBacktestInputBuildResult:
        request_path = Path(request_path).resolve()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        protocol_path = Path(request["protocol_path"])
        if not protocol_path.is_absolute():
            protocol_path = (request_path.parent / protocol_path).resolve()
        protocol = self._protocol(protocol_path)
        scenario = self._scenario(protocol, str(request.get("scenario", "baseline")))
        research_build_id = str(request.get("research_build_id", "")).strip()
        terminal_signal_build_id = str(request.get("terminal_signal_build_id", "")).strip()
        if not research_build_id or not terminal_signal_build_id:
            raise ValueError(
                "Backtest input compilation requires research and terminal signal IDs."
            )
        initial_equity = self._positive(request.get("initial_equity"), "initial_equity")
        partition = PartitionName(str(request.get("partition", PartitionName.DEVELOPMENT.value)))
        chain, chain_versions = self._signal_chain(
            terminal_signal_build_id,
            research_build_id,
            self._expected_signal_protocol_id(protocol, scenario),
        )
        self._validate_partition(chain, protocol, partition)
        research_dir, research_manifest, research_versions = self._research_build(research_build_id)
        self._validate_roll_scenario(protocol, scenario, research_manifest)
        provider = str(research_manifest.get("provider", "")).strip()
        if not provider:
            raise ValueError("Futures research manifest requires provider.")
        contract_partition = date.fromisoformat(
            str(research_manifest["contract_master_partition_date"])
        )
        contract_path = self._partition_path("futures_contracts", provider, contract_partition)
        contracts = self._read_required(contract_path, "contract master")
        self._verify_declared_source(research_manifest, contract_path)
        delay_days = int(scenario["execution_delay_days"])
        cost_multiplier = self._positive(scenario["cost_multiplier"], "cost_multiplier")
        margin_multiplier = self._positive(scenario["margin_multiplier"], "margin_multiplier")
        available_dates = self._available_dates("futures_daily", provider)
        self._validate_complete_partition(chain, protocol, partition, available_dates)
        trade_dates = self._required_trade_dates(chain, available_dates, delay_days)
        market_paths = [
            self._partition_path(dataset, provider, trading_date)
            for trading_date in trade_dates
            for dataset in (
                "futures_daily",
                "futures_settlements",
                "futures_limits",
            )
        ]
        for path in market_paths:
            if not path.is_file():
                raise FileNotFoundError(f"Required futures backtest partition not found: {path}")
        days, counts = self._compile_days(
            chain,
            trade_dates,
            provider,
            contracts,
            delay_days,
            cost_multiplier,
            margin_multiplier,
        )
        versions = [
            self._file_version(request_path, request_path.parent),
            self._file_version(protocol_path, protocol_path.parent),
            *chain_versions,
            *research_versions,
            self._file_version(contract_path, self.curated_root),
            *(self._file_version(path, self.curated_root) for path in market_paths),
        ]
        build_payload = {
            "schema_version": 2,
            "protocol_id": protocol.protocol_id,
            "protocol_hash": protocol.content_hash,
            "partition": partition.value,
            "scenario": scenario,
            "research_build_id": research_build_id,
            "terminal_signal_build_id": terminal_signal_build_id,
            "initial_equity": initial_equity,
            "inputs": versions,
        }
        data_version = hashlib.sha256(
            self._canonical(
                {
                    "inputs": sorted(
                        (item for item in versions if str(item["path"]).startswith("futures/")),
                        key=lambda item: (item["path"], item["sha256"]),
                    )
                }
            ).encode("utf-8")
        ).hexdigest()
        build_id = hashlib.sha256(self._canonical(build_payload).encode("utf-8")).hexdigest()[:20]
        output_dir = self.curated_root / "futures" / "backtest-inputs" / f"build_id={build_id}"
        input_path = output_dir / "input.json"
        manifest_path = output_dir / "manifest.json"
        report_path = self.reports_root / "futures" / "backtest-inputs" / build_id / "report.md"
        if output_dir.exists():
            if not input_path.is_file() or not manifest_path.is_file():
                raise RuntimeError(f"Incomplete immutable backtest input exists: {output_dir}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self._verify_output(manifest, input_path)
            return self._result(manifest, output_dir, input_path, manifest_path, report_path, True)
        payload = {
            "research_build_id": research_build_id,
            "initial_equity": initial_equity,
            "days": days,
        }
        manifest = {
            **build_payload,
            "build_id": build_id,
            "passed": True,
            "data_version": data_version,
            "rows": {"days": len(days), **counts},
        }
        self._write_immutable(output_dir, payload, manifest)
        self._write_report(report_path, manifest)
        return self._result(manifest, output_dir, input_path, manifest_path, report_path, False)

    def _signal_chain(
        self,
        terminal_build_id: str,
        research_build_id: str,
        expected_signal_protocol_id: str,
    ) -> tuple[list[tuple[dict[str, Any], pl.DataFrame]], list[dict[str, Any]]]:
        reversed_chain: list[tuple[dict[str, Any], pl.DataFrame]] = []
        versions: list[dict[str, Any]] = []
        seen: set[str] = set()
        build_id: str | None = terminal_build_id
        while build_id:
            if build_id in seen:
                raise ValueError("Futures signal chain contains a cycle.")
            seen.add(build_id)
            directory = self.curated_root / "futures" / "signals" / f"build_id={build_id}"
            manifest_path = directory / "manifest.json"
            targets_path = directory / "targets.parquet"
            for path in (manifest_path, targets_path):
                if not path.is_file():
                    raise FileNotFoundError(f"Futures signal chain input not found: {path}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("build_id") != build_id or not manifest.get("passed"):
                raise ValueError(f"Invalid futures signal build: {build_id}")
            if manifest.get("research_build_id") != research_build_id:
                raise ValueError("Futures signal chain changed research build.")
            if manifest.get("protocol_id") != expected_signal_protocol_id:
                raise ValueError("Futures signal chain does not match the frozen strategy config.")
            self._verify_output(manifest, targets_path)
            reversed_chain.append((manifest, pl.read_parquet(targets_path)))
            versions.extend(
                (
                    self._file_version(manifest_path, self.curated_root),
                    self._file_version(targets_path, self.curated_root),
                )
            )
            build_id = manifest.get("previous_signal_build_id")
        chain = list(reversed(reversed_chain))
        for previous, current in zip(chain, chain[1:], strict=False):
            if previous[0]["eligible_date"] != current[0]["signal_date"]:
                raise ValueError("Futures signal chain dates are not contiguous.")
        return chain, versions

    def _research_build(self, build_id: str) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
        directory = self.curated_root / "futures" / "research" / f"build_id={build_id}"
        manifest_path = directory / "manifest.json"
        roll_path = directory / "roll_schedule.parquet"
        for path in (manifest_path, roll_path):
            if not path.is_file():
                raise FileNotFoundError(f"Futures research input not found: {path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("build_id") != build_id or not manifest.get("passed"):
            raise ValueError("Referenced futures research build is invalid.")
        self._verify_output(manifest, roll_path)
        return (
            directory,
            manifest,
            [
                self._file_version(manifest_path, self.curated_root),
                self._file_version(roll_path, self.curated_root),
            ],
        )

    @staticmethod
    def _validate_partition(
        chain: list[tuple[dict[str, Any], pl.DataFrame]],
        protocol: StrategyProtocol,
        partition: PartitionName,
    ) -> None:
        expected = protocol.partition(partition)
        if expected.end_date is None:
            raise ValueError("Forward partition cannot be compiled as a historical backtest.")
        first = date.fromisoformat(chain[0][0]["signal_date"])
        last = date.fromisoformat(chain[-1][0]["eligible_date"])
        if first < expected.start_date or last > expected.end_date:
            raise ValueError(
                f"Signal chain falls outside frozen {partition.value} partition: "
                f"{expected.start_date} to {expected.end_date}."
            )

    def _required_trade_dates(
        self,
        chain: list[tuple[dict[str, Any], pl.DataFrame]],
        available_dates: list[date],
        delay_days: int,
    ) -> list[date]:
        first = date.fromisoformat(chain[0][0]["signal_date"])
        terminal = date.fromisoformat(chain[-1][0]["eligible_date"])
        eligible_index = self._date_index(available_dates, terminal)
        final_index = eligible_index + delay_days
        if final_index + 1 >= len(available_dates):
            raise ValueError("Daily data lacks the post-terminal date required for settlement.")
        last_required = available_dates[final_index + 1]
        return [value for value in available_dates if first <= value <= last_required]

    @staticmethod
    def _validate_complete_partition(
        chain: list[tuple[dict[str, Any], pl.DataFrame]],
        protocol: StrategyProtocol,
        partition: PartitionName,
        available_dates: list[date],
    ) -> None:
        expected = protocol.partition(partition)
        if expected.end_date is None:
            raise ValueError("Historical partition requires an end date.")
        partition_dates = [
            value for value in available_dates if expected.start_date <= value <= expected.end_date
        ]
        if len(partition_dates) < 2:
            raise ValueError(f"No complete daily coverage for frozen {partition.value} partition.")
        chain_dates = [
            date.fromisoformat(chain[0][0]["signal_date"]),
            *(date.fromisoformat(item[0]["eligible_date"]) for item in chain),
        ]
        if chain_dates != partition_dates:
            raise ValueError(
                f"Signal chain must match every archived {partition.value} trading date."
            )

    def _compile_days(
        self,
        chain: list[tuple[dict[str, Any], pl.DataFrame]],
        trade_dates: list[date],
        provider: str,
        contracts: pl.DataFrame,
        delay_days: int,
        cost_multiplier: float,
        margin_multiplier: float,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        snapshots = {
            date.fromisoformat(manifest["signal_date"]): (manifest, frame)
            for manifest, frame in chain
        }
        contract_rows = self._contract_rows(contracts)
        previous_targets: dict[str, dict[str, Any]] = {}
        days: list[dict[str, Any]] = []
        target_count = 0
        roll_count = 0
        deferred_count = 0
        for index, trading_date in enumerate(trade_dates[:-1]):
            next_date = trade_dates[index + 1]
            daily = self._read_required(
                self._partition_path("futures_daily", provider, trading_date), "daily"
            )
            settlements = self._read_required(
                self._partition_path("futures_settlements", provider, trading_date),
                "settlements",
            )
            limits = self._read_required(
                self._partition_path("futures_limits", provider, trading_date), "limits"
            )
            daily_rows = self._dated_rows(daily, trading_date, "daily")
            settlement_rows = self._dated_rows(settlements, trading_date, "settlements")
            limit_rows = self._dated_rows(limits, trading_date, "limits")
            codes = sorted(set(daily_rows) & set(settlement_rows) & set(limit_rows))
            bars = [self._bar(code, daily_rows[code], limit_rows[code]) for code in codes]
            marks = [
                self._mark(
                    code,
                    settlement_rows[code],
                    previous_targets,
                    margin_multiplier,
                )
                for code in codes
            ]
            raw_day: dict[str, Any] = {
                "trade_date": trading_date.isoformat(),
                "next_trade_date": next_date.isoformat(),
                "bars": bars,
                "settlements": marks,
                "targets": [],
                "rolls": [],
                "liquidation_priority": [],
            }
            snapshot = snapshots.get(trading_date)
            if snapshot is not None:
                manifest, frame = snapshot
                desired = {str(row["product_code"]): row for row in frame.to_dicts()}
                selection_date = date.fromisoformat(manifest["eligible_date"])
                selection_index = self._date_index(trade_dates, selection_date)
                eligible_index = selection_index + delay_days
                if eligible_index >= len(trade_dates):
                    raise ValueError("Compiled dates do not cover delayed target eligibility.")
                target_eligible_date = trade_dates[eligible_index]
                raw_day["target_selection_date"] = selection_date.isoformat()
                raw_day["target_eligible_date"] = target_eligible_date.isoformat()
                raw_day["rebalance_id"] = f"trend-{manifest['build_id']}"
                for product, row in sorted(desired.items()):
                    current = previous_targets.get(product)
                    desired_lots = int(row["target_signed_lots"])
                    desired_code = str(row["contract_code"]).strip().upper()
                    if (
                        current is not None
                        and int(current["target_signed_lots"]) != 0
                        and str(current["contract_code"]).strip().upper() != desired_code
                    ):
                        roll = self._roll(
                            product,
                            current,
                            row,
                            trading_date,
                            next_date,
                            contract_rows,
                            settlement_rows,
                            cost_multiplier,
                        )
                        raw_day["rolls"].append(roll)
                        roll_count += 1
                        if desired_lots != int(current["target_signed_lots"]):
                            deferred_count += 1
                    else:
                        raw_day["targets"].append(
                            self._target(
                                product,
                                row,
                                trading_date,
                                contract_rows,
                                provider,
                                cost_multiplier,
                            )
                        )
                        target_count += 1
                previous_targets = desired
            raw_day["liquidation_priority"] = [
                self._liquidation(
                    row,
                    contract_rows,
                    settlement_rows,
                    cost_multiplier,
                )
                for row in previous_targets.values()
                if int(row["target_signed_lots"]) != 0
                and str(row["contract_code"]).strip().upper() in settlement_rows
            ]
            days.append(raw_day)
        return days, {
            "targets": target_count,
            "rolls": roll_count,
            "deferred_resizes": deferred_count,
        }

    def _target(
        self,
        product: str,
        row: dict[str, Any],
        signal_date: date,
        contracts: dict[str, dict[str, Any]],
        provider: str,
        cost_multiplier: float,
    ) -> dict[str, Any]:
        code = str(row["contract_code"]).strip().upper()
        contract = self._one_contract(contracts, code)
        settlement = self._settlement_row(provider, signal_date, code)
        return {
            "product_code": product,
            "contract_code": code,
            "signed_lots": int(row["target_signed_lots"]),
            "multiplier": self._positive(contract.get("multiplier"), f"multiplier for {code}"),
            "tick_size": self._positive(contract.get("per_unit"), f"tick size for {code}"),
            "fees": self._fees(settlement, cost_multiplier),
        }

    def _roll(
        self,
        product: str,
        current: dict[str, Any],
        desired: dict[str, Any],
        signal_date: date,
        eligible_date: date,
        contracts: dict[str, dict[str, Any]],
        settlements: dict[str, dict[str, Any]],
        cost_multiplier: float,
    ) -> dict[str, Any]:
        old_code = str(current["contract_code"]).strip().upper()
        new_code = str(desired["contract_code"]).strip().upper()
        old_lots = int(current["target_signed_lots"])
        old_contract = self._one_contract(contracts, old_code)
        new_contract = self._one_contract(contracts, new_code)
        old_multiplier = self._positive(old_contract.get("multiplier"), "old multiplier")
        new_multiplier = self._positive(new_contract.get("multiplier"), "new multiplier")
        old_tick = self._positive(old_contract.get("per_unit"), "old tick size")
        new_tick = self._positive(new_contract.get("per_unit"), "new tick size")
        if old_multiplier != new_multiplier or old_tick != new_tick:
            raise ValueError(f"Roll contract specifications differ for {product}.")
        old_settlement = settlements.get(old_code)
        new_settlement = settlements.get(new_code)
        if old_settlement is None or new_settlement is None:
            raise ValueError(f"Missing roll settlement inputs for {product} on {signal_date}.")
        return {
            "roll_id": f"roll-{product}-{signal_date.isoformat()}",
            "product_code": product,
            "old_contract": old_code,
            "new_contract": new_code,
            "position_side": "buy" if old_lots > 0 else "sell",
            "lots": abs(old_lots),
            "multiplier": old_multiplier,
            "tick_size": old_tick,
            "close_fees": self._fees(old_settlement, cost_multiplier),
            "open_fees": self._fees(new_settlement, cost_multiplier),
        }

    def _liquidation(
        self,
        row: dict[str, Any],
        contracts: dict[str, dict[str, Any]],
        settlements: dict[str, dict[str, Any]],
        cost_multiplier: float,
    ) -> dict[str, Any]:
        code = str(row["contract_code"]).strip().upper()
        contract = self._one_contract(contracts, code)
        return {
            "contract_code": code,
            "multiplier": self._positive(contract.get("multiplier"), f"multiplier for {code}"),
            "tick_size": self._positive(contract.get("per_unit"), f"tick size for {code}"),
            "fees": self._fees(settlements[code], cost_multiplier),
        }

    @staticmethod
    def _bar(code: str, daily: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract_code": code,
            "open": daily.get("open"),
            "high": daily.get("high"),
            "low": daily.get("low"),
            "volume": daily.get("vol"),
            "up_limit": limits.get("up_limit"),
            "down_limit": limits.get("down_limit"),
        }

    def _mark(
        self,
        code: str,
        settlement: dict[str, Any],
        targets: dict[str, dict[str, Any]],
        margin_multiplier: float,
    ) -> dict[str, Any]:
        signed_lots = next(
            (
                int(row["target_signed_lots"])
                for row in targets.values()
                if str(row["contract_code"]).strip().upper() == code
            ),
            1,
        )
        return {
            "contract_code": code,
            "settlement_price": settlement.get("settle"),
            "long_margin_rate": self._margin_rate(
                settlement.get("long_margin_rate"), margin_multiplier
            ),
            "short_margin_rate": self._margin_rate(
                settlement.get("short_margin_rate"), margin_multiplier
            ),
            "position_direction": "long" if signed_lots >= 0 else "short",
        }

    @staticmethod
    def _fees(settlement: dict[str, Any], multiplier: float) -> dict[str, Any]:
        rule = {
            "per_lot": float(settlement.get("trading_fee") or 0) * multiplier,
            "notional_rate": float(settlement.get("trading_fee_rate") or 0) * multiplier,
        }
        return {"open": rule, "close": rule, "close_today": rule, "close_yesterday": rule}

    def _settlement_row(self, provider: str, trading_date: date, code: str) -> dict[str, Any]:
        frame = self._read_required(
            self._partition_path("futures_settlements", provider, trading_date),
            "settlements",
        )
        rows = self._dated_rows(frame, trading_date, "settlements")
        if code not in rows:
            raise ValueError(f"Missing settlement row for {code} on {trading_date}.")
        return rows[code]

    @staticmethod
    def _contract_rows(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for row in frame.to_dicts():
            code = str(row.get("ts_code", "")).strip().upper()
            if code in rows:
                raise ValueError(f"Duplicate contract master row: {code}")
            rows[code] = row
        return rows

    @staticmethod
    def _one_contract(rows: dict[str, dict[str, Any]], code: str) -> dict[str, Any]:
        if code not in rows:
            raise ValueError(f"Missing contract master row: {code}")
        return rows[code]

    def _dated_rows(
        self, frame: pl.DataFrame, trading_date: date, description: str
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in frame.to_dicts():
            code = str(row.get("ts_code", "")).strip().upper()
            row_date = self._compact_date(row.get("trade_date"))
            if row_date != trading_date:
                continue
            if code in result:
                raise ValueError(f"Duplicate {description} row for {code} on {trading_date}.")
            result[code] = row
        return result

    def _available_dates(self, dataset: str, provider: str) -> list[date]:
        directory = self.curated_root / "futures" / dataset / f"provider={provider}"
        dates = []
        for path in directory.glob("as_of_date=*") if directory.exists() else ():
            try:
                value = date.fromisoformat(path.name.removeprefix("as_of_date="))
            except ValueError:
                continue
            if (path / "data.parquet").is_file():
                dates.append(value)
        return sorted(dates)

    @staticmethod
    def _date_index(dates: list[date], value: date) -> int:
        try:
            return dates.index(value)
        except ValueError as error:
            raise ValueError(f"Required futures trading date is unavailable: {value}") from error

    def _partition_path(self, dataset: str, provider: str, trading_date: date) -> Path:
        return (
            self.curated_root
            / "futures"
            / dataset
            / f"provider={provider}"
            / f"as_of_date={trading_date.isoformat()}"
            / "data.parquet"
        )

    @staticmethod
    def _read_required(path: Path, description: str) -> pl.DataFrame:
        if not path.is_file():
            raise FileNotFoundError(f"Required {description} input not found: {path}")
        return pl.read_parquet(path)

    @staticmethod
    def _protocol(path: Path) -> StrategyProtocol:
        protocol = StrategyProtocol.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if protocol.status != ProtocolStatus.FROZEN:
            raise ValueError("Futures formal validation requires a frozen protocol.")
        if protocol.content_hash != protocol.calculated_hash():
            raise ValueError("Frozen futures protocol hash mismatch.")
        return protocol

    @staticmethod
    def _scenario(protocol: StrategyProtocol, name: str) -> dict[str, Any]:
        matches = [item for item in protocol.execution.get("scenarios", []) if item["name"] == name]
        if len(matches) != 1:
            raise ValueError(f"Unknown or duplicate frozen futures scenario: {name}")
        return matches[0]

    @staticmethod
    def _expected_signal_protocol_id(
        protocol: StrategyProtocol,
        scenario: dict[str, Any],
    ) -> str:
        multiplier = float(scenario.get("lookback_multiplier", 1.0))
        volatility_lookback = int(scenario.get("volatility_lookback", 40))
        if multiplier == 1.0 and volatility_lookback == 40:
            return protocol.config_hash
        baseline = FuturesTrendProtocol()
        lookbacks = tuple(round(value * multiplier) for value in baseline.lookbacks)
        return FuturesTrendProtocol(
            lookbacks=lookbacks,
            volatility_lookback=volatility_lookback,
        ).protocol_id

    @staticmethod
    def _validate_roll_scenario(
        protocol: StrategyProtocol,
        scenario: dict[str, Any],
        research_manifest: dict[str, Any],
    ) -> None:
        delta = int(scenario.get("roll_confirmation_days_delta", 0))
        if delta == 0:
            return
        baseline = int(protocol.execution["roll_confirmation_days"])
        actual = research_manifest.get("config", {}).get("roll_confirmation_days")
        if actual != baseline + delta:
            raise ValueError(
                "Roll robustness scenario requires a separately built research "
                f"artifact with roll_confirmation_days={baseline + delta}."
            )

    @staticmethod
    def _margin_rate(value: Any, multiplier: float) -> float:
        rate = float(value) * multiplier
        if not math.isfinite(rate) or not 0 < rate < 1:
            raise ValueError("Stressed futures margin rate must be finite and in (0, 1).")
        return rate

    @staticmethod
    def _positive(value: Any, name: str) -> float:
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"{name} must be finite and positive.")
        return number

    @staticmethod
    def _compact_date(value: Any) -> date | None:
        text = str(value or "").replace("-", "")
        if len(text) != 8 or not text.isdigit():
            return None
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))

    @staticmethod
    def _file_version(path: Path, base: Path) -> dict[str, Any]:
        return {
            "path": path.relative_to(base).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }

    @staticmethod
    def _verify_output(manifest: dict[str, Any], path: Path) -> None:
        matches = [
            item for item in manifest.get("output_versions", []) if item.get("path") == path.name
        ]
        if len(matches) != 1:
            raise ValueError(f"Manifest does not declare output hash: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if matches[0].get("sha256") != digest or matches[0].get("size") != path.stat().st_size:
            raise ValueError(f"Immutable input changed after build: {path}")

    def _verify_declared_source(self, manifest: dict[str, Any], path: Path) -> None:
        relative = path.relative_to(self.curated_root).as_posix()
        matches = [item for item in manifest.get("inputs", []) if item.get("path") == relative]
        if len(matches) != 1:
            raise ValueError(f"Research manifest does not declare source input: {relative}")
        actual = self._file_version(path, self.curated_root)
        if matches[0].get("sha256") != actual["sha256"] or matches[0].get("size") != actual["size"]:
            raise ValueError(f"Research source input changed after build: {relative}")

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _write_immutable(
        self,
        output_dir: Path,
        payload: dict[str, Any],
        manifest: dict[str, Any],
    ) -> None:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
        temporary.mkdir()
        try:
            input_path = temporary / "input.json"
            self._atomic_json(input_path, payload)
            manifest["output_versions"] = [
                {
                    "path": input_path.name,
                    "sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
                    "size": input_path.stat().st_size,
                }
            ]
            self._atomic_json(temporary / "manifest.json", manifest)
            os.replace(temporary, output_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _write_report(self, path: Path, manifest: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(
            [
                "# Futures Backtest Input Compilation",
                "",
                f"- Build ID: `{manifest['build_id']}`",
                f"- Protocol: `{manifest['protocol_id']}`",
                f"- Partition: {manifest['partition']}",
                f"- Scenario: {manifest['scenario']['name']}",
                f"- Days: {manifest['rows']['days']}",
                f"- Targets: {manifest['rows']['targets']}",
                f"- Rolls: {manifest['rows']['rolls']}",
                f"- Deferred roll-day resizes: {manifest['rows']['deferred_resizes']}",
                "",
            ]
        )
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    @staticmethod
    def _result(
        manifest: dict[str, Any],
        output_dir: Path,
        input_path: Path,
        manifest_path: Path,
        report_path: Path,
        reused: bool,
    ) -> FuturesBacktestInputBuildResult:
        return FuturesBacktestInputBuildResult(
            build_id=manifest["build_id"],
            output_dir=output_dir,
            input_path=input_path,
            manifest_path=manifest_path,
            report_path=report_path,
            day_rows=manifest["rows"]["days"],
            target_rows=manifest["rows"]["targets"],
            roll_rows=manifest["rows"]["rolls"],
            deferred_resize_rows=manifest["rows"]["deferred_resizes"],
            data_version=manifest["data_version"],
            reused=reused,
        )
