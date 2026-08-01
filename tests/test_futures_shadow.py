from __future__ import annotations

import hashlib
import json
from pathlib import Path

import polars as pl
import pytest

from qtrade.futures.shadow import FuturesShadowObservationService

SIGNAL_ID = "signal-shadow-1"
RESEARCH_ID = "research-shadow-1"
CONTRACT = "CU2609.SHF"
PROTOCOL_ID = "futures_trend_v1"
SIGNAL_PROTOCOL_ID = "5e8bc622c86e0742cc44"


def version(path: Path, root: Path) -> dict:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def write_shadow_inputs(root: Path) -> tuple[Path, Path, Path]:
    curated = root / "curated"
    reports = root / "reports"
    protocol_path = Path(__file__).parents[1] / "config" / "futures_trend_v1.yaml"

    research = curated / "futures" / "research" / f"build_id={RESEARCH_ID}"
    research.mkdir(parents=True)
    (research / "manifest.json").write_text(
        json.dumps({"build_id": RESEARCH_ID, "provider": "fake", "passed": True}),
        encoding="utf-8",
    )

    contracts = (
        curated
        / "futures"
        / "futures_contracts"
        / "provider=fake"
        / "as_of_date=2026-08-01"
    )
    contracts.mkdir(parents=True)
    pl.DataFrame(
        {
            "ts_code": [CONTRACT],
            "multiplier": [5.0],
            "per_unit": [10.0],
        }
    ).write_parquet(contracts / "data.parquet")

    signal = curated / "futures" / "signals" / f"build_id={SIGNAL_ID}"
    signal.mkdir(parents=True)
    pl.DataFrame(
        {
            "product_code": ["CU"],
            "contract_code": [CONTRACT],
            "target_signed_lots": [2],
            "initial_margin": [100.0],
            "stress_margin": [150.0],
        }
    ).write_parquet(signal / "targets.parquet")
    target_version = version(signal / "targets.parquet", signal)
    (signal / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": SIGNAL_ID,
                "passed": True,
                "protocol_id": SIGNAL_PROTOCOL_ID,
                "research_build_id": RESEARCH_ID,
                "previous_signal_build_id": None,
                "signal_date": "2026-08-03",
                "eligible_date": "2026-08-04",
                "equity": 1_000_000.0,
                "initial_margin": 100.0,
                "stress_margin": 150.0,
                "inputs": [version(contracts / "data.parquet", curated)],
                "output_versions": [target_version],
            }
        ),
        encoding="utf-8",
    )

    for dataset, frame in (
        (
            "futures_daily",
            pl.DataFrame(
                {
                    "ts_code": [CONTRACT],
                    "trade_date": ["20260804"],
                    "open": [100.0],
                    "high": [110.0],
                    "low": [90.0],
                    "settle": [102.0],
                    "vol": [1_000.0],
                }
            ),
        ),
        (
            "futures_settlements",
            pl.DataFrame(
                {
                    "ts_code": [CONTRACT],
                    "trade_date": ["20260804"],
                    "settle": [102.0],
                    "trading_fee_rate": [0.0],
                    "trading_fee": [2.0],
                    "long_margin_rate": [0.10],
                    "short_margin_rate": [0.12],
                }
            ),
        ),
        (
            "futures_limits",
            pl.DataFrame(
                {
                    "ts_code": [CONTRACT],
                    "trade_date": ["20260804"],
                    "up_limit": [120.0],
                    "down_limit": [80.0],
                }
            ),
        ),
    ):
        directory = (
            curated
            / "futures"
            / dataset
            / "provider=fake"
            / "as_of_date=2026-08-04"
        )
        directory.mkdir(parents=True)
        frame.write_parquet(directory / "data.parquet")

    request = root / "shadow.json"
    request.write_text(
        json.dumps(
            {
                "protocol_path": str(protocol_path),
                "signal_build_id": SIGNAL_ID,
                "observation_date": "2026-08-04",
            }
        ),
        encoding="utf-8",
    )
    return curated, reports, request


def test_shadow_observation_is_immutable_and_records_execution_gaps(
    tmp_path: Path,
) -> None:
    curated, reports, request = write_shadow_inputs(tmp_path)
    service = FuturesShadowObservationService(curated, reports)

    first = service.build(request)
    second = service.build(request)

    assert first.reused is False
    assert second.reused is True
    assert second.build_id == first.build_id
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    rows = pl.read_parquet(first.output_path).to_dicts()
    assert manifest["protocol_id"] == PROTOCOL_ID
    assert manifest["protocol_hash"]
    assert manifest["night_session_status"] == "not_observable_from_daily_data"
    assert manifest["availability_basis"] == "exact_date_archived_partitions"
    assert manifest["recorded_at"].endswith("+00:00")
    assert manifest["fully_executable"] is True
    assert manifest["theoretical_fee"] == 20.0
    assert manifest["observed_initial_margin"] == 102.0
    assert manifest["initial_margin_deviation"] == 2.0
    assert "total_return" not in manifest
    assert rows[0]["status"] == "filled"
    assert rows[0]["fill_price"] == 110.0
    assert rows[0]["limit_status"] == "within_limits"
    assert rows[0]["fee_actual_status"] == "broker_actual_unavailable"
    assert all(item["sha256"] for item in manifest["inputs"])
    assert all(item["sha256"] for item in manifest["output_versions"])


