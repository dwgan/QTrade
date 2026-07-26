from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPORT_NAMES = (
    "market",
    "industry",
    "factors",
    "observations",
)
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


class OverviewRepository:
    def __init__(self, reports_root: Path) -> None:
        self.root = Path(reports_root)

    def available_dates(self) -> list[str]:
        dates: set[str] = set()
        for name in REPORT_NAMES:
            directory = self.root / name
            if not directory.exists():
                continue
            dates.update(
                item.name
                for item in directory.iterdir()
                if item.is_dir() and DATE_PATTERN.fullmatch(item.name)
            )
        return sorted(dates, reverse=True)

    def overview(self, as_of_date: date) -> dict[str, Any]:
        day = as_of_date.isoformat()
        return {
            "date": day,
            "market": _load_json(self.root / "market" / day / "market.json"),
            "industry": _load_json(self.root / "industry" / day / "industry.json"),
            "factors": _load_json(self.root / "factors" / day / "factors.json"),
            "observation": _load_json(
                self.root / "observations" / day / "observation.json"
            ),
            "pipeline": _load_json(self.root / "pipeline" / day / "run.json"),
            "quality": _load_json(
                self.root / "data-quality" / day / "report.json"
            ),
        }


class WatchlistEditor:
    def __init__(self, config_path: Path) -> None:
        self.path = Path(config_path)

    @staticmethod
    def normalize(symbols: list[str]) -> list[str]:
        normalized = list(
            dict.fromkeys(value.strip().upper() for value in symbols if value.strip())
        )
        invalid = [value for value in normalized if not SYMBOL_PATTERN.fullmatch(value)]
        if invalid:
            raise ValueError(
                "股票代码格式应为 6 位数字加交易所后缀，例如 600519.SH；"
                f"无效代码：{', '.join(invalid)}"
            )
        return normalized

    def read(self) -> list[str]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        start, end = self._locate(lines)
        if start is None:
            return []
        symbols: list[str] = []
        for line in lines[start + 1 : end]:
            value = line.strip()
            if value.startswith("- "):
                symbols.append(value[2:].strip().upper())
        return symbols

    def write(self, symbols: list[str]) -> list[str]:
        values = self.normalize(symbols)
        text = self.path.read_text(encoding="utf-8")
        trailing_newline = text.endswith("\n")
        lines = text.splitlines()
        start, end = self._locate(lines)
        replacement = ["  watchlist_symbols:"] + [
            f"    - {symbol}" for symbol in values
        ]
        if not values:
            replacement = ["  watchlist_symbols: []"]
        if start is None:
            observation = next(
                (index for index, line in enumerate(lines) if line == "observation:"),
                None,
            )
            if observation is None:
                lines.extend(["", "observation:", *replacement])
            else:
                lines[observation + 1 : observation + 1] = replacement
        else:
            lines[start:end] = replacement
        content = "\n".join(lines) + ("\n" if trailing_newline else "")
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8", newline="\n")
        os.replace(temporary, self.path)
        return values

    @staticmethod
    def _locate(lines: list[str]) -> tuple[int | None, int]:
        in_observation = False
        for index, line in enumerate(lines):
            if line and not line.startswith(" "):
                in_observation = line == "observation:"
                continue
            if in_observation and line.startswith("  watchlist_symbols:"):
                end = index + 1
                while end < len(lines):
                    candidate = lines[end]
                    if candidate and not candidate.startswith("    "):
                        break
                    end += 1
                return index, end
        return None, len(lines)


@dataclass
class TaskSnapshot:
    id: str | None = None
    state: str = "idle"
    date: str | None = None
    mode: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    output: str = ""


PipelineRunner = Callable[[date, bool], tuple[int, str]]


class PipelineTaskManager:
    def __init__(self, runner: PipelineRunner) -> None:
        self.runner = runner
        self._lock = threading.Lock()
        self._snapshot = TaskSnapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._snapshot)

    def start(self, as_of_date: date, skip_data: bool) -> dict[str, Any]:
        with self._lock:
            if self._snapshot.state == "running":
                raise RuntimeError("已有任务正在运行，请等待完成。")
            task_id = uuid.uuid4().hex
            self._snapshot = TaskSnapshot(
                id=task_id,
                state="running",
                date=as_of_date.isoformat(),
                mode="existing" if skip_data else "update",
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
        thread = threading.Thread(
            target=self._run,
            args=(task_id, as_of_date, skip_data),
            name=f"qtrade-ui-{task_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self.snapshot()

    def _run(self, task_id: str, as_of_date: date, skip_data: bool) -> None:
        try:
            exit_code, output = self.runner(as_of_date, skip_data)
        except Exception as exc:  # UI task boundary must preserve unexpected failures.
            exit_code, output = 1, str(exc)
        with self._lock:
            if self._snapshot.id != task_id:
                return
            self._snapshot.state = "completed" if exit_code == 0 else "failed"
            self._snapshot.exit_code = exit_code
            self._snapshot.output = output[-12000:]
            self._snapshot.finished_at = datetime.now().isoformat(timespec="seconds")


class SubprocessPipelineRunner:
    def __init__(self, config_path: Path, working_directory: Path) -> None:
        self.config_path = Path(config_path).resolve()
        self.working_directory = Path(working_directory).resolve()

    def __call__(self, as_of_date: date, skip_data: bool) -> tuple[int, str]:
        command = [
            sys.executable,
            "-m",
            "qtrade",
            "--config",
            str(self.config_path),
            "pipeline",
            "daily",
            "--date",
            as_of_date.isoformat(),
        ]
        if skip_data:
            command.append("--skip-data")
        result = subprocess.run(
            command,
            cwd=self.working_directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1"},
            check=False,
        )
        output = "\n".join(
            value.strip() for value in (result.stdout, result.stderr) if value.strip()
        )
        return result.returncode, output
