from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from qtrade.config import FuturesConfig
from qtrade.futures.continuous import FuturesSeriesBuilder, FuturesSeriesResult
from qtrade.futures.domain import FuturesDataset
from qtrade.futures.storage import FuturesParquetStore


@dataclass(frozen=True)
class FuturesResearchBuildResult:
    build_id: str
    output_dir: Path
    manifest_path: Path
    report_path: Path
    roll_rows: int
    continuous_rows: int
    universe_rows: int
    issue_count: int
    passed: bool
    reused: bool = False


class FuturesResearchService:
    OUTPUT_FILES = {
        "roll_schedule": "roll_schedule.parquet",
        "continuous": "continuous.parquet",
        "universe": "universe.parquet",
        "vendor_comparison": "vendor_comparison.parquet",
    }

    def __init__(
        self,
        config: FuturesConfig,
        curated_store: FuturesParquetStore,
        provider: str,
        reports_root: Path,
    ) -> None:
        self.config = config
        self.curated_store = curated_store
        self.provider = provider
        self.reports_root = Path(reports_root)
        self.builder = FuturesSeriesBuilder(config)

    def build(self, start_date: date, end_date: date) -> FuturesResearchBuildResult:
        if start_date >= end_date:
            raise ValueError("Futures research requires at least two ordered trading dates.")
        daily_dates = self.curated_store.available_dates(
            FuturesDataset.DAILY,
            self.provider,
            start_date,
            end_date,
        )
        if len(daily_dates) < 2:
            raise ValueError("At least two futures daily partitions are required.")
        candidate_contract_dates = self.curated_store.available_dates(
            FuturesDataset.CONTRACTS,
            self.provider,
            date.min,
            start_date,
        )
        contract_date, contract_observed_at, contracts = self._visible_contract_master(
            candidate_contract_dates,
            start_date,
        )
        if contract_date is None or contract_observed_at is None or contracts is None:
            raise FileNotFoundError(
                "No contract master observed on or before the research start date."
            )
        mapping_dates = self.curated_store.available_dates(
            FuturesDataset.MAPPINGS,
            self.provider,
            start_date,
            end_date,
        )
        input_paths = [
            self.curated_store.data_path(FuturesDataset.DAILY, self.provider, value)
            for value in daily_dates
        ]
        input_paths.append(
            self.curated_store.data_path(FuturesDataset.CONTRACTS, self.provider, contract_date)
        )
        input_paths.append(
            self.curated_store.partition_dir(
                FuturesDataset.CONTRACTS,
                self.provider,
                contract_date,
            )
            / "metadata.json"
        )
        input_paths.extend(
            self.curated_store.data_path(FuturesDataset.MAPPINGS, self.provider, value)
            for value in mapping_dates
        )
        input_versions = [self._file_version(path) for path in input_paths]
        build_payload = {
            "schema_version": 2,
            "provider": self.provider,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "config": self.config.model_dump(mode="json"),
            "inputs": input_versions,
        }
        build_id = hashlib.sha256(self._canonical(build_payload).encode("utf-8")).hexdigest()[:20]
        output_dir = self.curated_store.root / "futures" / "research" / f"build_id={build_id}"
        manifest_path = output_dir / "manifest.json"
        report_path = (
            self.reports_root
            / "futures"
            / "continuous"
            / end_date.isoformat()
            / build_id
            / "report.md"
        )
        if output_dir.exists():
            if not manifest_path.exists():
                raise RuntimeError(f"Incomplete immutable futures build exists: {output_dir}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return self._result_from_manifest(
                manifest,
                output_dir,
                manifest_path,
                report_path,
                reused=True,
            )

        daily = self.curated_store.read_range(
            FuturesDataset.DAILY,
            self.provider,
            start_date,
            end_date,
        )
        mappings = self.curated_store.read_range(
            FuturesDataset.MAPPINGS,
            self.provider,
            start_date,
            end_date,
        )
        series = self.builder.build(daily, contracts, mappings)
        manifest = self._manifest(
            build_id,
            build_payload,
            contract_date,
            contract_observed_at,
            series,
        )
        self._write_immutable(output_dir, series, manifest)
        self._write_report(report_path, manifest, series)
        return self._result_from_manifest(
            manifest,
            output_dir,
            manifest_path,
            report_path,
            reused=False,
        )

    def _visible_contract_master(
        self,
        candidate_dates: list[date],
        start_date: date,
    ) -> tuple[date | None, date | None, Any | None]:
        for partition_date in reversed(candidate_dates):
            frame = self.curated_store.read(
                FuturesDataset.CONTRACTS,
                self.provider,
                partition_date,
            )
            observed_dates = [
                self._compact_date(value) for value in frame.get_column("observed_at").to_list()
            ]
            metadata_path = (
                self.curated_store.partition_dir(
                    FuturesDataset.CONTRACTS,
                    self.provider,
                    partition_date,
                )
                / "metadata.json"
            )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            fetched_at = date.fromisoformat(str(metadata["fetched_at"])[:10])
            actual_observation = max([fetched_at, *(value for value in observed_dates if value)])
            if actual_observation <= start_date:
                return partition_date, actual_observation, frame
        return None, None, None

    def _manifest(
        self,
        build_id: str,
        build_payload: dict[str, Any],
        contract_partition_date: date,
        contract_observed_at: date,
        series: FuturesSeriesResult,
    ) -> dict[str, Any]:
        comparison_rows = series.vendor_comparison.height
        matched_rows = (
            int(series.vendor_comparison.get_column("matched").sum()) if comparison_rows else 0
        )
        return {
            **build_payload,
            "build_id": build_id,
            "contract_master_partition_date": contract_partition_date.isoformat(),
            "contract_master_observed_at": contract_observed_at.isoformat(),
            "passed": series.passed,
            "rows": {
                "roll_schedule": series.roll_schedule.height,
                "continuous": series.continuous.height,
                "universe": series.universe.height,
                "vendor_comparison": comparison_rows,
            },
            "roll_events": (
                int(series.roll_schedule.get_column("roll").sum())
                if series.roll_schedule.height
                else 0
            ),
            "vendor_matches": matched_rows,
            "vendor_match_rate": matched_rows / comparison_rows if comparison_rows else None,
            "issues": series.issues,
            "outputs": self.OUTPUT_FILES,
        }

    def _write_immutable(
        self,
        output_dir: Path,
        series: FuturesSeriesResult,
        manifest: dict[str, Any],
    ) -> None:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary_dir = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
        temporary_dir.mkdir(parents=False, exist_ok=False)
        frames = {
            "roll_schedule": series.roll_schedule,
            "continuous": series.continuous,
            "universe": series.universe,
            "vendor_comparison": series.vendor_comparison,
        }
        try:
            for name, frame in frames.items():
                destination = temporary_dir / self.OUTPUT_FILES[name]
                frame.write_parquet(destination, compression="zstd")
            manifest["output_versions"] = [
                self._output_version(temporary_dir / filename, filename)
                for filename in self.OUTPUT_FILES.values()
            ]
            self._atomic_json(temporary_dir / "manifest.json", manifest)
            os.replace(temporary_dir, output_dir)
        except Exception:
            shutil.rmtree(temporary_dir, ignore_errors=True)
            raise

    def _write_report(
        self,
        report_path: Path,
        manifest: dict[str, Any],
        series: FuturesSeriesResult,
    ) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        json_path = report_path.with_suffix(".json")
        self._atomic_json(json_path, manifest)
        issue_lines = [
            f"- [{issue['severity'].upper()}] `{issue['code']}`: "
            f"{issue['message']} ({issue['rows']} rows)"
            for issue in series.issues
        ] or ["- None"]
        match_rate = manifest["vendor_match_rate"]
        match_text = "N/A" if match_rate is None else f"{match_rate:.2%}"
        content = "\n".join(
            [
                "# Futures Continuous Series Quality Report",
                "",
                f"- Build ID: `{manifest['build_id']}`",
                f"- Range: {manifest['start_date']} to {manifest['end_date']}",
                f"- Passed: {manifest['passed']}",
                f"- Contract master partition: {manifest['contract_master_partition_date']}",
                f"- Contract master observed at: {manifest['contract_master_observed_at']}",
                f"- Roll schedule rows: {manifest['rows']['roll_schedule']}",
                f"- Roll events: {manifest['roll_events']}",
                f"- Continuous rows: {manifest['rows']['continuous']}",
                f"- Universe rows: {manifest['rows']['universe']}",
                f"- Vendor comparison rows: {manifest['rows']['vendor_comparison']}",
                f"- Vendor match rate: {match_text}",
                "",
                "## Quality Issues",
                "",
                *issue_lines,
                "",
                "The vendor mapping is a comparison only. The self-built schedule remains the",
                "research source of truth. Continuous research prices must not be used for fills",
                "or settlement PnL.",
                "",
            ]
        )
        temporary = report_path.with_name(f".{report_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, report_path)

    def _file_version(self, path: Path) -> dict[str, Any]:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return {
            "path": path.relative_to(self.curated_store.root).as_posix(),
            "sha256": digest.hexdigest(),
            "size": path.stat().st_size,
        }

    @staticmethod
    def _output_version(path: Path, filename: str) -> dict[str, Any]:
        return {
            "path": filename,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _compact_date(value: Any) -> date | None:
        text = str(value).strip().replace("-", "")
        if len(text) != 8 or not text.isdigit():
            return None
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)

    @staticmethod
    def _result_from_manifest(
        manifest: dict[str, Any],
        output_dir: Path,
        manifest_path: Path,
        report_path: Path,
        *,
        reused: bool,
    ) -> FuturesResearchBuildResult:
        return FuturesResearchBuildResult(
            build_id=manifest["build_id"],
            output_dir=output_dir,
            manifest_path=manifest_path,
            report_path=report_path,
            roll_rows=manifest["rows"]["roll_schedule"],
            continuous_rows=manifest["rows"]["continuous"],
            universe_rows=manifest["rows"]["universe"],
            issue_count=len(manifest["issues"]),
            passed=manifest["passed"],
            reused=reused,
        )
