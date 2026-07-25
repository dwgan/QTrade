import json
from pathlib import Path

from test_industry_analyzer import (
    AS_OF_DATE,
    make_index_history,
    make_stock_history,
)

from qtrade.config import IndustryConfig
from qtrade.industry.analyzer import IndustryAnalyzer
from qtrade.industry.reporting import IndustryReportWriter


def test_industry_report_writes_rankings_and_styles(tmp_path: Path) -> None:
    stocks, master = make_stock_history()
    analysis = IndustryAnalyzer(IndustryConfig(minimum_stocks=5), "000300.SH").analyze(
        AS_OF_DATE,
        stocks,
        make_index_history(),
        master,
        AS_OF_DATE,
    )

    json_path, markdown_path = IndustryReportWriter(tmp_path, top_count=2).write(analysis)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert len(payload["industries"]) == 3
    assert "行业排名（前 2）" in markdown
    assert "中盘相对大盘" in markdown
    assert "科技" in markdown
