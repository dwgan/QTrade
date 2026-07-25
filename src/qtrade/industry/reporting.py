from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from qtrade.industry.models import IndustryAnalysis, IndustryState

STATE_LABELS = {
    IndustryState.TREND_STRENGTHENING: "趋势增强",
    IndustryState.STRONG_CONTINUATION: "强势延续",
    IndustryState.HIGH_LEVEL_DIVERGENCE: "高位分化",
    IndustryState.WEAK_RECOVERY: "弱势修复",
    IndustryState.WEAKENING: "趋势转弱",
    IndustryState.NEUTRAL: "中性",
}

STYLE_LABELS = {
    "mid_vs_large": "中盘相对大盘",
    "small_vs_large": "小盘相对大盘",
}


class IndustryReportWriter:
    def __init__(self, reports_root: Path, top_count: int) -> None:
        self.reports_root = Path(reports_root)
        self.top_count = top_count

    def write(self, analysis: IndustryAnalysis) -> tuple[Path, Path]:
        directory = self.reports_root / "industry" / analysis.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "industry.json"
        markdown_path = directory / "industry.md"
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

    def _markdown(self, analysis: IndustryAnalysis) -> str:
        lines = [
            f"# 行业与风格分析：{analysis.as_of_date.isoformat()}",
            "",
            f"- 行业分类字段：{analysis.classification}",
            f"- 分类快照日期：{analysis.classification_snapshot_date}",
            f"- 比较基准：{analysis.benchmark_code}",
            f"- 数据置信度：{analysis.data_confidence}",
            "",
            "## 风格相对强弱",
            "",
            "| 风格 | 5日相对收益 | 20日相对收益 | 60日相对收益 | 当前领先 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for style in analysis.styles:
            if style.leader == "numerator":
                leader = style.numerator_code
            elif style.leader == "denominator":
                leader = style.denominator_code
            elif style.leader == "balanced":
                leader = "均衡"
            else:
                leader = "数据不足"
            lines.append(
                f"| {STYLE_LABELS.get(style.name, style.name)} | "
                f"{self._percent(style.relative_return_5d)} | "
                f"{self._percent(style.relative_return_20d)} | "
                f"{self._percent(style.relative_return_60d)} | {leader} |"
            )

        lines.extend(
            [
                "",
                f"## 行业排名（前 {min(self.top_count, len(analysis.industries))}）",
                "",
                "| 排名 | 行业 | 得分 | 状态 | 股票数 | 5日收益 | 20日超额 | "
                "60日超额 | 60日均线上方 | 上涨比例 |",
                "| ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for item in analysis.industries[: self.top_count]:
            lines.append(
                f"| {item.rank} | {item.name} | {item.score:.1f} | "
                f"{STATE_LABELS[item.state]} | {item.stock_count} | "
                f"{self._percent(item.return_5d)} | "
                f"{self._percent(item.relative_return_20d)} | "
                f"{self._percent(item.relative_return_60d)} | "
                f"{self._percent(item.above_ma_60)} | "
                f"{self._percent(item.advance_ratio)} |"
            )

        lines.extend(["", "## 数据提示", ""])
        if analysis.warnings:
            lines.extend(f"- {warning}" for warning in analysis.warnings)
        else:
            lines.append("- 未发现影响本次分析的数据问题。")
        lines.extend(
            [
                "",
                "> 行业排名反映相对强弱和内部扩散度，用于研究参考，不代表行业或个股的确定性收益。",
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
