import json
from datetime import date
from pathlib import Path

from qtrade.market.models import (
    BreadthMetrics,
    IndexMetrics,
    MarketAnalysis,
    MarketState,
    RiskMetrics,
)
from qtrade.market.reporting import MarketReportWriter


def test_market_report_writes_json_and_markdown(tmp_path: Path) -> None:
    as_of_date = date(2026, 7, 24)
    analysis = MarketAnalysis(
        as_of_date=as_of_date,
        primary_index_code="000300.SH",
        state=MarketState.BALANCED,
        temperature=60,
        trend_score=50,
        breadth=BreadthMetrics(eligible_stocks=500, score=60),
        risk=RiskMetrics(health_score=80),
        indices=[IndexMetrics(code="000300.SH", close=4000, observations=200)],
        history_start_date=as_of_date,
        history_end_date=as_of_date,
        data_confidence="high",
    )

    json_path, markdown_path = MarketReportWriter(tmp_path).write(analysis)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert payload["state"] == "balanced"
    assert payload["temperature"] == 60
    assert "市场状态：**均衡**" in markdown
    assert "不构成确定性预测或投资建议" in markdown
