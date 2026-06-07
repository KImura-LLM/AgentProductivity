from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskSource(StrEnum):
    NOTION = "notion"
    TICKTICK = "ticktick"


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    DONE = "done"


class TaskPriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Energy(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NormalizedTask(BaseModel):
    id: str
    source: TaskSource
    title: str
    project: str | None = None
    area: str = "other"
    status: TaskStatus = TaskStatus.TODO
    priority: TaskPriority | None = None
    deadline: date | None = None
    time: str | None = None
    estimated_minutes: int | None = None
    energy: Energy | None = None
    notes: str | None = None
    url: str | None = None
    source_name: str | None = None
    source_id: str | None = None
    project_id: str | None = None
    created_time: datetime | None = None
    updated_time: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_done(self) -> bool:
        return self.status == TaskStatus.DONE

    @property
    def display_source(self) -> str:
        if self.source == TaskSource.NOTION and self.source_name:
            return f"Notion/{self.source_name}"
        if self.source == TaskSource.TICKTICK and self.source_name:
            return f"TickTick/{self.source_name}"
        return self.source.value


class SourceError(BaseModel):
    source: TaskSource
    message: str


class TaskCollection(BaseModel):
    tasks: list[NormalizedTask] = Field(default_factory=list)
    errors: list[SourceError] = Field(default_factory=list)

    def extend(self, other: TaskCollection) -> None:
        self.tasks.extend(other.tasks)
        self.errors.extend(other.errors)


class CandidateMatch(BaseModel):
    task: NormalizedTask
    score: float


class PendingAction(BaseModel):
    kind: str
    user_id: int
    summary: str
    payload: dict[str, Any]
    expires_at: datetime

    def expired(self, now: datetime) -> bool:
        return now >= self.expires_at


class EffectivenessEntry(BaseModel):
    day: date
    sleep_time: str | None = None
    wake_time: str | None = None
    wind_down_time: str | None = None
    work_finished_time: str | None = None
    focus_done: bool | None = None
    sleep_deviation_reason: str | None = None
    wake_deviation_reason: str | None = None
    notes: list[str] = Field(default_factory=list)
    updated_at: datetime


class AnalysisSnapshot(BaseModel):
    tasks: list[NormalizedTask]
    errors: list[SourceError] = Field(default_factory=list)
    generated_at: datetime
    timezone: str
