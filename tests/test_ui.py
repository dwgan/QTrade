import json
import time
from datetime import date
from pathlib import Path

import pytest

from qtrade.ui.application import (
    OverviewRepository,
    PipelineTaskManager,
    WatchlistEditor,
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
