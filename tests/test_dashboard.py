import json
from datetime import date
from pathlib import Path

from qtrade.dashboard.builder import DashboardBuilder


def test_dashboard_builds_from_available_reports_and_escapes_content(
    tmp_path: Path,
) -> None:
    day = date(2026, 7, 24)
    market_dir = tmp_path / "market" / day.isoformat()
    factor_dir = tmp_path / "factors" / day.isoformat()
    market_dir.mkdir(parents=True)
    factor_dir.mkdir(parents=True)
    (market_dir / "market.json").write_text(
        json.dumps(
            {
                "state": "balanced",
                "temperature": 55.0,
                "trend_score": 60.0,
                "breadth": {"score": 52.0},
                "risk": {"health_score": 48.0},
            }
        ),
        encoding="utf-8",
    )
    (factor_dir / "factors.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "rank": 1,
                        "ts_code": "000001.SZ",
                        "name": "<script>alert(1)</script>",
                        "industry": "Bank",
                        "score": 90,
                        "quality_score": 88,
                        "momentum_score": 92,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    path = DashboardBuilder(tmp_path).build(day)
    content = path.read_text(encoding="utf-8")

    assert "QTrade 收盘观察" in content
    assert "balanced" in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
    assert "<script>alert(1)</script>" not in content
    assert "当日结构化报告尚未生成" in content


def test_dashboard_hides_stale_report_when_pipeline_step_failed(tmp_path: Path) -> None:
    day = date(2026, 7, 24)
    factor_dir = tmp_path / "factors" / day.isoformat()
    pipeline_dir = tmp_path / "pipeline" / day.isoformat()
    factor_dir.mkdir(parents=True)
    pipeline_dir.mkdir(parents=True)
    (factor_dir / "factors.json").write_text(
        json.dumps({"candidates": [{"name": "STALE"}]}),
        encoding="utf-8",
    )
    (pipeline_dir / "run.json").write_text(
        json.dumps(
            {
                "steps": [
                    {"name": "factor_analysis", "status": "failed"},
                ]
            }
        ),
        encoding="utf-8",
    )

    content = DashboardBuilder(tmp_path).build(day).read_text(encoding="utf-8")

    assert "STALE" not in content
