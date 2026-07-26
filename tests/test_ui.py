import json
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest

from qtrade.ui.application import (
    OverviewRepository,
    PipelineTaskManager,
    SubprocessPipelineRunner,
    WatchlistEditor,
    recent_quarter_ends,
)


def test_overview_repository_lists_dates_and_loads_reports(tmp_path: Path) -> None:
    market = tmp_path / "market" / "2026-07-24"
    pipeline = tmp_path / "pipeline" / "2026-07-23"
    market.mkdir(parents=True)
    pipeline.mkdir(parents=True)
    (market / "market.json").write_text(
        json.dumps({"state": "balanced"}),
        encoding="utf-8",
    )
    (pipeline / "run.json").write_text(
        json.dumps({"status": "success"}),
        encoding="utf-8",
    )

    repository = OverviewRepository(tmp_path)

    assert repository.available_dates() == ["2026-07-24"]
    overview = repository.overview(date(2026, 7, 24))
    assert overview["market"] == {"state": "balanced"}
    assert overview["pipeline"] is None


def test_watchlist_editor_preserves_other_configuration(tmp_path: Path) -> None:
    config = tmp_path / "base.yaml"
    config.write_text(
        "app:\n"
        "  name: qtrade\n"
        "observation:\n"
        "  watchlist_symbols: []\n"
        "  candidate_count: 20\n"
        "validation:\n"
        "  fail_on_warning: false\n",
        encoding="utf-8",
    )
    editor = WatchlistEditor(config)

    values = editor.write(["600519.sh", "000858.SZ", "600519.SH"])

    assert values == ["600519.SH", "000858.SZ"]
    assert editor.read() == values
    content = config.read_text(encoding="utf-8")
    assert "  candidate_count: 20" in content
    assert "validation:" in content


def test_watchlist_editor_rejects_invalid_symbol(tmp_path: Path) -> None:
    config = tmp_path / "base.yaml"
    config.write_text("observation:\n  watchlist_symbols: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="无效代码"):
        WatchlistEditor(config).write(["贵州茅台"])


def test_pipeline_task_manager_runs_one_task_and_records_output() -> None:
    def runner(as_of_date: date, skip_data: bool) -> tuple[int, str]:
        assert as_of_date == date(2026, 7, 24)
        assert skip_data is True
        return 0, "completed"

    manager = PipelineTaskManager(runner)
    started = manager.start(date(2026, 7, 24), skip_data=True)

    assert started["state"] in {"running", "completed"}
    for _ in range(100):
        snapshot = manager.snapshot()
        if snapshot["state"] != "running":
            break
        time.sleep(0.005)

    assert snapshot["state"] == "completed"
    assert snapshot["exit_code"] == 0
    assert snapshot["output"] == "completed"


def test_recent_quarter_ends_includes_latest_completed_quarter() -> None:
    assert recent_quarter_ends(date(2026, 7, 17)) == (
        "20260630",
        "20260331",
        "20251231",
        "20250930",
        "20250630",
    )


def test_pipeline_runner_rejects_future_and_incomplete_existing_data(
    tmp_path: Path,
) -> None:
    runner = SubprocessPipelineRunner(
        config_path=tmp_path / "base.yaml",
        working_directory=tmp_path,
        curated_root=tmp_path / "curated",
        provider="fake",
    )

    with pytest.raises(ValueError, match="尚未到来"):
        runner.validate(date.max, skip_data=False)
    with pytest.raises(ValueError, match="缺少本地分析数据"):
        runner.validate(date(2026, 7, 17), skip_data=True)


def test_pipeline_runner_fetches_financials_before_online_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="success", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = SubprocessPipelineRunner(
        config_path=tmp_path / "base.yaml",
        working_directory=tmp_path,
        curated_root=tmp_path / "curated",
        provider="fake",
    )

    exit_code, output = runner(date(2026, 7, 17), skip_data=False)

    assert exit_code == 0
    assert len(commands) == 2
    assert "financials" in commands[0]
    assert commands[0][commands[0].index("--periods") + 1].startswith("20260630,")
    assert commands[1][-4:] == ["pipeline", "daily", "--date", "2026-07-17"]
    assert "准备财务快照" in output


def test_pipeline_runner_accepts_complete_existing_inputs(tmp_path: Path) -> None:
    runner = SubprocessPipelineRunner(
        config_path=tmp_path / "base.yaml",
        working_directory=tmp_path,
        curated_root=tmp_path / "curated",
        provider="fake",
    )
    day = date(2026, 7, 17)
    for dataset in (*runner.DAILY_INPUTS, "security_master", "financial_indicators"):
        partition = (
            tmp_path
            / "curated"
            / dataset
            / "provider=fake"
            / f"as_of_date={day.isoformat()}"
        )
        partition.mkdir(parents=True)
        (partition / "data.parquet").touch()

    runner.validate(day, skip_data=True)
