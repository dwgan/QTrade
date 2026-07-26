from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import polars as pl

from qtrade.observation.models import DailyObservation


class ObservationReportWriter:
    def __init__(self, reports_root: Path) -> None:
        self.root = Path(reports_root)

    def write(
        self,
        observation: DailyObservation,
        shadow_curve: pl.DataFrame | None = None,
        shadow_trades: pl.DataFrame | None = None,
    ) -> tuple[Path, Path]:
        directory = self.root / "observations" / observation.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "observation.json"
        markdown_path = directory / "observation.md"
        self._atomic_text(
            json_path,
            json.dumps(
                observation.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
        )
        self._atomic_text(markdown_path, self._markdown(observation))
        if shadow_curve is not None and not shadow_curve.is_empty():
            self._atomic_parquet(directory / "shadow_equity_curve.parquet", shadow_curve)
        if shadow_trades is not None and not shadow_trades.is_empty():
            self._atomic_parquet(directory / "shadow_rebalances.parquet", shadow_trades)
        return json_path, markdown_path

    @staticmethod
    def _markdown(observation: DailyObservation) -> str:
        lines = [
            f"# 每日观察：{observation.as_of_date}",
            "",
            f"- 当前因子快照：{observation.current_snapshot_date}",
            f"- 对比快照：{observation.previous_snapshot_date or '无'}",
            "",
            "## 候选变化",
            "",
            "### 新进入",
            "",
        ]
        lines.extend(
            f"- {item.ts_code} {item.name}：第 {item.current_rank} 名，"
            f"得分 {item.score or 0:.1f}"
            for item in observation.entered_candidates
        )
        if not observation.entered_candidates:
            lines.append("- 无")
        lines.extend(["", "### 退出", ""])
        lines.extend(
            f"- {item.ts_code} {item.name}：原第 {item.previous_rank} 名，"
            f"当前 {item.current_rank or '未排名'}"
            for item in observation.exited_candidates
        )
        if not observation.exited_candidates:
            lines.append("- 无")

        lines.extend(
            [
                "",
                "## 排名变化",
                "",
                "| 股票 | 名称 | 当前 | 前次 | 变化 |",
                "| --- | --- | ---: | ---: | ---: |",
            ]
        )
        for item in observation.rank_movers:
            lines.append(
                f"| {item.ts_code} | {item.name} | {item.current_rank} | "
                f"{item.previous_rank} | {item.rank_change:+d} |"
            )
        if not observation.rank_movers:
            lines.append("| - | 无可比较变化 | - | - | - |")

        lines.extend(
            [
                "",
                "## 自选股",
                "",
                "| 股票 | 名称 | 状态 | 排名 | 变化 | 得分 |",
                "| --- | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for item in observation.watchlist:
            change = f"{item.rank_change:+d}" if item.rank_change is not None else "-"
            lines.append(
                f"| {item.ts_code} | {item.name or '-'} | {item.status} | "
                f"{item.current_rank or '-'} | {change} | {item.score or 0:.1f} |"
            )
        if not observation.watchlist:
            lines.append("| - | 尚未配置 | - | - | - | - |")

        lines.extend(["", "## 影子组合", ""])
        shadow = observation.shadow_portfolio
        if shadow is None:
            lines.append("- 历史快照或行情不足，暂未生成。")
        else:
            lines.extend(
                [
                    f"- 区间：{shadow.start_date} 至 {shadow.end_date}",
                    f"- 组合权益：{shadow.equity:,.2f}",
                    f"- 组合/基准收益：{shadow.total_return:.2%} / "
                    f"{shadow.benchmark_return:.2%}",
                    f"- 最大回撤：{shadow.max_drawdown:.2%}",
                    f"- 现金权重：{shadow.cash_weight:.2%}",
                    f"- 当前持仓：{', '.join(shadow.holdings) if shadow.holdings else '无'}",
                ]
            )
        if observation.warnings:
            lines.extend(["", "## 提示", "", *[f"- {item}" for item in observation.warnings]])
        lines.extend(["", "> 观察结果用于复盘和缩小研究范围，不构成买卖建议。", ""])
        return "\n".join(lines)

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)

    @staticmethod
    def _atomic_parquet(path: Path, frame: pl.DataFrame) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        frame.write_parquet(temporary, compression="zstd")
        os.replace(temporary, path)
