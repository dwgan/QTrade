from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from qtrade.market.models import MarketAnalysis, MarketState

STATE_LABELS = {
    MarketState.ATTACK: "进攻",
    MarketState.BALANCED: "均衡",
    MarketState.DEFENSIVE: "防守",
    MarketState.HIGH_RISK: "高风险",
    MarketState.INSUFFICIENT_DATA: "数据不足",
}


class MarketReportWriter:
    def __init__(self, reports_root: Path) -> None:
        self.reports_root = Path(reports_root)

    def write(self, analysis: MarketAnalysis) -> tuple[Path, Path]:
        directory = self.reports_root / "market" / analysis.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "market.json"
        markdown_path = directory / "market.md"

        self._atomic_text(
            json_path,
            json.dumps(
                analysis.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
        )
        self._atomic_text(markdown_path, self._markdown(analysis))
        return json_path, markdown_path

    @staticmethod
    def _percent(value: float | None) -> str:
        return "—" if value is None else f"{value:.1%}"

    @staticmethod
    def _score(value: float | None) -> str:
        return "—" if value is None else f"{value:.1f}"

    def _markdown(self, analysis: MarketAnalysis) -> str:
        lines = [
            f"# 市场分析：{analysis.as_of_date.isoformat()}",
            "",
            f"- 市场状态：**{STATE_LABELS[analysis.state]}**",
            f"- 市场温度：{self._score(analysis.temperature)} / 100",
            f"- 数据置信度：{analysis.data_confidence}",
            f"- 历史区间：{analysis.history_start_date} 至 {analysis.history_end_date}",
            "",
            "## 评分构成",
            "",
            "| 维度 | 得分 |",
            "| --- | ---: |",
            f"| 趋势 | {self._score(analysis.trend_score)} |",
            f"| 市场宽度 | {self._score(analysis.breadth.score)} |",
            f"| 风险健康度 | {self._score(analysis.risk.health_score)} |",
            "",
            "市场温度使用趋势 45%、市场宽度 35%、风险健康度 20% 合成。"
            "风险健康度越高表示波动和回撤压力越小。",
            "",
            "## 主要指数",
            "",
            "| 指数 | 收盘 | 20日收益 | 60日收益 | 趋势得分 | 20日年化波动 | 120日回撤 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for item in analysis.indices:
            lines.append(
                f"| {item.code} | {item.close:.2f} | {self._percent(item.return_20d)} | "
                f"{self._percent(item.return_60d)} | {self._score(item.trend_score)} | "
                f"{self._percent(item.annualized_volatility_20d)} | "
                f"{self._percent(item.drawdown_120d)} |"
            )

        breadth = analysis.breadth
        lines.extend(
            [
                "",
                "## 市场宽度",
                "",
                f"- 有效股票数：{breadth.eligible_stocks}",
                f"- 位于20日均线上方：{self._percent(breadth.above_ma_20)}",
                f"- 位于60日均线上方：{self._percent(breadth.above_ma_60)}",
                f"- 位于120日均线上方：{self._percent(breadth.above_ma_120)}",
                f"- 当日上涨比例：{self._percent(breadth.advance_ratio)}",
                f"- 60日新高比例：{self._percent(breadth.new_high_60_ratio)}",
                f"- 60日新低比例：{self._percent(breadth.new_low_60_ratio)}",
                "",
                "## 风险",
                "",
                f"- 主要指数20日年化波动：{self._percent(analysis.risk.annualized_volatility_20d)}",
                f"- 主要指数120日回撤：{self._percent(analysis.risk.drawdown_120d)}",
                "",
                "## 数据提示",
                "",
            ]
        )
        if analysis.warnings:
            lines.extend(f"- {warning}" for warning in analysis.warnings)
        else:
            lines.append("- 未发现影响本次评分的数据问题。")
        lines.extend(
            [
                "",
                "> 本报告用于个人研究和风险观察，不构成确定性预测或投资建议。",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
