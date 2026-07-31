from __future__ import annotations

import polars as pl
import pytest

from qtrade.futures.risk import FuturesBacktestDataGate


def requirements() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "contract_code": ["CU2608.SHF", "CU2608.SHF"],
            "trade_date": ["2026-07-31", "2026-07-31"],
            "direction": ["long", "short"],
        }
    )


def contracts() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["CU2608.SHF"],
            "multiplier": [5.0],
            "observed_at": ["20260730"],
        }
    )


def daily() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["CU2608.SHF"],
            "trade_date": ["20260731"],
            "open": [80_000.0],
            "settle": [80_100.0],
            "vol": [10_000.0],
        }
    )


def settlements() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["CU2608.SHF"],
            "trade_date": ["20260731"],
            "long_margin_rate": [0.10],
            "short_margin_rate": [0.12],
        }
    )


def limits() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_code": ["CU2608.SHF"],
            "trade_date": ["20260731"],
            "up_limit": [88_000.0],
            "down_limit": [72_000.0],
        }
    )


def test_complete_historical_execution_data_passes_gate() -> None:
    readiness = FuturesBacktestDataGate().validate(
        requirements(),
        contracts(),
        daily(),
        settlements(),
        limits(),
    )

    assert readiness.ready
    assert readiness.required_rows == 2
    readiness.require_ready()


def test_missing_execution_margin_and_limits_are_separate_blockers() -> None:
    broken_daily = daily().with_columns(pl.lit(0.0).alias("open"))
    broken_settlements = settlements().with_columns(pl.lit(None).alias("long_margin_rate"))
    readiness = FuturesBacktestDataGate().validate(
        requirements().filter(pl.col("direction") == "long"),
        contracts(),
        broken_daily,
        broken_settlements,
        limits().clear(),
    )

    assert not readiness.ready
    assert {issue.code for issue in readiness.issues} == {
        "missing_execution_bar",
        "missing_margin_rate",
        "missing_price_limits",
    }
    with pytest.raises(ValueError, match="missing_price_limits=1"):
        readiness.require_ready()


def test_duplicate_source_keys_are_not_silently_overwritten() -> None:
    duplicated_daily = pl.concat([daily(), daily()])

    readiness = FuturesBacktestDataGate().validate(
        requirements().head(1),
        contracts(),
        duplicated_daily,
        settlements(),
        limits(),
    )

    assert not readiness.ready
    issue = next(item for item in readiness.issues if item.code == "duplicate_daily_key")
    assert issue.rows == 2


def test_invalid_requirement_direction_is_rejected() -> None:
    invalid = requirements().head(1).with_columns(pl.lit("flat").alias("direction"))

    with pytest.raises(ValueError, match="long or short"):
        FuturesBacktestDataGate().validate(
            invalid,
            contracts(),
            daily(),
            settlements(),
            limits(),
        )
