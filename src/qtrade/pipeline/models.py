from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class StepStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class PipelineStep(BaseModel):
    name: str
    status: StepStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str = ""
    artifacts: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class PipelineRun(BaseModel):
    as_of_date: date
    created_at: datetime
    finished_at: datetime
    status: str
    steps: list[PipelineStep]
