from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

BUILD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
PRODUCT_PATTERN = re.compile(r"^[A-Z0-9]{1,12}$")


class FuturesUiRepository:
    def __init__(self, curated_root: Path, reports_root: Path, provider: str) -> None:
        self.curated_root = Path(curated_root)
        self.reports_root = Path(reports_root)
        self.provider = provider

    def meta(self) -> dict[str, Any]:
        signals = self._builds("signals", "signal_date")
        backtests = self._backtest_builds()
        research = self._builds("research", "end_date")
        return {
            "signals": [
                {
                    "build_id": item["build_id"],
                    "signal_date": item.get("signal_date"),
                    "eligible_date": item.get("eligible_date"),
                    "target_count": item.get("rows", {}).get("targets", 0),
                    "protocol_id": item.get("protocol_id"),
                }
                for item in reversed(signals[-60:])
            ],
            "backtests": [
                {
                    "build_id": item["build_id"],
                    "last_trade_date": item.get("last_trade_date"),
                    "final_equity": item.get("final_equity"),
                    "passed": item.get("passed", False),
                }
                for item in reversed(backtests[-30:])
            ],
            "latest_signal_build_id": signals[-1]["build_id"] if signals else None,
            "latest_backtest_build_id": backtests[-1]["build_id"] if backtests else None,
            "latest_research_build_id": research[-1]["build_id"] if research else None,
            "provider": self.provider,
        }

    def dashboard(self, signal_build_id: str | None = None) -> dict[str, Any]:
        signal_manifest, signal_dir = self._signal(signal_build_id)
        if signal_manifest is None or signal_dir is None:
            return self._empty_dashboard()
        targets_path = signal_dir / "targets.parquet"
        self._verify_output(signal_manifest, targets_path)
        targets = self._json_rows(pl.read_parquet(targets_path).sort("product_code"))
        research_manifest = self._manifest(
            "research",
            str(signal_manifest["research_build_id"]),
        )
        if research_manifest is None:
            raise ValueError("期货 signal 引用的研究 manifest 无效或未通过质量门禁。")
        backtest_manifest = self._latest_backtest_for_research(
            str(signal_manifest["research_build_id"])
        )
        portfolio = self._portfolio(backtest_manifest)
        quality = self._latest_readiness()
        long_count = sum(int(item.get("target_signed_lots") or 0) > 0 for item in targets)
        short_count = sum(int(item.get("target_signed_lots") or 0) < 0 for item in targets)
        flat_count = len(targets) - long_count - short_count
        sector_risk = signal_manifest.get("sector_daily_risk", {})
        return {
            "signal": {
                "build_id": signal_manifest["build_id"],
                "signal_date": signal_manifest.get("signal_date"),
                "eligible_date": signal_manifest.get("eligible_date"),
                "protocol_id": signal_manifest.get("protocol_id"),
                "research_build_id": signal_manifest.get("research_build_id"),
                "target_count": len(targets),
                "long_count": long_count,
                "short_count": short_count,
                "flat_count": flat_count,
                "equity": signal_manifest.get("equity"),
                "daily_risk_budget": signal_manifest.get("portfolio_daily_risk_budget"),
                "total_daily_risk": signal_manifest.get("total_daily_risk"),
                "initial_margin": signal_manifest.get("initial_margin"),
                "stress_margin": signal_manifest.get("stress_margin"),
                "sector_risk": [
                    {"sector": sector, "daily_risk": value}
                    for sector, value in sorted(sector_risk.items())
                ],
            },
            "targets": targets,
            "portfolio": portfolio,
            "research": {
                "build_id": research_manifest.get("build_id") if research_manifest else None,
                "start_date": research_manifest.get("start_date") if research_manifest else None,
                "end_date": research_manifest.get("end_date") if research_manifest else None,
                "rows": research_manifest.get("rows", {}) if research_manifest else {},
                "roll_events": research_manifest.get("roll_events", 0) if research_manifest else 0,
                "issues": research_manifest.get("issues", []) if research_manifest else [],
            },
            "quality": quality,
        }

    def chart(self, signal_build_id: str | None, product_code: str) -> dict[str, Any]:
        product = product_code.strip().upper()
        if not PRODUCT_PATTERN.fullmatch(product):
            raise ValueError("无效的期货品种代码。")
        signal_manifest, signal_dir = self._signal(signal_build_id)
        if signal_manifest is None or signal_dir is None:
            raise FileNotFoundError("期货信号构建不存在。")
        targets_path = signal_dir / "targets.parquet"
        self._verify_output(signal_manifest, targets_path)
        targets = pl.read_parquet(targets_path).filter(pl.col("product_code") == product)
        if targets.is_empty():
            raise FileNotFoundError(f"信号中没有品种 {product}。")
        target = targets.row(0, named=True)
        research_dir = self._build_dir("research", str(signal_manifest["research_build_id"]))
        research_manifest = self._read_manifest(research_dir / "manifest.json")
        continuous_path = research_dir / "continuous.parquet"
        roll_path = research_dir / "roll_schedule.parquet"
        self._verify_output(research_manifest, continuous_path)
        self._verify_output(research_manifest, roll_path)
        continuous = (
            pl.read_parquet(continuous_path)
            .filter(pl.col("product_code") == product)
            .sort("trade_date")
            .tail(180)
        )
        continuous_columns = [
            name
            for name in (
                "trade_date",
                "contract_code",
                "continuous_index",
                "research_price",
            )
            if name in continuous.columns
        ]
        rolls = (
            pl.read_parquet(roll_path)
            .filter((pl.col("product_code") == product) & pl.col("roll"))
            .sort("effective_date")
            .tail(30)
        )
        roll_columns = [
            name
            for name in (
                "decision_date",
                "effective_date",
                "previous_contract",
                "selected_contract",
                "reason",
            )
            if name in rolls.columns
        ]
        contract_code = str(target["contract_code"]).strip().upper()
        actual = self._actual_contract_bars(
            contract_code,
            limit=180,
            research_manifest=research_manifest,
        )
        return {
            "product_code": product,
            "contract_code": contract_code,
            "signal_date": signal_manifest.get("signal_date"),
            "continuous": self._json_rows(continuous.select(continuous_columns)),
            "actual": actual,
            "rolls": self._json_rows(rolls.select(roll_columns)),
        }

    def backtest(self, build_id: str | None = None) -> dict[str, Any]:
        builds = self._backtest_builds()
        if not builds:
            return {
                "build_id": None,
                "metrics": {},
                "accounts": [],
                "positions": [],
                "executions": [],
                "issues": [],
            }
        selected = (
            next((item for item in builds if item["build_id"] == build_id), None)
            if build_id
            else builds[-1]
        )
        if selected is None:
            raise FileNotFoundError("期货回测构建不存在。")
        directory = self._build_dir("backtests", str(selected["build_id"]))
        artifact_names = (
            "accounts.parquet",
            "positions.parquet",
            "executions.parquet",
            "issues.parquet",
        )
        artifacts = {}
        for name in artifact_names:
            path = directory / name
            self._verify_output(selected, path)
            artifacts[name] = pl.read_parquet(path)
        accounts = artifacts["accounts.parquet"]
        positions = artifacts["positions.parquet"]
        executions = artifacts["executions.parquet"]
        issues = artifacts["issues.parquet"]
        if accounts.height > 320:
            indices = sorted(
                {round(index * (accounts.height - 1) / 319) for index in range(320)}
            )
            account_view = accounts[indices]
        else:
            account_view = accounts
        equities = (
            [float(value) for value in accounts.get_column("equity").to_list()]
            if "equity" in accounts.columns
            else []
        )
        total_return = (
            round(equities[-1] / equities[0] - 1, 12)
            if len(equities) >= 2 and equities[0]
            else None
        )
        maximum_drawdown = self._maximum_drawdown(equities)
        latest_positions = self._latest_rows(positions, "trade_date")
        return {
            "build_id": selected["build_id"],
            "research_build_id": selected.get("research_build_id"),
            "passed": selected.get("passed", False),
            "metrics": {
                "initial_equity": equities[0] if equities else None,
                "final_equity": equities[-1] if equities else selected.get("final_equity"),
                "total_return": total_return,
                "maximum_drawdown": maximum_drawdown,
                "margin_call_days": selected.get("margin_call_days", 0),
                "issue_count": selected.get("issue_count", 0),
            },
            "accounts": self._json_rows(account_view),
            "positions": self._json_rows(latest_positions),
            "executions": self._json_rows(executions.tail(80)),
            "issues": self._json_rows(issues.tail(80)),
        }

    def _signal(
        self,
        build_id: str | None,
    ) -> tuple[dict[str, Any] | None, Path | None]:
        builds = self._builds("signals", "signal_date")
        if not builds:
            return None, None
        selected = (
            next((item for item in builds if item["build_id"] == build_id), None)
            if build_id
            else builds[-1]
        )
        if selected is None:
            raise FileNotFoundError("期货信号构建不存在。")
        return selected, self._build_dir("signals", str(selected["build_id"]))

    def _portfolio(self, manifest: dict[str, Any] | None) -> dict[str, Any]:
        if manifest is None:
            return {
                "build_id": None,
                "trade_date": None,
                "equity": None,
                "available_cash": None,
                "margin": None,
                "stress_margin": None,
                "positions": [],
            }
        detail = self.backtest(str(manifest["build_id"]))
        accounts = detail["accounts"]
        latest = accounts[-1] if accounts else {}
        return {
            "build_id": manifest["build_id"],
            "trade_date": latest.get("trade_date"),
            "equity": latest.get("equity"),
            "available_cash": latest.get("available_cash"),
            "margin": latest.get("margin"),
            "stress_margin": latest.get("stress_margin"),
            "positions": detail["positions"],
        }

    def _latest_backtest_for_research(self, research_build_id: str) -> dict[str, Any] | None:
        matches = [
            item
            for item in self._backtest_builds()
            if item.get("research_build_id") == research_build_id
        ]
        return matches[-1] if matches else None

    def _latest_readiness(self) -> dict[str, Any]:
        root = self.reports_root / "futures" / "validation-readiness"
        paths = sorted(
            root.glob("*/*/*/report.json") if root.exists() else (),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if not paths:
            return {
                "ready": False,
                "protocol_id": None,
                "partition": None,
                "dataset_coverage": {},
                "issues": [
                    {
                        "severity": "warning",
                        "code": "readiness_not_run",
                        "message": "尚未运行期货验证就绪审计。",
                    }
                ],
            }
        value = self._read_json(paths[-1])
        if value is None:
            return {"ready": False, "issues": []}
        return {
            "ready": bool(value.get("ready", False)),
            "protocol_id": value.get("protocol_id"),
            "partition": value.get("partition"),
            "dataset_coverage": value.get("dataset_coverage", {}),
            "issues": [
                {
                    "severity": item.get("severity"),
                    "code": item.get("code"),
                    "message": self._safe_text(item.get("message")),
                }
                for item in value.get("issues", [])
                if isinstance(item, dict)
            ],
        }

    def _actual_contract_bars(
        self,
        contract_code: str,
        limit: int,
        research_manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        prefix = f"futures/futures_daily/provider={self.provider}/"
        frames = []
        declared = sorted(
            [
                item
                for item in research_manifest.get("inputs", [])
                if str(item.get("path", "")).replace("\\", "/").startswith(prefix)
                and str(item.get("path", "")).replace("\\", "/").endswith(
                    "/data.parquet"
                )
            ],
            key=lambda item: str(item.get("path", "")),
        )
        for version in declared:
            relative = Path(str(version["path"]))
            path = (self.curated_root / relative).resolve()
            root = self.curated_root.resolve()
            if root not in path.parents:
                raise ValueError("期货研究输入路径越出只读数据目录。")
            self._verify_version(version, path, "期货研究输入")
            frame = pl.read_parquet(path)
            if "ts_code" not in frame.columns:
                continue
            selected = frame.filter(pl.col("ts_code") == contract_code)
            if not selected.is_empty():
                frames.append(selected)
        if not frames:
            return []
        frame = pl.concat(frames, how="diagonal_relaxed").sort("trade_date").tail(limit)
        columns = [
            name
            for name in (
                "trade_date",
                "open",
                "high",
                "low",
                "close",
                "settle",
                "vol",
                "oi",
            )
            if name in frame.columns
        ]
        rows = self._json_rows(frame.select(columns))
        return [{"contract_code": contract_code, **item} for item in rows]

    def _builds(self, kind: str, date_key: str) -> list[dict[str, Any]]:
        root = self.curated_root / "futures" / kind
        values = []
        for path in root.glob("build_id=*/manifest.json") if root.exists() else ():
            manifest = self._read_json(path)
            expected_id = path.parent.name.removeprefix("build_id=")
            build_id = str(manifest.get("build_id", "")) if manifest else ""
            if (
                manifest is not None
                and manifest.get("passed")
                and build_id == expected_id
                and BUILD_ID_PATTERN.fullmatch(build_id)
            ):
                values.append(manifest)
        return sorted(values, key=lambda item: (str(item.get(date_key) or ""), item["build_id"]))

    def _backtest_builds(self) -> list[dict[str, Any]]:
        values = self._builds("backtests", "build_id")
        for item in values:
            accounts_path = (
                self._build_dir("backtests", str(item["build_id"]))
                / "accounts.parquet"
            )
            self._verify_output(item, accounts_path)
            accounts = pl.read_parquet(accounts_path)
            if not accounts.is_empty() and "trade_date" in accounts.columns:
                item["last_trade_date"] = str(accounts.get_column("trade_date").tail(1).item())
        return sorted(
            values,
            key=lambda item: (str(item.get("last_trade_date") or ""), item["build_id"]),
        )

    def _manifest(self, kind: str, build_id: str) -> dict[str, Any] | None:
        if not BUILD_ID_PATTERN.fullmatch(build_id):
            raise ValueError("无效的期货构建 ID。")
        path = self._build_dir(kind, build_id) / "manifest.json"
        value = self._read_json(path)
        return value if value and value.get("passed") else None

    def _build_dir(self, kind: str, build_id: str) -> Path:
        if not BUILD_ID_PATTERN.fullmatch(build_id):
            raise ValueError("无效的期货构建 ID。")
        return self.curated_root / "futures" / kind / f"build_id={build_id}"

    @staticmethod
    def _verify_output(manifest: dict[str, Any], path: Path) -> None:
        matches = [
            item
            for item in manifest.get("output_versions", [])
            if item.get("path") == path.name
        ]
        if len(matches) != 1 or not path.is_file():
            raise ValueError(f"不可变期货产物缺少哈希声明：{path.name}")
        FuturesUiRepository._verify_version(matches[0], path, "不可变期货产物")

    @staticmethod
    def _verify_version(version: dict[str, Any], path: Path, label: str) -> None:
        if not path.is_file():
            raise ValueError(f"{label}缺少哈希声明对应文件：{path.name}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if version.get("sha256") != digest or version.get("size") != path.stat().st_size:
            raise ValueError(f"{label}哈希不匹配：{path.name}")

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        value = FuturesUiRepository._read_json(path)
        if value is None or not value.get("passed"):
            raise ValueError("期货构建 manifest 无效或未通过质量门禁。")
        return value

    @staticmethod
    def _safe_text(value: Any) -> str:
        text = str(value or "")
        return re.sub(r"[A-Za-z]:[\\/][^\r\n\"']+", "[本地路径]", text)[:500]

    @staticmethod
    def _latest_rows(frame: pl.DataFrame, column: str) -> pl.DataFrame:
        if frame.is_empty() or column not in frame.columns:
            return frame
        latest = frame.get_column(column).max()
        return frame.filter(pl.col(column) == latest).sort(
            "contract_code" if "contract_code" in frame.columns else column
        )

    @staticmethod
    def _maximum_drawdown(equities: list[float]) -> float | None:
        if not equities:
            return None
        peak = equities[0]
        drawdown = 0.0
        for equity in equities:
            if not math.isfinite(equity):
                return None
            peak = max(peak, equity)
            if peak:
                drawdown = max(drawdown, 1 - equity / peak)
        return drawdown

    @staticmethod
    def _json_rows(frame: pl.DataFrame) -> list[dict[str, Any]]:
        rows = []
        for row in frame.to_dicts():
            rows.append(
                {
                    key: value.isoformat() if isinstance(value, (date, datetime)) else value
                    for key, value in row.items()
                }
            )
        return rows

    @staticmethod
    def _empty_dashboard() -> dict[str, Any]:
        return {
            "signal": {
                "build_id": None,
                "target_count": 0,
                "long_count": 0,
                "short_count": 0,
                "flat_count": 0,
            },
            "targets": [],
            "portfolio": {"positions": []},
            "research": {"rows": {}, "issues": []},
            "quality": {
                "ready": False,
                "issues": [
                    {
                        "severity": "warning",
                        "code": "missing_signal_snapshot",
                        "message": "尚无可展示的期货信号快照。",
                    }
                ],
            },
        }