def test_shadow_observation_blocks_missing_limits_without_partial_output(
    tmp_path: Path,
) -> None:
    curated, reports, request = write_shadow_inputs(tmp_path)
    limits = (
        curated
        / "futures"
        / "futures_limits"
        / "provider=fake"
        / "as_of_date=2026-08-04"
        / "data.parquet"
    )
    limits.unlink()

    with pytest.raises(FileNotFoundError, match="futures_limits"):
        FuturesShadowObservationService(curated, reports).build(request)

    assert not (curated / "futures" / "shadow-observations").exists()


def test_shadow_observation_rejects_tampered_signal(tmp_path: Path) -> None:
    curated, reports, request = write_shadow_inputs(tmp_path)
    targets = (
        curated
        / "futures"
        / "signals"
        / f"build_id={SIGNAL_ID}"
        / "targets.parquet"
    )
    targets.write_bytes(targets.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="hash mismatch"):
        FuturesShadowObservationService(curated, reports).build(request)


def test_shadow_observation_records_locked_limit_as_real_blocker(
    tmp_path: Path,
) -> None:
    curated, reports, request = write_shadow_inputs(tmp_path)
    daily_path = (
        curated
        / "futures"
        / "futures_daily"
        / "provider=fake"
        / "as_of_date=2026-08-04"
        / "data.parquet"
    )
    daily = pl.read_parquet(daily_path).with_columns(
        pl.lit(120.0).alias("open"),
        pl.lit(120.0).alias("high"),
        pl.lit(120.0).alias("low"),
    )
    daily.write_parquet(daily_path)

    result = FuturesShadowObservationService(curated, reports).build(request)
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    row = pl.read_parquet(result.output_path).row(0, named=True)

    assert manifest["fully_executable"] is False
    assert manifest["blocked_execution_rows"] == 1
    assert row["status"] == "blocked"
    assert row["reason"] == "locked_limit_up"
    assert row["fill_price"] is None


def test_shadow_observation_records_both_roll_legs(tmp_path: Path) -> None:
    curated, reports, request = write_shadow_inputs(tmp_path)
    old_contract = "CU2608.SHF"
    signal_dir = curated / "futures" / "signals" / f"build_id={SIGNAL_ID}"
    manifest_path = signal_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["previous_signal_build_id"] = "signal-shadow-0"

    previous_dir = curated / "futures" / "signals" / "build_id=signal-shadow-0"
    previous_dir.mkdir(parents=True)
    pl.DataFrame(
        {
            "product_code": ["CU"],
            "contract_code": [old_contract],
            "target_signed_lots": [1],
        }
    ).write_parquet(previous_dir / "targets.parquet")
    (previous_dir / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": "signal-shadow-0",
                "passed": True,
                "protocol_id": SIGNAL_PROTOCOL_ID,
                "research_build_id": RESEARCH_ID,
                "signal_date": "2026-08-02",
                "eligible_date": "2026-08-03",
                "output_versions": [version(previous_dir / "targets.parquet", previous_dir)],
            }
        ),
        encoding="utf-8",
    )

    contract_path = next(
        (curated / "futures" / "futures_contracts").glob(
            "provider=*/as_of_date=*/data.parquet"
        )
    )
    contracts = pl.concat(
        [
            pl.read_parquet(contract_path),
            pl.DataFrame(
                {"ts_code": [old_contract], "multiplier": [5.0], "per_unit": [10.0]}
            ),
        ]
    )
    contracts.write_parquet(contract_path)
    manifest["inputs"] = [version(contract_path, curated)]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    additions = {
        "futures_daily": {
            "ts_code": [old_contract],
            "trade_date": ["20260804"],
            "open": [99.0],
            "high": [105.0],
            "low": [95.0],
            "settle": [100.0],
            "vol": [800.0],
        },
        "futures_settlements": {
            "ts_code": [old_contract],
            "trade_date": ["20260804"],
            "settle": [100.0],
            "trading_fee_rate": [0.0],
            "trading_fee": [2.0],
            "long_margin_rate": [0.10],
            "short_margin_rate": [0.12],
        },
        "futures_limits": {
            "ts_code": [old_contract],
            "trade_date": ["20260804"],
            "up_limit": [120.0],
            "down_limit": [80.0],
        },
    }
    for dataset, row in additions.items():
        path = (
            curated
            / "futures"
            / dataset
            / "provider=fake"
            / "as_of_date=2026-08-04"
            / "data.parquet"
        )
        pl.concat([pl.read_parquet(path), pl.DataFrame(row)]).write_parquet(path)

    result = FuturesShadowObservationService(curated, reports).build(request)
    shadow_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    rows = pl.read_parquet(result.output_path).sort("leg").to_dicts()

    assert shadow_manifest["roll_leg_rows"] == 2
    assert {row["leg"] for row in rows} == {"roll_close", "roll_open"}
    assert all(row["is_roll"] for row in rows)
    assert all(row["status"] == "filled" for row in rows)
