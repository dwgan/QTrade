from __future__ import annotations

import html
import json
import os
import uuid
from datetime import date
from pathlib import Path
from typing import Any


class DashboardBuilder:
    """Build a dependency-free HTML dashboard from generated JSON reports."""

    def __init__(self, reports_root: Path) -> None:
        self.root = Path(reports_root)

    @staticmethod
    def _load(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None

    @staticmethod
    def _value(value: Any, suffix: str = "") -> str:
        if value is None:
            return "-"
        return f"{html.escape(str(value))}{suffix}"

    @staticmethod
    def _percent(value: Any) -> str:
        return "-" if value is None else f"{float(value):.2%}"

    def build(self, as_of_date: date) -> Path:
        day = as_of_date.isoformat()
        market = self._load(self.root / "market" / day / "market.json")
        industry = self._load(self.root / "industry" / day / "industry.json")
        factors = self._load(self.root / "factors" / day / "factors.json")
        observation = self._load(
            self.root / "observations" / day / "observation.json"
        )
        pipeline = self._load(self.root / "pipeline" / day / "run.json")
        if pipeline is not None:
            statuses = {
                item.get("name"): item.get("status")
                for item in pipeline.get("steps") or []
            }
            if statuses.get("market_analysis") != "completed":
                market = None
            if statuses.get("industry_analysis") != "completed":
                industry = None
            if statuses.get("factor_analysis") != "completed":
                factors = None
            if statuses.get("daily_observation") != "completed":
                observation = None
        content = self._render(
            as_of_date,
            market,
            industry,
            factors,
            observation,
            pipeline,
        )
        directory = self.root / "dashboard" / day
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "index.html"
        temporary = directory / f".index.{uuid.uuid4().hex}.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, target)
        return target

    def _render(
        self,
        as_of_date: date,
        market: dict[str, Any] | None,
        industry: dict[str, Any] | None,
        factors: dict[str, Any] | None,
        observation: dict[str, Any] | None,
        pipeline: dict[str, Any] | None,
    ) -> str:
        sections = [
            self._market_section(market),
            self._industry_section(industry),
            self._candidate_section(factors),
            self._observation_section(observation),
            self._pipeline_section(pipeline),
        ]
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>QTrade {as_of_date}</title>
  <style>
    :root {{ color-scheme: light; --ink:#182230; --muted:#667085; --line:#e4e7ec;
      --surface:#fff; --canvas:#f5f7fa; --accent:#175cd3; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--canvas); color:var(--ink);
      font:14px/1.5 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }}
    main {{ max-width:1180px; margin:0 auto; padding:28px 20px 60px; }}
    header {{ display:flex; justify-content:space-between; align-items:end; margin-bottom:22px; }}
    h1 {{ margin:0; font-size:28px; }} h2 {{ margin:0 0 14px; font-size:18px; }}
    .muted {{ color:var(--muted); }} .grid {{ display:grid;
      grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
    .card,section {{ background:var(--surface); border:1px solid var(--line);
      border-radius:12px; box-shadow:0 1px 2px #1018280d; }}
    section {{ padding:18px; margin-top:16px; overflow:auto; }}
    .card {{ padding:14px; }} .label {{ color:var(--muted); font-size:12px; }}
    .metric {{ margin-top:4px; font-size:22px; font-weight:650; }}
    table {{ width:100%; border-collapse:collapse; white-space:nowrap; }}
    th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; }}
    th {{ color:var(--muted); font-weight:600; }} td.num,th.num {{ text-align:right; }}
    .pill {{ display:inline-block; padding:2px 8px; border-radius:999px;
      color:var(--accent); background:#eff8ff; }} ul {{ margin:8px 0; padding-left:20px; }}
    .empty {{ color:var(--muted); padding:12px 0; }}
  </style>
</head>
<body><main>
  <header><div><h1>QTrade 收盘观察</h1><div class="muted">本地只读研究看板</div></div>
  <div class="pill">{as_of_date}</div></header>
  {''.join(sections)}
</main></body></html>"""

    def _market_section(self, data: dict[str, Any] | None) -> str:
        if data is None:
            return self._empty_section("市场状态")
        breadth = data.get("breadth") or {}
        risk = data.get("risk") or {}
        cards = (
            ("市场状态", data.get("state")),
            ("市场温度", data.get("temperature")),
            ("趋势得分", data.get("trend_score")),
            ("宽度得分", breadth.get("score")),
            ("风险健康度", risk.get("health_score")),
        )
        return "<section><h2>市场状态</h2><div class=\"grid\">" + "".join(
            f'<div class="card"><div class="label">{html.escape(label)}</div>'
            f'<div class="metric">{self._value(value)}</div></div>'
            for label, value in cards
        ) + "</div></section>"

    def _industry_section(self, data: dict[str, Any] | None) -> str:
        if data is None:
            return self._empty_section("行业强弱")
        rows = []
        for item in (data.get("industries") or [])[:10]:
            rows.append(
                "<tr>"
                f"<td>{self._value(item.get('rank'))}</td>"
                f"<td>{self._value(item.get('name'))}</td>"
                f"<td class=\"num\">{self._value(item.get('score'))}</td>"
                f"<td class=\"num\">{self._percent(item.get('return_20d'))}</td>"
                f"<td>{self._value(item.get('state'))}</td>"
                "</tr>"
            )
        return self._table_section(
            "行业强弱",
            ("排名", "行业", "得分", "20日收益", "状态"),
            rows,
        )

    def _candidate_section(self, data: dict[str, Any] | None) -> str:
        if data is None:
            return self._empty_section("多因子候选")
        rows = []
        for item in (data.get("candidates") or [])[:20]:
            rows.append(
                "<tr>"
                f"<td>{self._value(item.get('rank'))}</td>"
                f"<td>{self._value(item.get('ts_code'))}</td>"
                f"<td>{self._value(item.get('name'))}</td>"
                f"<td>{self._value(item.get('industry'))}</td>"
                f"<td class=\"num\">{self._value(item.get('score'))}</td>"
                f"<td class=\"num\">{self._value(item.get('quality_score'))}</td>"
                f"<td class=\"num\">{self._value(item.get('momentum_score'))}</td>"
                "</tr>"
            )
        return self._table_section(
            "多因子候选",
            ("排名", "代码", "名称", "行业", "综合", "质量", "动量"),
            rows,
        )

    def _observation_section(self, data: dict[str, Any] | None) -> str:
        if data is None:
            return self._empty_section("每日变化与影子组合")
        entered = data.get("entered_candidates") or []
        exited = data.get("exited_candidates") or []
        shadow = data.get("shadow_portfolio") or {}
        holdings = shadow.get("holdings") or []
        return (
            "<section><h2>每日变化与影子组合</h2><div class=\"grid\">"
            f'<div class="card"><div class="label">新进入 / 退出</div>'
            f'<div class="metric">{len(entered)} / {len(exited)}</div></div>'
            f'<div class="card"><div class="label">影子组合收益</div>'
            f'<div class="metric">{self._percent(shadow.get("total_return"))}</div></div>'
            f'<div class="card"><div class="label">影子组合回撤</div>'
            f'<div class="metric">{self._percent(shadow.get("max_drawdown"))}</div></div>'
            f'<div class="card"><div class="label">持仓数 / 现金</div>'
            f'<div class="metric">{len(holdings)} / '
            f'{self._percent(shadow.get("cash_weight"))}</div></div>'
            "</div></section>"
        )

    def _pipeline_section(self, data: dict[str, Any] | None) -> str:
        if data is None:
            return self._empty_section("流水线状态")
        rows = [
            "<tr>"
            f"<td>{self._value(item.get('name'))}</td>"
            f"<td>{self._value(item.get('status'))}</td>"
            f"<td>{self._value(item.get('message'))}</td>"
            "</tr>"
            for item in data.get("steps") or []
        ]
        return self._table_section("流水线状态", ("步骤", "状态", "说明"), rows)

    @staticmethod
    def _empty_section(title: str) -> str:
        return (
            f"<section><h2>{html.escape(title)}</h2>"
            '<div class="empty">当日结构化报告尚未生成。</div></section>'
        )

    @staticmethod
    def _table_section(title: str, headers: tuple[str, ...], rows: list[str]) -> str:
        header = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
        body = "".join(rows) or (
            f'<tr><td class="empty" colspan="{len(headers)}">暂无数据</td></tr>'
        )
        return (
            f"<section><h2>{html.escape(title)}</h2><table><thead><tr>{header}</tr>"
            f"</thead><tbody>{body}</tbody></table></section>"
        )
