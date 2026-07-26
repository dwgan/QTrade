from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from qtrade.pipeline.models import PipelineRun


class PipelineReportWriter:
    def __init__(self, reports_root: Path) -> None:
        self.root = Path(reports_root)

    def write(self, run: PipelineRun) -> tuple[Path, Path]:
        directory = self.root / "pipeline" / run.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "run.json"
        markdown_path = directory / "run.md"
        self._atomic(
            json_path,
            json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2),
        )
        lines = [
            f"# 日度流水线：{run.as_of_date}",
            "",
            f"- 状态：{run.status}",
            f"- 开始：{run.created_at.isoformat(timespec='seconds')}",
            f"- 完成：{run.finished_at.isoformat(timespec='seconds')}",
            "",
            "| 步骤 | 状态 | 说明 |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| {step.name} | {step.status.value} | {step.message or '-'} |"
            for step in run.steps
        )
        lines.extend(["", "## 产物", ""])
        artifacts = [
            artifact
            for step in run.steps
            for artifact in step.artifacts
        ]
        lines.extend(f"- `{artifact}`" for artifact in artifacts)
        if not artifacts:
            lines.append("- 无")
        lines.append("")
        self._atomic(markdown_path, "\n".join(lines))
        return json_path, markdown_path

    @staticmethod
    def _atomic(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
