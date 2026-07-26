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


def recent_quarter_ends(as_of_date: date, count: int = 5) -> tuple[str, ...]:
    values = [
        date(year, month, day)
        for year in range(as_of_date.year - 2, as_of_date.year + 1)
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
        if date(year, month, day) <= as_of_date
    ]
    return tuple(value.strftime("%Y%m%d") for value in sorted(values, reverse=True)[:count])


class PipelineTaskManager:
    def __init__(self, runner: PipelineRunner) -> None:
        self.runner = runner
        self._lock = threading.Lock()
        self._snapshot = TaskSnapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._snapshot)

    def start(self, as_of_date: date, skip_data: bool) -> dict[str, Any]:
        validator = getattr(self.runner, "validate", None)
        if validator is not None:
            validator(as_of_date, skip_data)
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
    DAILY_INPUTS = (
        "daily_prices",
        "adjust_factors",
        "index_daily",
        "daily_basic",
        "stock_limit",
    )

    def __init__(
        self,
        config_path: Path,
        working_directory: Path,
        curated_root: Path,
        provider: str,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.working_directory = Path(working_directory).resolve()
        self.curated_root = Path(curated_root).resolve()
        self.provider = provider

    def validate(self, as_of_date: date, skip_data: bool) -> None:
        if as_of_date > date.today():
            raise ValueError(
                f"{as_of_date.isoformat()} 尚未到来，请选择已经收盘的交易日。"
            )
        if as_of_date.weekday() >= 5:
            raise ValueError(
                f"{as_of_date.isoformat()} 是周末，请选择已经收盘的交易日。"
            )
        if not skip_data:
            return
        missing = [
            dataset
            for dataset in self.DAILY_INPUTS
            if not self._exact_partition_exists(dataset, as_of_date)
        ]
        if not self._latest_partition_exists("security_master", as_of_date):
            missing.append("security_master")
        if not self._latest_partition_exists("financial_indicators", as_of_date):
            missing.append("financial_indicators")
        if missing:
            names = ", ".join(missing)
            raise ValueError(
                f"{as_of_date.isoformat()} 缺少本地分析数据：{names}。"
                "请先点击“更新数据并分析”。"
            )

    def __call__(self, as_of_date: date, skip_data: bool) -> tuple[int, str]:
        outputs: list[str] = []
        if not skip_data and not self._exact_partition_exists(
            "financial_indicators", as_of_date
        ):
            periods = ",".join(recent_quarter_ends(as_of_date))
            financial_command = [
                sys.executable,
                "-m",
                "qtrade",
                "--config",
                str(self.config_path),
                "data",
                "financials",
                "--date",
                as_of_date.isoformat(),
                "--periods",
                periods,
            ]
            exit_code, output = self._execute(financial_command)
            outputs.append("准备财务快照：\n" + output)
            if exit_code != 0:
                return exit_code, "\n\n".join(outputs)

        pipeline_command = [
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
            pipeline_command.append("--skip-data")
        exit_code, output = self._execute(pipeline_command)
        outputs.append("运行日终分析：\n" + output)
        return exit_code, "\n\n".join(outputs)

    def _execute(self, command: list[str]) -> tuple[int, str]:
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

    def _partition_root(self, dataset: str) -> Path:
        return self.curated_root / dataset / f"provider={self.provider}"

    def _exact_partition_exists(self, dataset: str, as_of_date: date) -> bool:
        return (
            self._partition_root(dataset)
            / f"as_of_date={as_of_date.isoformat()}"
            / "data.parquet"
        ).is_file()

    def _latest_partition_exists(self, dataset: str, as_of_date: date) -> bool:
        root = self._partition_root(dataset)
        if not root.exists():
            return False
        for item in root.glob("as_of_date=*"):
            try:
                partition_date = date.fromisoformat(item.name.removeprefix("as_of_date="))
            except ValueError:
                continue
            if partition_date <= as_of_date and (item / "data.parquet").is_file():
                return True
        return False
