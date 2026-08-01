from __future__ import annotations

import hashlib
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import polars as pl
import pytest

from qtrade.ui.futures import FuturesUiRepository
from qtrade.ui.server import make_handler

RESEARCH_ID = "research-ui-1"
SIGNAL_ID = "signal-ui-1"
BACKTEST_ID = "backtest-ui-1"
CONTRACT = "CU2609.SHF"


def write_versioned_parquet(directory: Path, filename: str, frame: pl.DataFrame) -> dict:
    path = directory / filename
    frame.write_parquet(path)
    return {
        "path": filename,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }


def write_ui_artifacts(curated: Path, reports: Path) -> None:
    research = curated / "futures" / "research" / f"build_id={RESEARCH_ID}"
    research.mkdir(parents=True)
    continuous_version = write_versioned_parquet(
        research,
        "continuous.parquet",
        pl.DataFrame(
            {
                "trade_date": ["2026-07-22", "2026-07-23", "2026-07-24"],
                "product_code": ["CU"] * 3,
                "contract_code": ["CU2608.SHF", "CU2608.SHF", CONTRACT],
                "continuous_index": [1.0, 1.01, 1.02],
                "research_price": [80_000.0, 80_800.0, 81_600.0],
            }
        ),
    )
    roll_version = write_versioned_parquet(
        research,
        "roll_schedule.parquet",
        pl.DataFrame(
            {
                "product_code": ["CU"],
                "decision_date": ["2026-07-23"],
                "effective_date": ["2026-07-24"],
                "previous_contract": ["CU2608.SHF"],
                "selected_contract": [CONTRACT],
                "roll": [True],
                "reason": ["open_interest_confirmation"],
            }
        ),
    )
    (research / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": RESEARCH_ID,
                "passed": True,
                "provider": "fake",
                "start_date": "2026-07-22",
                "end_date": "2026-07-24",
                "rows": {"continuous": 3, "universe": 1, "roll_schedule": 1},
                "issue_count": 0,
                "output_versions": [continuous_version, roll_version],
            }
        ),
        encoding="utf-8",
    )

    signal = curated / "futures" / "signals" / f"build_id={SIGNAL_ID}"
    signal.mkdir(parents=True)
    target_version = write_versioned_parquet(
        signal,
        "targets.parquet",
        pl.DataFrame(
            {
                "signal_date": ["2026-07-23"],
                "eligible_date": ["2026-07-24"],
                "product_code": ["CU"],
                "contract_code": [CONTRACT],
                "sector": ["base_metals"],
                "signal_strength": [1.0],
                "estimated_daily_volatility": [0.015],
                "target_signed_lots": [2],
                "initial_margin": [81_600.0],
                "stress_margin": [122_400.0],
                "status": ["targeted"],
                "limit_reasons": [[]],
            }
        ),
    )
    (signal / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": SIGNAL_ID,
                "passed": True,
                "research_build_id": RESEARCH_ID,
                "protocol_id": "trend-protocol",
                "signal_date": "2026-07-23",
                "eligible_date": "2026-07-24",
                "equity": 1_000_000.0,
                "portfolio_daily_risk_budget": 5_000.0,
                "total_daily_risk": 2_500.0,
                "initial_margin": 81_600.0,
                "stress_margin": 122_400.0,
                "sector_daily_risk": {"base_metals": 2_500.0},
                "rows": {"targets": 1},
                "output_versions": [target_version],
            }
        ),
        encoding="utf-8",
    )

    backtest = curated / "futures" / "backtests" / f"build_id={BACKTEST_ID}"
    backtest.mkdir(parents=True)
    (backtest / "manifest.json").write_text(
        json.dumps(
            {
                "build_id": BACKTEST_ID,
                "passed": True,
                "research_build_id": RESEARCH_ID,
                "final_equity": 1_020_000.0,
                "margin_call_days": 0,
                "issue_count": 1,
            }
        ),
        encoding="utf-8",
    )
    pl.DataFrame(
        {
            "trade_date": ["2026-07-23", "2026-07-24"],
            "equity": [1_000_000.0, 1_020_000.0],
            "margin": [0.0, 81_600.0],
            "stress_margin": [0.0, 122_400.0],
            "available_cash": [1_000_000.0, 938_400.0],
            "margin_call": [False, False],
        }
    ).write_parquet(backtest / "accounts.parquet")
    pl.DataFrame(
        {
            "trade_date": ["2026-07-24"],
            "contract_code": [CONTRACT],
            "signed_lots": [2],
            "multiplier": [5.0],
            "settlement_basis": [81_600.0],
        }
    ).write_parquet(backtest / "positions.parquet")
    pl.DataFrame(
        {
            "trade_date": ["2026-07-24"],
            "order_id": ["trend:1"],
            "status": ["filled"],
            "reason": ["filled"],
            "fill_price": [81_500.0],
        }
    ).write_parquet(backtest / "executions.parquet")
    pl.DataFrame(
        {
            "trade_date": ["2026-07-24"],
            "severity": ["warning"],
            "code": ["delayed_fill"],
            "message": ["filled after one retry"],
        }
    ).write_parquet(backtest / "issues.parquet")
    backtest_manifest = json.loads((backtest / "manifest.json").read_text(encoding="utf-8"))
    backtest_manifest["output_versions"] = [
        {
            "path": name,
            "sha256": hashlib.sha256((backtest / name).read_bytes()).hexdigest(),
            "size": (backtest / name).stat().st_size,
        }
        for name in (
            "accounts.parquet",
            "positions.parquet",
            "executions.parquet",
            "issues.parquet",
        )
    ]
    (backtest / "manifest.json").write_text(
        json.dumps(backtest_manifest),
        encoding="utf-8",
    )

    readiness = (
        reports
        / "futures"
        / "validation-readiness"
        / "futures_trend_v1"
        / "development"
        / "audit"
    )
    readiness.mkdir(parents=True)
    (readiness / "report.json").write_text(
        json.dumps(
            {
                "protocol_id": "futures_trend_v1",
                "partition": "development",
                "ready": False,
                "dataset_coverage": {
                    "futures_daily": {
                        "first": "2026-07-01",
                        "last": "2026-07-31",
                        "partitions": 23,
                    },
                    "futures_limits": {"first": None, "last": None, "partitions": 0},
                },
                "issues": [
                    {"severity": "error", "code": "missing_futures_limits", "message": "No limits."}
                ],
            }
        ),
        encoding="utf-8",
    )

    daily = (
        curated
        / "futures"
        / "futures_daily"
        / "provider=fake"
        / "as_of_date=2026-07-24"
    )
    daily.mkdir(parents=True)
    pl.DataFrame(
        {
            "ts_code": [CONTRACT],
            "trade_date": ["20260724"],
            "open": [81_000.0],
            "high": [82_000.0],
            "low": [80_500.0],
            "close": [81_800.0],
            "settle": [81_600.0],
            "vol": [50_000.0],
            "oi": [120_000.0],
        }
    ).write_parquet(daily / "data.parquet")
    research_manifest = json.loads((research / "manifest.json").read_text(encoding="utf-8"))
    daily_path = daily / "data.parquet"
    research_manifest["inputs"] = [
        {
            "path": daily_path.relative_to(curated).as_posix(),
            "sha256": hashlib.sha256(daily_path.read_bytes()).hexdigest(),
            "size": daily_path.stat().st_size,
        }
    ]
    (research / "manifest.json").write_text(
        json.dumps(research_manifest),
        encoding="utf-8",
    )


