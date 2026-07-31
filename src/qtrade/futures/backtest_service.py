from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from qtrade.config import FuturesConfig
from qtrade.futures.backtest import (
    FuturesDailyPortfolioEngine,
    FuturesDailyPortfolioInput,
    FuturesDirectionalMarginRates,
    FuturesLiquidationSpec,
)
from qtrade.futures.execution import (
    FuturesDailyExecutionBar,
    FuturesFeeRule,
    FuturesFeeSchedule,
    FuturesOrder,
)
from qtrade.futures.lifecycle import FuturesPositionTarget, FuturesRollPlan
from qtrade.futures.portfolio import FuturesOffset, FuturesSettlementMark, FuturesSide


@dataclass(frozen=True)
class FuturesBacktestBuildResult:
    build_id: str
    output_dir: Path
    manifest_path: Path
    report_path: Path
    day_rows: int
    order_rows: int
    execution_rows: int
    issue_count: int
    passed: bool
    reused: bool = False


class FuturesBacktestService:
    OUTPUT_FILES = {
        "orders": "orders.parquet",
        "executions": "executions.parquet",
        "ledger": "ledger.parquet",
        "positions": "positions.parquet",
        "accounts": "accounts.parquet",
        "issues": "issues.parquet",
    }

    def __init__(
        self,
        config: FuturesConfig,
        curated_root: Path,
        reports_root: Path,
    ) -> None:
        self.config = config
        self.curated_root = Path(curated_root)
        self.reports_root = Path(reports_root)

    def build(self, input_path: Path) -> FuturesBacktestBuildResult:
        input_path = Path(input_path).resolve()
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        research_build_id = str(payload.get("research_build_id", "")).strip()
        if not research_build_id:
            raise ValueError("Futures backtest input requires research_build_id.")
        research_dir = self.curated_root / "futures" / "research" / f"build_id={research_build_id}"
        research_manifest = research_dir / "manifest.json"
        roll_path = research_dir / "roll_schedule.parquet"
        for path in (research_manifest, roll_path):
            if not path.is_file():
                raise FileNotFoundError(f"Futures research build input not found: {path}")
        source_manifest = json.loads(research_manifest.read_text(encoding="utf-8"))
        if source_manifest.get("build_id") != research_build_id or not source_manifest.get(
            "passed"
        ):
            raise ValueError(
                "Referenced futures research build is invalid or failed quality checks."
            )

        versions = [
            self._file_version(input_path, base=input_path.parent),
            self._file_version(research_manifest, base=self.curated_root),
            self._file_version(roll_path, base=self.curated_root),
        ]
        build_payload = {
            "schema_version": 1,
            "research_build_id": research_build_id,
            "config": {
                "execution_slippage_ticks": self.config.execution_slippage_ticks,
                "margin_call_buffer": self.config.margin_call_buffer,
                "stress_margin_multiplier": self.config.stress_margin_multiplier,
            },
            "inputs": versions,
        }
        build_id = hashlib.sha256(self._canonical(build_payload).encode()).hexdigest()[:20]
        output_dir = self.curated_root / "futures" / "backtests" / f"build_id={build_id}"
        manifest_path = output_dir / "manifest.json"
        report_path = self.reports_root / "futures" / "backtests" / build_id / "report.md"
        if output_dir.exists():
            if not manifest_path.is_file():
                raise RuntimeError(f"Incomplete immutable futures backtest exists: {output_dir}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return self._result(manifest, output_dir, manifest_path, report_path, reused=True)

        roll_lookup = self._roll_lookup(pl.read_parquet(roll_path))
        engine = FuturesDailyPortfolioEngine(
            initial_equity=self._positive(payload.get("initial_equity"), "initial_equity"),
            slippage_ticks=self.config.execution_slippage_ticks,
            margin_call_buffer=self.config.margin_call_buffer,
            stress_margin_multiplier=self.config.stress_margin_multiplier,
        )
        rows = self._run(engine, payload, roll_lookup)
        issues = rows["issues"]
        passed = not any(row["severity"] == "error" for row in issues)
        manifest = {
            **build_payload,
            "build_id": build_id,
            "passed": passed,
            "rows": {name: len(values) for name, values in rows.items()},
            "outputs": self.OUTPUT_FILES,
            "issue_count": len(issues),
            "blocked_execution_count": sum(row["status"] != "filled" for row in rows["executions"]),
            "margin_call_days": sum(row["margin_call"] for row in rows["accounts"]),
            "final_equity": rows["accounts"][-1]["equity"] if rows["accounts"] else None,
        }
        self._write_immutable(output_dir, rows, manifest)
        self._write_report(report_path, manifest, issues)
        return self._result(manifest, output_dir, manifest_path, report_path, reused=False)

    def _run(
        self,
        engine: FuturesDailyPortfolioEngine,
        payload: dict[str, Any],
        roll_lookup: dict[tuple[str, str], str],
    ) -> dict[str, list[dict[str, Any]]]:
        days = payload.get("days")
        if not isinstance(days, list) or not days:
            raise ValueError("Futures backtest input requires non-empty days.")
        output: dict[str, list[dict[str, Any]]] = {
            "orders": [],
            "executions": [],
            "ledger": [],
            "positions": [],
            "accounts": [],
            "issues": [],
        }
        previous_entry_count = 0
        seen_orders: set[str] = set()
        for raw_day in days:
            day, product_targets = self._day(raw_day)
            self._validate_targets(day, product_targets, roll_lookup)
            result = engine.run_day(day)
            orders = [*result.generated_target_orders, *result.generated_liquidation_orders]
            for plan in day.roll_plans:
                orders.extend((plan.close_order, plan.open_order))
            for order in orders:
                if order.order_id not in seen_orders:
                    output["orders"].append(self._order_row(order))
                    seen_orders.add(order.order_id)
            for execution in result.executions:
                output["executions"].append(self._execution_row(execution, "order"))
            for attempt in result.roll_attempts:
                for execution in (attempt.close_result, attempt.open_result):
                    if execution is not None:
                        output["executions"].append(
                            self._execution_row(execution, f"roll:{attempt.roll_id}")
                        )
            new_entries = engine.ledger.entries[previous_entry_count:]
            output["ledger"].extend(asdict(entry) for entry in new_entries)
            previous_entry_count = len(engine.ledger.entries)
            output["accounts"].append(asdict(result.snapshot))
            output["positions"].extend(
                {
                    "trade_date": day.trade_date,
                    "contract_code": code,
                    "signed_lots": position.signed_lots,
                    "multiplier": position.multiplier,
                    "settlement_basis": position.settlement_basis,
                }
                for code, position in sorted(engine.ledger.positions.items())
            )
            output["issues"].extend(
                {
                    "trade_date": day.trade_date,
                    "severity": "warning",
                    "code": execution.reason,
                    "order_id": execution.order_id,
                    "message": "Futures order remained pending after the daily attempt.",
                }
                for execution in result.executions
                if execution.status.value != "filled"
            )
            if result.snapshot.margin_call:
                output["issues"].append(
                    {
                        "trade_date": day.trade_date,
                        "severity": "warning",
                        "code": "margin_call",
                        "order_id": None,
                        "message": "End-of-day available cash was negative.",
                    }
                )
        return output

    def _day(
        self,
        raw: dict[str, Any],
    ) -> tuple[FuturesDailyPortfolioInput, list[tuple[str, str]]]:
        trading_date = date.fromisoformat(str(raw["trade_date"]))
        next_date = date.fromisoformat(str(raw["next_trade_date"]))
        bars = {
            str(row["contract_code"]).strip().upper(): FuturesDailyExecutionBar(
                trade_date=trading_date,
                open_price=row.get("open"),
                high_price=row.get("high"),
                low_price=row.get("low"),
                volume=row.get("volume"),
                up_limit=row.get("up_limit"),
                down_limit=row.get("down_limit"),
            )
            for row in raw.get("bars", [])
        }
        marks: dict[str, FuturesSettlementMark] = {}
        rates: dict[str, FuturesDirectionalMarginRates] = {}
        for row in raw.get("settlements", []):
            code = str(row["contract_code"]).strip().upper()
            long_rate = self._rate(row.get("long_margin_rate"))
            short_rate = self._rate(row.get("short_margin_rate"))
            direction = str(row.get("position_direction", "long")).lower()
            mark_rate = long_rate if direction == "long" else short_rate
            marks[code] = FuturesSettlementMark(code, float(row["settlement_price"]), mark_rate)
            rates[code] = FuturesDirectionalMarginRates(long_rate, short_rate)
        targets: list[FuturesPositionTarget] = []
        product_targets: list[tuple[str, str]] = []
        for row in raw.get("targets", []):
            code = str(row["contract_code"]).strip().upper()
            product = str(row["product_code"]).strip().upper()
            product_targets.append((product, code))
            targets.append(
                FuturesPositionTarget(
                    code,
                    int(row["signed_lots"]),
                    float(row["multiplier"]),
                    float(row["tick_size"]),
                    self._fees(row.get("fees", {})),
                    FuturesOffset(row.get("close_offset", "close")),
                )
            )
        roll_rows = raw.get("rolls", [])
        rolls = tuple(self._roll(row, trading_date, next_date) for row in roll_rows)
        product_targets.extend(
            (
                str(row["product_code"]).strip().upper(),
                str(row["new_contract"]).strip().upper(),
            )
            for row in roll_rows
        )
        liquidations = tuple(
            FuturesLiquidationSpec(
                str(row["contract_code"]).strip().upper(),
                float(row["multiplier"]),
                float(row["tick_size"]),
                self._fees(row.get("fees", {})),
                FuturesOffset(row.get("close_offset", "close")),
            )
            for row in raw.get("liquidation_priority", [])
        )
        return (
            FuturesDailyPortfolioInput(
                trade_date=trading_date,
                next_trade_date=next_date,
                bars=bars,
                settlement_marks=marks,
                margin_rates=rates,
                targets=tuple(targets),
                roll_plans=rolls,
                liquidation_priority=liquidations,
                rebalance_id=raw.get("rebalance_id"),
            ),
            product_targets,
        )

    def _roll(self, row: dict[str, Any], signal_date: date, eligible_date: date) -> FuturesRollPlan:
        side = FuturesSide(row["position_side"])
        close_side = FuturesSide.SELL if side == FuturesSide.BUY else FuturesSide.BUY
        roll_id = str(row["roll_id"])
        common = {
            "signal_date": signal_date,
            "eligible_date": eligible_date,
            "lots": int(row["lots"]),
            "multiplier": float(row["multiplier"]),
            "tick_size": float(row["tick_size"]),
        }
        fees = self._fees(row.get("fees", {}))
        return FuturesRollPlan(
            roll_id,
            FuturesOrder(
                order_id=f"{roll_id}:close",
                contract_code=str(row["old_contract"]).strip().upper(),
                side=close_side,
                offset=FuturesOffset(row.get("close_offset", "close_yesterday")),
                fee_rule=fees.rule_for(FuturesOffset(row.get("close_offset", "close_yesterday"))),
                **common,
            ),
            FuturesOrder(
                order_id=f"{roll_id}:open",
                contract_code=str(row["new_contract"]).strip().upper(),
                side=side,
                offset=FuturesOffset.OPEN,
                fee_rule=fees.rule_for(FuturesOffset.OPEN),
                **common,
            ),
        )

    @staticmethod
    def _fees(raw: dict[str, Any]) -> FuturesFeeSchedule:
        def rule(name: str) -> FuturesFeeRule | None:
            value = raw.get(name)
            if value is None:
                return None
            return FuturesFeeRule(
                float(value.get("per_lot", 0)),
                float(value.get("notional_rate", 0)),
            )

        return FuturesFeeSchedule(
            open_rule=rule("open") or FuturesFeeRule(),
            close_rule=rule("close") or FuturesFeeRule(),
            close_today_rule=rule("close_today"),
            close_yesterday_rule=rule("close_yesterday"),
        )

    @staticmethod
    def _roll_lookup(frame: pl.DataFrame) -> dict[tuple[str, str], str]:
        required = {"product_code", "effective_date", "selected_contract"}
        if missing := required - set(frame.columns):
            raise ValueError("Futures roll schedule missing columns: " + ", ".join(sorted(missing)))
        if frame.select("product_code", "effective_date").is_duplicated().any():
            raise ValueError("Futures roll schedule contains duplicate product-date rows.")
        return {
            (str(row["effective_date"]), str(row["product_code"]).strip().upper()): str(
                row["selected_contract"]
            )
            .strip()
            .upper()
            for row in frame.iter_rows(named=True)
        }

    @staticmethod
    def _validate_targets(
        day: FuturesDailyPortfolioInput,
        targets: list[tuple[str, str]],
        lookup: dict[tuple[str, str], str],
    ) -> None:
        for product, contract in targets:
            key = (day.next_trade_date.isoformat(), product)
            selected = lookup.get(key)
            if selected is None:
                raise ValueError(
                    f"No point-in-time selected contract for target: {product} {key[0]}"
                )
            if selected != contract:
                raise ValueError(
                    f"Target contract violates point-in-time roll schedule: {product} "
                    f"expected {selected}, got {contract}"
                )

    def _write_immutable(
        self,
        output_dir: Path,
        rows: dict[str, list[dict[str, Any]]],
        manifest: dict[str, Any],
    ) -> None:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
        temporary.mkdir()
        try:
            for name, filename in self.OUTPUT_FILES.items():
                pl.DataFrame(rows[name]).write_parquet(temporary / filename, compression="zstd")
            self._atomic_json(temporary / "manifest.json", manifest)
            os.replace(temporary, output_dir)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _write_report(
        self,
        path: Path,
        manifest: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        lines = [
            "# Futures Backtest Quality Report",
            "",
            f"- Build ID: `{manifest['build_id']}`",
            f"- Research build: `{manifest['research_build_id']}`",
            f"- Passed: {manifest['passed']}",
            f"- Trading days: {manifest['rows']['accounts']}",
            f"- Orders: {manifest['rows']['orders']}",
            f"- Execution attempts: {manifest['rows']['executions']}",
            f"- Blocked attempts: {manifest['blocked_execution_count']}",
            f"- Margin call days: {manifest['margin_call_days']}",
            f"- Final equity: {manifest['final_equity']}",
            "",
            "## Issues",
            "",
            *(
                [
                    f"- [{row['severity'].upper()}] `{row['code']}` on {row['trade_date']}"
                    for row in issues
                ]
                or ["- None"]
            ),
            "",
            "Continuous research prices are never used for fills, settlement PnL, or margin.",
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        os.replace(temporary, path)
        self._atomic_json(path.with_suffix(".json"), manifest)

    @staticmethod
    def _order_row(order: FuturesOrder) -> dict[str, Any]:
        return {
            "order_id": order.order_id,
            "signal_date": order.signal_date,
            "eligible_date": order.eligible_date,
            "contract_code": order.contract_code,
            "side": order.side.value,
            "offset": order.offset.value,
            "lots": order.lots,
            "multiplier": order.multiplier,
            "tick_size": order.tick_size,
        }

    @staticmethod
    def _execution_row(execution: Any, source: str) -> dict[str, Any]:
        fill = execution.fill
        return {
            "order_id": execution.order_id,
            "attempt_date": execution.attempt_date,
            "source": source,
            "status": execution.status.value,
            "reason": execution.reason,
            "contract_code": fill.contract_code if fill else None,
            "price": fill.price if fill else None,
            "lots": fill.lots if fill else None,
            "fee": fill.fee if fill else None,
            "required_margin": execution.required_margin,
        }

    @staticmethod
    def _positive(value: Any, name: str) -> float:
        result = float(value)
        if result <= 0:
            raise ValueError(f"Futures backtest {name} must be positive.")
        return result

    @staticmethod
    def _rate(value: Any) -> float:
        result = float(value)
        return result / 100 if result >= 1 else result

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _file_version(path: Path, *, base: Path) -> dict[str, Any]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            relative = path.name
        return {"path": relative, "sha256": digest, "size": path.stat().st_size}

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, path)

    @staticmethod
    def _result(
        manifest: dict[str, Any],
        output_dir: Path,
        manifest_path: Path,
        report_path: Path,
        *,
        reused: bool,
    ) -> FuturesBacktestBuildResult:
        return FuturesBacktestBuildResult(
            manifest["build_id"],
            output_dir,
            manifest_path,
            report_path,
            manifest["rows"]["accounts"],
            manifest["rows"]["orders"],
            manifest["rows"]["executions"],
            manifest["issue_count"],
            manifest["passed"],
            reused,
        )
