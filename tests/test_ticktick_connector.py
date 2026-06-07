from datetime import date
from typing import Any

import httpx
import pytest

from productivity_agent.config import Settings
from productivity_agent.connectors.ticktick import TickTickConnector
from productivity_agent.models import NormalizedTask, TaskSource, TaskStatus
from productivity_agent.storage import JsonStateStore


class CapturingClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append({"method": method, "url": url, **kwargs})
        return httpx.Response(200, json={}, request=httpx.Request(method, url))


@pytest.mark.asyncio
async def test_ticktick_reschedule_updates_start_and_due_to_same_all_day_date(tmp_path) -> None:
    client = CapturingClient()
    connector = TickTickConnector(
        settings=Settings(
            TICKTICK_ACCESS_TOKEN="token",
            TIMEZONE="Europe/Moscow",
            APP_STATE_PATH=tmp_path / "state.json",
        ),
        state_store=JsonStateStore(tmp_path / "state.json"),
        client=client,
    )
    task = NormalizedTask(
        id="task-1",
        source=TaskSource.TICKTICK,
        title="Подготовить отчет",
        status=TaskStatus.TODO,
        deadline=date(2026, 5, 26),
        project_id="project-1",
    )

    await connector.reschedule_task(task, date(2026, 5, 30))

    payload = client.requests[0]["json"]
    assert payload["startDate"] == "2026-05-30T00:00:00+0300"
    assert payload["dueDate"] == "2026-05-30T00:00:00+0300"
    assert payload["isAllDay"] is True
    assert payload["timeZone"] == "Europe/Moscow"


@pytest.mark.asyncio
async def test_ticktick_reschedule_preserves_task_time_without_creating_range(tmp_path) -> None:
    client = CapturingClient()
    connector = TickTickConnector(
        settings=Settings(
            TICKTICK_ACCESS_TOKEN="token",
            TIMEZONE="Europe/Moscow",
            APP_STATE_PATH=tmp_path / "state.json",
        ),
        state_store=JsonStateStore(tmp_path / "state.json"),
        client=client,
    )
    task = NormalizedTask(
        id="task-1",
        source=TaskSource.TICKTICK,
        title="Позвонить врачу",
        status=TaskStatus.TODO,
        deadline=date(2026, 5, 26),
        time="14:30",
        project_id="project-1",
    )

    await connector.reschedule_task(task, date(2026, 5, 30))

    payload = client.requests[0]["json"]
    assert payload["startDate"] == "2026-05-30T14:30:00+0300"
    assert payload["dueDate"] == "2026-05-30T14:30:00+0300"
    assert payload["isAllDay"] is False
