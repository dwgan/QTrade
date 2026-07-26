from datetime import date
from pathlib import Path
from types import SimpleNamespace

from qtrade.domain import Dataset
from qtrade.pipeline.models import StepStatus
from qtrade.pipeline.service import DailyPipelineService


class SuccessfulService:
    def __init__(self, tmp_path: Path, name: str) -> None:
        self.path = tmp_path / f"{name}.json"

    def run(self, as_of_date: date):
        self.path.write_text(as_of_date.isoformat(), encoding="utf-8")
        return SimpleNamespace(json_path=self.path)


class OriginAwareService(SuccessfulService):
    supports_signal_origin = True

    def __init__(self, tmp_path: Path, name: str) -> None:
        super().__init__(tmp_path, name)
        self.origins: list[str] = []

    def run(self, as_of_date: date, signal_origin: str | None = None):
        if signal_origin is not None:
            self.origins.append(signal_origin)
        return super().run(as_of_date)


class FakeDashboard:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "index.html"

    def build(self, as_of_date: date) -> Path:
        self.path.write_text(as_of_date.isoformat(), encoding="utf-8")
        return self.path


class FailingDataService:
    def update(self, as_of_date: date, datasets: list[Dataset]):
        item = SimpleNamespace(dataset=datasets[0], status="failed")
        return SimpleNamespace(succeeded=False, datasets=[item])


class MustNotRun:
    def run(self, as_of_date: date):
        raise AssertionError(f"Analysis unexpectedly ran for {as_of_date}")


def _pipeline(
    tmp_path: Path,
    data_service,
    analysis_service,
) -> DailyPipelineService:
    return DailyPipelineService(
        data_service=data_service,
        market_service=analysis_service,
        industry_service=analysis_service,
        factor_service=analysis_service,
        observation_service=analysis_service,
        dashboard_builder=FakeDashboard(tmp_path),
        reports_root=tmp_path / "reports",
    )


def test_pipeline_runs_all_steps_from_existing_data(tmp_path: Path) -> None:
    service = SuccessfulService(tmp_path, "analysis")

    result = _pipeline(tmp_path, None, service).run(
        date(2026, 7, 24),
        [Dataset.DAILY_PRICES],
        skip_data=True,
    )

    assert result.run.status == "success"
    assert [step.status for step in result.run.steps] == [
        StepStatus.SKIPPED,
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
    ]
    assert result.json_path.exists()
    assert result.markdown_path.exists()


def test_pipeline_marks_past_factor_signal_as_reconstructed(
    tmp_path: Path,
) -> None:
    service = OriginAwareService(tmp_path, "analysis")

    result = _pipeline(tmp_path, None, service).run(
        date(2026, 7, 24),
        [Dataset.DAILY_PRICES],
        skip_data=True,
    )

    assert result.run.status == "success"
    assert service.origins == ["reconstructed"]


def test_pipeline_stops_dependent_steps_after_data_failure(tmp_path: Path) -> None:
    result = _pipeline(tmp_path, FailingDataService(), MustNotRun()).run(
        date(2026, 7, 24),
        [Dataset.DAILY_PRICES],
    )

    statuses = {step.name: step.status for step in result.run.steps}
    assert result.run.status == "failed"
    assert statuses["data_update"] == StepStatus.FAILED
    assert statuses["market_analysis"] == StepStatus.SKIPPED
    assert statuses["industry_analysis"] == StepStatus.SKIPPED
    assert statuses["factor_analysis"] == StepStatus.SKIPPED
    assert statuses["daily_observation"] == StepStatus.SKIPPED
    assert statuses["dashboard"] == StepStatus.COMPLETED


def test_pipeline_records_unavailable_data_service(tmp_path: Path) -> None:
    service = DailyPipelineService(
        data_service=None,
        market_service=MustNotRun(),
        industry_service=MustNotRun(),
        factor_service=MustNotRun(),
        observation_service=MustNotRun(),
        dashboard_builder=FakeDashboard(tmp_path),
        reports_root=tmp_path / "reports",
        data_service_error="Missing provider token.",
    )

    result = service.run(date(2026, 7, 24), [Dataset.DAILY_PRICES])

    assert result.run.status == "failed"
    assert result.run.steps[0].status == StepStatus.FAILED
    assert result.run.steps[0].message == "Missing provider token."
