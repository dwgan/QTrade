from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from qtrade.config import FuturesConfig
from qtrade.futures.backtest_input import (
    FuturesBacktestInputBuildResult,
    FuturesBacktestInputCompiler,
)
from qtrade.futures.backtest_service import FuturesBacktestService
from qtrade.futures.trend import FuturesTrendProtocol
from qtrade.research.protocols import (
    ExperimentRecord,
    ExperimentStore,
    PartitionName,
    ProtocolStatus,
    ProtocolStore,
    StrategyProtocol,
    canonical_hash,
    current_git_commit,
    git_research_tree_is_clean,
)


@dataclass(frozen=True)
class FuturesValidationResult:
    experiment_id: str
    protocol_id: str
    partition: PartitionName
    accepted: bool
    scenario_count: int
    result_json: Path
    result_markdown: Path


class FuturesStrategyValidationService:
    def __init__(
        self,
        config: FuturesConfig,
        curated_root: Path,
        reports_root: Path,
        runtime_root: Path,
        project_root: Path,
        *,
        enforce_clean_git: bool = True,
    ) -> None:
        self.config = config
        self.curated_root = Path(curated_root)
        self.reports_root = Path(reports_root)
        self.runtime_root = Path(runtime_root)
        self.project_root = Path(project_root)
        self.enforce_clean_git = enforce_clean_git
        self.protocols = ProtocolStore(runtime_root)
        self.experiments = ExperimentStore(runtime_root)
        self.compiler = FuturesBacktestInputCompiler(curated_root, reports_root)

    def run(self, request_path: Path) -> FuturesValidationResult:
        request_path = Path(request_path).resolve()
        request = json.loads(request_path.read_text(encoding="utf-8"))
        protocol_path = Path(request["protocol_path"])
        if not protocol_path.is_absolute():
            protocol_path = (request_path.parent / protocol_path).resolve()
        protocol = self._load_protocol(protocol_path)
        installed_path = self.protocols.install_frozen(protocol)
        partition = PartitionName(str(request["partition"]))
        if partition == PartitionName.FORWARD:
            raise ValueError("Forward observation is not a historical validation partition.")
        if protocol.config_hash != FuturesTrendProtocol().protocol_id:
            raise ValueError("Frozen protocol does not match the implemented trend strategy.")
        if self.enforce_clean_git and not git_research_tree_is_clean(self.project_root):
            raise ValueError("Formal futures validation requires a clean research Git worktree.")
        strategy_commit = self._strategy_commit()
        if protocol.code_commit != "unknown" and strategy_commit != protocol.code_commit:
            raise ValueError(
                f"Frozen strategy commit {protocol.code_commit} does not match {strategy_commit}."
            )
        if partition == PartitionName.VALIDATION:
            self._require_partition_data_version(protocol, partition)
            trials = [
                item
                for item in self.experiments.list(protocol.protocol_id)
                if item.partition == partition and item.kind == "futures_robustness_suite"
            ]
            if len(trials) >= protocol.allowed_trials:
                raise ValueError(
                    f"Protocol validation trial limit reached ({protocol.allowed_trials})."
                )
        if partition == PartitionName.HOLDOUT:
            state = self.protocols.state(protocol.protocol_id)
            if state.holdout_revealed_at is None:
                if not bool(request.get("reveal_holdout")):
                    raise ValueError("Holdout is sealed; explicit one-time reveal is required.")
                self._require_partition_data_version(protocol, partition)
                self.protocols.reveal_holdout(protocol.protocol_id)
            else:
                self._require_partition_data_version(protocol, partition)
        expected = protocol.partition(partition)
        if expected.end_date is None:
            raise ValueError("Historical validation partition requires an end date.")
        experiment = self.experiments.create(
            ExperimentRecord(
                protocol_id=protocol.protocol_id,
                partition=partition,
                kind="futures_robustness_suite",
                start_date=expected.start_date,
                end_date=expected.end_date,
                code_commit=current_git_commit(self.project_root),
                config_hash=protocol.config_hash,
                protocol_hash=protocol.content_hash,
            )
        )
        try:
            compiled = self._compile_scenarios(
                request,
                protocol,
                installed_path,
                partition,
                experiment.experiment_id,
            )
            data_version = canonical_hash(
                {name: item.data_version for name, item in sorted(compiled.items())}
            )
            self._verify_partition_data_version(protocol, partition, data_version)
            summaries = self._run_compiled(request, compiled)
            accepted, checks = self._acceptance(protocol, partition, summaries)
            result_dir = (
                self.reports_root
                / "futures"
                / "validation"
                / protocol.protocol_id
                / partition.value
                / experiment.experiment_id
            )
            result_json = result_dir / "result.json"
            result_markdown = result_dir / "report.md"
            payload = {
                "experiment_id": experiment.experiment_id,
                "protocol_id": protocol.protocol_id,
                "protocol_hash": protocol.content_hash,
                "partition": partition.value,
                "data_version": data_version,
                "accepted": accepted,
                "checks": checks,
                "scenarios": summaries,
            }
            self._atomic_json(result_json, payload)
            self._write_report(result_markdown, payload)
            self.experiments.complete(
                experiment.experiment_id,
                data_version=data_version,
                result_json=result_json,
                result_markdown=result_markdown,
            )
            return FuturesValidationResult(
                experiment_id=experiment.experiment_id,
                protocol_id=protocol.protocol_id,
                partition=partition,
                accepted=accepted,
                scenario_count=len(summaries),
                result_json=result_json,
                result_markdown=result_markdown,
            )
        except Exception as error:
            self.experiments.fail(experiment.experiment_id, str(error))
            raise

    def _compile_scenarios(
        self,
        request: dict[str, Any],
        protocol: StrategyProtocol,
        protocol_path: Path,
        partition: PartitionName,
        experiment_id: str,
    ) -> dict[str, FuturesBacktestInputBuildResult]:
        signal_ids = request.get("scenario_signal_build_ids", {})
        research_ids = request.get("scenario_research_build_ids", {})
        baseline_signal = str(signal_ids.get("baseline", "")).strip()
        baseline_research = str(request.get("research_build_id", "")).strip()
        if not baseline_signal or not baseline_research:
            raise ValueError("Validation requires baseline signal and research build IDs.")
        compiled: dict[str, FuturesBacktestInputBuildResult] = {}
        for scenario in protocol.execution["scenarios"]:
            name = str(scenario["name"])
            signal_id = str(signal_ids.get(name) or baseline_signal).strip()
            research_id = str(research_ids.get(name) or baseline_research).strip()
            compile_request = {
                "protocol_path": str(protocol_path),
                "partition": partition.value,
                "scenario": name,
                "research_build_id": research_id,
                "terminal_signal_build_id": signal_id,
                "initial_equity": request["initial_equity"],
            }
            compile_path = (
                self.runtime_root / "futures" / "validation" / experiment_id / f"{name}.json"
            )
            self._atomic_json(compile_path, compile_request)
            compiled[name] = self.compiler.build(compile_path)
        return compiled

    def _run_compiled(
        self,
        request: dict[str, Any],
        compiled: dict[str, FuturesBacktestInputBuildResult],
    ) -> dict[str, dict[str, Any]]:
        summaries: dict[str, dict[str, Any]] = {}
        for name, input_build in compiled.items():
            backtest = FuturesBacktestService(
                self.config,
                self.curated_root,
                self.reports_root,
            ).build(input_build.input_path)
            if not backtest.passed:
                raise ValueError(f"Backtest quality gate failed for scenario: {name}")
            summaries[name] = self._metrics(backtest.output_dir, float(request["initial_equity"]))
            summaries[name]["input_build_id"] = input_build.build_id
            summaries[name]["backtest_build_id"] = backtest.build_id
        return summaries

    @staticmethod
    def _metrics(output_dir: Path, initial_equity: float) -> dict[str, Any]:
        accounts = pl.read_parquet(output_dir / "accounts.parquet").sort("trade_date")
        if accounts.is_empty():
            raise ValueError("Futures validation backtest produced no account rows.")
        equities = [float(value) for value in accounts.get_column("equity").to_list()]
        final_equity = equities[-1]
        total_return = final_equity / initial_equity - 1
        periods = max(len(equities) - 1, 1)
        annualized_return = (
            (final_equity / initial_equity) ** (252 / periods) - 1 if final_equity > 0 else -1.0
        )
        peak = equities[0]
        maximum_drawdown = 0.0
        for equity in equities:
            peak = max(peak, equity)
            maximum_drawdown = max(maximum_drawdown, 1 - equity / peak)
        return {
            "days": len(equities),
            "final_equity": final_equity,
            "total_return": total_return,
            "annualized_return": annualized_return,
            "maximum_drawdown": maximum_drawdown,
            "margin_call_days": int(accounts.get_column("margin_call").sum()),
        }

    @staticmethod
    def _acceptance(
        protocol: StrategyProtocol,
        partition: PartitionName,
        summaries: dict[str, dict[str, Any]],
    ) -> tuple[bool, dict[str, bool]]:
        criteria = protocol.acceptance_criteria
        checks: dict[str, bool] = {
            "all_scenarios_completed": len(summaries) == len(protocol.execution["scenarios"]),
        }
        baseline = summaries.get("baseline")
        if baseline is not None and "maximum_drawdown_max" in criteria:
            checks["maximum_drawdown"] = baseline["maximum_drawdown"] <= float(
                criteria["maximum_drawdown_max"]
            )
        return_keys = {
            PartitionName.DEVELOPMENT: "development_annualized_return_min",
            PartitionName.VALIDATION: "validation_annualized_return_min",
            PartitionName.HOLDOUT: "holdout_annualized_return_min",
        }
        return_key = return_keys[partition]
        if baseline is not None and return_key in criteria:
            checks["baseline_annualized_return"] = baseline["annualized_return"] >= float(
                criteria[return_key]
            )
        scenario_checks = {
            "double_cost": "double_cost_annualized_return_min",
            "delayed_execution_1d": "delayed_execution_annualized_return_min",
            "slower_roll": "slower_roll_annualized_return_min",
            "margin_up_50pct": "margin_up_50pct_annualized_return_min",
        }
        for scenario, key in scenario_checks.items():
            if scenario in summaries and key in criteria:
                checks[f"{scenario}_annualized_return"] = summaries[scenario][
                    "annualized_return"
                ] >= float(criteria[key])
        if "margin_up_50pct" in summaries and "margin_stress_insolvency_days_max" in criteria:
            checks["margin_stress_insolvency"] = summaries["margin_up_50pct"][
                "margin_call_days"
            ] <= int(criteria["margin_stress_insolvency_days_max"])
        neighbors = [name for name in ("faster_trend", "slower_trend") if name in summaries]
        if neighbors and "positive_parameter_neighbors_min" in criteria:
            checks["positive_parameter_neighbors"] = sum(
                summaries[name]["annualized_return"] > 0 for name in neighbors
            ) >= int(criteria["positive_parameter_neighbors_min"])
        return all(checks.values()), checks

    @staticmethod
    def _verify_partition_data_version(
        protocol: StrategyProtocol,
        partition: PartitionName,
        actual: str,
    ) -> None:
        if partition == PartitionName.DEVELOPMENT:
            return
        expected = protocol.partition_data_versions.get(partition)
        if expected is None:
            raise ValueError(
                f"Frozen protocol has no pinned {partition.value} data version; "
                "the partition remains sealed."
            )
        if expected != actual:
            raise ValueError(f"Frozen {partition.value} data version does not match inputs.")

    @staticmethod
    def _require_partition_data_version(
        protocol: StrategyProtocol,
        partition: PartitionName,
    ) -> None:
        if partition == PartitionName.DEVELOPMENT:
            return
        if partition not in protocol.partition_data_versions:
            raise ValueError(
                f"Frozen protocol has no pinned {partition.value} data version; "
                "the partition remains sealed."
            )

    @staticmethod
    def _load_protocol(path: Path) -> StrategyProtocol:
        protocol = StrategyProtocol.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if protocol.status != ProtocolStatus.FROZEN:
            raise ValueError("Futures validation protocol must be frozen.")
        if protocol.content_hash != protocol.calculated_hash():
            raise ValueError("Frozen futures validation protocol hash mismatch.")
        return protocol

    def _strategy_commit(self) -> str:
        paths = (
            "src/qtrade/futures/trend.py",
            "src/qtrade/futures/trend_buffer.py",
            "src/qtrade/futures/trend_risk.py",
            "src/qtrade/futures/sectors.py",
        )
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%h", "--", *paths],
                cwd=self.project_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        return result.stdout.strip() or "unknown"

    @staticmethod
    def _write_report(path: Path, payload: dict[str, Any]) -> None:
        lines = [
            "# Futures Strategy Validation",
            "",
            f"- Experiment: `{payload['experiment_id']}`",
            f"- Protocol: `{payload['protocol_id']}`",
            f"- Partition: {payload['partition']}",
            f"- Data version: `{payload['data_version']}`",
            f"- Accepted: {payload['accepted']}",
            "",
            "## Checks",
            "",
            *[
                f"- {'PASS' if passed else 'FAIL'}: `{name}`"
                for name, passed in payload["checks"].items()
            ],
            "",
            "## Scenarios",
            "",
            *[
                f"- `{name}`: annualized={values['annualized_return']:.4f}, "
                f"max_drawdown={values['maximum_drawdown']:.4f}"
                for name, values in payload["scenarios"].items()
            ],
            "",
        ]
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        os.replace(temporary, path)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
