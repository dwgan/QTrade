from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from qtrade.factors.analyzer import FactorComputation

EXCLUSION_LABELS = {
    "missing_security_metadata": "缺少证券或行业信息",
    "special_treatment_or_delisting": "ST或退市风险",
    "excluded_financial_industry": "首版排除金融行业",
    "insufficient_listing_history": "上市时间不足",
    "missing_valuation_data": "缺少估值数据",
    "missing_financial_data": "缺少可用财务公告",
    "low_liquidity": "流动性不足",
    "at_up_limit": "处于涨停",
    "at_down_limit": "处于跌停",
}


class FactorReportWriter:
    def __init__(self, reports_root: Path) -> None:
        self.reports_root = Path(reports_root)

    def write(self, computation: FactorComputation) -> tuple[Path, Path, Path]:
        analysis = computation.analysis
        directory = self.reports_root / "factors" / analysis.as_of_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / "factors.json"
        markdown_path = directory / "factors.md"
        rankings_path = directory / "rankings.parquet"
        self._atomic_text(
            json_path,
            json.dumps(
                analysis.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
        )
        self._atomic_text(markdown_path, self._markdown(computation))
        temporary = directory / f".rankings.{uuid.uuid4().hex}.parquet"
        computation.rankings.write_parquet(temporary, compression="zstd")
        os.replace(temporary, rankings_path)
        return json_path, markdown_path, rankings_path

    @staticmethod
    def _markdown(computation: FactorComputation) -> str:
        analysis = computation.analysis
        lines = [
            f"# 多因子候选股票：{analysis.as_of_date.isoformat()}",
            "",
            f"- 初始股票数：{analysis.universe_size}",
            f"- 过滤后股票数：{analysis.eligible_size}",
            f"- 完成排名股票数：{analysis.ranked_size}",
            f"- 数据置信度：{analysis.data_confidence}",
            "",
            "## 候选股票",
            "",
            "| 全局排名 | 股票 | 名称 | 行业 | 综合 | 质量 | 价值 | 动量 | 低风险 | 主要理由 |",
            "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
        for item in analysis.candidates:
            lines.append(
                f"| {item.rank} | {item.ts_code} | {item.name} | {item.industry} | "
                f"{item.score:.1f} | {item.quality_score:.1f} | "
                f"{item.value_score:.1f} | {item.momentum_score:.1f} | "
                f"{item.low_risk_score:.1f} | {'；'.join(item.reasons)} |"
            )
            if item.risk_flags:
                lines.append(f"|  |  | 风险 |  |  |  |  |  |  | {'；'.join(item.risk_flags)} |")

        lines.extend(["", "## 过滤统计", ""])
        if analysis.exclusion_counts:
            for reason, count in analysis.exclusion_counts.items():
                lines.append(f"- {EXCLUSION_LABELS.get(reason, reason)}：{count}")
        else:
            lines.append("- 没有股票被过滤。")

        lines.extend(["", "## 数据提示", ""])
        if analysis.warnings:
            lines.extend(f"- {warning}" for warning in analysis.warnings)
        else:
            lines.append("- 未发现影响本次排名的数据问题。")
        lines.extend(
            [
                "",
                "> 综合排名用于缩小研究范围，不代表买入建议。使用前仍需检查公司公告、"
                "行业风险和个人持仓约束。",
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
