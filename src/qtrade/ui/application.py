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

import polars as pl

from qtrade.research.protocols import ExperimentStore, PartitionName, ProtocolStore

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


@dataclass
class BacktestTaskSnapshot:
    id: str | None = None
    state: str = "idle"
    action: str | None = None
    protocol_id: str | None = None
    partition: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    output: str = ""
    result_id: str | None = None


BacktestRunner = Callable[[str, dict[str, Any]], tuple[int, str, str | None]]


class BacktestTaskManager:
    def __init__(self, runner: BacktestRunner) -> None:
        self.runner = runner
        self._lock = threading.Lock()
        self._snapshot = BacktestTaskSnapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._snapshot)

    def start(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in {"prepare", "run"}:
            raise ValueError("未知的回测任务。")
        protocol_id = str(payload.get("protocol_id", "")).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", protocol_id):
            raise ValueError("方案 ID 只能使用小写字母、数字、下划线和连字符。")
        with self._lock:
            if self._snapshot.state == "running":
                raise RuntimeError("已有回测任务正在运行，请等待完成。")
            task_id = uuid.uuid4().hex
            self._snapshot = BacktestTaskSnapshot(
                id=task_id,
                state="running",
                action=action,
                protocol_id=protocol_id,
                partition=(
                    str(payload.get("partition"))
                    if payload.get("partition")
                    else None
                ),
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
        thread = threading.Thread(
            target=self._run,
            args=(task_id, action, payload),
            name=f"qtrade-backtest-{task_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self.snapshot()

    def _run(
        self,
        task_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            exit_code, output, result_id = self.runner(action, payload)
        except Exception as exc:  # UI task boundary preserves unexpected failures.
            exit_code, output, result_id = 1, str(exc), None
        with self._lock:
            if self._snapshot.id != task_id:
                return
            self._snapshot.state = "completed" if exit_code == 0 else "failed"
            self._snapshot.exit_code = exit_code
            self._snapshot.output = output[-20000:]
            self._snapshot.result_id = result_id
            self._snapshot.finished_at = datetime.now().isoformat(timespec="seconds")


class BacktestRepository:
    def __init__(self, runtime_root: Path, reports_root: Path) -> None:
        self.protocols = ProtocolStore(runtime_root)
        self.experiments = ExperimentStore(runtime_root)
        self.reports_root = Path(reports_root).resolve()

    def meta(self) -> dict[str, Any]:
        protocols = []
        for item in self.protocols.list():
            state = self.protocols.state(item.protocol_id)
            trials = self.experiments.list(item.protocol_id)
            protocols.append(
                {
                    "protocol_id": item.protocol_id,
                    "title": item.title,
                    "hypothesis": item.hypothesis,
                    "status": item.status.value,
                    "allowed_trials": item.allowed_trials,
                    "partitions": {
                        partition.name.value: {
                            "start_date": partition.start_date.isoformat(),
                            "end_date": (
                                partition.end_date.isoformat()
                                if partition.end_date
                                else None
                            ),
                            "data_pinned": (
                                partition.name in item.partition_data_versions
                            ),
                        }
                        for partition in item.partitions
                        if partition.name != PartitionName.FORWARD
                    },
                    "holdout_revealed": state.holdout_revealed_at is not None,
                    "trial_counts": {
                        name.value: sum(
                            trial.partition == name for trial in trials
                        )
                        for name in (
                            PartitionName.DEVELOPMENT,
                            PartitionName.VALIDATION,
                            PartitionName.HOLDOUT,
                        )
                    },
                }
            )
        records = sorted(
            self.experiments.list(),
            key=lambda item: item.started_at,
            reverse=True,
        )
        results = [
            {
                "experiment_id": item.experiment_id,
                "protocol_id": item.protocol_id,
                "partition": item.partition.value if item.partition else "exploratory",
                "status": item.status.value,
                "start_date": item.start_date.isoformat(),
                "end_date": item.end_date.isoformat(),
                "started_at": item.started_at.isoformat(),
                "error": item.error,
                "has_result": bool(item.result_json and Path(item.result_json).is_file()),
            }
            for item in records[:50]
        ]
        return {"protocols": protocols, "results": results}

    def result(self, experiment_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]{32}", experiment_id):
            raise ValueError("无效的回测结果 ID。")
        record = self.experiments.load(experiment_id)
        if not record.result_json:
            raise FileNotFoundError("该实验尚未生成回测结果。")
        summary_path = Path(record.result_json).resolve()
        if (
            self.reports_root not in summary_path.parents
            or not summary_path.is_file()
        ):
            raise FileNotFoundError("回测结果文件不存在或不在报告目录内。")
        summary = _load_json(summary_path)
        if summary is None:
            raise ValueError("回测结果 JSON 无效。")
        directory = summary_path.parent
        curve_path = directory / "equity_curve.parquet"
        trades_path = directory / "rebalances.parquet"
        curve: list[dict[str, Any]] = []
        if curve_path.is_file():
            frame = pl.read_parquet(curve_path).select(
                "trade_date",
                "equity",
                "benchmark_equity",
            )
            if frame.height > 320:
                indices = sorted(
                    {
                        round(index * (frame.height - 1) / 319)
                        for index in range(320)
                    }
                )
                frame = frame[indices]
            curve = frame.with_columns(
                pl.col("trade_date").cast(pl.String)
            ).to_dicts()
        rebalances: list[dict[str, Any]] = []
        if trades_path.is_file():
            trades = pl.read_parquet(trades_path)
            available = [
                name
                for name in (
                    "signal_date",
                    "execution_date",
                    "status",
                    "holdings",
                    "turnover",
                    "blocked_buys",
                    "blocked_sells",
                )
                if name in trades.columns
            ]
            rebalances = (
                trades.select(available)
                .tail(30)
                .with_columns(
                    pl.col(name).cast(pl.String)
                    for name in ("signal_date", "execution_date")
                    if name in available
                )
                .to_dicts()
            )
        return {
            "experiment_id": experiment_id,
            "summary": summary,
            "curve": curve,
            "rebalances": rebalances,
        }


class SubprocessBacktestRunner:
    PARTITIONS = ("development", "validation", "holdout")

    def __init__(
        self,
        config_path: Path,
        working_directory: Path,
        runtime_root: Path,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.working_directory = Path(working_directory).resolve()
        self.protocols = ProtocolStore(runtime_root)
        self.experiments = ExperimentStore(runtime_root)

    def __call__(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> tuple[int, str, str | None]:
        return (
            self._prepare(payload)
            if action == "prepare"
            else self._run_backtest(payload)
        )

    def _base(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "qtrade",
            "--config",
            str(self.config_path),
        ]

    def _prepare(self, payload: dict[str, Any]) -> tuple[int, str, None]:
        protocol_id = str(payload["protocol_id"])
        outputs: list[str] = []
        try:
            protocol = self.protocols.load(protocol_id)
        except FileNotFoundError:
            required = (
                "title",
                "hypothesis",
                "development_start",
                "development_end",
                "validation_start",
                "validation_end",
                "holdout_start",
                "holdout_end",
            )
            if missing := [name for name in required if not payload.get(name)]:
                raise ValueError(
                    "创建方案缺少字段：" + ", ".join(missing)
                ) from None
            command = [
                *self._base(),
                "protocol",
                "create",
                "--id",
                protocol_id,
                "--title",
                str(payload["title"]),
                "--hypothesis",
                str(payload["hypothesis"]),
                "--development-start",
                str(payload["development_start"]),
                "--development-end",
                str(payload["development_end"]),
                "--validation-start",
                str(payload["validation_start"]),
                "--validation-end",
                str(payload["validation_end"]),
                "--holdout-start",
                str(payload["holdout_start"]),
                "--holdout-end",
                str(payload["holdout_end"]),
                "--signal-frequency",
                "month_end",
                "--allowed-trials",
                str(int(payload.get("allowed_trials", 3))),
            ]
            exit_code, output = self._execute(command)
            outputs.append("创建研究方案：\n" + output)
            if exit_code:
                return exit_code, "\n\n".join(outputs), None
            protocol = self.protocols.load(protocol_id)
        if protocol.status.value == "frozen":
            return 0, "方案已经冻结，可以直接运行回测。", None

        for partition in self.PARTITIONS:
            commands = (
                [
                    *self._base(),
                    "research",
                    "build-signals",
                    "--protocol",
                    protocol_id,
                    "--partition",
                    partition,
                    "--frequency",
                    "month_end",
                ],
                [
                    *self._base(),
                    "protocol",
                    "pin-data",
                    "--id",
                    protocol_id,
                    "--partition",
                    partition,
                ],
            )
            for label, command in zip(
                ("生成历史信号", "固定数据版本"),
                commands,
                strict=True,
            ):
                exit_code, output = self._execute(command)
                outputs.append(f"{label}（{partition}）：\n{output}")
                if exit_code:
                    return exit_code, "\n\n".join(outputs), None
        exit_code, output = self._execute(
            [*self._base(), "protocol", "freeze", "--id", protocol_id]
        )
        outputs.append("冻结研究方案：\n" + output)
        return exit_code, "\n\n".join(outputs), None

    def _run_backtest(
        self,
        payload: dict[str, Any],
    ) -> tuple[int, str, str | None]:
        protocol_id = str(payload["protocol_id"])
        partition = str(payload.get("partition", ""))
        if partition not in self.PARTITIONS:
            raise ValueError("回测区间必须是开发集、验证集或封存集。")
        if partition == "holdout" and not payload.get("confirm_holdout"):
            raise ValueError("揭晓封存集前必须勾选确认。")
        protocol = self.protocols.load(protocol_id)
        selected = protocol.partition(PartitionName(partition))
        assert selected.end_date is not None
        before = {
            item.experiment_id for item in self.experiments.list(protocol_id)
        }
        command = [
            *self._base(),
            "backtest",
            "candidates",
            "--start",
            selected.start_date.isoformat(),
            "--end",
            selected.end_date.isoformat(),
            "--protocol",
            protocol_id,
            "--partition",
            partition,
        ]
        if partition == "holdout":
            command.append("--reveal-holdout")
        exit_code, output = self._execute(command)
        created = [
            item
            for item in self.experiments.list(protocol_id)
            if item.experiment_id not in before
        ]
        result_id = (
            max(created, key=lambda item: item.started_at).experiment_id
            if created
            else None
        )
        return exit_code, output, result_id

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
            value.strip()
            for value in (result.stdout, result.stderr)
            if value.strip()
        )
        return result.returncode, output


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