def test_futures_ui_repository_builds_operator_dashboard(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    reports = tmp_path / "reports"
    write_ui_artifacts(curated, reports)
    repository = FuturesUiRepository(curated, reports, "fake")

    meta = repository.meta()
    dashboard = repository.dashboard(SIGNAL_ID)

    assert meta["latest_signal_build_id"] == SIGNAL_ID
    assert meta["latest_backtest_build_id"] == BACKTEST_ID
    assert dashboard["signal"]["target_count"] == 1
    assert dashboard["signal"]["long_count"] == 1
    assert dashboard["targets"][0]["contract_code"] == CONTRACT
    assert dashboard["portfolio"]["positions"][0]["signed_lots"] == 2
    assert dashboard["quality"]["ready"] is False
    assert dashboard["quality"]["issues"][0]["code"] == "missing_futures_limits"


def test_futures_ui_repository_returns_contract_chart_and_backtest(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    reports = tmp_path / "reports"
    write_ui_artifacts(curated, reports)
    repository = FuturesUiRepository(curated, reports, "fake")

    chart = repository.chart(SIGNAL_ID, "CU")
    backtest = repository.backtest(BACKTEST_ID)

    assert len(chart["continuous"]) == 3
    assert chart["actual"][0]["contract_code"] == CONTRACT
    assert chart["rolls"][0]["selected_contract"] == CONTRACT
    assert backtest["metrics"]["total_return"] == 0.02
    assert backtest["positions"][0]["contract_code"] == CONTRACT
    assert backtest["issues"][0]["code"] == "delayed_fill"


def test_futures_ui_repository_returns_truthful_empty_state(tmp_path: Path) -> None:
    repository = FuturesUiRepository(tmp_path / "curated", tmp_path / "reports", "fake")

    dashboard = repository.dashboard()
    backtest = repository.backtest()

    assert dashboard["signal"]["build_id"] is None
    assert dashboard["targets"] == []
    assert dashboard["portfolio"]["positions"] == []
    assert dashboard["quality"]["ready"] is False
    assert dashboard["quality"]["issues"][0]["code"] == "missing_signal_snapshot"
    assert backtest["metrics"] == {}


def test_futures_ui_repository_rejects_tampered_content(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    reports = tmp_path / "reports"
    write_ui_artifacts(curated, reports)
    target = curated / "futures" / "signals" / f"build_id={SIGNAL_ID}" / "targets.parquet"
    target.write_bytes(target.read_bytes() + b"tampered")

    repository = FuturesUiRepository(curated, reports, "fake")

    with pytest.raises(ValueError, match="哈希"):
        repository.dashboard(SIGNAL_ID)


def test_futures_ui_repository_rejects_unversioned_backtest(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    reports = tmp_path / "reports"
    write_ui_artifacts(curated, reports)
    manifest_path = curated / "futures" / "backtests" / f"build_id={BACKTEST_ID}" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("output_versions")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="哈希声明"):
        FuturesUiRepository(curated, reports, "fake").backtest(BACKTEST_ID)


def test_futures_api_is_read_only_structured_and_does_not_leak_paths(tmp_path: Path) -> None:
    curated = tmp_path / "private" / "curated"
    reports = tmp_path / "private" / "reports"
    write_ui_artifacts(curated, reports)
    repository = FuturesUiRepository(curated, reports, "fake")
    application = SimpleNamespace(futures=repository, assets_root=tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(f"{base}/api/futures/meta") as response:
            meta = json.load(response)
        with urlopen(f"{base}/api/futures/overview") as response:
            overview = json.load(response)
        with urlopen(f"{base}/api/futures/chart?product=CU") as response:
            chart = json.load(response)
        assert meta["latest_signal_build_id"] == SIGNAL_ID
        assert overview["signal"]["build_id"] == SIGNAL_ID
        assert chart["contract_code"] == CONTRACT

        with pytest.raises(HTTPError) as captured:
            urlopen(f"{base}/api/futures/chart?product=../../private")
        payload = json.load(captured.value)
        assert captured.value.code == 400
        assert payload["error"]["code"] == "invalid_request"

        def leak_local_path(*_args: object) -> None:
            raise ValueError(f"private artifact failed: {tmp_path}")

        repository.chart = leak_local_path  # type: ignore[method-assign]
        with pytest.raises(HTTPError) as captured:
            urlopen(f"{base}/api/futures/chart?product=CU")
        payload = json.load(captured.value)
        assert str(tmp_path) not in json.dumps(payload)
        assert "[本地路径]" in payload["error"]["message"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_futures_workspace_static_contract() -> None:
    assets = Path(__file__).parents[1] / "src" / "qtrade" / "ui" / "assets"
    html = (assets / "index.html").read_text(encoding="utf-8")
    script = (assets / "app.js").read_text(encoding="utf-8")
    styles = (assets / "styles.css").read_text(encoding="utf-8")

    for marker in (
        'id="marketSwitch"',
        'id="futuresWorkspace"',
        'id="futuresSignals"',
        'id="futuresPositions"',
        'id="futuresChart"',
        'id="futuresQuality"',
    ):
        assert marker in html
    assert "/api/futures/overview" in script
    assert "/api/futures/chart" in script
    assert "overflow-x: auto" in styles
