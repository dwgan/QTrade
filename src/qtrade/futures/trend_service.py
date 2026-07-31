from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from qtrade.futures.sectors import futures_sector, futures_sector_registry_id
from qtrade.futures.trend import FuturesTrendEngine, FuturesTrendProtocol


@dataclass(frozen=True)
class FuturesTrendBuildResult:
    build_id: str
    output_dir: Path
    manifest_path: Path
    report_path: Path
    target_rows: int
    insufficient_capital_rows: int
    total_daily_risk: float
    initial_margin: float
    stress_margin: float
    buffered_rows: int
    reused: bool = False


class FuturesTrendService:
    OUTPUT_FILE = "targets.parquet"
    TARGET_SCHEMA = {
        "signal_date": pl.String,
        "eligible_date": pl.String,
        "product_code": pl.String,
        "contract_code": pl.String,
        "sector": pl.String,
        "signal_strength": pl.Float64,
        "estimated_daily_volatility": pl.Float64,
        "product_daily_risk_budget": pl.Float64,
        "allocated_daily_risk": pl.Float64,
        "one_lot_daily_risk": pl.Float64,
        "one_lot_initial_margin": pl.Float64,
        "unconstrained_signed_lots": pl.Int64,
        "buffered_signed_lots": pl.Int64,
        "buffer_applied": pl.Boolean,
        "buffer_reason": pl.String,
        "target_signed_lots": pl.Int64,
        "initial_margin": pl.Float64,
        "stress_margin": pl.Float64,
        "limit_reasons": pl.List(pl.String),
        "status": pl.String,
        "protocol_id": pl.String,
        "research_build_id": pl.String,
    }

    def __init__(
        self,
        curated_root: Path,
        reports_root: Path,
        protocol: FuturesTrendProtocol | None = None,
    ) -> None:
        self.curated_root = Path(curated_root)
        self.reports_root = Path(reports_root)
        self.protocol = protocol or FuturesTrendProtocol()

    def build(self, input_path: Path) -> FuturesTrendBuildResult:
        input_path = Path(input_path).resolve()
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        research_build_id = str(payload.get("research_build_id", "")).strip()
        if not research_build_id:
            raise ValueError("Futures trend input requires research_build_id.")
        signal_date = self._date(payload.get("signal_date"), "signal_date")
        eligible_date = self._date(payload.get("eligible_date"), "eligible_date")
        equity = self._positive(payload.get("equity"), "equity")
        previous_signal_build_id = str(payload.get("previous_signal_build_id") or "").strip()

        research_dir = self.curated_root / "futures" / "research" / f"build_id={research_build_id}"
        manifest_path = research_dir / "manifest.json"
        continuous_path = research_dir / "continuous.parquet"
        universe_path = research_dir / "universe.parquet"
        roll_path = research_dir / "roll_schedule.parquet"
        research_paths = (manifest_path, continuous_path, universe_path, roll_path)
        for path in research_paths:
            if not path.is_file():
                raise FileNotFoundError(f"Futures research build input not found: {path}")
        source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if source_manifest.get("build_id") != research_build_id or not source_manifest.get(
            "passed"
        ):
            raise ValueError(
                "Referenced futures research build is invalid or failed quality checks."
            )
        for path in (continuous_path, universe_path, roll_path):
            self._verify_research_output(source_manifest, path)

        provider = str(source_manifest.get("provider", "")).strip()
        contract_partition = self._date(
            source_manifest.get("contract_master_partition_date"),
            "contract_master_partition_date",
        )
        if not provider:
            raise ValueError("Futures research manifest requires provider.")
        daily_path = self._partition_path("futures_daily", provider, signal_date)
        contract_path = self._partition_path("futures_contracts", provider, contract_partition)
        settlement_path = self._partition_path("futures_settlements", provider, signal_date)
        self._verify_declared_input(source_manifest, daily_path)
        self._verify_declared_input(source_manifest, contract_path)
        if not settlement_path.is_file():
            raise FileNotFoundError(f"Futures settlement partition not found: {settlement_path}")
        previous_targets, previous_rows, previous_versions = self._previous_targets(
            previous_signal_build_id,
            research_build_id,
            signal_date,
        )

        versions = [
            self._file_version(input_path, input_path.parent),
            *(self._file_version(path, self.curated_root) for path in research_paths),
            self._file_version(daily_path, self.curated_root),
            self._file_version(contract_path, self.curated_root),
            self._file_version(settlement_path, self.curated_root),
            *previous_versions,
        ]
        build_payload = {
            "schema_version": 3,
            "research_build_id": research_build_id,
            "previous_signal_build_id": previous_signal_build_id or None,
            "protocol_id": self.protocol.protocol_id,
            "protocol": asdict(self.protocol),
            "sector_registry_id": futures_sector_registry_id(),
            "signal_date": signal_date.isoformat(),
            "eligible_date": eligible_date.isoformat(),
            "equity": equity,
            "inputs": versions,
        }
        build_id = hashlib.sha256(self._canonical(build_payload).encode("utf-8")).hexdigest()[:20]
        output_dir = self.curated_root / "futures" / "signals" / f"build_id={build_id}"
        output_manifest = output_dir / "manifest.json"
        report_path = self.reports_root / "futures" / "signals" / build_id / "report.md"
        if output_dir.exists():
            if not output_manifest.is_file() or not (output_dir / self.OUTPUT_FILE).is_file():
                raise RuntimeError(f"Incomplete immutable futures trend build exists: {output_dir}")
            manifest = json.loads(output_manifest.read_text(encoding="utf-8"))
            self._verify_signal_output(manifest, output_dir / self.OUTPUT_FILE)
            return self._result(manifest, output_dir, output_manifest, report_path, reused=True)

        continuous = pl.read_parquet(continuous_path)
        universe = pl.read_parquet(universe_path)
        roll_schedule = pl.read_parquet(roll_path)
        contracts = self._contract_inputs(
            signal_date,
            eligible_date,
            roll_schedule,
            pl.read_parquet(daily_path),
            pl.read_parquet(contract_path),
            pl.read_parquet(settlement_path),
        )
        result = FuturesTrendEngine(self.protocol).generate(
            signal_date,
            eligible_date,
            equity,
            continuous,
            universe,
            roll_schedule,
            contracts,
            previous_targets=previous_targets,
        )
        target_rows = [
            {
                **asdict(target),
                "signal_date": target.signal_date.isoformat(),
                "eligible_date": target.eligible_date.isoformat(),
                "protocol_id": result.protocol_id,
                "research_build_id": research_build_id,
            }
            for target in result.targets
        ]
        target_rows.extend(
            self._universe_exit_rows(
                previous_rows,
                {row["product_code"] for row in target_rows},
                signal_date,
                eligible_date,
                research_build_id,
                result.protocol_id,
            )
        )
        target_rows.sort(key=lambda row: row["product_code"])
        insufficient_capital_rows = sum(
            row["status"] == "insufficient_capital" for row in target_rows
        )
        buffered_rows = sum(bool(row["buffer_applied"]) for row in target_rows)
        manifest = {
            **build_payload,
            "build_id": build_id,
            "passed": True,
            "outputs": {"targets": self.OUTPUT_FILE},
            "rows": {"targets": len(target_rows)},
            "insufficient_capital_rows": insufficient_capital_rows,
            "buffered_rows": buffered_rows,
            "portfolio_daily_risk_budget": result.portfolio_daily_risk_budget,
            "total_daily_risk": result.total_daily_risk,
            "sector_daily_risk": result.sector_daily_risk,
            "initial_margin": result.initial_margin,
            "stress_margin": result.stress_margin,
        }
        self._write_immutable(output_dir, target_rows, manifest)
        self._write_report(report_path, manifest)
        return self._result(manifest, output_dir, output_manifest, report_path, reused=False)

    def _previous_targets(
        self,
        build_id: str,
        research_build_id: str,
        signal_date: date,
    ) -> tuple[dict[str, int], list[dict[str, Any]], list[dict[str, Any]]]:
        if not build_id:
            return {}, [], []
        directory = self.curated_root / "futures" / "signals" / f"build_id={build_id}"
        manifest_path = directory / "manifest.json"
        targets_path = directory / self.OUTPUT_FILE
        for path in (manifest_path, targets_path):
            if not path.is_file():
                raise FileNotFoundError(f"Previous futures signal build input not found: {path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("build_id") != build_id or not manifest.get("passed"):
            raise ValueError("Previous futures signal build is invalid or failed quality checks.")
        if manifest.get("protocol_id") != self.protocol.protocol_id:
            raise ValueError("Previous futures signal build uses a different frozen protocol.")
        if manifest.get("research_build_id") != research_build_id:
            raise ValueError("Previous futures signal build uses a different research build.")
        if self._date(manifest.get("eligible_date"), "previous eligible_date") != signal_date:
            raise ValueError("Previous signal eligible date must equal the current signal date.")
        self._verify_signal_output(manifest, targets_path)
        frame = pl.read_parquet(targets_path)
        if frame.select("product_code").is_duplicated().any():
            raise ValueError("Previous futures signal targets must have unique product codes.")
        rows = frame.to_dicts()
        targets = {str(row["product_code"]): int(row["target_signed_lots"]) for row in rows}
        versions = [
            self._file_version(manifest_path, self.curated_root),
            self._file_version(targets_path, self.curated_root),
        ]
        return targets, rows, versions

    @staticmethod
    def _universe_exit_rows(
        previous_rows: list[dict[str, Any]],
        current_products: set[str],
        signal_date: date,
        eligible_date: date,
        research_build_id: str,
        protocol_id: str,
    ) -> list[dict[str, Any]]:
        rows = []
        for previous in previous_rows:
            product_code = str(previous["product_code"])
            if product_code in current_products or int(previous["target_signed_lots"]) == 0:
                continue
            rows.append(
                {
                    **previous,
                    "signal_date": signal_date.isoformat(),
                    "eligible_date": eligible_date.isoformat(),
                    "signal_strength": 0.0,
                    "product_daily_risk_budget": 0.0,
                    "allocated_daily_risk": 0.0,
                    "unconstrained_signed_lots": 0,
                    "buffered_signed_lots": 0,
                    "buffer_applied": False,
                    "buffer_reason": None,
                    "target_signed_lots": 0,
                    "initial_margin": 0.0,
                    "stress_margin": 0.0,
                    "limit_reasons": [],
                    "status": "universe_exit",
                    "protocol_id": protocol_id,
                    "research_build_id": research_build_id,
                }
            )
        return rows

    def _contract_inputs(
        self,
        signal_date: date,
        eligible_date: date,
        roll_schedule: pl.DataFrame,
        daily: pl.DataFrame,
        contract_master: pl.DataFrame,
        settlements: pl.DataFrame,
    ) -> pl.DataFrame:
        selected_products = {
            str(row["selected_contract"]): str(row["product_code"])
            for row in roll_schedule.to_dicts()
            if self._date(row["decision_date"], "decision_date") == signal_date
            and self._date(row["effective_date"], "effective_date") == eligible_date
            and bool(row["universe_eligible"])
        }
        selected = set(selected_products)
        daily_rows = self._unique_by_code(daily, selected, "daily")
        contract_rows = self._unique_by_code(contract_master, selected, "contract master")
        settlement_rows = self._unique_by_code(settlements, selected, "settlement")
        records = []
        for contract_code in sorted(selected):
            daily_date = self._compact_date(daily_rows[contract_code].get("trade_date"))
            settlement_date = self._compact_date(settlement_rows[contract_code].get("trade_date"))
            if daily_date != signal_date or settlement_date != signal_date:
                raise ValueError(
                    f"Contract inputs for {contract_code} must be observed on signal date."
                )
            records.append(
                {
                    "trade_date": signal_date.isoformat(),
                    "contract_code": contract_code,
                    "settle": daily_rows[contract_code].get("settle"),
                    "multiplier": contract_rows[contract_code].get("multiplier"),
                    "sector": futures_sector(selected_products[contract_code]),
                    "long_margin_rate": settlement_rows[contract_code].get("long_margin_rate"),
                    "short_margin_rate": settlement_rows[contract_code].get("short_margin_rate"),
                }
            )
        if not records:
            return pl.DataFrame(
                schema={
                    "trade_date": pl.String,
                    "contract_code": pl.String,
                    "settle": pl.Float64,
                    "multiplier": pl.Float64,
                    "sector": pl.String,
                    "long_margin_rate": pl.Float64,
                    "short_margin_rate": pl.Float64,
                }
            )
        return pl.DataFrame(records, infer_schema_length=None, strict=False)

    @staticmethod
    def _unique_by_code(
        frame: pl.DataFrame,
        selected: set[str],
        description: str,
    ) -> dict[str, dict[str, Any]]:
        code_column = "ts_code" if "ts_code" in frame.columns else "contract_code"
        grouped: dict[str, list[dict[str, Any]]] = {code: [] for code in selected}
        for row in frame.to_dicts():
            code = str(row.get(code_column, ""))
            if code in grouped:
                grouped[code].append(row)
        invalid = {code: len(rows) for code, rows in grouped.items() if len(rows) != 1}
        if invalid:
            raise ValueError(
                f"Expected one {description} row per selected contract; found {invalid}."
            )
        return {code: rows[0] for code, rows in grouped.items()}

    def _partition_path(self, dataset: str, provider: str, as_of_date: date) -> Path:
        return (
            self.curated_root
            / "futures"
            / dataset
            / f"provider={provider}"
            / f"as_of_date={as_of_date.isoformat()}"
            / "data.parquet"
        )

    def _verify_declared_input(self, manifest: dict[str, Any], path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"Declared futures source partition not found: {path}")
        relative = path.relative_to(self.curated_root).as_posix()
        matches = [item for item in manifest.get("inputs", []) if item.get("path") == relative]
        if len(matches) != 1:
            raise ValueError(
                f"Research manifest does not uniquely declare source input: {relative}"
            )
        actual = self._file_version(path, self.curated_root)
        if matches[0].get("sha256") != actual["sha256"] or matches[0].get("size") != actual["size"]:
            raise ValueError(f"Research source input changed after build: {relative}")

    @staticmethod
    def _verify_research_output(manifest: dict[str, Any], path: Path) -> None:
        matches = [
            item for item in manifest.get("output_versions", []) if item.get("path") == path.name
        ]
        if len(matches) != 1:
            raise ValueError(f"Research manifest does not declare output hash: {path.name}")
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digest_changed = matches[0].get("sha256") != actual_digest
        size_changed = matches[0].get("size") != path.stat().st_size
        if digest_changed or size_changed:
            raise ValueError(f"Futures research output changed after build: {path.name}")

    @staticmethod
    def _verify_signal_output(manifest: dict[str, Any], path: Path) -> None:
        matches = [
            item for item in manifest.get("output_versions", []) if item.get("path") == path.name
        ]
        if len(matches) != 1:
            raise ValueError(f"Signal manifest does not declare output hash: {path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if matches[0].get("sha256") != digest or matches[0].get("size") != path.stat().st_size:
            raise ValueError(f"Futures signal output changed after build: {path.name}")

    @staticmethod
    def _file_version(path: Path, base: Path) -> dict[str, Any]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "path": path.relative_to(base).as_posix(),
            "sha256": digest,
            "size": path.stat().st_size,
        }

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _date(value: Any, name: str) -> date:
        try:
            return value if isinstance(value, date) else date.fromisoformat(str(value))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be an ISO date.") from error

    @staticmethod
    def _compact_date(value: Any) -> date | None:
        text = str(value or "").strip().replace("-", "")
        if len(text) != 8 or not text.isdigit():
            return None
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))

    @staticmethod
    def _positive(value: Any, name: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{name} must be finite and positive.") from error
        if not number > 0 or number == float("inf"):
            raise ValueError(f"{name} must be finite and positive.")
        return number

    def _write_immutable(
        self,
        output_dir: Path,
        rows: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> None:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
        temporary.mkdir(parents=False, exist_ok=False)
        try:
            frame = (
                pl.DataFrame(rows, infer_schema_length=None, strict=False)
                if rows
                else pl.DataFrame(schema=self.TARGET_SCHEMA)
            )
            frame.write_parquet(temporary / self.OUTPUT_FILE, compression="zstd")
            target_path = temporary / self.OUTPUT_FILE
            manifest["output_versions"] = [
                {
                    "path": self.OUTPUT_FILE,
                    "sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
                    "size": target_path.stat().st_size,
                }
            ]
            self._atomic_json(temporary / "manifest.json", manifest)
            os.replace(temporary, output_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _write_report(self, path: Path, manifest: dict[str, Any]) -> None:
        content = "\n".join(
            [
                "# Futures Trend Target Snapshot",
                "",
                f"- Build ID: `{manifest['build_id']}`",
                f"- Research build: `{manifest['research_build_id']}`",
                f"- Protocol ID: `{manifest['protocol_id']}`",
                f"- Signal date: {manifest['signal_date']}",
                f"- Eligible date: {manifest['eligible_date']}",
                f"- Target rows: {manifest['rows']['targets']}",
                f"- Insufficient capital rows: {manifest['insufficient_capital_rows']}",
                f"- Buffered rows: {manifest['buffered_rows']}",
                f"- Total daily risk: {manifest['total_daily_risk']:.2f}",
                f"- Initial margin: {manifest['initial_margin']:.2f}",
                f"- Stress margin: {manifest['stress_margin']:.2f}",
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
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)

    @staticmethod
    def _result(
        manifest: dict[str, Any],
        output_dir: Path,
        manifest_path: Path,
        report_path: Path,
        reused: bool,
    ) -> FuturesTrendBuildResult:
        return FuturesTrendBuildResult(
            build_id=manifest["build_id"],
            output_dir=output_dir,
            manifest_path=manifest_path,
            report_path=report_path,
            target_rows=manifest["rows"]["targets"],
            insufficient_capital_rows=manifest["insufficient_capital_rows"],
            total_daily_risk=manifest["total_daily_risk"],
            initial_margin=manifest["initial_margin"],
            stress_margin=manifest["stress_margin"],
            buffered_rows=manifest["buffered_rows"],
            reused=reused,
        )
