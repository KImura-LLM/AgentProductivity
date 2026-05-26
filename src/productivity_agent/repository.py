from __future__ import annotations

import logging
from datetime import date

from rapidfuzz import fuzz

from productivity_agent.connectors.base import SourceUnavailable
from productivity_agent.connectors.notion import NotionConnector
from productivity_agent.connectors.ticktick import TickTickConnector
from productivity_agent.models import (
    CandidateMatch,
    NormalizedTask,
    SourceError,
    TaskCollection,
    TaskSource,
)

logger = logging.getLogger(__name__)


class TaskRepository:
    def __init__(self, notion: NotionConnector, ticktick: TickTickConnector) -> None:
        self.notion = notion
        self.ticktick = ticktick

    async def collect(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        project: str | None = None,
        include_done: bool = False,
        sources: set[TaskSource] | None = None,
    ) -> TaskCollection:
        collection = TaskCollection()
        if sources is None or TaskSource.NOTION in sources:
            await self._collect_source(collection, TaskSource.NOTION, date_from, date_to, project, include_done)
        if sources is None or TaskSource.TICKTICK in sources:
            await self._collect_source(collection, TaskSource.TICKTICK, date_from, date_to, project, include_done)
        return collection

    async def find_candidates(self, query: str, limit: int = 5) -> list[CandidateMatch]:
        collection = await self.collect(include_done=False)
        lowered = query.lower().strip()
        matches: list[CandidateMatch] = []
        for task in collection.tasks:
            title = task.title.lower()
            score = float(fuzz.token_set_ratio(lowered, title))
            if lowered and lowered in title:
                score = max(score, 95.0)
            if score >= 50:
                matches.append(CandidateMatch(task=task, score=score))
        return sorted(matches, key=lambda item: item.score, reverse=True)[:limit]

    async def create_task(
        self,
        source: TaskSource,
        title: str,
        deadline: date | None,
        time: str | None,
        destination: str | None = None,
        notes: str | None = None,
    ) -> NormalizedTask:
        connector = self._connector(source)
        return await connector.create_task(title, deadline=deadline, time=time, destination=destination, notes=notes)

    async def complete_task(self, task: NormalizedTask) -> NormalizedTask:
        return await self._connector(task.source).complete_task(task)

    async def reschedule_task(self, task: NormalizedTask, deadline: date) -> NormalizedTask:
        return await self._connector(task.source).reschedule_task(task, deadline)

    async def update_status(self, task: NormalizedTask, status: str) -> NormalizedTask:
        return await self._connector(task.source).update_status(task, status)

    async def _collect_source(
        self,
        collection: TaskCollection,
        source: TaskSource,
        date_from: date | None,
        date_to: date | None,
        project: str | None,
        include_done: bool,
    ) -> None:
        try:
            tasks = await self._connector(source).list_tasks(
                date_from=date_from,
                date_to=date_to,
                project=project,
                include_done=include_done,
            )
            collection.tasks.extend(tasks)
        except SourceUnavailable as exc:
            logger.warning("%s source unavailable: %s", source.value, exc.message)
            collection.errors.append(SourceError(source=source, message=exc.message))

    def _connector(self, source: TaskSource) -> NotionConnector | TickTickConnector:
        if source == TaskSource.NOTION:
            return self.notion
        if source == TaskSource.TICKTICK:
            return self.ticktick
        raise ValueError(f"Unsupported source: {source}")
