from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from qtrade.config import FuturesConfig
from qtrade.futures.backtest_service import FuturesBacktestService

RESEARCH_BUILD_ID = "research-source-1"
CONTRACT = "A2609.DCE"


def write_research_build(root: Path) -> None:
    directory = root / "futures" / "research" / f"build_id={RESEARCH_BUILD_ID}"
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text(
        json.dumps({"build_id": RESEARCH_BUILD_ID, "passed": True}),
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "product_code": ["A"],
            "effective_date": ["2026-01-06"],
            "selected_contract": [CONTRACT],
        }
    ).write_parquet(directory / "roll_schedule.parquet")


def input_payload(contract_code: str = CONTRACT, signed_lots: int = 1) -> dict:
    return {
        "research_build_id": RESEARCH_BUILD_ID,
        "initial_equity": 10_000,
        "days": [
            {
                "trade_date": "2026-01-05",
                "next_trade_date": "2026-01-06",
                "bars": [],
                "settlements": [],
                "rebalance_id": "trend-2026-01-05",
                "targets": [
                    {
                        "product_code": "A",
                        "contract_code": contract_code,
                        "signed_lots": signed_lots,
                        "multiplier": 10,
                        "tick_size": 1,
                    }
                ],
            },
            {
                "trade_date": "2026-01-06",
                "next_trade_date": "2026-01-07",
                "bars": [
                    {
                        "contract_code": CONTRACT,
                        "open": 100,
                        "high": 105,
                        "low": 95,
                        "volume": 1_000,
                        "up_limit": 110,
                        "down_limit": 90,
                    }
                ],
                "settlements": [
                    {
                        "contract_code": CONTRACT,
                        "settlement_price": 102,
                        "long_margin_rate": 0.1,
                        "short_margin_rate": 0.12,
                        "position_direction": "long",
                    }
                ],
            },
        ],
    }


def write_input(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def test_backtest_build_is_content_addressed_complete_and_reused(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    reports = tmp_path / "reports"
    write_research_build(curated)
    input_path = tmp_path / "backtest.json"
    write_input(input_path, input_payload())
    service = FuturesBacktestService(FuturesConfig(), curated, reports)

    first = service.build(input_path)
    second = service.build(input_path)

    assert first.passed
    assert not first.reused
    assert second.reused
    assert second.build_id == first.build_id
    assert first.day_rows == 2
    assert first.order_rows == 1
    assert first.execution_rows == 1
    assert first.report_path.is_file()
    for filename in service.OUTPUT_FILES.values():
        assert (first.output_dir / filename).is_file()
        pl.read_parquet(first.output_dir / filename)
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["research_build_id"] == RESEARCH_BUILD_ID
    assert len(manifest["inputs"]) == 3


def test_backtest_input_change_creates_new_build(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_research_build(curated)
    input_path = tmp_path / "backtest.json"
    service = FuturesBacktestService(FuturesConfig(), curated, tmp_path / "reports")
    write_input(input_path, input_payload(signed_lots=1))
    first = service.build(input_path)

    write_input(input_path, input_payload(signed_lots=2))
    second = service.build(input_path)

    assert second.build_id != first.build_id
    assert not second.reused


def test_backtest_rejects_contract_not_selected_by_point_in_time_roll(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_research_build(curated)
    input_path = tmp_path / "backtest.json"
    write_input(input_path, input_payload(contract_code="A2701.DCE"))

    with pytest.raises(ValueError, match="violates point-in-time roll schedule"):
        FuturesBacktestService(FuturesConfig(), curated, tmp_path / "reports").build(input_path)

    assert not (curated / "futures" / "backtests").exists()
