from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import polars as pl

from qtrade.config import FuturesConfig
from qtrade.futures.models import (
    FuturesAuditReport,
    FuturesExchangeCoverage,
    FuturesQueryCheck,
)


class FuturesQuerySource(Protocol):
    @property
    def name(self) -> str: ...

    def query(self, operation: str, **params: Any) -> pl.DataFrame: ...


@dataclass(frozen=True)
class FuturesAuditResult:
    report: FuturesAuditReport
    json_path: Path
    markdown_path: Path


QUERY_SPECS: dict[str, tuple[str, ...]] = {
    "fut_basic": (
        "ts_code",
        "symbol",
        "name",
        "exchange",
        "fut_code",
        "multiplier",
        "trade_unit",
        "per_unit",
        "list_date",
        "delist_date",
        "trade_time_desc",
    ),
    "fut_daily": (
        "ts_code",
        "trade_date",
        "pre_settle",
        "open",
        "high",
        "low",
        "close",
        "settle",
        "vol",
        "amount",
        "oi",
    ),
    "fut_settle": (
        "ts_code",
        "trade_date",
        "settle",
        "trading_fee_rate",
        "trading_fee",
        "long_margin_rate",
        "short_margin_rate",
    ),
    "ft_limit": (
        "trade_date",
        "ts_code",
        "up_limit",
        "down_limit",
        "m_ratio",
        "cont",
        "exchange",
    ),
    "fut_mapping": (
        "ts_code",
        "trade_date",
        "mapping_ts_code",
    ),
}


