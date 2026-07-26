from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from datetime import datetime
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
        temporary_rankings = directory / f".rankings.{uuid.uuid4().hex}.parquet"
        computation.rankings.write_parquet(temporary_rankings, compression="zstd")
        rankings_hash = self._file_hash(temporary_rankings)
        semantic_analysis = analysis.model_dump(
            mode="json",
            exclude={"created_at"},
        )
        analysis_hash = hashlib.sha256(
            json.dumps(
                semantic_analysis,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        signal_id = hashlib.sha256(
            f"{analysis.signal_origin.value}:{analysis_hash}:{rankings_hash}".encode()
        ).hexdigest()
        version_directory = directory / "versions" / signal_id
        if version_directory.exists():
            temporary_rankings.unlink()
            self._verify_version(version_directory, signal_id)
        else:
            version_directory.mkdir(parents=True)
            self._atomic_text(
                version_directory / "factors.json",
                json.dumps(
                    analysis.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            self._atomic_text(
                version_directory / "factors.md",
                self._markdown(computation),
            )
            os.replace(temporary_rankings, version_directory / "rankings.parquet")
            manifest = {
                "schema_version": 1,
                "signal_id": signal_id,
                "as_of_date": analysis.as_of_date.isoformat(),
                "origin": analysis.signal_origin.value,
                "created_at": datetime.now().isoformat(),
                "analysis_content_hash": analysis_hash,
                "files": {
                    name: self._file_hash(version_directory / name)
                    for name in ("factors.json", "factors.md", "rankings.parquet")
                },
            }
            self._atomic_text(
                version_directory / "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        json_path = directory / "factors.json"
        markdown_path = directory / "factors.md"
        rankings_path = directory / "rankings.parquet"
        for name, target in (
            ("factors.json", json_path),
            ("factors.md", markdown_path),
            ("rankings.parquet", rankings_path),
        ):
            self._atomic_copy(version_directory / name, target)
        self._atomic_text(
            directory / "latest.json",
            json.dumps(
                {
                    "signal_id": signal_id,
                    "origin": analysis.signal_origin.value,
                    "version_path": f"versions/{signal_id}",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        return json_path, markdown_path, rankings_path

    @classmethod
    def _verify_version(cls, directory: Path, signal_id: str) -> None:
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            raise ValueError(f"Incomplete immutable signal version: {directory}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("signal_id") != signal_id:
            raise ValueError(f"Signal manifest id mismatch: {directory}")
        for name, expected in manifest.get("files", {}).items():
            path = directory / name
            if not path.exists() or cls._file_hash(path) != expected:
                raise ValueError(f"Immutable signal file hash mismatch: {path}")

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _atomic_copy(source: Path, target: Path) -> None:
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)

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
