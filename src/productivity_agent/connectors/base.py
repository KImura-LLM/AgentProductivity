from __future__ import annotations

from datetime import date
from typing import Protocol

from productivity_agent.models import NormalizedTask


class SourceUnavailable(RuntimeError):
    def __init__(self, source: str, message: str) -> None:
        super().__init__(message)
        self.source = source
        self.message = message


class TaskConnector(Protocol):
    async def list_tasks(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        project: str | None = None,
        include_done: bool = False,
    ) -> list[NormalizedTask]: ...

    async def create_task(
        self,
        title: str,
        deadline: date | None = None,
        time: str | None = None,
        project: str | None = None,
        destination: str | None = None,
        notes: str | None = None,
    ) -> NormalizedTask: ...

    async def complete_task(self, task: NormalizedTask) -> NormalizedTask: ...

    async def reschedule_task(self, task: NormalizedTask, deadline: date) -> NormalizedTask: ...

    async def update_status(self, task: NormalizedTask, status: str) -> NormalizedTask: ...