class FuturesDataAuditService:
    def __init__(
        self,
        source: FuturesQuerySource,
        config: FuturesConfig,
        reports_root: Path,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.source = source
        self.config = config
        self.reports_root = Path(reports_root)
        self.secrets = tuple(value for value in secrets if value)

    def run(self, as_of_date: date) -> FuturesAuditResult:
        if as_of_date > date.today():
            raise ValueError("Futures audit date cannot be in the future.")
        ymd = as_of_date.strftime("%Y%m%d")
        checks: list[FuturesQueryCheck] = []
        contracts: dict[str, pl.DataFrame] = {}
        daily: dict[str, pl.DataFrame] = {}
        settlements: dict[str, pl.DataFrame] = {}

        for exchange in self.config.exchanges:
            contracts[exchange] = self._checked_query(
                checks,
                "fut_basic",
                exchange,
                exchange=exchange,
                fut_type="1",
                fields=",".join(QUERY_SPECS["fut_basic"]),
            )
            daily[exchange] = self._checked_query(
                checks,
                "fut_daily",
                exchange,
                trade_date=ymd,
                exchange=exchange,
                fields=",".join(QUERY_SPECS["fut_daily"]),
            )
            settlements[exchange] = self._checked_query(
                checks,
                "fut_settle",
                exchange,
                trade_date=ymd,
                exchange=exchange,
                fields=",".join(QUERY_SPECS["fut_settle"]),
            )

        limits = self._checked_query(
            checks,
            "ft_limit",
            None,
            trade_date=ymd,
            fields=",".join(QUERY_SPECS["ft_limit"]),
        )
        mappings = self._checked_query(
            checks,
            "fut_mapping",
            None,
            trade_date=ymd,
            fields=",".join(QUERY_SPECS["fut_mapping"]),
        )

        exchange_coverage = [
            self._exchange_coverage(
                exchange,
                as_of_date,
                contracts[exchange],
                daily[exchange],
                settlements[exchange],
                limits,
            )
            for exchange in self.config.exchanges
        ]
        blockers = self._blockers(checks, exchange_coverage)
        backtest_blockers = self._backtest_blockers(
            checks,
            exchange_coverage,
            blockers,
        )
        warnings = self._warnings(checks, exchange_coverage, mappings)
        report = FuturesAuditReport(
            as_of_date=as_of_date,
            provider=self.source.name,
            ready_for_data_foundation=not blockers,
            query_checks=checks,
            exchanges=exchange_coverage,
            mapping_rows=mappings.height,
            blockers=blockers,
            backtest_blockers=backtest_blockers,
            warnings=warnings,
            ready_for_backtest=not backtest_blockers,
        )
        json_path, markdown_path = self._write(report)
        return FuturesAuditResult(report, json_path, markdown_path)

    def _checked_query(
        self,
        checks: list[FuturesQueryCheck],
        endpoint: str,
        scope: str | None,
        **params: Any,
    ) -> pl.DataFrame:
        required = set(QUERY_SPECS[endpoint])
        try:
            frame = self.source.query(endpoint, **params)
            missing = sorted(required - set(frame.columns))
            passed = frame.height > 0 and not missing
            checks.append(
                FuturesQueryCheck(
                    endpoint=endpoint,
                    exchange=scope,
                    passed=passed,
                    row_count=frame.height,
                    missing_columns=missing,
                    error="接口返回空数据。" if frame.is_empty() else None,
                )
            )
            return frame
        except Exception as exc:
            checks.append(
                FuturesQueryCheck(
                    endpoint=endpoint,
                    exchange=scope,
                    passed=False,
                    error=self._safe_error(str(exc)),
                )
            )
            return pl.DataFrame()

    def _safe_error(self, message: str) -> str:
        sanitized = message
        for secret in self.secrets:
            sanitized = sanitized.replace(secret, "***")
        return sanitized[:500]

    def _exchange_coverage(
        self,
        exchange: str,
        as_of_date: date,
        contracts: pl.DataFrame,
        daily: pl.DataFrame,
        settlements: pl.DataFrame,
        limits: pl.DataFrame,
    ) -> FuturesExchangeCoverage:
        active = self._active_contracts(contracts, as_of_date)
        if active.is_empty():
            return FuturesExchangeCoverage(
                exchange=exchange,
                active_contracts=0,
                listed_products=0,
                daily_contracts=daily.height,
                daily_products=0,
                settlement_contracts=settlements.height,
                settlements_missing_margin=0,
                limit_contracts=0,
                contracts_missing_unit=0,
                contracts_missing_trading_hours=0,
            )

        contract_products = active.select("ts_code", "fut_code").unique()
        daily_with_product = (
            daily.join(contract_products, on="ts_code", how="inner")
            if {"ts_code", "vol", "oi"} <= set(daily.columns)
            else pl.DataFrame()
        )
        liquid_codes: list[str] = []
        if not daily_with_product.is_empty():
            liquid_codes = (
                daily_with_product.with_columns(
                    pl.col("vol").cast(pl.Float64, strict=False).fill_null(0),
                    pl.col("oi").cast(pl.Float64, strict=False).fill_null(0),
                )
                .group_by("fut_code")
                .agg(
                    pl.col("vol").sum().alias("volume"),
                    pl.col("oi").sum().alias("open_interest"),
                )
                .filter(
                    (pl.col("volume") >= self.config.audit_minimum_daily_volume)
                    & (pl.col("open_interest") >= self.config.audit_minimum_open_interest)
                    & ~pl.col("fut_code").is_in(
                        self.config.excluded_product_codes,
                    )
                )
                .sort(["volume", "fut_code"], descending=[True, False])
                .get_column("fut_code")
                .cast(pl.String)
                .to_list()
            )

        unit_missing = (
            active.filter(pl.col("multiplier").cast(pl.Float64, strict=False).fill_null(0) <= 0)
            .filter(pl.col("per_unit").cast(pl.Float64, strict=False).fill_null(0) <= 0)
            .height
        )
        hours_missing = active.filter(
            pl.col("trade_time_desc").is_null()
            | (pl.col("trade_time_desc").cast(pl.String).str.strip_chars() == "")
        ).height
        limit_contracts = 0
        if {"ts_code", "exchange"} <= set(limits.columns):
            limit_contracts = (
                limits.filter(pl.col("exchange").cast(pl.String) == exchange)
                .get_column("ts_code")
                .n_unique()
            )
        missing_margin = 0
        if {
            "long_margin_rate",
            "short_margin_rate",
        } <= set(settlements.columns):
            missing_margin = (
                settlements.with_columns(
                    pl.col("long_margin_rate").cast(pl.Float64, strict=False).alias("_long_margin"),
                    pl.col("short_margin_rate")
                    .cast(pl.Float64, strict=False)
                    .alias("_short_margin"),
                )
                .filter(
                    pl.col("_long_margin").is_null()
                    | pl.col("_short_margin").is_null()
                    | (pl.col("_long_margin") <= 0)
                    | (pl.col("_short_margin") <= 0)
                )
                .height
            )

        return FuturesExchangeCoverage(
            exchange=exchange,
            active_contracts=active.get_column("ts_code").n_unique(),
            listed_products=active.get_column("fut_code").n_unique(),
            daily_contracts=daily.get_column("ts_code").n_unique()
            if "ts_code" in daily.columns
            else 0,
            daily_products=daily_with_product.get_column("fut_code").n_unique()
            if not daily_with_product.is_empty()
            else 0,
            settlement_contracts=settlements.get_column("ts_code").n_unique()
            if "ts_code" in settlements.columns
            else 0,
            settlements_missing_margin=missing_margin,
            limit_contracts=limit_contracts,
            contracts_missing_unit=unit_missing,
            contracts_missing_trading_hours=hours_missing,
            liquid_product_codes=liquid_codes,
        )

    @staticmethod
    def _active_contracts(frame: pl.DataFrame, as_of_date: date) -> pl.DataFrame:
        required = set(QUERY_SPECS["fut_basic"])
        if frame.is_empty() or required - set(frame.columns):
            return pl.DataFrame()
        ymd = as_of_date.strftime("%Y%m%d")
        return frame.filter(
            (pl.col("list_date").cast(pl.String) <= ymd)
            & (pl.col("delist_date").cast(pl.String) >= ymd)
        )

    @staticmethod
    def _blockers(
        checks: list[FuturesQueryCheck],
        exchanges: list[FuturesExchangeCoverage],
    ) -> list[str]:
        blockers = [
            (
                f"{check.endpoint}"
                + (f"/{check.exchange}" if check.exchange else "")
                + f" 不可用：{check.error or '字段不完整'}"
            )
            for check in checks
            if not check.passed and check.endpoint != "ft_limit"
        ]
        for item in exchanges:
            if item.active_contracts == 0:
                blockers.append(f"{item.exchange} 没有可识别的在市合约。")
            if item.daily_contracts == 0:
                blockers.append(f"{item.exchange} 没有当日期货日线。")
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _backtest_blockers(
        checks: list[FuturesQueryCheck],
        exchanges: list[FuturesExchangeCoverage],
        foundation_blockers: list[str],
    ) -> list[str]:
        blockers = list(foundation_blockers)
        blockers.extend(
            (
                f"{check.endpoint}"
                + (f"/{check.exchange}" if check.exchange else "")
                + f" 不可用：{check.error or '字段不完整'}"
            )
            for check in checks
            if not check.passed and check.endpoint == "ft_limit"
        )
        blockers.extend(
            f"{item.exchange} 有 {item.settlements_missing_margin} 条结算记录缺少可用保证金比例"
            for item in exchanges
            if item.settlements_missing_margin
        )
        return list(dict.fromkeys(blockers))

    @staticmethod
    def _warnings(
        checks: list[FuturesQueryCheck],
        exchanges: list[FuturesExchangeCoverage],
        mappings: pl.DataFrame,
    ) -> list[str]:
        warnings: list[str] = []
        for item in exchanges:
            if item.contracts_missing_unit:
                warnings.append(
                    f"{item.exchange} 有 {item.contracts_missing_unit} 个在市合约缺少可用交易单位。"
                )
            if item.contracts_missing_trading_hours:
                warnings.append(
                    f"{item.exchange} 有 {item.contracts_missing_trading_hours} 个在市合约"
                    "缺少交易时段，需用交易所规则补齐。"
                )
            if item.daily_products < item.listed_products:
                warnings.append(
                    f"{item.exchange} 当日有行情的品种为 {item.daily_products}/"
                    f"{item.listed_products}，未成交品种不能进入初始池。"
                )
        if mappings.is_empty():
            warnings.append("主力映射为空，暂时不能验证供应商换月覆盖。")
        if not any(check.endpoint == "ft_limit" and check.passed for check in checks):
            warnings.append(
                "当前数据源未取得官方 ft_limit 涨跌停数据；阶段 1 数据底座可继续，"
                "但阶段 3 回测前必须从交易所或其他数据源补齐，否则无法正确模拟"
                "不可成交场景。"
            )
        return warnings

    def _write(self, report: FuturesAuditReport) -> tuple[Path, Path]:
        directory = self.reports_root / "futures" / "audit" / report.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "audit.json"
        markdown_path = directory / "audit.md"
        self._atomic(
            json_path,
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
        )
        lines = [
            f"# 期货数据可行性审计：{report.as_of_date}",
            "",
            f"- 数据源：{report.provider}",
            f"- 阶段 1 数据底座就绪：{'是' if report.ready_for_data_foundation else '否'}",
            f"- 阶段 3 回测内核就绪：{'是' if report.ready_for_backtest else '否'}",
            f"- 主力映射：{report.mapping_rows} 行",
            "",
            "## 接口检查",
            "",
            "| 接口 | 市场 | 状态 | 行数 | 缺失字段/错误 |",
            "| --- | --- | --- | ---: | --- |",
        ]
        lines.extend(
            "| {endpoint} | {exchange} | {status} | {rows} | {detail} |".format(
                endpoint=item.endpoint,
                exchange=item.exchange or "全市场",
                status="通过" if item.passed else "失败",
                rows=item.row_count,
                detail=", ".join(item.missing_columns) or item.error or "-",
            )
            for item in report.query_checks
        )
        lines.extend(
            [
                "",
                "## 市场覆盖",
                "",
                "| 市场 | 在市合约 | 上市品种 | 日线合约/品种 | 结算参数 | 涨跌停 |"
                " 保证金缺失 | 单位缺失 | 时段缺失 |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        lines.extend(
            f"| {item.exchange} | {item.active_contracts} | {item.listed_products} | "
            f"{item.daily_contracts}/{item.daily_products} | "
            f"{item.settlement_contracts} | {item.limit_contracts} | "
            f"{item.settlements_missing_margin} | "
            f"{item.contracts_missing_unit} | "
            f"{item.contracts_missing_trading_hours} |"
            for item in report.exchanges
        )
        lines.extend(["", "## 初始流动性候选", ""])
        for item in report.exchanges:
            values = "、".join(item.liquid_product_codes) or "无"
            lines.append(f"- {item.exchange}：{values}")
        lines.extend(["", "## 阻塞项", ""])
        lines.extend(f"- {value}" for value in report.blockers)
        if not report.blockers:
            lines.append("- 无")
        lines.extend(["", "## 回测前阻塞项", ""])
        lines.extend(f"- {value}" for value in report.backtest_blockers)
        if not report.backtest_blockers:
            lines.append("- 无")
        lines.extend(["", "## 警告", ""])
        lines.extend(f"- {value}" for value in report.warnings)
        if not report.warnings:
            lines.append("- 无")
        self._atomic(markdown_path, "\n".join(lines))
        return json_path, markdown_path

    @staticmethod
    def _atomic(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
