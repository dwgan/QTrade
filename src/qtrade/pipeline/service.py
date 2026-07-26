from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from qtrade.domain import Dataset
from qtrade.pipeline.models import PipelineRun, PipelineStep, StepStatus
from qtrade.pipeline.reporting import PipelineReportWriter


@dataclass(frozen=True)
class PipelineResult:
    run: PipelineRun
    json_path: Path
    markdown_path: Path


class DailyPipelineService:
    def __init__(
        self,
        data_service: Any,
        market_service: Any,
        industry_service: Any,
        factor_service: Any,
        observation_service: Any,
        dashboard_builder: Any,
        reports_root: Path,
        data_service_error: str | None = None,
    ) -> None:
        self.data_service = data_service
        self.market_service = market_service
        self.industry_service = industry_service
        self.factor_service = factor_service
        self.observation_service = observation_service
        self.dashboard_builder = dashboard_builder
        self.data_service_error = data_service_error
        self.reporter = PipelineReportWriter(reports_root)

    @staticmethod
    def _artifacts(result: Any) -> list[str]:
        return [
            str(value)
            for name in ("json_path", "markdown_path", "rankings_path")
            if (value := getattr(result, name, None)) is not None
        ]

    @staticmethod
    def _execute(
        name: str,
        operation: Callable[[], Any],
        success_message: Callable[[Any], str] | None = None,
    ) -> tuple[PipelineStep, Any | None]:
        started = datetime.now()
        try:
            result = operation()
            message = success_message(result) if success_message else "Completed."
            return (
                PipelineStep(
                    name=name,
                    status=StepStatus.COMPLETED,
                    started_at=started,
                    finished_at=datetime.now(),
                    message=message,
                    artifacts=DailyPipelineService._artifacts(result),
                ),
                result,
            )
        except Exception as exc:  # Pipeline boundary must persist failures from every step.
            return (
                PipelineStep(
                    name=name,
                    status=StepStatus.FAILED,
                    started_at=started,
                    finished_at=datetime.now(),
                    message=str(exc),
                ),
                None,
            )

    @staticmethod
    def _skipped(name: str, message: str) -> PipelineStep:
        return PipelineStep(name=name, status=StepStatus.SKIPPED, message=message)

    @staticmethod
    def _status(steps: list[PipelineStep]) -> str:
        return "failed" if any(step.status == StepStatus.FAILED for step in steps) else "success"

    def run(
        self,
        as_of_date: date,
        datasets: list[Dataset],
        skip_data: bool = False,
    ) -> PipelineResult:
        created_at = datetime.now()
        steps: list[PipelineStep] = []
        data_ok = True
        if skip_data:
            steps.append(self._skipped("data_update", "Skipped by command option."))
        elif self.data_service is None:
            steps.append(
                PipelineStep(
                    name="data_update",
                    status=StepStatus.FAILED,
                    message=self.data_service_error or "Data service is unavailable.",
                )
            )
            data_ok = False
        else:
            data_step, update_result = self._execute(
                "data_update",
                lambda: self.data_service.update(as_of_date, datasets),
                lambda value: (
                    f"{sum(item.status == 'completed' for item in value.datasets)}/"
                    f"{len(value.datasets)} datasets completed."
                ),
            )
            if update_result is not None and not update_result.succeeded:
                failed = [
                    item.dataset.value
                    for item in update_result.datasets
                    if item.status != "completed"
                ]
                data_step.status = StepStatus.FAILED
                data_step.message = "Dataset update failed: " + ", ".join(failed)
            steps.append(data_step)
            data_ok = data_step.status == StepStatus.COMPLETED

        analyses = (
            ("market_analysis", self.market_service),
            ("industry_analysis", self.industry_service),
            ("factor_analysis", self.factor_service),
        )
        factor_ok = False
        for name, service in analyses:
            if not data_ok:
                step = self._skipped(name, "Skipped because data update failed.")
            else:
                def run_analysis(service=service):
                    return service.run(as_of_date)

                if name == "factor_analysis" and getattr(
                    service, "supports_signal_origin", False
                ):
                    origin = (
                        "live_observed"
                        if as_of_date == date.today()
                        else "reconstructed"
                    )
                    def run_analysis(service=service, origin=origin):
                        return service.run(
                            as_of_date,
                            signal_origin=origin,
                        )

                step, _ = self._execute(
                    name,
                    run_analysis,
                )
            steps.append(step)
            if name == "factor_analysis":
                factor_ok = step.status == StepStatus.COMPLETED

        if factor_ok:
            observation_step, _ = self._execute(
                "daily_observation",
                lambda: self.observation_service.run(as_of_date),
            )
        else:
            observation_step = self._skipped(
                "daily_observation",
                "Skipped because factor analysis did not complete.",
            )
        steps.append(observation_step)

        preliminary = PipelineRun(
            as_of_date=as_of_date,
            created_at=created_at,
            finished_at=datetime.now(),
            status=self._status(steps),
            steps=steps,
        )
        self.reporter.write(preliminary)
        dashboard_step, dashboard_path = self._execute(
            "dashboard",
            lambda: self.dashboard_builder.build(as_of_date),
        )
        if dashboard_path is not None:
            dashboard_step.artifacts = [str(dashboard_path)]
        steps.append(dashboard_step)
        final = PipelineRun(
            as_of_date=as_of_date,
            created_at=created_at,
            finished_at=datetime.now(),
            status=self._status(steps),
            steps=steps,
        )
        json_path, markdown_path = self.reporter.write(final)
        return PipelineResult(final, json_path, markdown_path)
