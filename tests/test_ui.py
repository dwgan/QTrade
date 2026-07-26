import json
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from qtrade.data.storage import ParquetDatasetStore
from qtrade.domain import DataBatch, Dataset
from qtrade.research.protocols import ExperimentRecord, ExperimentStore
from qtrade.ui.application import (
    BacktestRepository,
    BacktestTaskManager,
    OverviewRepository,
    PipelineTaskManager,
    SubprocessBacktestRunner,
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


def test_backtest_task_manager_records_result_id() -> None:
    def runner(action: str, payload: dict) -> tuple[int, str, str]:
        assert action == "run"
        assert payload["partition"] == "validation"
        return 0, "backtest completed", "a" * 32

    manager = BacktestTaskManager(runner)
    manager.start(
        "run",
        {
            "protocol_id": "quality_v1",
            "partition": "validation",
        },
    )
    for _ in range(100):
        snapshot = manager.snapshot()
        if snapshot["state"] != "running":
            break
        time.sleep(0.005)

    assert snapshot["state"] == "completed"
    assert snapshot["result_id"] == "a" * 32
    assert snapshot["output"] == "backtest completed"


def test_backtest_repository_loads_summary_and_downsampled_curve(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    result_dir = reports / "research/backtests/quality_v1/experiment"
    result_dir.mkdir(parents=True)
    summary = result_dir / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "protocol_id": "quality_v1",
                "portfolio": {"total_return": 0.2},
            }
        ),
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "trade_date": [
                (date(2020, 1, 1) + timedelta(days=index)).isoformat()
                for index in range(400)
            ],
            "equity": list(range(400)),
            "benchmark_equity": list(range(400)),
        }
    ).write_parquet(result_dir / "equity_curve.parquet")
    pl.DataFrame(
        {
            "signal_date": [date(2020, 1, 31), date(2020, 2, 28)],
            "execution_date": [date(2020, 2, 3), date(2020, 3, 2)],
            "status": ["completed", "completed"],
            "holdings": [2, 2],
            "holding_codes": [
                ["000001.SZ", "000002.SZ"],
                ["000002.SZ", "000003.SZ"],
            ],
            "turnover": [1.0, 1.0],
            "transaction_cost": [100.0, 100.0],
            "slippage_cost": [30.0, 30.0],
            "blocked_buys": [0, 0],
            "blocked_sells": [0, 0],
            "equity_after_cost": [999_870.0, 1_009_740.0],
        }
    ).write_parquet(result_dir / "rebalances.parquet")
    runtime = tmp_path / "runtime"
    experiments = ExperimentStore(runtime)
    record = experiments.create(
        ExperimentRecord(
            protocol_id="quality_v1",
            kind="candidate_backtest",
            start_date=date(2020, 1, 1),
            end_date=date(2020, 12, 31),
            code_commit="abc",
            config_hash="def",
        )
    )
    experiments.complete(
        record.experiment_id,
        data_version="1" * 64,
        result_json=summary,
        result_markdown=result_dir / "summary.md",
    )

    result = BacktestRepository(runtime, reports).result(record.experiment_id)

    assert result["summary"]["portfolio"]["total_return"] == 0.2
    assert len(result["curve"]) == 320
    assert result["curve"][0]["trade_date"] == "2020-01-01"
    assert len(result["positions"]) == 2
    assert result["positions"][0]["holdings"][0]["change"] == "added"
    assert {
        item["ts_code"] for item in result["positions"][1]["holdings"]
        if item["change"] == "added"
    } == {"000003.SZ"}
    assert result["positions"][1]["removed"] == [
        {"ts_code": "000001.SZ", "name": None}
    ]
    daily_by_date = {
        item["trade_date"]: item["codes"] for item in result["daily_positions"]
    }
    assert daily_by_date["2020-02-03"] == ["000001.SZ", "000002.SZ"]
    assert daily_by_date["2020-03-02"] == ["000002.SZ", "000003.SZ"]


def test_backtest_repository_loads_adjusted_security_chart_and_markers(
    tmp_path: Path,
) -> None:
    reports = tmp_path / "reports"
    result_dir = reports / "research/backtests/quality_v1/experiment"
    result_dir.mkdir(parents=True)
    summary = result_dir / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "protocol_id": "quality_v1",
                "start_date": "2020-02-03",
                "end_date": "2020-03-02",
                "signal_versions": {},
            }
        ),
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "signal_date": [date(2020, 1, 31), date(2020, 2, 28)],
            "execution_date": [date(2020, 2, 3), date(2020, 3, 2)],
            "holding_codes": [["000001.SZ"], []],
        }
    ).write_parquet(result_dir / "rebalances.parquet")
    runtime = tmp_path / "runtime"
    experiments = ExperimentStore(runtime)
    record = experiments.create(
        ExperimentRecord(
            protocol_id="quality_v1",
            kind="candidate_backtest",
            start_date=date(2020, 2, 3),
            end_date=date(2020, 3, 2),
            code_commit="abc",
            config_hash="def",
        )
    )
    experiments.complete(
        record.experiment_id,
        data_version="1" * 64,
        result_json=summary,
        result_markdown=result_dir / "summary.md",
    )
    curated_root = tmp_path / "curated"
    store = ParquetDatasetStore(curated_root, "curated")
    for trade_date, prices, factor in (
        (date(2020, 2, 3), (10.0, 11.0, 9.0, 10.5), 1.0),
        (date(2020, 3, 2), (20.0, 22.0, 19.0, 21.0), 2.0),
    ):
        store.write(
            DataBatch(
                Dataset.DAILY_PRICES,
                "fake",
                trade_date,
                pl.DataFrame(
                    {
                        "ts_code": ["000001.SZ"],
                        "trade_date": [trade_date],
                        "open": [prices[0]],
                        "high": [prices[1]],
                        "low": [prices[2]],
                        "close": [prices[3]],
                        "vol": [1000.0],
                    }
                ),
            )
        )
        store.write(
            DataBatch(
                Dataset.ADJUST_FACTORS,
                "fake",
                trade_date,
                pl.DataFrame(
                    {
                        "ts_code": ["000001.SZ"],
                        "trade_date": [trade_date],
                        "adj_factor": [factor],
                    }
                ),
            )
        )

    chart = BacktestRepository(
        runtime,
        reports,
        curated_root,
        "fake",
    ).security_chart(record.experiment_id, "000001.SZ")

    assert chart["price_mode"] == "forward_adjusted_to_backtest_end"
    assert chart["bars"][0]["open"] == 5.0
    assert chart["bars"][1]["close"] == 21.0
    assert chart["markers"] == [
        {"trade_date": "2020-02-03", "side": "buy"},
        {"trade_date": "2020-03-02", "side": "sell"},
    ]


def test_preparing_an_already_frozen_protocol_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SubprocessBacktestRunner(
        config_path=tmp_path / "base.yaml",
        working_directory=tmp_path,
        runtime_root=tmp_path / "runtime",
    )
    monkeypatch.setattr(
        runner.protocols,
        "load",
        lambda _: SimpleNamespace(status=SimpleNamespace(value="frozen")),
    )

    exit_code, output, result_id = runner(
        "prepare",
        {"protocol_id": "quality_v1"},
    )

    assert exit_code == 0
    assert "已经冻结" in output
    assert result_id is None


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
