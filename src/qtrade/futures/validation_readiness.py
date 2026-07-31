from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from qtrade.research.protocols import PartitionName, ProtocolStatus, StrategyProtocol


@dataclass(frozen=True)
class FuturesValidationReadinessResult:
    protocol_id: str
    partition: PartitionName
    ready: bool
    issue_count: int
    report_json: Path
    report_markdown: Path


class FuturesValidationReadinessService:
    DATASETS = ("futures_daily", "futures_settlements", "futures_limits")

    def __init__(self, curated_root: Path, reports_root: Path) -> None:
        self.curated_root = Path(curated_root)
        self.reports_root = Path(reports_root)

    def audit(
        self,
        protocol_path: Path,
        partition: PartitionName,
        provider: str,
    ) -> FuturesValidationReadinessResult:
        protocol_path = Path(protocol_path).resolve()
        protocol = StrategyProtocol.model_validate(
            yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
        )
        if protocol.status != ProtocolStatus.FROZEN:
            raise ValueError("Futures validation readiness requires a frozen protocol.")
        if protocol.content_hash != protocol.calculated_hash():
            raise ValueError("Frozen futures protocol hash mismatch.")
        expected = protocol.partition(partition)
        if expected.end_date is None:
            raise ValueError("Forward partition cannot be audited as historical validation.")
        issues: list[dict[str, Any]] = []
        dataset_dates: dict[str, list[date]] = {}
        for dataset in self.DATASETS:
            dates = self._available_dates(dataset, provider)
            dataset_dates[dataset] = dates
            if not dates:
                issues.append(
                    self._issue(
                        "error",
                        f"missing_{dataset}",
                        f"No {dataset} partitions are available for provider {provider}.",
                    )
                )
                continue
            if dates[0] > expected.start_date or dates[-1] < expected.end_date:
                issues.append(
                    self._issue(
                        "error",
                        f"insufficient_{dataset}_coverage",
                        f"{dataset} covers {dates[0]} to {dates[-1]}, but frozen "
                        f"{partition.value} requires {expected.start_date} to {expected.end_date}.",
                    )
                )
        research_builds = self._passed_builds("research")
        signal_builds = self._passed_builds("signals")
        if not research_builds:
            issues.append(
                self._issue(
                    "error",
                    "missing_research_build",
                    "No passed phase-2 research build exists.",
                )
            )
        if not signal_builds:
            issues.append(
                self._issue(
                    "error",
                    "missing_signal_chain",
                    "No passed futures signal snapshot exists.",
                )
            )
        else:
            signal_dates = sorted(
                date.fromisoformat(str(item["signal_date"]))
                for item in signal_builds
                if item.get("signal_date")
            )
            incomplete_signal_coverage = (
                not signal_dates
                or signal_dates[0] > expected.start_date
                or signal_dates[-1] < expected.end_date
            )
            if incomplete_signal_coverage:
                actual = f"{signal_dates[0]} to {signal_dates[-1]}" if signal_dates else "none"
                issues.append(
                    self._issue(
                        "error",
                        "insufficient_signal_chain_coverage",
                        f"Signal snapshots cover {actual}, but frozen {partition.value} requires "
                        f"{expected.start_date} to {expected.end_date}.",
                    )
                )
        payload = {
            "protocol_id": protocol.protocol_id,
            "protocol_hash": protocol.content_hash,
            "partition": partition.value,
            "provider": provider,
            "required_start": expected.start_date.isoformat(),
            "required_end": expected.end_date.isoformat(),
            "ready": not issues,
            "dataset_coverage": {
                name: {
                    "first": dates[0].isoformat() if dates else None,
                    "last": dates[-1].isoformat() if dates else None,
                    "partitions": len(dates),
                }
                for name, dates in dataset_dates.items()
            },
            "research_builds": len(research_builds),
            "signal_builds": len(signal_builds),
            "issues": issues,
        }
        audit_id = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        directory = (
            self.reports_root
            / "futures"
            / "validation-readiness"
            / protocol.protocol_id
            / partition.value
            / audit_id
        )
        report_json = directory / "report.json"
        report_markdown = directory / "report.md"
        self._atomic_json(report_json, payload)
        self._write_markdown(report_markdown, payload)
        return FuturesValidationReadinessResult(
            protocol_id=protocol.protocol_id,
            partition=partition,
            ready=payload["ready"],
            issue_count=len(issues),
            report_json=report_json,
            report_markdown=report_markdown,
        )

    def _available_dates(self, dataset: str, provider: str) -> list[date]:
        root = self.curated_root / "futures" / dataset / f"provider={provider}"
        values = []
        for path in root.glob("as_of_date=*") if root.exists() else ():
            try:
                value = date.fromisoformat(path.name.removeprefix("as_of_date="))
            except ValueError:
                continue
            if (path / "data.parquet").is_file():
                values.append(value)
        return sorted(values)

    def _passed_builds(self, kind: str) -> list[dict[str, Any]]:
        root = self.curated_root / "futures" / kind
        builds = []
        for path in root.glob("build_id=*/manifest.json") if root.exists() else ():
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("passed"):
                builds.append(manifest)
        return builds

    @staticmethod
    def _issue(severity: str, code: str, message: str) -> dict[str, str]:
        return {"severity": severity, "code": code, "message": message}

    @staticmethod
    def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
        lines = [
            "# Futures Validation Readiness",
            "",
            f"- Protocol: `{payload['protocol_id']}`",
            f"- Partition: {payload['partition']}",
            f"- Ready: {payload['ready']}",
            f"- Research builds: {payload['research_builds']}",
            f"- Signal builds: {payload['signal_builds']}",
            "",
            "## Issues",
            "",
            *(
                [
                    f"- [{item['severity'].upper()}] `{item['code']}`: {item['message']}"
                    for item in payload["issues"]
                ]
                or ["- None"]
            ),
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        os.replace(temporary, path)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
