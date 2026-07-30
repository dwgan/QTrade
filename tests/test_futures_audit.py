from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from qtrade.config import FuturesConfig
from qtrade.futures.audit import FuturesDataAuditService


class FakeFuturesSource:
    name = "fake"

    def query(self, operation: str, **params: Any) -> pl.DataFrame:
        if operation == "fut_basic":
            return pl.DataFrame(
                {
                    "ts_code": ["CU2608.SHF"],
                    "symbol": ["CU2608"],
                    "name": ["沪铜2608"],
                    "exchange": ["SHFE"],
                    "fut_code": ["CU"],
                    "multiplier": [None],
                    "trade_unit": ["吨/手"],
                    "per_unit": [5.0],
                    "list_date": ["20250818"],
                    "delist_date": ["20260817"],
                    "trade_time_desc": ["21:00-01:00; 09:00-15:00"],
                }
            )
        if operation == "fut_daily":
            return pl.DataFrame(
                {
                    "ts_code": ["CU2608.SHF"],
                    "trade_date": ["20260724"],
                    "pre_settle": [78000.0],
                    "open": [78100.0],
                    "high": [79000.0],
                    "low": [77900.0],
                    "close": [78800.0],
                    "settle": [78600.0],
                    "vol": [10000.0],
                    "amount": [100000.0],
                    "oi": [12000.0],
                }
            )
        if operation == "fut_settle":
            return pl.DataFrame(
                {
                    "ts_code": ["CU2608.SHF"],
                    "trade_date": ["20260724"],
                    "settle": [78600.0],
                    "trading_fee_rate": [0.00005],
                    "trading_fee": [0.0],
                    "long_margin_rate": [0.1],
                    "short_margin_rate": [0.1],
                }
            )
        if operation == "ft_limit":
            return pl.DataFrame(
                {
                    "trade_date": ["20260724"],
                    "ts_code": ["CU2608.SHF"],
                    "up_limit": [85000.0],
                    "down_limit": [71000.0],
                    "m_ratio": [10.0],
                    "cont": ["CU"],
                    "exchange": ["SHFE"],
                }
            )
        if operation == "fut_mapping":
            return pl.DataFrame(
                {
                    "ts_code": ["CU.SHF"],
                    "trade_date": ["20260724"],
                    "mapping_ts_code": ["CU2608.SHF"],
                }
            )
        raise AssertionError(operation)


def test_futures_audit_writes_ready_report(tmp_path: Path) -> None:
    service = FuturesDataAuditService(
        FakeFuturesSource(),
        FuturesConfig(exchanges=["SHFE"]),
        tmp_path,
    )

    result = service.run(date(2026, 7, 24))

    assert result.report.ready_for_data_foundation
    assert result.report.ready_for_backtest
    assert result.report.mapping_rows == 1
    assert result.report.exchanges[0].liquid_product_codes == ["CU"]
    assert result.report.exchanges[0].contracts_missing_unit == 0
    assert result.report.exchanges[0].settlements_missing_margin == 0
    assert result.json_path.is_file()
    assert "期货数据可行性审计" in result.markdown_path.read_text(encoding="utf-8")


class FailingFuturesSource(FakeFuturesSource):
    def query(self, operation: str, **params: Any) -> pl.DataFrame:
        if operation == "ft_limit":
            raise RuntimeError("permission denied for secret-value")
        return super().query(operation, **params)


class MissingMarginFuturesSource(FakeFuturesSource):
    def query(self, operation: str, **params: Any) -> pl.DataFrame:
        frame = super().query(operation, **params)
        if operation == "fut_settle":
            return frame.with_columns(
                pl.lit(None).alias("long_margin_rate"),
                pl.lit(None).alias("short_margin_rate"),
            )
        return frame


def test_futures_audit_continues_after_permission_failure(
    tmp_path: Path,
) -> None:
    service = FuturesDataAuditService(
        FailingFuturesSource(),
        FuturesConfig(exchanges=["SHFE"]),
        tmp_path,
        secrets=("secret-value",),
    )

    result = service.run(date(2026, 7, 24))

    assert result.report.ready_for_data_foundation
    assert not result.report.ready_for_backtest
    assert not result.report.blockers
    assert result.report.backtest_blockers
    failure = next(item for item in result.report.query_checks if item.endpoint == "ft_limit")
    assert failure.error == "permission denied for ***"
    assert result.json_path.is_file()


def test_missing_margin_blocks_backtest_not_data_foundation(
    tmp_path: Path,
) -> None:
    result = FuturesDataAuditService(
        MissingMarginFuturesSource(),
        FuturesConfig(exchanges=["SHFE"]),
        tmp_path,
    ).run(date(2026, 7, 24))

    assert result.report.ready_for_data_foundation
    assert not result.report.ready_for_backtest
    assert result.report.exchanges[0].settlements_missing_margin == 1
    assert any("保证金比例" in item for item in result.report.backtest_blockers)
