from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from qtrade.config import FuturesConfig
from qtrade.futures.backtest_input import FuturesBacktestInputCompiler
from qtrade.futures.strategy_validation import FuturesStrategyValidationService
from qtrade.futures.validation_readiness import FuturesValidationReadinessService
from qtrade.research.protocols import (
    PartitionName,
    ProtocolStatus,
    ProtocolStore,
    StrategyProtocol,
)


def test_committed_futures_trend_protocol_is_frozen_and_hash_verified() -> None:
    path = Path("config/futures_trend_v1.yaml")
    protocol = StrategyProtocol.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    assert protocol.status == ProtocolStatus.FROZEN
    assert protocol.content_hash == protocol.calculated_hash()
    assert protocol.allowed_trials == 1
    assert protocol.partition(PartitionName.DEVELOPMENT).start_date.isoformat() == "2015-01-01"
    assert protocol.partition(PartitionName.HOLDOUT).end_date.isoformat() == "2024-12-31"
    assert {item["name"] for item in protocol.execution["scenarios"]} == {
        "baseline",
        "delayed_execution_1d",
        "double_cost",
        "margin_up_50pct",
        "slower_roll",
        "faster_trend",
        "slower_trend",
    }


def test_committed_frozen_protocol_installs_idempotently(tmp_path: Path) -> None:
    protocol = StrategyProtocol.model_validate(
        yaml.safe_load(Path("config/futures_trend_v1.yaml").read_text(encoding="utf-8"))
    )
    store = ProtocolStore(tmp_path)

    first = store.install_frozen(protocol)
    second = store.install_frozen(protocol)

    assert first == second
    assert store.load(protocol.protocol_id) == protocol


def test_futures_holdout_is_rejected_before_any_data_read(tmp_path: Path) -> None:
    request_path = tmp_path / "holdout.json"
    request_path.write_text(
        json.dumps(
            {
                "protocol_path": str(Path("config/futures_trend_v1.yaml").resolve()),
                "partition": "holdout",
            }
        ),
        encoding="utf-8",
    )
    service = FuturesStrategyValidationService(
        FuturesConfig(),
        tmp_path / "curated",
        tmp_path / "reports",
        tmp_path / "runtime",
        Path.cwd(),
        enforce_clean_git=False,
    )

    with pytest.raises(ValueError, match="Holdout is sealed"):
        service.run(request_path)

    assert service.experiments.list("futures_trend_v1") == []


def test_readiness_audit_records_missing_limits_and_signal_coverage(tmp_path: Path) -> None:
    result = FuturesValidationReadinessService(
        tmp_path / "curated",
        tmp_path / "reports",
    ).audit(
        Path("config/futures_trend_v1.yaml"),
        PartitionName.DEVELOPMENT,
        "tushare",
    )

    payload = json.loads(result.report_json.read_text(encoding="utf-8"))
    codes = {item["code"] for item in payload["issues"]}
    assert not result.ready
    assert "missing_futures_limits" in codes
    assert "missing_signal_chain" in codes
    assert result.report_markdown.is_file()


def test_formal_compiler_rejects_signal_chain_with_a_missing_trading_day() -> None:
    protocol = StrategyProtocol.model_validate(
        yaml.safe_load(Path("config/futures_trend_v1.yaml").read_text(encoding="utf-8"))
    )
    chain = [
        (
            {"signal_date": "2015-01-05", "eligible_date": "2015-01-07"},
            None,
        )
    ]

    with pytest.raises(ValueError, match="every archived"):
        FuturesBacktestInputCompiler._validate_complete_partition(
            chain,  # type: ignore[arg-type]
            protocol,
            PartitionName.DEVELOPMENT,
            [date(2015, 1, 5), date(2015, 1, 6), date(2015, 1, 7)],
        )


def test_failed_validation_trial_is_retained_and_consumes_trial_limit(
    tmp_path: Path,
) -> None:
    baseline = StrategyProtocol.model_validate(
        yaml.safe_load(Path("config/futures_trend_v1.yaml").read_text(encoding="utf-8"))
    )
    values = baseline.model_dump(mode="json")
    values.update(
        protocol_id="futures_trial_limit_v1",
        partition_data_versions={PartitionName.VALIDATION.value: "0" * 64},
        content_hash="pending",
    )
    normalized = StrategyProtocol.model_validate(values)
    protocol = normalized.model_copy(update={"content_hash": normalized.calculated_hash()})
    protocol_path = tmp_path / "protocol.yaml"
    protocol_path.write_text(
        yaml.safe_dump(protocol.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    request_path = tmp_path / "validation.json"
    request_path.write_text(
        json.dumps(
            {
                "protocol_path": str(protocol_path),
                "partition": "validation",
                "research_build_id": "missing",
                "scenario_signal_build_ids": {"baseline": "missing"},
                "initial_equity": 1_000_000,
            }
        ),
        encoding="utf-8",
    )
    service = FuturesStrategyValidationService(
        FuturesConfig(),
        tmp_path / "curated",
        tmp_path / "reports",
        tmp_path / "runtime",
        Path.cwd(),
        enforce_clean_git=False,
    )

    with pytest.raises(FileNotFoundError):
        service.run(request_path)

    experiments = service.experiments.list(protocol.protocol_id)
    assert len(experiments) == 1
    assert experiments[0].error
    with pytest.raises(ValueError, match="trial limit reached"):
        service.run(request_path)


def test_parameter_neighbors_require_distinct_signal_protocols() -> None:
    protocol = StrategyProtocol.model_validate(
        yaml.safe_load(Path("config/futures_trend_v1.yaml").read_text(encoding="utf-8"))
    )
    scenarios = {item["name"]: item for item in protocol.execution["scenarios"]}

    baseline = FuturesBacktestInputCompiler._expected_signal_protocol_id(
        protocol,
        scenarios["baseline"],
    )
    faster = FuturesBacktestInputCompiler._expected_signal_protocol_id(
        protocol,
        scenarios["faster_trend"],
    )
    slower = FuturesBacktestInputCompiler._expected_signal_protocol_id(
        protocol,
        scenarios["slower_trend"],
    )

    assert baseline == protocol.config_hash
    assert len({baseline, faster, slower}) == 3
