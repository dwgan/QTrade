from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import polars as pl
from pydantic import BaseModel

from qtrade.research.models import CandidateBacktestAnalysis, FactorResearchAnalysis


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    os.replace(temporary, path)


def _write_parquet(path: Path, frame: pl.DataFrame) -> None:
    if not frame.columns:
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    frame.write_parquet(temporary, compression="zstd")
    os.replace(temporary, path)


def _json(path: Path, model: BaseModel) -> None:
    _atomic_text(path, json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2))


class ResearchReportWriter:
    def __init__(self, reports_root: Path) -> None:
        self.root = Path(reports_root)

    def write_factor(
        self,
        analysis: FactorResearchAnalysis,
        ic_detail: pl.DataFrame,
        return_detail: pl.DataFrame,
    ) -> tuple[Path, Path]:
        directory = (
            self.root
            / "research"
            / "factors"
            / f"{analysis.start_date}_{analysis.end_date}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "summary.json"
        markdown_path = directory / "summary.md"
        _json(json_path, analysis)
        _atomic_text(markdown_path, self._factor_markdown(analysis))
        _write_parquet(directory / "ic_detail.parquet", ic_detail)
        _write_parquet(directory / "forward_returns.parquet", return_detail)
        return json_path, markdown_path

    def write_backtest(
        self,
        analysis: CandidateBacktestAnalysis,
        curve: pl.DataFrame,
        trades: pl.DataFrame,
    ) -> tuple[Path, Path]:
        directory = (
            self.root
            / "research"
            / "backtests"
            / f"{analysis.start_date}_{analysis.end_date}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "summary.json"
        markdown_path = directory / "summary.md"
        _json(json_path, analysis)
        _atomic_text(markdown_path, self._backtest_markdown(analysis))
        _write_parquet(directory / "equity_curve.parquet", curve)
        _write_parquet(directory / "rebalances.parquet", trades)
        return json_path, markdown_path

    @staticmethod
    def _factor_markdown(analysis: FactorResearchAnalysis) -> str:
        lines = [
            f"# 因子研究：{analysis.start_date} 至 {analysis.end_date}",
            "",
            f"- 前瞻周期：{analysis.forward_horizon_days} 个交易日",
            f"- 快照：{analysis.evaluated_snapshot_count}/{analysis.snapshot_count} 个可评估",
            f"- 最高组减最低组：{analysis.top_bottom_spread or 0:.2%}",
            "",
            "## Rank IC",
            "",
            "| 因子 | 样本期数 | IC均值 | ICIR | 正值比例 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        for item in analysis.factor_metrics:
            lines.append(
                f"| {item.factor} | {item.observations} | "
                f"{item.ic_mean or 0:.4f} | {item.icir or 0:.4f} | "
                f"{item.ic_positive_ratio or 0:.1%} |"
            )
        lines.extend(
            [
                "",
                "## 分组收益",
                "",
                "| 分组 | 样本数 | 平均前瞻收益 |",
                "| ---: | ---: | ---: |",
            ]
        )
        for item in analysis.quantile_metrics:
            lines.append(
                f"| Q{item.quantile} | {item.observations} | "
                f"{item.mean_forward_return or 0:.2%} |"
            )
        lines.extend(["", "> 结果仅用于研究；历史统计不代表未来表现。", ""])
        return "\n".join(lines)

    @staticmethod
    def _backtest_markdown(analysis: CandidateBacktestAnalysis) -> str:
        return "\n".join(
            [
                f"# 候选组合回测：{analysis.start_date} 至 {analysis.end_date}",
                "",
                f"- 执行规则：{analysis.execution_rule}",
                f"- 单边成本率：{analysis.transaction_cost_rate:.3%}",
                f"- 滑点率：{analysis.slippage_rate:.3%}",
                f"- 调仓次数：{analysis.rebalance_count}",
                f"- 受限买入/卖出：{analysis.blocked_buy_orders}/"
                f"{analysis.blocked_sell_orders}",
                f"- 延迟执行交易日：{analysis.delayed_execution_days}",
                f"- 期末权益：{analysis.final_equity:,.2f}",
                "",
                "| 指标 | 组合 | 基准 |",
                "| --- | ---: | ---: |",
                f"| 总收益 | {analysis.portfolio.total_return:.2%} | "
                f"{analysis.benchmark.total_return:.2%} |",
                f"| 年化收益 | {analysis.portfolio.annualized_return:.2%} | "
                f"{analysis.benchmark.annualized_return:.2%} |",
                f"| 年化波动 | {analysis.portfolio.annualized_volatility:.2%} | "
                f"{analysis.benchmark.annualized_volatility:.2%} |",
                f"| 最大回撤 | {analysis.portfolio.max_drawdown:.2%} | "
                f"{analysis.benchmark.max_drawdown:.2%} |",
                "",
                "## 样本拆分",
                "",
                *[
                    f"- {item.sample}（{item.start_date} 至 {item.end_date}）："
                    f"{item.portfolio.total_return:.2%}"
                    for item in analysis.sample_performance
                ],
                "",
                "## 成本敏感性",
                "",
                *[
                    f"- 交易成本 {item.transaction_cost_rate:.3%}，"
                    f"总成本率 {item.total_cost_rate:.3%}："
                    f"收益 {item.total_return:.2%}，回撤 {item.max_drawdown:.2%}"
                    for item in analysis.cost_sensitivity
                ],
                "",
                "> 回测已模拟停牌无报价、收盘涨跌停限制、延迟成交和滑点；"
                "尚未模拟整手、成交容量与盘中价格路径。",
                "",
            ]
        )
