from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from qtrade.futures.execution import (
    FuturesDailyExecutionBar,
    FuturesDailyExecutionEngine,
    FuturesExecutionStatus,
    FuturesFeeRule,
    FuturesOrder,
)
from qtrade.futures.portfolio import FuturesOffset, FuturesSide
from qtrade.research.protocols import PartitionName, ProtocolStatus, StrategyProtocol

BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class FuturesShadowObservationResult:
    build_id: str
    output_dir: Path
    output_path: Path
    manifest_path: Path
    report_path: Path
    observation_date: date
    execution_rows: int
    blocked_rows: int
    fully_executable: bool
    reused: bool = False


class FuturesShadowObservationService:
    OUTPUT_FILE = "executions.parquet"
    OUTPUT_SCHEMA = {
        "product_code": pl.String,
        "contract_code": pl.String,
        "leg": pl.String,
        "side": pl.String,
        "offset": pl.String,
        "lots": pl.Int64,
        "status": pl.String,
        "reason": pl.String,
        "fill_price": pl.Float64,
        "theoretical_fee": pl.Float64,
        "required_margin": pl.Float64,
        "limit_status": pl.String,
        "fee_actual_status": pl.String,
        "is_roll": pl.Boolean,
    }

    def __init__(self, curated_root: Path, reports_root: Path) -> None:
        self.curated_root = Path(curated_root).resolve()
        self.reports_root = Path(reports_root)

    def build(self, request_path: Path) -> FuturesShadowObservationResult:
        request_path = Path(request_path).resolve()
        request = self._read_json(request_path, "shadow observation request")
        protocol_path = Path(str(request.get("protocol_path", "")))
        if not protocol_path.is_absolute():
            protocol_path = (request_path.parent / protocol_path).resolve()
        protocol = self._protocol(protocol_path)
        signal_build_id = self._build_id(request.get("signal_build_id"), "signal")
        observation_date = self._date(request.get("observation_date"), "observation_date")

        signal_dir = self._build_dir("signals", signal_build_id)
        signal_manifest_path = signal_dir / "manifest.json"
        targets_path = signal_dir / "targets.parquet"
        signal = self._read_json(signal_manifest_path, "signal manifest")
        if signal.get("build_id") != signal_build_id or not signal.get("passed"):
            raise ValueError("Shadow observation requires a passed immutable signal build.")
        self._verify_output(signal, targets_path)
        signal_date = self._date(signal.get("signal_date"), "signal_date")
        eligible_date = self._date(signal.get("eligible_date"), "eligible_date")
        if observation_date != eligible_date:
            raise ValueError("Shadow observation date must equal signal eligible_date.")
        if signal.get("protocol_id") != protocol.config_hash:
            raise ValueError("Shadow signal does not match the frozen formal protocol config.")
        forward = protocol.partition(PartitionName.FORWARD)
        if signal_date < forward.start_date:
            raise ValueError("Shadow signal date precedes the frozen forward partition.")

        targets = pl.read_parquet(targets_path)
        target_rows = self._target_rows(targets, "current signal")
        previous_id = str(signal.get("previous_signal_build_id") or "")
        previous_rows, previous_versions = self._previous_targets(previous_id, signal)

        research_id = self._build_id(signal.get("research_build_id"), "research")
        research_manifest_path = self._build_dir("research", research_id) / "manifest.json"
        research = self._read_json(research_manifest_path, "research manifest")
        if research.get("build_id") != research_id or not research.get("passed"):
            raise ValueError("Shadow signal references an invalid research build.")
        provider = str(research.get("provider", "")).strip()
        if not provider:
            raise ValueError("Shadow research manifest requires a provider.")

        contract_path, contract_version = self._declared_contract_master(signal)
        market_paths = {
            dataset: self._partition_path(dataset, provider, observation_date)
            for dataset in (
                "futures_daily",
                "futures_settlements",
                "futures_limits",
            )
        }
        for dataset, path in market_paths.items():
            if not path.is_file():
                raise FileNotFoundError(
                    f"Required shadow observation {dataset} partition not found."
                )

        daily = self._dated_rows(
            pl.read_parquet(market_paths["futures_daily"]),
            observation_date,
            "daily",
        )
        settlements = self._dated_rows(
            pl.read_parquet(market_paths["futures_settlements"]),
            observation_date,
            "settlements",
        )
        limits = self._dated_rows(
            pl.read_parquet(market_paths["futures_limits"]),
            observation_date,
            "limits",
        )
        contracts = self._contract_rows(pl.read_parquet(contract_path))
        self._validate_target_coverage(
            target_rows,
            contracts,
            daily,
            settlements,
            limits,
        )
        equity = self._positive(signal.get("equity"), "signal equity")
        executions = self._executions(
            signal_date,
            eligible_date,
            equity,
            target_rows,
            previous_rows,
            contracts,
            daily,
            settlements,
            limits,
        )
        observed_margin = self._observed_margin(
            target_rows,
            contracts,
            settlements,
        )
        planned_margin = self._non_negative(signal.get("initial_margin"), "planned margin")
        theoretical_fee = round(
            sum(float(row["theoretical_fee"] or 0) for row in executions),
            12,
        )
        blocked_rows = sum(row["status"] != FuturesExecutionStatus.FILLED for row in executions)
        fully_executable = blocked_rows == 0

        input_versions = [
            self._file_version(request_path, request_path.parent),
            self._file_version(protocol_path, protocol_path.parent),
            self._file_version(signal_manifest_path, self.curated_root),
            self._file_version(targets_path, self.curated_root),
            self._file_version(research_manifest_path, self.curated_root),
            contract_version,
            *(self._file_version(path, self.curated_root) for path in market_paths.values()),
            *previous_versions,
        ]
        payload = {
            "schema_version": 1,
            "protocol_id": protocol.protocol_id,
            "protocol_hash": protocol.content_hash,
            "signal_protocol_id": signal.get("protocol_id"),
            "signal_build_id": signal_build_id,
            "previous_signal_build_id": previous_id or None,
            "research_build_id": research_id,
            "signal_date": signal_date.isoformat(),
            "observation_date": observation_date.isoformat(),
            "availability_basis": "exact_date_archived_partitions",
            "night_session_status": "not_observable_from_daily_data",
            "broker_execution_status": "not_connected",
            "fee_actual_status": "broker_actual_unavailable",
            "inputs": input_versions,
        }
        build_id = hashlib.sha256(self._canonical(payload).encode("utf-8")).hexdigest()[:20]
        output_dir = self._build_dir("shadow-observations", build_id)
        output_path = output_dir / self.OUTPUT_FILE
        manifest_path = output_dir / "manifest.json"
        report_path = (
            self.reports_root
            / "futures"
            / "shadow-observations"
            / observation_date.isoformat()
            / build_id
            / "report.md"
        )
        if output_dir.exists():
            manifest = self._read_json(manifest_path, "shadow observation manifest")
            self._verify_output(manifest, output_path)
            return self._result(manifest, output_dir, output_path, manifest_path, report_path, True)

        manifest = {
            **payload,
            "build_id": build_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "passed": True,
            "fully_executable": fully_executable,
            "rows": {"executions": len(executions)},
            "blocked_execution_rows": blocked_rows,
            "roll_leg_rows": sum(bool(row["is_roll"]) for row in executions),
            "theoretical_fee": theoretical_fee,
            "planned_initial_margin": planned_margin,
            "observed_initial_margin": observed_margin,
            "initial_margin_deviation": round(observed_margin - planned_margin, 12),
            "observed_stress_margin": round(
                observed_margin
                * self._positive(
                    protocol.strategy.get("stress_margin_multiplier"),
                    "stress margin multiplier",
                ),
                12,
            ),
        }
        stress_limit = equity * self._positive(
            protocol.strategy.get("stress_margin_fraction"),
            "stress margin fraction",
        )
        manifest["stress_margin_policy_passed"] = (
            manifest["observed_stress_margin"] <= stress_limit
        )
        self._write_immutable(output_dir, executions, manifest)
        self._write_report(report_path, manifest)
        return self._result(manifest, output_dir, output_path, manifest_path, report_path, False)

    def _executions(
        self,
        signal_date: date,
        eligible_date: date,
        equity: float,
        targets: dict[str, dict[str, Any]],
        previous: dict[str, dict[str, Any]],
        contracts: dict[str, dict[str, Any]],
        daily: dict[str, dict[str, Any]],
        settlements: dict[str, dict[str, Any]],
        limits: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        engine = FuturesDailyExecutionEngine(slippage_ticks=1)
        rows: list[dict[str, Any]] = []
        for product in sorted(set(targets) | set(previous)):
            current = previous.get(product)
            target = targets.get(product)
            current_code = str(current.get("contract_code", "")) if current else ""
            target_code = str(target.get("contract_code", "")) if target else current_code
            current_lots = int(current.get("target_signed_lots", 0)) if current else 0
            target_lots = int(target.get("target_signed_lots", 0)) if target else 0
            legs = self._transition_legs(
                product,
                current_code,
                current_lots,
                target_code,
                target_lots,
            )
            prior_filled = True
            for leg, code, signed_lots, offset, is_roll in legs:
                side = FuturesSide.BUY if signed_lots > 0 else FuturesSide.SELL
                lots = abs(signed_lots)
                if not prior_filled:
                    rows.append(
                        self._blocked_leg(
                            product,
                            code,
                            leg,
                            side,
                            offset,
                            lots,
                            is_roll,
                        )
                    )
                    continue
                contract = self._required(contracts, code, "contract master")
                daily_row = self._required(daily, code, "daily")
                settlement = self._required(settlements, code, "settlements")
                limit = self._required(limits, code, "limits")
                multiplier = self._positive(contract.get("multiplier"), "contract multiplier")
                tick_size = self._positive(contract.get("per_unit"), "contract tick size")
                fee_rule = FuturesFeeRule(
                    per_lot=self._non_negative(settlement.get("trading_fee"), "trading fee")
                    * multiplier,
                    notional_rate=self._non_negative(
                        settlement.get("trading_fee_rate"),
                        "trading fee rate",
                    )
                    * multiplier,
                )
                order = FuturesOrder(
                    order_id=f"shadow:{product}:{leg}",
                    signal_date=signal_date,
                    eligible_date=eligible_date,
                    contract_code=code,
                    side=side,
                    offset=offset,
                    lots=lots,
                    multiplier=multiplier,
                    tick_size=tick_size,
                    fee_rule=fee_rule,
                )
                bar = FuturesDailyExecutionBar(
                    trade_date=eligible_date,
                    open_price=daily_row.get("open"),
                    high_price=daily_row.get("high"),
                    low_price=daily_row.get("low"),
                    volume=daily_row.get("vol"),
                    up_limit=limit.get("up_limit"),
                    down_limit=limit.get("down_limit"),
                )
                margin_rate = self._margin_rate(settlement, signed_lots)
                result = engine.attempt(
                    order,
                    eligible_date,
                    bar,
                    available_cash=equity,
                    margin_rate=margin_rate,
                )
                fill = result.fill
                rows.append(
                    {
                        "product_code": product,
                        "contract_code": code,
                        "leg": leg,
                        "side": side.value,
                        "offset": offset.value,
                        "lots": lots,
                        "status": result.status.value,
                        "reason": result.reason,
                        "fill_price": fill.price if fill else None,
                        "theoretical_fee": fill.fee if fill else None,
                        "required_margin": result.required_margin,
                        "limit_status": (
                            "within_limits"
                            if result.reason in {"filled", "insufficient_margin"}
                            else result.reason
                        ),
                        "fee_actual_status": "broker_actual_unavailable",
                        "is_roll": is_roll,
                    }
                )
                prior_filled = result.status == FuturesExecutionStatus.FILLED
        return rows

    @staticmethod
    def _transition_legs(
        product: str,
        current_code: str,
        current_lots: int,
        target_code: str,
        target_lots: int,
    ) -> list[tuple[str, str, int, FuturesOffset, bool]]:
        legs: list[tuple[str, str, int, FuturesOffset, bool]] = []
        changed_contract = bool(
            current_lots and target_lots and current_code != target_code
        )
        reversal = current_lots * target_lots < 0
        if changed_contract or reversal:
            legs.append(
                (
                    "roll_close" if changed_contract else "reverse_close",
                    current_code,
                    -current_lots,
                    FuturesOffset.CLOSE,
                    changed_contract,
                )
            )
            legs.append(
                (
                    "roll_open" if changed_contract else "reverse_open",
                    target_code,
                    target_lots,
                    FuturesOffset.OPEN,
                    changed_contract,
                )
            )
            return legs
        delta = target_lots - current_lots
        if not delta:
            return legs
        increasing = abs(target_lots) > abs(current_lots)
        legs.append(
            (
                "open" if increasing else "close",
                target_code or current_code,
                delta,
                FuturesOffset.OPEN if increasing else FuturesOffset.CLOSE,
                False,
            )
        )
        return legs

    @staticmethod
    def _blocked_leg(
        product: str,
        code: str,
        leg: str,
        side: FuturesSide,
        offset: FuturesOffset,
        lots: int,
        is_roll: bool,
    ) -> dict[str, Any]:
        return {
            "product_code": product,
            "contract_code": code,
            "leg": leg,
            "side": side.value,
            "offset": offset.value,
            "lots": lots,
            "status": FuturesExecutionStatus.BLOCKED.value,
            "reason": "previous_leg_blocked",
            "fill_price": None,
            "theoretical_fee": None,
            "required_margin": 0.0,
            "limit_status": "not_attempted",
            "fee_actual_status": "broker_actual_unavailable",
            "is_roll": is_roll,
        }

    def _observed_margin(
        self,
        targets: dict[str, dict[str, Any]],
        contracts: dict[str, dict[str, Any]],
        settlements: dict[str, dict[str, Any]],
    ) -> float:
        total = 0.0
        for target in targets.values():
            lots = int(target.get("target_signed_lots", 0))
            if not lots:
                continue
            code = str(target["contract_code"])
            contract = self._required(contracts, code, "contract master")
            settlement = self._required(settlements, code, "settlements")
            total += (
                abs(lots)
                * self._positive(contract.get("multiplier"), "contract multiplier")
                * self._positive(settlement.get("settle"), "settlement price")
                * self._margin_rate(settlement, lots)
            )
        return round(total, 12)

    def _validate_target_coverage(
        self,
        targets: dict[str, dict[str, Any]],
        contracts: dict[str, dict[str, Any]],
        daily: dict[str, dict[str, Any]],
        settlements: dict[str, dict[str, Any]],
        limits: dict[str, dict[str, Any]],
    ) -> None:
        for target in targets.values():
            if not int(target.get("target_signed_lots", 0)):
                continue
            code = str(target["contract_code"])
            self._required(contracts, code, "contract master")
            self._required(daily, code, "daily")
            self._required(settlements, code, "settlements")
            self._required(limits, code, "limits")

    def _previous_targets(
        self,
        build_id: str,
        current_signal: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        if not build_id:
            return {}, []
        validated_id = self._build_id(build_id, "previous signal")
        directory = self._build_dir("signals", validated_id)
        manifest_path = directory / "manifest.json"
        targets_path = directory / "targets.parquet"
        manifest = self._read_json(manifest_path, "previous signal manifest")
        if manifest.get("build_id") != validated_id or not manifest.get("passed"):
            raise ValueError("Previous shadow signal is invalid.")
        if manifest.get("protocol_id") != current_signal.get("protocol_id"):
            raise ValueError("Shadow signal chain changed frozen strategy parameters.")
        if manifest.get("research_build_id") != current_signal.get("research_build_id"):
            raise ValueError("Shadow signal chain changed research build.")
        if manifest.get("eligible_date") != current_signal.get("signal_date"):
            raise ValueError("Shadow signal chain is not consecutive.")
        self._verify_output(manifest, targets_path)
        return (
            self._target_rows(pl.read_parquet(targets_path), "previous signal"),
            [
                self._file_version(manifest_path, self.curated_root),
                self._file_version(targets_path, self.curated_root),
            ],
        )

    def _declared_contract_master(
        self,
        signal: dict[str, Any],
    ) -> tuple[Path, dict[str, Any]]:
        matches = [
            item
            for item in signal.get("inputs", [])
            if str(item.get("path", "")).replace("\\", "/").startswith(
                "futures/futures_contracts/"
            )
            and str(item.get("path", "")).replace("\\", "/").endswith(
                "/data.parquet"
            )
        ]
        if len(matches) != 1:
            raise ValueError("Shadow signal requires one declared contract master input.")
        path = (self.curated_root / Path(str(matches[0]["path"]))).resolve()
        if self.curated_root not in path.parents:
            raise ValueError("Declared contract master is outside the curated data root.")
        self._verify_version(matches[0], path, "contract master")
        return path, self._file_version(path, self.curated_root)

    @staticmethod
    def _target_rows(frame: pl.DataFrame, description: str) -> dict[str, dict[str, Any]]:
        required = {"product_code", "contract_code", "target_signed_lots"}
        if missing := required - set(frame.columns):
            raise ValueError(
                f"{description} targets missing columns: {', '.join(sorted(missing))}"
            )
        rows: dict[str, dict[str, Any]] = {}
        for row in frame.to_dicts():
            product = str(row["product_code"]).strip().upper()
            if product in rows:
                raise ValueError(f"Duplicate {description} product: {product}")
            rows[product] = row
        return rows

    @staticmethod
    def _contract_rows(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for row in frame.to_dicts():
            code = str(row.get("ts_code", "")).strip().upper()
            if not code or code in rows:
                raise ValueError("Contract master contains blank or duplicate contract codes.")
            rows[code] = row
        return rows

    def _dated_rows(
        self,
        frame: pl.DataFrame,
        expected_date: date,
        description: str,
    ) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for row in frame.to_dicts():
            if self._compact_date(row.get("trade_date")) != expected_date:
                continue
            code = str(row.get("ts_code", "")).strip().upper()
            if not code or code in rows:
                raise ValueError(
                    f"Shadow {description} contains blank or duplicate contract rows."
                )
            rows[code] = row
        return rows

    def _partition_path(self, dataset: str, provider: str, value: date) -> Path:
        return (
            self.curated_root
            / "futures"
            / dataset
            / f"provider={provider}"
            / f"as_of_date={value.isoformat()}"
            / "data.parquet"
        )

    def _build_dir(self, kind: str, build_id: str) -> Path:
        return self.curated_root / "futures" / kind / f"build_id={build_id}"

    @staticmethod
    def _protocol(path: Path) -> StrategyProtocol:
        if not path.is_file():
            raise FileNotFoundError("Frozen shadow protocol file not found.")
        protocol = StrategyProtocol.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        if protocol.status != ProtocolStatus.FROZEN:
            raise ValueError("Shadow observation requires a frozen protocol.")
        if protocol.content_hash != protocol.calculated_hash():
            raise ValueError("Frozen shadow protocol hash mismatch.")
        protocol.partition(PartitionName.FORWARD)
        return protocol

    @staticmethod
    def _verify_output(manifest: dict[str, Any], path: Path) -> None:
        matches = [
            item
            for item in manifest.get("output_versions", [])
            if item.get("path") == path.name
        ]
        if len(matches) != 1:
            raise ValueError(f"Immutable shadow input lacks hash declaration: {path.name}")
        FuturesShadowObservationService._verify_version(
            matches[0],
            path,
            "immutable shadow input",
        )

    @staticmethod
    def _verify_version(version: dict[str, Any], path: Path, label: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"{label} file not found: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if version.get("sha256") != digest or version.get("size") != path.stat().st_size:
            raise ValueError(f"{label} hash mismatch: {path.name}")

    @staticmethod
    def _file_version(path: Path, base: Path) -> dict[str, Any]:
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            relative = path.name
        return {
            "path": relative,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }

    @staticmethod
    def _read_json(path: Path, description: str) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(f"Required {description} not found.")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid {description} JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{description} must be a JSON object.")
        return value

    @staticmethod
    def _build_id(value: Any, description: str) -> str:
        result = str(value or "").strip()
        if not BUILD_ID_PATTERN.fullmatch(result):
            raise ValueError(f"Invalid {description} build ID.")
        return result

    @staticmethod
    def _date(value: Any, description: str) -> date:
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"Invalid shadow {description}.") from exc

    @staticmethod
    def _compact_date(value: Any) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.strptime(text.replace("-", ""), "%Y%m%d").date()
        except ValueError:
            return None

    @staticmethod
    def _positive(value: Any, description: str) -> float:
        result = float(value)
        if not 0 < result < float("inf"):
            raise ValueError(f"Shadow {description} must be positive and finite.")
        return result

    @staticmethod
    def _non_negative(value: Any, description: str) -> float:
        result = float(value or 0)
        if not 0 <= result < float("inf"):
            raise ValueError(f"Shadow {description} must be non-negative and finite.")
        return result

    def _margin_rate(self, settlement: dict[str, Any], signed_lots: int) -> float:
        key = "long_margin_rate" if signed_lots > 0 else "short_margin_rate"
        value = self._positive(settlement.get(key), key)
        result = value / 100 if value >= 1 else value
        if not 0 < result < 1:
            raise ValueError(f"Shadow {key} must normalize to (0, 1).")
        return result

    @staticmethod
    def _required(
        rows: dict[str, dict[str, Any]],
        code: str,
        description: str,
    ) -> dict[str, Any]:
        try:
            return rows[code]
        except KeyError as exc:
            raise ValueError(f"Missing shadow {description} row for {code}.") from exc

    def _write_immutable(
        self,
        output_dir: Path,
        executions: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> None:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
        temporary.mkdir()
        try:
            frame = (
                pl.DataFrame(executions, schema=self.OUTPUT_SCHEMA)
                if executions
                else pl.DataFrame(schema=self.OUTPUT_SCHEMA)
            )
            output_path = temporary / self.OUTPUT_FILE
            frame.write_parquet(output_path, compression="zstd")
            manifest["output_versions"] = [
                self._file_version(output_path, temporary)
            ]
            self._atomic_json(temporary / "manifest.json", manifest)
            os.replace(temporary, output_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _write_report(self, path: Path, manifest: dict[str, Any]) -> None:
        content = "\n".join(
            [
                "# Futures Forward Shadow Observation",
                "",
                f"- Build ID: `{manifest['build_id']}`",
                f"- Protocol: `{manifest['protocol_id']}`",
                f"- Protocol hash: `{manifest['protocol_hash']}`",
                f"- Signal build: `{manifest['signal_build_id']}`",
                f"- Observation date: {manifest['observation_date']}",
                f"- Fully executable: {manifest['fully_executable']}",
                f"- Blocked legs: {manifest['blocked_execution_rows']}",
                f"- Theoretical fee: {manifest['theoretical_fee']}",
                f"- Initial margin deviation: {manifest['initial_margin_deviation']}",
                "- Night session: not observable from daily data",
                "- Broker actual fee/execution: unavailable; no broker is connected",
                "",
                "This record contains no return claim and must not be used for short-term",
                "parameter changes to the frozen protocol.",
                "",
            ]
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, path)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _result(
        manifest: dict[str, Any],
        output_dir: Path,
        output_path: Path,
        manifest_path: Path,
        report_path: Path,
        reused: bool,
    ) -> FuturesShadowObservationResult:
        return FuturesShadowObservationResult(
            build_id=str(manifest["build_id"]),
            output_dir=output_dir,
            output_path=output_path,
            manifest_path=manifest_path,
            report_path=report_path,
            observation_date=date.fromisoformat(str(manifest["observation_date"])),
            execution_rows=int(manifest.get("rows", {}).get("executions", 0)),
            blocked_rows=int(manifest.get("blocked_execution_rows", 0)),
            fully_executable=bool(manifest.get("fully_executable", False)),
            reused=reused,
        )
