from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from qtrade.futures.trend_service import FuturesTrendService

RESEARCH_BUILD_ID = "trend-source-1"
SIGNAL_DATE = date(2025, 5, 11)
ELIGIBLE_DATE = date(2025, 5, 12)
CONTRACT = "CU2607.SHF"


def file_version(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def write_source_build(curated: Path, *, eligible: bool = True) -> None:
    daily_path = (
        curated
        / "futures"
        / "futures_daily"
        / "provider=tushare"
        / f"as_of_date={SIGNAL_DATE.isoformat()}"
        / "data.parquet"
    )
    contract_path = (
        curated
        / "futures"
        / "futures_contracts"
        / "provider=tushare"
        / f"as_of_date={SIGNAL_DATE.isoformat()}"
        / "data.parquet"
    )
    settlement_path = (
        curated
        / "futures"
        / "futures_settlements"
        / "provider=tushare"
        / f"as_of_date={SIGNAL_DATE.isoformat()}"
        / "data.parquet"
    )
    daily_path.parent.mkdir(parents=True)
    contract_path.parent.mkdir(parents=True)
    settlement_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "ts_code": [CONTRACT],
            "trade_date": [SIGNAL_DATE.strftime("%Y%m%d")],
            "settle": [800.0],
        }
    ).write_parquet(daily_path)
    pl.DataFrame({"ts_code": [CONTRACT], "multiplier": [5.0]}).write_parquet(contract_path)
    pl.DataFrame(
        {
            "ts_code": [CONTRACT],
            "trade_date": [SIGNAL_DATE.strftime("%Y%m%d")],
            "long_margin_rate": [0.10],
            "short_margin_rate": [0.12],
        }
    ).write_parquet(settlement_path)

    research = curated / "futures" / "research" / f"build_id={RESEARCH_BUILD_ID}"
    research.mkdir(parents=True)
    dates = [SIGNAL_DATE - timedelta(days=130 - index) for index in range(132)]
    prices = [100.0]
    for index in range(1, len(dates)):
        prices.append(prices[-1] * (1.006 if index % 2 else 1.002))
    pl.DataFrame(
        {
            "trade_date": [value.isoformat() for value in dates],
            "product_code": ["CU"] * len(dates),
            "continuous_index": prices,
        }
    ).write_parquet(research / "continuous.parquet")
    pl.DataFrame(
        {
            "trade_date": [SIGNAL_DATE.isoformat()],
            "product_code": ["CU"],
            "eligible": [eligible],
        }
    ).write_parquet(research / "universe.parquet")
    pl.DataFrame(
        {
            "decision_date": [SIGNAL_DATE.isoformat()],
            "effective_date": [ELIGIBLE_DATE.isoformat()],
            "product_code": ["CU"],
            "selected_contract": [CONTRACT],
            "universe_eligible": [eligible],
        }
    ).write_parquet(research / "roll_schedule.parquet")
    output_versions = [
        file_version(research / filename, research)
        for filename in ("continuous.parquet", "universe.parquet", "roll_schedule.parquet")
    ]
    manifest = {
        "build_id": RESEARCH_BUILD_ID,
        "passed": True,
        "provider": "tushare",
        "contract_master_partition_date": SIGNAL_DATE.isoformat(),
        "inputs": [file_version(daily_path, curated), file_version(contract_path, curated)],
        "output_versions": output_versions,
    }
    (research / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def write_input(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "research_build_id": RESEARCH_BUILD_ID,
                "signal_date": SIGNAL_DATE.isoformat(),
                "eligible_date": ELIGIBLE_DATE.isoformat(),
                "equity": 10_000_000,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_trend_snapshot_is_content_addressed_and_reused(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_source_build(curated)
    input_path = tmp_path / "trend.json"
    write_input(input_path)
    service = FuturesTrendService(curated, tmp_path / "reports")

    first = service.build(input_path)
    second = service.build(input_path)

    assert not first.reused
    assert second.reused
    assert second.build_id == first.build_id
    assert first.target_rows == 1
    target = pl.read_parquet(first.output_dir / "targets.parquet").row(0, named=True)
    assert target["contract_code"] == CONTRACT
    assert target["sector"] == "base_metals"
    assert target["target_signed_lots"] > 0
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["research_build_id"] == RESEARCH_BUILD_ID
    assert manifest["protocol_id"]
    assert manifest["sector_registry_id"]
    assert manifest["initial_margin"] <= manifest["equity"] * 0.25
    assert manifest["stress_margin"] <= manifest["equity"] * 0.50
    assert first.report_path.is_file()


def test_trend_snapshot_allows_an_empty_point_in_time_universe(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_source_build(curated, eligible=False)
    input_path = tmp_path / "trend.json"
    write_input(input_path)

    result = FuturesTrendService(curated, tmp_path / "reports").build(input_path)

    assert result.target_rows == 0
    assert pl.read_parquet(result.output_dir / "targets.parquet").is_empty()


def test_trend_snapshot_rejects_a_changed_declared_source_partition(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_source_build(curated)
    daily_path = (
        curated
        / "futures"
        / "futures_daily"
        / "provider=tushare"
        / f"as_of_date={SIGNAL_DATE.isoformat()}"
        / "data.parquet"
    )
    pl.DataFrame({"ts_code": [CONTRACT], "settle": [999.0]}).write_parquet(daily_path)
    input_path = tmp_path / "trend.json"
    write_input(input_path)

    with pytest.raises(ValueError, match="changed after build"):
        FuturesTrendService(curated, tmp_path / "reports").build(input_path)

    assert not (curated / "futures" / "signals").exists()


def test_trend_snapshot_rejects_a_changed_research_output(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_source_build(curated)
    research = curated / "futures" / "research" / f"build_id={RESEARCH_BUILD_ID}"
    pl.read_parquet(research / "continuous.parquet").with_columns(
        (pl.col("continuous_index") * 2).alias("continuous_index")
    ).write_parquet(research / "continuous.parquet")
    input_path = tmp_path / "trend.json"
    write_input(input_path)

    with pytest.raises(ValueError, match="research output changed after build"):
        FuturesTrendService(curated, tmp_path / "reports").build(input_path)

    assert not (curated / "futures" / "signals").exists()


def test_trend_snapshot_rejects_missing_signal_date_margin_rates(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    write_source_build(curated)
    settlement_path = (
        curated
        / "futures"
        / "futures_settlements"
        / "provider=tushare"
        / f"as_of_date={SIGNAL_DATE.isoformat()}"
        / "data.parquet"
    )
    settlement_path.unlink()
    input_path = tmp_path / "trend.json"
    write_input(input_path)

    with pytest.raises(FileNotFoundError, match="settlement partition"):
        FuturesTrendService(curated, tmp_path / "reports").build(input_path)

    assert not (curated / "futures" / "signals").exists()
