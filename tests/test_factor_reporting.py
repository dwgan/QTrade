import json
from pathlib import Path

from test_factor_analyzer import AS_OF_DATE, factor_inputs

from qtrade.config import FactorConfig
from qtrade.factors.analyzer import FactorAnalyzer
from qtrade.factors.reporting import FactorReportWriter


def test_factor_report_writes_json_markdown_and_rankings(tmp_path: Path) -> None:
    computation = FactorAnalyzer(
        FactorConfig(
            minimum_listing_days=0,
            liquidity_exclusion_percentile=0,
            candidate_count=5,
        )
    ).analyze(
        AS_OF_DATE,
        *factor_inputs(),
        AS_OF_DATE,
        AS_OF_DATE,
        AS_OF_DATE,
    )

    json_path, markdown_path, rankings_path = FactorReportWriter(tmp_path).write(computation)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert len(payload["candidates"]) == 5
    assert "多因子候选股票" in markdown
    assert "综合排名用于缩小研究范围" in markdown
    assert rankings_path.exists()
