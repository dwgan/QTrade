import json
from pathlib import Path

import polars as pl
import pytest
from test_factor_analyzer import AS_OF_DATE, factor_inputs

from qtrade.config import FactorConfig
from qtrade.factors.analyzer import FactorAnalyzer, FactorComputation
from qtrade.factors.reporting import FactorReportWriter
from qtrade.research.snapshots import FactorSnapshotStore


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


def test_factor_report_keeps_content_addressed_immutable_versions(
    tmp_path: Path,
) -> None:
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
    writer = FactorReportWriter(tmp_path)

    writer.write(computation)
    day = tmp_path / "factors" / AS_OF_DATE.isoformat()
    first_pointer = json.loads((day / "latest.json").read_text(encoding="utf-8"))
    first_version = day / "versions" / first_pointer["signal_id"]
    first_bytes = (first_version / "rankings.parquet").read_bytes()
    writer.write(computation)

    assert len(list((day / "versions").iterdir())) == 1
    assert first_pointer["origin"] == "reconstructed"

    changed = FactorComputation(
        analysis=computation.analysis,
        rankings=computation.rankings.with_columns(
            (pl.col("score") + 0.01).alias("score")
        ),
    )
    writer.write(changed)

    assert len(list((day / "versions").iterdir())) == 2
    assert (first_version / "rankings.parquet").read_bytes() == first_bytes


def test_factor_snapshot_store_rejects_tampered_immutable_version(
    tmp_path: Path,
) -> None:
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
    FactorReportWriter(tmp_path).write(computation)
    day = tmp_path / "factors" / AS_OF_DATE.isoformat()
    latest = json.loads((day / "latest.json").read_text(encoding="utf-8"))
    version_ranking = day / "versions" / latest["signal_id"] / "rankings.parquet"
    version_ranking.write_bytes(b"tampered")

    store = FactorSnapshotStore(tmp_path)
    with pytest.raises(ValueError, match="hash mismatch"):
        store.read(AS_OF_DATE)
